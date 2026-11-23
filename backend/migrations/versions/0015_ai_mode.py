"""RR Trading One (additive): the operator's AI mode. ``AI`` (the default, the existing behaviour: only AI-confirmed
candidates are auto-executed) or ``ENGINE_ONLY`` (no AI review; the engine's own BUY/SELL signals are auto-executed,
the Risk Engine still decides). Existing rows get ``AI``; nothing else changes.

Rollback: ``alembic downgrade 0014`` drops only this column.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trading_state', sa.Column('ai_mode', sa.String(16), nullable=False, server_default='AI'))


def downgrade() -> None:
    op.drop_column('trading_state', 'ai_mode')
