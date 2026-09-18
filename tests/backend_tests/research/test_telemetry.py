"""Consolidated research tests (see individual section headers).

Merged from smaller modules; test functions and assertions are unchanged.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError as PydanticValidationError

from backend.routers.research.telemetry import submit_telemetry_batch
from research.participants.enums import EnrollmentStatus, IdentityReasonCode
from research.participants.models import Enrollment, ResearchEligibility
from research.runtime.bootstrap.capability import issue_capability, verify_capability
from research.runtime.sessions.enums import SessionState
from research.runtime.sessions.models import ResearchSessionV1
from research.study.protocol.enums import TelemetryFieldClass
from research.study.protocol.models import TelemetryPolicy
from research.telemetry import (
    CanonicalEventType,
    CanonicalEventV1,
    CanonicalFidelity,
    Coverage,
    CoverageState,
    EventBuilder,
    EventMetrics,
    EventSource,
    FieldClass,
    PolicyAction,
    SequenceAllocator,
    TelemetryValidationCode,
    build_event,
    validate_canonical_event,
)
from research.telemetry.enums import CanonicalFidelity, CoverageState
from research.telemetry.ingestion import (
    BatchReceipt,
    EventAck,
    EventDisposition,
    FakeIngestionStore,
    IngestionReasonCode,
    TelemetryBatchAckV1,
    TelemetryBatchRequestV1,
    compute_event_digest,
    ingest_batch,
)
from research.telemetry.ingestion.models import IngestionContext, ResearchEventRecord
from research.telemetry.models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    PrivacySummary,
)
from research.telemetry.normalization import (
    CanonicalCandidateV1,
    GenericAcpNormalizer,
    enrich_with_adapter,
)
from research.telemetry.privacy import (
    PrivacyPolicy,
    classify_event_payload,
    classify_field,
    contains_secret_value,
    filter_event,
    filter_payload,
    is_secret_key,
    looks_secret_value,
)
from research.telemetry.projections import (
    COVERAGE_VERSION,
    DERIVATION_VERSION,
    coverage_by_family,
    derive_event_count_metric,
    derive_usage_metric,
    event_family,
    session_summary,
)
from research.telemetry.schema import (
    CANONICAL_EVENT_V1_SCHEMA_PATH,
    build_canonical_event_v1_schema,
    load_canonical_event_v1_schema,
    serialize_canonical_event_v1_schema,
)

# --------------------------------------------------------------------------
# test_telemetry_schema
# --------------------------------------------------------------------------
# Tests for the canonical telemetry schema and normalization (Issue 06).
telemetry_schema__FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "research" / "telemetry"
)
telemetry_schema__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def telemetry_schema___load(name: str):
    return json.loads((telemetry_schema__FIXTURE_DIR / name).read_text())


def telemetry_schema___codes(errors) -> set[TelemetryValidationCode]:
    return {error.code for error in errors}


def telemetry_schema___event(**overrides) -> CanonicalEventV1:
    builder = EventBuilder()
    params = {
        "emitter_id": "emitter-synthetic",
        "event_type": CanonicalEventType.TOOL_STARTED,
        "source": EventSource.ACP,
        "occurred_at": telemetry_schema__NOW,
        "normalizer_version": "generic-acp-v1",
        "payload": {"tool_call_id": "call-1", "tool_name": "read"},
    }
    params.update(overrides)
    return builder.build(**params)


# ---------------------------------------------------------------------------
# Builder: identity, ordering, immutability
# ---------------------------------------------------------------------------


def test_build_allocates_fresh_id_and_strictly_increasing_sequence():
    builder = EventBuilder()
    first = builder.build(
        emitter_id="e1",
        event_type="tool.started",
        source="acp",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="generic-acp-v1",
    )
    second = builder.build(
        emitter_id="e1",
        event_type="tool.completed",
        source="acp",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="generic-acp-v1",
    )
    assert first.event_id != second.event_id
    assert first.emitter_sequence == 1
    assert second.emitter_sequence == 2
    assert second.emitter_sequence > first.emitter_sequence


def test_per_emitter_sequences_are_independent_not_a_global_counter():
    builder = EventBuilder()
    assert (
        builder.build(
            emitter_id="emitter-a",
            event_type="tool.started",
            source="acp",
            occurred_at=telemetry_schema__NOW,
            normalizer_version="v1",
        ).emitter_sequence
        == 1
    )
    assert (
        builder.build(
            emitter_id="emitter-a",
            event_type="tool.completed",
            source="acp",
            occurred_at=telemetry_schema__NOW,
            normalizer_version="v1",
        ).emitter_sequence
        == 2
    )
    # A different emitter starts its own sequence at 1, not 3.
    assert (
        builder.build(
            emitter_id="emitter-b",
            event_type="tool.started",
            source="ide",
            occurred_at=telemetry_schema__NOW,
            normalizer_version="v1",
        ).emitter_sequence
        == 1
    )


def test_sequence_allocator_isolates_emitters():
    allocator = SequenceAllocator()
    assert allocator.next("a") == 1
    assert allocator.next("b") == 1
    assert allocator.next("a") == 2
    assert allocator.current("a") == 2
    assert allocator.current("b") == 1


def test_event_is_immutable_after_construction():
    event = telemetry_schema___event()
    with pytest.raises(PydanticValidationError):
        event.emitter_id = "other"  # type: ignore[misc]
    with pytest.raises(PydanticValidationError):
        event.payload = {"x": 1}  # type: ignore[misc]


def test_builder_does_not_derive_cross_process_latency():
    builder = EventBuilder()
    first = builder.build(
        emitter_id="e1",
        event_type="tool.started",
        source="acp",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="v1",
        monotonic_ns=1_000,
    )
    second = builder.build(
        emitter_id="e1",
        event_type="tool.completed",
        source="acp",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="v1",
        monotonic_ns=9_999_999,
    )
    assert first.metrics.latency_ms is None
    assert second.metrics.latency_ms is None
    assert first.monotonic_ns == 1_000
    assert second.monotonic_ns == 9_999_999


# ---------------------------------------------------------------------------
# Provenance, fidelity, unknown preservation
# ---------------------------------------------------------------------------


def test_provenance_and_fidelity_survive_json_round_trip():
    event = telemetry_schema___event(
        fidelity=CanonicalFidelity.INFERRED,
        adapter_version="adapter-9.9.9",
        source_event_id="source-42",
        evidence_digest="sha256:" + "a" * 64,
    )
    data = json.loads(event.model_dump_json())
    assert data["provenance"]["fidelity"] == "inferred"
    assert data["provenance"]["adapter_version"] == "adapter-9.9.9"
    assert data["provenance"]["source_event_id"] == "source-42"

    restored = CanonicalEventV1.model_validate(data)
    assert restored.provenance == event.provenance
    assert validate_canonical_event(restored) == []


def test_unknown_event_type_and_source_are_preserved_as_unknown_fields():
    event = telemetry_schema___event(event_type="vendor.magic", source="vendor-channel")

    assert event.event_type == CanonicalEventType.UNKNOWN_SOURCE_EVENT.value
    assert event.unknown_event_type == "vendor.magic"
    assert event.unknown_source == "vendor-channel"
    assert event.coverage.state == CoverageState.NEEDS_REVIEW
    assert event.needs_review is True

    codes = telemetry_schema___codes(validate_canonical_event(event))
    assert TelemetryValidationCode.UNKNOWN_EVENT_TYPE in codes
    assert TelemetryValidationCode.UNKNOWN_SOURCE in codes
    assert all(
        error.needs_review
        for error in validate_canonical_event(event)
        if error.code
        in (
            TelemetryValidationCode.UNKNOWN_EVENT_TYPE,
            TelemetryValidationCode.UNKNOWN_SOURCE,
        )
    )


def test_unknown_lifecycle_state_is_preserved():
    event = telemetry_schema___event(lifecycle_state="teleported")
    assert event.lifecycle_state == "teleported"
    assert event.unknown_lifecycle_state == "teleported"
    assert event.needs_review is True


# ---------------------------------------------------------------------------
# Usage: null vs observed zero
# ---------------------------------------------------------------------------


def test_missing_usage_is_null_with_unavailable_coverage():
    event = telemetry_schema___event(
        metrics=EventMetrics(
            usage_tokens=None,
            usage_capability=Coverage(
                state=CoverageState.UNAVAILABLE,
                reason="source did not expose usage",
                capability="usage",
            ),
        )
    )
    data = json.loads(event.model_dump_json())
    assert data["metrics"]["usage_tokens"] is None
    assert data["metrics"]["usage_capability"]["state"] == "UNAVAILABLE"
    # A missing measurement is not zero and not false.
    assert data["metrics"]["usage_tokens"] != 0

    restored = CanonicalEventV1.model_validate(data)
    assert restored.metrics.usage_tokens is None
    assert restored.metrics.usage_capability.state == CoverageState.UNAVAILABLE


def test_observed_zero_usage_stays_zero():
    event = telemetry_schema___event(
        metrics=EventMetrics(
            usage_tokens=0,
            usage_capability=Coverage(
                state=CoverageState.AVAILABLE, capability="usage"
            ),
        )
    )
    data = json.loads(event.model_dump_json())
    assert data["metrics"]["usage_tokens"] == 0
    assert data["metrics"]["usage_tokens"] is not None
    assert data["metrics"]["usage_capability"]["state"] == "AVAILABLE"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validation_accepts_a_valid_event():
    assert validate_canonical_event(telemetry_schema___event()) == []


def test_validation_rejects_missing_provenance():
    data = json.loads(telemetry_schema___event().model_dump_json())
    data.pop("provenance")
    errors = validate_canonical_event(data)
    assert TelemetryValidationCode.SCHEMA_INVALID in telemetry_schema___codes(errors)


def test_validation_rejects_unsupported_schema_version():
    data = json.loads(telemetry_schema___event().model_dump_json())
    data["schema_version"] = "2"
    errors = validate_canonical_event(data)
    assert TelemetryValidationCode.SCHEMA_VERSION_UNSUPPORTED in telemetry_schema___codes(errors)


def test_validation_rejects_bad_emitter_sequence():
    data = json.loads(telemetry_schema___event().model_dump_json())
    data["emitter_sequence"] = 0
    errors = validate_canonical_event(data)
    assert TelemetryValidationCode.INVALID_EMITTER_SEQUENCE in telemetry_schema___codes(errors)


def test_validation_rejects_missing_emitter_id():
    data = json.loads(telemetry_schema___event().model_dump_json())
    data["emitter_id"] = ""
    errors = validate_canonical_event(data)
    assert TelemetryValidationCode.MISSING_EMITTER_ID in telemetry_schema___codes(errors)


def test_validation_flags_secret_payload_as_defense_in_depth():
    event = telemetry_schema___event(payload={"api_key": "sk-CANARY-VALIDATION-0001"})
    errors = validate_canonical_event(event)
    assert TelemetryValidationCode.SECRET_PRESENT in telemetry_schema___codes(errors)


def test_validation_flags_secrets_in_non_payload_envelope_fields():
    base = telemetry_schema___event()
    # A secret can never ride along in provenance, correlation ids or the
    # preserved unknown-value fields; every one is scanned defensively.
    for field_name, update in (
        ("provenance", {"provenance": base.provenance.model_copy(update={"source_event_id": "sk-CANARY-PROV-0001"})}),
        ("correlations", {"correlations": Correlations(correlation_id="sk-CANARY-CORR-0001")}),
        ("unknown_event_type", {"unknown_event_type": "sk-CANARY-UNKNOWN-0001"}),
        ("unknown_source", {"unknown_source": "sk-CANARY-UNKNOWN-SRC-0001"}),
        ("unknown_lifecycle_state", {"unknown_lifecycle_state": "sk-CANARY-UNKNOWN-LIFE-0001"}),
    ):
        event = base.model_copy(update=update)
        codes = telemetry_schema___codes(validate_canonical_event(event))
        assert TelemetryValidationCode.SECRET_PRESENT in codes, field_name


def test_filter_event_blocks_and_clears_a_secret_in_a_correlation_id():
    event = telemetry_schema___event().model_copy(
        update={"correlations": Correlations(correlation_id="sk-CANARY-FILTER-0001")}
    )
    result = filter_event(event, PrivacyPolicy.default())
    assert result.summary.blocked is True
    assert result.event.payload == {}
    assert "correlations" in (result.summary.block_reason or "")


def test_validation_rejects_non_mapping_input():
    errors = validate_canonical_event(["not", "an", "event"])  # type: ignore[arg-type]
    assert TelemetryValidationCode.SCHEMA_INVALID in telemetry_schema___codes(errors)


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


def test_json_schema_is_valid_and_matches_accepted_fixture():
    schema = load_canonical_event_v1_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    accepted = telemetry_schema___load("canonical_event_accepted.json")
    jsonschema.validate(accepted, schema)
    assert accepted["schema_version"] == "1"


def test_json_schema_accepts_a_built_and_filtered_event():
    schema = load_canonical_event_v1_schema()
    filtered = filter_event(telemetry_schema___event(), PrivacyPolicy.default()).event
    jsonschema.validate(json.loads(filtered.model_dump_json()), schema)


def test_json_schema_rejects_unknown_top_level_property():
    schema = load_canonical_event_v1_schema()
    accepted = telemetry_schema___load("canonical_event_accepted.json")
    accepted["surprise_field"] = "no"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(accepted, schema)


def test_json_schema_rejects_secret_vocabulary_event_type():
    schema = load_canonical_event_v1_schema()
    accepted = telemetry_schema___load("canonical_event_accepted.json")
    accepted["event_type"] = "acp.tool_call"  # vendor name, not canonical
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(accepted, schema)


# ---------------------------------------------------------------------------
# Cross-source golden normalization
# ---------------------------------------------------------------------------


def telemetry_schema___candidate_row(candidate: CanonicalCandidateV1) -> dict:
    return {
        "event_type": candidate.event_type.value,
        "fidelity": candidate.fidelity.value,
        "mapping_rule_id": candidate.mapping_rule_id,
        "coverage_state": candidate.coverage.state.value,
    }


def test_golden_acp_normalization():
    normalizer = GenericAcpNormalizer()
    actual = [
        [telemetry_schema___candidate_row(candidate) for candidate in normalizer.normalize(m).candidates]
        for m in telemetry_schema___load("acp_transcript_excerpt.json")
    ]
    assert actual == telemetry_schema___load("golden_acp_candidates.json")


def telemetry_schema___candidate_full_row(candidate: CanonicalCandidateV1) -> dict:
    return {
        "event_type": candidate.event_type.value,
        "fidelity": candidate.fidelity.value,
        "mapping_rule_id": candidate.mapping_rule_id,
        "coverage_state": candidate.coverage.state.value,
        "lifecycle_state": candidate.lifecycle_state,
        "usage_tokens": candidate.metrics.usage_tokens,
        "usage_state": candidate.metrics.usage_capability.state.value,
        "permission_id": candidate.correlations.permission_id,
        "tool_call_id": candidate.correlations.tool_call_id,
        "payload": candidate.payload,
    }


def test_golden_acp_completeness_normalization():
    # Issue 08 §13.4: permission requests/responses, tool lifecycle, agent
    # message completion, usage, plans and agent errors are all produced from
    # real ACP constructs, correlated per stream.
    normalizer = GenericAcpNormalizer()
    actual = [
        telemetry_schema___candidate_full_row(candidate)
        for m in telemetry_schema___load("acp_completeness_transcript.json")
        for candidate in normalizer.normalize(m).candidates
    ]
    assert actual == telemetry_schema___load("golden_acp_completeness.json")


def test_unknown_acp_method_is_metadata_only_and_needs_review():
    normalizer = GenericAcpNormalizer()
    message = telemetry_schema___load("acp_transcript_excerpt.json")[-1]
    result = normalizer.normalize(message)

    candidate = result.candidates[0]
    assert candidate.event_type == CanonicalEventType.UNKNOWN_SOURCE_EVENT
    assert candidate.coverage.state == CoverageState.NEEDS_REVIEW
    assert candidate.payload["unknown_method"] == "vendor/unknownThing"
    # Parameter names only; the secret value never enters the candidate.
    serialized = json.dumps(candidate.payload)
    assert "ghp_CANARYACP0000000003" not in serialized
    assert candidate.payload["param_names"] == ["count", "secret"]


def test_adapter_enrichment_cannot_replace_generic_fields():
    candidate = CanonicalCandidateV1(
        event_type=CanonicalEventType.TOOL_STARTED,
        mapping_rule_id="acp.update.tool_call",
        payload={"tool_name": "generic-name", "tool_call_id": "call-1"},
    )

    class _Adapter:
        adapter_version = "adapter-1.0.0"
        mapping_rule_version = "mapping-1"
        supported_release_ranges = [">=1.0.0,<2.0.0"]

        def enrich(self, generic: CanonicalCandidateV1):
            return generic.model_copy(
                update={
                    "payload": {
                        "tool_name": "adapter-rename",
                        "adapter_extra": "value",
                    }
                }
            )

    enriched = enrich_with_adapter(candidate, _Adapter())
    # Generic payload wins; the adapter can only add.
    assert enriched.payload["tool_name"] == "generic-name"
    assert enriched.payload["adapter_extra"] == "value"
    assert enriched.adapter_version == "adapter-1.0.0"


def test_build_event_convenience_uses_default_allocator():
    first = build_event(
        emitter_id="default-emitter",
        event_type="ide.file.saved",
        source="ide",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="ide-v1",
    )
    second = build_event(
        emitter_id="default-emitter",
        event_type="ide.file.saved",
        source="ide",
        occurred_at=telemetry_schema__NOW,
        normalizer_version="ide-v1",
    )
    assert second.emitter_sequence == first.emitter_sequence + 1


def test_emitter_sequence_override_is_honoured_for_deterministic_fixtures():
    event = telemetry_schema___event(event_id=uuid.UUID("55555555-5555-4555-8555-555555555555"), emitter_sequence=7)
    assert event.emitter_sequence == 7
    assert event.event_id == uuid.UUID("55555555-5555-4555-8555-555555555555")


def test_policy_action_enum_is_closed_and_additive():
    assert {action.value for action in PolicyAction} == {
        "ALLOW",
        "REDACT",
        "HASH",
        "DROP",
        "BLOCK",
    }


# --------------------------------------------------------------------------
# test_telemetry_ingestion
# --------------------------------------------------------------------------
# Tests for idempotent canonical telemetry ingestion (Issue 09).
telemetry_ingestion__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
telemetry_ingestion__SECRET = "fixture-ingestion-secret"
telemetry_ingestion__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research" / "ingestion"

telemetry_ingestion__STUDY = uuid.UUID("11111111-1111-4111-8111-111111111111")
telemetry_ingestion__REV = uuid.UUID("22222222-2222-4222-8222-222222222222")
telemetry_ingestion__ENR = uuid.UUID("33333333-3333-4333-8333-333333333333")
telemetry_ingestion__SESS = uuid.UUID("44444444-4444-4444-8444-444444444444")
telemetry_ingestion__OTHER_SESS = uuid.UUID("55555555-5555-4555-8555-555555555555")


def telemetry_ingestion___enrollment(
    *, status: EnrollmentStatus = EnrollmentStatus.ACTIVE, epoch: int = 0
) -> Enrollment:
    return Enrollment(
        enrollment_id=telemetry_ingestion__ENR,
        participant_id=uuid.uuid4(),
        study_id=telemetry_ingestion__STUDY,
        study_revision_id=telemetry_ingestion__REV,
        participant_code="p_synthetic",
        status=status,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=telemetry_ingestion__NOW
        ),
        enrolled_at=telemetry_ingestion__NOW,
        updated_at=telemetry_ingestion__NOW,
        revocation_epoch=epoch,
    )


def telemetry_ingestion___session() -> ResearchSessionV1:
    return ResearchSessionV1(
        research_session_id=telemetry_ingestion__SESS,
        enrollment_id=telemetry_ingestion__ENR,
        study_revision_id=telemetry_ingestion__REV,
        state=SessionState.RUNNING,
        opened_at=telemetry_ingestion__NOW,
        last_activity_at=telemetry_ingestion__NOW,
        manifest_digest="sha256:" + "m" * 64,
    )


def telemetry_ingestion___event(
    event_id: uuid.UUID,
    seq: int,
    *,
    session_id: uuid.UUID = telemetry_ingestion__SESS,
    payload: dict | None = None,
    usage: int | None = None,
    usage_state: CoverageState = CoverageState.UNAVAILABLE,
    schema_version: str = "1",
) -> CanonicalEventV1:
    event = EventBuilder().build(
        emitter_id="acp-proxy",
        event_type="tool.completed",
        source="acp",
        occurred_at=telemetry_ingestion__NOW,
        normalizer_version="generic-acp-v1",
        study_id=telemetry_ingestion__STUDY,
        revision_id=telemetry_ingestion__REV,
        enrollment_id=telemetry_ingestion__ENR,
        research_session_id=session_id,
        payload=payload if payload is not None else {"tool_name": "read"},
        metrics=EventMetrics(
            usage_tokens=usage,
            usage_capability=Coverage(state=usage_state, capability="usage"),
        ),
        event_id=event_id,
        emitter_sequence=seq,
    )
    if schema_version != "1":
        event = event.model_copy(update={"schema_version": schema_version})
    return event


def telemetry_ingestion___capability(
    *, epoch: int = 0, scope: list[str] | None = None, audience: str = "research-runtime", now: datetime = telemetry_ingestion__NOW, ttl: int = 600
):
    return issue_capability(
        audience=audience,
        scope=scope or ["telemetry:write"],
        ttl_seconds=ttl,
        revocation_epoch=epoch,
        secret=telemetry_ingestion__SECRET,
        now=now,
        enrollment_id=telemetry_ingestion__ENR,
        research_session_id=telemetry_ingestion__SESS,
        revision_id=telemetry_ingestion__REV,
    )


def telemetry_ingestion___verifier(secret: str = telemetry_ingestion__SECRET):
    def verify(
        capability,
        *,
        now,
        current_revocation_epoch,
        expected_enrollment_id=None,
        expected_research_session_id=None,
        expected_revision_id=None,
    ):
        return verify_capability(
            capability,
            secret,
            expected_audience="research-runtime",
            expected_scope=["telemetry:write"],
            now=now,
            current_revocation_epoch=current_revocation_epoch,
            expected_enrollment_id=expected_enrollment_id,
            expected_research_session_id=expected_research_session_id,
            expected_revision_id=expected_revision_id,
        )

    return verify


def telemetry_ingestion___request(events, *, capability=None, batch_id: uuid.UUID | None = None, schema_version: str = "1"):
    return TelemetryBatchRequestV1(
        batch_id=batch_id or uuid.uuid4(),
        telemetry_schema_version=schema_version,
        session_capability=capability or telemetry_ingestion___capability(),
        events=events,
        client_instance_id="client-synthetic-1",
    )


def telemetry_ingestion___ingest(request, store, *, enrollment=None, session=None, now=telemetry_ingestion__NOW, secret=telemetry_ingestion__SECRET, **kwargs):
    return ingest_batch(
        request,
        capability_verifier=telemetry_ingestion___verifier(secret),
        enrollment_resolver=lambda i: enrollment if i == telemetry_ingestion__ENR else None,
        session_resolver=lambda i: session if i == telemetry_ingestion__SESS else None,
        store=store,
        now=now,
        **kwargs,
    )


def telemetry_ingestion___load(name: str) -> TelemetryBatchRequestV1:
    return TelemetryBatchRequestV1.model_validate(
        json.loads((telemetry_ingestion__FIXTURE_DIR / name).read_text())
    )


def telemetry_ingestion___by_id(acks):
    return {ack.event_id: ack for ack in acks}


# ---------------------------------------------------------------------------
# Valid batches and idempotency
# ---------------------------------------------------------------------------


def test_valid_batch_is_accepted_and_stored_once():
    store = FakeIngestionStore()
    request = telemetry_ingestion___load("batch_valid.json")

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert {a.event_id for a in ack.accepted} == {
        event.event_id for event in request.events
    }
    assert ack.rejected == [] and ack.retryable == []
    assert store.event_count() == len(request.events)
    stored = {record.event_id: record for record in store.stored_events()}
    for event in request.events:
        assert stored[event.event_id].digest == compute_event_digest(event)


def test_identical_duplicate_post_returns_same_receipt_and_one_fact():
    store = FakeIngestionStore()
    request = telemetry_ingestion___load("batch_valid.json")
    first = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    second = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert second.receipt_id == first.receipt_id
    assert store.event_count() == len(request.events)


def test_duplicate_event_in_new_batch_is_acknowledged_not_a_second_fact():
    store = FakeIngestionStore()
    valid = telemetry_ingestion___load("batch_valid.json")
    telemetry_ingestion___ingest(valid, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    duplicate_batch = telemetry_ingestion___load("batch_duplicate_conflict.json")
    ack = telemetry_ingestion___ingest(duplicate_batch, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    by_id = telemetry_ingestion___by_id(ack.duplicate)
    assert valid.events[0].event_id in by_id
    assert by_id[valid.events[0].event_id].stored_digest == compute_event_digest(
        valid.events[0]
    )
    # The colliding event is a permanent integrity conflict, not an overwrite.
    rejected = telemetry_ingestion___by_id(ack.rejected)
    colliding_id = valid.events[1].event_id
    assert rejected[colliding_id].reason == IngestionReasonCode.INTEGRITY_CONFLICT
    assert store.event_count() == len(valid.events)

    original = {r.event_id: r for r in store.stored_events()}[colliding_id]
    assert original.digest == compute_event_digest(valid.events[1])
    assert original.envelope["payload"] == valid.events[1].payload


def test_mixed_valid_and_out_of_scope_batch_stores_only_valid_ids():
    store = FakeIngestionStore()
    request = telemetry_ingestion___load("mixed_valid_revoked.json")

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    accepted_ids = {a.event_id for a in ack.accepted}
    assert accepted_ids == {request.events[0].event_id}
    rejected = telemetry_ingestion___by_id(ack.rejected)
    assert rejected[request.events[1].event_id].reason == (
        IngestionReasonCode.SESSION_OUT_OF_SCOPE
    )
    assert store.event_count() == 1


def test_batch_spanning_two_sessions_rejects_only_out_of_scope_ids():
    store = FakeIngestionStore()
    in_scope = telemetry_ingestion___event(uuid.uuid4(), 1)
    out_of_scope = telemetry_ingestion___event(uuid.uuid4(), 2, session_id=telemetry_ingestion__OTHER_SESS)
    request = telemetry_ingestion___request([in_scope, out_of_scope])

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert {a.event_id for a in ack.accepted} == {in_scope.event_id}
    assert telemetry_ingestion___by_id(ack.rejected)[out_of_scope.event_id].reason == (
        IngestionReasonCode.SESSION_OUT_OF_SCOPE
    )
    assert store.event_count() == 1


# ---------------------------------------------------------------------------
# Validation and malformed batches
# ---------------------------------------------------------------------------


def test_sensitive_payload_is_rejected_permanently():
    store = FakeIngestionStore()
    event = telemetry_ingestion___event(
        uuid.uuid4(), 1, payload={"tool_name": "read", "api_key": "sk-CANARY-INGEST-0001"}
    )
    request = telemetry_ingestion___request([event])

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert ack.rejected[0].reason == IngestionReasonCode.SENSITIVE_PAYLOAD
    assert store.event_count() == 0


def test_unsupported_schema_version_is_rejected():
    store = FakeIngestionStore()
    event = telemetry_ingestion___event(uuid.uuid4(), 1, schema_version="2")
    request = telemetry_ingestion___request([event], schema_version="2")

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert ack.rejected[0].reason == IngestionReasonCode.UNKNOWN_SCHEMA_VERSION
    assert store.event_count() == 0


def test_oversized_batch_is_rejected_without_storing_anything():
    store = FakeIngestionStore()
    events = [telemetry_ingestion___event(uuid.uuid4(), i + 1) for i in range(3)]
    request = telemetry_ingestion___request(events)

    ack = telemetry_ingestion___ingest(
        request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session(), max_events=2
    )

    assert all(a.reason == IngestionReasonCode.BATCH_TOO_LARGE for a in ack.rejected)
    assert store.event_count() == 0


# ---------------------------------------------------------------------------
# Authorization: capability and enrollment
# ---------------------------------------------------------------------------


def test_expired_capability_is_retryable_and_stores_nothing():
    store = FakeIngestionStore()
    capability = telemetry_ingestion___capability(now=telemetry_ingestion__NOW - timedelta(seconds=1200), ttl=60)
    request = telemetry_ingestion___request([telemetry_ingestion___event(uuid.uuid4(), 1)], capability=capability)

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert all(
        a.reason == IngestionReasonCode.CAPABILITY_INVALID
        and a.disposition == EventDisposition.RETRYABLE
        for a in ack.retryable
    )
    assert ack.retry_hint is not None
    assert store.event_count() == 0
    # A retryable batch is not cached as a terminal receipt.
    assert store.get_receipt(request.batch_id) is None


def test_revoked_capability_epoch_is_rejected_and_stores_nothing():
    store = FakeIngestionStore()
    request = telemetry_ingestion___request([telemetry_ingestion___event(uuid.uuid4(), 1)], capability=telemetry_ingestion___capability(epoch=0))

    ack = telemetry_ingestion___ingest(
        request, store, enrollment=telemetry_ingestion___enrollment(epoch=5), session=telemetry_ingestion___session()
    )

    assert ack.rejected[0].reason == IngestionReasonCode.REVOKED
    assert store.event_count() == 0


def test_non_active_enrollment_is_rejected_and_stores_nothing():
    store = FakeIngestionStore()
    request = telemetry_ingestion___request([telemetry_ingestion___event(uuid.uuid4(), 1)])

    ack = telemetry_ingestion___ingest(
        request,
        store,
        enrollment=telemetry_ingestion___enrollment(status=EnrollmentStatus.COMPLETED),
        session=telemetry_ingestion___session(),
    )

    assert ack.rejected[0].reason == IngestionReasonCode.ENROLLMENT_NOT_ACTIVE
    assert store.event_count() == 0


def test_cookie_identity_is_not_accepted_in_place_of_a_capability():
    # The batch endpoint takes no authenticated user dependency: the only
    # accepted authorization is the scoped session capability.
    assert "current_user" not in inspect.signature(submit_telemetry_batch).parameters

    store = FakeIngestionStore()
    request = telemetry_ingestion___request([telemetry_ingestion___event(uuid.uuid4(), 1)], capability=telemetry_ingestion___capability())
    # A wrong-secret verifier (e.g. a cookie-derived identity is not consulted).
    ack = ingest_batch(
        request,
        capability_verifier=telemetry_ingestion___verifier("a-different-secret"),
        enrollment_resolver=lambda i: telemetry_ingestion___enrollment(),
        session_resolver=lambda i: telemetry_ingestion___session(),
        store=store,
        now=telemetry_ingestion__NOW,
    )
    assert ack.retryable[0].reason == IngestionReasonCode.CAPABILITY_INVALID
    assert store.event_count() == 0


# ---------------------------------------------------------------------------
# Ordering and store failure
# ---------------------------------------------------------------------------


def test_event_sequence_gap_is_accepted_with_a_coverage_diagnostic():
    store = FakeIngestionStore()
    first = telemetry_ingestion___event(uuid.uuid4(), 1)
    telemetry_ingestion___ingest(telemetry_ingestion___request([first]), store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    gapped = telemetry_ingestion___event(uuid.uuid4(), 5)
    ack = telemetry_ingestion___ingest(
        telemetry_ingestion___request([gapped]), store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session()
    )

    assert ack.accepted[0].reason == IngestionReasonCode.EVENT_SEQUENCE_GAP
    assert str(gapped.event_id) in ack.coverage_diagnostics
    assert store.event_count() == 2


def test_continuity_required_rejects_a_sequence_gap():
    store = FakeIngestionStore()
    first = telemetry_ingestion___event(uuid.uuid4(), 1)
    telemetry_ingestion___ingest(telemetry_ingestion___request([first]), store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    gapped = telemetry_ingestion___event(uuid.uuid4(), 5)
    ack = telemetry_ingestion___ingest(
        telemetry_ingestion___request([gapped]),
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
        continuity_required=True,
    )

    assert ack.rejected[0].reason == IngestionReasonCode.CONTINUITY_REQUIRED
    assert store.event_count() == 1


def test_store_unavailability_yields_no_false_ack():
    store = FakeIngestionStore()
    store.fail_inserts = True
    request = telemetry_ingestion___request([telemetry_ingestion___event(uuid.uuid4(), 1)])

    ack = telemetry_ingestion___ingest(request, store, enrollment=telemetry_ingestion___enrollment(), session=telemetry_ingestion___session())

    assert not ack.accepted
    assert ack.retryable[0].reason == IngestionReasonCode.STORE_UNAVAILABLE
    assert ack.retry_hint is not None
    assert store.event_count() == 0
    assert store.get_receipt(request.batch_id) is None


def test_event_digest_is_deterministic_and_content_addressed():
    event = telemetry_ingestion___event(uuid.uuid4(), 1)
    assert compute_event_digest(event) == compute_event_digest(event)
    changed = event.model_copy(update={"payload": {"tool_name": "write"}})
    assert compute_event_digest(changed) != compute_event_digest(event)


def test_telemetry_routes_are_wired_under_the_research_prefix():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/telemetry/batches" in paths
    assert "/research/telemetry/receipts/{receipt_id}" in paths


# --------------------------------------------------------------------------
# test_batch_acknowledgements
# --------------------------------------------------------------------------
# Tests for deterministic, disjoint ingestion acknowledgements (Issue 09).
batch_acknowledgements__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
batch_acknowledgements__SECRET = "ack-secret"
batch_acknowledgements__STUDY = uuid.uuid4()
batch_acknowledgements__REV = uuid.uuid4()
batch_acknowledgements__ENR = uuid.uuid4()
batch_acknowledgements__SESS = uuid.uuid4()


def batch_acknowledgements___enrollment() -> Enrollment:
    return Enrollment(
        enrollment_id=batch_acknowledgements__ENR,
        participant_id=uuid.uuid4(),
        study_id=batch_acknowledgements__STUDY,
        study_revision_id=batch_acknowledgements__REV,
        participant_code="p_synthetic",
        status=EnrollmentStatus.ACTIVE,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=batch_acknowledgements__NOW
        ),
        enrolled_at=batch_acknowledgements__NOW,
        updated_at=batch_acknowledgements__NOW,
    )


def batch_acknowledgements___session() -> ResearchSessionV1:
    return ResearchSessionV1(
        research_session_id=batch_acknowledgements__SESS,
        enrollment_id=batch_acknowledgements__ENR,
        study_revision_id=batch_acknowledgements__REV,
        state=SessionState.RUNNING,
        opened_at=batch_acknowledgements__NOW,
        last_activity_at=batch_acknowledgements__NOW,
        manifest_digest="sha256:" + "m" * 64,
    )


def batch_acknowledgements___event(seq: int, *, event_id: uuid.UUID | None = None, payload: dict | None = None):
    return EventBuilder().build(
        emitter_id="acp-proxy",
        event_type="tool.completed",
        source="acp",
        occurred_at=batch_acknowledgements__NOW,
        normalizer_version="generic-acp-v1",
        study_id=batch_acknowledgements__STUDY,
        revision_id=batch_acknowledgements__REV,
        enrollment_id=batch_acknowledgements__ENR,
        research_session_id=batch_acknowledgements__SESS,
        payload=payload if payload is not None else {"tool_name": "read"},
        event_id=event_id or uuid.uuid4(),
        emitter_sequence=seq,
    )


def batch_acknowledgements___request(events) -> TelemetryBatchRequestV1:
    capability = issue_capability(
        "research-runtime",
        ["telemetry:write"],
        600,
        0,
        batch_acknowledgements__SECRET,
        now=batch_acknowledgements__NOW,
        enrollment_id=batch_acknowledgements__ENR,
        research_session_id=batch_acknowledgements__SESS,
        revision_id=batch_acknowledgements__REV,
    )
    return TelemetryBatchRequestV1(
        batch_id=uuid.uuid4(),
        session_capability=capability,
        events=events,
        client_instance_id="client-1",
    )


def batch_acknowledgements___ingest(request, store):
    def verifier(
        capability,
        *,
        now,
        current_revocation_epoch,
        expected_enrollment_id=None,
        expected_research_session_id=None,
        expected_revision_id=None,
    ):
        return verify_capability(
            capability,
            batch_acknowledgements__SECRET,
            "research-runtime",
            ["telemetry:write"],
            now=now,
            current_revocation_epoch=current_revocation_epoch,
            expected_enrollment_id=expected_enrollment_id,
            expected_research_session_id=expected_research_session_id,
            expected_revision_id=expected_revision_id,
        )

    return ingest_batch(
        request,
        capability_verifier=verifier,
        enrollment_resolver=lambda i: batch_acknowledgements___enrollment(),
        session_resolver=lambda i: batch_acknowledgements___session(),
        store=store,
        now=batch_acknowledgements__NOW,
    )


def batch_acknowledgements___groups(ack: TelemetryBatchAckV1) -> dict[str, set[str]]:
    def ids(acks):
        return {str(a.event_id) for a in acks if a.event_id is not None}

    return {
        "accepted": ids(ack.accepted),
        "duplicate": ids(ack.duplicate),
        "rejected": ids(ack.rejected),
        "retryable": ids(ack.retryable),
    }


def test_acknowledgement_is_deterministic_for_identical_events():
    events = [batch_acknowledgements___event(1), batch_acknowledgements___event(2)]
    first_store = FakeIngestionStore()
    second_store = FakeIngestionStore()

    first = batch_acknowledgements___ingest(batch_acknowledgements___request(events), first_store)
    second = batch_acknowledgements___ingest(batch_acknowledgements___request(events), second_store)

    assert batch_acknowledgements___groups(first) == batch_acknowledgements___groups(second)
    assert first.accepted[0].reason == second.accepted[0].reason


def test_event_id_never_appears_in_two_groups():
    event_id = uuid.uuid4()
    with pytest.raises(PydanticValidationError):
        TelemetryBatchAckV1(
            receipt_id=uuid.uuid4(),
            batch_id=uuid.uuid4(),
            server_time=batch_acknowledgements__NOW,
            accepted=[
                EventAck(event_id=event_id, disposition=EventDisposition.ACCEPTED)
            ],
            rejected=[
                EventAck(event_id=event_id, disposition=EventDisposition.REJECTED)
            ],
        )


def test_mixed_batch_groups_each_id_exactly_once():
    valid = batch_acknowledgements___event(1)
    conflicting_id = uuid.uuid4()
    seeded_store = FakeIngestionStore()
    batch_acknowledgements___ingest(batch_acknowledgements___request([valid, batch_acknowledgements___event(2, event_id=conflicting_id)]), seeded_store)

    duplicate = batch_acknowledgements___event(1, event_id=valid.event_id, payload={"tool_name": "read"})
    colliding = batch_acknowledgements___event(9, event_id=conflicting_id, payload={"tool_name": "write"})
    request = batch_acknowledgements___request([duplicate, colliding])

    ack = batch_acknowledgements___ingest(request, seeded_store)

    groups = batch_acknowledgements___groups(ack)
    all_ids = [event_id for group in groups.values() for event_id in group]
    assert len(all_ids) == len(set(all_ids))
    assert str(valid.event_id) in groups["duplicate"]
    assert str(conflicting_id) in groups["rejected"]


def test_pre_existing_emitter_sequence_conflict_rejects_only_the_poison_event():
    store = FakeIngestionStore()
    seeded = batch_acknowledgements___event(1)
    batch_acknowledgements___ingest(
        batch_acknowledgements___request([seeded]), store
    )
    assert store.event_count() == 1

    # Same (session, emitter, sequence) as the stored fact, but a different
    # event id: this is the poison event once telemetry storage resumes.
    poison_id = uuid.uuid4()
    poison = batch_acknowledgements___event(
        1, event_id=poison_id, payload={"tool_name": "write"}
    )
    sibling_id = uuid.uuid4()
    sibling = batch_acknowledgements___event(
        2, event_id=sibling_id, payload={"tool_name": "read"}
    )

    ack = batch_acknowledgements___ingest(
        batch_acknowledgements___request([poison, sibling]), store
    )

    groups = batch_acknowledgements___groups(ack)
    assert str(poison_id) in groups["rejected"]
    assert str(sibling_id) in groups["accepted"]
    # The poison event must no longer drag its siblings into retryable limbo.
    assert groups["retryable"] == set()

    poison_ack = next(a for a in ack.rejected if a.event_id == poison_id)
    assert poison_ack.reason == IngestionReasonCode.INTEGRITY_CONFLICT

    # The sibling is durably persisted; only the poison event is discarded.
    stored_ids = {str(record.event_id) for record in store.stored_events()}
    assert str(sibling_id) in stored_ids
    assert str(poison_id) not in stored_ids
    assert store.event_count() == 2


def test_multiple_poison_events_still_persist_every_clean_sibling():
    store = FakeIngestionStore()
    batch_acknowledgements___ingest(
        batch_acknowledgements___request(
            [batch_acknowledgements___event(1), batch_acknowledgements___event(2)]
        ),
        store,
    )

    poison_a = uuid.uuid4()
    poison_b = uuid.uuid4()
    clean = uuid.uuid4()
    request = batch_acknowledgements___request(
        [
            batch_acknowledgements___event(1, event_id=poison_a),
            batch_acknowledgements___event(2, event_id=poison_b),
            batch_acknowledgements___event(3, event_id=clean),
        ]
    )

    ack = batch_acknowledgements___ingest(request, store)

    groups = batch_acknowledgements___groups(ack)
    assert groups["rejected"] == {str(poison_a), str(poison_b)}
    assert groups["accepted"] == {str(clean)}
    assert groups["retryable"] == set()
    assert str(clean) in {str(record.event_id) for record in store.stored_events()}


def test_two_process_emitters_may_share_a_sequence_in_one_session():
    """Distinct per-process emitter ids make the (session, emitter, seq) key safe."""
    store = FakeIngestionStore()
    now = batch_acknowledgements__NOW

    def event(emitter_id: str, seq: int, event_id: uuid.UUID) -> CanonicalEventV1:
        return EventBuilder().build(
            emitter_id=emitter_id,
            event_type="tool.completed",
            source="acp",
            occurred_at=now,
            normalizer_version="generic-acp-v1",
            study_id=batch_acknowledgements__STUDY,
            revision_id=batch_acknowledgements__REV,
            enrollment_id=batch_acknowledgements__ENR,
            research_session_id=batch_acknowledgements__SESS,
            payload={"tool_name": "read"},
            event_id=event_id,
            emitter_sequence=seq,
        )

    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    ack = batch_acknowledgements___ingest(
        batch_acknowledgements___request(
            [
                event("acp-proxy:11111111", 1, first_id),
                event("acp-proxy:22222222", 1, second_id),
            ]
        ),
        store,
    )

    groups = batch_acknowledgements___groups(ack)
    assert groups["accepted"] == {str(first_id), str(second_id)}
    assert groups["rejected"] == set()
    assert groups["retryable"] == set()
    assert store.event_count() == 2


def test_batch_ack_is_logged_with_counts_and_reason_codes(caplog, monkeypatch):
    from backend.routers.research import telemetry as telemetry_router

    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        server_time=batch_acknowledgements__NOW,
        accepted=[
            EventAck(
                event_id=uuid.uuid4(), disposition=EventDisposition.ACCEPTED
            )
        ],
        rejected=[
            EventAck(
                event_id=uuid.uuid4(),
                disposition=EventDisposition.REJECTED,
                reason=IngestionReasonCode.INTEGRITY_CONFLICT,
            )
        ],
        retryable=[
            EventAck(
                event_id=uuid.uuid4(),
                disposition=EventDisposition.RETRYABLE,
                reason=IngestionReasonCode.STORE_UNAVAILABLE,
            )
        ],
    )
    monkeypatch.setattr(telemetry_router, "ingest_batch", lambda *args, **kwargs: ack)

    payload = TelemetryBatchRequestV1(
        batch_id=ack.batch_id,
        session_capability=batch_acknowledgements___request([]).session_capability,
        events=[],
        client_instance_id="client-ack-log",
    )

    class _FakeApp:
        def get_db_session(self):
            return MagicMock()

    with caplog.at_level(logging.WARNING, logger=telemetry_router.__name__):
        response = submit_telemetry_batch(payload, app=_FakeApp())

    assert response.status_code == 200
    logged = caplog.text
    assert str(ack.batch_id) in logged
    assert "accepted=1" in logged
    assert "rejected=1" in logged
    assert "retryable=1" in logged
    assert IngestionReasonCode.INTEGRITY_CONFLICT.value in logged
    assert IngestionReasonCode.STORE_UNAVAILABLE.value in logged


def test_null_acknowledgement_never_means_accepted():
    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(), batch_id=uuid.uuid4(), server_time=batch_acknowledgements__NOW
    )
    assert ack.is_empty is True
    assert ack.acknowledged_ids() == []

    rejected_only = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        server_time=batch_acknowledgements__NOW,
        rejected=[
            EventAck(
                event_id=uuid.uuid4(),
                disposition=EventDisposition.REJECTED,
                reason=IngestionReasonCode.SENSITIVE_PAYLOAD,
            )
        ],
    )
    # Rejected ids are not acknowledged (the spool must not delete them silently).
    assert rejected_only.acknowledged_ids() == []


def test_acknowledged_ids_are_accepted_plus_duplicate_only():
    accepted_id = uuid.uuid4()
    duplicate_id = uuid.uuid4()
    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        server_time=batch_acknowledgements__NOW,
        accepted=[
            EventAck(event_id=accepted_id, disposition=EventDisposition.ACCEPTED)
        ],
        duplicate=[
            EventAck(event_id=duplicate_id, disposition=EventDisposition.DUPLICATE)
        ],
    )
    assert set(ack.acknowledged_ids()) == {accepted_id, duplicate_id}


def test_receipt_is_immutable_and_retry_safe():
    store = FakeIngestionStore()
    events = [batch_acknowledgements___event(1)]
    request = batch_acknowledgements___request(events)

    first = batch_acknowledgements___ingest(request, store)
    retried = batch_acknowledgements___ingest(request, store)

    assert retried.receipt_id == first.receipt_id
    assert store.event_count() == 1

    # A second receipt for the same batch cannot replace the first.
    replacement = BatchReceipt(
        receipt_id=uuid.uuid4(),
        batch_id=request.batch_id,
        accepted_at=batch_acknowledgements__NOW,
        ack=TelemetryBatchAckV1(
            receipt_id=uuid.uuid4(), batch_id=request.batch_id, server_time=batch_acknowledgements__NOW
        ),
    )
    store.record_batch_receipt(replacement)
    assert store.get_receipt(request.batch_id).receipt_id == first.receipt_id


# --------------------------------------------------------------------------
# test_ingestion_provenance
# --------------------------------------------------------------------------
# Provenance round-trip regression tests for canonical telemetry ingestion.
#
# The canonical :class:`~research.telemetry.models.Provenance` carried by a
# ``CanonicalEventV1`` is copied verbatim onto ``ResearchEventRecord`` and
# persisted by :func:`research.telemetry.ingestion.ingest_batch`. These tests pin that
# contract: every provenance field survives ingestion unchanged (no stripping, no
# default substitution), the fidelity stays the lowercase canonical wire value,
# and nullable fields stay ``None`` rather than becoming ``""`` or a default.
#
# The in-memory :class:`~research.telemetry.ingestion.FakeIngestionStore` is used, so no
# PostgreSQL is required.
ingestion_provenance__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
ingestion_provenance__SECRET = "fixture-ingestion-secret"

ingestion_provenance__STUDY = uuid.UUID("11111111-1111-4111-8111-111111111111")
ingestion_provenance__REV = uuid.UUID("22222222-2222-4222-8222-222222222222")
ingestion_provenance__ENR = uuid.UUID("33333333-3333-4333-8333-333333333333")
ingestion_provenance__SESS = uuid.UUID("44444444-4444-4444-8444-444444444444")

ingestion_provenance__NORMALIZER_VERSION = "generic-acp-v1"
ingestion_provenance__ADAPTER_VERSION = "codex-v1"
ingestion_provenance__SOURCE_EVENT_ID = "acp-source-0001"
ingestion_provenance__EVIDENCE_DIGEST = "sha256:" + "e" * 64


def ingestion_provenance___enrollment() -> Enrollment:
    return Enrollment(
        enrollment_id=ingestion_provenance__ENR,
        participant_id=uuid.uuid4(),
        study_id=ingestion_provenance__STUDY,
        study_revision_id=ingestion_provenance__REV,
        participant_code="p_synthetic",
        status=EnrollmentStatus.ACTIVE,
        eligibility=ResearchEligibility(
            eligible=True, reasons=[IdentityReasonCode.ELIGIBLE], evaluated_at=ingestion_provenance__NOW
        ),
        enrolled_at=ingestion_provenance__NOW,
        updated_at=ingestion_provenance__NOW,
        revocation_epoch=0,
    )


def ingestion_provenance___session() -> ResearchSessionV1:
    return ResearchSessionV1(
        research_session_id=ingestion_provenance__SESS,
        enrollment_id=ingestion_provenance__ENR,
        study_revision_id=ingestion_provenance__REV,
        state=SessionState.RUNNING,
        opened_at=ingestion_provenance__NOW,
        last_activity_at=ingestion_provenance__NOW,
        manifest_digest="sha256:" + "m" * 64,
    )


def ingestion_provenance___event(
    event_id: uuid.UUID,
    seq: int,
    *,
    source_event_id: str | None = ingestion_provenance__SOURCE_EVENT_ID,
    adapter_version: str | None = ingestion_provenance__ADAPTER_VERSION,
    fidelity: CanonicalFidelity = CanonicalFidelity.NORMALIZED,
    evidence_digest: str | None = ingestion_provenance__EVIDENCE_DIGEST,
) -> CanonicalEventV1:
    return EventBuilder().build(
        emitter_id="acp-proxy",
        event_type="tool.completed",
        source="acp",
        occurred_at=ingestion_provenance__NOW,
        normalizer_version=ingestion_provenance__NORMALIZER_VERSION,
        source_event_id=source_event_id,
        adapter_version=adapter_version,
        fidelity=fidelity,
        evidence_digest=evidence_digest,
        study_id=ingestion_provenance__STUDY,
        revision_id=ingestion_provenance__REV,
        enrollment_id=ingestion_provenance__ENR,
        research_session_id=ingestion_provenance__SESS,
        payload={"tool_name": "read"},
        metrics=EventMetrics(
            usage_tokens=None,
            usage_capability=Coverage(state=CoverageState.UNAVAILABLE, capability="usage"),
        ),
        event_id=event_id,
        emitter_sequence=seq,
    )


def ingestion_provenance___capability():
    return issue_capability(
        audience="research-runtime",
        scope=["telemetry:write"],
        ttl_seconds=600,
        revocation_epoch=0,
        secret=ingestion_provenance__SECRET,
        now=ingestion_provenance__NOW,
        enrollment_id=ingestion_provenance__ENR,
        research_session_id=ingestion_provenance__SESS,
        revision_id=ingestion_provenance__REV,
    )


def ingestion_provenance___verifier():
    def verify(
        capability,
        *,
        now,
        current_revocation_epoch,
        expected_enrollment_id=None,
        expected_research_session_id=None,
        expected_revision_id=None,
    ):
        return verify_capability(
            capability,
            ingestion_provenance__SECRET,
            expected_audience="research-runtime",
            expected_scope=["telemetry:write"],
            now=now,
            current_revocation_epoch=current_revocation_epoch,
            expected_enrollment_id=expected_enrollment_id,
            expected_research_session_id=expected_research_session_id,
            expected_revision_id=expected_revision_id,
        )

    return verify


def ingestion_provenance___request(events) -> TelemetryBatchRequestV1:
    return TelemetryBatchRequestV1(
        batch_id=uuid.uuid4(),
        session_capability=ingestion_provenance___capability(),
        events=events,
        client_instance_id="client-synthetic-1",
    )


def ingestion_provenance___ingest(request, store):
    return ingest_batch(
        request,
        capability_verifier=ingestion_provenance___verifier(),
        enrollment_resolver=lambda i: ingestion_provenance___enrollment() if i == ingestion_provenance__ENR else None,
        session_resolver=lambda i: ingestion_provenance___session() if i == ingestion_provenance__SESS else None,
        store=store,
        now=ingestion_provenance__NOW,
    )


def ingestion_provenance___stored_record(store, event_id: uuid.UUID):
    stored = {record.event_id: record for record in store.stored_events()}
    assert event_id in stored
    return stored[event_id]


def test_full_provenance_round_trips_through_ingest_batch():
    store = FakeIngestionStore()
    event = ingestion_provenance___event(uuid.uuid4(), 1)

    ack = ingestion_provenance___ingest(ingestion_provenance___request([event]), store)

    assert [a.event_id for a in ack.accepted] == [event.event_id]
    assert ack.rejected == [] and ack.retryable == []
    assert store.event_count() == 1

    record = ingestion_provenance___stored_record(store, event.event_id)

    # Every field is copied exactly, with no stripping or default substitution.
    assert record.envelope["provenance"] == {
        "source": "acp",
        "source_event_id": ingestion_provenance__SOURCE_EVENT_ID,
        "normalizer_version": ingestion_provenance__NORMALIZER_VERSION,
        "adapter_version": ingestion_provenance__ADAPTER_VERSION,
        "fidelity": "normalized",
        "evidence_digest": ingestion_provenance__EVIDENCE_DIGEST,
    }
    # Fidelity stays the lowercase canonical wire value (not the uppercase
    # receipt vocabulary and not the enum's repr).
    assert record.envelope["provenance"]["fidelity"] == CanonicalFidelity.NORMALIZED.value
    assert record.envelope["provenance"]["fidelity"] == "normalized"
    # The record-level ``source`` mirrors the canonical source too.
    assert record.source == "acp"
    assert record.envelope["provenance"]["source"] == record.source


def test_nullable_provenance_fields_stay_none_through_ingest_batch():
    store = FakeIngestionStore()
    event = ingestion_provenance___event(uuid.uuid4(), 1, source_event_id=None, adapter_version=None)

    ack = ingestion_provenance___ingest(ingestion_provenance___request([event]), store)

    assert [a.event_id for a in ack.accepted] == [event.event_id]
    assert store.event_count() == 1

    record = ingestion_provenance___stored_record(store, event.event_id)

    # Nullable fields are present in the persisted mapping but stay None; they
    # are not dropped, coerced to "", or replaced with a default.
    assert set(record.envelope["provenance"]) == {
        "source",
        "source_event_id",
        "normalizer_version",
        "adapter_version",
        "fidelity",
        "evidence_digest",
    }
    assert record.envelope["provenance"]["source_event_id"] is None
    assert record.envelope["provenance"]["adapter_version"] is None
    assert record.envelope["provenance"]["source_event_id"] != ""
    assert record.envelope["provenance"]["adapter_version"] != ""
    # Non-nullable fields are still populated exactly.
    assert record.envelope["provenance"]["source"] == "acp"
    assert record.envelope["provenance"]["normalizer_version"] == ingestion_provenance__NORMALIZER_VERSION
    assert record.envelope["provenance"]["fidelity"] == "normalized"
    assert record.envelope["provenance"]["evidence_digest"] == ingestion_provenance__EVIDENCE_DIGEST


# --------------------------------------------------------------------------
# test_privacy_filter
# --------------------------------------------------------------------------
# Tests for the canonical privacy filter (Issue 06).
#
# Includes property-style tests (hypothesis) that synthetic secrets nested
# anywhere never survive filtering/serialization under any policy.
privacy_filter__FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "research" / "telemetry"
)
privacy_filter__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

privacy_filter__CANARY_KEY = "sk-CANARY-SECRET-0000000001"
privacy_filter__CANARY_GHP = "ghp_CANARYSECRET0000000002"
privacy_filter__CANARY_BEARER = "Bearer CANARY-BEARER-0000000003"
privacy_filter__CANARY_PEM = "-----BEGIN RSA PRIVATE KEY-----\nCANARY-PEM\n-----END RSA PRIVATE KEY-----"
privacy_filter__CANARY_PASSWORD = "hunter2-CANARY-PASSWORD"
privacy_filter__CANARY_PROMPT = "CANARY-PROMPT-TEXT"
privacy_filter__CANARY_REASONING = "CANARY-REASONING-TEXT"

privacy_filter___SECRET_CANARIES = (privacy_filter__CANARY_KEY, privacy_filter__CANARY_GHP, privacy_filter__CANARY_BEARER, privacy_filter__CANARY_PEM)


def privacy_filter___load(name: str):
    return json.loads((privacy_filter__FIXTURE_DIR / name).read_text())


def privacy_filter___allow_everything() -> PrivacyPolicy:
    return PrivacyPolicy(
        allowed_field_classes=[
            FieldClass.SYSTEM,
            FieldClass.BEHAVIORAL,
            FieldClass.CODE_METADATA,
            FieldClass.CONTENT,
        ],
        content_allowed=True,
        consent_active=True,
        code_metadata_mode="allow",
    )


def privacy_filter___event_with_payload(payload: dict) -> CanonicalEventV1:
    return EventBuilder().build(
        emitter_id="privacy-test",
        event_type=CanonicalEventType.TOOL_STARTED,
        source=EventSource.ACP,
        occurred_at=privacy_filter__NOW,
        normalizer_version="v1",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("api_key", "x", FieldClass.SECRET),
        ("password", "x", FieldClass.SECRET),
        ("authorization", "x", FieldClass.SECRET),
        ("private_key", "x", FieldClass.SECRET),
        ("tool_name", "read", FieldClass.BEHAVIORAL),
        ("duration_ms", 5, FieldClass.SYSTEM),
        ("usage_tokens", 12, FieldClass.SYSTEM),
        ("path", "/a/b.py", FieldClass.CODE_METADATA),
        ("language", "python", FieldClass.CODE_METADATA),
        ("prompt", "hello", FieldClass.CONTENT),
        ("reasoning", "hello", FieldClass.CONTENT),
        ("note", "hello", FieldClass.CONTENT),
    ],
)
def test_classify_field_taxonomy(name, value, expected):
    assert classify_field(name, value) == expected


def test_usage_tokens_is_not_classified_as_secret():
    assert classify_field("usage_tokens", 12) == FieldClass.SYSTEM
    assert is_secret_key("usage_tokens") is False
    assert is_secret_key("token") is True
    assert is_secret_key("session_token") is True


def test_secret_value_shapes_override_innocent_keys():
    assert classify_field("note", privacy_filter__CANARY_KEY) == FieldClass.SECRET
    assert classify_field("note", privacy_filter__CANARY_GHP) == FieldClass.SECRET
    assert classify_field("note", privacy_filter__CANARY_BEARER) == FieldClass.SECRET
    assert classify_field("note", privacy_filter__CANARY_PEM) == FieldClass.SECRET
    assert looks_secret_value("plain text") is False


def test_contains_secret_value_finds_nested_secrets():
    payload = privacy_filter___load("secrets_canary.json")
    assert contains_secret_value(payload) is True
    assert contains_secret_value({"safe": {"tool_name": "read"}}) is False


def test_classify_event_payload_flattens_paths():
    classified = classify_event_payload(
        {"tool_name": "read", "nested": {"api_key": "x"}, "prompt": "hi"}
    )
    assert classified["tool_name"] == FieldClass.BEHAVIORAL
    assert classified["nested.api_key"] == FieldClass.SECRET
    assert classified["prompt"] == FieldClass.CONTENT


# ---------------------------------------------------------------------------
# Policy action resolution (every FieldClass / every PolicyAction)
# ---------------------------------------------------------------------------


def test_default_policy_maps_every_field_class():
    policy = PrivacyPolicy.default()
    assert policy.action_for(FieldClass.SYSTEM) == PolicyAction.ALLOW
    assert policy.action_for(FieldClass.BEHAVIORAL) == PolicyAction.ALLOW
    assert policy.action_for(FieldClass.CODE_METADATA) == PolicyAction.HASH
    assert policy.action_for(FieldClass.CONTENT) == PolicyAction.REDACT
    assert policy.action_for(FieldClass.SECRET) == PolicyAction.DROP


def test_content_requires_allowance_and_active_consent():
    no_consent = PrivacyPolicy(content_allowed=True, consent_active=False)
    assert no_consent.action_for(FieldClass.CONTENT) == PolicyAction.REDACT

    with_consent = PrivacyPolicy(content_allowed=True, consent_active=True)
    assert with_consent.action_for(FieldClass.CONTENT) == PolicyAction.ALLOW

    not_allowed = PrivacyPolicy(content_allowed=False, consent_active=True)
    assert not_allowed.action_for(FieldClass.CONTENT) == PolicyAction.REDACT


def test_blocked_class_yields_block_action():
    policy = PrivacyPolicy(blocked_field_classes=[FieldClass.BEHAVIORAL])
    assert policy.action_for(FieldClass.BEHAVIORAL) == PolicyAction.BLOCK


def test_code_metadata_mode_allow():
    policy = PrivacyPolicy(code_metadata_mode="allow")
    assert policy.action_for(FieldClass.CODE_METADATA) == PolicyAction.ALLOW


def test_from_revision_policy_maps_protocol_field_classes():
    structural = PrivacyPolicy.from_revision_policy(
        TelemetryPolicy(allowed_field_classes=[TelemetryFieldClass.STRUCTURAL]),
        consent_active=True,
    )
    assert FieldClass.SYSTEM in structural.allowed_field_classes
    assert structural.content_allowed is False
    assert structural.action_for(FieldClass.CONTENT) == PolicyAction.REDACT
    assert structural.policy_digest

    with_content = PrivacyPolicy.from_revision_policy(
        TelemetryPolicy(
            allowed_field_classes=[
                TelemetryFieldClass.STRUCTURAL,
                TelemetryFieldClass.CONTENT,
            ]
        ),
        consent_active=False,
    )
    assert with_content.content_allowed is True
    # Allowed by the revision but consent is not active: still redacted.
    assert with_content.action_for(FieldClass.CONTENT) == PolicyAction.REDACT

    consented = PrivacyPolicy.from_revision_policy(
        TelemetryPolicy(
            allowed_field_classes=[
                TelemetryFieldClass.STRUCTURAL,
                TelemetryFieldClass.CONTENT,
            ]
        ),
        consent_active=True,
    )
    assert consented.action_for(FieldClass.CONTENT) == PolicyAction.ALLOW


# ---------------------------------------------------------------------------
# Engine behaviour
# ---------------------------------------------------------------------------


def test_filter_drops_secret_hashes_metadata_and_redacts_content():
    payload = {
        "tool_name": "read",
        "duration_ms": 5,
        "path": "/Users/synthetic/project/main.py",
        "prompt": privacy_filter__CANARY_PROMPT,
        "api_key": privacy_filter__CANARY_KEY,
    }
    filtered, summary = filter_payload(payload, PrivacyPolicy.default())

    assert filtered["tool_name"] == "read"
    assert filtered["duration_ms"] == 5
    assert filtered["path"].startswith("sha256:")
    assert filtered["prompt"] == "[REDACTED]"
    assert "api_key" not in filtered
    serialized = json.dumps(filtered)
    assert "/Users/synthetic" not in serialized
    assert privacy_filter__CANARY_KEY not in serialized
    assert privacy_filter__CANARY_PROMPT not in serialized
    assert summary.blocked is False
    assert summary.actions[PolicyAction.DROP.value] >= 1
    assert summary.actions[PolicyAction.HASH.value] == 1
    assert summary.actions[PolicyAction.REDACT.value] == 1
    assert "api_key" in summary.redacted_fields
    assert summary.field_classes[FieldClass.SECRET.value] == 1


def test_filter_allows_content_only_under_allowance_and_consent():
    payload = {"prompt": privacy_filter__CANARY_PROMPT, "reasoning": privacy_filter__CANARY_REASONING}
    denied, _ = filter_payload(payload, PrivacyPolicy.default())
    assert denied["prompt"] == "[REDACTED]"
    assert denied["reasoning"] == "[REDACTED]"

    allowed, _ = filter_payload(payload, privacy_filter___allow_everything())
    assert allowed["prompt"] == privacy_filter__CANARY_PROMPT
    assert allowed["reasoning"] == privacy_filter__CANARY_REASONING


def test_reasoning_and_thoughts_prohibited_by_default():
    filtered, _ = filter_payload(
        {"reasoning": privacy_filter__CANARY_REASONING, "thought": privacy_filter__CANARY_REASONING},
        PrivacyPolicy.default(),
    )
    assert filtered["reasoning"] == "[REDACTED]"
    assert filtered["thought"] == "[REDACTED]"


def test_secret_never_serializes_even_under_allow_everything_policy():
    payload = {
        "api_key": privacy_filter__CANARY_KEY,
        "password": privacy_filter__CANARY_PASSWORD,
        "nested": {"authorization": privacy_filter__CANARY_BEARER},
        "items": [{"token": privacy_filter__CANARY_GHP}],
        "pem": {"private_key": privacy_filter__CANARY_PEM},
        "prompt": privacy_filter__CANARY_PROMPT,
    }
    filtered, summary = filter_payload(payload, privacy_filter___allow_everything())

    assert summary.blocked is False
    serialized = json.dumps(filtered)
    for canary in (privacy_filter__CANARY_KEY, privacy_filter__CANARY_PASSWORD, privacy_filter__CANARY_BEARER, privacy_filter__CANARY_GHP, privacy_filter__CANARY_PEM):
        assert canary not in serialized
    assert contains_secret_value(filtered) is False
    # Content is still allowed when explicitly consented.
    assert filtered["prompt"] == privacy_filter__CANARY_PROMPT


def test_block_action_rejects_the_whole_event():
    policy = PrivacyPolicy(blocked_field_classes=[FieldClass.BEHAVIORAL])
    filtered, summary = filter_payload({"tool_name": "read"}, policy)

    assert summary.blocked is True
    assert summary.block_reason
    assert filtered == {}


def test_scalar_list_items_with_secret_shapes_are_dropped():
    filtered, summary = filter_payload(
        {"counts": [privacy_filter__CANARY_KEY, 3]}, PrivacyPolicy.default()
    )
    assert filtered["counts"] == [3]
    assert summary.actions[PolicyAction.DROP.value] >= 1


def test_filter_event_attaches_summary_and_preserves_other_fields():
    event = privacy_filter___event_with_payload({"tool_name": "read", "prompt": privacy_filter__CANARY_PROMPT})
    result = filter_event(event, PrivacyPolicy.default())

    assert result.event.event_id == event.event_id
    assert result.event.emitter_sequence == event.emitter_sequence
    assert result.event.privacy == result.summary
    assert result.event.payload["tool_name"] == "read"
    assert result.event.payload["prompt"] == "[REDACTED]"
    assert privacy_filter__CANARY_PROMPT not in result.event.model_dump_json()


def test_privacy_summary_records_categories_and_paths():
    filtered, summary = filter_payload(
        {"nested": {"path": "/a/b.py", "api_key": privacy_filter__CANARY_KEY}},
        PrivacyPolicy.default(),
    )
    assert "nested.path" in summary.redacted_fields
    assert "nested.api_key" in summary.redacted_fields
    assert summary.field_classes[FieldClass.CODE_METADATA.value] == 1
    assert summary.field_classes[FieldClass.SECRET.value] == 1
    assert filtered["nested"]["path"].startswith("sha256:")


# ---------------------------------------------------------------------------
# Canary scans (fixture + regression)
# ---------------------------------------------------------------------------


def test_synthetic_secret_fixture_cannot_reach_serialized_output():
    fixture = privacy_filter___load("secrets_canary.json")
    canaries = (
        privacy_filter__CANARY_PASSWORD,
        "sk-CANARY-NESTED-0000000001",
        privacy_filter__CANARY_BEARER,
        privacy_filter__CANARY_PEM,
        privacy_filter__CANARY_PROMPT,
        privacy_filter__CANARY_REASONING,
        "CANARY-PEM-BODY",
    )
    for policy in (PrivacyPolicy.default(), privacy_filter___allow_everything()):
        filtered, summary = filter_payload(fixture, policy)
        serialized = json.dumps(filtered)
        for canary in canaries[:4]:
            assert canary not in serialized
        assert summary.blocked is False
        assert summary.actions

    # Under the default policy, content is also redacted.
    denied, _ = filter_payload(fixture, PrivacyPolicy.default())
    denied_serialized = json.dumps(denied)
    for canary in (privacy_filter__CANARY_PROMPT, privacy_filter__CANARY_REASONING, "CANARY-PEM-BODY"):
        assert canary not in denied_serialized


def test_regression_raw_content_cannot_appear_under_default_policy():
    payload = {
        "prompt": "CANARY-RAW-PROMPT",
        "source": "CANARY-RAW-SOURCE",
        "command_output": "CANARY-RAW-COMMAND-OUTPUT",
        "reasoning": "CANARY-RAW-REASONING",
        "raw_payload": {"text": "CANARY-RAW-PAYLOAD"},
    }
    filtered, _ = filter_payload(payload, PrivacyPolicy.default())
    serialized = json.dumps(filtered)
    for canary in (
        "CANARY-RAW-PROMPT",
        "CANARY-RAW-SOURCE",
        "CANARY-RAW-COMMAND-OUTPUT",
        "CANARY-RAW-REASONING",
        "CANARY-RAW-PAYLOAD",
    ):
        assert canary not in serialized


def test_end_to_end_event_serialization_has_no_secret_canary():
    event = privacy_filter___event_with_payload(
        {
            "tool_name": "run",
            "api_key": privacy_filter__CANARY_KEY,
            "password": privacy_filter__CANARY_PASSWORD,
            "arguments": {"command": "echo hello"},
            "content": privacy_filter__CANARY_PROMPT,
        }
    )
    filtered = filter_event(event, PrivacyPolicy.default()).event
    serialized = filtered.model_dump_json()
    for canary in (privacy_filter__CANARY_KEY, privacy_filter__CANARY_PASSWORD, privacy_filter__CANARY_PROMPT):
        assert canary not in serialized

    # Even with an allow-everything policy the secret-keyed fields are dropped;
    # only explicitly-consented content may remain.
    permissive = filter_event(event, privacy_filter___allow_everything()).event
    permissive_serialized = permissive.model_dump_json()
    assert privacy_filter__CANARY_KEY not in permissive_serialized
    assert privacy_filter__CANARY_PASSWORD not in permissive_serialized
    assert permissive.payload["content"] == privacy_filter__CANARY_PROMPT


# ---------------------------------------------------------------------------
# Property-style tests
# ---------------------------------------------------------------------------

privacy_filter___SECRET_LEAVES = st.sampled_from(
    [
        "sk-CANARY-PROP-0000000001",
        "ghp_CANARYPROP0000000002",
        "Bearer CANARY-PROP-0000000003",
        "-----BEGIN RSA PRIVATE KEY-----PROP-----END RSA PRIVATE KEY-----",
    ]
)
privacy_filter___KEYS = st.sampled_from(
    [
        "api_key",
        "password",
        "token",
        "authorization",
        "private_key",
        "tool_name",
        "note",
        "path",
        "content",
        "reasoning",
        "nested",
        "items",
    ]
)
privacy_filter___LEAVES = st.one_of(privacy_filter___SECRET_LEAVES, st.integers(), st.booleans(), st.text(max_size=8))
privacy_filter___NESTED = st.recursive(
    privacy_filter___LEAVES,
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(privacy_filter___KEYS, children, max_size=3),
    max_leaves=20,
)
privacy_filter___PAYLOADS = st.dictionaries(privacy_filter___KEYS, privacy_filter___NESTED, max_size=5)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(payload=privacy_filter___PAYLOADS)
def test_property_synthetic_secrets_never_survive_any_policy(payload):
    for policy in (PrivacyPolicy.default(), privacy_filter___allow_everything()):
        filtered, summary = filter_payload(payload, policy)
        assert summary.blocked is False
        assert contains_secret_value(filtered) is False
        serialized = json.dumps(filtered)
        for canary in (
            "sk-CANARY-PROP",
            "ghp_CANARYPROP",
            "CANARY-PROP-0000000003",
            "PROP-----END RSA PRIVATE KEY",
        ):
            assert canary not in serialized


# --------------------------------------------------------------------------
# test_projection_coverage
# --------------------------------------------------------------------------
# Tests for versioned coverage/metric projections (Issue 09).
projection_coverage__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
projection_coverage__REV = uuid.uuid4()


def projection_coverage___event(
    event_type: str,
    *,
    usage: int | None = None,
    usage_state: CoverageState = CoverageState.UNAVAILABLE,
    coverage_state: CoverageState = CoverageState.AVAILABLE,
    run_id: str | None = None,
    session_id: uuid.UUID | None = None,
    payload: dict | None = None,
):
    return EventBuilder().build(
        emitter_id="acp-proxy",
        event_type=event_type,
        source="acp",
        occurred_at=projection_coverage__NOW,
        normalizer_version="generic-acp-v1",
        revision_id=projection_coverage__REV,
        research_session_id=session_id or uuid.uuid4(),
        agent_run_id=run_id,
        payload=payload or {"tool_name": "read"},
        metrics=EventMetrics(
            usage_tokens=usage,
            usage_capability=Coverage(state=usage_state, capability="usage"),
        ),
        coverage=Coverage(state=coverage_state),
    )


def projection_coverage___record(event) -> ResearchEventRecord:
    return ResearchEventRecord(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        source=event.source,
        study_id=event.study_id,
        revision_id=event.revision_id,
        enrollment_id=event.enrollment_id,
        research_session_id=event.research_session_id,
        agent_run_id=event.agent_run_id,
        emitter_id=event.emitter_id,
        emitter_sequence=event.emitter_sequence,
        occurred_at=event.occurred_at,
        envelope=event.model_dump(mode="json"),
        digest="sha256:" + "d" * 64,
        accepted_at=projection_coverage__NOW,
    )


def test_event_family_is_the_canonical_prefix():
    assert event_family("tool.completed") == "tool"
    assert event_family("ide.file.saved") == "ide"
    assert event_family("unknown_source_event") == "unknown_source_event"


def test_coverage_shows_available_and_unavailable_explicitly():
    events = [
        projection_coverage___event("tool.completed", coverage_state=CoverageState.AVAILABLE),
        projection_coverage___event("tool.failed", coverage_state=CoverageState.UNAVAILABLE),
        projection_coverage___event("tool.failed", coverage_state=CoverageState.UNKNOWN),
        projection_coverage___event("agent.message.started", coverage_state=CoverageState.PARTIAL),
    ]

    coverage = coverage_by_family(events, projection_coverage__REV, population="tool_family", computed_at=projection_coverage__NOW)

    assert coverage.coverage_version == COVERAGE_VERSION
    assert coverage.population == "tool_family"
    assert coverage.denominators == {"events": 4, "families": 2}
    families = {family.family: family for family in coverage.families}
    assert families["tool"].total == 3
    assert families["tool"].available == 1
    assert families["tool"].unavailable == 1
    assert families["tool"].unknown == 1
    assert families["agent"].partial == 1


def test_missing_usage_is_null_with_unavailable_coverage_never_zero():
    events = [
        projection_coverage___event("tool.completed", usage=None, usage_state=CoverageState.UNAVAILABLE),
        projection_coverage___event("tool.completed", usage=None, usage_state=CoverageState.UNKNOWN),
    ]

    metric = derive_usage_metric(events)

    assert metric.value is None
    assert metric.coverage_state == CoverageState.UNAVAILABLE
    assert metric.numerator is None
    assert metric.denominator is None
    assert metric.source_event_count == 2


def test_observed_zero_usage_is_a_zero_value_with_available_coverage():
    events = [
        projection_coverage___event("tool.completed", usage=0, usage_state=CoverageState.AVAILABLE),
    ]

    metric = derive_usage_metric(events)

    assert metric.value == 0.0
    assert metric.coverage_state == CoverageState.AVAILABLE
    assert metric.numerator == 0
    assert metric.denominator == 1


def test_derived_metric_averages_only_observed_usage():
    events = [
        projection_coverage___event("tool.completed", usage=10, usage_state=CoverageState.AVAILABLE),
        projection_coverage___event("tool.completed", usage=20, usage_state=CoverageState.AVAILABLE),
        projection_coverage___event("tool.completed", usage=None, usage_state=CoverageState.UNAVAILABLE),
    ]

    metric = derive_usage_metric(events)

    assert metric.value == 15.0
    assert metric.numerator == 30
    assert metric.denominator == 2
    assert metric.source_event_count == 3


def test_derivations_are_versioned_and_retain_provenance():
    events = [projection_coverage___event("tool.completed", run_id="run-1")]
    metric = derive_usage_metric(events)
    count_metric = derive_event_count_metric(events)
    coverage = coverage_by_family(events, projection_coverage__REV)
    summary = session_summary(events, events[0].research_session_id)

    assert metric.derivation_version == DERIVATION_VERSION
    assert count_metric.derivation_version == DERIVATION_VERSION
    assert coverage.coverage_version == COVERAGE_VERSION
    assert metric.coverage_predicate
    assert summary.derivation_version == DERIVATION_VERSION
    assert summary.event_count == 1
    assert summary.agent_run_ids == ["run-1"]
    assert summary.first_occurred_at == projection_coverage__NOW
    assert summary.family_counts == {"tool": 1}


def test_projections_do_not_replace_or_mutate_source_events():
    events = [projection_coverage___event("tool.completed", usage=5, usage_state=CoverageState.AVAILABLE)]
    records = [projection_coverage___record(events[0])]
    snapshot = [event.model_dump(mode="json") for event in events]

    coverage_by_family(records, projection_coverage__REV)
    derive_usage_metric(records)
    session_summary(records)

    assert [event.model_dump(mode="json") for event in events] == snapshot
    assert len(events) == 1
    assert records[0].event_id == events[0].event_id


# --------------------------------------------------------------------------
# test_telemetry_authority_phase2
# --------------------------------------------------------------------------
# One telemetry authority: the Python model generates the JSON Schema, missing
# versions are typed, provenance/correlation are scanned, and blocked events are
# rejected at the persistence boundary.


def test_generated_schema_matches_the_checked_in_contract():
    generated = build_canonical_event_v1_schema()
    assert generated == load_canonical_event_v1_schema()
    assert serialize_canonical_event_v1_schema(generated) == (
        CANONICAL_EVENT_V1_SCHEMA_PATH.read_text(encoding="utf-8")
    )


def test_schema_event_type_vocabulary_is_the_python_enum():
    schema = load_canonical_event_v1_schema()
    expected = [event_type.value for event_type in CanonicalEventType]
    assert schema["properties"]["event_type"]["enum"] == expected
    assert schema["properties"]["schema_version"] == {"const": "1"}


def test_removed_no_producer_event_types_are_absent_everywhere():
    # No producer in the IDE collector, the shared ACP/proxy normalizer, or the
    # server: these were removed from the vocabulary rather than reserved.
    removed = {
        "system.coverage",
    }
    schema_enum = set(load_canonical_event_v1_schema()["properties"]["event_type"]["enum"])
    model_values = {event_type.value for event_type in CanonicalEventType}
    assert removed.isdisjoint(model_values)
    assert removed.isdisjoint(schema_enum)

    # Every type with a real producer remains in both the enum and the schema.
    # ``permission.decided``, ``tool.created``, ``agent.message.completed`` and
    # ``plan.updated`` now have real producers in the shared ACP normalizer
    # (Issue 08 §13.4), so they moved out of the removed set.
    produced = {
        "interaction.started",
        "interaction.completed",
        "agent.message.started",
        "agent.message.completed",
        "tool.created",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "permission.requested",
        "permission.decided",
        "plan.updated",
        "usage.updated",
        "ide.document.changed",
        "ide.file.opened",
        "ide.file.saved",
        "ide.file.closed",
        "ide.run.executed",
        "system.agent.crashed",
        "system.proxy.error",
        "agent.error",
        "unknown_source_event",
    }
    assert produced <= model_values
    assert produced == schema_enum


def test_schema_allows_unknown_source_and_lifecycle_like_the_model():
    schema = load_canonical_event_v1_schema()
    # Unknown source/lifecycle are preserved verbatim by the model, so the schema
    # must not enum-restrict them (otherwise the two contracts disagree).
    assert schema["properties"]["source"]["type"] == "string"
    assert "enum" not in schema["properties"]["source"]
    assert "enum" not in schema["properties"]["lifecycle_state"]

    accepted = telemetry_schema___load("canonical_event_accepted.json")
    accepted["source"] = "vendor-channel"
    accepted["lifecycle_state"] = "teleported"
    accepted["unknown_source"] = "vendor-channel"
    accepted["unknown_lifecycle_state"] = "teleported"
    jsonschema.validate(accepted, schema)

    restored = CanonicalEventV1.model_validate(accepted)
    assert restored.source == "vendor-channel"
    assert restored.unknown_source == "vendor-channel"
    assert restored.lifecycle_state == "teleported"
    assert restored.unknown_lifecycle_state == "teleported"


def test_shared_canonical_fixture_round_trips_python_and_kotlin_shape():
    """The shared accepted fixture is the Python<->Kotlin cross-language contract.

    Python must validate and rehydrate it unchanged, and the Kotlin client model
    must declare every wire field the fixture carries. This is the machine check
    that keeps the two hand-written models on the same envelope.
    """
    schema = load_canonical_event_v1_schema()
    fixture = telemetry_schema___load("canonical_event_accepted.json")
    jsonschema.validate(fixture, schema)

    restored = CanonicalEventV1.model_validate(fixture)
    round_tripped = json.loads(restored.model_dump_json())
    assert set(round_tripped) == set(fixture)
    assert round_tripped["provenance"] == fixture["provenance"]
    assert round_tripped["coverage"] == fixture["coverage"]

    kotlin_path = (
        Path(__file__).resolve().parents[4]
        / "code4me2"
        / "src"
        / "main"
        / "kotlin"
        / "me"
        / "code4me"
        / "research"
        / "telemetry"
        / "CanonicalEvent.kt"
    )
    if not kotlin_path.exists():  # pragma: no cover - sibling client tree absent
        pytest.skip("Kotlin client tree is not present in this checkout")
    kotlin = kotlin_path.read_text(encoding="utf-8")
    for field_name in fixture:
        assert f'"{field_name}"' in kotlin, field_name
    for nested in ("correlations", "metrics", "privacy", "provenance", "coverage"):
        assert f'"{nested}"' in kotlin, nested



def test_missing_schema_version_is_a_typed_reason_not_a_default():
    event = telemetry_schema___event()
    data = json.loads(event.model_dump_json())
    data.pop("schema_version")

    # The model requires the version: it is never silently defaulted.
    with pytest.raises(PydanticValidationError):
        CanonicalEventV1.model_validate(data)

    codes = telemetry_schema___codes(validate_canonical_event(data))
    assert TelemetryValidationCode.SCHEMA_VERSION_MISSING in codes

    # And the ingestion mapping is the typed permanent reason.
    from research.telemetry.ingestion.validation import validate_batch_event

    issues = validate_batch_event(event, None)
    assert issues == []


def test_provenance_and_correlation_secrets_are_rejected():
    provenance_event = telemetry_ingestion___event(
        uuid.uuid4(), 1, payload={"tool_name": "read"}
    ).model_copy(
        update={
            "provenance": telemetry_ingestion___event(uuid.uuid4(), 1).provenance.model_copy(
                update={"source_event_id": "sk-CANARY-PHASE2-0001"}
            )
        }
    )
    provenance_codes = telemetry_schema___codes(validate_canonical_event(provenance_event))
    assert TelemetryValidationCode.SECRET_PRESENT in provenance_codes

    correlation_event = telemetry_ingestion___event(
        uuid.uuid4(), 1, payload={"tool_name": "read"}
    ).model_copy(
        update={"correlations": Correlations(correlation_id="ghp_CANARYPHASE20000000002")}
    )
    correlation_codes = telemetry_schema___codes(validate_canonical_event(correlation_event))
    assert TelemetryValidationCode.SECRET_PRESENT in correlation_codes

    store = FakeIngestionStore()
    request = telemetry_ingestion___request([provenance_event])
    ack = telemetry_ingestion___ingest(
        request,
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )
    assert ack.rejected[0].reason == IngestionReasonCode.SENSITIVE_PAYLOAD
    assert store.event_count() == 0


def test_privacy_blocked_event_is_rejected_at_the_persistence_boundary():
    blocked = telemetry_ingestion___event(uuid.uuid4(), 1).model_copy(
        update={"privacy": PrivacySummary(blocked=True, block_reason="policy BLOCK")}
    )
    codes = telemetry_schema___codes(validate_canonical_event(blocked))
    assert TelemetryValidationCode.PRIVACY_BLOCKED in codes

    store = FakeIngestionStore()
    ack = telemetry_ingestion___ingest(
        telemetry_ingestion___request([blocked]),
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )
    assert ack.rejected[0].reason == IngestionReasonCode.PRIVACY_BLOCKED
    assert store.event_count() == 0


def test_terminal_session_rejects_events_with_a_permanent_reason():
    for terminal in (SessionState.ENDED, SessionState.REVOKED):
        store = FakeIngestionStore()
        session = telemetry_ingestion___session().model_copy(update={"state": terminal})
        event = telemetry_ingestion___event(uuid.uuid4(), 1)
        ack = telemetry_ingestion___ingest(
            telemetry_ingestion___request([event]),
            store,
            enrollment=telemetry_ingestion___enrollment(),
            session=session,
        )
        assert ack.rejected[0].reason == IngestionReasonCode.SESSION_TERMINAL
        assert ack.rejected[0].disposition == EventDisposition.REJECTED
        assert store.event_count() == 0


def test_stored_identity_is_derived_from_authorization_not_payload():
    # The event carries enrollment/session (needed to resolve the subject) but no
    # study/revision: the stored fact must take its identity from the authorized
    # enrollment/session, never from what the payload omitted.
    event = telemetry_ingestion___event(
        uuid.uuid4(), 1, payload={"tool_name": "read"}
    ).model_copy(update={"study_id": None, "revision_id": None})
    store = FakeIngestionStore()

    ack = telemetry_ingestion___ingest(
        telemetry_ingestion___request([event]),
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )

    assert [a.event_id for a in ack.accepted] == [event.event_id]
    record = {r.event_id: r for r in store.stored_events()}[event.event_id]
    assert record.study_id == telemetry_ingestion__STUDY
    assert record.revision_id == telemetry_ingestion__REV
    assert record.enrollment_id == telemetry_ingestion__ENR
    assert record.research_session_id == telemetry_ingestion__SESS


def test_payload_context_mismatch_is_rejected():
    event = telemetry_ingestion___event(uuid.uuid4(), 1, payload={"tool_name": "read"}).model_copy(
        update={"study_id": uuid.uuid4()}
    )
    store = FakeIngestionStore()

    ack = telemetry_ingestion___ingest(
        telemetry_ingestion___request([event]),
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )

    assert ack.rejected[0].reason == IngestionReasonCode.CONTEXT_MISMATCH
    assert store.event_count() == 0


class _ConcurrentBatchStore(FakeIngestionStore):
    """A store whose batch receipt appears the moment the subject lock is taken.

    This models a concurrent submission of the same ``batch_id`` that committed
    while this request waited for the enrollment/session row lock.
    """

    def __init__(self, winner: BatchReceipt) -> None:
        super().__init__()
        self._winner = winner
        self._locked = False

    def observe_subject_lock(self) -> None:
        self._locked = True

    def get_receipt(self, batch_id):
        if self._locked and batch_id == self._winner.batch_id:
            return self._winner
        return super().get_receipt(batch_id)


def test_concurrent_same_batch_returns_the_winning_receipt():
    event = telemetry_ingestion___event(uuid.uuid4(), 1)
    request = telemetry_ingestion___request([event])
    winner = BatchReceipt(
        receipt_id=uuid.uuid4(),
        batch_id=request.batch_id,
        enrollment_id=telemetry_ingestion__ENR,
        research_session_id=telemetry_ingestion__SESS,
        accepted_at=telemetry_ingestion__NOW,
        ack=TelemetryBatchAckV1(
            receipt_id=uuid.uuid4(),
            batch_id=request.batch_id,
            server_time=telemetry_ingestion__NOW,
            accepted=[
                EventAck(
                    event_id=event.event_id,
                    disposition=EventDisposition.ACCEPTED,
                )
            ],
        ),
    )
    store = _ConcurrentBatchStore(winner)

    def session_resolver(_id):
        store.observe_subject_lock()
        return telemetry_ingestion___session()

    ack = ingest_batch(
        request,
        capability_verifier=telemetry_ingestion___verifier(),
        enrollment_resolver=lambda i: telemetry_ingestion___enrollment(),
        session_resolver=session_resolver,
        store=store,
        now=telemetry_ingestion__NOW,
    )

    # Exactly one receipt and one set of events: the loser returns the winner's.
    assert ack.receipt_id == winner.ack.receipt_id
    assert store.event_count() == 0


def test_receipt_failure_rolls_back_the_events_atomically():
    store = FakeIngestionStore()
    store.fail_receipts = True
    event = telemetry_ingestion___event(uuid.uuid4(), 1)
    request = telemetry_ingestion___request([event])

    ack = telemetry_ingestion___ingest(
        request,
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )

    assert not ack.accepted
    assert ack.retryable[0].reason == IngestionReasonCode.STORE_UNAVAILABLE
    assert store.event_count() == 0
    assert store.get_receipt(request.batch_id) is None


def test_commit_failure_returns_retryable_and_stores_nothing():
    store = FakeIngestionStore()
    store.fail_commit = True
    event = telemetry_ingestion___event(uuid.uuid4(), 1)
    request = telemetry_ingestion___request([event])

    ack = telemetry_ingestion___ingest(
        request,
        store,
        enrollment=telemetry_ingestion___enrollment(),
        session=telemetry_ingestion___session(),
    )

    assert not ack.accepted
    assert ack.retryable[0].reason == IngestionReasonCode.STORE_UNAVAILABLE
    assert store.event_count() == 0
    assert store.get_receipt(request.batch_id) is None

