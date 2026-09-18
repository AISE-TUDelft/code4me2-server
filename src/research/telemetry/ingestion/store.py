"""Persistence helpers and an in-memory test double for ingestion.

The SQLAlchemy store takes a caller-supplied ``Session`` and stages its writes:
``insert_events`` and ``record_batch_receipt`` never commit, so the ingestion
service can persist the accepted events and the immutable batch receipt in one
transaction and return the ACK only after ``commit``. ``FakeIngestionStore`` is
a pure in-memory double that mirrors the same commit/rollback semantics (and
enforces unique event ids/digest conflicts), so atomicity is machine-testable
without PostgreSQL.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from database.research_schemas import (
    ResearchEvent,
    TelemetryBatchReceipt,
)

from .errors import IntegrityConflictError, ReceiptConflictError, StoreUnavailable
from .models import BatchReceipt, IngestionStore, ResearchEventRecord

__all__ = [
    "FakeIngestionStore",
    "IngestionStore",
    "SqlAlchemyIngestionStore",
    "get_receipt_by_id",
    "row_to_record",
]


class FakeIngestionStore:
    """In-memory ingestion store for deterministic tests.

    Writes are staged until :meth:`commit`, so a failed commit (or a mid-batch
    error) leaves the committed state untouched. ``fail_inserts`` simulates a
    transient failure while staging events, ``fail_receipts`` while staging the
    receipt and ``fail_commit`` at commit time.
    """

    def __init__(self) -> None:
        self._events: dict[uuid.UUID, ResearchEventRecord] = {}
        self._receipts: dict[uuid.UUID, BatchReceipt] = {}
        self._staged_events: dict[uuid.UUID, ResearchEventRecord] = {}
        self._staged_receipts: dict[uuid.UUID, BatchReceipt] = {}
        self.fail_inserts = False
        self.fail_receipts = False
        self.fail_commit = False

    # -- events -----------------------------------------------------------

    def get_stored_digests(
        self, event_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Return ``{event_id: digest}`` for event ids already committed."""
        return {
            event_id: self._events[event_id].digest
            for event_id in event_ids
            if event_id in self._events
        }

    def _emitter_key_conflict(
        self, record: ResearchEventRecord
    ) -> Optional[ResearchEventRecord]:
        """Return a committed/staged record colliding on the emitter key.

        Mirrors the database's ``uq_research_event_session_emitter_sequence``
        unique constraint: one ``(research_session_id, emitter_id,
        emitter_sequence)`` may identify at most one event id.
        """
        for existing in (*self._events.values(), *self._staged_events.values()):
            if (
                existing.event_id != record.event_id
                and existing.emitter_id == record.emitter_id
                and existing.research_session_id == record.research_session_id
                and existing.emitter_sequence == record.emitter_sequence
            ):
                return existing
        return None

    def insert_events(self, records: Sequence[ResearchEventRecord]) -> list[uuid.UUID]:
        """Stage records, enforcing unique ids, digests, and emitter keys.

        A same-id/same-digest insert is a no-op (idempotent); a same-id/different
        digest, or a different event id reusing the same
        ``(session, emitter, sequence)`` key, raises
        :class:`IntegrityConflictError`. ``fail_inserts`` simulates a transiently
        unavailable store.
        """
        if self.fail_inserts:
            raise StoreUnavailable("in-memory store is unavailable")

        inserted: list[uuid.UUID] = []
        for record in records:
            existing = self._events.get(record.event_id)
            if existing is not None:
                if existing.digest != record.digest:
                    raise IntegrityConflictError(
                        record.event_id, existing.digest, record.digest
                    )
                continue
            emitter_conflict = self._emitter_key_conflict(record)
            if emitter_conflict is not None:
                # The stored digest is the one that already owns this emitter
                # key; the presented record is the poison event.
                raise IntegrityConflictError(
                    record.event_id, emitter_conflict.digest, record.digest
                )
            self._staged_events[record.event_id] = record
            inserted.append(record.event_id)
        return inserted

    def stored_events(self) -> list[ResearchEventRecord]:
        """Return the committed records (test introspection)."""
        return list(self._events.values())

    def event_count(self) -> int:
        """Number of committed canonical facts."""
        return len(self._events)

    # -- receipts ---------------------------------------------------------

    def record_batch_receipt(self, receipt: BatchReceipt) -> None:
        """Stage a receipt; the committed receipt for a batch is immutable."""
        if self.fail_receipts:
            raise StoreUnavailable("in-memory receipt store is unavailable")
        self._staged_receipts[receipt.batch_id] = receipt

    def get_receipt(self, batch_id: uuid.UUID) -> Optional[BatchReceipt]:
        """Fetch the committed immutable receipt for a batch, or ``None``."""
        return self._receipts.get(batch_id)

    # -- transaction boundary --------------------------------------------

    def commit(self) -> None:
        """Make every staged write durable, atomically."""
        if self.fail_commit:
            raise StoreUnavailable("in-memory commit failed")
        # The batch id is unique: a committed receipt for the same batch wins
        # and this commit must not replace it.
        for batch_id, receipt in self._staged_receipts.items():
            existing = self._receipts.get(batch_id)
            if existing is not None and existing.receipt_id != receipt.receipt_id:
                raise ReceiptConflictError(batch_id)
        for key, record in self._staged_events.items():
            self._events.setdefault(key, record)
        for batch_id, receipt in self._staged_receipts.items():
            self._receipts.setdefault(batch_id, receipt)
        self._staged_events.clear()
        self._staged_receipts.clear()

    def rollback(self) -> None:
        """Discard every staged write, leaving committed state unchanged."""
        self._staged_events.clear()
        self._staged_receipts.clear()

    # -- emitter cursors --------------------------------------------------

    def get_emitter_cursor(
        self, emitter_id: str, research_session_id: Optional[uuid.UUID]
    ) -> Optional[int]:
        """Return the max committed sequence for an emitter, or ``None``."""
        sequences = [
            record.emitter_sequence
            for record in self._events.values()
            if record.emitter_id == emitter_id
            and record.research_session_id == research_session_id
        ]
        return max(sequences) if sequences else None


