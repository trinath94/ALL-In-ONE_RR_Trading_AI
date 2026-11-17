"""Parts 27-30 Phase 5 (additive): pattern detections from the new pattern detectors (harmonic, Elliott), with their
lifecycle, measurements and strategy relations. No existing table is changed.

Rollback: ``alembic downgrade 0012`` drops only this table.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    j = postgresql.JSONB()
    op.create_table(
        'pattern_detections',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('key', sa.String(160), nullable=False, unique=True),
        sa.Column('symbol_id', sa.Integer(), sa.ForeignKey('symbols.id', ondelete='CASCADE'), nullable=False),
        sa.Column('timeframe', sa.String(8), nullable=False),
        sa.Column('family', sa.String(24), nullable=False),
        sa.Column('pattern', sa.String(32), nullable=False),
        sa.Column('direction', sa.String(8), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('quality', sa.Float(), nullable=False),
        sa.Column('points', j, nullable=False), sa.Column('ratios', j, nullable=False),
        sa.Column('levels', j, nullable=False), sa.Column('history', j, nullable=False),
        sa.Column('measure', j, nullable=False), sa.Column('relations', j, nullable=False),
        sa.Column('context', j, nullable=False),
        sa.Column('rules', sa.Text(), nullable=False),
        sa.Column('detector_version', sa.String(32), nullable=False),
        sa.Column('known_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('found_live', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_pattern_detections_created_at', 'pattern_detections', ['created_at'])
    op.create_index('ix_pattern_detections_symbol_tf_id', 'pattern_detections', ['symbol_id', 'timeframe', 'id'])
    op.create_index('ix_pattern_detections_pattern_state', 'pattern_detections', ['pattern', 'state'])


def downgrade() -> None:
    op.drop_index('ix_pattern_detections_pattern_state', table_name='pattern_detections')
    op.drop_index('ix_pattern_detections_symbol_tf_id', table_name='pattern_detections')
    op.drop_index('ix_pattern_detections_created_at', table_name='pattern_detections')
    op.drop_table('pattern_detections')
