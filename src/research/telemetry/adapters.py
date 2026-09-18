"""Server-authorized adapters feeding legacy producers into the canonical stream.

The inference relay, the self-reporting runtime and the OTel span endpoint
observe agent activity that used to be written to the legacy ``agent_event``
table. For a *research-bound* task (an explicit ``research_session_id`` /
``enrollment_id`` / ``study_id`` on ``agent_task``) those observations
are now built as canonical events and persisted through the one ingestion writer
(:func:`ingest_events_for_context`), so the dashboards read the canonical event
authority and there is no parallel canonical writer.

A task with no research binding keeps the legacy operational path: ``agent_event``
is retained for genuinely non-research operational data and this adapter returns
``False`` so the caller performs its legacy write. Nothing is fabricated: a fact
with no exact canonical equivalent is preserved as an explicit unknown (with its
original kind in ``payload.legacy_kind`` and provenance), never reclassified, and
a missing measurement is never turned into zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from research.participants import identity as identity_store
from research.runtime.sessions import store as session_store
from research.telemetry.builder import EventBuilder
from research.telemetry.enums import CanonicalEventType, EventSource
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import ingest_events_for_context
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import Correlations, Coverage, EventMetrics

__all__ = [
    "LegacyFact",
    "RELAY_NORMALIZER_VERSION",
    "record_legacy_facts",
    "research_bound",
]

RELAY_NORMALIZER_VERSION = "relay-adapter-v1"

#: Legacy producer kind -> canonical event type. A kind with no exact canonical
#: equivalent is preserved as ``unknown_source_event`` (the builder records the
#: original kind), never reclassified into a nearby concept.
_KIND_TO_TYPE: dict[str, CanonicalEventType] = {
    "model_call": CanonicalEventType.AGENT_MESSAGE_COMPLETED,
    "tool_call": CanonicalEventType.TOOL_COMPLETED,
    "tool_failed": CanonicalEventType.TOOL_FAILED,
    "observation": CanonicalEventType.UNKNOWN_SOURCE_EVENT,
}


@dataclass(frozen=True)
class LegacyFact:
    """One already-sanitized legacy observation, ready to canonicalize.

    ``payload`` must contain only structural metadata (no prompt/source/output/
    diff/reasoning content): content stays behind the legacy consent gate.
    """

    kind: str
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    metrics: EventMetrics = field(default_factory=EventMetrics)
    coverage: Coverage = field(default_factory=Coverage)
    correlations: Correlations = field(default_factory=Correlations)
    source_event_id: Optional[str] = None
    emitter_id: str = "relay"


def research_bound(task: Any) -> bool:
    """Whether a task carries the explicit phase-05 research binding."""
    return getattr(task, "research_session_id", None) is not None


def build_legacy_events(task: Any, facts: Sequence[LegacyFact]) -> list:
    """Build canonical events for a research-bound task, in order."""
    builder = EventBuilder()
    sequence = int(getattr(task, "next_event_index", 0) or 0)
    events = []
    for fact in facts:
        sequence += 1
        event_type = _KIND_TO_TYPE.get(
            fact.kind, CanonicalEventType.UNKNOWN_SOURCE_EVENT
        )
        events.append(
            builder.build(
                emitter_id=fact.emitter_id,
                event_type=event_type,
                source=EventSource.RELAY,
                occurred_at=fact.occurred_at,
                normalizer_version=RELAY_NORMALIZER_VERSION,
                payload={**fact.payload, "legacy_kind": fact.kind},
                metrics=fact.metrics,
                coverage=fact.coverage,
                correlations=fact.correlations,
                source_event_id=fact.source_event_id,
                study_id=getattr(task, "study_id", None),
                enrollment_id=getattr(task, "enrollment_id", None),
                research_session_id=getattr(task, "research_session_id", None),
                agent_run_id=getattr(task, "external_run_id", None),
                emitter_sequence=sequence,
            )
        )
    return events


def record_legacy_facts(db: Any, *, task: Any, facts: Sequence[LegacyFact]) -> bool:
    """Persist legacy facts through the canonical ingestion writer.

    Returns ``True`` when the facts were canonicalized (the caller must **not**
    also write ``agent_event``). Returns ``False`` when the task has no research
    binding — or its binding rows are unavailable — so the caller keeps the
    legacy operational write. Never raises: a canonical-write failure returns
    ``False`` so the caller's legacy path still records the observation.
    """
    if task is None or not facts or not research_bound(task):
        return False
    try:
        enrollment_row = (
            identity_store.get_enrollment(db, task.enrollment_id)
            if getattr(task, "enrollment_id", None) is not None
            else None
        )
        session_row = (
            session_store.get_session(db, task.research_session_id)
            if getattr(task, "research_session_id", None) is not None
            else None
        )
        if enrollment_row is None or session_row is None:
            return False
        enrollment = identity_store.row_to_enrollment(enrollment_row)
        session = session_store.row_to_session(session_row)
        context = IngestionContext(
            study_id=task.study_id,
            enrollment_id=task.enrollment_id,
            research_session_id=task.research_session_id,
            revocation_epoch=enrollment.revocation_epoch,
        )
        events = build_legacy_events(task, facts)
        ingest_events_for_context(
            context=context,
            enrollment=enrollment,
            session=session,
            events=events,
            store=SqlAlchemyIngestionStore(db),
        )
        return True
    except Exception:  # noqa: BLE001 - the caller keeps the legacy fallback
        return False
