"""Pure server-authoritative allocation over enrollment-scoped units.

The unit of assignment is ``enrollment_id`` (never account, device, task, or
process). Assignments are always sticky: a repeated bootstrap returns the
existing profile and never re-randomizes it. Profiles are selected by the study
at creation time and carry a digest-pinned, non-secret snapshot.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from research.participants.enums import EnrollmentStatus

from .enums import AllocationOutcome, AssignmentReasonCode
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
) -> AssignmentResult:
    """Allocate (or return) one sticky profile with equal probability."""
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

    strategy = "RANDOM_EQUAL"
    randomization_epoch = 0
    selected = (rng or random.SystemRandom()).choice(normalized_profiles)

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
        reason=AssignmentReasonCode.RANDOM_EQUAL,
    )
