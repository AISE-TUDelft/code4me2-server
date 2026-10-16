"""Idempotent canonical telemetry ingestion (Issue 09).

Public surface:

* :mod:`research.telemetry.ingestion.enums` - dispositions and typed ingest reason codes.
* :mod:`research.telemetry.ingestion.models` - ``TelemetryBatchRequestV1`` / ``TelemetryBatchAckV1``,
  event records, receipts and the :class:`~research.telemetry.ingestion.models.IngestionStore`
  protocol.
* :mod:`research.telemetry.ingestion.validation` - per-event schema/context/sensitive scan.
* :mod:`research.telemetry.ingestion.service` - :func:`ingest_batch`, the deterministic
  authorize-classify-persist service.
* :mod:`research.telemetry.ingestion.store` - SQLAlchemy persistence and the in-memory
  :class:`~research.telemetry.ingestion.store.FakeIngestionStore` test double.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .enums import (
    PERMANENT_REASONS,
    RETRYABLE_REASONS,
    EventDisposition,
    IngestionReasonCode,
    is_permanent,
    is_retryable,
)
from .errors import IntegrityConflictError, ReceiptConflictError, StoreUnavailable
from .models import (
    BatchReceipt,
    EventAck,
    IngestionContext,
    IngestionIssue,
    IngestionStore,
    ResearchEventRecord,
    TelemetryBatchAckV1,
    TelemetryBatchRequestV1,
)
from .service import CapabilityVerifier, compute_event_digest, ingest_batch
from .store import FakeIngestionStore, SqlAlchemyIngestionStore, row_to_record
from .validation import validate_batch_event

__all__ = [
    "PERMANENT_REASONS",
    "RETRYABLE_REASONS",
    "BatchReceipt",
    "CapabilityVerifier",
    "EventAck",
    "EventDisposition",
    "FakeIngestionStore",
    "IngestionContext",
    "IngestionIssue",
    "IngestionReasonCode",
    "IngestionStore",
    "IntegrityConflictError",
    "ResearchEventRecord",
    "ReceiptConflictError",
    "SqlAlchemyIngestionStore",
    "StoreUnavailable",
    "TelemetryBatchAckV1",
    "TelemetryBatchRequestV1",
    "compute_event_digest",
    "ingest_batch",
    "is_permanent",
    "is_retryable",
    "row_to_record",
    "validate_batch_event",
]
