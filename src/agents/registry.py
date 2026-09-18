"""Server-authoritative agent assignment for research tasks.

There is exactly one assignment authority: the research ``study_assignment`` row
allocated by :mod:`research.runtime.assignment`. Bootstrap and task creation both
resolve that same persisted row, so a participant always gets one sticky
condition per enrollment/revision.

A research run requires a real participant mapping, an **active** enrollment, an
open research study window, and a published revision carrying a frozen condition
config. When any of those is missing this returns ``None`` — there is no fallback
to the first active profile, the first arm, a mutable ``AgentProfile`` row, or
another researcher's configuration.
"""

from __future__ import annotations

import logging
import random
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
from research.runtime.assignment.service import allocate
from research.study.protocol import store as protocol_store
from research.study.protocol.models import ResolvedAgentConfig, ResolvedDistribution, StudyProtocolV1


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
    revision_id: Optional[uuid.UUID] = None
    arm_name: Optional[str] = None
    is_baseline: Optional[bool] = None


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


def _frozen_config(
    resolved: ResolvedDistribution, funding_owner_user_id: Optional[uuid.UUID]
) -> Optional[FrozenAgentConfig]:
    """Build the immutable execution config from a revision's frozen pin.

    A revision without a frozen ``agent_config`` (one published before config
    freezing) has no immutable config, so there is nothing to execute: this
    returns ``None`` rather than reading a mutable profile row.
    """
    config: Optional[ResolvedAgentConfig] = resolved.agent_config
    if config is None:
        return None
    return FrozenAgentConfig(
        profile_id=config.profile_id,
        name=config.name,
        model=config.model,
        framework_version=config.framework_version,
        tools_json=config.tools_json,
        approval_policy=config.approval_policy,
        max_steps=config.max_steps,
        temperature=config.temperature,
        max_context_tokens=config.max_context_tokens,
        connection_id=config.connection_id,
        connection_label=config.connection_label,
        release_id=resolved.release_id,
        distribution_mode=resolved.distribution_mode,
        artifact_digest=resolved.artifact_digest,
        agent_command=resolved.agent_command,
        agent_command_args=list(resolved.agent_command_args),
        funding_owner_user_id=(
            config.funding_owner_user_id or funding_owner_user_id
        ),
    )


def resolve_assignment_context(
    db: Session, user_id: uuid.UUID
) -> Optional[AgentAssignmentResolution]:
    """Resolve the frozen profile/assignment for a new research task.

    Steps: participant mapping → active enrollment → open research study →
    published revision → sticky ``study_assignment`` (allocated once, conflict
    safe) → frozen condition config. Any missing step is a refusal (``None``).
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

    revision_row = protocol_store.get_revision(db, enrollment.study_revision_id)
    if revision_row is None:
        logging.error("[Agent/registry] enrollment revision is missing")
        return None
    revision = protocol_store.row_to_revision(revision_row)

    assignment_row = assignment_store.get_assignment_for_enrollment_revision(
        db, enrollment.enrollment_id, revision.revision_id
    )
    if assignment_row is not None:
        assignment = assignment_store.row_to_assignment(assignment_row)
    else:
        allocation = allocate(
            enrollment, revision, existing=None, rng=random.Random(), now=now
        )
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
            # race, so this context adopts the authoritative sticky condition.
            winner_row = assignment_store.create_assignment(db, assignment)
            if winner_row is not None and winner_row.assignment_id != assignment.assignment_id:
                assignment = assignment_store.row_to_assignment(winner_row)

    protocol = StudyProtocolV1.model_validate(revision.protocol_json)
    condition = next(
        (
            candidate
            for candidate in protocol.conditions
            if candidate.condition_id == assignment.condition_id
        ),
        None,
    )
    if condition is None:
        logging.error(
            "[Agent/registry] assignment condition %r is not in the revision",
            assignment.condition_id,
        )
        return None

    resolved = getattr(condition, "resolved_distribution", None)
    if resolved is None:
        logging.error(
            "[Agent/registry] condition %r carries no frozen distribution",
            condition.condition_id,
        )
        return None
    profile = _frozen_config(resolved, getattr(study, "created_by", None))
    if profile is None:
        logging.error(
            "[Agent/registry] revision has no frozen agent config for condition %r; "
            "refusing assignment",
            condition.condition_id,
        )
        return None

    return AgentAssignmentResolution(
        profile=profile,
        study_id=enrollment.study_id,
        assignment_id=assignment.assignment_id,
        revision_id=assignment.study_revision_id,
        arm_name=assignment.condition_id,
        is_baseline=getattr(condition, "is_baseline", None),
    )


def resolve_assignment(db: Session, user_id: uuid.UUID) -> Optional[FrozenAgentConfig]:
    """Return the frozen profile selected for a new task, or ``None``."""
    resolution = resolve_assignment_context(db, user_id)
    return resolution.profile if resolution is not None else None
