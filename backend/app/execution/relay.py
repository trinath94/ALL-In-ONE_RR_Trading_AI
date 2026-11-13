"""Outbound relay to the RR MT5 Connector desktop agent (``RR_BRIDGE_MODE=relay``).

The Windows agent dials **out** to the engine over a WebSocket (``/api/v1/mt5/agent/ws``); nothing listens on
Windows. ``BridgeClient`` is unchanged: in relay mode it gets a :class:`RelayTransport` instead of a TCP
connection. Every request still carries the original per-request HMAC signature (timestamp + nonce + body hash)
and is verified **on the agent** by the original connector code (journal, idempotency, replay protection).

Flow (any engine process -> the API process that holds the agent's socket), via Redis:

    BridgeClient --RPUSH rr:relay:req:<agent>--> hub (API process) --ws--> agent
    BridgeClient <--BLPOP rr:relay:resp:<id>---- hub <--ws-- agent

Safety:
* A request carries a deadline. The hub drops expired requests, so an order queued while the agent was offline is
  never delivered late (the connector also rejects signatures older than 30 s).
* A timeout surfaces as ``httpx.ReadTimeout`` -> ``BridgeUnavailable``: for a trade the outcome is UNKNOWN and the
  existing journal reconciliation (``/executions/{id}``) resolves it, exactly as with a dropped HTTPS connection.
* A response is accepted only for a pending request id issued to that same agent, once.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass

import httpx
import redis

from app.core.config import get_settings

log = logging.getLogger("relay")

REQ_Q = "rr:relay:req:{agent}"
RESP = "rr:relay:resp:{rid}"
PENDING = "rr:relay:pending:{rid}"
ONLINE = "rr:relay:online:{agent}"
FORWARD_HEADERS = ("content-type", "x-rr-timestamp", "x-rr-nonce", "x-rr-signature")

_redis: redis.Redis | None = None


def sync_redis() -> redis.Redis:
    """Dedicated client without a socket timeout (BLPOP blocks for up to the bridge timeout)."""
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def is_online(agent_id: int) -> bool:
    try:
        return bool(sync_redis().exists(ONLINE.format(agent=agent_id)))
    except redis.RedisError:
        return False


class RelayTransport(httpx.BaseTransport):
    def __init__(self, agent_id: int, timeout: float = 10.0, client: redis.Redis | None = None):
        self.agent_id, self.timeout = agent_id, timeout
        self.r = client or sync_redis()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            if not self.r.exists(ONLINE.format(agent=self.agent_id)):
                raise httpx.ConnectError(f"RR MT5 Connector agent {self.agent_id} is offline", request=request)
            rid = uuid.uuid4().hex
            body = request.read()
            msg = {"type": "request", "id": rid, "method": request.method,
                   "path": request.url.raw_path.decode("ascii"),
                   "headers": {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS},
                   "body": base64.b64encode(body).decode(), "deadline": time.time() + self.timeout}
            q = REQ_Q.format(agent=self.agent_id)
            pipe = self.r.pipeline()
            pipe.set(PENDING.format(rid=rid), str(self.agent_id), ex=int(self.timeout) + 10)
            pipe.rpush(q, json.dumps(msg))
            pipe.expire(q, 120)
            pipe.execute()
            res = self.r.blpop([RESP.format(rid=rid)], timeout=max(1, math.ceil(self.timeout)))
        except redis.RedisError as exc:
            raise httpx.ConnectError(f"relay unavailable: {exc}", request=request) from exc
        if res is None:
            self.r.delete(PENDING.format(rid=rid))
            raise httpx.ReadTimeout("no response from RR MT5 Connector agent", request=request)
        resp = json.loads(res[1])
        return httpx.Response(int(resp["status"]), headers={"content-type": "application/json"},
                              content=base64.b64decode(resp.get("body") or ""), request=request)


@dataclass
class ResolvedAgent:
    id: int
    secret: str


def select_agent(db):
    """The agent that serves the active trading account; otherwise the most recently seen enabled agent.
    (The connector still verifies login + server on every trading command, so a wrong pick cannot trade.)"""
    from sqlalchemy import select
    from app.db.models_agent import MT5Agent
    from app.services import trading as tr
    agents = db.scalars(select(MT5Agent).where(MT5Agent.enabled.is_(True), MT5Agent.revoked_at.is_(None))
                        .order_by(MT5Agent.last_seen_at.desc().nullslast())).all()
    if not agents:
        return None
    acct = tr.active_account(db)
    if acct is not None:
        for a in agents:
            if a.account_login == acct.login and (a.account_server or "") == acct.server:
                return a
    online = [a for a in agents if is_online(a.id)]
    return (online or agents)[0]


def resolve_agent() -> ResolvedAgent | None:
    from app.core.security import decrypt_secret
    from app.db.session import session_scope
    with session_scope() as db:
        a = select_agent(db)
        return ResolvedAgent(a.id, decrypt_secret(a.bridge_secret_enc)) if a else None
