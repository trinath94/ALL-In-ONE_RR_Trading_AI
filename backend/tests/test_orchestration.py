"""Agent orchestration (RR Trading One Phase 9, specification Part 9): the closed registry of 21 agents, the typed run
contract, cycle records from the real scan, standalone records from the learner and the trading worker (only when
something happened), and the control-room API."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models_ai import AgentRun
from app.db.session import session_scope
from app.learning_engine import scheduler as ls
from app.orchestration import agents as ag
from app.orchestration import orchestrator as orch
from app.scanner.service import scan_once
from app.workers import roles
from app.workers import main as legacy
from tests.test_api import h, login, make_user
from tests.test_execution_e2e import stack  # noqa: F401 (fixture)
from tests.test_roles import allow_unknown_news, enable_auto, make_signal, orders
from tests.test_scanner_scheduling import varied

CONTRACT = {"cycle_id", "agent_id", "agent_version", "task", "input_refs", "output_schema", "output", "status",
            "started_at", "completed_at", "duration_ms", "provider", "model", "evidence", "errors"}


def runs(**where):
    with session_scope() as db:
        q = select(AgentRun).order_by(AgentRun.id)
        for k, v in where.items():
            q = q.where(getattr(AgentRun, k) == v)
        return [orch.AgentRunRecord.model_validate(r, from_attributes=True) for r in db.scalars(q)]


def test_registry_is_closed_and_only_two_agents_touch_execution():
    assert len(ag.REGISTRY) == 21
    assert {a.id for a in ag.REGISTRY if a.execution_path} == {"trade_execution", "trade_management"}
    assert all(a.kind in ("deterministic", "llm", "hybrid") for a in ag.REGISTRY)
    llm = {a.id for a in ag.REGISTRY if a.kind != "deterministic"}
    assert llm == {"signal_quality", "strategy_discovery"}, "LLMs only for review of shortlisted candidates and research"
    assert set(ag.SPECIALIST_AGENT.values()) | set(ag.LEARNER_AGENT.values()) <= set(ag.BY_ID)
    assert set(ag.LEARNER_AGENT) == {j.name for j in ls.JOBS}, "every learner job belongs to an agent"


def test_contract_refuses_unknown_agents(db_clean):
    orch.record("trade_execution", "x", started_at=datetime.now(UTC), duration_ms=1)
    orch.write([orch.AgentRunRecord(cycle_id="c", agent_id="rogue_bot", agent_version="1", task="t", output_schema="s",
                                    status="ok", started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
                                    duration_ms=1)])
    assert [r.agent_id for r in runs()] == ["trade_execution"]


@pytest.fixture
def scan(stack):
    fake, bridge = stack
    shift = [0]
    varied(fake, shift)
    yield fake, bridge, shift


def test_scan_cycle_records_each_agent_that_ran(scan):
    fake, bridge, shift = scan
    scan_once(bridge, None, lambda _id: None)
    rs = runs()
    cycles = {r.cycle_id for r in rs}
    assert len(cycles) == 1
    by = {r.agent_id: r for r in rs}
    expected = {"master_orchestrator", "symbol_scanner", "historical_performance", "original_strategy_analysis",
                "market_regime", "multi_timeframe", "news_events", "signal_quality"}
    assert expected <= set(by), set(by)
    for r in rs:
        assert set(r.model_dump()) == CONTRACT and r.agent_version == ag.BY_ID[r.agent_id].version
        assert r.completed_at >= r.started_at and r.status in ("ok", "partial")
    osa = by["original_strategy_analysis"]
    assert osa.input_refs["units"] == 6 and len(osa.input_refs["refs"]) == 6 and osa.duration_ms > 0
    assert osa.output["not_applicable"] > 0 and osa.output["no_signal"] > 0
    assert by["symbol_scanner"].output["analysed"] == 6
    assert by["master_orchestrator"].output["analysed"] == 6 and "symbol_scanner" in by["master_orchestrator"].input_refs["agents"]
    assert by["original_strategy_analysis"].evidence == {"basis": "deterministic computation"}
    assert by["signal_quality"].provider is None, "no AI provider configured: no AI step recorded"
    scan_once(bridge, None, lambda _id: None)
    assert len(runs()) == len(rs), "a cycle with nothing due records nothing"


def test_learner_job_is_recorded_as_its_agent(db_clean):
    job = ls.Job("failure_analysis", 900, 30, lambda: {"analysed": 3, "skipped": False})
    ls.run_job(job, force=True)
    bad = ls.Job("consensus_weights", 900, 30, lambda: 1 / 0)
    ls.run_job(bad, force=True)
    r1, r2 = runs()
    assert (r1.agent_id, r1.status, r1.output, r1.task) == ("post_trade_learning", "ok", {"analysed": 3}, "learner:failure_analysis")
    assert (r2.agent_id, r2.status) == ("adaptive_optimisation", "error") and "ZeroDivisionError" in r2.errors[0]


def test_trade_execution_recorded_only_when_something_happens(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert legacy.startup_checks(bridge)[0]
    sid = make_signal(datetime.now(UTC))
    started = datetime.now(UTC).replace(year=2000)
    auto = legacy.auto_execute_factory(bridge)
    enable_auto(False)
    roles.trading_cycle(auto, started)
    roles.trading_cycle(auto, started)
    assert runs(agent_id="trade_execution") == [], "re-checking while auto-trading is off is not a run"
    enable_auto(True)
    roles.trading_cycle(auto, started)
    roles.trading_cycle(auto, started)
    rs = runs(agent_id="trade_execution")
    assert orders() == 1 and len(rs) == 1
    assert rs[0].input_refs == {"signal_id": sid} and rs[0].output["order_status"] and rs[0].status == "ok"


def test_control_room_api(scan):
    fake, bridge, _ = scan
    scan_once(bridge, None, lambda _id: None)
    from app.main import app
    make_user("viewer9", "readonly")
    with TestClient(app) as c:
        login(c, "viewer9")
        rows = {a["id"]: a for a in c.get("/api/agents").json()}
        assert len(rows) == 21 and rows["original_strategy_analysis"]["runs_24h"]["ok"] == 1
        assert rows["trade_execution"]["last_run"] is None and rows["trade_execution"]["execution_path"] is True
        cyc = c.get("/api/agents/cycles").json()
        assert len(cyc) == 1 and cyc[0]["output"]["analysed"] == 6
        detail = c.get("/api/agents/runs", params={"cycle_id": cyc[0]["cycle_id"]}).json()
        assert len(detail) >= 8 and all(set(CONTRACT) <= set(d) for d in detail)
        assert c.get("/api/agents/runs", params={"agent_id": "nope"}).status_code == 404
