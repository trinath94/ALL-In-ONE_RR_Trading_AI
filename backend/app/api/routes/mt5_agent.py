"""RR MT5 Connector (Windows desktop agent) API — additive, under ``/api/v1/mt5``.

Integration contract: docs/WINDOWS_CONNECTOR.md. Trading, positions, orders and accounts keep using the
existing ``/api/*`` routes; this router only adds what the desktop agent itself needs (registration, the
outbound relay socket, heartbeat, status, update check) and admin management of agents.

Agent authentication (no user session):
* **Registration** — one-time token (created by an admin, valid ``RR_AGENT_TOKEN_TTL_HOURS``) plus an Ed25519
  signature made with the device's own key over ``RRMT5-REGISTER\\n{device_id}\\n{public_key}\\n{ts}\\n{sha256(token)}``.
* **Session** — API key (stored hashed) + Ed25519 signature over ``RRMT5-HELLO\\n{agent_id}\\n{ts}\\n{nonce}``,
  timestamp within 60 s, nonce single-use (replay protection).
* **REST** — the same key + signature over ``RRMT5-REQ\\n{METHOD}\\n{path}\\n{ts}\\n{nonce}\\n{sha256(body)}``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, current_user, rate_limit, require
from app.core.config import get_settings
from app.core.security import encrypt_secret
from app.db.models import User
from app.db.models_agent import MT5Agent, MT5AgentToken
from app.db.session import get_db, session_scope
from app.execution import relay
from app.notifications.service import notify
from app.services.events import audit, get_redis, system_event

log = logging.getLogger("mt5_agent")
router = APIRouter(prefix="/api/v1/mt5", tags=["mt5-agent"])
UTC = timezone.utc
MAX_SKEW = 60
STATUS_KEYS = ("terminal", "account", "mt5_server", "live_authorized", "local_kill_switch", "engine_rtt_ms",
               "app_version", "guard", "symbols", "state")


def _sha(v: str | bytes) -> str:
    return hashlib.sha256(v.encode() if isinstance(v, str) else v).hexdigest()


def _verify_sig(public_key_b64: str, message: str, sig_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(sig_b64),
                                                                                    message.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _fresh(ts) -> bool:
    try:
        return abs(time.time() - float(ts)) <= MAX_SKEW
    except (TypeError, ValueError):
        return False


def _use_nonce(agent_id: int, nonce: str) -> bool:
    if not nonce or len(nonce) > 64:
        return False
    return bool(get_redis().set(f"rr:agent:nonce:{agent_id}:{nonce}", 1, nx=True, ex=MAX_SKEW * 3))


def authenticate(db: Session, agent_id, api_key: str, message: str, sig: str, ts, nonce: str) -> MT5Agent:
    agent = db.get(MT5Agent, int(agent_id)) if str(agent_id).isdigit() else None
    if agent is None or not hmac.compare_digest(agent.api_key_hash, _sha(api_key or "")):
        raise HTTPException(401, "unknown agent or bad key")
    if not agent.enabled or agent.revoked_at is not None:
        raise HTTPException(403, "agent revoked")
    if not _fresh(ts):
        raise HTTPException(401, "stale timestamp (check the PC clock)")
    if not _verify_sig(agent.public_key, message, sig):
        raise HTTPException(401, "bad signature")
    if not _use_nonce(agent.id, nonce):
        raise HTTPException(401, "replayed request")
    return agent


def agent_out(a: MT5Agent) -> dict:
    st = a.status or {}
    return {"id": a.id, "name": a.name, "device_id": a.device_id, "device_info": a.device_info,
            "app_version": a.app_version, "enabled": a.enabled and a.revoked_at is None,
            "revoked_at": a.revoked_at, "created_at": a.created_at, "last_seen_at": a.last_seen_at,
            "last_ip": a.last_ip, "online": relay.is_online(a.id), "account_login": a.account_login,
            "account_server": a.account_server, "status": {k: st.get(k) for k in STATUS_KEYS}}


def apply_status(a: MT5Agent, payload: dict, ip: str | None) -> None:
    a.status = {k: payload.get(k) for k in STATUS_KEYS if k in payload}
    a.app_version = str(payload.get("app_version") or a.app_version)[:32]
    acct = payload.get("account") or {}
    if acct.get("login"):
        a.account_login, a.account_server = int(acct["login"]), str(acct.get("server") or "")[:128]
    a.last_seen_at, a.last_ip = datetime.now(UTC), ip


# ---- registration ------------------------------------------------------------------------------
class RegisterIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    device_id: str = Field(min_length=16, max_length=64)
    device_name: str = Field(default="", max_length=128)
    device_info: dict = Field(default_factory=dict)
    public_key: str = Field(min_length=40, max_length=64)
    app_version: str = Field(default="", max_length=32)
    ts: float
    signature: str = Field(max_length=128)


@router.post("/agent/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    rate_limit(f"agent-register:{ip}", 10, 3600)
    if not _fresh(body.ts):
        raise HTTPException(401, "stale timestamp (check the PC clock)")
    msg = f"RRMT5-REGISTER\n{body.device_id}\n{body.public_key}\n{body.ts}\n{_sha(body.token)}"
    if not _verify_sig(body.public_key, msg, body.signature):
        raise HTTPException(401, "bad device signature")
    tok = db.scalar(select(MT5AgentToken).where(MT5AgentToken.token_hash == _sha(body.token)).with_for_update())
    now = datetime.now(UTC)
    if tok is None or tok.used_at is not None or tok.expires_at < now:
        audit(db, None, "MT5_AGENT_REGISTER_DENIED", body.device_id, {"ip": ip}, ip)
        db.commit()
        raise HTTPException(401, "registration token invalid, expired or already used")
    api_key, bridge_secret = secrets.token_urlsafe(36), secrets.token_urlsafe(48)
    agent = db.scalar(select(MT5Agent).where(MT5Agent.device_id == body.device_id))
    if agent is None:
        agent = MT5Agent(device_id=body.device_id)
        db.add(agent)
    # re-registering the same device rotates every credential and re-enables it
    agent.name = (body.device_name or tok.label or f"agent-{body.device_id[:8]}")[:128]
    agent.device_info = {k: str(v)[:256] for k, v in list(body.device_info.items())[:20]}
    agent.public_key, agent.api_key_hash = body.public_key, _sha(api_key)
    agent.bridge_secret_enc, agent.app_version = encrypt_secret(bridge_secret), body.app_version
    agent.enabled, agent.revoked_at, agent.last_ip = True, None, ip
    db.flush()
    tok.used_at, tok.used_by_agent_id = now, agent.id
    audit(db, tok.created_by, "MT5_AGENT_REGISTERED", f"agent:{agent.id}", {"device": agent.name, "ip": ip}, ip)
    system_event(db, "connector", "info", f"RR MT5 Connector registered: {agent.name}")
    db.commit()
    return {"agent_id": agent.id, "name": agent.name, "api_key": api_key, "bridge_secret": bridge_secret,
            "ws_path": "/api/v1/mt5/agent/ws", "heartbeat_seconds": 5,
            "bridge_mode": get_settings().bridge_mode}


# ---- outbound relay socket ---------------------------------------------------------------------
@router.websocket("/agent/ws")
async def agent_socket(ws: WebSocket):
    await ws.accept()
    ip = ws.client.host if ws.client else None
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if hello.get("type") != "hello":
            raise HTTPException(400, "expected hello")

        def _auth():
            with session_scope() as db:
                aid = hello.get("agent_id")
                a = authenticate(db, aid, hello.get("api_key", ""), f"RRMT5-HELLO\n{aid}\n{hello.get('ts')}\n"
                                 f"{hello.get('nonce')}", hello.get("signature", ""), hello.get("ts"), hello.get("nonce", ""))
                a.last_seen_at, a.last_ip = datetime.now(UTC), ip
                return a.id, a.name
        agent_id, name = await asyncio.to_thread(_auth)
    except HTTPException as exc:
        await ws.send_json({"type": "error", "detail": exc.detail})
        await ws.close(code=4403 if exc.status_code == 403 else 4401)
        return
    except Exception:  # noqa: BLE001 - malformed hello / timeout
        await ws.close(code=4400)
        return

    import redis.asyncio as aioredis
    s = get_settings()
    r = aioredis.Redis.from_url(s.redis_url, decode_responses=True)
    session_id = secrets.token_hex(12)
    online = relay.ONLINE.format(agent=agent_id)
    await r.set(online, session_id, ex=s.agent_heartbeat_timeout_seconds)
    await ws.send_json({"type": "welcome", "session_id": session_id, "agent_id": agent_id, "server_time": time.time(),
                        "bridge_mode": s.bridge_mode})
    log.info("agent %s (%s) connected from %s", agent_id, name, ip)

    async def pump():                       # engine -> agent
        q = relay.REQ_Q.format(agent=agent_id)
        while True:
            if await r.get(online) not in (session_id, None):
                return                      # superseded by a newer session of the same agent
            item = await r.blpop([q], timeout=1)
            if not item:
                continue
            msg = json.loads(item[1])
            if float(msg.get("deadline", 0)) < time.time():
                log.warning("dropped expired relay request %s %s", msg.get("method"), msg.get("path"))
                continue
            await ws.send_text(item[1])

    async def receive():                    # agent -> engine
        while True:
            m = await ws.receive_json()
            kind = m.get("type")
            if kind == "response":
                rid = str(m.get("id", ""))
                key = relay.PENDING.format(rid=rid)
                if await r.get(key) == str(agent_id) and await r.delete(key):
                    out = relay.RESP.format(rid=rid)
                    await r.rpush(out, json.dumps({"status": int(m.get("status", 502)), "body": m.get("body", "")}))
                    await r.expire(out, 30)
            elif kind == "heartbeat":
                await r.set(online, session_id, ex=s.agent_heartbeat_timeout_seconds)

                def _hb(payload=m):
                    with session_scope() as db:
                        a = db.get(MT5Agent, agent_id)
                        if a is None or a.revoked_at is not None or not a.enabled:
                            return False
                        apply_status(a, payload, ip)
                        return True
                if not await asyncio.to_thread(_hb):
                    await ws.close(code=4403)
                    return
                await ws.send_json({"type": "heartbeat_ack", "ts": m.get("ts"), "server_time": time.time()})
            elif kind == "kill_switch":
                reason = str(m.get("reason") or "emergency stop pressed on the PC")[:200]

                def _kill():
                    from app.execution import manager as em
                    with session_scope() as db:
                        em.kill_switch(db, None, None, f"RR MT5 Connector '{name}': {reason}")
                await asyncio.to_thread(_kill)
                await ws.send_json({"type": "kill_switch_ack"})
            elif kind == "event":
                def _ev(payload=m):
                    with session_scope() as db:
                        level = payload.get("level") if payload.get("level") in ("info", "warning", "error") else "info"
                        system_event(db, "connector", level, f"[{name}] {str(payload.get('message', ''))[:300]}")
                        if level == "error":
                            notify(db, "CONNECTOR", f"RR MT5 Connector '{name}'", str(payload.get("message", ""))[:300])
                await asyncio.to_thread(_ev)

    tasks = [asyncio.create_task(pump()), asyncio.create_task(receive())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if await r.get(online) == session_id:
            await r.delete(online)
        await r.aclose()
        log.info("agent %s disconnected", agent_id)


# ---- agent REST (heartbeat fallback, update check) -----------------------------------------------
async def agent_from_headers(request: Request, db: Session) -> MT5Agent:
    h = request.headers
    body = await request.body()
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    msg = f"RRMT5-REQ\n{request.method}\n{path}\n{h.get('x-rr-agent-ts')}\n{h.get('x-rr-agent-nonce')}\n{_sha(body)}"
    return authenticate(db, h.get("x-rr-agent-id", ""), h.get("x-rr-agent-key", ""), msg, h.get("x-rr-agent-sig", ""),
                        h.get("x-rr-agent-ts"), h.get("x-rr-agent-nonce", ""))


@router.post("/heartbeat")
async def heartbeat(request: Request, db: Session = Depends(get_db)):
    agent = await agent_from_headers(request, db)
    apply_status(agent, json.loads(await request.body() or b"{}"), client_ip(request))
    db.commit()
    return {"ok": True, "server_time": time.time(), "online_via_socket": relay.is_online(agent.id)}


@router.get("/agent/update")
async def update_check(request: Request, db: Session = Depends(get_db)):
    await agent_from_headers(request, db)
    s = get_settings()
    if not (s.agent_latest_version and s.agent_update_url.startswith("https://") and len(s.agent_update_sha256) == 64):
        return {"available": False}
    return {"available": True, "version": s.agent_latest_version, "url": s.agent_update_url,
            "sha256": s.agent_update_sha256.lower()}


# ---- user-facing status & administration ----------------------------------------------------------
@router.get("/status")
def status(_: User = Depends(current_user), db: Session = Depends(get_db)):
    agents = db.scalars(select(MT5Agent).order_by(MT5Agent.id)).all()
    sel = relay.select_agent(db) if get_settings().bridge_mode == "relay" else None
    return {"bridge_mode": get_settings().bridge_mode, "selected_agent_id": sel.id if sel else None,
            "agents": [agent_out(a) for a in agents]}


@router.get("/agents")
def agents(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [agent_out(a) for a in db.scalars(select(MT5Agent).order_by(MT5Agent.id))]


class TokenIn(BaseModel):
    label: str = Field(default="", max_length=128)


@router.post("/agent/tokens")
def create_token(body: TokenIn, request: Request, user: User = Depends(require("system.manage")),
                 db: Session = Depends(get_db)):
    token = "rrmt5_" + secrets.token_urlsafe(32)
    exp = datetime.now(UTC) + timedelta(hours=get_settings().agent_token_ttl_hours)
    db.add(MT5AgentToken(token_hash=_sha(token), label=body.label, created_by=user.id, expires_at=exp))
    audit(db, user.id, "MT5_AGENT_TOKEN_CREATED", body.label, {"expires_at": exp.isoformat()}, client_ip(request))
    db.commit()
    return {"token": token, "expires_at": exp, "note": "Shown once. Enter it in the RR MT5 Connector setup."}


@router.post("/agents/{agent_id}/revoke")
def revoke(agent_id: int, request: Request, user: User = Depends(require("system.manage")),
           db: Session = Depends(get_db)):
    a = db.get(MT5Agent, agent_id)
    if a is None:
        raise HTTPException(404, "agent not found")
    a.enabled, a.revoked_at = False, datetime.now(UTC)
    audit(db, user.id, "MT5_AGENT_REVOKED", f"agent:{agent_id}", {"name": a.name}, client_ip(request))
    db.commit()
    get_redis().delete(relay.ONLINE.format(agent=agent_id))
    return agent_out(a)
