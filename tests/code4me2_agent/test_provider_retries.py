from __future__ import annotations

from threading import Event

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from code4me2_agent.adapters import (
    OpenAICompatibleProvider,
    ProviderCancelled,
    ProviderRequestFailed,
    RetryPolicy,
    _call_with_retries,
    _classify_managed_error,
    _classify_sdk_error,
    _normalize_openai_provider_response,
    _normalize_usage,
    _usage_is_reported,
)
from code4me2_agent.runtime_auth import AcpAuthorizationFailure, AcpSessionExpired

FAST = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=20.0, max_total_wait=60.0)


def _status_error(status: int, headers: dict[str, str] | None = None) -> APIStatusError:
    response = httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("POST", "http://model.test/v1/chat/completions"),
        text="upstream said no",
    )
    return APIStatusError("boom", response=response, body=None)


def _sequence(*outcomes):
    calls = {"count": 0}

    def call():
        outcome = outcomes[calls["count"]]
        calls["count"] += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return call, calls


def test_retries_retryable_statuses_with_backoff_and_retry_after():
    call, calls = _sequence(_status_error(503), _status_error(429, {"retry-after": "0"}), {"ok": True})
    sleeps: list[float] = []

    result = _call_with_retries(
        call,
        policy=FAST,
        classify=_classify_sdk_error(FAST),
        sleep_fn=sleeps.append,
        rand=lambda: 1.0,
    )

    assert result == {"ok": True}
    assert calls["count"] == 3
    # Attempt 1 backs off by the base delay (jitter fixed at its maximum), the
    # 429 honours Retry-After: 0; sleeps happen in 0.25 s slices.
    assert sum(sleeps[:4]) == pytest.approx(1.0)
    assert sum(sleeps) == pytest.approx(1.0)


def test_does_not_retry_client_errors():
    for status in (400, 403):
        call, calls = _sequence(_status_error(status), {"ok": True})
        with pytest.raises(ProviderRequestFailed) as error:
            _call_with_retries(call, policy=FAST, classify=_classify_sdk_error(FAST), sleep_fn=lambda _s: None)
        assert calls["count"] == 1
        assert error.value.status_code == status
        assert error.value.retryable is False
        assert f"HTTP {status}" in str(error.value)


def test_connection_errors_are_retried_and_reported_after_exhaustion():
    request = httpx.Request("POST", "http://model.test")
    call, calls = _sequence(*[APIConnectionError(request=request)] * 4)

    with pytest.raises(ProviderRequestFailed) as error:
        _call_with_retries(call, policy=FAST, classify=_classify_sdk_error(FAST), sleep_fn=lambda _s: None)

    assert calls["count"] == 4
    assert error.value.attempts == 4
    assert error.value.retryable is True


def test_retry_stops_when_cancelled_between_attempts():
    cancel = Event()

    def call():
        cancel.set()
        raise _status_error(503)

    with pytest.raises(ProviderCancelled):
        _call_with_retries(
            call,
            policy=FAST,
            classify=_classify_sdk_error(FAST),
            cancellation_event=cancel,
            sleep_fn=lambda _s: None,
        )


def test_total_wait_cap_gives_up():
    policy = RetryPolicy(max_attempts=4, max_total_wait=20.0, retry_after_cap=30.0)
    call, calls = _sequence(_status_error(503, {"retry-after": "600"}), {"ok": True})

    with pytest.raises(ProviderRequestFailed, match="retry budget exhausted"):
        _call_with_retries(call, policy=policy, classify=_classify_sdk_error(policy), sleep_fn=lambda _s: None)
    assert calls["count"] == 1


def test_sdk_client_is_reused_with_zero_sdk_retries(monkeypatch):
    provider = OpenAICompatibleProvider(
        kind="openai",
        base_url="http://model.test",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[],
    )

    assert provider._client() is provider._client()
    assert provider._client().max_retries == 0


