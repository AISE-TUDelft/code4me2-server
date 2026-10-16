"""Pure server-authoritative allocation over enrollment-scoped units.

The unit of assignment is ``enrollment_id`` (never account, device, task, or
process). Assignments are always sticky: a repeated bootstrap returns the
existing profile and never re-randomizes it. Profiles are selected by the study
at creation time and carry a digest-pinned, non-secret snapshot. The draw follows
the study's frozen assignment policy (see :mod:`.hashing`).
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from research.participants.enums import EnrollmentStatus
from research.study.protocol.enums import AssignmentStrategy

from .enums import AllocationOutcome, AssignmentReasonCode
from .hashing import hashed_profile
from .models import AssignmentIssue, AssignmentResult, AssignmentV1, StudyProfileSelection

if TYPE_CHECKING:
    from research.participants.models import Enrollment



def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _issue(code: AssignmentReasonCode, message: str, field: str = "") -> AssignmentIssue:
    return AssignmentIssue(code=code, message=message, field=field)


def _blocked(
    outcome: AllocationOutcome, code: AssignmentReasonCode, message: str, field: str = ""
) -> AssignmentResult:
    return AssignmentResult(
        outcome=outcome, reason=code, issue=_issue(code, message, field)
    )


def allocate(
    enrollment: Enrollment,
    profiles: Sequence[StudyProfileSelection],
    existing: Optional[AssignmentV1] = None,
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
    strategy: str = AssignmentStrategy.RANDOM_EQUAL.value,
) -> AssignmentResult:
    """Allocate (or return) one sticky profile with equal probability.

    ``strategy`` is the study's frozen policy (``hashing.assignment_strategy``):
    ``RANDOM_EQUAL`` draws from the system CSPRNG, ``DETERMINISTIC_HASH`` takes
    the salted-hash arm; anything else is refused.
    """
    timestamp = _now(now)

    if enrollment is None:
        return _blocked(
            AllocationOutcome.INELIGIBLE,
            AssignmentReasonCode.NO_ENROLLMENT,
            "an enrollment is required for allocation",
            "enrollment_id",
        )

    if enrollment.status != EnrollmentStatus.ACTIVE:
        return _blocked(
            AllocationOutcome.INELIGIBLE,
            AssignmentReasonCode.ENROLLMENT_NOT_ACTIVE,
            f"enrollment is {enrollment.status.value}",
            "status",
        )

    if profiles is None:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.NO_PROFILES,
            "study profiles are required for allocation",
            "profiles",
        )

    if existing is not None:
        if (
            existing.enrollment_id == enrollment.enrollment_id
            and existing.study_id == enrollment.study_id
        ):
            return AssignmentResult(
                outcome=AllocationOutcome.EXISTING,
                assignment=existing,
                created=False,
                reason=AssignmentReasonCode.STICKY_EXISTING,
            )
        return _blocked(
            AllocationOutcome.CONFLICT,
            AssignmentReasonCode.STUDY_MISMATCH,
            "an assignment already exists for a different enrollment/study",
            "study_id",
        )

    normalized_profiles = [StudyProfileSelection.model_validate(profile) for profile in profiles]
    if not normalized_profiles:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.NO_PROFILES,
            "the study declares no agent profiles",
            "profiles",
        )
    if any(profile.study_id != enrollment.study_id for profile in normalized_profiles):
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.STUDY_MISMATCH,
            "all profiles must belong to the enrollment study",
            "study_id",
        )

    randomization_epoch = 0
    if strategy == AssignmentStrategy.DETERMINISTIC_HASH.value:
        selected = hashed_profile(
            normalized_profiles,
            study_id=enrollment.study_id,
            enrollment_id=enrollment.enrollment_id,
            randomization_epoch=randomization_epoch,
        )
        reason = AssignmentReasonCode.DETERMINISTIC_HASH
    elif strategy == AssignmentStrategy.RANDOM_EQUAL.value:
        selected = (rng or random.SystemRandom()).choice(normalized_profiles)
        reason = AssignmentReasonCode.RANDOM_EQUAL
    else:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.UNKNOWN_STRATEGY,
            f"unknown assignment strategy {strategy!r}",
            "strategy",
        )

    assignment = AssignmentV1(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_id=enrollment.study_id,
        agent_profile_id=selected.agent_profile_id,
        strategy=strategy,
        randomization_epoch=randomization_epoch,
        assigned_at=timestamp,
        profile_digest=selected.profile_digest,
        profile_snapshot_json=selected.profile_snapshot_json,
    )
    return AssignmentResult(
        outcome=AllocationOutcome.CREATED,
        assignment=assignment,
        created=True,
        reason=reason,
    )
