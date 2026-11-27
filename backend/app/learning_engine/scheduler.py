"""Learner process: scheduled learning/intelligence jobs, isolated from the trading worker.

Run: ``python -m app.learning_engine.scheduler`` (docker service ``learner``). A Redis lock ensures a
single learner. Every job run is a ``learning_jobs`` row with an idempotency key per period
(``<job>:<bucket>``), so the same job/period never runs twice; failures retry up to MAX_ATTEMPTS; each
run has a timeout. Jobs never place orders.

Phase 20: the production jobs are **isolated**: each run is a separate process. A thread cannot be stopped, so before
this a job that exceeded its timeout kept running in the background, was marked TIMEOUT, and was started again on the
next loop while the first copy was still running (a slow job could pile up copies, competing for CPU and the
database). An isolated run is terminated when it times out, when an operator cancels it, or when the learner stops,
so it never overlaps itself. Ad-hoc ``Job``s keep the original in-process behaviour (``isolated=False``).
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select
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
POLL_SECONDS = 1.0          # isolated runs: how often the parent checks for a result, timeout, cancel or stop
KILL_GRACE_SECONDS = 5.0    # SIGTERM first, then SIGKILL
# CANCELLED (by an operator) is final for its period; TIMEOUT, FAILED and INTERRUPTED (learner stopping) may retry.
FINAL = ("DONE", "RUNNING", "SKIPPED", "CANCELLED")


_usage = None


def _report_usage() -> None:
    """Phase 20: the learner container's CPU and memory (it includes a running isolated job's process)."""
    global _usage
    if _usage is None:
        from app.services.resources import Reporter
        _usage = Reporter("learner")
    _usage.tick()


def cancel_key(name: str) -> str:
    return f"rr:learner:cancel:{name}"


@dataclass
class Job:
    name: str
    period_seconds: int
    timeout: int
    fn: Callable[[], dict]
    module: str = "strategy_optimisation"
    isolated: bool = False   # Phase 20: run in its own process (fn must be a module-level function)


def bucket(period: int, now: datetime | None = None) -> str:
    t = int((now or datetime.now(UTC)).timestamp())
    return str(t - t % period)


def _isolated_child(fn: Callable[[], dict], conn) -> None:
    """Child process of an isolated run: fresh imports, connections and logging; the result goes back as JSON."""
    configure_logging()
    try:
        res = fn()
        conn.send({"result": json.loads(json.dumps(res, default=str)) if res is not None else None})
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, which records it
        log.exception("job failed")
        conn.send({"error": repr(exc)[:2000]})
    finally:
        conn.close()


def _run_isolated(job: Job, should_stop: Callable[[], bool]) -> tuple[dict, str | None]:
    """Run ``job.fn`` in a separate process. Returns (box, forced status): forced is TIMEOUT, CANCELLED or INTERRUPTED
    when the parent ended the run, else None (box holds "result" or "error")."""
    ctx = multiprocessing.get_context("spawn")   # nothing inherited: no shared database sockets or locks
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_isolated_child, args=(job.fn, send), name=f"learner-{job.name}")
    proc.start()
    send.close()
    r = get_redis()
    r.delete(cancel_key(job.name))
    deadline = time.monotonic() + job.timeout
    box: dict = {}
    forced = None
    try:
        while True:
            _report_usage()
            if recv.poll(POLL_SECONDS):
                try:
                    box.update(recv.recv())
                except EOFError:
                    box["error"] = f"the job process exited without a result (exit code {proc.exitcode})"
                break
            if not proc.is_alive() and not recv.poll(0):
                box["error"] = f"the job process exited without a result (exit code {proc.exitcode})"
                break
            if time.monotonic() > deadline:
                forced = "TIMEOUT"
            elif r.get(cancel_key(job.name)):
                r.delete(cancel_key(job.name))
                forced = "CANCELLED"
            elif should_stop():
                forced = "INTERRUPTED"
            if forced:
                break
    finally:
        recv.close()
        proc.join(0.5 if forced is None else 0)
        if proc.is_alive():
            proc.terminate()
            proc.join(KILL_GRACE_SECONDS)
            if proc.is_alive():
                proc.kill()
                proc.join(KILL_GRACE_SECONDS)
    return box, forced


def run_job(job: Job, now: datetime | None = None, force: bool = False,
            should_stop: Callable[[], bool] = lambda: False) -> dict:
    key = f"{job.name}:{'manual-' + str(time.time_ns()) if force else bucket(job.period_seconds, now)}"
    with session_scope() as db:
        row = db.scalar(select(LearningJob).where(LearningJob.idempotency_key == key))
        if row is None:
            db.execute(insert(LearningJob).values(job_type=job.name, idempotency_key=key, status="QUEUED", attempts=0,
                                                  params={}).on_conflict_do_nothing(index_elements=["idempotency_key"]))
            db.flush()
            row = db.scalar(select(LearningJob).where(LearningJob.idempotency_key == key))
        if row.status in FINAL or row.attempts >= MAX_ATTEMPTS:
            return {"skipped": True, "status": row.status}
        row.status, row.attempts, row.started_at = "RUNNING", row.attempts + 1, datetime.now(UTC)
        job_id = row.id
    started_at = datetime.now(UTC)
    agent_activity(job.module, "processing", f"learning job {job.name} started")
    forced = None
    if job.isolated:
        box, forced = _run_isolated(job, should_stop)
        alive = False
    else:
        box = {}

        def target():
            try:
                box["result"] = job.fn()
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s failed", job.name)
                box["error"] = repr(exc)[:2000]
        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(job.timeout)
        alive = th.is_alive()
    with session_scope() as db:
        row = db.get(LearningJob, job_id)
        row.finished_at = datetime.now(UTC)
        if alive or forced == "TIMEOUT":
            row.status, row.error = "TIMEOUT", f"exceeded {job.timeout}s" + (" (process terminated)" if forced else "")
        elif forced == "CANCELLED":
            row.status, row.error = "CANCELLED", "cancelled by an operator (process terminated)"
        elif forced == "INTERRUPTED":
            row.status, row.error = "INTERRUPTED", "the learner is stopping (process terminated); retried on the next start"
        elif "error" in box:
            row.status, row.error = "FAILED", box["error"]
        else:
            res = box.get("result") or {}
            row.status = "SKIPPED" if res.get("skipped") else "DONE"
            row.result = {k: v for k, v in res.items() if k != "skipped"} | ({"reason": res["skipped"]} if res.get("skipped") else {})
        status = row.status
    agent_activity(job.module, "approved" if status in ("DONE", "SKIPPED") else "error", f"learning job {job.name}: {status}")
    _record_agent_run(job, key, status, box, started_at)
    return {"status": status, **box}


def _record_agent_run(job: Job, key: str, status: str, box: dict, started_at: datetime) -> None:
    """Control-room record of the agent this job belongs to (RR Trading One Phase 9)."""
    from app.orchestration.agents import LEARNER_AGENT
    from app.orchestration.orchestrator import record
    agent = LEARNER_AGENT.get(job.name)
    if agent is None:
        return
    res = box.get("result") or {}
    record(agent, f"learner:{job.name}", started_at=started_at,
           duration_ms=(datetime.now(UTC) - started_at).total_seconds() * 1000,
           status={"DONE": "ok", "SKIPPED": "skipped"}.get(status, "error"), input_refs={"job": key},
           output={k: v for k, v in res.items() if isinstance(v, (int, float, str, bool)) and k != "skipped"},
           evidence={"skipped_reason": res["skipped"]} if res.get("skipped") else {},
           errors=[box["error"][:300]] if "error" in box else
           ([status] if status in ("TIMEOUT", "CANCELLED", "INTERRUPTED") else []))


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


def job_adaptive_weights() -> dict:
    """Phase 12: propose an adaptive strategy weight profile from DEMO realised trades (validated on unseen trades;
    shadow until an administrator promotes it)."""
    from app.learning_engine import adaptive
    with session_scope() as db:
        reg = adaptive.fit_profile(db, "DEMO")
        return {"model": f"{reg.model_key}:v{reg.version}", "status": reg.status, "trades": reg.dataset_ref.get("trades")}


def job_experiments() -> dict:
    """Advance experiments that are waiting on evidence: unit tests, validation, DEMO forward tests."""
    from app.db.models import StrategyExperiment, Symbol
    from app.scanner.service import bars_to_df
    from app.strategies.experimental import lifecycle
    out = {"unit": 0, "validated": 0, "forward": 0}
    with session_scope() as db:
        exps = db.scalars(select(StrategyExperiment).where(StrategyExperiment.stage.in_(
            ["DEVELOPING", "EXPERIMENTAL", "DEMO_TESTING"]), StrategyExperiment.source_code.isnot(None))).all()
        # Parts 27-30 Phase 6: parameter variants (no code of their own) awaiting their first evaluation, 2 per run
        variants = [e for e in db.scalars(select(StrategyExperiment).where(StrategyExperiment.stage == "DISCOVERED",
                                                                        StrategyExperiment.source_code.is_(None))
                                          .order_by(StrategyExperiment.id))
                    if (e.proposal or {}).get("kind") == "parameter_variant"
                    and "variant_evaluation" not in (e.evidence or {})][:2]
        if not exps and not variants:
            return {"skipped": "no experiments awaiting evidence"}
        out["variants"] = 0
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
            if variants and sym is not None:
                from app.strategies.experimental import variants as var
                df = bars_to_df(b.rates(sym.broker_symbol, "H1", 3000))
                spec = {"symbol": sym.broker_symbol, "timeframe": "H1", "point": float(sym.point),
                        "tick_size": float(sym.tick_size), "tick_value": float(sym.tick_value),
                        "spread_points": float(df["spread"].median()) if "spread" in df else 10}
                for e in variants:
                    var.evaluate(db, e, df, spec)
                    out["variants"] += 1
        finally:
            b.close()
    return out


def job_config_profiles() -> dict:
    """Master upgrade: validate queued configuration profiles (2 per run) on broker bars, then apply the
    administrator's automatic promotion policy (DEMO only; off by default)."""
    from app.db.models import Symbol
    from app.db.models_opportunity import StrategyConfigProfile
    from app.scanner.service import bars_to_df
    from app.strategies import profiles
    out = {"validated": 0, "rejected": 0}
    with session_scope() as db:
        queued = db.scalars(select(StrategyConfigProfile).where(StrategyConfigProfile.status == "VALIDATING")
                            .order_by(StrategyConfigProfile.id).limit(2)).all()
        if queued:
            b = _bridge()
            try:
                for p in queued:
                    sym = None
                    if p.symbols:
                        sym = db.scalar(select(Symbol).where(Symbol.broker_symbol == p.symbols[0]))
                    sym = sym or db.scalar(select(Symbol).where(Symbol.scan_enabled.is_(True), Symbol.tradable.is_(True))
                                           .order_by(Symbol.priority).limit(1))
                    if sym is None:
                        return {**out, "skipped": "no scanned symbol to validate on"}
                    tf = (p.timeframes or ["H1"])[0]
                    df = bars_to_df(b.rates(sym.broker_symbol, tf, 6000))
                    spec = {"symbol": sym.broker_symbol, "timeframe": tf, "point": float(sym.point),
                            "tick_size": float(sym.tick_size), "tick_value": float(sym.tick_value),
                            "spread_points": float(df["spread"].median()) if "spread" in df else 10}
                    v = profiles.validate(db, p, df, spec)
                    out["validated" if v["passed"] else "rejected"] += 1
            finally:
                b.close()
        out["promoted"] = profiles.auto_promote(db)
    return out


