"""RR Trading One Phase 20 (additive): indexes for the engine's hot paths, measured on a database grown to ~40 days of
a 50-symbol scan (see docs/RR_TRADING_ONE_PHASE20_REPORT.md). Without them, the trading worker's 5-second poll, the
monitor's signal expiry, the control room and the scanner page read every row ever stored, so they slow down as
history grows; with them their cost no longer depends on history.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only NEW signals are polled for execution and for expiry; they are few, whatever the history.
    op.create_index('ix_trading_signals_new_expires', 'trading_signals', ['expires_at'],
                    postgresql_where=sa.text("status = 'NEW'"))
    # Latest run of one agent; latest analysis of one symbol and timeframe.
    op.create_index('ix_agent_runs_agent_id_id', 'agent_runs', ['agent_id', 'id'])
    op.create_index('ix_analysis_results_symbol_tf_id', 'analysis_results', ['symbol_id', 'timeframe', 'id'])


def downgrade() -> None:
    op.drop_index('ix_analysis_results_symbol_tf_id', table_name='analysis_results')
    op.drop_index('ix_agent_runs_agent_id_id', table_name='agent_runs')
    op.drop_index('ix_trading_signals_new_expires', table_name='trading_signals')
