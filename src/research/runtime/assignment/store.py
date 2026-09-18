"""CRUD-style persistence helpers for assignment and exposure.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. Assignments are
immutable facts; the unique ``(enrollment_id, study_revision_id)`` constraint is
the concurrency guard, so a concurrent first-bootstrap race yields one row and
the loser re-reads it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

    from .models import AssignmentV1, ExposureV1

from database.research_schemas import StudyAssignment

from .enums import ExposureOutcome


def create_assignment(
    session: Session, assignment: AssignmentV1, *, commit: bool = True
) -> StudyAssignment:
    """Insert one immutable assignment row, or return the existing winner.

    The unique ``(enrollment_id, study_revision_id)`` constraint is the
    concurrency guard: a savepoint around the insert means a losing concurrent
    create rolls back only its own insert and returns the winning row, so the
    caller always uses the authoritative sticky assignment. ``commit=False`` lets
    bootstrap own one unit of work.
    """
    row = StudyAssignment(
        assignment_id=assignment.assignment_id,
        enrollment_id=assignment.enrollment_id,
        study_revision_id=assignment.study_revision_id,
        condition_id=assignment.condition_id,
        strategy=assignment.strategy,
        randomization_epoch=assignment.randomization_epoch,
        protocol_digest=assignment.protocol_digest,
        assigned_at=assignment.assigned_at,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        existing = get_assignment_for_enrollment_revision(
            session, assignment.enrollment_id, assignment.study_revision_id
        )
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


def get_assignment_for_enrollment_revision(
    session: Session, enrollment_id: uuid.UUID, study_revision_id: uuid.UUID
) -> Optional[StudyAssignment]:
    """Fetch the unique sticky assignment for ``(enrollment, revision)``."""
    statement = select(StudyAssignment).where(
        StudyAssignment.enrollment_id == enrollment_id,
        StudyAssignment.study_revision_id == study_revision_id,
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


def insert_exposure(session: Session, exposure: ExposureV1) -> Any:
    """Reject the removed condition-exposure persistence contract."""
    raise NotImplementedError("condition exposure persistence was removed")


def get_exposure_by_idempotency_key(
    session: Session, idempotency_key: str
) -> Any:
    """Reject the removed condition-exposure lookup contract."""
    raise NotImplementedError("condition exposure persistence was removed")


def list_exposures(
    session: Session, assignment_id: Optional[uuid.UUID] = None
) -> Sequence[Any]:
    """Reject the removed condition-exposure listing contract."""
    raise NotImplementedError("condition exposure persistence was removed")


def row_to_assignment(row: StudyAssignment) -> AssignmentV1:
    """Rehydrate an assignment row into the domain model."""
    from .models import AssignmentV1 as AssignmentV1Model

    return AssignmentV1Model(
        assignment_id=row.assignment_id,
        enrollment_id=row.enrollment_id,
        study_revision_id=row.study_revision_id,
        condition_id=row.condition_id,
        strategy=row.strategy,
        randomization_epoch=row.randomization_epoch,
        assigned_at=row.assigned_at,
        protocol_digest=row.protocol_digest,
    )


def row_to_exposure(row: Any) -> ExposureV1:
    """Rehydrate an exposure row into the domain model."""
    from .models import ExposureEnvironment
    from .models import ExposureV1 as ExposureV1Model

    return ExposureV1Model(
        exposure_id=row.exposure_id,
        assignment_id=row.assignment_id,
        study_revision_id=row.study_revision_id,
        environment=ExposureEnvironment.model_validate(row.environment_json or {}),
        agent_release_id=row.agent_release_id,
        artifact_digest=row.artifact_digest,
        adapter_version=row.adapter_version,
        observed_configuration=row.observed_configuration or {},
        started_at=row.started_at,
        outcome=ExposureOutcome(row.outcome),
        evidence_digest=row.evidence_digest,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
    )


def exposure_summary(row: Any) -> dict[str, Any]:
    """Return a compact exposure summary safe for responses."""
    started_at = row.started_at
    return {
        "exposure_id": str(row.exposure_id),
        "assignment_id": str(row.assignment_id),
        "study_revision_id": str(row.study_revision_id),
        "agent_release_id": row.agent_release_id,
        "artifact_digest": row.artifact_digest,
        "outcome": row.outcome,
        "started_at": (
            started_at.isoformat() if isinstance(started_at, datetime) else None
        ),
        "evidence_digest": row.evidence_digest,
        "idempotency_key": row.idempotency_key,
    }
