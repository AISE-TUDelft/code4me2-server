"""Consolidated research tests (see individual section headers).

Merged from smaller modules; test functions and assertions are unchanged.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.studies import (
    DraftCreateRequest,
    PublishRequest,
    RetireRequest,
    StudyCreateRequest,
    ValidateRequest,
    _distribution_errors,
    create_draft,
    get_revision,
    list_drafts,
    list_revisions,
    publish_draft,
    retire_published_revision,
    supersede_revision,
    validate_draft,
)
from backend.routers.research.studies import (
    create_study as create_study_endpoint,
)
from database import research_schemas
from research.analysis.read_models.enums import ResearcherRole
from research.participants.enums import EnrollmentStatus
from research.study.agents.distributions import resolve_distribution_view
from research.study.agents.enums import (
    DistributionMode,
    QualificationStatus,
    RegistryReasonCode,
)
from research.study.agents.models import (
    AdapterRef,
    AgentReleaseV1,
    DistributionArtifact,
)
from research.study.agents.registry import AgentRegistry
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.protocol import store as store_module
from research.study.protocol.canonical import protocol_canonical_json, protocol_digest
from research.study.protocol.enums import (
    AssignmentStrategy,
    AssignmentUnit,
    PublicationOutcome,
    ReleaseResolutionStatus,
    RevisionStatus,
    ValidationReasonCode,
    ValidationSeverity,
)
from research.study.protocol.models import ExplicitUnknown, StudyProtocolV1
from research.study.protocol.publication import (
    RevisionLineage,
    lineage_from_revisions,
    publish_revision,
    retire_revision,
)
from research.study.protocol.validation import (
    DistributionResolution,
    NullReleaseResolver,
    ReleaseResolution,
    is_publishable,
    normalized_condition_weights,
    validate_protocol,
    warnings,
)

# --------------------------------------------------------------------------
# test_protocol_publication
# --------------------------------------------------------------------------
# Tests for the versioned study protocol and immutable publication (Issue 02).
#
# Route handlers are exercised by calling the functions directly with a
# ``MagicMock`` app/session. No ``TestClient`` is used because this environment
# has no PostgreSQL/Redis; canonicalization, validation and publication are pure
# and fully unit-testable.
protocol_publication__FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
)
protocol_publication__FIXTURE_NAME = "approved_study_protocol_v1.json"

# Recorded digest of the approved fixture. Changing the fixture's policy,
# ordering normalization or the canonical serializer MUST update this constant
# deliberately, never incidentally.
protocol_publication__APPROVED_PROTOCOL_DIGEST = (
    "a0e2ec1dca1d5fd3eae49fe60b7db32d286bfcf33d63d88fc8ac695c7a1e0cf6"
)

protocol_publication__NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def protocol_publication___fixture_data() -> dict:
    return json.loads((protocol_publication__FIXTURE_DIR / protocol_publication__FIXTURE_NAME).read_text())


def protocol_publication___protocol(**edits) -> StudyProtocolV1:
    data = protocol_publication___fixture_data()
    for key, value in edits.items():
        data[key] = value
    return StudyProtocolV1.model_validate(data)


def protocol_publication___codes(errors) -> set[ValidationReasonCode]:
    return {error.code for error in errors}


def protocol_publication___has(errors, code: ValidationReasonCode) -> bool:
    return code in protocol_publication___codes(errors)


def protocol_publication___admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def protocol_publication___non_admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=False, email="user@example.com", name="User"
    )


class protocol_publication___FakeResolver:
    """Resolver double returning a fixed status for every release."""

    def __init__(self, status: ReleaseResolutionStatus, digest: str | None = None):
        self.status = status
        self.digest = digest
        self.calls: list[tuple] = []

    def resolve(self, agent_id, *, release_id=None, version=None) -> ReleaseResolution:
        self.calls.append((agent_id, release_id, version))
        return ReleaseResolution(
            status=self.status,
            agent_id=agent_id,
            release_id=release_id,
            version=version,
            artifact_digest=self.digest,
        )


class protocol_publication___FakeDistributionResolver:
    """Distribution-resolver double returning one fixed view for every id.

    The default is a verified PACKAGED distribution so an unresolved draft can
    be exercised against the approved fixture without a database.
    """

    def __init__(
        self,
        *,
        found: bool = True,
        distribution_mode: str = "PACKAGED",
        release_id: str | None = "rel-0001",
        agent_id: str | None = "synthetic-agent",
        version: str | None = "1.0.0",
        artifact_digest: str | None = None,
        agent_package: str | None = None,
        agent_command: str | None = None,
        verified: bool = True,
        release_status: ReleaseResolutionStatus = ReleaseResolutionStatus.RESOLVED,
    ) -> None:
        self.kwargs = dict(
            found=found,
            distribution_mode=distribution_mode,
            release_id=release_id,
            agent_id=agent_id,
            version=version,
            artifact_digest=artifact_digest,
            agent_package=agent_package,
            agent_command=agent_command,
            verified=verified,
            release_status=release_status,
        )
        self.calls: list = []

    def resolve(self, distribution_id) -> DistributionResolution:
        self.calls.append(distribution_id)
        return DistributionResolution(distribution_id=distribution_id, **self.kwargs)


class protocol_publication___FixtureDistributionResolver:
    """Verified distribution resolver mirroring the approved fixture's pins.

    It is installed in place of the DB-backed resolver so route-level tests can
    run without PostgreSQL while still exercising the freeze/validate path.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self._by_id = {}
        for condition in protocol_publication___fixture_data()["conditions"]:
            frozen = condition.get("resolved_distribution") or {}
            self._by_id[uuid.UUID(condition["distribution_id"])] = frozen

    def resolve(self, distribution_id) -> DistributionResolution:
        frozen = self._by_id.get(distribution_id, {})
        return DistributionResolution(
            found=True,
            distribution_id=distribution_id,
            distribution_mode=frozen.get("distribution_mode", "PACKAGED"),
            release_id=frozen.get("release_id") or "rel-0001",
            agent_id=frozen.get("agent_id"),
            version=frozen.get("version"),
            artifact_digest=frozen.get("artifact_digest"),
            verified=frozen.get("verified", True),
            release_status=ReleaseResolutionStatus.RESOLVED,
        )


@pytest.fixture()
def protocol_publication__verified_distributions(monkeypatch):
    """Route tests: replace the DB distribution resolver with a fixture mirror."""
    monkeypatch.setattr(
        "backend.routers.research.studies._DbDistributionResolver",
        protocol_publication___FixtureDistributionResolver,
    )


def protocol_publication___revision_row(revision) -> SimpleNamespace:
    return SimpleNamespace(
        revision_id=revision.revision_id,
        study_id=revision.study_id,
        revision_number=revision.revision_number,
        status=revision.status.value,
        protocol_json=revision.protocol_json,
        protocol_digest=revision.protocol_digest,
        published_at=revision.published_at,
        supersedes_revision_id=revision.supersedes_revision_id,
        created_at=revision.created_at,
    )


def protocol_publication___published(protocol: StudyProtocolV1, lineage: RevisionLineage | None = None):
    lineage = lineage or RevisionLineage(study_id=protocol.study_id)
    result = publish_revision(protocol, lineage, now=protocol_publication__NOW)
    assert result.outcome == PublicationOutcome.PUBLISHED
    assert result.revision is not None
    return result.revision


def protocol_publication___lineage_for(revision, status: RevisionStatus = RevisionStatus.PUBLISHED):
    return RevisionLineage(
        study_id=revision.study_id,
        latest_revision_id=revision.revision_id,
        latest_revision_number=revision.revision_number,
        latest_digest=revision.protocol_digest,
        latest_status=status,
    )


# ---------------------------------------------------------------------------
# Canonical serialization and digest
# ---------------------------------------------------------------------------


def test_approved_fixture_digest_matches_recorded_constant():
    protocol = StudyProtocolV1.model_validate(protocol_publication___fixture_data())
    assert protocol_digest(protocol) == protocol_publication__APPROVED_PROTOCOL_DIGEST


def test_canonical_digest_is_order_independent():
    first = protocol_publication___protocol()
    data = protocol_publication___fixture_data()
    data["conditions"] = list(reversed(data["conditions"]))
    data["telemetry_policy"]["allowed_field_classes"] = list(
        reversed(data["telemetry_policy"]["allowed_field_classes"])
    )
    data["environment_requirements"]["required_capabilities"] = list(
        reversed(data["environment_requirements"]["required_capabilities"])
    )
    second = StudyProtocolV1.model_validate(data)

    assert protocol_digest(first) == protocol_digest(second)
    assert protocol_canonical_json(first) == protocol_canonical_json(second)


def test_canonical_digest_ignores_nonsemantic_whitespace():
    data = protocol_publication___fixture_data()
    compact = json.loads(json.dumps(data, separators=(",", ":")))
    indented = json.loads(json.dumps(data, indent=4))

    assert protocol_digest(StudyProtocolV1.model_validate(compact)) == protocol_digest(
        StudyProtocolV1.model_validate(indented)
    )


def test_canonical_digest_changes_on_any_policy_edit():
    baseline = protocol_publication___protocol()
    original = protocol_digest(baseline)

    edits = []
    retention = protocol_publication___fixture_data()
    retention["privacy_policy"]["retention_days"] = 30
    edits.append(retention)

    weight = protocol_publication___fixture_data()
    weight["conditions"][0]["weight"] = 1.5
    edits.append(weight)

    release = protocol_publication___fixture_data()
    release["conditions"][1]["resolved_distribution"]["release_id"] = "rel-0003"
    edits.append(release)

    capability = protocol_publication___fixture_data()
    capability["environment_requirements"]["required_capabilities"].append(
        {"capability": "DIFF", "require_state": "SUPPORTED", "evidence_ref": None}
    )
    edits.append(capability)


    completion = protocol_publication___fixture_data()
    completion["completion"]["target_enrollments"] = 250
    edits.append(completion)

    for edited in edits:
        assert protocol_digest(StudyProtocolV1.model_validate(edited)) != original


