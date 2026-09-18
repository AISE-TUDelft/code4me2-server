"""Tests for the ACP host-evidence and compatibility gate (Issue 01).

Route handlers are exercised by calling the functions directly with a
``MagicMock`` app/session. No ``TestClient`` is used because this environment
has no PostgreSQL/Redis; the compatibility logic itself is pure and fully
unit-testable.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from research.compatibility import store as store_module
from research.compatibility.canonical import (
    canonical_hash,
    canonical_json,
    receipt_content_hash,
)
from research.compatibility.enums import (
    CapabilityId,
    CapabilityState,
    CompatibilityDecision,
    CompatibilityReasonCode,
    EnforcementOwner,
    Fidelity,
)
from research.compatibility.evaluate import evaluate_compatibility
from research.compatibility.models import (
    AcpCapabilityReceiptV1,
    AgentIdentity,
    CompatibilityRequest,
    EnvironmentTuple,
    RequiredCapability,
)
from research.compatibility.receipt import (
    ACP_PARSE_FAILED_EVENT_TYPE,
    build_receipt_from_fixture,
    parse_failure_event,
    receipt_parse_error,
    validate_receipt,
)
from research.compatibility.redaction import REDACTED, redact

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "compatibility"
)

GOOD_FIXTURE = "host_intellij_2026_2__agent_codex_v1.json"
PARTIAL_FIXTURE = "host_intellij_2026_2__partial_tool_updates.json"
PARSE_FAILURE_FIXTURE = "parse_failure_transcript.json"

ENVIRONMENT = EnvironmentTuple(
    ide_build="IU-262.1234.56",
    ai_assistant_build="AI-262.1.0",
    plugin_version="0.9.0",
    os="macOS 15.5",
    arch="aarch64",
    host_kind="IntelliJ IDEA",
)
AGENT = AgentIdentity(
    agent_id="codex-acp",
    agent_version="1.2.3",
    adapter_version="0.4.0",
    release_digest="sha256:abcdef",
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def _good_receipt() -> AcpCapabilityReceiptV1:
    return build_receipt_from_fixture(
        _load(GOOD_FIXTURE), ENVIRONMENT, AGENT, capture_channel="fixture"
    )


def _partial_receipt() -> AcpCapabilityReceiptV1:
    return build_receipt_from_fixture(
        _load(PARTIAL_FIXTURE), ENVIRONMENT, AGENT, capture_channel="fixture"
    )


def _receipt_with_operations(
    receipt: AcpCapabilityReceiptV1, operations
) -> AcpCapabilityReceiptV1:
    """Replace observed operations and re-finalise the content hash."""
    updated = receipt.model_copy(
        deep=True,
        update={"observed_operations": list(operations), "content_hash": ""},
    )
    updated.content_hash = receipt_content_hash(updated)
    return updated


def _summary_row() -> SimpleNamespace:
    return SimpleNamespace(
        receipt_id=uuid.uuid4(),
        captured_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        created_at=datetime(2026, 8, 1, 10, 5, tzinfo=timezone.utc),
        status="SUPPORTED",
        content_hash="a" * 64,
        protocol_version="1",
        ide_build="IU-262.1234.56",
        ai_assistant_build="AI-262.1.0",
        plugin_version="0.9.0",
        os="macOS 15.5",
        arch="aarch64",
        agent_id="codex-acp",
        agent_version="1.2.3",
        adapter_version="0.4.0",
    )


# ---------------------------------------------------------------------------
# Canonical hashing
# ---------------------------------------------------------------------------


def test_canonical_hash_is_dict_order_independent():
    first = {"b": [1, {"d": 4, "c": 3}], "a": 1}
    second = {"a": 1, "b": [1, {"c": 3, "d": 4}]}
    assert canonical_hash(first) == canonical_hash(second)


def test_canonical_hash_ignores_nonsemantic_whitespace():
    first = json.loads('{"a": 1, "b": {"c": 2, "d": 3}}')
    second = json.loads('{  "b" : { "d" : 3 , "c" : 2 } , "a" : 1 }')
    assert canonical_hash(first) == canonical_hash(second)


def test_receipt_content_hash_round_trips_through_json():
    receipt = _good_receipt()
    assert receipt.content_hash == receipt_content_hash(receipt)

    reloaded = AcpCapabilityReceiptV1.model_validate(
        receipt.model_dump(mode="json")
    )
    assert reloaded.content_hash == receipt.content_hash
    assert receipt_content_hash(reloaded) == receipt.content_hash


def test_receipt_content_hash_detects_mutation():
    receipt = _good_receipt()
    tampered = receipt.model_copy(deep=True)
    tampered.status = CapabilityState.PARTIAL
    assert receipt_content_hash(tampered) != tampered.content_hash


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_removes_nested_secrets_and_does_not_mutate_input():
    original = {
        "api_key": "value",
        "nested": {"password": "hunter2", "keep_me": 1},
        "items": [{"authorization": "Bearer abcdef"}],
        "session_token": "abc",
    }
    snapshot = copy.deepcopy(original)

    result = redact(original)

    assert original == snapshot
    assert result.value["api_key"] == REDACTED
    assert result.value["nested"]["password"] == REDACTED
    assert result.value["nested"]["keep_me"] == 1
    assert result.value["items"][0]["authorization"] == REDACTED
    assert result.value["session_token"] == REDACTED
    assert result.redaction_count >= 4
    assert result.changed is True


def test_redact_masks_inline_secret_shapes_in_string_values():
    result = redact(
        {
            "note": (
                "sk-ABCDEFGH12345678 and ghp_ABCDEFGHIJKLMNOPQRST and "
                "AKIAABCDEFGHIJKLMNOP and Bearer TOKENVALUE12345"
            )
        }
    )
    value = result.value["note"]
    assert "sk-ABCDEFGH12345678" not in value
    assert "ghp_ABCDEFGHIJKLMNOPQRST" not in value
    assert "AKIAABCDEFGHIJKLMNOP" not in value
    assert "TOKENVALUE12345" not in value
    assert REDACTED in value
    assert result.value_redactions >= 4


def test_canary_never_appears_in_canonical_serialization():
    transcript = _load(GOOD_FIXTURE)
    redacted_transcript = redact(transcript).value
    serialized_transcript = canonical_json(redacted_transcript)
    assert "sk-CANARY" not in serialized_transcript
    assert "CANARY-CLIENT-SECRET" not in serialized_transcript
    assert "CANARY-BEARER-TOKEN" not in serialized_transcript

    receipt = _good_receipt()
    serialized_receipt = canonical_json(receipt.model_dump(mode="json"))
    assert "CANARY" not in serialized_receipt
    assert "sk-" not in serialized_receipt


# ---------------------------------------------------------------------------
# Receipt validation rejects unscoped capability claims
# ---------------------------------------------------------------------------


def _good_receipt_dict() -> dict:
    return _good_receipt().model_dump(mode="json")


def test_validate_receipt_rejects_missing_environment():
    data = _good_receipt_dict()
    data["environment"] = {}
    reasons = validate_receipt(data)
    assert reasons
    assert any(r.code == CompatibilityReasonCode.ENVIRONMENT_MISMATCH for r in reasons)


def test_validate_receipt_rejects_missing_agent():
    data = _good_receipt_dict()
    data["agent"] = {}
    reasons = validate_receipt(data)
    assert reasons
    assert any(r.code == CompatibilityReasonCode.AGENT_ID_MISMATCH for r in reasons)
    assert any(r.code == CompatibilityReasonCode.AGENT_VERSION_MISMATCH for r in reasons)


def test_validate_receipt_rejects_missing_protocol_version():
    data = _good_receipt_dict()
    data["protocol_version"] = ""
    reasons = validate_receipt(data)
    assert reasons
    assert any(
        r.code == CompatibilityReasonCode.PROTOCOL_VERSION_MISMATCH for r in reasons
    )


def test_validate_receipt_rejects_operation_without_capability():
    data = _good_receipt_dict()
    data["observed_operations"] = [{"state": "SUPPORTED"}]
    reasons = validate_receipt(data)
    assert reasons
    assert any(r.code == CompatibilityReasonCode.CAPABILITY_MISSING for r in reasons)


def test_validate_receipt_accepts_scoped_receipt():
    assert validate_receipt(_good_receipt()) == []


# ---------------------------------------------------------------------------
# Fixture ingestion
# ---------------------------------------------------------------------------


def test_good_fixture_yields_supported_receipt():
    receipt = _good_receipt()
    assert receipt.status == CapabilityState.SUPPORTED
    assert len(receipt.observed_operations) == len(CapabilityId)
    assert all(
        operation.state == CapabilityState.SUPPORTED
        for operation in receipt.observed_operations
    )
    assert receipt.evidence and all(ref.redacted for ref in receipt.evidence)
    assert receipt.declared["agent.loadSession"] is True
    assert validate_receipt(receipt) == []


def test_partial_fixture_yields_partial_receipt():
    receipt = _partial_receipt()
    assert receipt.status == CapabilityState.PARTIAL
    partial = {
        operation.capability: operation
        for operation in receipt.observed_operations
        if operation.state == CapabilityState.PARTIAL
    }
    assert CapabilityId.TOOL_UPDATES in partial
    assert partial[CapabilityId.TOOL_UPDATES].limitations


def test_parse_failure_yields_broken_receipt_and_system_event():
    transcript = _load(PARSE_FAILURE_FIXTURE)
    receipt = build_receipt_from_fixture(
        transcript, ENVIRONMENT, AGENT, capture_channel="fixture"
    )
    assert receipt.status == CapabilityState.BROKEN
    assert receipt.observed_operations == []
    assert receipt.evidence  # redacted transcript digest is retained

    event = parse_failure_event(
        transcript["parse_error"],
        transcript=transcript,
        capture_channel="fixture",
        environment=ENVIRONMENT,
        agent=AGENT,
    )
    assert event["event_type"] == ACP_PARSE_FAILED_EVENT_TYPE
    assert event["source"] == "acp"
    assert event["severity"] == "error"
    assert event["evidence"]["sha256"]
    assert event["evidence"]["redacted"] is True
    assert "CANARY" not in canonical_json(event)


def test_malformed_transcript_without_explicit_parse_error_is_broken():
    transcript = {"operations": [{"state": "SUPPORTED"}]}
    assert receipt_parse_error(transcript) is not None
    receipt = build_receipt_from_fixture(
        transcript, ENVIRONMENT, AGENT, capture_channel="fixture"
    )
    assert receipt.status == CapabilityState.BROKEN


def test_fixture_evidence_stores_only_digests_of_redacted_payloads():
    receipt = _good_receipt()
    transcript_ref = next(
        ref for ref in receipt.evidence if ref.kind == "redacted_transcript"
    )
    assert len(transcript_ref.sha256) == 64
    assert transcript_ref.size is not None and transcript_ref.size > 0
    # Evidence carries references + digests, never payload text.
    assert all("sk-" not in ref.ref for ref in receipt.evidence)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _compatible_request(**overrides) -> CompatibilityRequest:
    payload = {
        "receipt": _good_receipt(),
        "requirements": [
            RequiredCapability(capability=CapabilityId.INITIALIZE),
            RequiredCapability(capability=CapabilityId.EDIT_PROPOSAL),
            RequiredCapability(capability=CapabilityId.SESSION_CLOSE),
        ],
        "expected_environment": ENVIRONMENT,
        "expected_agent": AGENT,
        "expected_protocol_version": "1",
    }
    payload.update(overrides)
    return CompatibilityRequest(**payload)


def test_evaluator_is_compatible_for_matching_receipt():
    result = evaluate_compatibility(_compatible_request())
    assert result.decision == CompatibilityDecision.COMPATIBLE
    assert result.reasons[0].code == CompatibilityReasonCode.OK
    assert all(evaluation.satisfied for evaluation in result.per_capability)


def test_evaluator_blocks_missing_receipt():
    result = evaluate_compatibility(CompatibilityRequest(receipt=None))
    assert result.decision == CompatibilityDecision.INSUFFICIENT_EVIDENCE
    assert result.reasons[0].code == CompatibilityReasonCode.MISSING_RECEIPT


def test_evaluator_blocks_receipt_hash_mismatch():
    receipt = _good_receipt()
    receipt.content_hash = "0" * 64
    result = evaluate_compatibility(_compatible_request(receipt=receipt))
    assert result.decision == CompatibilityDecision.INSUFFICIENT_EVIDENCE
    assert any(
        reason.code == CompatibilityReasonCode.RECEIPT_HASH_MISMATCH
        for reason in result.reasons
    )


def test_evaluator_blocks_missing_required_capability():
    receipt = _good_receipt()
    operations = [
        operation
        for operation in receipt.observed_operations
        if operation.capability != CapabilityId.SESSION_LOAD
    ]
    scoped = _receipt_with_operations(receipt, operations)
    result = evaluate_compatibility(
        _compatible_request(
            receipt=scoped,
            requirements=[RequiredCapability(capability=CapabilityId.SESSION_LOAD)],
        )
    )
    assert result.decision == CompatibilityDecision.INSUFFICIENT_EVIDENCE
    assert any(
        reason.code == CompatibilityReasonCode.CAPABILITY_MISSING
        for reason in result.reasons
    )
    assert result.per_capability[0].satisfied is False


def test_evaluator_blocks_unknown_required_capability():
    receipt = _good_receipt()
    operations = [
        (
            operation.model_copy(update={"state": CapabilityState.UNKNOWN})
            if operation.capability == CapabilityId.PLANS
            else operation
        )
        for operation in receipt.observed_operations
    ]
    scoped = _receipt_with_operations(receipt, operations)
    result = evaluate_compatibility(
        _compatible_request(
            receipt=scoped,
            requirements=[RequiredCapability(capability=CapabilityId.PLANS)],
        )
    )
    assert result.decision == CompatibilityDecision.INSUFFICIENT_EVIDENCE
    assert any(
        reason.code == CompatibilityReasonCode.CAPABILITY_UNKNOWN
        for reason in result.reasons
    )


def test_evaluator_blocks_partial_required_capability():
    result = evaluate_compatibility(
        _compatible_request(
            receipt=_partial_receipt(),
            requirements=[
                RequiredCapability(capability=CapabilityId.TOOL_UPDATES)
            ],
        )
    )
    assert result.decision == CompatibilityDecision.INSUFFICIENT_EVIDENCE
    assert any(
        reason.code == CompatibilityReasonCode.CAPABILITY_PARTIAL
        for reason in result.reasons
    )


def test_evaluator_allows_partial_when_partial_is_required():
    result = evaluate_compatibility(
        _compatible_request(
            receipt=_partial_receipt(),
            requirements=[
                RequiredCapability(
                    capability=CapabilityId.TOOL_UPDATES,
                    require_state=CapabilityState.PARTIAL,
                )
            ],
        )
    )
    assert result.decision == CompatibilityDecision.COMPATIBLE


def test_evaluator_blocks_unavailable_required_capability():
    receipt = _good_receipt()
    operations = [
        (
            operation.model_copy(update={"state": CapabilityState.UNAVAILABLE})
            if operation.capability == CapabilityId.PERMISSION_REQUEST
            else operation
        )
        for operation in receipt.observed_operations
    ]
    scoped = _receipt_with_operations(receipt, operations)
    result = evaluate_compatibility(
        _compatible_request(
            receipt=scoped,
            requirements=[
                RequiredCapability(capability=CapabilityId.PERMISSION_REQUEST)
            ],
        )
    )
    assert result.decision == CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
    assert any(
        reason.code == CompatibilityReasonCode.CAPABILITY_UNAVAILABLE
        for reason in result.reasons
    )


def test_evaluator_blocks_broken_receipt():
    receipt = build_receipt_from_fixture(
        _load(PARSE_FAILURE_FIXTURE),
        ENVIRONMENT,
        AGENT,
        capture_channel="fixture",
    )
    result = evaluate_compatibility(CompatibilityRequest(receipt=receipt))
    assert result.decision == CompatibilityDecision.BROKEN
    assert any(
        reason.code == CompatibilityReasonCode.PARSE_FAILED
        for reason in result.reasons
    )


def test_evaluator_blocks_environment_mismatch():
    mismatched = EnvironmentTuple(
        ide_build="IU-262.9999.99",
        ai_assistant_build=ENVIRONMENT.ai_assistant_build,
        plugin_version=ENVIRONMENT.plugin_version,
        os=ENVIRONMENT.os,
        arch=ENVIRONMENT.arch,
    )
    result = evaluate_compatibility(
        _compatible_request(expected_environment=mismatched)
    )
    assert result.decision == CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
    assert any(
        reason.code == CompatibilityReasonCode.IDE_BUILD_MISMATCH
        for reason in result.reasons
    )


def test_evaluator_blocks_protocol_version_mismatch():
    result = evaluate_compatibility(
        _compatible_request(expected_protocol_version="2")
    )
    assert result.decision == CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
    assert any(
        reason.code == CompatibilityReasonCode.PROTOCOL_VERSION_MISMATCH
        for reason in result.reasons
    )


def test_evaluator_blocks_agent_version_mismatch():
    mismatched_agent = AgentIdentity(agent_id=AGENT.agent_id, agent_version="9.9.9")
    result = evaluate_compatibility(
        _compatible_request(expected_agent=mismatched_agent)
    )
    assert result.decision == CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
    assert any(
        reason.code == CompatibilityReasonCode.AGENT_VERSION_MISMATCH
        for reason in result.reasons
    )


def test_evaluator_blocks_adapter_version_mismatch():
    mismatched_agent = AgentIdentity(
        agent_id=AGENT.agent_id,
        agent_version=AGENT.agent_version,
        adapter_version="0.5.0",
    )
    result = evaluate_compatibility(
        _compatible_request(expected_agent=mismatched_agent)
    )
    assert result.decision == CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT
    assert any(
        reason.code == CompatibilityReasonCode.ADAPTER_VERSION_MISMATCH
        for reason in result.reasons
    )


# ---------------------------------------------------------------------------
# Persistence helpers (no PostgreSQL; session is a MagicMock)
# ---------------------------------------------------------------------------


def test_persist_receipt_never_stores_canary_and_links_evidence():
    receipt = _good_receipt()
    session = MagicMock()

    row = store_module.persist_receipt(session, receipt)

    added = [call.args[0] for call in session.add.call_args_list]
    receipt_rows = [item for item in added if hasattr(item, "receipt_json")]
    assert len(receipt_rows) == 1
    # Evidence is part of receipt_json, so no child rows are written.
    assert len(added) == 1
    stored_evidence = receipt_rows[0].receipt_json["evidence"]
    assert len(stored_evidence) == len(receipt.evidence)
    assert [item["sha256"] for item in stored_evidence] == [
        evidence.sha256 for evidence in receipt.evidence
    ]

    serialized = canonical_json(receipt_rows[0].receipt_json)
    assert "CANARY" not in serialized
    assert "sk-" not in serialized
    assert row.receipt_id == receipt.receipt_id
    assert row.content_hash == receipt.content_hash
    session.commit.assert_called_once()
    session.refresh.assert_called_once()


def test_store_round_trips_receipt_json():
    receipt = _good_receipt()
    fake_row = SimpleNamespace(receipt_json=receipt.model_dump(mode="json"))
    restored = store_module.row_to_receipt(fake_row)
    assert restored.content_hash == receipt.content_hash
    assert receipt_content_hash(restored) == receipt.content_hash



# ---------------------------------------------------------------------------
# Route removal: capability self-attestation endpoints are gone
# ---------------------------------------------------------------------------


def test_compatibility_self_attestation_routes_are_removed():
    """The public compatibility receipts/evaluate/fixtures surface is removed.

    Qualification stays confined to the admin-only /research/packages operator
    surface; the pure evaluator used by bootstrap is unaffected.
    """
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/compatibility/receipts" not in paths
    assert "/research/compatibility/evaluate" not in paths
    assert "/research/compatibility/fixtures" not in paths
    assert "/research/packages/receipts" in paths
