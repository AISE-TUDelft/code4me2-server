"""Idempotent batch ingestion service (Issue 09).

Authorization happens inside the same transaction that persists the batch: the
scoped session capability is verified (signature, audience, scope, expiry,
revocation epoch), the enrollment and research session rows are locked
(``SELECT ... FOR UPDATE``) and validated, the events are classified, and the
accepted events plus the immutable batch receipt are committed together. The
ACK is returned only after commit, so a mid-batch database error rolls the whole
batch back and produces a retryable result — never a partial accept.

Per event, classification is deterministic given store state:

* same ``event_id`` + same digest already stored -> ``DUPLICATE`` (acknowledged,
  never a second fact);
* same ``event_id`` + different digest -> ``REJECTED`` integrity conflict, never
  overwritten;
* invalid/unsafe/out-of-scope/blocked -> ``REJECTED`` with a permanent typed
  reason;
* transient store failure -> ``RETRYABLE`` with a hint and no false ACK;
* otherwise -> ``ACCEPTED``.

The returned acknowledgement is immutable and retry-safe: re-POSTing the same
``batch_id`` returns the stored receipt unchanged, and a concurrent duplicate
loses the receipt race but returns the winner's receipt (never a second one).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Protocol, Sequence

from research.canonical import canonical_hash
from research.participants.enums import EnrollmentStatus
from research.runtime.bootstrap.models import CapabilityReasonCode
from research.telemetry.privacy import PrivacyPolicy, filter_event

from .enums import EventDisposition, IngestionReasonCode
from .errors import IntegrityConflictError, ReceiptConflictError, StoreUnavailable
from .models import (
    BatchReceipt,
    EventAck,
    IngestionContext,
    IngestionStore,
    ResearchEventRecord,
    TelemetryBatchAckV1,
    TelemetryBatchRequestV1,
)
from .validation import validate_batch_event

if TYPE_CHECKING:
    from research.participants.models import Enrollment
    from research.runtime.bootstrap.models import CapabilityVerification, SessionCapability
    from research.runtime.sessions.models import ResearchSessionV1
    from research.telemetry.models import CanonicalEventV1

__all__ = ["CapabilityVerifier", "compute_event_digest", "ingest_batch"]

DEFAULT_RETRY_HINT_SECONDS = 30
DEFAULT_MAX_EVENTS = 500


class CapabilityVerifier(Protocol):
    """Verifies a session capability against its resolved subject.

    The resolved ``(study, enrollment, session)`` are passed so the verifier
    can reject a cryptographically valid capability that was issued for a
    different subject. Verifiers may accept them as optional keyword arguments
    (the default ``None`` skips the subject comparison).
    """

    def __call__(
        self,
        capability: SessionCapability,
        *,
        now: datetime,
        current_revocation_epoch: Optional[int],
        expected_enrollment_id: Optional[uuid.UUID] = None,
        expected_research_session_id: Optional[uuid.UUID] = None,
        expected_study_id: Optional[uuid.UUID] = None,
    ) -> CapabilityVerification:  # pragma: no cover - structural protocol
        ...


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def compute_event_digest(event: CanonicalEventV1) -> str:
    """Content digest of a canonical event (stable, order-independent)."""
    return canonical_hash(event.model_dump(mode="json"))


def _record_from_event(
    event: CanonicalEventV1,
    context: IngestionContext,
    digest: str,
    accepted_at: datetime,
) -> ResearchEventRecord:
    """Build the stored record from the *authorized* context.

    ``study_id``/``enrollment_id``/``research_session_id`` are
    always taken from the authorized enrollment/session, never from the client
    event payload. A payload field that disagrees with the context is rejected
    by :func:`validate_batch_event` before this runs.
    """
    return ResearchEventRecord(
        event_id=event.event_id,
        schema_version=event.schema_version,
        event_type=event.event_type,
        source=event.source,
        study_id=context.study_id,
        enrollment_id=context.enrollment_id,
        research_session_id=context.research_session_id,
        agent_run_id=event.agent_run_id,
        emitter_id=event.emitter_id,
        emitter_sequence=event.emitter_sequence,
        occurred_at=event.occurred_at,
        # The complete validated envelope is the stored authority; the scalar
        # fields above are searchable projections derived from it.
        envelope=event.model_dump(mode="json"),
        digest=digest,
        accepted_at=accepted_at,
        retention_state="RETAINED",
    )


def _ack(
    batch_id: uuid.UUID,
    server_time: datetime,
    *,
    accepted: Sequence[EventAck] = (),
    duplicate: Sequence[EventAck] = (),
    rejected: Sequence[EventAck] = (),
    retryable: Sequence[EventAck] = (),
    retry_hint: Optional[int] = None,
    diagnostics: Optional[dict[str, str]] = None,
) -> TelemetryBatchAckV1:
    return TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=batch_id,
        server_time=server_time,
        accepted=list(accepted),
        duplicate=list(duplicate),
        rejected=list(rejected),
        retryable=list(retryable),
        retry_hint=retry_hint,
        coverage_diagnostics=dict(diagnostics or {}),
    )


def _retryable_ack(ack: TelemetryBatchAckV1) -> TelemetryBatchAckV1:
    """Re-target every accepted event as retryable (no false ACK)."""
    retryable = [
        EventAck(
            event_id=event_ack.event_id,
            disposition=EventDisposition.RETRYABLE,
            reason=IngestionReasonCode.STORE_UNAVAILABLE,
        )
        for event_ack in ack.accepted
    ]
    return TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=ack.batch_id,
        server_time=ack.server_time,
        duplicate=list(ack.duplicate),
        rejected=list(ack.rejected),
        retryable=list(ack.retryable) + retryable,
        retry_hint=ack.retry_hint,
        coverage_diagnostics=dict(ack.coverage_diagnostics),
    )


def _finalize(
    store: IngestionStore,
    batch_id: uuid.UUID,
    ack: TelemetryBatchAckV1,
    *,
    records: Sequence[ResearchEventRecord] = (),
    enrollment_id: Optional[uuid.UUID],
    research_session_id: Optional[uuid.UUID],
    retry_hint: Optional[int] = None,
) -> TelemetryBatchAckV1:
    """Persist the accepted events and the immutable receipt in one transaction.

    A retryable batch is not cached (a retry must be retryable): any row locks
    acquired for the batch are released with a rollback. A terminal decision
    stages the events and receipt, commits once, and only then returns the ACK.

    An :class:`IntegrityConflictError` identifies a single poison event; it is
    terminally rejected and the remaining records are persisted in a fresh
    transaction, so one bad event cannot make its siblings retryable forever.
    """
    if ack.retryable:
        store.rollback()
        return ack

    receipt = BatchReceipt(
        receipt_id=ack.receipt_id,
        batch_id=batch_id,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        accepted_at=ack.server_time,
        ack=ack,
    )
    try:
        if records:
            store.insert_events(records)
        store.record_batch_receipt(receipt)
        store.commit()
    except ReceiptConflictError:
        # A concurrent submission already committed a receipt for this batch:
        # never a second one. Return the winner's immutable receipt.
        store.rollback()
        existing = store.get_receipt(batch_id)
        return existing.ack if existing is not None else _retryable_ack(ack)
    except IntegrityConflictError as conflict:
        store.rollback()
        # A single poison event (its ``(session, emitter, sequence)`` key or its
        # event-id/digest identity is already taken) must not wedge the whole
        # batch: terminally reject only that event and persist the survivors.
        # Each recursion removes exactly one record, so this is bounded by the
        # batch size and always terminates.
        survivors = [
            record for record in records if record.event_id != conflict.event_id
        ]
        if len(survivors) == len(records):
            # The conflict is not one of the staged records (for example a
            # concurrent commit we cannot attribute): never loop, and never
            # falsely acknowledge; fall back to a retryable batch.
            return _retryable_ack(ack)
        rejected = list(ack.rejected) + [
            EventAck(
                event_id=conflict.event_id,
                disposition=EventDisposition.REJECTED,
                reason=IngestionReasonCode.INTEGRITY_CONFLICT,
                stored_digest=conflict.stored_digest,
            )
        ]
        accepted_survivors = [
            event_ack
            for event_ack in ack.accepted
            if event_ack.event_id != conflict.event_id
        ]
        conflict_ack = TelemetryBatchAckV1(
            receipt_id=uuid.uuid4(),
            batch_id=ack.batch_id,
            server_time=ack.server_time,
            accepted=accepted_survivors,
            duplicate=list(ack.duplicate),
            rejected=rejected,
            retryable=list(ack.retryable),
            retry_hint=retry_hint,
            coverage_diagnostics=dict(ack.coverage_diagnostics),
        )
        return _finalize(
            store,
            batch_id,
            conflict_ack,
            records=survivors,
            enrollment_id=enrollment_id,
            research_session_id=research_session_id,
            retry_hint=retry_hint,
        )
    except StoreUnavailable:
        store.rollback()
        return _retryable_ack(ack)

    # The unique ``batch_id`` guarantees one owner. If a concurrent submission
    # won, return its stored receipt rather than this (unstored) ACK.
    stored = store.get_receipt(batch_id)
    if stored is not None and stored.receipt_id != ack.receipt_id:
        return stored.ack
    return ack


def _reject_all(
    store: IngestionStore,
    batch_id: uuid.UUID,
    events: Sequence[CanonicalEventV1],
    server_time: datetime,
    reason: IngestionReasonCode,
    *,
    disposition: EventDisposition,
    retry_hint: Optional[int] = None,
    enrollment_id: Optional[uuid.UUID] = None,
    research_session_id: Optional[uuid.UUID] = None,
    max_acks: Optional[int] = None,
    persist_receipt: bool = True,
) -> TelemetryBatchAckV1:
    """Reject every event in the batch with one typed reason.

    ``persist_receipt=False`` is used for pre-authorization rejections (an
    unknown/out-of-scope subject, an oversized batch): those must not write a
    durable receipt, otherwise an unauthenticated caller could grow the receipt
    table one row per invented batch id. ``max_acks`` bounds the per-event ack
    list so an oversized batch cannot build an unbounded response.
    """
    bounded = list(events[:max_acks]) if max_acks is not None else list(events)
    group = [
        EventAck(event_id=event.event_id, disposition=disposition, reason=reason)
        for event in bounded
    ]
    if disposition == EventDisposition.RETRYABLE:
        ack = _ack(batch_id, server_time, retryable=group, retry_hint=retry_hint)
    else:
        ack = _ack(batch_id, server_time, rejected=group)
    if not persist_receipt:
        return ack
    return _finalize(
        store,
        batch_id,
        ack,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        retry_hint=retry_hint,
    )


def _anchor_context(
    request: TelemetryBatchRequestV1,
    enrollment_resolver: Callable[[uuid.UUID], Optional[Enrollment]],
    session_resolver: Callable[[uuid.UUID], Optional[ResearchSessionV1]],
) -> Optional[tuple[IngestionContext, Enrollment, ResearchSessionV1]]:
    """Resolve (and lock) the authorized subject of the batch.

    The resolvers are expected to lock the enrollment and session rows
    (``SELECT ... FOR UPDATE``); this serializes concurrent batches for the same
    subject, which is what makes batch idempotency concurrency-safe.
    """
    for event in request.events:
        if event.research_session_id is None or event.enrollment_id is None:
            continue
        session = session_resolver(event.research_session_id)
        if session is None:
            continue
        enrollment = enrollment_resolver(event.enrollment_id)
        if enrollment is None:
            continue
        if session.enrollment_id != enrollment.enrollment_id:
            continue
        if enrollment.study_id != session.study_id:
            continue
        context = IngestionContext(
            study_id=enrollment.study_id,
            enrollment_id=enrollment.enrollment_id,
            research_session_id=session.research_session_id,
            revocation_epoch=enrollment.revocation_epoch,
        )
        return context, enrollment, session
    return None


def _is_terminal(session: ResearchSessionV1) -> bool:
    state = session.state
    return state.is_terminal if hasattr(state, "is_terminal") else False


def _receipt_denied_ack(
    request: TelemetryBatchRequestV1, server_time: datetime, retry_hint: int
) -> TelemetryBatchAckV1:
    """A refusal that leaks no receipt content and never writes a new receipt."""
    return _ack(
        request.batch_id,
        server_time,
        retryable=[
            EventAck(
                event_id=event.event_id,
                disposition=EventDisposition.RETRYABLE,
                reason=IngestionReasonCode.CAPABILITY_INVALID,
            )
            for event in request.events
        ],
        retry_hint=retry_hint,
    )


def _authorized_receipt(
    request: TelemetryBatchRequestV1,
    receipt: BatchReceipt,
    *,
    receipt_capability_verifier: CapabilityVerifier,
    server_time: datetime,
    retry_hint: int,
) -> TelemetryBatchAckV1:
    """Return a stored receipt only to the capability's own subject (ISSUE-08).

    The batch id alone is never sufficient: the signed capability must cover
    the receipt's enrollment/session subject. Receipts persisted before subject
    binding existed (no enrollment/session) are refused rather than echoed.
    """
    if receipt.enrollment_id is None and receipt.research_session_id is None:
        return _receipt_denied_ack(request, server_time, retry_hint)
    verification = receipt_capability_verifier(
        request.session_capability,
        now=server_time,
        current_revocation_epoch=None,
        expected_enrollment_id=receipt.enrollment_id,
        expected_research_session_id=receipt.research_session_id,
    )
    if not verification.ok:
        return _receipt_denied_ack(request, server_time, retry_hint)
    return receipt.ack


def ingest_batch(
    request: TelemetryBatchRequestV1,
    *,
    capability_verifier: CapabilityVerifier,
    enrollment_resolver: Callable[[uuid.UUID], Optional[Enrollment]],
    session_resolver: Callable[[uuid.UUID], Optional[ResearchSessionV1]],
    store: IngestionStore,
    now: Optional[datetime] = None,
    max_events: int = DEFAULT_MAX_EVENTS,
    continuity_required: bool = False,
    retry_hint: int = DEFAULT_RETRY_HINT_SECONDS,
    kill_switch_check: Optional[Callable[[], bool]] = None,
    privacy_policy_resolver: Optional[Callable[[Enrollment], PrivacyPolicy]] = None,
    receipt_capability_verifier: Optional[CapabilityVerifier] = None,
) -> TelemetryBatchAckV1:
    """Ingest one batch and return a durable, deterministic acknowledgement."""
    server_time = _now(now)

    # Retry of an already-received batch returns the immutable receipt, but only
    # after the capability proves the caller owns its subject (ISSUE-08).
    existing_receipt = store.get_receipt(request.batch_id)
    if existing_receipt is not None:
        return _authorized_receipt(
            request,
            existing_receipt,
            receipt_capability_verifier=(
                receipt_capability_verifier or capability_verifier
            ),
            server_time=server_time,
            retry_hint=retry_hint,
        )

    if len(request.events) > max_events:
        return _reject_all(
            store,
            request.batch_id,
            request.events,
            server_time,
            IngestionReasonCode.BATCH_TOO_LARGE,
            disposition=EventDisposition.REJECTED,
            retry_hint=retry_hint,
            max_acks=max_events,
            persist_receipt=False,
        )

    # Operator kill switch (Issue 13): accepted batches are not stored while
    # engaged. The check is injected so ingestion keeps no operations dependency.
    if kill_switch_check is not None and kill_switch_check():
        kill_switch_group = [
            EventAck(
                event_id=event.event_id,
                disposition=EventDisposition.RETRYABLE,
                reason=IngestionReasonCode.KILL_SWITCH_ENGAGED,
            )
            for event in request.events
        ]
        return _ack(
            request.batch_id, server_time, retryable=kill_switch_group, retry_hint=retry_hint
        )

    if not request.events:
        # An empty batch has no subject to anchor or authorize: refuse it with a
        # typed reason and never persist a durable receipt for an
        # unauthenticated caller (ISSUE-08).
        return _ack(
            request.batch_id,
            server_time,
            retry_hint=retry_hint,
            diagnostics={"batch": IngestionReasonCode.EMPTY_BATCH.value},
        )

    anchored = _anchor_context(request, enrollment_resolver, session_resolver)
    if anchored is None:
        return _reject_all(
            store,
            request.batch_id,
            request.events,
            server_time,
            IngestionReasonCode.SESSION_OUT_OF_SCOPE,
            disposition=EventDisposition.REJECTED,
            retry_hint=retry_hint,
            persist_receipt=False,
        )
    context, enrollment, session = anchored

    # Re-check after acquiring the subject locks: a concurrent submission of the
    # same batch_id has now either committed (return its receipt) or is blocked
    # behind the same lock.
    locked_receipt = store.get_receipt(request.batch_id)
    if locked_receipt is not None:
        store.rollback()
        return _authorized_receipt(
            request,
            locked_receipt,
            receipt_capability_verifier=(
                receipt_capability_verifier or capability_verifier
            ),
            server_time=server_time,
            retry_hint=retry_hint,
        )

    verification = capability_verifier(
        request.session_capability,
        now=server_time,
        current_revocation_epoch=enrollment.revocation_epoch,
        expected_enrollment_id=context.enrollment_id,
        expected_research_session_id=context.research_session_id,
        expected_study_id=context.study_id,
    )
    if not verification.ok:
        if verification.reason == CapabilityReasonCode.REVOKED:
            return _reject_all(
                store,
                request.batch_id,
            request.events,
                server_time,
                IngestionReasonCode.REVOKED,
                disposition=EventDisposition.REJECTED,
                enrollment_id=context.enrollment_id,
                research_session_id=context.research_session_id,
                retry_hint=retry_hint,
            )
        return _reject_all(
            store,
            request.batch_id,
            request.events,
            server_time,
            IngestionReasonCode.CAPABILITY_INVALID,
            disposition=EventDisposition.RETRYABLE,
            retry_hint=retry_hint,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
        )

    if enrollment.status != EnrollmentStatus.ACTIVE:
        return _reject_all(
            store,
            request.batch_id,
            request.events,
            server_time,
            IngestionReasonCode.ENROLLMENT_NOT_ACTIVE,
            disposition=EventDisposition.REJECTED,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
            retry_hint=retry_hint,
        )

    if _is_terminal(session):
        # ENDED/REVOKED sessions are terminal: no later event is ever stored.
        return _reject_all(
            store,
            request.batch_id,
            request.events,
            server_time,
            IngestionReasonCode.SESSION_TERMINAL,
            disposition=EventDisposition.REJECTED,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
            retry_hint=retry_hint,
        )

    return _ingest_authorized_events(
        store,
        batch_id=request.batch_id,
        events=request.events,
        context=context,
        telemetry_schema_version=request.telemetry_schema_version,
        server_time=server_time,
        continuity_required=continuity_required,
        retry_hint=retry_hint,
        privacy_policy=privacy_policy_resolver(enrollment) if privacy_policy_resolver else PrivacyPolicy.default(),
    )


def _ingest_authorized_events(
    store: IngestionStore,
    *,
    batch_id: uuid.UUID,
    events: Sequence[CanonicalEventV1],
    context: IngestionContext,
    telemetry_schema_version: str,
    server_time: datetime,
    continuity_required: bool,
    retry_hint: int,
    privacy_policy: Optional[PrivacyPolicy] = None,
) -> TelemetryBatchAckV1:
    """Persist already-authorized events with the HTTP route's exact semantics.

    Shared by the participant upload route and the server-authorized internal
    entry point: identical per-event validation, digest/uniqueness handling,
    sequence continuity, receipt and commit-before-ACK. The caller has already
    verified the capability (route) or derived the context server-side
    (internal adapter) and applied the terminal/enrollment rules.
    """
    accepted: list[EventAck] = []
    duplicate: list[EventAck] = []
    rejected: list[EventAck] = []
    retryable: list[EventAck] = []
    diagnostics: dict[str, str] = {}
    candidates: list[tuple[CanonicalEventV1, str]] = []

    # An event id may appear more than once in one request (a producer retry
    # folded into a batch). The first occurrence wins; the repeats are not a
    # second fact and are reported as DUPLICATE below, so an id is acknowledged
    # exactly once and never appears in two ack groups (TI-02).
    unique_events: list[CanonicalEventV1] = []
    repeated_ids: set[uuid.UUID] = set()
    seen_ids: set[uuid.UUID] = set()
    for event in events:
        if event.event_id in seen_ids:
            repeated_ids.add(event.event_id)
            continue
        seen_ids.add(event.event_id)
        unique_events.append(event)

    try:
        stored = store.get_stored_digests([event.event_id for event in unique_events])
    except StoreUnavailable:
        return _reject_all(
            store,
            batch_id,
            unique_events,
            server_time,
            IngestionReasonCode.STORE_UNAVAILABLE,
            disposition=EventDisposition.RETRYABLE,
            retry_hint=retry_hint,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
        )

    for event in unique_events:
        issues = validate_batch_event(
            event,
            context,
            telemetry_schema_version=telemetry_schema_version,
        )
        if issues:
            code = issues[0].code
            rejected.append(
                EventAck(
                    event_id=event.event_id,
                    disposition=EventDisposition.REJECTED,
                    reason=code,
                )
            )
            continue

        if privacy_policy is not None:
            filtered = filter_event(event, privacy_policy)
            if filtered.summary.blocked or filtered.event.payload != event.payload:
                # Do not rewrite canonical bytes: that would invalidate the
                # producer's digest and turn a retry into an integrity conflict.
                rejected.append(EventAck(
                    event_id=event.event_id,
                    disposition=EventDisposition.REJECTED,
                    reason=IngestionReasonCode.PRIVACY_BLOCKED,
                ))
                continue

        digest = compute_event_digest(event)
        existing_digest = stored.get(event.event_id)
        if existing_digest is not None:
            if existing_digest == digest:
                duplicate.append(
                    EventAck(
                        event_id=event.event_id,
                        disposition=EventDisposition.DUPLICATE,
                        stored_digest=existing_digest,
                    )
                )
            else:
                rejected.append(
                    EventAck(
                        event_id=event.event_id,
                        disposition=EventDisposition.REJECTED,
                        reason=IngestionReasonCode.INTEGRITY_CONFLICT,
                        stored_digest=existing_digest,
                    )
                )
            continue

        candidates.append((event, digest))

    # Sequence continuity: a gap is accepted with a coverage diagnostic unless
    # the protocol declares continuity required.
    sequenced: list[tuple[CanonicalEventV1, str]] = []
    in_batch_last: dict[tuple[str, Optional[uuid.UUID]], int] = {}
    for event, digest in candidates:
        key = (event.emitter_id, event.research_session_id or context.research_session_id)
        cursor = store.get_emitter_cursor(key[0], key[1])
        last = max(cursor or 0, in_batch_last.get(key, 0))
        expected = last + 1
        if event.emitter_sequence != expected:
            if continuity_required:
                rejected.append(
                    EventAck(
                        event_id=event.event_id,
                        disposition=EventDisposition.REJECTED,
                        reason=IngestionReasonCode.CONTINUITY_REQUIRED,
                    )
                )
                in_batch_last[key] = max(last, event.emitter_sequence)
                continue
            diagnostics[str(event.event_id)] = (
                f"expected emitter sequence {expected}, received "
                f"{event.emitter_sequence}"
            )
            accepted.append(
                EventAck(
                    event_id=event.event_id,
                    disposition=EventDisposition.ACCEPTED,
                    reason=IngestionReasonCode.EVENT_SEQUENCE_GAP,
                    stored_digest=digest,
                )
            )
        else:
            accepted.append(
                EventAck(
                    event_id=event.event_id,
                    disposition=EventDisposition.ACCEPTED,
                    stored_digest=digest,
                )
            )
        in_batch_last[key] = max(last, event.emitter_sequence)
        sequenced.append((event, digest))

    # A repeated id's single ack is a DUPLICATE: the first occurrence is the
    # stored fact, the repeats add no second ack (TI-02).
    if repeated_ids:
        moved = [ack for ack in accepted if ack.event_id in repeated_ids]
        accepted = [ack for ack in accepted if ack.event_id not in repeated_ids]
        duplicate.extend(
            ack.model_copy(
                update={
                    "disposition": EventDisposition.DUPLICATE,
                    "reason": None,
                }
            )
            for ack in moved
        )

    records = [
        _record_from_event(event, context, digest, server_time)
        for event, digest in sequenced
    ]
    ack = _ack(
        batch_id,
        server_time,
        accepted=accepted,
        duplicate=duplicate,
        rejected=rejected,
        retryable=retryable,
        retry_hint=retry_hint,
        diagnostics=diagnostics,
    )
    return _finalize(
        store,
        batch_id,
        ack,
        records=records,
        enrollment_id=context.enrollment_id,
        research_session_id=context.research_session_id,
        retry_hint=retry_hint,
    )


def ingest_events_for_context(
    *,
    context: IngestionContext,
    enrollment: Enrollment,
    session: ResearchSessionV1,
    events: Sequence[CanonicalEventV1],
    store: IngestionStore,
    now: Optional[datetime] = None,
    kill_switch_check: Optional[Callable[[], bool]] = None,
    continuity_required: bool = False,
    telemetry_schema_version: str = "1",
    max_events: int = DEFAULT_MAX_EVENTS,
    retry_hint: int = DEFAULT_RETRY_HINT_SECONDS,
    privacy_policy: Optional[PrivacyPolicy] = None,
) -> TelemetryBatchAckV1:
    """Server-authorized internal ingestion entry point (no HTTP hop).

    A narrow adapter calls this directly with a context it has already derived
    from the authorized account + frozen assignment (an explicit research
    binding), so legacy producers stop writing a parallel store. It applies the
    same kill-switch, privacy-policy, enrollment-ACTIVE and terminal-session
    rules as the participant upload route, then shares the route's validation,
    uniqueness, receipt and commit-before-ACK semantics. The
    context/assignment is trusted server-side state, never a caller-supplied
    body.
    """
    server_time = _now(now)
    batch_id = uuid.uuid4()

    if len(events) > max_events:
        return _reject_all(
            store,
            batch_id,
            events,
            server_time,
            IngestionReasonCode.BATCH_TOO_LARGE,
            disposition=EventDisposition.REJECTED,
            retry_hint=retry_hint,
        )

    if kill_switch_check is not None and kill_switch_check():
        group = [
            EventAck(
                event_id=event.event_id,
                disposition=EventDisposition.RETRYABLE,
                reason=IngestionReasonCode.KILL_SWITCH_ENGAGED,
            )
            for event in events
        ]
        return _ack(batch_id, server_time, retryable=group, retry_hint=retry_hint)

    if not events:
        return _ack(
            batch_id,
            server_time,
            retry_hint=retry_hint,
            diagnostics={"batch": IngestionReasonCode.EMPTY_BATCH.value},
        )

    if enrollment.status != EnrollmentStatus.ACTIVE:
        return _reject_all(
            store,
            batch_id,
            events,
            server_time,
            IngestionReasonCode.ENROLLMENT_NOT_ACTIVE,
            disposition=EventDisposition.REJECTED,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
            retry_hint=retry_hint,
        )

    if _is_terminal(session):
        return _reject_all(
            store,
            batch_id,
            events,
            server_time,
            IngestionReasonCode.SESSION_TERMINAL,
            disposition=EventDisposition.REJECTED,
            enrollment_id=context.enrollment_id,
            research_session_id=context.research_session_id,
            retry_hint=retry_hint,
        )

    return _ingest_authorized_events(
        store,
        batch_id=batch_id,
        events=events,
        context=context,
        telemetry_schema_version=telemetry_schema_version,
        server_time=server_time,
        continuity_required=continuity_required,
        retry_hint=retry_hint,
        privacy_policy=privacy_policy,
    )
