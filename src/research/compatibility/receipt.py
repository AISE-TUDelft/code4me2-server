"""Fixture-transcript ingestion and receipt validation.

The ingestion path turns a captured ACP observation transcript into a redacted,
hashed :class:`~research.compatibility.models.AcpCapabilityReceiptV1`. Raw
payloads are never embedded in the receipt: only redacted digests are, so a
receipt remains safe to persist and export.

A malformed transcript never fails open. It yields a ``BROKEN`` receipt and a
separate ``acp_parse_failed`` system event built by
:func:`parse_failure_event`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import uuid4

from pydantic import BaseModel

from .canonical import canonical_bytes, canonical_hash, receipt_content_hash
from .enums import (
    CapabilityId,
    CapabilityState,
    CompatibilityReasonCode,
    EnforcementOwner,
    Fidelity,
)
from .models import (
    AcpCapabilityReceiptV1,
    AgentIdentity,
    CapabilityResult,
    EnvironmentTuple,
    EvidenceRef,
    ReasonDetail,
)
from .redaction import redact

# Event contract for a failed fixture parse. Kept as a plain dict so the
# canonical research event envelope stays the single event authority (no raw content).
ACP_PARSE_FAILED_EVENT_TYPE = "system.acp_parse_failed"

_REQUIRED_ENVIRONMENT_FIELDS = (
    "ide_build",
    "ai_assistant_build",
    "plugin_version",
    "os",
    "arch",
)


def _coerce_fidelity(value: Any) -> Fidelity:
    if value is None:
        return Fidelity.NORMALIZED
    try:
        return Fidelity(str(value).strip().upper())
    except ValueError:
        return Fidelity.INFERRED


def _coerce_owner(value: Any) -> EnforcementOwner:
    if value is None:
        return EnforcementOwner.UNKNOWN
    try:
        return EnforcementOwner(str(value).strip().upper())
    except ValueError:
        return EnforcementOwner.UNKNOWN


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _extract_capabilities(message: Any) -> Optional[Mapping[str, Any]]:
    """Pull the capability object out of an initialize request/response."""
    if not isinstance(message, Mapping):
        return None
    for container_key in ("result", "params"):
        container = message.get(container_key)
        if isinstance(container, Mapping):
            for caps_key in ("agentCapabilities", "clientCapabilities"):
                caps = container.get(caps_key)
                if isinstance(caps, Mapping):
                    return caps
    for caps_key in ("agentCapabilities", "clientCapabilities"):
        caps = message.get(caps_key)
        if isinstance(caps, Mapping):
            return caps
    return None


def _flatten_capabilities(
    prefix: str, value: Any, out: dict[str, str | bool]
) -> None:
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            _flatten_capabilities(name, item, out)
        elif isinstance(item, bool):
            out[name] = item
        elif isinstance(item, str):
            out[name] = item
        elif item is None:
            out[name] = False
        else:
            out[name] = str(item)


def _declared_capabilities(transcript: Mapping[str, Any]) -> dict[str, str | bool]:
    declared: dict[str, str | bool] = {}
    raw = transcript.get("declared")
    if isinstance(raw, Mapping):
        _flatten_capabilities("", raw, declared)
    _flatten_capabilities(
        "client", _extract_capabilities(transcript.get("initialize_request")), declared
    )
    _flatten_capabilities(
        "agent", _extract_capabilities(transcript.get("initialize_response")), declared
    )
    return declared


def _protocol_version(transcript: Mapping[str, Any]) -> str:
    direct = transcript.get("protocol_version")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for message_key in ("initialize_response", "initialize_request"):
        message = transcript.get(message_key)
        if not isinstance(message, Mapping):
            continue
        for container_key in ("result", "params"):
            container = message.get(container_key)
            if isinstance(container, Mapping):
                version = container.get("protocolVersion")
                if isinstance(version, str) and version.strip():
                    return version.strip()
    return "unknown"


def _evidence_ref_for_operation(
    raw: Mapping[str, Any],
    capability: CapabilityId,
    index: int,
    capture_channel: str,
) -> EvidenceRef:
    payload = raw.get("evidence")
    if payload is None:
        payload = {key: raw.get(key) for key in ("method", "id", "result", "error")}
    redacted = redact(payload).value
    encoded = canonical_bytes(redacted)
    return EvidenceRef(
        ref=f"fixture://{capture_channel}/operation/{index}/{capability.value.lower()}",
        kind="acp_operation",
        sha256=canonical_hash(redacted),
        size=len(encoded),
        redacted=True,
    )


def _redacted_transcript_evidence(
    transcript: Any, capture_channel: str
) -> EvidenceRef:
    redacted = redact(transcript).value
    encoded = canonical_bytes(redacted)
    return EvidenceRef(
        ref=f"fixture://{capture_channel}/transcript.redacted.json",
        kind="redacted_transcript",
        sha256=canonical_hash(redacted),
        size=len(encoded),
        redacted=True,
    )


def _parse_operations(
    transcript: Any, capture_channel: str
) -> tuple[list[CapabilityResult], list[EvidenceRef], Optional[str]]:
    """Parse fixture operations, returning ``(results, evidence, parse_error)``."""
    if not isinstance(transcript, Mapping):
        return [], [], "fixture transcript is not an object"

    explicit_error = transcript.get("parse_error")
    if explicit_error:
        return [], [], str(explicit_error)

    raw_operations = transcript.get("operations")
    if raw_operations is None:
        raw_operations = []
    if not isinstance(raw_operations, list):
        return [], [], "fixture 'operations' must be a list"

    results: list[CapabilityResult] = []
    evidence: list[EvidenceRef] = []
    for index, raw in enumerate(raw_operations):
        if not isinstance(raw, Mapping):
            return [], [], f"operation {index} is not an object"
        capability_raw = raw.get("capability")
        state_raw = raw.get("state")
        if not capability_raw or not state_raw:
            return [], [], f"operation {index} is missing capability or state"
        try:
            capability = CapabilityId(str(capability_raw).strip().upper())
        except ValueError:
            return [], [], f"operation {index} has unknown capability {capability_raw!r}"
        try:
            state = CapabilityState(str(state_raw).strip().upper())
        except ValueError:
            return [], [], f"operation {index} has unknown state {state_raw!r}"

        ref = _evidence_ref_for_operation(raw, capability, index, capture_channel)
        evidence.append(ref)

        limitations = raw.get("limitations") or []
        if isinstance(limitations, str):
            limitations = [limitations]
        elif not isinstance(limitations, list):
            limitations = [str(limitations)]

        results.append(
            CapabilityResult(
                capability=capability,
                state=state,
                fidelity=_coerce_fidelity(raw.get("fidelity")),
                enforcement_owner=_coerce_owner(raw.get("enforcement_owner")),
                limitations=[str(item) for item in limitations],
                observed_at=_parse_datetime(raw.get("observed_at")),
                evidence_refs=[ref.ref],
            )
        )
    return results, evidence, None


def _aggregate_status(results: list[CapabilityResult]) -> CapabilityState:
    if not results:
        return CapabilityState.UNKNOWN
    states = {result.state for result in results}
    for state in (
        CapabilityState.BROKEN,
        CapabilityState.UNAVAILABLE,
        CapabilityState.UNKNOWN,
        CapabilityState.PARTIAL,
    ):
        if state in states:
            return state
    return CapabilityState.SUPPORTED


def build_receipt_from_fixture(
    transcript: dict,
    environment: EnvironmentTuple,
    agent: AgentIdentity,
    *,
    capture_channel: str,
    captured_at: Optional[datetime] = None,
) -> AcpCapabilityReceiptV1:
    """Build a redacted, hashed receipt from a captured fixture transcript.

    A transcript is treated as broken when it carries a ``parse_error`` or is
    structurally malformed (unknown capability/state, non-object operations,
    or a non-object transcript). In that case the receipt has
    ``status=BROKEN``, no observed operations, and only the redacted
    transcript digest as evidence.
    """
    captured = captured_at or datetime.now(timezone.utc)
    operations, operation_evidence, parse_error = _parse_operations(
        transcript, capture_channel
    )

    if parse_error is not None:
        operations = []
        operation_evidence = []

    evidence = [_redacted_transcript_evidence(transcript, capture_channel)]
    evidence.extend(operation_evidence)

    if parse_error is not None:
        status = CapabilityState.BROKEN
    else:
        status = _aggregate_status(operations)

    declared = _declared_capabilities(transcript) if isinstance(transcript, Mapping) else {}

    receipt = AcpCapabilityReceiptV1(
        schema_version="1",
        receipt_id=uuid4(),
        captured_at=captured,
        environment=environment,
        agent=agent,
        protocol_version=(
            _protocol_version(transcript) if isinstance(transcript, Mapping) else "unknown"
        ),
        declared=declared,
        observed_operations=operations,
        evidence=evidence,
        status=status,
        content_hash="",
    )
    receipt.content_hash = receipt_content_hash(receipt)
    return receipt


def receipt_parse_error(transcript: Any) -> Optional[str]:
    """Return the parse-error reason a transcript would yield, or ``None``."""
    return _parse_operations(transcript, "fixture")[2]


def parse_failure_event(
    reason: str,
    *,
    transcript: Any = None,
    capture_channel: str = "fixture",
    environment: Optional[EnvironmentTuple] = None,
    agent: Optional[AgentIdentity] = None,
    captured_at: Optional[datetime] = None,
) -> dict:
    """Build the ``system.acp_parse_failed`` event for a broken transcript.

    The event carries only a hashed evidence reference and caller-supplied
    metadata; it never fabricates probe content.
    """
    captured = captured_at or datetime.now(timezone.utc)
    evidence = _redacted_transcript_evidence(transcript, capture_channel)
    return {
        "event_type": ACP_PARSE_FAILED_EVENT_TYPE,
        "source": "acp",
        "severity": "error",
        "reason": str(reason),
        "captured_at": captured.isoformat(),
        "environment": environment.model_dump(mode="json") if environment else None,
        "agent": agent.model_dump(mode="json") if agent else None,
        "evidence": evidence.model_dump(mode="json"),
    }


def _as_mapping(receipt: AcpCapabilityReceiptV1 | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(receipt, BaseModel):
        return receipt.model_dump(mode="json")
    if isinstance(receipt, Mapping):
        return receipt
    return {}


def _safe_capability(value: Any) -> Optional[CapabilityId]:
    if value is None:
        return None
    try:
        return CapabilityId(str(value).strip().upper())
    except ValueError:
        return None


def validate_receipt(
    receipt: AcpCapabilityReceiptV1 | Mapping[str, Any],
) -> list[ReasonDetail]:
    """Return the reasons a receipt makes an unscoped capability claim.

    An empty list means the receipt is scoped: it names its environment,
    agent identity, negotiated protocol version, and every observed operation
    carries both a capability and a state.
    """
    data = _as_mapping(receipt)
    reasons: list[ReasonDetail] = []

    environment = data.get("environment")
    if not isinstance(environment, Mapping):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.ENVIRONMENT_MISMATCH,
                message="receipt environment is missing",
            )
        )
    else:
        for field in _REQUIRED_ENVIRONMENT_FIELDS:
            if not environment.get(field):
                reasons.append(
                    ReasonDetail(
                        code=CompatibilityReasonCode.ENVIRONMENT_MISMATCH,
                        message=f"receipt environment.{field} is missing",
                    )
                )

    agent = data.get("agent")
    if not isinstance(agent, Mapping) or not agent.get("agent_id"):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.AGENT_ID_MISMATCH,
                message="receipt agent.agent_id is missing",
            )
        )
    if not isinstance(agent, Mapping) or not agent.get("agent_version"):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.AGENT_VERSION_MISMATCH,
                message="receipt agent.agent_version is missing",
            )
        )

    if not data.get("protocol_version"):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.PROTOCOL_VERSION_MISMATCH,
                message="receipt protocol_version is missing",
            )
        )

    operations = data.get("observed_operations")
    if operations is None:
        operations = []
    if not isinstance(operations, list):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.PARSE_FAILED,
                message="receipt observed_operations is not a list",
            )
        )
    else:
        for index, operation in enumerate(operations):
            if not isinstance(operation, Mapping) or not operation.get("capability"):
                reasons.append(
                    ReasonDetail(
                        code=CompatibilityReasonCode.CAPABILITY_MISSING,
                        message=f"observed operation {index} is missing a capability",
                    )
                )
            elif not operation.get("state"):
                reasons.append(
                    ReasonDetail(
                        code=CompatibilityReasonCode.CAPABILITY_UNKNOWN,
                        capability=_safe_capability(operation.get("capability")),
                        message=f"observed operation {index} is missing a state",
                    )
                )
    return reasons