def job_discovery() -> dict:
    from app.ai import gateway
    if not gateway.configured("strategy_discovery"):
        return {"skipped": "no AI provider is configured for strategy discovery"}
    from app.agents.strategy_discovery import agent
    from app.learning_engine import performance
    with session_scope() as db:
        summary = {d: {k: v.metrics.get("verdict") for k, v in [(r.key, r) for r in performance.latest(db, "DEMO", d)]}
                   for d in ("strategy", "regime", "symbol")}
        return agent.research(db, summary)


def job_improvement_research() -> dict:
    """Engine-triggered improvement research (engine-first operation). The engine finds strategies whose recent losses
    exceed what their validated evidence predicts (binomial test, ``review.loss_significance``) and asks the AI for one
    configuration proposal each, with the finding as the research package. At most ``improvement_daily_cap`` per day,
    never twice for the same strategy within 7 days, never past the AI budget. Proposals are stored and versioned; the
    ``config_profiles`` job validates them, and only validated ones can be activated (with rollback)."""
    from datetime import timedelta

    from app.ai import gateway
    from app.db.models_ai import AIControl
    from app.db.models_opportunity import StrategyConfigProfile
    from app.learning_engine import review
    from app.strategies import profiles, registry
    with session_scope() as db:
        c = db.get(AIControl, 1)
        if c is not None and not c.auto_improvement:
            return {"skipped": "automatic improvement research is switched off"}
        if not gateway.configured("config_proposal"):
            return {"skipped": "no AI provider is configured"}
        if gateway.budget_status().get("exhausted"):
            return {"skipped": "AI budget reached"}
        now = datetime.now(UTC)
        cap = int(c.improvement_daily_cap) if c is not None else 2
        today = db.scalar(select(func.count(StrategyConfigProfile.id)).where(
            StrategyConfigProfile.source == "AI", StrategyConfigProfile.created_at >= now - timedelta(days=1),
            StrategyConfigProfile.evidence["trigger"].astext == "engine")) or 0
        if today >= cap:
            return {"skipped": f"daily cap {cap} reached"}
        recent = set(db.scalars(select(StrategyConfigProfile.strategy_code).where(
            StrategyConfigProfile.source == "AI", StrategyConfigProfile.created_at >= now - timedelta(days=7))))
        weak = []
        for code, cls in registry.all_strategies().items():
            if not dict(cls.params) or code in recent:
                continue
            for mode in ("DEMO", "LIVE"):
                ls = review.loss_significance(db, mode, code)
                if ls["verdict"] == "recurring_defect_suspected":
                    weak.append((ls["p_value"], code, {**ls, "mode": mode}))
                    break
        weak.sort()
        out = {"weak_strategies": len(weak), "proposals": [], "errors": []}
        for _, code, ls in weak[: cap - today]:
            try:
                with db.begin_nested():
                    p = profiles.propose_ai(db, code, None, context=ls)
                out["proposals"].append({"strategy": code, "profile": p.id, "version": p.version})
            except Exception as exc:  # noqa: BLE001 - one failure never stops the engine
                out["errors"].append(f"{code}: {type(exc).__name__}: {str(exc)[:160]}")
        return out


