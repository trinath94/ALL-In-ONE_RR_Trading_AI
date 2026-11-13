"""Scan scheduling for many symbols and timeframes (RR Trading One Phase 8).

The original scanner analysed every enabled symbol on every timeframe every cycle (60 s by default) with a flat
thread pool. With a real broker catalogue that has three problems:

* the same closed bar was analysed again every minute, storing a new analysis and often a new signal (and
  notification) for an unchanged decision point;
* nothing bounded a cycle: hundreds of jobs simply ran until done, so cycles overlapped the next interval;
* one slow symbol (a stalled terminal request) held a worker indefinitely.

This module decides **which** jobs run and **in what order**, and runs them under a budget:

* a job (symbol, timeframe) is *due* when the broker has closed a bar newer than the last one analysed for it;
* due jobs are ordered by symbol priority, then by how long they have been waiting (oldest closed bar first), so
  jobs deferred by the budget go first next cycle (fairness, no starvation);
* jobs start only while the cycle budget lasts and the process is not stopping; the rest are *deferred* (left due);
* a job that exceeds its timeout is abandoned (reported, its worker slot freed); a per-job lock in Redis keeps an
  abandoned job from being started a second time while it is still running.

It never changes what a strategy computes: the analysis of a job is the original ``scanner.service.analyse``.
"""
from __future__ import annotations

import json
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

from app.signals.engine import TF_MINUTES

STATUS_KEY = "rr:scanner:status"
HISTORY_KEY = "rr:scanner:history"
HISTORY_LEN = 50


def last_key(symbol_id: int, tf: str) -> str:
    return f"rr:scan:last:{symbol_id}:{tf}"


def inflight_key(symbol_id: int, tf: str) -> str:
    return f"rr:scan:inflight:{symbol_id}:{tf}"


@dataclass(order=True)
class Job:
    sort_key: tuple = field(init=False, repr=False)
    priority: int
    bar_epoch: int          # latest closed bar the broker reports (0 = unknown)
    tf: str
    symbol_id: int
    symbol: str = ""

    def __post_init__(self):
        # priority first; then oldest pending bar (bar close time), then higher timeframe, then name: deterministic
        close = self.bar_epoch + TF_MINUTES.get(self.tf, 60) * 60 if self.bar_epoch else 0
        self.sort_key = (self.priority, close, -TF_MINUTES.get(self.tf, 60), self.symbol, self.tf)


def is_due(last_analysed: int | None, closed_bar: int | None) -> bool:
    """Due when the broker reports a closed bar we have not analysed. Unknown bar (probe failed): due, so the
    analysis itself reports the problem rather than the job silently never running."""
    if closed_bar is None:
        return True
    return last_analysed is None or closed_bar > last_analysed


def order(jobs: list[Job]) -> list[Job]:
    return sorted(jobs)


@dataclass
class CycleReport:
    started_at: str
    timeframes: list[str]
    symbols: int
    jobs_total: int = 0
    not_due: int = 0
    due: int = 0
    analysed: int = 0
    insufficient_data: int = 0
    errors: int = 0
    deferred: int = 0
    timed_out: int = 0
    in_flight_elsewhere: int = 0
    cancelled: int = 0
    duration_s: float = 0.0
    budget_s: float = 0.0
    by_timeframe: dict = field(default_factory=dict)
    error_samples: list = field(default_factory=list)

    def dict(self) -> dict:
        return dict(self.__dict__)


def run_jobs(jobs: list[Job], work: Callable[[Job], dict | None], *, max_workers: int, budget_s: float,
             job_timeout_s: float, should_stop: Callable[[], bool], report: CycleReport,
             clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Run ordered jobs on a bounded pool within the cycle budget. Returns the work() results that completed."""
    start = clock()
    deadline = start + budget_s
    pending = deque(jobs)
    running: dict[Future, tuple[Job, float]] = {}
    results: list[dict] = []
    tf_counts: Counter = Counter()
    ex = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="scan")
    try:
        while pending or running:
            if should_stop():
                report.cancelled += len(pending)
                pending.clear()
                break
            while pending and len(running) < max_workers and clock() < deadline:
                job = pending.popleft()
                running[ex.submit(work, job)] = (job, clock())
            if not running:
                break                                     # budget spent with nothing running: the rest is deferred
            done, _ = wait(list(running), timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                job, _t0 = running.pop(f)
                try:
                    r = f.result()
                except Exception as exc:  # noqa: BLE001 - work() already isolates; this is a last resort
                    r = {"symbol": job.symbol, "tf": job.tf, "error": repr(exc)[:300]}
                if r is None:
                    continue
                kind = r.get("outcome", "analysed")
                if kind == "skipped_inflight":
                    report.in_flight_elsewhere += 1
                    continue
                if r.get("error"):
                    report.errors += 1
                    if len(report.error_samples) < 5:
                        report.error_samples.append(f"{r.get('symbol')} {r.get('tf')}: {r['error'][:160]}")
                elif kind == "insufficient_data":
                    report.insufficient_data += 1
                else:
                    report.analysed += 1
                    tf_counts[job.tf] += 1
                results.append(r)
            now = clock()
            for f, (job, t0) in list(running.items()):
                if now - t0 > job_timeout_s:
                    running.pop(f)                        # abandoned: the thread may finish later; its lock and its
                    report.timed_out += 1                 # bar marker keep it from being run twice
                    if len(report.error_samples) < 5:
                        report.error_samples.append(f"{job.symbol} {job.tf}: timed out after {job_timeout_s:.0f}s")
        report.deferred += len(pending)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    report.duration_s = round(clock() - start, 2)
    report.budget_s = budget_s
    report.by_timeframe = dict(tf_counts)
    return results


def publish_status(r, report: CycleReport) -> None:
    """Latest cycle + a short history, for the console. Best effort: scanning never fails on this."""
    try:
        payload = json.dumps(report.dict(), default=str)
        p = r.pipeline()
        p.set(STATUS_KEY, payload)
        p.lpush(HISTORY_KEY, payload)
        p.ltrim(HISTORY_KEY, 0, HISTORY_LEN - 1)
        p.execute()
    except Exception:  # noqa: BLE001
        pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
