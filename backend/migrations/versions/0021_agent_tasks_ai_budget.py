"""Autonomous agent upgrade (additive): Engine Assistant tasks (command, findings, plan, report), the AI API budget
(no limits by default) and the policy for an unavailable AI (default "pause": the existing behaviour).

Rollback: ``alembic downgrade 0020`` drops only these.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ai_control', sa.Column('daily_budget_usd', sa.Float(), nullable=True))
    op.add_column('ai_control', sa.Column('monthly_budget_usd', sa.Float(), nullable=True))
    op.add_column('ai_control', sa.Column('daily_token_limit', sa.Integer(), nullable=True))
    op.add_column('ai_control', sa.Column('ai_unavailable_policy', sa.String(16), nullable=False, server_default='pause'))
    op.create_table(
        'agent_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('command', sa.Text(), nullable=False),
        sa.Column('workflow', sa.String(32), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('findings', postgresql.JSONB(), nullable=False),
        sa.Column('plan', postgresql.JSONB(), nullable=False),
        sa.Column('report', postgresql.JSONB(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_agent_tasks_workflow', 'agent_tasks', ['workflow'])
    op.create_index('ix_agent_tasks_created_at', 'agent_tasks', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_agent_tasks_created_at', 'agent_tasks')
    op.drop_index('ix_agent_tasks_workflow', 'agent_tasks')
    op.drop_table('agent_tasks')
    for c in ('ai_unavailable_policy', 'daily_token_limit', 'monthly_budget_usd', 'daily_budget_usd'):
        op.drop_column('ai_control', c)
