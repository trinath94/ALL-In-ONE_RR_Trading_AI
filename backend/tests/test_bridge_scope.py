"""Role-scoped bridge credentials (RR Trading One, Phase 1 gap 7.4), end to end over the real relay.

A read-scoped process (the learner and analysis containers: RR_BRIDGE_SCOPE=read, no RR_ENCRYPTION_KEY) can read
everything it needs from MT5 but cannot execute: the client refuses, a hand-forged request is refused by the
original connector on the PC, and the trading secret cannot even be decrypted.
"""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import BrokerConnection
from app.db.models_agent import MT5Agent
from app.db.session import session_scope
from app.execution import relay
from app.execution.bridge import BridgeClient, BridgeError, BridgeRejected
from app.services import trading as tr
from app.workers import main as worker
from tests.test_execution_e2e import allow_unknown_news, open_eur
from tests.test_mt5_agent import engine, make_manager, new_token, wait  # noqa: F401 (fixture)

READ_KEY = Fernet.generate_key().decode()


def use(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    get_settings.cache_clear()


@pytest.fixture
def scoped_agent(engine, tmp_path, monkeypatch):
    use(monkeypatch, RR_BRIDGE_READ_KEY=READ_KEY)
    m, fake, store, vault = make_manager(tmp_path)
    d = m.register(engine, new_token())
    att = m.connect_mt5("Demo-Server", 443)
    assert att.mode == "DEMO"
    m.start()
    assert wait(lambda: relay.is_online(store.get("agent_id")))
    yield m, fake, store, vault, d
    m.stop()
    get_settings.cache_clear()


def test_registration_issues_a_distinct_encrypted_read_credential(scoped_agent):
    _, _, store, vault, d = scoped_agent
    ro, rw = vault.get("bridge-readonly-secret"), vault.get("bridge-secret")
    assert ro and rw and ro != rw and d["bridge_readonly_secret"] == ro
    with session_scope() as db:
        a = db.get(MT5Agent, store.get("agent_id"))
        assert a.bridge_readonly_secret_enc and ro not in a.bridge_readonly_secret_enc
        assert Fernet(READ_KEY.encode()).decrypt(a.bridge_readonly_secret_enc.encode()).decode() == ro


def test_read_scope_reads_but_can_never_execute(scoped_agent, monkeypatch):
    m, fake, store, vault, _ = scoped_agent
    full = BridgeClient()
    assert worker.startup_checks(full)[0]
    allow_unknown_news()
    with session_scope() as db:
        tr.discover_symbols(db, full, db.scalar(select(BrokerConnection.id)))
    # ---- become a read-scoped process exactly as the learner/analysis containers are configured
    use(monkeypatch, RR_BRIDGE_SCOPE="read", RR_ENCRYPTION_KEY="")
    ro = BridgeClient()
    assert ro.cfg.read_only and ro.cfg.secret == vault.get("bridge-readonly-secret")
    assert ro.health()["connected"] and ro.account()["login"] == 1001
    assert len(ro.rates("EURUSD.r", "M5", 20)) == 20 and ro.tick("EURUSD.r")["bid"] > 0
    assert isinstance(ro.positions(), list) and "margin" in ro.margin("EURUSD.r", "BUY", 0.1, 1.1)
    cmd = {"execution_id": "scope-1", "action": "MARKET_BUY", "symbol": "EURUSD.r", "volume": 0.1,
           "stop_loss": 1.095, "take_profit": 1.11, "account_login": 1001, "account_server": "Demo-Server"}
    with pytest.raises(BridgeError, match="read-only"):
        ro.submit(cmd)                                          # 1. the client refuses
    with pytest.raises(BridgeRejected) as exc:
        ro._request("POST", "/executions", cmd)                 # 2. a forged request: the PC connector refuses
    assert exc.value.status == 403 and "read-only" in str(exc.value.detail)
    assert fake.send_calls == 0 and not fake.positions          # nothing ever reached MT5
    with pytest.raises(RuntimeError, match="RR_ENCRYPTION_KEY"):
        relay.resolve_agent("full")                             # 3. the trading secret cannot be decrypted


def test_agent_without_read_credential_fails_closed(engine, tmp_path, monkeypatch):
    use(monkeypatch, RR_BRIDGE_READ_KEY=None)
    m, _, store, vault, *_ = make_manager(tmp_path)
    m.register(engine, new_token())                             # engine without a read key: no read credential
    assert vault.get("bridge-readonly-secret") is None
    use(monkeypatch, RR_BRIDGE_SCOPE="read")
    with pytest.raises(BridgeError, match="no read-only credential"):
        BridgeClient()
    use(monkeypatch, RR_BRIDGE_SCOPE="full")
    assert not BridgeClient().cfg.read_only


def test_read_scope_requires_relay_mode(monkeypatch, db_clean):
    use(monkeypatch, RR_BRIDGE_MODE="direct", RR_BRIDGE_SCOPE="read")
    try:
        with pytest.raises(BridgeError, match="requires RR_BRIDGE_MODE=relay"):
            BridgeClient()
        use(monkeypatch, RR_BRIDGE_SCOPE="everything")
        with pytest.raises(BridgeError, match="invalid RR_BRIDGE_SCOPE"):
            BridgeClient()
    finally:
        use(monkeypatch, RR_BRIDGE_MODE=None, RR_BRIDGE_SCOPE=None)


def test_full_scope_still_executes_with_read_credential_present(scoped_agent):
    _, fake, *_ = scoped_agent
    full = BridgeClient()
    assert not full.cfg.read_only
    assert worker.startup_checks(full)[0]
    allow_unknown_news()
    with session_scope() as db:
        tr.discover_symbols(db, full, db.scalar(select(BrokerConnection.id)))
        assert open_eur(db, full, key="scope-full").status == "CONFIRMED"
    assert fake.send_calls == 1 and len(fake.positions) == 1
