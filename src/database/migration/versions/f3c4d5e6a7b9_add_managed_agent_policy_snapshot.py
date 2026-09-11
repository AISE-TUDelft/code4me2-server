"""add managed agent policy snapshot

Revision ID: f3c4d5e6a7b9
Revises: e2b3c4d5f6a8
Create Date: 2026-09-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3c4d5e6a7b9"
down_revision = "e2b3c4d5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_task",
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("agent_task", "policy_snapshot", schema="public")