def test_managed_backend_retries_transient_failures_only():
    policy = RetryPolicy(max_attempts=3, base_delay=0.001, max_delay=0.002, max_total_wait=1.0)
    attempts = {"count": 0}

    def managed_request(*, run_id, session_id, model_request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise AcpAuthorizationFailure("busy", status_code=503)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    provider = OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://backend",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[],
        managed_request=managed_request,
        retry_policy=policy,
    )
    turn = provider.generate([{"role": "user", "content": "hi"}], run_id="run-1")
    assert attempts["count"] == 2
    assert turn.output["final_answer"] == "ok"
    assert turn.usage_estimated is True

    def forbidden(*, run_id, session_id, model_request):
        attempts["count"] += 1
        raise AcpAuthorizationFailure("policy", status_code=403)

    provider = OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://backend",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[],
        managed_request=forbidden,
        retry_policy=policy,
    )
    attempts["count"] = 0
    with pytest.raises(ProviderRequestFailed) as error:
        provider.generate([{"role": "user", "content": "hi"}], run_id="run-2")
    assert attempts["count"] == 1
    assert error.value.status_code == 403


def test_managed_classifier_treats_expired_session_as_non_retryable():
    classified = _classify_managed_error(FAST)(AcpSessionExpired("expired", status_code=401))
    assert classified.retryable is False
    transient = _classify_managed_error(FAST)(AcpAuthorizationFailure("net", transient=True))
    assert transient.retryable is True


def test_empty_tool_list_omits_tools_and_tool_choice_and_forwards_max_output_tokens():
    seen: dict[str, object] = {}

    def managed_request(*, run_id, session_id, model_request):
        seen.update(model_request)
        return {"choices": [{"message": {"content": "ok"}}]}

    provider = OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://backend",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[],
        managed_request=managed_request,
        max_output_tokens=256,
        retry_policy=FAST,
    )
    provider.generate([{"role": "user", "content": "hi"}], tool_choice="none")

    assert "tools" not in seen and "tool_choice" not in seen
    assert seen["max_tokens"] == 256


def test_tool_choice_is_forwarded_when_tools_exist():
    seen: dict[str, object] = {}

    def managed_request(*, run_id, session_id, model_request):
        seen.update(model_request)
        return {"choices": [{"message": {"content": "ok"}}]}

    provider = OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://backend",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        managed_request=managed_request,
        retry_policy=FAST,
    )
    provider.generate([{"role": "user", "content": "hi"}], tool_choice="none")
    assert seen["tool_choice"] == "none"
    assert [tool["function"]["name"] for tool in seen["tools"]] == ["read_file"]


def test_reasoning_read_from_reasoning_content_and_bad_arguments_flagged():
    normalized = _normalize_openai_provider_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "reasoning_content": "thinking",
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}},
                            {"id": "c2", "function": {"name": "read_file", "arguments": "[1, 2]"}},
                        ],
                    }
                }
            ]
        }
    )

    assert normalized["reasoning"] == "thinking"
    assert normalized["tool_calls"][0]["argument_error"].startswith("Tool arguments were not valid JSON")
    assert normalized["tool_calls"][0]["raw_arguments"] == "{not json"
    assert normalized["tool_calls"][1]["argument_error"] == "Tool arguments must be a JSON object."


def test_normalize_usage_marks_estimates():
    assert _usage_is_reported({"prompt_tokens": 12, "completion_tokens": 3}) is True
    assert _usage_is_reported({"prompt_tokens": 0}) is False
    assert _usage_is_reported(None) is False
    estimated = _normalize_usage(None, [{"role": "user", "content": "x" * 40}], {"final_answer": "y"})
    assert estimated["total_tokens"] == estimated["prompt_tokens"] + estimated["completion_tokens"]


def test_null_arguments_are_treated_as_an_empty_object():
    from code4me2_agent.adapters import _normalize_tool_calls

    normalized = _normalize_openai_provider_response(
        {"choices": [{"message": {"tool_calls": [{"id": "c1", "function": {"name": "list_files", "arguments": None}}]}}]}
    )
    assert normalized["tool_calls"][0]["arguments"] == {}
    assert "argument_error" not in normalized["tool_calls"][0]

    calls = _normalize_tool_calls([{"id": "c2", "name": "list_files", "arguments": None}])
    assert calls[0].arguments == {} and calls[0].argument_error is None
