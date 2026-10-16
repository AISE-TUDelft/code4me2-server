"""Focused contracts for the revision-free study lifecycle."""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from research.analysis.operations.enums import KillSwitchScopeKind
from research.analysis.operations.kill_switch import KillSwitchRegistry
from research.analysis.operations.models import KillSwitchScope
from research.participants.enums import EnrollmentStatus, RetentionAction
from research.participants.models import Enrollment, ResearchEligibility
from research.runtime.assignment.enums import AllocationOutcome, AssignmentReasonCode
from research.runtime.assignment.models import AssignmentV1, StudyProfileSelection
from research.runtime.assignment.service import allocate
from research.runtime.bootstrap.capability import issue_capability, verify_capability
from research.runtime.sessions.service import open_session
from research.telemetry.models import CanonicalEventV1

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def enrollment(study_id: uuid.UUID) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
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


def profile(study_id: uuid.UUID, name: str) -> StudyProfileSelection:
    profile_id = uuid.uuid4()
    return StudyProfileSelection(
        study_id=study_id,
        agent_profile_id=profile_id,
        profile_digest=f"digest-{name}",
        profile_snapshot_json={"profile_id": str(profile_id), "name": name, "model": "test"},
    )


def test_equal_random_assignment_is_sticky_and_profile_scoped():
    study_id = uuid.uuid4()
    subject = enrollment(study_id)
    profiles = [profile(study_id, "control"), profile(study_id, "treatment")]

    result = allocate(subject, profiles, rng=random.Random(2), now=NOW)

    assert result.outcome == AllocationOutcome.CREATED
    assert result.reason == AssignmentReasonCode.RANDOM_EQUAL
    assert result.assignment is not None
    assert result.assignment.study_id == study_id
    assert result.assignment.agent_profile_id in {item.agent_profile_id for item in profiles}

    replay = allocate(subject, profiles, existing=result.assignment, now=NOW)
    assert replay.outcome == AllocationOutcome.EXISTING
    assert replay.assignment == result.assignment


def test_equal_random_assignment_is_balanced_over_a_synthetic_sample():
    study_id = uuid.uuid4()
    profiles = [profile(study_id, name) for name in ("one", "two", "three")]
    rng = random.Random(42)
    counts = Counter()

    for _ in range(300):
        result = allocate(enrollment(study_id), profiles, rng=rng, now=NOW)
        assert result.assignment is not None
        counts[result.assignment.agent_profile_id] += 1

    assert set(counts) == {item.agent_profile_id for item in profiles}
    assert all(75 <= count <= 125 for count in counts.values())


def test_allocation_fails_closed_without_selected_profiles():
    result = allocate(enrollment(uuid.uuid4()), [], now=NOW)
    assert result.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert result.reason == AssignmentReasonCode.NO_PROFILES


def test_capability_is_bound_to_study_not_revision():
    study_id = uuid.uuid4()
    capability = issue_capability(
        audience="research-runtime",
        scope=["telemetry:write"],
        ttl_seconds=60,
        revocation_epoch=0,
        secret="test-secret",
        now=NOW,
        enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(),
        study_id=study_id,
    )

    assert verify_capability(
        capability,
        "test-secret",
        "research-runtime",
        ["telemetry:write"],
        now=NOW,
        expected_study_id=study_id,
    ).ok
    mismatch = verify_capability(
        capability,
        "test-secret",
        "research-runtime",
        ["telemetry:write"],
        now=NOW,
        expected_study_id=uuid.uuid4(),
    )
    assert mismatch.ok is False


def test_stop_epoch_revokes_an_already_issued_capability():
    study_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()
    session_id = uuid.uuid4()
    capability = issue_capability(
        audience="research-runtime",
        scope=["session:heartbeat"],
        ttl_seconds=60,
        revocation_epoch=0,
        secret="test-secret",
        now=NOW,
        enrollment_id=enrollment_id,
        research_session_id=session_id,
        study_id=study_id,
    )

    verification = verify_capability(
        capability,
        "test-secret",
        "research-runtime",
        ["session:heartbeat"],
        now=NOW,
        current_revocation_epoch=1,
        expected_enrollment_id=enrollment_id,
        expected_research_session_id=session_id,
        expected_study_id=study_id,
    )

    assert verification.ok is False
    assert verification.reason.value == "REVOKED"


def test_session_and_telemetry_contracts_have_no_revision_field():
    study_id = uuid.uuid4()
    session = open_session(
        enrollment(study_id),
        SimpleNamespace(study_id=study_id),
        manifest_digest="digest",
        now=NOW,
    )
    assert session.study_id == study_id
    with pytest.raises(ValidationError):
        CanonicalEventV1.model_validate({"revision_id": str(uuid.uuid4())})


def test_kill_switch_has_study_and_enrollment_scopes_only():
    assert not hasattr(KillSwitchScopeKind, "REVISION")
    study_id = uuid.uuid4()
    registry = KillSwitchRegistry()
    registry.engage(KillSwitchScope(kind=KillSwitchScopeKind.STUDY, scope_id=study_id), "stop", now=NOW)
    assert registry.is_engaged(study_id=study_id, now=NOW)
