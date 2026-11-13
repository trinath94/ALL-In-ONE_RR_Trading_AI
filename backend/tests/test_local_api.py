"""Local supervisor API (/api/v1/local): HMAC-authenticated, disabled without a key, one-time admin bootstrap, and
DEMO-account registration only for what an online connector actually reports."""
import json
import secrets
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes.local import expected_signature
from app.core.config import get_settings
from app.db.models import AuditLog, TradingAccount, TradingState, User
from app.db.models_agent import MT5Agent
from app.db.session import session_scope
from app.execution import relay

KEY = "s" * 48
# Shared with the Rust client test (desktop/core/src/engine.rs): identical inputs must give identical signatures.
VECTOR = ("k" * 40, "POST", "/api/v1/local/demo-account", "1794580000", "nonce-1",
          b'{"login":1001,"server":"Demo-Server"}', "dd2GgOMPmpjcDzrKCY63qGvkTNxKWny/K9fk9p1q2u8=")


def test_signature_test_vector():
    key, method, path, ts, nonce, body, sig = VECTOR
    assert expected_signature(key, method, path, ts, nonce, body) == sig


@pytest.fixture
def client(db_clean, monkeypatch):
    monkeypatch.setenv("RR_SUPERVISOR_KEY", KEY)
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def call(c, method, path, payload=None, key=KEY, ts=None, nonce=None, tamper=False):
    body = json.dumps(payload).encode() if payload is not None else b""
    ts = ts or str(time.time())
    nonce = nonce or secrets.token_hex(8)
    sig = expected_signature(key, method, path, ts, nonce, body + (b" " if tamper else b""))
    h = {"X-RR-Local-Ts": ts, "X-RR-Local-Nonce": nonce, "X-RR-Local-Sig": sig, "Content-Type": "application/json"}
    return c.request(method, path, content=body or None, headers=h)


def test_disabled_without_key(db_clean, monkeypatch):
    monkeypatch.delenv("RR_SUPERVISOR_KEY", raising=False)
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        for m, p in (("GET", "/api/v1/local/status"), ("POST", "/api/v1/local/bootstrap-admin"),
                     ("POST", "/api/v1/local/demo-account")):
            assert call(c, m, p, {} if m == "POST" else None).status_code == 404


def test_authentication_failures(client):
    p = "/api/v1/local/status"
    assert client.get(p).status_code == 401
    assert call(client, "GET", p, key="x" * 48).status_code == 401
    assert call(client, "GET", p, tamper=True).status_code == 401
    assert call(client, "GET", p, ts=str(time.time() - 600)).status_code == 401
    n = secrets.token_hex(8)
    assert call(client, "GET", p, nonce=n).status_code == 200
    assert call(client, "GET", p, nonce=n).status_code == 401          # replay


def test_status_reports_live_state_masked(client):
    s = call(client, "GET", "/api/v1/local/status").json()
    assert s["admin_exists"] is False
    assert s["state"]["active_mode"] == "DEMO" and s["state"]["auto_trading"] is False
    assert s["state"]["kill_switch"] is False and s["active_account"] is None and s["connector"] is None
    assert set(s["roles"]) == {"monitor", "analysis", "trading", "learner"}


def test_bootstrap_admin_once(client):
    weak = call(client, "POST", "/api/v1/local/bootstrap-admin", {"username": "owner", "password": "short"})
    assert weak.status_code == 422
    bad = call(client, "POST", "/api/v1/local/bootstrap-admin", {"username": "o w n e r", "password": "x" * 12})
    assert bad.status_code == 422
    r = call(client, "POST", "/api/v1/local/bootstrap-admin", {"username": "owner", "password": "Correct-Horse-1"})
    assert r.status_code == 200 and r.json()["username"] == "owner"
    again = call(client, "POST", "/api/v1/local/bootstrap-admin", {"username": "second", "password": "Correct-Horse-2"})
    assert again.status_code == 409
    with session_scope() as db:
        users = db.scalars(select(User)).all()
        assert [u.username for u in users] == ["owner"] and users[0].role.name == "admin"
        assert users[0].password_hash != "Correct-Horse-1"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "LOCAL_BOOTSTRAP_ADMIN")).ip == "local-supervisor"
    lr = client.post("/api/auth/login", json={"username": "owner", "password": "Correct-Horse-1"})
    assert lr.status_code == 200
    assert call(client, "GET", "/api/v1/local/status").json()["admin_exists"] is True


def agent(login=1001, server="Demo-Server", account_type="DEMO", online=True) -> int:
    with session_scope() as db:
        a = MT5Agent(name="pc", device_id=secrets.token_hex(16), public_key="k" * 44, api_key_hash="h" * 64,
                     bridge_secret_enc="x", status={"account": {"login": login, "server": server, "company": "Fake Broker",
                                                                "account_type": account_type}},
                     account_login=login, account_server=server)
        db.add(a)
        db.flush()
        aid = a.id
    if online:
        relay.sync_redis().set(relay.ONLINE.format(agent=aid), 1, ex=60)
    return aid


def test_demo_account_registered_and_activated_from_connector_report(client):
    with session_scope() as db:
        st = db.scalar(select(TradingState))
        st.startup_checks_passed = True
    agent()
    r = call(client, "POST", "/api/v1/local/demo-account", {"login": 1001, "server": "Demo-Server"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["created"] is True and d["mode"] == "DEMO" and d["login"] == "•001"
    with session_scope() as db:
        a = db.get(TradingAccount, d["account_id"])
        assert a.is_active and a.mode == "DEMO" and a.login == 1001
        st = db.scalar(select(TradingState))
        assert st.auto_trading is False and st.startup_checks_passed is False, "switching account re-arms startup checks"
    again = call(client, "POST", "/api/v1/local/demo-account", {"login": 1001, "server": "Demo-Server"})
    assert again.status_code == 200 and again.json()["created"] is False
    s = call(client, "GET", "/api/v1/local/status").json()
    assert s["active_account"]["login"] == "•001" and s["connector"]["account_type"] == "DEMO"


@pytest.mark.parametrize("kw,login,server", [
    ({}, 2002, "Demo-Server"),                               # connector is logged in to another account
    ({}, 1001, "Other-Server"),                              # other server
    ({"online": False}, 1001, "Demo-Server"),                # connector offline
    ({"account_type": "LIVE"}, 1001, "Demo-Server"),         # a LIVE account is never created here
])
def test_demo_account_refused_unless_connector_reports_it_as_demo(client, kw, login, server):
    agent(**kw)
    r = call(client, "POST", "/api/v1/local/demo-account", {"login": login, "server": server})
    assert r.status_code == 409
    with session_scope() as db:
        assert db.scalar(select(TradingAccount)) is None


def test_existing_live_account_is_never_changed(client):
    from app.services import trading as tr
    with session_scope() as db:
        tr.create_account_record(db, login=1001, server="Demo-Server", mode="LIVE")
    agent()
    r = call(client, "POST", "/api/v1/local/demo-account", {"login": 1001, "server": "Demo-Server"})
    assert r.status_code == 409
    with session_scope() as db:
        a = db.scalar(select(TradingAccount))
        assert a.mode == "LIVE" and not a.is_active
