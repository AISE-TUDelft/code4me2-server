"""Ingestion rejection hardening (production-readiness regression tests).

Pre-authorization rejections (an unknown/out-of-scope subject, an oversized
batch) must not write durable receipts: otherwise an unauthenticated caller can
grow ``telemetry_batch_receipt`` one row per invented batch id. An oversized
batch must also bound its per-event acknowledgement list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from research.telemetry.ingestion.enums import EventDisposition, IngestionReasonCode
from research.telemetry.ingestion.models import (
    BatchReceipt,
    ResearchEventRecord,
    TelemetryBatchAckV1,
    TelemetryBatchRequestV1,
)
from research.telemetry.ingestion.service import ingest_batch

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class _RecordingStore:
    """Minimal IngestionStore double that records receipt writes."""

    def __init__(self) -> None:
        self.receipts: list[BatchReceipt] = []
        self.events: list[ResearchEventRecord] = []
        self.commits = 0

    def get_receipt(self, batch_id: uuid.UUID) -> Optional[BatchReceipt]:
        return next((r for r in self.receipts if r.batch_id == batch_id), None)

    def insert_events(self, records: Sequence[ResearchEventRecord]) -> None:
        self.events.extend(records)

    def record_batch_receipt(self, receipt: BatchReceipt) -> None:
        self.receipts.append(receipt)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - not expected here
        raise AssertionError("rollback must not be needed for a rejection")


def _event(index: int) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "schema_version": "1",
        "event_type": "tool.completed",
        "source": "ide",
        "study_id": str(uuid.uuid4()),
        "enrollment_id": str(uuid.uuid4()),
        "research_session_id": str(uuid.uuid4()),
        "occurred_at": NOW.isoformat(),
        "emitter_id": "hardening-emitter",
        "emitter_sequence": index,
        "provenance": {"source": "ide", "normalizer_version": "1", "fidelity": "normalized"},
    }


def _capability() -> dict[str, Any]:
    return {
        "capability_id": str(uuid.uuid4()),
        "audience": "research-runtime",
        "scope": ["telemetry:write"],
        "issued_at": NOW.isoformat(),
        "expires_at": NOW.isoformat(),
        "revocation_epoch": 0,
        "enrollment_id": str(uuid.uuid4()),
        "research_session_id": str(uuid.uuid4()),
        "study_id": str(uuid.uuid4()),
        "signature": "0" * 64,
    }


def _request(count: int) -> TelemetryBatchRequestV1:
    return TelemetryBatchRequestV1.model_validate(
        {
            "batch_id": str(uuid.uuid4()),
            "session_capability": _capability(),
            "client_instance_id": "hardening-client",
            "events": [_event(index) for index in range(count)],
        }
    )


def _verifier_ok(capability, **kwargs):  # noqa: ANN001, ANN003 - structural double
    from research.runtime.bootstrap.capability import CapabilityVerification

    return CapabilityVerification(ok=True, reason=None, message="")


def test_out_of_scope_batch_is_rejected_without_persisting_a_receipt():
    store = _RecordingStore()

    ack = ingest_batch(
        _request(1),
        capability_verifier=_verifier_ok,
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=NOW,
    )

    assert [entry.reason for entry in ack.rejected] == [IngestionReasonCode.SESSION_OUT_OF_SCOPE]
    assert store.receipts == [], "a pre-authorization rejection must not write a receipt"
    assert store.commits == 0


def test_oversized_batch_bounds_acks_and_does_not_persist_a_receipt():
    store = _RecordingStore()
    request = _request(3)

    ack = ingest_batch(
        request,
        capability_verifier=_verifier_ok,
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=NOW,
        max_events=2,
    )

    assert len(ack.rejected) <= 2, "the ack list must be bounded by max_events"
    assert all(
        entry.reason == IngestionReasonCode.BATCH_TOO_LARGE for entry in ack.rejected
    )
    assert store.receipts == [], "an oversized batch must not write a receipt"


def test_an_empty_batch_is_refused_without_persisting_a_receipt():
    """ISSUE-08: no durable receipt for a batch with no subject and no fact."""
    store = _RecordingStore()

    ack = ingest_batch(
        _request(0),
        capability_verifier=_verifier_ok,
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=NOW,
    )

    assert ack.accepted == [] and ack.rejected == []
    assert ack.coverage_diagnostics == {"batch": IngestionReasonCode.EMPTY_BATCH.value}
    assert store.receipts == [], "an empty batch must not create a durable receipt"
    assert store.commits == 0


def _verifier_subject(expected: tuple[uuid.UUID, uuid.UUID]):
    from research.runtime.bootstrap.capability import CapabilityVerification
    from research.runtime.bootstrap.models import CapabilityReasonCode

    def verify(  # noqa: ANN001 - structural double
        capability,
        *,
        now,
        current_revocation_epoch,
        expected_enrollment_id=None,
        expected_research_session_id=None,
        expected_study_id=None,
    ):
        ok = (
            expected_enrollment_id == expected[0]
            and expected_research_session_id == expected[1]
        )
        return CapabilityVerification(
            ok=ok,
            reason=(
                CapabilityReasonCode.OK
                if ok
                else CapabilityReasonCode.SUBJECT_MISMATCH
            ),
            message="",
        )

    return verify


def _stored_receipt(request, *, enrollment_id, research_session_id):
    ack = TelemetryBatchAckV1(
        receipt_id=uuid.uuid4(),
        batch_id=request.batch_id,
        server_time=NOW,
    )
    return BatchReceipt(
        receipt_id=ack.receipt_id,
        batch_id=request.batch_id,
        enrollment_id=enrollment_id,
        research_session_id=research_session_id,
        accepted_at=NOW,
        ack=ack,
    )


def test_a_known_batch_id_alone_cannot_read_the_receipt():
    """Knowing another batch id must not disclose its ACK (ISSUE-08)."""
    store = _RecordingStore()
    request = _request(1)
    subject = (uuid.uuid4(), uuid.uuid4())
    receipt = _stored_receipt(
        request, enrollment_id=subject[0], research_session_id=subject[1]
    )
    store.receipts.append(receipt)

    # The caller's capability verifies cryptographically but does not cover the
    # receipt's subject: the lookup must be refused without any ACK content.
    ack = ingest_batch(
        request,
        capability_verifier=_verifier_ok,
        receipt_capability_verifier=_verifier_subject(
            (uuid.uuid4(), uuid.uuid4())
        ),
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=NOW,
    )

    assert ack.receipt_id != receipt.ack.receipt_id
    assert ack.accepted == [] and ack.duplicate == []
    assert all(
        entry.reason == IngestionReasonCode.CAPABILITY_INVALID for entry in ack.retryable
    )
    assert len(store.receipts) == 1, "the refusal must not write a second receipt"


def test_the_owning_subject_still_receives_its_receipt():
    """A legitimate retry with the matching subject keeps idempotency."""
    store = _RecordingStore()
    request = _request(1)
    subject = (uuid.uuid4(), uuid.uuid4())
    receipt = _stored_receipt(
        request, enrollment_id=subject[0], research_session_id=subject[1]
    )
    store.receipts.append(receipt)

    ack = ingest_batch(
        request,
        capability_verifier=_verifier_ok,
        receipt_capability_verifier=_verifier_subject(subject),
        enrollment_resolver=lambda _id: None,
        session_resolver=lambda _id: None,
        store=store,
        now=NOW,
    )

    assert ack.receipt_id == receipt.ack.receipt_id
    assert len(store.receipts) == 1
