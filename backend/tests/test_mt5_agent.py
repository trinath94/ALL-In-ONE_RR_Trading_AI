"""RR MT5 Connector desktop agent <-> RR AI Engine, end to end over a real socket.

Real uvicorn engine (test DB + Redis), the real desktop ``ConnectorManager`` (original connector code inside)
attached to the FakeMT5 terminal, and the unchanged ``BridgeClient`` in relay mode. Nothing listens on the
"Windows" side: the agent dials out.
"""
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows-connector"))

from app.core.config import get_settings  # noqa: E402
from app.db.models import BrokerConnection, MT5Terminal, RiskConfig, TradingAccount, TradingState  # noqa: E402
from app.db.models_agent import MT5Agent, MT5AgentToken  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.execution import relay  # noqa: E402
from app.execution.bridge import BridgeClient, BridgeRejected, BridgeUnavailable  # noqa: E402
from app.services import trading as tr  # noqa: E402
from app.workers import main as worker  # noqa: E402
from connector.adapter import MT5Adapter  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402
from rr_connector.agent import RegistrationError  # noqa: E402
from rr_connector.manager import ConnectorManager  # noqa: E402
from rr_connector.store import Store  # noqa: E402
from rr_connector.terminals import Terminal  # noqa: E402
from rr_connector.vault import MemoryKeyring, Vault  # noqa: E402
from tests.test_execution_e2e import allow_unknown_news, free_port, open_eur  # noqa: E402


def wait(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def agent_status(agent_id) -> dict:
    with session_scope() as db:
        return dict(db.get(MT5Agent, agent_id).status or {})


def kill_switch_on() -> bool:
    with session_scope() as db:
        return bool(db.scalar(select(TradingState)).kill_switch)


def new_token(label="pc1", hours=1) -> str:
    import secrets as s
    from datetime import datetime, timedelta, timezone
    from app.api.routes.mt5_agent import _sha
    tok = "rrmt5_" + s.token_urlsafe(32)
    with session_scope() as db:
        db.add(MT5AgentToken(token_hash=_sha(tok), label=label,
                             expires_at=datetime.now(timezone.utc) + timedelta(hours=hours)))
    return tok


@pytest.fixture
def engine(db_clean, monkeypatch):
    monkeypatch.setenv("RR_BRIDGE_MODE", "relay")
    get_settings.cache_clear()
    from app.main import app
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    assert wait(lambda: server.started)
    with session_scope() as db:
        term = MT5Terminal(name="desktop", bridge_url="relay://agent")
        db.add(term)
        db.flush()
        bc = BrokerConnection(broker_name="Fake", server="Demo-Server", terminal_id=term.id)
        db.add(bc)
        db.flush()
        db.add(TradingAccount(broker_connection_id=bc.id, login=1001, server="Demo-Server", mode="DEMO", is_active=True))
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(timeout=5)
    monkeypatch.delenv("RR_BRIDGE_MODE")
    get_settings.cache_clear()


def make_manager(tmp_path, fake=None, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    os.environ["RRMT5_HOME"] = str(tmp_path)
    vault = Vault(MemoryKeyring())
    store = Store(tmp_path / "config.db", vault)
    fake = fake or FakeMT5(login=1001, server="Demo-Server")
    term = Terminal(folder="C:/MT5", exe="C:/MT5/terminal64.exe", running=True, sources=["process"])
    m = ConnectorManager(store, vault, MT5Adapter(fake), finder=lambda: [term], launcher=lambda exe: None,
                         prober=lambda a, p: {"reachable": True, "latency_ms": 3.0},
                         journal_path=str(tmp_path / "journal.sqlite3"), watchdog_s=0.2, **kw)
    m.engine.heartbeat_s = 0.3
    return m, fake, store, vault


@pytest.fixture
def agent(engine, tmp_path):
    m, fake, store, vault = make_manager(tmp_path)
    m.register(engine, new_token())
    att = m.connect_mt5("Demo-Server", 443)
    assert att.server_check["result"] == "MATCH" and att.mode == "DEMO"
    m.start()
    assert wait(lambda: m.snapshot["engine"] == "CONNECTED"), m.snapshot
    assert wait(lambda: relay.is_online(store.get("agent_id")))
    with session_scope() as db:
        tr.discover_symbols(db, BridgeClient(), db.scalar(select(BrokerConnection.id)))
    yield m, fake, store
    m.stop()


# ---- registration & authentication --------------------------------------------------------------
def test_registration_requires_valid_single_use_token_and_stores_only_hashes(engine, tmp_path):
    m, _, store, vault = make_manager(tmp_path)
    with pytest.raises(RegistrationError, match="invalid"):
        m.register(engine, "rrmt5_" + "x" * 40)
    tok = new_token()
    d = m.register(engine, tok)
    assert d["agent_id"] and vault.get("api-key") and vault.get("bridge-secret")
    with session_scope() as db:
        a = db.get(MT5Agent, d["agent_id"])
        assert a.api_key_hash != vault.get("api-key") and vault.get("bridge-secret") not in a.bridge_secret_enc
        assert db.scalar(select(MT5AgentToken)).token_hash != tok
    m2, *_ = make_manager(tmp_path / "other")
    with pytest.raises(RegistrationError, match="already used"):
        m2.register(engine, tok)
    with pytest.raises(RegistrationError, match="HTTPS"):
        m2.register("http://engine.example.com", new_token())


def test_registration_rejects_forged_device_signature(engine):
    import httpx
    from app.api.routes.mt5_agent import _sha  # noqa: F401
    tok = new_token()
    r = httpx.post(engine + "/api/v1/mt5/agent/register", json={
        "token": tok, "device_id": "d" * 32, "public_key": base64.b64encode(b"k" * 32).decode(), "ts": time.time(),
        "signature": base64.b64encode(b"s" * 64).decode()})
    assert r.status_code == 401 and "signature" in r.json()["detail"]


def test_socket_rejects_bad_key_and_replayed_hello(engine, tmp_path):
    import asyncio
    import websockets
    m, _, store, vault = make_manager(tmp_path)
    m.register(engine, new_token())
    aid, key = store.get("agent_id"), vault.get("api-key")
    url = engine.replace("http://", "ws://") + "/api/v1/mt5/agent/ws"

    async def hello(api_key, ts, nonce):
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "hello", "agent_id": aid, "api_key": api_key, "ts": ts, "nonce": nonce,
                                      "signature": m.identity.sign(f"RRMT5-HELLO\n{aid}\n{ts}\n{nonce}")}))
            return json.loads(await ws.recv())
    ts = time.time()
    assert asyncio.run(hello("wrong", ts, "n1"))["type"] == "error"
    assert asyncio.run(hello(key, ts, "n2"))["type"] == "welcome"
    assert "replayed" in asyncio.run(hello(key, ts, "n2"))["detail"]
    assert "stale" in asyncio.run(hello(key, ts - 600, "n3"))["detail"]


