"""Rotating file log in %LOCALAPPDATA%\\RRMT5Connector\\logs (secrets are never logged)."""
from __future__ import annotations

import logging
import logging.handlers
import re

from rr_connector import paths

SECRET_RE = re.compile(r"(api[_-]?key|secret|signature|token|password)(['\"]?\s*[:=]\s*['\"]?)([^'\",\s]+)", re.I)


class Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", msg)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def setup(level: int = logging.INFO) -> None:
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / "connector.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    fh.addFilter(Redact())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
