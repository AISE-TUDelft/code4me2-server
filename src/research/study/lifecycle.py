"""Transactional lifecycle operations for research studies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.db_schemas import ResearchStudyStatus, Study
from database.research_schemas import (
    ResearchEnrollment,
    ResearchSessionV1,
    StudyAssignment,
)


@dataclass(frozen=True)
class StudyStopSummary:
    """Counts and identity returned by a terminal study stop."""

    study_id: uuid.UUID
    enrollment_count: int
    assignment_count: int
    session_count: int


def stop_research_study(
    session: Session,
    study_id: uuid.UUID,
    *,
    actor: str,
    now: Optional[datetime] = None,
) -> StudyStopSummary:
    """Stop a research study without deleting retained research data."""
    study = session.execute(
        select(Study).where(Study.study_id == study_id).with_for_update()
    ).scalar_one_or_none()
    if study is None or not bool(getattr(study, "is_research", False)):
        raise ValueError("research study not found")

    timestamp = now or datetime.now(timezone.utc)
    if getattr(study, "research_status", None) != ResearchStudyStatus.STUDY_STOPPED.value:
        setattr(study, "research_status", ResearchStudyStatus.STUDY_STOPPED.value)
        setattr(study, "is_active", False)
        setattr(study, "stopped_at", timestamp)
        setattr(study, "stopped_by", actor)

    enrollments = list(
        session.execute(
            select(ResearchEnrollment)
            .where(
                ResearchEnrollment.study_id == study_id,
                ResearchEnrollment.status == "ACTIVE",
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    enrollment_ids = [enrollment.enrollment_id for enrollment in enrollments]
    for enrollment in enrollments:
        setattr(enrollment, "status", "STUDY_STOPPED")
        setattr(
            enrollment,
            "revocation_epoch",
            int(getattr(enrollment, "revocation_epoch", 0)) + 1,
        )
        setattr(enrollment, "updated_at", timestamp)

    assignments = []
    if enrollment_ids:
        assignments = list(
            session.execute(
                select(StudyAssignment)
                .where(StudyAssignment.enrollment_id.in_(enrollment_ids))
                .with_for_update()
            )
            .scalars()
            .all()
        )
        for assignment in assignments:
                setattr(assignment, "status", "STUDY_STOPPED")

    sessions = list(
        session.execute(
            select(ResearchSessionV1)
            .where(
                ResearchSessionV1.study_id == study_id,
                ResearchSessionV1.state.not_in(("ended", "revoked")),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for research_session in sessions:
        setattr(research_session, "state", "revoked")
        setattr(research_session, "closed_at", timestamp)
        setattr(research_session, "close_reason", "STUDY_STOPPED")

    session.commit()
    return StudyStopSummary(
        study_id=study_id,
        enrollment_count=len(enrollments),
        assignment_count=len(assignments),
        session_count=len(sessions),
    )
