"""Consolidated research tests (see individual section headers).

Merged from smaller modules; test functions and assertions are unchanged.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from research.analysis.read_models import (
    ConditionExposureV1,
    DerivedMetricV1,
    EnrollmentCoverageV1,
    ReadModelAuthorizationError,
    ReadModelReasonCode,
    ResearcherGrant,
    ResearcherRole,
    StudyRevisionSummaryV1,
    TelemetryCoverageV1,
    build_condition_exposures,
    build_derived_metrics,
    build_enrollment_coverage,
    build_revision_summary,
    build_telemetry_coverage,
    can_read_private_mapping,
    can_read_study,
    can_read_telemetry,
    require_study_access,
    require_telemetry_access,
    role_for_study,
)
from research.analysis.read_models import store as read_store
from research.analysis.read_models.enums import ResearcherRole
from research.analysis.read_models.rbac import ResearcherGrant
from research.analysis.read_models.service import build_derived_metrics, build_telemetry_coverage
from research.participants.enums import EnrollmentStatus, IdentityReasonCode
from research.participants.models import Enrollment, ResearchEligibility
from research.runtime.assignment.enums import ExposureOutcome
from research.runtime.assignment.models import AssignmentV1, ExposureEnvironment, ExposureV1
from research.runtime.sessions.enums import SessionState
from research.runtime.sessions.models import ResearchSessionV1
from research.study.protocol.canonical import protocol_digest
from research.study.protocol.enums import RevisionStatus
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.publication import StudyRevision
from research.telemetry import EventBuilder
from research.telemetry.enums import CoverageState
from research.telemetry.ingestion.models import ResearchEventRecord
from research.telemetry.models import Coverage, EventMetrics
from research.telemetry.privacy import PrivacyPolicy, filter_event

# --------------------------------------------------------------------------
# test_coverage_read_models
# --------------------------------------------------------------------------
# Tests for researcher read models and scoped RBAC (Issue 12).
coverage_read_models__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
coverage_read_models__PROTOCOL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
    / "approved_study_protocol_v1.json"
)


def coverage_read_models___protocol() -> StudyProtocolV1:
    return StudyProtocolV1.model_validate(json.loads(coverage_read_models__PROTOCOL_FIXTURE.read_text()))


def coverage_read_models___revision() -> StudyRevision:
    protocol = coverage_read_models___protocol()
    return StudyRevision(
        revision_id=uuid.uuid4(),
        study_id=protocol.study_id,
        revision_number=1,
        status=RevisionStatus.PUBLISHED,
        protocol_json=protocol.model_dump(mode="json"),
        protocol_digest=protocol_digest(protocol),
        published_at=coverage_read_models__NOW,
        created_at=coverage_read_models__NOW,
    )


def coverage_read_models___enrollment(revision: StudyRevision, status: EnrollmentStatus) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=revision.study_id,
        study_revision_id=revision.revision_id,
        participant_code="p_synthetic",
        status=status,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=coverage_read_models__NOW
        ),
        enrolled_at=coverage_read_models__NOW,
        updated_at=coverage_read_models__NOW,
    )


def coverage_read_models___record(
    revision: StudyRevision,
    enrollment: Enrollment,
    *,
    seq: int,
    usage: int | None = None,
    usage_state: CoverageState = CoverageState.UNAVAILABLE,
) -> ResearchEventRecord:
    event = EventBuilder().build(
        emitter_id="acp-proxy",
        event_type="tool.completed",
        source="acp",
        occurred_at=coverage_read_models__NOW,
        normalizer_version="generic-acp-v1",
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=uuid.uuid4(),
        payload={"tool_name": "read"},
        metrics=EventMetrics(
            usage_tokens=usage,
            usage_capability=Coverage(state=usage_state, capability="usage"),
        ),
        emitter_sequence=seq,
    )
    return ResearchEventRecord(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        source=event.source,
        study_id=event.study_id,
        revision_id=event.revision_id,
        enrollment_id=event.enrollment_id,
        research_session_id=event.research_session_id,
        agent_run_id=None,
        emitter_id=event.emitter_id,
        emitter_sequence=event.emitter_sequence,
        occurred_at=event.occurred_at,
        envelope=event.model_dump(mode="json"),
        digest="sha256:" + "d" * 64,
        accepted_at=coverage_read_models__NOW,
    )


def coverage_read_models___assignment(revision: StudyRevision, condition_id: str) -> AssignmentV1:
    return AssignmentV1(
        assignment_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        study_revision_id=revision.revision_id,
        condition_id=condition_id,
        strategy="WEIGHTED_RANDOM",
        randomization_epoch=0,
        assigned_at=coverage_read_models__NOW,
        protocol_digest=revision.protocol_digest,
    )


def coverage_read_models___exposure(
    assignment: AssignmentV1, outcome: ExposureOutcome = ExposureOutcome.STARTED
) -> ExposureV1:
    return ExposureV1(
        exposure_id=uuid.uuid4(),
        assignment_id=assignment.assignment_id,
        study_revision_id=assignment.study_revision_id,
        environment=ExposureEnvironment(os="macOS", arch="aarch64"),
        agent_release_id="rel-1",
        outcome=outcome,
        started_at=coverage_read_models__NOW,
        idempotency_key=str(uuid.uuid4()),
        created_at=coverage_read_models__NOW,
    )


# ---------------------------------------------------------------------------
# Read models carry revision/digest/version/population
# ---------------------------------------------------------------------------


def test_revision_summary_carries_revision_digest_and_conditions():
    revision = coverage_read_models___revision()
    summary = build_revision_summary(revision)

    assert isinstance(summary, StudyRevisionSummaryV1)
    assert summary.revision_id == revision.revision_id
    assert summary.revision_digest == revision.protocol_digest
    assert summary.population == "revision"
    assert summary.condition_count == len(revision.protocol_json["conditions"])
    assert summary.condition_ids


def test_enrollment_coverage_counts_states_and_fails_closed_when_empty():
    revision = coverage_read_models___revision()
    enrollments = [
        coverage_read_models___enrollment(revision, EnrollmentStatus.ACTIVE),
        coverage_read_models___enrollment(revision, EnrollmentStatus.ACTIVE),
        coverage_read_models___enrollment(revision, EnrollmentStatus.COMPLETED),
    ]
    coverage = build_enrollment_coverage(
        enrollments,
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
    )

    assert isinstance(coverage, EnrollmentCoverageV1)
    assert coverage.revision_digest == revision.protocol_digest
    assert coverage.population == "enrolled"
    assert coverage.total_enrollments == 3
    assert coverage.status_counts["ACTIVE"] == 2
    assert coverage.status_counts["COMPLETED"] == 1
    assert coverage.withdrawn_count == 0
    assert coverage.coverage == CoverageState.AVAILABLE

    empty = build_enrollment_coverage(
        [],
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
    )
    assert empty.total_enrollments == 0
    assert empty.coverage == CoverageState.UNAVAILABLE
    assert empty.coverage_reason


def test_condition_exposure_is_assignment_vs_exposure():
    revision = coverage_read_models___revision()
    first = coverage_read_models___assignment(revision, "control")
    second = coverage_read_models___assignment(revision, "treatment")
    exposed = coverage_read_models___exposure(first, ExposureOutcome.STARTED)

    models = build_condition_exposures(
        [first, second],
        [exposed],
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
    )
    by_condition = {model.condition_id: model for model in models}

    assert isinstance(by_condition["control"], ConditionExposureV1)
    assert by_condition["control"].assigned_count == 1
    assert by_condition["control"].exposed_count == 1
    assert by_condition["control"].exposure_rate == 1.0
    # Treatment has an assignment but no exposure yet: rate is a real 0.0, not null.
    assert by_condition["treatment"].assigned_count == 1
    assert by_condition["treatment"].exposed_count == 0
    assert by_condition["treatment"].exposure_rate == 0.0
    assert by_condition["treatment"].coverage == CoverageState.AVAILABLE


def test_telemetry_coverage_and_metrics_carry_revision_and_version():
    revision = coverage_read_models___revision()
    enrollment = coverage_read_models___enrollment(revision, EnrollmentStatus.ACTIVE)
    records = [
        coverage_read_models___record(revision, enrollment, seq=1, usage=7, usage_state=CoverageState.AVAILABLE),
        coverage_read_models___record(revision, enrollment, seq=2),
    ]

    coverage = build_telemetry_coverage(
        records,
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
    )
    assert isinstance(coverage, TelemetryCoverageV1)
    assert coverage.revision_digest == revision.protocol_digest
    assert coverage.coverage_version == "coverage-v1"
    assert coverage.population == "all_events"
    assert coverage.denominators["events"] == 2

    metrics = build_derived_metrics(
        records,
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
    )
    for metric in metrics:
        assert isinstance(metric, DerivedMetricV1)
        assert metric.revision_digest == revision.protocol_digest
        assert metric.derivation_version
        assert metric.population == "all_events"
        assert metric.coverage_predicate


def test_missing_usage_is_null_and_never_zero():
    revision = coverage_read_models___revision()
    enrollment = coverage_read_models___enrollment(revision, EnrollmentStatus.ACTIVE)
    no_usage = [coverage_read_models___record(revision, enrollment, seq=1), coverage_read_models___record(revision, enrollment, seq=2)]

    metric = next(
        metric
        for metric in build_derived_metrics(
            no_usage,
            study_id=revision.study_id,
            revision_id=revision.revision_id,
            revision_digest=revision.protocol_digest,
        )
        if metric.metric_id == "usage_tokens_per_event"
    )
    assert metric.value is None
    assert metric.numerator is None
    assert metric.denominator is None
    assert metric.coverage_state == CoverageState.UNAVAILABLE

    observed_zero = [coverage_read_models___record(revision, enrollment, seq=1, usage=0, usage_state=CoverageState.AVAILABLE)]
    zero_metric = next(
        metric
        for metric in build_derived_metrics(
            observed_zero,
            study_id=revision.study_id,
            revision_id=revision.revision_id,
            revision_digest=revision.protocol_digest,
        )
        if metric.metric_id == "usage_tokens_per_event"
    )
    assert zero_metric.value == 0.0
    assert zero_metric.coverage_state == CoverageState.AVAILABLE


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_rbac_is_scoped_to_permitted_studies():
    permitted = uuid.uuid4()
    other = uuid.uuid4()
    grant = ResearcherGrant(
        researcher_id="researcher-1", study_id=permitted, role=ResearcherRole.OWNER
    )

    assert role_for_study([grant], "researcher-1", permitted) == ResearcherRole.OWNER
    assert can_read_study(
        ResearcherRole.OWNER, permitted, permitted_study_ids=[permitted]
    )
    # A role on one study never widens to another study.
    assert not can_read_study(
        ResearcherRole.OWNER, other, permitted_study_ids=[permitted]
    )
    with pytest.raises(ReadModelAuthorizationError) as error:
        require_study_access([grant], "researcher-1", other)
    assert error.value.code == ReadModelReasonCode.FORBIDDEN_STUDY

    with pytest.raises(ReadModelAuthorizationError) as missing:
        require_study_access([grant], "someone-else", permitted)
    assert missing.value.code == ReadModelReasonCode.NO_ROLE


def test_privacy_operator_cannot_read_telemetry_but_can_read_mapping():
    assert can_read_telemetry(ResearcherRole.ANALYST) is True
    assert can_read_telemetry(ResearcherRole.PRIVACY_OPERATOR) is False
    assert can_read_private_mapping(ResearcherRole.PRIVACY_OPERATOR) is True
    assert can_read_private_mapping(ResearcherRole.ANALYST) is False

    with pytest.raises(ReadModelAuthorizationError) as error:
        require_telemetry_access(ResearcherRole.PRIVACY_OPERATOR)
    assert error.value.code == ReadModelReasonCode.TELEMETRY_FORBIDDEN


# ---------------------------------------------------------------------------
# Persistence helpers (MagicMock session)
# ---------------------------------------------------------------------------


def test_read_store_helpers_use_session():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.execute.return_value.scalars.return_value.first.return_value = None
    study_id = uuid.uuid4()

    assert list(read_store.list_revisions(session, study_id)) == []
    assert list(read_store.list_enrollments(session, study_id)) == []
    assert list(read_store.list_assignments(session, study_id)) == []
    assert list(read_store.list_events(session, uuid.uuid4())) == []


def test_read_model_routes_are_wired_and_export_routes_are_removed():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/operations/enrollments/coverage" in paths
    assert "/research/operations/exposures" in paths
    # Redundant read/ops surfaces are removed (no production caller).
    assert "/research/operations/revisions" not in paths
    assert "/research/operations/telemetry/coverage" not in paths
    assert "/research/operations/telemetry/metrics" not in paths
    assert "/research/operations/health" not in paths
    assert "/research/operations/release-evidence" not in paths
    assert "/research/operations/pilots/{pilot_run_id}" not in paths
    # The public export job surface is removed (no nonexistent artifact is
    # presented as downloadable).
    assert "/research/exports" not in paths
    assert "/research/exports/{export_id}" not in paths
