"""Supervised entrypoint for the learner in RR Trading One (``python -m app.learning_engine.entry``).

The original scheduler (``app.learning_engine.scheduler.main``) is used unchanged. It holds ``rr:learner:lock``
with a 300 s TTL, and its jobs can outlast a container's stop grace period. When Docker has to kill it, the lock
outlives the process (Redis persists it), and the next start exits with "another learner holds the lock",
restarting in a loop until the TTL expires. On a desktop that is stopped and started often, this is common.

This wrapper records which container owns the learner. At start-up it clears a leftover lock **only** if that
record names this same container (a stable compose hostname), i.e. the lock can only be its own previous
incarnation's. A lock owned by anything else is left alone, so the original single-learner guarantee holds.
"""
from __future__ import annotations

import logging
import socket

from app.learning_engine import scheduler
from app.services.events import get_redis

log = logging.getLogger("learner")
OWNER_KEY = "rr:learner:owner"


def _text(v):
    return v.decode() if isinstance(v, bytes) else v


def clear_own_stale_lock(r=None, owner: str | None = None) -> bool:
    """Returns True if a stale lock left by this same container was removed."""
    r = r or get_redis()
    owner = owner or socket.gethostname()
    cleared = False
    if r.exists(scheduler.LOCK) and _text(r.get(OWNER_KEY)) == owner:
        r.delete(scheduler.LOCK)
        cleared = True
        log.warning("removed the learner lock left by this container's previous run")
    if not r.exists(scheduler.LOCK):
        r.set(OWNER_KEY, owner)
    return cleared


def main() -> None:
    clear_own_stale_lock()
    scheduler.main()


if __name__ == "__main__":
    main()
