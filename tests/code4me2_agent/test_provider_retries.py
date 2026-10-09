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


def test_tool_calls_without_a_provider_id_get_distinct_ids_across_steps():
    """Providers that omit call ids (or send null/blank ones) used to get
    ``tool-call-1`` on every step, or ``"None"`` for all of them, so telemetry
    and traces merged the calls of different steps into one."""
    from code4me2_agent.adapters import _normalize_tool_calls

    def step():
        return _normalize_openai_provider_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "read_file", "arguments": "{}"}},
                                {"id": None, "function": {"name": "list_files", "arguments": "{}"}},
                                {"id": " ", "function": {"name": "list_files", "arguments": "{}"}},
                            ]
                        }
                    }
                ]
            }
        )["tool_calls"]

    ids = [call["id"] for call in step() + step()]
    assert len(set(ids)) == 6 and all(ids)
    # A provider's own id is kept, and the parsed id reaches the tool call as is.
    assert _normalize_tool_calls([{"id": "c7", "name": "read_file", "arguments": {}}])[0].tool_call_id == "c7"
    first = step()[0]
    assert _normalize_tool_calls([first])[0].tool_call_id == first["id"]


@pytest.mark.parametrize("base_url", ["https://opencode.ai/zen/go/v1", "https://opencode.ai/zen/v1"])
def test_direct_opencode_calls_carry_a_stable_session_header(monkeypatch, base_url):
    monkeypatch.setenv("OPENCODE_TEST_KEY", "secret-key")

    def headers(session_id):
        return OpenAICompatibleProvider(
            kind="openai",
            base_url=base_url,
            model="deepseek-v4-flash",
            api_key_env="OPENCODE_TEST_KEY",
            timeout_seconds=5.0,
            tool_definitions=[],
            session_id=session_id,
        )._headers()

    first = headers("swebench-run-a")
    assert first["Authorization"] == "Bearer secret-key"
    assert first["x-opencode-session"].startswith("c4m-")
    assert "swebench-run-a" not in first["x-opencode-session"]
    assert headers("swebench-run-a")["x-opencode-session"] == first["x-opencode-session"]
    assert headers("swebench-run-b")["x-opencode-session"] != first["x-opencode-session"]


def test_other_direct_providers_get_no_opencode_header():
    provider = OpenAICompatibleProvider(
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        model="m",
        api_key_env="",
        timeout_seconds=5.0,
        tool_definitions=[],
        session_id="s",
    )

    assert "x-opencode-session" not in provider._headers()


def test_request_deadline_abandons_a_request_that_never_answers(monkeypatch):
    import time as _time
    from types import SimpleNamespace

    from openai import APITimeoutError

    from code4me2_agent.adapters import _classify_sdk_error

    provider = OpenAICompatibleProvider(
        kind="openai",
        base_url="http://model.test",
        model="m",
        api_key_env="",
        timeout_seconds=300.0,
        tool_definitions=[],
        request_deadline_seconds=0.3,
    )
    # A server that keeps the connection alive (DeepSeek's blank-line keep-alive)
    # never trips the SDK's read timeout; only the wall-clock deadline ends it.
    hanging = SimpleNamespace(create=lambda **_kwargs: _time.sleep(30))
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(with_raw_response=hanging))
    )
    monkeypatch.setattr(provider, "_client", lambda: client)

    started = _time.perf_counter()
    with pytest.raises(APITimeoutError) as caught:
        provider._sdk_call({"model": "m", "messages": []}, None)

    assert _time.perf_counter() - started < 5
    assert _classify_sdk_error(FAST)(caught.value).retryable


def test_request_deadline_is_read_from_the_config_file(tmp_path):
    import json

    from code4me2_agent.config import AgentConfig

    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"adapter": {"provider": {"request_deadline_seconds": 120}}}))
    assert AgentConfig.from_file(path).adapter.provider.request_deadline_seconds == 120.0
    path.write_text(json.dumps({"adapter": {"provider": {}}}))
    assert AgentConfig.from_file(path).adapter.provider.request_deadline_seconds is None


