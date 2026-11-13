import os

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db.models import Role, User
from app.db.session import session_scope

PW = "Correct-Horse-9"


def make_user(name, role):
    with session_scope() as db:
        from sqlalchemy import select
        r = db.scalar(select(Role).where(Role.name == role))
        db.add(User(username=name, password_hash=hash_password(PW), role_id=r.id))


@pytest.fixture
def client(db_clean):
    from app.main import app
    make_user("admin", "admin")
    make_user("trader", "trader")
    make_user("viewer", "readonly")
    return TestClient(app)


def login(c, user, pw=PW):
    r = c.post("/api/auth/login", json={"username": user, "password": pw})
    return r


def h(c):
    return {"X-CSRF-Token": c.cookies.get("rr_csrf")}


def test_login_sets_httponly_cookie_and_me(client):
    r = login(client, "admin")
    assert r.status_code == 200
    sc = r.headers.get_list("set-cookie")
    assert any(x.startswith("rr_session=") and "httponly" in x.lower() and "samesite=strict" in x.lower() for x in sc)
    me = client.get("/api/auth/me").json()
    assert me["role"] == "admin" and "trade.live" in me["permissions"]


def test_unauthenticated_rejected(client):
    assert client.get("/api/dashboard/summary").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_bad_password_and_lockout(client):
    for _ in range(5):
        assert login(client, "trader", "wrong-password").status_code == 401
    assert login(client, "trader").status_code == 423   # locked even with right password
    assert login(client, "nobody", "x").status_code == 401


def test_csrf_required_for_writes(client):
    login(client, "admin")
    assert client.post("/api/control/pause", json={"enabled": True}).status_code == 403
    assert client.post("/api/control/pause", json={"enabled": True}, headers={"X-CSRF-Token": "forged"}).status_code == 403
    r = client.post("/api/control/pause", json={"enabled": True}, headers=h(client))
    assert r.status_code == 200 and r.json()["pause_new_entries"] is True


def test_rbac_readonly_cannot_trade_or_configure(client):
    login(client, "viewer")
    assert client.get("/api/dashboard/summary").status_code == 200
    assert client.post("/api/control/pause", json={"enabled": True}, headers=h(client)).status_code == 403
    assert client.put("/api/risk/config/DEMO", json={"risk_per_trade_pct": 1}, headers=h(client)).status_code == 403
    assert client.get("/api/users").status_code == 403


def test_trader_cannot_switch_to_live(client):
    login(client, "trader")
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 403


def test_live_requires_confirmation_and_approved_account(client):
    login(client, "admin")
    r = client.post("/api/control/mode", json={"mode": "LIVE"}, headers=h(client))
    assert r.status_code == 428
    r = client.post("/api/control/mode", json={"mode": "LIVE", "confirm_text": "ENABLE LIVE TRADING"}, headers=h(client))
    assert r.status_code == 409 and "approved" in r.json()["detail"]


def test_auto_trading_requires_verified_account(client):
    login(client, "admin")
    r = client.post("/api/control/auto-trading", json={"enabled": True}, headers=h(client))
    assert r.status_code == 409


def test_risk_config_validation_and_isolation(client):
    login(client, "admin")
    assert client.put("/api/risk/config/DEMO", json={"bogus": 1}, headers=h(client)).status_code == 400
    assert client.put("/api/risk/config/DEMO", json={"max_daily_loss_pct": 50}, headers=h(client)).status_code == 400
    r = client.put("/api/risk/config/DEMO", json={"risk_per_trade_pct": 1.0}, headers=h(client))
    assert r.status_code == 200 and r.json()["risk_per_trade_pct"] == 1.0
    live = client.get("/api/risk/config/LIVE").json()
    assert live["risk_per_trade_pct"] == 0.25   # demo change did not leak into live


def test_kill_switch_and_reset(client):
    login(client, "trader")
    r = client.post("/api/control/kill-switch", json={"reason": "manual test"}, headers=h(client))
    assert r.status_code == 200 and r.json()["state"]["kill_switch"] is True
    assert client.post("/api/control/kill-switch/reset", headers=h(client)).status_code == 403  # admin only
    login(client, "admin")
    r = client.post("/api/control/kill-switch/reset", headers=h(client))
    assert r.json()["kill_switch"] is False and r.json()["startup_checks_passed"] is False


def test_strategies_endpoint_lists_full_registry(client):
    login(client, "viewer")
    rows = client.get("/api/strategies").json()
    assert len(rows) == 215
    unavailable = [r for r in rows if r["availability"].startswith("unavailable")]
    assert all(not r["enabled"] for r in unavailable)


def test_risky_strategy_cannot_be_enabled_for_live(client):
    login(client, "admin")
    r = client.patch("/api/strategies/quant_martingale", json={"enabled": True, "mode": "LIVE"}, headers=h(client))
    assert r.status_code == 409


def test_user_management_and_audit(client):
    login(client, "admin")
    r = client.post("/api/users", json={"username": "newbie", "password": "Another-Pass-1", "role": "trader"},
                    headers=h(client))
    assert r.status_code == 200
    assert client.post("/api/users", json={"username": "x", "password": "short", "role": "trader"},
                       headers=h(client)).status_code == 422
    actions = [a["action"] for a in client.get("/api/system/audit").json()]
    assert "USER_CREATE" in actions and "LOGIN" in actions


def test_logout_revokes_session(client):
    login(client, "admin")
    assert client.post("/api/auth/logout", headers=h(client)).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_password_reset_flow(client):
    login(client, "admin")
    uid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "trader")
    tok = client.post(f"/api/users/{uid}/reset-token", headers=h(client)).json()["token"]
    c2 = TestClient(client.app)
    r = c2.post("/api/auth/password-reset/confirm", json={"username": "trader", "token": tok, "new_password": "Brand-New-Pass-7"})
    assert r.status_code == 200
    assert login(c2, "trader", "Brand-New-Pass-7").status_code == 200
    _ = os
