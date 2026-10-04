"""ACP chat lifecycle: chat starts and ends, load replays and explicit chat ids.

Pure normalizer tests (no database). The payload keys and method sets come from
the shared contract in :mod:`research.telemetry.chat_lifecycle`.
"""

from __future__ import annotations

from typing import Any, Optional

from research.telemetry.chat_lifecycle import (
    ACP_METHOD_KEY,
    CANCEL_METHOD,
    CHAT_END_METHODS,
    CHAT_START_METHODS,
    REVISE_OPTION_ID,
    SELECTED_OPTION_ID_KEY,
)
from research.telemetry.enums import CanonicalEventType
from research.telemetry.normalization.generic_acp import (
    GENERIC_ACP_NORMALIZER_VERSION,
    GenericAcpNormalizer,
)
from research.telemetry.privacy import PrivacyPolicy, filter_payload

HOST = "host_to_agent"
AGENT = "agent_to_host"


def _request(request_id: int, method: str, params: Optional[dict] = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _result(request_id: int, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "failed"}}


def _update(session_id: str, kind: str = "agent_message_chunk") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": kind, "content": {"type": "text", "text": "hi"}},
        },
    }


def _candidates(normalizer: GenericAcpNormalizer, message: dict, direction: str) -> list:
    return normalizer.normalize(message, direction=direction).candidates


def test_normalizer_version_is_v2() -> None:
    assert GENERIC_ACP_NORMALIZER_VERSION == "generic-acp-v2"
    result = GenericAcpNormalizer().normalize(_request(1, "initialize"), direction=HOST)
    assert result.normalizer_version == "generic-acp-v2"


def test_method_derived_candidates_name_their_acp_method() -> None:
    for method, expected_type in (
        ("initialize", CanonicalEventType.INTERACTION_STARTED),
        ("session/prompt", CanonicalEventType.AGENT_MESSAGE_STARTED),
        ("fs/read_text_file", CanonicalEventType.IDE_FILE_OPENED),
        ("fs/write_text_file", CanonicalEventType.IDE_FILE_SAVED),
        ("terminal/create", CanonicalEventType.TOOL_STARTED),
    ):
        request = _request(1, method, {"sessionId": "s1"})
        candidate = _candidates(GenericAcpNormalizer(), request, HOST)[0]
        assert candidate.event_type == expected_type
        assert candidate.payload[ACP_METHOD_KEY] == method


def test_new_and_fork_start_a_chat_from_the_agent_response() -> None:
    for method in ("session/new", "session/fork"):
        assert method in CHAT_START_METHODS
        normalizer = GenericAcpNormalizer()
        # A fork names its parent chat; the new chat is the one the agent answers with.
        request = normalizer.normalize(
            _request(2, method, {"sessionId": "parent", "cwd": "/w"}), direction=HOST
        )
        assert request.candidates == []

        response = _candidates(normalizer, _result(2, {"sessionId": "chat-new"}), AGENT)

        assert len(response) == 1
        started = response[0]
        assert started.event_type == CanonicalEventType.INTERACTION_STARTED
        assert started.lifecycle_state is None
        assert started.payload == {ACP_METHOD_KEY: method, "session_id": "chat-new"}


def test_failed_new_chat_yields_only_its_error() -> None:
    normalizer = GenericAcpNormalizer()
    assert _candidates(normalizer, _request(2, "session/new", {"cwd": "/w"}), HOST) == []

    failed = _candidates(normalizer, _error(2), AGENT)
    assert [candidate.event_type for candidate in failed] == [CanonicalEventType.AGENT_ERROR]

    # The failed request is settled: a stray later answer starts no chat.
    stray = _candidates(normalizer, _result(2, {"sessionId": "late"}), AGENT)
    assert stray[0].event_type == CanonicalEventType.UNKNOWN_SOURCE_EVENT


def test_new_chat_is_only_taken_from_an_agent_answer() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _request(2, "session/new"), HOST)

    host_answer = _candidates(normalizer, _result(2, {"sessionId": "spoof"}), HOST)
    assert host_answer[0].event_type == CanonicalEventType.UNKNOWN_SOURCE_EVENT

    agent_answer = _candidates(normalizer, _result(2, {"sessionId": "chat-1"}), AGENT)
    assert agent_answer[0].event_type == CanonicalEventType.INTERACTION_STARTED
    assert agent_answer[0].payload["session_id"] == "chat-1"


