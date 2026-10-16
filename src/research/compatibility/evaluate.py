"""Deterministic compatibility evaluator.

Every rule below is explicit and order-independent except for the aggregate
severity ranking (``BROKEN`` > ``INCOMPATIBLE_ENVIRONMENT`` >
``INSUFFICIENT_EVIDENCE`` > ``COMPATIBLE``). Evaluation never silently passes a
missing, partial, or unverified requirement.
"""

from __future__ import annotations

from typing import Optional

from .canonical import receipt_content_hash
from .enums import (
    CapabilityState,
    CompatibilityDecision,
    CompatibilityReasonCode,
)
from .models import (
    AcpCapabilityReceiptV1,
    AgentIdentity,
    CapabilityEvaluation,
    CompatibilityRequest,
    CompatibilityResult,
    EnvironmentTuple,
    ReasonDetail,
    RequiredCapability,
)
from .receipt import validate_receipt

_ENVIRONMENT_FIELD_REASONS = {
    "ide_build": CompatibilityReasonCode.IDE_BUILD_MISMATCH,
    "ai_assistant_build": CompatibilityReasonCode.AI_ASSISTANT_BUILD_MISMATCH,
    "plugin_version": CompatibilityReasonCode.PLUGIN_VERSION_MISMATCH,
    "os": CompatibilityReasonCode.ENVIRONMENT_MISMATCH,
    "arch": CompatibilityReasonCode.ENVIRONMENT_MISMATCH,
    "host_kind": CompatibilityReasonCode.ENVIRONMENT_MISMATCH,
}

_AGENT_FIELD_REASONS = {
    "agent_id": CompatibilityReasonCode.AGENT_ID_MISMATCH,
    "agent_version": CompatibilityReasonCode.AGENT_VERSION_MISMATCH,
    "adapter_version": CompatibilityReasonCode.ADAPTER_VERSION_MISMATCH,
}

# Aggregate severity ranking (higher wins).
_SEVERITY = {
    CompatibilityDecision.COMPATIBLE: 0,
    CompatibilityDecision.INSUFFICIENT_EVIDENCE: 1,
    CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT: 2,
    CompatibilityDecision.BROKEN: 3,
}

_REASON_SEVERITY = {
    CompatibilityReasonCode.OK: CompatibilityDecision.COMPATIBLE,
    CompatibilityReasonCode.MISSING_RECEIPT: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.RECEIPT_UNVERIFIED: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.RECEIPT_HASH_MISMATCH: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.ENVIRONMENT_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.IDE_BUILD_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.AI_ASSISTANT_BUILD_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.PLUGIN_VERSION_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.AGENT_ID_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.AGENT_VERSION_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.ADAPTER_VERSION_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.PROTOCOL_VERSION_MISMATCH: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.CAPABILITY_MISSING: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.CAPABILITY_UNKNOWN: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.CAPABILITY_PARTIAL: CompatibilityDecision.INSUFFICIENT_EVIDENCE,
    CompatibilityReasonCode.CAPABILITY_UNAVAILABLE: CompatibilityDecision.INCOMPATIBLE_ENVIRONMENT,
    CompatibilityReasonCode.CAPABILITY_BROKEN: CompatibilityDecision.BROKEN,
    CompatibilityReasonCode.PARSE_FAILED: CompatibilityDecision.BROKEN,
}


def _result(
    reasons: list[ReasonDetail],
    per_capability: Optional[list[CapabilityEvaluation]] = None,
) -> CompatibilityResult:
    if not reasons:
        return CompatibilityResult(
            decision=CompatibilityDecision.COMPATIBLE,
            reasons=[ReasonDetail(code=CompatibilityReasonCode.OK, message="all requirements satisfied")],
            per_capability=per_capability or [],
        )
    decision = CompatibilityDecision.COMPATIBLE
    for reason in reasons:
        candidate = _REASON_SEVERITY.get(
            reason.code, CompatibilityDecision.INSUFFICIENT_EVIDENCE
        )
        if _SEVERITY[candidate] > _SEVERITY[decision]:
            decision = candidate
    return CompatibilityResult(
        decision=decision,
        reasons=reasons,
        per_capability=per_capability or [],
    )


def _environment_reasons(
    expected: Optional[EnvironmentTuple], actual: EnvironmentTuple
) -> list[ReasonDetail]:
    if expected is None:
        return []
    reasons: list[ReasonDetail] = []
    for field, code in _ENVIRONMENT_FIELD_REASONS.items():
        expected_value = getattr(expected, field, None)
        if expected_value is None:
            continue
        actual_value = getattr(actual, field, None)
        if actual_value != expected_value:
            reasons.append(
                ReasonDetail(
                    code=code,
                    message=(
                        f"environment.{field} expected {expected_value!r} "
                        f"but receipt has {actual_value!r}"
                    ),
                )
            )
    return reasons


def _agent_reasons(
    expected: Optional[AgentIdentity], actual: AgentIdentity
) -> list[ReasonDetail]:
    if expected is None:
        return []
    reasons: list[ReasonDetail] = []
    for field, code in _AGENT_FIELD_REASONS.items():
        expected_value = getattr(expected, field, None)
        if expected_value is None:
            continue
        actual_value = getattr(actual, field, None)
        if actual_value != expected_value:
            reasons.append(
                ReasonDetail(
                    code=code,
                    message=(
                        f"agent.{field} expected {expected_value!r} "
                        f"but receipt has {actual_value!r}"
                    ),
                )
            )
    return reasons


