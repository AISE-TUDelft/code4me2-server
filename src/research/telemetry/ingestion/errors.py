"""Typed errors for ingestion store failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

__all__ = [
    "IntegrityConflictError",
    "ReceiptConflictError",
    "StoreUnavailable",
]


class StoreUnavailable(RuntimeError):
    """Raised when the event store is transiently unavailable.

    The caller must return a retryable acknowledgement, never a false accept.
    """


class ReceiptConflictError(RuntimeError):
    """Raised when a concurrent commit already stored a receipt for the batch.

    The batch id is unique, so exactly one commit wins. The loser must discard
    its own staged writes and return the winner's immutable receipt.
    """

    def __init__(self, batch_id: "UUID") -> None:
        super().__init__(f"batch {batch_id} already has a stored receipt")
        self.batch_id = batch_id


class IntegrityConflictError(RuntimeError):
    """Raised when an event id already exists with a different digest."""

    def __init__(
        self, event_id: UUID, stored_digest: str, presented_digest: str
    ) -> None:
        super().__init__(
            f"integrity conflict for event {event_id}: stored digest differs from "
            "the presented digest"
        )
        self.event_id = event_id
        self.stored_digest = stored_digest
        self.presented_digest = presented_digest
