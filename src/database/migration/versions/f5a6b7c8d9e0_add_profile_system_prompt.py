"""add researcher-defined system prompt to agent profiles

Adds ``system_prompt`` to ``agent_profile``: an optional researcher-authored
instruction set that fully replaces the built-in runtime prompt for the agent
(it is injected at the same point, and the runtime only appends the workspace
root for technical correctness). NULL means "use the runtime's default prompt".

The prompt is experiment configuration, not user content: it is authored by the
researcher as part of the arm's definition, so it is not subject to content
consent gating (unlike task messages). It is served to the agent through
``GET /api/acp/agent-config`` like the rest of the assigned profile.

Revision ID: f5a6b7c8d9e0
Revises: e2b3c4d5f6a8
Create Date: 2026-09-14

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f5a6b7c8d9e0"
down_revision = "e2b3c4d5f6a8"
branch_labels = None
depends_on = None

_SCHEMA = "public"


def upgrade() -> None:
    op.add_column(
        "agent_profile",
        sa.Column("system_prompt", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agent_profile", "system_prompt", schema=_SCHEMA)