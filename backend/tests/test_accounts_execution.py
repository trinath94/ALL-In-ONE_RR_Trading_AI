"""Multi-account execution: one signal is evaluated for every execution account in the active mode, each through its
own connector, its own reconciliation, its own risk limits and its own idempotency key. Two simulated broker accounts
(FakeMT5 behind two real connector apps); the engine code is real."""
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient  # noqa: F401
from sqlalchemy import select

from app.db.models import RiskEvaluation, TradeOrder, TradingAccount, TradingSignal, User
from app.db.session import session_scope
from app.execution.bridge import AccountBridges, BridgeClient, BridgeConfig
from app.scanner.service import scan_once
from app.services import trading as tr
from app.workers import main as worker
from connector.adapter import MT5Adapter
from connector.app import ConnectorConfig, create_app
from fake_mt5 import FakeMT5
from tests.test_api import client, h, login  # noqa: F401 (fixture)
from tests.test_execution_e2e import SECRET, allow_unknown_news, free_port, stack  # noqa: F401 (fixture)
from tests.test_part27 import force_buy, scan  # noqa: F401 (fixture)
from tests.test_parts27_30_integration import align_feed
from tests.test_roles import enable_auto
from tests.test_scanner_scheduling import varied


@pytest.fixture
def two(scan, tmp_path):
    """The scan stack (account 1001, active) plus a second DEMO account 1002 with its own connector and terminal."""
    fake1, bridge1, shift = scan
    fake2 = FakeMT5(login=1002, server="Demo-Server")
    app = create_app(ConnectorConfig(SECRET, 1002, "Demo-Server", journal_path=str(tmp_path / "journal2.db")),
                     MT5Adapter(fake2), connect=False)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    bridge2 = BridgeClient(BridgeConfig(f"http://127.0.0.1:{port}", SECRET))
    varied(fake2, shift)
    with session_scope() as db:
        a1 = db.scalar(select(TradingAccount).where(TradingAccount.login == 1001))
        a2 = TradingAccount(broker_connection_id=a1.broker_connection_id, login=1002, server="Demo-Server", mode="DEMO",
                            trading_enabled=True, verified=True, label="DEMO Account 2")
        db.add(a2)
        db.flush()
        ids = (a1.id, a2.id)
    bridges = AccountBridges(bridge1, factory=lambda a: bridge2 if a.login == 1002 else None)
    yield fake1, fake2, bridge1, bridge2, bridges, shift, ids
    bridge2.close()
    server.should_exit = True
    th.join(timeout=5)


def ready(fake1, fake2, bridge1, bridges, shift, monkeypatch):
    force_buy(monkeypatch)
    align_feed(fake1, shift)
    align_feed(fake2, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge1)[0]
    worker.other_accounts_cycle(bridges)           # reconciles account 2 through its own connector
    enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = "ENGINE_ONLY"


def test_the_second_account_is_reconciled_through_its_own_connector(two):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    with session_scope() as db:
        assert [a.id for a in tr.execution_accounts(db)] == [a1, a2]
        assert not tr.is_reconciled(db, db.get(TradingAccount, a2))
    assert worker.other_accounts_cycle(bridges) == {1002: "ok"}
    with session_scope() as db:
        acct = db.get(TradingAccount, a2)
        assert tr.is_reconciled(db, acct) and acct.reconciled
        assert tr.get_state(db).startup_checks_passed is False, "the active account's own state is untouched"


