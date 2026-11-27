"""Priority symbols: the owner's list of symbols the scanner and the candidate ranking put first every day.

NULL = the built-in default list (app.scanner.priority.DEFAULT). Additive.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scanner_settings", sa.Column("priority_symbols", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("scanner_settings", "priority_symbols")
