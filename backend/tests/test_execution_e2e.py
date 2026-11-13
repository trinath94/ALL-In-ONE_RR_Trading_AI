"""End-to-end execution tests: Linux execution manager -> signed HTTP bridge -> real connector app
(journal + idempotency) -> FakeMT5, with a real PostgreSQL database."""
import os
import socket
import threading
import time

import pytest
import uvicorn
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import (BrokerConnection, ClosedPosition, ExecutionIdempotency, MT5Terminal, OpenPosition,
                           RiskConfig, Symbol, TradeExecution, TradeOrder, TradingAccount)
from app.db.session import session_scope
from app.execution import manager as em
from app.execution.bridge import BridgeClient, BridgeConfig, BridgeUnavailable
from app.services import trading as tr
from app.workers import main as worker
from connector.adapter import MT5Adapter
from connector.app import ConnectorConfig, create_app
from fake_mt5 import FakeMT5

SECRET = os.environ["RR_BRIDGE_SHARED_SECRET"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def stack(db_clean, tmp_path):
    fake = FakeMT5(login=1001, server="Demo-Server")
    app = create_app(ConnectorConfig(SECRET, 1001, "Demo-Server", journal_path=str(tmp_path / "journal.db")),
                     MT5Adapter(fake), connect=False)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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
        db.add(TradingAccount(broker_connection_id=bc.id, login=1001, server="Demo-Server", mode="DEMO", is_active=True))
        db.flush()
        tr.discover_symbols(db, bridge, bc.id)
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "DEMO"))
        rc.params = {**rc.params, "news_unknown_blocks": True}
    yield fake, bridge
    bridge.close()
    server.should_exit = True
    th.join(timeout=5)


def allow_unknown_news():
    with session_scope() as db:
        rc = db.scalar(select(RiskConfig).where(RiskConfig.account_mode == "DEMO"))
        rc.params = {**rc.params, "news_unknown_blocks": False}


def open_eur(db, bridge, key="k1", source="MANUAL", **kw):
    return em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.0980, take_profit=1.1050,
                            source=source, dedupe_key=key, **kw)


def test_symbols_discovered_dynamically(stack):
    with session_scope() as db:
        syms = {s.broker_symbol: s for s in db.scalars(select(Symbol))}
    assert set(syms) == {"EURUSD.r", "XAUUSD.r"}
    assert syms["EURUSD.r"].canonical == "EURUSD" and syms["EURUSD.r"].category == "forex"
    assert syms["XAUUSD.r"].category == "metals" and syms["EURUSD.r"].tradable


def test_entries_blocked_until_startup_checks_pass(stack):
    fake, bridge = stack
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="Startup"):
        open_eur(db, bridge)
    ok, report = worker.startup_checks(bridge)
    assert ok, report


def test_unknown_news_blocks_by_default(stack):
    fake, bridge = stack
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="UNKNOWN"):
        open_eur(db, bridge)
    assert fake.send_calls == 0


def test_order_confirmed_by_mt5_and_synchronised(stack):
    fake, bridge = stack
    allow_unknown_news()
    assert worker.startup_checks(bridge)[0]
    with session_scope() as db:
        o = open_eur(db, bridge)
        assert o.status == "CONFIRMED", o.status_detail
        # 0.5% of 10k = $50 over (ask 1.1001 - SL 1.0980 = 210pt) + 10pt spread + 5pt expected slippage
        assert float(o.volume) == pytest.approx(0.22)
        ex = db.scalar(select(TradeExecution).where(TradeExecution.order_id == o.id))
        assert ex.retcode == 10009
        pos = db.scalar(select(OpenPosition))
        assert pos.execution_id == o.execution_id and float(pos.stop_loss) == pytest.approx(1.098)
    assert fake.send_calls == 1


