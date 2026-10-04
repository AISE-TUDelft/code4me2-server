"""record which consent version each enrollment accepted

Revision ID: c5e8f1a2d3b4
Revises: b7c1d2e3f4a5
Create Date: 2026-10-04 03:30:00

A study may carry its own consent document and tick-box statements; each
enrollment now stores the digest of the consent view it accepted and that view
with the participant's answers. Both columns are nullable and older code
ignores them; enrollments created before this revision keep null values.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c5e8f1a2d3b4'
down_revision = 'b7c1d2e3f4a5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.research_enrollment "
        "ADD COLUMN IF NOT EXISTS consent_digest VARCHAR NULL, "
        "ADD COLUMN IF NOT EXISTS consent_snapshot_json JSONB NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.research_enrollment "
        "DROP COLUMN IF EXISTS consent_snapshot_json, "
        "DROP COLUMN IF EXISTS consent_digest"
    )
