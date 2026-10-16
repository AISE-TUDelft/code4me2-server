"""CRUD-style persistence helpers for lifecycle-owned research studies.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. The router is
responsible for session lifecycle (``App.get_db_session`` / ``rollback`` /
``close``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from agents.tools import set_harness_profile_fields
from database.db_schemas import AgentProfile, Study as StudyRow
from database import crud as database_crud
from database.research_schemas import (
    ResearchEnrollment,
    ResearchSessionV1,
    StudyAgentProfile,
    StudyAssignment,
)
from research.study.agents.distributions import validate_profile_configuration
from research.study.agents.enums import QualificationStatus
from research.study.agents.store import get_release, row_to_release
from research.canonical import canonical_hash



def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StudyView:
    """Study identity, backed by the real ``public.study`` row."""

    study_id: uuid.UUID
    name: str
    description: Optional[str]
    owner: Optional[str]
    created_by: Optional[uuid.UUID] = None
    is_research: bool = False
    is_active: bool = False
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    research_status: Optional[str] = None
    research_config_digest: Optional[str] = None
    join_code: Optional[str] = None
    consent_locked_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    stopped_by: Optional[str] = None
    created_at: Optional[datetime] = None
    profile_selections: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class StudyReadMetrics:
    """Safe researcher-facing counts derived from persisted study rows."""

    enrollment_count: int
    active_enrollment_count: int
    assignment_count: int
    active_assignment_count: int
    active_session_count: int


def _study_row_view(
    row: StudyRow, profile_selections: Optional[list[dict[str, Any]]] = None
) -> StudyView:
    return StudyView(
        study_id=getattr(row, "study_id"),
        name=getattr(row, "name"),
        description=getattr(row, "description", None),
        owner=None,
        created_by=getattr(row, "created_by", None),
        is_research=bool(getattr(row, "is_research", False)),
        is_active=bool(getattr(row, "is_active", False)),
        starts_at=getattr(row, "starts_at", None),
        ends_at=getattr(row, "ends_at", None),
        research_status=getattr(row, "research_status", None),
        research_config_digest=getattr(row, "research_config_digest", None),
        join_code=getattr(row, "join_code", None),
        consent_locked_at=getattr(row, "consent_locked_at", None),
        stopped_at=getattr(row, "stopped_at", None),
        stopped_by=getattr(row, "stopped_by", None),
        created_at=getattr(row, "created_at", None),
        profile_selections=list(profile_selections or []),
    )


def _study_view(session: Session, row: StudyRow) -> StudyView:
    selections = session.execute(
        select(StudyAgentProfile)
        .where(StudyAgentProfile.study_id == row.study_id)
        .order_by(StudyAgentProfile.selection_order.asc())
    ).scalars().all()
    return _study_row_view(
        row,
        [
            {
                "profile_id": str(selection.profile_id),
                "name": (selection.profile_snapshot_json or {}).get("name", ""),
                "model": (selection.profile_snapshot_json or {}).get("model", ""),
                "profile_digest": selection.profile_digest,
                "selection_order": selection.selection_order,
            }
            for selection in selections
        ],
    )


def build_profile_selections(
    session: Session,
    *,
    study_id: uuid.UUID,
    profile_ids: Optional[Sequence[uuid.UUID]],
    created_by: Optional[uuid.UUID],
    allow_shared_profiles: bool = False,
    timestamp: Optional[datetime] = None,
) -> list[StudyAgentProfile]:
    """Validate and build the frozen ``StudyAgentProfile`` rows for a study.

    The single implementation of the profile-freeze invariants (owner, active,
    release-qualified, executable configuration) shared by :func:`create_study`
    and the stopped-study clone. The returned rows are *not* added to the
    session: the caller inserts them in its own transaction so study creation
    and clone stay atomic.
    """
    selected_profile_ids = list(profile_ids or [])
    if len(selected_profile_ids) != len(set(selected_profile_ids)):
        raise ValueError("study profile selection contains duplicates")
    created_at = timestamp or _now()
    selections: list[StudyAgentProfile] = []
    for selection_order, profile_id in enumerate(selected_profile_ids):
        profile = session.get(AgentProfile, profile_id)
        if profile is None or not bool(getattr(profile, "is_active", True)):
            raise ValueError("selected agent profile is unavailable")
        if not allow_shared_profiles and profile.owner_user_id != created_by:
            raise PermissionError("selected agent profile is not owned by the researcher")
        database_crud.validate_profile_release(session, profile.release_id)
        if not profile.release_id:
            raise ValueError("RELEASE_UNRESOLVED: selected profile has no release")
        release = get_release(session, profile.release_id)
        if release is None:
            raise ValueError("RELEASE_UNRESOLVED: selected profile release is missing")
        release_status = str(release.status or "").upper()
        if release_status in {
            QualificationStatus.RETIRED.value,
            QualificationStatus.BLOCKED.value,
        }:
            raise ValueError("RELEASE_WITHDRAWN: selected profile release is withdrawn")
        if release_status != QualificationStatus.QUALIFIED.value:
            raise ValueError("RELEASE_NOT_QUALIFIED: selected profile release is not qualified")
        # Framework, distribution mode/identity, tools and approval evidence must
        # be executable *together* before the profile is frozen into the study
        # (ISSUE-03/17). A qualified BYOA release stays a valid choice.
        validate_profile_configuration(
            profile, row_to_release(release), release_json=release.release_json
        )
        snapshot = {
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
        # Only a set prompt is frozen, so a profile without one keeps the exact
        # snapshot (and profile_digest) it produced before the column existed.
        if getattr(profile, "system_prompt", None) is not None:
            snapshot["system_prompt"] = profile.system_prompt
        # Likewise the built-in runtime's command/harness settings (D-01),
        # validated above with the profile↔release contract.
        snapshot.update(set_harness_profile_fields(profile))
        selections.append(
            StudyAgentProfile(
                study_id=study_id,
                profile_id=profile.profile_id,
                profile_digest=canonical_hash(snapshot),
                profile_snapshot_json=snapshot,
                selection_order=selection_order,
                created_at=created_at,
            )
        )
    return selections


def create_study(
    session: Session,
    *,
    study_id: uuid.UUID,
    name: str,
    description: Optional[str] = None,
    owner: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
    starts_at: Optional[datetime] = None,
    ends_at: Optional[datetime] = None,
    is_research: bool = True,
    default_config_id: Optional[int] = None,
    research_status: Optional[str] = None,
    research_config_json: Optional[dict[str, Any]] = None,
    research_config_digest: Optional[str] = None,
    join_code: Optional[str] = None,
    profile_ids: Optional[Sequence[uuid.UUID]] = None,
    allow_shared_profiles: bool = False,
    now: Optional[datetime] = None,
    inference_budget_default_micro_usd: int = 0,
    inference_budget_warning_fraction: Optional[Any] = None,
    inference_budget_updated_by: Optional[str] = None,
) -> StudyView:
    """Insert the real ``public.study`` identity row.

    Agent research studies set ``is_research`` and never fabricate a completion
    ``default_config_id``. Research rows start in ``DRAFT`` unless a caller
    explicitly supplies another lifecycle state.
    """
    timestamp = now or _now()
    selected_profile_ids = list(profile_ids or [])
    config = dict(research_config_json or {})
    if selected_profile_ids:
        config["profile_ids"] = [str(profile_id) for profile_id in selected_profile_ids]
    config_digest = research_config_digest or canonical_hash(config)
    row = StudyRow(
        study_id=study_id,
        name=name,
        description=description,
        created_by=created_by,
        starts_at=starts_at or timestamp,
        ends_at=ends_at,
        is_active=False,
        default_config_id=default_config_id,
        is_research=is_research,
        research_status=(research_status or ("DRAFT" if is_research else None)),
        research_config_json=config,
        research_config_digest=config_digest,
        join_code=join_code,
        created_at=timestamp,
        # Participant budgets (shared provider key): editable later and kept
        # outside the digested research configuration on purpose.
        inference_budget_default_micro_usd=int(inference_budget_default_micro_usd or 0),
        inference_budget_updated_at=timestamp if inference_budget_default_micro_usd else None,
        inference_budget_updated_by=inference_budget_updated_by,
    )
    if inference_budget_warning_fraction is not None:
        row.inference_budget_warning_fraction = inference_budget_warning_fraction
    session.add(row)
    for selection in build_profile_selections(
        session,
        study_id=study_id,
        profile_ids=selected_profile_ids,
        created_by=created_by,
        allow_shared_profiles=allow_shared_profiles,
        timestamp=timestamp,
    ):
        session.add(selection)
    session.commit()
    session.refresh(row)
    return _study_view(session, row)


def get_study(session: Session, study_id: uuid.UUID) -> Optional[StudyView]:
    """Fetch a study identity row by id, or ``None``."""
    row = session.get(StudyRow, study_id)
    return _study_view(session, row) if row is not None else None


def get_study_read_metrics(session: Session, study_id: uuid.UUID) -> StudyReadMetrics:
    """Return persisted counts without selecting private participant fields."""
    enrollment_count, active_enrollment_count = session.execute(
        select(
            func.count(ResearchEnrollment.enrollment_id),
            func.count(ResearchEnrollment.enrollment_id).filter(
                ResearchEnrollment.status == "ACTIVE"
            ),
        ).where(ResearchEnrollment.study_id == study_id)
    ).one()
    assignment_count, active_assignment_count = session.execute(
        select(
            func.count(StudyAssignment.assignment_id),
            func.count(StudyAssignment.assignment_id).filter(
                StudyAssignment.status == "ACTIVE"
            ),
        ).where(StudyAssignment.study_id == study_id)
    ).one()
    active_session_count = session.execute(
        select(func.count(ResearchSessionV1.session_id)).where(
            ResearchSessionV1.study_id == study_id,
            ResearchSessionV1.state.not_in(("ended", "revoked")),
        )
    ).scalar_one()
    return StudyReadMetrics(
        enrollment_count=int(enrollment_count),
        active_enrollment_count=int(active_enrollment_count),
        assignment_count=int(assignment_count),
        active_assignment_count=int(active_assignment_count),
        active_session_count=int(active_session_count),
    )


def list_studies(
    session: Session, owner_user_id: Optional[uuid.UUID] = None
) -> Sequence[StudyView]:
    """List studies, owner-scoped when ``owner_user_id`` is given."""
    statement = select(StudyRow)
    if owner_user_id is not None:
        statement = statement.where(StudyRow.created_by == owner_user_id)
    statement = statement.order_by(StudyRow.created_at.asc())
    return [_study_view(session, row) for row in session.execute(statement).scalars().all()]


def set_study_active(
    session: Session, study_id: uuid.UUID, active: bool
) -> Optional[StudyRow]:
    """Apply an ordinary activation/deactivation lifecycle transition.

    Activation is a plain projection update: it sets ``is_active`` alongside
    ``research_status`` and imposes no owner-level study limit, so several
    studies may be ACTIVE for the same owner at once. A stopped study is
    terminal and cannot be reactivated (``ValueError``).
    """
    row = session.get(StudyRow, study_id)
    if row is None:
        return None
    if (
        active
        and getattr(row, "research_status", None) == "STUDY_STOPPED"
    ):
        raise ValueError("stopped research studies cannot be reactivated")
    setattr(row, "is_active", active)
    if getattr(row, "is_research", False):
        setattr(row, "research_status", "ACTIVE" if active else "DRAFT")
    session.commit()
    session.refresh(row)
    return row


def research_study_is_open(study: Any, now: Optional[datetime] = None) -> bool:
    """Whether a research study is currently open for execution.

    ``is_active`` alone is a writer-set flag; an ended window must refuse
    behaviourally even before a writer flips the flag, so the reader path checks
    the schedule too.
    """
    if study is None or not getattr(study, "is_research", False):
        return False
    if not getattr(study, "is_active", False):
        return False
    timestamp = now or _now()
    starts_at = getattr(study, "starts_at", None)
    ends_at = getattr(study, "ends_at", None)
    if starts_at is not None and timestamp < starts_at:
        return False
    if ends_at is not None and timestamp > ends_at:
        return False
    return True


def get_study_by_join_code(
    session: Session, join_code: str
) -> Optional[Any]:
    """Fetch a study by its study-owned join code."""
    normalized = str(join_code or "").strip().upper()
    if not normalized:
        return None
    return session.execute(
        select(StudyRow).where(StudyRow.join_code == normalized)
    ).scalars().first()


def get_study_join_code(
    session: Session, study_id: uuid.UUID
) -> Optional[Any]:
    """Fetch the study-owned join code, or ``None``."""
    row = session.get(StudyRow, study_id)
    return row if row is not None and getattr(row, "join_code", None) else None
