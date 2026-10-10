"""store the commands a built-in agent profile blocks

Revision ID: d41e7c9a2b6f
Revises: c5e8f1a2d3b4
Create Date: 2026-10-10 12:00:00

Built-in (``code4me2-agent``) profiles now list the programs the agent may not
run (``commands_denylist``) instead of the ones it may run. The new column is
nullable (NULL blocks nothing) and older code ignores it. The retired
``commands_allowlist_json`` column is left in place and unused, so a rollback
to the previous backend still finds it; study snapshots keep their own copy.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'd41e7c9a2b6f'
down_revision = 'c5e8f1a2d3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.agent_profile "
        "ADD COLUMN IF NOT EXISTS commands_denylist_json TEXT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.agent_profile DROP COLUMN IF EXISTS commands_denylist_json"
    )
