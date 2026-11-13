"""Application configuration loaded from environment variables.

Secrets (Claude API key, bridge shared secret, encryption key, DB password) are
read from the environment / Docker secrets only and never shipped to the frontend.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RR_", extra="ignore")

    environment: str = "development"  # development | production | test
    app_name: str = "RR Trading Engine"

    database_url: str = "postgresql+psycopg://rr:rr@localhost:5432/rr_trading"
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: str = Field(default="change-me-in-production-please-32b+", min_length=16)
    jwt_algorithm: str = "HS256"
    session_minutes: int = 60
    cookie_secure: bool = True
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # Fernet key (urlsafe base64, 32 bytes) used to encrypt stored broker credentials.
    encryption_key: str = ""

    # Claude
    anthropic_api_key: str = ""  # read by the SDK from ANTHROPIC_API_KEY as well
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "high"
    claude_max_candidates_per_scan: int = 5
    claude_min_interval_seconds: int = 300  # per symbol/timeframe re-analysis throttle

    # Windows MT5 bridge
    bridge_url: str = "https://mt5-bridge.local:8443"
    bridge_shared_secret: str = ""
    bridge_ca_bundle: str | None = None  # path to CA bundle / self-signed cert
    bridge_timeout_seconds: float = 10.0
    bridge_heartbeat_seconds: int = 5
    # "direct": the engine calls the connector over HTTPS (original mode, inbound port on Windows).
    # "relay": the RR MT5 Connector desktop agent dials out over a WebSocket and the same signed requests
    # are relayed to it (no inbound port on Windows). Default keeps the original behaviour.
    bridge_mode: str = "direct"
    agent_heartbeat_timeout_seconds: int = 30
    agent_token_ttl_hours: int = 24
    agent_latest_version: str = ""        # published desktop connector version (update mechanism)
    agent_update_url: str = ""            # HTTPS URL of RR_MT5_Connector_Setup.exe
    agent_update_sha256: str = ""         # SHA-256 of that installer; the agent refuses a mismatching file
    # RR Trading One: role-scoped bridge credentials. When a read key (Fernet) is configured, registration also
    # issues a read-only HMAC secret, encrypted with this key. Processes running with bridge_scope="read" sign with
    # it and the connector refuses it for executions; such processes are not given RR_ENCRYPTION_KEY, so they
    # cannot decrypt the trading secret either. Defaults keep the original single-secret behaviour.
    bridge_read_key: str = ""
    # RR Trading One Phase 9: Fernet key for AI provider API keys stored in the database (ai_providers). Held only by
    # the api and analysis containers (desktop stack secrets, DPAPI). Without it, provider keys cannot be stored.
    ai_keys_key: str = ""
    agent_run_retention_days: int = 30
    bridge_scope: str = "full"            # full | read
    # RR Trading One: key for the local supervisor API (/api/v1/local). Empty = API disabled (404).
    supervisor_key: str = ""
    # Whether newly discovered broker symbols start scan-enabled. True keeps the original behaviour; the RR Trading
    # One local stack sets false (new symbols are scanned only after the user enables them).
    new_symbols_scan_enabled: bool = True
    symbol_refresh_hours: int = 6
    # Frozen inventory of the 215 original strategies, used by the library view to show baseline integrity. Empty means
    # backend/tests/baseline/strategy_inventory.json (shipped in the backend image).
    strategy_baseline_path: str = ""

    # Scanner
    scan_interval_seconds: int = 60
    scan_timeframes: str = "M15,H1,H4"
    scan_max_parallel: int = 4
    scan_bars: int = 500
    # RR Trading One Phase 8: scheduling. Each symbol/timeframe is analysed once per newly closed bar (the decision
    # point backtests replicate); strategies run only on the timeframes they declare; a cycle has a time budget, and
    # jobs that do not fit wait for the next cycle (oldest first) instead of piling up.
    scan_once_per_bar: bool = True
    scan_enforce_timeframes: bool = True
    scan_cycle_budget_seconds: int = 50
    scan_job_timeout_seconds: int = 45

    # News
    news_provider: str = ""  # "" => disconnected (treated as UNKNOWN risk)
    news_api_url: str = ""
    news_api_key: str = ""
    news_block_minutes_before: int = 30
    news_block_minutes_after: int = 30
    news_unknown_policy: str = "block"  # block | allow  (block is default; never assume no news)

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""

    # Part 25/26
    consensus_mode: str = "advisory"          # advisory | enforce (enforce downgrades disagreeing BUY/SELL to WAIT)
    max_quote_age_seconds: int = 120          # stale quotes cannot authorise entries
    regime_intel_enabled: bool = True
    learner_interval_seconds: int = 60

    broker_timezone: str = "Etc/GMT-2"  # common MT5 server tz; override per broker
    cors_origins: str = "http://localhost:3000"

    @property
    def timeframes(self) -> list[str]:
        return [t.strip().upper() for t in self.scan_timeframes.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
