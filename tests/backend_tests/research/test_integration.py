"""Consolidated research tests (see individual section headers).

Merged from smaller modules; test functions and assertions are unchanged.
"""

from __future__ import annotations

import json
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.bootstrap import (
    BOOTSTRAP_SIGNING_SECRET,
    EnvironmentReport,
    ExposureRequest,
    ResearchSessionRequest,
    _PersistentSessionFactory,
    create_exposure,
)
from backend.routers.research.bootstrap import (
    create_research_session as assignment_bootstrap__create_research_session,
)
from backend.routers.research.sessions import (
    CloseSessionRequest,
    CreateSessionRequest,
    HeartbeatRequest,
    SessionSummaryRequest,
    close_research_session,
    get_research_session,
    heartbeat,
)
from backend.routers.research.sessions import (
    create_research_session as research_sessions__create_research_session,
)
from research.analysis.operations import (
    HealthInputs,
    HealthSignalState,
    HealthThresholds,
    KillSwitchRegistry,
    KillSwitchScope,
    KillSwitchScopeKind,
    OperationalHealthV1,
    OperationsReasonCode,
    ReleaseDecision,
    ReleaseEvidenceV1,
    RetentionVerificationStatus,
    aggregate_health,
    engage_kill_switch,
    evaluate_release,
    health_summary,
    is_engaged,
    kill_switch_check,
    kill_switch_issue,
    record_decision,
    release_kill_switch,
    verify_deletion,
)
from research.analysis.operations import store as operations_store
from research.analysis.operations.enums import OperationsReasonCode
from research.analysis.read_models.service import build_derived_metrics, build_telemetry_coverage
from research.compatibility.enums import CompatibilityDecision
from research.compatibility.models import CompatibilityResult
from research.participants import identity as participants_identity
from research.participants.enums import (
    EnrollmentStatus,
    IdentityReasonCode,
)
from research.participants.models import (
    Enrollment,
    Participant,
    PseudonymousRecord,
    ResearchEligibility,
)
from research.runtime.assignment import store as assignment_store
from research.runtime.assignment.enums import (
    AllocationOutcome,
    AssignmentReasonCode,
    ExposureOutcome,
    ExposureReasonCode,
)
from research.runtime.assignment.exposure import record_exposure
from research.runtime.assignment.models import AssignmentV1, ExposureEnvironment, ExposureV1
from research.runtime.assignment.service import allocate
from research.runtime.bootstrap import store as bootstrap_store
from research.runtime.bootstrap.capability import issue_capability, verify_capability
from research.runtime.bootstrap.models import (
    BootstrapAgentProfile,
    BootstrapIssue,
    BootstrapManifestV1,
    BootstrapOutcome,
    BootstrapReasonCode,
    BootstrapResult,
    CapabilityReasonCode,
    ManifestReasonCode,
    SessionCapability,
)
from research.runtime.bootstrap.service import (
    BootstrapSigningContext,
    EphemeralSessionFactory,
    compose_bootstrap,
)
from research.runtime.bootstrap.signer import verify_manifest
from research.runtime.sessions import service as sessions
from research.runtime.sessions import store as session_store
from research.runtime.sessions.enums import (
    AgentRunOutcome,
    CloseReason,
    SessionReasonCode,
    SessionState,
)
from research.runtime.sessions.models import AgentRunV1, ResearchSessionV1, SessionPolicyV1
from research.runtime.sessions.service import (
    can_transition,
    close,
    end,
    end_agent_run,
    expire_if_idle,
    go_offline,
    on_agent_run_crashed,
    on_qualifying_activity,
    open_session,
    policy_ref,
    record_activity,
    recover,
    resume,
    revoke,
    session_policy_from_revision,
    start_agent_run,
    suspend,
)
from research.study.agents.enums import (
    DistributionMode,
    DistributionSourceType,
    QualificationStatus,
)
from research.study.agents.models import AdapterRef, AgentReleaseV1, DistributionArtifact
from research.study.agents.registry import AgentRegistry
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.protocol.canonical import protocol_digest
from research.study.protocol.enums import RetentionAction, RevisionStatus
from research.study.protocol.models import ResolvedDistribution, StudyProtocolV1
from research.study.protocol.publication import RevisionLineage, StudyRevision, publish_revision
from research.telemetry import EventBuilder
from research.telemetry.enums import CoverageState
from research.telemetry.ingestion import (
    EventDisposition,
    FakeIngestionStore,
    IngestionReasonCode,
    TelemetryBatchRequestV1,
    ingest_batch,
)
from research.telemetry.models import Coverage, EventMetrics
from research.telemetry.normalization import materialize_candidate
from research.telemetry.normalization.generic_acp import GenericAcpNormalizer
from research.telemetry.privacy import PrivacyPolicy, filter_event
from research.telemetry.projections.service import coverage_by_family, derive_usage_metric

# --------------------------------------------------------------------------
# test_synthetic_rehearsal
# --------------------------------------------------------------------------
# End-to-end synthetic rehearsal of the implemented research stack (Issue 13).
#
# This runs the real domain services (protocol publication, agent registry,
# identity/consent, assignment, bootstrap/capability, exposure, sessions, telemetry
# normalization, ingestion, projections, read models, exports) with in-memory
# fakes for stores and no network. It then injects offline, crash, revocation and
# withdrawal/retention failures and asserts the observable outcomes.
synthetic_rehearsal__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

# Fixed instant for the assignment/bootstrap fixtures (restored after the
# consent-removal cleanup removed the block that defined it).
assignment_bootstrap__NOW = synthetic_rehearsal__NOW

# Assignment/bootstrap fixture constants (restored after the consent-removal
# cleanup removed the block that defined them).
assignment_bootstrap__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research"
assignment_bootstrap__PROTOCOL_FIXTURE = assignment_bootstrap__FIXTURE_DIR / "protocol" / "approved_study_protocol_v1.json"
assignment_bootstrap__BOOTSTRAP_FIXTURE = assignment_bootstrap__FIXTURE_DIR / "bootstrap"
assignment_bootstrap__SECRET = "fixture-bootstrap-secret"
assignment_bootstrap__PLATFORM = ("macOS", "aarch64")
assignment_bootstrap__SYNTHETIC_ACCOUNT = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
assignment_bootstrap__SYNTHETIC_EMAIL = "synthetic-participant@example.invalid"
assignment_bootstrap__CANARY_API_KEY = "CANARY-API-KEY-DO-NOT-STORE"
assignment_bootstrap__CANARY_COMMAND = "canary-agent-command --do-not-leak"
synthetic_rehearsal__SECRET = "rehearsal-fixture-secret"
synthetic_rehearsal__PLATFORM = ("macOS", "aarch64")
synthetic_rehearsal__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research"
synthetic_rehearsal__PROTOCOL_FIXTURE = synthetic_rehearsal__FIXTURE_DIR / "protocol" / "approved_study_protocol_v1.json"
synthetic_rehearsal__ACP_FIXTURE = synthetic_rehearsal__FIXTURE_DIR / "telemetry" / "acp_transcript_excerpt.json"

synthetic_rehearsal__CANARY_ACCOUNT = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
synthetic_rehearsal__CANARY_EMAIL = "rehearsal-canary@example.invalid"
synthetic_rehearsal__CANARY_PROMPT = "CANARY-REHEARSAL-PROMPT"
synthetic_rehearsal__CANARY_REASONING = "CANARY-REHEARSAL-REASONING"
synthetic_rehearsal__CANARY_SOURCE = "CANARY-REHEARSAL-SOURCE"
synthetic_rehearsal__CANARY_SECRET = "sk-CANARY-REHEARSAL-0000000001"
synthetic_rehearsal__CANARY_TOKEN = "sess-CANARY-REHEARSAL-0000000002"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def synthetic_rehearsal___protocol() -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(json.loads(synthetic_rehearsal__PROTOCOL_FIXTURE.read_text()))


def synthetic_rehearsal___participant() -> Participant:
    return Participant(participant_id=uuid.uuid4(), account_id=synthetic_rehearsal__CANARY_ACCOUNT, created_at=synthetic_rehearsal__NOW)


def synthetic_rehearsal___eligible() -> ResearchEligibility:
    return ResearchEligibility(
        eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=synthetic_rehearsal__NOW
    )


def synthetic_rehearsal___release_for(protocol: StudyProtocolV1, condition_id: str) -> AgentReleaseV1:
    pin = next(c for c in protocol.conditions if c.condition_id == condition_id).resolved_distribution
    return AgentReleaseV1(
        agent_id=pin.agent_id,
        release_id=pin.release_id or "rel-1",
        version=pin.version or "1.0.0",
        source_type=DistributionSourceType.EXTERNAL_REGISTRY,
        source_manifest_digest="sha256:" + "1" * 64,
        artifacts=[
            DistributionArtifact(
                os=synthetic_rehearsal__PLATFORM[0],
                arch=synthetic_rehearsal__PLATFORM[1],
                path="artifact.bin",
                sha256=pin.artifact_digest or "sha256:" + "a" * 64,
                size=1,
            )
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter", version="0.4.0", digest="sha256:" + "d" * 64
        ),
        qualification_status=QualificationStatus.QUALIFIED,
    )


def synthetic_rehearsal___event(
    revision,
    enrollment,
    session,
    *,
    seq: int,
    event_type: str,
    usage: int | None = None,
    usage_state: CoverageState = CoverageState.UNAVAILABLE,
    payload: dict | None = None,
    occurred_at: datetime = synthetic_rehearsal__NOW,
):
    return EventBuilder().build(
        emitter_id="acp-proxy",
        event_type=event_type,
        source="acp",
        occurred_at=occurred_at,
        normalizer_version="generic-acp-v1",
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        payload=payload if payload is not None else {"tool_name": "read"},
        metrics=EventMetrics(
            usage_tokens=usage,
            usage_capability=Coverage(state=usage_state, capability="usage"),
        ),
        emitter_sequence=seq,
        event_id=uuid.uuid4(),
    )


def synthetic_rehearsal___acp_tool_event(revision, enrollment, session, *, seq: int):
    """Materialize a real ACP tool_call transcript message into canonical form."""
    normalizer = GenericAcpNormalizer()
    message = json.loads(synthetic_rehearsal__ACP_FIXTURE.read_text())[2]
    result = normalizer.normalize(message)
    candidate = next(
        c for c in result.candidates if c.event_type.value == "tool.completed"
    )
    return materialize_candidate(
        EventBuilder(),
        candidate,
        result,
        emitter_id="acp-proxy",
        occurred_at=synthetic_rehearsal__NOW,
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        emitter_sequence=seq,
        event_id=uuid.uuid4(),
    )


def assignment_bootstrap___protocol_data() -> dict:
    return json.loads(assignment_bootstrap__PROTOCOL_FIXTURE.read_text())


def assignment_bootstrap___protocol(strategy: str = "DETERMINISTIC_HASH") -> StudyProtocolV1:
    data = assignment_bootstrap___protocol_data()
    data["assignment"]["strategy"] = strategy
    return StudyProtocolV1.model_validate(data)


def assignment_bootstrap___revision(
    protocol: StudyProtocolV1, *, status: RevisionStatus = RevisionStatus.PUBLISHED
) -> StudyRevision:
    return StudyRevision(
        revision_id=uuid.uuid4(),
        study_id=protocol.study_id,
        revision_number=1,
        status=status,
        protocol_json=protocol.model_dump(mode="json"),
        protocol_digest=protocol_digest(protocol),
        published_at=assignment_bootstrap__NOW,
        created_at=assignment_bootstrap__NOW,
    )


def assignment_bootstrap___enrollment(
    revision: StudyRevision,
    *,
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE,
    enrolled_at: datetime = assignment_bootstrap__NOW,
) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=revision.study_id,
        study_revision_id=revision.revision_id,
        participant_code="p_synthetic",
        status=status,
        eligibility=ResearchEligibility(
            eligible=True,
            reasons=[IdentityReasonCode.ELIGIBLE],
            evaluated_at=assignment_bootstrap__NOW,
        ),
        enrolled_at=enrolled_at,
        updated_at=assignment_bootstrap__NOW,
    )


def assignment_bootstrap___condition(protocol: StudyProtocolV1, condition_id: str):
    return next(c for c in protocol.conditions if c.condition_id == condition_id)


