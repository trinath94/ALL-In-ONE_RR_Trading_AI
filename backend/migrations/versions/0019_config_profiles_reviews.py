"""Adaptive configuration and trade reviews (additive): versioned strategy configuration profiles, the automatic
promotion policy (everything off), and a structured review on each failure report. No existing row changes; the
strategies, their source and default parameters are untouched.

Rollback: ``alembic downgrade 0018`` drops only these.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'strategy_config_profiles',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('strategy_code', sa.String(96), sa.ForeignKey('strategies.code', ondelete='CASCADE'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), sa.ForeignKey('strategy_config_profiles.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('params', postgresql.JSONB(), nullable=False),
        sa.Column('regime', sa.String(32), nullable=True),
        sa.Column('symbols', postgresql.JSONB(), nullable=True),
        sa.Column('timeframes', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('ai_provider', sa.String(160), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('validation', postgresql.JSONB(), nullable=False),
        sa.Column('history', postgresql.JSONB(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('strategy_code', 'version', name='uq_profile_version'),
    )
    op.create_index('ix_profile_strategy_status', 'strategy_config_profiles', ['strategy_code', 'status'])
    op.create_index('ix_strategy_config_profiles_status', 'strategy_config_profiles', ['status'])
    op.create_index('ix_strategy_config_profiles_created_at', 'strategy_config_profiles', ['created_at'])
    op.create_table(
        'automation_policy',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('auto_promote_profiles_demo', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('auto_promote_experiments_demo', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('failure_reports', sa.Column('review', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('failure_reports', 'review')
    op.drop_table('automation_policy')
    op.drop_index('ix_strategy_config_profiles_created_at', 'strategy_config_profiles')
    op.drop_index('ix_strategy_config_profiles_status', 'strategy_config_profiles')
    op.drop_index('ix_profile_strategy_status', 'strategy_config_profiles')
    op.drop_table('strategy_config_profiles')
