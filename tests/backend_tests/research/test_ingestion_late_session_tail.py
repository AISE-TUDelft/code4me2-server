"""Session-tail acceptance and emitter-sequence overlaps (review C-02 / C-01).

Before this change every event of an ENDED session was rejected
``SESSION_TERMINAL`` and the client discarded it: the last poll window, the
closing events and anything collected offline were lost on every idle timeout.
Now an ENDED session's tail is stored while it occurred within the grace window
after ``closed_at``; the capability is bound to the enrollment and study (the
client's next session capability delivers the previous tail). ``REVOKED``
sessions stay fully terminal. A reused ``(session, emitter, sequence)`` key is
no longer a conflict that deletes data: both facts are stored, the overlap is a
coverage diagnostic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from research.participants.enums import EnrollmentStatus
from research.participants.models import Enrollment
from research.runtime.bootstrap.capability import CapabilityVerification
from research.runtime.bootstrap.models import CapabilityReasonCode
from research.runtime.sessions.enums import CloseReason, SessionState
from research.runtime.sessions.models import ResearchSessionV1
from research.telemetry.ingestion.enums import EventDisposition, IngestionReasonCode
from research.telemetry.ingestion.models import IngestionContext, TelemetryBatchRequestV1
from research.telemetry.ingestion.service import ingest_batch, ingest_events_for_context
from research.telemetry.ingestion.store import FakeIngestionStore
from research.telemetry.models import CanonicalEventV1

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CLOSED_AT = NOW - timedelta(minutes=5)


def _enrollment(status: EnrollmentStatus = EnrollmentStatus.ACTIVE) -> Enrollment:
    return Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        participant_code="participant-code",
        status=status,
        eligibility={"eligible": True, "reasons": [], "evaluated_at": NOW},
        enrolled_at=NOW - timedelta(days=1),
        consent_accepted_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )


def _session(enrollment: Enrollment, state: SessionState) -> ResearchSessionV1:
    terminal = state.is_terminal
    return ResearchSessionV1(
        research_session_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_id=enrollment.study_id,
        context_id="ctx-1",
        state=state,
        opened_at=NOW - timedelta(hours=1),
        last_activity_at=CLOSED_AT - timedelta(minutes=10),
        closed_at=CLOSED_AT if terminal else None,
        close_reason=(
            (CloseReason.REVOKED if state == SessionState.REVOKED else CloseReason.IDLE_TIMEOUT)
            if terminal
            else None
        ),
    )


def _event(
    enrollment: Enrollment,
    session: ResearchSessionV1,
    *,
    occurred_at: datetime,
    sequence: int = 1,
    emitter_id: str = "ide:ctx-1:abcd1234",
    event_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    return {
        "event_id": str(event_id or uuid.uuid4()),
        "schema_version": "1",
        "event_type": "tool.completed",
        "source": "ide",
        "study_id": str(enrollment.study_id),
        "enrollment_id": str(enrollment.enrollment_id),
        "research_session_id": str(session.research_session_id),
        "occurred_at": occurred_at.isoformat(),
        "emitter_id": emitter_id,
        "emitter_sequence": sequence,
        "provenance": {"source": "ide", "normalizer_version": "1", "fidelity": "normalized"},
    }


def _capability(enrollment: Enrollment, session_id: uuid.UUID) -> dict[str, Any]:
    return {
        "capability_id": str(uuid.uuid4()),
        "audience": "research-runtime",
        "scope": ["telemetry:write"],
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "revocation_epoch": 0,
        "enrollment_id": str(enrollment.enrollment_id),
        "research_session_id": str(session_id),
        "study_id": str(enrollment.study_id),
        "signature": "0" * 64,
    }


def _request(enrollment: Enrollment, capability_session: uuid.UUID, events: list[dict]) -> TelemetryBatchRequestV1:
    return TelemetryBatchRequestV1.model_validate(
        {
            "batch_id": str(uuid.uuid4()),
            "session_capability": _capability(enrollment, capability_session),
            "client_instance_id": "client-1",
            "events": events,
        }
    )


class _RecordingVerifier:
    """Accepts every capability and records the subject binding it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, capability, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(kwargs)
        return CapabilityVerification(ok=True, reason=CapabilityReasonCode.OK, message="")


