"""Self-report ingestion keeps the runtime's ``call_purpose`` as structural metadata.

The built-in agent tags every model call ("turn", "summarize", "self_review";
run 2026-09-26-agent-harness-tiers, decision D-02) so analytics can separate
harness calls from the agent's own steps even when content is not stored.
"""

from __future__ import annotations

import json
import uuid

from agents.ingest import (
    _fact_from_columns,
    build_correlation_index,
    map_event_to_columns,
)
from research.telemetry.privacy.classify import FieldClass, classify_field


def _event(event_type: str, payload: dict) -> dict:
    return {
        "schema_version": "code4me.agent.event.v1",
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "timestamp": "2026-09-26T10:00:00Z",
        "sequence": 1,
        "run_id": "run-1",
        "request_id": "req-1",
        "session_id": "s-1",
        "payload": payload,
        "metrics": {"duration_ms": 12.0, "prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }


def _columns(event: dict, *, content_included: bool) -> dict:
    by_request_id, by_tool_call_id = build_correlation_index([event])
    return map_event_to_columns(
        event,
        content_included=content_included,
        by_request_id=by_request_id,
        by_tool_call_id=by_tool_call_id,
    )


def test_model_call_purpose_survives_without_content():
    event = _event(
        "agent.model.completed",
        {"model": "m", "finish_reason": "stop", "call_purpose": "self_review", "usage": {}},
    )

    columns = _columns(event, content_included=False)

    assert json.loads(columns["extra_json"])["call_purpose"] == "self_review"
    assert columns["payload_json"] is None  # no content stored
    fact = _fact_from_columns(columns, content_included=False)
    assert fact.payload["call_purpose"] == "self_review"


def test_the_tag_is_behavioral_metadata_never_content():
    assert classify_field("call_purpose", "summarize") is FieldClass.BEHAVIORAL


def test_other_events_and_untagged_calls_are_unchanged():
    tool = _columns(_event("agent.tool.completed", {"tool_name": "read_file", "call_purpose": "x"}), content_included=False)
    assert "call_purpose" not in json.loads(tool["extra_json"])
    untagged = _columns(_event("agent.model.completed", {"model": "m"}), content_included=False)
    assert "call_purpose" not in json.loads(untagged["extra_json"])
    assert "call_purpose" not in _fact_from_columns(untagged).payload
