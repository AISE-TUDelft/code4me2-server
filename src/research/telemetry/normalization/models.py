"""Shared normalization contracts.

A normalizer emits zero or more :class:`CanonicalCandidateV1` objects wrapped in
a :class:`NormalizationResultV1`. Candidates are pre-build: the
:class:`~research.telemetry.builder.EventBuilder` allocates the immutable event
id and per-emitter sequence, so normalizers stay pure and deterministic.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..enums import CanonicalEventType, CanonicalFidelity, EventSource
from ..models import Correlations, Coverage, EventMetrics

_BASE_CONFIG = ConfigDict(extra="forbid")


class CanonicalCandidateV1(BaseModel):
    """A pre-build canonical candidate produced by a normalizer."""

    model_config = _BASE_CONFIG

    event_type: CanonicalEventType
    fidelity: CanonicalFidelity = CanonicalFidelity.NORMALIZED
    mapping_rule_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    correlations: Correlations = Field(default_factory=Correlations)
    lifecycle_state: Optional[str] = None
    coverage: Coverage = Field(default_factory=Coverage)
    metrics: EventMetrics = Field(default_factory=EventMetrics)
    adapter_version: Optional[str] = None


class NormalizationResultV1(BaseModel):
    """The result of normalizing one source observation."""

    model_config = _BASE_CONFIG

    source: EventSource
    normalizer_version: str
    source_event_id: Optional[str] = None
    adapter_version: Optional[str] = None
    candidates: list[CanonicalCandidateV1] = Field(default_factory=list)
    unmapped_reason: Optional[str] = None
