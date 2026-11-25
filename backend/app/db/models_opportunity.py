"""Parts 27-30 Phase 4 tables (migration 0012, additive): opportunity lifecycle with its decision trace, the state
transition log, and the data-source registry."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class Opportunity(Base):
    """One researched setup: an analysis in which at least one strategy produced a setup. It follows the lifecycle in
    ``app.intelligence.opportunity.STATES``; ``trace`` is the decision trace (one entry per stage, with the evidence
    each stage actually had) and ``score_breakdown`` the components that already exist, never a recomputed score."""
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opportunities_symbol_tf_id", "symbol_id", "timeframe", "id"),
                      Index("ix_opportunities_state_id", "state", "id"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(8))
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_results.id", ondelete="SET NULL"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"), index=True)
    trigger: Mapped[str] = mapped_column(String(64), default="bar_close")      # bar_close | event:<kind>
    direction: Mapped[str | None] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(32))
    state_reason: Mapped[str] = mapped_column(Text, default="")
    score_breakdown: Mapped[dict] = mapped_column(JSONB, default=dict)
    trace: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class OpportunityTransition(Base):
    __tablename__ = "opportunity_transitions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataSource(Base):
    """Source registry: every external input the engine reads, with its observed health. Status is what the engine
    saw on its last use (ok / error / not_configured / unknown), never assumed."""
    __tablename__ = "data_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))          # market_data | economic_calendar | ai_provider | ...
    provider: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    ok_count: Mapped[int] = mapped_column(BigInteger, default=0)
    error_count: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PatternDetection(Base):
    """Parts 27-30 Phase 5 (migration 0013): one formation found by a pattern detector (``app.patterns``), with its
    lifecycle. ``key`` identifies the formation (symbol, timeframe, pattern, defining pivots) so repeated analyses
    update one record instead of adding duplicates."""
    __tablename__ = "pattern_detections"
    __table_args__ = (Index("ix_pattern_detections_symbol_tf_id", "symbol_id", "timeframe", "id"),
                      Index("ix_pattern_detections_pattern_state", "pattern", "state"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(8))
    family: Mapped[str] = mapped_column(String(24))
    pattern: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(24))
    quality: Mapped[float] = mapped_column(Float)
    points: Mapped[list] = mapped_column(JSONB, default=list)
    ratios: Mapped[dict] = mapped_column(JSONB, default=dict)
    levels: Mapped[dict] = mapped_column(JSONB, default=dict)
    history: Mapped[list] = mapped_column(JSONB, default=list)
    measure: Mapped[dict] = mapped_column(JSONB, default=dict)          # pattern learning: entry, R, MFE/MAE, outcome
    relations: Mapped[dict] = mapped_column(JSONB, default=dict)        # supporting / contradicting strategies
    context: Mapped[dict] = mapped_column(JSONB, default=dict)          # regime, session, spread, data quality
    rules: Mapped[str] = mapped_column(Text, default="")
    detector_version: Mapped[str] = mapped_column(String(32))
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # bar at which the formation became known
    found_live: Mapped[bool] = mapped_column(Boolean, default=True)     # known on the latest bars, not from history
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SimulationRun(Base):
    """Parts 27-30 Phase 7 (migration 0014): a scenario simulation or counterfactual analysis. Research only: no
    trading decision reads these."""
    __tablename__ = "simulation_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))                  # scenario | counterfactual
    subject_type: Mapped[str] = mapped_column(String(24))          # opportunity | closed_position | manual
    subject_id: Mapped[int | None] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    setup: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    bars: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class StrategyConfigProfile(Base):
    """Master upgrade, adaptive configuration (migration 0019): a versioned parameter profile for one existing strategy,
    optionally for one market regime, symbols and timeframes. The strategy's source and defaults are never changed; a
    profile only supplies parameter values. Lifecycle: PROPOSED -> VALIDATING -> VALIDATED | REJECTED -> DEMO_ACTIVE
    -> LIVE_ACTIVE, and RETIRED. Only validated profiles can be activated; LIVE needs an authorised user."""
    __tablename__ = "strategy_config_profiles"
    __table_args__ = (Index("ix_profile_strategy_status", "strategy_code", "status"),
                      __import__("sqlalchemy").UniqueConstraint("strategy_code", "version", name="uq_profile_version"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_code: Mapped[str] = mapped_column(String(96), ForeignKey("strategies.code", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_config_profiles.id", ondelete="SET NULL"))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    regime: Mapped[str | None] = mapped_column(String(32))              # None: any regime
    symbols: Mapped[list | None] = mapped_column(JSONB)                 # None: any symbol
    timeframes: Mapped[list | None] = mapped_column(JSONB)              # None: any timeframe
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", index=True)
    source: Mapped[str] = mapped_column(String(16), default="USER")      # USER | AI | VARIANT
    ai_provider: Mapped[str | None] = mapped_column(String(160))        # provider/model that proposed it (AI)
    reason: Mapped[str] = mapped_column(Text, default="")
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    history: Mapped[list] = mapped_column(JSONB, default=list)          # [{at, from, to, by, note}]
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationPolicy(Base):
    """Administrator policy for automatic promotion (migration 0019, one row). Everything is off by default; LIVE is
    never promoted automatically."""
    __tablename__ = "automation_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    auto_promote_profiles_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    auto_promote_experiments_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Master upgrade, self-healing (migration 0020): safe, verified, recorded repairs from the repair catalogue. Never
    # touches trading controls (kill switch, risk lock, LIVE), orders or positions.
    auto_repair: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    """Master upgrade, error detection (migration 0020): one classified problem, de-duplicated by fingerprint. Every
    occurrence increments ``occurrences``; a resolved incident that happens again is re-opened. Lifecycle: OPEN ->
    ACKNOWLEDGED -> RESOLVED (by a person) | AUTO_RESOLVED (the condition cleared or a verified repair)."""
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incident_status_seen", "status", "last_seen"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(40), unique=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(10))                    # info | warning | error | critical
    component: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text, default="")
    diagnosis: Mapped[str] = mapped_column(Text, default="")
    recommended_fix: Mapped[str] = mapped_column(Text, default="")
    repair: Mapped[str | None] = mapped_column(String(40))              # catalogue action, if any
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    reopened: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[list] = mapped_column(JSONB, default=list)         # [{at, action, ok, verified, detail}]
    ai_diagnosis: Mapped[dict | None] = mapped_column(JSONB)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
