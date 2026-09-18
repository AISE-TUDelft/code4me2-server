"""Legacy producer -> canonical mapping and source ownership.

The relay/self-report adapters translate legacy observation kinds into the
existing canonical vocabulary (never a parallel one), keep relay observations
distinguishable by provenance, and preserve a fact with no canonical equivalent
as an explicit unknown. Edit acceptance has no producer, so the canonical
vocabulary never carries an ``edit.*`` type and permission vs edit stay separate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from research.telemetry.adapters import LegacyFact, build_legacy_events
from research.telemetry.enums import CanonicalEventType
from research.telemetry.models import Coverage, EventMetrics

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _task():
    return SimpleNamespace(
        study_id="11111111-1111-1111-1111-111111111111",
        study_revision_id="22222222-2222-2222-2222-222222222222",
        enrollment_id="33333333-3333-3333-3333-333333333333",
        research_session_id="44444444-4444-4444-4444-444444444444",
        external_run_id="run-1",
        next_event_index=0,
    )


def _fact(kind, **kwargs):
    return LegacyFact(
        kind=kind,
        occurred_at=NOW,
        metrics=kwargs.get("metrics", EventMetrics()),
        coverage=kwargs.get("coverage", Coverage()),
        **({"payload": kwargs["payload"]} if "payload" in kwargs else {}),
    )


def test_each_legacy_kind_maps_to_an_existing_canonical_type():
    events = build_legacy_events(
        _task(),
        [
            _fact("model_call"),
            _fact("tool_call"),
            _fact("tool_failed"),
            _fact("observation"),
        ],
    )
    assert [e.event_type for e in events] == [
        "agent.message.completed",
        "tool.completed",
        "tool.failed",
        # No canonical equivalent: preserved as an explicit unknown, never
        # reclassified into a nearby concept.
        CanonicalEventType.UNKNOWN_SOURCE_EVENT.value,
    ]
    for event in events:
        assert event.provenance.source == "relay"


def test_relay_observations_carry_the_legacy_kind_and_the_run_binding():
    (event,) = build_legacy_events(
        _task(), [_fact("model_call", payload={"model": "m"})]
    )
    assert event.payload["legacy_kind"] == "model_call"
    assert event.agent_run_id == "run-1"
    assert str(event.research_session_id) == "44444444-4444-4444-4444-444444444444"
    # Sequence is allocated from the task cursor so ordering is explicit.
    assert event.emitter_sequence == 1


def test_permission_and_edit_types_stay_separate():
    # Permission decisions are a canonical observation; there is no edit type at
    # all, so a dashboard can never synthesize edit acceptance from anything.
    names = {member.value for member in CanonicalEventType}
    assert "permission.requested" in names
    assert "permission.decided" in names
    assert not {n for n in names if n.startswith("edit.")}


def test_missing_usage_is_unavailable_never_zero():
    fact = LegacyFact(
        kind="model_call",
        occurred_at=NOW,
        metrics=EventMetrics(usage_tokens=None),
    )
    (event,) = build_legacy_events(_task(), [fact])
    assert event.metrics.usage_tokens is None
    assert event.metrics.usage_capability.state.value == "UNKNOWN"
