"""Prices and charges. USD per million tokens equals micro-USD per token."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import select

from database.db_schemas import ProviderModelPrice

from .errors import PriceMissing
from .models import ModelPrice, UsageSnapshot

__all__ = [
    "MAX_USD_AMOUNT",
    "MICRO_USD_PER_USD",
    "charge_for_usage",
    "cost_micro_usd",
    "get_model_price",
    "parse_usd_amount",
    "parse_usd_per_million",
    "price_from_row",
    "usd_to_micro",
]

MICRO_USD_PER_USD = Decimal(1_000_000)
#: Upper bound for any single budget amount or price (sanity, not policy).
MAX_USD_AMOUNT = Decimal("100000")


def _parse_decimal(value, *, what: str, maximum: Decimal) -> Decimal:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{what} is required")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{what} must be a decimal number") from exc
    if not amount.is_finite():
        raise ValueError(f"{what} must be a finite number")
    if amount < 0:
        raise ValueError(f"{what} must not be negative")
    if amount > maximum:
        raise ValueError(f"{what} must not exceed {maximum}")
    if amount.as_tuple().exponent < -6:
        raise ValueError(f"{what} may have at most 6 decimal places")
    return amount


def parse_usd_amount(value) -> Decimal:
    """A USD budget amount from the API boundary (string in, Decimal out)."""
    return _parse_decimal(value, what="amount", maximum=MAX_USD_AMOUNT)


def parse_usd_per_million(value) -> Decimal:
    """A price in USD per million tokens from the API boundary."""
    return _parse_decimal(value, what="price", maximum=Decimal("10000"))


def usd_to_micro(value: Decimal | str | int | float) -> int:
    """Convert a USD amount to integer micro-USD, rounding up."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a decimal amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return int(math.ceil(amount * MICRO_USD_PER_USD))


def cost_micro_usd(tokens: Optional[int], usd_per_million: Decimal) -> int:
    """``ceil(tokens * price)``; zero for missing/non-positive token counts."""
    if tokens is None or tokens <= 0:
        return 0
    return int(math.ceil(Decimal(int(tokens)) * Decimal(usd_per_million)))


def price_from_row(row: Any) -> ModelPrice:
    return ModelPrice(
        connection_id=getattr(row, "connection_id", None),
        model=str(row.model),
        input_usd_per_million=Decimal(row.input_usd_per_million),
        output_usd_per_million=Decimal(row.output_usd_per_million),
        cached_input_usd_per_million=(
            None
            if row.cached_input_usd_per_million is None
            else Decimal(row.cached_input_usd_per_million)
        ),
    )


def get_model_price(db, connection_id: Optional[uuid.UUID], model: str) -> ModelPrice:
    """Return the price row for ``(connection_id, model)`` or raise :class:`PriceMissing`.

    There is deliberately no default price: a metered model without a price
    cannot be held or settled, so the call is refused before anything is sent.
    """
    if connection_id is None or not model:
        raise PriceMissing(connection_id, model)
    row = db.execute(
        select(ProviderModelPrice).where(
            ProviderModelPrice.connection_id == connection_id,
            ProviderModelPrice.model == model,
        )
    ).scalars().first()
    if row is None:
        raise PriceMissing(connection_id, model)
    return price_from_row(row)


def charge_for_usage(price: ModelPrice, usage: UsageSnapshot) -> tuple[int, str]:
    """Return ``(charged_micro_usd, usage_source)`` for settled usage.

    A provider-reported cost (OpenRouter ``usage.cost``) is exact and wins.
    Otherwise cached prompt tokens bill at the cached price (or the input price
    when none is configured) and completion tokens (which already include
    reasoning tokens on OpenAI/OpenRouter) bill at the output price.
    """
    if usage.cost_usd is not None:
        return usd_to_micro(usage.cost_usd), "provider_cost"
    prompt = int(usage.prompt_tokens or 0)
    cached = min(int(usage.cached_prompt_tokens or 0), prompt)
    uncached = max(prompt - cached, 0)
    cached_price = (
        price.cached_input_usd_per_million
        if price.cached_input_usd_per_million is not None
        else price.input_usd_per_million
    )
    charged = (
        cost_micro_usd(uncached, price.input_usd_per_million)
        + cost_micro_usd(cached, cached_price)
        + cost_micro_usd(usage.completion_tokens, price.output_usd_per_million)
    )
    return charged, "price_table"
