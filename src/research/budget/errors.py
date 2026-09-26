"""Typed failures of the budget ledger."""

from __future__ import annotations

import uuid
from typing import Optional

__all__ = [
    "AdjustmentInvalid",
    "BalanceMissing",
    "BudgetError",
    "BudgetRefused",
    "IdempotencyConflict",
    "PriceMissing",
]


class BudgetError(Exception):
    """Base class for ledger failures."""


class PriceMissing(BudgetError):
    """The frozen model has no price row on its connection (fail closed)."""

    def __init__(self, connection_id: Optional[uuid.UUID], model: str) -> None:
        super().__init__(f"no price configured for model {model!r} on connection {connection_id}")
        self.connection_id = connection_id
        self.model = model


class BalanceMissing(BudgetError):
    """No balance row exists for the enrollment (and it could not be created)."""

    def __init__(self, enrollment_id: uuid.UUID) -> None:
        super().__init__(f"no inference balance for enrollment {enrollment_id}")
        self.enrollment_id = enrollment_id


class BudgetRefused(BudgetError):
    """The worst-case hold does not fit the available budget."""

    def __init__(self, *, available_micro_usd: int, needed_micro_usd: int) -> None:
        super().__init__(
            f"budget refused: available {available_micro_usd} micro-USD, "
            f"needed {needed_micro_usd} micro-USD"
        )
        self.available_micro_usd = int(available_micro_usd)
        self.needed_micro_usd = int(needed_micro_usd)


class IdempotencyConflict(BudgetError):
    """An idempotency key was reused with a different request body."""


class AdjustmentInvalid(BudgetError):
    """An adjustment amount/kind is not acceptable."""
