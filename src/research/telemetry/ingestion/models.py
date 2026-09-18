"""Pydantic v2 contracts for idempotent telemetry ingestion (Issue 09).

``TelemetryBatchRequestV1`` carries already-privacy-filtered canonical events
plus a scoped session capability. ``TelemetryBatchAckV1`` groups event ids by
disposition; an event id never appears in two groups, and a missing
acknowledgement is never treated as accepted.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Optional, Protocol, Sequence
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research.runtime.bootstrap.models import (
    SessionCapability,  # noqa: TC001 - pydantic resolves model annotations at runtime
)
from research.telemetry.models import (
    CanonicalEventV1,  # noqa: TC001 - pydantic resolves model annotations at runtime
)

from .enums import (
    EventDisposition,  # noqa: TC001 - pydantic resolves model field annotations at runtime
    IngestionReasonCode,  # noqa: TC001 - pydantic resolves model field annotations at runtime
)

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BASE = ConfigDict(extra="forbid")


class TelemetryBatchRequestV1(BaseModel):
    """One upload request from the durable local spool."""

    model_config = _BASE

    batch_id: UUID
    protocol_version: str = "1"
    telemetry_schema_version: str = "1"
    session_capability: SessionCapability
    events: list[CanonicalEventV1] = Field(default_factory=list)
    client_instance_id: str
    previous_ack_cursor: Optional[str] = None

    @property
    def size(self) -> int:
        """Number of events in the batch."""
        return len(self.events)


class EventAck(BaseModel):
    """Per-event acknowledgement."""

    model_config = _BASE

    event_id: Optional[UUID] = None
    disposition: EventDisposition
    reason: Optional[IngestionReasonCode] = None
    retry_hint: Optional[int] = None
    stored_digest: Optional[str] = None


class TelemetryBatchAckV1(BaseModel):
    """Durable batch acknowledgement, safe to retry and deterministic per event."""

    model_config = _BASE

    receipt_id: UUID
    batch_id: UUID
    server_time: datetime
    accepted: list[EventAck] = Field(default_factory=list)
    duplicate: list[EventAck] = Field(default_factory=list)
    rejected: list[EventAck] = Field(default_factory=list)
    retryable: list[EventAck] = Field(default_factory=list)
    retry_hint: Optional[int] = None
    # Coverage diagnostics (e.g. per-emitter sequence gaps) keyed by event id.
    coverage_diagnostics: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_event_ids(self) -> TelemetryBatchAckV1:
        seen: set[UUID] = set()
        for group in (self.accepted, self.duplicate, self.rejected, self.retryable):
            for ack in group:
                if ack.event_id is None:
                    continue
                if ack.event_id in seen:
                    raise ValueError(
                        f"event {ack.event_id} appears in more than one ack group"
                    )
                seen.add(ack.event_id)
        return self

    def acknowledged_ids(self) -> list[UUID]:
        """Ids the spool may delete: accepted + duplicate."""
        return [ack.event_id for ack in self.accepted + self.duplicate if ack.event_id]

    @property
    def is_empty(self) -> bool:
        """Whether the acknowledgement carries no per-event dispositions."""
        return not (self.accepted or self.duplicate or self.rejected or self.retryable)


class IngestionContext(BaseModel):
    """Authorized, resolved context every accepted event is bound to.

    Every identity field is derived from the authorized enrollment/session, never
    from the client event payload.
    """

    model_config = _FROZEN

    study_id: UUID
    enrollment_id: UUID
    study_revision_id: UUID
    research_session_id: UUID
    revocation_epoch: int = 0


class IngestionIssue(BaseModel):
    """One typed validation finding for a batch event."""

    model_config = _BASE

    code: IngestionReasonCode
    field: str = ""
    message: str = ""
    permanent: bool = True


class ResearchEventRecord(BaseModel):
    """One immutable accepted canonical event fact.

    ``envelope`` is the complete validated ``CanonicalEventV1`` document and is
    the event authority: it preserves correlations, monotonic time, lifecycle,
    unknown-value fields, provenance, coverage and the privacy summary. The
    remaining fields are the searchable scalar columns derived from that same
    envelope in the same transaction (identity/ordering for indexes and joins);
    they are never a second, independently writable copy of the content.
    Ingestion metadata (``digest``, ``accepted_at``, ``retention_state``) lives
    outside the envelope.
    """

    model_config = _FROZEN

    event_id: UUID
    schema_version: str
    event_type: str
    source: str
    study_id: Optional[UUID] = None
    revision_id: Optional[UUID] = None
    enrollment_id: Optional[UUID] = None
    research_session_id: Optional[UUID] = None
    agent_run_id: Optional[str] = None
    emitter_id: str
    emitter_sequence: int
    occurred_at: datetime
    # The complete validated canonical envelope (the authority).
    envelope: dict[str, Any] = Field(default_factory=dict)
    digest: str
    accepted_at: datetime
    retention_state: str = "RETAINED"


class BatchReceipt(BaseModel):
    """An immutable, retry-safe batch acknowledgement record."""

    model_config = _FROZEN

    receipt_id: UUID
    batch_id: UUID
    enrollment_id: Optional[UUID] = None
    research_session_id: Optional[UUID] = None
    accepted_at: datetime
    ack: TelemetryBatchAckV1


class IngestionStore(Protocol):
    """The persistence surface the ingestion service depends on.

    The service owns the transaction boundary: ``insert_events`` and
    ``record_batch_receipt`` stage their writes, and ``commit``/``rollback``
    decide whether the whole batch (accepted events + immutable receipt) becomes
    durable. A batch therefore commits in full or not at all.
    """

    def get_stored_digests(
        self, event_ids: Sequence[UUID]
    ) -> dict[UUID, str]:  # pragma: no cover - structural protocol
        ...

    def insert_events(
        self, records: Sequence[ResearchEventRecord]
    ) -> list[UUID]:  # pragma: no cover - structural protocol
        ...

    def record_batch_receipt(
        self, receipt: BatchReceipt
    ) -> None:  # pragma: no cover - structural protocol
        ...

    def get_receipt(
        self, batch_id: UUID
    ) -> Optional[BatchReceipt]:  # pragma: no cover - structural protocol
        ...

    def get_emitter_cursor(
        self, emitter_id: str, research_session_id: Optional[UUID]
    ) -> Optional[int]:  # pragma: no cover - structural protocol
        ...

    def commit(self) -> None:  # pragma: no cover - structural protocol
        ...

    def rollback(self) -> None:  # pragma: no cover - structural protocol
        ...