def _evaluate_requirement(
    receipt: AcpCapabilityReceiptV1, requirement: RequiredCapability
) -> CapabilityEvaluation:
    observed = next(
        (
            operation
            for operation in receipt.observed_operations
            if operation.capability == requirement.capability
        ),
        None,
    )
    base = CapabilityEvaluation(
        capability=requirement.capability,
        required_state=requirement.require_state,
        observed_state=observed.state if observed else None,
        enforcement_owner=observed.enforcement_owner if observed else None,
        satisfied=False,
    )

    if observed is None:
        base.reason = ReasonDetail(
            code=CompatibilityReasonCode.CAPABILITY_MISSING,
            capability=requirement.capability,
            message=(
                f"receipt has no observed operation for {requirement.capability.value}"
            ),
        )
        return base

    state = observed.state
    if state == CapabilityState.BROKEN:
        base.reason = ReasonDetail(
            code=CompatibilityReasonCode.CAPABILITY_BROKEN,
            capability=requirement.capability,
            message=f"{requirement.capability.value} is broken in this environment",
        )
        return base

    if state == CapabilityState.UNAVAILABLE:
        base.reason = ReasonDetail(
            code=CompatibilityReasonCode.CAPABILITY_UNAVAILABLE,
            capability=requirement.capability,
            message=f"{requirement.capability.value} is unavailable in this environment",
        )
        return base

    if state == CapabilityState.UNKNOWN:
        if requirement.require_state == CapabilityState.UNKNOWN:
            base.satisfied = True
            return base
        base.reason = ReasonDetail(
            code=CompatibilityReasonCode.CAPABILITY_UNKNOWN,
            capability=requirement.capability,
            message=f"{requirement.capability.value} was never observed (UNKNOWN)",
        )
        return base

    if state == CapabilityState.PARTIAL:
        if requirement.require_state in (
            CapabilityState.PARTIAL,
            CapabilityState.UNKNOWN,
        ):
            base.satisfied = True
            return base
        base.reason = ReasonDetail(
            code=CompatibilityReasonCode.CAPABILITY_PARTIAL,
            capability=requirement.capability,
            message=(
                f"{requirement.capability.value} is only PARTIAL but "
                f"{requirement.require_state.value} was required"
            ),
        )
        return base

    # Remaining state: SUPPORTED.
    if requirement.require_state == CapabilityState.SUPPORTED:
        base.satisfied = True
        return base

    # require_state is PARTIAL or UNKNOWN, both weaker than SUPPORTED.
    base.satisfied = True
    return base


def evaluate_compatibility(request: CompatibilityRequest) -> CompatibilityResult:
    """Evaluate ``request`` and return a typed, justified decision."""
    receipt = request.receipt
    if receipt is None:
        return _result(
            [
                ReasonDetail(
                    code=CompatibilityReasonCode.MISSING_RECEIPT,
                    message="no capability receipt was supplied",
                )
            ]
        )

    if not isinstance(receipt, AcpCapabilityReceiptV1):
        try:
            receipt = AcpCapabilityReceiptV1.model_validate(receipt)
        except Exception as error:  # noqa: BLE001 - report any parse failure
            return _result(
                [
                    ReasonDetail(
                        code=CompatibilityReasonCode.RECEIPT_UNVERIFIED,
                        message=f"receipt could not be parsed: {error}",
                    )
                ]
            )

    validation_reasons = validate_receipt(receipt)
    if validation_reasons:
        return _result(
            [
                ReasonDetail(
                    code=CompatibilityReasonCode.RECEIPT_UNVERIFIED,
                    capability=reason.capability,
                    message=f"receipt is not scoped: {reason.message}",
                )
                for reason in validation_reasons
            ]
        )

    broken = receipt.status == CapabilityState.BROKEN or any(
        operation.state == CapabilityState.BROKEN
        for operation in receipt.observed_operations
    )
    if broken:
        if receipt.status == CapabilityState.BROKEN and not receipt.observed_operations:
            reason = ReasonDetail(
                code=CompatibilityReasonCode.PARSE_FAILED,
                message="fixture transcript could not be parsed",
            )
        else:
            reason = ReasonDetail(
                code=CompatibilityReasonCode.CAPABILITY_BROKEN,
                message="receipt contains a broken capability",
            )
        return _result([reason])

    if receipt_content_hash(receipt) != receipt.content_hash:
        return _result(
            [
                ReasonDetail(
                    code=CompatibilityReasonCode.RECEIPT_HASH_MISMATCH,
                    message="receipt content hash does not match its canonical content",
                )
            ]
        )

    reasons: list[ReasonDetail] = []
    reasons.extend(_environment_reasons(request.expected_environment, receipt.environment))
    reasons.extend(_agent_reasons(request.expected_agent, receipt.agent))
    if (
        request.expected_protocol_version is not None
        and request.expected_protocol_version != receipt.protocol_version
    ):
        reasons.append(
            ReasonDetail(
                code=CompatibilityReasonCode.PROTOCOL_VERSION_MISMATCH,
                message=(
                    f"protocol_version expected {request.expected_protocol_version!r} "
                    f"but receipt has {receipt.protocol_version!r}"
                ),
            )
        )

    per_capability = [
        _evaluate_requirement(receipt, requirement)
        for requirement in request.requirements
    ]
    reasons.extend(
        evaluation.reason
        for evaluation in per_capability
        if evaluation.reason is not None
    )

    return _result(reasons, per_capability)
