"""Canonical event construction and per-emitter sequencing.

The builder owns two things the source mappers must not invent:

* a fresh, immutable ``event_id``;
* a strictly-increasing ``emitter_sequence`` **per emitter** (independent
  emitters each start their own sequence; there is no fake global counter).

It never derives cross-process latency: ``latency_ms`` is only copied from an
explicit caller-provided metric. It also does not redact; callers must pass the
result through :func:`research.telemetry.privacy.engine.filter_event` before the
event reaches a spool, log, retry queue, or export.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Mapping, Optional

from .enums import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalEventType,
    CanonicalFidelity,
    CoverageState,
    EventSource,
    LifecycleState,
)
from .models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    PrivacySummary,
    Provenance,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


class SequenceAllocator:
    """Allocates strictly-increasing sequences independently per emitter."""

    def __init__(self, start: int = 1) -> None:
        self._start = start
        self._last: dict[str, int] = {}

    def next(self, emitter_id: str) -> int:
        """Return the next sequence for ``emitter_id`` (starting at ``start``)."""
        value = self._last.get(emitter_id, self._start - 1) + 1
        self._last[emitter_id] = value
        return value

    def current(self, emitter_id: str) -> int:
        """Return the last allocated sequence for ``emitter_id`` (0 if none)."""
        return self._last.get(emitter_id, self._start - 1)


def canonical_event_type(raw: Any) -> tuple[str, Optional[str]]:
    """Return ``(canonical_type, unknown_original)`` for a raw event type."""
    if isinstance(raw, CanonicalEventType):
        return raw.value, None
    text = str(raw)
    try:
        return CanonicalEventType(text).value, None
    except ValueError:
        return CanonicalEventType.UNKNOWN_SOURCE_EVENT.value, text


def canonical_source(raw: Any) -> tuple[str, Optional[str]]:
    """Return ``(canonical_source, unknown_original)`` for a raw source."""
    if isinstance(raw, EventSource):
        return raw.value, None
    text = str(raw)
    try:
        return EventSource(text.strip().lower()).value, None
    except ValueError:
        return text, text


def canonical_lifecycle(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """Return ``(canonical_lifecycle, unknown_original)`` for a raw state."""
    if raw is None:
        return None, None
    if isinstance(raw, LifecycleState):
        return raw.value, None
    text = str(raw)
    try:
        return LifecycleState(text.strip().lower()).value, None
    except ValueError:
        return text, text


class EventBuilder:
    """Builds immutable :class:`CanonicalEventV1` envelopes."""

    def __init__(self, allocator: Optional[SequenceAllocator] = None) -> None:
        self.allocator = allocator or SequenceAllocator()

    def build(
        self,
        *,
        emitter_id: str,
        event_type: Any,
        source: Any,
        occurred_at: datetime,
        normalizer_version: str,
        payload: Optional[Mapping[str, Any]] = None,
        fidelity: CanonicalFidelity = CanonicalFidelity.EXACT,
        adapter_version: Optional[str] = None,
        source_event_id: Optional[str] = None,
        evidence_digest: Optional[str] = None,
        metrics: Optional[EventMetrics] = None,
        coverage: Optional[Coverage] = None,
        privacy: Optional[PrivacySummary] = None,
        lifecycle_state: Any = None,
        correlations: Optional[Correlations] = None,
        monotonic_ns: Optional[int] = None,
        study_id: Optional[UUID] = None,
        revision_id: Optional[UUID] = None,
        enrollment_id: Optional[UUID] = None,
        research_session_id: Optional[UUID] = None,
        agent_run_id: Optional[str] = None,
        event_id: Optional[UUID] = None,
        emitter_sequence: Optional[int] = None,
    ) -> CanonicalEventV1:
        """Build one canonical event with a fresh id and allocated sequence."""
        canonical_type, unknown_event_type = canonical_event_type(event_type)
        canonical_source_value, unknown_source = canonical_source(source)
        lifecycle, unknown_lifecycle = canonical_lifecycle(lifecycle_state)

        resolved_coverage = coverage or Coverage()
        if unknown_event_type or unknown_source or unknown_lifecycle:
            resolved_coverage = Coverage(
                state=CoverageState.NEEDS_REVIEW,
                reason="unrecognized input preserved for review",
                capability=resolved_coverage.capability,
            )

        return CanonicalEventV1(
            event_id=event_id or uuid.uuid4(),
            schema_version=CANONICAL_SCHEMA_VERSION,
            event_type=canonical_type,
            source=canonical_source_value,
            study_id=study_id,
            revision_id=revision_id,
            enrollment_id=enrollment_id,
            research_session_id=research_session_id,
            agent_run_id=agent_run_id,
            occurred_at=occurred_at,
            monotonic_ns=monotonic_ns,
            emitter_id=emitter_id,
            emitter_sequence=(
                emitter_sequence
                if emitter_sequence is not None
                else self.allocator.next(emitter_id)
            ),
            correlations=correlations or Correlations(),
            lifecycle_state=lifecycle,
            payload=dict(payload or {}),
            metrics=metrics or EventMetrics(),
            privacy=privacy or PrivacySummary(),
            provenance=Provenance(
                source=canonical_source_value,
                source_event_id=source_event_id,
                normalizer_version=normalizer_version,
                adapter_version=adapter_version,
                fidelity=fidelity,
                evidence_digest=evidence_digest,
            ),
            coverage=resolved_coverage,
            unknown_event_type=unknown_event_type,
            unknown_source=unknown_source,
            unknown_lifecycle_state=unknown_lifecycle,
        )


_DEFAULT_ALLOCATOR = SequenceAllocator()


def build_event(**kwargs: Any) -> CanonicalEventV1:
    """Build an event using the process-wide default sequence allocator."""
    return EventBuilder(_DEFAULT_ALLOCATOR).build(**kwargs)