def test_duplicate_signal_never_executes_twice(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        a = open_eur(db, bridge, key="sig-42")
    with session_scope() as db:
        b = open_eur(db, bridge, key="sig-42")
    assert a.execution_id == b.execution_id
    assert fake.send_calls == 1
    with session_scope() as db:
        assert db.scalar(select(func.count(TradeOrder.id))) == 1


def test_lost_response_is_reconciled_not_resent(stack, monkeypatch):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    real_submit = BridgeClient.submit

    def lossy(self, cmd):
        real_submit(self, cmd)            # reaches MT5 ...
        raise BridgeUnavailable("network dropped before response")   # ... but the reply is lost

    monkeypatch.setattr(BridgeClient, "submit", lossy)
    with session_scope() as db:
        o = open_eur(db, bridge, key="lossy")
        assert o.status == "UNKNOWN"
        eid = o.execution_id
    monkeypatch.setattr(BridgeClient, "submit", real_submit)
    with session_scope() as db:
        assert em.reconcile_unknown(db, bridge)["resolved"] == 1
        o = db.scalar(select(TradeOrder).where(TradeOrder.execution_id == eid))
        assert o.status == "CONFIRMED"
        assert db.get(ExecutionIdempotency, eid).state == "DONE"
    assert fake.send_calls == 1


def test_risk_rejection_releases_reservation(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="wrong side"):
        em.open_position(db, bridge, symbol="EURUSD.r", direction="BUY", stop_loss=1.2, take_profit=1.3,
                         source="MANUAL", dedupe_key="bad")
    with session_scope() as db:
        assert db.scalar(select(func.count(ExecutionIdempotency.execution_id))) == 0
    assert fake.send_calls == 0


def test_kill_switch_closes_and_blocks(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        open_eur(db, bridge, key="k-a")
    with session_scope() as db:
        acts = em.kill_switch(db, bridge, None, "test emergency", "CLOSE_ALL")
    assert acts and not fake.positions
    with session_scope() as db:
        assert db.scalar(select(func.count(OpenPosition.id))) == 0
        closed = db.scalar(select(ClosedPosition))
        assert closed is not None and closed.execution_id is not None
    with session_scope() as db, pytest.raises(em.ExecutionBlocked, match="[Kk]ill"):
        open_eur(db, bridge, key="k-b")


def test_identity_mismatch_is_detected(stack):
    fake, bridge = stack
    with session_scope() as db:
        acct = db.scalar(select(TradingAccount))
        acct.login = 9999
    ok, report = worker.startup_checks(bridge)
    assert not ok and any("FAIL" in r for r in report)


def test_restart_adopts_broker_positions_and_protects_unprotected(stack):
    fake, bridge = stack
    fake.positions[777] = dict(ticket=777, symbol="EURUSD.r", type=0, volume=0.1, price_open=1.1, price_current=1.1001,
                               sl=0.0, tp=0.0, profit=1.0, swap=0.0, magic=0, comment="manual", time=int(time.time()))
    ok, report = worker.startup_checks(bridge)
    assert ok, report
    assert fake.positions[777]["sl"] > 0   # PROTECT policy attached an ATR stop
    with session_scope() as db:
        assert db.scalar(select(OpenPosition).where(OpenPosition.ticket == 777)).stop_loss is not None
    # nothing was opened by the restart itself
    assert sum(1 for t in fake.positions if t != 777) == 0


def test_position_management_partial_and_break_even(stack):
    fake, bridge = stack
    allow_unknown_news()
    worker.startup_checks(bridge)
    with session_scope() as db:
        o = open_eur(db, bridge, key="m1")
    ticket = next(iter(fake.positions))
    fake.positions[ticket]["price_current"] = 1.1035   # ~1.6R
    with session_scope() as db:
        tr.sync_positions(db, bridge, tr.active_account(db))
    worker.manage_positions(bridge)
    p = fake.positions[ticket]
    assert p["volume"] < float(o.volume) and p["sl"] >= 1.1001
    _ = get_settings


def test_tls_pinning_and_ip_allowlist(tmp_path):
    """Self-signed connector cert is accepted only when pinned; non-allow-listed IPs are refused."""
    import subprocess
    import sys as _sys
    gen = os.path.join(os.path.dirname(__file__), "..", "..", "mt5-connector", "windows", "gen_cert.py")
    subprocess.run([_sys.executable, gen, str(tmp_path), "127.0.0.1"], check=True, capture_output=True)
    crt, key = str(tmp_path / "connector.crt"), str(tmp_path / "connector.key")

    def serve(allowed):
        app = create_app(ConnectorConfig(SECRET, 1001, "Demo-Server", journal_path=str(tmp_path / f"j{len(allowed)}.db"),
                                         allowed_ips=allowed), MT5Adapter(FakeMT5()), connect=False)
        port = free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, ssl_certfile=crt, ssl_keyfile=key,
                                            log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        return srv, port

    srv, port = serve(["127.0.0.1/32"])
    assert BridgeClient(BridgeConfig(f"https://127.0.0.1:{port}", SECRET, ca_bundle=crt)).health()["connected"]
    with pytest.raises(BridgeUnavailable, match="CERTIFICATE_VERIFY_FAILED"):
        BridgeClient(BridgeConfig(f"https://127.0.0.1:{port}", SECRET)).health()
    srv.should_exit = True
    srv2, port2 = serve(["10.9.9.9/32"])
    from app.execution.bridge import BridgeRejected
    with pytest.raises(BridgeRejected) as ei:
        BridgeClient(BridgeConfig(f"https://127.0.0.1:{port2}", SECRET, ca_bundle=crt)).health()
    assert ei.value.status == 403
    srv2.should_exit = True
