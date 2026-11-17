"""Parts 27-30 Phase 7 (additive): simulation runs (scenario stress tests and counterfactual alternatives). Research
records only. No existing table is changed.

Rollback: ``alembic downgrade 0013`` drops only this table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'simulation_runs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('subject_type', sa.String(24), nullable=False),
        sa.Column('subject_id', sa.BigInteger(), nullable=True),
        sa.Column('symbol', sa.String(64), nullable=False),
        sa.Column('timeframe', sa.String(8), nullable=False),
        sa.Column('setup', postgresql.JSONB(), nullable=False),
        sa.Column('result', postgresql.JSONB(), nullable=False),
        sa.Column('bars', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_simulation_runs_created_at', 'simulation_runs', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_simulation_runs_created_at', table_name='simulation_runs')
    op.drop_table('simulation_runs')