def test_load_and_resume_start_a_chat_from_the_request() -> None:
    for method in ("session/load", "session/resume"):
        assert method in CHAT_START_METHODS
        candidates = _candidates(
            GenericAcpNormalizer(), _request(4, method, {"sessionId": "chat-1", "cwd": "/w"}), HOST
        )
        assert len(candidates) == 1
        started = candidates[0]
        assert started.event_type == CanonicalEventType.INTERACTION_STARTED
        assert started.lifecycle_state is None
        assert started.payload == {ACP_METHOD_KEY: method, "session_id": "chat-1"}


def test_close_and_delete_end_a_chat_with_lifecycle_completed() -> None:
    assert CHAT_END_METHODS == {"session/close", "session/delete"}
    for method in sorted(CHAT_END_METHODS):
        candidates = _candidates(
            GenericAcpNormalizer(), _request(5, method, {"sessionId": "chat-1"}), HOST
        )
        assert len(candidates) == 1
        ended = candidates[0]
        assert ended.event_type == CanonicalEventType.INTERACTION_COMPLETED
        assert ended.lifecycle_state == "completed"
        assert ended.payload == {ACP_METHOD_KEY: method, "session_id": "chat-1"}


def test_cancel_stays_an_interrupt_without_lifecycle() -> None:
    cancel = {
        "jsonrpc": "2.0",
        "method": CANCEL_METHOD,
        "params": {"sessionId": "chat-1"},
    }
    candidates = _candidates(GenericAcpNormalizer(), cancel, HOST)

    assert len(candidates) == 1
    interrupt = candidates[0]
    assert interrupt.event_type == CanonicalEventType.INTERACTION_COMPLETED
    assert interrupt.lifecycle_state is None
    assert interrupt.payload == {ACP_METHOD_KEY: CANCEL_METHOD, "session_id": "chat-1"}


def test_lifecycle_metadata_survives_a_metadata_only_policy() -> None:
    normalizer = GenericAcpNormalizer()
    payloads = [
        _candidates(normalizer, _request(4, "session/load", {"sessionId": "c"}), HOST)[0].payload,
        _candidates(normalizer, _request(5, "session/close", {"sessionId": "c"}), HOST)[0].payload,
    ]
    _candidates(normalizer, _request(6, "session/new"), HOST)
    payloads.append(_candidates(normalizer, _result(6, {"sessionId": "d"}), AGENT)[0].payload)

    for payload in payloads:
        filtered, summary = filter_payload(payload, PrivacyPolicy.default())
        assert filtered == payload
        assert not summary.blocked


def test_load_replay_is_dropped_until_the_load_is_answered() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _request(4, "session/load", {"sessionId": "chat-1"}), HOST)

    replayed = normalizer.normalize(_update("chat-1", "user_message_chunk"), direction=AGENT)
    assert replayed.candidates == []
    assert _candidates(normalizer, _update("chat-1"), AGENT) == []
    # Another chat's live updates are not part of the replay.
    other = _candidates(normalizer, _update("chat-2"), AGENT)
    assert [candidate.event_type for candidate in other] == [
        CanonicalEventType.AGENT_MESSAGE_STARTED
    ]

    _candidates(normalizer, _result(4, {}), AGENT)

    live = _candidates(normalizer, _update("chat-1"), AGENT)
    assert [candidate.event_type for candidate in live] == [
        CanonicalEventType.AGENT_MESSAGE_STARTED
    ]


def test_a_failed_load_ends_the_replay_window() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _request(4, "session/load", {"sessionId": "chat-1"}), HOST)
    assert _candidates(normalizer, _update("chat-1"), AGENT) == []

    failed = _candidates(normalizer, _error(4), AGENT)
    assert [candidate.event_type for candidate in failed] == [CanonicalEventType.AGENT_ERROR]

    assert len(_candidates(normalizer, _update("chat-1"), AGENT)) == 1


