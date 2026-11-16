"""Security (RR Trading One Phase 21, specification Part 21): secrets never reach the logs."""
import io
import json
import logging

import pytest

from app.core import logging as rrlog


@pytest.fixture
def secrets_env(monkeypatch):
    vals = {"RR_JWT_SECRET": "jwt-Secret-Value-123456", "RR_ENCRYPTION_KEY": "fernet_KEY_abcdefghijklmnop=",
            "RR_BRIDGE_SHARED_SECRET": "bridge-shared-SECRET-0987", "RR_SUPERVISOR_KEY": "supervisor+key/with=chars",
            "RR_AI_KEYS_KEY": "ai-keys-key-ABCDEFGHIJ", "RR_MODEL_SIGNING_KEY": "model-signing-key-ZZZZZZZ",
            "RR_DATABASE_URL": "postgresql+psycopg://rr:Db%2FPass%40word99@postgres:5432/rr_trading",
            "RR_REDIS_URL": "redis://:RedisPassw0rdXYZ@redis:6379/0"}
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    return vals


def emit(logger_name: str, fmt: logging.Formatter, fn) -> str:
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(fmt)
    lg = logging.getLogger(logger_name)
    lg.handlers[:] = [h]
    lg.propagate = False
    fn(lg)
    return buf.getvalue()


def test_engine_secrets_and_credential_shapes_are_masked_in_messages_and_tracebacks(secrets_env):
    def go(lg):
        lg.error("db %s redis %s jwt=%s", secrets_env["RR_DATABASE_URL"], secrets_env["RR_REDIS_URL"],
                 secrets_env["RR_JWT_SECRET"])
        lg.warning("headers {'Authorization': 'Bearer eyJhbGciOi.payload.sig', 'x-api-key': 'k-123456789'} "
                   "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv body {\"password\": \"hunter2hunter2\"} url "
                   "https://user:urlpass123@example.com/x?api_key=QWERTY12345&ok=1")
        try:
            raise RuntimeError(f"connect failed with {secrets_env['RR_BRIDGE_SHARED_SECRET']} / "
                               f"{secrets_env['RR_SUPERVISOR_KEY']} / Db/Pass@word99")
        except RuntimeError:
            lg.exception("boom")
    out = emit("t_json", rrlog.JsonFormatter(), go)
    for line in out.splitlines():
        json.loads(line)                                        # still valid JSON
    leaks = [v for v in [*secrets_env.values(), "Db%2FPass%40word99", "Db/Pass@word99", "RedisPassw0rdXYZ",
                         "eyJhbGciOi.payload.sig", "k-123456789", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv", "hunter2hunter2",
                         "urlpass123", "QWERTY12345"] if v in out and not v.startswith(("postgresql", "redis://"))]
    assert leaks == [], leaks
    assert "postgresql+psycopg://rr:***@postgres:5432/rr_trading" in out, "useful context is kept"
    assert "ok=1" in out and "connect failed with ***" in out


def test_uvicorn_handlers_are_redacted_too(secrets_env):
    fmt = logging.Formatter("%(levelname)s %(message)s")
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(fmt)
    lg = logging.getLogger("uvicorn.error")
    old = lg.handlers[:]
    lg.handlers[:] = [h]
    try:
        rrlog.configure_logging()
        try:
            raise ValueError(f"bad {secrets_env['RR_ENCRYPTION_KEY']}")
        except ValueError:
            lg.exception("Exception in ASGI application %s", secrets_env["RR_AI_KEYS_KEY"])
    finally:
        lg.handlers[:] = old
    out = buf.getvalue()
    assert secrets_env["RR_ENCRYPTION_KEY"] not in out and secrets_env["RR_AI_KEYS_KEY"] not in out
    assert "Exception in ASGI application ***" in out and "ValueError: bad ***" in out