def _ingest(store, enrollment, session, request, verifier=None, **overrides):
    verifier = verifier or _RecordingVerifier()
    return ingest_batch(
        request,
        capability_verifier=verifier,
        enrollment_resolver=lambda _id: enrollment,
        session_resolver=lambda _id: session,
        store=store,
        now=NOW,
        **overrides,
    ), verifier


# ---------------------------------------------------------------------------
# C-02: the tail of an ENDED session
# ---------------------------------------------------------------------------


def test_events_of_an_ended_session_within_grace_are_stored_and_late_ones_rejected():
    enrollment = _enrollment()
    ended = _session(enrollment, SessionState.ENDED)
    new_session_id = uuid.uuid4()
    in_grace_id = uuid.uuid4()
    late_id = uuid.uuid4()
    request = _request(
        enrollment,
        new_session_id,
        [
            _event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=100), sequence=1, event_id=in_grace_id),
            _event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=2000), sequence=2, event_id=late_id),
        ],
    )
    store = FakeIngestionStore()

    ack, verifier = _ingest(store, enrollment, ended, request)

    assert [entry.event_id for entry in ack.accepted] == [in_grace_id]
    assert [(entry.event_id, entry.reason) for entry in ack.rejected] == [
        (late_id, IngestionReasonCode.SESSION_TERMINAL)
    ]
    assert ack.retryable == []
    # The stored fact keeps its true (ended) session attribution.
    stored = store.stored_events()
    assert [record.event_id for record in stored] == [in_grace_id]
    assert stored[0].research_session_id == ended.research_session_id
    # The receipt is durable, so a retry of the batch returns the same decision.
    assert store.get_receipt(request.batch_id) is not None
    # The capability was bound to enrollment + study, not to the ended session,
    # so the participant's NEXT session capability can deliver this tail.
    assert verifier.calls[0]["expected_research_session_id"] is None
    assert verifier.calls[0]["expected_enrollment_id"] == enrollment.enrollment_id
    assert verifier.calls[0]["expected_study_id"] == enrollment.study_id


def test_an_ended_session_whose_events_are_all_late_persists_a_terminal_receipt():
    enrollment = _enrollment()
    ended = _session(enrollment, SessionState.ENDED)
    request = _request(
        enrollment,
        uuid.uuid4(),
        [_event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(hours=2))],
    )
    store = FakeIngestionStore()

    ack, _ = _ingest(store, enrollment, ended, request)

    assert ack.accepted == []
    assert [entry.reason for entry in ack.rejected] == [IngestionReasonCode.SESSION_TERMINAL]
    assert store.event_count() == 0
    assert store.get_receipt(request.batch_id) is not None


def test_the_grace_window_is_configurable_and_zero_still_accepts_events_before_close():
    enrollment = _enrollment()
    ended = _session(enrollment, SessionState.ENDED)
    before_close = uuid.uuid4()
    after_close = uuid.uuid4()
    request = _request(
        enrollment,
        uuid.uuid4(),
        [
            _event(enrollment, ended, occurred_at=CLOSED_AT - timedelta(seconds=5), sequence=1, event_id=before_close),
            _event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=5), sequence=2, event_id=after_close),
        ],
    )
    store = FakeIngestionStore()

    ack, _ = _ingest(store, enrollment, ended, request, late_event_grace_seconds=0)

    assert [entry.event_id for entry in ack.accepted] == [before_close]
    assert [entry.event_id for entry in ack.rejected] == [after_close]


