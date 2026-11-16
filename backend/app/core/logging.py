"""Structured JSON logging.

Phase 21 (Part 21: never expose passwords, API keys or bridge secrets in logs): every record is redacted before it is
written, in its message and its traceback. The engine's own secrets (read from its environment) are replaced by
``***`` wherever they appear, and so are credential-shaped text: passwords in URLs, Authorization / API-key headers,
provider API keys, and ``password=`` / ``"api_key": ...`` pairs. Redaction is a safety net; code still never logs a
secret on purpose.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

MASK = "***"
SECRET_ENV = ("RR_JWT_SECRET", "RR_ENCRYPTION_KEY", "RR_BRIDGE_SHARED_SECRET", "RR_BRIDGE_READ_KEY", "RR_SUPERVISOR_KEY",
              "RR_AI_KEYS_KEY", "RR_MODEL_SIGNING_KEY", "RR_ANTHROPIC_API_KEY", "RR_ADMIN_PASSWORD", "RR_TELEGRAM_BOT_TOKEN",
              "RR_SMTP_PASSWORD")
URL_ENV = ("RR_DATABASE_URL", "RR_REDIS_URL")
PATTERNS = [
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/@\s]*:)([^@\s]+)(@)"), r"\1" + MASK + r"\3"),          # scheme://user:PASS@
    (re.compile(r"(?i)\b(authorization['\"]?\s*[:=]\s*['\"]?\s*(?:bearer|basic|token)?\s*)([^\s,;\"']+)"), r"\1" + MASK),
    (re.compile(r"(?i)\b(x-api-key|api[-_]?key|x-rr-agent-key|x-rr-local-sig|x-rr-agent-sig)(['\"]?\s*[:=]\s*['\"]?)([^\s,;\"'&]+)"),
     r"\1\2" + MASK),
    (re.compile(r"(?i)\b(password|passwd|secret|token)(['\"]?\s*[:=]\s*['\"]?)([^\s,;\"'&]+)"), r"\1\2" + MASK),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), MASK),                                               # provider API keys
]


def _known_secrets() -> list[str]:
    vals = [os.environ.get(k, "") for k in SECRET_ENV]
    for k in URL_ENV:
        try:
            pw = urlsplit(os.environ.get(k, "")).password
        except ValueError:
            pw = None
        if pw:
            vals += [pw, unquote(pw)]
    # longest first, so a secret containing another is masked whole; short values are not secrets worth matching
    return sorted({v for v in vals if len(v) >= 8}, key=len, reverse=True)


def redact(text: str) -> str:
    for v in _known_secrets():
        text = text.replace(v, MASK)
    for rx, rep in PATTERNS:
        text = rx.sub(rep, text)
    return text


class RedactFilter(logging.Filter):
    """Redacts a record in place (message and traceback) for handlers with their own formatter (uvicorn's)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        if record.exc_info and not getattr(record, "_rr_redacted", False):
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record._rr_redacted = True
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("execution_id", "symbol", "user_id", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers (request errors with tracebacks, access lines): redact those too
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            if not any(isinstance(f, RedactFilter) for f in h.filters):
                h.addFilter(RedactFilter())
