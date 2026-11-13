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

    # Scanner
    scan_interval_seconds: int = 60
    scan_timeframes: str = "M15,H1,H4"
    scan_max_parallel: int = 4
    scan_bars: int = 500

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
