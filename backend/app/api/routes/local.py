"""Local supervisor API for RR Trading One (``/api/v1/local``), used by the desktop app on the same PC.

Disabled (404) unless ``RR_SUPERVISOR_KEY`` is configured; in the local stack only the ``api`` container receives
it. Every request carries an HMAC-SHA256 signature made with that key:

    X-RR-Local-Ts, X-RR-Local-Nonce, X-RR-Local-Sig = base64(HMAC(key, "RRLOCAL\\n{METHOD}\\n{path}\\n{ts}\\n{nonce}\\n{sha256(body)}"))

with a ±60 s timestamp window and single-use nonces (Redis). Nothing here can trade, switch to LIVE, enable
auto-trading or change risk settings:

* ``GET  /status``           live safety state for the Control Centre (logins masked, no secrets)
* ``POST /bootstrap-admin``  create the first administrator — only while the engine has no users at all
* ``POST /demo-account``     register + activate the DEMO account the connected MT5 connector is logged in to;
                             refused unless an online connector reports exactly that login/server as DEMO
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Role, Symbol, TradingAccount, User
from app.db.models_agent import MT5Agent
from app.db.session import get_db
from app.execution import relay
from app.services import trading as tr
from app.services.events import audit, get_redis

router = APIRouter(prefix="/api/v1/local", tags=["local-supervisor"])
MAX_SKEW = 60
LOCAL_IP = "local-supervisor"


def mask(login) -> str:
    s = str(login or "")
    return ("•" * max(0, len(s) - 3) + s[-3:]) if s else ""


def expected_signature(key: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = f"RRLOCAL\n{method.upper()}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
    return base64.b64encode(hmac.new(key.encode(), msg.encode(), hashlib.sha256).digest()).decode()


async def supervisor(request: Request) -> None:
    key = get_settings().supervisor_key
    if len(key) < 32:
        raise HTTPException(404, "Not Found")          # feature off: indistinguishable from an unknown route
    h = request.headers
    ts, nonce, sig = h.get("x-rr-local-ts", ""), h.get("x-rr-local-nonce", ""), h.get("x-rr-local-sig", "")
    try:
        fresh = abs(time.time() - float(ts)) <= MAX_SKEW
    except ValueError:
        fresh = False
    if not fresh:
        raise HTTPException(401, "stale or missing timestamp")
    if not nonce or len(nonce) > 64:
        raise HTTPException(401, "missing nonce")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    body = await request.body()
    if not hmac.compare_digest(expected_signature(key, request.method, path, ts, nonce, body), sig):
        raise HTTPException(401, "bad signature")
    if not get_redis().set(f"rr:local:nonce:{nonce}", 1, nx=True, ex=MAX_SKEW * 3):
        raise HTTPException(401, "replayed request")


@router.get("/status", dependencies=[Depends(supervisor)])
def status(db: Session = Depends(get_db)):
    st = tr.get_state(db)
    r = get_redis()
    active = tr.active_account(db)
    agent = relay.select_agent(db)
    ast = (agent.status or {}) if agent else {}
    acct = ast.get("account") or {}
    return {
        "admin_exists": bool(db.scalar(select(func.count(User.id)))),
        "state": {"active_mode": st.active_mode, "auto_trading": st.auto_trading, "kill_switch": st.kill_switch,
                  "kill_reason": st.kill_reason or "", "risk_lock": st.risk_lock, "risk_lock_reason": st.risk_lock_reason or "",
                  "pause_new_entries": st.pause_new_entries, "startup_checks_passed": st.startup_checks_passed},
        "active_account": None if active is None else {
            "mode": active.mode, "login": mask(active.login), "server": active.server, "verified": active.verified,
            "live_approved": bool(active.live_approved_by)},
        "live_accounts_approved": db.scalar(select(func.count(TradingAccount.id)).where(
            TradingAccount.mode == "LIVE", TradingAccount.live_approved_by.isnot(None))) or 0,
        "connector": None if agent is None else {
            "online": relay.is_online(agent.id), "login": mask(acct.get("login")), "server": acct.get("server"),
            "account_type": acct.get("account_type"), "live_authorized_on_pc": bool(ast.get("live_authorized")),
            "pc_emergency_stop": bool(ast.get("local_kill_switch"))},
        "symbols": {"discovered": db.scalar(select(func.count(Symbol.id))) or 0,
                    "scan_enabled": db.scalar(select(func.count(Symbol.id)).where(Symbol.scan_enabled.is_(True))) or 0},
        "roles": {name: bool(r.exists(key)) for name, key in (("monitor", "rr:role:monitor:lock"),
                  ("analysis", "rr:role:analysis:lock"), ("trading", "rr:role:trading:lock"),
                  ("learner", "rr:learner:lock"))},
    }


class BootstrapIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=256)


@router.post("/bootstrap-admin", dependencies=[Depends(supervisor)])
def bootstrap_admin(body: BootstrapIn, db: Session = Depends(get_db)):
    """First administrator. Refused as soon as any user exists (one-time; serialised with an advisory lock)."""
    db.execute(text("SELECT pg_advisory_xact_lock(7271001)"))
    if db.scalar(select(func.count(User.id))):
        raise HTTPException(409, "the engine already has users; sign in to manage them")
    role = db.scalar(select(Role).where(Role.name == "admin"))
    if role is None:
        raise HTTPException(503, "engine not initialised (roles missing)")
    try:
        pw = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    u = User(username=body.username, password_hash=pw, role_id=role.id)
    db.add(u)
    db.flush()
    audit(db, u.id, "LOCAL_BOOTSTRAP_ADMIN", body.username, {}, LOCAL_IP)
    db.commit()
    return {"ok": True, "username": u.username}


class DemoAccountIn(BaseModel):
    login: int = Field(gt=0)
    server: str = Field(min_length=2, max_length=128)


@router.post("/demo-account", dependencies=[Depends(supervisor)])
def demo_account(body: DemoAccountIn, db: Session = Depends(get_db)):
    """Register (if needed) and activate the DEMO account an online connector is logged in to. Only facts reported
    by the connector are accepted; a LIVE account is never created or activated here."""
    agents = db.scalars(select(MT5Agent).where(MT5Agent.enabled.is_(True), MT5Agent.revoked_at.is_(None))).all()
    match = None
    for a in agents:
        acct = (a.status or {}).get("account") or {}
        if relay.is_online(a.id) and int(acct.get("login") or 0) == body.login and acct.get("server") == body.server:
            match = (a, acct)
            break
    if match is None:
        raise HTTPException(409, "no online MT5 connector is logged in to this account; start the connector first")
    agent, acct = match
    if acct.get("account_type") != "DEMO":
        raise HTTPException(409, "the connected account is not a DEMO account; LIVE accounts are set up in the engine "
                                 "with administrator approval")
    a = db.scalar(select(TradingAccount).where(TradingAccount.login == body.login, TradingAccount.server == body.server))
    created = a is None
    if a is None:
        a = tr.create_account_record(db, login=body.login, server=body.server, mode="DEMO",
                                     broker_name=str(acct.get("company") or "Broker")[:64],
                                     terminal_name="rr-trading-one-connector", bridge_url="relay://agent")
        audit(db, None, "ACCOUNT_CREATE", f"{body.login}@{body.server}", {"mode": "DEMO", "via": "local-supervisor"}, LOCAL_IP)
    elif a.mode != "DEMO":
        raise HTTPException(409, "this account is registered in the engine as LIVE; it is not changed here")
    tr.activate_account(db, a)
    audit(db, None, "ACCOUNT_ACTIVATED", f"{a.login}@{a.server}", {"mode": "DEMO", "via": "local-supervisor"}, LOCAL_IP)
    db.commit()
    return {"ok": True, "created": created, "account_id": a.id, "mode": a.mode, "login": mask(a.login), "server": a.server,
            "agent_id": agent.id}