def test_canonical_digest_distinguishes_null_from_explicit_unknown():
    null_data = protocol_publication___fixture_data()
    null_data["enrollment"]["capacity"] = None
    unknown_data = protocol_publication___fixture_data()
    unknown_data["enrollment"]["capacity"] = {"kind": "UNKNOWN"}

    assert protocol_digest(
        StudyProtocolV1.model_validate(null_data)
    ) != protocol_digest(StudyProtocolV1.model_validate(unknown_data))


# ---------------------------------------------------------------------------
# Validation: structural, schedule, assignment, consent, environment
# ---------------------------------------------------------------------------


def test_validation_accepts_approved_fixture():
    assert validate_protocol(protocol_publication___protocol()) == []


def test_validation_rejects_unknown_schema_version():
    errors = validate_protocol(protocol_publication___protocol(schema_version="2"))
    assert protocol_publication___has(errors, ValidationReasonCode.UNSUPPORTED_SCHEMA_VERSION)


def test_validation_rejects_duplicate_condition_ids():
    data = protocol_publication___fixture_data()
    data["conditions"][1]["condition_id"] = "control"
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.DUPLICATE_CONDITION_ID)


def test_validation_rejects_delete_all_retention_as_unsupported_authoring():
    data = protocol_publication___fixture_data()
    data["privacy_policy"]["retention_action"] = "DELETE_ALL"
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.RETENTION_UNSUPPORTED)
    assert "privacy_policy.retention_action" in {error.field for error in errors}


def test_validation_accepts_retain_anonymized_and_delete_identifiable():
    for action in ("RETAIN_ANONYMIZED", "DELETE_IDENTIFIABLE"):
        data = protocol_publication___fixture_data()
        data["privacy_policy"]["retention_action"] = action
        errors = validate_protocol(data)
        assert not protocol_publication___has(
            errors, ValidationReasonCode.RETENTION_UNSUPPORTED
        ), action


def test_validation_rejects_non_positive_weights():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["weight"] = 0
    assert protocol_publication___has(validate_protocol(data), ValidationReasonCode.NON_POSITIVE_WEIGHT)

    data = protocol_publication___fixture_data()
    data["conditions"][0]["weight"] = -2
    assert protocol_publication___has(validate_protocol(data), ValidationReasonCode.NON_POSITIVE_WEIGHT)


def test_validation_rejects_empty_condition_set():
    errors = validate_protocol(protocol_publication___protocol(conditions=[]))
    assert protocol_publication___has(errors, ValidationReasonCode.NO_CONDITIONS)


def test_validation_rejects_expired_fixed_schedule():
    expired = {
        "kind": "FIXED",
        "start_at": "2020-01-01T00:00:00Z",
        "end_at": "2020-02-01T00:00:00Z",
    }
    errors = validate_protocol(protocol_publication___protocol(schedule=expired), now=protocol_publication__NOW)
    assert protocol_publication___has(errors, ValidationReasonCode.FIXED_SCHEDULE_EXPIRED)


def test_validation_accepts_active_fixed_schedule():
    active = {
        "kind": "FIXED",
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-12-01T00:00:00Z",
    }
    assert validate_protocol(protocol_publication___protocol(schedule=active), now=protocol_publication__NOW) == []


def test_validation_rejects_zero_rolling_duration():
    data = protocol_publication___fixture_data()
    data["schedule"] = {"kind": "ROLLING", "duration_seconds": 0}
    assert protocol_publication___has(
        validate_protocol(StudyProtocolV1.model_validate(data)),
        ValidationReasonCode.ROLLING_DURATION_INVALID,
    )

    data = protocol_publication___fixture_data()
    data["schedule"] = {"kind": "ROLLING", "duration_seconds": None}
    assert protocol_publication___has(
        validate_protocol(StudyProtocolV1.model_validate(data)),
        ValidationReasonCode.ROLLING_DURATION_INVALID,
    )


def test_validation_rejects_non_enrollment_assignment_unit():
    data = protocol_publication___fixture_data()
    data["assignment"]["unit"] = AssignmentUnit.SESSION.value
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.ASSIGNMENT_UNIT_NOT_ENROLLMENT)


def test_validation_rejects_unknown_assignment_strategy():
    data = protocol_publication___fixture_data()
    data["assignment"]["strategy"] = "MAGIC"
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.ASSIGNMENT_STRATEGY_UNKNOWN)


def test_validation_rejects_stratified_without_strata():
    data = protocol_publication___fixture_data()
    data["assignment"]["strategy"] = AssignmentStrategy.STRATIFIED.value
    data["assignment"]["stratification"] = None
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.ASSIGNMENT_STRATIFICATION_MISSING)




def test_validation_rejects_missing_expected_protocol_version():
    data = protocol_publication___fixture_data()
    data["environment_requirements"]["expected_protocol_version"] = None
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.ENVIRONMENT_PROTOCOL_VERSION_MISSING)


def test_validation_flags_unknown_required_capability_as_needs_review():
    data = protocol_publication___fixture_data()
    data["environment_requirements"]["required_capabilities"].append(
        {"capability": "TELEPORT", "require_state": "SUPPORTED"}
    )
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.UNKNOWN_REQUIRED_CAPABILITY)
    unknown = next(
        error
        for error in errors
        if error.code == ValidationReasonCode.UNKNOWN_REQUIRED_CAPABILITY
    )
    assert unknown.severity == ValidationSeverity.NEEDS_REVIEW


def test_validation_flags_explicit_unknown_environment_requirement():
    data = protocol_publication___fixture_data()
    data["environment_requirements"]["host_kind"] = {"kind": "UNKNOWN"}
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.UNKNOWN_ENVIRONMENT_REQUIREMENT)


def test_normalized_condition_weights_are_deterministic():
    first = protocol_publication___protocol()
    assert normalized_condition_weights(first) == {"control": 0.25, "treatment": 0.75}

    scaled_data = protocol_publication___fixture_data()
    scaled_data["conditions"][0]["weight"] = 2.0
    scaled_data["conditions"][1]["weight"] = 6.0
    scaled = StudyProtocolV1.model_validate(scaled_data)
    assert normalized_condition_weights(scaled) == {"control": 0.25, "treatment": 0.75}


# ---------------------------------------------------------------------------
# Validation: agent release pinning and resolver cross-checks
# ---------------------------------------------------------------------------


def test_validation_rejects_unpinned_packaged_distribution():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(
        release_id=None, verified=False
    )
    errors = validate_protocol(data, distribution_resolver=resolver)
    assert protocol_publication___has(
        errors, ValidationReasonCode.AGENT_RELEASE_UNPINNED
    )


def test_validation_rejects_latest_distribution_release():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(release_id="latest")
    assert protocol_publication___has(
        validate_protocol(data, distribution_resolver=resolver),
        ValidationReasonCode.AGENT_RELEASE_LATEST,
    )


def test_validation_rejects_mutable_agent_command_on_packaged_pin():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["resolved_distribution"]["agent_command"] = (
        "npx some-agent@latest"
    )
    assert protocol_publication___has(
        validate_protocol(data), ValidationReasonCode.MUTABLE_AGENT_COMMAND
    )


def test_validation_rejects_unresolved_release_via_injected_resolver():
    resolver = protocol_publication___FakeDistributionResolver(
        found=True,
        release_id="rel-0001",
        verified=False,
        release_status=ReleaseResolutionStatus.NOT_FOUND,
    )
    errors = validate_protocol(
        protocol_publication___protocol(), distribution_resolver=resolver
    )
    assert protocol_publication___has(errors, ValidationReasonCode.RELEASE_UNRESOLVED)
    assert resolver.calls  # the resolver was consulted per condition


def test_validation_flags_unverified_distribution_for_admin_as_warning():
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.UNQUALIFIED
    )
    errors = validate_protocol(
        protocol_publication___protocol(),
        distribution_resolver=resolver,
        actor_is_admin=True,
    )
    assert protocol_publication___has(errors, ValidationReasonCode.DISTRIBUTION_UNVERIFIED)
    unverified = next(
        error
        for error in errors
        if error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    )
    assert unverified.severity == ValidationSeverity.WARNING


def test_validation_rejects_unverified_distribution_for_researcher():
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.UNQUALIFIED
    )
    errors = validate_protocol(
        protocol_publication___protocol(),
        distribution_resolver=resolver,
        actor_is_admin=False,
    )
    assert protocol_publication___has(errors, ValidationReasonCode.DISTRIBUTION_UNVERIFIED)
    unverified = next(
        error
        for error in errors
        if error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    )
    assert unverified.severity == ValidationSeverity.ERROR


def test_validation_blocks_withdrawn_release():
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.WITHDRAWN
    )
    errors = validate_protocol(
        protocol_publication___protocol(), distribution_resolver=resolver
    )
    assert protocol_publication___has(errors, ValidationReasonCode.RELEASE_WITHDRAWN)


def test_validation_detects_release_digest_mismatch():
    resolver = protocol_publication___FakeDistributionResolver(
        artifact_digest="sha256:" + "f" * 64
    )
    errors = validate_protocol(
        protocol_publication___protocol(), distribution_resolver=resolver
    )
    assert protocol_publication___has(errors, ValidationReasonCode.RELEASE_DIGEST_MISMATCH)


def test_validation_accepts_resolved_release_with_matching_digest():
    data = protocol_publication___fixture_data()
    data["conditions"] = [data["conditions"][0]]
    digest = data["conditions"][0]["resolved_distribution"]["artifact_digest"]
    resolver = protocol_publication___FakeDistributionResolver(
        release_id="rel-0001", artifact_digest=digest
    )
    errors = validate_protocol(
        StudyProtocolV1.model_validate(data), distribution_resolver=resolver
    )
    assert errors == []


