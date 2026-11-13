"""Part 25/26 API groups: /api/learning/* (learning & strategy evolution) and /api/intelligence/*.
Additive; existing routes are untouched. All data comes from the database / live services."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user, require, user_perms
from app.core.config import get_settings
from app.db.models import BacktestJob, ClosedPosition, StrategyExperiment, TradingSignal, User
from app.db.models_intel import (AIRecommendation, AnomalyEvent, ApprovalRecord, ConsensusDecision, DriftEvent,
                                 FailureReport, LearningJob, LearningMemory, MissedOpportunity, ModelPrediction,
                                 ModelRegistry, PositionAdjustment, RegimeHistory, SyncState, TradeFeatures)
from app.db.session import get_db
from app.learning_engine import performance as perf
from app.services.events import audit, get_redis

learning = APIRouter(prefix="/api/learning", tags=["learning"])
intel = APIRouter(prefix="/api/intelligence", tags=["intelligence"])
UTC = timezone.utc


def _mode(mode: str) -> str:
    if mode not in ("DEMO", "LIVE"):
        raise HTTPException(400, "mode must be DEMO or LIVE")
    return mode


# ================================================================ LEARNING ========================
@learning.get("/overview")
def overview(mode: str = "DEMO", _: User = Depends(current_user), db: Session = Depends(get_db)):
    _mode(mode)
    trades = perf.load_trades(db, mode)
    stages = Counter(s for (s,) in db.execute(select(StrategyExperiment.stage)))
    models_ = Counter(s for (s,) in db.execute(select(ModelRegistry.status)))
    since = datetime.now(UTC) - timedelta(days=7)
    return {
        "mode": mode,
        "trades_analysed": len(trades), "wins": sum(1 for t in trades if t["net"] > 0),
        "losses": sum(1 for t in trades if t["net"] < 0),
        "overall": perf.compute_metrics(trades),
        "features_enriched": db.scalar(select(func.count(TradeFeatures.id)).where(TradeFeatures.mode == mode)),
        "experiments_by_stage": dict(stages),
        "backtest_queue": db.scalar(select(func.count(BacktestJob.id)).where(BacktestJob.status.in_(["QUEUED", "RUNNING"]))),
        "models_by_status": dict(models_),
        "drift_alerts_7d": db.scalar(select(func.count(DriftEvent.id)).where(DriftEvent.created_at >= since)),
        "open_recommendations": db.scalar(select(func.count(AIRecommendation.id)).where(AIRecommendation.status == "OPEN")),
        "pending_approvals": {
            "experiments": stages.get("APPROVAL_REQUIRED", 0),
            "models": models_.get("VALIDATED", 0) + models_.get("APPROVED", 0)},
        "sync": [{"account_id": s.account_id, "stream": s.stream, "last_time": s.last_time, "status": s.status,
                  "detail": s.detail} for s in db.scalars(select(SyncState))],
        "consensus_mode": get_settings().consensus_mode,
    }


@learning.get("/performance")
def performance(mode: str = "DEMO", dimension: str | None = None, _: User = Depends(current_user),
                db: Session = Depends(get_db)):
    trades = perf.load_trades(db, _mode(mode))
    dims = perf.by_dimension(trades)
    if dimension:
        if dimension not in dims:
            raise HTTPException(400, f"dimension must be one of {list(dims)}")
        dims = {dimension: dims[dimension]}
    return {"mode": mode, "overall": perf.compute_metrics(trades), "dimensions": dims,
            "min_trades_for_verdict": perf.MIN_TRADES}


@learning.get("/performance/correlation")
def correlation(mode: str = "DEMO", _: User = Depends(current_user), db: Session = Depends(get_db)):
    return perf.strategy_correlation(perf.load_trades(db, _mode(mode)))


@learning.get("/trades")
def trades(mode: str = "DEMO", limit: int = 200, _: User = Depends(current_user), db: Session = Depends(get_db)):
    q = (select(ClosedPosition, TradeFeatures).outerjoin(TradeFeatures, TradeFeatures.closed_position_id == ClosedPosition.id)
         .where((ClosedPosition.mode == _mode(mode)) | (TradeFeatures.mode == mode))
         .order_by(ClosedPosition.closed_at.desc()).limit(min(limit, 2000)))
    out = []
    for cp, tf in db.execute(q):
        out.append({"ticket": cp.ticket, "symbol": cp.symbol, "direction": cp.direction, "closed_at": cp.closed_at,
                    "net": round(float(cp.profit) + float(cp.commission or 0) + float(cp.swap or 0), 2), "r": cp.r_multiple,
                    "partial_exits": cp.partial_exits, "sync_status": cp.sync_status, "strategies": cp.strategy_codes,
                    "outcome": tf.outcome if tf else None, "regime": tf.regime if tf else None,
                    "session": tf.session if tf else None, "mae_r": tf.mae_r if tf else None, "mfe_r": tf.mfe_r if tf else None,
                    "holding_seconds": tf.holding_seconds if tf else None, "grade": tf.grade if tf else None,
                    "data_quality": tf.data_quality if tf else {"enrichment": "pending"}})
    return out


@learning.get("/memory")
def memory(mode: str = "DEMO", _: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"kind": m.kind, "key": m.key, "summary": m.summary, "evidence_count": m.evidence_count, "updated_at": m.updated_at}
            for m in db.scalars(select(LearningMemory).where(LearningMemory.mode == _mode(mode))
                                .order_by(LearningMemory.evidence_count.desc()).limit(200))]


@learning.get("/failures")
def failures(mode: str = "DEMO", _: User = Depends(current_user), db: Session = Depends(get_db)):
    reps = db.scalars(select(FailureReport).where(FailureReport.mode == _mode(mode)).order_by(FailureReport.id.desc()).limit(500)).all()
    causes, strengths = Counter(), Counter()
    by_strategy: dict[str, Counter] = defaultdict(Counter)
    for r in reps:
        cp = db.get(ClosedPosition, r.closed_position_id)
        for c in r.causes:
            causes[c["cause"]] += 1
            for s in (cp.strategy_codes or ["manual"]):
                by_strategy[s][c["cause"]] += 1
        for s in r.strengths:
            strengths[s["strength"]] += 1
    missed = db.scalars(select(MissedOpportunity).order_by(MissedOpportunity.id.desc()).limit(200)).all()
    mo = Counter(m.hypothetical_outcome for m in missed)
    return {"losing_patterns": dict(causes.most_common()), "winning_patterns": dict(strengths.most_common()),
            "repeated_errors_by_strategy": {k: dict(v.most_common(4)) for k, v in by_strategy.items() if sum(v.values()) >= 3},
            "recent": [{"closed_position_id": r.closed_position_id, "outcome": r.outcome, "causes": r.causes,
                        "strengths": r.strengths, "at": r.created_at} for r in reps[:50]],
            "missed_opportunities": {"counts": dict(mo), "note": "hypothetical outcomes of rejected/expired signals — "
                                                                 "excluded from performance and not proof a rejection was wrong",
                                     "recent": [{"signal_id": m.signal_id, "reason": m.rejection_reason,
                                                 "outcome": m.hypothetical_outcome, "r": m.hypothetical_r} for m in missed[:30]]}}


@learning.get("/recommendations")
def recommendations(status: str | None = None, _: User = Depends(current_user), db: Session = Depends(get_db)):
    q = select(AIRecommendation).order_by(AIRecommendation.id.desc()).limit(200)
    if status:
        q = q.where(AIRecommendation.status == status)
    return [{"id": r.id, "source": r.source, "kind": r.kind, "subject": r.subject, "recommendation": r.recommendation,
             "evidence": r.evidence, "status": r.status, "outcome": r.outcome, "at": r.created_at} for r in db.scalars(q)]


class RecPatch(BaseModel):
    status: str = Field(pattern="^(ACCEPTED|DISMISSED|IMPLEMENTED)$")
    note: str = ""


@learning.patch("/recommendations/{rec_id}")
def patch_recommendation(rec_id: int, body: RecPatch, user: User = Depends(require("strategy.manage")),
                         db: Session = Depends(get_db)):
    r = db.get(AIRecommendation, rec_id)
    if not r:
        raise HTTPException(404)
    db.add(ApprovalRecord(entity_type="recommendation", entity_id=r.id, action=body.status.lower(), from_status=r.status,
                          to_status=body.status, user_id=user.id, notes=body.note))
    r.status = body.status
    return {"ok": True}


@learning.get("/jobs")
def jobs(_: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.learning_engine.scheduler import JOBS
    rows = db.scalars(select(LearningJob).order_by(LearningJob.id.desc()).limit(200)).all()
    return {"schedule": [{"name": j.name, "period_seconds": j.period_seconds, "timeout": j.timeout} for j in JOBS],
            "history": [{"id": r.id, "job": r.job_type, "status": r.status, "attempts": r.attempts, "result": r.result,
                         "error": r.error, "started_at": r.started_at, "finished_at": r.finished_at} for r in rows]}


@learning.post("/jobs/{name}/run")
def trigger_job(name: str, user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    from app.learning_engine.scheduler import JOB_MAP
    if name not in JOB_MAP:
        raise HTTPException(404, "unknown job")
    get_redis().set(f"rr:learner:trigger:{name}", "1", ex=3600)
    audit(db, user.id, "LEARNING_JOB_TRIGGER", name)
    return {"queued": name, "note": "the learner process picks this up on its next cycle"}


# ---- experiments --------------------------------------------------------------------------------
def exp_out(e: StrategyExperiment, full: bool = False) -> dict:
    v = e.validation or {}
    val = v.get("validation") or {}
    d = {"id": e.id, "code": e.code, "title": e.title, "category": e.category, "version": e.version, "stage": e.stage,
         "hypothesis": e.hypothesis, "duplicate_of": e.duplicate_of, "created_at": e.created_at,
         "static_ok": (v.get("static") or {}).get("ok"), "static_errors": (v.get("static") or {}).get("errors"),
         "unit": v.get("unit"), "validation_summary": {
             "passed": val.get("validation_passed"), "final_test_passed": val.get("final_test_passed"),
             "checks": val.get("checks"), "validation": val.get("validation"), "final_test": val.get("final_test"),
             "walk_forward": val.get("walk_forward_folds"), "monte_carlo": val.get("monte_carlo_validation"),
             "benchmark": (val.get("benchmark") or {}).get("validation"), "cost": val.get("cost_sensitivity"),
             "expectancy_r_lower_bound": val.get("expectancy_r_lower_bound")} if val else None,
         "forward": {k: (v.get("forward") or {}).get(k) for k in ("resolved", "wins", "sum_r", "note")} if v.get("forward") else None,
         "versions": [{"version": x["version"], "source_hash": x["source_hash"], "replaced_at": x["replaced_at"]}
                      for x in v.get("versions", [])]}
    if full:
        d["source_code"], d["proposal"] = e.source_code, e.proposal
    return d


@learning.get("/experiments")
def experiments(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [exp_out(e) for e in db.scalars(select(StrategyExperiment).order_by(StrategyExperiment.id.desc()))]


@learning.get("/experiments/{exp_id}")
def experiment(exp_id: int, _: User = Depends(current_user), db: Session = Depends(get_db)):
    e = db.get(StrategyExperiment, exp_id)
    if not e:
        raise HTTPException(404)
    hist = [{"action": a.action, "from": a.from_status, "to": a.to_status, "user_id": a.user_id, "notes": a.notes, "at": a.created_at}
            for a in db.scalars(select(ApprovalRecord).where(ApprovalRecord.entity_type == "experiment", ApprovalRecord.entity_id == e.id)
                                .order_by(ApprovalRecord.id))]
    return {**exp_out(e, full=True), "history": hist}


class SourceIn(BaseModel):
    source: str = Field(min_length=50, max_length=40_000)


@learning.post("/experiments/{exp_id}/source")
def experiment_source(exp_id: int, body: SourceIn, user: User = Depends(require("strategy.manage")),
                      db: Session = Depends(get_db)):
    from app.strategies.experimental import lifecycle
    e = db.get(StrategyExperiment, exp_id)
    if not e:
        raise HTTPException(404)
    check = lifecycle.attach_source(db, e, body.source, user.id, "source submitted via API")
    audit(db, user.id, "EXPERIMENT_SOURCE", e.title, {"version": e.version, "ok": check["ok"]})
    return {"static": check, "experiment": exp_out(e)}


@learning.post("/experiments/{exp_id}/unit")
def experiment_unit(exp_id: int, user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    from app.strategies.experimental import lifecycle, sandbox
    e = db.get(StrategyExperiment, exp_id)
    if not e or not e.source_code:
        raise HTTPException(404, "experiment or source not found")
    try:
        res = lifecycle.run_unit(db, e, user.id)
    except sandbox.SandboxError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"unit": res, "stage": e.stage}


@learning.post("/experiments/{exp_id}/rollback")
def experiment_rollback(exp_id: int, user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    from app.strategies.experimental import lifecycle
    e = db.get(StrategyExperiment, exp_id)
    if not e:
        raise HTTPException(404)
    try:
        lifecycle.rollback(db, e, user.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return exp_out(e)


@learning.post("/experiments/{exp_id}/develop")
def experiment_develop(exp_id: int, user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    if not get_settings().anthropic_api_key:
        raise HTTPException(409, "Claude API key not configured")
    from app.agents.claude_agent import AgentError
    from app.agents.strategy_discovery import agent
    e = db.get(StrategyExperiment, exp_id)
    if not e:
        raise HTTPException(404)
    try:
        return agent.develop(db, e, user_id=user.id)
    except (AgentError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from None


@learning.post("/research")
def research(user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    if not get_settings().anthropic_api_key:
        raise HTTPException(409, "Claude API key not configured")
    from app.agents.claude_agent import AgentError
    from app.agents.strategy_discovery import agent
    summary = {d: {r.key: r.metrics.get("verdict") for r in perf.latest(db, "DEMO", d)} for d in ("strategy", "regime")}
    try:
        out = agent.research(db, summary)
    except AgentError as exc:
        raise HTTPException(502, str(exc)) from None
    audit(db, user.id, "STRATEGY_RESEARCH", "", {"proposals": len(out["proposals"])})
    return out


@learning.post("/experiments/library")
def seed_library(user: User = Depends(require("strategy.manage")), db: Session = Depends(get_db)):
    from app.strategies.experimental import lifecycle
    return lifecycle.register_library(db)


# ---- models --------------------------------------------------------------------------------------
def model_out(m: ModelRegistry) -> dict:
    return {"id": m.id, "model_key": m.model_key, "version": m.version, "algorithm": m.algorithm, "target": m.target,
            "status": m.status, "dataset": m.dataset_ref, "metrics": m.metrics, "notes": m.notes,
            "calibration": {"test_reliability": (m.calibration or {}).get("test_reliability"),
                            "segments": (m.calibration or {}).get("segments")},
            "trained_at": m.trained_at, "validated_at": m.validated_at, "approved_by": m.approved_by,
            "approved_at": m.approved_at, "rollback_to": m.rollback_to, "features": m.features}


@learning.get("/models")
def models_list(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [model_out(m) for m in db.scalars(select(ModelRegistry).order_by(ModelRegistry.model_key, ModelRegistry.version.desc()))]


class ModelTransition(BaseModel):
    to: str = Field(pattern="^(APPROVED|PRODUCTION|REJECTED|RETIRED)$")
    notes: str = Field(default="", max_length=2000)


@learning.post("/models/{model_id}/transition")
def model_transition(model_id: int, body: ModelTransition, user: User = Depends(require("strategy.manage")),
                     db: Session = Depends(get_db)):
    from app.learning_engine import models as mreg
    m = db.get(ModelRegistry, model_id)
    if not m:
        raise HTTPException(404)
    if body.to == "PRODUCTION" and "trade.live" not in user_perms(user):
        raise HTTPException(403, "Production promotion requires administrator (trade.live) permission")
    try:
        mreg.transition(db, m, body.to, user.id, body.notes)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.id, "MODEL_TRANSITION", f"{m.model_key}:v{m.version}", {"to": body.to})
    return model_out(m)


@learning.post("/models/{model_key}/rollback")
def model_rollback(model_key: str, user: User = Depends(require("trade.live")), db: Session = Depends(get_db)):
    from app.learning_engine import models as mreg
    try:
        prev = mreg.rollback(db, model_key, user.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.id, "MODEL_ROLLBACK", model_key, {"to_version": prev.version})
    return model_out(prev)


@learning.get("/drift")
def drift_events(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": d.id, "scope": d.scope, "subject": d.subject, "metric": d.metric, "value": d.value, "threshold": d.threshold,
             "severity": d.severity, "details": d.details, "at": d.created_at}
            for d in db.scalars(select(DriftEvent).order_by(DriftEvent.id.desc()).limit(200))]


@learning.get("/approvals")
def approvals(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": a.id, "entity_type": a.entity_type, "entity_id": a.entity_id, "action": a.action, "from": a.from_status,
             "to": a.to_status, "user_id": a.user_id, "notes": a.notes, "at": a.created_at}
            for a in db.scalars(select(ApprovalRecord).order_by(ApprovalRecord.id.desc()).limit(300))]


# ================================================================ INTELLIGENCE =====================
@intel.get("/regimes")
def regimes(_: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = select(RegimeHistory.symbol, RegimeHistory.timeframe, func.max(RegimeHistory.id).label("mid")) \
        .group_by(RegimeHistory.symbol, RegimeHistory.timeframe).subquery()
    rows = db.scalars(select(RegimeHistory).join(sub, RegimeHistory.id == sub.c.mid)).all()
    return [{"symbol": r.symbol, "timeframe": r.timeframe, "bar_time": r.bar_time, "regime": r.regime,
             "probabilities": r.probabilities, "confidence": r.confidence, "stability": r.stability,
             "transition_warning": r.transition_warning, "suitable": r.suitable[:30], "unsuitable_count": len(r.unsuitable),
             "hmm": (r.details or {}).get("hmm"), "model_version": r.model_version} for r in rows]


@intel.get("/regimes/{symbol}")
def regime_history(symbol: str, timeframe: str = "H1", limit: int = 200, _: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    rows = db.scalars(select(RegimeHistory).where(RegimeHistory.symbol == symbol, RegimeHistory.timeframe == timeframe)
                      .order_by(RegimeHistory.bar_time.desc()).limit(min(limit, 2000))).all()
    return [{"bar_time": r.bar_time, "regime": r.regime, "confidence": r.confidence, "stability": r.stability,
             "transition_warning": r.transition_warning} for r in rows]


@intel.get("/consensus")
def consensus_list(limit: int = 100, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": c.id, "symbol": c.symbol, "timeframe": c.timeframe, "decision": c.decision, "agreement": c.agreement,
             "conflicts": c.conflicts, "weights_version": c.weights_version, "explanation": c.explanation,
             "specialists": c.specialists, "signal_id": c.signal_id, "at": c.created_at}
            for c in db.scalars(select(ConsensusDecision).order_by(ConsensusDecision.id.desc()).limit(min(limit, 500)))]


@intel.get("/consensus/analytics")
def consensus_analytics(_: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.intelligence.consensus import vote_matrix
    from app.learning_engine.consensus_weights import specialist_performance
    return {"vote_correlation": vote_matrix(db), "specialist_performance": specialist_performance(db)}


@intel.get("/probability")
def probability_overview(_: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.learning_engine import models as mreg
    from app.intelligence.probability import MODEL_KEY
    prod, val = mreg.production(db, MODEL_KEY), mreg.latest_validated(db, MODEL_KEY)
    recent = db.execute(select(ModelPrediction, ModelRegistry.version).join(ModelRegistry, ModelRegistry.id == ModelPrediction.model_id)
                        .order_by(ModelPrediction.id.desc()).limit(100)).all()
    return {"production": model_out(prod) if prod else None, "latest_validated": model_out(val) if val else None,
            "status": "PRODUCTION model active" if prod else ("Shadow only (no approved model)" if val else
                                                             "Unavailable: no validated model yet"),
            "recent_predictions": [{"symbol": p.symbol, "timeframe": p.timeframe, "raw": p.raw_score, "probability": p.probability,
                                    "uncertainty": p.uncertainty, "shadow": p.shadow, "model_version": v, "at": p.created_at}
                                   for p, v in recent]}


@intel.get("/anomalies")
def anomalies(active: bool = False, _: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.intelligence.anomaly import unavailable_sources
    q = select(AnomalyEvent).order_by(AnomalyEvent.id.desc()).limit(300)
    if active:
        q = q.where(AnomalyEvent.resolved_at.is_(None))
    return {"events": [{"id": a.id, "symbol": a.symbol, "kind": a.kind, "severity": a.severity, "explanation": a.explanation,
                        "metrics": a.metrics, "blocks_entries": a.blocks_entries, "source": a.source,
                        "detected_at": a.detected_at, "resolved_at": a.resolved_at, "resolution": a.resolution}
                       for a in db.scalars(q)], "unavailable_data": unavailable_sources()}


class ResolveIn(BaseModel):
    note: str = Field(min_length=5, max_length=1000)


@intel.post("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(anomaly_id: int, body: ResolveIn, user: User = Depends(require("trade.control")),
                    db: Session = Depends(get_db)):
    a = db.get(AnomalyEvent, anomaly_id)
    if not a or a.resolved_at:
        raise HTTPException(404, "no active anomaly with that id")
    a.resolved_at, a.resolution = datetime.now(UTC), f"manual recovery check by {user.username}: {body.note}"
    audit(db, user.id, "ANOMALY_RESOLVED", a.symbol, {"kind": a.kind, "note": body.note})
    return {"ok": True}


@intel.get("/portfolio")
def portfolio_risk(_: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.intelligence import portfolio
    from app.services import trading as tr
    acct = tr.active_account(db)
    if not acct:
        return {"available": False}
    a = portfolio.assess(db, acct)
    corr = portfolio.cached_corr()
    syms = sorted({s for pair in corr for s in pair})[:25]
    matrix = [[1.0 if x == y else corr.get((x, y), corr.get((y, x))) for y in syms] for x in syms]
    return {**a, "heatmap": {"symbols": syms, "matrix": matrix, "note": None if corr else "correlation cache empty (learner job 'portfolio')"}}


@intel.get("/execution")
def execution(mode: str | None = None, _: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.intelligence import execution_analytics
    return execution_analytics.analyse(db, mode)


@intel.get("/fingerprints")
def fingerprints(_: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.intelligence import fingerprint
    return fingerprint.duplicate_report(db)


@intel.get("/adjustments")
def adjustments(_: User = Depends(current_user), db: Session = Depends(get_db)):
    return [{"ticket": a.ticket, "field": a.field, "previous": a.previous_value, "new": a.new_value, "reason": a.reason,
             "accepted": a.accepted, "at": a.created_at}
            for a in db.scalars(select(PositionAdjustment).order_by(PositionAdjustment.id.desc()).limit(200))]


@intel.get("/signals/{signal_id}")
def signal_intelligence(signal_id: int, _: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.db.models import AnalysisResult
    s = db.get(TradingSignal, signal_id)
    if not s:
        raise HTTPException(404)
    ar = db.get(AnalysisResult, s.analysis_id) if s.analysis_id else None
    return {"signal_id": s.id, "intelligence": ((ar.score or {}).get("intelligence") if ar else None) or {}}
