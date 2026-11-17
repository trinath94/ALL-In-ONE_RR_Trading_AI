"""DEMO and LIVE separation (RR Trading One Phase 16, specification Part 16), in-process against a fake terminal that
reports a REAL account. No real LIVE trading takes place.

The whole LIVE lifecycle through the API: separate identity and records, verification against the terminal (exact
login, exact server, account type), administrator approval with typed confirmation, activation, the typed mode
switch, new entries blocked until verification and reconciliation, the LIVE risk configuration, explicit confirmation
for manual LIVE orders, revocation, and the audit trail."""
import threading
import time

import pytest
import uvicorn
from sqlalchemy import select

from app.api.routes import trading as trading_routes
from app.db.models import AuditLog, BrokerConnection, MT5Terminal, RiskConfig, RiskEvaluation, TradeOrder, TradingAccount, User
from app.db.session import session_scope
from app.execution import manager as em
from app.execution.bridge import BridgeClient, BridgeConfig
from app.main import app
from app.services import trading as tr
from app.workers import main as worker
from connector.adapter import MT5Adapter
from connector.app import ConnectorConfig, create_app
from fake_mt5 import FakeMT5
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import SECRET, free_port


@pytest.fixture
def live_terminal(db_clean, tmp_path):
    """The PC's terminal is logged in to a REAL account; the engine still has its DEMO account active."""
    fake = FakeMT5(login=2002, server="Live-Server")
    fake.trade_mode = 2
    capp = create_app(ConnectorConfig(SECRET, 2002, "Live-Server", journal_path=str(tmp_path / "j.db")),
                      MT5Adapter(fake), connect=False)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(capp, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    bridge = BridgeClient(BridgeConfig(f"http://127.0.0.1:{port}", SECRET))
    with session_scope() as db:
        term = MT5Terminal(name="t1", bridge_url=f"http://127.0.0.1:{port}")
        db.add(term)
        db.flush()
        bc = BrokerConnection(broker_name="Fake", server="Demo-Server", terminal_id=term.id)
        db.add(bc)
        db.flush()
        db.add(TradingAccount(broker_connection_id=bc.id, login=1001, server="Demo-Server", mode="DEMO",
                              is_active=True, verified=True))
        for mode in ("DEMO", "LIVE"):
            rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == mode))
            rc.params = {**rc.params, "news_unknown_blocks": False}   # the test has no news feed
    app.dependency_overrides[trading_routes.bridge_dep] = lambda: bridge
    yield fake, bridge
    app.dependency_overrides.pop(trading_routes.bridge_dep, None)
    bridge.close()
    server.should_exit = True
    th.join(timeout=5)


def state(client):
    return client.get("/api/control/state").json()