def row_to_record(row: ResearchEvent) -> ResearchEventRecord:
    """Rehydrate a stored event row into the record model.

    The complete ``envelope_json`` document is the authority; the searchable
    scalar columns are read alongside it (they are derived from the same envelope
    at insert time).
    """
    data = {
        name: getattr(row, name)
        for name in ResearchEventRecord.model_fields
        if name != "envelope"
    }
    data["envelope"] = row.envelope_json or {}
    return ResearchEventRecord.model_validate(data)


class SqlAlchemyIngestionStore:
    """SQLAlchemy-backed ingestion store with an explicit transaction boundary."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_stored_digests(
        self, event_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Return ``{event_id: digest}`` for already-stored event ids."""
        if not event_ids:
            return {}
        statement = select(ResearchEvent.event_id, ResearchEvent.digest).where(
            ResearchEvent.event_id.in_(list(event_ids))
        )
        return {row[0]: row[1] for row in self.session.execute(statement).all()}

    def _emitter_key_conflict(
        self, records: Sequence[ResearchEventRecord]
    ) -> Optional[IntegrityConflictError]:
        """Attribute a unique-key violation to the exact poison record.

        Called only after a failed insert has been rolled back, so the probe runs
        in a fresh transaction. Returns ``None`` when no record collides on the
        ``(research_session_id, emitter_id, emitter_sequence)`` unique key.
        """
        for record in records:
            statement = select(ResearchEvent.event_id, ResearchEvent.digest).where(
                ResearchEvent.research_session_id == record.research_session_id,
                ResearchEvent.emitter_id == record.emitter_id,
                ResearchEvent.emitter_sequence == record.emitter_sequence,
                ResearchEvent.event_id != record.event_id,
            )
            row = self.session.execute(statement).first()
            if row is not None:
                return IntegrityConflictError(record.event_id, row[1], record.digest)
        return None

    def insert_events(self, records: Sequence[ResearchEventRecord]) -> list[uuid.UUID]:
        """Stage insert-only event persistence (unique ``event_id``).

        ``ON CONFLICT DO NOTHING`` makes a concurrent duplicate a no-op rather
        than aborting the transaction, so the digest-identity check (performed
        inside the same transaction) remains authoritative and an existing fact
        is never overwritten.

        A violation of the ``(research_session_id, emitter_id,
        emitter_sequence)`` unique key is not covered by the ``event_id``
        conflict target; it aborts the statement. Rather than reporting the whole
        batch retryable (which would wedge every later retry), the conflict is
        attributed to the exact event so the service can terminally reject only
        that poison event and persist its siblings.
        """
        if not records:
            return []
        try:
            values = [
                {
                    **record.model_dump(exclude={"envelope"}),
                    "envelope_json": record.envelope,
                }
                for record in records
            ]
            statement = pg_insert(ResearchEvent).values(values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["event_id"]
            )
            self.session.execute(statement)
        except IntegrityError as error:
            self.session.rollback()
            conflict = self._emitter_key_conflict(records)
            if conflict is not None:
                raise conflict from error
            raise StoreUnavailable("event store is unavailable") from error
        except SQLAlchemyError as error:
            raise StoreUnavailable("event store is unavailable") from error
        return [record.event_id for record in records]

    def record_batch_receipt(self, receipt: BatchReceipt) -> None:
        """Stage a receipt if the batch has none (immutable, retry-safe).

        ``ON CONFLICT DO NOTHING`` on the unique ``batch_id`` means a concurrent
        commit for the same batch never replaces the winner's receipt; the
        service re-reads the stored receipt after commit and returns that one.
        """
        try:
            statement = (
                pg_insert(TelemetryBatchReceipt)
                .values(
                    receipt_id=receipt.receipt_id,
                    batch_id=str(receipt.batch_id),
                    enrollment_id=receipt.enrollment_id,
                    research_session_id=receipt.research_session_id,
                    accepted_at=receipt.accepted_at,
                    receipt_json=receipt.ack.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=["batch_id"])
            )
            self.session.execute(statement)
        except SQLAlchemyError as error:
            raise StoreUnavailable("receipt store is unavailable") from error

    def commit(self) -> None:
        """Commit the whole batch (events + receipt) atomically."""
        try:
            self.session.commit()
        except IntegrityError as error:
            self.session.rollback()
            # A concurrent same-batch commit is handled with ON CONFLICT DO
            # NOTHING, so an integrity error here is a genuine store failure
            # (for example a globally reused event id): never a false accept.
            raise StoreUnavailable(
                "event insert hit a database integrity constraint"
            ) from error
        except SQLAlchemyError as error:
            self.session.rollback()
            raise StoreUnavailable("event store is unavailable") from error

    def rollback(self) -> None:
        """Discard the current transaction."""
        self.session.rollback()

    def get_receipt(self, batch_id: uuid.UUID) -> Optional[BatchReceipt]:
        """Fetch the immutable receipt for a batch, or ``None``."""
        statement = select(TelemetryBatchReceipt).where(
            TelemetryBatchReceipt.batch_id == str(batch_id)
        )
        row = self.session.execute(statement).scalars().first()
        if row is None:
            return None
        from .models import TelemetryBatchAckV1

        return BatchReceipt(
            receipt_id=row.receipt_id,
            batch_id=uuid.UUID(str(row.batch_id)),
            enrollment_id=row.enrollment_id,
            research_session_id=row.research_session_id,
            accepted_at=row.accepted_at,
            ack=TelemetryBatchAckV1.model_validate(row.receipt_json),
        )

    def get_emitter_cursor(
        self, emitter_id: str, research_session_id: Optional[uuid.UUID]
    ) -> Optional[int]:
        """Return the max stored sequence for an emitter, or ``None``.

        The cursor is a pure projection of the event store, so no cursor row is
        persisted: ``max(emitter_sequence)`` for the ``(emitter, session)``.
        """
        statement = select(func.max(ResearchEvent.emitter_sequence)).where(
            ResearchEvent.emitter_id == emitter_id,
            ResearchEvent.research_session_id == research_session_id,
        )
        value = self.session.execute(statement).scalar()
        return int(value) if value is not None else None


def get_receipt_by_id(
    session: Session, receipt_id: uuid.UUID
) -> Optional[BatchReceipt]:
    """Fetch an immutable receipt by its receipt id, or ``None``."""
    statement = select(TelemetryBatchReceipt).where(
        TelemetryBatchReceipt.receipt_id == receipt_id
    )
    row = session.execute(statement).scalars().first()
    if row is None:
        return None
    from .models import TelemetryBatchAckV1

    return BatchReceipt(
        receipt_id=row.receipt_id,
        batch_id=uuid.UUID(str(row.batch_id)),
        enrollment_id=row.enrollment_id,
        research_session_id=row.research_session_id,
        accepted_at=row.accepted_at,
        ack=TelemetryBatchAckV1.model_validate(row.receipt_json),
    )
