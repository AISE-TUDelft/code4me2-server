"""Pydantic v2 contracts for the canonical telemetry envelope (``CanonicalEventV1``).

Design rules encoded here:

* An event is immutable after construction (``frozen=True``).
* ``usage_tokens`` is nullable and is paired with an explicit
  :class:`Coverage` ``usage_capability``: ``0`` means an observed zero and
  ``None`` means "not exposed". The two are never conflated.
* Unknown enum/input values are preserved on ``unknown_*`` fields and flagged
  with a ``NEEDS_REVIEW`` coverage state rather than reclassified.
* ``SECRET`` material is never persistable; the privacy engine removes it
  before serialization. The model itself carries only sanitized payload.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import CanonicalFidelity, CoverageState

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class Provenance(BaseModel):
    """Where a canonical event came from and how faithfully it was derived."""

    model_config = _FROZEN

    source: str
    source_event_id: Optional[str] = None
    normalizer_version: str
    adapter_version: Optional[str] = None
    fidelity: CanonicalFidelity = CanonicalFidelity.NORMALIZED
    evidence_digest: Optional[str] = None


class PrivacySummary(BaseModel):
    """What the privacy filter did to one event."""

    model_config = _FROZEN

    policy_digest: Optional[str] = None
    actions: dict[str, int] = Field(default_factory=dict)
    redacted_fields: list[str] = Field(default_factory=list)
    blocked: bool = False
    block_reason: Optional[str] = None
    field_classes: dict[str, int] = Field(default_factory=dict)


class Coverage(BaseModel):
    """Coverage of one measured/observed field or capability."""

    model_config = _FROZEN

    state: CoverageState = CoverageState.UNKNOWN
    reason: Optional[str] = None
    capability: Optional[str] = None


class EventMetrics(BaseModel):
    """Numeric measurements attached to an event.

    ``usage_tokens`` is ``Optional`` and paired with ``usage_capability``:
    an observed zero is ``0`` with ``AVAILABLE`` coverage, while an unexposed
    count is ``None`` with ``UNAVAILABLE`` coverage.
    """

    model_config = _FROZEN

    usage_tokens: Optional[int] = None
    usage_capability: Coverage = Field(default_factory=Coverage)
    latency_ms: Optional[int] = None
    counts: dict[str, int] = Field(default_factory=dict)


class Correlations(BaseModel):
    """Opaque correlation handles that tie related events together."""

    model_config = _FROZEN

    turn_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    permission_id: Optional[str] = None
    edit_id: Optional[str] = None
    correlation_id: Optional[str] = None


class CanonicalEventV1(BaseModel):
    """One immutable, privacy-filtered canonical telemetry event."""

    model_config = _FROZEN

    event_id: UUID
    # Required: the envelope version is never silently defaulted on ingestion.
    # A missing version is a typed schema error, not an implicit "1".
    schema_version: str
    # Canonical dotted event type, or "unknown_source_event" when the source
    # construct is not documented. The original value is kept in
    # ``unknown_event_type``.
    event_type: str
    source: str

    study_id: Optional[UUID] = None
    revision_id: Optional[UUID] = None
    enrollment_id: Optional[UUID] = None
    research_session_id: Optional[UUID] = None
    agent_run_id: Optional[str] = None

    occurred_at: datetime
    # Per-emitter monotonic clock reading, if the emitter provided one. Global
    # ordering is a query projection, never a stored fact.
    monotonic_ns: Optional[int] = None

    emitter_id: str
    emitter_sequence: int

    correlations: Correlations = Field(default_factory=Correlations)
    lifecycle_state: Optional[str] = None

    # Sanitized, privacy-filtered mapping. Unknown fields are prefixed
    # ``unknown_`` and never reclassified into a known concept.
    payload: dict[str, Any] = Field(default_factory=dict)

    metrics: EventMetrics = Field(default_factory=EventMetrics)
    privacy: PrivacySummary = Field(default_factory=PrivacySummary)
    provenance: Provenance
    coverage: Coverage = Field(default_factory=Coverage)

    # Preserved, un-reclassified inputs.
    unknown_event_type: Optional[str] = None
    unknown_source: Optional[str] = None
    unknown_lifecycle_state: Optional[str] = None

    @property
    def needs_review(self) -> bool:
        """Whether any part of this event was not recognized and needs review."""
        return (
            self.coverage.state == CoverageState.NEEDS_REVIEW
            or self.unknown_event_type is not None
            or self.unknown_source is not None
            or self.unknown_lifecycle_state is not None
        )