def assignment_bootstrap___release_for(
    protocol: StudyProtocolV1,
    condition_id: str,
    *,
    platform: tuple[str, str] = assignment_bootstrap__PLATFORM,
    qualification: QualificationStatus = QualificationStatus.QUALIFIED,
    artifact_digest: str | None = None,
) -> AgentReleaseV1:
    condition = assignment_bootstrap___condition(protocol, condition_id)
    pin = condition.resolved_distribution
    return AgentReleaseV1(
        agent_id=pin.agent_id,
        release_id=pin.release_id or "rel-x",
        version=pin.version or "1.0.0",
        source_type=DistributionSourceType.EXTERNAL_REGISTRY,
        source_manifest_digest="sha256:" + "1" * 64,
        artifacts=[
            DistributionArtifact(
                os=platform[0],
                arch=platform[1],
                path="artifact.bin",
                sha256=artifact_digest or pin.artifact_digest or "sha256:" + "a" * 64,
                size=1,
            )
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter",
            version=condition.adapter_version or "0.4.0",
            digest="sha256:" + "d" * 64,
        ),
        qualification_status=qualification,
    )


def assignment_bootstrap___signer(ttl: int = 900) -> BootstrapSigningContext:
    return BootstrapSigningContext(secret=assignment_bootstrap__SECRET, capability_ttl_seconds=ttl)


def assignment_bootstrap___compatible() -> CompatibilityResult:
    return CompatibilityResult(decision=CompatibilityDecision.COMPATIBLE)


def assignment_bootstrap___compose(
    enrollment: Enrollment,
    revision: StudyRevision,
    assignment: AssignmentV1,
    release: AgentReleaseV1,
    **overrides,
):
    params = {
        "compatibility_ref": "receipt-synthetic",
        "session_factory": EphemeralSessionFactory(),
        "signer": assignment_bootstrap___signer(),
        "now": assignment_bootstrap__NOW,
        "compatibility_result": assignment_bootstrap___compatible(),
        "platform": assignment_bootstrap__PLATFORM,
    }
    params.update(overrides)
    return compose_bootstrap(enrollment, revision, assignment, release, **params)


def assignment_bootstrap___allocated(protocol: StudyProtocolV1 | None = None):
    protocol = protocol or assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    result = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW)
    assert result.assignment is not None
    return enrollment, revision, result.assignment


def assignment_bootstrap___admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def assignment_bootstrap___participant(account_id: uuid.UUID = assignment_bootstrap__SYNTHETIC_ACCOUNT) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=account_id, is_admin=False, email=assignment_bootstrap__SYNTHETIC_EMAIL, name="Participant"
    )


# ---------------------------------------------------------------------------
# Allocation: sticky, scoped, weighted
# ---------------------------------------------------------------------------


def test_allocation_is_sticky_and_never_re_randomizes():
    protocol = assignment_bootstrap___protocol("WEIGHTED_RANDOM")
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)

    first = allocate(enrollment, revision, rng=random.Random(1), now=assignment_bootstrap__NOW)
    assert first.outcome == AllocationOutcome.CREATED
    assert first.reason == AssignmentReasonCode.WEIGHTED_DRAW

    second = allocate(
        enrollment, revision, existing=first.assignment, rng=random.Random(999), now=assignment_bootstrap__NOW
    )
    assert second.outcome == AllocationOutcome.EXISTING
    assert second.reason == AssignmentReasonCode.STICKY_EXISTING
    assert second.assignment.condition_id == first.assignment.condition_id
    assert second.assignment.assignment_id == first.assignment.assignment_id


def test_weighted_allocation_distribution_within_tolerance():
    protocol = assignment_bootstrap___protocol("WEIGHTED_RANDOM")
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)

    rng = random.Random(20260915)
    draws = 4000
    counts = Counter(
        allocate(enrollment, revision, rng=rng, now=assignment_bootstrap__NOW).assignment.condition_id
        for _ in range(draws)
    )
    # Fixture weights: control 1.0, treatment 3.0 -> 0.25 / 0.75.
    treatment_fraction = counts["treatment"] / draws
    assert 0.72 < treatment_fraction < 0.78
    assert counts["control"] > 0
    assert counts["treatment"] > 0


def test_deterministic_hash_allocation_is_stable_for_the_enrollment():
    protocol = assignment_bootstrap___protocol("DETERMINISTIC_HASH")
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)

    first = allocate(enrollment, revision, now=assignment_bootstrap__NOW)
    second = allocate(enrollment, revision, now=assignment_bootstrap__NOW)
    assert first.reason == AssignmentReasonCode.DETERMINISTIC_HASH
    assert first.assignment.condition_id == second.assignment.condition_id


def test_assignment_is_scoped_to_enrollment_and_revision():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    first = assignment_bootstrap___enrollment(revision)
    second = assignment_bootstrap___enrollment(revision)

    a = allocate(first, revision, rng=random.Random(1), now=assignment_bootstrap__NOW).assignment
    b = allocate(second, revision, rng=random.Random(1), now=assignment_bootstrap__NOW).assignment

    assert a.enrollment_id == first.enrollment_id
    assert a.study_revision_id == revision.revision_id
    assert a.protocol_digest == revision.protocol_digest
    assert a.assignment_id != b.assignment_id


def test_later_revision_does_not_rebucket_existing_enrollment():
    protocol = assignment_bootstrap___protocol("WEIGHTED_RANDOM")
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    existing = allocate(enrollment, revision, rng=random.Random(1), now=assignment_bootstrap__NOW).assignment

    # A weight change is a *new* revision id; the enrollment stays bound to v1.
    changed = assignment_bootstrap___protocol("WEIGHTED_RANDOM")
    changed.conditions[0].weight = 9.0
    changed.conditions[1].weight = 1.0
    revision_v2 = assignment_bootstrap___revision(changed)

    result = allocate(enrollment, revision_v2, existing=existing, rng=random.Random(2), now=assignment_bootstrap__NOW)
    assert result.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert result.reason == AssignmentReasonCode.REVISION_MISMATCH

    # Re-running against the original revision keeps the original condition.
    again = allocate(
        enrollment, revision, existing=existing, rng=random.Random(3), now=assignment_bootstrap__NOW
    )
    assert again.outcome == AllocationOutcome.EXISTING
    assert again.assignment.condition_id == existing.condition_id


def test_non_active_enrollment_is_blocked_with_a_typed_reason_not_a_default_arm():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)

    withdrawn = assignment_bootstrap___enrollment(revision, status=EnrollmentStatus.COMPLETED)
    result = allocate(withdrawn, revision, now=assignment_bootstrap__NOW)
    assert result.outcome == AllocationOutcome.INELIGIBLE
    assert result.reason == AssignmentReasonCode.ENROLLMENT_NOT_ACTIVE
    assert result.assignment is None


def test_unpublished_revision_and_unknown_strategy_fail_closed():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol, status=RevisionStatus.DRAFT)
    enrollment = assignment_bootstrap___enrollment(revision)
    result = allocate(enrollment, revision, now=assignment_bootstrap__NOW)
    assert result.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert result.reason == AssignmentReasonCode.REVISION_NOT_PUBLISHED

    unknown = assignment_bootstrap___protocol("MAGIC")
    revision = assignment_bootstrap___revision(unknown)
    enrollment = assignment_bootstrap___enrollment(revision)
    result = allocate(enrollment, revision, now=assignment_bootstrap__NOW)
    assert result.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert result.reason == AssignmentReasonCode.UNKNOWN_STRATEGY


def test_stratified_allocation_is_unsupported_without_stratum_values():
    protocol = assignment_bootstrap___protocol("STRATIFIED")
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    result = allocate(enrollment, revision, now=assignment_bootstrap__NOW)
    assert result.outcome == AllocationOutcome.INSUFFICIENT_EVIDENCE
    assert result.reason == AssignmentReasonCode.STRATIFIED_UNSUPPORTED


# ---------------------------------------------------------------------------
# Exposure receipts
# ---------------------------------------------------------------------------


def test_exposure_has_distinct_id_and_timestamp_from_assignment():
    _, _, assignment = assignment_bootstrap___allocated()
    result = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.STARTED,
        "exp-key-1",
        agent_release_id="rel-1",
        now=assignment_bootstrap__NOW,
    )
    assert result.accepted is True
    assert result.is_exposure is True
    assert result.exposure.exposure_id != assignment.assignment_id
    assert result.exposure.assignment_id == assignment.assignment_id
    assert result.exposure.started_at == assignment_bootstrap__NOW


def test_repeated_exposure_preserves_first_terminal_outcome_and_audits_retry():
    _, _, assignment = assignment_bootstrap___allocated()
    first = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.SUCCEEDED,
        "exp-key-1",
        now=assignment_bootstrap__NOW,
    )
    replay = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.SUCCEEDED,
        "exp-key-1",
        existing=first.exposure,
        now=assignment_bootstrap__NOW + timedelta(seconds=30),
    )
    assert replay.accepted is True
    assert replay.reused is True
    assert replay.reason == ExposureReasonCode.IDEMPOTENT_REPLAY
    assert replay.exposure.exposure_id == first.exposure.exposure_id
    assert replay.exposure.started_at == first.exposure.started_at
    assert replay.audit

    conflict = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.SUCCEEDED,
        "exp-key-2",
        existing=first.exposure,
        now=assignment_bootstrap__NOW,
    )
    assert conflict.accepted is False
    assert conflict.reason == ExposureReasonCode.CONFLICT


def test_launch_failure_records_a_non_exposure_with_evidence():
    _, _, assignment = assignment_bootstrap___allocated()
    result = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.RUNTIME_UNAVAILABLE,
        "exp-key-fail",
        evidence_digest="sha256:" + "e" * 64,
        now=assignment_bootstrap__NOW,
    )
    assert result.accepted is True
    assert result.is_exposure is False
    assert result.exposure.is_exposure is False
    assert result.exposure.evidence_digest == "sha256:" + "e" * 64


def test_exposure_requires_an_idempotency_key():
    _, _, assignment = assignment_bootstrap___allocated()
    result = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.STARTED,
        "   ",
        now=assignment_bootstrap__NOW,
    )
    assert result.accepted is False
    assert result.reason == ExposureReasonCode.IDEMPOTENCY_KEY_REQUIRED


# ---------------------------------------------------------------------------
# Bootstrap manifest: issuance, projection, secrecy
# ---------------------------------------------------------------------------


def assignment_bootstrap___issued_manifest():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(
        enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW
    ).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)
    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)
    assert result.manifest is not None, result.issue
    return enrollment, revision, assignment, release, result.manifest


def test_manifest_is_signed_and_contains_every_policy():
    _, _, assignment, release, manifest = assignment_bootstrap___issued_manifest()

    verification = verify_manifest(manifest, assignment_bootstrap__SECRET)
    assert verification.ok is True
    assert verification.reason == ManifestReasonCode.OK
    assert manifest.manifest_digest
    assert manifest.signature
    assert manifest.assignment.assignment_id == assignment.assignment_id
    assert manifest.agent_release.artifact_digest == release.artifacts[0].sha256

    policies = manifest.policies
    assert policies.telemetry_policy is not None
    assert policies.privacy_policy is not None
    assert policies.session_policy is not None
    # Consent has no per-study document: the manifest carries no consent digest.
    assert policies.consent_policy_digest is None


def test_manifest_tampering_fails_with_a_typed_reason():
    _, _, _, _, manifest = assignment_bootstrap___issued_manifest()
    tampered = manifest.model_copy(update={"revision_id": uuid.uuid4()})
    verification = verify_manifest(tampered, assignment_bootstrap__SECRET)
    assert verification.ok is False
    assert verification.reason == ManifestReasonCode.MANIFEST_DIGEST_MISMATCH

    wrong_secret = verify_manifest(manifest, "not-the-secret")
    assert wrong_secret.ok is False
    assert wrong_secret.reason == ManifestReasonCode.MANIFEST_SIGNATURE_MISMATCH


def test_manifest_contains_no_login_identity_or_secret_canary():
    enrollment, _, _, _, manifest = assignment_bootstrap___issued_manifest()
    serialized = manifest.model_dump_json()
    lowered = serialized.lower()

    for forbidden in (
        str(assignment_bootstrap__SYNTHETIC_ACCOUNT),
        assignment_bootstrap__SYNTHETIC_EMAIL,
        assignment_bootstrap__CANARY_API_KEY,
        assignment_bootstrap__CANARY_COMMAND,
        "user_id",
        "account_id",
        "api_key",
        "provider_token",
        "password",
    ):
        assert forbidden.lower() not in lowered

    # Null optional policies mean inherited/disabled, never a fabricated value.
    assert manifest.compatibility.receipt_ref is not None
    assert manifest.research_session.research_session_id


def test_valid_manifest_fixture_verifies_with_the_fixture_secret():
    manifest = BootstrapManifestV1.model_validate(
        json.loads((assignment_bootstrap__BOOTSTRAP_FIXTURE / "manifest_valid.json").read_text())
    )
    assert verify_manifest(manifest, "fixture-bootstrap-secret").ok is True
    assert manifest.manifest_version == "1"


