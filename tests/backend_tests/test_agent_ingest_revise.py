"""Self-report ingestion of the built-in agent's "Revise…" decisions.

Run 2026-10-03-pilot-feedback, issue C1: a ``permission.decided`` with decision
``revised`` carries the form outcome (``elicitation_action``), what became of
the kept hunks (``revise_status``), ``hunk_count``/``kept_hunk_count`` and the
user's instructions as ``text``. The first two and the counts are structural
and survive without content; ``text`` is content and is stored only with it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from agents.ingest import _fact_from_columns, build_correlation_index, map_event_to_columns
from code4me2_agent.acp_updates import REVISE_OPTION_ID
from research.telemetry.chat_lifecycle import REVISE_OPTION_ID as SHARED_REVISE_OPTION_ID
from research.telemetry.chat_lifecycle import REVISED_DECISION
from research.telemetry.privacy.classify import FieldClass, classify_field

REVISED = {
    "tool_name": "edit_file",
    "tool_call_id": "edit-1",
    "kind": "edit",
    "decision": "revised",
    "decision_scope": "none",
    "elicitation_action": "accept",
    "hunk_count": 3,
    "kept_hunk_count": 1,
    "revise_status": "applied",
    "text": "Call the new lines added_a.",
}


def _event(payload: dict, event_type: str = "agent.permission.decided") -> dict:
    return {
        "schema_version": "code4me.agent.event.v1",
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "timestamp": "2026-10-04T10:00:00Z",
        "sequence": 1,
        "run_id": "run-1",
        "request_id": "req-1",
        "session_id": "s-1",
        "payload": payload,
        "metrics": {},
    }


def _columns(event: dict, *, content_included: bool) -> dict:
    by_request_id, by_tool_call_id = build_correlation_index([event])
    return map_event_to_columns(
        event,
        content_included=content_included,
        by_request_id=by_request_id,
        by_tool_call_id=by_tool_call_id,
    )


def test_revise_metadata_survives_without_content():
    columns = _columns(_event(REVISED), content_included=False)

    extra = json.loads(columns["extra_json"])
    assert {key: extra[key] for key in ("elicitation_action", "revise_status", "hunk_count", "kept_hunk_count")} == {
        "elicitation_action": "accept",
        "revise_status": "applied",
        "hunk_count": 3,
        "kept_hunk_count": 1,
    }
    assert "text" not in extra
    assert columns["payload_json"] is None
    fact = _fact_from_columns(columns, content_included=False)
    assert fact.payload["decision"] == REVISED_DECISION
    assert (fact.payload["elicitation_action"], fact.payload["revise_status"]) == ("accept", "applied")
    assert (fact.metrics.counts["hunk_count"], fact.metrics.counts["kept_hunk_count"]) == (3, 1)
    assert "Call the new lines" not in json.dumps(fact.payload)


def test_instructions_are_stored_only_with_content():
    columns = _columns(_event(REVISED), content_included=True)

    fact = _fact_from_columns(columns, content_included=True)

    assert json.loads(fact.payload["payload"])["payload"]["text"] == "Call the new lines added_a."
    assert fact.payload["revise_status"] == "applied"


def test_a_declined_form_keeps_its_outcome_and_malformed_values_are_dropped():
    declined = _columns(
        _event({"decision": "rejected", "elicitation_action": "decline", "hunk_count": 2}),
        content_included=False,
    )
    fact = _fact_from_columns(declined)
    assert fact.payload["elicitation_action"] == "decline"
    assert fact.metrics.counts["hunk_count"] == 2
    assert "kept_hunk_count" not in fact.metrics.counts and "revise_status" not in fact.payload

    malformed = _columns(
        _event({"decision": "revised", "revise_status": {"x": 1}, "hunk_count": True, "kept_hunk_count": "2"}),
        content_included=False,
    )
    extra = json.loads(malformed["extra_json"])
    assert "revise_status" not in extra and "hunk_count" not in extra
    assert extra["kept_hunk_count"] == 2


def test_other_events_are_unchanged():
    plain = _columns(_event({"decision": "accepted", "decision_scope": "once"}), content_included=False)
    extra = json.loads(plain["extra_json"])
    assert not set(extra) & {"elicitation_action", "revise_status", "hunk_count", "kept_hunk_count"}
    fact = _fact_from_columns(plain)
    assert not set(fact.payload) & {"elicitation_action", "revise_status"}
    assert not set(fact.metrics.counts) & {"hunk_count", "kept_hunk_count"}


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("elicitation_action", "accept", FieldClass.BEHAVIORAL),
        ("revise_status", "file_changed", FieldClass.BEHAVIORAL),
        ("decision", "revised", FieldClass.BEHAVIORAL),
        ("hunk_count", 3, FieldClass.SYSTEM),
        ("kept_hunk_count", 1, FieldClass.SYSTEM),
        ("text", "Call it added_a.", FieldClass.CONTENT),
    ],
)
def test_revise_keys_are_classified_as_the_contract_says(key, value, expected):
    assert classify_field(key, value) is expected


def test_the_runtime_offers_the_shared_option_id():
    # The agent runtime ships without the server packages, so it keeps its own
    # copy of the literal the proxy and analytics read.
    assert REVISE_OPTION_ID == SHARED_REVISE_OPTION_ID == "revise"
    assert REVISED_DECISION == "revised"
