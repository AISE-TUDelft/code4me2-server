"""Server-authoritative agent assignment for research tasks.

There is exactly one assignment authority: the research ``study_assignment`` row
allocated by :mod:`research.runtime.assignment`. Bootstrap and task creation both
resolve that same persisted row, so a participant always gets one sticky agent
profile per enrollment.

A research run requires a real participant mapping, an **active** enrollment, an
open research study window, and a selected profile snapshot. When any of those
is missing this returns ``None``; mutable profile templates are never used as a
runtime fallback.
"""

from __future__ import annotations

import logging
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database.db_schemas import Study as StudyRow
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus
from research.runtime.assignment import store as assignment_store
from research.runtime.assignment.enums import AllocationOutcome
from research.runtime.assignment.models import StudyProfileSelection
from research.runtime.assignment.service import allocate
from research.study.protocol import store as protocol_store


@dataclass(frozen=True)
class FrozenAgentConfig:
    """The immutable execution config of one assigned condition.

    Built only from the revision's frozen ``resolved_distribution`` (and its
    ``agent_config``). It is a plain value object, never a mutable ORM
    ``AgentProfile`` row, so editing a profile template cannot change a
    published study's tasks or a bootstrap manifest.
    """

    profile_id: uuid.UUID
    name: str
    model: str
    framework_version: str
    tools_json: str
    approval_policy: str
    max_steps: int
    temperature: Optional[float] = None
    max_context_tokens: Optional[int] = None
    connection_id: Optional[uuid.UUID] = None
    connection_label: Optional[str] = None
    release_id: Optional[str] = None
    distribution_mode: str = "PACKAGED"
    artifact_digest: Optional[str] = None
    agent_command: Optional[str] = None
    agent_command_args: list[str] = field(default_factory=list)
    # The researcher whose connection grant funds execution (study owner).
    funding_owner_user_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class AgentAssignmentResolution:
    """The frozen profile and immutable experiment arm for one task."""

    profile: FrozenAgentConfig
    study_id: Optional[uuid.UUID] = None
    assignment_id: Optional[uuid.UUID] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_enrollment(db: Session, participant_id: uuid.UUID):
    """Return the participant's active enrollment row, or ``None``.

    One active enrollment per account is enforced by a partial unique index, so
    there is at most one candidate.
    """
    for row in identity_store.list_enrollments(db, participant_id):
        if row.status == EnrollmentStatus.ACTIVE.value:
            return row
    return None


def _frozen_config_from_snapshot(
    profile_snapshot: dict, funding_owner_user_id: Optional[uuid.UUID]
) -> Optional[FrozenAgentConfig]:
    """Build runtime config only from the assignment's immutable snapshot."""
    profile_id = profile_snapshot.get("profile_id")
    if not profile_id:
        return None
    tools_json = profile_snapshot.get("tools_json", "[]")
    if not isinstance(tools_json, str):
        tools_json = json.dumps(tools_json, separators=(",", ":"))
    return FrozenAgentConfig(
        profile_id=uuid.UUID(str(profile_id)),
        name=profile_snapshot.get("name", "assigned-profile"),
        model=profile_snapshot.get("model", ""),
        framework_version=profile_snapshot.get("framework_version", "code4me2-agent"),
        tools_json=tools_json,
        approval_policy=profile_snapshot.get("approval_policy", "per_step"),
        max_steps=profile_snapshot.get("max_steps", 1),
        temperature=profile_snapshot.get("temperature"),
        max_context_tokens=profile_snapshot.get("max_context_tokens"),
        connection_id=(
            uuid.UUID(str(profile_snapshot["connection_id"]))
            if profile_snapshot.get("connection_id")
            else None
        ),
        release_id=profile_snapshot.get("release_id"),
        funding_owner_user_id=funding_owner_user_id,
    )


def resolve_assignment_context(
    db: Session, user_id: uuid.UUID
) -> Optional[AgentAssignmentResolution]:
    """Resolve the frozen profile/assignment for a new research task.

    Steps: participant mapping → active enrollment → open research study →
    sticky ``study_assignment`` → frozen profile snapshot. Any missing step is
    a refusal (``None``).
    """
    participant_row = identity_store.get_participant_by_account(db, user_id)
    if participant_row is None:
        logging.info(
            "[Agent/registry] account %s has no research participant mapping; "
            "refusing assignment",
            str(user_id)[:8],
        )
        return None

    enrollment_row = _active_enrollment(db, participant_row.participant_id)
    if enrollment_row is None:
        logging.info(
            "[Agent/registry] participant %s has no active enrollment; refusing "
            "assignment",
            str(participant_row.participant_id)[:8],
        )
        return None

    enrollment = identity_store.row_to_enrollment(enrollment_row)
    study = db.get(StudyRow, enrollment.study_id)
    now = _now()
    if not protocol_store.research_study_is_open(study, now):
        logging.info(
            "[Agent/registry] study %s is not open for execution; refusing assignment",
            enrollment.study_id,
        )
        return None

    assignment_row = assignment_store.get_assignment_for_enrollment(
        db, enrollment.enrollment_id
    )
    if assignment_row is None:
        from sqlalchemy import select
        from database.research_schemas import StudyAgentProfile

        profile_rows = db.execute(
            select(StudyAgentProfile)
            .where(StudyAgentProfile.study_id == enrollment.study_id)
            .order_by(StudyAgentProfile.selection_order.asc())
        ).scalars().all()
        profiles = [
            StudyProfileSelection(
                study_id=row.study_id,
                agent_profile_id=row.profile_id,
                profile_digest=row.profile_digest,
                profile_snapshot_json=row.profile_snapshot_json,
                selection_order=row.selection_order,
            )
            for row in profile_rows
        ]
        allocation = allocate(enrollment, profiles, existing=None, now=now)
        if (
            allocation.outcome
            not in (AllocationOutcome.CREATED, AllocationOutcome.EXISTING)
            or allocation.assignment is None
        ):
            logging.warning(
                "[Agent/registry] allocation refused: %s",
                getattr(allocation.issue, "code", allocation.outcome),
            )
            return None
        assignment = allocation.assignment
        if allocation.created:
            # create_assignment returns the winning row on a concurrent first-use
            # race, so this context adopts the authoritative sticky profile.
            winner_row = assignment_store.create_assignment(db, assignment)
            if winner_row is not None and winner_row.assignment_id != assignment.assignment_id:
                assignment = assignment_store.row_to_assignment(winner_row)

    if assignment_row is not None:
        assignment = assignment_store.row_to_assignment(assignment_row)
    profile = _frozen_config_from_snapshot(
        assignment.profile_snapshot_json, getattr(study, "created_by", None)
    )
    if profile is None:
        logging.error(
            "[Agent/registry] assignment has no frozen profile snapshot; refusing assignment",
        )
        return None

    return AgentAssignmentResolution(
        profile=profile,
        study_id=enrollment.study_id,
        assignment_id=assignment.assignment_id,
    )


def resolve_assignment(db: Session, user_id: uuid.UUID) -> Optional[FrozenAgentConfig]:
    """Return the frozen profile selected for a new task, or ``None``."""
    resolution = resolve_assignment_context(db, user_id)
    return resolution.profile if resolution is not None else None
