"""RR Trading One: symbol catalogue tracking — exclusion, last seen, no longer offered (additive)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('symbols', sa.Column('excluded', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('symbols', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('symbols', sa.Column('missing_since', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('symbols', 'missing_since')
    op.drop_column('symbols', 'last_seen_at')
    op.drop_column('symbols', 'excluded')
