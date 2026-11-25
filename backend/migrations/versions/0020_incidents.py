"""Error detection and self-healing (additive): classified, de-duplicated incidents, and the administrator's switch for
safe automatic repairs (on by default; the catalogue never touches trading controls, orders or positions).

Rollback: ``alembic downgrade 0019`` drops only these.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'incidents',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('fingerprint', sa.String(40), nullable=False, unique=True),
        sa.Column('category', sa.String(32), nullable=False),
        sa.Column('severity', sa.String(10), nullable=False),
        sa.Column('component', sa.String(32), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('explanation', sa.Text(), nullable=False),
        sa.Column('diagnosis', sa.Text(), nullable=False),
        sa.Column('recommended_fix', sa.Text(), nullable=False),
        sa.Column('repair', sa.String(40), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('occurrences', sa.Integer(), nullable=False),
        sa.Column('reopened', sa.Integer(), nullable=False),
        sa.Column('data', postgresql.JSONB(), nullable=False),
        sa.Column('attempts', postgresql.JSONB(), nullable=False),
        sa.Column('ai_diagnosis', postgresql.JSONB(), nullable=True),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('resolution', sa.Text(), nullable=False),
        sa.Column('acknowledged_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_incidents_category', 'incidents', ['category'])
    op.create_index('ix_incident_status_seen', 'incidents', ['status', 'last_seen'])
    op.add_column('automation_policy', sa.Column('auto_repair', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('automation_policy', 'auto_repair')
    op.drop_index('ix_incident_status_seen', 'incidents')
    op.drop_index('ix_incidents_category', 'incidents')
    op.drop_table('incidents')
