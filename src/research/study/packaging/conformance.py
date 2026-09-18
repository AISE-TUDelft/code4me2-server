"""Capability-aware conformance runner and qualification linking (Issue 11).

The runner executes only cases whose prerequisites are established by injected
receipt/fixture evidence. A missing capability yields ``UNSUPPORTED``/``UNKNOWN``,
never ``PASS``. A case that mutates state without a successful cleanup yields
``FAIL``. Results are bound to the exact artifact/adapter/host/plugin/protocol/
fixture digests by :class:`ConformanceReceiptV1`.

The qualification link promotes an agent release only when a receipt for the
claimed platform/host proves every required case ``PASS``; it never auto-promotes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from research.canonical import canonical_hash

from .enums import ConformanceStatus, PackageReasonCode, PrerequisiteState
from .models import (
    ConformanceCaseResultV1,
    ConformanceCaseV1,
    ConformanceReceiptV1,
    PlatformTriple,
    normalize_sha256,
)

if TYPE_CHECKING:
    from research.study.agents.models import AgentReleaseV1

__all__ = [
    "CaseObservation",
    "ConformanceObserver",
    "ConformanceRunner",
    "QualificationDecision",
    "qualification_for_release",
]

_BASE = ConfigDict(extra="forbid")

#: Worst-first precedence for a receipt's aggregate status.
_STATUS_PRECEDENCE = (
    ConformanceStatus.FAIL,
    ConformanceStatus.BLOCKED,
    ConformanceStatus.UNKNOWN,
    ConformanceStatus.UNSUPPORTED,
    ConformanceStatus.PASS,
)


class CaseObservation(BaseModel):
    """What an observer observed while executing one case."""

    model_config = _BASE

    host_observations: list[str] = Field(default_factory=list)
    agent_observations: list[str] = Field(default_factory=list)
    cleanup_ok: Optional[bool] = None
    performance_ms: Optional[float] = None
    evidence: dict[str, str] = Field(default_factory=dict)
    status: Optional[ConformanceStatus] = None
    error: Optional[str] = None


class ConformanceObserver(Protocol):
    """Executes one conformance case and returns its observations."""

    def observe(self, case: ConformanceCaseV1) -> CaseObservation:  # pragma: no cover - protocol
        ...


def _coerce_state(value: object) -> PrerequisiteState:
    if isinstance(value, PrerequisiteState):
        return value
    text = str(getattr(value, "value", value)).strip().upper()
    try:
        return PrerequisiteState(text)
    except ValueError:
        return PrerequisiteState.UNKNOWN


class ConformanceRunner:
    """Runs conformance cases with explicit, truthful statuses."""

    def __init__(self, observer: ConformanceObserver) -> None:
        self._observer = observer

    def run(
        self,
        cases: Sequence[ConformanceCaseV1],
        prerequisites: Mapping[str, object],
        *,
        artifact_digest: str,
        adapter_digest: str,
        host: PlatformTriple,
        plugin_version: str,
        protocol_version: str,
        fixture_digests: Optional[Mapping[str, str]] = None,
        now: Optional[datetime] = None,
        receipt_id: Optional[UUID] = None,
    ) -> ConformanceReceiptV1:
        """Execute prerequisite-established cases and return a bound receipt."""
        states = {name: _coerce_state(value) for name, value in prerequisites.items()}
        results = [self._run_case(case, states) for case in cases]
        status = self._aggregate(results)
        return ConformanceReceiptV1(
            receipt_id=receipt_id or uuid.uuid4(),
            artifact_digest=artifact_digest,
            adapter_digest=adapter_digest,
            host=host,
            plugin_version=plugin_version,
            protocol_version=protocol_version,
            fixture_digests=dict(fixture_digests or {}),
            case_results=results,
            status=status,
            created_at=now or datetime.now(timezone.utc),
        )

    def _run_case(
        self, case: ConformanceCaseV1, states: Mapping[str, PrerequisiteState]
    ) -> ConformanceCaseResultV1:
        missing = [name for name in case.prerequisites if states.get(name) != PrerequisiteState.ESTABLISHED]
        if missing:
            unsupported = [
                name
                for name in missing
                if states.get(name) == PrerequisiteState.UNSUPPORTED
            ]
            if unsupported:
                return self._result(
                    case,
                    ConformanceStatus.UNSUPPORTED,
                    reason="capability is not supported on this host/agent: "
                    + ", ".join(sorted(unsupported)),
                )
            return self._result(
                case,
                ConformanceStatus.UNKNOWN,
                reason="capability is not established: " + ", ".join(sorted(missing)),
            )

        try:
            observation = self._observer.observe(case)
        except Exception as error:  # noqa: BLE001 - an observer failure blocks the case
            return self._result(
                case, ConformanceStatus.BLOCKED, reason=f"observer raised: {error}"
            )

        if observation.error:
            return self._result(
                case, ConformanceStatus.BLOCKED, observation, reason=observation.error
            )

        # A mutation without a verified cleanup is a failure, never a pass.
        if observation.cleanup_ok is False:
            return self._result(
                case,
                ConformanceStatus.FAIL,
                observation,
                reason="case mutated state without a successful cleanup",
            )

        if observation.status is not None and observation.status != ConformanceStatus.PASS:
            return self._result(
                case,
                observation.status,
                observation,
                reason="observer reported a non-pass status",
            )

        if case.cleanup_assertion and observation.cleanup_ok is None:
            return self._result(
                case,
                ConformanceStatus.UNKNOWN,
                observation,
                reason="cleanup assertion was not verified",
            )

        missing_host = [
            expected
            for expected in case.expected_host_observations
            if expected not in observation.host_observations
        ]
        missing_agent = [
            expected
            for expected in case.expected_agent_observations
            if expected not in observation.agent_observations
        ]
        if missing_host or missing_agent:
            return self._result(
                case,
                ConformanceStatus.FAIL,
                observation,
                reason="expected observations were not all present",
            )

        if (
            case.max_performance_ms is not None
            and observation.performance_ms is not None
            and observation.performance_ms > case.max_performance_ms
        ):
            return self._result(
                case,
                ConformanceStatus.FAIL,
                observation,
                reason=(
                    f"performance {observation.performance_ms}ms exceeded bound "
                    f"{case.max_performance_ms}ms"
                ),
            )

        return self._result(case, ConformanceStatus.PASS, observation)

    @staticmethod
    def _result(
        case: ConformanceCaseV1,
        status: ConformanceStatus,
        observation: Optional[CaseObservation] = None,
        *,
        reason: str = "",
    ) -> ConformanceCaseResultV1:
        observed = observation or CaseObservation()
        evidence_digest = canonical_hash(
            {
                "case_id": case.case_id,
                "status": status.value,
                "host_observations": observed.host_observations,
                "agent_observations": observed.agent_observations,
                "cleanup_ok": observed.cleanup_ok,
                "evidence": observed.evidence,
                "reason": reason,
            }
        )
        return ConformanceCaseResultV1(
            case_id=case.case_id,
            status=status,
            host_observations=list(observed.host_observations),
            agent_observations=list(observed.agent_observations),
            cleanup_ok=observed.cleanup_ok,
            performance_ms=observed.performance_ms,
            evidence_digest=evidence_digest,
            reason=reason,
        )

    @staticmethod
    def _aggregate(results: Sequence[ConformanceCaseResultV1]) -> ConformanceStatus:
        if not results:
            return ConformanceStatus.UNKNOWN
        seen = {result.status for result in results}
        for status in _STATUS_PRECEDENCE:
            if status in seen:
                return status
        return ConformanceStatus.UNKNOWN


class QualificationDecision(BaseModel):
    """Typed outcome of linking conformance evidence to release qualification."""

    model_config = _BASE

    promote: bool
    reason: PackageReasonCode
    receipt_id: Optional[UUID] = None
    message: str = ""


def qualification_for_release(
    release: AgentReleaseV1,
    receipts: Sequence[ConformanceReceiptV1],
    *,
    required_cases: Sequence[str],
    os: str,
    arch: str,
) -> QualificationDecision:
    """Decide whether a release may be promoted for one host/platform tuple.

    Promotion requires a receipt that binds the exact release artifact digest and
    adapter digest for ``(os, arch)`` and proves every ``required_cases`` entry
    ``PASS``. Anything else is an explicit non-promotion.
    """
    artifact = next(
        (item for item in release.artifacts if item.os == os and item.arch == arch),
        None,
    )
    if artifact is None:
        return QualificationDecision(
            promote=False,
            reason=PackageReasonCode.MISSING_COMPONENT,
            message=f"release has no artifact for '{os}-{arch}'",
        )
    if release.adapter is None:
        return QualificationDecision(
            promote=False,
            reason=PackageReasonCode.CONFORMANCE_NOT_PASSED,
            message="release declares no adapter identity",
        )

    artifact_hex = normalize_sha256(artifact.sha256)
    adapter_hex = normalize_sha256(release.adapter.digest)
    required = set(required_cases)

    candidates = [
        receipt
        for receipt in receipts
        if normalize_sha256(receipt.artifact_digest) == artifact_hex
        and receipt.adapter_digest == release.adapter.digest
    ]
    if not candidates:
        return QualificationDecision(
            promote=False,
            reason=PackageReasonCode.CONFORMANCE_NOT_PASSED,
            message="no conformance receipt is bound to this artifact/adapter",
        )

    host_matched = [
        receipt
        for receipt in candidates
        if receipt.host.os == os and receipt.host.arch == arch
    ]
    if not host_matched:
        return QualificationDecision(
            promote=False,
            reason=PackageReasonCode.RECEIPT_HOST_MISMATCH,
            message=f"no conformance receipt was recorded for '{os}-{arch}'",
        )

    for receipt in host_matched:
        if adapter_hex is not None and normalize_sha256(receipt.adapter_digest) != adapter_hex:
            continue
        if required.issubset(receipt.passed_cases()):
            return QualificationDecision(
                promote=True,
                reason=PackageReasonCode.OK,
                receipt_id=receipt.receipt_id,
                message="all required cases passed for the claimed platform/host",
            )

    return QualificationDecision(
        promote=False,
        reason=PackageReasonCode.CONFORMANCE_NOT_PASSED,
        message="required conformance cases did not all pass for this platform/host",
    )
