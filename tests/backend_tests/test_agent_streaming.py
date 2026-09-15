"""Streaming regression tests for the I10 inference relay.

Covers ``StreamingAccumulator`` (split SSE lines, ``[DONE]``, colocated usage,
tool_calls deltas, Responses ``output_text.delta``, cached/reasoning/details,
tool-only finish, missing usage as null, bounded tail) and the
``run_inference`` streaming path (verbatim passthrough, pre-stream 429/503
retry honoring Retry-After, no mid-stream retry, client-cancel finishing as
``cancelled_client`` with exactly one telemetry row, ``api_kind`` routing).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from agents.normalize import (
    StreamingAccumulator,
    parse_responses_api_stream,
    parse_stream,
)


def _inference_module() -> Any:
    """Import the relay lazily: it needs backend deps (fastapi) that the
    agent-only venv does not install. Tests needing it skip with a reason
    instead of breaking collection of the pure-accumulator tests."""
    return pytest.importorskip(
        "agents.inference", reason="backend deps (fastapi) not installed in this venv"
    )


def _chat_content_chunk(content: str) -> str:
    return "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})


def test_split_lines_across_feeds() -> None:
    acc = StreamingAccumulator(api_kind="chat_completions")
    first = 'data: {"choices": [{"delta": {"content": "Hel'
    events = acc.feed(first.encode())
    assert events == []
    events = acc.feed('lo"}}]}\n\n'.encode())
    assert any(e.get("type") == "text_delta" for e in events)
    prompt, completion, total, finish, text, tool_seen, source = acc.finalize()
    assert text == "Hello"
    assert (prompt, completion, total) == (None, None, None)
    assert source == "missing"
    assert tool_seen == 0


def test_done_colocated_usage_and_finish() -> None:
    raw = (
        _chat_content_chunk("Hi") + "\n\n"
        'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
        '"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}\n\n'
        "data: [DONE]\n\n"
    )
    prompt, completion, total, finish, text = parse_stream(raw)
    assert (prompt, completion, total, finish, text) == (2, 1, 3, "stop", "Hi")


def test_tool_calls_deltas_assemble_and_finish_tool_calls() -> None:
    acc = StreamingAccumulator(api_kind="chat_completions")
    acc.feed(
        (
            'data: {"choices": [{"delta": {"tool_calls": ['
            '{"index": 0, "id": "call_1", "function": {"name": "read_file", '
            '"arguments": "{\\"path\\":"}}]}}]}\n\n'
        ).encode()
    )
    acc.feed(
        (
            'data: {"choices": [{"delta": {"tool_calls": ['
            '{"index": 0, "function": {"arguments": "\\"x\\"}"}}]}}]}\n\n'
        ).encode()
    )
    prompt, completion, total, finish, text, tool_seen, source = acc.finalize()
    assert finish == "tool_calls"
    assert tool_seen == 1
    assert text is None  # tool-only streams invent no text
    assert source == "missing"


def test_responses_output_text_delta_and_completed_usage() -> None:
    raw = (
        'data: {"type": "response.output_text.delta", "delta": "Hel"}\n\n'
        'data: {"type": "response.output_text.delta", "delta": "lo"}\n\n'
        'data: {"type": "response.completed", "response": {'
        '"status": "completed", '
        '"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}}\n\n'
    )
    prompt, completion, total, finish, text = parse_responses_api_stream(raw)
    assert (prompt, completion, total, finish, text) == (4, 2, 6, "completed", "Hello")


def test_usage_details_cached_reasoning_and_generic_details() -> None:
    acc = StreamingAccumulator(api_kind="chat_completions")
    acc.feed(
        (
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
            '"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, '
            '"prompt_tokens_details": {"cached_tokens": 3}, '
            '"completion_tokens_details": {"reasoning_tokens": 2}, '
            '"details": {"cached_tokens": 99}}}\n\n'
        ).encode()
    )
    acc.finalize()
    # Specific details keys win over the generic "details" nesting.
    assert acc.extra_details() == {"cached_tokens": 3, "reasoning_tokens": 2}


def test_text_tail_is_bounded() -> None:
    acc = StreamingAccumulator(api_kind="chat_completions")
    big = "x" * 5000
    acc.feed((_chat_content_chunk(big) + "\n\n").encode())
    _, _, _, _, text, _, _ = acc.finalize()
    assert text is not None and len(text) <= 4096
    assert text == big[-4096:]


def test_resolve_api_kind_explicit_and_auto() -> None:
    inference_mod = _inference_module()
    _resolve_api_kind = inference_mod._resolve_api_kind
    assert _resolve_api_kind({"messages": []}, "chat_completions") == (
        False,
        "chat_completions",
    )
    assert _resolve_api_kind({"messages": []}, "responses") == (True, "responses")
    assert _resolve_api_kind({"input": []}, "auto")[0] is True
    assert _resolve_api_kind({"messages": []}, "auto")[0] is False


# ── run_inference streaming path ──────────────────────────────────────────────


class _FakeDB:
    def close(self) -> None:
        pass


class _FakeApp:
    def get_db_session(self) -> _FakeDB:
        return _FakeDB()


class _FakeUpstream:
    base_url = "https://fake-upstream.example/v1"
    api_key = "test-key-not-a-secret"

    def endpoint(self, *, responses_api: bool = False) -> str:
        return "https://fake-upstream.example/v1/chat/completions"


class _FakeStream:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: tuple[bytes | BaseException, ...] = (),
        headers: Optional[dict[str, str]] = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = dict(headers or {})
        self._body = body
        self.closed = False

    async def aiter_bytes(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        self.closed = True


class _FakeHttpClient:
    def __init__(self, streams: list[_FakeStream], opened: list["_FakeHttpClient"]) -> None:
        # Shared queue: pre-stream retries open a NEW client per attempt and
        # each client consumes the next queued upstream response.
        self._streams = streams
        self.closed = False
        opened.append(self)

    def build_request(self, *args: Any, **kwargs: Any) -> object:
        return object()

    async def send(self, request: object, stream: bool = True) -> _FakeStream:
        return self._streams.pop(0)

    async def aclose(self) -> None:
        self.closed = True


SSE_OK = (
    b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
    b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], '
    b'"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}\n\n'
    b"data: [DONE]\n\n"
)


def _install_harness(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[_FakeStream],
    *,
    sleeps: list[float] | None = None,
) -> dict[str, Any]:
    inference_mod = _inference_module()
    captured: dict[str, Any] = {"model_calls": [], "tool_calls": []}
    captured["inference"] = inference_mod
    opened: list[_FakeHttpClient] = []
    captured["opened"] = opened

    monkeypatch.setattr(
        inference_mod.provider_module,
        "resolve_upstream",
        lambda **kwargs: _FakeUpstream(),
    )
    monkeypatch.setattr(
        inference_mod.crud,
        "get_last_agent_event_for_session",
        lambda db, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        inference_mod.crud,
        "get_agent_task",
        lambda db, task_id: SimpleNamespace(
            framework_version=None, task_description="existing", approval_policy=None
        ),
    )
    monkeypatch.setattr(
        inference_mod,
        "write_model_call_event",
        lambda app, task_uuid, record, latency_ms, span, extra=None: captured[
            "model_calls"
        ].append(
            {"record": record, "latency_ms": latency_ms, "span": span, "extra": extra}
        ),
    )
    monkeypatch.setattr(
        inference_mod,
        "write_tool_call_events",
        lambda *args, **kwargs: captured["tool_calls"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        inference_mod.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _FakeHttpClient(streams, opened),
    )
    if sleeps is not None:
        real_sleep = asyncio.sleep

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        monkeypatch.setattr(inference_mod.asyncio, "sleep", _fake_sleep)
        assert real_sleep is not None
    return captured


def _base_body() -> dict[str, Any]:
    return {
        "model": "requested-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


async def _drain(response: Any) -> bytes:
    parts = []
    async for chunk in response.body_iterator:
        parts.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    return b"".join(parts)


def test_streaming_passthrough_and_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_harness(
        monkeypatch, [_FakeStream(status_code=200, chunks=(SSE_OK,))]
    )
    inference_mod = captured["inference"]
    response = asyncio.run(
        inference_mod.run_inference(
            task_uuid=uuid.uuid4(),
            session_uuid=uuid.uuid4(),
            openai_body=_base_body(),
            enrichment=None,
            agent_profile="profile",
            model="assigned-model",
            app=_FakeApp(),  # type: ignore[arg-type]
            api_kind="chat_completions",
        )
    )
    body = asyncio.run(_drain(response))
    assert body == SSE_OK  # verbatim passthrough, never synthesized

    assert len(captured["model_calls"]) == 1
    call = captured["model_calls"][0]
    record = call["record"]
    assert (record.prompt_tokens, record.completion_tokens, record.total_tokens) == (
        2,
        1,
        3,
    )
    assert record.finish_reason == "stop"
    extra = call["extra"]
    assert extra["attempt_count"] == 1
    assert extra["total_retry_ms"] == 0
    assert extra["usage_source"] == "stream_usage"
    assert extra["bytes_forwarded"] == len(SSE_OK)
    assert extra["chunks"] == 1
    assert extra["ttft_ms"] is not None and extra["ttfv_ms"] is not None
    assert extra["tool_calls_seen"] == 0


def test_pre_stream_429_retry_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    captured = _install_harness(
        monkeypatch,
        [
            _FakeStream(
                status_code=429,
                headers={"retry-after": "2"},
                body=b'{"error": "slow down"}',
            ),
            _FakeStream(status_code=200, chunks=(SSE_OK,)),
        ],
        sleeps=sleeps,
    )
    inference_mod = captured["inference"]
    response = asyncio.run(
        inference_mod.run_inference(
            task_uuid=uuid.uuid4(),
            session_uuid=uuid.uuid4(),
            openai_body=_base_body(),
            enrichment=None,
            agent_profile="profile",
            model="assigned-model",
            app=_FakeApp(),  # type: ignore[arg-type]
            api_kind="chat_completions",
        )
    )
    assert asyncio.run(_drain(response)) == SSE_OK
    assert sleeps == [2.0]
    assert len(captured["opened"]) == 2
    call = captured["model_calls"][0]
    assert call["extra"]["attempt_count"] == 2
    assert call["extra"]["total_retry_ms"] == 2000


def test_terminal_pre_stream_error_still_writes_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_harness(
        monkeypatch,
        [_FakeStream(status_code=500, headers={}, body=b'{"error": "boom"}')],
    )
    inference_mod = captured["inference"]
    response = asyncio.run(
        inference_mod.run_inference(
            task_uuid=uuid.uuid4(),
            session_uuid=uuid.uuid4(),
            openai_body=_base_body(),
            enrichment=None,
            agent_profile="profile",
            model="assigned-model",
            app=_FakeApp(),  # type: ignore[arg-type]
            api_kind="chat_completions",
        )
    )
    assert response.status_code == 500
    assert len(captured["model_calls"]) == 1
    call = captured["model_calls"][0]
    assert call["record"].prompt_tokens is None  # missing usage is null, not 0
    assert call["extra"]["usage_source"] == "missing"
    assert call["extra"]["attempt_count"] == 1


def test_client_cancel_finishes_cancelled_client_with_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
    captured = _install_harness(
        monkeypatch, [_FakeStream(status_code=200, chunks=(chunk, chunk))]
    )
    calls = {"n": 0}

    def _is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    inference_mod = captured["inference"]
    response = asyncio.run(
        inference_mod.run_inference(
            task_uuid=uuid.uuid4(),
            session_uuid=uuid.uuid4(),
            openai_body=_base_body(),
            enrichment=None,
            agent_profile="profile",
            model="assigned-model",
            app=_FakeApp(),  # type: ignore[arg-type]
            api_kind="chat_completions",
            is_disconnected=_is_disconnected,
        )
    )
    body = asyncio.run(_drain(response))
    assert body == chunk
    assert len(captured["model_calls"]) == 1
    call = captured["model_calls"][0]
    assert call["record"].finish_reason == "cancelled_client"
    assert call["extra"]["cancel_source"] == "client"


def test_mid_stream_abort_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
    captured = _install_harness(
        monkeypatch,
        [_FakeStream(status_code=200, chunks=(chunk, RuntimeError("reset")))],
    )
    inference_mod = captured["inference"]
    response = asyncio.run(
        inference_mod.run_inference(
            task_uuid=uuid.uuid4(),
            session_uuid=uuid.uuid4(),
            openai_body=_base_body(),
            enrichment=None,
            agent_profile="profile",
            model="assigned-model",
            app=_FakeApp(),  # type: ignore[arg-type]
            api_kind="chat_completions",
        )
    )
    assert asyncio.run(_drain(response)) == chunk
    assert len(captured["opened"]) == 1  # no mid-stream retry
    assert len(captured["model_calls"]) == 1
    assert captured["model_calls"][0]["record"].finish_reason == "stream_aborted"


# ── managed SSE route (/api/acp/inference with stream:true) ──────────────────


def _managed_acp_module() -> Any:
    """Import the managed route lazily, like the relay above."""
    return pytest.importorskip(
        "backend.routers.acp", reason="backend deps (fastapi) not installed in this venv"
    )


def _managed_scope_and_task() -> tuple[Any, Any]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    scope = SimpleNamespace(
        user_id=str(user_id),
        project_id=str(project_id),
        session_id=str(parent_session_id),
    )
    task = SimpleNamespace(
        task_id=uuid.uuid4(),
        owner_user_id=user_id,
        owner_project_id=project_id,
        agent_session_id="acp-session-1",
        source="code4me2_agent",
        policy_snapshot={
            "version": "1",
            "model": "managed-model",
            "tools": ["read_file"],
            "approval_policy": "auto",
            "max_iterations": 3,
            "max_context_tokens": 1000,
            "temperature": 0.2,
            "store_agent_content": False,
        },
        agent_profile="managed-profile",
        framework_version="code4me2-agent",
    )
    return scope, task


class _FakePostResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any | None = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload if payload is not None else {}).encode()
        self.headers = dict(headers or {})
        self.text = self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def _install_managed_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    streams: tuple[_FakeStream, ...] = (),
    nonstream: tuple[_FakePostResponse, ...] = (),
    sleeps: list[float] | None = None,
) -> dict[str, Any]:
    inference_mod = _inference_module()
    acp_mod = _managed_acp_module()
    captured: dict[str, Any] = {
        "model_calls": [],
        "bodies": [],
        "opened": [],
        "inference": inference_mod,
        "acp": acp_mod,
    }
    streams_queue = list(streams)
    nonstream_queue = list(nonstream)

    class _Client:
        def build_request(self, *args: Any, **kwargs: Any) -> object:
            captured["bodies"].append(kwargs.get("content"))
            return object()

        async def send(self, request: object, stream: bool = True) -> _FakeStream:
            captured["opened"].append("stream")
            return streams_queue.pop(0)

        async def post(
            self, url: str, content: bytes | None = None, headers: Any = None
        ) -> _FakePostResponse:
            captured["bodies"].append(content)
            return nonstream_queue.pop(0)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        inference_mod.provider_module,
        "resolve_upstream",
        lambda **kwargs: _FakeUpstream(),
    )
    monkeypatch.setattr(
        inference_mod.crud,
        "get_last_agent_event_for_session",
        lambda db, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        inference_mod.crud,
        "get_agent_task",
        lambda db, task_id: SimpleNamespace(
            framework_version=None, task_description="existing", approval_policy=None
        ),
    )
    monkeypatch.setattr(
        inference_mod,
        "write_model_call_event",
        lambda app, task_uuid, record, latency_ms, span, extra=None: captured[
            "model_calls"
        ].append(
            {"record": record, "latency_ms": latency_ms, "span": span, "extra": extra}
        ),
    )
    monkeypatch.setattr(
        inference_mod,
        "write_tool_call_events",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        inference_mod.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _Client(),
    )
    scope, task = _managed_scope_and_task()
    captured["scope"] = scope
    captured["task"] = task
    monkeypatch.setattr(
        acp_mod.crud,
        "get_agent_task_by_external_run_id",
        lambda db, run_id: task,
    )
    monkeypatch.setattr(
        acp_mod.crud,
        "get_agent_profile",
        lambda db, name: SimpleNamespace(
            base_url="https://provider.example/v1", api_key_ref="KEY"
        ),
    )
    if sleeps is not None:
        real_sleep = asyncio.sleep

        async def _fake_sleep(seconds: float) -> None:
            sleeps.append(float(seconds))

        monkeypatch.setattr(inference_mod.asyncio, "sleep", _fake_sleep)
        assert real_sleep is not None
    return captured


def _managed_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "participant-override",
        "temperature": 9.9,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    body.update(overrides)
    return body


def test_managed_stream_happy_path_verbatim_overrides_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_managed_harness(
        monkeypatch, streams=(_FakeStream(status_code=200, chunks=(SSE_OK,)),)
    )
    acp_mod = captured["acp"]
    response = asyncio.run(
        acp_mod.run_managed_inference(
            acp_mod.ManagedInferenceRequest(
                run_id="run-1",
                session_id="acp-session-1",
                request=_managed_body(),
            ),
            _FakeApp(),  # type: ignore[arg-type]
            captured["scope"],
        )
    )
    assert asyncio.run(_drain(response)) == SSE_OK  # verbatim, never synthesized
    sent = json.loads(bytes(captured["bodies"][0]).decode("utf-8"))
    assert sent["model"] == "managed-model"  # policy override wins
    assert sent["temperature"] == 0.2
    assert sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}  # forced by relay
    assert captured["model_calls"] == []  # runtime self-reports; server only logs


def test_managed_stream_tool_only_usage_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_sse = (
        b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", '
        b'"function": {"name": "read_file", "arguments": "{\\"path\\":\\"x\\"}"}}]}}]}\n\n'
        b'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], '
        b'"usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}}\n\n'
        b"data: [DONE]\n\n"
    )
    captured = _install_managed_harness(
        monkeypatch, streams=(_FakeStream(status_code=200, chunks=(tool_sse,)),)
    )
    acp_mod = captured["acp"]
    response = asyncio.run(
        acp_mod.run_managed_inference(
            acp_mod.ManagedInferenceRequest(
                run_id="run-1",
                session_id="acp-session-1",
                request=_managed_body(
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "read_file", "parameters": {}},
                        }
                    ]
                ),
            ),
            _FakeApp(),  # type: ignore[arg-type]
            captured["scope"],
        )
    )
    assert asyncio.run(_drain(response)) == tool_sse
    assert captured["model_calls"] == []


def test_managed_stream_pre_stream_429_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    captured = _install_managed_harness(
        monkeypatch,
        streams=(
            _FakeStream(
                status_code=429,
                headers={"retry-after": "2"},
                body=b'{"error": "slow down"}',
            ),
            _FakeStream(status_code=200, chunks=(SSE_OK,)),
        ),
        sleeps=sleeps,
    )
    acp_mod = captured["acp"]
    response = asyncio.run(
        acp_mod.run_managed_inference(
            acp_mod.ManagedInferenceRequest(
                run_id="run-1",
                session_id="acp-session-1",
                request=_managed_body(),
            ),
            _FakeApp(),  # type: ignore[arg-type]
            captured["scope"],
        )
    )
    assert asyncio.run(_drain(response)) == SSE_OK
    assert sleeps == [2.0]
    assert captured["opened"] == ["stream", "stream"]
    sent = json.loads(bytes(captured["bodies"][1]).decode("utf-8"))
    assert sent["model"] == "managed-model"
    assert captured["model_calls"] == []


def test_managed_stream_client_disconnect_closes_upstream_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
    upstream = _FakeStream(status_code=200, chunks=(chunk, chunk))
    captured = _install_managed_harness(
        monkeypatch, streams=(upstream,)
    )
    calls = {"n": 0}

    def _is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    acp_mod = captured["acp"]
    response = asyncio.run(
        acp_mod.run_managed_inference(
            acp_mod.ManagedInferenceRequest(
                run_id="run-1",
                session_id="acp-session-1",
                request=_managed_body(),
            ),
            _FakeApp(),  # type: ignore[arg-type]
            captured["scope"],
            SimpleNamespace(is_disconnected=_is_disconnected),
        )
    )
    assert asyncio.run(_drain(response)) == chunk
    assert upstream.closed is True
    assert captured["model_calls"] == []


def test_managed_non_stream_stays_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_payload = {
        "id": "chatcmpl-1",
        "model": "managed-model",
        "choices": [
            {"finish_reason": "stop", "message": {"content": "hello", "role": "assistant"}}
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    captured = _install_managed_harness(
        monkeypatch, nonstream=(_FakePostResponse(status_code=200, payload=upstream_payload),)
    )
    acp_mod = captured["acp"]
    response = asyncio.run(
        acp_mod.run_managed_inference(
            acp_mod.ManagedInferenceRequest(
                run_id="run-1",
                session_id="acp-session-1",
                request={
                    "model": "participant-override",
                    "temperature": 9.9,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            ),
            _FakeApp(),  # type: ignore[arg-type]
            captured["scope"],
        )
    )
    assert response.status_code == 200
    assert json.loads(bytes(response.body).decode("utf-8")) == upstream_payload
    sent = json.loads(bytes(captured["bodies"][0]).decode("utf-8"))
    assert sent["model"] == "managed-model"
    assert sent["temperature"] == 0.2
    assert "stream" not in sent
    assert captured["model_calls"] == []


def test_managed_stream_still_rejects_input_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_managed_harness(monkeypatch)
    acp_mod = captured["acp"]
    with pytest.raises(Exception) as error:
        asyncio.run(
            acp_mod.run_managed_inference(
                acp_mod.ManagedInferenceRequest(
                    run_id="run-1",
                    session_id="acp-session-1",
                    request={"input": [{"role": "user", "content": "hi"}], "stream": True},
                ),
                _FakeApp(),  # type: ignore[arg-type]
                captured["scope"],
            )
        )
    assert error.value.status_code == 400


def test_managed_stream_enforces_tool_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_managed_harness(monkeypatch)
    acp_mod = captured["acp"]
    with pytest.raises(Exception) as error:
        asyncio.run(
            acp_mod.run_managed_inference(
                acp_mod.ManagedInferenceRequest(
                    run_id="run-1",
                    session_id="acp-session-1",
                    request=_managed_body(
                        tools=[
                            {
                                "type": "function",
                                "function": {"name": "write_file", "parameters": {}},
                            }
                        ]
                    ),
                ),
                _FakeApp(),  # type: ignore[arg-type]
                captured["scope"],
            )
        )
    assert error.value.status_code == 403
