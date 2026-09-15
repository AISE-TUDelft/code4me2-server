from __future__ import annotations

import json
import logging
import os
import platform
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from time import perf_counter, sleep, time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from openai import APIError, APIStatusError, OpenAI

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from threading import Event

    from code4me2_agent.command_tools import WorkspaceCommandTools
    from code4me2_agent.config import AgentConfig
    from code4me2_agent.events import AgentEventSink
    from code4me2_agent.file_tools import WorkspaceFileTools
    from code4me2_agent.mcp_tools import StdioMcpToolBroker
    from code4me2_agent.telemetry import AgentTelemetryRecorder


@dataclass(frozen=True)
class AdapterResult:
    final_response: str
    stop_reason: str
    run_status: str
    thoughts: tuple[str, ...] = ()


class AgentAdapter(Protocol):
    def handle_prompt(
        self,
        *,
        prompt: str,
        run_id: str,
        request_id: str,
        message_id: str | None,
        memory: "MemoryWindow | None" = None,
        cancellation_event: Event | None = None,
        on_delta: Any | None = None,
    ) -> AdapterResult: ...


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ParsedProviderOutput:
    tool_calls: list[ToolCall]
    final_answer: str | None
    thought: str | None = None


@dataclass(frozen=True)
class ProviderTurn:
    output: dict[str, Any]
    usage: dict[str, int]
    finish_reason: str | None
    model: str
    request_payload: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None


class FakeProviderExhaustedError(RuntimeError):
    pass


class BackendProviderRequestError(RuntimeError):
    pass


class ProviderStreamCancelledError(RuntimeError):
    """Raised when a cancellable provider wait observes cancellation."""

    pass


