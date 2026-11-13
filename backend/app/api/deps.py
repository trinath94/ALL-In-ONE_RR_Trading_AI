"""Authentication, CSRF, RBAC and rate-limit dependencies."""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.security import decode_session_token, sha256
from app.db.models import User, UserSession
from app.db.session import get_db
from app.services.events import get_redis

SESSION_COOKIE = "rr_session"
CSRF_COOKIE = "rr_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

PERMISSIONS = {
    "view": "View dashboards and data",
    "trade.manual": "Submit manual orders and manage positions",
    "trade.control": "Auto trading on/off, pause, kill switch, close all",
    "trade.live": "Activate / switch to LIVE trading",
    "strategy.manage": "Enable/disable strategies, edit parameters, promote experiments",
    "risk.manage": "Edit risk configuration",
    "backtest.run": "Run backtests and optimisations",
    "users.manage": "Manage users and roles",
    "system.manage": "Accounts, connectors, notification settings",
}
ROLE_PERMS = {
    "admin": list(PERMISSIONS),
    "trader": ["view", "trade.manual", "trade.control", "backtest.run"],
    "readonly": ["view"],
}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


def rate_limit(key: str, limit: int, window: int) -> None:
    try:
        r = get_redis()
        k = f"rr:rl:{key}:{int(time.time() // window)}"
        n = r.incr(k)
        if n == 1:
            r.expire(k, window)
    except Exception:  # noqa: BLE001 - if redis is down, fail closed only for auth endpoints
        if key.startswith("login"):
            raise HTTPException(503, "rate limiter unavailable") from None
        return
    if n > limit:
        raise HTTPException(429, "Too many requests")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        claims = decode_session_token(token)
    except jwt.PyJWTError:
        raise HTTPException(401, "Session invalid or expired") from None
    sess = db.get(UserSession, claims.get("sid"))
    if sess is None or sess.revoked or sess.expires_at < datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired")
    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(401, "User disabled")
    if request.method not in SAFE_METHODS:
        header = request.headers.get("x-csrf-token", "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not header or not hmac.compare_digest(header, cookie) or not hmac.compare_digest(sha256(header), sess.csrf_hash):
            raise HTTPException(403, "CSRF token missing or invalid")
        rate_limit(f"write:{user.id}", 120, 60)
    request.state.user = user
    request.state.session_id = sess.id
    return user


def user_perms(user: User) -> set[str]:
    perms = {p.code for p in user.role.permissions} if user.role else set()
    return perms or set(ROLE_PERMS.get(user.role.name if user.role else "", []))


def require(perm: str):
    def dep(user: User = Depends(current_user)) -> User:
        if perm not in user_perms(user):
            raise HTTPException(403, f"Permission '{perm}' required")
        return user
    return dep
