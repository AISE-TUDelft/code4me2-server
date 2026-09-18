"""Pydantic v2 contracts for the bootstrap manifest and session capability.

The manifest is a signed, short-lived, secret-free projection. It contains only
the pinned revision reference, the sticky assignment, the pinned agent release,
the policy set, an optional compatibility receipt reference, and a scoped
session capability. It never contains account identity, provider credentials,
raw consent documents, or arbitrary launch commands; ``null`` optional policy
values mean inherited/disabled, and unavailable capabilities carry a typed
state and reason.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from enum import Enum
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from research.telemetry.enums import CoverageState

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BASE = ConfigDict(extra="forbid")


class BootstrapOutcome(str, Enum):
    """Terminal result of composing a bootstrap manifest."""

    ISSUED = "ISSUED"
    BLOCKED = "BLOCKED"


class BootstrapReasonCode(str, Enum):
    """Stable machine-readable reason a bootstrap was blocked (or issued)."""

    OK = "OK"
    ENROLLMENT_NOT_ACTIVE = "ENROLLMENT_NOT_ACTIVE"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    REVOKED = "REVOKED"
    STUDY_NOT_OPEN = "STUDY_NOT_OPEN"
    STUDY_CLOSED = "STUDY_CLOSED"
    STUDY_MISMATCH = "STUDY_MISMATCH"
    ASSIGNMENT_MISMATCH = "ASSIGNMENT_MISMATCH"
    RELEASE_NOT_QUALIFIED = "RELEASE_NOT_QUALIFIED"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    ARTIFACT_UNAVAILABLE = "ARTIFACT_UNAVAILABLE"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    COMPATIBILITY_MISSING = "COMPATIBILITY_MISSING"
    INCOMPATIBLE_ENVIRONMENT = "INCOMPATIBLE_ENVIRONMENT"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    # No signing secret is configured, so no capability/manifest may be issued.
    SIGNING_SECRET_MISSING = "SIGNING_SECRET_MISSING"
    # Operator kill switch: no new session may be bootstrapped while engaged.
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


class ManifestReasonCode(str, Enum):
    """Stable machine-readable manifest verification reason."""

    OK = "OK"
    MISSING_SIGNATURE = "MISSING_SIGNATURE"
    MANIFEST_DIGEST_MISMATCH = "MANIFEST_DIGEST_MISMATCH"
    MANIFEST_SIGNATURE_MISMATCH = "MANIFEST_SIGNATURE_MISMATCH"


class CapabilityReasonCode(str, Enum):
    """Stable machine-readable capability verification reason."""

    OK = "OK"
    SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
    SIGNING_SECRET_MISSING = "SIGNING_SECRET_MISSING"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    SCOPE_MISSING = "SCOPE_MISSING"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    # The capability is cryptographically valid but belongs to a different
    # subject than the resource it is being presented for.
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    STUDY_MISMATCH = "STUDY_MISMATCH"


class ResearchSessionRef(BaseModel):
    """Opaque research session descriptor (Issue 07 owns its lifecycle)."""

    model_config = _BASE

    research_session_id: UUID
    opened_at: datetime


class SessionCapability(BaseModel):
    """Short-lived, scoped, signed session capability bound to its subject.

    The capability is only meaningful for one ``(study, enrollment, research
    session)`` tuple. It carries that subject explicitly and the signed
    payload covers it, so a capability issued for one participant cannot be
    replayed against another participant's enrollment, session, or revision.
    """

    model_config = _FROZEN

    capability_id: UUID
    audience: str
    scope: list[str] = Field(default_factory=list)
    issued_at: datetime
    expires_at: datetime
    revocation_epoch: int = 0
    # The subject the capability is bound to. All three are required so a
    # capability can never be issued without an explicit binding.
    enrollment_id: UUID
    research_session_id: UUID
    study_id: UUID
    signature: str = ""


class BootstrapAssignment(BaseModel):
    """The assignment projection embedded in a manifest."""

    model_config = _FROZEN

    assignment_id: UUID
    agent_profile_id: UUID
    strategy: str
    randomization_epoch: int = 0
    profile_digest: str


class BootstrapAgentRelease(BaseModel):
    """The pinned agent release projection embedded in a manifest.

    ``distribution_mode`` selects how the agent reaches the participant:
    ``PACKAGED`` (default) pins ``artifact_digest``; ``BYOA_EXTERNAL`` pins a
    participant-installed command/package identity and leaves
    ``artifact_digest`` empty.
    """

    model_config = _FROZEN

    agent_id: str
    release_id: str
    artifact_digest: str = ""
    adapter_version: Optional[str] = None
    distribution_mode: str = "PACKAGED"
    agent_command: Optional[str] = None
    agent_command_args: list[str] = Field(default_factory=list)
    agent_package: Optional[str] = None


class BootstrapAgentProfile(BaseModel):
    """The non-secret provider/model identity of a condition's agent profile.

    This is a projection of the server-side ``AgentProfile`` the condition
    references. It deliberately carries no credential and not even the
    ``api_key_ref`` env-var *name*: the runtime relays inference through the
    backend, which resolves and injects the key server-side, so the secret and
    its reference never leave the server.
    """

    model_config = _FROZEN

    profile_id: UUID
    name: str
    framework_version: str
    model: str
    base_url: Optional[str] = None
    temperature: Optional[float] = None


class BootstrapTelemetryPolicy(BaseModel):
    """Telemetry field-class allowance projected into a manifest."""

    model_config = _FROZEN

    allowed_field_classes: list[str] = Field(default_factory=list)
    content_capture: bool = False


class BootstrapPrivacyPolicy(BaseModel):
    """Retention policy projected into a manifest."""

    model_config = _FROZEN

    retention_action: str
    retention_days: Optional[int] = None


class BootstrapSessionPolicy(BaseModel):
    """Session timing policy projected into a manifest."""

    model_config = _FROZEN

    idle_timeout_seconds: Optional[int] = None
    resume_grace_seconds: Optional[int] = None
    heartbeat_seconds: Optional[int] = None


class BootstrapPolicies(BaseModel):
    """The policy set; ``null`` members mean inherited/disabled."""

    model_config = _FROZEN

    telemetry_policy: Optional[BootstrapTelemetryPolicy] = None
    privacy_policy: Optional[BootstrapPrivacyPolicy] = None
    session_policy: Optional[BootstrapSessionPolicy] = None
    consent_policy_digest: Optional[str] = None


class BootstrapCompatibility(BaseModel):
    """Compatibility projection with an explicit coverage state and reason."""

    model_config = _FROZEN

    receipt_ref: Optional[str] = None
    state: CoverageState = CoverageState.UNKNOWN
    reason: Optional[str] = None


class BootstrapManifestV1(BaseModel):
    """Signed, short-lived, secret-free launch contract (schema version 1)."""

    model_config = _FROZEN

    manifest_version: str = "1"
    manifest_digest: str = ""
    signature: str = ""
    generated_at: datetime
    study_id: UUID
    enrollment_id: UUID
    research_config_digest: Optional[str] = None
    research_session: ResearchSessionRef
    assignment: BootstrapAssignment
    agent_release: BootstrapAgentRelease
    # Optional non-secret provider/model identity of the condition's linked
    # agent profile. Absent (null) for protocols whose conditions predate the
    # profile link or that pin a release without a profile.
    agent_profile: Optional[BootstrapAgentProfile] = None
    policies: BootstrapPolicies
    compatibility_receipt_ref: Optional[str] = None
    compatibility: BootstrapCompatibility = Field(default_factory=BootstrapCompatibility)
    session_capability: SessionCapability


class ManifestSignature(BaseModel):
    """Content digest and HMAC signature for a manifest."""

    model_config = _BASE

    digest: str
    signature: str


class ManifestVerification(BaseModel):
    """Typed result of verifying a manifest's digest and signature."""

    model_config = _BASE

    ok: bool
    reason: ManifestReasonCode = ManifestReasonCode.OK
    expected_digest: str = ""
    actual_digest: str = ""
    message: Optional[str] = None


class CapabilityVerification(BaseModel):
    """Typed result of server-side capability verification."""

    model_config = _BASE

    ok: bool
    reason: CapabilityReasonCode = CapabilityReasonCode.OK
    message: Optional[str] = None


class BootstrapIssue(BaseModel):
    """One typed bootstrap block reason."""

    model_config = _BASE

    code: BootstrapReasonCode
    message: str
    field: str = ""


class BootstrapResult(BaseModel):
    """Typed outcome of composing a bootstrap manifest."""

    model_config = _BASE

    outcome: BootstrapOutcome
    reason: BootstrapReasonCode = BootstrapReasonCode.OK
    manifest: Optional[BootstrapManifestV1] = None
    issue: Optional[BootstrapIssue] = None
