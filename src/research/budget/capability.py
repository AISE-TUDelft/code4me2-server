"""The inference capability Goose presents as its bearer token.

It is the existing HMAC-signed :class:`SessionCapability` with the audience
``inference`` and the scope ``inference:relay``: minted at bootstrap, bound to
the enrollment/session/study, revoked through ``enrollment.revocation_epoch``
and verified server-side on every call. Goose reads ``OPENAI_API_KEY`` once
per process, so the token is the base64url encoding of the capability's
canonical JSON (signature included); the server decodes it and re-verifies
the signature, so the encoding only has to round-trip the JSON content.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import datetime
from typing import Optional

from research.canonical import canonical_bytes
from research.runtime.bootstrap.capability import (
    INFERENCE_AUDIENCE,
    INFERENCE_SCOPE,
    issue_inference_capability,
    verify_capability,
)
from research.runtime.bootstrap.models import CapabilityVerification, SessionCapability

__all__ = [
    "INFERENCE_AUDIENCE",
    "INFERENCE_SCOPE",
    "decode_capability_bearer",
    "encode_capability_bearer",
    "issue_inference_capability",
    "verify_inference_capability",
]

_BEARER_RE = re.compile(r"^[A-Za-z0-9_-]{16,8192}$")


def encode_capability_bearer(capability: SessionCapability) -> str:
    """base64url (no padding) of the capability's canonical JSON, signature included."""
    payload = capability.model_dump(mode="json")
    return base64.urlsafe_b64encode(canonical_bytes(payload)).decode("ascii").rstrip("=")


def decode_capability_bearer(token: Optional[str]) -> Optional[SessionCapability]:
    """Parse a bearer back into a capability; ``None`` for anything malformed."""
    if not token:
        return None
    text = token.strip()
    if text.lower().startswith("bearer "):
        text = text[len("bearer ") :].strip()
    if not _BEARER_RE.match(text):
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return SessionCapability.model_validate(payload)
    except Exception:  # noqa: BLE001 - any validation failure is "not a capability"
        return None


def verify_inference_capability(
    capability: SessionCapability,
    *,
    secret: str,
    current_revocation_epoch: int,
    expected_enrollment_id: uuid.UUID,
    expected_study_id: uuid.UUID,
    now: Optional[datetime] = None,
) -> CapabilityVerification:
    """Signature, audience, scope, expiry, epoch and subject checks.

    The research session is deliberately not required to be live: budgets are
    per enrollment, and an idle-ended session must not break a running chat.
    """
    return verify_capability(
        capability,
        secret,
        INFERENCE_AUDIENCE,
        list(INFERENCE_SCOPE),
        now,
        current_revocation_epoch,
        expected_enrollment_id=expected_enrollment_id,
        expected_study_id=expected_study_id,
    )