def test_null_release_resolver_qualifies_only_named_releases():
    resolver = NullReleaseResolver()
    named = resolver.resolve("agent", release_id="rel-1", version=None)
    assert named.status == ReleaseResolutionStatus.RESOLVED
    unnamed = resolver.resolve("agent", release_id=None, version=None)
    assert unnamed.status == ReleaseResolutionStatus.UNQUALIFIED


# ---------------------------------------------------------------------------
# Validation: document safety (identifiers / credentials)
# ---------------------------------------------------------------------------


def test_validation_rejects_participant_identifier_leakage():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["declared_overrides"] = {"participant_id": "person-42"}
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_IDENTIFIER)


def test_validation_rejects_session_and_account_identifier_leakage():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["declared_overrides"] = {
        "session_id": "sess-1",
        "user_id": "acct-1",
    }
    errors = validate_protocol(data)
    assert protocol_publication___codes(errors) & {
        ValidationReasonCode.FORBIDDEN_IDENTIFIER,
    }


def test_validation_rejects_credential_key_and_value_leakage():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["declared_overrides"] = {"api_key": "sk-ABCDEFGH12345678"}
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_CREDENTIAL)

    data = protocol_publication___fixture_data()
    data["metadata"]["owner"] = "Bearer ABCDEFGH12345678"
    assert protocol_publication___has(validate_protocol(data), ValidationReasonCode.FORBIDDEN_CREDENTIAL)


def test_scan_document_safety_ignores_clean_document():
    from research.study.protocol.validation import scan_document_safety

    assert scan_document_safety(protocol_publication___protocol()) == []


# ---------------------------------------------------------------------------
# Null vs explicit-unknown semantics
# ---------------------------------------------------------------------------


def test_null_optional_policy_stays_null_on_roundtrip():
    data = protocol_publication___fixture_data()
    data["enrollment"]["capacity"] = None
    data["enrollment"]["allow_reentry"] = None
    data["session_policy"] = {
        "idle_timeout_seconds": None,
        "resume_grace_seconds": None,
        "heartbeat_seconds": None,
    }
    protocol = StudyProtocolV1.model_validate(data)

    assert protocol.enrollment.capacity is None
    assert protocol.session_policy.idle_timeout_seconds is None

    dumped = protocol.model_dump(mode="json")
    assert dumped["enrollment"]["capacity"] is None
    assert dumped["session_policy"]["idle_timeout_seconds"] is None

    reloaded = StudyProtocolV1.model_validate(dumped)
    assert reloaded.enrollment.capacity is None
    assert reloaded.session_policy.heartbeat_seconds is None


def test_explicit_unknown_stays_typed_unknown_on_roundtrip():
    data = protocol_publication___fixture_data()
    data["enrollment"]["capacity"] = {"kind": "UNKNOWN"}
    protocol = StudyProtocolV1.model_validate(data)

    assert isinstance(protocol.enrollment.capacity, ExplicitUnknown)

    dumped = protocol.model_dump(mode="json")
    assert dumped["enrollment"]["capacity"] == {"kind": "UNKNOWN"}

    reloaded = StudyProtocolV1.model_validate(dumped)
    assert isinstance(reloaded.enrollment.capacity, ExplicitUnknown)


def test_explicit_unknown_host_kind_blocks_with_needs_review():
    data = protocol_publication___fixture_data()
    data["environment_requirements"]["host_kind"] = {"kind": "UNKNOWN"}
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.UNKNOWN_ENVIRONMENT_REQUIREMENT)
    assert not any(error.severity == ValidationSeverity.ERROR for error in errors)


def test_null_and_unknown_are_distinct_states():
    null_data = protocol_publication___fixture_data()
    null_data["enrollment"]["capacity"] = None
    unknown_data = protocol_publication___fixture_data()
    unknown_data["enrollment"]["capacity"] = {"kind": "UNKNOWN"}

    null_protocol = StudyProtocolV1.model_validate(null_data)
    unknown_protocol = StudyProtocolV1.model_validate(unknown_data)

    assert null_protocol.enrollment.capacity is None
    assert isinstance(unknown_protocol.enrollment.capacity, ExplicitUnknown)
    assert null_protocol.enrollment.capacity != unknown_protocol.enrollment.capacity


# ---------------------------------------------------------------------------
# Publication: immutability, lineage, concurrency
# ---------------------------------------------------------------------------


def test_publish_produces_immutable_content_addressed_revision():
    protocol = protocol_publication___protocol()
    result = publish_revision(
        protocol, RevisionLineage(study_id=protocol.study_id), now=protocol_publication__NOW
    )

    assert result.outcome == PublicationOutcome.PUBLISHED
    revision = result.revision
    assert revision is not None
    assert revision.revision_number == 1
    assert revision.status == RevisionStatus.PUBLISHED
    assert revision.supersedes_revision_id is None
    assert revision.published_at == protocol_publication__NOW
    assert revision.protocol_digest == protocol_digest(protocol)
    assert result.audit is not None
    assert result.audit.event_type == "study.revision.published"
    assert result.audit.revision_id == revision.revision_id


def test_editing_published_revision_creates_successor_without_mutating_prior():
    protocol = protocol_publication___protocol()
    first = protocol_publication___published(protocol)
    first_snapshot = first.model_dump(mode="json")

    edited = protocol.model_copy(deep=True)
    edited.privacy_policy.retention_days = 30
    result = publish_revision(edited, protocol_publication___lineage_for(first), now=protocol_publication__NOW)
    assert result.outcome == PublicationOutcome.PUBLISHED
    successor = result.revision
    assert successor is not None

    assert successor.revision_number == 2
    assert successor.supersedes_revision_id == first.revision_id
    assert successor.protocol_digest != first.protocol_digest
    assert successor.protocol_json["privacy_policy"]["retention_days"] == 30
    # The prior revision object is untouched.
    assert first.model_dump(mode="json") == first_snapshot
    assert first.protocol_json["privacy_policy"]["retention_days"] == 365


def test_optimistic_concurrency_conflict_on_stale_revision_number():
    protocol = protocol_publication___protocol()
    first = protocol_publication___published(protocol)

    result = publish_revision(
        protocol,
        protocol_publication___lineage_for(first),
        expected_revision_number=0,
        now=protocol_publication__NOW,
    )

    assert result.outcome == PublicationOutcome.CONFLICT
    assert result.revision is None
    assert result.conflict is not None
    assert result.conflict.expected_revision_number == 0
    assert result.conflict.actual_revision_number == 1
    assert result.audit is not None
    assert result.audit.event_type == "study.revision.publish_conflict"


def test_optimistic_concurrency_accepts_matching_revision_number():
    protocol = protocol_publication___protocol()
    first = protocol_publication___published(protocol)

    result = publish_revision(
        protocol,
        protocol_publication___lineage_for(first),
        expected_revision_number=first.revision_number,
        now=protocol_publication__NOW,
    )
    assert result.outcome == PublicationOutcome.PUBLISHED
    assert result.revision.revision_number == 2


def test_publication_unresolved_release_blocks_with_typed_reason():
    protocol = protocol_publication___protocol()
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.NOT_FOUND
    )
    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=resolver,
        now=protocol_publication__NOW,
    )
    assert result.outcome == PublicationOutcome.VALIDATION_FAILED
    assert protocol_publication___has(result.errors, ValidationReasonCode.RELEASE_UNRESOLVED)


def test_publication_freezes_the_resolved_distribution_pin():
    """Publish writes the resolved pin into the stored protocol_json."""
    protocol = protocol_publication___protocol()
    digest = "sha256:" + "a" * 64
    resolver = protocol_publication___FakeDistributionResolver(
        release_id="rel-0001", artifact_digest=digest, verified=True
    )
    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=resolver,
        actor_is_admin=True,
        now=protocol_publication__NOW,
    )

    assert result.outcome == PublicationOutcome.PUBLISHED
    revision = result.revision
    assert revision is not None
    for condition in revision.protocol_json["conditions"]:
        frozen = condition["resolved_distribution"]
        assert frozen["release_id"] == "rel-0001"
        assert frozen["artifact_digest"] == digest
        assert frozen["distribution_mode"] == "PACKAGED"
        assert frozen["verified"] is True
        assert frozen["resolved_at"] is not None
    # The stored digest is the digest of the FROZEN document.
    frozen_protocol = StudyProtocolV1.model_validate(revision.protocol_json)
    assert revision.protocol_digest == protocol_digest(frozen_protocol)


def test_admin_can_publish_an_unverified_distribution_and_is_warned():
    protocol = protocol_publication___protocol()
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.UNQUALIFIED
    )
    errors = validate_protocol(
        protocol, distribution_resolver=resolver, actor_is_admin=True
    )
    assert is_publishable(errors)
    assert {error.code for error in warnings(errors)} == {
        ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    }

    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=resolver,
        actor_is_admin=True,
        now=protocol_publication__NOW,
    )
    assert result.outcome == PublicationOutcome.PUBLISHED
    assert result.revision is not None


def test_researcher_publish_of_an_unverified_distribution_is_blocked():
    protocol = protocol_publication___protocol()
    resolver = protocol_publication___FakeDistributionResolver(
        verified=False, release_status=ReleaseResolutionStatus.UNQUALIFIED
    )
    errors = validate_protocol(
        protocol, distribution_resolver=resolver, actor_is_admin=False
    )
    assert not is_publishable(errors)

    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=resolver,
        actor_is_admin=False,
        now=protocol_publication__NOW,
    )
    assert result.outcome == PublicationOutcome.VALIDATION_FAILED
    assert ValidationReasonCode.DISTRIBUTION_UNVERIFIED in protocol_publication___codes(
        result.errors
    )


