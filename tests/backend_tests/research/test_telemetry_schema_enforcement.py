"""Schema-enforcement hardening for canonical telemetry ingestion.

Covers TSCH-01/02 (unknown event_type/source and provenance/source consistency),
TI-02 (in-batch duplicate guard) and TSCH-05 (usage coverage default).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from research.telemetry.enums import CoverageState
from research.telemetry.ingestion.enums import IngestionReasonCode, is_permanent
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _ingest_authorized_events
from research.telemetry.ingestion.store import FakeIngestionStore
from research.telemetry.ingestion.validation import validate_batch_event
from research.telemetry.models import (
    CanonicalEventV1,
    Coverage,
    EventMetrics,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _raw(**overrides: Any) -> CanonicalEventV1:
    data: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "event_type": "tool.completed",
        "source": "ide",
        "occurred_at": NOW.isoformat(),
        "emitter_id": "schema-emitter",
        "emitter_sequence": 1,
        "provenance": {"source": "ide", "normalizer_version": "1"},
    }
    data.update(overrides)
    return CanonicalEventV1.model_validate(data)


def _context() -> IngestionContext:
    return IngestionContext(
        study_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(),
        revocation_epoch=0,
    )


# -- TSCH-01: event_type enforcement --------------------------------------


def test_unrecognized_event_type_without_a_marker_is_permanently_rejected():
    event = _raw(event_type="vendor.weird.thing")
    issues = validate_batch_event(event, None)
    codes = [issue.code for issue in issues]
    assert IngestionReasonCode.UNKNOWN_EVENT_TYPE in codes
    assert is_permanent(IngestionReasonCode.UNKNOWN_EVENT_TYPE)
    assert all(issue.permanent for issue in issues)


def test_marker_preserved_event_type_is_accepted():
    event = _raw(
        event_type="unknown_source_event",
        unknown_event_type="vendor.weird.thing",
    )
    codes = {issue.code for issue in validate_batch_event(event, None)}
    assert IngestionReasonCode.UNKNOWN_EVENT_TYPE not in codes


# -- TSCH-02: source enforcement and provenance consistency ----------------


def test_unrecognized_source_without_a_marker_is_permanently_rejected():
    event = _raw(source="some-cli", provenance={"source": "some-cli", "normalizer_version": "1"})
    codes = {issue.code for issue in validate_batch_event(event, None)}
    assert IngestionReasonCode.UNKNOWN_SOURCE in codes
    assert is_permanent(IngestionReasonCode.UNKNOWN_SOURCE)


def test_marker_preserved_source_is_accepted():
    event = _raw(
        source="some-cli",
        unknown_source="some-cli",
        provenance={"source": "some-cli", "normalizer_version": "1"},
    )
    codes = {issue.code for issue in validate_batch_event(event, None)}
    assert IngestionReasonCode.UNKNOWN_SOURCE not in codes


def test_provenance_source_must_match_the_top_level_source():
    event = _raw(source="ide", provenance={"source": "acp", "normalizer_version": "1"})
    issues = validate_batch_event(event, None)
    assert any(
        issue.field == "provenance.source"
        and issue.code == IngestionReasonCode.INVALID_SCHEMA
        for issue in issues
    )


# -- TI-02: in-batch duplicate guard ---------------------------------------


def test_a_repeated_event_id_never_raises_and_is_reported_as_duplicate():
    context = _context()
    event_id = uuid.uuid4()
    common = {
        "event_id": str(event_id),
        "study_id": str(context.study_id),
        "enrollment_id": str(context.enrollment_id),
        "research_session_id": str(context.research_session_id),
    }
    first = _raw(**common, emitter_sequence=1)
    repeat = _raw(**common, emitter_sequence=2)
    store = FakeIngestionStore()

    ack = _ingest_authorized_events(
        store,
        batch_id=uuid.uuid4(),
        events=[first, repeat],
        context=context,
        telemetry_schema_version="1",
        server_time=NOW,
        continuity_required=False,
        retry_hint=30,
    )

    assert [entry.event_id for entry in ack.duplicate] == [event_id]
    assert ack.accepted == []
    assert ack.rejected == []
    assert store.event_count() == 1, "the first occurrence is the one stored fact"


# -- TSCH-05: usage coverage default ---------------------------------------


def test_bare_event_usage_is_unavailable_and_top_level_coverage_stays_unknown():
    assert EventMetrics().usage_capability.state == CoverageState.UNAVAILABLE
    # The top-level Coverage default is deliberately unchanged.
    assert Coverage().state == CoverageState.UNKNOWN
