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
from research.telemetry.enums import CanonicalEventType, FieldClass
from research.telemetry.models import Correlations, Coverage, EventMetrics

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
    assert event.metrics.usage_capability.state.value == "UNAVAILABLE"


def test_server_built_payloads_only_lose_plain_metadata_the_policy_drops():
    from research.telemetry.adapters import _policy_payload
    from research.telemetry.privacy import PrivacyPolicy

    payload = {
        "model": "m1",  # BEHAVIORAL
        "tool_schema_bytes": 800,  # SYSTEM
        "file_extension": ".py",  # CODE_METADATA: hashed by ingestion, never stripped here
        "result_text": "print()",  # CONTENT: refused by ingestion, never stripped here
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz0123",  # SECRET: refused by ingestion
        "extra": {"model": "nested"},  # containers are left to ingestion
    }
    metrics_only = PrivacyPolicy.from_study_policy({"allowed_field_classes": ["METRICS"]}, consent_active=True)
    assert _policy_payload(payload, metrics_only) == {
        key: value for key, value in payload.items() if key != "model"
    }
    # The default policy and no policy change nothing.
    assert _policy_payload(payload, PrivacyPolicy.from_study_policy({}, consent_active=True)) == payload
    assert _policy_payload(payload, None) == payload
    # A blocked class is not silently stripped: ingestion refuses it.
    blocked = PrivacyPolicy.from_study_policy({"allowed_field_classes": ["METRICS"]}, consent_active=True).model_copy(
        update={"blocked_field_classes": [FieldClass.BEHAVIORAL]}
    )
    assert "model" in _policy_payload(payload, blocked)


def test_server_markers_survive_a_metrics_only_policy():
    """``legacy_kind`` and ``upstream_status`` are the relay's bookkeeping, never
    participant data: the dashboards read them to find model/tool calls and
    would report zero under a METRICS-only policy if they were stripped."""
    from research.telemetry.adapters import _policy_payload
    from research.telemetry.privacy import PrivacyPolicy

    payload = {"legacy_kind": "model_call", "upstream_status": "200", "model": "m1"}
    metrics_only = PrivacyPolicy.from_study_policy({"allowed_field_classes": ["METRICS"]}, consent_active=True)
    assert _policy_payload(payload, metrics_only) == {"legacy_kind": "model_call", "upstream_status": "200"}


def test_reserved_sequences_continue_across_batches_and_tasks():
    """Regression: sequence reuse across batches/tasks caused INTEGRITY_CONFLICT.

    A reserved base continues numbering (never restarts at 0/1), and each
    task's facts are their own emitter namespace so two tasks sharing a
    session never collide on (session, emitter, sequence).
    """
    task = _task()
    task.task_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    first = build_legacy_events(task, [_fact("model_call")], first_sequence=0)
    second = build_legacy_events(task, [_fact("tool_call")], first_sequence=1)
    assert [e.emitter_sequence for e in first] == [1]
    assert [e.emitter_sequence for e in second] == [2]
    assert first[0].emitter_id == second[0].emitter_id
    assert first[0].emitter_id.endswith(str(task.task_id))

    other = _task()
    other.task_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    third = build_legacy_events(other, [_fact("tool_call")], first_sequence=0)
    assert third[0].emitter_sequence == 1
    assert third[0].emitter_id != first[0].emitter_id


def test_request_side_kinds_map_to_start_created_run_types():
    """R5: request-side legacy kinds normalize instead of staying unknown.

    Completions keep their existing mapping (token/step aggregation reads
    only the completion side, so pairing them never double-counts).
    """
    events = build_legacy_events(
        _task(),
        [
            _fact("model_request"),
            _fact("tool_request"),
            _fact("run_started"),
            _fact("run_completed"),
        ],
    )
    assert [e.event_type for e in events] == [
        "agent.message.started",
        "tool.created",
        "agent.run.started",
        "agent.run.completed",
    ]


def test_unmapped_kinds_keep_an_explicit_unknown_marker():
    """Unknowns stay accepted but self-describing for forensics."""
    (event,) = build_legacy_events(_task(), [_fact("observation")])
    assert event.event_type == CanonicalEventType.UNKNOWN_SOURCE_EVENT.value
    assert event.unknown_event_type == "observation"


def test_permission_kinds_map_to_permission_types_with_metadata():
    """Permission request/decision outcomes canonicalize with their metadata."""
    requested = LegacyFact(
        kind="permission_requested",
        occurred_at=NOW,
        payload={"tool_name": "write_file", "tool_call_id": "tc-1", "kind": "edit"},
        metrics=EventMetrics(),
        coverage=Coverage(),
    )
    decided = LegacyFact(
        kind="permission_decided",
        occurred_at=NOW,
        payload={
            "tool_name": "write_file",
            "tool_call_id": "tc-1",
            "kind": "edit",
            "decision": "accepted",
            "decision_scope": "once",
        },
        metrics=EventMetrics(),
        coverage=Coverage(),
    )
    req_event, dec_event = build_legacy_events(_task(), [requested, decided])
    assert req_event.event_type == "permission.requested"
    assert dec_event.event_type == "permission.decided"
    assert dec_event.payload["decision"] == "accepted"
    assert dec_event.payload["decision_scope"] == "once"


def test_trace_and_span_default_to_the_run_and_the_source_event():
    """Facts without their own handles are traced by the run and their event id."""
    defaulted, explicit = build_legacy_events(
        _task(),
        [
            LegacyFact(kind="tool_call", occurred_at=NOW, source_event_id="evt-1"),
            LegacyFact(
                kind="model_call",
                occurred_at=NOW,
                source_event_id="evt-2",
                correlations=Correlations(trace_id="trace-9", span_id="span-9"),
            ),
        ],
    )
    assert defaulted.correlations.trace_id == "run-1"
    assert defaulted.correlations.span_id == "evt-1"
    # A producer's own handles win over the defaults.
    assert explicit.correlations.trace_id == "trace-9"
    assert explicit.correlations.span_id == "span-9"
