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
``None`` so the caller performs its legacy write. Nothing is fabricated: a fact
with no exact canonical equivalent is preserved as an explicit unknown (with its
original kind in ``payload.legacy_kind`` and provenance), never reclassified, and
a missing measurement is never turned into zero.

For a research-bound task there is **no silent legacy fallback** (ISSUE-07): the
adapter always returns a result, and a canonical write that fails retryably or
is refused is reported to the caller so the producer keeps the batch instead of
recording it in the legacy table.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

from research.participants import identity as identity_store
from research.runtime.sessions import store as session_store
from database import crud
from research.telemetry.builder import EventBuilder
from research.telemetry.content_policy import resolve_study_content_policy
from research.telemetry.enums import CanonicalEventType, EventSource
from research.telemetry.ingestion.models import (
    IngestionContext,
    TelemetryBatchAckV1,
)
from research.telemetry.ingestion.service import ingest_events_for_context
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import Correlations, Coverage, EventMetrics
from research.telemetry.privacy import PrivacyPolicy

__all__ = [
    "CanonicalIngestionFailed",
    "CanonicalRecordResult",
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


class CanonicalIngestionFailed(Exception):
    """A research-bound canonical write did not commit.

    Raised so a route can map the failure to a truthful HTTP result instead of
    reporting success or silently writing the legacy table (ISSUE-07).
    """

    def __init__(
        self,
        *,
        reason: str,
        message: str = "",
        retryable: bool = True,
        ack: Optional[TelemetryBatchAckV1] = None,
    ) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason
        self.retryable = retryable
        self.ack = ack


@dataclass(frozen=True)
class CanonicalRecordResult:
    """Outcome of one internal canonical write (ISSUE-07).

    ``written`` is True only after the ingestion writer committed. A research
    bound caller must treat any non-written result as a failure: ``retryable``
    marks a transient failure (keep and re-send the batch), otherwise the facts
    were terminally refused.
    """

    written: bool
    retryable: bool = False
    reason: str = "OK"
    message: str = ""
    ack: Optional[TelemetryBatchAckV1] = None

    @property
    def ingested_event_ids(self) -> tuple[uuid.UUID, ...]:
        if self.ack is None:
            return ()
        return tuple(
            entry.event_id
            for entry in (self.ack.accepted + self.ack.duplicate)
            if entry.event_id is not None
        )


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
    """Whether a task carries the explicit research binding.

    Either canonical attribution id is sufficient: ``enrollment_id`` alone (a
    binding without an unambiguous session) must still use the canonical
    authority and must never fall back to the legacy table. This is pinned by
    ``test_agent_event_boundary.py``.
    """
    return (
        getattr(task, "research_session_id", None) is not None
        or getattr(task, "enrollment_id", None) is not None
    )


def build_legacy_events(
    task: Any, facts: Sequence[LegacyFact], *, first_sequence: Optional[int] = None
) -> list:
    """Build canonical events for a research-bound task, in order.

    ``(research_session_id, emitter_id, emitter_sequence)`` is a unique key in
    the store, so sequences must never be reused within a session+emitter.
    Callers that persist must reserve ``len(facts)`` indexes up front (see
    ``record_legacy_facts``) and pass the reserved base as ``first_sequence``;
    deriving from ``task.next_event_index`` without reserving replays the same
    sequences on every call and every later batch is rejected as an integrity
    conflict. When ``first_sequence`` is omitted the legacy counter-derived
    numbering is kept for backward compatibility (tests, dry runs).

    The relay adapts spans from many tasks in one session, but the store's
    uniqueness key is ``(session, emitter, sequence)``: sharing one emitter
    across tasks would collide independent streams. Each task's facts are
    therefore their own emitter namespace (``"<emitter>:<task_id>"``), matching
    the documented model that independent emitters each own their sequence.
    """
    builder = EventBuilder()
    task_namespace = str(getattr(task, "task_id", "unbound"))
    if first_sequence is None:
        sequence = int(getattr(task, "next_event_index", 0) or 0)
    else:
        # The reserved base is the 0-based legacy event index; canonical
        # emitter sequences are 1-based continuations of the same counter,
        # matching the historical counter-derived numbering (first event of a
        # fresh task is sequence 1, never 0 which validation rejects).
        sequence = int(first_sequence)
    events = []
    for fact in facts:
        sequence += 1
        event_type = _KIND_TO_TYPE.get(
            fact.kind, CanonicalEventType.UNKNOWN_SOURCE_EVENT
        )
        events.append(
            builder.build(
                emitter_id=f"{fact.emitter_id}:{task_namespace}",
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


def _kill_switch_check(db: Any, task: Any):
    """Scope the operator kill switch to the task's study/enrollment."""
    from research.analysis.operations import store as operations_store

    return operations_store.db_kill_switch_check(
        db,
        study_id=getattr(task, "study_id", None),
        enrollment_id=getattr(task, "enrollment_id", None),
    )


def record_legacy_facts(
    db: Any, *, task: Any, facts: Sequence[LegacyFact]
) -> Optional[CanonicalRecordResult]:
    """Persist legacy facts through the canonical ingestion writer.

    Returns ``None`` when the task has no research binding, so the caller keeps
    its legacy operational write. For a research-bound task it always returns a
    result: the caller must not fall back to ``agent_event`` even when the
    canonical write failed (ISSUE-07). Never raises.
    """
    if task is None or not facts or not research_bound(task):
        return None
    try:
        enrollment_row = (
            identity_store.get_enrollment(db, task.enrollment_id, for_update=True)
            if getattr(task, "enrollment_id", None) is not None
            else None
        )
        session_row = (
            session_store.get_session(db, task.research_session_id, for_update=True)
            if getattr(task, "research_session_id", None) is not None
            else None
        )
        if enrollment_row is None or session_row is None:
            return CanonicalRecordResult(
                written=False,
                retryable=True,
                reason="RESEARCH_CONTEXT_UNAVAILABLE",
                message=(
                    "the task's enrollment/session rows are unavailable; "
                    "the batch must be retried, never written to the legacy table"
                ),
            )
        enrollment = identity_store.row_to_enrollment(enrollment_row)
        session = session_store.row_to_session(session_row)

        # The study policy is the single authority for content storage on this
        # boundary too (ISSUE-01/ISSUE-07). Missing/malformed policy denies
        # content; the ingestion writer rejects the offending facts instead of
        # storing them.
        decision = resolve_study_content_policy(
            db,
            account_id=getattr(task, "owner_user_id", None),
            study_id=task.study_id,
            enrollment_id=task.enrollment_id,
        )
        policy = decision.policy or PrivacyPolicy.default()

        context = IngestionContext(
            study_id=task.study_id,
            enrollment_id=task.enrollment_id,
            research_session_id=task.research_session_id,
            revocation_epoch=enrollment.revocation_epoch,
        )
        # Reserve the emitter sequences atomically before building: without
        # this, every call replays sequences from the (never advanced) task
        # counter and every batch after the first is rejected as an integrity
        # conflict. Burned indexes on failed writes surface as ordinary gaps.
        first_sequence = crud.reserve_agent_event_indexes(
            db, getattr(task, "task_id", None), len(facts)
        )
        events = build_legacy_events(task, facts, first_sequence=first_sequence)
        ack = ingest_events_for_context(
            context=context,
            enrollment=enrollment,
            session=session,
            events=events,
            store=SqlAlchemyIngestionStore(db),
            kill_switch_check=_kill_switch_check(db, task),
            privacy_policy=policy,
        )
        if ack.retryable:
            return CanonicalRecordResult(
                written=False,
                retryable=True,
                reason="RETRYABLE",
                message="canonical ingestion returned retryable events",
                ack=ack,
            )
        if ack.rejected and not (ack.accepted or ack.duplicate):
            return CanonicalRecordResult(
                written=False,
                retryable=False,
                reason="REJECTED",
                message="canonical ingestion rejected the batch",
                ack=ack,
            )
        return CanonicalRecordResult(written=True, reason="OK", ack=ack)
    except Exception as error:  # noqa: BLE001 - research-bound writes must not raise
        logging.warning(
            "[relay-adapter] canonical write failed for task %s — %s",
            getattr(task, "task_id", None),
            error,
        )
        return CanonicalRecordResult(
            written=False,
            retryable=True,
            reason="STORE_UNAVAILABLE",
            message="canonical ingestion store is unavailable; retry the batch",
        )
