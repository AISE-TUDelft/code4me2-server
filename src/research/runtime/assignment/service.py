"""Pure server-authoritative allocation over enrollment-scoped units.

The unit of assignment is ``enrollment_id`` (never account, device, task, or
process). Assignments are always sticky: when one already exists for the same
``(enrollment_id, study_revision_id)`` it is returned unchanged and never
re-randomized, and a changed weight or a later revision never rebuckets an
existing enrollment. There is no runtime switch. Reallocation, if it is ever
required, must be defined by a successor revision rather than by mutating an
existing assignment. Ambiguous eligibility fails closed with a typed reason
rather than a default arm.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from research.canonical import canonical_hash
from research.participants.enums import EnrollmentStatus
from research.study.protocol.enums import AssignmentStrategy, RevisionStatus
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.validation import (
    ProtocolValidationError,
    normalized_condition_weights,
)

from .enums import AllocationOutcome, AssignmentReasonCode
from .models import AssignmentIssue, AssignmentResult, AssignmentV1

if TYPE_CHECKING:
    from research.participants.models import Enrollment
    from research.study.protocol.publication import StudyRevision



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


def _protocol(revision: StudyRevision) -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(revision.protocol_json)


def _weighted_condition(
    weights: dict[str, float], rng: random.Random
) -> str:
    draw = rng.random()
    cumulative = 0.0
    last = ""
    for condition_id in sorted(weights):
        last = condition_id
        cumulative += weights[condition_id]
        if draw < cumulative:
            return condition_id
    return last


def _deterministic_condition(
    weights: dict[str, float],
    *,
    enrollment_id: uuid.UUID,
    revision_id: uuid.UUID,
    randomization_epoch: int,
) -> str:
    seed = canonical_hash(
        {
            "enrollment_id": str(enrollment_id),
            "revision_id": str(revision_id),
            "randomization_epoch": randomization_epoch,
        }
    )
    draw = int(seed, 16) / float(1 << 256)
    cumulative = 0.0
    last = ""
    for condition_id in sorted(weights):
        last = condition_id
        cumulative += weights[condition_id]
        if draw < cumulative:
            return condition_id
    return last


def allocate(
    enrollment: Enrollment,
    revision: StudyRevision,
    existing: Optional[AssignmentV1] = None,
    rng: Optional[random.Random] = None,
    now: Optional[datetime] = None,
) -> AssignmentResult:
    """Allocate (or return) the sticky condition for one enrollment/revision."""
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

    if revision is None or revision.revision_id != enrollment.study_revision_id:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.REVISION_MISMATCH,
            "enrollment is not bound to the requested revision",
            "study_revision_id",
        )

    if revision.status != RevisionStatus.PUBLISHED:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.REVISION_NOT_PUBLISHED,
            f"revision is {revision.status.value}; only PUBLISHED revisions allocate",
            "status",
        )

    if existing is not None:
        if (
            existing.enrollment_id == enrollment.enrollment_id
            and existing.study_revision_id == revision.revision_id
        ):
            return AssignmentResult(
                outcome=AllocationOutcome.EXISTING,
                assignment=existing,
                created=False,
                reason=AssignmentReasonCode.STICKY_EXISTING,
            )
        return _blocked(
            AllocationOutcome.CONFLICT,
            AssignmentReasonCode.REVISION_MISMATCH,
            "an assignment already exists for a different enrollment/revision",
            "study_revision_id",
        )

    protocol = _protocol(revision)
    if not protocol.conditions:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.NO_CONDITIONS,
            "the revision declares no conditions",
            "conditions",
        )

    try:
        weights = normalized_condition_weights(protocol)
    except ProtocolValidationError:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.WEIGHTS_INVALID,
            "condition weights are not normalizable",
            "conditions",
        )

    strategy = protocol.assignment.strategy
    randomization_epoch = 0

    if strategy == AssignmentStrategy.WEIGHTED_RANDOM.value:
        condition_id = _weighted_condition(weights, rng or random.Random())
        reason = AssignmentReasonCode.WEIGHTED_DRAW
    elif strategy == AssignmentStrategy.DETERMINISTIC_HASH.value:
        condition_id = _deterministic_condition(
            weights,
            enrollment_id=enrollment.enrollment_id,
            revision_id=revision.revision_id,
            randomization_epoch=randomization_epoch,
        )
        reason = AssignmentReasonCode.DETERMINISTIC_HASH
    elif strategy == AssignmentStrategy.STRATIFIED.value:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.STRATIFIED_UNSUPPORTED,
            "stratified allocation requires stratum values that are not available",
            "assignment.strategy",
        )
    else:
        return _blocked(
            AllocationOutcome.INSUFFICIENT_EVIDENCE,
            AssignmentReasonCode.UNKNOWN_STRATEGY,
            f"unknown assignment strategy {strategy!r}",
            "assignment.strategy",
        )

    assignment = AssignmentV1(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision.revision_id,
        condition_id=condition_id,
        strategy=strategy,
        randomization_epoch=randomization_epoch,
        assigned_at=timestamp,
        protocol_digest=revision.protocol_digest,
    )
    return AssignmentResult(
        outcome=AllocationOutcome.CREATED,
        assignment=assignment,
        created=True,
        reason=reason,
    )
