"""AI studio tasks (RR Trading One Phase 14, specification Part 14): learning explanations and research summaries.

Both run through the AI gateway (routes, fallback, retries, rate limits, strict schema validation, redaction, ledger),
so each can use its own provider and model. Both are read-only: the AI receives facts the engine already recorded
and writes text for people; nothing it returns is used as evidence or changes a model, a weight, a strategy, a risk
setting or an order.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

SUBJECTS = ("model", "fusion")


class Explanation(BaseModel):
    summary: str
    key_points: list[str]
    limitations: list[str]
    what_would_change_it: list[str]


EXPLANATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "key_points", "limitations", "what_would_change_it"],
    "properties": {"summary": {"type": "string"},
                   **{k: {"type": "array", "items": {"type": "string"}}
                      for k in ("key_points", "limitations", "what_would_change_it")}},
}
EXPLANATION_SYSTEM = """You explain RR Trading Engine's learning results to a trader in plain language. You receive the \
facts the engine recorded about ONE item: a model or weight profile with its validation results, or one fused trading \
decision with every factor behind it.

Rules:
- Explain only what the facts say. Never invent numbers, results or causes, and never round a result into a stronger claim.
- Sample sizes matter: say plainly when evidence is thin (INSUFFICIENT_DATA, LOW_DATA_CONFIDENCE, fewer than 30 trades).
- A score is not a probability; only a calibrated probability is a probability.
- State the limitations of the evidence.
- "What would change it" lists the evidence that would change the result, not actions to take.
- You do not recommend approving, promoting, trading, sizing or changing settings. You explain."""


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str
    summary: str
    sources: list[str]


class ResearchSummary(BaseModel):
    headline: str
    items: list[SummaryItem]
    open_questions: list[str]


SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["headline", "items", "open_questions"],
    "properties": {
        "headline": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "required": ["topic", "summary", "sources"],
                                             "properties": {"topic": {"type": "string"}, "summary": {"type": "string"},
                                                            "sources": {"type": "array", "items": {"type": "string"}}}}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}
SUMMARY_SYSTEM = """You summarise RR Trading Engine's recent research for the people running it. You receive the open \
research records of the last days: AI trade-research findings, retraining recommendations, strategy proposals in the \
experimental sandbox and their stage, model and weight-profile validation results, and how the fused decisions \
compared with the original engine.

Rules:
- Summarise only what the records say; cite the record ids you used in "sources" (e.g. "recommendation 12").
- Keep findings and hypotheses apart; say when evidence is thin.
- Do not recommend trades, sizing, approvals or setting changes. This is a reading digest."""


def model_facts(db: Session, model_id: int) -> dict | None:
    from app.db.models_intel import ModelRegistry
    m = db.get(ModelRegistry, model_id)
    if m is None:
        return None
    from app.learning_engine import models as mreg
    return {"kind": "model", "model": f"{m.model_key}:v{m.version}", "algorithm": m.algorithm, "target": m.target,
            "status": m.status, "notes": m.notes, "dataset": m.dataset_ref, "metrics": m.metrics,
            "calibration": {"test_reliability": (m.calibration or {}).get("test_reliability")},
            "signature": mreg.signature_state(m), "trained_at": m.trained_at.isoformat() if m.trained_at else None}


def fusion_facts(db: Session, analysis_id: int) -> dict | None:
    from app.db.models import AnalysisResult
    ar = db.get(AnalysisResult, analysis_id)
    fu = (ar.score or {}).get("fusion") if ar else None
    if not fu or "decision" not in fu:
        return None
    return {"kind": "fused_decision", "timeframe": ar.timeframe, "bar_time": ar.bar_time.isoformat(), "fusion": fu}


def research_records(db: Session, days: int = 14) -> dict:
    from app.db.models import StrategyExperiment
    from app.db.models_intel import AIRecommendation, ModelRegistry
    from app.intelligence import fusion
    since = datetime.now(UTC) - timedelta(days=days)
    recs = db.scalars(select(AIRecommendation).where(AIRecommendation.created_at >= since,
                                                     AIRecommendation.source != "research_summary")
                      .order_by(AIRecommendation.id.desc()).limit(40)).all()
    exps = db.scalars(select(StrategyExperiment).order_by(StrategyExperiment.id.desc()).limit(20)).all()
    models = db.scalars(select(ModelRegistry).where(ModelRegistry.trained_at >= since)
                        .order_by(ModelRegistry.id.desc()).limit(20)).all()
    return {
        "period_days": days,
        "recommendations": [{"id": r.id, "source": r.source, "kind": r.kind, "subject": r.subject, "status": r.status,
                             "text": r.recommendation[:1500]} for r in recs],
        "strategy_proposals": [{"id": e.id, "title": e.title, "stage": e.stage, "hypothesis": (e.hypothesis or "")[:600]}
                               for e in exps],
        "models": [{"id": m.id, "model": f"{m.model_key}:v{m.version}", "status": m.status, "notes": m.notes[:400],
                    "trades_or_rows": (m.dataset_ref or {}).get("trades") or (m.dataset_ref or {}).get("rows")}
                   for m in models],
        "fusion_vs_original": fusion.benchmark_report(db, days),
    }
