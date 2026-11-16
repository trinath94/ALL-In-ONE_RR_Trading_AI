"""Windows user interface data (RR Trading One Phase 17, specification Part 17): the Command Centre overview, the
Control Room's agent fields, the Broker Symbol Scanner's fields, the Learning Centre's figures, Market Analysis, the
Trading Worker view and the Backup and Recovery facts. Every value comes from live state or recorded rows."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import AnalysisResult, Symbol, TradingSignal
from app.db.models_ai import AgentRun
from app.db.models_intel import AnomalyEvent, ApprovalRecord
from app.db.session import session_scope
from app.services import trading as tr
from tests.test_adaptive import mk_symbol
from tests.test_api import client, login  # noqa: F401 (fixture)
from tests.test_trade_intelligence import accounts

NOW = datetime.now(UTC)


def seed():
    demo, _ = accounts()
    with session_scope() as db:
        sym = mk_symbol(db, demo)
        sym.scan_enabled, sym.priority = True, 3
        db.add(AnalysisResult(
            symbol_id=sym.id, timeframe="H1", bar_time=NOW, regime="TRENDING",
            structure={"price": 1.1, "spread_points": 12.0, "htf_bias": 1, "news": "CLEAR", "data_caps": ["tick_volume"],
                       "unavailable": ["vol_real_delta"], "peer_problems": [], "regime": {"primary": "TRENDING", "tags": []},
                       "sessions": ["LONDON"], "atr": 0.001, "evaluation": {"evaluated": 10}},
            strategy_outputs=[{"strategy": "A", "direction": "BUY", "strength": 0.7, "component": "trend", "entry": 1.1,
                               "stop_loss": 1.09, "reasons": ["x"]}, {"strategy": "B", "status": "error"}],
            score={"decision": "WAIT", "grade": "B", "score": 60, "direction": "BUY", "strategies": [{"strategy": "A"}],
                   "intelligence": {"consensus": {"decision": "BUY", "agreement": 0.75}},
                   "fusion": {"decision": "WAIT", "evidence": "CALIBRATED", "benchmark": {"decision": "WAIT"},
                              "probability": {"value": 0.58, "samples": 64, "source": "x", "kind": "k"}}}))
        for i, (dec, review, minutes) in enumerate([("BUY", "BUY", 5), ("BUY", None, 5), ("WAIT", "BUY", 5), ("SELL", "BUY", 5)]):
            db.add(TradingSignal(symbol_id=sym.id, decision=dec, grade="A", timeframe="H1", score=70, strategies=[],
                                 regime="TRENDING", explanation="", ai_review={"decision": review} if review else None,
                                 expires_at=NOW + timedelta(minutes=30), created_at=NOW - timedelta(minutes=minutes - i)))
        db.add(AnomalyEvent(symbol="EURUSD.r", kind="PRICE_GAP", severity="critical", explanation="gap", blocks_entries=True))
        db.add(AgentRun(cycle_id="c1", agent_id="market_regime", agent_version="1.0", task="scan_cycle",
                        input_refs={"refs": ["EURUSD.r:H1", "EURUSD.r:H1", "XAUUSD.r:H4"], "units": 3}, output_schema="x",
                        output={"regime_TRENDING": 1}, status="ok", started_at=NOW, completed_at=NOW, duration_ms=3.2,
                        evidence={}, errors=[]))
        db.add(AgentRun(cycle_id="c2", agent_id="news_events", agent_version="1.0", task="scan_cycle", input_refs={},
                        output_schema="x", output={}, status="error", started_at=NOW, completed_at=NOW, duration_ms=1,
                        evidence={}, errors=["feed down"]))
        db.add(ApprovalRecord(entity_type="model", entity_id=1, action="rollback", from_status="PRODUCTION",
                              to_status="RETIRED", notes="rolled back to v1"))
    return demo


def test_command_centre_overview(client):
    seed()
    login(client, "viewer")
    o = client.get("/api/dashboard/overview").json()
    assert o["services"]["backend"]["status"] == "UP" and o["services"]["database"]["status"] == "UP"
    assert o["services"]["redis"]["status"] == "UP"
    assert o["services"]["broker_connection"]["status"] == "DOWN", "no verified active account and no MT5 heartbeat"
    assert set(o["roles"]) == {"monitor", "analysis", "learner", "trading_worker"}
    assert o["symbols_discovered"] == 1 and o["symbols_scanning"] == 1 and o["account_mode"] == "DEMO"
    assert isinstance(o["active_strategies"], int)
    assert o["agents"]["active_last_hour"] == 2 and o["agents"]["runs_last_hour"] == {"ok": 1, "error": 1}
    assert len(o["latest_signals"]) == 4 and o["recent_executions"] == []
    assert any(a["kind"] == "ANOMALY PRICE_GAP" for a in o["critical_alerts"])
    assert o["overall_health"] == "DOWN", "a down service makes the whole DOWN, never 'healthy'"
    assert any(a["kind"] == "BROKER_CONNECTION DOWN" for a in o["critical_alerts"])
    assert o["recent_critical_events"] == [], "past events are listed apart from active alerts"


def test_past_critical_events_are_grouped_and_do_not_make_the_system_unhealthy(client):
    """Found on the install: a cleared kill switch and repeated past disconnections were shown as current critical alerts
    and marked the system DEGRADED. They are now grouped under recent events; only active conditions count."""
    from app.db.models import Notification, SystemEvent
    with session_scope() as db:
        for _ in range(3):
            db.add(Notification(kind="MT5_DISCONNECTED", severity="critical", title="MT5 connector DISCONNECTED"))
        db.add(SystemEvent(component="execution", level="critical", message="Kill switch: test (cleared since)"))
    login(client, "viewer")
    o = client.get("/api/dashboard/overview").json()
    rec = {g["kind"]: g["count"] for g in o["recent_critical_events"]}
    assert rec == {"MT5_DISCONNECTED": 3, "SYSTEM execution": 1}
    assert not any(a["kind"].startswith("MT5_DISCONNECTED") or a["kind"].startswith("SYSTEM") for a in o["critical_alerts"])


def test_control_room_agent_fields(client):
    seed()
    login(client, "viewer")
    agents = {a["id"]: a for a in client.get("/api/agents").json()}
    assert agents["market_regime"]["markets"] == ["EURUSD.r H1", "XAUUSD.r H4"] and agents["market_regime"]["health"] == "healthy"
    assert agents["news_events"]["health"] == "failing" and agents["news_events"]["last_run"]["errors"] == ["feed down"]
    assert agents["trade_execution"]["health"] == "idle (no run in 24 h)" and agents["trade_execution"]["markets"] == []


def test_scanner_fields(client):
    seed()
    login(client, "viewer")
    r = client.get("/api/scanner").json()[0]
    assert r["bid"] == 1.1 and r["ask"] == round(1.1 + 12 * 0.00001, 8) and r["spread_points"] == 12.0
    assert (r["consensus"], r["consensus_agreement"]) == ("BUY", 0.75)
    assert r["probability"] == 0.58 and r["probability_evidence"] == "CALIBRATED"
    assert r["data_quality"]["status"] == "PARTIAL" and r["data_quality"]["strategies_without_data"] == 1
    assert r["scan_priority"] == 3 and r["htf_bias"] == 1 and r["regime"] == "TRENDING" and r["score"] == 60


def test_learning_centre_figures(client):
    seed()
    login(client, "viewer")
    ov = client.get("/api/learning/overview?mode=DEMO").json()
    for k in ("breakeven", "unattributed", "training_jobs_7d", "learning_memory", "rollbacks"):
        assert k in ov, k
    assert ov["rollbacks"][0]["notes"] == "rolled back to v1"


def test_market_analysis_worker_and_data(client):
    seed()
    login(client, "viewer")
    a = client.get("/api/analysis/latest?symbol=EURUSD.r&timeframe=H1").json()
    assert a["available"] and [s["strategy"] for s in a["setups"]] == ["A"], "errors are not setups"
    assert a["engine"]["decision"] == "WAIT" and a["fusion"]["decision"] == "WAIT" and a["regime"]["primary"] == "TRENDING"
    assert client.get("/api/analysis/latest?symbol=EURUSD.r&timeframe=M5").json()["available"] is False
    assert client.get("/api/analysis/latest?symbol=NOPE&timeframe=H1").status_code == 404
    w = client.get("/api/worker/status").json()
    assert w["auto_trading"] is False and {c["status"] for c in w["candidates"]} == {"auto trading is OFF", "WAIT / NO_TRADE are never executed"}
    with session_scope() as db:
        st = tr.get_state(db)
        st.auto_trading, st.auto_trading_since = True, NOW - timedelta(minutes=10)
    reasons = {c["decision"] + ":" + str(c["review"]): c["status"] for c in client.get("/api/worker/status").json()["candidates"]}
    assert reasons["BUY:BUY"].startswith("eligible") and reasons["BUY:None"] == "not confirmed by the AI review"
    assert reasons["SELL:BUY"] == "AI review BUY does not confirm SELL" and reasons["WAIT:BUY"] == "WAIT / NO_TRADE are never executed"
    with session_scope() as db:
        tr.get_state(db).auto_trading_since = NOW + timedelta(minutes=1)
    assert all(c["status"] in ("produced before auto trading was switched on", "WAIT / NO_TRADE are never executed")
               for c in client.get("/api/worker/status").json()["candidates"])
    d = client.get("/api/system/data").json()
    assert d["schema_version"] and d["database_bytes"] > 0 and d["records"]["trading_signals"] == 4
    with session_scope() as db:
        assert db.scalar(select(Symbol)).priority == 3


def test_chart_endpoints_report_a_connector_outage_as_502_not_a_server_error(client):
    """Phase 17 (found by the page check with the connector offline): /tick and /overlays raised unhandled errors (500)
    while /rates answered 502; all three now answer 502 with the reason."""
    from app.api.routes import trading as trading_routes
    from app.execution.bridge import BridgeUnavailable
    from app.main import app

    class Offline:
        def rates(self, *a, **k):
            raise BridgeUnavailable("RR MT5 Connector agent 1 is offline")
        tick = rates

        def close(self):
            pass
    app.dependency_overrides[trading_routes.bridge_dep] = lambda: Offline()
    try:
        login(client, "viewer")
        for path in ("rates?timeframe=H1", "tick", "overlays?timeframe=H1"):
            r = client.get(f"/api/market/EURUSD.r/{path}")
            assert r.status_code == 502 and "offline" in r.json()["detail"], (path, r.status_code)
    finally:
        app.dependency_overrides.pop(trading_routes.bridge_dep, None)
