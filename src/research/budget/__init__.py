"""Participant inference budgets for the shared provider key.

See ``ledger`` for the accounting invariant, ``meter`` for the per-call wrapper
used by the relay endpoints, ``estimate``/``pricing``/``usage`` for the
arithmetic and ``capability`` for the bearer Goose presents to the gateway.
"""

from .capability import (
    INFERENCE_AUDIENCE,
    INFERENCE_SCOPE,
    decode_capability_bearer,
    encode_capability_bearer,
    issue_inference_capability,
    verify_inference_capability,
)
from .errors import (
    AdjustmentInvalid,
    BalanceMissing,
    BudgetError,
    BudgetRefused,
    IdempotencyConflict,
    PriceMissing,
)
from .meter import (
    AVAILABLE_HEADER,
    InferenceMeter,
    format_usd,
    price_missing_response,
    quota_refusal_response,
)
from .models import (
    AdjustmentRecord,
    BalanceView,
    HoldPlan,
    ModelPrice,
    Reservation,
    SettlementOutcome,
    UsageSnapshot,
)
from .pricing import MICRO_USD_PER_USD, cost_micro_usd, get_model_price, usd_to_micro
from .settings import BudgetSettings

__all__ = [
    "AVAILABLE_HEADER",
    "AdjustmentInvalid",
    "AdjustmentRecord",
    "BalanceMissing",
    "BalanceView",
    "BudgetError",
    "BudgetRefused",
    "BudgetSettings",
    "HoldPlan",
    "INFERENCE_AUDIENCE",
    "INFERENCE_SCOPE",
    "IdempotencyConflict",
    "InferenceMeter",
    "MICRO_USD_PER_USD",
    "ModelPrice",
    "PriceMissing",
    "Reservation",
    "SettlementOutcome",
    "UsageSnapshot",
    "cost_micro_usd",
    "decode_capability_bearer",
    "encode_capability_bearer",
    "format_usd",
    "get_model_price",
    "issue_inference_capability",
    "price_missing_response",
    "quota_refusal_response",
    "usd_to_micro",
    "verify_inference_capability",
]