def test_manifest_projects_a_linked_agent_profile_without_provider_secrets():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(
        enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW
    ).assignment
    assert assignment is not None
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)
    profile = BootstrapAgentProfile(
        profile_id=uuid.uuid4(),
        name="arm-a",
        framework_version="code4me2-agent",
        model="qwen2.5-coder:7b",
        base_url="http://localhost:11434/v1",
        temperature=0.2,
    )

    result = assignment_bootstrap___compose(
        enrollment, revision, assignment, release, agent_profile=profile
    )

    assert result.manifest is not None
    assert result.manifest.agent_profile == profile
    # The provider identity is projected, but never the credential or its
    # env-var reference: the server-side relay is the only credential path.
    serialized = result.manifest.model_dump_json().lower()
    for forbidden in ("api_key", "secret", "password", "token"):
        assert forbidden not in serialized
    assert verify_manifest(result.manifest, assignment_bootstrap__SECRET).ok is True


def test_manifest_agent_profile_is_null_when_the_condition_has_no_link():
    _, _, _, _, manifest = assignment_bootstrap___issued_manifest()
    assert manifest.agent_profile is None


# ---------------------------------------------------------------------------
# Bootstrap blocking: lifecycle, compatibility, no fallback
# ---------------------------------------------------------------------------


def test_incompatible_environment_is_blocked_with_a_typed_reason():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)
    result = assignment_bootstrap___compose(
        enrollment,
        revision,
        assignment,
        release,
        compatibility_result=CompatibilityResult(
            decision=CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
        ),
    )
    assert result.manifest is None
    assert result.reason.value == "INCOMPATIBLE_ENVIRONMENT"


def test_missing_compatibility_fails_closed():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)
    result = assignment_bootstrap___compose(enrollment, revision, assignment, release, compatibility_result=None)
    assert result.manifest is None
    assert result.reason.value == "COMPATIBILITY_MISSING"


def test_missing_platform_artifact_never_falls_back():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id, platform=("Linux", "x86_64"))
    result = assignment_bootstrap___compose(
        enrollment, revision, assignment, release, platform=("macOS", "aarch64")
    )
    assert result.manifest is None
    assert result.reason.value == "ARTIFACT_UNAVAILABLE"


def assignment_bootstrap___release_with_two_platforms(
    protocol: StudyProtocolV1,
    condition_id: str,
    *,
    macos_digest: str,
    linux_digest: str,
) -> AgentReleaseV1:
    condition = assignment_bootstrap___condition(protocol, condition_id)
    pin = condition.resolved_distribution
    return AgentReleaseV1(
        agent_id=pin.agent_id,
        release_id=pin.release_id or "rel-x",
        version=pin.version or "1.0.0",
        source_type=DistributionSourceType.EXTERNAL_REGISTRY,
        source_manifest_digest="sha256:" + "1" * 64,
        artifacts=[
            DistributionArtifact(
                os="macOS", arch="aarch64", path="macos.bin", sha256=macos_digest, size=1
            ),
            DistributionArtifact(
                os="Linux", arch="x86_64", path="linux.bin", sha256=linux_digest, size=1
            ),
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter",
            version=condition.adapter_version or "0.4.0",
            digest="sha256:" + "d" * 64,
        ),
        qualification_status=QualificationStatus.QUALIFIED,
    )


def test_manifest_pins_the_platform_artifact_digest_not_another_platform():
    """The digest that lands in the manifest is the participant's os/arch artifact."""
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    pin = assignment_bootstrap___condition(protocol, assignment.condition_id).resolved_distribution
    other_platform_digest = "sha256:" + "e" * 64
    release = assignment_bootstrap___release_with_two_platforms(
        protocol,
        assignment.condition_id,
        macos_digest=pin.artifact_digest,
        linux_digest=other_platform_digest,
    )

    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)

    assert result.manifest is not None, result.issue
    manifest = result.manifest
    assert manifest.agent_release.artifact_digest == pin.artifact_digest
    assert manifest.agent_release.artifact_digest != other_platform_digest
    assert manifest.agent_release.agent_id == release.agent_id
    assert manifest.agent_release.release_id == release.release_id
    assert manifest.agent_release.adapter_version == release.adapter.version


def test_compose_bootstrap_projects_the_byoa_mode_and_identity():
    """A BYOA distribution yields a manifest without a digest but with identity."""
    protocol = assignment_bootstrap___protocol()
    condition = assignment_bootstrap___condition(protocol, protocol.conditions[0].condition_id)
    protocol = protocol.model_copy(
        update={
            "conditions": [
                c.model_copy(
                    update={
                        "resolved_distribution": ResolvedDistribution(
                            distribution_id=c.distribution_id,
                            distribution_mode=DistributionMode.BYOA_EXTERNAL.value,
                            release_id="rel-byoa",
                            agent_id="goose",
                            version="0.9.0",
                            agent_package="goose",
                            agent_command="goose",
                            agent_command_args=["acp"],
                            verified=False,
                        )
                    }
                )
                for c in protocol.conditions
            ]
        }
    )
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = AgentReleaseV1(
        agent_id="goose",
        release_id="rel-byoa",
        version="0.9.0",
        source_type=DistributionSourceType.EXTERNAL_REGISTRY,
        source_manifest_digest="sha256:" + "3" * 64,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_command="goose",
        agent_command_args=["acp"],
        agent_package="goose",
        adapter=AdapterRef(
            adapter_id="acp-adapter",
            version=condition.adapter_version or "0.4.0",
            digest="sha256:" + "d" * 64,
        ),
        qualification_status=QualificationStatus.QUALIFIED,
    )

    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)

    assert result.manifest is not None, result.issue
    agent_release = result.manifest.agent_release
    assert agent_release.distribution_mode == DistributionMode.BYOA_EXTERNAL.value
    assert agent_release.artifact_digest == ""
    assert agent_release.agent_command == "goose"
    assert agent_release.agent_command_args == ["acp"]
    assert agent_release.agent_package == "goose"


def test_registry_resolver_pins_the_participant_platform_artifact_digest():
    """A registry-resolved release pins exactly the platform artifact digest."""
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    pin = assignment_bootstrap___condition(protocol, assignment.condition_id).resolved_distribution
    other_platform_digest = "sha256:" + "f" * 64
    release = assignment_bootstrap___release_with_two_platforms(
        protocol,
        assignment.condition_id,
        macos_digest=pin.artifact_digest,
        linux_digest=other_platform_digest,
    )

    registry = AgentRegistry()
    registry.register_release(release)
    resolver = RegistryReleaseResolver(registry, platform=assignment_bootstrap__PLATFORM)
    result = assignment_bootstrap___compose(
        enrollment, revision, assignment, release, release_resolver=resolver
    )

    assert result.manifest is not None, result.issue
    assert result.manifest.agent_release.artifact_digest == pin.artifact_digest
    assert result.manifest.agent_release.artifact_digest != other_platform_digest


def test_platform_artifact_mismatch_against_the_condition_pin_is_blocked():
    """A digest that disagrees with the condition pin blocks; nothing is issued."""
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(
        protocol, assignment.condition_id, artifact_digest="sha256:" + "f" * 64
    )

    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)

    assert result.manifest is None
    assert result.reason.value == "ARTIFACT_MISMATCH"
    assert result.issue is not None
    assert result.issue.field == "resolved_distribution.artifact_digest"


def test_unqualified_release_is_blocked():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(
        protocol,
        assignment.condition_id,
        qualification=QualificationStatus.DRAFT,
    )
    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)
    assert result.manifest is None
    assert result.reason.value == "RELEASE_NOT_QUALIFIED"




def test_closed_study_window_is_blocked():
    data = assignment_bootstrap___protocol_data()
    data["schedule"] = {
        "kind": "FIXED",
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-06-01T00:00:00Z",
    }
    protocol = StudyProtocolV1.model_validate(data)
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)
    result = assignment_bootstrap___compose(enrollment, revision, assignment, release)
    assert result.manifest is None
    assert result.reason.value == "STUDY_CLOSED"


# ---------------------------------------------------------------------------
# Session capability
# ---------------------------------------------------------------------------


def assignment_bootstrap___capability(**overrides) -> SessionCapability:
    params = {
        "audience": "research-runtime",
        "scope": ["telemetry:write", "session:heartbeat"],
        "ttl_seconds": 600,
        "revocation_epoch": 0,
        "secret": assignment_bootstrap__SECRET,
        "now": assignment_bootstrap__NOW,
        "enrollment_id": uuid.uuid4(),
        "research_session_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
    }
    params.update(overrides)
    return issue_capability(**params)


def test_capability_verifies_and_is_scoped():
    capability = assignment_bootstrap___capability()
    result = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
    )
    assert result.ok is True
    assert result.reason == CapabilityReasonCode.OK


def test_capability_expiry_wrong_audience_missing_scope_and_revocation():
    capability = assignment_bootstrap___capability()

    expired = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW + timedelta(seconds=601),
    )
    assert expired.reason == CapabilityReasonCode.EXPIRED

    wrong_audience = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="other-audience",
        expected_scope=[],
        now=assignment_bootstrap__NOW,
    )
    assert wrong_audience.reason == CapabilityReasonCode.WRONG_AUDIENCE

    missing_scope = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write", "admin:everything"],
        now=assignment_bootstrap__NOW,
    )
    assert missing_scope.reason == CapabilityReasonCode.SCOPE_MISSING

    revoked = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
        current_revocation_epoch=3,
    )
    assert revoked.reason == CapabilityReasonCode.REVOKED


def test_capability_not_yet_valid_and_tampered_signature():
    capability = assignment_bootstrap___capability()

    not_yet = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW - timedelta(seconds=1),
    )
    assert not_yet.reason == CapabilityReasonCode.NOT_YET_VALID

    tampered = capability.model_copy(update={"scope": ["admin:everything"]})
    mismatch = verify_capability(
        tampered,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=[],
        now=assignment_bootstrap__NOW,
    )
    assert mismatch.reason == CapabilityReasonCode.SIGNATURE_MISMATCH


def test_capability_is_bound_to_its_subject():
    subject = dict(
        enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
    )
    capability = assignment_bootstrap___capability(**subject)

    assert capability.enrollment_id == subject["enrollment_id"]
    assert capability.research_session_id == subject["research_session_id"]
    assert capability.revision_id == subject["revision_id"]

    # A cryptographically valid capability for a different subject is refused.
    enrollment_mismatch = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
        expected_enrollment_id=uuid.uuid4(),
        expected_research_session_id=subject["research_session_id"],
        expected_revision_id=subject["revision_id"],
    )
    assert enrollment_mismatch.reason == CapabilityReasonCode.SUBJECT_MISMATCH

    session_mismatch = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
        expected_enrollment_id=subject["enrollment_id"],
        expected_research_session_id=uuid.uuid4(),
        expected_revision_id=subject["revision_id"],
    )
    assert session_mismatch.reason == CapabilityReasonCode.SESSION_MISMATCH

    revision_mismatch = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
        expected_enrollment_id=subject["enrollment_id"],
        expected_research_session_id=subject["research_session_id"],
        expected_revision_id=uuid.uuid4(),
    )
    assert revision_mismatch.reason == CapabilityReasonCode.REVISION_MISMATCH

    matching = verify_capability(
        capability,
        assignment_bootstrap__SECRET,
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
        expected_enrollment_id=subject["enrollment_id"],
        expected_research_session_id=subject["research_session_id"],
        expected_revision_id=subject["revision_id"],
    )
    assert matching.ok is True


def test_capability_issuance_and_verification_require_a_signing_secret():
    with pytest.raises(ValueError, match="signing secret"):
        assignment_bootstrap___capability(secret="")

    capability = assignment_bootstrap___capability()
    missing = verify_capability(
        capability,
        "",
        expected_audience="research-runtime",
        expected_scope=["telemetry:write"],
        now=assignment_bootstrap__NOW,
    )
    assert missing.ok is False
    assert missing.reason == CapabilityReasonCode.SIGNING_SECRET_MISSING


def test_compose_bootstrap_requires_a_signing_secret():
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(
        enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW
    ).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)

    result = assignment_bootstrap___compose(
        enrollment,
        revision,
        assignment,
        release,
        signer=BootstrapSigningContext(secret="", capability_ttl_seconds=900),
    )
    assert result.manifest is None
    assert result.reason == BootstrapReasonCode.SIGNING_SECRET_MISSING


# ---------------------------------------------------------------------------
# Persistence helpers (MagicMock session)
# ---------------------------------------------------------------------------