def test_a_later_prompt_ends_the_replay_window() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _request(4, "session/load", {"sessionId": "chat-1"}), HOST)
    assert _candidates(normalizer, _update("chat-1"), AGENT) == []

    _candidates(normalizer, _request(5, "session/prompt", {"sessionId": "chat-1"}), HOST)

    assert len(_candidates(normalizer, _update("chat-1"), AGENT)) == 1


def test_prompt_responses_carry_their_prompts_chat() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _request(3, "session/prompt", {"sessionId": "chat-a"}), HOST)
    _candidates(normalizer, _request(9, "session/prompt", {"sessionId": "chat-b"}), HOST)
    _candidates(normalizer, _update("chat-b"), AGENT)

    # Answered in the reverse order, after the other chat spoke last.
    second = _candidates(normalizer, _result(9, {"stopReason": "end_turn"}), AGENT)[0]
    first = _candidates(normalizer, _result(3, {"stopReason": "end_turn"}), AGENT)[0]

    assert second.event_type == first.event_type == CanonicalEventType.AGENT_MESSAGE_COMPLETED
    assert second.payload["session_id"] == "chat-b"
    assert first.payload["session_id"] == "chat-a"


def _permission_request(permission_id: int, session_id: str) -> dict:
    return _request(
        permission_id,
        "session/request_permission",
        {
            "sessionId": session_id,
            "toolCall": {"toolCallId": "call-1"},
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                {"optionId": REVISE_OPTION_ID, "name": "Revise…", "kind": "reject_once"},
            ],
        },
    )


def test_permission_decisions_carry_the_requests_chat() -> None:
    normalizer = GenericAcpNormalizer()
    requested = _candidates(normalizer, _permission_request(7, "chat-a"), AGENT)[0]
    _candidates(normalizer, _update("chat-b"), AGENT)

    decided = _candidates(
        normalizer,
        _result(7, {"outcome": {"outcome": "selected", "optionId": "allow"}}),
        HOST,
    )[0]

    assert requested.payload["session_id"] == "chat-a"
    assert decided.event_type == CanonicalEventType.PERMISSION_DECIDED
    assert decided.payload["session_id"] == "chat-a"
    assert decided.payload["decision"] == "allow"


def test_a_revise_decision_passes_through_as_a_reject() -> None:
    normalizer = GenericAcpNormalizer()
    _candidates(normalizer, _permission_request(7, "chat-a"), AGENT)

    decided = _candidates(
        normalizer,
        _result(7, {"outcome": {"outcome": "selected", "optionId": REVISE_OPTION_ID}}),
        HOST,
    )[0]

    assert decided.event_type == CanonicalEventType.PERMISSION_DECIDED
    assert decided.payload == {
        "session_id": "chat-a",
        "outcome": "selected",
        SELECTED_OPTION_ID_KEY: REVISE_OPTION_ID,
        "selected_option_kind": "reject_once",
        "decision": "reject",
    }
    # Every key is metadata: a metadata-only study keeps the row unchanged.
    filtered, summary = filter_payload(decided.payload, PrivacyPolicy.default())
    assert filtered == decided.payload
    assert not summary.blocked


def test_elicitation_frames_keep_names_only() -> None:
    normalizer = GenericAcpNormalizer()
    request = _request(
        12,
        "elicitation/create",
        {
            "sessionId": "chat-a",
            "message": "CANARY-ELICITATION-MESSAGE",
            "requestedSchema": {"type": "object", "properties": {"instructions": {}}},
        },
    )
    answer = _result(
        12, {"action": "accept", "content": {"instructions": "CANARY-ELICITATION-ANSWER"}}
    )

    asked = _candidates(normalizer, request, AGENT)
    answered = _candidates(normalizer, answer, HOST)

    assert [candidate.event_type for candidate in asked + answered] == [
        CanonicalEventType.UNKNOWN_SOURCE_EVENT,
        CanonicalEventType.UNKNOWN_SOURCE_EVENT,
    ]
    assert asked[0].payload["unknown_method"] == "elicitation/create"
    assert asked[0].payload["param_names"] == ["message", "requestedSchema", "sessionId"]
    serialized = repr([candidate.payload for candidate in asked + answered])
    assert "CANARY" not in serialized
    assert "accept" not in serialized
