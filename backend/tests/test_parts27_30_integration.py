"""Parts 27-30 Phase 8: integration. The whole flow through the real code in DEMO, with only the broker (FakeMT5 behind
the real bridge) and the AI providers (scripted) simulated:

    scan -> opportunity with its full decision trace -> dual-provider consensus review -> QUALIFIED
    -> auto-execution through the Risk Engine and the journalled execution manager -> EXECUTED (risk and execution
    stages) -> simulation of the executed setup -> kill switch -> still DEMO, LIVE never touched.

Also: partial fills (Section 6, item 21), the one required validation no earlier test covered."""
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai import gateway
from app.db.models import OpenPosition, TradeOrder, TradingSignal
from app.db.models_ai import AgentRun, AITaskRoute
from app.db.models_opportunity import Opportunity, PatternDetection, SimulationRun
from app.db.session import session_scope
from app.execution import manager as em
from app.scanner.service import expire_signals, scan_once
from app.services import readiness
from app.services import trading as tr
from app.workers import main as worker
from tests.test_ai_gateway import REVIEW_OK, Scripted, keys, provider  # noqa: F401 (fixture)
from tests.test_api import h, login, make_user
from tests.test_execution_e2e import allow_unknown_news, open_eur, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_roles import enable_auto

TRACE = ["data", "market_regime", "strategies", "multi_timeframe", "timeframe_alignment", "patterns", "news",
         "fundamental", "signal_engine", "adaptive", "consensus", "probability", "anomalies", "fusion", "entry_validation",
         "exit_plan", "failure_prediction", "strategy_selection", "health", "decision", "ai_review", "risk_engine",
         "execution"]


