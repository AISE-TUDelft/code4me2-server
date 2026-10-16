"""ACP tool lifecycle events retain their native tool-call correlation."""

from research.telemetry.enums import CanonicalEventType
from research.telemetry.normalization.generic_acp import GenericAcpNormalizer


def _update(kind: str, status: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "session-1",
            "update": {
                "sessionUpdate": kind,
                "toolCallId": "tool-1",
                "kind": "read",
                "status": status,
            },
        },
    }


def test_tool_lifecycle_carries_tool_call_id_in_canonical_correlations() -> None:
    normalizer = GenericAcpNormalizer()
    cases = (
        ("tool_call", "pending", CanonicalEventType.TOOL_CREATED),
        ("tool_call_update", "in_progress", CanonicalEventType.TOOL_STARTED),
        ("tool_call_update", "completed", CanonicalEventType.TOOL_COMPLETED),
    )
    for kind, status, expected_type in cases:
        candidates = normalizer.normalize(_update(kind, status)).candidates
        assert len(candidates) == 1
        assert candidates[0].event_type == expected_type
        assert candidates[0].correlations.tool_call_id == "tool-1"


def test_failed_tool_retains_tool_call_id() -> None:
    candidate = GenericAcpNormalizer().normalize(_update("tool_call_update", "failed")).candidates[0]
    assert candidate.event_type == CanonicalEventType.TOOL_FAILED
    assert candidate.correlations.tool_call_id == "tool-1"

