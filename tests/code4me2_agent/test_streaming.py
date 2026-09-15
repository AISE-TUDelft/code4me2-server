"""Streaming + cancellation regression tests for the I12 Python runtime.

Covers: provider ``generate_stream`` (stream=True, coalesced ``on_delta``,
managed fallback, prompt cancellation), ``AcpUpdateBuilder.agent_message_chunk``
(delta metadata while the final ``agent_message`` stays unchanged),
``AcpSessionEventSink.agent_message_delta`` delivery via the async runner,
cancellable 429 sleep / subprocess / MCP waits, approval cancellation,
``MemoryWindow.discard_trailing_orphan_tool_calls`` on cancel paths,
``edit_seq`` on edit outputs, and the toy MCP fixture end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import sys
import threading
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from urllib import error as urllib_error

import pytest

from code4me2_agent.acp_runtime import AcpSessionEventSink
from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.adapters import (
    BackendProviderRequestError,
    MemoryWindow,
    OpenAICompatibleProvider,
    OpenAICompatibleReactAdapter,
    ProviderStreamCancelledError,
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
    _cancelled_result,
)
from code4me2_agent.command_tools import WorkspaceCommandTools
from code4me2_agent.config import AgentConfig
from code4me2_agent.file_tools import WorkspaceFileTools
from code4me2_agent.mcp_tools import StdioMcpToolBroker, _McpTool
from code4me2_agent.runtime_auth import (
    AcpAuthorizationFailure,
    AcpBackendAuthorization,
    AcpSessionExpired,
    ManagedBridgeAuthorization,
    ManagedStreamUnsupportedError,
    _ManagedSseStream,
)
from code4me2_agent.telemetry import AgentTelemetryRecorder


def _test_config(workspace: Path) -> AgentConfig:
    return AgentConfig(
        workspace_root=workspace,
        trace_path=workspace / "agent-events.jsonl",
        session_id="test-session",
    )


# ── generate_stream ───────────────────────────────────────────────────────────


class _FakeToolCallDelta:
    def __init__(
        self,
        index: int = 0,
        id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> None:
        self.index = index
        self.id = id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _FakeChunk:
    def __init__(
        self,
        *,
        content: str | None = None,
        tool_calls: list[Any] | None = None,
        finish_reason: str | None = None,
        usage: Any = None,
        model: str | None = None,
    ) -> None:
        self.choices = [
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ]
        self.usage = usage
        self.model = model


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


def _provider_with_stream(chunks: list[Any], seen: list[dict]) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        kind="openai",
        base_url="http://127.0.0.1:9",
        model="test-model",
        api_key_env="CODE4ME_TEST_MISSING_API_KEY",
        timeout_seconds=5.0,
    )
    stream = _FakeStream(chunks)

    def _client():
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **payload: (seen.append(payload), stream)[1]
                )
            )
        )

    provider._client = _client  # type: ignore[method-assign]
    provider._fake_stream = stream  # type: ignore[attr-defined]
    return provider


def test_generate_stream_sends_stream_flag_and_reassembles() -> None:
    seen: list[dict] = []
    provider = _provider_with_stream(
        [
            _FakeChunk(content="Hello "),
            _FakeChunk(
                content="world",
                tool_calls=[
                    _FakeToolCallDelta(
                        index=0, id="call_1", name="read_file", arguments='{"path":'
                    )
                ],
            ),
            _FakeChunk(
                tool_calls=[
                    _FakeToolCallDelta(index=0, arguments='"x"}'),
                ],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
                model="test-model",
            ),
        ],
        seen,
    )
    deltas: list[str] = []
    turn = provider.generate_stream(
        [{"role": "user", "content": "hi"}],
        run_id="run-1",
        on_delta=deltas.append,
    )

    assert seen and seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert turn.output["final_answer"] == "Hello world"
    assert turn.output["tool_calls"] == [
        {"id": "call_1", "name": "read_file", "arguments": {"path": "x"}}
    ]
    assert turn.finish_reason == "tool_calls"
    assert turn.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }
    # Coalesced on_delta traffic reassembles to the same visible text.
    assert deltas and "".join(deltas) == "Hello world"
    assert provider._fake_stream.closed is True


def test_generate_stream_without_on_delta_still_reassembles() -> None:
    provider = _provider_with_stream([_FakeChunk(content="ok")], [])
    turn = provider.generate_stream([{"role": "user", "content": "hi"}])
    assert turn.output["final_answer"] == "ok"


def test_generate_stream_cancel_closes_stream() -> None:
    provider = _provider_with_stream([_FakeChunk(content="partial")], [])
    cancel = Event()
    cancel.set()
    with pytest.raises(ProviderStreamCancelledError):
        provider.generate_stream(
            [{"role": "user", "content": "hi"}], cancellation_event=cancel
        )
    assert provider._fake_stream.closed is True


def test_generate_stream_managed_backend_falls_back_to_single_shot() -> None:
    seen_requests: list[dict] = []

    def _managed_request(*, run_id: str, session_id: str, model_request: dict) -> dict:
        seen_requests.append(model_request)
        return {
            "model": "managed-model",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "managed ok"}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    provider = OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://127.0.0.1:9",
        model="managed-model",
        api_key_env="CODE4ME_TEST_MISSING_API_KEY",
        timeout_seconds=5.0,
        managed_request=_managed_request,
    )
    turn = provider.generate_stream(
        [{"role": "user", "content": "hi"}],
        run_id="run-1",
        on_delta=lambda _text: None,
    )
    assert turn.output["final_answer"] == "managed ok"
    assert seen_requests and seen_requests[0].get("stream") is None


# ── agent_message_chunk / agent_message_delta ─────────────────────────────────


def test_agent_message_chunk_marks_delta_and_final_is_unchanged() -> None:
    builder = AcpUpdateBuilder()
    chunk = builder.agent_message_chunk("Hel", message_id="msg-1")
    assert chunk.field_meta == {"code4me2": {"phase": "delta"}}

    final = builder.agent_message("Hello", message_id="msg-1")
    field_meta = getattr(final, "field_meta", None)
    assert not (isinstance(field_meta, dict) and "code4me2" in field_meta)


class _RecordingConn:
    def __init__(self, fail: bool = False) -> None:
        self.updates: list[dict[str, Any]] = []
        self._fail = fail

    async def session_update(self, *, session_id: str, update: object, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("client gone")
        self.updates.append({"session_id": session_id, "update": update})


class _BlockingRunner:
    def run(self, awaitable: Any, timeout_seconds: float | None = None) -> Any:
        return asyncio.run(awaitable)


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, *, event_type: str, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, **kwargs})


def test_agent_message_delta_emits_via_runner_and_ignores_empty() -> None:
    conn = _RecordingConn()
    sink = AcpSessionEventSink(
        conn=conn,
        session_id="session-1",
        updates=AcpUpdateBuilder(),
        telemetry=_RecordingTelemetry(),
        async_runner=_BlockingRunner(),
    )
    sink.agent_message_delta("", message_id="msg-1")
    assert conn.updates == []
    sink.agent_message_delta("hi", message_id="msg-1", run_id="r", request_id="q")
    assert len(conn.updates) == 1
    assert conn.updates[0]["update"].field_meta == {"code4me2": {"phase": "delta"}}


def test_agent_message_delta_failure_is_telemetry_not_a_turn_failure() -> None:
    telemetry = _RecordingTelemetry()
    sink = AcpSessionEventSink(
        conn=_RecordingConn(fail=True),
        session_id="session-1",
        updates=AcpUpdateBuilder(),
        telemetry=telemetry,
        async_runner=_BlockingRunner(),
    )
    # Must not raise: a dropped chunk costs telemetry, not the turn.
    sink.agent_message_delta("hi", message_id="msg-1", run_id="r", request_id="q")
    assert [e["event_type"] for e in telemetry.events] == ["agent.acp.update_failed"]


# ── orphan tool calls on cancel ───────────────────────────────────────────────


def _assistant_tool_message(*ids: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "name": "read_file", "arguments": {"path": "a.txt"}}
            for call_id in ids
        ],
    }


def test_discard_trailing_orphan_removes_only_unanswered_tail() -> None:
    memory = MemoryWindow(strategy="last_messages", max_messages=20, max_tokens=1000)
    memory.append({"role": "user", "content": "hi"})
    memory.append(_assistant_tool_message("answered"))
    memory.append({"role": "tool", "tool_call_id": "answered", "content": "{}"})
    memory.append(_assistant_tool_message("orphan"))

    assert memory.discard_trailing_orphan_tool_calls() == 1
    snapshot = memory.snapshot()
    assert snapshot[-1].get("role") == "tool"
    # Second call is a no-op: the completed pair is untouched.
    assert memory.discard_trailing_orphan_tool_calls() == 0


def test_discard_trailing_orphan_keeps_partially_answered_batch() -> None:
    memory = MemoryWindow(strategy="last_messages", max_messages=20, max_tokens=1000)
    memory.append(_assistant_tool_message("t1", "t2"))
    memory.append({"role": "tool", "tool_call_id": "t1", "content": "{}"})
    assert memory.discard_trailing_orphan_tool_calls() == 0


def test_discard_trailing_orphan_handles_legacy_json_shape() -> None:
    memory = MemoryWindow(strategy="last_messages", max_messages=20, max_tokens=1000)
    memory.append(
        {
            "role": "assistant",
            "content": json.dumps(
                {"tool_calls": [{"id": "x", "name": "read_file", "arguments": {}}]}
            ),
        }
    )
    assert memory.discard_trailing_orphan_tool_calls() == 1
    assert memory.snapshot() == []


def test_cancelled_result_discards_orphans() -> None:
    memory = MemoryWindow(strategy="last_messages", max_messages=20, max_tokens=1000)
    memory.append({"role": "user", "content": "hi"})
    memory.append(_assistant_tool_message("orphan"))
    result = _cancelled_result(memory=memory)
    assert (result.stop_reason, result.run_status, result.final_response) == (
        "cancelled",
        "cancelled",
        "",
    )
    assert [m.get("role") for m in memory.snapshot()] == ["user"]


# ── cancellable waits ─────────────────────────────────────────────────────────


def test_per_step_approval_cancel_does_not_block() -> None:
    cancel = Event()
    cancel.set()
    registry = ToolRegistry(
        file_tools=object(),
        command_tools=object(),
        allowed_tools={"run_command"},
        approval_policy="per_step",
        event_sink=SimpleNamespace(tool_call=lambda event: None),
    )
    with pytest.raises(ToolRegistryError) as raised:
        registry.execute(
            ToolCall(
                tool_call_id="t1",
                name="run_command",
                arguments={"argv": ["ls"], "cwd": "."},
            ),
            run_id="run-1",
            request_id="request-1",
            cancellation_event=cancel,
        )
    assert raised.value.failure_reason == "approval_cancelled"


def test_local_subprocess_cancel_returns_promptly(tmp_path: Path) -> None:
    tools = WorkspaceCommandTools(_test_config(tmp_path))
    cancel = Event()
    cancel.set()
    # Cancelled local runs kill promptly and raise like the ACP/MCP paths,
    # instead of returning an empty completed result.
    with pytest.raises(asyncio.CancelledError):
        tools._run_local(
            argv=["echo", "hi"],
            cwd=tmp_path,
            cancellation_event=cancel,
        )


def test_mcp_execute_cancel_raises_before_blocking(tmp_path: Path) -> None:
    broker = StdioMcpToolBroker(servers=[], cwd=tmp_path)
    broker._tools["mcp__toy__toy_add"] = _McpTool(
        public_name="mcp__toy__toy_add",
        server_name="toy",
        remote_name="toy_add",
        definition={},
    )
    loop = asyncio.new_event_loop()
    broker._loop = loop
    try:
        cancel = Event()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            broker.execute(
                "mcp__toy__toy_add", {"a": 1, "b": 2}, cancellation_event=cancel
            )
    finally:
        loop.close()


def test_run_with_cancellation_supports_single_arg_runners() -> None:
    from code4me2_agent.async_bridge import run_with_cancellation

    async def _answer() -> str:
        return "ok"

    assert (
        run_with_cancellation(_answer(), SimpleNamespace(run=lambda aw: asyncio.run(aw)), None)
        == "ok"
    )
    cancel = Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        run_with_cancellation(
            _answer(), SimpleNamespace(run=lambda aw: asyncio.run(aw)), cancel
        )


# ── edit_seq ──────────────────────────────────────────────────────────────────


def test_edit_outputs_carry_monotonic_edit_seq(tmp_path: Path) -> None:
    tools = WorkspaceFileTools(_test_config(tmp_path))
    created = tools.create_file(path="a.txt", content="one")
    written = tools.write_file(path="a.txt", content="two")
    replaced = tools.replace_text(path="a.txt", old_text="two", new_text="three")

    assert created.edit_seq is not None and written.edit_seq is not None
    assert replaced.edit_seq is not None
    assert created.edit_seq < written.edit_seq < replaced.edit_seq
    assert created.content_hash == hashlib.sha256(b"one").hexdigest()[:16]
    assert isinstance(written.mtime_ns, int)


# ── toy MCP fixture ───────────────────────────────────────────────────────────


def test_toy_mcp_server_executes_add(tmp_path: Path) -> None:
    toy_path = Path(__file__).with_name("mcp_toy_server.py")
    broker = StdioMcpToolBroker.open(
        [
            {
                "name": "toy",
                "command": sys.executable,
                "args": [str(toy_path)],
                "env": [],
            }
        ],
        cwd=tmp_path,
    )
    assert broker is not None
    try:
        assert "mcp__toy__toy_add" in [
            d["function"]["name"] for d in broker.definitions()
        ]
        result = broker.execute("mcp__toy__toy_add", {"a": 40, "b": 2})
        assert result["status"] == "completed"
        assert "42" in json.dumps(result)
    finally:
        broker.close()


# ── managed SSE streaming ───────────────────────────────────────────────────


class _FakeManagedSse:
    """Parsed-event stand-in for the runtime_auth SSE reader."""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.closed = False

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._events)

    def close(self) -> None:
        self.closed = True


def _managed_provider(
    stream_fn: Any,
    *,
    single_shot: Any | None = None,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        kind="managed_backend",
        base_url="http://127.0.0.1:9",
        model="managed-model",
        api_key_env="CODE4ME_TEST_MISSING_API_KEY",
        timeout_seconds=5.0,
        session_id="managed-session",
        managed_request=single_shot,
        managed_request_stream=stream_fn,
    )


def test_generate_stream_managed_coalesces_and_reassembles() -> None:
    seen: list[dict] = []
    stream = _FakeManagedSse(
        [
            {"choices": [{"delta": {"content": "Hello "}}], "model": "managed-model"},
            {
                "choices": [
                    {
                        "delta": {
                            "content": "world",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"x"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                    "total_tokens": 12,
                },
                "model": "managed-model",
            },
        ]
    )

    def _stream_fn(
        *, run_id: str, session_id: str, model_request: dict
    ) -> _FakeManagedSse:
        seen.append(model_request)
        assert session_id == "managed-session"
        return stream

    provider = _managed_provider(_stream_fn)
    deltas: list[str] = []
    turn = provider.generate_stream(
        [{"role": "user", "content": "hi"}],
        run_id="run-1",
        on_delta=deltas.append,
    )

    assert seen and seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}
    assert turn.output["final_answer"] == "Hello world"
    assert turn.output["tool_calls"] == [
        {"id": "call_1", "name": "read_file", "arguments": {"path": "x"}}
    ]
    assert turn.finish_reason == "tool_calls"
    assert turn.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }
    assert deltas and "".join(deltas) == "Hello world"
    assert stream.closed is True


def test_generate_stream_managed_cancel_closes_stream() -> None:
    opened = {"n": 0}

    def _stream_fn(**kwargs: Any) -> Any:
        opened["n"] += 1
        return _FakeManagedSse([{"choices": [{"delta": {"content": "partial"}}]}])

    provider = _managed_provider(_stream_fn)
    cancel = Event()
    cancel.set()
    with pytest.raises(ProviderStreamCancelledError):
        provider.generate_stream(
            [{"role": "user", "content": "hi"}], cancellation_event=cancel
        )
    # Already cancelled: no connection is opened at all.
    assert opened["n"] == 0


def test_generate_stream_managed_cancel_mid_stream_closes_stream() -> None:
    stream = _FakeManagedSse(
        [
            {"choices": [{"delta": {"content": "a" * 40}}]},
            {
                "choices": [{"delta": {"content": "b"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        ]
    )
    provider = _managed_provider(lambda **kwargs: stream)
    cancel = Event()

    def _on_delta(text: str) -> None:
        cancel.set()

    with pytest.raises(ProviderStreamCancelledError):
        provider.generate_stream(
            [{"role": "user", "content": "hi"}],
            on_delta=_on_delta,
            cancellation_event=cancel,
        )
    assert stream.closed is True


def test_generate_stream_managed_old_server_falls_back_once() -> None:
    calls = {"stream": 0, "single": 0}

    def _stream_fn(**kwargs: Any) -> Any:
        calls["stream"] += 1
        raise ManagedStreamUnsupportedError("old server")

    def _single_shot(*, run_id: str, session_id: str, model_request: dict) -> dict:
        calls["single"] += 1
        assert model_request.get("stream") is None
        return {
            "model": "managed-model",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "managed ok"}}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    provider = _managed_provider(_stream_fn, single_shot=_single_shot)
    turn = provider.generate_stream(
        [{"role": "user", "content": "hi"}],
        run_id="run-1",
        on_delta=lambda _text: None,
    )
    assert turn.output["final_answer"] == "managed ok"
    assert calls == {"stream": 1, "single": 1}  # fallback once, never loops


def test_generate_stream_managed_401_propagates_for_reauth() -> None:
    def _stream_fn(**kwargs: Any) -> Any:
        raise AcpSessionExpired("expired")

    provider = _managed_provider(_stream_fn)
    with pytest.raises(AcpSessionExpired):
        provider.generate_stream([{"role": "user", "content": "hi"}])


def test_generate_stream_managed_pre_stream_error_is_provider_error() -> None:
    def _stream_fn(**kwargs: Any) -> Any:
        raise AcpAuthorizationFailure("rejected")

    provider = _managed_provider(_stream_fn)
    with pytest.raises(BackendProviderRequestError):
        provider.generate_stream([{"role": "user", "content": "hi"}])


def test_generate_stream_managed_mid_stream_error_is_provider_error() -> None:
    class _Breaking:
        closed = False

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise RuntimeError("connection reset")

        def close(self) -> None:
            self.closed = True

    breaking = _Breaking()
    provider = _managed_provider(lambda **kwargs: breaking)
    with pytest.raises(BackendProviderRequestError):
        provider.generate_stream([{"role": "user", "content": "hi"}])
    assert breaking.closed is True


def test_handle_prompt_streams_managed_when_on_delta(tmp_path: Path) -> None:
    stream = _FakeManagedSse(
        [
            {"choices": [{"delta": {"content": "streamed "}}]},
            {
                "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
        ]
    )

    def _stream_fn(
        *, run_id: str, session_id: str, model_request: dict
    ) -> _FakeManagedSse:
        assert model_request["stream"] is True
        return stream

    def _must_not_run(**kwargs: Any) -> Any:
        raise AssertionError("single-shot generate() must not run when streaming")

    provider = _managed_provider(_stream_fn, single_shot=_must_not_run)
    adapter = OpenAICompatibleReactAdapter(
        _test_config(tmp_path),
        telemetry=SimpleNamespace(record=lambda **kwargs: None),
        tool_registry=SimpleNamespace(definitions=list, known_tool_names=set),
    )
    adapter._provider = lambda: provider  # type: ignore[method-assign]
    deltas: list[str] = []
    result = adapter.handle_prompt(
        prompt="hello",
        run_id="run-1",
        request_id="request-1",
        message_id="msg-1",
        memory=MemoryWindow(strategy="last_messages", max_messages=20, max_tokens=1000),
        on_delta=deltas.append,
    )
    assert result.final_response == "streamed answer"
    assert result.stop_reason == "end_turn"
    assert "".join(deltas) == "streamed answer"
    assert stream.closed is True


# ── runtime_auth managed SSE transport ────────────────────────────────────────


class _FakeHttpResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.closed = False
        self.status = 200

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self) -> None:
        self.closed = True


def _auth() -> AcpBackendAuthorization:
    return AcpBackendAuthorization(
        backend_url="http://127.0.0.1:9", grant=None, acp_token="secret-token"
    )


def test_managed_stream_posts_stream_true_and_parses_incrementally(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    seen: dict[str, Any] = {}
    response = _FakeHttpResponse(
        [
            b": keep-alive\n",
            b"\n",
            b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n',
            b"\n",
            b"data: not-json\n",
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        ]
    )

    def _urlopen(http_request: Any, timeout: Any = None) -> _FakeHttpResponse:
        seen["request"] = http_request
        seen["timeout"] = timeout
        return response

    import code4me2_agent.runtime_auth as runtime_auth_mod

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
    with caplog.at_level(logging.INFO, logger="code4me2_agent.runtime_auth"):
        stream = _auth().managed_inference_stream(
            run_id="run-1",
            session_id="session-1",
            model_request={"model": "m", "messages": []},
        )
        assert isinstance(stream, _ManagedSseStream)
        events = list(stream)

    payload = json.loads(seen["request"].data.decode("utf-8"))
    assert payload["run_id"] == "run-1"
    assert payload["session_id"] == "session-1"
    assert payload["request"]["stream"] is True
    assert seen["request"].get_header("Authorization") == "Bearer secret-token"
    assert seen["timeout"] == 120.0
    assert events == [{"choices": [{"delta": {"content": "Hi"}}]}]
    assert response.closed is True
    # Status/lengths only: no credential or body content in the logs.
    assert "secret-token" not in caplog.text
    assert '"content": "Hi"' not in caplog.text


def test_managed_stream_old_server_400_maps_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    def _urlopen(http_request: Any, timeout: Any = None) -> Any:
        raise urllib_error.HTTPError(
            http_request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b"Managed protocol v1 requires non-streaming inference"),
        )

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
    with pytest.raises(ManagedStreamUnsupportedError):
        _auth().managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )


def test_managed_stream_401_maps_to_reauth_and_other_errors_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    def _denied(http_request: Any, timeout: Any = None) -> Any:
        raise urllib_error.HTTPError(
            http_request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _denied)
    with pytest.raises(AcpSessionExpired):
        _auth().managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )

    def _broken(http_request: Any, timeout: Any = None) -> Any:
        raise urllib_error.HTTPError(
            http_request.full_url, 500, "Boom", {}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _broken)
    with pytest.raises(AcpAuthorizationFailure):
        _auth().managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )


def test_managed_bridge_stream_reauthenticates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    calls = {"n": 0}
    sentinel: Any = object()

    def _flaky(
        self: Any,
        *,
        run_id: str,
        session_id: str,
        model_request: dict,
        cancellation_event: Any | None = None,
    ) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AcpSessionExpired("expired")
        return sentinel

    monkeypatch.setattr(
        runtime_auth_mod.AcpBackendAuthorization,
        "managed_inference_stream",
        _flaky,
    )
    bridge = ManagedBridgeAuthorization()
    bridge._workspace_root = Path("/tmp")  # type: ignore[attr-defined]
    monkeypatch.setattr(bridge, "prepare_workspace", lambda workspace: None)
    monkeypatch.setattr(
        bridge, "authenticate", lambda: SimpleNamespace(project_id="p", workspace="w")
    )
    assert (
        bridge.managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )
        is sentinel
    )
    assert calls["n"] == 2


# ── exact 400 classifier (old-server stream rejection) ───────────────────────


def _http_error(
    url: str, status: int, body: bytes
) -> urllib_error.HTTPError:
    return urllib_error.HTTPError(
        url, status, "Bad Request", {}, io.BytesIO(body)
    )


def test_managed_stream_old_server_400_json_detail_maps_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    def _urlopen(http_request: Any, timeout: Any = None) -> Any:
        raise _http_error(
            http_request.full_url,
            400,
            json.dumps(
                {"detail": "Managed protocol v1 requires non-streaming inference"}
            ).encode("utf-8"),
        )

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
    with pytest.raises(ManagedStreamUnsupportedError):
        _auth().managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )


def test_managed_stream_400_with_other_detail_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the exact stream-rejection detail falls back; other 400s — even
    ones mentioning streams — stay hard authorization failures."""
    import code4me2_agent.runtime_auth as runtime_auth_mod

    bodies = [
        json.dumps(
            {"detail": "Managed protocol v1 requires a Chat Completions request"}
        ).encode("utf-8"),
        json.dumps({"detail": "upstream stream error"}).encode("utf-8"),
        b"upstream streaming failed",
    ]
    for body in bodies:
        def _urlopen(http_request: Any, timeout: Any = None, _body: bytes = body) -> Any:
            raise _http_error(http_request.full_url, 400, _body)

        monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
        with pytest.raises(AcpAuthorizationFailure):
            _auth().managed_inference_stream(
                run_id="run-1", session_id="s", model_request={"messages": []}
            )


