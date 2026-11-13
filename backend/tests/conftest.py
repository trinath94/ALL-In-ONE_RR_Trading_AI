import os
import sys

from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "..", "mt5-connector"))
sys.path.insert(0, os.path.join(ROOT, "..", "mt5-connector", "tests"))

os.environ.setdefault("RR_DATABASE_URL", "postgresql+psycopg://rr:rr@localhost:5432/rr_test")
os.environ.setdefault("RR_REDIS_URL", "redis://localhost:6379/15")
os.environ["RR_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["RR_BRIDGE_SHARED_SECRET"] = "b" * 48
os.environ["RR_JWT_SECRET"] = "t" * 48
os.environ["RR_COOKIE_SECURE"] = "false"
os.environ["RR_ENVIRONMENT"] = "test"
os.environ["RR_ANTHROPIC_API_KEY"] = ""

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def migrated_db():
    """Fresh schema via the real Alembic migration, then seed."""
    from alembic import command
    from alembic.config import Config

    from app.core.config import get_settings
    from app.db.session import reset_engine
    get_settings.cache_clear()
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    reset_engine()
    from app.cli import seed
    seed()
    import redis
    redis.Redis.from_url(os.environ["RR_REDIS_URL"]).flushdb()
    yield
    reset_engine()


TRANSIENT = ["login_history", "user_sessions", "audit_logs", "notifications", "system_events", "trade_executions",
             "trade_orders", "risk_evaluations", "execution_idempotency", "open_positions", "closed_positions",
             "account_snapshots", "trading_signals", "analysis_results", "agent_decisions", "agent_conversations",
             "symbols", "trading_accounts", "broker_connections", "mt5_terminals", "users",
             # Part 25/26
             "mt5_deals", "sync_states", "trade_features", "performance_snapshots", "learning_memory",
             "regime_history", "model_predictions", "model_registry", "consensus_decisions", "anomaly_events",
             "portfolio_snapshots", "failure_reports", "missed_opportunities", "position_adjustments",
             "strategy_fingerprints", "drift_events", "ai_recommendations", "approval_records", "learning_jobs",
             "strategy_experiments",
             # RR MT5 Connector desktop agents
             "mt5_agent_tokens", "mt5_agents"]


@pytest.fixture
def db_clean(migrated_db):
    from sqlalchemy import text

    from app.db.session import session_scope
    with session_scope() as db:
        db.execute(text("TRUNCATE " + ",".join(TRANSIENT) + " RESTART IDENTITY CASCADE"))
    from app.cli import seed
    seed()  # restores risk configs / trading state removed by the cascade
    with session_scope() as db:
        db.execute(text("UPDATE trading_state SET active_mode='DEMO', auto_trading=false, pause_new_entries=false, "
                        "kill_switch=false, risk_lock=false, startup_checks_passed=false"))
    import redis
    redis.Redis.from_url(os.environ["RR_REDIS_URL"]).flushdb()
    yield
