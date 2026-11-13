"""RR Trading One Phase 9 tables (migration 0007): agent run log, AI providers, task routes, usage ledger."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class AgentRun(Base):
    """One agent's work in one orchestration cycle: the audit record of the typed agent contract."""
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(48), index=True)
    agent_version: Mapped[str] = mapped_column(String(16))
    task: Mapped[str] = mapped_column(String(64))
    input_refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    output_schema: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16))           # ok | error | skipped | partial
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str | None] = mapped_column(String(48))
    model: Mapped[str | None] = mapped_column(String(96))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AIProvider(Base):
    __tablename__ = "ai_providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(24))              # anthropic | openai_compatible
    base_url: Mapped[str | None] = mapped_column(String(255))
    api_key_enc: Mapped[str | None] = mapped_column(Text)      # Fernet (RR_AI_KEYS_KEY); never returned by the API
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_s: Mapped[int] = mapped_column(Integer, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    price_in_per_mtok: Mapped[float | None] = mapped_column(Float)
    price_out_per_mtok: Mapped[float | None] = mapped_column(Float)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AITaskRoute(Base):
    __tablename__ = "ai_task_routes"
    task: Mapped[str] = mapped_column(String(48), primary_key=True)
    chain: Mapped[list] = mapped_column(JSONB)                 # [{"provider_id": 1, "model": "..."}], tried in order
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task: Mapped[str] = mapped_column(String(48), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(24))            # ok | error | invalid_output | rate_limited | skipped
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float)          # None when no price is configured (never guessed)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    fallback_from: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
