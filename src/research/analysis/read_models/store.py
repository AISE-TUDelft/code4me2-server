"""Session-supplied read queries for the researcher control plane (Issue 12).

These helpers are thin wrappers over the canonical stores: they return domain
records (never raw ORM/JSON) and never join login identity into a read model.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from research.participants.models import Enrollment
    from research.runtime.assignment.models import AssignmentV1
    from research.runtime.sessions.models import AgentRunV1, ResearchSessionV1
    from research.telemetry.ingestion.models import ResearchEventRecord

from database.research_schemas import (
    ResearchAgentRun,
    ResearchEnrollment,
    ResearchEvent,
    StudyAssignment,
)
from database.research_schemas import (
    ResearchSessionV1 as ResearchSessionRow,
)
from research.participants import identity as identity_store
from research.runtime.assignment import store as assignment_store
from research.runtime.sessions import store as session_store
from research.telemetry.ingestion import store as ingestion_store

__all__ = [
    "list_agent_runs",
    "list_assignments",
    "list_enrollments",
    "list_events",
    "list_sessions",
]


def list_enrollments(session: Session, study_id: uuid.UUID) -> Sequence[Enrollment]:
    """List a study's enrollments as domain records (no login identity)."""
    statement = select(ResearchEnrollment).where(ResearchEnrollment.study_id == study_id)
    return [
        identity_store.row_to_enrollment(row)
        for row in session.execute(statement).scalars().all()
    ]


def list_assignments(session: Session, study_id: uuid.UUID) -> Sequence[AssignmentV1]:
    """List a study's assignments as domain records (joined via enrollment)."""
    statement = (
        select(StudyAssignment)
        .join(
            ResearchEnrollment,
            StudyAssignment.enrollment_id == ResearchEnrollment.enrollment_id,
        )
        .where(ResearchEnrollment.study_id == study_id)
    )
    return [
        assignment_store.row_to_assignment(row)
        for row in session.execute(statement).scalars().all()
    ]


def list_sessions(
    session: Session, study_id: uuid.UUID
) -> Sequence[ResearchSessionV1]:
    """List a study's research sessions as domain records."""
    statement = (
        select(ResearchSessionRow)
        .join(ResearchEnrollment, ResearchSessionRow.enrollment_id == ResearchEnrollment.enrollment_id)
        .where(ResearchEnrollment.study_id == study_id)
        .order_by(ResearchSessionRow.created_at.asc())
    )
    return [
        session_store.row_to_session(row)
        for row in session.execute(statement).scalars().all()
    ]


def list_agent_runs(
    session: Session, research_session_id: uuid.UUID
) -> Sequence[AgentRunV1]:
    """List the agent runs of one session as domain records."""
    statement = (
        select(ResearchAgentRun)
        .where(ResearchAgentRun.research_session_id == research_session_id)
        .order_by(ResearchAgentRun.started_at.asc())
    )
    return [
        session_store.row_to_agent_run(row)
        for row in session.execute(statement).scalars().all()
    ]


def list_events(
    session: Session,
    study_id: uuid.UUID,
    *,
    research_session_ids: Optional[Sequence[uuid.UUID]] = None,
) -> Sequence[ResearchEventRecord]:
    """List a study's canonical events as records (sorted deterministically).

    Retention tombstones (``retention_state == "DELETED"``) are excluded so a
    post-withdrawal export never reads deleted data.
    """
    statement = select(ResearchEvent).where(
        ResearchEvent.study_id == study_id,
        ResearchEvent.retention_state != "DELETED",
    )
    if research_session_ids is not None:
        statement = statement.where(
            ResearchEvent.research_session_id.in_(list(research_session_ids))
        )
    statement = statement.order_by(
        ResearchEvent.research_session_id.asc(),
        ResearchEvent.emitter_id.asc(),
        ResearchEvent.emitter_sequence.asc(),
    )
    return [
        ingestion_store.row_to_record(row)
        for row in session.execute(statement).scalars().all()
    ]
