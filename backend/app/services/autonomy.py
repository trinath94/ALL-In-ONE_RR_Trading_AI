"""Engine-first operation (owner's decision, 2026-11-26): how much the engine does by itself and how much is sent to
the Anthropic API. Used by the AI Studio autonomy dashboard and the Engine Assistant ("can the engine operate without
Anthropic?", "reduce token usage")."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import gateway
from app.db.models_ai import AIUsage

UTC = timezone.utc

# Routine tasks run with every scan or chat message; improvement tasks run only when the engine or the user asks.
ROUTINE_TASKS = ("candidate_review", "engine_assistant")


def report(db: Session, days: int = 1) -> dict:
    """What the engine did by itself versus what was sent to the Anthropic API, over the last ``days``: internal
    workload, engine-decided versus AI-reviewed candidates, AI calls / tokens / cost split into routine and improvement
    tasks, Claude Code tasks, budget, and the improvement proposals with their validation status."""
    from app.db.models import AgentConversation, AnalysisResult, TradeOrder, TradingSignal
    from app.db.models_ai import AgentRun
    from app.db.models_intel import LearningJob
    from app.db.models_opportunity import AgentTask, StrategyConfigProfile
    from app.services import trading as tr
    days = max(1, min(days, 90))
    since = datetime.now(UTC) - timedelta(days=days)
    n = lambda q: int(db.scalar(q) or 0)  # noqa: E731
    sigs = db.scalars(select(TradingSignal.ai_review).where(TradingSignal.created_at >= since,
                                                            TradingSignal.decision.in_(["BUY", "SELL", "NO_TRADE",
                                                                                        "WAIT"]))).all()
    modes = [(r or {}).get("mode") or ("AI" if (r or {}).get("decision") not in (None, "SKIPPED", "UNREVIEWED")
                                        else (r or {}).get("decision") or "none") for r in sigs]
    engine_decided = sum(1 for m in modes if m in ("ENGINE", "ENGINE_ONLY"))
    ai_reviewed = n(select(func.count(AgentConversation.id)).where(AgentConversation.purpose == "SIGNAL_REVIEW",
                                                                   AgentConversation.created_at >= since))
    internal = {
        "scan_cycles": n(select(func.count(func.distinct(AgentRun.cycle_id))).where(AgentRun.started_at >= since)),
        "agent_runs": n(select(func.count(AgentRun.id)).where(AgentRun.started_at >= since)),
        "analyses": n(select(func.count(AnalysisResult.id)).where(AnalysisResult.created_at >= since)),
        "signals": len(sigs), "engine_decided_candidates": engine_decided, "ai_reviewed_candidates": ai_reviewed,
        "orders": n(select(func.count(TradeOrder.id)).where(TradeOrder.created_at >= since)),
        "learner_jobs": n(select(func.count(LearningJob.id)).where(LearningJob.started_at >= since))}
    decided = engine_decided + ai_reviewed
    internal["engine_decision_share"] = round(engine_decided / decided, 3) if decided else None
    rows = db.execute(select(AIUsage.task, func.count(), func.sum(AIUsage.input_tokens), func.sum(AIUsage.output_tokens),
                             func.sum(AIUsage.cache_read_tokens), func.sum(AIUsage.cost))
                      .where(AIUsage.created_at >= since, AIUsage.status.in_(["ok", "error", "invalid_output",
                                                                              "rate_limited"]))
                      .group_by(AIUsage.task)).all()
    by_task = [{"task": t, "kind": "routine" if t in ROUTINE_TASKS else "improvement", "calls": c,
                "input_tokens": int(i or 0), "output_tokens": int(o or 0), "cache_read_tokens": int(cr or 0),
                "cost": round(float(cost), 6) if cost is not None else None} for t, c, i, o, cr, cost in rows]

    def total(kind=None):
        rs = [r for r in by_task if kind is None or r["kind"] == kind]
        return {"calls": sum(r["calls"] for r in rs), "input_tokens": sum(r["input_tokens"] for r in rs),
                "output_tokens": sum(r["output_tokens"] for r in rs),
                "cost": round(sum(r["cost"] or 0 for r in rs), 6)}
    cc = db.scalars(select(AgentTask).where(AgentTask.kind == "claude_code", AgentTask.created_at >= since)).all()
    profiles_ = db.scalars(select(StrategyConfigProfile).where(StrategyConfigProfile.source == "AI")
                           .order_by(StrategyConfigProfile.id.desc()).limit(20)).all()
    return {
        "days": days, "internal": internal,
        "ai": {"total": total(), "routine": total("routine"), "improvement": total("improvement"), "by_task": by_task},
        "claude_code": {"tasks": len(cc), "cost_usd": round(sum(float((t.usage or {}).get("cost_usd") or 0) for t in cc), 6),
                        "by_status": {s: sum(1 for t in cc if t.status == s) for s in {t.status for t in cc}}},
        "budget": gateway.budget_status(), "policy": engine_first(db),
        "mode": {"ai_mode": tr.get_state(db).ai_mode, "active_mode": tr.get_state(db).active_mode},
        "improvements": [{"id": p.id, "strategy": p.strategy_code, "version": p.version, "status": p.status,
                          "created_at": p.created_at, "trigger": (p.evidence or {}).get("trigger"),
                          "validated": (p.validation or {}).get("passed"), "reason": (p.reason or "")[:300],
                          "ai_provider": p.ai_provider} for p in profiles_]}


def engine_first(db: Session) -> dict:
    from app.db.models_ai import AIControl
    c = db.get(AIControl, 1)
    return {"candidate_review": (c.candidate_review if c else None) or "off",
            "review_daily_cap": c.review_daily_cap if c else 10,
            "review_confidence_below": c.review_confidence_below if c else 0.6,
            "assistant_ai": bool(c and c.assistant_ai), "auto_improvement": bool(c.auto_improvement) if c else True,
            "improvement_daily_cap": c.improvement_daily_cap if c else 2}


def independence(db: Session, rep: dict | None = None) -> dict:
    """Can the engine operate without the Anthropic API, and what still depends on it."""
    from app.services import trading as tr
    from app.services.trading import ai_unavailable_policy
    rep = rep or report(db, 1)
    pol, st = rep["policy"], tr.get_state(db)
    routine = []
    if st.ai_mode != "ENGINE_ONLY" and pol["candidate_review"] != "off":
        routine.append(f"candidate review is '{pol['candidate_review']}': scan candidates are sent to the AI")
    if pol["assistant_ai"]:
        routine.append("the Engine Assistant phrases answers with the AI")
    blocks_without_ai = (st.ai_mode != "ENGINE_ONLY" and pol["candidate_review"] != "off"
                         and ai_unavailable_policy(db) == "pause")
    return {"routine_dependencies": routine, "can_trade_without_ai": not blocks_without_ai,
            "routine_calls_last_day": rep["ai"]["routine"]["calls"],
            "improvement_calls_last_day": rep["ai"]["improvement"]["calls"],
            "note": ("Indicators, strategies, signals, risk, execution, position management, monitoring, learning and "
                     "reports are computed by the engine itself; the Anthropic API is used for improvement research "
                     "and for questions you ask.")}
