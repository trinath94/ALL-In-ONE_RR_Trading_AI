"""RR MT5 Connector (Windows desktop agent) registry — additive tables (migration 0003).

An agent is one Windows PC running the desktop connector. It opens an outbound WebSocket to the engine; the
engine relays the existing signed bridge requests over it, so no inbound port is needed on Windows.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class MT5AgentToken(Base):
    """One-time registration token. Only its SHA-256 is stored; the clear token is shown once."""
    __tablename__ = "mt5_agent_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_agent_id: Mapped[int | None] = mapped_column(Integer)


class MT5Agent(Base):
    __tablename__ = "mt5_agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    device_id: Mapped[str] = mapped_column(String(64), unique=True)
    device_info: Mapped[dict] = mapped_column(JSONB, default=dict)
    public_key: Mapped[str] = mapped_column(String(128))              # Ed25519, base64 (raw 32 bytes)
    api_key_hash: Mapped[str] = mapped_column(String(64))             # SHA-256 of the agent API key
    bridge_secret_enc: Mapped[str] = mapped_column(Text)              # Fernet(RR_ENCRYPTION_KEY) — per-agent HMAC secret
    app_version: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ip: Mapped[str | None] = mapped_column(String(64))
    # last reported state (terminal, account snapshot, latency, live authorisation flag...)
    status: Mapped[dict] = mapped_column(JSONB, default=dict)
    account_login: Mapped[int | None] = mapped_column(BigInteger)
    account_server: Mapped[str | None] = mapped_column(String(128))
