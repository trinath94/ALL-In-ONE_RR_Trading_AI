"""Part 25/26 entities — additive extension of the original schema (see docs/PART25_26_BASELINE.md).

Single sources of truth are preserved: trade outcomes stay in ``closed_positions`` (enriched here by
``trade_features``), execution facts stay in ``trade_executions``, experiments stay in
``strategy_experiments`` (extended with additive columns).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow
from app.db.models import ACCOUNT_MODE


class MT5Deal(Base):
    """Raw MT5 deal ledger (incremental sync). Unique per account + MT5 deal ticket."""
    __tablename__ = "mt5_deals"
    __table_args__ = (UniqueConstraint("account_id", "deal_ticket", name="uq_mt5_deal"),
                      Index("ix_mt5_deal_position", "account_id", "position_id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"))
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    deal_ticket: Mapped[int] = mapped_column(BigInteger)
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64))
    deal_type: Mapped[int] = mapped_column(Integer)          # 0 buy, 1 sell, 2+ balance/credit/...
    entry: Mapped[int] = mapped_column(Integer)              # 0 in, 1 out, 2 inout, 3 out_by
    reason: Mapped[int] = mapped_column(Integer, default=-1)
    volume: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    profit: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    swap: Mapped[float] = mapped_column(Float, default=0)
    fee: Mapped[float] = mapped_column(Float, default=0)
    magic: Mapped[int] = mapped_column(BigInteger, default=0)
    comment: Mapped[str] = mapped_column(String(64), default="")
    deal_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncState(Base):
    __tablename__ = "sync_states"
    __table_args__ = (UniqueConstraint("account_id", "stream", name="uq_sync_state"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"))
    stream: Mapped[str] = mapped_column(String(32))          # deals
    last_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="OK")
    detail: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TradeFeatures(Base):
    """Context & quality enrichment for one closed position (1:1 with closed_positions)."""
    __tablename__ = "trade_features"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    closed_position_id: Mapped[int] = mapped_column(ForeignKey("closed_positions.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE, index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(4))
    session: Mapped[str | None] = mapped_column(String(32))
    regime: Mapped[str | None] = mapped_column(String(32))
    volatility_pct: Mapped[float | None] = mapped_column(Float)   # ATR / price at entry
    spread_points: Mapped[float | None] = mapped_column(Float)
    news_status: Mapped[str | None] = mapped_column(String(16))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"))
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_results.id", ondelete="SET NULL"))
    agent_conversation_id: Mapped[int | None] = mapped_column(ForeignKey("agent_conversations.id", ondelete="SET NULL"))
    model_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    confluence_score: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(8))
    initial_risk: Mapped[float | None] = mapped_column(Float)      # account currency
    risk_pct: Mapped[float | None] = mapped_column(Float)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    net_profit: Mapped[float | None] = mapped_column(Float)
    slippage_points: Mapped[float | None] = mapped_column(Float)
    holding_seconds: Mapped[int | None] = mapped_column(Integer)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mae_r: Mapped[float | None] = mapped_column(Float)             # max adverse excursion in R
    mfe_r: Mapped[float | None] = mapped_column(Float)             # max favourable excursion in R
    outcome: Mapped[str] = mapped_column(String(8))                # WIN | LOSS | BE
    partial_exits: Mapped[int] = mapped_column(Integer, default=0)
    entry_method: Mapped[str | None] = mapped_column(String(32))
    sl_method: Mapped[str | None] = mapped_column(String(32))
    tp_method: Mapped[str | None] = mapped_column(String(32))
    data_quality: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PerformanceSnapshot(Base):
    """Performance by dimension with uncertainty (learning memory: quantitative)."""
    __tablename__ = "performance_snapshots"
    __table_args__ = (Index("ix_perf_snap_lookup", "mode", "dimension", "key", "computed_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    dimension: Mapped[str] = mapped_column(String(32))   # strategy | combination | symbol | timeframe | session | regime ...
    key: Mapped[str] = mapped_column(String(160))
    sample_size: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LearningMemory(Base):
    """Structured qualitative memory: failure patterns, strengths, recommendation outcomes, notes."""
    __tablename__ = "learning_memory"
    __table_args__ = (UniqueConstraint("mode", "kind", "key", name="uq_learning_memory"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    kind: Mapped[str] = mapped_column(String(32), index=True)   # failure_pattern | strength | recommendation_outcome | weakness
    key: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RegimeHistory(Base):
    __tablename__ = "regime_history"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "bar_time", "model_version", name="uq_regime_bar"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(4))
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    regime: Mapped[str] = mapped_column(String(32))
    probabilities: Mapped[dict] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    stability: Mapped[float] = mapped_column(Float)
    transition_warning: Mapped[str | None] = mapped_column(Text)
    suitable: Mapped[list] = mapped_column(JSONB, default=list)
    unsuitable: Mapped[list] = mapped_column(JSONB, default=list)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    model_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ModelRegistry(Base):
    __tablename__ = "model_registry"
    __table_args__ = (UniqueConstraint("model_key", "version", name="uq_model_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    model_key: Mapped[str] = mapped_column(String(64), index=True)   # e.g. prob_tp_before_sl, consensus_weights
    version: Mapped[int] = mapped_column(Integer)
    algorithm: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    features: Mapped[list] = mapped_column(JSONB, default=list)
    dataset_ref: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    calibration: Mapped[dict] = mapped_column(JSONB, default=dict)
    feature_baseline: Mapped[dict] = mapped_column(JSONB, default=dict)   # training distributions for drift (PSI)
    status: Mapped[str] = mapped_column(String(16), default="TRAINED")
    # TRAINED -> VALIDATED -> APPROVED -> PRODUCTION | REJECTED | RETIRED
    artifact: Mapped[bytes | None] = mapped_column(LargeBinary)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_to: Mapped[int | None] = mapped_column(ForeignKey("model_registry.id"))
    notes: Mapped[str] = mapped_column(Text, default="")


class ModelPrediction(Base):
    __tablename__ = "model_predictions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("model_registry.id", ondelete="CASCADE"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(4))
    raw_score: Mapped[float] = mapped_column(Float)
    probability: Mapped[float | None] = mapped_column(Float)
    uncertainty: Mapped[float | None] = mapped_column(Float)
    shadow: Mapped[bool] = mapped_column(Boolean, default=True)   # True => not used for decisions
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ConsensusDecision(Base):
    __tablename__ = "consensus_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_results.id", ondelete="SET NULL"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(4))
    decision: Mapped[str] = mapped_column(String(16))
    agreement: Mapped[float] = mapped_column(Float)
    specialists: Mapped[list] = mapped_column(JSONB)
    conflicts: Mapped[list] = mapped_column(JSONB, default=list)
    weights_version: Mapped[str] = mapped_column(String(48))
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))     # info | warning | critical
    explanation: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    blocks_entries: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="broker_feed")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    metrics: Mapped[dict] = mapped_column(JSONB)
    correlation: Mapped[dict] = mapped_column(JSONB, default=dict)
    recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FailureReport(Base):
    __tablename__ = "failure_reports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    closed_position_id: Mapped[int] = mapped_column(ForeignKey("closed_positions.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    outcome: Mapped[str] = mapped_column(String(8))
    causes: Mapped[list] = mapped_column(JSONB)            # [{cause, evidence, confidence}]
    strengths: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MissedOpportunity(Base):
    """Rejected/unexecuted signals with their hypothetical path. Never mixed into performance metrics."""
    __tablename__ = "missed_opportunities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("trading_signals.id", ondelete="CASCADE"), unique=True)
    rejection_reason: Mapped[str] = mapped_column(Text)
    hypothetical_outcome: Mapped[str] = mapped_column(String(16))   # TP_FIRST | SL_FIRST | NEITHER | NO_DATA
    hypothetical_r: Mapped[float | None] = mapped_column(Float)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PositionAdjustment(Base):
    __tablename__ = "position_adjustments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"))
    ticket: Mapped[int] = mapped_column(BigInteger, index=True)
    field: Mapped[str] = mapped_column(String(16))
    previous_value: Mapped[float | None] = mapped_column(Float)
    new_value: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    accepted: Mapped[bool] = mapped_column(Boolean)
    execution_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class StrategyFingerprint(Base):
    __tablename__ = "strategy_fingerprints"
    __table_args__ = (UniqueConstraint("strategy_code", "code_hash", name="uq_fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_code: Mapped[str] = mapped_column(String(96), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    static: Mapped[dict] = mapped_column(JSONB)
    signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DriftEvent(Base):
    __tablename__ = "drift_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))       # feature | prediction | performance
    subject: Mapped[str] = mapped_column(String(128))    # model key / strategy code
    metric: Mapped[str] = mapped_column(String(32))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(16))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))      # learning | claude | failure | drift | portfolio
    kind: Mapped[str] = mapped_column(String(48))
    subject: Mapped[str] = mapped_column(String(128))
    recommendation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")   # OPEN | ACCEPTED | DISMISSED | IMPLEMENTED
    outcome: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))   # model | experiment | recommendation
    entity_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LearningJob(Base):
    __tablename__ = "learning_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(48), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")   # QUEUED RUNNING DONE FAILED SKIPPED TIMEOUT
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