def _stream_enabled() -> bool:
    """Feature flag for provider streaming; default on, opt-out via env."""
    raw = os.getenv("CODE4ME_STREAM_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _turn_was_cancelled(cancellation_event: Event | None) -> bool:
    return cancellation_event is not None and cancellation_event.is_set()


def _cancelled_result(
    thoughts: list[str] | tuple[str, ...] = (),
    memory: MemoryWindow | None = None,
) -> AdapterResult:
    if memory is not None:
        try:
            memory.discard_trailing_orphan_tool_calls()
        except Exception:
            pass
    return AdapterResult(
        final_response="",
        stop_reason="cancelled",
        run_status="cancelled",
        thoughts=tuple(thoughts),
    )


def _rate_limit_provider_request_from_env() -> None:
    state_path_text = os.getenv("CODE4ME_RATE_LIMIT_STATE_PATH", "").strip()
    if not state_path_text:
        return
    limit = _positive_int_env("CODE4ME_MODEL_REQUEST_LIMIT", 5)
    window_seconds = _positive_float_env("CODE4ME_MODEL_REQUEST_WINDOW_SECONDS", 70.0)
    _rate_limit_provider_request(
        state_path=Path(state_path_text).expanduser(),
        limit=limit,
        window_seconds=window_seconds,
    )


def _rate_limit_provider_request(
    *,
    state_path: Path,
    limit: int,
    window_seconds: float,
    now_fn=time,
    sleep_fn=sleep,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            while True:
                now = float(now_fn())
                timestamps = _read_rate_limit_timestamps(state_path)
                timestamps = sorted(
                    timestamp
                    for timestamp in timestamps
                    if now - timestamp < window_seconds
                )
                if len(timestamps) < limit:
                    timestamps.append(now)
                    _write_rate_limit_timestamps(state_path, timestamps)
                    return
                sleep_seconds = max(0.0, timestamps[0] + window_seconds - now)
                logging.info(
                    "Model request rate limit reached: limit=%s window_seconds=%s sleeping=%.3fs",
                    limit,
                    window_seconds,
                    sleep_seconds,
                )
                sleep_fn(sleep_seconds)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_rate_limit_timestamps(state_path: Path) -> list[float]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw_timestamps = data.get("timestamps", [])
    if not isinstance(raw_timestamps, list):
        return []
    timestamps: list[float] = []
    for value in raw_timestamps:
        try:
            timestamps.append(float(value))
        except (TypeError, ValueError):
            continue
    return timestamps


def _write_rate_limit_timestamps(state_path: Path, timestamps: list[float]) -> None:
    state_path.write_text(
        json.dumps({"timestamps": timestamps}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def _non_negative_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


class ToolRegistryError(RuntimeError):
    def __init__(self, message: str, *, failure_reason: str) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


class DeterministicEchoAdapter:
    def handle_prompt(
        self,
        *,
        prompt: str,
        run_id: str,
        request_id: str,
        message_id: str | None,
        memory: "MemoryWindow | None" = None,
        cancellation_event: Event | None = None,
        on_delta: Any | None = None,
    ) -> AdapterResult:
        if _turn_was_cancelled(cancellation_event):
            return _cancelled_result(memory=memory)
        return AdapterResult(
            final_response=f"Code4Me ACP echo: {prompt}",
            stop_reason="end_turn",
            run_status="completed",
        )


class ToolRegistry:
    def __init__(
        self,
        file_tools: WorkspaceFileTools,
        command_tools: WorkspaceCommandTools,
        *,
        allowed_tools: frozenset[str] | list[str] | None = None,
        event_sink: AgentEventSink | None = None,
        mcp_tools: StdioMcpToolBroker | None = None,
        approval_policy: str = "auto",
    ) -> None:
        from code4me2_agent.events import (
            NoopAgentEventSink,
            ThoughtEvent,
            ToolCallEvent,
        )

        self._file_tools = file_tools
        self._command_tools = command_tools
        self._mcp_tools = mcp_tools
        self._allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None
        self._approval_policy = approval_policy
        self._event_sink = event_sink or NoopAgentEventSink()
        self._event_type = ToolCallEvent
        self._thought_event_type = ThoughtEvent

    def execute(
        self, tool_call: ToolCall, *, run_id: str, request_id: str,
        cancellation_event: Event | None = None,
    ) -> dict[str, Any]:
        name = tool_call.name
        mcp_wildcard_allowed = (
            name.startswith("mcp__")
            and self._allowed_tools is not None
            and "mcp__*" in self._allowed_tools
        )
        if (
            self._allowed_tools is not None
            and name not in self._allowed_tools
            and not mcp_wildcard_allowed
        ):
            raise ToolRegistryError(
                f"Tool is disabled by the assigned study policy: {name}",
                failure_reason="tool_not_allowed",
            )
        if self._approval_policy not in {"auto", "per_step", "suggestion_only"}:
            raise ToolRegistryError(
                f"Unknown assigned approval policy: {self._approval_policy}",
                failure_reason="invalid_approval_policy",
            )
        if self._approval_policy == "suggestion_only" and (
            _requires_manual_approval(name)
        ):
            raise ToolRegistryError(
                f"Tool execution is disabled by suggestion-only policy: {name}",
                failure_reason="approval_policy_denied",
            )
        arguments = dict(tool_call.arguments)
        edit_preview = self._edit_preview(
            tool_call,
            arguments,
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
        )
        self._emit_tool_start(
            tool_call,
            arguments,
            edit_preview=edit_preview,
            run_id=run_id,
            request_id=request_id,
        )
        if self._approval_policy == "per_step" and _requires_manual_approval(name):
            if cancellation_event is not None and cancellation_event.is_set():
                raise ToolRegistryError(
                    "Tool approval cancelled.",
                    failure_reason="approval_cancelled",
                )
            request_approval = getattr(self._event_sink, "request_approval", None)
            decision = (
                request_approval(tool_call, arguments)
                if callable(request_approval)
                else None
            )
            if not getattr(decision, "accepted", bool(decision)):
                outcome = getattr(decision, "decision", "unavailable")
                self._emit_tool_failed(
                    tool_call,
                    arguments,
                    edit_preview=edit_preview,
                    run_id=run_id,
                    request_id=request_id,
                    message=f"Not run: approval {outcome}.",
                )
                raise ToolRegistryError(
                    f"Tool approval {outcome} for: {name}",
                    failure_reason=f"approval_{outcome}",
                )
        try:
            if name == "read_file":
                result = _call_file_tool(
                    self._file_tools.read_file,
                    path=str(arguments["path"]),
                    line_start=_optional_int(arguments.get("line_start")),
                    line_end=_optional_int(arguments.get("line_end")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif name == "create_file":
                result = _call_file_tool(
                    self._file_tools.create_file,
                    path=str(arguments["path"]),
                    content=str(arguments.get("content", "")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif name == "write_file":
                result = _call_file_tool(
                    self._file_tools.write_file,
                    path=str(arguments["path"]),
                    content=str(arguments.get("content", "")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif name == "replace_text":
                result = _call_file_tool(
                    self._file_tools.replace_text,
                    path=str(arguments["path"]),
                    old_text=str(arguments.get("old_text", "")),
                    new_text=str(arguments.get("new_text", "")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif name == "list_files":
                result = self._file_tools.list_files(
                    path=str(arguments.get("path", ".")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                )
            elif name == "search_files":
                result = self._file_tools.search_files(
                    query=str(arguments["query"]),
                    path=str(arguments.get("path", ".")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                )
            elif name == "run_command":
                result = self._command_tools.run_command(
                    argv=arguments["argv"],
                    cwd=str(arguments.get("cwd", ".")),
                    tool_call_id=tool_call.tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif self._mcp_tools is not None and self._mcp_tools.has_tool(name):
                result = self._mcp_tools.execute(
                    name, arguments, cancellation_event=cancellation_event
                )
            else:
                raise ToolRegistryError(
                    f"Unsupported tool name: {name}",
                    failure_reason="unsupported_tool",
                )
        except Exception:
            self._emit_tool_failed(
                tool_call,
                arguments,
                edit_preview=edit_preview,
                run_id=run_id,
                request_id=request_id,
            )
            raise
        tool_output = asdict(result) if is_dataclass(result) else dict(result)
        self._emit_tool_completed(
            tool_call,
            arguments,
            tool_output,
            edit_preview=edit_preview,
            run_id=run_id,
            request_id=request_id,
        )
        return tool_output
    def definitions(self) -> list[dict[str, Any]]:
        definitions = _tool_definitions()
        if self._mcp_tools is not None:
            definitions.extend(self._mcp_tools.definitions())
        if self._allowed_tools is not None:
            definitions = [
                definition
                for definition in definitions
                if (
                    str(definition.get("function", {}).get("name", ""))
                    in self._allowed_tools
                    or (
                        "mcp__*" in self._allowed_tools
                        and str(definition.get("function", {}).get("name", "")).startswith("mcp__")
                    )
                )
            ]
        return definitions

    def set_allowed_tools(self, allowed_tools: frozenset[str] | list[str] | None) -> None:
        self._allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None

    def known_tool_names(self) -> set[str]:
        names: set[str] = set()
        for tool in self.definitions():
            function = tool.get("function")
            if isinstance(function, dict):
                name = str(function.get("name", "")).strip()
                if name:
                    names.add(name)
        return names

    def _emit_tool_start(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        *,
        edit_preview: dict[str, str | None] | None = None,
        run_id: str,
        request_id: str,
    ) -> None:
        metadata = _tool_event_metadata(tool_call.name, arguments)
        if metadata is None:
            return
        self._event_sink.tool_call(
            self._event_type(
                phase="started",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id=run_id,
                request_id=request_id,
                title=metadata["title"],
                kind=metadata["kind"],
                status="pending",
                path=metadata.get("path"),
                diff_old_text=edit_preview.get("old_text") if edit_preview else None,
                diff_new_text=edit_preview.get("new_text") if edit_preview else None,
                raw_input=metadata.get("raw_input"),
            )
        )

    def _emit_tool_completed(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        tool_output: dict[str, Any],
        *,
        edit_preview: dict[str, str | None] | None = None,
        run_id: str,
        request_id: str,
    ) -> None:
        metadata = _tool_event_metadata(tool_call.name, arguments)
        if metadata is None:
            return
        self._event_sink.tool_call(
            self._event_type(
                phase="completed",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id=run_id,
                request_id=request_id,
                title=metadata["title"],
                kind=metadata["kind"],
                status="completed",
                path=metadata.get("path"),
                diff_old_text=edit_preview.get("old_text") if edit_preview else None,
                diff_new_text=edit_preview.get("new_text") if edit_preview else None,
                content_text=None if edit_preview else metadata.get("content_text"),
                raw_output=tool_output,
            )
        )

    def _emit_tool_failed(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        *,
        run_id: str,
        request_id: str,
        message: str | None = None,
        edit_preview: dict[str, str | None] | None = None,
    ) -> None:
        metadata = _tool_event_metadata(tool_call.name, arguments)
        if metadata is None:
            return
        self._event_sink.tool_call(
            self._event_type(
                phase="failed",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id=run_id,
                request_id=request_id,
                title=metadata["title"],
                kind=metadata["kind"],
                status="failed",
                path=metadata.get("path"),
                diff_old_text=edit_preview.get("old_text") if edit_preview else None,
                diff_new_text=edit_preview.get("new_text") if edit_preview else None,
                content_text=message or f"{metadata.get('content_text', metadata['title'])} failed",
            )
        )

    def _edit_preview(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        *,
        run_id: str,
        request_id: str,
        cancellation_event: Any | None = None,
    ) -> dict[str, str | None] | None:
        name = tool_call.name
        if name not in {"create_file", "write_file", "replace_text"}:
            return None
        path = str(arguments["path"])
        if name == "create_file":
            return {"old_text": None, "new_text": str(arguments.get("content", ""))}
        try:
            old_text = _call_file_tool(
                self._file_tools.read_file,
                path=path,
                tool_call_id=tool_call.tool_call_id,
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
            ).content
        except FileNotFoundError:
            old_text = ""
        if name == "replace_text":
            old_fragment = str(arguments.get("old_text", ""))
            if old_fragment not in old_text:
                raise ValueError("old_text was not found in the file.")
            new_text = old_text.replace(old_fragment, str(arguments.get("new_text", "")), 1)
        else:
            new_text = str(arguments.get("content", ""))
        return {"old_text": old_text, "new_text": new_text}


def _requires_manual_approval(tool_name: str) -> bool:
    return tool_name in {
        "create_file",
        "write_file",
        "replace_text",
        "run_command",
    } or tool_name.startswith("mcp__")


def _call_file_tool(operation: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke a file-tool operation, tolerating older fakes without cancel support."""
    try:
        return operation(*args, **kwargs)
    except TypeError as exc:
        if "cancellation_event" not in str(exc):
            raise
        kwargs.pop("cancellation_event", None)
        return operation(*args, **kwargs)


class FakeOpenAICompatibleProvider:
    def __init__(self, script: list[dict[str, object]]) -> None:
        self._script = [dict(item) for item in script]

    def generate(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._script:
            raise FakeProviderExhaustedError(
                "Fake provider script exhausted before producing a final answer."
            )
        current = dict(self._script.pop(0))
        delay_seconds = current.pop("delay_seconds", None)
        if delay_seconds is not None:
            sleep(max(0.0, float(delay_seconds)))
        current["seen_messages"] = [dict(message) for message in messages]
        return current


def _managed_event_to_chunk(event: Any) -> Any:
    """Adapt one parsed managed SSE payload to an SDK-shaped chunk.

    Lets the managed streaming branch reuse the exact accumulate/coalesce/
    ``on_delta`` machinery below: ``choices[].delta.content`` /
    ``tool_calls``, ``finish_reason``, ``usage`` and ``model``.
    """
    choices: list[Any] = []
    usage: Any = None
    model: Any = None
    if isinstance(event, dict):
        for choice in event.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {}) or {}
            if not isinstance(delta, dict):
                delta = {}
            tool_calls: list[Any] = []
            for tool_call in delta.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function", {}) or {}
                if not isinstance(function, dict):
                    function = {}
                tool_calls.append(
                    SimpleNamespace(
                        index=tool_call.get("index", 0) or 0,
                        id=tool_call.get("id"),
                        function=SimpleNamespace(
                            name=function.get("name"),
                            arguments=function.get("arguments"),
                        ),
                    )
                )
            choices.append(
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=delta.get("content"),
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason=choice.get("finish_reason"),
                )
            )
        usage = event.get("usage")
        model = event.get("model")
    return SimpleNamespace(choices=choices, usage=usage, model=model)


def _iter_managed_sse_chunks(sse_events: Any) -> Any:
    """Yield SDK-shaped chunks from parsed managed SSE payloads.

    Mid-stream transport failures become :class:`BackendProviderRequestError`
    (a turn failure, never a client-side retry — the server owns the
    pre-stream 429 budget); the underlying SSE connection is always closed.
    Cancellation is polled per chunk by the consumer, which closes this
    iterator promptly.
    """
    try:
        iterator = iter(sse_events)
    except TypeError as exc:
        raise BackendProviderRequestError(
            f"The managed backend stream was unusable: {exc}"
        ) from exc
    try:
        for event in iterator:
            yield _managed_event_to_chunk(event)
    except GeneratorExit:
        raise
    except (BackendProviderRequestError, ProviderStreamCancelledError):
        raise
    except Exception as exc:
        raise BackendProviderRequestError(
            f"The managed backend stream failed mid-response: {exc}"
        ) from exc
    finally:
        close = getattr(sse_events, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _managed_stream_terminated(
    sse_events: Any,
    *,
    finish_reason: str | None,
    usage_payload: Any,
) -> bool:
    """Whether a managed SSE stream ended with a terminal signal.

    Exactly one of ``[DONE]`` (recorded on the stream reader), a
    finish_reason, or a usage payload must be present; a clean EOF with none
    of them means the response was cut off and must fail, not truncate
    silently. Streams without the ``done_received`` marker (older fakes) rely
    on finish_reason/usage alone.
    """
    if getattr(sse_events, "done_received", False):
        return True
    if isinstance(finish_reason, str) and finish_reason.strip():
        return True
    return isinstance(usage_payload, dict) and bool(usage_payload)


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        kind: str,
        base_url: str,
        model: str,
        api_key_env: str,
        timeout_seconds: float,
        auth_headers: dict[str, str] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        session_id: str = "",
        managed_request: Any | None = None,
        managed_request_stream: Any | None = None,
    ) -> None:
        self._kind = kind.strip() or "code4me_backend"
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key_env = api_key_env
        self._timeout_seconds = timeout_seconds
        self._auth_headers = dict(auth_headers or {})
        self._tool_definitions = list(tool_definitions or _tool_definitions())
        self._temperature = temperature
        self._session_id = session_id
        self._managed_request = managed_request
        self._managed_request_stream = managed_request_stream

    def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str = "",
        cancellation_event: Event | None = None,
    ) -> ProviderTurn:
        request_payload = {
            "model": self._model,
            "messages": [_to_openai_message(message) for message in messages],
            "tools": self._tool_definitions,
            "tool_choice": "auto",
        }
        if self._temperature is not None:
            request_payload["temperature"] = self._temperature

        if self._kind == "managed_backend":
            if not callable(self._managed_request):
                raise BackendProviderRequestError("Managed backend transport is unavailable.")
            try:
                response_payload = self._managed_request(
                    run_id=run_id,
                    session_id=self._session_id,
                    model_request=request_payload,
                )
            except Exception as exc:
                raise BackendProviderRequestError(
                    f"The managed backend model request failed: {exc}"
                ) from exc
            normalized_output = _normalize_openai_provider_response(response_payload)
            return ProviderTurn(
                output=normalized_output,
                usage=_normalize_usage(response_payload.get("usage"), messages, normalized_output),
                finish_reason=_response_finish_reason(response_payload),
                model=str(response_payload.get("model") or self._model),
                request_payload=request_payload,
                raw_response=response_payload,
            )

        client = self._client()
        max_429_retries = _non_negative_int_env("CODE4ME_BACKEND_429_MAX_RETRIES", 3)
        retry_sleep_seconds = _non_negative_float_env("CODE4ME_BACKEND_429_RETRY_SLEEP", 10.0)
        try:
            for attempt in range(1, max_429_retries + 2):
                logging.info(
                    "Agent model request via OpenAI SDK: kind=%s endpoint=%s model=%s",
                    self._kind,
                    f"{self._openai_base_url()}/chat/completions",
                    self._model,
                )
                try:
                    _rate_limit_provider_request_from_env()
                    raw_response = client.chat.completions.with_raw_response.create(**request_payload)
                    logging.info(
                        "Agent model response via OpenAI SDK: status=%s model=%s",
                        raw_response.http_response.status_code,
                        self._model,
                    )
                    response_payload = raw_response.http_response.json()
                    break
                except APIStatusError as exc:
                    if exc.status_code == 429 and attempt <= max_429_retries:
                        logging.info(
                            "Backend model request returned HTTP 429; sleeping %.3fs before retry %s/%s",
                            retry_sleep_seconds,
                            attempt,
                            max_429_retries,
                        )
                        if cancellation_event is not None and cancellation_event.wait(
                            timeout=retry_sleep_seconds
                        ):
                            raise ProviderStreamCancelledError(
                                "Provider retry wait was cancelled."
                            ) from None
                        sleep(retry_sleep_seconds) if cancellation_event is None else None
                        continue
                    raise BackendProviderRequestError(f"The backend model request failed with HTTP {exc.status_code}: {exc.response.text}") from None
        except ProviderStreamCancelledError:
            raise
        except APIError as exc:
            raise BackendProviderRequestError(f"The backend model request failed: {exc}") from None

        normalized_output = _normalize_openai_provider_response(response_payload)
        return ProviderTurn(
            output=normalized_output,
            usage=_normalize_usage(
                response_payload.get("usage"), messages, normalized_output
            ),
            finish_reason=_response_finish_reason(response_payload),
            model=str(response_payload.get("model") or self._model),
            request_payload=request_payload,
            raw_response=response_payload,
        )

    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str = "",
        on_delta: Any | None = None,
        cancellation_event: Event | None = None,
    ) -> ProviderTurn:
        """Streaming variant: accumulate content+tool deltas, coalesce on_delta.

        Calls ``on_delta(text)`` with coalesced fragments (>=40 chars or
        100ms). Checks cancellation per chunk and closes the stream promptly.
        Managed transports stream through the backend SSE relay when the
        session provides a stream callable, and fall back to single-shot
        ``generate()`` otherwise (or once against an old server).
        """
        import time as _time

        request_payload = {
            "model": self._model,
            "messages": [_to_openai_message(message) for message in messages],
            "tools": self._tool_definitions,
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._temperature is not None:
            request_payload["temperature"] = self._temperature

        sse_events: Any = None
        if self._kind == "managed_backend":
            stream_fn = self._managed_request_stream
            if not callable(stream_fn):
                # Wired without streaming: managed protocol v1 single-shot.
                return self.generate(messages, run_id=run_id, cancellation_event=cancellation_event)
            from code4me2_agent.runtime_auth import (
                AcpSessionExpired,
                ManagedStreamUnsupportedError,
            )

            if cancellation_event is not None and cancellation_event.is_set():
                # Check before opening: the transport would close the
                # connection on the first chunk anyway.
                raise ProviderStreamCancelledError("Provider stream was cancelled.")
            try:
                try:
                    sse_events = stream_fn(
                        run_id=run_id,
                        session_id=self._session_id,
                        model_request=dict(request_payload),
                        cancellation_event=cancellation_event,
                    )
                except TypeError as exc:
                    # Older stream callables take exactly
                    # (run_id, session_id, model_request); retry without the
                    # cancel hook and attach it afterwards when supported.
                    if "cancellation_event" not in str(exc):
                        raise
                    sse_events = stream_fn(
                        run_id=run_id,
                        session_id=self._session_id,
                        model_request=dict(request_payload),
                    )
            except ManagedStreamUnsupportedError:
                # Old server answered 400 stream-unsupported: fall back once
                # to single-shot. generate() never streams, so this cannot loop.
                logging.info(
                    "[Agent/provider] managed streaming unsupported — single-shot fallback"
                )
                return self.generate(messages, run_id=run_id, cancellation_event=cancellation_event)
            except (AcpSessionExpired, ProviderStreamCancelledError):
                # Re-authentication is owned by the authorization layer (the
                # bridge retries once); never mask it as a turn failure.
                raise
            except Exception as exc:
                raise BackendProviderRequestError(
                    f"The managed backend streaming request failed: {exc}"
                ) from exc
            # No managed 429 retry client-side: the server owns the pre-stream
            # budget, so any 429 surfacing here is already terminal.
            #
            # Late-attach the cancel hook for streams constructed without one
            # (older callables): attaching before the first chunk means the
            # cancel watcher is in place before any read can block.
            set_cancel = getattr(sse_events, "set_cancel_event", None)
            if callable(set_cancel) and cancellation_event is not None:
                try:
                    set_cancel(cancellation_event)
                except Exception as exc:
                    logging.warning(
                        "[Agent/provider] could not attach stream cancel hook: %s",
                        exc,
                    )
            stream = _iter_managed_sse_chunks(sse_events)
        else:
            client = self._client()
            try:
                _rate_limit_provider_request_from_env()
                stream = client.chat.completions.create(**request_payload)
            except APIError as exc:
                raise BackendProviderRequestError(f"The backend model request failed: {exc}") from None

        content_parts: list[str] = []
        tool_slots: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_payload: Any = None
        model_name = self._model
        pending_delta = ""
        last_emit = _time.monotonic()

        def _flush(force: bool = False) -> None:
            nonlocal pending_delta, last_emit
            # No receiver: drop without accumulating (avoids unbounded growth
            # when streaming is disabled) and never fail the turn.
            if on_delta is None:
                pending_delta = ""
                return
            if not pending_delta:
                return
            now = _time.monotonic()
            if force or len(pending_delta) >= 40 or (now - last_emit) >= 0.1:
                # Bounded by the sink (agent_message_delta times out after 5s
                # and records telemetry-only failures), so a hung client
                # cannot block the turn indefinitely.
                try:
                    on_delta(pending_delta)
                except Exception:
                    logging.warning(
                        "[Agent/provider] on_delta callback failed — continuing",
                        exc_info=True,
                    )
                pending_delta = ""
                last_emit = now

        try:
            for chunk in stream:
                if cancellation_event is not None and cancellation_event.is_set():
                    try:
                        close = getattr(stream, "close", None)
                        if callable(close):
                            close()
                    except Exception:
                        pass
                    raise ProviderStreamCancelledError("Provider stream was cancelled.")
                try:
                    choices = getattr(chunk, "choices", []) or []
                except Exception:
                    choices = []
                for choice in choices:
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        content = getattr(delta, "content", None)
                        if isinstance(content, str) and content:
                            content_parts.append(content)
                            # Skip delta accumulation when nobody receives it.
                            if on_delta is not None:
                                pending_delta += content
                                _flush()
                        raw_tool_calls = getattr(delta, "tool_calls", None)
                        if raw_tool_calls:
                            for tc in raw_tool_calls:
                                index = getattr(tc, "index", 0) or 0
                                slot = tool_slots.setdefault(
                                    int(index), {"id": None, "name": None, "arguments": ""}
                                )
                                tc_id = getattr(tc, "id", None)
                                if tc_id:
                                    slot["id"] = str(tc_id)
                                fn = getattr(tc, "function", None)
                                if fn is not None:
                                    fname = getattr(fn, "name", None)
                                    if fname:
                                        slot["name"] = str(fname)
                                    fargs = getattr(fn, "arguments", None)
                                    if isinstance(fargs, str):
                                        slot["arguments"] += fargs
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = str(fr)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    try:
                        usage_payload = chunk_usage.model_dump() if hasattr(chunk_usage, "model_dump") else dict(chunk_usage)
                    except Exception:
                        usage_payload = None
                chunk_model = getattr(chunk, "model", None)
                if chunk_model:
                    model_name = str(chunk_model)
            _flush(force=True)
        finally:
            try:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass

        # A managed stream that ends (clean EOF) without any terminal signal —
        # ``[DONE]``, a finish_reason, or a usage payload — was cut off
        # mid-response. Surfacing partial text as a final answer would silently
        # truncate the turn, so fail it instead.
        if self._kind == "managed_backend" and not _managed_stream_terminated(
            sse_events,
            finish_reason=finish_reason,
            usage_payload=usage_payload,
        ):
            raise BackendProviderRequestError(
                "The managed backend stream ended without a terminal signal "
                "([DONE], finish_reason, or usage); the response may be truncated."
            )

        # Reassemble a non-streaming-shaped payload for the existing normalizer.
        tool_calls_payload = []
        for index in sorted(tool_slots):
            slot = tool_slots[index]
            name = str(slot.get("name") or "").strip()
            if not name:
                continue
            raw_args = slot.get("arguments") or "{}"
            tool_call_id = slot.get("id")
            if not tool_call_id:
                # The provider never sent an id, so there is no provenance
                # for one: log the synthesis (name/index) rather than minting
                # it silently, since memory and tool results key on this id.
                logging.warning(
                    "[Agent/provider] streamed tool call without id (name=%s index=%s) — using synthesized id",
                    name,
                    index,
                )
                tool_call_id = f"tool-call-{index + 1}"
            tool_calls_payload.append(
                {
                    "id": str(tool_call_id),
                    "type": "function",
                    "function": {"name": name, "arguments": raw_args},
                }
            )
        response_payload = {
            "model": model_name,
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "content": "".join(content_parts),
                        "tool_calls": tool_calls_payload,
                    },
                }
            ],
            "usage": usage_payload,
        }
        normalized_output = _normalize_openai_provider_response(response_payload)
        return ProviderTurn(
            output=normalized_output,
            usage=_normalize_usage(usage_payload, messages, normalized_output),
            finish_reason=finish_reason,
            model=model_name,
            request_payload=request_payload,
            raw_response=None,
        )

    def _client(self) -> OpenAI:
        return _OpenAIClientWithoutSdkAuth(
            api_key="code4me-local-client",
            base_url=self._openai_base_url(),
            timeout=self._timeout_seconds,
            default_headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "code4me2-agent/0.1"}
        api_key = os.getenv(self._api_key_env, "").strip()
        if self._kind == "code4me_backend":
            headers.update(self._auth_headers)
        elif self._kind == "openai":
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif self._kind != "managed_backend":
            raise ValueError(f"Unsupported provider kind: {self._kind}")
        return headers

    def _openai_base_url(self) -> str:
        if self._kind == "code4me_backend":
            return f"{self._base_url}/api/acp"
        base_url = self._base_url
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return base_url

class _OpenAIClientWithoutSdkAuth(OpenAI):
    @property
    def auth_headers(self) -> dict[str, str]:
        return {}

class MemoryWindow:
    def __init__(self, *, strategy: str, max_messages: int, max_tokens: int) -> None:
        self._strategy = (
            strategy
            if strategy in {"last_messages", "token_window"}
            else "last_messages"
        )
        self._max_messages = max(1, max_messages)
        self._max_tokens = max(1, max_tokens)
        self._messages: list[dict[str, Any]] = []

    def append(self, message: dict[str, Any]) -> None:
        self._messages.append(dict(message))

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [dict(message) for message in messages]

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(message) for message in self._messages]

    def ensure_system_message(self, content: str) -> None:
        message = {"role": "system", "content": content}
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0] = message
            return
        self._messages.insert(0, message)

    def window(self) -> list[dict[str, Any]]:
        pinned_system, source_messages = self._split_pinned_system()
        max_messages = None if self._strategy == "token_window" else self._max_messages

        selected: list[dict[str, Any]] = []
        token_total = (
            _message_token_count(pinned_system) if pinned_system is not None else 0
        )
        for message in reversed(source_messages):
            message_tokens = _message_token_count(message)
            if selected and token_total + message_tokens > self._max_tokens:
                break
            if max_messages is not None and len(selected) >= max_messages:
                break
            selected.append(message)
            token_total += message_tokens
        selected.reverse()
        if pinned_system is not None:
            selected.insert(0, pinned_system)
        return selected

    def discard_trailing_orphan_tool_calls(self) -> int:
        """Strip trailing assistant tool_calls with no matching tool results.

        Called on every cancelled turn so a cancel between the assistant
        tool-call append and its result does not leave an orphan that would
        confuse the next model call. Only trailing orphans are removed;
        completed call/result pairs are untouched. Returns removals.
        """
        removed = 0
        while self._messages:
            last = self._messages[-1]
            if not isinstance(last, dict) or last.get("role") != "assistant":
                break
            pending_ids: set[str] = set()
            raw_calls = last.get("tool_calls")
            if isinstance(raw_calls, list) and raw_calls:
                for tc in raw_calls:
                    if isinstance(tc, dict) and tc.get("id"):
                        pending_ids.add(str(tc["id"]))
            if not pending_ids:
                # Legacy fake shape stores tool calls as JSON text.
                content = last.get("content")
                if isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    if isinstance(parsed, dict) and isinstance(
                        parsed.get("tool_calls"), list
                    ):
                        for tc in parsed["tool_calls"]:
                            if isinstance(tc, dict) and tc.get("id"):
                                pending_ids.add(str(tc["id"]))
            if not pending_ids:
                break
            # Check whether any later tool message already answered them —
            # by construction there is none (we only inspect the tail), but
            # scan forward defensively for result coverage.
            answered: set[str] = set()
            for msg in self._messages:
                if isinstance(msg, dict) and msg.get("role") == "tool":
                    tid = msg.get("tool_call_id")
                    if tid:
                        answered.add(str(tid))
            orphan_ids = pending_ids - answered
            if not orphan_ids:
                break
            # Only strip when ALL pending ids in the trailing message are
            # orphans (avoids half-removing a partially answered batch).
            if orphan_ids != pending_ids:
                break
            self._messages.pop()
            removed += 1
        return removed

    def _split_pinned_system(
        self,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if self._messages and self._messages[0].get("role") == "system":
            return dict(self._messages[0]), self._messages[1:]
        return None, self._messages


class OpenAICompatibleReactAdapter:
    def __init__(
        self,
        config: AgentConfig,
        *,
        telemetry: AgentTelemetryRecorder,
        tool_registry: ToolRegistry,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        from code4me2_agent.events import NoopAgentEventSink, ThoughtEvent

        self._config = config
        self._telemetry = telemetry
        self._tool_registry = tool_registry
        self._event_sink = event_sink or NoopAgentEventSink()
        self._thought_event_type = ThoughtEvent

    def handle_prompt(
        self,
        *,
        prompt: str,
        run_id: str,
        request_id: str,
        message_id: str | None,
        memory: "MemoryWindow | None" = None,
        cancellation_event: Event | None = None,
        on_delta: Any | None = None,
    ) -> AdapterResult:
        provider = self._provider()
        if memory is None:
            memory = MemoryWindow(
                strategy=self._config.adapter.memory_window.strategy,
                max_messages=self._config.adapter.memory_window.max_messages,
                max_tokens=self._config.adapter.memory_window.max_tokens,
            )
        memory.ensure_system_message(self._system_context())
        memory.append({"role": "user", "content": prompt})
        thoughts: list[str] = []
        for iteration in range(1, self._config.adapter.max_iterations + 1):
            if _turn_was_cancelled(cancellation_event):
                return _cancelled_result(thoughts, memory)
            messages = memory.window()
            thought_started_at = perf_counter()
            self._event_sink.thought(
                self._thought_event_type(
                    run_id=run_id,
                    request_id=request_id,
                    text="",
                    phase="started",
                )
            )
            self._telemetry.record(
                event_type="agent.model.requested",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "iteration": iteration,
                    "message_count": len(messages),
                    "approx_token_count": sum(
                        _message_token_count(message) for message in messages
                    ),
                },
                raw_payload={"messages": messages},
            )
            try:
                started_at = perf_counter()
                # 4
                if isinstance(provider, OpenAICompatibleProvider):
                    use_stream = _stream_enabled() and on_delta is not None
                    if use_stream:
                        try:
                            provider_turn = provider.generate_stream(
                                messages,
                                run_id=run_id,
                                on_delta=on_delta,
                                cancellation_event=cancellation_event,
                            )
                        except ProviderStreamCancelledError:
                            self._complete_thought_phase(
                                run_id=run_id,
                                request_id=request_id,
                                started_at=thought_started_at,
                            )
                            return _cancelled_result(thoughts, memory)
                    else:
                        provider_turn = provider.generate(
                            messages,
                            run_id=run_id,
                            cancellation_event=cancellation_event,
                        )
                    self._record_model_completed(
                        run_id=run_id,
                        request_id=request_id,
                        duration_ms=(perf_counter() - started_at) * 1000,
                        provider_turn=provider_turn,
                    )
                    output = provider_turn.output
                else:
                    if _turn_was_cancelled(cancellation_event):
                        self._complete_thought_phase(
                            run_id=run_id,
                            request_id=request_id,
                            started_at=thought_started_at,
                        )
                        return _cancelled_result(thoughts, memory)
                    output = provider.generate(messages)
            except ProviderStreamCancelledError:
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                return _cancelled_result(thoughts, memory)
            except FakeProviderExhaustedError:
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                self._record_loop_failure(
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="provider_exhausted",
                )
                return self._failed_result(
                    "The fake provider stopped before returning a final answer.",
                    stop_reason="provider_exhausted",
                )
            except BackendProviderRequestError as exc:
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                self._record_loop_failure(
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="backend_provider_request_failed",
                )
                return self._failed_result(
                    str(exc),
                    stop_reason="error",
                )
            except Exception as exc:
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                self._record_loop_failure(
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="provider_request_failed",
                )
                return self._failed_result(
                    f"The provider request failed: {exc}",
                    stop_reason="error",
                )
            if _turn_was_cancelled(cancellation_event):
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                return _cancelled_result(thoughts, memory)
            # 5
            parsed = self._parse_output(output, run_id=run_id, request_id=request_id)
            if parsed is None:
                self._complete_thought_phase(
                    run_id=run_id,
                    request_id=request_id,
                    started_at=thought_started_at,
                )
                return self._failed_result(
                    "The adapter could not parse the provider output.",
                    stop_reason="error",
                )
            if parsed.thought:
                thoughts.append(parsed.thought)
            self._complete_thought_phase(
                run_id=run_id,
                request_id=request_id,
                started_at=thought_started_at,
                text=parsed.thought or "",
            )
            if parsed.tool_calls:
                memory.append(
                    self._assistant_tool_call_message(
                        parsed.tool_calls,
                        structured=isinstance(provider, OpenAICompatibleProvider),
                    )
                )
                for tool_call in parsed.tool_calls:
                    if _turn_was_cancelled(cancellation_event):
                        return _cancelled_result(thoughts, memory)
                    self._telemetry.record(
                        event_type="agent.tool.called",
                        run_id=run_id,
                        request_id=request_id,
                        parent_event_id=None,
                        payload={
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.tool_call_id,
                            "arguments": tool_call.arguments,
                        },
                        raw_payload={
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.tool_call_id,
                            "arguments": tool_call.arguments,
                        },
                    )
                    try:
                        # 7
                        tool_output = self._tool_registry.execute(
                            tool_call,
                            run_id=run_id,
                            request_id=request_id,
                            cancellation_event=cancellation_event,
                        )
                    except ToolRegistryError as exc:
                        self._record_tool_failure(
                            run_id=run_id,
                            request_id=request_id,
                            tool_call=tool_call,
                            failure_reason=exc.failure_reason,
                            error_message=str(exc),
                        )
                        if exc.failure_reason == "approval_rejected":
                            tool_output = {
                                "status": "rejected",
                                "reason": "user_rejected",
                                "message": (
                                    "The user rejected this action. Do not retry this "
                                    "requested change with another mutating tool; explain "
                                    "that no change was made or ask what they prefer instead."
                                ),
                                "tool_name": tool_call.name,
                                "tool_call_id": tool_call.tool_call_id,
                            }
                        else:
                            tool_output = {
                                "status": "denied",
                                "reason": exc.failure_reason,
                                "error": str(exc),
                                "tool_name": tool_call.name,
                                "tool_call_id": tool_call.tool_call_id,
                            }
                    except PermissionError as exc:
                        tool_output = {
                            "status": "denied",
                            "error": str(exc),
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.tool_call_id,
                        }
                    except Exception as exc:
                        if tool_call.name == "run_command":
                            self._record_tool_failure(
                                run_id=run_id,
                                request_id=request_id,
                                tool_call=tool_call,
                                failure_reason="tool_execution_error",
                                error_message=str(exc),
                            )
                            tool_output = {
                                "status": "failed",
                                "error": str(exc),
                                "tool_name": tool_call.name,
                                "tool_call_id": tool_call.tool_call_id,
                            }
                            memory.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.tool_call_id,
                                    "name": tool_call.name,
                                    "content": json.dumps(tool_output, sort_keys=True),
                                }
                            )
                            continue
                        return self._tool_failure_result(
                            run_id=run_id,
                            request_id=request_id,
                            tool_call=tool_call,
                            failure_reason="tool_execution_error",
                            error_message=str(exc),
                            thoughts=thoughts,
                        )
                    if _turn_was_cancelled(cancellation_event):
                        return _cancelled_result(thoughts, memory)
                    memory.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.tool_call_id,
                            "name": tool_call.name,
                            "content": json.dumps(tool_output, sort_keys=True),
                        }
                    )
                continue
            if parsed.final_answer is not None:
                if _turn_was_cancelled(cancellation_event):
                    return _cancelled_result(thoughts, memory)
                if (
                    iteration < self._config.adapter.max_iterations
                    and _USER_REQUESTED_FILE_CHANGE_RE.search(prompt)
                    and _TEXT_CONTAINS_CODE_BLOCK_RE.search(parsed.final_answer)
                ):
                    file_path = _extract_path_from_prompt(prompt)
                    if file_path is None:
                        file_path = _extract_path_from_memory(memory)
                    file_content = _extract_content_from_code_block(parsed.final_answer)
                    if file_path is not None and file_content is not None:
                        tool_call = ToolCall(
                            tool_call_id="auto-convert",
                            name="write_file",
                            arguments={"path": file_path, "content": file_content},
                        )
                        try:
                            if _turn_was_cancelled(cancellation_event):
                                return _cancelled_result(thoughts, memory)
                            tool_output = self._tool_registry.execute(
                                tool_call,
                                run_id=run_id,
                                request_id=request_id,
                                cancellation_event=cancellation_event,
                            )
                            if _turn_was_cancelled(cancellation_event):
                                return _cancelled_result(thoughts, memory)
                        except ToolRegistryError:
                            pass
                        except PermissionError:
                            pass
                        else:
                            memory.append({"role": "assistant", "content": parsed.final_answer})
                            memory.append(
                                self._assistant_tool_call_message(
                                    [tool_call],
                                    structured=isinstance(provider, OpenAICompatibleProvider),
                                )
                            )
                            memory.append({
                                "role": "tool",
                                "tool_call_id": tool_call.tool_call_id,
                                "name": tool_call.name,
                                "content": json.dumps(tool_output, sort_keys=True),
                            })
                            return AdapterResult(
                                final_response=parsed.final_answer,
                                stop_reason="end_turn",
                                run_status="completed",
                                thoughts=tuple(thoughts),
                            )
                    memory.append({"role": "assistant", "content": parsed.final_answer})
                    memory.append({
                        "role": "user",
                        "content": (
                            "You described the change but did not apply it. "
                            "Call replace_text for a focused edit, or write_file with the file path and the complete new content."
                        ),
                    })
                    continue
                memory.append({"role": "assistant", "content": parsed.final_answer})
                return AdapterResult(
                    final_response=parsed.final_answer,
                    stop_reason="end_turn",
                    run_status="completed",
                    thoughts=tuple(thoughts),
                )
            # TODO it feels like there might be visible answer and tool call. check it out.
            memory.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response had no visible answer and no tool call. "
                        "Respond with user-visible content, or call one of the available tools."
                    ),
                }
            )
            continue

        if _turn_was_cancelled(cancellation_event):
            return _cancelled_result(thoughts, memory)
        self._record_loop_failure(
            run_id=run_id,
            request_id=request_id,
            failure_reason="max_iterations_exceeded",
        )
        return self._failed_result(
            "The agent reached the maximum number of iterations without a final answer.",
            stop_reason="max_iterations",
            thoughts=thoughts,
        )

    def _complete_thought_phase(
        self,
        *,
        run_id: str,
        request_id: str,
        started_at: float,
        text: str = "",
    ) -> None:
        self._event_sink.thought(
            self._thought_event_type(
                run_id=run_id,
                request_id=request_id,
                text=text,
                phase="completed",
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
        )

    def _failed_result(
        self,
        final_response: str,
        *,
        stop_reason: str,
        thoughts: list[str] | tuple[str, ...] = (),
    ) -> AdapterResult:
        return AdapterResult(
            final_response=final_response,
            stop_reason=stop_reason,
            run_status="failed",
            thoughts=tuple(thoughts),
        )

    def _tool_failure_result(
        self,
        *,
        run_id: str,
        request_id: str,
        tool_call: ToolCall,
        failure_reason: str,
        error_message: str,
        thoughts: list[str] | tuple[str, ...] = (),
    ) -> AdapterResult:
        self._record_tool_failure(
            run_id=run_id,
            request_id=request_id,
            tool_call=tool_call,
            failure_reason=failure_reason,
            error_message=error_message,
        )
        return self._failed_result(
            "The adapter could not complete the requested tool call.",
            stop_reason="tool_error",
            thoughts=thoughts,
        )

    def _provider(self) -> FakeOpenAICompatibleProvider | OpenAICompatibleProvider:
        if self._config.adapter.fake_provider.enabled:
            return FakeOpenAICompatibleProvider(
                self._config.adapter.fake_provider.script
            )
        return OpenAICompatibleProvider(
            kind=self._config.adapter.provider.kind,
            base_url=self._config.adapter.provider.base_url,
            model=self._config.adapter.provider.model,
            api_key_env=self._config.adapter.provider.api_key_env,
            timeout_seconds=self._config.adapter.provider.timeout_seconds,
            auth_headers=self._config.adapter.provider.auth_headers,
            tool_definitions=self._tool_registry.definitions(),
            temperature=self._config.adapter.provider.temperature,
            session_id=self._config.session_id,
            managed_request=self._config.managed_request,
            managed_request_stream=self._config.managed_request_stream,
        )

    def _system_context(self) -> str:
        from code4me2_agent.command_tools import available_commands

        workspace_root = self._config.workspace_root.as_posix()
        executable_commands = available_commands(
            self._config.commands.allowlisted_commands
        )
        command_summary = ", ".join(executable_commands) or "none"
        return (
            "You are a helpful programming assistant. "
            f"The current working directory and workspace root is {workspace_root}. "
            f"The participant host operating system is {platform.system()}; executable commands allowed by policy are: {command_summary}. "
            "Respond conversationally to greetings and general questions without calling tools. "
            "Only use tools when the user explicitly asks you to read, search, create, edit, or list files, "
            "or to run a command in the workspace. "
            "When the user asks about a file's contents, always call read_file first before answering. "
            "When the user asks you to change, edit, update, or modify a file's contents, "
            "you MUST call replace_text for small edits or write_file with the complete new content rather than just describing the change. "
            "Do not output code as text when the user expects you to apply the change. "
            "When the user asks you to run, execute, compile, or build a file or command, "
            "you MUST call run_command with the appropriate argv rather than just describing the command. "
            "Never assume Bash or Unix utilities are installed when they are absent from the executable command list. "
            "When calling tools, prefer workspace-relative paths and cwd='.' unless the user explicitly asks for an absolute path. "
            "Do not invent container paths such as /workspace. "
            "After file tools complete, answer directly in assistant text; do not run commands just to print a confirmation."
            " Tools whose names start with mcp__ come from MCP servers configured by the ACP client; "
            "use them when their descriptions match the user's request."
        )

    def _assistant_tool_call_message(
        self,
        tool_calls: list[ToolCall],
        *,
        structured: bool,
    ) -> dict[str, Any]:
        if structured:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call.tool_call_id,
                        "name": tool_call.name,
                        "arguments": dict(tool_call.arguments),
                    }
                    for tool_call in tool_calls
                ],
            }
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "tool_calls": [
                        {
                            "id": tool_call.tool_call_id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                        }
                        for tool_call in tool_calls
                    ]
                },
                sort_keys=True,
            ),
        }

    def _parse_output(
        self,
        output: dict[str, Any],
        *,
        run_id: str,
        request_id: str,
    ) -> ParsedProviderOutput | None:
        raw_output = output.get("output", output)
        if not isinstance(raw_output, dict):
            raw_output = {"text": str(raw_output)}

        tool_calls_data = raw_output.get("tool_calls")
        final_answer = raw_output.get("final_answer")
        if isinstance(tool_calls_data, list) or isinstance(final_answer, str):
            # 6 - TODO here might be the problem in tool calling returns
            fallback_tool_call = _single_json_tool_call_from_text(
                final_answer,
                known_tool_names=self._tool_registry.known_tool_names(),
            )
            if fallback_tool_call is not None and not tool_calls_data:
                return ParsedProviderOutput(
                    tool_calls=[fallback_tool_call],
                    final_answer=(
                        str(final_answer)
                        if final_answer is not None and str(final_answer).strip()
                        else None
                    ),
                    thought=_normalize_optional_text(raw_output.get("reasoning")),
                )
            return ParsedProviderOutput(
                tool_calls=_normalize_tool_calls(tool_calls_data),
                final_answer=(
                    str(final_answer)
                    if final_answer is not None and str(final_answer).strip()
                    else None
                ),
                thought=_normalize_optional_text(raw_output.get("reasoning")),
            )

        text = raw_output.get("text")
        if not isinstance(text, str):
            self._record_parse_failure(
                run_id=run_id,
                request_id=request_id,
                output=raw_output,
                failure_reason="missing_structured_output",
            )
            return None
        try:
            parsed_text = json.loads(text)
        except json.JSONDecodeError:
            self._record_parse_failure(
                run_id=run_id,
                request_id=request_id,
                output={"text": text},
                failure_reason="invalid_json_text",
            )
            return None
        if not isinstance(parsed_text, dict):
            self._record_parse_failure(
                run_id=run_id,
                request_id=request_id,
                output={"text": text},
                failure_reason="json_text_not_object",
            )
            return None
        fallback_tool_call = _single_tool_call_from_object(
            parsed_text,
            known_tool_names=self._tool_registry.known_tool_names(),
        )
        if fallback_tool_call is not None:
            return ParsedProviderOutput(
                tool_calls=[fallback_tool_call],
                final_answer=None,
                thought=_normalize_optional_text(raw_output.get("reasoning")),
            )
        return ParsedProviderOutput(
            tool_calls=_normalize_tool_calls(parsed_text.get("tool_calls")),
            final_answer=(
                str(parsed_text["final_answer"])
                if parsed_text.get("final_answer") is not None
                else None
            ),
            thought=_normalize_optional_text(parsed_text.get("reasoning")),
        )

    def _record_parse_failure(
        self,
        *,
        run_id: str,
        request_id: str,
        output: dict[str, Any],
        failure_reason: str,
    ) -> None:
        self._telemetry.record(
            event_type="agent.adapter.parse_failed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "adapter_name": self._config.adapter.name,
                "failure_reason": failure_reason,
            },
            raw_payload=output,
        )

    def _record_loop_failure(
        self,
        *,
        run_id: str,
        request_id: str,
        failure_reason: str,
    ) -> None:
        self._telemetry.record(
            event_type="agent.adapter.loop_failed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "adapter_name": self._config.adapter.name,
                "max_iterations": self._config.adapter.max_iterations,
                "failure_reason": failure_reason,
            },
        )

    def _record_tool_failure(
        self,
        *,
        run_id: str,
        request_id: str,
        tool_call: ToolCall,
        failure_reason: str,
        error_message: str,
    ) -> None:
        self._telemetry.record(
            event_type="agent.tool.failed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.tool_call_id,
                "failure_reason": failure_reason,
                "error_message": error_message,
            },
        )

    def _record_model_completed(
        self,
        *,
        run_id: str,
        request_id: str,
        duration_ms: float,
        provider_turn: ProviderTurn,
    ) -> None:
        usage = provider_turn.usage
        self._telemetry.record(
            event_type="agent.model.completed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "model": provider_turn.model,
                "base_url": self._config.adapter.provider.base_url,
                "finish_reason": provider_turn.finish_reason,
                "usage": usage,
            },
            metrics={
                "duration_ms": round(duration_ms, 3),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            raw_payload={
                "request": provider_turn.request_payload,
                "response": provider_turn.raw_response,
            },
        )


def create_agent_adapter(
    config: AgentConfig,
    *,
    telemetry: AgentTelemetryRecorder,
    file_tools: WorkspaceFileTools,
    command_tools: WorkspaceCommandTools,
    event_sink: AgentEventSink | None = None,
    mcp_tools: StdioMcpToolBroker | None = None,
) -> AgentAdapter:
    if config.adapter.name == "openai_compatible_react":
        return OpenAICompatibleReactAdapter(
            config,
            telemetry=telemetry,
            tool_registry=ToolRegistry(
                file_tools,
                command_tools,
                allowed_tools=config.allowed_tools
                if config.allowed_tools is not None
                else (
                    frozenset(config.tools)
                    if getattr(config, "tools", None) is not None
                    else None
                ),
                approval_policy=config.approval_policy,
                event_sink=event_sink,
                mcp_tools=mcp_tools,
            ),
            event_sink=event_sink,
        )
    return DeterministicEchoAdapter()


def _tool_event_metadata(
    tool_name: str, values: dict[str, Any]
) -> dict[str, Any] | None:
    if tool_name == "read_file":
        path = str(values.get("path", "")).strip()
        filename = path.rsplit("/", 1)[-1] or path
        title = f"Read {path}" if path else "Read file"
        return {
            "kind": "read",
            "title": title,
            "path": path or None,
            "content_text": f"Read file {filename}" if filename else "Read file",
            "raw_input": {"path": path} if path else None,
            "raw_output": {"path": path} if path else None,
        }
    if tool_name == "create_file":
        return _edit_tool_event_metadata(
            values,
            title_prefix="Create",
            fallback_title="Create file",
            content_prefix="Created file",
        )
    if tool_name == "write_file":
        return _edit_tool_event_metadata(
            values,
            title_prefix="Update",
            fallback_title="Update file",
            content_prefix="Updated file",
        )
    if tool_name == "replace_text":
        return _edit_tool_event_metadata(
            values,
            title_prefix="Replace",
            fallback_title="Replace text",
            content_prefix="Replaced text in",
        )
    if tool_name == "run_command":
        argv = values.get("argv")
        command = " ".join(str(arg) for arg in argv) if isinstance(argv, (list, tuple)) else "command"
        return {
            "kind": "execute",
            "title": f"Run {command}",
            "content_text": f"Run command: {command}",
            "raw_input": {"argv": argv, "cwd": values.get("cwd", ".")},
            "raw_output": None,
        }
    if tool_name.startswith("mcp__"):
        title = f"Call MCP tool {tool_name.removeprefix('mcp__')}"
        return {
            "kind": "other",
            "title": title,
            "content_text": title,
            "raw_input": values,
            "raw_output": values,
        }
    return None


def _edit_tool_event_metadata(
    values: dict[str, Any],
    *,
    title_prefix: str,
    fallback_title: str,
    content_prefix: str,
) -> dict[str, Any]:
    path = str(values.get("path", "")).strip()
    filename = path.rsplit("/", 1)[-1] or path
    title = f"{title_prefix} {path}" if path else fallback_title
    return {
        "kind": "edit",
        "title": title,
        "path": path or None,
        "content_text": f"{content_prefix} {filename}" if filename else content_prefix,
        "raw_input": {"path": path} if path else None,
        "raw_output": {"path": path} if path else None,
    }


def _normalize_tool_calls(value: Any) -> list[ToolCall]:
    if not isinstance(value, list):
        return []
    normalized: list[ToolCall] = []
    for index, raw_tool_call in enumerate(value):
        if not isinstance(raw_tool_call, dict):
            continue
        arguments = raw_tool_call.get("arguments", {})
        normalized.append(
            ToolCall(
                tool_call_id=str(raw_tool_call.get("id", f"tool-call-{index + 1}")),
                name=str(raw_tool_call.get("name", "")).strip(),
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
            )
        )
    return [tool_call for tool_call in normalized if tool_call.name]


# TODO check here
def _single_json_tool_call_from_text(
    value: Any,
    *,
    known_tool_names: set[str] | None = None,
) -> ToolCall | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    result = _try_parse_json(text, known_tool_names=known_tool_names)
    if result is not None:
        return result
    for match in _CODE_FENCE_RE.finditer(text):
        result = _try_parse_json(
            match.group(1).strip(),
            known_tool_names=known_tool_names,
        )
        if result is not None:
            return result
    idx = text.find('{"name":')
    if idx >= 0:
        brace_count = 0
        for i in range(idx, len(text)):
            if text[i] == "{":
                brace_count += 1
            elif text[i] == "}":
                brace_count -= 1
                if brace_count == 0:
                    result = _try_parse_json(
                        text[idx : i + 1],
                        known_tool_names=known_tool_names,
                    )
                    if result is not None:
                        return result
    return None


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
_USER_REQUESTED_FILE_CHANGE_RE = re.compile(
    r"(?:change|edit|update|modify|write|replace|fix|save)\s",
    re.IGNORECASE,
)
_TEXT_CONTAINS_CODE_BLOCK_RE = re.compile(r"```[\w]*\n", re.MULTILINE)
_CODE_BLOCK_CONTENT_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_PATH_FROM_PROMPT_RE = re.compile(
    r"(?:change|edit|update|modify|write|replace|fix|save)\s+"
    r"(?:the\s+|file\s+)?(\S+\.\w+)",
    re.IGNORECASE,
)


def _extract_path_from_prompt(prompt: str) -> str | None:
    match = _PATH_FROM_PROMPT_RE.search(prompt)
    if match is None:
        return None
    return match.group(1).strip()


def _extract_path_from_memory(memory: MemoryWindow) -> str | None:
    for message in reversed(memory._messages):
        if message.get("role") == "tool" and message.get("name") == "read_file":
            content = message.get("content", "")
            if isinstance(content, str):
                try:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        path = data.get("path")
                        if isinstance(path, str) and path.strip():
                            return path.strip()
                except (json.JSONDecodeError, TypeError):
                    pass
    return None


def _extract_content_from_code_block(text: str) -> str | None:
    match = _CODE_BLOCK_CONTENT_RE.search(text)
    if match is None:
        return None
    return match.group(1).strip()


def _try_parse_json(
    text: str,
    *,
    known_tool_names: set[str] | None = None,
) -> ToolCall | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _single_tool_call_from_object(
        parsed,
        known_tool_names=known_tool_names,
    )


def _single_tool_call_from_object(
    value: dict[str, Any],
    *,
    known_tool_names: set[str] | None = None,
) -> ToolCall | None:
    name = str(value.get("name", "")).strip()
    if name not in (known_tool_names or _known_tool_names()):
        return None
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict):
        return None
    return ToolCall(
        tool_call_id=str(value.get("id", "tool-call-1")),
        name=name,
        arguments=dict(arguments),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _known_tool_names() -> set[str]:
    names: set[str] = set()
    for tool in _tool_definitions():
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _message_token_count(message: dict[str, Any]) -> int:
    parts: list[str] = []
    for key in ("role", "content", "tool_call_id", "name"):
        value = message.get(key)
        if value:
            parts.append(str(value))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        parts.append(json.dumps(tool_calls, sort_keys=True))
    text = " ".join(parts).strip()
    if not text:
        return 1
    return max(1, len(text.split()))


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        _function_tool(
            "read_file",
            "Read a UTF-8 text file inside the configured workspace. Optionally pass 1-based line_start and line_end.",
            {
                "path": {"type": "string"},
                "line_start": {"type": "integer", "minimum": 1},
                "line_end": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
        _function_tool(
            "create_file",
            (
                "Create a new UTF-8 text file inside the configured workspace. "
                "Use this only when the target file does not already exist. "
                "Do not call write_file immediately after a successful create_file for the same path "
                "unless you are intentionally changing the content again."
            ),
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            ["path", "content"],
        ),
        _function_tool(
            "write_file",
            (
                "Overwrite a UTF-8 text file inside the configured workspace. "
                "Use this when the file already exists or when you intentionally need to replace its content. "
                "If you just created the file with create_file and the content is already correct, do not call write_file again."
            ),
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            ["path", "content"],
        ),
        _function_tool(
            "replace_text",
            (
                "Replace the first exact occurrence of old_text in a UTF-8 text file. "
                "Prefer this for small edits instead of sending an entire file through write_file. "
                "Call read_file first so old_text exactly matches the current file content."
            ),
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            ["path", "old_text", "new_text"],
        ),
        _function_tool(
            "list_files",
            "List files underneath a workspace-relative path.",
            {
                "path": {"type": "string"},
            },
            [],
        ),
        _function_tool(
            "search_files",
            "Search for text matches in workspace files.",
            {
                "query": {"type": "string"},
                "path": {"type": "string"},
            },
            ["query"],
        ),
        _function_tool(
            "run_command",
            "Run an allowlisted workspace command without a shell.",
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "cwd": {"type": "string"},
            },
            ["argv"],
        ),
    ]


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _to_openai_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role", "user"))
    if role == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message.get("content", ""),
            "tool_calls": [
                {
                    "id": str(tool_call.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(tool_call.get("name", "")),
                        "arguments": json.dumps(
                            tool_call.get("arguments", {}), sort_keys=True
                        ),
                    },
                }
                for tool_call in message.get("tool_calls", [])
                if isinstance(tool_call, dict)
            ],
        }
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": str(message.get("tool_call_id", "")),
            "content": str(message.get("content", "")),
        }
    return {
        "role": role,
        "content": str(message.get("content", "")),
    }


def _normalize_openai_provider_response(
    response_payload: dict[str, Any]
) -> dict[str, Any]:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Provider response did not include choices.")
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message", {})
    if not isinstance(message, dict):
        raise RuntimeError("Provider response choice message was not an object.")

    tool_calls = []
    raw_tool_calls = message.get("tool_calls", [])
    if isinstance(raw_tool_calls, list):
        for index, raw_tool_call in enumerate(raw_tool_calls):
            if not isinstance(raw_tool_call, dict):
                continue
            function_data = raw_tool_call.get("function", {})
            if not isinstance(function_data, dict):
                continue
            raw_arguments = function_data.get("arguments", "{}")
            if not isinstance(raw_arguments, str):
                raw_arguments = json.dumps(raw_arguments)
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            name = str(function_data.get("name", "")).strip()
            if not name:
                continue
            tool_calls.append(
                {
                    "id": str(raw_tool_call.get("id", f"tool-call-{index + 1}")),
                    "name": name,
                    "arguments": arguments,
                }
            )

    final_answer = message.get("content")
    if isinstance(final_answer, list):
        final_answer = "".join(
            str(block.get("text", ""))
            for block in final_answer
            if isinstance(block, dict) and block.get("type") == "text"
        )

    normalized: dict[str, Any] = {"tool_calls": tool_calls}
    reasoning = _normalize_optional_text(message.get("reasoning"))
    if reasoning is not None:
        normalized["reasoning"] = reasoning
    if final_answer is not None:
        normalized["final_answer"] = str(final_answer)
    return normalized


def _response_finish_reason(response_payload: dict[str, Any]) -> str | None:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    finish_reason = first_choice.get("finish_reason")
    return str(finish_reason) if finish_reason is not None else None


def _normalize_usage(
    raw_usage: Any,
    messages: list[dict[str, Any]],
    output: dict[str, Any],
) -> dict[str, int]:
    if isinstance(raw_usage, dict):
        prompt_tokens = _int_or_zero(raw_usage.get("prompt_tokens"))
        completion_tokens = _int_or_zero(raw_usage.get("completion_tokens"))
        total_tokens = _int_or_zero(raw_usage.get("total_tokens"))
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens > 0 or completion_tokens > 0 or total_tokens > 0:
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

    prompt_tokens = sum(_message_token_count(message) for message in messages)
    completion_tokens = _estimated_output_token_count(output)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _estimated_output_token_count(output: dict[str, Any]) -> int:
    parts: list[str] = []
    final_answer = output.get("final_answer")
    if final_answer:
        parts.append(str(final_answer))
    tool_calls = output.get("tool_calls", [])
    if isinstance(tool_calls, list):
        parts.append(json.dumps(tool_calls, sort_keys=True))
    text = " ".join(part for part in parts if part).strip()
    if not text:
        return 1
    return max(1, len(text.split()))


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
