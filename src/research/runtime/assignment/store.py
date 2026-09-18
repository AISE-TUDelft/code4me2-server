"""CRUD-style persistence helpers for assignment.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. Assignments are
immutable facts; the unique ``enrollment_id`` constraint is
the concurrency guard, so a concurrent first-bootstrap race yields one row and
the loser re-reads it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

    from .models import AssignmentV1

from database.research_schemas import StudyAssignment


def create_assignment(
    session: Session, assignment: AssignmentV1, *, commit: bool = True
) -> StudyAssignment:
    """Insert one immutable assignment row, or return the existing winner.

    The unique ``enrollment_id`` constraint is the
    concurrency guard: a savepoint around the insert means a losing concurrent
    create rolls back only its own insert and returns the winning row, so the
    caller always uses the authoritative sticky assignment. ``commit=False`` lets
    bootstrap own one unit of work.
    """
    row = StudyAssignment(
        assignment_id=assignment.assignment_id,
        enrollment_id=assignment.enrollment_id,
        study_id=assignment.study_id,
        agent_profile_id=assignment.agent_profile_id,
        strategy=assignment.strategy,
        randomization_epoch=assignment.randomization_epoch,
        profile_digest=assignment.profile_digest,
        profile_snapshot_json=assignment.profile_snapshot_json,
        status=assignment.status,
        assigned_at=assignment.assigned_at,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        existing = get_assignment_for_enrollment(session, assignment.enrollment_id)
        if existing is not None:
            return existing
        raise
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(row)
    return row


def get_assignment(
    session: Session, assignment_id: uuid.UUID
) -> Optional[StudyAssignment]:
    """Fetch an assignment by id, or ``None``."""
    return session.get(StudyAssignment, assignment_id)


def get_assignment_for_enrollment(
    session: Session, enrollment_id: uuid.UUID
) -> Optional[StudyAssignment]:
    """Fetch the unique sticky assignment for one enrollment."""
    statement = select(StudyAssignment).where(
        StudyAssignment.enrollment_id == enrollment_id,
    )
    return session.execute(statement).scalars().first()


def list_assignments(
    session: Session, enrollment_id: Optional[uuid.UUID] = None
) -> Sequence[StudyAssignment]:
    """List assignments, optionally for one enrollment."""
    statement = select(StudyAssignment)
    if enrollment_id is not None:
        statement = statement.where(StudyAssignment.enrollment_id == enrollment_id)
    statement = statement.order_by(StudyAssignment.assigned_at.asc())
    return list(session.execute(statement).scalars().all())


def row_to_assignment(row: StudyAssignment) -> AssignmentV1:
    """Rehydrate an assignment row into the domain model."""
    from .models import AssignmentV1 as AssignmentV1Model

    return AssignmentV1Model(
        assignment_id=row.assignment_id,
        enrollment_id=row.enrollment_id,
        study_id=row.study_id,
        agent_profile_id=row.agent_profile_id,
        strategy=row.strategy,
        randomization_epoch=row.randomization_epoch,
        assigned_at=row.assigned_at,
        profile_digest=row.profile_digest,
        profile_snapshot_json=row.profile_snapshot_json,
        status=row.status,
    )
