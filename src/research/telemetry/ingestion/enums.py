"""Closed vocabularies for idempotent telemetry ingestion (Issue 09).

A disposition is terminal for one event: ``REJECTED`` reasons are permanent
(the event must not be retried) while ``RETRYABLE`` reasons are transient.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "EventDisposition",
    "IngestionReasonCode",
    "PERMANENT_REASONS",
    "RETRYABLE_REASONS",
    "is_permanent",
    "is_retryable",
]


class EventDisposition(str, Enum):
    """Terminal per-event acknowledgement disposition."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    RETRYABLE = "RETRYABLE"


class IngestionReasonCode(str, Enum):
    """Stable machine-readable reason for an event disposition."""

    OK = "OK"

    # Permanent (the event must not be retried).
    INVALID_SCHEMA = "INVALID_SCHEMA"
    UNKNOWN_SCHEMA_VERSION = "UNKNOWN_SCHEMA_VERSION"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    MISSING_EVENT_ID = "MISSING_EVENT_ID"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    SESSION_OUT_OF_SCOPE = "SESSION_OUT_OF_SCOPE"
    SESSION_TERMINAL = "SESSION_TERMINAL"
    ENROLLMENT_NOT_ACTIVE = "ENROLLMENT_NOT_ACTIVE"
    REVOKED = "REVOKED"
    INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
    SENSITIVE_PAYLOAD = "SENSITIVE_PAYLOAD"
    PRIVACY_BLOCKED = "PRIVACY_BLOCKED"
    EVENT_SEQUENCE_GAP = "EVENT_SEQUENCE_GAP"
    RECEIPT_CONFLICT = "RECEIPT_CONFLICT"
    DUPLICATE_BATCH = "DUPLICATE_BATCH"
    BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
    EMPTY_BATCH = "EMPTY_BATCH"
    CONTINUITY_REQUIRED = "CONTINUITY_REQUIRED"

    # Transient (safe to retry after re-bootstrap or backoff).
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    # Operator kill switch: new batches are not stored while engaged.
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


# Reason codes that permanently reject an event.
PERMANENT_REASONS = frozenset(
    {
        IngestionReasonCode.INVALID_SCHEMA,
        IngestionReasonCode.UNKNOWN_SCHEMA_VERSION,
        IngestionReasonCode.MISSING_PROVENANCE,
        IngestionReasonCode.MISSING_EVENT_ID,
        IngestionReasonCode.CONTEXT_MISMATCH,
        IngestionReasonCode.SESSION_OUT_OF_SCOPE,
        IngestionReasonCode.SESSION_TERMINAL,
        IngestionReasonCode.ENROLLMENT_NOT_ACTIVE,
        IngestionReasonCode.REVOKED,
        IngestionReasonCode.INTEGRITY_CONFLICT,
        IngestionReasonCode.SENSITIVE_PAYLOAD,
        IngestionReasonCode.PRIVACY_BLOCKED,
        IngestionReasonCode.EVENT_SEQUENCE_GAP,
        IngestionReasonCode.RECEIPT_CONFLICT,
        IngestionReasonCode.DUPLICATE_BATCH,
        IngestionReasonCode.BATCH_TOO_LARGE,
        IngestionReasonCode.EMPTY_BATCH,
        IngestionReasonCode.CONTINUITY_REQUIRED,
    }
)

# Reason codes that permit a later retry.
RETRYABLE_REASONS = frozenset(
    {
        IngestionReasonCode.STORE_UNAVAILABLE,
        IngestionReasonCode.CAPABILITY_INVALID,
        IngestionReasonCode.KILL_SWITCH_ENGAGED,
    }
)


def is_permanent(reason: IngestionReasonCode) -> bool:
    """Whether ``reason`` permanently rejects an event."""
    return reason in PERMANENT_REASONS


def is_retryable(reason: IngestionReasonCode) -> bool:
    """Whether ``reason`` permits a later retry."""
    return reason in RETRYABLE_REASONS