def test_a_revoked_session_rejects_everything_permanently_under_the_next_capability():
    """Plugin review F4: a REVOKED session's records presented under the
    enrollment's next session capability must be a PERMANENT per-event
    rejection (the client discards them), never a retryable capability refusal
    the uploader would retry forever, starving every later session's batches."""
    enrollment = _enrollment()
    revoked = _session(enrollment, SessionState.REVOKED)
    next_session_id = uuid.uuid4()
    request = _request(
        enrollment,
        next_session_id,
        [_event(enrollment, revoked, occurred_at=CLOSED_AT + timedelta(seconds=1))],
    )
    store = FakeIngestionStore()
    verifier = _SessionBindingVerifier()

    ack, _ = _ingest(store, enrollment, revoked, request, verifier=verifier)

    assert ack.accepted == [] and ack.retryable == []
    assert [entry.reason for entry in ack.rejected] == [IngestionReasonCode.SESSION_TERMINAL]
    assert store.event_count() == 0
    # Bound to enrollment + study (the binding is relaxed for any terminal session).
    assert verifier.calls[0]["expected_research_session_id"] is None
    assert verifier.calls[0]["expected_enrollment_id"] == enrollment.enrollment_id
    # The permanent decision is durable: a lost-ACK replay returns the same receipt.
    replay, _ = _ingest(store, enrollment, revoked, request, verifier=verifier)
    assert replay.receipt_id == ack.receipt_id
    assert replay.retryable == []


def test_a_live_session_keeps_the_strict_session_binding():
    enrollment = _enrollment()
    running = _session(enrollment, SessionState.RUNNING)
    request = _request(
        enrollment,
        running.research_session_id,
        [_event(enrollment, running, occurred_at=NOW)],
    )
    store = FakeIngestionStore()

    ack, verifier = _ingest(store, enrollment, running, request)

    assert len(ack.accepted) == 1
    assert verifier.calls[0]["expected_research_session_id"] == running.research_session_id


def test_a_non_active_enrollment_still_rejects_an_ended_sessions_tail():
    enrollment = _enrollment(EnrollmentStatus.REVOKED)
    ended = _session(enrollment, SessionState.ENDED)
    request = _request(
        enrollment,
        uuid.uuid4(),
        [_event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=1))],
    )
    store = FakeIngestionStore()

    ack, _ = _ingest(store, enrollment, ended, request)

    assert [entry.reason for entry in ack.rejected] == [IngestionReasonCode.ENROLLMENT_NOT_ACTIVE]


def test_the_internal_entry_point_keeps_terminal_sessions_strict():
    enrollment = _enrollment()
    ended = _session(enrollment, SessionState.ENDED)
    context = IngestionContext(
        study_id=enrollment.study_id,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=ended.research_session_id,
    )
    event = CanonicalEventV1.model_validate(
        _event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=1))
    )

    ack = ingest_events_for_context(
        context=context,
        enrollment=enrollment,
        session=ended,
        events=[event],
        store=FakeIngestionStore(),
        now=NOW,
    )

    assert [entry.reason for entry in ack.rejected] == [IngestionReasonCode.SESSION_TERMINAL]


# ---------------------------------------------------------------------------
# C-01: a restarted emitter counter never deletes facts
# ---------------------------------------------------------------------------


def test_a_reused_emitter_sequence_stores_both_facts_with_a_gap_diagnostic():
    enrollment = _enrollment()
    running = _session(enrollment, SessionState.RUNNING)
    store = FakeIngestionStore()
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()

    first, _ = _ingest(
        store,
        enrollment,
        running,
        _request(enrollment, running.research_session_id, [_event(enrollment, running, occurred_at=NOW, sequence=1, event_id=first_id)]),
    )
    assert [entry.event_id for entry in first.accepted] == [first_id]

    # "IDE restart inside the session": the same emitter restarts at 1.
    second, _ = _ingest(
        store,
        enrollment,
        running,
        _request(enrollment, running.research_session_id, [_event(enrollment, running, occurred_at=NOW, sequence=1, event_id=second_id)]),
    )

    assert [entry.event_id for entry in second.accepted] == [second_id]
    assert second.accepted[0].reason == IngestionReasonCode.EVENT_SEQUENCE_GAP
    assert str(second_id) in second.coverage_diagnostics
    assert second.rejected == [] and second.retryable == []
    assert store.event_count() == 2


