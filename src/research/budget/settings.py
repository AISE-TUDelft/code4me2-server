"""Tunables for the metered inference relay (read from the environment).

Every knob has a conservative default so a deployment without any of these
variables still enforces budgets. Values are read at call time (like the other
research knobs) so a running backend picks up changes on restart only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

__all__ = ["BudgetSettings"]


def _int(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(environ: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class BudgetSettings:
    """Knobs for hold estimation, output capping and reservation lifetimes."""

    #: Global per-call output cap (tokens); the injected ``max_tokens`` never exceeds it.
    output_token_ceiling: int = 8192
    #: Below this affordable output cap a call is refused instead of sent.
    min_output_tokens: int = 256
    #: Multiplier applied to the byte-based prompt estimate.
    input_estimate_safety: float = 1.5
    #: Estimator divisor (UTF-8 bytes per token; 3 is conservative for code).
    input_bytes_per_token: float = 3.0
    #: Estimator per-message overhead (role/format tokens).
    per_message_token_overhead: int = 4
    #: A hold older than this is EXPIRED (charged in full) by the next reservation.
    reservation_deadline_seconds: int = 900
    #: Wall-clock cap on one streamed response; must stay below the deadline minus the upstream timeout.
    stream_total_timeout_seconds: int = 600
    #: Lifetime of an inference capability handed to Goose (read once per process).
    capability_ttl_seconds: int = 7 * 24 * 3600

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "BudgetSettings":
        env = os.environ if environ is None else environ
        settings = cls(
            output_token_ceiling=_int(env, "INFERENCE_OUTPUT_TOKEN_CEILING", cls.output_token_ceiling),
            min_output_tokens=_int(env, "INFERENCE_MIN_OUTPUT_TOKENS", cls.min_output_tokens),
            input_estimate_safety=_float(
                env, "INFERENCE_INPUT_ESTIMATE_SAFETY", cls.input_estimate_safety, minimum=1.0
            ),
            input_bytes_per_token=_float(
                env, "INFERENCE_INPUT_BYTES_PER_TOKEN", cls.input_bytes_per_token, minimum=0.5
            ),
            per_message_token_overhead=_int(
                env, "INFERENCE_PER_MESSAGE_TOKEN_OVERHEAD", cls.per_message_token_overhead, minimum=0
            ),
            reservation_deadline_seconds=_int(
                env, "INFERENCE_RESERVATION_DEADLINE_SECONDS", cls.reservation_deadline_seconds
            ),
            stream_total_timeout_seconds=_int(
                env, "INFERENCE_STREAM_TOTAL_TIMEOUT_SECONDS", cls.stream_total_timeout_seconds
            ),
            capability_ttl_seconds=_int(
                env, "INFERENCE_CAPABILITY_TTL_SECONDS", cls.capability_ttl_seconds
            ),
        )
        if settings.min_output_tokens > settings.output_token_ceiling:
            raise ValueError(
                "INFERENCE_MIN_OUTPUT_TOKENS must not exceed INFERENCE_OUTPUT_TOKEN_CEILING"
            )
        return settings