def test_managed_stream_400_unreadable_body_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    class _Unreadable(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            raise OSError("gone")

    def _urlopen(http_request: Any, timeout: Any = None) -> Any:
        raise urllib_error.HTTPError(
            http_request.full_url, 400, "Bad Request", {}, _Unreadable(b"x")
        )

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
    with pytest.raises(AcpAuthorizationFailure):
        _auth().managed_inference_stream(
            run_id="run-1", session_id="s", model_request={"messages": []}
        )


# ── cancel during managed SSE ─────────────────────────────────────────────────


def test_managed_stream_cancelled_before_open_skips_urlopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import code4me2_agent.runtime_auth as runtime_auth_mod

    calls = {"n": 0}

    def _urlopen(http_request: Any, timeout: Any = None) -> Any:
        calls["n"] += 1
        raise AssertionError("urlopen must not run when already cancelled")

    monkeypatch.setattr(runtime_auth_mod.request, "urlopen", _urlopen)
    cancel = Event()
    cancel.set()
    with pytest.raises(ProviderStreamCancelledError):
        _auth().managed_inference_stream(
            run_id="run-1",
            session_id="s",
            model_request={"messages": []},
            cancellation_event=cancel,
        )
    assert calls["n"] == 0


class _BlockingHttpResponse:
    """readline blocks until the connection is closed, like a hung socket read."""

    def __init__(self) -> None:
        self.closed = False
        self._unblock = threading.Event()

    def readline(self) -> bytes:
        self._unblock.wait(timeout=10.0)
        if self.closed:
            raise OSError("socket closed")
        return b""

    def close(self) -> None:
        self.closed = True
        self._unblock.set()


def test_managed_sse_cancel_unblocks_hung_read() -> None:
    response = _BlockingHttpResponse()
    cancel = Event()
    stream = _ManagedSseStream(response, url="http://127.0.0.1:9", cancellation_event=cancel)
    errors: list[BaseException] = []

    def _consume() -> None:
        try:
            list(stream)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=_consume, daemon=True)
    started = time.monotonic()
    worker.start()
    time.sleep(0.2)  # let the read block
    cancel.set()
    worker.join(timeout=5.0)
    elapsed = time.monotonic() - started

    assert not worker.is_alive()
    assert response.closed is True
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderStreamCancelledError)
    assert elapsed < 5.0


# ── clean-EOF truncation ──────────────────────────────────────────────────────


def test_generate_stream_managed_clean_eof_without_terminator_is_truncation() -> None:
    stream = _FakeManagedSse([{"choices": [{"delta": {"content": "partial"}}]}])
    provider = _managed_provider(lambda **kwargs: stream)
    with pytest.raises(BackendProviderRequestError):
        provider.generate_stream([{"role": "user", "content": "hi"}])
    assert stream.closed is True


def test_generate_stream_managed_done_terminator_without_finish_or_usage_is_ok() -> None:
    response = _FakeHttpResponse(
        [
            b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n',
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        ]
    )
    stream = _ManagedSseStream(response, url="http://127.0.0.1:9")
    provider = _managed_provider(lambda **kwargs: stream)
    turn = provider.generate_stream([{"role": "user", "content": "hi"}])
    assert turn.output["final_answer"] == "Hi"
    assert stream.done_received is True
    assert response.closed is True
