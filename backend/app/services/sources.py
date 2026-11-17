"""Data-source registry (Parts 27-30 Phase 4).

Every external input the engine reads has a row with its *observed* health: the engine records each use (ok or the
error it got) where the data is fetched. Nothing is assumed: a source never used is ``unknown``, a calendar without a
provider is ``not_configured``.

AI providers are not duplicated here: their health comes from the existing AI usage ledger (``ai_usage``) and is
merged into the API view.

Writes are batched in memory and flushed in their own short transaction at most every ``FLUSH_S`` seconds per source
(or at once when the status changes), so recording never holds a lock inside an analysis transaction.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models_opportunity import DataSource

log = logging.getLogger("sources")
FLUSH_S = 30.0
KNOWN = {
    "mt5_market_data": ("MT5 market data (bars and quotes via the RR MT5 Connector)", "market_data", "mt5"),
    "economic_calendar": ("Economic calendar", "economic_calendar", "generic_json"),
}

_lock = threading.Lock()
_pending: dict[str, dict] = {}


def ensure(db: Session) -> None:
    from app.core.config import get_settings
    s = get_settings()
    have = {r.key for r in db.scalars(select(DataSource))}
    for key, (name, cat, provider) in KNOWN.items():
        if key in have:
            continue
        status = "unknown"
        notes = ""
        if key == "economic_calendar" and (s.news_provider != "generic_json" or not s.news_api_url):
            status, notes = "not_configured", "set RR_NEWS_PROVIDER=generic_json and RR_NEWS_API_URL"
        # ON CONFLICT: several roles may register the same source at once
        db.execute(pg_insert(DataSource).values(key=key, name=name, category=cat, provider=provider, enabled=True,
                                                status=status, ok_count=0, error_count=0, notes=notes,
                                                updated_at=datetime.now(UTC))
                   .on_conflict_do_nothing(index_elements=["key"]))
    db.flush()


def observe(key: str, ok: bool, error: str | None = None, latency_ms: float | None = None) -> None:
    """Record one use of a source. Best effort: never raises."""
    if key not in KNOWN:
        return
    now = time.monotonic()
    with _lock:
        p = _pending.setdefault(key, {"ok": 0, "err": 0, "last_error": None, "latency": None, "status": None,
                                      "flushed": 0.0, "last_ok": None, "last_err": None})
        status = "ok" if ok else "error"
        changed = p["status"] != status
        p["status"] = status
        if ok:
            p["ok"] += 1
            p["last_ok"] = datetime.now(UTC)
        else:
            p["err"] += 1
            p["last_err"] = datetime.now(UTC)
            p["last_error"] = (error or "error")[:1000]
        if latency_ms is not None:
            p["latency"] = round(latency_ms, 1)
        due = changed or now - p["flushed"] >= FLUSH_S
        if not due:
            return
        batch = dict(p)
        p.update(ok=0, err=0, flushed=now)
    _flush(key, batch)


def _flush(key: str, b: dict) -> None:
    from app.db.session import session_scope
    try:
        with session_scope() as db:
            ensure(db)
            row = db.scalar(select(DataSource).where(DataSource.key == key).with_for_update())
            row.status = b["status"]
            row.ok_count += b["ok"]
            row.error_count += b["err"]
            if b["last_ok"]:
                row.last_ok_at = b["last_ok"]
            if b["last_err"]:
                row.last_error_at, row.last_error = b["last_err"], b["last_error"]
            if b["latency"] is not None:
                row.latency_ms = b["latency"]
            row.updated_at = datetime.now(UTC)
    except Exception:  # noqa: BLE001 - the registry must not break the work it describes
        log.warning("source registry update failed for %s", key, exc_info=True)


def listing(db: Session) -> dict:
    """Registry rows plus AI providers' health from the usage ledger (last 24 h)."""
    from datetime import timedelta

    from sqlalchemy import func

    from app.db.models_ai import AIProvider, AIUsage
    ensure(db)
    rows = [{"key": r.key, "name": r.name, "category": r.category, "provider": r.provider, "enabled": r.enabled,
             "status": r.status, "last_ok_at": r.last_ok_at, "last_error_at": r.last_error_at, "last_error": r.last_error,
             "ok_count": r.ok_count, "error_count": r.error_count, "latency_ms": r.latency_ms, "notes": r.notes}
            for r in db.scalars(select(DataSource).order_by(DataSource.id))]
    since = datetime.now(UTC) - timedelta(hours=24)
    usage = {(p, st): n for p, st, n in db.execute(select(AIUsage.provider, AIUsage.status, func.count())
                                                     .where(AIUsage.created_at >= since)
                                                     .group_by(AIUsage.provider, AIUsage.status))}
    ai = []
    for p in db.scalars(select(AIProvider).order_by(AIProvider.id)):
        ok = usage.get((p.name, "ok"), 0)
        bad = sum(n for (name, st), n in usage.items() if name == p.name and st != "ok")
        ai.append({"key": f"ai:{p.name}", "name": p.name, "category": "ai_provider", "provider": p.kind,
                   "enabled": p.enabled, "status": "unknown" if not ok + bad else ("ok" if ok and not bad else
                                                                                     "error" if not ok else "degraded"),
                   "ok_24h": ok, "errors_24h": bad, "notes": "from the AI usage ledger"})
    return {"sources": rows, "ai_providers": ai}
