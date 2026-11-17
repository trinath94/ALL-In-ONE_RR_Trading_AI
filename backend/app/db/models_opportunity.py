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