def test_one_signal_trades_each_account_with_its_own_limits(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    ready(fake1, fake2, bridge1, bridges, shift, monkeypatch)
    with session_scope() as db:
        db.get(TradingAccount, a2).risk_overrides = {"max_lot_size": 0.01}
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    with session_scope() as db:
        orders = db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED")).all()
        by_acct = {}
        for o in orders:
            by_acct.setdefault(o.account_id, []).append(o)
        assert set(by_acct) == {a1, a2}, by_acct
        assert all(float(o.volume) <= 0.01 for o in by_acct[a2]), "account 2's own lot limit"
        assert any(float(o.volume) > 0.01 for o in by_acct[a1]), "account 1 keeps the template limit"
        for o in by_acct[a2]:                      # the same signal, once per account
            assert sum(1 for x in by_acct[a2] if x.signal_id == o.signal_id) == 1
        evs = db.scalars(select(RiskEvaluation).where(RiskEvaluation.approved.is_(True))).all()
        assert {e.account_id for e in evs} == {a1, a2}
    assert fake1.positions and fake2.positions, "each order went to its own terminal"
    assert {p["volume"] for p in fake2.positions.values()} <= {0.01}


def test_a_rejection_on_one_account_never_undoes_anothers_execution(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    ready(fake1, fake2, bridge1, bridges, shift, monkeypatch)
    with session_scope() as db:
        db.get(TradingAccount, a2).risk_overrides = {"max_spread_points": 0.1}     # the Risk Engine refuses here
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    with session_scope() as db:
        confirmed = db.scalars(select(TradeOrder).where(TradeOrder.status == "CONFIRMED")).all()
        assert confirmed and {o.account_id for o in confirmed} == {a1}
        for o in confirmed:
            assert db.get(TradingSignal, o.signal_id).status == "EXECUTED"
        rej = db.scalars(select(RiskEvaluation).where(RiskEvaluation.account_id == a2)).all()
        assert rej and not any(e.approved for e in rej)
    assert not fake2.positions


def test_research_only_and_unreconciled_accounts_are_not_traded(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    force_buy(monkeypatch)
    align_feed(fake1, shift)
    align_feed(fake2, shift)
    allow_unknown_news()
    assert worker.startup_checks(bridge1)[0]          # account 2 never reconciled
    enable_auto()
    with session_scope() as db:
        tr.get_state(db).ai_mode = "ENGINE_ONLY"
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    assert fake1.positions and not fake2.positions, "no entry before the account's own reconciliation"
    with session_scope() as db:
        db.get(TradingAccount, a2).trading_enabled = False
        assert [a.id for a in tr.execution_accounts(db)] == [a1]


def test_the_kill_switch_applies_to_every_account(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    ready(fake1, fake2, bridge1, bridges, shift, monkeypatch)
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    assert fake2.positions
    with session_scope() as db:
        rc_pol = tr.account_risk_params(db, db.get(TradingAccount, a2)).existing_position_policy
        from app.execution import manager as em
        em.kill_switch(db, bridge1, None, "test kill")
    worker.other_accounts_cycle(bridges)
    with session_scope() as db:
        assert db.get(TradingAccount, a2).emergency_applied, rc_pol
    n_orders = len(fake2.positions)
    scan_once(bridge1, None, worker.auto_execute_factory(bridge1, bridges))
    assert len(fake2.positions) <= n_orders, "no new entry on any account while the kill switch is on"
    with session_scope() as db:
        st = tr.get_state(db)
        st.kill_switch, st.kill_reason = False, ""
    worker.other_accounts_cycle(bridges)
    with session_scope() as db:
        assert not db.get(TradingAccount, a2).emergency_applied, "re-armed after the reset"


def test_a_live_account_is_never_traded_while_demo_is_active(two):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    with session_scope() as db:
        demo = db.get(TradingAccount, a1)
        admin = db.scalar(select(User.id).where(User.username == "admin"))
        db.add(TradingAccount(broker_connection_id=demo.broker_connection_id, login=5005, server="Live-Srv", mode="LIVE",
                              verified=True, trading_enabled=True, live_approved_by=admin))
    with session_scope() as db:
        assert 5005 not in {a.login for a in tr.execution_accounts(db)}


def test_accounts_status_endpoint(client, two):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    login(client, "viewer")
    r = client.get("/api/accounts/status").json()
    rows = {x["login"]: x for x in r["accounts"]}
    assert rows[1001]["trading_now"] and rows[1002]["trading_now"]
    assert rows[1002]["reconciled_now"] is False and rows[1002]["label"] == "DEMO Account 2"
    assert r["bridge_mode"] in ("direct", "relay")


def test_without_a_connector_an_account_is_skipped_not_traded(two, monkeypatch):
    fake1, fake2, bridge1, bridge2, bridges, shift, (a1, a2) = two
    nobody = AccountBridges(bridge1)                  # default factory: needs a relay agent for account 1002
    assert worker.other_accounts_cycle(nobody) == {1002: "no connector"}
    assert "relay" in nobody.errors[a2] or "connector" in nobody.errors[a2].lower()