def test_live_lifecycle_end_to_end(client, live_terminal):
    fake, bridge = live_terminal
    login(client, "admin")
    # separate identity: a new record for the LIVE login; the DEMO identity can never also be LIVE
    r = client.post("/api/accounts", json={"login": 2002, "server": "Live-Server", "mode": "LIVE", "password": "pw-live"},
                    headers=h(client))
    assert r.status_code == 200 and r.json()["mode"] == "LIVE" and r.json()["live_approved"] is False
    lid = r.json()["id"]
    r = client.post("/api/accounts", json={"login": 1001, "server": "Demo-Server", "mode": "LIVE"}, headers=h(client))
    assert r.status_code == 409 and "already registered as a DEMO account" in r.text
    with session_scope() as db:
        live = db.get(TradingAccount, lid)
        demo = db.scalar(select(TradingAccount).where(TradingAccount.mode == "DEMO"))
        assert live.encrypted_password and live.encrypted_password != demo.encrypted_password and "pw-live" not in live.encrypted_password
        assert live.broker_connection_id != demo.broker_connection_id, "its own broker profile"

    # approval requires prior verification against the terminal, an administrator and the typed phrase
    assert client.post(f"/api/accounts/{lid}/approve-live", json={"confirm_text": "APPROVE 2002"}, headers=h(client)).status_code == 409
    assert client.post(f"/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client)).status_code == 409
    r = client.post(f"/api/accounts/{lid}/verify", headers=h(client))
    assert r.status_code == 200 and r.json()["verified"] is True
    login(client, "trader")
    assert client.post(f"/api/accounts/{lid}/approve-live", json={"confirm_text": "APPROVE 2002"}, headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post(f"/api/accounts/{lid}/approve-live", json={"confirm_text": "approve 2002"}, headers=h(client)).status_code == 428
    assert client.post(f"/api/accounts/{lid}/approve-live", json={"confirm_text": "APPROVE 2002"}, headers=h(client)).json()["live_approved"]
    assert client.post(f"/api/accounts/{lid}/activate", headers=h(client)).status_code == 200
    assert state(client)["mode"] == "DEMO", "approving and activating a LIVE account does not switch the mode"

    # Parts 27-30 Phase 6 (Part 29.10): the LIVE readiness checklist also requires a DEMO track record. Without it the
    # switch is refused (see tests/test_part29.py); here the DEMO account gets the required closed trades first.
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 409 and "closed DEMO trades" in r.text
    from tests.test_part29 import seed_demo_trades
    seed_demo_trades(20)

    # the typed switch: auto trading off, new entries blocked until verification and reconciliation
    login(client, "trader")
    assert client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client)).status_code == 403
    login(client, "admin")
    assert client.post("/api/control/mode", json={"mode": "LIVE"}, headers=h(client)).status_code == 428
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 200 and r.json()["mode"] == "LIVE" and r.json()["auto_trading"] is False
    assert r.json()["startup_checks_passed"] is False and r.json()["new_entries"].startswith("blocked until")
    with session_scope() as db:
        with pytest.raises(em.ExecutionBlocked, match="Startup reconciliation has not passed"):
            em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.0980, take_profit=1.1050,
                             source="MANUAL", dedupe_key="live-0", confirm_live=True)
    with session_scope() as db:
        tr.discover_symbols(db, bridge, db.get(TradingAccount, lid).broker_connection_id)
    ok, report = worker.startup_checks(bridge)
    assert ok, report

    # manual LIVE orders need explicit confirmation; the LIVE risk configuration (not DEMO's) sizes them
    with session_scope() as db:
        with pytest.raises(em.ExecutionBlocked, match="Explicit LIVE confirmation"):
            em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.0980, take_profit=1.1050,
                             source="MANUAL", dedupe_key="live-1")
    with session_scope() as db:
        o = em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.0980, take_profit=1.1050,
                             source="MANUAL", dedupe_key="live-2", confirm_live=True)
        assert o.status == "CONFIRMED" and o.account_id == lid
        ev = db.get(RiskEvaluation, o.risk_evaluation_id)
        live_risk = tr.risk_params(db, "LIVE").risk_per_trade_pct
        assert live_risk < tr.risk_params(db, "DEMO").risk_per_trade_pct
        assert ev.risk_percent <= live_risk + 1e-9

    # revoking the approval re-locks LIVE at once
    r = client.post(f"/api/accounts/{lid}/revoke-live", headers=h(client))
    assert r.status_code == 200 and r.json()["live_approved"] is False
    with session_scope() as db:
        with pytest.raises(em.ExecutionBlocked, match="not verified/approved"):
            em.open_position(db, bridge, symbol="EURUSD.r", direction="SELL", stop_loss=1.1030, take_profit=1.0950,
                             source="MANUAL", dedupe_key="live-3", confirm_live=True)
    assert client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"},
                       headers=h(client)).json()["mode"] == "LIVE", "already LIVE: no-op"

    # back to DEMO: no phrase needed, the position left on LIVE is reported
    r = client.post("/api/control/mode", json={"mode": "DEMO"}, headers=h(client)).json()
    assert r["mode"] == "DEMO" and r["startup_checks_passed"] is False
    assert r["warnings"] and "remain open on the LIVE account 2002" in r["warnings"][0]
    with session_scope() as db:
        trail = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
    for action in ("ACCOUNT_CREATE", "ACCOUNT_VERIFIED", "LIVE_ACCOUNT_APPROVED", "ACCOUNT_ACTIVATED", "MODE_SWITCH",
                   "LIVE_ACCOUNT_REVOKED"):
        assert action in trail, action


def test_identity_is_exact_login_exact_server_and_account_type(live_terminal):
    fake, bridge = live_terminal
    with session_scope() as db:
        bc = db.scalar(select(BrokerConnection))
        for login_, server, mode, msg in [(2002, "Live-Server-2", "LIVE", "expected 2002@Live-Server-2"),
                                          (2003, "Live-Server", "LIVE", "expected 2003@Live-Server"),
                                          (2002, "Live-Server", "DEMO", "configured DEMO but broker reports REAL")]:
            acct = TradingAccount(broker_connection_id=bc.id, login=login_, server=server, mode=mode)
            with pytest.raises(tr.IdentityError, match=msg):
                tr.sync_account(db, bridge, acct)
        fake.trade_mode = 0
        acct = TradingAccount(broker_connection_id=bc.id, login=2002, server="Live-Server", mode="LIVE")
        with pytest.raises(tr.IdentityError, match="configured LIVE but broker reports DEMO"):
            tr.sync_account(db, bridge, acct)


def test_mode_switch_refused_while_executions_are_unresolved(client, live_terminal):
    login(client, "admin")
    with session_scope() as db:
        acct = db.scalar(select(TradingAccount).where(TradingAccount.mode == "DEMO"))
        db.add(TradeOrder(execution_id="pending-1", account_id=acct.id, symbol="EURUSD.r", action="MARKET_BUY",
                          volume=0.1, status="UNKNOWN"))
        admin_id = db.scalar(select(User.id).where(User.username == "admin"))
        db.add(TradingAccount(broker_connection_id=acct.broker_connection_id, login=2002, server="Live-Server",
                              mode="LIVE", is_active=True, verified=True, live_approved_by=admin_id))
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 409 and "unresolved" in r.text
    assert state(client)["mode"] == "DEMO"
