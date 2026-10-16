"""Salted-hash arm assignment (the protocol formula reported in the paper)."""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import datetime, timezone

import pytest

from research.participants.enums import EnrollmentStatus, RetentionAction
from research.participants.models import Enrollment, ResearchEligibility
from research.runtime.assignment.enums import AllocationOutcome, AssignmentReasonCode
from research.runtime.assignment.hashing import (
    assignment_strategy,
    hash_arm_index,
    hashed_profile,
    manual_override_enabled,
    new_study_assignment_policy,
)
from research.runtime.assignment.models import StudyProfileSelection
from research.runtime.assignment.service import allocate

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
STUDY = uuid.UUID(int=1)
ENROLLMENT = uuid.UUID(int=2)

#: chi-square critical values at p = 0.001 for K - 1 degrees of freedom.
CHI2_CRITICAL_P001 = {1: 10.828, 2: 13.816, 3: 16.266}


def enrollment(study_id: uuid.UUID, enrollment_id: uuid.UUID | None = None) -> Enrollment:
    return Enrollment(
        enrollment_id=enrollment_id or uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=study_id,
        participant_code="p_test",
        status=EnrollmentStatus.ACTIVE,
        eligibility=ResearchEligibility(eligible=True),
        enrolled_at=NOW,
        consent_accepted_at=NOW,
        updated_at=NOW,
        retention_action=RetentionAction.RETAIN_ANONYMIZED,
    )


def profile(study_id: uuid.UUID, name: str, order: int) -> StudyProfileSelection:
    profile_id = uuid.uuid4()
    return StudyProfileSelection(
        study_id=study_id,
        agent_profile_id=profile_id,
        profile_digest=f"digest-{name}",
        profile_snapshot_json={"profile_id": str(profile_id), "name": name, "model": "test"},
        selection_order=order,
    )


def seeded_uuids(seed: int, count: int) -> list[uuid.UUID]:
    rng = random.Random(seed)
    return [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(count)]


def test_golden_vector_matches_the_documented_formula():
    # SHA-256("00000000-0000-0000-0000-000000000001:0:00000000-0000-0000-0000-000000000002")
    # starts with 39673deb8a861ef7 (checked with `shasum -a 256`).
    h = 0x39673DEB8A861EF7
    for arm_count, expected in ((2, 0), (3, 0), (5, 1)):
        assert (arm_count * h) >> 64 == expected
        assert hash_arm_index(STUDY, 0, ENROLLMENT, arm_count) == expected


def test_index_is_deterministic_in_range_and_accepts_uuid_strings():
    for enrollment_id in seeded_uuids(7, 200):
        index = hash_arm_index(STUDY, 0, enrollment_id, 3)
        assert 0 <= index < 3
        assert hash_arm_index(str(STUDY).upper(), 0, str(enrollment_id), 3) == index
    assert hash_arm_index(STUDY, 0, ENROLLMENT, 1) == 0


def test_study_and_epoch_salt_the_draw():
    ids = seeded_uuids(11, 400)
    base = [hash_arm_index(STUDY, 0, item, 2) for item in ids]
    other_study = [hash_arm_index(uuid.UUID(int=3), 0, item, 2) for item in ids]
    other_epoch = [hash_arm_index(STUDY, 1, item, 2) for item in ids]
    assert base != other_study
    assert base != other_epoch


def test_arm_count_must_be_positive():
    with pytest.raises(ValueError):
        hash_arm_index(STUDY, 0, ENROLLMENT, 0)


@pytest.mark.parametrize("arm_count", [2, 3, 4])
def test_assignments_are_uniform_over_a_large_synthetic_sample(arm_count):
    sample = seeded_uuids(2026 + arm_count, 30_000)
    counts = Counter(hash_arm_index(STUDY, 0, item, arm_count) for item in sample)
    expected = len(sample) / arm_count
    statistic = sum((counts[arm] - expected) ** 2 / expected for arm in range(arm_count))
    assert statistic < CHI2_CRITICAL_P001[arm_count - 1]


def test_hashed_profile_orders_arms_by_selection_order():
    profiles = [profile(STUDY, name, order) for order, name in enumerate(("a", "b", "c", "d", "e"))]
    shuffled = [profiles[3], profiles[0], profiles[4], profiles[1], profiles[2]]
    assert hashed_profile(shuffled, study_id=STUDY, enrollment_id=ENROLLMENT) is profiles[1]


def test_allocate_with_hash_strategy_is_deterministic_and_labelled():
    profiles = [profile(STUDY, name, order) for order, name in enumerate(("control", "treatment"))]
    subject = enrollment(STUDY, ENROLLMENT)

    first = allocate(subject, profiles, now=NOW, strategy="DETERMINISTIC_HASH")
    second = allocate(subject, profiles, rng=random.Random(99), now=NOW, strategy="DETERMINISTIC_HASH")

    assert first.outcome == AllocationOutcome.CREATED
    assert first.reason == AssignmentReasonCode.DETERMINISTIC_HASH
    assert first.assignment is not None and second.assignment is not None
    assert first.assignment.strategy == "DETERMINISTIC_HASH"
    assert first.assignment.randomization_epoch == 0
    # The golden vector picks arm 0 for two arms.
    assert first.assignment.agent_profile_id == profiles[0].agent_profile_id
    assert second.assignment.agent_profile_id == first.assignment.agent_profile_id


def test_allocate_defaults_to_random_equal_and_refuses_unknown_strategies():
    profiles = [profile(STUDY, name, order) for order, name in enumerate(("control", "treatment"))]

    default = allocate(enrollment(STUDY), profiles, rng=random.Random(3), now=NOW)
    assert default.reason == AssignmentReasonCode.RANDOM_EQUAL
    assert default.assignment is not None and default.assignment.strategy == "RANDOM_EQUAL"

    refused = allocate(enrollment(STUDY), profiles, now=NOW, strategy="STRATIFIED")
    assert refused.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert refused.reason == AssignmentReasonCode.UNKNOWN_STRATEGY
    assert refused.assignment is None


def test_policy_helpers_treat_legacy_configs_as_random_equal():
    assert assignment_strategy({}) == "RANDOM_EQUAL"
    assert assignment_strategy(None) == "RANDOM_EQUAL"
    assert assignment_strategy({"assignment": "bogus"}) == "RANDOM_EQUAL"
    assert manual_override_enabled({}) is False

    policy = new_study_assignment_policy(manual_override=True)
    assert policy == {"strategy": "DETERMINISTIC_HASH", "manual_override": True}
    assert assignment_strategy({"assignment": policy}) == "DETERMINISTIC_HASH"
    assert manual_override_enabled({"assignment": policy}) is True
    assert manual_override_enabled({"assignment": {"manual_override": "yes"}}) is False
