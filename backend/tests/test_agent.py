"""Safe operator agent (Engine Assistant commands), the AI API budget, de-duplication/cache, and the policy for an
unavailable AI. The agent inspects, plans safe actions, runs them only after approval, verifies and records them, and
never changes code, LIVE, risk limits, trading controls or orders."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.claude_agent import REVIEW_SCHEMA, Review
from app.ai import gateway
from app.db.models import AuditLog, TradeOrder, TradingSignal
from app.db.models_ai import AIControl, AIUsage
from app.db.models_intel import LearningJob
from app.db.models_opportunity import AgentTask
from app.db.session import session_scope
from app.scanner.service import scan_once
from app.services import agent, assistant, incidents
from app.services import trading as tr
from app.workers import main as worker
from tests.test_ai_gateway import REVIEW_OK, Scripted, keys, provider, route  # noqa: F401 (fixture)
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto


@pytest.mark.parametrize("command,wf", [
    ("Audit my entire trading engine.", "audit"),
    ("Find and repair all errors.", "repair"),
    ("Check whether my engine is ready for autonomous trading.", "readiness"),
    ("Inspect all connected broker accounts.", "accounts"),
    ("Check all available trading symbols.", "symbols"),
    ("Find missing features required for high-quality trading.", "capabilities"),
    ("Test the engine using the connected DEMO account.", "demo_test"),
    ("Prepare the engine for LIVE trading.", "live_prep"),
    ("Complete the current upgrade and hand control back to the engine.", "handover"),
])
def test_commands_select_the_workflow(command, wf):
    assert agent.is_command(command) and agent.workflow_for(command)[0] == wf


def test_questions_are_not_commands():
    for q in ("why was signal #3 not executed?", "engine status", "what should I do next?"):
        assert not agent.is_command(q)


def state(db):
    st = tr.get_state(db)
    return (st.active_mode, st.auto_trading, st.kill_switch, st.risk_lock, st.ai_mode, db.query(TradeOrder).count())


def test_audit_inspects_everything_and_changes_nothing(stack):
    with session_scope() as db:
        before = state(db)
        t = agent.start(db, "Audit my entire trading engine.", None)
        titles = {f["title"] for f in t.findings}
        assert set(t.report["checks"]) == set(agent.CHECKS)
        assert "The trading worker is stopped" in titles and "Auto trading is OFF" in titles
        assert all({"component", "severity", "evidence", "impact", "fix_kind", "fix"} <= set(f) for f in t.findings)
        assert state(db) == before, "inspection never changes the engine"
        text = agent.describe(t)
        assert f"task #{t.id}" in text and "Findings:" in text


def test_repair_plan_runs_only_after_approval_and_is_verified(stack):
    with session_scope() as db:
        db.add(LearningJob(job_type="experiments", idempotency_key="stuck-a", status="RUNNING",
                           started_at=datetime.now(UTC) - timedelta(hours=5)))
        inc = incidents.record(db, "execution", "warning", "Unknown broker symbol FOO.r")
        t = agent.start(db, "Find and repair all errors.", None)
        actions = {(p["action"], p["target"]) for p in t.plan}
        assert ("recover_stale_jobs", None) in actions and ("incident_repair", inc.id) in actions
        assert t.status == "AWAITING_APPROVAL"
        assert db.scalar(select(LearningJob.status).where(LearningJob.idempotency_key == "stuck-a")) == "RUNNING"
        agent.approve(db, t, None)
        assert t.status == "DONE" and all(s["status"] == "DONE" for s in t.plan)
        assert db.scalar(select(LearningJob.status).where(LearningJob.idempotency_key == "stuck-a")) == "INTERRUPTED"
        jobs = next(s for s in t.plan if s["action"] == "recover_stale_jobs")
        assert jobs["verified"] is True and "Learner job experiments stuck" in t.report["resolved"]
        with pytest.raises(ValueError):
            agent.approve(db, t, None)                              # never twice


def test_trading_controls_are_never_in_a_plan(stack):
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch, st.kill_reason = True, "test"
        st.risk_lock, st.risk_lock_reason = True, "daily loss"
        t = agent.start(db, "Find and repair all errors.", None)
        ctrl = [f for f in t.findings if f["component"] == "controls" and f["severity"] == "critical"]
        assert ctrl and all(f["fix_kind"] == "user" for f in ctrl)
        assert not any(p["action"] not in agent.ACTIONS for p in t.plan)
        if t.plan:
            agent.approve(db, t, None)
        assert tr.get_state(db).kill_switch and tr.get_state(db).risk_lock


def test_live_preparation_reports_and_never_enables_live(stack):
    with session_scope() as db:
        t = agent.start(db, "Prepare the engine for LIVE trading.", None)
        assert t.workflow == "live_prep" and "never enables LIVE" in t.report["note"]
        assert t.report["live_readiness"]["ready"] is False and tr.get_state(db).active_mode == "DEMO"


def test_symbols_and_handover_reports(stack):
    with session_scope() as db:
        s = agent.start(db, "Check all available trading symbols.", None)
        rep = s.report["symbols"][0]
        assert rep["symbols"] == 2 and rep["tradable"] == 2 and set(rep["by_category"]) == {"forex", "metals"}
        hnd = agent.start(db, "Complete the current upgrade and hand control back to the engine.", None)
        for k in ("runtime", "ai_usage", "symbol_discovery", "repairs_executed", "handover", "not_done_by_the_agent"):
            assert k in hnd.report, k
        assert "operates by itself" in hnd.report["handover"]


def test_chat_command_and_approval_api(client, stack):
    login(client, "viewer")
    r = client.post("/api/assistant/ask", json={"question": "Audit my entire trading engine"}, headers=h(client)).json()
    assert r["intent"] == "command" and r["task"]["workflow"] == "audit"
    tid = r["task"]["id"]
    assert client.get(f"/api/agent/tasks/{tid}").json()["id"] == tid
    assert client.post(f"/api/agent/tasks/{tid}/approve", headers=h(client)).status_code == 403
    with session_scope() as db:
        incidents.record(db, "execution", "warning", "Unknown broker symbol BAR.r")
    login(client, "admin")
    r = client.post("/api/assistant/ask", json={"question": "fix all errors"}, headers=h(client)).json()
    tid = r["task"]["id"]
    assert r["task"]["status"] == "AWAITING_APPROVAL"
    out = client.post(f"/api/agent/tasks/{tid}/approve", headers=h(client)).json()
    assert out["status"] == "DONE" and "Executed:" in out["answer"]
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "AGENT_TASK_APPROVED"))


# ---- AI budget, cache, unavailable policy ------------------------------------------------------------------------------
def run_review(fake, ttl=900):
    return gateway.run("candidate_review", "s", {"c": 1}, REVIEW_SCHEMA, Review, provider_factory=fake.factory,
                       sleep=lambda s: None, cache_ttl=ttl)


def test_identical_requests_are_answered_from_the_cache(keys):
    a = provider("p", price=1.0)
    route("candidate_review", (a, "m"))
    fake = Scripted({"p": [dict(REVIEW_OK)]})                    # one answer only: a second call would fail
    run_review(fake)
    out, meta = run_review(fake)
    assert meta["cached"] is True and out.decision == "BUY"
    with session_scope() as db:
        assert [u.status for u in db.scalars(select(AIUsage).order_by(AIUsage.id))] == ["ok", "cached"]


def test_the_budget_stops_ai_calls_and_the_engine_continues(keys):
    a = provider("p", price=1000.0)
    route("candidate_review", (a, "m"))
    with session_scope() as db:
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           daily_budget_usd=0.001))
    gateway.run("candidate_review", "s", {"c": 2}, REVIEW_SCHEMA, Review,
                provider_factory=Scripted({"p": [dict(REVIEW_OK)]}).factory, sleep=lambda s: None)
    assert gateway.budget_status()["exhausted"]
    with pytest.raises(gateway.GatewayError) as e:
        gateway.run("candidate_review", "s", {"c": 3}, REVIEW_SCHEMA, Review,
                    provider_factory=Scripted({"p": [dict(REVIEW_OK)]}).factory, sleep=lambda s: None)
    assert "budget" in str(e.value)


def scan_ai_mode(scan, monkeypatch, policy):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = "AI"
        db.merge(AIControl(id=1, single_active=False, active_model="", fallback_enabled=False, fallback_model="",
                           ai_unavailable_policy=policy))
    scan_once(bridge, None, worker.auto_execute_factory(bridge))       # no AI provider: AI unavailable
    with session_scope() as db:
        reviews = {(s.ai_review or {}).get("decision") for s in db.scalars(select(TradingSignal)) if s.decision == "BUY"}
        orders = db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED")).all()
    return reviews, orders, fake


def test_unavailable_ai_pauses_by_default(scan, monkeypatch):
    reviews, orders, fake = scan_ai_mode(scan, monkeypatch, "pause")
    assert reviews <= {"UNREVIEWED", None} and not orders and not fake.positions


def test_unavailable_ai_with_engine_only_policy_keeps_trading(scan, monkeypatch):
    reviews, orders, fake = scan_ai_mode(scan, monkeypatch, "engine_only")
    assert "SKIPPED" in reviews and orders and fake.positions, "the engine continues without AI through the Risk Engine"
    with session_scope() as db:
        sig = db.get(TradingSignal, orders[0].signal_id)
        assert sig.ai_review["mode"] == "AI_UNAVAILABLE"
        d = assistant.diagnose_signal(db, sig)
        assert next(c for c in d["checks"] if c["check"] == "ai")["ok"] is True


def test_a_rejection_by_the_ai_is_never_overridden_by_the_policy():
    st = type("S", (), {"auto_trading": True, "auto_trading_since": None, "ai_mode": "AI"})()
    sig = type("G", (), {"decision": "BUY", "created_at": datetime.now(UTC), "ai_review": {"decision": "NO_TRADE"}})()
    assert tr.auto_execution_block(st, sig, "engine_only") is not None
    sig.ai_review = {"decision": "SKIPPED", "mode": "AI_UNAVAILABLE"}
    assert tr.auto_execution_block(st, sig, "engine_only") is None
    assert tr.auto_execution_block(st, sig, "pause") is not None


def test_budget_api(client, stack):
    login(client, "admin")
    r = client.put("/api/ai/budget", json={"daily_budget_usd": 2.5, "daily_token_limit": 100000,
                                           "ai_unavailable_policy": "engine_only"}, headers=h(client)).json()
    assert r["budget"]["daily_budget_usd"] == 2.5 and r["ai_unavailable_policy"] == "engine_only"
    assert client.put("/api/ai/budget", json={"daily_budget_usd": -1}, headers=h(client)).status_code == 422
    login(client, "trader")
    assert client.put("/api/ai/budget", json={"daily_budget_usd": 1}, headers=h(client)).status_code == 403
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "AI_BUDGET_UPDATE"))
