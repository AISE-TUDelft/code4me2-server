"""Pure tests for the budget arithmetic: settings, estimate, capping, usage, pricing, bearer."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from research.budget import (
    BudgetRefused,
    BudgetSettings,
    ModelPrice,
    UsageSnapshot,
    decode_capability_bearer,
    encode_capability_bearer,
    issue_inference_capability,
    verify_inference_capability,
)
from research.budget.estimate import (
    apply_output_cap,
    client_output_cap,
    estimate_prompt_tokens,
    plan_hold,
    upstream_kind,
)
from research.budget.meter import format_usd, quota_refusal_response
from research.budget.pricing import charge_for_usage, cost_micro_usd, usd_to_micro
from research.budget.usage import extract_usage_from_json, extract_usage_from_sse
from research.runtime.bootstrap.models import CapabilityReasonCode

SETTINGS = BudgetSettings()
PRICE = ModelPrice(
    connection_id=uuid.uuid4(),
    model="test/model",
    input_usd_per_million=Decimal("1.0"),  # 1 micro-USD per token
    output_usd_per_million=Decimal("4.0"),  # 4 micro-USD per token
    cached_input_usd_per_million=Decimal("0.5"),
)


# --------------------------------------------------------------------------- settings


def test_settings_defaults_and_env_overrides():
    assert BudgetSettings.from_env({}) == BudgetSettings()
    custom = BudgetSettings.from_env(
        {"INFERENCE_OUTPUT_TOKEN_CEILING": "4096", "INFERENCE_INPUT_ESTIMATE_SAFETY": "2"}
    )
    assert custom.output_token_ceiling == 4096
    assert custom.input_estimate_safety == 2.0


@pytest.mark.parametrize(
    "environ",
    [
        {"INFERENCE_OUTPUT_TOKEN_CEILING": "abc"},
        {"INFERENCE_MIN_OUTPUT_TOKENS": "0"},
        {"INFERENCE_INPUT_ESTIMATE_SAFETY": "0.5"},
        {"INFERENCE_MIN_OUTPUT_TOKENS": "9000", "INFERENCE_OUTPUT_TOKEN_CEILING": "8192"},
    ],
)
def test_settings_reject_invalid_values(environ):
    with pytest.raises(ValueError):
        BudgetSettings.from_env(environ)


# --------------------------------------------------------------------------- estimate


def test_estimate_grows_with_bytes_tools_and_safety():
    short = {"messages": [{"role": "user", "content": "hi"}]}
    long = {"messages": [{"role": "user", "content": "x" * 3000}]}
    with_tools = {**long, "tools": [{"type": "function", "function": {"name": "t", "parameters": {"a": "b" * 300}}}]}
    assert estimate_prompt_tokens(short, SETTINGS) < estimate_prompt_tokens(long, SETTINGS)
    assert estimate_prompt_tokens(long, SETTINGS) < estimate_prompt_tokens(with_tools, SETTINGS)
    # 3000 bytes / 3 per token = 1000 tokens (+overheads), times the 1.5 safety factor.
    assert estimate_prompt_tokens(long, SETTINGS) >= 1500
    doubled = BudgetSettings(input_estimate_safety=3.0)
    assert estimate_prompt_tokens(long, doubled) >= 2 * estimate_prompt_tokens(long, SETTINGS) - 2


def test_estimate_tolerates_odd_bodies():
    assert estimate_prompt_tokens({}, SETTINGS) > 0
    assert estimate_prompt_tokens({"messages": "not a list"}, SETTINGS) > 0
    assert estimate_prompt_tokens({"messages": [{"content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}]}, SETTINGS) > 0


def test_client_output_cap_takes_the_smaller_positive_value():
    assert client_output_cap({}) is None
    assert client_output_cap({"max_tokens": 100}) == 100
    assert client_output_cap({"max_tokens": 100, "max_completion_tokens": 40}) == 40
    assert client_output_cap({"max_tokens": -1, "max_completion_tokens": True}) is None


# --------------------------------------------------------------------------- plan_hold


def test_plan_hold_bounds_output_by_budget_and_ceiling():
    # available 10_000 micro-USD; input 1000 tokens => 1000; remaining 9000 / 4 = 2250 output tokens
    plan = plan_hold(PRICE, available_micro_usd=10_000, estimated_prompt_tokens=1000, client_cap=None, settings=SETTINGS)
    assert plan.output_cap_tokens == 2250
    assert plan.hold_micro_usd == 1000 + 2250 * 4
    rich = plan_hold(PRICE, available_micro_usd=10**9, estimated_prompt_tokens=1000, client_cap=None, settings=SETTINGS)
    assert rich.output_cap_tokens == SETTINGS.output_token_ceiling
    capped = plan_hold(PRICE, available_micro_usd=10**9, estimated_prompt_tokens=1000, client_cap=300, settings=SETTINGS)
    assert capped.output_cap_tokens == 300


def test_plan_hold_refuses_when_minimum_output_is_unaffordable():
    with pytest.raises(BudgetRefused) as refused:
        plan_hold(PRICE, available_micro_usd=1500, estimated_prompt_tokens=1000, client_cap=None, settings=SETTINGS)
    assert refused.value.available_micro_usd == 1500
    assert refused.value.needed_micro_usd == 1000 + SETTINGS.min_output_tokens * 4
    # A client asking for a short answer is a valid request, not exhaustion...
    short = plan_hold(PRICE, available_micro_usd=10**9, estimated_prompt_tokens=10, client_cap=100, settings=SETTINGS)
    assert short.output_cap_tokens == 100
    # ...unless even that short answer is unaffordable.
    with pytest.raises(BudgetRefused) as tiny:
        plan_hold(PRICE, available_micro_usd=10 + 50 * 4, estimated_prompt_tokens=10, client_cap=100, settings=SETTINGS)
    assert tiny.value.needed_micro_usd == 10 + 100 * 4


def test_plan_hold_with_free_output_uses_the_ceiling():
    free = ModelPrice(None, "m", Decimal("1"), Decimal("0"))
    plan = plan_hold(free, available_micro_usd=100, estimated_prompt_tokens=10, client_cap=None, settings=SETTINGS)
    assert plan.output_cap_tokens == SETTINGS.output_token_ceiling
    assert plan.hold_micro_usd == 10


# --------------------------------------------------------------------------- apply_output_cap


def test_apply_output_cap_per_host():
    assert upstream_kind("https://api.openai.com/v1") == "openai"
    assert upstream_kind("https://openrouter.ai/api/v1") == "openrouter"
    assert upstream_kind("https://llm.example.org/v1") == "other"

    body = {"max_tokens": 5000, "n": 3, "best_of": 2}
    apply_output_cap(body, 1200, upstream_base_url="https://api.openai.com/v1")
    assert body == {"max_completion_tokens": 1200, "n": 1}

    body = {"max_completion_tokens": 9000, "usage": {"other": 1}}
    apply_output_cap(body, 1200, upstream_base_url="https://openrouter.ai/api/v1")
    assert body["max_tokens"] == 1200
    assert body["max_completion_tokens"] == 1200
    assert body["usage"] == {"other": 1, "include": True}
    assert body["n"] == 1

    body = {}
    apply_output_cap(body, 700, upstream_base_url="https://llm.example.org/v1")
    assert body == {"max_tokens": 700, "n": 1}


# --------------------------------------------------------------------------- usage


def _sse(*chunks: str) -> str:
    return "".join(f"data: {chunk}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def test_usage_from_openai_final_chunk_with_empty_choices():
    raw = _sse(
        '{"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        '{"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15,"prompt_tokens_details":{"cached_tokens":4}}}',
    )
    usage = extract_usage_from_sse(raw)
    assert usage == UsageSnapshot(prompt_tokens=12, completion_tokens=3, cached_prompt_tokens=4, cost_usd=None, finish_reason="stop")


def test_usage_from_openrouter_final_chunk_with_choice_and_cost():
    raw = _sse(
        '{"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":20,"completion_tokens":5,"total_tokens":25,"cost":0.00123}}',
    )
    usage = extract_usage_from_sse(raw)
    assert usage.prompt_tokens == 20 and usage.completion_tokens == 5
    assert usage.cost_usd == Decimal("0.00123")
    assert usage.finish_reason == "stop"


def test_usage_missing_or_error_yields_none():
    assert extract_usage_from_sse("") is None
    assert extract_usage_from_sse(_sse('{"choices":[{"delta":{"content":"partial"}}]}')) is None
    assert extract_usage_from_sse(_sse('{"error":{"message":"boom","code":500}}')) is None
    assert extract_usage_from_sse("data: not json\n\n") is None
    assert extract_usage_from_json({"error": {"message": "x"}}) is None
    assert extract_usage_from_json({"choices": [], "usage": "nope"}) is None
    assert extract_usage_from_json(None) is None


def test_usage_from_json_body():
    usage = extract_usage_from_json(
        {"choices": [{"finish_reason": "length"}], "usage": {"prompt_tokens": 7, "completion_tokens": 9}}
    )
    assert usage == UsageSnapshot(7, 9, None, None, "length")


# --------------------------------------------------------------------------- pricing


def test_cost_rounds_up_and_ignores_missing_tokens():
    assert cost_micro_usd(None, Decimal("1")) == 0
    assert cost_micro_usd(0, Decimal("1")) == 0
    assert cost_micro_usd(3, Decimal("0.4")) == 2  # 1.2 -> 2
    assert usd_to_micro("0.0000011") == 2
    assert usd_to_micro(Decimal("1.5")) == 1_500_000
    with pytest.raises(ValueError):
        usd_to_micro("abc")


def test_charge_for_usage_prefers_provider_cost_then_price_table():
    charged, source = charge_for_usage(PRICE, UsageSnapshot(100, 10, cost_usd=Decimal("0.001")))
    assert (charged, source) == (1000, "provider_cost")
    charged, source = charge_for_usage(PRICE, UsageSnapshot(100, 10, cached_prompt_tokens=40))
    # 60 uncached * 1 + 40 cached * 0.5 + 10 output * 4
    assert (charged, source) == (60 + 20 + 40, "price_table")
    no_cached_price = ModelPrice(None, "m", Decimal("1"), Decimal("4"), None)
    charged, _ = charge_for_usage(no_cached_price, UsageSnapshot(100, 10, cached_prompt_tokens=40))
    assert charged == 100 + 40


def test_refusal_response_shape_and_formatting():
    response = quota_refusal_response(BudgetRefused(available_micro_usd=120_000, needed_micro_usd=450_000))
    assert response.status_code == 402
    body = response.body.decode()
    assert '"type": "insufficient_quota"' in body
    assert '"code": "quota_exhausted"' in body
    assert "$0.12" in body and "$0.45" in body
    from research.budget.meter import budget_unconfigured_response, price_missing_response

    unpriced = price_missing_response().body.decode()
    # Participant-facing: the arm's model name never appears (only the generic word "model").
    assert '"code": "price_missing"' in unpriced and "model" in unpriced and "goose" not in unpriced
    unconfigured = budget_unconfigured_response().body.decode()
    assert '"code": "quota_exhausted"' in unconfigured and "$" not in unconfigured
    assert format_usd(5) == "$0.0000" or format_usd(5).startswith("$0.00")
    assert format_usd(2_500_000) == "$2.50"


# --------------------------------------------------------------------------- capability bearer


def test_inference_capability_bearer_round_trip_and_verification():
    secret = "unit-test-secret"
    enrollment_id, session_id, study_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    capability = issue_inference_capability(
        secret=secret, ttl_seconds=3600, revocation_epoch=2, enrollment_id=enrollment_id,
        research_session_id=session_id, study_id=study_id, now=now,
    )
    token = encode_capability_bearer(capability)
    assert "=" not in token and token.isascii()
    decoded = decode_capability_bearer("Bearer " + token)
    assert decoded == capability
    verified = verify_inference_capability(
        decoded, secret=secret, current_revocation_epoch=2, expected_enrollment_id=enrollment_id,
        expected_study_id=study_id, now=now + timedelta(seconds=10),
    )
    assert verified.ok, verified.message
    stale = verify_inference_capability(
        decoded, secret=secret, current_revocation_epoch=3, expected_enrollment_id=enrollment_id,
        expected_study_id=study_id, now=now,
    )
    assert stale.reason == CapabilityReasonCode.REVOKED
    wrong_subject = verify_inference_capability(
        decoded, secret=secret, current_revocation_epoch=2, expected_enrollment_id=uuid.uuid4(),
        expected_study_id=study_id, now=now,
    )
    assert not wrong_subject.ok
    expired = verify_inference_capability(
        decoded, secret=secret, current_revocation_epoch=2, expected_enrollment_id=enrollment_id,
        expected_study_id=study_id, now=now + timedelta(seconds=3601),
    )
    assert expired.reason == CapabilityReasonCode.EXPIRED


def test_tampered_or_malformed_bearers_are_rejected():
    secret = "unit-test-secret"
    capability = issue_inference_capability(
        secret=secret, ttl_seconds=60, revocation_epoch=0, enrollment_id=uuid.uuid4(),
        research_session_id=uuid.uuid4(), study_id=uuid.uuid4(),
    )
    tampered = capability.model_copy(update={"scope": ["inference:relay", "telemetry:write"]})
    decoded = decode_capability_bearer(encode_capability_bearer(tampered))
    assert decoded is not None
    verified = verify_inference_capability(
        decoded, secret=secret, current_revocation_epoch=0,
        expected_enrollment_id=capability.enrollment_id, expected_study_id=capability.study_id,
    )
    assert verified.reason == CapabilityReasonCode.SIGNATURE_MISMATCH
    session_audience = issue_inference_capability(
        secret=secret, ttl_seconds=60, revocation_epoch=0, enrollment_id=capability.enrollment_id,
        research_session_id=capability.research_session_id, study_id=capability.study_id,
    ).model_copy(update={"audience": "research-runtime"})
    assert decode_capability_bearer("") is None
    assert decode_capability_bearer("sk-notacapability") is None
    assert decode_capability_bearer("!!!!") is None
    assert decode_capability_bearer(encode_capability_bearer(session_audience)) is not None
