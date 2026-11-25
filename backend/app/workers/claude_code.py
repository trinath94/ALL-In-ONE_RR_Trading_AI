"""The ``claude-code`` role: runs queued Claude Code tasks one at a time in the isolated workspace.

Usage: ``python -m app.workers.claude_code``

It holds no broker key (the compose service does not get RR_ENCRYPTION_KEY), never touches the trading roles, and
idles while Claude Code is switched off. If it is stopped or Claude is unavailable, the engine keeps trading.
"""
from __future__ import annotations

import logging
import signal
import time

from app.ai import claude_code as cc
from app.core.logging import configure_logging
from app.db.session import session_scope

log = logging.getLogger("worker.claude_code")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True


def cycle() -> int | None:
    with session_scope() as db:
        if not cc.control(db).claude_code_enabled:
            return None
    tid = cc.next_queued()
    if tid is not None:
        log.info("claude-code task #%s -> %s", tid, cc.run_task(tid))
    return tid


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    if cc.workspace() is not None:
        try:
            cc.init_workspace()
        except Exception as exc:  # noqa: BLE001
            log.error("Claude Code workspace not ready: %s", exc)
        try:
            problem = cc.ensure_test_database()
            if problem:
                log.warning("Claude Code test database: %s (patch tests will fail until it is fixed)", problem)
        except Exception as exc:  # noqa: BLE001
            log.warning("Claude Code test database not ready: %s", exc)
    n = cc.recover_interrupted()
    if n:
        log.warning("%s Claude Code task(s) were interrupted by a restart; they can be resumed", n)
    while not _stop:
        try:
            if cycle() is None:
                time.sleep(3)
        except Exception:  # noqa: BLE001 - never crash-loop the role
            log.exception("claude-code cycle failed")
            time.sleep(10)


if __name__ == "__main__":
    main()
