"""Worst-case hold planning and output capping for one Chat Completions call.

The prompt estimate is tokenizer-free on purpose (no network tokenizer, no
model-specific vocabularies): UTF-8 bytes divided by a conservative
bytes-per-token constant plus per-message overhead, times a safety factor.
Over-estimating only makes a call near exhaustion fail a little early; the
settled charge always uses the provider's real token counts.
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional

from .errors import BudgetRefused
from .models import HoldPlan, ModelPrice
from .pricing import cost_micro_usd
from .settings import BudgetSettings

__all__ = [
    "apply_output_cap",
    "client_output_cap",
    "estimate_prompt_tokens",
    "minimal_call_micro_usd",
    "plan_hold",
    "upstream_kind",
]


def _utf8_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8", errors="replace"))


def estimate_prompt_tokens(openai_body: dict, settings: BudgetSettings) -> int:
    """Conservative token estimate for the prompt side of ``openai_body``."""
    messages = openai_body.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    tools = openai_body.get("tools") or []
    message_bytes = sum(_utf8_bytes(message) for message in messages)
    tool_bytes = _utf8_bytes(tools) if tools else 0
    raw = (
        message_bytes / settings.input_bytes_per_token
        + len(messages) * settings.per_message_token_overhead
        + tool_bytes / settings.input_bytes_per_token
        + 8
    )
    return int(math.ceil(raw * settings.input_estimate_safety))


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and value.is_integer():
        return int(value)
    return None


def client_output_cap(openai_body: dict) -> Optional[int]:
    """The smaller of the client's ``max_tokens`` / ``max_completion_tokens``, if any."""
    caps = [
        cap
        for cap in (
            _positive_int(openai_body.get("max_tokens")),
            _positive_int(openai_body.get("max_completion_tokens")),
        )
        if cap is not None
    ]
    return min(caps) if caps else None


def minimal_call_micro_usd(price: ModelPrice, settings: BudgetSettings) -> int:
    """What the cheapest sensible call costs: a tiny prompt plus the minimum output."""
    return cost_micro_usd(64, price.input_usd_per_million) + cost_micro_usd(
        settings.min_output_tokens, price.output_usd_per_million
    )


def plan_hold(
    price: ModelPrice,
    *,
    available_micro_usd: int,
    estimated_prompt_tokens: int,
    client_cap: Optional[int],
    settings: BudgetSettings,
) -> HoldPlan:
    """Choose the output cap the budget affords and the hold that bounds the call.

    Raises :class:`BudgetRefused` when even ``min_output_tokens`` of output
    would not fit after the estimated input cost.
    """
    input_cost = cost_micro_usd(estimated_prompt_tokens, price.input_usd_per_million)
    remaining = int(available_micro_usd) - input_cost
    output_price = price.output_usd_per_million
    if output_price <= 0:
        affordable = settings.output_token_ceiling
    elif remaining <= 0:
        affordable = 0
    else:
        affordable = int(remaining // output_price)  # floor(micro-USD / micro-USD-per-token)
    cap = min(settings.output_token_ceiling, affordable)
    # The smallest useful completion: the configured minimum, or less when the
    # client itself asked for a shorter answer (a small max_tokens is a valid
    # request, not a sign of an exhausted budget).
    minimum = settings.min_output_tokens
    if client_cap is not None:
        cap = min(cap, client_cap)
        minimum = min(minimum, client_cap)
    if cap < minimum:
        needed = input_cost + cost_micro_usd(minimum, output_price)
        raise BudgetRefused(available_micro_usd=int(available_micro_usd), needed_micro_usd=needed)
    return HoldPlan(
        estimated_prompt_tokens=int(estimated_prompt_tokens),
        input_cost_micro_usd=input_cost,
        output_cap_tokens=int(cap),
        output_cost_micro_usd=cost_micro_usd(int(cap), output_price),
    )


def upstream_kind(base_url: Optional[str]) -> str:
    """``openai`` for api.openai.com, ``openrouter`` for openrouter.ai, else ``other``."""
    host = (base_url or "").lower()
    if "api.openai.com" in host:
        return "openai"
    if "openrouter.ai" in host:
        return "openrouter"
    return "other"


def apply_output_cap(openai_body: dict, cap: int, *, upstream_base_url: Optional[str]) -> None:
    """Bound the completion in place so the provider cannot exceed the hold.

    api.openai.com rejects ``max_tokens`` for reasoning models, so it gets
    ``max_completion_tokens`` only; every other OpenAI-compatible host gets
    ``max_tokens`` (and a client-sent ``max_completion_tokens`` is capped too).
    OpenRouter additionally receives ``usage: {include: true}`` so the settled
    charge can use its reported cost. Multiple completions are never allowed.
    """
    cap = int(cap)
    kind = upstream_kind(upstream_base_url)
    if kind == "openai":
        openai_body.pop("max_tokens", None)
        openai_body["max_completion_tokens"] = cap
    else:
        openai_body["max_tokens"] = cap
        if "max_completion_tokens" in openai_body:
            existing = _positive_int(openai_body.get("max_completion_tokens"))
            openai_body["max_completion_tokens"] = min(existing, cap) if existing else cap
    if kind == "openrouter":
        usage = openai_body.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        usage["include"] = True
        openai_body["usage"] = usage
    openai_body["n"] = 1
    openai_body.pop("best_of", None)