def test_create_assignment_adds_expected_row():
    _, _, assignment = assignment_bootstrap___allocated()
    session = MagicMock()

    row = assignment_store.create_assignment(session, assignment)

    added = session.add.call_args.args[0]
    assert added.condition_id == assignment.condition_id
    assert added.enrollment_id == assignment.enrollment_id
    assert added.study_revision_id == assignment.study_revision_id
    assert row.assignment_id == assignment.assignment_id
    session.commit.assert_called_once()
    session.refresh.assert_called_once()


def test_insert_exposure_adds_expected_row():
    _, _, assignment = assignment_bootstrap___allocated()
    exposure = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.STARTED,
        "exp-key-store",
        agent_release_id="rel-1",
        now=assignment_bootstrap__NOW,
    ).exposure
    session = MagicMock()

    row = assignment_store.insert_exposure(session, exposure)

    added = session.add.call_args.args[0]
    assert added.idempotency_key == "exp-key-store"
    assert added.outcome == "STARTED"
    assert row.exposure_id == exposure.exposure_id
    session.commit.assert_called_once()


def test_row_round_trips_for_assignment_and_exposure():
    _, _, assignment = assignment_bootstrap___allocated()
    assignment_row = SimpleNamespace(
        assignment_id=assignment.assignment_id,
        enrollment_id=assignment.enrollment_id,
        study_revision_id=assignment.study_revision_id,
        condition_id=assignment.condition_id,
        strategy=assignment.strategy,
        randomization_epoch=assignment.randomization_epoch,
        protocol_digest=assignment.protocol_digest,
        assigned_at=assignment.assigned_at,
    )
    restored = assignment_store.row_to_assignment(assignment_row)
    assert restored.assignment_id == assignment.assignment_id
    assert restored.condition_id == assignment.condition_id

    exposure = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.FAILED,
        "exp-key-round",
        evidence_digest="sha256:" + "e" * 64,
        now=assignment_bootstrap__NOW,
    ).exposure
    exposure_row = SimpleNamespace(
        exposure_id=exposure.exposure_id,
        assignment_id=exposure.assignment_id,
        study_revision_id=exposure.study_revision_id,
        environment_json=exposure.environment.model_dump(mode="json"),
        agent_release_id=exposure.agent_release_id,
        artifact_digest=exposure.artifact_digest,
        adapter_version=exposure.adapter_version,
        observed_configuration=exposure.observed_configuration,
        started_at=exposure.started_at,
        outcome=exposure.outcome.value,
        evidence_digest=exposure.evidence_digest,
        idempotency_key=exposure.idempotency_key,
        created_at=exposure.created_at,
    )
    restored_exposure = assignment_store.row_to_exposure(exposure_row)
    assert isinstance(restored_exposure, ExposureV1)
    assert restored_exposure.outcome == ExposureOutcome.FAILED
    assert restored_exposure.is_exposure is False


def test_bootstrap_store_reads_revocation_epoch():
    # Session capabilities are stateless: nothing is persisted, and the only
    # server-side state consulted is the enrollment's revocation epoch.
    session = MagicMock()
    session.get.return_value = SimpleNamespace(revocation_epoch=4)
    assert bootstrap_store.revocation_epoch_for(session, uuid.uuid4()) == 4

    session.get.return_value = None
    assert bootstrap_store.revocation_epoch_for(session, uuid.uuid4()) is None


def test_bootstrap_persists_the_session_it_embeds_and_reuses_it():
    """Bootstrap must persist a real session row, not mint an ephemeral id.

    The manifest's ``research_session_id`` has to resolve through the session
    store, otherwise ingestion rejects every uploaded event as
    ``SESSION_OUT_OF_SCOPE``. A second bootstrap for the same enrollment must
    reuse the existing non-terminal session instead of creating a duplicate.
    """
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    assignment = allocate(
        enrollment, revision, rng=random.Random(7), now=assignment_bootstrap__NOW
    ).assignment
    release = assignment_bootstrap___release_for(protocol, assignment.condition_id)

    db = MagicMock()
    persisted: list[ResearchSessionV1] = []

    def _create_session(_db, session, **_kwargs):
        persisted.append(session)
        return SimpleNamespace(
            session_id=session.research_session_id, opened_at=session.opened_at
        )

    def _active_session(_db, _enrollment_id, _context_id):
        if not persisted:
            return None
        stored = persisted[-1]
        return SimpleNamespace(
            session_id=stored.research_session_id, opened_at=stored.opened_at
        )

    with patch(
        "backend.routers.research.bootstrap.session_store.create_session",
        side_effect=_create_session,
    ) as create, patch(
        "backend.routers.research.bootstrap.session_store."
        "get_active_session_for_context",
        side_effect=_active_session,
    ):
        factory = _PersistentSessionFactory(db)
        first = assignment_bootstrap___compose(
            enrollment, revision, assignment, release, session_factory=factory
        )
        second = assignment_bootstrap___compose(
            enrollment, revision, assignment, release, session_factory=factory
        )

    assert first.manifest is not None, first.issue
    assert second.manifest is not None, second.issue

    # One enrollment, one session: the second bootstrap reused the first row.
    assert create.call_count == 1
    assert len(persisted) == 1
    row = persisted[0]
    assert isinstance(row, ResearchSessionV1)
    assert row.enrollment_id == enrollment.enrollment_id
    assert row.study_revision_id == revision.revision_id
    assert row.state == SessionState.NOT_STARTED
    assert row.opened_at == assignment_bootstrap__NOW

    # The id the plugin receives is the id that was persisted.
    assert first.manifest.research_session.research_session_id == row.research_session_id
    assert (
        second.manifest.research_session.research_session_id == row.research_session_id
    )
    assert first.manifest.research_session.opened_at == assignment_bootstrap__NOW


# ---------------------------------------------------------------------------
# Router wiring and handlers
# ---------------------------------------------------------------------------


def test_bootstrap_routes_are_wired_under_the_research_prefix():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/bootstrap/research-sessions" in paths
    assert "/research/bootstrap/exposures" in paths


