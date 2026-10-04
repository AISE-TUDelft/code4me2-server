"""``session/prompt`` content capture in the shared ACP normalizer.

Pure tests (no database). The prompt is CONTENT: the real privacy filter keeps
only ``[REDACTED]`` under a metadata-only policy, keeps the blocks under content
capture, and filtering is idempotent, which is what ingestion requires before it
accepts an already-filtered event.
"""

from __future__ import annotations

from datetime import datetime, timezone

from research.telemetry.builder import EventBuilder, SequenceAllocator
from research.telemetry.chat_lifecycle import ACP_METHOD_KEY
from research.telemetry.enums import CanonicalEventType
from research.telemetry.normalization import materialize_candidate
from research.telemetry.normalization.generic_acp import GenericAcpNormalizer
from research.telemetry.privacy import PrivacyPolicy, filter_event, filter_payload
from research.telemetry.privacy.engine import REDACTED_MARKER

PROMPT_TEXT = "CANARY-PARTICIPANT-PROMPT: why does the build fail?"
BUDGET = 64 * 1024
CONTENT_CAPTURE = PrivacyPolicy(content_allowed=True, consent_active=True)


def _prompt_result(blocks, *, prompt_id: int = 3, session_id: str = "chat-1"):
    message = {
        "jsonrpc": "2.0",
        "id": prompt_id,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": blocks},
    }
    return GenericAcpNormalizer().normalize(message, direction="host_to_agent")


def _content(blocks) -> dict:
    candidates = _prompt_result(blocks).candidates
    assert len(candidates) == 2
    return candidates[1].payload


def test_prompt_maps_to_an_unchanged_turn_start_plus_the_user_prompt() -> None:
    result = _prompt_result([{"type": "text", "text": PROMPT_TEXT}])
    turn_start, content = result.candidates

    assert turn_start.event_type == CanonicalEventType.AGENT_MESSAGE_STARTED
    assert turn_start.mapping_rule_id == "acp.session.prompt"
    assert turn_start.lifecycle_state is None
    assert turn_start.payload == {ACP_METHOD_KEY: "session/prompt", "session_id": "chat-1"}

    assert content.event_type == CanonicalEventType.AGENT_MESSAGE_STARTED
    assert content.mapping_rule_id == "acp.session.prompt.content"
    # A message kind and lifecycle ``started``: counted like a chunk, never a turn.
    assert content.lifecycle_state == "started"
    assert content.payload == {
        ACP_METHOD_KEY: "session/prompt",
        "message_kind": "user",
        "session_id": "chat-1",
        "prompt": [{"type": "text", "text": PROMPT_TEXT}],
    }
    assert turn_start.correlations.turn_id == content.correlations.turn_id == "3"


def test_a_prompt_without_blocks_maps_to_the_turn_start_only() -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "session/prompt",
        "params": {"sessionId": "chat-1"},
    }
    candidates = GenericAcpNormalizer().normalize(message).candidates

    assert [candidate.mapping_rule_id for candidate in candidates] == ["acp.session.prompt"]


def test_text_and_resource_links_are_kept_without_meta() -> None:
    prompt = _content(
        [
            {
                "type": "text",
                "text": PROMPT_TEXT,
                "annotations": {"audience": ["user"]},
                "_meta": {"vendor": "x"},
            },
            {
                "type": "resource_link",
                "uri": "file:///work/src/main.py",
                "name": "main.py",
                "title": "Main module",
                "description": "entry point",
                "mimeType": "text/x-python",
                "size": 1234,
                "_meta": {"vendor": "x"},
            },
            {"type": "vendor_block", "payload": "CANARY-UNKNOWN-BLOCK"},
        ]
    )["prompt"]

    assert prompt == [
        {"type": "text", "text": PROMPT_TEXT},
        {
            "type": "resource_link",
            "uri": "file:///work/src/main.py",
            "name": "main.py",
            "title": "Main module",
            "description": "entry point",
            "mimeType": "text/x-python",
            "size": 1234,
        },
        {"type": "vendor_block"},
    ]


def test_binary_content_is_reduced_to_its_length() -> None:
    prompt = _content(
        [
            {"type": "image", "mimeType": "image/png", "data": "QUJDRA==", "_meta": {}},
            {"type": "audio", "mimeType": "audio/wav", "data": "UklGRg=="},
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///work/logo.png",
                    "mimeType": "image/png",
                    "blob": "iVBORw0KGgo=",
                    "_meta": {},
                },
            },
        ]
    )["prompt"]

    assert prompt == [
        {"type": "image", "mimeType": "image/png", "data_length": 8},
        {"type": "audio", "mimeType": "audio/wav", "data_length": 8},
        {
            "type": "resource",
            "resource": {"uri": "file:///work/logo.png", "mimeType": "image/png", "data_length": 12},
        },
    ]


