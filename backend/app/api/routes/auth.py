from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CSRF_COOKIE, SESSION_COOKIE, client_ip, current_user, rate_limit, require, user_perms
from app.core.config import get_settings
from app.core.security import (burn_password_check, create_session_token, hash_password, new_csrf_token, new_session_id, sha256,
                               verify_password)
from app.db.models import LoginHistory, Role, User, UserSession
from app.db.session import get_db
from app.services.events import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])
UTC = timezone.utc


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "email": u.email, "role": u.role.name if u.role else None,
            "permissions": sorted(user_perms(u)), "is_active": u.is_active}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    rate_limit(f"login:{ip}", 10, 60)
    s = get_settings()
    user = db.scalar(select(User).where(User.username == body.username))
    ua = request.headers.get("user-agent", "")[:255]
    now = datetime.now(UTC)

    def fail(reason: str):
        db.add(LoginHistory(user_id=user.id if user else None, username=body.username[:64], success=False, ip=ip,
                            user_agent=ua, reason=reason))
        db.commit()
        raise HTTPException(401, "Invalid credentials")

    if user is None:
        burn_password_check(body.password)
        fail("unknown user")
    if user.locked_until and user.locked_until > now:
        db.add(LoginHistory(user_id=user.id, username=user.username, success=False, ip=ip, user_agent=ua, reason="locked"))
        db.commit()
        raise HTTPException(423, "Account temporarily locked")
    if not user.is_active:
        fail("disabled")
    if not verify_password(body.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= s.max_failed_logins:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_logins = 0
        fail("bad password")
    user.failed_logins, user.locked_until = 0, None
    sid, csrf = new_session_id(), new_csrf_token()
    token, exp = create_session_token(user.id, sid, user.role.name)
    db.add(UserSession(id=sid, user_id=user.id, csrf_hash=sha256(csrf), expires_at=exp, ip=ip, user_agent=ua))
    db.add(LoginHistory(user_id=user.id, username=user.username, success=True, ip=ip, user_agent=ua))
    audit(db, user.id, "LOGIN", user.username, ip=ip)
    db.commit()   # commit before the response: the session must exist when the client's next request arrives
    secure = s.cookie_secure
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite="strict",
                        max_age=s.session_minutes * 60, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure, samesite="strict",
                        max_age=s.session_minutes * 60, path="/")
    return {"user": user_out(user), "csrf": csrf, "expires_at": exp.isoformat()}


@router.post("/logout")
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sess = db.get(UserSession, request.state.session_id)
    if sess:
        sess.revoked = True
    audit(db, user.id, "LOGOUT", user.username, ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=256)


@router.post("/password")
def change_password(body: PasswordChange, request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password incorrect")
    user.password_hash = hash_password(body.new_password)
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.id != request.state.session_id)):
        s.revoked = True
    audit(db, user.id, "PASSWORD_CHANGE", user.username, ip=client_ip(request))
    return {"ok": True}


class ResetConfirm(BaseModel):
    username: str
    token: str
    new_password: str = Field(min_length=10, max_length=256)


@router.post("/password-reset/confirm")
def reset_confirm(body: ResetConfirm, request: Request, db: Session = Depends(get_db)):
    rate_limit(f"login:reset:{client_ip(request)}", 5, 300)
    u = db.scalar(select(User).where(User.username == body.username))
    if not u or not u.reset_token_hash or not u.reset_token_expires or u.reset_token_expires < datetime.now(UTC) \
            or sha256(body.token) != u.reset_token_hash:
        raise HTTPException(400, "Invalid or expired reset token")
    u.password_hash = hash_password(body.new_password)
    u.reset_token_hash = u.reset_token_expires = None
    for s in db.scalars(select(UserSession).where(UserSession.user_id == u.id)):
        s.revoked = True
    audit(db, u.id, "PASSWORD_RESET", u.username, ip=client_ip(request))
    return {"ok": True}


# ---- user management (admin) -------------------------------------------------------------
users = APIRouter(prefix="/api/users", tags=["users"])


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: str | None = None
    password: str = Field(min_length=10, max_length=256)
    role: str = Field(pattern="^(admin|trader|readonly)$")


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|trader|readonly)$")
    is_active: bool | None = None


@users.get("")
def list_users(_: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.id))]


@users.post("")
def create_user(body: UserIn, admin: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "Username exists")
    role = db.scalar(select(Role).where(Role.name == body.role))
    u = User(username=body.username, email=body.email, password_hash=hash_password(body.password), role_id=role.id)
    db.add(u)
    db.flush()
    audit(db, admin.id, "USER_CREATE", body.username, {"role": body.role})
    return user_out(u)


@users.patch("/{user_id}")
def patch_user(user_id: int, body: UserPatch, admin: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404)
    if u.id == admin.id and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "Cannot demote or disable yourself")
    if body.role:
        u.role_id = db.scalar(select(Role).where(Role.name == body.role)).id
    if body.is_active is not None:
        u.is_active = body.is_active
        if not body.is_active:
            for s in db.scalars(select(UserSession).where(UserSession.user_id == u.id)):
                s.revoked = True
    audit(db, admin.id, "USER_UPDATE", u.username, body.model_dump(exclude_none=True))
    db.flush()
    db.refresh(u)
    return user_out(u)


@users.post("/{user_id}/reset-token")
def issue_reset(user_id: int, admin: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    """Admin-issued one-time reset token (valid 30 min), delivered out-of-band to the user."""
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404)
    tok = secrets.token_urlsafe(24)
    u.reset_token_hash, u.reset_token_expires = sha256(tok), datetime.now(UTC) + timedelta(minutes=30)
    audit(db, admin.id, "RESET_TOKEN_ISSUED", u.username)
    return {"token": tok, "expires_in_minutes": 30}


@users.get("/login-history")
def login_history(_: User = Depends(require("users.manage")), db: Session = Depends(get_db), limit: int = 200):
    rows = db.scalars(select(LoginHistory).order_by(LoginHistory.id.desc()).limit(min(limit, 1000)))
    return [{"username": r.username, "success": r.success, "ip": r.ip, "reason": r.reason,
             "user_agent": r.user_agent, "at": r.created_at} for r in rows]
