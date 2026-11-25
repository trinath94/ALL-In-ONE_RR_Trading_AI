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
    # Phase 14: optional client-side limit; above it the gateway moves to the next provider instead of calling
    max_requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AIControl(Base):
    """Single Active AI (migration 0018): one row (id=1). When ``single_active`` is on, every AI task uses only the
    selected provider and model (plus the explicitly configured fallback, if enabled) instead of the per-task routes.
    Off (default) = the per-task routes as before."""
    __tablename__ = "ai_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    single_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    active_provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    active_model: Mapped[str] = mapped_column(String(96), default="", server_default="")
    fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    fallback_provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id", ondelete="SET NULL"))
    fallback_model: Mapped[str] = mapped_column(String(96), default="", server_default="")
    # Autonomous agent upgrade (migration 0021): API budget (None = no limit). When a limit is reached, AI calls are
    # skipped and the engine keeps working; the usage ledger is the source of the spend.
    daily_budget_usd: Mapped[float | None] = mapped_column(Float)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer)
    # What the engine does in With AI mode when no AI answer is available (provider down, budget exhausted, no
    # provider): "pause" (default: candidates stay unreviewed, nothing is auto-executed) or "engine_only" (the
    # engine's own validated signals continue through the Risk Engine).
    ai_unavailable_policy: Mapped[str] = mapped_column(String(16), default="pause", server_default="pause")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AITaskRoute(Base):
    __tablename__ = "ai_task_routes"
    task: Mapped[str] = mapped_column(String(48), primary_key=True)
    chain: Mapped[list] = mapped_column(JSONB)                 # [{"provider_id": 1, "model": "..."}], tried in order
    # Parts 27-30 (migration 0012): "fallback" (original: first valid answer) or "consensus" (the first two providers
    # answer independently and must agree; candidate review only).
    mode: Mapped[str] = mapped_column(String(16), default="fallback", server_default="fallback")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task: Mapped[str] = mapped_column(String(48), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(96))               # the model the provider reports it served
    requested_model: Mapped[str | None] = mapped_column(String(96))  # Phase 14: the model the route asked for
    status: Mapped[str] = mapped_column(String(24))            # ok | error | invalid_output | rate_limited | skipped
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float)          # None when no price is configured (never guessed)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    fallback_from: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