def test_embedded_resource_text_is_truncated_after_the_typed_text() -> None:
    attached = "x" * (BUDGET + 500)
    # The attached file comes first; the participant's question must survive it.
    prompt = _content(
        [
            {"type": "resource", "resource": {"uri": "file:///work/big.log", "text": attached}},
            {"type": "text", "text": PROMPT_TEXT},
        ]
    )["prompt"]

    resource, typed = prompt
    assert typed == {"type": "text", "text": PROMPT_TEXT}
    assert resource["truncated"] is True
    kept = resource["resource"]["text"]
    assert attached.startswith(kept)
    assert len(kept) + len("file:///work/big.log") + len(PROMPT_TEXT) == BUDGET


def test_resource_text_within_the_budget_is_kept_whole() -> None:
    prompt = _content(
        [{"type": "resource", "resource": {"uri": "file:///a.py", "text": "print(1)\n"}}]
    )["prompt"]

    assert prompt == [
        {"type": "resource", "resource": {"uri": "file:///a.py", "text": "print(1)\n"}}
    ]


def test_typed_text_beyond_the_budget_is_truncated_too() -> None:
    pasted = "y" * (BUDGET + 1)
    prompt = _content(
        [
            {"type": "text", "text": pasted},
            {"type": "resource_link", "uri": "file:///work/a.py", "name": "a.py"},
        ]
    )["prompt"]

    assert prompt[0] == {"type": "text", "text": pasted[:BUDGET], "truncated": True}
    assert prompt[1] == {
        "type": "resource_link",
        "uri": "",
        "name": "",
        "truncated": True,
    }


def test_at_most_32_blocks_are_kept() -> None:
    blocks = [{"type": "text", "text": f"part {index}"} for index in range(40)]
    prompt = _content(blocks)["prompt"]

    assert len(prompt) == 32
    assert prompt[-1] == {"type": "text", "text": "part 31"}


def _filtered_twice(policy: PrivacyPolicy):
    """Filter the content event as the proxy does, then as ingestion re-checks it."""
    result = _prompt_result(
        [
            {"type": "text", "text": PROMPT_TEXT},
            {"type": "resource_link", "uri": "file:///work/src/main.py", "name": "main.py"},
            {"type": "resource", "resource": {"uri": "file:///work/a.py", "text": "print(1)"}},
            {"type": "image", "mimeType": "image/png", "data": "QUJD"},
        ]
    )
    event = materialize_candidate(
        EventBuilder(SequenceAllocator()),
        result.candidates[1],
        result,
        emitter_id="acp-proxy:test",
        occurred_at=datetime.now(timezone.utc),
    )
    proxy_filtered = filter_event(event, policy)
    ingestion_check = filter_event(proxy_filtered.event, policy)
    return proxy_filtered, ingestion_check


def test_metadata_only_policy_redacts_the_prompt_and_keeps_the_metadata() -> None:
    proxy_filtered, ingestion_check = _filtered_twice(PrivacyPolicy.default())

    assert not proxy_filtered.summary.blocked
    assert proxy_filtered.event.payload == {
        ACP_METHOD_KEY: "session/prompt",
        "message_kind": "user",
        "session_id": "chat-1",
        "prompt": REDACTED_MARKER,
    }
    assert "CANARY" not in proxy_filtered.event.model_dump_json()
    # Ingestion accepts it: filtering again changes nothing.
    assert not ingestion_check.summary.blocked
    assert ingestion_check.event.payload == proxy_filtered.event.payload


def test_content_capture_keeps_the_prompt_and_is_idempotent() -> None:
    proxy_filtered, ingestion_check = _filtered_twice(CONTENT_CAPTURE)

    assert not proxy_filtered.summary.blocked
    prompt = proxy_filtered.event.payload["prompt"]
    assert prompt[0] == {"type": "text", "text": PROMPT_TEXT}
    assert prompt[2]["resource"]["text"] == "print(1)"
    assert prompt[3] == {"type": "image", "mimeType": "image/png", "data_length": 4}
    assert proxy_filtered.event.payload["message_kind"] == "user"
    # Ingestion accepts it: filtering again changes nothing.
    assert not ingestion_check.summary.blocked
    assert ingestion_check.event.payload == proxy_filtered.event.payload


def test_a_secret_in_the_prompt_is_dropped_even_under_content_capture() -> None:
    payload = _content([{"type": "text", "text": "my key is sk-CANARYPROMPTSECRET0001"}])

    filtered, summary = filter_payload(payload, CONTENT_CAPTURE)

    assert "sk-CANARY" not in repr(filtered)
    assert filtered["prompt"] == [{"type": "text"}]
    assert summary.redacted_fields == ["prompt[0].text"]
    assert filter_payload(filtered, CONTENT_CAPTURE)[0] == filtered
