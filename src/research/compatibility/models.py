"""Pydantic v2 contracts for the ACP capability receipt and evaluation API.

These models are the wire + persistence contract for Issue 01. They are
deliberately permissive about *missing* optional evidence (so an incomplete
receipt can be represented and then explicitly rejected by
:func:`research.compatibility.receipt.validate_receipt`) but strict about
enum-typed values.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    CapabilityId,
    CapabilityState,
    CompatibilityDecision,
    CompatibilityReasonCode,
    EnforcementOwner,
    Fidelity,
)


class EnvironmentTuple(BaseModel):
    """Version-pinned host environment a receipt was captured against.

    Fields are optional so the same type can express a *partial* expectation in
    :class:`CompatibilityRequest` (only present fields are compared). On a
    receipt, :func:`research.compatibility.receipt.validate_receipt` requires
    the core fields to be present.
    """

    model_config = ConfigDict(extra="ignore")

    ide_build: Optional[str] = None
    ai_assistant_build: Optional[str] = None
    plugin_version: Optional[str] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    host_kind: Optional[str] = None


class AgentIdentity(BaseModel):
    """Agent + adapter identity a receipt was captured against.

    ``agent_id`` / ``agent_version`` are required on a receipt (enforced by
    validation); they are optional here so a partial expected identity can be
    supplied as an override.
    """

    model_config = ConfigDict(extra="ignore")

    agent_id: Optional[str] = None
    agent_version: Optional[str] = None
    adapter_version: Optional[str] = None
    release_digest: Optional[str] = None
    executable_digest: Optional[str] = None


class EvidenceRef(BaseModel):
    """A content-addressed pointer to redacted evidence.

    The raw payload is never stored here; only its digest and a stable
    reference are. ``redacted`` records that the pointed-to artifact has been
    through :func:`research.compatibility.redaction.redact`.
    """

    model_config = ConfigDict(extra="ignore")

    ref: str
    kind: str
    sha256: str
    size: Optional[int] = None
    redacted: bool = True


class CapabilityResult(BaseModel):
    """Observed result for one capability in one environment."""

    model_config = ConfigDict(extra="ignore")

    capability: CapabilityId
    state: CapabilityState
    fidelity: Fidelity = Fidelity.NORMALIZED
    enforcement_owner: EnforcementOwner = EnforcementOwner.UNKNOWN
    limitations: list[str] = Field(default_factory=list)
    observed_at: Optional[datetime] = None
    evidence_refs: list[str] = Field(default_factory=list)


class AcpCapabilityReceiptV1(BaseModel):
    """Immutable, redacted, hashed capability receipt (schema version 1)."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1"
    receipt_id: UUID
    captured_at: datetime
    environment: EnvironmentTuple
    agent: AgentIdentity
    protocol_version: str
    declared: dict[str, str | bool] = Field(default_factory=dict)
    observed_operations: list[CapabilityResult] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    status: CapabilityState
    # Content hash over the canonical serialization of this model with
    # ``content_hash`` excluded. Empty until finalised by the builder.
    content_hash: str = ""


class RequiredCapability(BaseModel):
    """One capability predicate a caller requires of a receipt."""

    model_config = ConfigDict(extra="ignore")

    capability: CapabilityId
    require_state: CapabilityState = CapabilityState.SUPPORTED
    enforcement_owner: Optional[EnforcementOwner] = None


class CompatibilityRequest(BaseModel):
    """Evaluate ``requirements`` against a receipt (inline or by id)."""

    model_config = ConfigDict(extra="ignore")

    receipt: Optional[AcpCapabilityReceiptV1] = None
    receipt_id: Optional[UUID] = None
    requirements: list[RequiredCapability] = Field(default_factory=list)
    expected_environment: Optional[EnvironmentTuple] = None
    expected_agent: Optional[AgentIdentity] = None
    # Negotiated ACP wire protocol the caller requires.
    expected_protocol_version: Optional[str] = None


class ReasonDetail(BaseModel):
    """Typed explanation for one part of a compatibility decision."""

    model_config = ConfigDict(extra="ignore")

    code: CompatibilityReasonCode
    capability: Optional[CapabilityId] = None
    message: str


class CapabilityEvaluation(BaseModel):
    """Per-requirement outcome, independent of the aggregate decision."""

    model_config = ConfigDict(extra="ignore")

    capability: CapabilityId
    required_state: CapabilityState
    observed_state: Optional[CapabilityState] = None
    enforcement_owner: Optional[EnforcementOwner] = None
    satisfied: bool
    reason: Optional[ReasonDetail] = None


class CompatibilityResult(BaseModel):
    """Aggregate compatibility decision and its full justification."""

    model_config = ConfigDict(extra="ignore")

    decision: CompatibilityDecision
    reasons: list[ReasonDetail] = Field(default_factory=list)
    per_capability: list[CapabilityEvaluation] = Field(default_factory=list)