def test_in_batch_sequence_reuse_is_accepted_not_rejected():
    enrollment = _enrollment()
    running = _session(enrollment, SessionState.RUNNING)
    store = FakeIngestionStore()
    ids = [uuid.uuid4(), uuid.uuid4()]
    request = _request(
        enrollment,
        running.research_session_id,
        [
            _event(enrollment, running, occurred_at=NOW, sequence=7, event_id=ids[0]),
            _event(enrollment, running, occurred_at=NOW, sequence=7, event_id=ids[1]),
        ],
    )

    ack, _ = _ingest(store, enrollment, running, request)

    assert sorted(entry.event_id for entry in ack.accepted) == sorted(ids)
    assert ack.rejected == []
    assert store.event_count() == 2
    assert all(entry.disposition == EventDisposition.ACCEPTED for entry in ack.accepted)


# ---------------------------------------------------------------------------
# Review F-2: a lost-ACK retry of an accepted tail batch must return its receipt
# ---------------------------------------------------------------------------


class _SessionBindingVerifier(_RecordingVerifier):
    """Enforces the session binding exactly like the real verifier does."""

    def __call__(self, capability, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(kwargs)
        expected = kwargs.get("expected_research_session_id")
        if expected is not None and capability.research_session_id != expected:
            return CapabilityVerification(
                ok=False, reason=CapabilityReasonCode.SESSION_MISMATCH, message="session mismatch"
            )
        return CapabilityVerification(ok=True, reason=CapabilityReasonCode.OK, message="")


def test_replaying_an_accepted_tail_batch_under_the_new_capability_returns_its_receipt():
    enrollment = _enrollment()
    ended = _session(enrollment, SessionState.ENDED)
    new_session_id = uuid.uuid4()
    event_id = uuid.uuid4()
    request = _request(
        enrollment,
        new_session_id,
        [_event(enrollment, ended, occurred_at=CLOSED_AT + timedelta(seconds=30), event_id=event_id)],
    )
    store = FakeIngestionStore()
    verifier = _SessionBindingVerifier()

    first, _ = _ingest(store, enrollment, ended, request, verifier=verifier)
    assert [entry.event_id for entry in first.accepted] == [event_id]

    # The client lost the ACK and retries the identical batch (same batch_id)
    # with the same new-session capability: the immutable receipt comes back,
    # never a retryable refusal that would wedge every later upload.
    replay, _ = _ingest(store, enrollment, ended, request, verifier=verifier)

    assert replay.receipt_id == first.receipt_id
    assert [entry.event_id for entry in replay.accepted] == [event_id]
    assert replay.retryable == []
    assert store.event_count() == 1


def test_replaying_a_live_sessions_batch_keeps_the_strict_session_binding():
    enrollment = _enrollment()
    running = _session(enrollment, SessionState.RUNNING)
    request = _request(
        enrollment,
        running.research_session_id,
        [_event(enrollment, running, occurred_at=NOW)],
    )
    store = FakeIngestionStore()
    verifier = _SessionBindingVerifier()
    first, _ = _ingest(store, enrollment, running, request, verifier=verifier)
    assert len(first.accepted) == 1

    # Same batch id presented with a capability for ANOTHER (live) session: the
    # receipt is not echoed to a different subject.
    foreign = TelemetryBatchRequestV1.model_validate(
        {
            **request.model_dump(mode="json"),
            "session_capability": _capability(enrollment, uuid.uuid4()),
        }
    )
    denied, _ = _ingest(store, enrollment, running, foreign, verifier=verifier)
    assert denied.accepted == []
    assert [entry.reason for entry in denied.retryable] == [IngestionReasonCode.CAPABILITY_INVALID]