def test_reasoning_goes_back_exactly_as_received_and_only_where_produced(monkeypatch):
    import json as _json

    from openai import OpenAI

    from code4me2_agent.adapters import _normalize_openai_provider_response

    captured = []

    def handler(request):
        captured.append(_json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "done",
                                     "reasoning_content": "thought 2"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        })

    provider = OpenAICompatibleProvider(kind="openai", base_url="http://model.test", model="m",
                                        api_key_env="", timeout_seconds=5.0, tool_definitions=[])
    client = OpenAI(api_key="x", base_url="http://model.test/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(provider, "_client", lambda: client)

    turn = provider.generate([
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "", "provider_reasoning": {"reasoning_content": "thought 1"},
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
        {"role": "assistant", "content": "earlier answer without reasoning"},
    ])

    sent = captured[0]["messages"]
    assert sent[1]["reasoning_content"] == "thought 1"  # the SDK passes it through unchanged
    assert "provider_reasoning" not in sent[1]
    assert "reasoning_content" not in sent[3]  # never invented for other messages
    assert turn.output["reasoning_fields"] == {"reasoning_content": "thought 2"}
    # OpenRouter-style `reasoning` is normalised to the one field we send back.
    openrouter = _normalize_openai_provider_response({"choices": [{"message": {
        "content": "x", "reasoning": "r", "reasoning_details": [{"type": "reasoning.text", "text": "r"}]}}]})
    assert openrouter["reasoning_fields"] == {"reasoning_content": "r"}
    empty = _normalize_openai_provider_response({"choices": [{"message": {"content": "x", "reasoning_content": ""}}]})
    assert empty["reasoning_fields"] == {"reasoning_content": ""}  # "" kept distinct from absent
    plain = _normalize_openai_provider_response({"choices": [{"message": {"content": "x"}}]})
    assert "reasoning_fields" not in plain


def _capturing_provider(monkeypatch, model, responses):
    import json as _json

    from openai import OpenAI

    captured = []

    def handler(request):
        captured.append(_json.loads(request.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    provider = OpenAICompatibleProvider(kind="openai", base_url="http://model.test", model=model, api_key_env="",
                                        timeout_seconds=5.0,
                                        tool_definitions=[{"type": "function", "function": {"name": "read_file",
                                                                                            "parameters": {}}}],
                                        retry_policy=RetryPolicy(max_attempts=1))
    client = OpenAI(api_key="x", base_url="http://model.test/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(provider, "_client", lambda: client)
    return provider, captured


_OK = (200, {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
_HISTORY = [
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "", "provider_reasoning": {"reasoning_content": "t1"},
     "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "x"},
    {"role": "assistant", "content": "runtime note without reasoning"},
    {"role": "user", "content": "next"},
]


def test_deepseek_v4_gets_reasoning_on_every_assistant_message_when_tools_are_sent(monkeypatch):
    provider, captured = _capturing_provider(monkeypatch, "deepseek-v4.1-flash", [_OK, _OK])
    provider.generate(list(_HISTORY))
    sent = [m for m in captured[0]["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in sent] == ["t1", ""]
    # Without tools DeepSeek ignores reasoning: no filling.
    provider.generate(list(_HISTORY), include_tools=False)
    sent = [m for m in captured[1]["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in sent] == ["t1", None]


def test_a_server_that_rejects_the_field_gets_none_after_one_retry(monkeypatch):
    rejected = (400, {"error": {"message": "Extra inputs are not permitted, field: 'messages[1].reasoning_content'"}})
    provider, captured = _capturing_provider(monkeypatch, "some-model", [rejected, _OK, _OK])
    provider.generate(list(_HISTORY))
    assert captured[0]["messages"][1]["reasoning_content"] == "t1"
    assert "reasoning_content" not in captured[1]["messages"][1]  # retried without it
    provider.generate(list(_HISTORY))
    assert "reasoning_content" not in captured[2]["messages"][1]  # and remembered


def test_a_server_that_requires_the_field_gets_it_filled_after_one_retry(monkeypatch):
    required = (400, {"error": {"message": "The `reasoning_content` in the thinking mode must be passed back to the API."}})
    provider, captured = _capturing_provider(monkeypatch, "some-thinking-model", [required, _OK])
    provider.generate(list(_HISTORY))
    assert [m.get("reasoning_content") for m in captured[1]["messages"] if m["role"] == "assistant"] == ["t1", ""]


def test_unrelated_bad_requests_are_not_retried(monkeypatch):
    bad = (400, {"error": {"message": "context length exceeded"}})
    provider, captured = _capturing_provider(monkeypatch, "deepseek-v4.1-flash", [bad])
    with pytest.raises(ProviderRequestFailed):
        provider.generate(list(_HISTORY))
    assert len(captured) == 1
