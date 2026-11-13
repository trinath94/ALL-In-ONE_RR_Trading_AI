"""PostgreSQL schema (31 domain tables). Managed with Alembic migrations."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer,
                        Numeric, String, Table, Column, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow

PRICE = Numeric(20, 8)
MONEY = Numeric(20, 2)

ACCOUNT_MODE = Enum("DEMO", "LIVE", name="account_mode")
DIRECTION = Enum("BUY", "SELL", name="direction")

# 3. permissions <-> roles association
role_permissions = Table(
    "role_permissions", Base.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


# 2. Roles
class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)  # admin | trader | readonly
    description: Mapped[str] = mapped_column(String(255), default="")
    permissions: Mapped[list["Permission"]] = relationship(secondary=role_permissions, lazy="selectin")


# 3. Permissions
class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")


# 1. Users
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_token_hash: Mapped[str | None] = mapped_column(String(128))
    reset_token_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[Role] = relationship(lazy="selectin")


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class LoginHistory(Base):
    __tablename__ = "login_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    username: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 6. MT5 terminals (Windows connector instances)
class MT5Terminal(TimestampMixin, Base):
    __tablename__ = "mt5_terminals"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    bridge_url: Mapped[str] = mapped_column(String(255))
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    terminal_build: Mapped[str | None] = mapped_column(String(32))


# 5. Broker connections
class BrokerConnection(TimestampMixin, Base):
    __tablename__ = "broker_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_name: Mapped[str] = mapped_column(String(128))
    server: Mapped[str] = mapped_column(String(128))
    terminal_id: Mapped[int] = mapped_column(ForeignKey("mt5_terminals.id"))
    timezone: Mapped[str] = mapped_column(String(64), default="Etc/GMT-2")


# 4. Trading accounts — DEMO and LIVE are separate rows with separate configuration.
class TradingAccount(TimestampMixin, Base):
    __tablename__ = "trading_accounts"
    __table_args__ = (UniqueConstraint("login", "server", name="uq_account_login_server"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_connection_id: Mapped[int] = mapped_column(ForeignKey("broker_connections.id"))
    login: Mapped[int] = mapped_column(BigInteger)
    server: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    encrypted_password: Mapped[str | None] = mapped_column(Text)  # Fernet ciphertext, never plaintext
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    live_approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    live_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)  # the account currently traded


# 7. Symbols — discovered from MT5, never hardcoded
class Symbol(TimestampMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (UniqueConstraint("broker_connection_id", "broker_symbol", name="uq_symbol_broker"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    broker_connection_id: Mapped[int] = mapped_column(ForeignKey("broker_connections.id"))
    broker_symbol: Mapped[str] = mapped_column(String(64), index=True)
    canonical: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    description: Mapped[str] = mapped_column(String(255), default="")
    base_currency: Mapped[str | None] = mapped_column(String(16))
    profit_currency: Mapped[str | None] = mapped_column(String(16))
    margin_currency: Mapped[str | None] = mapped_column(String(16))
    digits: Mapped[int] = mapped_column(Integer, default=5)
    point: Mapped[Decimal] = mapped_column(PRICE)
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    tick_value: Mapped[Decimal] = mapped_column(PRICE)
    contract_size: Mapped[Decimal] = mapped_column(PRICE)
    volume_min: Mapped[Decimal] = mapped_column(PRICE)
    volume_max: Mapped[Decimal] = mapped_column(PRICE)
    volume_step: Mapped[Decimal] = mapped_column(PRICE)
    stops_level: Mapped[int] = mapped_column(Integer, default=0)
    freeze_level: Mapped[int] = mapped_column(Integer, default=0)
    trade_mode: Mapped[int] = mapped_column(Integer, default=0)
    filling_modes: Mapped[int] = mapped_column(Integer, default=0)
    tradable: Mapped[bool] = mapped_column(Boolean, default=False)
    scan_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    spec_raw: Mapped[dict] = mapped_column(JSONB, default=dict)


# 8. Market data (OHLC cache — source of truth stays the broker)
class MarketBar(Base):
    __tablename__ = "market_data"
    __table_args__ = (UniqueConstraint("symbol_id", "timeframe", "ts", name="uq_bar"),
                      Index("ix_bar_lookup", "symbol_id", "timeframe", "ts"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    tick_volume: Mapped[int] = mapped_column(BigInteger, default=0)  # broker tick volume, NOT exchange volume
    real_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    spread: Mapped[int] = mapped_column(Integer, default=0)


# 9. Market sessions (configurable, IANA timezone so DST is handled)
class MarketSession(Base):
    __tablename__ = "market_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    timezone: Mapped[str] = mapped_column(String(64))
    start_local: Mapped[str] = mapped_column(String(5))  # HH:MM in that timezone
    end_local: Mapped[str] = mapped_column(String(5))
    kind: Mapped[str] = mapped_column(String(16), default="session")  # session | killzone | macro


# 10. Strategies
class Strategy(TimestampMixin, Base):
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    stage: Mapped[str] = mapped_column(String(24), default="PRODUCTION")  # promotion workflow stage
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    availability: Mapped[str] = mapped_column(String(255), default="available")
    risky: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_versions.id", use_alter=True))


# 11. Strategy versions (complete history, never silently replaced)
class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    code_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# 12. Strategy parameters (definition + defaults)
class StrategyParameter(Base):
    __tablename__ = "strategy_parameters"
    __table_args__ = (UniqueConstraint("strategy_version_id", "name", name="uq_param"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    default_value: Mapped[dict] = mapped_column(JSONB)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)


# 13. Strategy configurations (per account mode / symbol overrides)
class StrategyConfiguration(TimestampMixin, Base):
    __tablename__ = "strategy_configurations"
    __table_args__ = (UniqueConstraint("strategy_id", "account_mode", "symbol_id", name="uq_strat_cfg"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    account_mode: Mapped[str] = mapped_column(ACCOUNT_MODE)
    symbol_id: Mapped[int | None] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    timeframes: Mapped[list] = mapped_column(JSONB, default=list)


# 14. Strategy performance (rolled-up stats per slice)
class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"
    __table_args__ = (Index("ix_perf_slice", "strategy_id", "symbol", "timeframe", "regime", "source"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    symbol: Mapped[str | None] = mapped_column(String(64))
    timeframe: Mapped[str | None] = mapped_column(String(4))
    regime: Mapped[str | None] = mapped_column(String(32))
    session: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))  # BACKTEST | OOS | WALKFORWARD | PAPER | LIVE
    trades: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    gross_profit: Mapped[float] = mapped_column(Float, default=0)
    gross_loss: Mapped[float] = mapped_column(Float, default=0)
    avg_r: Mapped[float] = mapped_column(Float, default=0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# 15. Analysis results
class AnalysisResult(Base):
    __tablename__ = "analysis_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
    timeframe: Mapped[str] = mapped_column(String(4))
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    regime: Mapped[str] = mapped_column(String(32))
    structure: Mapped[dict] = mapped_column(JSONB, default=dict)
    strategy_outputs: Mapped[list] = mapped_column(JSONB, default=list)
    score: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 16. Trading signals
class TradingSignal(TimestampMixin, Base):
    __tablename__ = "trading_signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), index=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_results.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(24))  # BUY SELL WAIT NO_TRADE
    grade: Mapped[str] = mapped_column(String(8))  # A+ A B C REJECTED
    timeframe: Mapped[str] = mapped_column(String(4))
    entry: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    tp1: Mapped[Decimal | None] = mapped_column(PRICE)
    tp2: Mapped[Decimal | None] = mapped_column(PRICE)
    tp3: Mapped[Decimal | None] = mapped_column(PRICE)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, default=0)
    calibrated_confidence: Mapped[float | None] = mapped_column(Float)  # None => insufficient OOS history
    confidence_sample: Mapped[int] = mapped_column(Integer, default=0)
    strategies: Mapped[list] = mapped_column(JSONB, default=list)
    regime: Mapped[str] = mapped_column(String(32), default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    ai_review: Mapped[dict | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="NEW")  # NEW APPROVED REJECTED EXECUTED EXPIRED


# 17. Risk evaluations
class RiskEvaluation(Base):
    __tablename__ = "risk_evaluations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("trading_accounts.id"))
    approved: Mapped[bool] = mapped_column(Boolean)
    volume: Mapped[Decimal | None] = mapped_column(PRICE)
    risk_amount: Mapped[float | None] = mapped_column(Float)
    risk_percent: Mapped[float | None] = mapped_column(Float)
    checks: Mapped[list] = mapped_column(JSONB, default=list)
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 18. Trade orders (application-side intent)
class TradeOrder(TimestampMixin, Base):
    __tablename__ = "trade_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"))
    risk_evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("risk_evaluations.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))  # MARKET_BUY SELL_LIMIT MODIFY CLOSE ...
    volume: Mapped[Decimal] = mapped_column(PRICE)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    deviation_points: Mapped[int] = mapped_column(Integer, default=10)
    position_ticket: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), default="AUTO")  # AUTO | MANUAL | SYSTEM
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    # PENDING -> SENT -> CONFIRMED | REJECTED | FAILED | UNKNOWN (needs reconciliation)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    strategy_codes: Mapped[list] = mapped_column(JSONB, default=list)


# 19. Trade executions (MT5-confirmed facts only)
class TradeExecution(Base):
    __tablename__ = "trade_executions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("trade_orders.id", ondelete="CASCADE"), index=True)
    retcode: Mapped[int] = mapped_column(Integer)
    retcode_text: Mapped[str] = mapped_column(String(255), default="")
    mt5_order: Mapped[int | None] = mapped_column(BigInteger)
    mt5_deal: Mapped[int | None] = mapped_column(BigInteger)
    filled_volume: Mapped[Decimal | None] = mapped_column(PRICE)
    filled_price: Mapped[Decimal | None] = mapped_column(PRICE)
    slippage_points: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# 20. Open positions (mirror of MT5, replaced by sync)
class OpenPosition(Base):
    __tablename__ = "open_positions"
    __table_args__ = (UniqueConstraint("account_id", "ticket", name="uq_open_pos"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    ticket: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(DIRECTION)
    volume: Mapped[Decimal] = mapped_column(PRICE)
    open_price: Mapped[Decimal] = mapped_column(PRICE)
    current_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    profit: Mapped[float] = mapped_column(Float, default=0)
    swap: Mapped[float] = mapped_column(Float, default=0)
    magic: Mapped[int] = mapped_column(BigInteger, default=0)
    comment: Mapped[str] = mapped_column(String(64), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    execution_id: Mapped[str | None] = mapped_column(String(64))
    strategy_codes: Mapped[list] = mapped_column(JSONB, default=list)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# 21. Closed positions
class ClosedPosition(Base):
    __tablename__ = "closed_positions"
    __table_args__ = (UniqueConstraint("account_id", "ticket", name="uq_closed_pos"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    ticket: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(DIRECTION)
    volume: Mapped[Decimal] = mapped_column(PRICE)
    open_price: Mapped[Decimal] = mapped_column(PRICE)
    close_price: Mapped[Decimal] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    profit: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0)
    swap: Mapped[float] = mapped_column(Float, default=0)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    close_reason: Mapped[str] = mapped_column(String(32), default="")
    execution_id: Mapped[str | None] = mapped_column(String(64), index=True)
    mode: Mapped[str | None] = mapped_column(ACCOUNT_MODE)               # Part 25: DEMO/LIVE dataset separation
    partial_exits: Mapped[int | None] = mapped_column(Integer)
    sync_status: Mapped[str | None] = mapped_column(String(16))          # COMPLETE | PARTIAL | CORRECTED
    strategy_codes: Mapped[list] = mapped_column(JSONB, default=list)
    regime: Mapped[str | None] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(4))


# 22. Account snapshots
class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("trading_accounts.id"), index=True)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    margin: Mapped[Decimal] = mapped_column(MONEY)
    free_margin: Mapped[Decimal] = mapped_column(MONEY)
    margin_level: Mapped[float | None] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 23. Backtesting jobs
class BacktestJob(TimestampMixin, Base):
    __tablename__ = "backtest_jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), default="BACKTEST")  # BACKTEST WALKFORWARD MONTECARLO OPTIMISE
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    params: Mapped[dict] = mapped_column(JSONB)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    error: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[float] = mapped_column(Float, default=0)


# 24. Backtesting results
class BacktestResult(Base):
    __tablename__ = "backtest_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("backtest_jobs.id", ondelete="CASCADE"), index=True)
    segment: Mapped[str] = mapped_column(String(32), default="FULL")  # FULL | IN_SAMPLE | OOS | WF_n
    metrics: Mapped[dict] = mapped_column(JSONB)
    equity_curve: Mapped[list] = mapped_column(JSONB, default=list)
    trades: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# 25. Optimisation results
class OptimisationResult(Base):
    __tablename__ = "optimisation_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("backtest_jobs.id", ondelete="CASCADE"), index=True)
    params: Mapped[dict] = mapped_column(JSONB)
    in_sample: Mapped[dict] = mapped_column(JSONB)
    out_of_sample: Mapped[dict] = mapped_column(JSONB)
    rank: Mapped[int] = mapped_column(Integer)


# 26. Agent conversations
class AgentConversation(Base):
    __tablename__ = "agent_conversations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32))  # SIGNAL_REVIEW | PERFORMANCE | DISCOVERY | TRADE_MGMT
    model: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    response: Mapped[dict | None] = mapped_column(JSONB)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 27. Agent decisions
class AgentDecision(Base):
    __tablename__ = "agent_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id", ondelete="CASCADE"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("trading_signals.id", ondelete="SET NULL"))
    symbol: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 28. Notifications
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    channels: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 29. Audit logs (append-only)
class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 30. System events
class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    component: Mapped[str] = mapped_column(String(48), index=True)
    level: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# 31. Execution idempotency records
class ExecutionIdempotency(Base):
    __tablename__ = "execution_idempotency"
    __table_args__ = (CheckConstraint("state in ('RESERVED','SENT','DONE','FAILED','UNKNOWN')", name="state"),)
    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True)  # account+signal+action
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# Configuration tables ---------------------------------------------------------------
class RiskConfig(TimestampMixin, Base):
    """Risk parameters, one row per account mode — DEMO and LIVE never share settings."""
    __tablename__ = "risk_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_mode: Mapped[str] = mapped_column(ACCOUNT_MODE, unique=True)
    params: Mapped[dict] = mapped_column(JSONB)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class TradingState(TimestampMixin, Base):
    """Singleton row holding global trading control state."""
    __tablename__ = "trading_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    active_mode: Mapped[str] = mapped_column(ACCOUNT_MODE, default="DEMO")
    auto_trading: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_new_entries: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_reason: Mapped[str] = mapped_column(Text, default="")
    startup_checks_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_lock: Mapped[bool] = mapped_column(Boolean, default=False)  # set when a risk limit trips
    risk_lock_reason: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class EconomicEvent(Base):
    __tablename__ = "economic_events"
    __table_args__ = (UniqueConstraint("provider", "provider_id", name="uq_econ_event"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(8), index=True)
    impact: Mapped[str] = mapped_column(String(8))  # HIGH MEDIUM LOW
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actual: Mapped[str | None] = mapped_column(String(32))
    forecast: Mapped[str | None] = mapped_column(String(32))
    previous: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationSetting(Base):
    __tablename__ = "notification_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), unique=True)
    in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    email: Mapped[bool] = mapped_column(Boolean, default=False)


class StrategyExperiment(TimestampMixin, Base):
    """Claude-proposed strategy research items, isolated from production."""
    __tablename__ = "strategy_experiments"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Part 25 additive columns (experimental development & lifecycle)
    code: Mapped[str | None] = mapped_column(String(96), unique=True)
    category: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[int | None] = mapped_column(Integer)
    source_code: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict | None] = mapped_column(JSONB)
    duplicate_of: Mapped[list | None] = mapped_column(JSONB)
    title: Mapped[str] = mapped_column(String(255))
    hypothesis: Mapped[str] = mapped_column(Text)
    proposal: Mapped[dict] = mapped_column(JSONB)
    stage: Mapped[str] = mapped_column(String(24), default="RESEARCH")
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


from app.db import models_intel  # noqa: E402,F401  (Part 25/26 additive tables)
from app.db import models_agent  # noqa: E402,F401  (RR MT5 Connector agents, migration 0003)
