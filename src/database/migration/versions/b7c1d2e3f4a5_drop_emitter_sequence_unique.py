"""drop the (session, emitter, sequence) unique constraint on research_event

Revision ID: b7c1d2e3f4a5
Revises: 8a0084080b46
Create Date: 2026-09-27 02:10:00

An emitter whose sequence counter restarts inside a live session (an IDE
restart, a re-launched proxy) reused ``(research_session_id, emitter_id,
emitter_sequence)`` and the unique constraint made ingestion reject - and the
client delete - the later facts (production-readiness review C-01). Identity is
``event_id`` + digest; the composite stays indexed for ordering and gap
diagnostics only.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b7c1d2e3f4a5'
down_revision = '8a0084080b46'
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_research_event_session_emitter_sequence"
_INDEX = "idx_research_event_session_emitter_sequence"


def upgrade() -> None:
    op.execute(f"ALTER TABLE public.research_event DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON public.research_event "
        "(research_session_id, emitter_id, emitter_sequence)"
    )


def downgrade() -> None:
    # Re-creating the unique constraint fails if overlapping sequences were
    # stored in the meantime; that is the intended signal (the data is valid).
    op.execute(f"DROP INDEX IF EXISTS public.{_INDEX}")
    op.execute(
        f"ALTER TABLE public.research_event ADD CONSTRAINT {_CONSTRAINT} "
        "UNIQUE (research_session_id, emitter_id, emitter_sequence)"
    )
