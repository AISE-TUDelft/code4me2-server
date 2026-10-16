"""Release gate evaluator (Issue 13).

``evaluate_release`` is a pure function over immutable evidence plus the
required component/evidence lists. Any unknown or missing required item yields
``NO_GO``; an expired component receipt or fixed study configuration yields
``EXPIRED``; a GO with recorded limitations is explicitly ``GO_WITH_LIMITS``.
A material artifact/configuration change invalidates an old GO.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from .enums import OperationsReasonCode, ReleaseDecision
from .models import (
    ReleaseEvaluationResult,
    ReleaseEvidenceV1,
    ReleaseReason,
)

__all__ = [
    "EXPIRED_REASONS",
    "HARD_REASONS",
    "NON_BLOCKING_REASONS",
    "evaluate_release",
    "record_decision",
]

HARD_REASONS = frozenset(
    {
        OperationsReasonCode.REQUIRED_COMPONENT_MISSING,
        OperationsReasonCode.REQUIRED_EVIDENCE_MISSING,
        OperationsReasonCode.MATERIAL_CHANGE,
        OperationsReasonCode.EVIDENCE_INCOMPLETE,
    }
)

EXPIRED_REASONS = frozenset(
    {
        OperationsReasonCode.COMPONENT_RECEIPT_EXPIRED,
        OperationsReasonCode.CONFIG_EXPIRED,
    }
)

NON_BLOCKING_REASONS = frozenset({OperationsReasonCode.LIMITATIONS_PRESENT})

_PASS = "PASS"
_UNKNOWN_DIGESTS = frozenset({"", "UNKNOWN", "MISSING", "LATEST"})


def _reason(
    code: OperationsReasonCode, message: str, field: str = ""
) -> ReleaseReason:
    return ReleaseReason(code=code, message=message, field=field)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def evaluate_release(
    evidence: ReleaseEvidenceV1,
    *,
    required_components: Iterable[str],
    required_evidence: Iterable[str],
    now: Optional[datetime] = None,
    current_component_digests: Optional[dict[str, str]] = None,
    current_config_digest: Optional[str] = None,
) -> ReleaseEvaluationResult:
    """Evaluate release evidence and return the typed gate decision."""
    timestamp = _now(now)
    reasons: list[ReleaseReason] = []

    for name in required_components:
        digest = evidence.component_artifacts.get(name)
        if digest is None:
            reasons.append(
                _reason(
                    OperationsReasonCode.REQUIRED_COMPONENT_MISSING,
                    f"required component {name!r} has no recorded digest",
                    name,
                )
            )
        elif digest.strip().upper() in _UNKNOWN_DIGESTS:
            reasons.append(
                _reason(
                    OperationsReasonCode.REQUIRED_COMPONENT_MISSING,
                    f"required component {name!r} digest is unknown",
                    name,
                )
            )

        expires_at = evidence.component_expires_at.get(name)
        if expires_at is not None and expires_at < timestamp:
            reasons.append(
                _reason(
                    OperationsReasonCode.COMPONENT_RECEIPT_EXPIRED,
                    f"component {name!r} receipt expired at {expires_at.isoformat()}",
                    name,
                )
            )

        if current_component_digests is not None:
            current = current_component_digests.get(name)
            if current is not None and digest is not None and current != digest:
                reasons.append(
                    _reason(
                        OperationsReasonCode.MATERIAL_CHANGE,
                        f"component {name!r} digest changed since the recorded evidence",
                        name,
                    )
                )

    for name in required_evidence:
        state = evidence.test_results.get(name)
        if state is None or state.strip().upper() != _PASS:
            reasons.append(
                _reason(
                    OperationsReasonCode.REQUIRED_EVIDENCE_MISSING,
                    f"required evidence {name!r} is missing or not PASS",
                    name,
                )
            )

    if evidence.config_expires_at is not None and evidence.config_expires_at < timestamp:
        reasons.append(
            _reason(
                OperationsReasonCode.CONFIG_EXPIRED,
                "the fixed study configuration expired at "
                f"{evidence.config_expires_at.isoformat()}",
                "study_id",
            )
        )

    if (
        current_config_digest is not None
        and evidence.config_digest is not None
        and current_config_digest != evidence.config_digest
    ):
        reasons.append(
            _reason(
                OperationsReasonCode.MATERIAL_CHANGE,
                "the study configuration digest changed since the recorded evidence",
                "study_id",
            )
        )

    codes = {reason.code for reason in reasons}
    if codes & EXPIRED_REASONS:
        decision = ReleaseDecision.EXPIRED
    elif codes & HARD_REASONS:
        decision = ReleaseDecision.NO_GO
    elif evidence.known_limitations:
        reasons.append(
            _reason(
                OperationsReasonCode.LIMITATIONS_PRESENT,
                "release is approved with recorded limitations: "
                + "; ".join(evidence.known_limitations),
                "known_limitations",
            )
        )
        decision = ReleaseDecision.GO_WITH_LIMITS
    else:
        decision = ReleaseDecision.GO

    return ReleaseEvaluationResult(
        decision=decision,
        reasons=reasons,
        config_digest=evidence.config_digest,
        evaluated_at=timestamp,
        evidence=evidence,
    )


def record_decision(
    evidence: ReleaseEvidenceV1,
    decision: ReleaseDecision,
    *,
    recorded_at: Optional[datetime] = None,
) -> ReleaseEvidenceV1:
    """Return an immutable copy of ``evidence`` with the gate decision recorded."""
    return evidence.model_copy(
        update={"decision": decision, "recorded_at": recorded_at or _now(None)}
    )
