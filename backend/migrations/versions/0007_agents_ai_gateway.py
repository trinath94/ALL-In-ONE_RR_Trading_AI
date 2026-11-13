"""RR Trading One: agent orchestration run log and AI provider gateway (additive)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        'agent_runs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('cycle_id', sa.String(36), nullable=False, index=True),
        sa.Column('agent_id', sa.String(48), nullable=False, index=True),
        sa.Column('agent_version', sa.String(16), nullable=False),
        sa.Column('task', sa.String(64), nullable=False),
        sa.Column('input_refs', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('output_schema', sa.String(64), nullable=False),
        sa.Column('output', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('duration_ms', sa.Float(), nullable=True),
        sa.Column('provider', sa.String(48), nullable=True),
        sa.Column('model', sa.String(96), nullable=True),
        sa.Column('evidence', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('errors', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_table(
        'ai_providers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(64), nullable=False, unique=True),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('base_url', sa.String(255), nullable=True),
        sa.Column('api_key_enc', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('timeout_s', sa.Integer(), nullable=False, server_default='120'),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('price_in_per_mtok', sa.Float(), nullable=True),
        sa.Column('price_out_per_mtok', sa.Float(), nullable=True),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'ai_task_routes',
        sa.Column('task', sa.String(48), primary_key=True),
        sa.Column('chain', JSONB, nullable=False),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'ai_usage',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('task', sa.String(48), nullable=False, index=True),
        sa.Column('provider', sa.String(64), nullable=False, index=True),
        sa.Column('model', sa.String(96), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('latency_ms', sa.Float(), nullable=False, server_default='0'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('fallback_from', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table('ai_usage')
    op.drop_table('ai_task_routes')
    op.drop_table('ai_providers')
    op.drop_table('agent_runs')