def test_lineage_from_revisions_picks_the_highest_revision_number():
    protocol = protocol_publication___protocol()
    first = protocol_publication___published(protocol)
    second = protocol_publication___published(
        protocol.model_copy(deep=True), protocol_publication___lineage_for(first)
    )
    # Pass them out of order to prove lineage is computed, not positional.
    lineage = lineage_from_revisions(protocol.study_id, [second, first])
    assert lineage.latest_revision_number == 2
    assert lineage.latest_revision_id == second.revision_id


def test_retire_revision_marks_copy_and_leaves_original_published():
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)

    result = retire_revision(revision, actor="admin@example.com", now=protocol_publication__NOW)
    assert result.retired is True
    assert result.revision is not None
    assert result.revision.status == RevisionStatus.RETIRED
    assert result.revision.protocol_digest == revision.protocol_digest
    assert result.audit is not None
    assert result.audit.event_type == "study.revision.retired"
    assert revision.status == RevisionStatus.PUBLISHED

    again = retire_revision(result.revision, now=protocol_publication__NOW)
    assert again.retired is False


# ---------------------------------------------------------------------------
# Persistence helpers (no PostgreSQL; session is a MagicMock)
# ---------------------------------------------------------------------------


def test_persist_revision_adds_only_the_revision_row():
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)
    session = MagicMock()

    row = store_module.persist_revision(session, revision)

    added = [call.args[0] for call in session.add.call_args_list]
    assert len(added) == 1
    revision_row = added[0]
    assert revision_row.status == RevisionStatus.PUBLISHED.value
    # Conditions are part of protocol_json, so no child condition rows are
    # written.
    assert len(revision_row.protocol_json["conditions"]) == len(
        revision.protocol_json["conditions"]
    )
    assert not hasattr(revision_row, "agent_release_json")
    assert row.revision_id == revision.revision_id
    assert revision_row.protocol_json == revision.protocol_json
    session.commit.assert_called_once()
    session.refresh.assert_called_once()


def test_row_to_revision_round_trips_protocol_json_into_model():
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)
    fake_row = protocol_publication___revision_row(revision)

    restored = store_module.row_to_revision(fake_row)
    assert isinstance(restored, type(revision))
    assert restored.revision_id == revision.revision_id
    assert restored.protocol_digest == revision.protocol_digest
    assert restored.status == RevisionStatus.PUBLISHED

    rehydrated = store_module.row_to_protocol(fake_row)
    assert isinstance(rehydrated, StudyProtocolV1)
    assert protocol_digest(rehydrated) == revision.protocol_digest


def test_create_draft_stores_protocol_mapping():
    protocol = protocol_publication___protocol()
    session = MagicMock()
    draft_id = uuid.uuid4()

    row = store_module.create_draft(
        session,
        draft_id=draft_id,
        study_id=protocol.study_id,
        name="Approved draft",
        protocol=protocol,
    )

    added = session.add.call_args.args[0]
    assert added.protocol_json == protocol.model_dump(mode="json")
    assert added.study_id == protocol.study_id
    assert row.draft_id == draft_id
    session.commit.assert_called_once()


def test_get_draft_and_list_revisions_use_session():
    session = MagicMock()
    draft_id = uuid.uuid4()
    store_module.get_draft(session, draft_id)
    session.get.assert_called_once()

    study_id = uuid.uuid4()
    session.execute.return_value.scalars.return_value.all.return_value = []
    assert list(store_module.list_revisions(session, study_id)) == []
    assert list(store_module.list_drafts(session, study_id)) == []


def test_retire_revision_store_flips_status():
    session = MagicMock()
    fake_row = SimpleNamespace(status=RevisionStatus.PUBLISHED.value)
    session.get.return_value = fake_row

    updated = store_module.retire_revision(session, uuid.uuid4())

    assert updated.status == RevisionStatus.RETIRED.value
    session.commit.assert_called_once()
    session.refresh.assert_called_once()


def test_revision_summary_is_non_secret_and_serializable():
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)
    summary = store_module.revision_summary(protocol_publication___revision_row(revision))

    assert summary["protocol_digest"] == revision.protocol_digest
    assert summary["status"] == "PUBLISHED"
    assert summary["supersedes_revision_id"] is None
    serialized = protocol_canonical_json(protocol)
    assert "sk-" not in json.dumps(summary)
    assert serialized  # canonical JSON is retained for export


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def test_study_routes_are_wired_under_the_research_prefix():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/studies/drafts" in paths
    assert "/research/studies/drafts/validate" in paths
    assert "/research/studies/drafts/{draft_id}" in paths
    assert "/research/studies/drafts/{draft_id}/publish" in paths
    assert "/research/studies/revisions" in paths
    assert "/research/studies/revisions/{revision_id}" in paths
    assert "/research/studies/revisions/{revision_id}/supersede" in paths
    assert "/research/studies/revisions/{revision_id}/retire" in paths


def test_create_draft_denies_a_participant():
    app = MagicMock()
    payload = DraftCreateRequest(
        study_id=protocol_publication___protocol().study_id, name="draft", protocol=protocol_publication___protocol()
    )

    # A participant account is not an enabled researcher; it is refused before
    # any database work.
    with pytest.raises(HTTPException) as error:
        create_draft(payload, protocol_publication___non_admin(), app)

    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_list_revisions_denies_a_non_owner():
    app = MagicMock()

    with pytest.raises(HTTPException) as error:
        list_revisions(uuid.uuid4(), protocol_publication___non_admin(), app)

    assert error.value.status_code == 403


