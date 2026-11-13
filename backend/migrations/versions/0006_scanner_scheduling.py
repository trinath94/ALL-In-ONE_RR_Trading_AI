"""RR Trading One: multi-symbol / multi-timeframe scanning — configured peer symbols, scanner settings (additive)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('symbols', sa.Column('peers', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))
    op.create_table(
        'scanner_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('timeframes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('id = 1', name='ck_scanner_settings_single_row'),
    )


def downgrade() -> None:
    op.drop_table('scanner_settings')
    op.drop_column('symbols', 'peers')