def test_partial_fill_is_recorded_and_the_filled_volume_is_what_gets_managed(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    fake.partial_fill = 0.5
    with session_scope() as db:
        o = open_eur(db, bridge, key="pf1")
        assert o.status == "CONFIRMED" and "Partially filled" in o.status_detail, o.status_detail
        ex = db.scalar(select(em.TradeExecution).where(em.TradeExecution.order_id == o.id))
        requested = float(o.volume)
        assert ex.retcode == 10010 and float(ex.filled_volume) == round(requested * 0.5, 2) < requested
    ticket = next(iter(fake.positions))
    with session_scope() as db:
        tr.sync_positions(db, bridge, tr.active_account(db))
        pos = db.scalar(select(OpenPosition).where(OpenPosition.ticket == ticket))
        assert float(pos.volume) == round(requested * 0.5, 2), "the broker's filled volume, not the request"
        risks = tr.open_risks(db, tr.active_account(db))
        assert risks[0].volume == float(pos.volume), "the Risk Engine sees the filled volume"
    fake.partial_fill = None
    fake.positions[ticket]["price_current"] = 1.1035                       # ~1.6R: partial close and break-even
    with session_scope() as db:
        tr.sync_positions(db, bridge, tr.active_account(db))
    worker.manage_positions(bridge)
    assert fake.positions[ticket]["volume"] < round(requested * 0.5, 2), "management works on the filled volume"


def consensus_reviewer():
    a, b = provider("claude-sim", kind="anthropic"), provider("openai-sim")
    with session_scope() as db:
        db.merge(AITaskRoute(task="candidate_review", chain=[{"provider_id": a, "model": "c"},
                                                              {"provider_id": b, "model": "o"}], mode="consensus"))
    return gateway.GatewayReviewer(Scripted({"claude-sim": [dict(REVIEW_OK)] * 20,
                                             "openai-sim": [dict(REVIEW_OK)] * 20}).factory)


def test_an_inconsistent_feed_is_refused_by_the_risk_engine_even_after_ai_consensus(scan, keys, monkeypatch):
    """The test feed's bars are a day old and its quote is far from their closes: everything up to the AI consensus
    approves, and the Risk Engine still refuses every order (signal expiry, critical anomaly). Nothing reaches MT5."""
    fake, bridge, _ = scan
    force_buy(monkeypatch)
    allow_unknown_news()
    reviewer = consensus_reviewer()
    assert worker.startup_checks(bridge)[0]
    enable_auto()
    scan_once(bridge, reviewer, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        expire_signals(db)
    from app.db.models import RiskEvaluation
    with session_scope() as db:
        assert not db.scalars(select(TradeOrder)).all() and not fake.positions
        evs = db.scalars(select(RiskEvaluation)).all()
        assert evs and not any(e.approved for e in evs)
        failed = {c["name"] for e in evs for c in e.checks if not c["passed"]}
        assert {"signal_expiry", "market_anomaly"} & failed, failed
        reviewed = [x for x in db.scalars(select(TradingSignal)) if (x.ai_review or {}).get("decision") == "BUY"]
        assert reviewed, "the AI consensus had confirmed them"
        assert {o.state for o in db.scalars(select(Opportunity)) if o.signal_id in {x.id for x in reviewed}} == {"REJECTED"}


def align_feed(fake, shift):
    """Current bars, and a quote at the last bar's close (as a real broker gives), instead of the day-old test feed."""
    from app.core.config import get_settings
    shift[0] = 24
    n = get_settings().scan_bars
    orig = fake.symbol_info_tick

    def tick(name):
        t = orig(name)
        c = float(fake.copy_rates_from_pos(name, 16385, 0, n)["close"][-1])
        return t._replace(bid=c, ask=c + (t.ask - t.bid))
    fake.symbol_info_tick = tick


def test_parts27_30_flow_end_to_end_in_demo(scan, keys, monkeypatch):
    fake, bridge, shift = scan
    force_buy(monkeypatch)
    align_feed(fake, shift)
    allow_unknown_news()
    reviewer = consensus_reviewer()
    ok, report = worker.startup_checks(bridge)
    assert ok, report
    enable_auto()
    with session_scope() as db:
        assert tr.get_state(db).active_mode == "DEMO"
    scan_once(bridge, reviewer, worker.auto_execute_factory(bridge))
    with session_scope() as db:
        expire_signals(db)                                                 # the monitor's sync of opportunity states
    with session_scope() as db:
        orders = db.scalars(select(TradeOrder).where(TradeOrder.source == "AUTO", TradeOrder.status == "CONFIRMED")).all()
        assert orders, "a consensus-confirmed, Risk-Engine-approved candidate was executed"
        sig = db.get(TradingSignal, orders[0].signal_id)
        assert sig.status == "EXECUTED" and sig.ai_review["decision"] == "BUY"
        opp = db.scalar(select(Opportunity).where(Opportunity.signal_id == sig.id))
        assert opp.state == "EXECUTED", (opp.state, opp.state_reason)
        stages = [t["stage"] for t in opp.trace]
        assert stages == TRACE, stages
        review = next(t for t in opp.trace if t["stage"] == "ai_review")
        assert review["evidence"]["mode"] == "consensus" and len(review["evidence"]["consensus"]["votes"]) == 2
        assert next(t for t in opp.trace if t["stage"] == "risk_engine")["result"] == "APPROVED"
        assert "data_quality" in db.get(__import__("app.db.models", fromlist=["AnalysisResult"]).AnalysisResult,
                                        opp.analysis_id).structure
        ran = {r.agent_id for r in db.scalars(select(AgentRun))}
        new = {"fundamental_analysis", "entry_validation", "exit_optimisation", "failure_prediction", "ai_consensus",
               "pattern_recognition", "strategy_ranking"}
        assert new <= ran, new - ran
        keys_ = db.scalars(select(PatternDetection.key)).all()
        assert len(keys_) == len(set(keys_)), "no duplicate pattern records"
        oid = opp.id
    # research on the executed setup, after more bars exist; research never trades
    shift[0] = 36
    from app.api.routes.trading import bridge_dep
    from app.main import app
    app.dependency_overrides[bridge_dep] = lambda: bridge
    make_user("integ-admin", "admin")
    try:
        with TestClient(app) as c:
            login(c, "integ-admin")
            n_orders = len(fake.positions)
            r = c.post("/api/simulations", json={"kind": "scenario", "opportunity_id": oid}, headers=h(c))
            assert r.status_code == 200 and len(r.json()["result"]["rows"]) == 7, r.text
            assert len(fake.positions) == n_orders, "a simulation never touches the broker"
            assert c.get(f"/api/opportunities/{oid}").json()["state"] == "EXECUTED"
            rd = c.get("/api/live/readiness").json()
            assert rd["ready"] is False and "identity" in rd["failing"], "no LIVE account: never ready"
    finally:
        app.dependency_overrides.pop(bridge_dep, None)
    with session_scope() as db:
        assert db.scalar(select(SimulationRun.id))
        before = db.scalar(select(OpenPosition.id).limit(1))
        em.kill_switch(db, bridge, None, "integration test")
    with session_scope() as db:
        st = tr.get_state(db)
        assert st.kill_switch and not st.auto_trading and st.active_mode == "DEMO"
        assert db.scalar(select(OpenPosition.id).limit(1)) == before, "PROTECT: positions kept with their stops"
        assert readiness.report(db)["ready"] is False
    _ = datetime.now(UTC)