# ---- relay: data, heartbeat, execution -----------------------------------------------------------
def test_engine_reaches_mt5_through_outbound_relay_and_trades_demo(agent, engine):
    m, fake, store = agent
    bridge = BridgeClient()
    assert bridge.cfg.relay_agent_id == store.get("agent_id")
    h = bridge.health()
    assert h["connected"] and h["identity_ok"] and h["account_login"] == 1001
    assert bridge.account()["account_type"] == "DEMO"
    assert {s["name"] for s in bridge.symbols()} == {"EURUSD.r", "XAUUSD.r"}
    assert bridge.tick("EURUSD.r")["bid"] > 0 and len(bridge.rates("EURUSD.r", "M5", 50)) == 50
    ok, report = worker.startup_checks(bridge)
    assert ok, report
    allow_unknown_news()
    with session_scope() as db:
        order = open_eur(db, bridge, key="relay-1")
        assert order.status == "CONFIRMED", order.status_detail
    assert len(fake.positions) == 1                        # actually executed in the (fake) terminal
    pos = next(iter(fake.positions.values()))
    assert pos["sl"] == pytest.approx(1.0980)
    # heartbeat reached the engine with the real account snapshot
    assert wait(lambda: (agent_status(store.get("agent_id")).get("account") or {}).get("balance") is not None)
    import httpx
    from tests.test_api import login, make_user
    make_user("viewer", "readonly")
    with httpx.Client(base_url=engine) as c:
        lr = login(c, "viewer")
        assert lr.status_code == 200, lr.text
        r = c.get("/api/v1/mt5/status")
        assert r.status_code == 200, r.text
        st = r.json()
        assert st["bridge_mode"] == "relay" and st["agents"][0]["online"]
        assert st["agents"][0]["status"]["account"]["login"] == 1001
        assert c.post("/api/v1/mt5/agent/tokens", json={}, headers={"X-CSRF-Token": c.cookies.get("rr_csrf")}).status_code == 403
    # every relayed trade is in the PC's local event log
    assert any("MARKET_BUY" in e["message"] for e in store.events(category="trade"))


