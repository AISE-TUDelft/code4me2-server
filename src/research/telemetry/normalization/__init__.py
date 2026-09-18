"""Source normalizers that map observations into canonical candidates.

* :mod:`research.telemetry.normalization.generic_acp` - the built-in ACP
  normalizer (plus a sanitized ``unknown_source_event`` fallback and the optional
  :class:`~research.telemetry.normalization.generic_acp.AgentAdapter` enrichment
  interface).

Normalizers are pure: :func:`materialize_candidate` attaches the event id and
per-emitter sequence through the :class:`~research.telemetry.builder.EventBuilder`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .generic_acp import (
    GENERIC_ACP_NORMALIZER_VERSION,
    AgentAdapter,
    GenericAcpNormalizer,
    enrich_with_adapter,
)
from .models import CanonicalCandidateV1, NormalizationResultV1

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from ..builder import EventBuilder
    from ..models import CanonicalEventV1

__all__ = [
    "GENERIC_ACP_NORMALIZER_VERSION",
    "AgentAdapter",
    "CanonicalCandidateV1",
    "GenericAcpNormalizer",
    "NormalizationResultV1",
    "enrich_with_adapter",
    "materialize_candidate",
]


def materialize_candidate(
    builder: EventBuilder,
    candidate: CanonicalCandidateV1,
    result: NormalizationResultV1,
    *,
    emitter_id: str,
    occurred_at: datetime,
    study_id: Optional[UUID] = None,
    revision_id: Optional[UUID] = None,
    enrollment_id: Optional[UUID] = None,
    research_session_id: Optional[UUID] = None,
    agent_run_id: Optional[str] = None,
    monotonic_ns: Optional[int] = None,
    event_id: Optional[UUID] = None,
    emitter_sequence: Optional[int] = None,
) -> CanonicalEventV1:
    """Build an immutable canonical event from a candidate and its result."""
    return builder.build(
        emitter_id=emitter_id,
        event_type=candidate.event_type,
        source=result.source,
        occurred_at=occurred_at,
        normalizer_version=result.normalizer_version,
        payload=candidate.payload,
        fidelity=candidate.fidelity,
        adapter_version=candidate.adapter_version or result.adapter_version,
        source_event_id=result.source_event_id,
        metrics=candidate.metrics,
        coverage=candidate.coverage,
        lifecycle_state=candidate.lifecycle_state,
        correlations=candidate.correlations,
        monotonic_ns=monotonic_ns,
        study_id=study_id,
        revision_id=revision_id,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        agent_run_id=agent_run_id,
        event_id=event_id,
        emitter_sequence=emitter_sequence,
    )
