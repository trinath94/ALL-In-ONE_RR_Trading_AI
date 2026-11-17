"""Parts 27-30 Phase 4 (additive): opportunity lifecycle and decision trace, opportunity state transitions, the
data-source registry, and the AI route mode (fallback | consensus). No existing table, column or row is changed:
``ai_task_routes.mode`` is added with the server default ``fallback``, which is the original behaviour.

Rollback: ``alembic downgrade 0011`` drops only what this revision added.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'opportunities',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('symbol_id', sa.Integer(), sa.ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        sa.Column('timeframe', sa.String(8), nullable=False),
        sa.Column('bar_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('analysis_id', sa.BigInteger(), sa.ForeignKey('analysis_results.id', ondelete='SET NULL'), nullable=True),
        sa.Column('signal_id', sa.BigInteger(), sa.ForeignKey('trading_signals.id', ondelete='SET NULL'), nullable=True),
        sa.Column('trigger', sa.String(64), nullable=False),
        sa.Column('direction', sa.String(8), nullable=True),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('state_reason', sa.Text(), nullable=False),
        sa.Column('score_breakdown', postgresql.JSONB(), nullable=False),
        sa.Column('trace', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_opportunities_signal_id', 'opportunities', ['signal_id'])
    op.create_index('ix_opportunities_created_at', 'opportunities', ['created_at'])
    op.create_index('ix_opportunities_symbol_tf_id', 'opportunities', ['symbol_id', 'timeframe', 'id'])
    op.create_index('ix_opportunities_state_id', 'opportunities', ['state', 'id'])

    op.create_table(
        'opportunity_transitions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('opportunity_id', sa.BigInteger(), sa.ForeignKey('opportunities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_state', sa.String(32), nullable=True),
        sa.Column('to_state', sa.String(32), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_opportunity_transitions_opportunity_id', 'opportunity_transitions', ['opportunity_id'])

    op.create_table(
        'data_sources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(64), nullable=False, unique=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('category', sa.String(32), nullable=False),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('ok_count', sa.BigInteger(), nullable=False),
        sa.Column('error_count', sa.BigInteger(), nullable=False),
        sa.Column('latency_ms', sa.Float(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.add_column('ai_task_routes', sa.Column('mode', sa.String(16), nullable=False, server_default='fallback'))


def downgrade() -> None:
    op.drop_column('ai_task_routes', 'mode')
    op.drop_table('data_sources')
    op.drop_index('ix_opportunity_transitions_opportunity_id', table_name='opportunity_transitions')
    op.drop_table('opportunity_transitions')
    op.drop_index('ix_opportunities_state_id', table_name='opportunities')
    op.drop_index('ix_opportunities_symbol_tf_id', table_name='opportunities')
    op.drop_index('ix_opportunities_created_at', table_name='opportunities')
    op.drop_index('ix_opportunities_signal_id', table_name='opportunities')
    op.drop_table('opportunities')
