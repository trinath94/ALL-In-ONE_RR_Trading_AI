"""Password hashing, session tokens, CSRF, credential encryption and request signing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def burn_password_check(password: str) -> None:
    """Spend the same argon2 cost for unknown usernames so timing does not reveal account existence."""
    verify_password(password, _DUMMY_HASH)


def create_session_token(user_id: int, session_id: str, role: str) -> tuple[str, datetime]:
    s = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(minutes=s.session_minutes)
    token = jwt.encode(
        {"sub": str(user_id), "sid": session_id, "role": role, "exp": exp, "iat": datetime.now(timezone.utc)},
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    return token, exp


def decode_session_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---- Credential encryption (broker passwords etc.) ---------------------------------
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("RR_ENCRYPTION_KEY is not configured; refusing to store credentials")
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - defensive
        raise RuntimeError("Unable to decrypt stored secret (wrong key?)") from exc


# ---- Service-to-service HMAC signing (Linux <-> Windows bridge) ---------------------
def sign_request(secret: str, method: str, path: str, body: bytes, timestamp: str | None = None,
                 nonce: str | None = None) -> dict[str, str]:
    """Return headers that authenticate a bridge request.

    Signature = HMAC-SHA256(secret, METHOD\nPATH\nTIMESTAMP\nNONCE\nSHA256(body)).
    The receiver rejects stale timestamps (>30s) and replayed nonces.
    """
    ts = timestamp or str(int(time.time()))
    n = nonce or secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    msg = f"{method.upper()}\n{path}\n{ts}\n{n}\n{digest}".encode()
    sig = base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()
    return {"X-RR-Timestamp": ts, "X-RR-Nonce": n, "X-RR-Signature": sig}


def verify_signature(secret: str, method: str, path: str, body: bytes, headers: dict[str, str],
                     max_skew: int = 30) -> bool:
    ts = headers.get("X-RR-Timestamp") or headers.get("x-rr-timestamp")
    nonce = headers.get("X-RR-Nonce") or headers.get("x-rr-nonce")
    sig = headers.get("X-RR-Signature") or headers.get("x-rr-signature")
    if not (ts and nonce and sig):
        return False
    try:
        if abs(int(time.time()) - int(ts)) > max_skew:
            return False
    except ValueError:
        return False
    expected = sign_request(secret, method, path, body, ts, nonce)["X-RR-Signature"]
    return hmac.compare_digest(expected, sig)