def job_playbooks() -> dict:
    """Grade market playbooks from the trades they governed and expire old ones (no AI call)."""
    from app.intelligence import playbook
    with session_scope() as db:
        return playbook.grade(db)


JOBS = [
    Job("trade_sync", 300, 240, job_trade_sync, "execution", isolated=True),
    Job("failure_analysis", 900, 600, job_failure_analysis, isolated=True),
    Job("portfolio", 900, 300, job_portfolio, "risk_engine", isolated=True),
    Job("performance_snapshot", 86400, 900, job_performance_snapshot, isolated=True),
    Job("drift", 86400, 900, job_drift, isolated=True),
    Job("fingerprints", 86400, 600, job_fingerprints, isolated=True),
    Job("experiments", 3600, 1800, job_experiments, isolated=True),
    Job("train_probability", 7 * 86400, 3600, job_train_probability, "quantitative_engine", isolated=True),
    Job("consensus_weights", 7 * 86400, 600, job_consensus_weights, isolated=True),
    Job("adaptive_weights", 7 * 86400, 900, job_adaptive_weights, isolated=True),
    Job("discovery", 7 * 86400, 900, job_discovery, "claude_agent", isolated=True),
    Job("config_profiles", 3600, 1800, job_config_profiles, isolated=True),
    Job("improvement_research", 86400, 1800, job_improvement_research, "claude_agent", isolated=True),
    Job("playbooks", 3600, 600, job_playbooks, isolated=True),
]
JOB_MAP = {j.name: j for j in JOBS}