def test_offline_agent_fails_fast_and_expired_requests_are_never_delivered(agent):
    m, fake, store = agent
    bridge = BridgeClient()
    m.stop()
    assert wait(lambda: not relay.is_online(store.get("agent_id")), timeout=15)
    t0 = time.time()
    with pytest.raises(BridgeUnavailable, match="offline"):
        bridge.health()
    assert time.time() - t0 < 2
    # a request whose deadline passed while queued is dropped by the hub, not executed later
    r = relay.sync_redis()
    r.rpush(relay.REQ_Q.format(agent=store.get("agent_id")), json.dumps(
        {"type": "request", "id": "late", "method": "POST", "path": "/executions", "headers": {}, "body": "",
         "deadline": time.time() - 1}))
    m.start()                                               # restart recovery: same PC, same credentials
    assert wait(lambda: relay.is_online(store.get("agent_id")))
    assert wait(lambda: r.llen(relay.REQ_Q.format(agent=store.get("agent_id"))) == 0)
    time.sleep(0.5)
    assert not fake.positions
    assert BridgeClient().health()["connected"]


def test_live_account_trades_are_refused_until_authorised_on_the_pc(agent):
    m, fake, store = agent
    fake.trade_mode = 2                                     # broker reports a REAL account
    bridge = BridgeClient()
    cmd = {"execution_id": "live-1", "action": "MARKET_BUY", "symbol": "EURUSD.r", "volume": 0.1,
           "account_login": 1001, "account_server": "Demo-Server", "sl": 1.0980, "tp": 1.1050}
    with pytest.raises(BridgeRejected) as e:
        bridge.submit(cmd)
    assert e.value.status == 403 and "not been authorised" in str(e.value.detail)
    assert not fake.positions
    assert wait(lambda: m.snapshot.get("mode") == "LIVE")
    with pytest.raises(ValueError, match="does not match"):
        m.authorize_live("9999", "ENABLE LIVE TRADING")
    with pytest.raises(ValueError, match="exactly"):
        m.authorize_live("1001", "yes")
    m.authorize_live("1001", "ENABLE LIVE TRADING")
    res = bridge.submit({**cmd, "execution_id": "live-2"})
    assert res["state"] == "DONE" and len(fake.positions) == 1
    with pytest.raises(BridgeRejected, match="lots"):
        bridge.submit({**cmd, "execution_id": "live-3", "volume": 5.0})      # PC max-lot cap


def test_pc_emergency_stop_activates_engine_kill_switch(agent):
    m, fake, store = agent
    m.emergency_stop("test")
    assert wait(kill_switch_on)
    with pytest.raises(BridgeRejected, match="emergency stop"):
        BridgeClient().submit({"execution_id": "k-1", "action": "MARKET_BUY", "symbol": "EURUSD.r", "volume": 0.1,
                               "account_login": 1001, "account_server": "Demo-Server"})


def test_revoked_agent_is_disconnected_and_cannot_return(agent):
    m, fake, store = agent
    with session_scope() as db:
        a = db.get(MT5Agent, store.get("agent_id"))
        a.enabled = False
    assert wait(lambda: m.snapshot["engine"] in ("REJECTED", "DISCONNECTED", "CONNECTING"), timeout=10)
    assert wait(lambda: m.snapshot["engine"] == "REJECTED", timeout=15), m.snapshot
    with pytest.raises(Exception):
        BridgeClient().health()


def test_terminal_crash_is_detected_and_recovered(agent):
    m, fake, store = agent
    fake.logged_in = False                                  # terminal gone / logged out
    assert wait(lambda: m.snapshot["mt5"] == "RECONNECTING", timeout=5)
    fake.logged_in = True
    assert wait(lambda: m.snapshot["mt5"] == "CONNECTED", timeout=5)
    assert BridgeClient().health()["connected"]
    _ = RiskConfig
