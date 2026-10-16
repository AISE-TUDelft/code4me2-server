"""Usage extraction that is safe for OpenAI *and* OpenRouter shapes.

OpenAI's ``stream_options.include_usage`` sends a final chunk with
``choices: []`` and a ``usage`` object; OpenRouter's final chunk carries
``usage`` (with ``cost``) next to a choice with an empty delta. The rule here
is shape-agnostic: the last chunk whose ``usage`` is an object wins. An error
object inside a 200 stream (OpenRouter) or a body without usage yields
``None`` so the caller forfeits the hold instead of guessing.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .models import UsageSnapshot

__all__ = ["extract_usage_from_json", "extract_usage_from_sse", "usage_from_object"]


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def usage_from_object(usage: Any, *, finish_reason: Optional[str] = None) -> Optional[UsageSnapshot]:
    if not isinstance(usage, dict):
        return None
    prompt = _int_or_none(usage.get("prompt_tokens"))
    completion = _int_or_none(usage.get("completion_tokens"))
    if prompt is None and completion is None:
        return None
    details = usage.get("prompt_tokens_details")
    cached = _int_or_none(details.get("cached_tokens")) if isinstance(details, dict) else None
    return UsageSnapshot(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_prompt_tokens=cached,
        cost_usd=_decimal_or_none(usage.get("cost")),
        finish_reason=finish_reason,
    )


def _finish_reason(payload: dict) -> Optional[str]:
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                return str(choice["finish_reason"])
    return None


def extract_usage_from_json(body: Any) -> Optional[UsageSnapshot]:
    """Usage from a non-streaming Chat Completions body (``None`` when absent)."""
    if not isinstance(body, dict):
        return None
    if "error" in body and "usage" not in body:
        return None
    return usage_from_object(body.get("usage"), finish_reason=_finish_reason(body))


def extract_usage_from_sse(raw_sse: str) -> Optional[UsageSnapshot]:
    """Usage from a buffered SSE stream; the last usage-bearing chunk wins."""
    if not raw_sse:
        return None
    usage: Optional[UsageSnapshot] = None
    finish_reason: Optional[str] = None
    saw_error = False
    for line in raw_sse.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("error"), dict):
            saw_error = True
        reason = _finish_reason(chunk)
        if reason:
            finish_reason = reason
        found = usage_from_object(chunk.get("usage"), finish_reason=finish_reason)
        if found is not None:
            usage = found
    if usage is None:
        return None
    if saw_error and not usage.has_tokens:
        return None
    if usage.finish_reason is None and finish_reason is not None:
        usage = UsageSnapshot(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            cost_usd=usage.cost_usd,
            finish_reason=finish_reason,
        )
    return usage
