"""System check agent and the end-to-end DEMO test trade, against the real MT5 connector app in front of a simulated
broker (the same path a real DEMO account uses)."""
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.models import AuditLog, ClosedPosition, OpenPosition, TradeOrder
from app.db.session import session_scope
from app.services import system_check as sc
from app.services import trading as tr
from app.workers import main as worker
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import allow_unknown_news, stack  # noqa: F401 (fixture)


def ready(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    return fake, bridge


def statuses(rep):
    return {s["key"]: s["status"] for s in rep["systems"]}


def test_check_reports_every_system_and_what_to_do(stack):
    fake, bridge = ready(stack)
    with session_scope() as db:
        rep = sc.run(db, bridge)
    assert set(statuses(rep)) == {"database", "services", "mt5", "market_data", "risk", "strategies", "ai", "incidents"}
    assert statuses(rep)["mt5"] == "ok" and statuses(rep)["database"] == "ok"
    fake.connected = False                                       # the MT5 terminal loses its broker connection
    with session_scope() as db:
        rep = sc.run(db, bridge)
    mt5 = next(s for s in rep["systems"] if s["key"] == "mt5")
    assert mt5["status"] == "failing" and rep["overall"] == "failing" and rep["can_trade"] is False
    p = next(p for p in mt5["problems"] if p["title"] == "MT5 terminal not connected")
    assert p["fix"] and p["impact"] and rep["fingerprint"]


def test_test_trade_proves_the_whole_execution_path_and_closes_the_position(stack):
    fake, bridge = ready(stack)
    with session_scope() as db:
        out = sc.test_trade(db, bridge, None)
    assert out["ok"], out
    names = [s["step"] for s in out["steps"]]
    assert names == ["DEMO mode", "DEMO account active", "Pre-flight system check", "Test symbol", "Live quote",
                     "Risk Engine approval", "Order filled by the broker", "Position visible in the engine",
                     "Position closed", "Close confirmed by the broker"]
    assert all(s["ok"] for s in out["steps"]) and out["symbol"] == "EURUSD.r"
    assert fake.send_calls == 2 and not fake.positions, "one real order, closed again"
    with session_scope() as db:
        orders = db.scalars(select(TradeOrder).order_by(TradeOrder.id)).all()
        assert [o.source for o in orders] == ["TEST", "TEST"] and not orders[0].strategy_codes
        assert float(orders[0].volume) == float(sc.pick_symbol(db).volume_min), "the broker's minimum volume"
        assert orders[0].stop_loss is not None and orders[0].risk_evaluation_id, "through the Risk Engine, with a SL"
        assert db.scalar(select(func.count(OpenPosition.id))) == 0


def test_test_trade_never_bypasses_controls_and_never_runs_in_live(stack):
    fake, bridge = ready(stack)
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch, st.kill_reason = True, "test"
    with session_scope() as db:
        out = sc.test_trade(db, bridge, None)
    step = next(s for s in out["steps"] if s["step"] == "Risk Engine approval")
    assert out["ok"] is False and step["ok"] is False and "kill switch" in step["detail"].lower()
    assert fake.send_calls == 0 and "never bypasses" in out["fix"]
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch, st.active_mode = False, "LIVE"
    with session_scope() as db:
        out = sc.test_trade(db, bridge, None)
    assert out["ok"] is False and out["steps"][0] == {**out["steps"][0], "step": "DEMO mode", "ok": False}
    assert "never placed on a LIVE account" in out["fix"] and fake.send_calls == 0


def test_test_trades_never_count_as_trading_results(stack):
    fake, bridge = ready(stack)
    with session_scope() as db:
        assert sc.test_trade(db, bridge, None)["ok"]
    with session_scope() as db:
        o = db.scalar(select(TradeOrder).where(TradeOrder.source == "TEST"))
        acct = tr.active_account(db)
        db.add(ClosedPosition(account_id=acct.id, ticket=1, symbol="EURUSD.r", direction="BUY", volume=0.01,
                              open_price=1.1, close_price=1.1, profit=-0.1, execution_id=o.execution_id, mode="DEMO",
                              opened_at=datetime.now(UTC), closed_at=datetime.now(UTC)))
        db.add(ClosedPosition(account_id=acct.id, ticket=2, symbol="EURUSD.r", direction="BUY", volume=0.01,
                              open_price=1.1, close_price=1.1, profit=1.0, execution_id=None, mode="DEMO",
                              opened_at=datetime.now(UTC), closed_at=datetime.now(UTC)))
    with session_scope() as db:
        n = db.scalar(select(func.count(ClosedPosition.id)).where(sc.not_test_trade()))
        assert n == 1, "the test trade is excluded; other closed trades still count"


def test_api_check_for_everyone_test_trade_needs_permission_and_confirmation(client, stack, monkeypatch):
    fake, bridge = ready(stack)
    monkeypatch.setattr("app.api.routes.trading.BridgeClient", lambda *a, **k: bridge)
    monkeypatch.setattr(bridge, "close", lambda: None)
    login(client, "viewer")
    rep = client.get("/api/system/check").json()
    assert rep["overall"] in ("ok", "warning", "failing") and len(rep["systems"]) == 8
    assert client.post("/api/system/test-trade", json={"confirm": True}, headers=h(client)).status_code == 403
    login(client, "trader")
    assert client.post("/api/system/test-trade", json={}, headers=h(client)).status_code == 422
    out = client.post("/api/system/test-trade", json={"confirm": True}, headers=h(client)).json()
    assert out["ok"], out
    with session_scope() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "SYSTEM_TEST_TRADE"))
