"""Learner process: scheduled learning/intelligence jobs, isolated from the trading worker.

Run: ``python -m app.learning_engine.scheduler`` (docker service ``learner``). A Redis lock ensures a
single learner. Every job run is a ``learning_jobs`` row with an idempotency key per period
(``<job>:<bucket>``), so the same job/period never runs twice; failures retry up to MAX_ATTEMPTS; each
run has a timeout. Jobs never place orders.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models_intel import LearningJob
from app.db.session import session_scope
from app.services.events import agent_activity, get_redis

log = logging.getLogger("learner")
UTC = timezone.utc
LOCK = "rr:learner:lock"
MAX_ATTEMPTS = 3


@dataclass
class Job:
    name: str
    period_seconds: int
    timeout: int
    fn: Callable[[], dict]
    module: str = "strategy_optimisation"


def bucket(period: int, now: datetime | None = None) -> str:
    t = int((now or datetime.now(UTC)).timestamp())
    return str(t - t % period)


def run_job(job: Job, now: datetime | None = None, force: bool = False) -> dict:
    key = f"{job.name}:{'manual-' + str(time.time_ns()) if force else bucket(job.period_seconds, now)}"
    with session_scope() as db:
        row = db.scalar(select(LearningJob).where(LearningJob.idempotency_key == key))
        if row is None:
            db.execute(insert(LearningJob).values(job_type=job.name, idempotency_key=key, status="QUEUED", attempts=0,
                                                  params={}).on_conflict_do_nothing(index_elements=["idempotency_key"]))
            db.flush()
            row = db.scalar(select(LearningJob).where(LearningJob.idempotency_key == key))
        if row.status in ("DONE", "RUNNING", "SKIPPED") or row.attempts >= MAX_ATTEMPTS:
            return {"skipped": True, "status": row.status}
        row.status, row.attempts, row.started_at = "RUNNING", row.attempts + 1, datetime.now(UTC)
        job_id = row.id
    box: dict = {}

    def target():
        try:
            box["result"] = job.fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job.name)
            box["error"] = repr(exc)[:2000]
    agent_activity(job.module, "processing", f"learning job {job.name} started")
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(job.timeout)
    with session_scope() as db:
        row = db.get(LearningJob, job_id)
        row.finished_at = datetime.now(UTC)
        if th.is_alive():
            row.status, row.error = "TIMEOUT", f"exceeded {job.timeout}s"
        elif "error" in box:
            row.status, row.error = "FAILED", box["error"]
        else:
            res = box.get("result") or {}
            row.status = "SKIPPED" if res.get("skipped") else "DONE"
            row.result = {k: v for k, v in res.items() if k != "skipped"} | ({"reason": res["skipped"]} if res.get("skipped") else {})
        status = row.status
    agent_activity(job.module, "approved" if status in ("DONE", "SKIPPED") else "error", f"learning job {job.name}: {status}")
    return {"status": status, **box}


# ---------------------------------------------------------------- job implementations ------------
def _bridge():
    from app.execution.bridge import BridgeClient
    return BridgeClient()


def job_trade_sync() -> dict:
    from app.learning_engine import collector, features
    from app.services import trading as tr
    with session_scope() as db:
        acct = tr.active_account(db)
        if acct is None:
            return {"skipped": "no active account"}
        b = _bridge()
        try:
            r = collector.sync_account_history(db, b, acct)
            r["enriched"] = features.enrich_pending(db, b)
        finally:
            b.close()
        r.pop("closed_position_ids", None)
        return r


def job_failure_analysis() -> dict:
    from app.learning_engine import failure
    out = {}
    with session_scope() as db:
        b = _bridge()
        try:
            for mode in ("DEMO", "LIVE"):
                out[mode] = failure.run(db, mode, b)
            out["missed"] = failure.missed_opportunities(db, b)
        finally:
            b.close()
    return out


def job_performance_snapshot() -> dict:
    from app.learning_engine import performance
    with session_scope() as db:
        return {m: performance.snapshot(db, m) for m in ("DEMO", "LIVE")}


def job_portfolio() -> dict:
    from app.intelligence import portfolio
    from app.services import trading as tr
    with session_scope() as db:
        b = _bridge()
        try:
            r = portfolio.refresh_corr(db, b)
        finally:
            b.close()
        acct = tr.active_account(db)
        if acct:
            portfolio.snapshot(db, acct)
        return r


def job_drift() -> dict:
    from app.learning_engine import drift
    with session_scope() as db:
        return drift.check(db)


def job_fingerprints() -> dict:
    from app.intelligence import fingerprint
    with session_scope() as db:
        return fingerprint.refresh(db)


def job_train_probability() -> dict:
    """Weekly: rebuild the dataset from broker history for the most active symbols/strategies and train.
    Ends in VALIDATED or REJECTED — never PRODUCTION."""
    import pandas as pd
    from sqlalchemy import func as f
    from app.db.models import Symbol, TradingSignal
    from app.learning_engine import datasets, models
    from app.scanner.service import bars_to_df
    with session_scope() as db:
        top = db.execute(select(TradingSignal.strategies).order_by(TradingSignal.id.desc()).limit(2000)).all()
        counts: dict[str, int] = {}
        for (strats,) in top:
            for s in strats:
                counts[s["strategy"]] = counts.get(s["strategy"], 0) + 1
        codes = [c for c, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:15]]
        if not codes:
            return {"skipped": "no live signals yet to select strategies"}
        syms = db.scalars(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))
                          .order_by(Symbol.priority).limit(4)).all()
        b = _bridge()
        frames = []
        try:
            for s in syms:
                df = bars_to_df(b.rates(s.broker_symbol, "H1", 6000))
                if len(df) > 1500:
                    spec = datasets.DatasetSpec(codes, horizon=48, spread_points=float(df["spread"].median()) if "spread" in df else 10,
                                                point=float(s.point), step=1)
                    frames.append(datasets.build(df, spec, s.broker_symbol, "H1"))
        finally:
            b.close()
        if not frames:
            return {"skipped": "insufficient broker history"}
        ds = pd.concat(frames, ignore_index=True)
        reg = models.train(db, ds, horizon=48, dataset_ref={"source": "broker history", "symbols": [s.broker_symbol for s in syms],
                                                            "strategies": codes, "timeframe": "H1"})
        _ = f
        return {"model": f"{reg.model_key}:v{reg.version}", "status": reg.status, "rows": reg.dataset_ref.get("rows")}


def job_consensus_weights() -> dict:
    from app.learning_engine import consensus_weights
    with session_scope() as db:
        reg = consensus_weights.fit(db)
        return {"model": f"consensus_weights:v{reg.version}", "status": reg.status}


def job_experiments() -> dict:
    """Advance experiments that are waiting on evidence: unit tests, validation, DEMO forward tests."""
    from app.db.models import StrategyExperiment, Symbol
    from app.scanner.service import bars_to_df
    from app.strategies.experimental import lifecycle
    out = {"unit": 0, "validated": 0, "forward": 0}
    with session_scope() as db:
        exps = db.scalars(select(StrategyExperiment).where(StrategyExperiment.stage.in_(
            ["DEVELOPING", "EXPERIMENTAL", "DEMO_TESTING"]), StrategyExperiment.source_code.isnot(None))).all()
        if not exps:
            return {"skipped": "no experiments awaiting evidence"}
        sym = db.scalar(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True)).order_by(Symbol.priority).limit(1))
        b = _bridge()
        try:
            for e in exps:
                st = lifecycle.normalise(e.stage)
                if st == "DEVELOPING" and (e.validation or {}).get("static", {}).get("ok"):
                    lifecycle.run_unit(db, e)
                    out["unit"] += 1
                elif st == "EXPERIMENTAL" and sym is not None:
                    df = bars_to_df(b.rates(sym.broker_symbol, "H1", 6000))
                    lifecycle.run_validation(db, e, df, {"symbol": sym.broker_symbol, "point": float(sym.point),
                                                         "tick_size": float(sym.tick_size), "tick_value": float(sym.tick_value),
                                                         "spread_points": float(df["spread"].median()) if "spread" in df else 10})
                    out["validated"] += 1
                elif st == "DEMO_TESTING" and sym is not None:
                    df = bars_to_df(b.rates(sym.broker_symbol, "H1", 600))
                    lifecycle.forward_step(db, e, sym.broker_symbol, df)
                    out["forward"] += 1
        finally:
            b.close()
    return out


def job_discovery() -> dict:
    if not get_settings().anthropic_api_key:
        return {"skipped": "Claude API key not configured"}
    from app.agents.strategy_discovery import agent
    from app.learning_engine import performance
    with session_scope() as db:
        summary = {d: {k: v.metrics.get("verdict") for k, v in [(r.key, r) for r in performance.latest(db, "DEMO", d)]}
                   for d in ("strategy", "regime", "symbol")}
        return agent.research(db, summary)


JOBS = [
    Job("trade_sync", 300, 240, job_trade_sync, "execution"),
    Job("failure_analysis", 900, 600, job_failure_analysis),
    Job("portfolio", 900, 300, job_portfolio, "risk_engine"),
    Job("performance_snapshot", 86400, 900, job_performance_snapshot),
    Job("drift", 86400, 900, job_drift),
    Job("fingerprints", 86400, 600, job_fingerprints),
    Job("experiments", 3600, 1800, job_experiments),
    Job("train_probability", 7 * 86400, 3600, job_train_probability, "quantitative_engine"),
    Job("consensus_weights", 7 * 86400, 600, job_consensus_weights),
    Job("discovery", 7 * 86400, 900, job_discovery, "claude_agent"),
]
JOB_MAP = {j.name: j for j in JOBS}


def main() -> None:
    configure_logging()
    r = get_redis()
    if not r.set(LOCK, "1", nx=True, ex=300):
        log.error("another learner holds the lock; exiting")
        return
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    with session_scope() as db:
        from app.strategies.experimental import lifecycle
        log.info("experimental library: %s", lifecycle.register_library(db))
    while not stop["flag"]:
        r.expire(LOCK, 300)
        for job in JOBS:
            if stop["flag"]:
                break
            if r.get(f"rr:learner:trigger:{job.name}"):
                r.delete(f"rr:learner:trigger:{job.name}")
                run_job(job, force=True)
            else:
                run_job(job)
            r.expire(LOCK, 300)
        time.sleep(get_settings().learner_interval_seconds)
    r.delete(LOCK)


if __name__ == "__main__":
    main()
