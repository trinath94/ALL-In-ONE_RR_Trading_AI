"""Persistent learning memory + evidence retrieval for Claude.

Memory lives in PostgreSQL (``performance_snapshots``, ``learning_memory``, ``trade_features``,
``failure_reports``, ``agent_decisions``). Retrieval is structured SQL: deterministic, auditable and
cheap. Claude's conversation history is never used as memory.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AgentDecision, ClosedPosition
from app.db.models_intel import FailureReport, LearningMemory, TradeFeatures
from app.learning_engine import performance as perf


def remember(db: Session, mode: str, kind: str, key: str, summary: str, evidence_delta: int = 1,
             payload: dict | None = None) -> LearningMemory:
    m = db.scalar(select(LearningMemory).where(LearningMemory.mode == mode, LearningMemory.kind == kind,
                                               LearningMemory.key == key))
    if m is None:
        m = LearningMemory(mode=mode, kind=kind, key=key[:160], summary=summary, evidence_count=0, payload={})
        db.add(m)
    m.summary = summary
    m.evidence_count = (m.evidence_count or 0) + evidence_delta
    m.payload = {**(m.payload or {}), **(payload or {})}
    db.flush()
    return m


def _compact(m: dict) -> dict:
    keep = ("sample_size", "verdict", "win_rate", "win_rate_ci95", "profit_factor", "profit_factor_ci95",
            "expectancy", "expectancy_ci95", "average_r", "max_drawdown")
    return {k: m.get(k) for k in keep}


def retrieve_evidence(db: Session, mode: str, symbol: str, timeframe: str | None, strategies: list[str],
                      regime: str | None, session: str | None = None, limit_trades: int = 15) -> dict:
    """Everything the agent should weigh before judging a new opportunity, with sample sizes."""
    ev: dict = {"mode": mode, "note": "Historical evidence from this platform's own trades. Samples < 30 are "
                                      "statistically inconclusive and must not be treated as proof."}
    lookups = [("symbol", symbol), ("timeframe", timeframe), ("regime", regime), ("session", session)]
    lookups += [("strategy", s) for s in strategies]
    if len(strategies) > 1:
        lookups.append(("combination", "+".join(sorted(strategies))))
    perf_ev = {}
    for dim, key in lookups:
        if not key:
            continue
        rows = perf.latest(db, mode, dim, key)
        perf_ev[f"{dim}:{key}"] = _compact(rows[0].metrics) if rows else {"sample_size": 0, "verdict": "NO_DATA"}
    ev["performance"] = perf_ev
    q = (select(ClosedPosition, TradeFeatures).join(TradeFeatures, TradeFeatures.closed_position_id == ClosedPosition.id)
         .where(TradeFeatures.mode == mode, ClosedPosition.symbol == symbol, perf.learnable())
         .order_by(ClosedPosition.closed_at.desc()).limit(limit_trades))
    ev["recent_trades_same_symbol"] = [
        {"closed_at": cp.closed_at.isoformat(), "direction": cp.direction, "r": cp.r_multiple, "outcome": tf.outcome,
         "strategies": cp.strategy_codes, "regime": tf.regime, "mae_r": tf.mae_r, "mfe_r": tf.mfe_r}
        for cp, tf in db.execute(q)]
    keys = set(strategies) | {symbol} | ({regime} if regime else set())
    mems = db.scalars(select(LearningMemory).where(LearningMemory.mode == mode,
                                                   LearningMemory.kind.in_(["failure_pattern", "weakness", "strength"]))
                      .order_by(LearningMemory.evidence_count.desc()).limit(50)).all()
    ev["patterns"] = [{"kind": m.kind, "key": m.key, "summary": m.summary, "evidence_count": m.evidence_count}
                      for m in mems if any(k in m.key for k in keys)][:10]
    # How did previous AI recommendations on this symbol turn out?
    rec = (select(AgentDecision.decision, TradeFeatures.outcome, TradeFeatures.r_multiple)
           .join(TradeFeatures, TradeFeatures.signal_id == AgentDecision.signal_id)
           .where(AgentDecision.symbol == symbol, TradeFeatures.mode == mode).limit(200))
    outcomes = [{"decision": d, "outcome": o, "r": r} for d, o, r in db.execute(rec)]
    wins = sum(1 for o in outcomes if o["outcome"] == "WIN")
    ev["previous_ai_recommendations"] = {"executed_and_closed": len(outcomes), "wins": wins,
                                         "win_rate_ci95": perf.wilson(wins, len(outcomes))}
    fails = (select(FailureReport.causes).join(ClosedPosition, ClosedPosition.id == FailureReport.closed_position_id)
             .where(ClosedPosition.symbol == symbol, FailureReport.mode == mode, FailureReport.outcome == "LOSS").limit(50))
    counts: dict[str, int] = {}
    for (causes,) in db.execute(fails):
        for c in causes:
            counts[c["cause"]] = counts.get(c["cause"], 0) + 1
    ev["loss_causes_same_symbol"] = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:6])
    return ev
