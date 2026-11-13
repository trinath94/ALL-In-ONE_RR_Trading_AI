"""Realtime event bus (Redis pub/sub) + audit/system-event persistence helpers."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.config import get_settings
from app.db.models import AuditLog, SystemEvent

log = logging.getLogger("events")
CHANNEL = "rr:events"
_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(get_settings().redis_url, decode_responses=True, socket_timeout=2)
    return _redis


def publish(kind: str, data: Any) -> None:
    """Fan-out to dashboard websockets. Failure to publish never breaks trading logic."""
    try:
        get_redis().publish(CHANNEL, json.dumps({"kind": kind, "ts": datetime.now(timezone.utc).isoformat(),
                                                 "data": data}, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("publish failed: %s", exc)


def audit(db, user_id: int | None, action: str, target: str = "", detail: dict | None = None, ip: str | None = None) -> None:
    db.add(AuditLog(user_id=user_id, action=action, target=target, detail=detail or {}, ip=ip))


def system_event(db, component: str, level: str, message: str, data: dict | None = None) -> None:
    db.add(SystemEvent(component=component, level=level, message=message, data=data or {}))
    publish("system_event", {"component": component, "level": level, "message": message})


def agent_activity(module: str, status: str, message: str, data: dict | None = None) -> None:
    """Real agent/module activity feed (only emitted where work actually happens)."""
    payload = {"module": module, "status": status, "message": message, "data": data or {}}
    publish("agent_activity", payload)
    try:
        r = get_redis()
        r.lpush("rr:activity", json.dumps({**payload, "ts": datetime.now(timezone.utc).isoformat()}, default=str))
        r.ltrim("rr:activity", 0, 499)
        r.hset("rr:module_status", module, json.dumps({"status": status, "message": message,
                                                       "ts": datetime.now(timezone.utc).isoformat()}))
    except Exception as exc:  # noqa: BLE001
        log.debug("activity store failed: %s", exc)