def test_research_session_router_rejects_non_active_enrollment():
    app = MagicMock()
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision, status=EnrollmentStatus.COMPLETED)

    with patch(
        "backend.routers.research.bootstrap.identity_store.get_participant_by_account",
        return_value=SimpleNamespace(participant_id=enrollment.participant_id),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.get_revision",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.row_to_revision",
        return_value=revision,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store."
        "get_assignment_for_enrollment_revision",
        return_value=None,
    ):
        with pytest.raises(HTTPException) as error:
            assignment_bootstrap__create_research_session(
                ResearchSessionRequest(
                    enrollment_id=enrollment.enrollment_id,
                    context_id="ctx-integration",
                    environment=EnvironmentReport(os="macOS", arch="aarch64"),
                ),
                assignment_bootstrap___participant(),
                app,
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == AssignmentReasonCode.ENROLLMENT_NOT_ACTIVE.value


def test_bootstrap_router_uses_a_persistent_factory_bound_to_the_request_db():
    """The bootstrap handler must persist through the request db, not mint ids."""
    app = MagicMock()
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    issued = assignment_bootstrap___issued_manifest()[4]
    stubbed = BootstrapResult(
        outcome=BootstrapOutcome.ISSUED,
        reason=BootstrapReasonCode.OK,
        manifest=issued,
    )

    with patch(
        "backend.routers.research.bootstrap.identity_store.get_participant_by_account",
        return_value=SimpleNamespace(participant_id=enrollment.participant_id),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.get_revision",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.row_to_revision",
        return_value=revision,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store."
        "get_assignment_for_enrollment_revision",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.registry_store.get_release",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.crud.get_agent_profile_by_id",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.compose_bootstrap",
        return_value=stubbed,
    ) as compose:
        response = assignment_bootstrap__create_research_session(
            ResearchSessionRequest(
                enrollment_id=enrollment.enrollment_id,
                context_id="ctx-integration",
                environment=EnvironmentReport(os="macOS", arch="aarch64"),
            ),
            assignment_bootstrap___participant(),
            app,
        )

    assert response.status_code == 201
    factory = compose.call_args.args[5]
    assert isinstance(factory, _PersistentSessionFactory)
    assert factory._db is app.get_db_session.return_value


def test_blocked_bootstrap_does_not_persist_the_sticky_assignment():
    """A kill-switched/failed bootstrap must leave no assignment row behind.

    The sticky assignment is committed only once ``compose_bootstrap`` actually
    issues a manifest. A typed block (here: kill switch) aborts before the
    assignment is written, so no orphan assignment can exist without a manifest
    or session.
    """
    app = MagicMock()
    protocol = assignment_bootstrap___protocol()
    revision = assignment_bootstrap___revision(protocol)
    enrollment = assignment_bootstrap___enrollment(revision)
    blocked = BootstrapResult(
        outcome=BootstrapOutcome.BLOCKED,
        reason=BootstrapReasonCode.KILL_SWITCH_ENGAGED,
        issue=BootstrapIssue(
            code=BootstrapReasonCode.KILL_SWITCH_ENGAGED,
            message="an operator kill switch is engaged for this scope",
            field="kill_switch",
        ),
    )

    with patch(
        "backend.routers.research.bootstrap.identity_store.get_participant_by_account",
        return_value=SimpleNamespace(participant_id=enrollment.participant_id),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.get_revision",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.protocol_store.row_to_revision",
        return_value=revision,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store."
        "get_assignment_for_enrollment_revision",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.registry_store.get_release",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.crud.get_agent_profile_by_id",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.compose_bootstrap",
        return_value=blocked,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store.create_assignment",
    ) as create:
        with pytest.raises(HTTPException) as error:
            assignment_bootstrap__create_research_session(
                ResearchSessionRequest(
                    enrollment_id=enrollment.enrollment_id,
                    context_id="ctx-integration",
                    environment=EnvironmentReport(os="macOS", arch="aarch64"),
                ),
                assignment_bootstrap___participant(),
                app,
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == BootstrapReasonCode.KILL_SWITCH_ENGAGED.value
    create.assert_not_called()


def test_compose_bootstrap_requires_an_explicit_session_factory():
    """No in-memory session factory is a default: the caller must inject one."""
    import inspect

    from research.runtime.bootstrap.service import compose_bootstrap

    parameters = inspect.signature(compose_bootstrap).parameters
    assert "session_factory" in parameters
    assert parameters["session_factory"].default is inspect.Parameter.empty
    assert parameters["session_factory"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_exposure_router_records_receipt():
    app = MagicMock()
    _, _, assignment = assignment_bootstrap___allocated()
    enrollment = SimpleNamespace(
        enrollment_id=assignment.enrollment_id,
        participant_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        revocation_epoch=0,
    )
    capability = issue_capability(
        audience="research-runtime",
        scope=["telemetry:write"],
        ttl_seconds=600,
        revocation_epoch=0,
        secret=BOOTSTRAP_SIGNING_SECRET,
        now=assignment_bootstrap__NOW,
        enrollment_id=assignment.enrollment_id,
        research_session_id=uuid.uuid4(),
        revision_id=assignment.study_revision_id,
    )

    with patch(
        "backend.routers.research.bootstrap.identity_store.get_participant_by_account",
        return_value=SimpleNamespace(participant_id=enrollment.participant_id),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store.get_assignment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.bootstrap.assignment_store.row_to_assignment",
        return_value=assignment,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store."
        "get_exposure_by_idempotency_key",
        return_value=None,
    ), patch(
        "backend.routers.research.bootstrap.assignment_store.insert_exposure",
        return_value=SimpleNamespace(),
    ) as insert, patch(
        "backend.routers.research.bootstrap.assignment_store.exposure_summary",
        return_value={"exposure_id": "exp-1", "outcome": "STARTED"},
    ), patch(
        "backend.routers.research.bootstrap._now",
        return_value=assignment_bootstrap__NOW,
    ):
        response = create_exposure(
            ExposureRequest(
                capability=capability,
                enrollment_id=assignment.enrollment_id,
                assignment_id=assignment.assignment_id,
                environment=ExposureEnvironment(os="macOS", arch="aarch64"),
                outcome=ExposureOutcome.STARTED,
                idempotency_key="router-key-1",
            ),
            assignment_bootstrap___participant(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["is_exposure"] is True
    assert body["exposure"]["exposure_id"] == "exp-1"
    insert.assert_called_once()


def test_allocated_assignment_has_a_distinct_exposure_fact():
    _, _, assignment = assignment_bootstrap___allocated()
    exposure = record_exposure(
        assignment,
        ExposureEnvironment(os="macOS", arch="aarch64"),
        ExposureOutcome.SUCCEEDED,
        "final-key",
        now=assignment_bootstrap__NOW,
    )
    assert exposure.exposure.assignment_id == assignment.assignment_id
    assert exposure.exposure.exposure_id != assignment.assignment_id
    assert exposure.exposure.started_at == assignment_bootstrap__NOW
    assert exposure.exposure.outcome == ExposureOutcome.SUCCEEDED


# --------------------------------------------------------------------------
# test_research_sessions
# --------------------------------------------------------------------------
# Tests for the research session lifecycle and agent runs (Issue 07).
research_sessions__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
research_sessions__PROTOCOL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
    / "approved_study_protocol_v1.json"
)

research_sessions__LEGAL_TARGETS = {
    SessionState.NOT_STARTED: {SessionState.RUNNING},
    SessionState.RUNNING: {
        SessionState.OFFLINE,
        SessionState.SUSPENDED,
        SessionState.ENDED,
        SessionState.REVOKED,
    },
    SessionState.OFFLINE: {
        SessionState.RUNNING,
        SessionState.ENDED,
        SessionState.REVOKED,
    },
    SessionState.SUSPENDED: {SessionState.RUNNING, SessionState.ENDED},
    SessionState.ENDED: set(),
    SessionState.REVOKED: set(),
}


def research_sessions___policy(
    idle: int = 600, grace: int = 120, heartbeat: int | None = 30
) -> SessionPolicyV1:
    return SessionPolicyV1(
        idle_timeout_seconds=idle,
        resume_grace_seconds=grace,
        heartbeat_seconds=heartbeat,
    )


def research_sessions___session(
    *,
    state: SessionState = SessionState.NOT_STARTED,
    last_activity_at: datetime | None = None,
    resume_generation: int = 0,
    enrollment_id: uuid.UUID | None = None,
    study_revision_id: uuid.UUID | None = None,
) -> ResearchSessionV1:
    terminal = state.is_terminal
    return ResearchSessionV1(
        research_session_id=uuid.uuid4(),
        enrollment_id=enrollment_id or uuid.uuid4(),
        study_revision_id=study_revision_id or uuid.uuid4(),
        state=state,
        opened_at=research_sessions__NOW if state != SessionState.NOT_STARTED else None,
        last_activity_at=last_activity_at,
        closed_at=research_sessions__NOW if terminal else None,
        close_reason=(
            CloseReason.EXPLICIT_COMPLETION
            if state == SessionState.ENDED
            else (CloseReason.REVOKED if state == SessionState.REVOKED else None)
        ),
        resume_generation=resume_generation,
        manifest_digest="sha256:" + "m" * 64,
    )


def research_sessions___protocol() -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(json.loads(research_sessions__PROTOCOL_FIXTURE.read_text()))


def research_sessions___enrollment(revision_id: uuid.UUID, *, status: EnrollmentStatus = EnrollmentStatus.ACTIVE):
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        study_revision_id=revision_id,
        participant_code="p_synthetic",
        status=status,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=research_sessions__NOW
        ),
        enrolled_at=research_sessions__NOW,
        updated_at=research_sessions__NOW,
        revocation_epoch=0,
    )


def research_sessions___capability(
    *,
    scope: list[str],
    epoch: int = 0,
    audience: str = "research-runtime",
    now: datetime = research_sessions__NOW,
    ttl: int = 600,
    enrollment_id: uuid.UUID | None = None,
    research_session_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
):
    return issue_capability(
        audience=audience,
        scope=scope,
        ttl_seconds=ttl,
        revocation_epoch=epoch,
        secret=BOOTSTRAP_SIGNING_SECRET,
        now=now,
        enrollment_id=enrollment_id or uuid.uuid4(),
        research_session_id=research_session_id or uuid.uuid4(),
        revision_id=revision_id or uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# State machine: legal / illegal transitions and terminal reasons
# ---------------------------------------------------------------------------


def test_legal_transition_map_matches_the_published_lifecycle():
    for from_state, targets in research_sessions__LEGAL_TARGETS.items():
        for to_state in SessionState:
            assert can_transition(from_state, to_state) == (to_state in targets), (
                from_state,
                to_state,
            )


def test_first_qualifying_activity_starts_the_session_and_records_a_transition():
    session = research_sessions___session()
    result = on_qualifying_activity(session, research_sessions__NOW)

    assert result.accepted is True
    assert result.session.state == SessionState.RUNNING
    assert result.session.opened_at == research_sessions__NOW
    assert result.transition is not None
    assert result.transition.from_state == SessionState.NOT_STARTED
    assert result.transition.to_state == SessionState.RUNNING


def test_offline_recover_and_suspend_round_trip():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session

    offline = go_offline(running, research_sessions__NOW + timedelta(seconds=1))
    assert offline.session.state == SessionState.OFFLINE

    recovered = recover(offline.session, research_sessions__NOW + timedelta(seconds=2))
    assert recovered.session.state == SessionState.RUNNING

    suspended = suspend(recovered.session, research_sessions__NOW + timedelta(seconds=3))
    assert suspended.session.state == SessionState.SUSPENDED


def test_illegal_transitions_return_typed_invalid_state_transition():
    not_started = research_sessions___session()
    assert end(not_started, research_sessions__NOW).reason == SessionReasonCode.INVALID_STATE_TRANSITION
    assert (
        go_offline(not_started, research_sessions__NOW).reason
        == SessionReasonCode.INVALID_STATE_TRANSITION
    )
    assert (
        revoke(not_started, research_sessions__NOW).reason == SessionReasonCode.INVALID_STATE_TRANSITION
    )

    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    assert (
        recover(running, research_sessions__NOW).reason == SessionReasonCode.INVALID_STATE_TRANSITION
    )
    assert (
        resume(running, research_sessions__NOW, policy=research_sessions___policy()).reason == SessionReasonCode.NOT_SUSPENDED
    )

    ended = end(running, research_sessions__NOW).session
    assert ended.state == SessionState.ENDED
    assert end(ended, research_sessions__NOW).reason == SessionReasonCode.SESSION_TERMINAL
    assert on_qualifying_activity(ended, research_sessions__NOW).reason == SessionReasonCode.SESSION_TERMINAL


def test_terminal_close_reasons_are_explicit():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session

    explicit = end(running, research_sessions__NOW)
    assert explicit.session.close_reason == CloseReason.EXPLICIT_COMPLETION

    idle = expire_if_idle(
        record_activity(running, research_sessions__NOW).session,
        research_sessions__NOW + timedelta(seconds=601),
        policy=research_sessions___policy(idle=600),
    )
    assert idle.session.close_reason == CloseReason.IDLE_TIMEOUT
    assert idle.reason == SessionReasonCode.IDLE_TIMEOUT

    revoked = revoke(running, research_sessions__NOW)
    assert revoked.session.state == SessionState.REVOKED
    assert revoked.session.close_reason == CloseReason.REVOKED

    ide_closed = close(running, CloseReason.IDE_CLOSED, research_sessions__NOW)
    assert ide_closed.session.state == SessionState.ENDED
    assert ide_closed.session.close_reason == CloseReason.IDE_CLOSED

    revoked_close = close(running, CloseReason.REVOKED, research_sessions__NOW)
    assert revoked_close.session.state == SessionState.REVOKED


def test_revoked_state_is_terminal_and_blocks_lifecycle_calls():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    revoked = revoke(running, research_sessions__NOW).session

    assert revoked.state.is_terminal
    assert on_qualifying_activity(revoked, research_sessions__NOW).reason == SessionReasonCode.SESSION_TERMINAL
    assert end(revoked, research_sessions__NOW).reason == SessionReasonCode.SESSION_TERMINAL
    assert revoke(revoked, research_sessions__NOW).reason == SessionReasonCode.SESSION_TERMINAL
    assert close(revoked, CloseReason.EXPLICIT_COMPLETION, research_sessions__NOW).reason == (
        SessionReasonCode.SESSION_TERMINAL
    )
    # A terminal session cannot start an agent run.
    assert (
        start_agent_run(revoked, research_sessions__NOW).reason == SessionReasonCode.SESSION_NOT_RUNNING
    )


# ---------------------------------------------------------------------------
# Policy inputs (never compiled constants)
# ---------------------------------------------------------------------------


def test_idle_decisions_depend_on_injected_policy():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    later = research_sessions__NOW + timedelta(seconds=601)

    strict = expire_if_idle(running, later, policy=research_sessions___policy(idle=600))
    assert strict.session.state == SessionState.ENDED

    lenient = expire_if_idle(running, later, policy=research_sessions___policy(idle=100_000))
    assert lenient.session.state == SessionState.RUNNING
    assert lenient.transition is None


def test_resume_decisions_depend_on_injected_policy():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    suspended = suspend(running, research_sessions__NOW).session
    later = research_sessions__NOW + timedelta(seconds=500)

    strict = resume(suspended, later, policy=research_sessions___policy(grace=120))
    assert strict.reopened is False
    assert strict.requires_new_session is True
    assert strict.session.state == SessionState.ENDED

    lenient = resume(suspended, later, policy=research_sessions___policy(grace=1000))
    assert lenient.reopened is True
    assert lenient.session.state == SessionState.RUNNING


def test_policy_ref_and_extraction_are_revision_driven():
    protocol = research_sessions___protocol()
    policy = session_policy_from_revision(protocol)
    assert policy is not None
    # The fixture declares idle=900, resume=300, heartbeat=30.
    assert policy.idle_timeout_seconds == 900
    assert policy.resume_grace_seconds == 300
    assert policy_ref(policy) == "idle_timeout_seconds=900,resume_grace_seconds=300"

    data = protocol.model_dump(mode="json")
    data["session_policy"] = {
        "idle_timeout_seconds": None,
        "resume_grace_seconds": None,
        "heartbeat_seconds": None,
    }
    assert session_policy_from_revision(StudyProtocolV1.model_validate(data)) is None


# ---------------------------------------------------------------------------
# Restart within/after grace
# ---------------------------------------------------------------------------


def test_restart_within_grace_reopens_same_session_with_new_generation():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    suspended = suspend(running, research_sessions__NOW + timedelta(seconds=10)).session

    result = resume(suspended, research_sessions__NOW + timedelta(seconds=100), policy=research_sessions___policy(grace=120))
    assert result.reopened is True
    assert result.requires_new_session is False
    assert result.session.research_session_id == suspended.research_session_id
    assert result.session.resume_generation == 1
    assert result.transition.policy_ref == "idle_timeout_seconds=600,resume_grace_seconds=120"


def test_restart_after_grace_closes_once_and_never_merges_later_activity():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    suspended = suspend(running, research_sessions__NOW).session

    result = resume(suspended, research_sessions__NOW + timedelta(seconds=121), policy=research_sessions___policy(grace=120))
    assert result.reopened is False
    assert result.requires_new_session is True
    assert result.session.state == SessionState.ENDED
    assert result.session.close_reason == CloseReason.RESUME_GRACE_EXPIRED
    assert result.transition.reason == SessionReasonCode.RESUME_GRACE_EXPIRED

    # Later activity on the closed session is rejected, not merged.
    merged = on_qualifying_activity(result.session, research_sessions__NOW + timedelta(seconds=200))
    assert merged.accepted is False
    assert merged.reason == SessionReasonCode.SESSION_TERMINAL

    # The caller must create a new session (distinct identity).
    new_session = open_session(
        research_sessions___enrollment(result.session.study_revision_id),
        SimpleNamespace(revision_id=result.session.study_revision_id),
        manifest_digest="sha256:" + "n" * 64,
        now=research_sessions__NOW + timedelta(seconds=200),
    )
    assert new_session.research_session_id != result.session.research_session_id
    assert new_session.state == SessionState.NOT_STARTED


# ---------------------------------------------------------------------------
# Agent runs are distinct child identities
# ---------------------------------------------------------------------------


def test_agent_run_ids_are_distinct_and_crash_keeps_the_session():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    start = start_agent_run(
        running,
        research_sessions__NOW + timedelta(seconds=1),
        agent_release_id="rel-1",
    )
    assert start.accepted is True
    assert start.run is not None
    assert start.run.agent_run_id != start.session.research_session_id

    crash = on_agent_run_crashed(start.run, running, research_sessions__NOW + timedelta(seconds=5))
    assert crash.run.outcome == AgentRunOutcome.CRASHED
    assert crash.run.is_terminal is True
    # The session is preserved and can host a new run.
    assert crash.session.state == SessionState.RUNNING

    second = start_agent_run(crash.session, research_sessions__NOW + timedelta(seconds=6))
    assert second.accepted is True
    assert second.run.agent_run_id != start.run.agent_run_id


def test_agent_run_cannot_start_outside_running_and_end_is_idempotent():
    not_started = research_sessions___session()
    assert (
        start_agent_run(not_started, research_sessions__NOW).reason == SessionReasonCode.SESSION_NOT_RUNNING
    )

    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    run = start_agent_run(running, research_sessions__NOW).run
    ended = end_agent_run(run, AgentRunOutcome.COMPLETED, research_sessions__NOW + timedelta(seconds=2))
    assert ended.run.outcome == AgentRunOutcome.COMPLETED

    again = end_agent_run(
        ended.run, AgentRunOutcome.CRASHED, research_sessions__NOW + timedelta(seconds=9)
    )
    assert again.reason == SessionReasonCode.AGENT_RUN_TERMINAL
    assert again.run.outcome == AgentRunOutcome.COMPLETED


def test_agent_run_model_rejects_interchangeable_ids():
    session_id = uuid.uuid4()
    with pytest.raises(PydanticValidationError):
        AgentRunV1(
            agent_run_id=session_id,
            research_session_id=session_id,
            started_at=research_sessions__NOW,
        )


# ---------------------------------------------------------------------------
# Persistence helpers (MagicMock session)
# ---------------------------------------------------------------------------


def test_create_session_adds_expected_row():
    session = research_sessions___session()
    db = MagicMock()

    row = session_store.create_session(db, session)

    added = db.add.call_args.args[0]
    assert added.session_id == session.research_session_id
    assert added.enrollment_id == session.enrollment_id
    assert added.state == SessionState.NOT_STARTED.value
    assert added.resume_generation == 0
    assert row.session_id == session.research_session_id
    db.commit.assert_called_once()
    db.refresh.assert_called_once()


def test_create_agent_run_adds_distinct_row():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    run = start_agent_run(running, research_sessions__NOW, agent_release_id="rel-1").run
    db = MagicMock()

    row = session_store.create_agent_run(db, run)

    added = db.add.call_args.args[0]
    assert added.agent_run_id == run.agent_run_id
    assert added.research_session_id == running.research_session_id
    assert added.agent_run_id != added.research_session_id
    assert row.agent_run_id == run.agent_run_id


def test_insert_transition_and_update_session():
    running = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).session
    db = MagicMock()
    row = session_store.insert_transition(
        db,
        on_qualifying_activity(research_sessions___session(), research_sessions__NOW).transition,
    )
    assert row.research_session_id is not None

    fake_row = SimpleNamespace(
        state="not_started",
        opened_at=None,
        last_activity_at=None,
        closed_at=None,
        close_reason=None,
        resume_generation=0,
        manifest_digest="",
        environment_json={},
    )
    db.get.return_value = fake_row
    updated = session_store.update_session(db, running)
    assert updated is fake_row
    assert updated.state == SessionState.RUNNING.value


def test_row_round_trips_for_session_run_and_transition():
    session = research_sessions___session(state=SessionState.RUNNING, last_activity_at=research_sessions__NOW)
    session_row = SimpleNamespace(
        session_id=session.research_session_id,
        enrollment_id=session.enrollment_id,
        study_revision_id=session.study_revision_id,
        context_id=session.context_id,
        state=session.state.value,
        opened_at=session.opened_at,
        last_activity_at=session.last_activity_at,
        closed_at=None,
        close_reason=None,
        resume_generation=session.resume_generation,
        manifest_digest=session.manifest_digest,
        environment_json={"environment_ref": "env-1"},
    )
    restored = session_store.row_to_session(session_row)
    assert restored.research_session_id == session.research_session_id
    assert restored.environment_ref == "env-1"

    run = start_agent_run(session, research_sessions__NOW, agent_release_id="rel-1").run
    run_row = SimpleNamespace(
        agent_run_id=run.agent_run_id,
        research_session_id=run.research_session_id,
        agent_release_id="rel-1",
        started_at=run.started_at,
        ended_at=None,
        outcome=None,
    )
    restored_run = session_store.row_to_agent_run(run_row)
    assert isinstance(restored_run, AgentRunV1)
    assert restored_run.agent_run_id == run.agent_run_id

    transition = on_qualifying_activity(research_sessions___session(), research_sessions__NOW).transition
    # Transitions are stored inline as serialized entries in the session row.
    serialized_transition = transition.model_dump(mode="json")
    restored_transition = session_store.row_to_transition(serialized_transition)
    assert restored_transition.to_state == SessionState.RUNNING
    assert restored_transition.transition_id == transition.transition_id


def test_get_active_session_for_enrollment_uses_query():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = None
    assert session_store.get_active_session_for_enrollment(db, uuid.uuid4()) is None


def test_get_session_for_update_takes_a_row_lock():
    db = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = None
    session_store.get_session(db, uuid.uuid4(), for_update=True)
    statement = db.execute.call_args.args[0]
    # A row lock is requested (``SELECT ... FOR UPDATE``), not a plain get.
    assert "FOR UPDATE" in str(statement)


def test_create_session_is_atomic_on_partial_unique_conflict():
    session = research_sessions___session()
    existing_row = SimpleNamespace(
        session_id=uuid.uuid4(), opened_at=research_sessions__NOW
    )
    db = MagicMock()
    db.begin_nested.return_value.__enter__.side_effect = IntegrityError(
        "INSERT", {}, Exception("duplicate key value violates unique constraint")
    )

    with patch.object(
        session_store,
        "get_active_session_for_context",
        return_value=existing_row,
    ) as active:
        row = session_store.create_session(db, session)

    # The losing concurrent create returns the winning session; it never leaves a
    # second active session or commits its own row.
    assert row is existing_row
    active.assert_called_once_with(
        db, session.enrollment_id, str(session.research_session_id)
    )
    db.commit.assert_not_called()



# ---------------------------------------------------------------------------
# Router: capability authorization
# ---------------------------------------------------------------------------


def test_session_routes_are_wired_under_the_research_prefix():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/sessions/heartbeat" in paths
    assert "/research/sessions/close" in paths
    assert "/research/sessions/summary" in paths
    assert any(path.startswith("/research/sessions") for path in paths)


def research_sessions___heartbeat_patches(session, enrollment, revision_row):
    return (
        patch(
            "backend.routers.research.sessions.session_store.get_session",
            return_value=SimpleNamespace(),
        ),
        patch(
            "backend.routers.research.sessions.session_store.row_to_session",
            return_value=session,
        ),
        patch(
            "backend.routers.research.sessions.session_store.insert_transition",
        ),
        patch(
            "backend.routers.research.sessions.session_store.update_session",
        ),
        patch(
            "backend.routers.research.sessions.session_store.session_summary",
            return_value={"research_session_id": str(session.research_session_id)},
        ),
        patch(
            "backend.routers.research.sessions.identity_store.get_enrollment",
            return_value=SimpleNamespace(),
        ),
        patch(
            "backend.routers.research.sessions.identity_store.row_to_enrollment",
            return_value=enrollment,
        ),
        patch(
            "backend.routers.research.sessions.protocol_store.get_revision",
            return_value=revision_row,
        ),
        patch(
            "backend.routers.research.sessions._kill_switch_for_session",
            return_value=lambda: False,
        ),
        patch("backend.routers.research.sessions._now", return_value=research_sessions__NOW),
    )


def research_sessions___run_heartbeat(session, enrollment, revision_row, capability):
    app = MagicMock()
    patches = research_sessions___heartbeat_patches(session, enrollment, revision_row)
    for p in patches:
        p.start()
    try:
        return heartbeat(
            HeartbeatRequest(
                capability=capability, research_session_id=session.research_session_id
            ),
            app,
        )
    finally:
        for p in reversed(patches):
            p.stop()


def test_heartbeat_accepts_a_valid_capability_and_advances_state():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    enrollment = research_sessions___enrollment(revision_id)
    session = research_sessions___session(
        state=SessionState.NOT_STARTED,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["session:heartbeat"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        revision_id=revision_id,
    )

    response = research_sessions___run_heartbeat(session, enrollment, revision_row, capability)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["session"]["research_session_id"] == str(session.research_session_id)


def test_heartbeat_rejects_expired_revoked_and_wrong_audience_capabilities():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    enrollment = research_sessions___enrollment(revision_id)
    session = research_sessions___session(
        state=SessionState.RUNNING,
        last_activity_at=research_sessions__NOW,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )

    subject = dict(
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        revision_id=revision_id,
    )
    expired = research_sessions___capability(
        scope=["session:heartbeat"],
        now=research_sessions__NOW - timedelta(seconds=1200),
        ttl=60,
        **subject,
    )
    with pytest.raises(HTTPException) as error:
        research_sessions___run_heartbeat(session, enrollment, revision_row, expired)
    assert error.value.status_code == 403
    assert error.value.detail["capability_reason"] == CapabilityReasonCode.EXPIRED.value

    revoked = research_sessions___capability(
        scope=["session:heartbeat"], epoch=0, **subject
    )
    enrollment_revoked = enrollment.model_copy(update={"revocation_epoch": 5})
    with pytest.raises(HTTPException) as error:
        research_sessions___run_heartbeat(session, enrollment_revoked, revision_row, revoked)
    assert error.value.status_code == 403
    assert error.value.detail["capability_reason"] == CapabilityReasonCode.REVOKED.value

    wrong_audience = research_sessions___capability(
        scope=["session:heartbeat"], audience="other", **subject
    )
    with pytest.raises(HTTPException) as error:
        research_sessions___run_heartbeat(session, enrollment, revision_row, wrong_audience)
    assert error.value.status_code == 403
    assert error.value.detail["capability_reason"] == (
        CapabilityReasonCode.WRONG_AUDIENCE.value
    )


def test_heartbeat_rejects_a_non_active_enrollment():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    withdrawn = research_sessions___enrollment(revision_id, status=EnrollmentStatus.COMPLETED)
    session = research_sessions___session(
        state=SessionState.RUNNING,
        last_activity_at=research_sessions__NOW,
        enrollment_id=withdrawn.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["session:heartbeat"],
        epoch=0,
        enrollment_id=withdrawn.enrollment_id,
        research_session_id=session.research_session_id,
        revision_id=revision_id,
    )

    with pytest.raises(HTTPException) as error:
        research_sessions___run_heartbeat(session, withdrawn, revision_row, capability)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == SessionReasonCode.ENROLLMENT_NOT_ACTIVE.value


def test_heartbeat_rejects_terminal_session():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    enrollment = research_sessions___enrollment(revision_id)
    revoked_session = research_sessions___session(
        state=SessionState.REVOKED,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["session:heartbeat"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=revoked_session.research_session_id,
        revision_id=revision_id,
    )

    with pytest.raises(HTTPException) as error:
        research_sessions___run_heartbeat(revoked_session, enrollment, revision_row, capability)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == SessionReasonCode.SESSION_TERMINAL.value


def test_ordinary_heartbeat_persists_last_activity_at_without_a_transition():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    enrollment = research_sessions___enrollment(revision_id)
    session = research_sessions___session(
        state=SessionState.RUNNING,
        last_activity_at=research_sessions__NOW - timedelta(seconds=5),
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["session:heartbeat"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        revision_id=revision_id,
    )
    app = MagicMock()
    patches = research_sessions___heartbeat_patches(session, enrollment, revision_row)
    started = [p.start() for p in patches]
    try:
        response = heartbeat(
            HeartbeatRequest(
                capability=capability, research_session_id=session.research_session_id
            ),
            app,
        )
    finally:
        for p in reversed(patches):
            p.stop()

    assert response.status_code == 200
    update_mock = started[3]  # session_store.update_session
    insert_transition_mock = started[2]
    # An ordinary heartbeat has no state transition, but it must still persist
    # the refreshed last-activity marker.
    insert_transition_mock.assert_not_called()
    assert update_mock.call_count == 1
    persisted = update_mock.call_args.args[1]
    assert persisted.last_activity_at == research_sessions__NOW
    assert persisted.state == SessionState.RUNNING



def test_create_session_returns_existing_active_session():
    revision_id = uuid.uuid4()
    revision_row = SimpleNamespace(
        revision_id=revision_id,
        status="PUBLISHED",
        protocol_json=research_sessions___protocol().model_dump(mode="json"),
    )
    enrollment = research_sessions___enrollment(revision_id)
    existing = research_sessions___session(
        state=SessionState.RUNNING,
        last_activity_at=research_sessions__NOW,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["telemetry:write"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=existing.research_session_id,
        revision_id=revision_id,
    )
    app = MagicMock()

    with patch(
        "backend.routers.research.sessions.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch(
        "backend.routers.research.sessions.protocol_store.get_revision",
        return_value=revision_row,
    ), patch(
        "backend.routers.research.sessions.session_store.get_active_session_for_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.session_store.row_to_session",
        return_value=existing,
    ), patch(
        "backend.routers.research.sessions.session_store.session_summary",
        return_value={"state": "running"},
    ), patch(
        "backend.routers.research.sessions._now",
        return_value=research_sessions__NOW,
    ):
        response = research_sessions__create_research_session(
            CreateSessionRequest(
                capability=capability,
                enrollment_id=enrollment.enrollment_id,
                study_revision_id=revision_id,
                manifest_digest="sha256:" + "m" * 64,
                context_id="ctx-integration",
            ),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["created"] is False
    assert body["session"]["state"] == "running"


def test_close_router_rejects_terminal_session():
    revision_id = uuid.uuid4()
    enrollment = research_sessions___enrollment(revision_id)
    revoked_session = research_sessions___session(
        state=SessionState.REVOKED,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["session:close"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=revoked_session.research_session_id,
        revision_id=revision_id,
    )
    app = MagicMock()

    with patch(
        "backend.routers.research.sessions.session_store.get_session",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.session_store.row_to_session",
        return_value=revoked_session,
    ), patch(
        "backend.routers.research.sessions.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch("backend.routers.research.sessions._now", return_value=research_sessions__NOW):
        with pytest.raises(HTTPException) as error:
            close_research_session(
                CloseSessionRequest(
                    capability=capability,
                    research_session_id=revoked_session.research_session_id,
                    reason=CloseReason.EXPLICIT_COMPLETION,
                ),
                app,
            )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == SessionReasonCode.SESSION_TERMINAL.value


def test_summary_router_returns_session_summary():
    revision_id = uuid.uuid4()
    enrollment = research_sessions___enrollment(revision_id)
    session = research_sessions___session(
        state=SessionState.RUNNING,
        last_activity_at=research_sessions__NOW,
        enrollment_id=enrollment.enrollment_id,
        study_revision_id=revision_id,
    )
    capability = research_sessions___capability(
        scope=["telemetry:write"],
        epoch=0,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=session.research_session_id,
        revision_id=revision_id,
    )
    app = MagicMock()

    with patch(
        "backend.routers.research.sessions.session_store.get_session",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.session_store.row_to_session",
        return_value=session,
    ), patch(
        "backend.routers.research.sessions.session_store.session_summary",
        return_value={"state": "running"},
    ), patch(
        "backend.routers.research.sessions.identity_store.get_enrollment",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.routers.research.sessions.identity_store.row_to_enrollment",
        return_value=enrollment,
    ), patch("backend.routers.research.sessions._now", return_value=research_sessions__NOW):
        response = get_research_session(
            SessionSummaryRequest(
                capability=capability,
                research_session_id=session.research_session_id,
            ),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["session"]["state"] == "running"


# --------------------------------------------------------------------------
# test_kill_switch_retention
# --------------------------------------------------------------------------
# Tests for the kill switch and retention deletion drill (Issue 13).
kill_switch_retention__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
kill_switch_retention__STUDY = uuid.UUID("11111111-1111-4111-8111-111111111111")
kill_switch_retention__OTHER_STUDY = uuid.UUID("99999999-9999-4999-8999-999999999999")
kill_switch_retention__SECRET = "fixture-kill-switch-secret"


# ---------------------------------------------------------------------------
# Kill switch: scope, audit, predicate
# ---------------------------------------------------------------------------


def test_kill_switch_scope_is_exact_and_auditable():
    registry = KillSwitchRegistry()
    record = engage_kill_switch(
        registry,
        KillSwitchScope(kind=KillSwitchScopeKind.STUDY, scope_id=kill_switch_retention__STUDY),
        "adverse event review",
        actor="operator@example.com",
        now=kill_switch_retention__NOW,
    )

    assert record.actor == "operator@example.com"
    assert record.reason == "adverse event review"
    assert record.engaged_at == kill_switch_retention__NOW
    assert is_engaged(registry, study_id=kill_switch_retention__STUDY, now=kill_switch_retention__NOW)
    assert not is_engaged(registry, study_id=kill_switch_retention__OTHER_STUDY, now=kill_switch_retention__NOW)
    assert kill_switch_issue(registry, study_id=kill_switch_retention__STUDY, now=kill_switch_retention__NOW) is not None
    assert (
        kill_switch_issue(registry, study_id=kill_switch_retention__STUDY, now=kill_switch_retention__NOW).code
        == OperationsReasonCode.KILL_SWITCH_ENGAGED
    )


def test_kill_switch_release_and_expiry_disengage():
    registry = KillSwitchRegistry()
    record = engage_kill_switch(
        registry,
        KillSwitchScope(kind=KillSwitchScopeKind.ENROLLMENT, scope_id=uuid.uuid4()),
        "participant request",
        now=kill_switch_retention__NOW,
    )
    released = release_kill_switch(registry, record.switch_id, now=kill_switch_retention__NOW + timedelta(minutes=1))
    assert released is not None and released.released_at is not None
    assert not is_engaged(registry, enrollment_id=record.scope.scope_id, now=kill_switch_retention__NOW)

    expiring = KillSwitchRegistry()
    scoped = engage_kill_switch(
        expiring,
        KillSwitchScope(kind=KillSwitchScopeKind.REVISION, scope_id=uuid.uuid4()),
        "temporary hold",
        now=kill_switch_retention__NOW,
        effective_until=kill_switch_retention__NOW + timedelta(minutes=5),
    )
    assert is_engaged(expiring, revision_id=scoped.scope.scope_id, now=kill_switch_retention__NOW)
    assert not is_engaged(
        expiring, revision_id=scoped.scope.scope_id, now=kill_switch_retention__NOW + timedelta(minutes=6)
    )


def test_kill_switch_check_predicate_and_persisted_row_round_trip():
    registry = KillSwitchRegistry()
    scope = KillSwitchScope(kind=KillSwitchScopeKind.STUDY, scope_id=kill_switch_retention__STUDY)
    record = engage_kill_switch(registry, scope, "hold", actor="op", now=kill_switch_retention__NOW)

    check = kill_switch_check(registry, study_id=kill_switch_retention__STUDY, now=kill_switch_retention__NOW)
    assert check() is True

    row = SimpleNamespace(
        record_id=record.switch_id,
        kind="KILL_SWITCH",
        payload_json=record.model_dump(mode="json"),
    )
    rehydrated = operations_store.row_to_kill_switch(row)
    assert rehydrated.scope == scope
    assert rehydrated.is_engaged(kill_switch_retention__NOW)


def test_db_kill_switch_check_matches_persisted_scope():
    from research.analysis.operations.models import KillSwitchRecord

    enrollment_id = uuid.uuid4()
    record = KillSwitchRecord(
        switch_id=uuid.uuid4(),
        scope=KillSwitchScope(
            kind=KillSwitchScopeKind.ENROLLMENT, scope_id=enrollment_id
        ),
        reason="participant request",
        actor="operator@example.com",
        engaged_at=kill_switch_retention__NOW,
    )
    row = SimpleNamespace(
        record_id=record.switch_id,
        kind="KILL_SWITCH",
        payload_json=record.model_dump(mode="json"),
    )
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [row]

    engaged = operations_store.db_kill_switch_check(
        session, enrollment_id=enrollment_id, now=kill_switch_retention__NOW
    )
    assert engaged() is True

    other_scope = operations_store.db_kill_switch_check(
        session, enrollment_id=uuid.uuid4(), now=kill_switch_retention__NOW
    )
    assert other_scope() is False

    assert operations_store.is_kill_switch_engaged(
        session, enrollment_id=enrollment_id, now=kill_switch_retention__NOW
    ) is True


def test_kill_switch_blocks_bootstrap_with_typed_reason():
    result = compose_bootstrap(
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        now=kill_switch_retention__NOW,
        kill_switch_check=lambda: True,
    )

    assert result.outcome == BootstrapOutcome.BLOCKED
    assert result.reason == BootstrapReasonCode.KILL_SWITCH_ENGAGED
    assert result.manifest is None


def test_kill_switch_rejects_ingestion_with_typed_retryable_reason():
    store = FakeIngestionStore()
    event = EventBuilder().build(
        emitter_id="acp-proxy",
        event_type="tool.completed",
        source="acp",
        occurred_at=kill_switch_retention__NOW,
        normalizer_version="generic-acp-v1",
        study_id=kill_switch_retention__STUDY,
        revision_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(),
        payload={"tool_name": "read"},
        emitter_sequence=1,
    )
    capability = issue_capability(
        audience="research-runtime",
        scope=["telemetry:write"],
        ttl_seconds=600,
        revocation_epoch=0,
        secret=kill_switch_retention__SECRET,
        now=kill_switch_retention__NOW,
        enrollment_id=event.enrollment_id,
        research_session_id=event.research_session_id,
        revision_id=event.revision_id,
    )
    request = TelemetryBatchRequestV1(
        batch_id=uuid.uuid4(),
        session_capability=capability,
        events=[event],
        client_instance_id="client-synthetic-1",
    )

    ack = ingest_batch(
        request,
        capability_verifier=lambda *a, **k: None,
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=kill_switch_retention__NOW,
        kill_switch_check=lambda: True,
    )

    assert ack.accepted == []
    assert len(ack.retryable) == 1
    assert ack.retryable[0].disposition == EventDisposition.RETRYABLE
    assert ack.retryable[0].reason == IngestionReasonCode.KILL_SWITCH_ENGAGED
    # No canonical record and no receipt is written while engaged.
    assert store.event_count() == 0
    assert store.get_receipt(request.batch_id) is None


# ---------------------------------------------------------------------------
# Retention deletion drill
# ---------------------------------------------------------------------------


def kill_switch_retention___enrollment() -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=kill_switch_retention__STUDY,
        study_revision_id=uuid.uuid4(),
        participant_code="p_synthetic",
        status=EnrollmentStatus.COMPLETED,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=kill_switch_retention__NOW
        ),
        enrolled_at=kill_switch_retention__NOW,
        updated_at=kill_switch_retention__NOW,
    )


def kill_switch_retention___record(record_id: str, *, linkable: bool = True) -> PseudonymousRecord:
    return PseudonymousRecord(
        record_id=record_id,
        account_id=uuid.uuid4() if linkable else None,
        email="participant@example.invalid" if linkable else None,
        session_token="sess-CANARY" if linkable else None,
        pseudonymous_payload={"events": 3},
    )


def test_delete_all_drill_is_verified_and_export_clean():
    enrollment = kill_switch_retention___enrollment()
    before = [kill_switch_retention___record("r1"), kill_switch_retention___record("r2")]

    verification = verify_deletion(
        enrollment,
        action=RetentionAction.DELETE_ALL,
        before_records=before,
        after_records=[],
        export_record_ids=[],
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.status == RetentionVerificationStatus.VERIFIED
    assert verification.deleted_count == 2
    assert verification.remaining_count == 0
    assert verification.export_exclusion_verified is True
    assert verification.close_out_allowed is True
    assert verification.evidence_digest


def test_partial_retention_failure_does_not_silently_close_out():
    enrollment = kill_switch_retention___enrollment()
    before = [kill_switch_retention___record("r1"), kill_switch_retention___record("r2")]

    # One record was not deleted: the drill must not close out.
    verification = verify_deletion(
        enrollment,
        action=RetentionAction.DELETE_ALL,
        before_records=before,
        after_records=[kill_switch_retention___record("r1")],
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.status == RetentionVerificationStatus.PARTIAL
    assert verification.close_out_allowed is False
    assert OperationsReasonCode.RETENTION_PARTIAL in {
        reason.code for reason in verification.reasons
    }
    assert verification.evidence_digest


def test_retained_linkable_fields_are_a_partial_failure():
    enrollment = kill_switch_retention___enrollment()
    before = [kill_switch_retention___record("r1"), kill_switch_retention___record("r2", linkable=False)]

    verification = verify_deletion(
        enrollment,
        action=RetentionAction.DELETE_IDENTIFIABLE,
        before_records=before,
        after_records=before,  # deletion was not actually applied
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.status == RetentionVerificationStatus.PARTIAL
    assert verification.close_out_allowed is False


def test_export_still_referencing_deleted_data_is_flagged():
    enrollment = kill_switch_retention___enrollment()
    before = [kill_switch_retention___record("r1")]

    verification = verify_deletion(
        enrollment,
        action=RetentionAction.DELETE_ALL,
        before_records=before,
        after_records=[],
        export_record_ids=["r1"],
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.export_exclusion_verified is False
    assert verification.status == RetentionVerificationStatus.PARTIAL
    assert verification.close_out_allowed is False
    assert OperationsReasonCode.EXPORT_EXCLUSION_UNVERIFIED in {
        reason.code for reason in verification.reasons
    }


def test_missing_retained_record_is_a_failed_drill():
    enrollment = kill_switch_retention___enrollment()
    before = [kill_switch_retention___record("r1"), kill_switch_retention___record("r2", linkable=False)]

    # RETAIN_ANONYMIZED must keep both records; an empty post-state is a failure.
    verification = verify_deletion(
        enrollment,
        action=RetentionAction.RETAIN_ANONYMIZED,
        before_records=before,
        after_records=[],
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.status == RetentionVerificationStatus.FAILED
    assert verification.close_out_allowed is False
    assert OperationsReasonCode.RETENTION_FAILED in {
        reason.code for reason in verification.reasons
    }


def test_export_exclusion_is_unknown_when_not_supplied():
    enrollment = kill_switch_retention___enrollment()
    verification = verify_deletion(
        enrollment,
        action=RetentionAction.DELETE_ALL,
        before_records=[kill_switch_retention___record("r1")],
        after_records=[],
        verified_at=kill_switch_retention__NOW,
    )

    assert verification.export_exclusion_verified is None
    assert verification.coverage.value == "AVAILABLE"


# --------------------------------------------------------------------------
# test_operations_health
# --------------------------------------------------------------------------
# Tests for operational health aggregation and thresholds (Issue 13).
operations_health__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
operations_health__WINDOW_END = operations_health__NOW + timedelta(minutes=5)
operations_health__STUDY = uuid.UUID("11111111-1111-4111-8111-111111111111")


def operations_health___health(**inputs):
    return aggregate_health(
        operations_health__STUDY, operations_health__NOW, operations_health__WINDOW_END, HealthInputs(**inputs), now=operations_health__NOW
    )


def operations_health___by_name(snapshot):
    return {signal.name: signal for signal in snapshot.signals}


def test_healthy_window_has_healthy_signals():
    snapshot = operations_health___health(
        spool_depth=10,
        spool_age_seconds=30,
        upload_ack_rate=1.0,
        runtime_exit_count=0,
        capability_mismatch_count=0,
        retention_job_status="OK",
        export_job_health="OK",
    )

    for signal in snapshot.signals:
        assert signal.state == HealthSignalState.HEALTHY, signal.name
    summary = health_summary(snapshot)
    assert summary["overall"] == HealthSignalState.HEALTHY.value


def test_spool_backlog_crosses_degraded_and_critical_thresholds():
    degraded = operations_health___by_name(operations_health___health(spool_depth=5_000))
    assert degraded["spool_depth"].state == HealthSignalState.DEGRADED
    assert degraded["spool_depth"].observed_value == 5_000
    assert health_summary(operations_health___health(spool_depth=5_000))["overall"] == "DEGRADED"

    critical = operations_health___by_name(operations_health___health(spool_depth=20_000))
    assert critical["spool_depth"].state == HealthSignalState.CRITICAL
    summary = health_summary(operations_health___health(spool_depth=20_000))
    assert summary["overall"] == "CRITICAL"
    assert summary["reasons"][0]["code"] == OperationsReasonCode.THRESHOLD_CRITICAL.value


def test_ack_rate_lower_is_worse():
    assert operations_health___by_name(operations_health___health(upload_ack_rate=0.97))["upload_ack_rate"].state == (
        HealthSignalState.DEGRADED
    )
    assert operations_health___by_name(operations_health___health(upload_ack_rate=0.90))["upload_ack_rate"].state == (
        HealthSignalState.CRITICAL
    )


def test_custom_thresholds_are_honored():
    snapshot = aggregate_health(
        operations_health__STUDY,
        operations_health__NOW,
        operations_health__WINDOW_END,
        HealthInputs(spool_depth=6),
        thresholds=HealthThresholds(spool_depth_degraded=5, spool_depth_critical=100),
        now=operations_health__NOW,
    )
    assert operations_health___by_name(snapshot)["spool_depth"].state == HealthSignalState.DEGRADED


def test_missing_inputs_are_unknown_and_never_zero():
    snapshot = operations_health___health()

    assert snapshot.spool_depth is None
    assert snapshot.upload_ack_rate is None
    for signal in snapshot.signals:
        assert signal.state == HealthSignalState.UNKNOWN, signal.name
        assert signal.observed_value is None
    assert health_summary(snapshot)["overall"] == HealthSignalState.UNKNOWN.value


def test_monitoring_outage_is_unknown_and_never_invents_health():
    snapshot = aggregate_health(
        operations_health__STUDY,
        operations_health__NOW,
        operations_health__WINDOW_END,
        HealthInputs(
            spool_depth=1,
            upload_ack_rate=1.0,
            runtime_exit_count=0,
            retention_job_status="OK",
        ),
        monitoring_available=False,
        now=operations_health__NOW,
    )

    # Every metric is withheld, not inferred: no scalar may claim health.
    assert snapshot.spool_depth is None
    assert snapshot.upload_ack_rate is None
    assert snapshot.runtime_exit_count is None
    assert snapshot.retention_job_status is None
    assert snapshot.session_state_counts == {}
    for signal in snapshot.signals:
        assert signal.state == HealthSignalState.UNKNOWN, signal.name
        assert signal.observed_value is None
        assert signal.reason
    assert health_summary(snapshot)["overall"] == HealthSignalState.UNKNOWN.value


def test_status_signals_reflect_retention_and_export_health():
    assert operations_health___by_name(operations_health___health(retention_job_status="PENDING"))[
        "retention_job_status"
    ].state == HealthSignalState.DEGRADED
    assert operations_health___by_name(operations_health___health(export_job_health="FAILED"))[
        "export_job_health"
    ].state == HealthSignalState.CRITICAL


def test_health_contract_rejects_content_fields():
    base = {
        "study_id": operations_health__STUDY,
        "window_start": operations_health__NOW,
        "window_end": operations_health__WINDOW_END,
        "captured_at": operations_health__NOW,
    }
    # No content/prompt/secret field can be attached to the health snapshot.
    for forbidden in ("prompt", "content", "secret", "source", "reasoning"):
        with pytest.raises(ValidationError):
            OperationalHealthV1(**base, **{forbidden: "CANARY"})

    # Health inputs are likewise metadata-only.
    with pytest.raises(ValidationError):
        HealthInputs(prompt="CANARY")
    with pytest.raises(ValidationError):
        HealthInputs(spool_depth=1, secret="CANARY")


def test_health_contract_is_immutable():
    snapshot = operations_health___health(spool_depth=1)
    with pytest.raises(ValidationError):
        snapshot.spool_depth = 999


def test_kill_switch_routes_are_wired_under_operations():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    for path in (
        "/research/operations/kill-switch",
        "/research/operations/kill-switch/{switch_id}/release",
    ):
        assert path in paths, path
    # Unused pilot/health/release-evidence surfaces are removed; kill-switch
    # enforcement (the predicate used by the funded gate) is retained.
    for removed in (
        "/research/operations/health",
        "/research/operations/release-evidence",
        "/research/operations/pilots/{pilot_run_id}",
    ):
        assert removed not in paths, removed


# --------------------------------------------------------------------------
# test_release_gate
# --------------------------------------------------------------------------
# Tests for the release gate (Issue 13).
release_gate__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
release_gate__REVISION = uuid.UUID("22222222-2222-4222-8222-222222222222")
release_gate__REQUIRED_COMPONENTS = ("code4me2-server", "plugin", "proxy")
release_gate__REQUIRED_EVIDENCE = ("pytest", "rehearsal")


def release_gate___evidence(**overrides) -> ReleaseEvidenceV1:
    data = {
        "release_id": uuid.uuid4(),
        "study_revision_id": release_gate__REVISION,
        "revision_digest": "sha256:" + "r" * 64,
        "component_artifacts": {
            "code4me2-server": "sha256:" + "a" * 64,
            "plugin": "sha256:" + "b" * 64,
            "proxy": "sha256:" + "c" * 64,
        },
        "test_results": {"pytest": "PASS", "rehearsal": "PASS"},
        "recorded_at": release_gate__NOW,
    }
    data.update(overrides)
    return ReleaseEvidenceV1(**data)


def release_gate___evaluate(evidence, **overrides):
    params = {
        "required_components": release_gate__REQUIRED_COMPONENTS,
        "required_evidence": release_gate__REQUIRED_EVIDENCE,
        "now": release_gate__NOW,
    }
    params.update(overrides)
    return evaluate_release(evidence, **params)


def test_go_requires_every_required_item_present():
    result = release_gate___evaluate(release_gate___evidence())

    assert result.decision == ReleaseDecision.GO
    assert result.reasons == []


def test_missing_required_component_is_no_go():
    evidence = release_gate___evidence(component_artifacts={"code4me2-server": "sha256:" + "a" * 64})
    result = release_gate___evaluate(evidence)

    assert result.decision == ReleaseDecision.NO_GO
    codes = {reason.code for reason in result.reasons}
    assert OperationsReasonCode.REQUIRED_COMPONENT_MISSING in codes
    assert all(reason.message for reason in result.reasons)


def test_unknown_component_digest_is_no_go():
    evidence = release_gate___evidence(
        component_artifacts={
            "code4me2-server": "UNKNOWN",
            "plugin": "sha256:" + "b" * 64,
            "proxy": "sha256:" + "c" * 64,
        }
    )
    result = release_gate___evaluate(evidence)

    assert result.decision == ReleaseDecision.NO_GO
    assert OperationsReasonCode.REQUIRED_COMPONENT_MISSING in {
        reason.code for reason in result.reasons
    }


def test_missing_or_non_pass_evidence_is_no_go():
    for value in ("UNKNOWN", "FAIL", "SKIPPED"):
        result = release_gate___evaluate(release_gate___evidence(test_results={"pytest": value, "rehearsal": "PASS"}))
        assert result.decision == ReleaseDecision.NO_GO, value
        assert OperationsReasonCode.REQUIRED_EVIDENCE_MISSING in {
            reason.code for reason in result.reasons
        }

    missing = release_gate___evaluate(release_gate___evidence(test_results={"pytest": "PASS"}))
    assert missing.decision == ReleaseDecision.NO_GO


def test_expired_component_receipt_is_expired():
    evidence = release_gate___evidence(
        component_expires_at={
            "code4me2-server": release_gate__NOW - timedelta(hours=1),
            "plugin": release_gate__NOW + timedelta(hours=1),
        }
    )
    result = release_gate___evaluate(evidence)

    assert result.decision == ReleaseDecision.EXPIRED
    assert OperationsReasonCode.COMPONENT_RECEIPT_EXPIRED in {
        reason.code for reason in result.reasons
    }


def test_expired_revision_is_expired():
    evidence = release_gate___evidence(revision_expires_at=release_gate__NOW - timedelta(days=1))
    result = release_gate___evaluate(evidence)

    assert result.decision == ReleaseDecision.EXPIRED
    assert OperationsReasonCode.REVISION_EXPIRED in {
        reason.code for reason in result.reasons
    }


def test_expiry_dominates_other_failures():
    evidence = release_gate___evidence(
        component_artifacts={"code4me2-server": "sha256:" + "a" * 64},
        revision_expires_at=release_gate__NOW - timedelta(days=1),
    )
    result = release_gate___evaluate(evidence)

    # An expired receipt/revision is EXPIRED even though evidence is also missing.
    assert result.decision == ReleaseDecision.EXPIRED


def test_material_component_change_invalidates_an_old_go():
    evidence = release_gate___evidence()
    assert release_gate___evaluate(evidence).decision == ReleaseDecision.GO

    changed = release_gate___evaluate(
        evidence,
        current_component_digests={"plugin": "sha256:" + "9" * 64},
    )
    assert changed.decision == ReleaseDecision.NO_GO
    assert OperationsReasonCode.MATERIAL_CHANGE in {
        reason.code for reason in changed.reasons
    }


def test_material_revision_change_invalidates_an_old_go():
    result = release_gate___evaluate(
        release_gate___evidence(), current_revision_digest="sha256:" + "z" * 64
    )
    assert result.decision == ReleaseDecision.NO_GO
    assert OperationsReasonCode.MATERIAL_CHANGE in {
        reason.code for reason in result.reasons
    }


def test_recorded_limitations_yield_go_with_limits():
    result = release_gate___evaluate(
        release_gate___evidence(known_limitations=["usage unavailable for vendor X"])
    )

    assert result.decision == ReleaseDecision.GO_WITH_LIMITS
    assert OperationsReasonCode.LIMITATIONS_PRESENT in {
        reason.code for reason in result.reasons
    }
    assert "usage unavailable for vendor X" in result.reasons[-1].message


def test_limitations_do_not_mask_a_hard_failure():
    result = release_gate___evaluate(
        release_gate___evidence(
            known_limitations=["documented"],
            component_artifacts={"code4me2-server": "sha256:" + "a" * 64},
        )
    )
    assert result.decision == ReleaseDecision.NO_GO


def test_release_evidence_is_immutable():
    evidence = release_gate___evidence()
    with pytest.raises(ValidationError):
        evidence.component_artifacts = {}

    recorded = record_decision(evidence, ReleaseDecision.GO, recorded_at=release_gate__NOW)
    assert recorded.decision == ReleaseDecision.GO
    assert evidence.decision is None  # the original is unchanged
    assert recorded is not evidence
