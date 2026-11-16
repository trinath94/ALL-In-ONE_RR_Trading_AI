"""RR Trading One Phase 15 (additive): when auto trading was last switched on. The trading worker executes only
signals created after it, so a candidate produced while auto trading was off is never executed retroactively.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trading_state', sa.Column('auto_trading_since', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('trading_state', 'auto_trading_since')
