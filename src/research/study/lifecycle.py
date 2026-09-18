"""Transactional lifecycle operations for research studies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from typing import Any, Optional
import uuid
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.db_schemas import ResearchStudyStatus, Study
from database.research_schemas import (
    RECORD_KIND_STUDY_LIFECYCLE,
    ResearchEnrollment,
    ResearchRecord,
    ResearchSessionV1,
    StudyAgentProfile,
    StudyAssignment,
)
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus
from research.participants.models import ResearchEligibility


@dataclass(frozen=True)
class StudyStopSummary:
    """Counts and identity returned by a terminal study stop."""

    study_id: uuid.UUID
    enrollment_count: int
    assignment_count: int
    session_count: int


@dataclass(frozen=True)
class EnrollmentRevokeSummary:
    """Summary returned after revoking one participant enrollment."""

    enrollment_id: uuid.UUID
    session_count: int


@dataclass(frozen=True)
class StudyEnrollmentSummary:
    """The enrollment and sticky profile assignment created by web consent."""

    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    assignment_id: Optional[uuid.UUID]
    agent_profile_id: Optional[uuid.UUID]
    created: bool
    reused: bool


def profile_snapshot(profile: Any) -> dict[str, Any]:
    """Return the non-secret profile configuration frozen into a study."""
    return {
        "profile_id": str(profile.profile_id),
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "release_id": profile.release_id,
        "connection_id": str(profile.connection_id) if profile.connection_id else None,
        "tools_json": profile.tools_json,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "temperature": profile.temperature,
        "max_context_tokens": profile.max_context_tokens,
    }


def allocate_join_code(session: Session) -> str:
    """Allocate a study-owned join code in the fresh schema."""
    for _ in range(8):
        candidate = secrets.token_hex(4).upper()
        exists = session.execute(
            select(Study.study_id).where(Study.join_code == candidate)
        ).scalar_one_or_none()
        if exists is None or not isinstance(exists, uuid.UUID):
            return candidate
    raise RuntimeError("unable to allocate a unique study join code")


def update_research_metadata(
    session: Session,
    study_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
) -> Study:
    """Update allowed metadata before the first consent locks the study."""
    study = session.execute(
        select(Study).where(Study.study_id == study_id).with_for_update()
    ).scalar_one_or_none()
    if study is None or not bool(getattr(study, "is_research", False)):
        raise ValueError("research study not found")
    if getattr(study, "consent_locked_at", None) is not None:
        raise PermissionError("study metadata is locked after first consent")
    if getattr(study, "research_status", None) == ResearchStudyStatus.STUDY_STOPPED.value:
        raise PermissionError("stopped study cannot be edited")

    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("study name must not be blank")
        setattr(study, "name", normalized_name)
    if description is not None:
        setattr(study, "description", description)
    if starts_at is not None:
        setattr(study, "starts_at", starts_at)
    if ends_at is not None:
        setattr(study, "ends_at", ends_at)
    session.commit()
    session.refresh(study)
    return study


def open_study_enrollment(
    session: Session,
    account_id: uuid.UUID,
    join_code: str,
    *,
    now: Optional[datetime] = None,
    rng: Optional[Any] = None,
) -> StudyEnrollmentSummary:
    """Accept web consent and create enrollment/profile assignment atomically."""
    timestamp = now or datetime.now(timezone.utc)
    normalized_code = str(join_code or "").strip().upper()
    study = session.execute(
        select(Study)
        .where(Study.join_code == normalized_code)
        .with_for_update()
    ).scalar_one_or_none()
    if study is None or not bool(getattr(study, "is_research", False)):
        raise ValueError("join code not found")
    if getattr(study, "research_status", None) == ResearchStudyStatus.STUDY_STOPPED.value:
        raise PermissionError("study is stopped")

    participant_row = identity_store.get_or_create_participant_row(
        session, account_id, now=timestamp, commit=False
    )
    participant_row = identity_store.lock_participant_by_account(session, account_id) or participant_row
    active_row = identity_store.get_active_enrollment_for_participant(session, participant_row.participant_id)
    if active_row is not None:
        if active_row.study_id != study.study_id:
            raise PermissionError("account already has an active enrollment")
        assignment = session.execute(
            select(StudyAssignment).where(StudyAssignment.enrollment_id == active_row.enrollment_id)
        ).scalar_one_or_none()
        # A reused join changes no state, but it still acquired study and
        # participant locks. Close the read transaction so callers that keep
        # the session alive cannot retain those locks after idempotent consent.
        session.commit()
        return StudyEnrollmentSummary(
            enrollment_id=active_row.enrollment_id,
            study_id=study.study_id,
            assignment_id=getattr(assignment, "assignment_id", None),
            agent_profile_id=getattr(assignment, "agent_profile_id", None),
            created=False,
            reused=True,
        )

    prior_row = identity_store.get_enrollment_for_participant_study(
        session, participant_row.participant_id, study.study_id
    )
    if prior_row is not None:
        raise PermissionError("account cannot rejoin this study")

    selected_profiles = list(
        session.execute(
            select(StudyAgentProfile)
            .where(StudyAgentProfile.study_id == study.study_id)
            .order_by(StudyAgentProfile.selection_order.asc())
            .with_for_update()
        ).scalars().all()
    )
    if not selected_profiles:
        raise ValueError("study has no selected agent profiles")
    selected = rng.choice(selected_profiles) if rng is not None else secrets.choice(selected_profiles)
    enrollment_id = uuid.uuid4()
    enrollment = ResearchEnrollment(
        enrollment_id=enrollment_id,
        participant_id=participant_row.participant_id,
        study_id=study.study_id,
        participant_code=identity_store.generate_participant_code(),
        status=EnrollmentStatus.ACTIVE.value,
        revocation_epoch=0,
        eligibility_json=ResearchEligibility(eligible=True).model_dump(mode="json"),
        enrolled_at=timestamp,
        updated_at=timestamp,
        consent_accepted_at=timestamp,
        retention_action="RETAIN_ANONYMIZED",
    )
    assignment = StudyAssignment(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment_id,
        study_id=study.study_id,
        agent_profile_id=selected.profile_id,
        strategy="RANDOM_EQUAL",
        randomization_epoch=0,
        profile_digest=selected.profile_digest,
        profile_snapshot_json=selected.profile_snapshot_json,
        status="ACTIVE",
        assigned_at=timestamp,
    )
    session.add_all([enrollment, assignment])
    if getattr(study, "consent_locked_at", None) is None:
        setattr(study, "consent_locked_at", timestamp)
    setattr(study, "research_status", ResearchStudyStatus.ACTIVE.value)
    setattr(study, "is_active", True)
    session.commit()
    return StudyEnrollmentSummary(
        enrollment_id=enrollment_id,
        study_id=study.study_id,
        assignment_id=assignment.assignment_id,
        agent_profile_id=assignment.agent_profile_id,
        created=True,
        reused=False,
    )


def clone_stopped_research_study(
    session: Session,
    study_id: uuid.UUID,
    *,
    actor: str,
) -> Study:
    """Create a fresh DRAFT from stopped metadata without copying participants."""
    source = session.execute(
        select(Study).where(Study.study_id == study_id).with_for_update()
    ).scalar_one_or_none()
    if source is None or not bool(getattr(source, "is_research", False)):
        raise ValueError("research study not found")
    if getattr(source, "research_status", None) != ResearchStudyStatus.STUDY_STOPPED.value:
        raise PermissionError("only stopped studies can be cloned")

    source_config = dict(getattr(source, "research_config_json", None) or {})
    source_config.pop("profile_ids", None)
    source_config.pop("agent_profile_ids", None)
    timestamp = datetime.now(timezone.utc)
    clone = Study(
        study_id=uuid.uuid4(),
        name=f"{source.name} (copy)",
        description=source.description,
        created_by=source.created_by,
        starts_at=source.starts_at,
        ends_at=source.ends_at,
        is_active=False,
        default_config_id=None,
        is_research=True,
        research_status=ResearchStudyStatus.DRAFT.value,
        research_config_json=source_config,
        research_config_digest=None,
        join_code=allocate_join_code(session),
        created_at=timestamp,
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)
    return clone


def revoke_research_enrollment(
    session: Session,
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    *,
    now: Optional[datetime] = None,
) -> EnrollmentRevokeSummary:
    """Revoke one enrollment while retaining all research records."""
    enrollment = session.execute(
        select(ResearchEnrollment)
        .where(
            ResearchEnrollment.enrollment_id == enrollment_id,
            ResearchEnrollment.study_id == study_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if enrollment is None:
        raise ValueError("enrollment not found")
    timestamp = now or datetime.now(timezone.utc)
    if getattr(enrollment, "status", None) != "REVOKED":
        setattr(enrollment, "status", "REVOKED")
        setattr(
            enrollment,
            "revocation_epoch",
            int(getattr(enrollment, "revocation_epoch", 0)) + 1,
        )
        setattr(enrollment, "updated_at", timestamp)
    assignments = list(
        session.execute(
            select(StudyAssignment)
            .where(StudyAssignment.enrollment_id == enrollment_id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for assignment in assignments:
        setattr(assignment, "status", "REVOKED")
    sessions = list(
        session.execute(
            select(ResearchSessionV1)
            .where(
                ResearchSessionV1.enrollment_id == enrollment_id,
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
        setattr(research_session, "close_reason", "REVOKED")
    session.commit()
    return EnrollmentRevokeSummary(
        enrollment_id=enrollment_id,
        session_count=len(sessions),
    )


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
    was_active = getattr(study, "research_status", None) != ResearchStudyStatus.STUDY_STOPPED.value
    if was_active:
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

    if was_active:
        session.add(
            ResearchRecord(
                record_id=uuid.uuid4(),
                kind=RECORD_KIND_STUDY_LIFECYCLE,
                scope_type="study",
                scope_id=study_id,
                study_id=study_id,
                actor=actor,
                occurred_at=timestamp,
                payload_json={
                    "event": "STUDY_STOPPED",
                    "enrollment_count": len(enrollments),
                    "assignment_count": len(assignments),
                    "session_count": len(sessions),
                },
            )
        )

    session.commit()
    return StudyStopSummary(
        study_id=study_id,
        enrollment_count=len(enrollments),
        assignment_count=len(assignments),
        session_count=len(sessions),
    )
