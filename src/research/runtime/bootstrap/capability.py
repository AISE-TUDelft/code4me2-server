"""Scoped, short-lived session capabilities.

A capability is signed with HMAC-SHA256 over its canonical mapping (excluding
its own signature) and is evaluated server-side for expiry, audience, scope and
revocation epoch. It never carries a provider or account secret.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from research.canonical import canonical_hash

from .models import (
    CapabilityReasonCode,
    CapabilityVerification,
    SessionCapability,
)

__all__ = [
    "INFERENCE_AUDIENCE",
    "INFERENCE_SCOPE",
    "capability_payload",
    "compute_capability_signature",
    "issue_capability",
    "issue_inference_capability",
    "verify_capability",
]

DEFAULT_AUDIENCE = "research-runtime"
DEFAULT_SCOPE = ("telemetry:write", "session:heartbeat", "session:close")

#: The inference capability a Goose study arm presents to the research
#: inference gateway (``/api/research/inference/v1/chat/completions``). Same
#: signing, binding and revocation rules as the session capability; a distinct
#: audience/scope so neither capability can be replayed as the other.
INFERENCE_AUDIENCE = "inference"
INFERENCE_SCOPE = ("inference:relay",)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def capability_payload(capability: SessionCapability) -> dict:
    """Return the canonical mapping covered by the capability signature."""
    data = capability.model_dump(mode="json")
    data.pop("signature", None)
    return data


def _signature_for(capability: SessionCapability, secret: str) -> str:
    digest = canonical_hash(capability_payload(capability))
    return hmac.new(
        secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def compute_capability_signature(capability: SessionCapability, secret: str) -> str:
    """Return the expected HMAC signature for a capability."""
    return _signature_for(capability, secret)


def issue_capability(
    audience: str,
    scope: list[str],
    ttl_seconds: int,
    revocation_epoch: int,
    secret: str,
    now: Optional[datetime] = None,
    *,
    enrollment_id: uuid.UUID,
    research_session_id: uuid.UUID,
    study_id: uuid.UUID,
) -> SessionCapability:
    """Issue a signed, short-lived capability bound to a subject.

    ``enrollment_id``, ``research_session_id`` and ``study_id`` are required
    and covered by the HMAC, so the capability is only usable for the exact
    enrollment/session/revision it was issued for. There is no default secret:
    issuing without a configured signing secret is refused.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if not secret or not secret.strip():
        raise ValueError("a non-empty signing secret is required to issue a capability")

    issued_at = _now(now)
    capability = SessionCapability(
        capability_id=uuid.uuid4(),
        audience=audience,
        scope=sorted(set(scope)),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
        revocation_epoch=revocation_epoch,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        study_id=study_id,
        signature="",
    )
    return capability.model_copy(update={"signature": _signature_for(capability, secret)})


def issue_inference_capability(
    *,
    secret: str,
    ttl_seconds: int,
    revocation_epoch: int,
    enrollment_id: uuid.UUID,
    research_session_id: uuid.UUID,
    study_id: uuid.UUID,
    now: Optional[datetime] = None,
) -> SessionCapability:
    """Issue the gateway bearer capability (audience ``inference``)."""
    return issue_capability(
        INFERENCE_AUDIENCE,
        list(INFERENCE_SCOPE),
        ttl_seconds,
        revocation_epoch,
        secret,
        now,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        study_id=study_id,
    )


def verify_capability(
    capability: SessionCapability,
    secret: str,
    expected_audience: str,
    expected_scope: list[str],
    now: Optional[datetime] = None,
    current_revocation_epoch: Optional[int] = None,
    *,
    expected_enrollment_id: Optional[uuid.UUID] = None,
    expected_research_session_id: Optional[uuid.UUID] = None,
    expected_study_id: Optional[uuid.UUID] = None,
) -> CapabilityVerification:
    """Verify a capability server-side, returning a typed reason.

    The subject arguments are optional so a stateless signature/expiry check can
    still be performed without a resolved resource, but every authorization use
    passes them so a valid capability for another subject can never be replayed.
    """
    if not secret or not secret.strip():
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.SIGNING_SECRET_MISSING,
            message="no signing secret is configured; refusing to verify capabilities",
        )

    if not capability.signature or not hmac.compare_digest(
        capability.signature, _signature_for(capability, secret)
    ):
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.SIGNATURE_MISMATCH,
            message="capability signature does not match",
        )

    if (
        expected_enrollment_id is not None
        and capability.enrollment_id != expected_enrollment_id
    ):
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.SUBJECT_MISMATCH,
            message="capability belongs to a different enrollment",
        )

    if (
        expected_research_session_id is not None
        and capability.research_session_id != expected_research_session_id
    ):
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.SESSION_MISMATCH,
            message="capability belongs to a different research session",
        )

    if expected_study_id is not None and capability.study_id != expected_study_id:
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.STUDY_MISMATCH,
            message="capability is bound to a different study",
        )

    if capability.audience != expected_audience:
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.WRONG_AUDIENCE,
            message=(
                f"capability audience {capability.audience!r} does not match "
                f"{expected_audience!r}"
            ),
        )

    missing = sorted(set(expected_scope) - set(capability.scope))
    if missing:
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.SCOPE_MISSING,
            message=f"capability is missing required scope {missing}",
        )

    timestamp = _now(now)
    if timestamp < capability.issued_at:
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.NOT_YET_VALID,
            message="capability is not valid yet",
        )
    if timestamp >= capability.expires_at:
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.EXPIRED,
            message="capability has expired",
        )

    if (
        current_revocation_epoch is not None
        and capability.revocation_epoch != current_revocation_epoch
    ):
        return CapabilityVerification(
            ok=False,
            reason=CapabilityReasonCode.REVOKED,
            message="capability revocation epoch is stale",
        )

    return CapabilityVerification(ok=True, reason=CapabilityReasonCode.OK)