def recover_interrupted() -> int:
    """Parts 27-30 Phase 7 (defect found by the backlog monitor): a learner killed outright (container stop, machine
    restart) leaves its job RUNNING, and RUNNING counts as final, so the row stayed RUNNING for ever and its period
    was never retried. Only one learner runs (``LOCK``; it expires 300 s after its holder dies), so when a learner
    starts, any RUNNING row belongs to a process that no longer exists: it becomes INTERRUPTED (retryable)."""
    with session_scope() as db:
        rows = db.scalars(select(LearningJob).where(LearningJob.status == "RUNNING")).all()
        for row in rows:
            row.status, row.finished_at = "INTERRUPTED", datetime.now(UTC)
            row.error = "the learner stopped while this job was running (found RUNNING at the next start)"
        return len(rows)


def main() -> None:
    configure_logging()
    from app.services.monitoring import install_error_counter
    install_error_counter("learner")
    r = get_redis()
    if not r.set(LOCK, "1", nx=True, ex=300):
        log.error("another learner holds the lock; exiting")
        return
    n = recover_interrupted()
    if n:
        log.warning("%d learning job(s) left RUNNING by a stopped learner marked INTERRUPTED", n)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    with session_scope() as db:
        from app.strategies.experimental import lifecycle
        log.info("experimental library: %s", lifecycle.register_library(db))
    while not stop["flag"]:
        r.expire(LOCK, 300)
        _report_usage()
        for job in JOBS:
            if stop["flag"]:
                break
            if r.get(f"rr:learner:trigger:{job.name}"):
                r.delete(f"rr:learner:trigger:{job.name}")
                run_job(job, force=True, should_stop=lambda: stop["flag"])
            else:
                run_job(job, should_stop=lambda: stop["flag"])
            r.expire(LOCK, 300)
        time.sleep(get_settings().learner_interval_seconds)
    r.delete(LOCK)


if __name__ == "__main__":
    main()
