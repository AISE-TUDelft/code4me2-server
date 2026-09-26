"""Plain value objects shared by the ledger, pricing and meter modules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

__all__ = [
    "AdjustmentRecord",
    "BalanceView",
    "HoldPlan",
    "ModelPrice",
    "Reservation",
    "SettlementOutcome",
    "UsageSnapshot",
]


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens (== micro-USD per token) for one connection/model."""

    connection_id: Optional[uuid.UUID]
    model: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    cached_input_usd_per_million: Optional[Decimal] = None


@dataclass(frozen=True)
class UsageSnapshot:
    """Token usage read from an upstream response (stream or JSON)."""

    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cached_prompt_tokens: Optional[int] = None
    #: Provider-reported cost in USD (OpenRouter ``usage.cost``); exact when present.
    cost_usd: Optional[Decimal] = None
    finish_reason: Optional[str] = None

    @property
    def has_tokens(self) -> bool:
        return self.prompt_tokens is not None or self.completion_tokens is not None


@dataclass(frozen=True)
class HoldPlan:
    """Worst-case cost of one call and the output cap that bounds it."""

    estimated_prompt_tokens: int
    input_cost_micro_usd: int
    output_cap_tokens: int
    output_cost_micro_usd: int

    @property
    def hold_micro_usd(self) -> int:
        return self.input_cost_micro_usd + self.output_cost_micro_usd


@dataclass(frozen=True)
class Reservation:
    """A committed hold (the ledger row exists and the balance counts it)."""

    reservation_id: uuid.UUID
    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    hold_micro_usd: int
    estimated_prompt_tokens: int
    output_cap_tokens: int
    available_after_micro_usd: int
    deadline_at: datetime


@dataclass(frozen=True)
class SettlementOutcome:
    """Result of settle/forfeit/void: ``applied`` is False for an idempotent no-op."""

    reservation_id: uuid.UUID
    state: str
    charged_micro_usd: int
    applied: bool


@dataclass(frozen=True)
class BalanceView:
    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    unit: str
    limit_micro_usd: int
    settled_micro_usd: int
    reserved_micro_usd: int
    settled_prompt_tokens: int
    settled_completion_tokens: int
    call_count: int
    refused_count: int
    last_call_at: Optional[datetime]
    limit_source: str
    exhausted_at: Optional[datetime]
    updated_at: datetime

    @property
    def available_micro_usd(self) -> int:
        return self.limit_micro_usd - self.settled_micro_usd - self.reserved_micro_usd

    @property
    def exhausted(self) -> bool:
        return self.exhausted_at is not None or self.available_micro_usd <= 0


@dataclass(frozen=True)
class AdjustmentRecord:
    adjustment_id: uuid.UUID
    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    kind: str
    delta_micro_usd: int
    limit_before_micro_usd: int
    limit_after_micro_usd: int
    in_flight_micro_usd: int
    reason: Optional[str]
    actor: Optional[str]
    idempotency_key: Optional[str]
    occurred_at: datetime
    replayed: bool = False
