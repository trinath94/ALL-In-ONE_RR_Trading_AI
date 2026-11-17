"""Operational monitoring additions (Parts 27-30 Phase 7, Part 30.8 and 30.1).

* **API latency and errors:** the API records, per minute in Redis, the request count, the total time and the
  server errors (5xx). The 15-minute view gives the mean latency and the error rate.
* **Log anomalies:** every process counts its ERROR and CRITICAL log records per minute, under its own name
  (role / service).
* **Database connections:** read from ``pg_stat_activity``, by state.
* **Queue backlog:** a learner job is *overdue* when its last finished run is older than twice its period, and
  *stuck* when it has been RUNNING longer than twice its timeout. The scanner's last cycle reports deferred jobs.
  ``check_backlog`` raises a system event and a notification at most once an hour while a problem lasts.

Counters expire after an hour. Recording is best effort and never affects the work it measures.
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

WINDOW_MIN = 15


def _minute(t: float | None = None) -> int:
    return int((t or time.time()) // 60)


def record_request(ms: float, status: int) -> None:
    try:
        from app.services.events import get_redis
        r = get_redis()
        k = f"rr:mon:api:{_minute()}"
        p = r.pipeline()
        p.hincrby(k, "n", 1)
        p.hincrbyfloat(k, "ms", round(ms, 3))
        if status >= 500:
            p.hincrby(k, "err", 1)
        p.expire(k, 3600)
        p.execute()
    except Exception:  # noqa: BLE001
        pass


class ErrorCounter(logging.Handler):
    """Counts ERROR/CRITICAL records per process name per minute (log anomaly signal)."""

    def __init__(self, name: str):
        super().__init__(level=logging.ERROR)
        self.name_ = name

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("redis"):
            return
        try:
            from app.services.events import get_redis
            r = get_redis()
            k = f"rr:mon:logerr:{self.name_}:{_minute()}"
            r.incr(k)
            r.expire(k, 3600)
        except Exception:  # noqa: BLE001 - never log from here (recursion)
            pass


def install_error_counter(name: str) -> None:
    root = logging.getLogger()
    if not any(isinstance(h, ErrorCounter) for h in root.handlers):
        root.addHandler(ErrorCounter(name))


def _api_window(r) -> dict:
    now = _minute()
    n = err = 0
    ms = 0.0
    for m in range(now - WINDOW_MIN + 1, now + 1):
        h = r.hgetall(f"rr:mon:api:{m}")
        if h:
            g = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in h.items()}
            n += int(g.get("n", 0))
            err += int(g.get("err", 0))
            ms += float(g.get("ms", 0))
    return {"requests": n, "mean_latency_ms": round(ms / n, 1) if n else None,
            "server_errors": err, "error_rate": round(err / n, 4) if n else None, "window_minutes": WINDOW_MIN}


def _log_errors(r) -> dict:
    now = _minute()
    out: dict[str, int] = {}
    for key in r.scan_iter("rr:mon:logerr:*"):
        k = key.decode() if isinstance(key, bytes) else key
        _, _, _, name, minute = k.split(":")
        if int(minute) > now - WINDOW_MIN:
            out[name] = out.get(name, 0) + int(r.get(key) or 0)
    return out


def backlog(db) -> dict:
    from sqlalchemy import func, select

    from app.db.models_intel import LearningJob
    from app.learning_engine.scheduler import JOBS
    now = datetime.now(UTC)
    overdue, stuck = [], []
    for j in JOBS:
        last = db.scalar(select(func.max(LearningJob.finished_at)).where(LearningJob.job_type == j.name))
        if last is None or now - last > timedelta(seconds=2 * j.period_seconds):
            overdue.append({"job": j.name, "last_finished": last.isoformat() if last else None,
                            "period_s": j.period_seconds})
        run = db.scalar(select(LearningJob).where(LearningJob.job_type == j.name, LearningJob.status == "RUNNING")
                        .order_by(LearningJob.id.desc()).limit(1))
        if run is not None and run.started_at and now - run.started_at > timedelta(seconds=2 * j.timeout):
            stuck.append({"job": j.name, "running_since": run.started_at.isoformat(), "timeout_s": j.timeout})
    scan = None
    try:
        import json

        from app.scanner import scheduler as sched
        from app.services.events import get_redis
        raw = get_redis().get(sched.STATUS_KEY)
        if raw:
            st = json.loads(raw)
            scan = {k: st.get(k) for k in ("finished_at", "due", "analysed", "deferred", "timed_out", "errors")}
    except Exception:  # noqa: BLE001
        pass
    return {"learner_overdue": overdue, "learner_stuck": stuck, "scanner_last_cycle": scan}


def snapshot(db) -> dict:
    from sqlalchemy import text

    from app.services.events import get_redis
    r = get_redis()
    conns = {str(state or "unknown"): int(n) for state, n in db.execute(text(
        "SELECT state, count(*) FROM pg_stat_activity WHERE datname = current_database() GROUP BY state"))}
    max_conn = db.execute(text("SHOW max_connections")).scalar()
    return {"at": datetime.now(UTC).isoformat(), "api": _api_window(r), "log_errors_15m": _log_errors(r),
            "db_connections": {"by_state": conns, "total": sum(conns.values()), "max": int(max_conn)},
            "backlog": backlog(db)}


def check_backlog(db) -> list[str]:
    """Raise an alert (system event + notification) for overdue / stuck jobs, at most once an hour."""
    from app.notifications.service import notify
    from app.services.events import get_redis, system_event
    b = backlog(db)
    msgs = [f"learner job {o['job']} overdue (last finished {o['last_finished'] or 'never'})" for o in b["learner_overdue"]
            if o["last_finished"] is not None]           # never-run jobs are not alarms right after installation
    msgs += [f"learner job {s['job']} running since {s['running_since']} (> 2x timeout)" for s in b["learner_stuck"]]
    if msgs and get_redis().set("rr:mon:backlog-alert", "1", nx=True, ex=3600):
        system_event(db, "monitor", "warning", "Job backlog", {"problems": msgs})
        notify(db, "SERVER_FAILURE", "Background jobs behind schedule", "; ".join(msgs)[:1000], severity="warning")
    return msgs