def test_validate_endpoint_accepts_approved_fixture(
    protocol_publication__verified_distributions,
):
    app = MagicMock()
    request = ValidateRequest(protocol=protocol_publication___protocol())

    response = validate_draft(request, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body == {"valid": True, "errors": [], "warnings": []}


def test_publish_endpoint_rejects_digest_mismatch():
    app = MagicMock()
    request = PublishRequest(
        protocol=protocol_publication___protocol(), expected_protocol_digest="deadbeef"
    )

    with patch(
        "backend.routers.research.studies.store.get_draft",
        return_value=SimpleNamespace(draft_id=uuid.uuid4()),
    ), pytest.raises(HTTPException) as error:
        publish_draft(uuid.uuid4(), request, protocol_publication___admin(), app)

    assert error.value.status_code == 400


def test_publish_endpoint_returns_typed_published_response(
    protocol_publication__verified_distributions,
):
    app = MagicMock()
    protocol = protocol_publication___protocol()
    request = PublishRequest(
        protocol=protocol, expected_protocol_digest=protocol_digest(protocol)
    )
    published = protocol_publication___published(protocol)
    fake_row = protocol_publication___revision_row(published)
    summary = store_module.revision_summary(fake_row)

    with patch(
        "backend.routers.research.studies.store.get_draft",
        return_value=SimpleNamespace(draft_id=uuid.uuid4()),
    ), patch(
        "backend.routers.research.studies.store.list_revisions",
        return_value=[],
    ), patch(
        "backend.routers.research.studies.store.persist_revision",
        return_value=fake_row,
    ) as persist, patch(
        "backend.routers.research.studies.store.persist_audit",
    ) as audit, patch(
        "backend.routers.research.studies.store.revision_summary",
        return_value=summary,
    ):
        response = publish_draft(uuid.uuid4(), request, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["outcome"] == PublicationOutcome.PUBLISHED.value
    assert body["revision"]["protocol_digest"] == published.protocol_digest
    assert body["conflict"] is None
    persist.assert_called_once()
    audit.assert_called_once()
    app.get_db_session.return_value.close.assert_called_once()


def test_publish_endpoint_warns_for_admin_unverified_distribution(monkeypatch):
    app = MagicMock()
    protocol = protocol_publication___protocol()
    request = PublishRequest(
        protocol=protocol, expected_protocol_digest=protocol_digest(protocol)
    )
    published = protocol_publication___published(protocol)
    fake_row = protocol_publication___revision_row(published)
    summary = store_module.revision_summary(fake_row)

    class UnverifiedFixtureResolver(protocol_publication___FixtureDistributionResolver):
        def resolve(self, distribution_id):
            view = super().resolve(distribution_id)
            return view.model_copy(
                update={
                    "verified": False,
                    "release_status": ReleaseResolutionStatus.UNQUALIFIED,
                }
            )

    monkeypatch.setattr(
        "backend.routers.research.studies._DbDistributionResolver",
        UnverifiedFixtureResolver,
    )

    with patch(
        "backend.routers.research.studies.store.get_draft",
        return_value=SimpleNamespace(draft_id=uuid.uuid4()),
    ), patch(
        "backend.routers.research.studies.store.list_revisions",
        return_value=[],
    ), patch(
        "backend.routers.research.studies.store.persist_revision",
        return_value=fake_row,
    ), patch(
        "backend.routers.research.studies.store.persist_audit",
    ), patch(
        "backend.routers.research.studies.store.revision_summary",
        return_value=summary,
    ):
        response = publish_draft(
            uuid.uuid4(), request, protocol_publication___admin(), app
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["outcome"] == PublicationOutcome.PUBLISHED.value
    assert {warning["code"] for warning in body["warnings"]} == {
        ValidationReasonCode.DISTRIBUTION_UNVERIFIED.value
    }


def test_publish_endpoint_returns_conflict_on_stale_revision(
    protocol_publication__verified_distributions,
):
    app = MagicMock()
    protocol = protocol_publication___protocol()
    request = PublishRequest(protocol=protocol, expected_revision_number=0)
    existing = protocol_publication___published(protocol)
    existing_row = protocol_publication___revision_row(existing)

    with patch(
        "backend.routers.research.studies.store.get_draft",
        return_value=SimpleNamespace(draft_id=uuid.uuid4()),
    ), patch(
        "backend.routers.research.studies.store.list_revisions",
        return_value=[existing_row],
    ):
        response = publish_draft(uuid.uuid4(), request, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 409
    assert body["outcome"] == PublicationOutcome.CONFLICT.value
    assert body["conflict"]["actual_revision_number"] == 1


def test_supersede_endpoint_rejects_missing_revision():
    app = MagicMock()
    request = PublishRequest(protocol=protocol_publication___protocol())

    with patch(
        "backend.routers.research.studies.store.get_revision",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as error:
            supersede_revision(uuid.uuid4(), request, protocol_publication___admin(), app)

    assert error.value.status_code == 404


def test_get_revision_endpoint_returns_protocol():
    app = MagicMock()
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)
    fake_row = protocol_publication___revision_row(revision)

    with patch(
        "backend.routers.research.studies.store.get_revision",
        return_value=fake_row,
    ):
        response = get_revision(revision.revision_id, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["revision"]["protocol_digest"] == revision.protocol_digest
    assert body["protocol"]["schema_version"] == "1"


def test_retire_endpoint_returns_retired_revision():
    app = MagicMock()
    protocol = protocol_publication___protocol()
    revision = protocol_publication___published(protocol)
    fake_row = protocol_publication___revision_row(revision)

    with patch(
        "backend.routers.research.studies.store.get_revision",
        return_value=fake_row,
    ), patch(
        "backend.routers.research.studies.store.retire_revision",
        return_value=SimpleNamespace(
            **{**protocol_publication___revision_row(revision).__dict__, "status": "RETIRED"}
        ),
    ), patch(
        "backend.routers.research.studies.store.persist_audit",
    ), patch(
        "backend.routers.research.studies.store.revision_summary",
        return_value={"revision_id": str(revision.revision_id), "status": "RETIRED"},
    ):
        response = retire_published_revision(
            revision.revision_id, RetireRequest(actor="admin@example.com"), protocol_publication___admin(), app
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["retired"] is True
    assert body["revision"]["status"] == "RETIRED"


def test_list_drafts_endpoint_returns_drafts():
    app = MagicMock()
    draft = SimpleNamespace(
        draft_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        name="draft",
        schema_version="1",
    )
    with patch(
        "backend.routers.research.studies.store.list_drafts",
        return_value=[draft],
    ):
        response = list_drafts(draft.study_id, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["drafts"][0]["name"] == "draft"


def test_supersede_endpoint_returns_successor_for_admin(
    protocol_publication__verified_distributions,
):
    app = MagicMock()
    protocol = protocol_publication___protocol()
    first = protocol_publication___published(protocol)
    first_row = protocol_publication___revision_row(first)

    edited = protocol.model_copy(deep=True)
    edited.privacy_policy.retention_days = 30

    with patch(
        "backend.routers.research.studies.store.get_revision",
        return_value=first_row,
    ), patch(
        "backend.routers.research.studies.store.list_revisions",
        return_value=[first_row],
    ), patch(
        "backend.routers.research.studies.store.persist_revision",
    ) as persist, patch(
        "backend.routers.research.studies.store.persist_audit",
    ), patch(
        "backend.routers.research.studies.store.revision_summary",
        return_value={"revision_number": 2, "status": "PUBLISHED"},
    ):
        response = supersede_revision(
            first.revision_id, PublishRequest(protocol=edited), protocol_publication___admin(), app
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["outcome"] == PublicationOutcome.PUBLISHED.value
    persisted_revision = persist.call_args.args[1]
    assert persisted_revision.revision_number == 2
    assert persisted_revision.supersedes_revision_id == first.revision_id


def test_copy_is_independent_of_fixture_mutation():
    first = copy.deepcopy(protocol_publication___fixture_data())
    second = protocol_publication___fixture_data()
    assert first == second


# ---------------------------------------------------------------------------
# Shared canonical helpers (research.canonical) and document path safety
# ---------------------------------------------------------------------------


def test_shared_canonical_module_is_reexported_by_compatibility():
    from research.canonical import canonical_hash as shared_hash
    from research.compatibility.canonical import canonical_hash as compat_hash

    payload = {"b": [1, {"d": 4, "c": 3}], "a": 1}
    reordered = {"a": 1, "b": [1, {"c": 3, "d": 4}]}
    # The shared primitive and the Issue 01 re-export are the same function.
    assert shared_hash(payload) == compat_hash(payload)
    assert compat_hash(payload) == compat_hash(reordered)


def test_shared_canonical_hash_ignores_whitespace_and_key_order():
    from research.canonical import canonical_bytes, canonical_hash

    first = json.loads('{"a": 1, "b": {"c": 2, "d": 3}}')
    second = json.loads('{  "b" : { "d" : 3 , "c" : 2 } , "a" : 1 }')
    assert canonical_hash(first) == canonical_hash(second)
    assert canonical_bytes(first) == canonical_bytes(second)


def test_validation_rejects_posix_local_path_value():
    data = protocol_publication___fixture_data()
    data["metadata"]["owner"] = "/Users/researcher/agent/bin/run"
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_LOCAL_PATH)


def test_validation_rejects_windows_local_path_value():
    data = protocol_publication___fixture_data()
    data["metadata"]["owner"] = "C:\\agent\\bin\\run.exe"
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_LOCAL_PATH)


def test_validation_rejects_embedded_local_path_in_free_form_metadata():
    data = protocol_publication___fixture_data()
    data["conditions"][0]["declared_overrides"] = {
        "launch": "run --config /home/researcher/.config/agent.toml"
    }
    errors = validate_protocol(data)
    assert protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_LOCAL_PATH)


def test_validation_does_not_flag_urls_or_relative_references():
    data = protocol_publication___fixture_data()
    data["metadata"]["owner"] = "https://registry.example.com/releases/rel-0001"
    errors = validate_protocol(data)
    assert not protocol_publication___has(errors, ValidationReasonCode.FORBIDDEN_LOCAL_PATH)


# ---------------------------------------------------------------------------
# Immutability guard (Issue 02 spec: assert_revision_mutable)
# ---------------------------------------------------------------------------


def test_assert_revision_mutable_rejects_published_revision():
    from research.study.protocol.publication import (
        ImmutableRevisionError,
        assert_revision_mutable,
    )

    revision = protocol_publication___published(protocol_publication___protocol())
    with pytest.raises(ImmutableRevisionError):
        assert_revision_mutable(revision)


def test_assert_revision_mutable_rejects_retired_revision():
    from research.study.protocol.publication import (
        ImmutableRevisionError,
        assert_revision_mutable,
    )

    revision = protocol_publication___published(protocol_publication___protocol()).model_copy(
        update={"status": RevisionStatus.RETIRED}
    )
    with pytest.raises(ImmutableRevisionError):
        assert_revision_mutable(revision)


def test_assert_revision_mutable_allows_draft():
    from research.study.protocol.publication import assert_revision_mutable

    revision = protocol_publication___published(protocol_publication___protocol()).model_copy(
        update={"status": RevisionStatus.DRAFT}
    )
    assert assert_revision_mutable(revision) is revision


# --------------------------------------------------------------------------
# test_seed_research_study
# --------------------------------------------------------------------------
# Focused tests for the synthetic study onboarding script (Issue 13 runbook).
#
# The environment has no PostgreSQL, so the script's orchestration is exercised
# against a tiny in-memory ``Session`` double that supports exactly the subset of
# SQLAlchemy the research stores use (``add``/``commit``/``refresh``/``get`` and
# simple ``select(...).where(...).scalars()`` queries). The real domain services
# (protocol publication, agent registry, identity enrollment/consent) therefore
# run for real; only row persistence is faked.
seed_research_study__SEED_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "dev" / "seed_research_study.py"
)


def seed_research_study___load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_research_study", seed_research_study__SEED_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_research_study"] = module
    spec.loader.exec_module(module)
    return module


seed_research_study__seed = seed_research_study___load_seed_module()


# ---------------------------------------------------------------------------
# A minimal in-memory SQLAlchemy Session double
# ---------------------------------------------------------------------------


class seed_research_study___Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "seed_research_study___Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None


def seed_research_study___clause_matches(row: Any, clause: Any) -> bool:
    nested = getattr(clause, "clauses", None)
    if nested is not None:
        return all(seed_research_study___clause_matches(row, item) for item in nested)
    left = getattr(clause, "left", None)
    if left is None:
        return True
    name = getattr(left, "name", None)
    right = getattr(clause, "right", None)
    expected = getattr(right, "value", right)
    actual = getattr(row, name, None)
    operator = getattr(clause, "operator", None)
    if getattr(operator, "__name__", "") == "ne":
        return actual != expected
    return actual == expected


class seed_research_study__FakeSession:
    """Stores added rows by class and answers the simple lookups we need."""

    def __init__(self) -> None:
        self._rows: dict[type, list[Any]] = {}

    def rows_of(self, model: type) -> list[Any]:
        return list(self._rows.get(model, []))

    def add(self, row: Any) -> None:
        # Mirror SQLAlchemy's identity map: adding an already-persistent row is
        # a no-op rather than a second row (stores re-add fetched rows).
        primary_key = type(row).__mapper__.primary_key[0].key
        rows = self._rows.setdefault(type(row), [])
        for index, existing in enumerate(rows):
            if getattr(existing, primary_key, None) == getattr(row, primary_key, None):
                rows[index] = row
                return
        rows.append(row)

    def commit(self) -> None:
        return None

    def flush(self) -> None:
        # The stores flush a parent row before adding FK-dependent children; the
        # in-memory double keeps everything in the same dict so this is a no-op.
        return None

    def refresh(self, row: Any) -> Any:
        return row

    def begin_nested(self) -> Any:
        # In-memory savepoint double: stores wrap an insert in ``begin_nested``
        # to survive a partial-unique conflict. The double keeps everything in
        # one dict, so a no-op context manager is sufficient.
        import contextlib

        return contextlib.nullcontext()

    def close(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def get(self, model: type, pk: Any) -> Optional[Any]:
        primary_key = model.__mapper__.primary_key[0].key
        for row in self._rows.get(model, []):
            if getattr(row, primary_key, None) == pk:
                return row
        return None

    def execute(self, statement: Any) -> seed_research_study___Result:
        entity = statement.column_descriptions[0]["entity"]
        rows = list(self._rows.get(entity, []))
        clause = statement.whereclause
        if clause is not None:
            rows = [row for row in rows if seed_research_study___clause_matches(row, clause)]
        return seed_research_study___Result(rows)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seed_research_study__patched_crud(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the login-account CRUD with an in-memory single user."""
    import database.crud as crud

    account_state: dict[str, Any] = {"user": None}

    def fake_get_user_by_email(_db, email):
        user = account_state["user"]
        if user is not None and str(user.email) == str(email):
            return user
        return None

    def fake_create_user(_db, payload):
        import database.db_schemas as db_schemas

        # A real ORM row so the seed can flip can_research/verified and add it
        # to the in-memory session.
        user = db_schemas.User(
            user_id=uuid.uuid4(),
            joined_at=datetime.now(timezone.utc),
            email=str(payload.email),
            name=payload.name,
            password="x",
            config_id=payload.config_id,
            verified=False,
            is_admin=False,
            can_research=False,
        )
        account_state["user"] = user
        return user

    def fake_get_all_configs(_db):
        return [SimpleNamespace(config_id=1)]

    monkeypatch.setattr(crud, "get_user_by_email", fake_get_user_by_email)
    monkeypatch.setattr(crud, "create_user", fake_create_user)
    monkeypatch.setattr(crud, "get_all_configs", fake_get_all_configs)
    return account_state


def seed_research_study___request(**overrides) -> "seed_research_study__seed.SeedRequest":
    base = dict(
        account_email="participant@example.com",
        account_password="Password123",
        account_name="Synthetic Participant",
        create_account=True,
        config_id=1,
        study_name="Synthetic Test Study",
        agent_id="synthetic-agent",
        release_id="synthetic-agent-rel-1",
        release_version="1.0.0",
        artifact_digest="sha256:" + "a" * 64,
        artifact_path="agents/macos-aarch64/code4me-agent",
        artifact_size=1024,
        os_name="macos",
        arch="aarch64",
        actor="seed-script",
    )
    base.update(overrides)
    return seed_research_study__seed.SeedRequest(**base)


# ---------------------------------------------------------------------------
# Pure planning helpers
# ---------------------------------------------------------------------------


def test_build_study_protocol_is_deterministic_and_valid():
    request = seed_research_study___request()
    release = seed_research_study__seed.build_synthetic_release(request)
    first = seed_research_study__seed.build_study_protocol(request, release)
    second = seed_research_study__seed.build_study_protocol(request, release)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.conditions) == 2
    assert {c.condition_id for c in first.conditions} == {"control", "treatment"}
    assert [c.weight for c in first.conditions] == [0.5, 0.5]
    for condition in first.conditions:
        assert condition.distribution_id == request.distribution_id
        assert condition.resolved_distribution is None

    resolver = protocol_publication___FakeDistributionResolver(
        release_id=release.release_id,
        version=release.version,
        artifact_digest=request.artifact_digest,
    )
    assert validate_protocol(first, distribution_resolver=resolver) == []


def test_release_is_qualifiable_by_the_registry_with_one_artifact():
    request = seed_research_study___request()
    release = seed_research_study__seed.build_synthetic_release(request)

    registry = AgentRegistry()
    assert registry.register_release(release).accepted
    # Qualification itself is derived from conformance evidence; the registry
    # only reports whether the release has the prerequisites to be qualified.
    assert registry.assess_qualification(release).qualifiable


# ---------------------------------------------------------------------------
# End-to-end orchestration against the fake session
# ---------------------------------------------------------------------------


def test_run_seed_publishes_revision_and_activates_enrollment(seed_research_study__patched_crud, capsys):
    session = seed_research_study__FakeSession()
    request = seed_research_study___request()

    summary = seed_research_study__seed.run_seed(session, request)
    print(seed_research_study__seed.format_summary(summary))
    printed = capsys.readouterr().out

    assert summary.enrollment_status == EnrollmentStatus.ACTIVE.value
    assert summary.enrollment_id in printed
    assert summary.account_created is True
    assert summary.account_password == request.account_password

    revisions = session.rows_of(research_schemas.StudyRevision)
    published = [
        row for row in revisions if row.status == RevisionStatus.PUBLISHED.value
    ]
    drafts = [row for row in revisions if row.status == RevisionStatus.DRAFT.value]
    assert len(published) == 1
    assert len(drafts) == 1
    # The stored revision's digest is the digest of the FROZEN document.
    assert published[0].protocol_digest == protocol_digest(
        StudyProtocolV1.model_validate(published[0].protocol_json)
    )
    for condition in published[0].protocol_json["conditions"]:
        assert condition["resolved_distribution"]["release_id"] == request.release_id
        assert (
            condition["resolved_distribution"]["artifact_digest"]
            == request.artifact_digest
        )

    enrollments = session.rows_of(research_schemas.ResearchEnrollment)
    assert len(enrollments) == 1
    assert enrollments[0].status == EnrollmentStatus.ACTIVE.value
    assert str(enrollments[0].enrollment_id) == summary.enrollment_id

    releases = session.rows_of(research_schemas.AgentRelease)
    assert len(releases) == 1
    assert releases[0].status == QualificationStatus.QUALIFIED.value


def test_run_seed_is_idempotent_on_second_run(seed_research_study__patched_crud):
    session = seed_research_study__FakeSession()
    request = seed_research_study___request()

    first = seed_research_study__seed.run_seed(session, request)
    second = seed_research_study__seed.run_seed(session, request)

    assert first.study_id == second.study_id
    assert first.revision_id == second.revision_id
    assert first.enrollment_id == second.enrollment_id
    assert second.enrollment_status == EnrollmentStatus.ACTIVE.value
    # The second run reuses the existing account, so no password is re-printed.
    assert second.account_created is False
    assert second.account_password is None

    # Study identity is the real ``public.study`` row, not a revision projection.
    import database.db_schemas as db_schemas

    studies = session.rows_of(db_schemas.Study)
    assert len(studies) == 1
    assert studies[0].study_id == request.study_id
    assert studies[0].name == request.study_name
    assert studies[0].is_research is True
    # The seed publishes a live study, as the publication route does.
    assert studies[0].is_active is True
    # The draft and the published revision share the ``study_revision`` table.
    revisions = session.rows_of(research_schemas.StudyRevision)
    assert sorted(row.status for row in revisions) == [
        RevisionStatus.DRAFT.value,
        RevisionStatus.PUBLISHED.value,
    ]
    assert len(session.rows_of(research_schemas.ResearchParticipant)) == 1
    assert len(session.rows_of(research_schemas.ResearchEnrollment)) == 1
    releases = session.rows_of(research_schemas.AgentRelease)
    assert len(releases) == 1
    # The distribution artifact lives in release_json, not a child table.
    assert len(releases[0].release_json["artifacts"]) == 1


def test_run_seed_requires_create_account_for_unknown_email(seed_research_study__patched_crud):
    session = seed_research_study__FakeSession()
    request = seed_research_study___request(create_account=False, account_password=None)

    with pytest.raises(seed_research_study__seed.SeedError, match="--create-account"):
        seed_research_study__seed.run_seed(session, request)


def test_run_seed_rejects_mutating_an_immutable_release(seed_research_study__patched_crud):
    session = seed_research_study__FakeSession()
    seed_research_study__seed.run_seed(session, seed_research_study___request())

    with pytest.raises(seed_research_study__seed.SeedError, match="immutable release"):
        seed_research_study__seed.run_seed(session, seed_research_study___request(artifact_digest="sha256:" + "b" * 64))


def test_main_uses_app_session_and_prints_enrollment_id(
    monkeypatch, capsys, seed_research_study__patched_crud
):
    session = seed_research_study__FakeSession()
    captured: dict[str, Any] = {}
    monkeypatch.setenv("CODE4ME_DEV_SEED", "1")

    class _FakeApp:
        def get_db_session(self):
            captured["session"] = session
            return session

    fake_app_module = MagicMock()
    fake_app_module.App.get_instance.return_value = _FakeApp()
    monkeypatch.setitem(sys.modules, "App", fake_app_module)

    exit_code = seed_research_study__seed.main(
        [
            "--account-email",
            "participant@example.com",
            "--account-password",
            "Password123",
            "--create-account",
        ]
    )

    printed = capsys.readouterr().out
    assert exit_code == 0
    assert "enrollment_id (join code):" in printed
    assert captured["session"] is session


def test_main_requires_an_explicit_dev_guard(monkeypatch):
    monkeypatch.delenv("CODE4ME_DEV_SEED", raising=False)
    monkeypatch.setenv("TEST_MODE", "false")

    with pytest.raises(SystemExit) as error:
        seed_research_study__seed.main(
            ["--account-email", "participant@example.com"]
        )

    assert "refuses to run" in str(error.value)


# --------------------------------------------------------------------------
# Agent distribution mode: PACKAGED (default) vs BYOA_EXTERNAL
# --------------------------------------------------------------------------


def agents_distribution___adapter() -> AdapterRef:
    return AdapterRef(adapter_id="adapter", version="1.0.0", digest="sha256:" + "d" * 64)


def agents_distribution___packaged(sha256: str = "sha256:" + "a" * 64) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="codex-acp",
        release_id="rel-packaged",
        version="1.0.0",
        source_manifest_digest="sha256:" + "1" * 64,
        artifacts=[
            DistributionArtifact(
                os="macos", arch="aarch64", path="artifact.bin", sha256=sha256, size=1
            )
        ],
        adapter=agents_distribution___adapter(),
        qualification_status=QualificationStatus.QUALIFIED,
    )


def agents_distribution___byoa(
    command: str = "goose", package: str = "goose"
) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="goose",
        release_id="rel-byoa",
        version="0.9.0",
        source_manifest_digest="sha256:" + "2" * 64,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_command=command,
        agent_command_args=["acp"],
        agent_package=package,
        adapter=agents_distribution___adapter(),
        qualification_status=QualificationStatus.QUALIFIED,
    )


class agents_distribution___ByoaResolver:
    """Resolver that always reports a participant-installed (BYOA) release."""

    def resolve(self, agent_id: str, *, release_id=None, version=None) -> ReleaseResolution:
        return ReleaseResolution(
            status=ReleaseResolutionStatus.RESOLVED,
            agent_id=agent_id,
            release_id=release_id,
            version=version,
            distribution_mode="BYOA_EXTERNAL",
        )


def test_packaged_is_the_default_mode_and_requires_a_digest():
    release = agents_distribution___packaged()
    assert release.distribution_mode == DistributionMode.PACKAGED
    assert AgentRegistry().register_release(release).accepted

    rejected = AgentRegistry().register_release(agents_distribution___packaged(sha256=""))
    assert not rejected.accepted
    assert rejected.issue is not None
    assert rejected.issue.code == RegistryReasonCode.DIGEST_MISMATCH


def test_byoa_requires_an_identity_but_not_a_digest():
    release = agents_distribution___byoa()
    assert release.is_byoa
    assert release.byoa_identity == "goose"
    assert AgentRegistry().register_release(release).accepted
    assert AgentRegistry().assess_qualification(release).qualifiable

    rejected = AgentRegistry().register_release(agents_distribution___byoa(command="", package=""))
    assert not rejected.accepted
    assert rejected.issue is not None
    assert rejected.issue.code == RegistryReasonCode.AGENT_NOT_FOUND


def test_resolve_distribution_returns_the_mode_specific_contract():
    packaged = AgentRegistry().resolve_distribution(
        agents_distribution___packaged(), "macos", "aarch64"
    )
    assert packaged.resolved
    assert packaged.distribution_mode == DistributionMode.PACKAGED
    assert packaged.artifact is not None

    byoa = AgentRegistry().resolve_distribution(
        agents_distribution___byoa(), "macos", "aarch64"
    )
    assert byoa.resolved
    assert byoa.distribution_mode == DistributionMode.BYOA_EXTERNAL
    assert byoa.artifact is None
    assert byoa.agent_command == "goose"
    assert byoa.agent_command_args == ["acp"]
    assert byoa.agent_package == "goose"


def test_registry_resolver_exposes_the_byoa_mode_without_a_digest():
    registry = AgentRegistry()
    registry.register_release(agents_distribution___byoa())

    resolution = RegistryReleaseResolver(registry).resolve("goose", release_id="rel-byoa")

    assert resolution.status == ReleaseResolutionStatus.RESOLVED
    assert resolution.distribution_mode == "BYOA_EXTERNAL"
    assert resolution.artifact_digest is None


def test_protocol_validation_allows_a_byoa_distribution_without_a_digest():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(
        distribution_mode="BYOA_EXTERNAL",
        release_id="rel-byoa",
        agent_package="goose",
        agent_command="goose",
        verified=False,
        release_status=ReleaseResolutionStatus.RESOLVED,
    )
    errors = validate_protocol(
        data, distribution_resolver=resolver, actor_is_admin=True
    )

    assert not protocol_publication___has(
        errors, ValidationReasonCode.AGENT_RELEASE_UNPINNED
    )
    assert protocol_publication___has(
        errors, ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    )


def test_protocol_validation_fails_closed_for_a_packaged_distribution_without_release():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(
        release_id=None, verified=False
    )
    errors = validate_protocol(data, distribution_resolver=resolver)
    assert protocol_publication___has(
        errors, ValidationReasonCode.AGENT_RELEASE_UNPINNED
    )


def test_byoa_distribution_requires_an_identity():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(
        distribution_mode="BYOA_EXTERNAL",
        release_id=None,
        agent_id=None,
        agent_package=None,
        agent_command=None,
        verified=False,
        release_status=ReleaseResolutionStatus.UNQUALIFIED,
    )
    errors = validate_protocol(
        data, distribution_resolver=resolver, actor_is_admin=True
    )
    assert protocol_publication___has(
        errors, ValidationReasonCode.AGENT_RELEASE_IDENTITY_REQUIRED
    )


def test_unknown_distribution_mode_is_rejected():
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    resolver = protocol_publication___FakeDistributionResolver(
        distribution_mode="SIDELOADED"
    )
    errors = validate_protocol(data, distribution_resolver=resolver)
    assert protocol_publication___has(
        errors, ValidationReasonCode.AGENT_RELEASE_MODE_UNKNOWN
    )


def test_seed_can_pin_a_byoa_goose_release(seed_research_study__patched_crud):
    session = seed_research_study__FakeSession()
    request = seed_research_study___request(
        distribution_mode="BYOA_EXTERNAL",
        agent_command="goose",
        agent_command_args=("acp",),
        agent_package="goose",
    )

    summary = seed_research_study__seed.run_seed(session, request)

    assert summary.distribution_mode == "BYOA_EXTERNAL"
    releases = session.rows_of(research_schemas.AgentRelease)
    assert len(releases) == 1
    release_json = releases[0].release_json
    assert release_json["distribution_mode"] == "BYOA_EXTERNAL"
    assert release_json["agent_command"] == "goose"
    assert release_json["agent_command_args"] == ["acp"]
    assert release_json["artifacts"] == []
    assert releases[0].status == QualificationStatus.QUALIFIED.value


# --------------------------------------------------------------------------
# Condition -> AgentProfile link and study creation
# --------------------------------------------------------------------------


def test_condition_uses_one_distribution_id_and_no_legacy_fields():
    """A condition is built from ONE distribution, not a (profile, release) pair."""
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        assert "distribution_id" in condition
        assert "agent_release" not in condition
        assert "agent_profile_id" not in condition

    protocol = StudyProtocolV1.model_validate(data)
    for condition in protocol.conditions:
        assert condition.distribution_id is not None
    assert protocol_digest(protocol) == protocol_publication__APPROVED_PROTOCOL_DIGEST


def test_distribution_id_round_trips_and_changes_the_digest():
    baseline = protocol_publication___protocol()
    new_id = uuid.uuid4()
    data = protocol_publication___fixture_data()
    data["conditions"][0]["distribution_id"] = str(new_id)
    data["conditions"][0]["resolved_distribution"]["distribution_id"] = str(new_id)
    changed = StudyProtocolV1.model_validate(data)

    assert changed.conditions[0].distribution_id == new_id
    assert protocol_digest(changed) != protocol_digest(baseline)
    canonical = json.loads(protocol_canonical_json(changed))
    assert canonical["conditions"][0]["distribution_id"] == str(new_id)


def protocol_publication___release_row(
    release: AgentReleaseV1, *, qualified: bool
) -> SimpleNamespace:
    payload = release.model_dump(mode="json")
    if qualified:
        # Evidence-bound receipt: artifact digest, adapter digest, host platform
        # and a passing case must all match the release to qualify it.
        artifact = release.artifacts[0] if release.artifacts else None
        payload["conformance"] = [
            {
                "status": "PASS",
                "artifact_digest": artifact.sha256 if artifact else release.source_manifest_digest,
                "adapter_digest": release.adapter.digest if release.adapter else None,
                "host": (
                    {"os": artifact.os, "arch": artifact.arch} if artifact else None
                ),
                "case_results": [{"case_id": "acp.initialize", "status": "PASS"}],
            }
        ]
    else:
        payload["conformance"] = []
    return SimpleNamespace(release_json=payload)


def protocol_publication___draft_data() -> dict:
    data = protocol_publication___fixture_data()
    for condition in data["conditions"]:
        condition.pop("resolved_distribution", None)
    return data


def test_distribution_errors_flags_unknown_distribution():
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    db = MagicMock()

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=None,
    ):
        errors = _distribution_errors(db, protocol, actor_is_admin=True)

    assert {error.code for error in errors} == {
        ValidationReasonCode.AGENT_PROFILE_NOT_FOUND
    }
    assert {error.field for error in errors} == {
        "conditions[0].distribution_id",
        "conditions[1].distribution_id",
    }


def test_distribution_errors_accepts_a_verified_distribution():
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    db = MagicMock()
    release = agents_distribution___packaged()
    profile = SimpleNamespace(
        profile_id=uuid.uuid4(),
        distribution_mode="PACKAGED",
        release_id=release.release_id,
        agent_package=None,
        agent_command=None,
        agent_command_args=None,
    )

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=profile,
    ), patch(
        "backend.routers.research.studies.registry_store.get_release",
        return_value=protocol_publication___release_row(release, qualified=True),
    ):
        errors = _distribution_errors(db, protocol, actor_is_admin=False)

    assert errors == []


def test_non_admin_cannot_build_from_an_unverified_distribution():
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    db = MagicMock()
    release = agents_distribution___packaged()
    profile = SimpleNamespace(
        profile_id=uuid.uuid4(),
        distribution_mode="PACKAGED",
        release_id=release.release_id,
        agent_package=None,
        agent_command=None,
        agent_command_args=None,
    )

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=profile,
    ), patch(
        "backend.routers.research.studies.registry_store.get_release",
        return_value=protocol_publication___release_row(release, qualified=False),
    ):
        errors = _distribution_errors(db, protocol, actor_is_admin=False)

    assert any(
        error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
        and error.severity == ValidationSeverity.ERROR
        for error in errors
    )


def test_admin_can_build_from_an_unverified_distribution_with_a_warning():
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    db = MagicMock()
    release = agents_distribution___packaged()
    profile = SimpleNamespace(
        profile_id=uuid.uuid4(),
        distribution_mode="PACKAGED",
        release_id=release.release_id,
        agent_package=None,
        agent_command=None,
        agent_command_args=None,
    )

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=profile,
    ), patch(
        "backend.routers.research.studies.registry_store.get_release",
        return_value=protocol_publication___release_row(release, qualified=False),
    ):
        errors = _distribution_errors(db, protocol, actor_is_admin=True)

    unverified = [
        error
        for error in errors
        if error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    ]
    assert unverified
    assert all(error.severity == ValidationSeverity.WARNING for error in unverified)


def test_byoa_distribution_has_no_digest_and_is_always_unverified():
    request = seed_research_study___request(
        distribution_mode="BYOA_EXTERNAL",
        agent_command="goose",
        agent_command_args=("acp",),
        agent_package="goose",
    )
    release = seed_research_study__seed.build_synthetic_release(request)
    distribution = seed_research_study__seed._SeedDistribution(
        request.distribution_id, request, release
    )

    view = resolve_distribution_view(distribution, release)

    assert view.distribution_mode == DistributionMode.BYOA_EXTERNAL.value
    assert view.artifact_digest is None
    assert view.verified is False


def test_packaged_distribution_without_release_id_fails_closed():
    request = seed_research_study___request()
    release = seed_research_study__seed.build_synthetic_release(request)
    profile = SimpleNamespace(
        profile_id=request.distribution_id,
        distribution_mode="PACKAGED",
        release_id=None,
        agent_package=None,
        agent_command=None,
        agent_command_args=None,
    )

    view = resolve_distribution_view(profile, None)

    assert view.verified is False
    assert view.release_id is None
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    resolver = protocol_publication___FakeDistributionResolver(
        release_id=None, verified=False
    )
    assert protocol_publication___has(
        validate_protocol(protocol, distribution_resolver=resolver),
        ValidationReasonCode.AGENT_RELEASE_UNPINNED,
    )


def test_create_draft_rejects_an_unknown_distribution():
    app = MagicMock()
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    payload = DraftCreateRequest(study_id=protocol.study_id, name="draft", protocol=protocol)

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as error:
            create_draft(payload, protocol_publication___admin(), app)

    assert error.value.status_code == 422
    assert error.value.detail[0]["code"] == (
        ValidationReasonCode.AGENT_PROFILE_NOT_FOUND.value
    )
    assert error.value.detail[0]["field"] == "conditions[0].distribution_id"


def test_validate_endpoint_checks_distribution_existence():
    app = MagicMock()
    protocol = StudyProtocolV1.model_validate(protocol_publication___draft_data())
    request = ValidateRequest(protocol=protocol)

    with patch(
        "backend.routers.research.studies.crud.get_agent_profile_by_id",
        return_value=None,
    ):
        response = validate_draft(request, protocol_publication___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 422
    assert body["valid"] is False
    assert any(
        item["code"] == ValidationReasonCode.AGENT_PROFILE_NOT_FOUND.value
        for item in body["errors"]
    )
    app.get_db_session.return_value.close.assert_called_once()


def test_study_routes_expose_a_create_endpoint():
    from backend.routers import router as api_router

    methods_by_path: dict[str, set[str]] = {}
    for route in api_router.routes:
        methods_by_path.setdefault(route.path, set()).update(
            getattr(route, "methods", set()) or set()
        )
    assert "POST" in methods_by_path["/research/studies"]


def _fake_study(**overrides):
    from research.study.protocol.store import StudyView

    base = dict(
        study_id=uuid.uuid4(),
        name="New Study",
        description="desc",
        owner=None,
        created_by=uuid.uuid4(),
        is_research=True,
        is_active=False,
        starts_at=None,
        ends_at=None,
        created_at=None,
    )
    base.update(overrides)
    return StudyView(**base)


def test_create_study_mints_an_identity_owned_by_caller():
    app = MagicMock()
    admin = protocol_publication___admin()
    request = StudyCreateRequest(name="  New Study  ", description="desc")
    study = _fake_study(created_by=admin.user_id)

    with patch(
        "backend.routers.research.studies.store.create_study", return_value=study
    ) as create:
        response = create_study_endpoint(request, admin, app)

    body = json.loads(response.body)
    assert response.status_code == 201
    payload = body["study"]
    assert payload["name"] == "New Study"
    assert payload["description"] == "desc"
    assert payload["created_by"] == str(admin.user_id)
    assert payload["is_research"] is True
    assert payload["is_active"] is False
    uuid.UUID(payload["study_id"])
    assert create.call_args.kwargs["created_by"] == admin.user_id
    assert create.call_args.kwargs["is_research"] is True
    app.get_db_session.return_value.close.assert_called_once()


def test_create_study_denies_a_participant():
    app = MagicMock()

    with pytest.raises(HTTPException) as error:
        create_study_endpoint(
            StudyCreateRequest(name="x"),
            protocol_publication___non_admin(),
            app,
        )

    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_create_study_allows_an_enabled_researcher():
    app = MagicMock()
    researcher = AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=False,
        email="r@example.com",
        name="Researcher",
        can_research=True,
    )
    study = _fake_study(created_by=researcher.user_id)

    with patch(
        "backend.routers.research.studies.store.create_study", return_value=study
    ):
        response = create_study_endpoint(StudyCreateRequest(name="x"), researcher, app)

    assert response.status_code == 201


def test_seed_pins_an_existing_distribution(
    seed_research_study__patched_crud,
):
    import database.db_schemas as db_schemas

    profile_id = uuid.uuid4()
    session = seed_research_study__FakeSession()
    session.add(
        db_schemas.AgentProfile(
            profile_id=profile_id,
            owner_user_id=uuid.uuid4(),
            name="existing-distribution",
            model="gpt",
            tools_json="[]",
            approval_policy="suggestion_only",
            max_steps=1,
            is_active=True,
            release_id="synthetic-agent-rel-1",
        )
    )
    request = seed_research_study___request(agent_profile_id=profile_id)

    summary = seed_research_study__seed.run_seed(session, request)

    assert summary.agent_profile_id == str(profile_id)
    revisions = session.rows_of(research_schemas.StudyRevision)
    published = [
        row for row in revisions if row.status == RevisionStatus.PUBLISHED.value
    ]
    assert len(published) == 1
    assert published[0].protocol_json["conditions"][0]["distribution_id"] == (
        str(profile_id)
    )


def test_seed_rejects_an_unknown_distribution(
    seed_research_study__patched_crud,
):
    session = seed_research_study__FakeSession()

    with pytest.raises(seed_research_study__seed.SeedError, match="does not exist"):
        seed_research_study__seed.run_seed(
            session, seed_research_study___request(agent_profile_id=uuid.uuid4())
        )


# --------------------------------------------------------------------------
# Fresh-DB seed: built manifest -> qualified release -> pinned distribution
# --------------------------------------------------------------------------


def fresh_db___manifest() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "runtime_version": "9.9.9",
        "managed_protocol_version": "1",
        "server_commit": "aaa1111",
        "artifacts": [
            {
                "runtime_id": "code4me-agent",
                "version": "9.9.9",
                "platform": "macos",
                "architecture": "arm64",
                "archive": "code4me-runtime/code4me-agent-macos-arm64.zip",
                "sha256": "a" * 64,
                "executable": "code4me2-agent",
            }
        ],
    }


def fresh_db___request(**overrides) -> "seed_research_study__seed.FreshDbRequest":
    base = dict(
        manifest=fresh_db___manifest(),
        artifact_sizes={"code4me-runtime/code4me-agent-macos-arm64.zip": 1024},
        study_name="Builtin Test Study",
        actor="test",
    )
    base.update(overrides)
    return seed_research_study__seed.FreshDbRequest(**base)


def test_seed_fresh_database_qualifies_the_manifest_release_and_pins_distribution(
    seed_research_study__patched_crud,
):
    import database.db_schemas as db_schemas

    session = seed_research_study__FakeSession()

    summary = seed_research_study__seed.seed_fresh_database(
        session, fresh_db___request()
    )

    assert summary.qualification == QualificationStatus.QUALIFIED.value
    assert summary.distribution_verified is True
    assert summary.supported_platforms == [{"os": "macos", "arch": "arm64"}]

    releases = session.rows_of(research_schemas.AgentRelease)
    assert len(releases) == 1
    assert releases[0].status == QualificationStatus.QUALIFIED.value
    # A receipt -- not a caller flag -- is what promoted the release.
    assert releases[0].release_json["conformance"]

    profiles = {row.name: row for row in session.rows_of(db_schemas.AgentProfile)}
    pinned = profiles["default-code4me2-agent"]
    # BYOA provisioning is retired: the seed creates one owner-scoped profile
    # that pins the registered release.
    assert pinned.release_id == releases[0].release_id
    assert pinned.connection_id is None
    assert pinned.owner_user_id is not None
    assert len(profiles) == 1

    published = [
        row
        for row in session.rows_of(research_schemas.StudyRevision)
        if row.status == RevisionStatus.PUBLISHED.value
    ]
    assert len(published) == 1
    policy = published[0].protocol_json["session_policy"]
    assert policy["idle_timeout_seconds"] and policy["resume_grace_seconds"]
    assert policy["heartbeat_seconds"]
    assert (
        published[0].protocol_json["conditions"][0]["resolved_distribution"]["verified"]
        is True
    )


def test_seed_fresh_database_is_idempotent(seed_research_study__patched_crud):
    import database.db_schemas as db_schemas

    session = seed_research_study__FakeSession()
    request = fresh_db___request()

    first = seed_research_study__seed.seed_fresh_database(session, request)
    second = seed_research_study__seed.seed_fresh_database(session, request)

    assert first.release_id == second.release_id
    assert first.study_id == second.study_id
    assert first.revision_id == second.revision_id
    assert second.distribution_verified is True

    assert len(session.rows_of(research_schemas.AgentRelease)) == 1
    assert len(session.rows_of(db_schemas.AgentProfile)) == 1
    revisions = session.rows_of(research_schemas.StudyRevision)
    assert len(revisions) == 2  # one draft + one published, no duplicates
