from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import platform
import random
import re
import shlex
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePath
from threading import Event, Thread
from time import perf_counter, sleep, time
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence, TypeVar
from urllib.parse import urlparse

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from code4me2_agent import prompting, slash_commands, tool_catalog
from code4me2_agent.config import HarnessOptions
from code4me2_agent.events import (
    AssistantTextEvent,
    NoopAgentEventSink,
    PlanEntrySpec,
    PlanEvent,
    ThoughtEvent,
    ToolCallEvent,
    UsageEvent,
    emit_event,
)
from code4me2_agent.file_tools import TextEdit, apply_text_edits, describe_strategy
from code4me2_agent.hunks import Hunk, RevisionOffer, apply_hunks
from code4me2_agent.patching import PatchError, parse_patch
from code4me2_agent.session_state import SessionToolState, normalize_workspace_path
from code4me2_agent.tool_catalog import (
    approval_kind,
    requires_manual_approval,
    tool_kind,
)
from code4me2_agent.tool_errors import (
    ToolArgumentError,
    ToolError,
    ToolFileNotFoundError,
)
from code4me2_agent.tls import USER_AGENT

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from code4me2_agent.command_tools import WorkspaceCommandTools
    from code4me2_agent.config import AgentConfig
    from code4me2_agent.events import AgentEventSink
    from code4me2_agent.file_tools import WorkspaceFileTools
    from code4me2_agent.mcp_tools import StdioMcpToolBroker
    from code4me2_agent.telemetry import AgentTelemetryRecorder

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Safety net for tool output entering the conversation; tools cap their own
# output well below this.
_MAX_TOOL_RESULT_CHARS = 32_000
# Room for the model's own output when sizing the request window.
_OUTPUT_HEADROOM_TOKENS = 1024
# Current-turn compaction elides down to this share of the budget at once.
_CURRENT_TURN_LOW_WATER = 0.6
_MAX_TOKEN_SCALE = 3.0
_BUDGET_NOTICE = (
    "This is the final model call for this turn: tool calls are disabled. Reply with a "
    "user-facing message that summarizes what you did and verified so far, and what "
    "remains to be done."
)
_EMPTY_RESPONSE_NUDGE = (
    "Your previous response was empty. Reply with user-visible text or call a tool."
)
_EMPTY_RESPONSE_TEXT = "The model returned an empty response twice; please try again."
_PARSE_ERROR_TEXT = "The model returned an unreadable response; please try again."


@dataclass(frozen=True)
class AdapterResult:
    final_response: str
    stop_reason: str
    run_status: str
    thoughts: tuple[str, ...] = ()
    # True when the adapter already streamed the final text to the client.
    response_emitted: bool = False
    usage: dict[str, int] | None = None
    # Optional model calls (self-review, summary) that failed this turn. They
    # have no closing event of their own; agent.run.completed reports them.
    side_call_failures: tuple[dict[str, Any], ...] = ()


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
    ) -> AdapterResult: ...


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    # Set when the provider returned arguments that were not a JSON object.
    argument_error: str | None = None
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ParsedProviderOutput:
    text: str | None
    tool_calls: list[ToolCall]
    thought: str | None = None
    finish_reason: str | None = None
    # The provider's own reasoning fields, exactly as returned, to send back.
    reasoning_fields: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderTurn:
    output: dict[str, Any]
    usage: dict[str, int]
    finish_reason: str | None
    model: str
    request_payload: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None
    usage_estimated: bool = False


class FakeProviderExhaustedError(RuntimeError):
    pass


class ProviderRequestFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable


# Backwards-compatible alias.
BackendProviderRequestError = ProviderRequestFailed


class ProviderCancelled(RuntimeError):
    pass


class ToolRegistryError(RuntimeError):
    def __init__(self, message: str, *, failure_reason: str) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


class ToolRevisionRequested(ToolRegistryError):
    """The user chose "Revise…": the call did not run as proposed.

    ``result`` is the tool result the model sees instead (status ``revise``).
    """

    def __init__(self, message: str, *, result: dict[str, Any]) -> None:
        super().__init__(message, failure_reason="approval_revised")
        self.result = result


def _turn_was_cancelled(cancellation_event: Event | None) -> bool:
    return cancellation_event is not None and cancellation_event.is_set()


def _cancelled_result(
    thoughts: list[str] | tuple[str, ...] = (),
) -> AdapterResult:
    return AdapterResult(
        final_response="",
        stop_reason="cancelled",
        run_status="cancelled",
        thoughts=tuple(thoughts),
    )


# ------------------------------------------------------------ rate limiting


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


# ------------------------------------------------------------------ retries


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 20.0
    max_total_wait: float = 60.0
    retry_after_cap: float = 30.0
    retry_statuses: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504})

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        return cls(
            max_attempts=_positive_int_env("CODE4ME_PROVIDER_MAX_ATTEMPTS", 4),
            max_total_wait=_positive_float_env("CODE4ME_PROVIDER_MAX_RETRY_SECONDS", 60.0),
        )


@dataclass(frozen=True)
class _ErrorClass:
    retryable: bool
    status_code: int | None = None
    retry_after: float | None = None
    message: str = ""


def _sleep_interruptible(
    seconds: float,
    cancellation_event: Event | None,
    sleep_fn: Callable[[float], None],
) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0:
        if _turn_was_cancelled(cancellation_event):
            raise ProviderCancelled("Cancelled while waiting to retry the model request.")
        step = min(0.25, remaining)
        sleep_fn(step)
        remaining -= step
    if _turn_was_cancelled(cancellation_event):
        raise ProviderCancelled("Cancelled while waiting to retry the model request.")


def _call_with_retries(
    call: Callable[[], T],
    *,
    policy: RetryPolicy,
    classify: Callable[[BaseException], _ErrorClass],
    cancellation_event: Event | None = None,
    sleep_fn: Callable[[float], None] = sleep,
    rand: Callable[[], float] = random.random,
    before_attempt: Callable[[], None] | None = None,
) -> T:
    total_wait = 0.0
    attempts = max(1, policy.max_attempts)
    for attempt in range(1, attempts + 1):
        if _turn_was_cancelled(cancellation_event):
            raise ProviderCancelled("Cancelled before the model request was sent.")
        if before_attempt is not None:
            before_attempt()
        try:
            return call()
        except ProviderCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            classified = classify(exc)
            description = classified.message or str(exc)
            if not classified.retryable or attempt >= attempts:
                raise ProviderRequestFailed(
                    description,
                    status_code=classified.status_code,
                    attempts=attempt,
                    retryable=classified.retryable,
                ) from exc
            if classified.retry_after is not None:
                delay = min(max(0.0, classified.retry_after), policy.retry_after_cap)
            else:
                cap = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
                delay = cap * (0.5 + 0.5 * rand())
            if total_wait + delay > policy.max_total_wait:
                raise ProviderRequestFailed(
                    f"{description} (gave up after {attempt} attempts; retry budget exhausted)",
                    status_code=classified.status_code,
                    attempts=attempt,
                    retryable=True,
                ) from exc
            logging.info(
                "Model request failed (attempt %s/%s, status=%s); retrying in %.2fs",
                attempt,
                attempts,
                classified.status_code,
                delay,
            )
            _sleep_interruptible(delay, cancellation_event, sleep_fn)
            total_wait += delay
    raise ProviderRequestFailed("Model request failed.", attempts=attempts)


def _parse_retry_after(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        return None
    from datetime import datetime, timezone

    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _classify_sdk_error(policy: RetryPolicy) -> Callable[[BaseException], _ErrorClass]:
    def classify(exc: BaseException) -> _ErrorClass:
        if isinstance(exc, APIStatusError):
            status = int(getattr(exc, "status_code", 0) or 0)
            headers = getattr(getattr(exc, "response", None), "headers", None)
            retry_after = None
            if headers is not None:
                try:
                    retry_after = _parse_retry_after(headers.get("retry-after"))
                except Exception:  # noqa: BLE001
                    retry_after = None
            body = ""
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    body = str(response.text)[:500]
                except Exception:  # noqa: BLE001
                    body = ""
            return _ErrorClass(
                retryable=status in policy.retry_statuses,
                status_code=status,
                retry_after=retry_after,
                message=f"The model request failed with HTTP {status}: {body}".rstrip(": "),
            )
        if isinstance(exc, APIConnectionError):
            return _ErrorClass(
                retryable=True,
                message=f"The model request could not reach the model service: {exc}",
            )
        return _ErrorClass(retryable=False, message=f"The model request failed: {exc}")

    return classify


def _classify_managed_error(policy: RetryPolicy) -> Callable[[BaseException], _ErrorClass]:
    def classify(exc: BaseException) -> _ErrorClass:
        status = getattr(exc, "status_code", None)
        transient = bool(getattr(exc, "transient", False))
        retryable = transient or (isinstance(status, int) and status in policy.retry_statuses)
        if status is not None:
            message = f"The managed model request failed with HTTP {status}."
        else:
            message = f"The managed model request failed: {exc}"
        return _ErrorClass(
            retryable=retryable,
            status_code=status if isinstance(status, int) else None,
            message=message,
        )

    return classify


# ----------------------------------------------------------------- adapters


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
    ) -> AdapterResult:
        if _turn_was_cancelled(cancellation_event):
            return _cancelled_result()
        return AdapterResult(
            final_response=f"Code4Me ACP echo: {prompt}",
            stop_reason="end_turn",
            run_status="completed",
        )


@dataclass(frozen=True)
class EditPreview:
    old_text: str | None
    new_text: str | None
    error: BaseException | None = None
    # Additional files of a multi-file change (apply_patch): (absolute path, old, new).
    extra_diffs: tuple[tuple[str, str | None, str], ...] = ()

    @property
    def has_diff(self) -> bool:
        return self.new_text is not None


# Tools whose "Revise…" form can keep some hunks of the change (apply_patch
# only for a single update without a move); others get instructions only.
_REVISE_HUNK_TOOLS = frozenset({"create_file", "write_file", "replace_text", "edit_file"})


@dataclass(frozen=True)
class _Revision:
    """What became of an accepted "Revise…" form (``revise_status``).

    ``applied``: the kept hunks were written; ``file_changed``: the file no
    longer held the previewed text, so nothing was written; ``write_failed``:
    writing them failed; ``instructions_only``: no hunk was kept.
    """

    status: str
    kept: tuple[Hunk, ...]
    total: int
    instructions: str
    path: str | None = None
    merged_text: str | None = None
    error: str | None = None
    syntax_error: dict[str, Any] | None = None

    def result(self) -> dict[str, Any]:
        """The tool result the model sees instead of the tool output."""
        result: dict[str, Any] = {
            "status": "revise",
            "kept_hunks": [hunk.label for hunk in self.kept],
            "total_hunks": self.total,
            "applied_path": self.path if self.status == "applied" else None,
            "user_instructions": self.instructions,
            "message": self._message(),
        }
        if self.syntax_error:
            result["syntax_error"] = self.syntax_error
        return result

    def _message(self) -> str:
        parts = f"{len(self.kept)} of the {self.total} parts of the proposed change"
        if self.status == "applied":
            done = (
                f"The user kept {parts}; they are already written to {self.path} (re-read the "
                "file before changing it again). Nothing else was applied."
            )
        elif self.status == "file_changed":
            done = (
                f"The user kept {parts}, but {self.path} changed after the change was proposed, "
                "so nothing was written; re-read the file."
            )
        elif self.status == "write_failed":
            done = (
                f"The user kept {parts}, but writing them to {self.path} failed ({self.error}); "
                "re-read the file."
            )
        else:
            done = "The user did not approve this call as proposed; nothing was applied."
        if self.instructions:
            return (
                f"{done} The user asked for a revision: follow user_instructions and propose the "
                "revised version in this turn; it needs the user's approval again."
            )
        return (
            f"{done} The user gave no further instructions: do not propose the dropped parts "
            "again; continue the task or ask the user what should change."
        )


# Tools whose success means the workspace changed; a loop-guard count resets
# after one, and verify-on-stop / self-review look at the turn's changes.
_WORKSPACE_MUTATIONS = frozenset(
    {"create_file", "write_file", "replace_text", "edit_file", "apply_patch", "delete_file", "move_file"}
)
# Guarded by read-before-edit: changing an existing file needs a prior look.
_READ_BEFORE_EDIT_TOOLS = frozenset({"write_file", "replace_text", "edit_file"})
# Built-in tools that never change anything; they may run in parallel.
_PARALLEL_SAFE_TOOLS = frozenset({"read_file", "list_files", "glob_files", "grep_files", "search_files"})


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
        telemetry: AgentTelemetryRecorder | None = None,
        workspace_root: Path | None = None,
        session_state: SessionToolState | None = None,
        harness: HarnessOptions | None = None,
    ) -> None:
        self._file_tools = file_tools
        self._command_tools = command_tools
        self._mcp_tools = mcp_tools
        self._allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else None
        self._approval_policy = approval_policy
        self._event_sink = event_sink or NoopAgentEventSink()
        self._telemetry = telemetry
        if workspace_root is None:
            candidate = getattr(file_tools, "workspace_root", None)
            workspace_root = candidate if isinstance(candidate, Path) else None
        self._workspace_root = workspace_root
        # Read-before-edit needs the session's memory of what was read; a
        # registry built without one (tests, tools used directly) skips it.
        self._session_state = session_state
        self._harness = harness or HarnessOptions()
        self._handlers: dict[str, Callable[..., Any]] = {
            "read_file": self._run_read_file,
            "create_file": self._run_create_file,
            "write_file": self._run_write_file,
            "replace_text": self._run_replace_text,
            "edit_file": self._run_edit_file,
            "apply_patch": self._run_apply_patch,
            "delete_file": self._run_delete_file,
            "move_file": self._run_move_file,
            "list_files": self._run_list_files,
            "glob_files": self._run_glob_files,
            "grep_files": self._run_grep_files,
            "search_files": self._run_search_files,
            "run_command": self._run_run_command,
            "update_plan": self._run_update_plan,
            "ask_user": self._run_ask_user,
        }

    # ------------------------------------------------------------ policy

    @property
    def approval_policy(self) -> str:
        return self._approval_policy

    @property
    def session_state(self) -> SessionToolState | None:
        return self._session_state

    @property
    def command_tools(self) -> WorkspaceCommandTools:
        return self._command_tools

    @property
    def file_tools(self) -> WorkspaceFileTools:
        return self._file_tools

    def _is_allowed(self, name: str) -> bool:
        if self._allowed_tools is None:
            return True
        if name in self._allowed_tools:
            return True
        return name.startswith("mcp__") and "mcp__*" in self._allowed_tools

    def mcp_access(self, name: str) -> str | None:
        """The broker's class for an MCP tool ("read", "execute", "edit", "other")."""
        return self._mcp_access(name)

    def _mcp_access(self, name: str) -> str | None:
        if not name.startswith("mcp__") or self._mcp_tools is None:
            return None
        access = getattr(self._mcp_tools, "tool_access", None)
        if not callable(access):
            return None
        try:
            return str(access(name))
        except Exception:  # noqa: BLE001
            return None

    def requires_approval(self, name: str) -> bool:
        """Whether ``name`` changes anything (approval under per_step, hidden under suggestion_only).

        Client-supplied MCP tools are treated as mutating unless the broker
        classified them read-only (the curated IDE diagnostics and search tools).
        """
        if name.startswith("mcp__") and self._mcp_access(name) == "read":
            return False
        return requires_manual_approval(name)

    def is_parallel_safe(self, name: str) -> bool:
        if name in _PARALLEL_SAFE_TOOLS:
            return True
        return name.startswith("mcp__") and self._mcp_access(name) == "read"

    def display_kind(self, name: str) -> str:
        access = self._mcp_access(name)
        if access == "read":
            return "search"
        if access == "execute":
            return "execute"
        if access == "edit":
            return "edit"
        return tool_kind(name)

    def definitions(self) -> list[dict[str, Any]]:
        definitions = tool_catalog.tool_definitions()
        if self._mcp_tools is not None:
            definitions.extend(self._mcp_tools.definitions())
        selected = [
            definition
            for definition in definitions
            if self._is_allowed(str(definition.get("function", {}).get("name", "")))
        ]
        if self._approval_policy == "suggestion_only":
            # The model must never see tools it cannot run; it proposes diffs instead.
            selected = [
                definition
                for definition in selected
                if not self.requires_approval(
                    str(definition.get("function", {}).get("name", ""))
                )
            ]
        blocked = getattr(self._command_tools, "blocked_commands", None)
        shell = _usable_shell(blocked) if blocked is not None else None
        if shell is not None:
            # "There is no shell" would contradict an installed, unblocked bash/sh.
            selected = [_with_shell_description(definition, shell) for definition in selected]
        return selected

    def _is_known(self, name: str) -> bool:
        """A catalogue tool or a tool of a connected MCP server (allowed or not)."""
        if name in tool_catalog.tool_names():
            return True
        return self._mcp_tools is not None and self._mcp_tools.has_tool(name)

    def known_tool_names(self) -> set[str]:
        names: set[str] = set()
        for tool in self.definitions():
            function = tool.get("function")
            if isinstance(function, dict):
                name = str(function.get("name", "")).strip()
                if name:
                    names.add(name)
        return names

    def needs_approval(self, name: str) -> bool:
        return self._approval_policy == "per_step" and self.requires_approval(name)

    # ------------------------------------------------------ read tracking

    def _relative(self, path: object) -> str | None:
        if self._workspace_root is None:
            return None
        return normalize_workspace_path(self._workspace_root, path)

    def _check_read_before_edit(self, name: str, arguments: dict[str, Any]) -> None:
        state = self._session_state
        if state is None or not self._harness.read_before_edit or arguments.get("force"):
            return
        if name in _READ_BEFORE_EDIT_TOOLS:
            paths = [arguments.get("path")]
        elif name == "apply_patch":
            paths = list(arguments.get("_guarded_paths") or [])
        else:
            return
        for path in paths:
            relative = self._relative(path)
            if relative is None or state.has_seen(relative):
                continue
            if self._workspace_root is not None and not (self._workspace_root / relative).exists():
                continue  # a new file: nothing to have read
            raise ToolError(
                f"{relative} has not been read in this session. Call read_file on it first so the "
                "edit is based on its current content (or pass force=true if you are certain of it).",
                code="read_before_edit",
            )

    def _note_success(self, name: str, arguments: dict[str, Any], output: dict[str, Any]) -> None:
        state = self._session_state
        if state is None:
            return
        if name in {"read_file", "create_file", "write_file", "replace_text", "edit_file"}:
            state.mark_seen(self._relative(output.get("path") or arguments.get("path")))
        elif name == "apply_patch":
            for item in output.get("files") or []:
                if isinstance(item, dict) and item.get("action") != "delete":
                    state.mark_seen(self._relative(item.get("path")))
        elif name == "move_file":
            source = self._relative(arguments.get("source_path"))
            if state.has_seen(source):
                state.mark_seen(self._relative(arguments.get("destination_path")))
        elif name in {"grep_files", "search_files"}:
            for match in output.get("matches") or []:
                if isinstance(match, dict):
                    state.mark_seen(self._relative(match.get("path")))

    # ----------------------------------------------------------- execute

    def _record_permission(
        self,
        event_type: str,
        *,
        run_id: str,
        request_id: str,
        parent_event_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Emit a permission.requested/decided telemetry event, if wired.

        Payloads carry only structural metadata (tool name/kind/ids, decision
        outcome and scope) — never arguments or content — so they survive both
        local redaction and the server privacy gate under metadata policies.
        Returns the recorded event (for parent linkage) or ``None``.
        """
        record = getattr(self._telemetry, "record", None)
        if not callable(record):
            return None
        try:
            return record(
                event_type=event_type,
                run_id=run_id,
                request_id=request_id,
                parent_event_id=parent_event_id,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break tools
            return None

    def execute(
        self,
        tool_call: ToolCall,
        *,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None = None,
    ) -> dict[str, Any]:
        name = tool_call.name
        arguments = dict(tool_call.arguments)
        metadata = _safe_tool_event_metadata(name, arguments, workspace_root=self._workspace_root)

        if not self._is_known(name):
            available = sorted(self.known_tool_names())
            hint = (
                " Use run_command to run programs."
                if name.lower() in _SHELL_LIKE_TOOL_NAMES and "run_command" in available
                else ""
            )
            message = f"Unknown tool: {name}. Available tools: {', '.join(available) or 'none'}.{hint}"
            self._emit_denied(tool_call, arguments, metadata, message=message)
            raise ToolRegistryError(message, failure_reason="unsupported_tool")
        if not self._is_allowed(name):
            message = f"Tool is disabled by the assigned study policy: {name}"
            self._emit_denied(tool_call, arguments, metadata, message=message)
            raise ToolRegistryError(message, failure_reason="tool_not_allowed")
        if self._approval_policy not in {"auto", "per_step", "suggestion_only"}:
            message = f"Unknown assigned approval policy: {self._approval_policy}"
            self._emit_denied(tool_call, arguments, metadata, message=message)
            raise ToolRegistryError(message, failure_reason="invalid_approval_policy")
        if self._approval_policy == "suggestion_only" and self.requires_approval(name):
            message = f"Tool execution is disabled by suggestion-only policy: {name}"
            self._record_permission(
                "agent.permission.decided",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "tool_name": name,
                    "tool_call_id": tool_call.tool_call_id,
                    "decision": "rejected",
                    "decision_scope": "policy",
                },
            )
            self._emit_denied(tool_call, arguments, metadata, message=message)
            raise ToolRegistryError(message, failure_reason="approval_policy_denied")

        try:
            validated = _validate_arguments(name, arguments)
            if name == "apply_patch":
                validated["_patch"] = parse_patch(validated["patch"])
                validated["_guarded_paths"] = [
                    change.path for change in validated["_patch"] if change.action != "add"
                ]
        except PatchError as exc:
            self._emit_denied(tool_call, arguments, metadata, message=f"Invalid patch: {exc}")
            raise ToolArgumentError(str(exc), field="patch") from None
        except ToolArgumentError as exc:
            self._emit_denied(tool_call, arguments, metadata, message=f"Invalid arguments: {exc}")
            raise
        metadata = _safe_tool_event_metadata(name, validated, workspace_root=self._workspace_root)
        if metadata is not None and name.startswith("mcp__"):
            metadata["kind"] = self.display_kind(name)
        try:
            self._check_read_before_edit(name, validated)
        except ToolError as exc:
            self._emit_denied(tool_call, arguments, metadata, message=str(exc))
            raise
        preview = self._edit_preview(name, validated, tool_call, run_id=run_id, request_id=request_id)
        pending = self.needs_approval(name)
        self._emit_tool_start(tool_call, validated, metadata, preview, pending=pending)
        if preview is not None and preview.error is not None:
            self._emit_tool_failed(tool_call, validated, metadata, preview, error=preview.error)
            raise preview.error

        if pending:
            requested = self._record_permission(
                "agent.permission.requested",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "tool_name": name,
                    "tool_call_id": tool_call.tool_call_id,
                    "kind": metadata["kind"] if metadata else None,
                },
            )
            request_approval = getattr(self._event_sink, "request_approval", None)
            offer = self._revision_offer(name, validated, preview)
            if not callable(request_approval):
                decision = None
            elif offer is not None and _accepts_keyword(request_approval, "revise"):
                decision = request_approval(tool_call, validated, revise=offer)
            else:
                decision = request_approval(tool_call, validated)
            outcome = getattr(decision, "decision", "unavailable")
            scope = getattr(decision, "scope", None)
            revision = (
                self._apply_revision(
                    tool_call,
                    decision,
                    offer or RevisionOffer(),
                    run_id=run_id,
                    request_id=request_id,
                )
                if outcome == "revised"
                else None
            )
            self._record_permission(
                "agent.permission.decided",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=(requested or {}).get("event_id"),
                payload={
                    "tool_name": name,
                    "tool_call_id": tool_call.tool_call_id,
                    "kind": metadata["kind"] if metadata else None,
                    "decision": (
                        outcome
                        if outcome in ("accepted", "rejected", "cancelled", "unavailable", "revised")
                        else "unavailable"
                    ),
                    "decision_scope": scope,
                    **_revision_telemetry(decision, offer, revision),
                },
            )
            if revision is not None:
                self._emit_revision_card(tool_call, validated, metadata, preview, offer, revision)
                raise ToolRevisionRequested(
                    f"Tool approval revised for: {name}", result=revision.result()
                )
            if not getattr(decision, "accepted", bool(decision)):
                self._emit_tool_failed(
                    tool_call,
                    validated,
                    metadata,
                    preview,
                    message=f"Not run: approval {outcome}.",
                )
                raise ToolRegistryError(
                    f"Tool approval {outcome} for: {name}",
                    failure_reason=f"approval_{outcome}",
                )
        else:
            # No user round-trip (auto policy or tool needs none): the policy
            # itself accepted, recorded so every execution has a decision.
            self._record_permission(
                "agent.permission.decided",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "tool_name": name,
                    "tool_call_id": tool_call.tool_call_id,
                    "decision": "accepted",
                    "decision_scope": "policy",
                },
            )

        try:
            handler = self._handlers.get(name)
            if handler is not None:
                result = handler(
                    tool_call,
                    validated,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
            elif self._mcp_tools is not None and self._mcp_tools.has_tool(name):
                result = self._mcp_tools.execute(name, validated)
            else:
                raise ToolRegistryError(
                    f"Unsupported tool name: {name}",
                    failure_reason="unsupported_tool",
                )
        except Exception as exc:
            self._emit_tool_failed(tool_call, validated, metadata, preview, error=exc)
            raise
        tool_output = asdict(result) if is_dataclass(result) else dict(result)
        if validated.get("_argument_notes"):
            tool_output["argument_notes"] = list(validated["_argument_notes"])
        self._note_success(name, validated, tool_output)
        self._emit_tool_completed(tool_call, validated, tool_output, metadata, preview)
        return tool_output

    def report_invalid_arguments(
        self,
        tool_call: ToolCall,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        """Draw started+failed cards for a call whose arguments never parsed."""
        metadata = _safe_tool_event_metadata(
            tool_call.name, dict(tool_call.arguments), workspace_root=self._workspace_root
        )
        self._emit_denied(
            tool_call,
            dict(tool_call.arguments),
            metadata,
            message=f"Invalid arguments: {tool_call.argument_error}",
        )

    # ------------------------------------------------------------ revise

    def _revision_offer(
        self, name: str, arguments: dict[str, Any], preview: EditPreview | None
    ) -> RevisionOffer | None:
        """What "Revise…" offers on this approval; None when the profile switched it off.

        A single-file text change offers its hunks; any other call offers
        instructions only.
        """
        if not self._harness.approval_revise:
            return None
        path: object = None
        if preview is not None and preview.has_diff:
            if name in _REVISE_HUNK_TOOLS:
                path = arguments.get("path")
            elif name == "apply_patch":
                actions = list(arguments.get("_patch") or [])
                if (
                    len(actions) == 1
                    and getattr(actions[0], "action", None) == "update"
                    and getattr(actions[0], "move_to", None) is None
                ):
                    path = getattr(actions[0], "path", None)
        if not isinstance(path, str) or not path or preview is None:
            return RevisionOffer()
        return RevisionOffer(path=path, old_text=preview.old_text, new_text=preview.new_text)

    def _apply_revision(
        self,
        tool_call: ToolCall,
        decision: object,
        offer: RevisionOffer,
        *,
        run_id: str,
        request_id: str,
    ) -> _Revision:
        """Write the hunks the user kept, once, if the file still holds the previewed text.

        The proposed call itself never runs. Nothing is written when no hunk
        was kept or the file changed since the preview.
        """
        wanted = set(getattr(decision, "kept_hunks", ()) or ())
        kept = tuple(hunk for hunk in offer.selectable_hunks if hunk.index in wanted)
        instructions = str(getattr(decision, "instructions", "") or "")
        total = len(offer.hunks)
        if not kept or offer.path is None or offer.old_text is None or offer.new_text is None:
            return _Revision("instructions_only", (), total, instructions)
        try:
            current: str | None = self._read_current_text(
                offer.path, tool_call, run_id=run_id, request_id=request_id
            )
        except Exception:  # noqa: BLE001 - gone or unreadable: not the previewed text
            current = None
        if current != offer.old_text:
            return _Revision("file_changed", kept, total, instructions, path=offer.path)
        merged = apply_hunks(offer.old_text, offer.new_text, [hunk.index for hunk in kept])
        write_file = self._file_tools.write_file
        kwargs: dict[str, Any] = {
            "path": offer.path,
            "content": merged,
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if _accepts_keyword(write_file, "tool_name"):
            kwargs["tool_name"] = tool_call.name
        try:
            written = write_file(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return _Revision(
                "write_failed", kept, total, instructions, path=offer.path, error=str(exc)
            )
        syntax_error = getattr(written, "syntax_error", None)
        return _Revision(
            "applied",
            kept,
            total,
            instructions,
            path=offer.path,
            merged_text=merged,
            syntax_error=syntax_error if isinstance(syntax_error, dict) else None,
        )

    # ---------------------------------------------------------- handlers

    def _run_read_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "path": args["path"],
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if args.get("offset") is not None:
            kwargs["offset"] = args["offset"]
        if args.get("limit") is not None:
            kwargs["limit"] = args["limit"]
        return self._file_tools.read_file(**kwargs)

    def _run_create_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.create_file(
            path=args["path"],
            content=args["content"],
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_write_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.write_file(
            path=args["path"],
            content=args["content"],
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_replace_text(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "path": args["path"],
            "old_text": args["old_text"],
            "new_text": args["new_text"],
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if args.get("replace_all") and _accepts_keyword(self._file_tools.replace_text, "replace_all"):
            kwargs["replace_all"] = True
        return self._file_tools.replace_text(**kwargs)

    def _run_edit_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.edit_file(
            path=args["path"],
            edits=args["edits"],
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_delete_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.delete_file(
            path=args["path"],
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_move_file(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.move_file(
            source_path=args["source_path"],
            destination_path=args["destination_path"],
            overwrite=bool(args.get("overwrite", False)),
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_list_files(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "path": args.get("path", "."),
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if _accepts_keyword(self._file_tools.list_files, "recursive"):
            kwargs.update(
                recursive=bool(args.get("recursive", False)),
                max_depth=args.get("max_depth"),
                max_results=args.get("max_results", 500),
                include_ignored=bool(args.get("include_ignored", False)),
            )
        return self._file_tools.list_files(**kwargs)

    def _run_glob_files(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.glob_files(
            args["pattern"],
            path=args.get("path", "."),
            max_results=args.get("max_results", 500),
            include_ignored=bool(args.get("include_ignored", False)),
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_grep_files(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "path": args.get("path", "."),
            "glob": args.get("glob"),
            "case_insensitive": bool(args.get("case_insensitive", False)),
            "context_lines": args.get("context_lines", 0),
            "max_results": args.get("max_results", 200),
            "output_mode": args.get("output_mode", "content"),
            "include_ignored": bool(args.get("include_ignored", False)),
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if cancellation_event is not None and _accepts_keyword(self._file_tools.grep_files, "cancellation_event"):
            kwargs["cancellation_event"] = cancellation_event
        return self._file_tools.grep_files(args["pattern"], **kwargs)

    def _run_search_files(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "query": args["query"],
            "path": args.get("path", "."),
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        if cancellation_event is not None and _accepts_keyword(self._file_tools.search_files, "cancellation_event"):
            kwargs["cancellation_event"] = cancellation_event
        return self._file_tools.search_files(**kwargs)

    def _run_apply_patch(self, tool_call, args, *, run_id, request_id, cancellation_event):
        return self._file_tools.apply_patch(
            args["_patch"],
            tool_call_id=tool_call.tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _run_run_command(self, tool_call, args, *, run_id, request_id, cancellation_event):
        kwargs: dict[str, Any] = {
            "argv": args["argv"],
            "cwd": args.get("cwd", "."),
            "tool_call_id": tool_call.tool_call_id,
            "run_id": run_id,
            "request_id": request_id,
        }
        run_command = self._command_tools.run_command
        if args.get("timeout_seconds") is not None and _accepts_keyword(run_command, "timeout_seconds"):
            kwargs["timeout_seconds"] = args["timeout_seconds"]
        if cancellation_event is not None and _accepts_keyword(run_command, "cancel_event"):
            kwargs["cancel_event"] = cancellation_event
        if _accepts_keyword(run_command, "on_output"):
            kwargs["on_output"] = self._command_progress(tool_call, args)
        return run_command(**kwargs)

    def _command_progress(self, tool_call: ToolCall, args: dict[str, Any]) -> Callable[[str], None]:
        """Stream the running command's latest output into its card."""
        metadata = _safe_tool_event_metadata("run_command", args, workspace_root=self._workspace_root)
        title = metadata["title"] if metadata else "Run command"

        def on_output(tail: str) -> None:
            self._event_sink.tool_call(
                ToolCallEvent(
                    phase="progress",
                    tool_call_id=tool_call.tool_call_id,
                    tool_name=tool_call.name,
                    run_id="",
                    request_id="",
                    title=title,
                    kind="execute",
                    status="in_progress",
                    content_text=f"Running…\n{tail}",
                )
            )

        return on_output

    def _run_ask_user(self, tool_call, args, *, run_id, request_id, cancellation_event):
        options = list(args.get("options") or [])
        record = getattr(self._telemetry, "record", None)
        if callable(record):
            record(
                event_type="agent.tool.completed",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "tool_name": "ask_user",
                    "tool_call_id": tool_call.tool_call_id,
                    "status": "completed",
                    "option_count": len(options),
                    "text": args["question"],
                },
            )
        return {
            "status": "ok",
            "question": args["question"],
            "options": options,
            "note": "The question is shown to the user; their answer arrives as the next message.",
        }

    def _run_update_plan(self, tool_call, args, *, run_id, request_id, cancellation_event):
        entries: list[PlanEntrySpec] = list(args["entries"])
        emit_event(
            self._event_sink,
            "plan",
            PlanEvent(
                run_id=run_id,
                request_id=request_id,
                tool_call_id=tool_call.tool_call_id,
                entries=tuple(entries),
            ),
        )
        record = getattr(self._telemetry, "record", None)
        if callable(record):
            record(
                event_type="agent.tool.completed",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload={
                    "tool_name": "update_plan",
                    "tool_call_id": tool_call.tool_call_id,
                    "status": "completed",
                    "entry_count": len(entries),
                    "entries": [asdict(entry) for entry in entries],
                },
            )
        return {
            "status": "ok",
            "entry_count": len(entries),
            "entries": [asdict(entry) for entry in entries],
        }

    # ------------------------------------------------------------ cards

    def _emit_tool_start(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None,
        preview: EditPreview | None,
        *,
        pending: bool,
    ) -> None:
        if metadata is None:
            return
        self._event_sink.tool_call(
            ToolCallEvent(
                phase="started",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id="",
                request_id="",
                title=metadata["title"],
                kind=metadata["kind"],
                status="pending" if pending else "in_progress",
                path=metadata.get("path"),
                diff_old_text=preview.old_text if preview is not None and preview.has_diff else None,
                diff_new_text=preview.new_text if preview is not None and preview.has_diff else None,
                raw_input=metadata.get("raw_input"),
                locations=metadata.get("locations"),
                extra_diffs=preview.extra_diffs if preview is not None else (),
            )
        )

    def _emit_tool_completed(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        tool_output: dict[str, Any],
        metadata: dict[str, Any] | None,
        preview: EditPreview | None,
    ) -> None:
        if metadata is None:
            return
        has_diff = preview is not None and preview.has_diff
        self._event_sink.tool_call(
            ToolCallEvent(
                phase="completed",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id="",
                request_id="",
                title=metadata["title"],
                kind=metadata["kind"],
                status="completed",
                path=metadata.get("path"),
                diff_old_text=preview.old_text if has_diff else None,
                diff_new_text=preview.new_text if has_diff else None,
                content_text=_completed_card_text(tool_call.name, tool_output, metadata, has_diff),
                raw_output=tool_output,
                locations=metadata.get("locations"),
                extra_diffs=preview.extra_diffs if preview is not None else (),
            )
        )

    def _emit_tool_failed(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None,
        preview: EditPreview | None,
        *,
        error: BaseException | None = None,
        message: str | None = None,
    ) -> None:
        if metadata is None:
            return
        has_diff = preview is not None and preview.has_diff
        text = message or f"{metadata['title']} failed: {error}"
        self._event_sink.tool_call(
            ToolCallEvent(
                phase="failed",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id="",
                request_id="",
                title=metadata["title"],
                kind=metadata["kind"],
                status="failed",
                path=metadata.get("path"),
                diff_old_text=preview.old_text if has_diff else None,
                diff_new_text=preview.new_text if has_diff else None,
                content_text=text,
                locations=metadata.get("locations"),
                extra_diffs=preview.extra_diffs if preview is not None else (),
            )
        )

    def _emit_denied(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None,
        *,
        message: str,
    ) -> None:
        self._emit_tool_start(tool_call, arguments, metadata, None, pending=False)
        self._emit_tool_failed(tool_call, arguments, metadata, None, message=f"Not run: {message}")

    def _emit_revision_card(
        self,
        tool_call: ToolCall,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None,
        preview: EditPreview | None,
        offer: RevisionOffer | None,
        revision: _Revision,
    ) -> None:
        """Close the card of a revised call: the old→merged diff when kept hunks were written."""
        if revision.status != "applied":
            detail = {
                "file_changed": " (the file changed, so the kept parts were not applied)",
                "write_failed": f" (applying the kept parts failed: {revision.error})",
            }.get(revision.status, "")
            self._emit_tool_failed(
                tool_call, arguments, metadata, preview, message=f"Not run: revision requested{detail}."
            )
            return
        if metadata is None:
            return
        self._event_sink.tool_call(
            ToolCallEvent(
                phase="completed",
                tool_call_id=tool_call.tool_call_id,
                tool_name=tool_call.name,
                run_id="",
                request_id="",
                title=metadata["title"],
                kind=metadata["kind"],
                status="completed",
                path=metadata.get("path"),
                diff_old_text=offer.old_text if offer is not None else None,
                diff_new_text=revision.merged_text,
                content_text=(
                    f"Applied {len(revision.kept)} of {revision.total} parts; "
                    "the agent is revising the rest."
                ),
                locations=metadata.get("locations"),
            )
        )

    # ---------------------------------------------------------- previews

    def _edit_preview(
        self,
        name: str,
        arguments: dict[str, Any],
        tool_call: ToolCall,
        *,
        run_id: str,
        request_id: str,
    ) -> EditPreview | None:
        if name == "apply_patch":
            return self._patch_preview(
                arguments, tool_call_id=tool_call.tool_call_id, run_id=run_id, request_id=request_id
            )
        if name not in {"create_file", "write_file", "replace_text", "edit_file", "delete_file"}:
            return None
        path = str(arguments.get("path", ""))
        if name == "create_file":
            return EditPreview(None, str(arguments.get("content", "")))
        try:
            current = self._read_current_text(path, tool_call, run_id=run_id, request_id=request_id)
        except (ToolFileNotFoundError, FileNotFoundError) as exc:
            if name == "write_file":
                current = ""
            elif name == "delete_file":
                return EditPreview(None, None)
            else:
                return EditPreview(None, None, exc)
        except Exception as exc:  # noqa: BLE001
            policy_denial = isinstance(exc, PermissionError) and getattr(exc, "errno", None) is None
            if name == "delete_file" and not policy_denial:
                # Directories, binaries, oversized or unreadable files: no diff,
                # the handler decides (unlink needs only directory permissions).
                return EditPreview(None, None)
            # Policy denials were already recorded by read_text; fail before the
            # handler would record them again.
            return EditPreview(None, None, exc)
        if name == "write_file":
            return EditPreview(current, str(arguments.get("content", "")))
        if name == "delete_file":
            return EditPreview(current, "")
        if name == "replace_text":
            edits = [
                TextEdit(
                    old_text=str(arguments.get("old_text", "")),
                    new_text=str(arguments.get("new_text", "")),
                    replace_all=bool(arguments.get("replace_all", False)),
                )
            ]
        else:
            edits = list(arguments.get("edits", []))
        try:
            new_text, _replacements = apply_text_edits(current, edits, path=path)
        except ToolError as exc:
            return EditPreview(current, None, exc)
        return EditPreview(current, new_text)

    def _patch_preview(
        self, arguments: dict[str, Any], *, tool_call_id: str, run_id: str, request_id: str
    ) -> EditPreview | None:
        plan = getattr(self._file_tools, "plan_patch", None)
        if not callable(plan):
            return None
        try:
            planned = plan(
                arguments["_patch"], tool_call_id=tool_call_id, run_id=run_id, request_id=request_id
            )
        except PatchError as exc:
            return EditPreview(None, None, ToolError(str(exc), code="patch_does_not_apply"))
        except Exception as exc:  # noqa: BLE001 - outside workspace, binary file, ...
            return EditPreview(None, None, exc)
        diffs = [
            (
                _absolute_path(self._workspace_root, item.target) or item.target,
                item.old_text,
                item.new_text if item.new_text is not None else "",
            )
            for item in planned
        ]
        if not diffs:
            return None
        _first_path, first_old, first_new = diffs[0]
        return EditPreview(first_old, first_new, extra_diffs=tuple(diffs[1:]))

    def _read_current_text(
        self,
        path: str,
        tool_call: ToolCall,
        *,
        run_id: str,
        request_id: str,
    ) -> str:
        read_text = getattr(self._file_tools, "read_text", None)
        if callable(read_text):
            kwargs: dict[str, Any] = {
                "strict": True,
                "tool_call_id": tool_call.tool_call_id,
                "run_id": run_id,
                "request_id": request_id,
            }
            if _accepts_keyword(read_text, "tool_name"):
                kwargs["tool_name"] = tool_call.name
            value = read_text(path, **kwargs)
        else:
            value = self._file_tools.read_file(
                path=path,
                tool_call_id=tool_call.tool_call_id,
                run_id=run_id,
                request_id=request_id,
            )
        return _coerce_text(value)


def _coerce_text(value: Any) -> str:
    if isinstance(value, tuple) and value:
        value = value[0]
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        return value["content"]
    return ""


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return True
    if name in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def _revision_telemetry(
    decision: object, offer: RevisionOffer | None, revision: _Revision | None
) -> dict[str, Any]:
    """Decision payload keys of a "Revise…" form; empty when no form was shown.

    The form outcome and status are behavioural and the counts system
    metadata; the instructions travel as ``text`` (content: dropped when
    content is not stored).
    """
    action = getattr(decision, "elicitation_action", None)
    if not isinstance(action, str):
        return {}
    payload: dict[str, Any] = {
        "elicitation_action": action,
        "hunk_count": len(offer.hunks) if offer is not None else 0,
    }
    if revision is not None:
        payload["kept_hunk_count"] = len(revision.kept)
        payload["revise_status"] = revision.status
        if revision.instructions:
            payload["text"] = revision.instructions
    return payload


def _wrote_revision(result: dict[str, Any]) -> bool:
    """A "Revise…" result whose kept hunks were written (the workspace changed)."""
    return result.get("status") == "revise" and bool(result.get("applied_path"))


def _requires_manual_approval(tool_name: str) -> bool:
    return requires_manual_approval(tool_name)


# -------------------------------------------------------- argument checks


def _arg_present(arguments: dict[str, Any], field_name: str) -> bool:
    return field_name in arguments and arguments[field_name] is not None


def _require_str(arguments: dict[str, Any], field_name: str, tool: str) -> str:
    value = arguments.get(field_name)
    if not isinstance(value, str):
        got = "nothing" if not _arg_present(arguments, field_name) else type(value).__name__
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected a string, got {got}.",
            field=field_name,
        )
    return value


def _optional_str(arguments: dict[str, Any], field_name: str, tool: str, default: str | None = None) -> str | None:
    if not _arg_present(arguments, field_name):
        return default
    value = arguments[field_name]
    if not isinstance(value, str):
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected a string, got {type(value).__name__}.",
            field=field_name,
        )
    return value


def _arg_int(
    arguments: dict[str, Any],
    field_name: str,
    tool: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
    adjustments: list[str] | None = None,
) -> int | None:
    """Parse an integer argument.

    With ``adjustments``, an out-of-range value is clamped to the bound and a
    note is recorded for the tool result (size and paging arguments, where a
    clamp is always what the model meant); without it, out of range is an error.
    """
    if not _arg_present(arguments, field_name):
        return default
    value = arguments[field_name]
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        parsed = int(value.strip())
    if parsed is None:
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected an integer, got {value!r}.",
            field=field_name,
        )
    if adjustments is not None and minimum is not None and parsed < minimum:
        adjustments.append(f"{field_name} {parsed} was raised to the minimum {minimum}")
        return minimum
    if adjustments is not None and maximum is not None and parsed > maximum:
        adjustments.append(f"{field_name} {parsed} was lowered to the maximum {maximum}")
        return maximum
    if (minimum is not None and parsed < minimum) or (maximum is not None and parsed > maximum):
        bounds = []
        if minimum is not None:
            bounds.append(f">= {minimum}")
        if maximum is not None:
            bounds.append(f"<= {maximum}")
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected an integer {' and '.join(bounds)}, got {parsed}.",
            field=field_name,
        )
    return parsed


def _arg_number(arguments: dict[str, Any], field_name: str, tool: str) -> float | None:
    if not _arg_present(arguments, field_name):
        return None
    value = arguments[field_name]
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        return float(value)
    elif isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ToolArgumentError(
        f"Invalid argument '{field_name}' for {tool}: expected a number, got {value!r}.",
        field=field_name,
    )


def _arg_bool(arguments: dict[str, Any], field_name: str, tool: str, default: bool = False) -> bool:
    if not _arg_present(arguments, field_name):
        return default
    value = arguments[field_name]
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ToolArgumentError(
        f"Invalid argument '{field_name}' for {tool}: expected true or false, got {value!r}.",
        field=field_name,
    )


def _arg_enum(
    arguments: dict[str, Any],
    field_name: str,
    tool: str,
    choices: Sequence[str],
    default: str,
) -> str:
    if not _arg_present(arguments, field_name):
        return default
    value = arguments[field_name]
    if not isinstance(value, str) or value not in choices:
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected one of {', '.join(choices)}, got {value!r}.",
            field=field_name,
        )
    return value


# Characters that would need a shell; such a string is never split into argv.
_SHELL_SYNTAX = frozenset("|&;<>()$`*?[]{}~!#\n\\")


def _require_str_list(
    arguments: dict[str, Any], field_name: str, tool: str, *, adjustments: list[str] | None = None
) -> list[str]:
    value = arguments.get(field_name)
    if isinstance(value, str):
        if adjustments is not None and value.strip() and not (_SHELL_SYNTAX & set(value)):
            try:
                split = shlex.split(value)
            except ValueError:
                split = []
            if split:
                adjustments.append(f"{field_name} was given as one string and split into {split!r}")
                return split
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected a list of strings, got a single "
            "string. Split the command into separate argv items; argv is not passed through a "
            "shell, so pipes, redirection and globs need a shell program as argv[0] (for example "
            '["bash", "-c", "..."]).',
            field=field_name,
        )
    if not isinstance(value, (list, tuple)) or not value or not all(isinstance(item, str) for item in value):
        raise ToolArgumentError(
            f"Invalid argument '{field_name}' for {tool}: expected a non-empty list of strings.",
            field=field_name,
        )
    return [str(item) for item in value]


def _require_edits(arguments: dict[str, Any], tool: str) -> list[TextEdit]:
    value = arguments.get("edits")
    if not isinstance(value, list) or not value:
        raise ToolArgumentError(
            f"Invalid argument 'edits' for {tool}: expected a non-empty list of "
            "{{old_text, new_text}} objects.",
            field="edits",
        )
    if len(value) > 50:
        raise ToolArgumentError(
            f"Invalid argument 'edits' for {tool}: at most 50 edits per call.", field="edits"
        )
    edits: list[TextEdit] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolArgumentError(
                f"Invalid argument 'edits[{index}]' for {tool}: expected an object.",
                field=f"edits[{index}]",
            )
        for key in ("old_text", "new_text"):
            if not isinstance(item.get(key), str):
                raise ToolArgumentError(
                    f"Invalid argument 'edits[{index}].{key}' for {tool}: expected a string.",
                    field=f"edits[{index}].{key}",
                )
        try:
            replace_all = _arg_bool(item, "replace_all", tool)
        except ToolArgumentError as exc:
            raise ToolArgumentError(str(exc), field=f"edits[{index}].{exc.field}") from None
        edits.append(
            TextEdit(old_text=item["old_text"], new_text=item["new_text"], replace_all=replace_all)
        )
    return edits


def _require_plan_entries(arguments: dict[str, Any], tool: str) -> list[PlanEntrySpec]:
    value = arguments.get("entries")
    if not isinstance(value, list) or not value:
        raise ToolArgumentError(
            f"Invalid argument 'entries' for {tool}: expected a non-empty list of plan entries.",
            field="entries",
        )
    if len(value) > 30:
        raise ToolArgumentError(
            f"Invalid argument 'entries' for {tool}: at most 30 entries.", field="entries"
        )
    entries: list[PlanEntrySpec] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolArgumentError(
                f"Invalid argument 'entries[{index}]' for {tool}: expected an object.",
                field=f"entries[{index}]",
            )
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolArgumentError(
                f"Invalid argument 'entries[{index}].content' for {tool}: expected a non-empty string.",
                field=f"entries[{index}].content",
            )
        if not _arg_present(item, "status"):
            raise ToolArgumentError(
                f"Invalid argument 'entries[{index}].status' for {tool}: status is required.",
                field=f"entries[{index}].status",
            )
        try:
            status = _arg_enum(item, "status", tool, ("pending", "in_progress", "completed"), "pending")
            priority = _arg_enum(item, "priority", tool, ("high", "medium", "low"), "medium")
        except ToolArgumentError as exc:
            raise ToolArgumentError(str(exc), field=f"entries[{index}].{exc.field}") from None
        entries.append(PlanEntrySpec(content=content.strip(), status=status, priority=priority))
    return entries


_SHELL_PROGRAMS = ("bash", "sh", "zsh")
_SHELL_LIKE_TOOL_NAMES = frozenset({"bash", "sh", "shell", "terminal", "exec", "execute", "execute_command",
                                     "run_shell", "run_terminal_cmd", "cmd", "powershell"})
_NO_SHELL_SENTENCE = (
    "There is no shell: pipes, globs, '&&', 'cd' and redirection are not available; pass "
    "arguments as separate argv items."
)


def _usable_shell(blocked: object) -> str | None:
    """The first installed shell the study does not block, else None."""
    if not isinstance(blocked, (set, frozenset, list, tuple)):
        return None
    from code4me2_agent.command_tools import available_commands, command_key

    keys = {command_key(str(name)) for name in blocked}
    installed = available_commands([shell for shell in _SHELL_PROGRAMS if shell not in keys])
    return installed[0] if installed else None


def _shell_sentence(shell: str) -> str:
    return (
        "argv is not passed through a shell; for pipes, globs, '&&', 'cd' or redirection run a "
        f'shell explicitly, e.g. ["{shell}", "-c", "cd src && pytest -q | tail -20"].'
    )


def _with_shell_description(definition: dict[str, Any], shell: str) -> dict[str, Any]:
    function = definition.get("function") or {}
    description = function.get("description")
    if function.get("name") != "run_command" or not isinstance(description, str):
        return definition
    if _NO_SHELL_SENTENCE not in description:
        return definition
    return {
        **definition,
        "function": {**function, "description": description.replace(_NO_SHELL_SENTENCE, _shell_sentence(shell))},
    }


def _validate_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, normalized argument mapping for a catalogue tool.

    Unknown (MCP or unsupported) tools pass through unchanged. Size and paging
    arguments out of range are clamped, and an argv given as one plain string is
    split; each adjustment is reported to the model as ``argument_notes``.
    """
    adjustments: list[str] = []
    validated = _validate_arguments_strict(name, arguments, adjustments)
    if adjustments:
        validated = {**validated, "_argument_notes": adjustments}
    return validated


def _validate_arguments_strict(
    name: str, arguments: dict[str, Any], adjustments: list[str]
) -> dict[str, Any]:
    if name == "read_file":
        return {
            "path": _require_str(arguments, "path", name),
            "offset": _arg_int(arguments, "offset", name, minimum=1, adjustments=adjustments)
            or _arg_int(arguments, "line_start", name, minimum=1, adjustments=adjustments),
            "limit": _arg_int(arguments, "limit", name, minimum=1, maximum=5000, adjustments=adjustments)
            or _line_end_limit(arguments, name),
        }
    if name == "create_file":
        return {
            "path": _require_str(arguments, "path", name),
            "content": _require_str(arguments, "content", name),
        }
    if name == "write_file":
        return {
            "path": _require_str(arguments, "path", name),
            "content": _require_str(arguments, "content", name),
            "force": _arg_bool(arguments, "force", name),
        }
    if name == "replace_text":
        return {
            "path": _require_str(arguments, "path", name),
            "old_text": _require_str(arguments, "old_text", name),
            "new_text": _require_str(arguments, "new_text", name),
            "replace_all": _arg_bool(arguments, "replace_all", name),
            "force": _arg_bool(arguments, "force", name),
        }
    if name == "edit_file":
        return {
            "path": _require_str(arguments, "path", name),
            "edits": _require_edits(arguments, name),
            "force": _arg_bool(arguments, "force", name),
        }
    if name == "apply_patch":
        patch = arguments.get("patch", arguments.get("input"))
        if not isinstance(patch, str) or not patch.strip():
            raise ToolArgumentError(
                "Invalid argument 'patch' for apply_patch: expected the patch text, starting with "
                "'*** Begin Patch' and ending with '*** End Patch'.",
                field="patch",
            )
        return {"patch": patch, "force": _arg_bool(arguments, "force", name)}
    if name == "ask_user":
        question = _require_str(arguments, "question", name).strip()
        if not question:
            raise ToolArgumentError(
                "Invalid argument 'question' for ask_user: expected a non-empty question.",
                field="question",
            )
        options = arguments.get("options")
        if options is None:
            options = []
        if not isinstance(options, list) or len(options) > 6 or not all(
            isinstance(option, str) and option.strip() and len(option) <= 200 for option in options
        ):
            raise ToolArgumentError(
                "Invalid argument 'options' for ask_user: expected at most 6 short answer choices.",
                field="options",
            )
        return {"question": question[:2000], "options": [option.strip() for option in options]}
    if name == "delete_file":
        return {"path": _require_str(arguments, "path", name)}
    if name == "move_file":
        return {
            "source_path": _require_str(arguments, "source_path", name),
            "destination_path": _require_str(arguments, "destination_path", name),
            "overwrite": _arg_bool(arguments, "overwrite", name),
        }
    if name == "list_files":
        return {
            "path": _optional_str(arguments, "path", name, ".") or ".",
            "recursive": _arg_bool(arguments, "recursive", name),
            "max_depth": _arg_int(arguments, "max_depth", name, minimum=1, maximum=20, adjustments=adjustments),
            "max_results": _arg_int(
                arguments, "max_results", name, minimum=1, maximum=2000, default=500, adjustments=adjustments
            ),
            "include_ignored": _arg_bool(arguments, "include_ignored", name),
        }
    if name == "glob_files":
        return {
            "pattern": _require_str(arguments, "pattern", name),
            "path": _optional_str(arguments, "path", name, ".") or ".",
            "max_results": _arg_int(
                arguments, "max_results", name, minimum=1, maximum=2000, default=500, adjustments=adjustments
            ),
            "include_ignored": _arg_bool(arguments, "include_ignored", name),
        }
    if name == "grep_files":
        return {
            "pattern": _require_str(arguments, "pattern", name),
            "path": _optional_str(arguments, "path", name, ".") or ".",
            "glob": _optional_str(arguments, "glob", name),
            "case_insensitive": _arg_bool(arguments, "case_insensitive", name),
            "context_lines": _arg_int(
                arguments, "context_lines", name, minimum=0, maximum=10, default=0, adjustments=adjustments
            ),
            "max_results": _arg_int(
                arguments, "max_results", name, minimum=1, maximum=1000, default=200, adjustments=adjustments
            ),
            "output_mode": _arg_enum(
                arguments, "output_mode", name, ("content", "files_with_matches", "count"), "content"
            ),
            "include_ignored": _arg_bool(arguments, "include_ignored", name),
        }
    if name == "search_files":
        return {
            "query": _require_str(arguments, "query", name),
            "path": _optional_str(arguments, "path", name, ".") or ".",
        }
    if name == "run_command":
        return {
            "argv": _require_str_list(arguments, "argv", name, adjustments=adjustments),
            "cwd": _optional_str(arguments, "cwd", name, ".") or ".",
            "timeout_seconds": _arg_number(arguments, "timeout_seconds", name),
        }
    if name == "update_plan":
        return {"entries": _require_plan_entries(arguments, name)}
    return dict(arguments)


def _line_end_limit(arguments: dict[str, Any], tool: str) -> int | None:
    line_end = _arg_int(arguments, "line_end", tool, minimum=1)
    if line_end is None:
        return None
    start = _arg_int(arguments, "line_start", tool, minimum=1) or 1
    return max(1, min(line_end - start + 1, 5000))


# ---------------------------------------------------------------- providers


class FakeOpenAICompatibleProvider:
    """Scripted provider for tests: one script entry per model call."""

    def __init__(self, script: list[dict[str, object]]) -> None:
        self._script = [dict(item) for item in script]
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str = "",
        tool_choice: str | None = "auto",
        cancellation_event: Event | None = None,
        include_tools: bool = True,
    ) -> ProviderTurn:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tool_choice": tool_choice,
                "include_tools": include_tools,
            }
        )
        if not self._script:
            raise FakeProviderExhaustedError("The fake provider script is exhausted.")
        step = self._script.pop(0)
        if "delay_seconds" in step:
            _sleep_interruptible(float(step["delay_seconds"]), cancellation_event, sleep)
        output: dict[str, Any] = {"tool_calls": list(step.get("tool_calls") or [])}
        text = step.get("final_answer", step.get("text"))
        if text is not None:
            output["final_answer"] = str(text)
        reasoning = _normalize_optional_text(step.get("reasoning"))
        if reasoning is not None:
            output["reasoning"] = reasoning
        if isinstance(step.get("reasoning_fields"), dict):
            output["reasoning_fields"] = dict(step["reasoning_fields"])
        raw_usage = step.get("usage")
        usage = _normalize_usage(raw_usage, messages, output)
        return ProviderTurn(
            output=output,
            usage=usage,
            finish_reason=_normalize_optional_text(step.get("finish_reason")),
            model=str(step.get("model") or "fake-model"),
            request_payload={"messages": messages, "tool_choice": tool_choice},
            raw_response=dict(step),
            usage_estimated=not isinstance(raw_usage, dict),
        )


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
        max_output_tokens: int | None = None,
        retry_policy: RetryPolicy | None = None,
        request_deadline_seconds: float | None = None,
    ) -> None:
        self._kind = kind.strip() or "code4me_backend"
        self._request_deadline_seconds = request_deadline_seconds
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key_env = api_key_env
        self._timeout_seconds = timeout_seconds
        self._auth_headers = dict(auth_headers or {})
        # An empty list is an explicit server-assigned policy: do not replace it
        # with the local default tools, which would make the backend reject the
        # managed request as broader than the frozen profile.
        self._tool_definitions = (
            tool_catalog.tool_definitions() if tool_definitions is None else list(tool_definitions)
        )
        self._temperature = temperature
        self._session_id = session_id
        self._managed_request = managed_request
        self._max_output_tokens = max_output_tokens
        self._retry_policy = retry_policy or RetryPolicy.from_env()
        self._client_instance: OpenAI | None = None
        # Reasoning pass-back: echo what the model produced, and fill "" where a
        # DeepSeek V4 model requires the field. Each adapts at most once when
        # the server says otherwise (see generate()).
        self._echo_reasoning = True
        self._fill_reasoning = _needs_reasoning_on_every_assistant(model)

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str = "",
        tool_choice: str | None = "auto",
        cancellation_event: Event | None = None,
        include_tools: bool = True,
    ) -> ProviderTurn:
        request_payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_to_openai_message(message) for message in messages],
        }
        if self._tool_definitions and include_tools:
            request_payload["tools"] = list(self._tool_definitions)
            request_payload["tool_choice"] = tool_choice or "auto"
        _apply_reasoning_policy(
            request_payload["messages"],
            echo=self._echo_reasoning,
            fill=self._fill_reasoning and "tools" in request_payload,
        )
        if self._temperature is not None:
            request_payload["temperature"] = self._temperature
        if self._max_output_tokens:
            request_payload["max_tokens"] = int(self._max_output_tokens)

        if self._kind == "managed_backend":
            if not callable(self._managed_request):
                raise ProviderRequestFailed("Managed backend transport is unavailable.")
            response_payload = _call_with_retries(
                lambda: self._managed_request(
                    run_id=run_id,
                    session_id=self._session_id,
                    model_request=request_payload,
                ),
                policy=self._retry_policy,
                classify=_classify_managed_error(self._retry_policy),
                cancellation_event=cancellation_event,
            )
        else:
            def call() -> Any:
                return _call_with_retries(
                    lambda: self._sdk_call(request_payload, cancellation_event),
                    policy=self._retry_policy,
                    classify=_classify_sdk_error(self._retry_policy),
                    cancellation_event=cancellation_event,
                    before_attempt=_rate_limit_provider_request_from_env,
                )

            try:
                response_payload = call()
            except ProviderRequestFailed as exc:
                # The server said how it treats reasoning on assistant messages:
                # adapt once for this session and resend the same request.
                verdict = _reasoning_rejection(exc)
                if verdict == "rejected" and self._echo_reasoning:
                    self._echo_reasoning = False
                elif verdict == "required" and not self._fill_reasoning:
                    self._fill_reasoning = True
                else:
                    raise
                logging.warning("Model service %s reasoning pass-back; adapting: %s", verdict, exc)
                _apply_reasoning_policy(
                    request_payload["messages"],
                    echo=self._echo_reasoning,
                    fill=self._fill_reasoning and "tools" in request_payload,
                )
                response_payload = call()
        if not isinstance(response_payload, dict):
            raise ProviderRequestFailed("The model service returned a non-object response.")
        normalized_output = _normalize_openai_provider_response(response_payload)
        raw_usage = response_payload.get("usage")
        return ProviderTurn(
            output=normalized_output,
            usage=_normalize_usage(raw_usage, messages, normalized_output),
            finish_reason=_response_finish_reason(response_payload),
            model=str(response_payload.get("model") or self._model),
            request_payload=request_payload,
            raw_response=response_payload,
            usage_estimated=not _usage_is_reported(raw_usage),
        )

    def _sdk_call(
        self,
        request_payload: dict[str, Any],
        cancellation_event: Event | None,
    ) -> dict[str, Any]:
        client = self._client()
        logging.info(
            "Agent model request via OpenAI SDK: kind=%s endpoint=%s model=%s",
            self._kind,
            f"{self._openai_base_url()}/chat/completions",
            self._model,
        )
        # A daemon thread (not an executor) so an abandoned request after a
        # cancel can never delay interpreter exit when stdin closes.
        outcome: dict[str, Any] = {}
        finished = Event()

        def worker() -> None:
            try:
                outcome["value"] = client.chat.completions.with_raw_response.create(**request_payload)
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc
            finally:
                finished.set()

        Thread(target=worker, daemon=True, name="code4me2-provider").start()
        deadline = (
            perf_counter() + self._request_deadline_seconds
            if self._request_deadline_seconds
            else None
        )
        while not finished.wait(0.25):
            if _turn_was_cancelled(cancellation_event):
                raise ProviderCancelled("Cancelled while waiting for the model response.")
            if deadline is not None and perf_counter() > deadline:
                # Retryable like any timeout; the abandoned daemon thread is dropped.
                raise APITimeoutError(
                    request=httpx.Request("POST", f"{self._openai_base_url()}/chat/completions")
                )
        if "error" in outcome:
            raise outcome["error"]
        raw_response = outcome["value"]
        logging.info(
            "Agent model response via OpenAI SDK: status=%s model=%s",
            raw_response.http_response.status_code,
            self._model,
        )
        payload = raw_response.http_response.json()
        return payload if isinstance(payload, dict) else {"choices": payload}

    def _client(self) -> OpenAI:
        if self._client_instance is None:
            self._client_instance = _OpenAIClientWithoutSdkAuth(
                api_key="code4me-local-client",
                base_url=self._openai_base_url(),
                timeout=self._timeout_seconds,
                default_headers=self._headers(),
                max_retries=0,
            )
        return self._client_instance

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        api_key = os.getenv(self._api_key_env, "").strip() if self._api_key_env else ""
        if self._kind == "code4me_backend":
            headers.update(self._auth_headers)
        elif self._kind == "openai":
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            headers.update(_opencode_session_headers(self._base_url, self._session_id))
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


# OpenCode (Zen and Go) rejects a request without ``x-opencode-session``
# (400 MissingSessionID). Same scheme as the backend relay (agents/provider.py):
# a one-way hash that stays stable for one agent session.
_OPENCODE_SESSION_DOMAIN = b"code4me-opencode-session\x00"


def _opencode_session_headers(base_url: str, session_id: str) -> dict[str, str]:
    host = (urlparse(base_url).hostname or "").lower()
    if not (host == "opencode.ai" or host.endswith(".opencode.ai")):
        return {}
    digest = hashlib.sha256(_OPENCODE_SESSION_DOMAIN + (session_id or "").encode("utf-8"))
    return {"x-opencode-session": f"c4m-{digest.hexdigest()[:32]}"}


class _OpenAIClientWithoutSdkAuth(OpenAI):
    @property
    def auth_headers(self) -> dict[str, str]:
        return {}


# ------------------------------------------------------------------- memory


def _estimate_text_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _estimate_tokens(message: dict[str, Any]) -> int:
    try:
        serialized = json.dumps(message, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(message)
    return len(serialized) // 4 + 4


def _unit_tokens(unit: list[dict[str, Any]]) -> int:
    return sum(_estimate_tokens(message) for message in unit)


def _split_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group messages so an assistant tool-call message stays with its results."""
    units: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            unit = [message]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                unit.append(messages[index])
                index += 1
            units.append(unit)
            continue
        if role == "tool":
            # Stray result without its call; dropping it keeps the request valid.
            index += 1
            continue
        units.append([message])
        index += 1
    return units


_ELIDE_MIN_CHARS = 600
_ELIDE_NOTE = (
    "Earlier tool output removed from context to save space; call the tool again if you need it."
)


def _elide_tool_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if not isinstance(content, str) or len(content) <= _ELIDE_MIN_CHARS:
        return message
    status = "ok"
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            if parsed.get("elided"):
                return message
            status = str(parsed.get("status", "ok"))
    except (json.JSONDecodeError, TypeError):
        pass
    elided = dict(message)
    elided["content"] = json.dumps(
        {"status": status, "elided": True, "note": _ELIDE_NOTE, "preview": content[:160]},
        sort_keys=True,
    )
    return elided


def _elide_unit(unit: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _elide_tool_message(message) if message.get("role") == "tool" else message
        for message in unit
    ]


def _repair_tool_groups(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert missing tool results and drop orphaned ones from a persisted history."""
    repaired: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            repaired.append(message)
            expected: dict[str, str] = {}
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id") is not None:
                    expected[str(call["id"])] = str(call.get("name", ""))
            index += 1
            seen: set[str] = set()
            while index < len(messages) and messages[index].get("role") == "tool":
                result = messages[index]
                call_id = str(result.get("tool_call_id", ""))
                if call_id in expected and call_id not in seen:
                    repaired.append(result)
                    seen.add(call_id)
                index += 1
            for call_id, name in expected.items():
                if call_id not in seen:
                    repaired.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": name,
                            "content": json.dumps(
                                {
                                    "status": "cancelled",
                                    "error": "Result unavailable (interrupted before completion).",
                                    "tool_name": name,
                                    "tool_call_id": call_id,
                                },
                                sort_keys=True,
                            ),
                        }
                    )
            continue
        if role == "tool":
            index += 1
            continue
        repaired.append(message)
        index += 1
    return repaired


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
        # Provider tokens per estimated (chars/4) token, learned from usage.
        # Code, line numbers and escaped JSON tokenize denser than chars/4.
        self._scale = 1.0

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    @property
    def scale(self) -> float:
        return self._scale

    def calibrate(self, *, provider_prompt_tokens: int, estimated_tokens: int) -> None:
        """Adopt the provider's own count of the last request.

        Never below 1.0 (the estimate stays a floor) and capped so one odd
        response cannot collapse the window.
        """
        if provider_prompt_tokens > 0 and estimated_tokens > 0:
            self._scale = min(_MAX_TOKEN_SCALE, max(1.0, provider_prompt_tokens / estimated_tokens))

    def compact_current_turn(
        self, *, reserve_tokens: int = 0, low_water: float = _CURRENT_TURN_LOW_WATER
    ) -> dict[str, int] | None:
        """Elide old tool output of the current turn in one step, in place.

        Trimming just enough on every call moves the cut forward each time and
        so changes the request prefix on every call, which defeats provider
        prompt caching. Eliding down to ``low_water`` of the budget once keeps
        the prefix stable until the turn has grown back to the budget.
        Returns what was done, or None when the turn still fits.
        """
        system, budget, older, protected = self._partition(reserve_tokens)
        used = sum(_unit_tokens(unit) for unit in protected)
        # Same trigger as _shrink_protected: earlier turns are window()'s and
        # summarisation's to drop; only a turn that alone overflows is touched.
        if used <= budget or len(protected) <= 2:
            return None
        target = int(budget * low_water)
        shrunk = list(protected)
        elided = 0
        # Never the user prompt (0) or the newest unit, which the model is acting on.
        for position in range(1, len(shrunk) - 1):
            replacement = _elide_unit(shrunk[position])
            if replacement != shrunk[position]:
                shrunk[position] = replacement
                elided += 1
            if sum(_unit_tokens(unit) for unit in shrunk) <= target:
                break
        if not elided:
            return None
        rebuilt: list[dict[str, Any]] = [system] if system is not None else []
        for unit in [*older, *shrunk]:
            rebuilt.extend(unit)
        self._messages = rebuilt
        return {
            "elided_units": elided,
            "estimated_tokens_before": used,
            "estimated_tokens_after": sum(_unit_tokens(unit) for unit in shrunk),
            "budget_tokens": budget,
        }

    def append(self, message: dict[str, Any]) -> None:
        self._messages.append(dict(message))
        self._bound_history()

    def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        cleaned = [dict(message) for message in messages if isinstance(message, dict)]
        system, rest = _split_system(cleaned)
        repaired = _repair_tool_groups(rest)
        self._messages = ([system] if system is not None else []) + repaired

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(message) for message in self._messages]

    def message_count(self) -> int:
        return len(self._messages)

    def older_unit_count(self) -> int:
        _system, _budget, older, _protected = self._partition(0)
        return len(older)

    def clear(self) -> None:
        system, _rest = self._split_pinned_system()
        self._messages = [system] if system is not None else []

    def ensure_system_message(self, content: str) -> None:
        message = {"role": "system", "content": content}
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0] = message
            return
        self._messages.insert(0, message)

    def estimated_tokens(self) -> int:
        return sum(_estimate_tokens(message) for message in self._messages)

    def _partition(
        self, reserve_tokens: int
    ) -> tuple[dict[str, Any] | None, int, list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
        """``(system, budget, older units, protected units of the current turn)``."""
        system, rest = self._split_pinned_system()
        budget = int(self._max_tokens / self._scale) - max(0, int(reserve_tokens))
        if system is not None:
            budget -= _estimate_tokens(system)
        units = _split_units(rest)
        last_user = -1
        for position, unit in enumerate(units):
            # Runtime-authored notes (review findings, checkpoints) are not the
            # user's request: the turn starts at the user's own message.
            if unit[0].get("role") == "user" and not _is_runtime_message(unit[0]):
                last_user = position
        if last_user < 0:
            return system, budget, units, []
        return system, budget, units[:last_user], units[last_user:]

    def compaction_plan(self, *, reserve_tokens: int = 0, keep_recent_units: int = 4) -> int:
        """How many of the oldest units to summarise, or 0 when everything still fits.

        Summarisation is the second layer: it is only needed once whole units
        would be dropped even with their tool output elided. The most recent
        ``keep_recent_units`` older units stay verbatim.
        """
        _system, budget, older, protected = self._partition(reserve_tokens)
        if len(older) <= keep_recent_units:
            return 0
        used = sum(_unit_tokens(unit) for unit in protected)
        fits_elided = used + sum(_unit_tokens(_elide_unit(unit)) for unit in older) <= budget
        if fits_elided:
            return 0
        count = len(older) - keep_recent_units
        if count == 1 and _is_runtime_message(older[0][0]):
            return 0  # only the previous checkpoint would be re-summarised
        return count

    def oldest_units(self, count: int) -> list[list[dict[str, Any]]]:
        _system, _budget, older, _protected = self._partition(0)
        return [list(unit) for unit in older[:count]]

    def compact(self, count: int, summary: str) -> None:
        """Replace the ``count`` oldest units with one checkpoint message."""
        system, rest = self._split_pinned_system()
        units = _split_units(rest)
        _system, _budget, older, _protected = self._partition(0)
        count = max(0, min(count, len(older)))
        if count == 0:
            return
        checkpoint = {
            "role": "user",
            "content": f"{CHECKPOINT_PREFIX}\n{summary.strip()}",
            "code4me_runtime": "checkpoint",
        }
        rebuilt: list[dict[str, Any]] = [system] if system is not None else []
        rebuilt.append(checkpoint)
        for unit in units[count:]:
            rebuilt.extend(unit)
        self._messages = rebuilt

    def window(self, *, reserve_tokens: int = 0) -> list[dict[str, Any]]:
        system, budget, older, protected = self._partition(reserve_tokens)

        message_budget = self._max_messages if self._strategy == "last_messages" else None
        used = sum(_unit_tokens(unit) for unit in protected)
        message_count = sum(len(unit) for unit in protected)

        chosen: list[list[dict[str, Any]]] = []
        position = len(older) - 1
        # Pass 1: full units, newest first, stopping at the first that does not fit.
        while position >= 0:
            unit = older[position]
            cost = _unit_tokens(unit)
            if used + cost > budget or (
                message_budget is not None and message_count + len(unit) > message_budget
            ):
                break
            chosen.append(unit)
            used += cost
            message_count += len(unit)
            position -= 1
        # Pass 2: the remaining older units with their tool output elided.
        while position >= 0:
            unit = _elide_unit(older[position])
            cost = _unit_tokens(unit)
            if used + cost > budget or (
                message_budget is not None and message_count + len(unit) > message_budget
            ):
                break
            chosen.append(unit)
            used += cost
            message_count += len(unit)
            position -= 1
        chosen.reverse()

        protected = _shrink_protected(protected, budget)

        selected: list[dict[str, Any]] = []
        if system is not None:
            selected.append(system)
        for unit in chosen:
            selected.extend(unit)
        for unit in protected:
            selected.extend(unit)
        return [dict(message) for message in selected]

    def _split_pinned_system(
        self,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        return _split_system(self._messages)

    def _bound_history(self) -> None:
        """Keep the persisted history bounded for long sessions."""
        total = self.estimated_tokens()
        if total <= 4 * self._max_tokens:
            return
        system, rest = self._split_pinned_system()
        units = _split_units(rest)
        keep_full = 6
        if len(units) > keep_full:
            units = [_elide_unit(unit) for unit in units[:-keep_full]] + units[-keep_full:]
        while units and sum(_unit_tokens(unit) for unit in units) > 6 * self._max_tokens:
            if len(units) <= 1:
                break
            units.pop(0)
        rebuilt: list[dict[str, Any]] = []
        if system is not None:
            rebuilt.append(system)
        for unit in units:
            rebuilt.extend(unit)
        self._messages = rebuilt


CHECKPOINT_PREFIX = (
    "[Context checkpoint written by the Code4Me runtime: a summary of the earlier part of this "
    "session, which no longer fits the context window. It is not a new request.]"
)


def _is_runtime_message(message: dict[str, Any]) -> bool:
    return bool(message.get("code4me_runtime"))


def _split_system(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if messages and messages[0].get("role") == "system":
        return dict(messages[0]), messages[1:]
    return None, list(messages)


def _shrink_protected(
    protected: list[list[dict[str, Any]]], budget: int
) -> list[list[dict[str, Any]]]:
    """Elide older tool groups of the current turn when it alone exceeds the budget."""
    if not protected or sum(_unit_tokens(unit) for unit in protected) <= budget:
        return protected
    shrunk = list(protected)
    # Never touch the user prompt (index 0); elide older units first.
    for position in range(1, len(shrunk) - 1):
        shrunk[position] = _elide_unit(shrunk[position])
        if sum(_unit_tokens(unit) for unit in shrunk) <= budget:
            break
    if sum(_unit_tokens(unit) for unit in shrunk) > budget and len(shrunk) > 1:
        # The newest unit itself is oversize (a batch of large tool results):
        # elide its results oldest-first, keeping the most recent one intact,
        # and only then everything.
        newest = list(shrunk[-1])
        tool_positions = [i for i, message in enumerate(newest) if message.get("role") == "tool"]
        for position in tool_positions[:-1]:
            newest[position] = _elide_tool_message(newest[position])
            shrunk[-1] = newest
            if sum(_unit_tokens(unit) for unit in shrunk) <= budget:
                break
        if sum(_unit_tokens(unit) for unit in shrunk) > budget:
            shrunk[-1] = _elide_unit(newest)
    if sum(_unit_tokens(unit) for unit in shrunk) > budget:
        logger.warning(
            "Current turn exceeds the context budget even after eliding tool output "
            "(estimated %s tokens, budget %s).",
            sum(_unit_tokens(unit) for unit in shrunk),
            budget,
        )
    return shrunk


# ------------------------------------------------------------- react loop


@dataclass
class _TurnState:
    thoughts: list[str] = field(default_factory=list)
    used_tools: bool = False
    empty_retries: int = 0
    pending_nudge: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_reported: bool = True
    # Every model call of the turn (usage) vs. the calls that count toward
    # max_iterations (decision D-02: summarisation does not count).
    model_calls: int = 0
    budget_used: int = 0
    # Loop guard: identical (tool, arguments) calls since the last change.
    call_counts: dict[str, int] = field(default_factory=dict)
    forced_final: str | None = None
    # Transient notes for the next request only.
    notes: list[str] = field(default_factory=list)
    after_compaction: bool = False
    # Tool-call sequence numbers: when the workspace last changed and when a
    # command last ran, for verify-on-stop.
    step: int = 0
    last_change_step: int = 0
    last_command_step: int = 0
    commands_run: list[str] = field(default_factory=list)
    verify_nudged: bool = False
    continuation_nudges: int = 0
    step_at_last_continuation: int = -1
    pending_continuation_flag: bool = False
    verify_runs: int = 0
    verification_note: str | None = None
    reviewed: bool = False
    compaction_failed: bool = False
    # Changes made through IDE tools (MCP refactorings) have no file diff.
    external_changes: bool = False
    side_call_failures: list[dict[str, Any]] = field(default_factory=list)
    # Reasoning fields of the latest model response, attached (once) to the
    # first stored message that holds that response's output.
    reasoning_fields: dict[str, Any] | None = None
    # An IDE build/run (MCP) after the last change: enough to skip the verify
    # nudge, never a replacement for the profile's verification command.
    last_ide_run_step: int = 0

    @property
    def usage(self) -> dict[str, int] | None:
        if not self.usage_reported or self.model_calls == 0:
            return None
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


_LOOP_NUDGE = (
    "You have called {name} with identical arguments {count} times in this turn and the result "
    "has not changed. Do not repeat it: use the result you already have, change the arguments "
    "or the approach, or stop and explain what is blocking you."
)
_LOOP_STOP_NOTICE = (
    "Tools are now disabled for this turn because the same call was repeated {count} times "
    "without progress. Reply with a user-facing message: what you did, what did not work, and "
    "what the user could do next."
)
_LOOP_NUDGE_AT = 3
_LOOP_STOP_AT = 5
_LOOP_NOT_RUN = "Not run: tools are disabled for this turn after repeated identical calls."
_LOOP_STOPPED_TEXT = (
    "I stopped because the same step kept repeating without progress, so tools were disabled "
    "for the rest of this turn. Send another message with more guidance to continue."
)
_MAX_CONTINUATIONS = 2
_CUT_OFF_NOTE = (
    "[Runtime check, not a message from the user] Your last message was cut off by the output "
    "limit. Continue where it stopped: call the tools you need, or finish your final answer."
)
_CONTINUE_NOTE = (
    "[Runtime check, not a message from the user] Your last message announced more work but "
    "called no tool, so the turn would end here. Continue: call the tools you need, or, if "
    "you are done, give your final answer."
)
_VERIFY_NUDGE = (
    "[Runtime check, not a message from the user] You changed {files} but ran no command since "
    "your last change. If a test, build or lint command applies, run it now and fix what fails; "
    "otherwise give your final answer and name the command the user should run."
)
_VERIFY_FAILED_NOTE = (
    "[Runtime check, not a message from the user] The runtime ran the configured verification "
    "command `{command}` after your changes and it failed ({outcome}). Fix the failures, or "
    "explain in your final answer why they are unrelated to your change."
)
_REVIEW_NOTE = (
    "[Self-review by the Code4Me runtime, not a message from the user] A separate review of "
    "your diff for this request raised these possible problems:\n{issues}\n"
    "Fix the real ones (and re-run verification if you changed code), then give your final "
    "answer. If a point is wrong, say so briefly instead of changing the code."
)
_REVIEW_SYSTEM_PROMPT = (
    "You review a change made by an AI coding agent. You see the user's request, the unified "
    "diff of the agent's changes, the commands it ran, and its final message. Report only real "
    "problems: bugs, syntax or import errors, broken callers, logic that does not do what the "
    "request asks, parts of the request left undone, changes the request did not ask for, and "
    "claims in the final message that the diff and commands do not support (for example tests "
    "said to pass that were never run). Ignore style, naming and formatting. If there is nothing "
    "important, reply with exactly NO_ISSUES. Otherwise list at most five issues, one per line, "
    "each starting with '- ' and naming the file (and line when you can)."
)
_SUMMARY_SYSTEM_PROMPT = (
    "You compress the earlier part of a coding-agent session into a checkpoint. The original "
    "messages are removed afterwards, so the agent will rely only on your checkpoint. Keep "
    "facts, not prose, under these headings: Goal; User requirements and constraints; Work done "
    "(files changed and how, commands run and their results); Current state; Decisions; Errors "
    "and open problems (quote error messages verbatim); Next steps. Use short bullet points, "
    "never invent anything, and stay under 350 words."
)
_SUMMARY_INPUT_CHARS = 60_000
_REVIEW_DIFF_CHARS = 30_000


class OpenAICompatibleReactAdapter:
    def __init__(
        self,
        config: AgentConfig,
        *,
        telemetry: AgentTelemetryRecorder,
        tool_registry: ToolRegistry,
        event_sink: AgentEventSink | None = None,
        session_state: SessionToolState | None = None,
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._tool_registry = tool_registry
        self._event_sink = event_sink or NoopAgentEventSink()
        self._provider_instance: FakeOpenAICompatibleProvider | OpenAICompatibleProvider | None = None
        self._session_state = (
            session_state
            or getattr(tool_registry, "session_state", None)
            or SessionToolState()
        )

    @property
    def _harness(self) -> HarnessOptions:
        return getattr(self._config, "harness", None) or HarnessOptions()

    # ------------------------------------------------------------ prompt

    def handle_prompt(
        self,
        *,
        prompt: str,
        run_id: str,
        request_id: str,
        message_id: str | None,
        memory: "MemoryWindow | None" = None,
        cancellation_event: Event | None = None,
    ) -> AdapterResult:
        if memory is None:
            memory = MemoryWindow(
                strategy=self._config.adapter.memory_window.strategy,
                max_messages=self._config.adapter.memory_window.max_messages,
                max_tokens=self._config.adapter.memory_window.max_tokens,
            )
        command = slash_commands.parse(prompt, managed=bool(self._config.managed_mode))
        if command is not None:
            return self._run_slash_command(
                command,
                memory=memory,
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
            )
        provider = self._provider()
        tool_names = sorted(self._tool_registry.known_tool_names())
        instructions = self._project_instructions()
        profile = self._prompt_profile()
        memory.ensure_system_message(
            self._system_context(
                tool_names=tool_names, instructions=instructions, prompt_profile=profile
            )
        )
        prior_messages = max(0, memory.message_count() - 1)
        memory.append({"role": "user", "content": prompt})
        self._session_state.begin_turn(run_id, prompt)
        try:
            return self._run_turn(
                provider,
                prompt=prompt,
                run_id=run_id,
                request_id=request_id,
                memory=memory,
                cancellation_event=cancellation_event,
                prior_messages=prior_messages,
                profile=profile,
                instructions=instructions,
            )
        finally:
            self._session_state.end_turn()

    def _run_turn(
        self,
        provider: FakeOpenAICompatibleProvider | OpenAICompatibleProvider,
        *,
        prompt: str,
        run_id: str,
        request_id: str,
        memory: MemoryWindow,
        cancellation_event: Event | None,
        prior_messages: int,
        profile: str,
        instructions: prompting.ProjectInstructions | None,
    ) -> AdapterResult:
        definitions = self._tool_registry.definitions()
        reserve_tokens = _estimate_text_tokens(json.dumps(definitions)) + _OUTPUT_HEADROOM_TOKENS
        state = _TurnState()
        max_iterations = max(1, int(self._config.adapter.max_iterations))
        harness = self._harness

        while True:
            if _turn_was_cancelled(cancellation_event):
                return self._cancelled(memory, state)
            if harness.context_summarization and not state.compaction_failed:
                compacted = self._maybe_compact(
                    provider,
                    memory,
                    state,
                    reserve_tokens=reserve_tokens,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
                if compacted == "cancelled":
                    return self._cancelled(memory, state)
                if compacted == "failed":
                    state.compaction_failed = True  # do not retry every iteration
            trimmed = memory.compact_current_turn(reserve_tokens=reserve_tokens)
            # Reported as integer metrics on this iteration's agent.model.requested
            # (no event type of its own: the study counts steps as events).
            request_metrics = (
                {
                    "context_elided_units": trimmed["elided_units"],
                    "context_tokens_before": trimmed["estimated_tokens_before"],
                    "context_tokens_after": trimmed["estimated_tokens_after"],
                    "context_budget_tokens": trimmed["budget_tokens"],
                    "token_scale_milli": int(round(memory.scale * 1000)),
                }
                if trimmed
                else None
            )
            iteration = state.budget_used + 1
            final_round = iteration >= max_iterations or state.forced_final is not None
            request_messages = memory.window(reserve_tokens=reserve_tokens)
            notes: list[str] = []
            tool_choice: str | None = "auto"
            if state.forced_final is not None:
                notes.append(state.forced_final)
                tool_choice = "none"
            elif final_round and max_iterations > 1:
                notes.append(_BUDGET_NOTICE)
                tool_choice = "none"
            else:
                if state.pending_nudge:
                    notes.append(_EMPTY_RESPONSE_NUDGE)
                    state.pending_nudge = False
                notes.extend(state.notes)
            state.notes = []
            reminded = bool(
                harness.instruction_reminders
                and prompting.should_remind(
                    iteration, prior_messages, after_compaction=state.after_compaction
                )
            )
            if reminded:
                notes.append(self._reminder(max_iterations - iteration + 1, instructions))
            state.after_compaction = False
            if not definitions:
                tool_choice = None
            transient = [{"role": "user", "content": "\n\n".join(notes)}] if notes else []
            messages = request_messages + transient

            payload: dict[str, Any] = {
                "iteration": iteration,
                "message_count": len(messages),
                "approx_token_count": sum(_estimate_tokens(message) for message in messages),
                "tool_choice": tool_choice,
                "final_round": final_round,
                "call_purpose": "turn",
            }
            if iteration == 1:
                payload["prompt_profile"] = profile
                if instructions is not None:
                    payload.update(instructions.telemetry())
            if reminded:
                payload["reminder"] = True
            if state.forced_final is not None:
                payload["loop_guard"] = "forced_stop"
            if state.pending_continuation_flag:
                payload["continuation_nudge"] = True
                state.pending_continuation_flag = False
            self._telemetry.record(
                event_type="agent.model.requested",
                run_id=run_id,
                request_id=request_id,
                parent_event_id=None,
                payload=payload,
                metrics=request_metrics,
                raw_payload={"messages": messages},
            )
            started_at = perf_counter()
            try:
                turn = provider.generate(
                    messages,
                    run_id=run_id,
                    tool_choice=tool_choice,
                    cancellation_event=cancellation_event,
                )
            except ProviderCancelled:
                return self._cancelled(memory, state)
            except FakeProviderExhaustedError:
                return self._fail(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="provider_exhausted",
                    stop_reason="provider_exhausted",
                    text="The fake provider stopped before returning a final answer.",
                )
            except ProviderRequestFailed as exc:
                return self._fail(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="provider_request_failed",
                    stop_reason="error",
                    text=_provider_error_text(exc),
                    failure=_model_failure_details(exc, started_at=started_at, purpose="turn"),
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="provider_request_failed",
                    stop_reason="error",
                    text=f"The model request failed: {exc}",
                    failure=_model_failure_details(exc, started_at=started_at, purpose="turn"),
                )
            state.budget_used += 1
            self._account_model_call(
                state,
                turn,
                run_id=run_id,
                request_id=request_id,
                duration_ms=(perf_counter() - started_at) * 1000,
                iteration=iteration,
                context_budget=memory.max_tokens,
            )
            if not turn.usage_estimated:
                memory.calibrate(
                    provider_prompt_tokens=int(turn.usage.get("prompt_tokens", 0)),
                    estimated_tokens=payload["approx_token_count"] + reserve_tokens - _OUTPUT_HEADROOM_TOKENS,
                )
            if _turn_was_cancelled(cancellation_event):
                return self._cancelled(memory, state)

            parsed = self._parse_output(turn, run_id=run_id, request_id=request_id)
            if parsed is None:
                return self._fail(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    failure_reason="parse_failed",
                    stop_reason="error",
                    text=_PARSE_ERROR_TEXT,
                )
            state.reasoning_fields = parsed.reasoning_fields
            if parsed.thought:
                state.thoughts.append(parsed.thought)
                emit_event(
                    self._event_sink,
                    "thought",
                    ThoughtEvent(
                        run_id=run_id,
                        request_id=request_id,
                        text=parsed.thought,
                        phase="completed",
                        duration_ms=round((perf_counter() - started_at) * 1000, 3),
                    ),
                )

            if parsed.tool_calls and state.forced_final is not None:
                # Tools were disabled after repeated identical calls; a provider
                # that ignores tool_choice="none" must not run them again. The
                # never-run calls are runtime-only: a reopened chat skips them
                # (the model's text is part of the final message below).
                memory.append(
                    _with_reasoning(
                        _assistant_tool_call_message(parsed.tool_calls, text=None, runtime="loop_stop"), state
                    )
                )
                for call in parsed.tool_calls:
                    memory.append(_tool_message(call, _not_run_result(call, _LOOP_NOT_RUN)))
                text = f"{parsed.text}\n\n{_LOOP_STOPPED_TEXT}" if parsed.text else _LOOP_STOPPED_TEXT
                return self._finish(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    text=text,
                    stop_reason="loop_detected",
                    iteration=iteration,
                    emit=True,
                )

            if parsed.tool_calls:
                if parsed.text:
                    self._emit_text(parsed.text, run_id=run_id, request_id=request_id, final=False, iteration=iteration)
                memory.append(_with_reasoning(_assistant_tool_call_message(parsed.tool_calls, text=parsed.text), state))
                state.used_tools = True
                results = self._execute_batch(
                    parsed.tool_calls,
                    memory=memory,
                    state=state,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                    max_chars=_batch_result_cap(memory.max_tokens, len(parsed.tool_calls)),
                )
                if results is None:
                    return self._cancelled(memory, state)
                question = _asked_question(parsed.tool_calls, results)
                if question is not None:
                    return self._finish(
                        memory,
                        state,
                        run_id=run_id,
                        request_id=request_id,
                        text=question,
                        stop_reason="awaiting_user",
                        iteration=iteration,
                        emit=True,
                    )
                self._update_loop_guard(state, parsed.tool_calls, results)
                if final_round:
                    return self._finish(
                        memory,
                        state,
                        run_id=run_id,
                        request_id=request_id,
                        text=_budget_exhausted_text(parsed.tool_calls, results),
                        stop_reason="loop_detected" if state.forced_final else "max_turn_requests",
                        iteration=iteration,
                        emit=True,
                    )
                continue

            cut_off = parsed.finish_reason == "length"
            if (
                getattr(self._config, "autonomous", False)
                and parsed.text
                and state.used_tools
                and state.forced_final is None
                and not final_round
                and state.continuation_nudges < _MAX_CONTINUATIONS
                and state.step > state.step_at_last_continuation
                and max_iterations - state.budget_used >= 2
                and (cut_off or _announces_more_work(parsed.text))
            ):
                # Headless only: "Let me also check…" (or an answer cut off at
                # the output limit) with no tool call would end the turn
                # mid-task. At most two nudges, each needing a tool run since
                # the last one, so the model can always end the turn.
                state.continuation_nudges += 1
                state.step_at_last_continuation = state.step
                state.pending_continuation_flag = True
                self._continue_with_note(
                    memory,
                    parsed.text,
                    note=_CUT_OFF_NOTE if cut_off else _CONTINUE_NOTE,
                    kind="continue",
                    run_id=run_id,
                    request_id=request_id,
                    iteration=iteration,
                    state=state,
                )
                continue
            if parsed.text:
                if state.forced_final is not None:
                    stop_reason = "loop_detected"
                elif final_round and max_iterations > 1 and state.used_tools:
                    stop_reason = "max_turn_requests"
                else:
                    stop_reason = _stop_reason_from_finish(parsed.finish_reason)
                    gate = self._stop_gate(
                        provider,
                        memory,
                        state,
                        prompt=prompt,
                        answer=parsed.text,
                        max_iterations=max_iterations,
                        run_id=run_id,
                        request_id=request_id,
                        cancellation_event=cancellation_event,
                    )
                    if gate == "cancelled":
                        return self._cancelled(memory, state)
                    if gate == "continue":
                        continue
                text = parsed.text
                if state.verification_note:
                    text = f"{text}\n\n{state.verification_note}"
                return self._finish(
                    memory,
                    state,
                    run_id=run_id,
                    request_id=request_id,
                    text=text,
                    stop_reason=stop_reason,
                    iteration=iteration,
                    emit=True,
                )

            if state.empty_retries == 0 and not final_round:
                state.empty_retries = 1
                state.pending_nudge = True
                continue
            return self._fail(
                memory,
                state,
                run_id=run_id,
                request_id=request_id,
                failure_reason="empty_response",
                stop_reason="error",
                text=_EMPTY_RESPONSE_TEXT,
            )

    # -------------------------------------------------------- model calls

    def _account_model_call(
        self,
        state: _TurnState,
        turn: ProviderTurn,
        *,
        run_id: str,
        request_id: str,
        duration_ms: float,
        iteration: int,
        context_budget: int,
        purpose: str = "turn",
    ) -> None:
        state.model_calls += 1
        self._record_model_completed(
            run_id=run_id,
            request_id=request_id,
            duration_ms=duration_ms,
            provider_turn=turn,
            purpose=purpose,
        )
        if turn.usage_estimated:
            state.usage_reported = False
            return
        state.prompt_tokens += turn.usage.get("prompt_tokens", 0)
        state.completion_tokens += turn.usage.get("completion_tokens", 0)
        state.total_tokens += turn.usage.get("total_tokens", 0)
        if purpose != "turn":
            # A summary or review call runs on a different, small context: an
            # IDE context meter must keep showing the conversation's usage.
            return
        emit_event(
            self._event_sink,
            "usage",
            UsageEvent(
                run_id=run_id,
                request_id=request_id,
                iteration=iteration,
                model=turn.model,
                prompt_tokens=turn.usage.get("prompt_tokens", 0),
                completion_tokens=turn.usage.get("completion_tokens", 0),
                total_tokens=turn.usage.get("total_tokens", 0),
                turn_total_tokens=state.total_tokens,
                context_budget_tokens=context_budget,
            ),
        )

    def _side_call(
        self,
        provider: FakeOpenAICompatibleProvider | OpenAICompatibleProvider,
        messages: list[dict[str, Any]],
        state: _TurnState,
        *,
        purpose: str,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
        context_budget: int,
        extra_payload: dict[str, Any] | None = None,
    ) -> str | None:
        """A tool-less model call outside the conversation (summary, review).

        Returns the text, ``None`` when the call failed (the turn carries on
        without it) and raises ``ProviderCancelled`` on cancellation.
        """
        self._telemetry.record(
            event_type="agent.model.requested",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "iteration": state.budget_used,
                "message_count": len(messages),
                "approx_token_count": sum(_estimate_tokens(message) for message in messages),
                "tool_choice": None,
                "final_round": False,
                "call_purpose": purpose,
                **(extra_payload or {}),
            },
            raw_payload={"messages": messages},
        )
        started_at = perf_counter()
        try:
            turn = provider.generate(
                messages,
                run_id=run_id,
                tool_choice=None,
                cancellation_event=cancellation_event,
                include_tools=False,
            )
        except ProviderCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the main loop continues without it
            logger.warning("The %s model call failed: %s", purpose, exc)
            payload, metrics = _model_failure_details(exc, started_at=started_at, purpose=purpose)
            state.side_call_failures.append(
                {
                    "call_purpose": purpose,
                    "error_type": payload["error_type"],
                    **{key: metrics[key] for key in ("status_code", "attempts") if key in metrics},
                }
            )
            return None
        self._account_model_call(
            state,
            turn,
            run_id=run_id,
            request_id=request_id,
            duration_ms=(perf_counter() - started_at) * 1000,
            iteration=max(1, state.budget_used),
            context_budget=context_budget,
            purpose=purpose,
        )
        output = turn.output.get("output", turn.output) if isinstance(turn.output, dict) else {}
        text = output.get("final_answer", output.get("text")) if isinstance(output, dict) else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    # ----------------------------------------------------- summarisation

    def _maybe_compact(
        self,
        provider: FakeOpenAICompatibleProvider | OpenAICompatibleProvider,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        reserve_tokens: int,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
        keep_recent_units: int = 4,
        force: bool = False,
    ) -> str | None:
        """Summarise the oldest units when elision alone no longer fits (D1)."""
        count = memory.compaction_plan(
            reserve_tokens=reserve_tokens, keep_recent_units=keep_recent_units
        )
        if count <= 0 and force:
            count = max(0, memory.older_unit_count() - keep_recent_units)
        if count <= 0:
            return None
        units = memory.oldest_units(count)
        rendered = _render_units_for_summary(units, max_chars=min(_SUMMARY_INPUT_CHARS, memory.max_tokens * 2))
        messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Session excerpt to compress:\n\n{rendered}"},
        ]
        try:
            summary = self._side_call(
                provider,
                messages,
                state,
                purpose="summarize",
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
                context_budget=memory.max_tokens,
                extra_payload={"compacted_units": count},
            )
        except ProviderCancelled:
            return "cancelled"
        if not summary:
            return "failed"
        memory.compact(count, summary)
        state.after_compaction = True
        return "compacted"

    # --------------------------------------------------------- stop gates

    def _stop_gate(
        self,
        provider: FakeOpenAICompatibleProvider | OpenAICompatibleProvider,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        prompt: str,
        answer: str,
        max_iterations: int,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
    ) -> str:
        """Before a final answer: verify (C5), then self-review (E3).

        Returns ``finish``, ``continue`` (the loop goes on with a note in
        memory) or ``cancelled``. A continuation needs room to act: two calls
        after a verification problem (fix, answer), three for a review
        (review, fix, answer); the last call of a turn has no tools.
        """
        changed = self._session_state.changed_paths()
        if not changed and not state.external_changes:
            return "finish"
        remaining = max_iterations - state.budget_used
        harness = self._harness
        if harness.verify_on_stop and remaining >= 2 and state.last_change_step > state.last_command_step:
            command = self._verify_command()
            if command is not None and state.verify_runs < 2:
                state.verify_runs += 1
                outcome = self._run_verification(
                    command,
                    memory,
                    state,
                    answer=answer,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                )
                if outcome in {"cancelled", "continue"}:
                    return outcome
            elif (
                command is None
                and not state.verify_nudged
                and self._can_run_commands()
                and state.last_change_step > state.last_ide_run_step
            ):
                state.verify_nudged = True
                self._continue_with_note(
                    memory,
                    answer,
                    note=_VERIFY_NUDGE.format(
                        files=(", ".join(changed[:5]) + (" and more" if len(changed) > 5 else ""))
                        or "files through IDE tools",
                    ),
                    kind="verify",
                    run_id=run_id,
                    request_id=request_id,
                    iteration=state.budget_used,
                    state=state,
                )
                return "continue"
        if harness.self_review and not state.reviewed and remaining >= 3:
            state.reviewed = True
            diff = self._session_state.turn_diff(max_chars=_REVIEW_DIFF_CHARS)
            if not diff.strip():
                return "finish"
            calls_before = state.model_calls
            try:
                issues = self._self_review(
                    provider,
                    state,
                    prompt=prompt,
                    answer=answer,
                    diff=diff,
                    run_id=run_id,
                    request_id=request_id,
                    cancellation_event=cancellation_event,
                    context_budget=memory.max_tokens,
                )
            except ProviderCancelled:
                return "cancelled"
            if state.model_calls > calls_before:
                state.budget_used += 1  # decision D-02: the review counts
            if issues:
                self._emit_text(
                    f"Self-review of the changes raised possible issues:\n{issues}",
                    run_id=run_id,
                    request_id=request_id,
                    final=False,
                    iteration=state.budget_used,
                )
                self._continue_with_note(
                    memory,
                    answer,
                    note=_REVIEW_NOTE.format(issues=issues),
                    kind="review",
                    run_id=run_id,
                    request_id=request_id,
                    iteration=state.budget_used,
                    emit_answer=False,
                    state=state,
                )
                return "continue"
        return "finish"

    def _continue_with_note(
        self,
        memory: MemoryWindow,
        answer: str,
        *,
        note: str,
        kind: str,
        run_id: str,
        request_id: str,
        iteration: int,
        emit_answer: bool = True,
        state: _TurnState | None = None,
    ) -> None:
        """Keep the model's provisional answer and add a runtime note; the loop continues."""
        provisional: dict[str, Any] = {"role": "assistant", "content": answer}
        if state is not None:
            provisional = _with_reasoning(provisional, state)
        if emit_answer:
            self._emit_text(answer, run_id=run_id, request_id=request_id, final=False, iteration=iteration)
        else:
            # The model sees its provisional answer; the user never did, so a
            # reopened chat must not replay it.
            provisional["code4me_runtime"] = "provisional"
        memory.append(provisional)
        memory.append({"role": "user", "content": note, "code4me_runtime": kind})

    def _verify_command(self) -> list[str] | None:
        """The profile's verification argv, when this session may actually run it.

        A per-user config row can block more programs, and the program may not
        be installed here; either falls back to asking the model to verify.
        """
        command = self._harness.verify_command
        if not command or not self._can_run_commands():
            return None
        from code4me2_agent.command_tools import available_commands, blocked_program

        if blocked_program(command, self._config.commands.blocked_commands) is not None:
            return None
        if not available_commands([Path(command[0]).name]):
            return None
        return list(command)

    def _can_run_commands(self) -> bool:
        if self._config.approval_policy == "suggestion_only":
            return False
        return "run_command" in self._tool_registry.known_tool_names()

    def _commands_status(self) -> str:
        """The ``/status`` description of what run_command may start."""
        if not self._can_run_commands():
            return "none"
        blocked = self._config.commands.blocked_commands
        return "any installed program" + (f" except {', '.join(blocked)}" if blocked else "")

    def _run_verification(
        self,
        command: list[str],
        memory: MemoryWindow,
        state: _TurnState,
        *,
        answer: str,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
    ) -> str:
        """Run the profile's verification command; ``continue`` when it failed."""
        call = ToolCall(
            tool_call_id=f"verify-{run_id[:8]}-{state.verify_runs}",
            name="run_command",
            arguments={"argv": list(command)},
        )
        result = self._run_tool_call(
            call,
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
            max_chars=_batch_result_cap(memory.max_tokens, 1),
        )
        if _turn_was_cancelled(cancellation_event):
            memory.append(_assistant_tool_call_message([call], text=None, runtime="verify"))
            memory.append(_tool_message(call, result))
            return "cancelled"
        status = result.get("status")
        exit_code = result.get("exit_code")
        shown = " ".join(command)
        state.step += 1
        state.last_command_step = state.step
        state.commands_run.append(shown)
        failed = (status == "ok" and exit_code not in (0, None)) or status == "timeout"
        if not failed:
            memory.append(_assistant_tool_call_message([call], text=None, runtime="verify"))
            memory.append(_tool_message(call, result))
            if status == "ok" and exit_code == 0:
                state.verification_note = f"Verified by the runtime: `{shown}` passed."
            else:
                # Not run (denied, rejected, not installed): not a failure of the change.
                state.verification_note = (
                    f"The runtime could not run the verification command `{shown}` "
                    f"({result.get('reason') or result.get('error_code') or status})."
                )
            return "passed" if status == "ok" else "skipped"
        outcome = f"exit code {exit_code}" if status == "ok" else "timed out"
        self._emit_text(answer, run_id=run_id, request_id=request_id, final=False, iteration=state.budget_used)
        memory.append(_with_reasoning({"role": "assistant", "content": answer}, state))
        memory.append(_assistant_tool_call_message([call], text=None, runtime="verify"))
        memory.append(_tool_message(call, result))
        memory.append(
            {
                "role": "user",
                "content": _VERIFY_FAILED_NOTE.format(command=shown, outcome=outcome),
                "code4me_runtime": "verify",
            }
        )
        state.verification_note = None
        return "continue"

    def _self_review(
        self,
        provider: FakeOpenAICompatibleProvider | OpenAICompatibleProvider,
        state: _TurnState,
        *,
        prompt: str,
        answer: str,
        diff: str,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
        context_budget: int,
    ) -> str | None:
        """One clean-context review call; returns the issues, or None when there are none."""
        commands = "\n".join(f"- {command}" for command in state.commands_run[-10:]) or "(none)"
        messages = [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{prompt[:6000]}\n\nDiff of the agent's changes:\n```diff\n{diff}```\n\n"
                    f"Commands the agent ran this turn:\n{commands}\n\nAgent's final message:\n{answer[:4000]}"
                ),
            },
        ]
        text = self._side_call(
            provider,
            messages,
            state,
            purpose="self_review",
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
            context_budget=context_budget,
        )
        return _review_issues(text)

    # ------------------------------------------------------- tool batches

    def _execute_batch(
        self,
        tool_calls: list[ToolCall],
        *,
        memory: MemoryWindow,
        state: _TurnState,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
        max_chars: int,
    ) -> list[dict[str, Any]] | None:
        """Run a batch in order; runs of read-only calls execute concurrently (E2).

        Results are appended to memory in call order. Returns None when the
        turn was cancelled (every call still gets a result message).
        """
        results: list[dict[str, Any]] = []
        index = 0
        asked = False
        while index < len(tool_calls):
            if _turn_was_cancelled(cancellation_event):
                for remaining in tool_calls[index:]:
                    memory.append(_tool_message(remaining, _cancelled_tool_result(remaining)))
                return None
            if asked:
                skipped = {
                    "tool_name": tool_calls[index].name,
                    "tool_call_id": tool_calls[index].tool_call_id,
                    "status": "skipped",
                    "reason": "Not run: the turn ended with a question to the user.",
                }
                results.append(skipped)
                memory.append(_tool_message(tool_calls[index], skipped))
                index += 1
                continue
            segment = [index]
            if self._harness.parallel_tools and self._parallel_safe(tool_calls[index]):
                while (
                    segment[-1] + 1 < len(tool_calls)
                    and len(segment) < 8
                    and self._parallel_safe(tool_calls[segment[-1] + 1])
                ):
                    segment.append(segment[-1] + 1)
            if len(segment) > 1:

                def run_unless_cancelled(call: ToolCall) -> dict[str, Any]:
                    if _turn_was_cancelled(cancellation_event):
                        return _cancelled_tool_result(call)
                    return self._run_tool_call(
                        call,
                        run_id=run_id,
                        request_id=request_id,
                        cancellation_event=cancellation_event,
                        max_chars=max_chars,
                    )

                with ThreadPoolExecutor(max_workers=min(4, len(segment)), thread_name_prefix="code4me2-tool") as pool:
                    futures = [pool.submit(run_unless_cancelled, tool_calls[position]) for position in segment]
                    segment_results = [future.result() for future in futures]
            else:
                segment_results = [
                    self._run_tool_call(
                        tool_calls[index],
                        run_id=run_id,
                        request_id=request_id,
                        cancellation_event=cancellation_event,
                        max_chars=max_chars,
                    )
                ]
            for position, result in zip(segment, segment_results):
                call = tool_calls[position]
                results.append(result)
                memory.append(_tool_message(call, result))
                self._note_step(state, call, result)
                if call.name == "ask_user" and result.get("status") == "ok":
                    asked = True
            index = segment[-1] + 1
        return results

    def _parallel_safe(self, tool_call: ToolCall) -> bool:
        if tool_call.argument_error:
            return False
        check = getattr(self._tool_registry, "is_parallel_safe", None)
        return bool(check(tool_call.name)) if callable(check) else False

    def _tool_class(self, name: str) -> str | None:
        """"edit" / "execute" for built-in or IDE (MCP) tools that change or run things."""
        if name in _WORKSPACE_MUTATIONS:
            return "edit"
        if name == "run_command":
            return "execute"
        access = getattr(self._tool_registry, "mcp_access", None)
        value = access(name) if callable(access) and name.startswith("mcp__") else None
        return value if value in ("edit", "execute") else None

    def _note_step(self, state: _TurnState, tool_call: ToolCall, result: dict[str, Any]) -> None:
        state.step += 1
        status = result.get("status")
        tool_class = self._tool_class(tool_call.name)
        if tool_class == "edit" and (status == "ok" or _wrote_revision(result)):
            state.last_change_step = state.step
            # A verification result describes the code as it was; any later
            # change voids it (the gate verifies again when it can).
            state.verification_note = None
            if tool_call.name.startswith("mcp__"):
                state.external_changes = True
        elif tool_class == "execute" and tool_call.name.startswith("mcp__") and status == "ok":
            state.last_ide_run_step = state.step
            state.commands_run.append(f"IDE: {tool_call.name.split('__', 2)[-1]}")
        elif tool_call.name == "run_command" and status in {"ok", "error", "timeout"}:
            argv = tool_call.arguments.get("argv")
            if isinstance(argv, str):
                argv = [argv]
            # Verify-on-stop: inspection (git status/diff, ls, cat, grep…) or file
            # shuffling after the last change is not verification; a command that
            # ran the code is, whatever its exit code (the model saw the output).
            if status == "ok" and isinstance(argv, list) and _is_verification_command(argv):
                state.last_command_step = state.step
            if isinstance(argv, list):
                state.commands_run.append(" ".join(str(item) for item in argv))

    def _update_loop_guard(
        self, state: _TurnState, tool_calls: list[ToolCall], results: list[dict[str, Any]]
    ) -> None:
        if not self._harness.loop_guard:
            return
        worst, worst_name = 0, ""
        for call, result in zip(tool_calls, results):
            if self._tool_class(call.name) == "edit" and (
                result.get("status") == "ok" or _wrote_revision(result)
            ):
                # The workspace changed: repeating a read or a test is progress.
                state.call_counts.clear()
                worst, worst_name = 0, ""
                continue
            key = call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)
            count = state.call_counts.get(key, 0) + 1
            state.call_counts[key] = count
            if count > worst:
                worst, worst_name = count, call.name
        if worst >= _LOOP_STOP_AT:
            state.forced_final = _LOOP_STOP_NOTICE.format(count=worst)
        elif worst >= _LOOP_NUDGE_AT:
            state.notes.append(_LOOP_NUDGE.format(name=worst_name, count=worst))

    # --------------------------------------------------------- outcomes

    def _emit_text(
        self, text: str, *, run_id: str, request_id: str, final: bool, iteration: int
    ) -> None:
        emit_event(
            self._event_sink,
            "assistant_text",
            AssistantTextEvent(
                run_id=run_id,
                request_id=request_id,
                message_id=request_id,
                text=text,
                final=final,
                iteration=iteration,
            ),
        )

    def _finish(
        self,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        run_id: str,
        request_id: str,
        text: str,
        stop_reason: str,
        iteration: int,
        emit: bool,
    ) -> AdapterResult:
        if emit:
            self._emit_text(text, run_id=run_id, request_id=request_id, final=True, iteration=iteration)
        memory.append(_with_reasoning({"role": "assistant", "content": text}, state))
        return AdapterResult(
            final_response=text,
            stop_reason=stop_reason,
            run_status="completed",
            thoughts=tuple(state.thoughts),
            response_emitted=True,
            usage=state.usage,
            side_call_failures=tuple(state.side_call_failures),
        )

    def _fail(
        self,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        run_id: str,
        request_id: str,
        failure_reason: str,
        stop_reason: str,
        text: str,
        failure: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> AdapterResult:
        extra_payload, metrics = failure if failure is not None else (None, None)
        self._record_loop_failure(
            run_id=run_id,
            request_id=request_id,
            failure_reason=failure_reason,
            extra_payload=extra_payload,
            metrics=metrics,
        )
        self._emit_text(text, run_id=run_id, request_id=request_id, final=True, iteration=state.model_calls)
        memory.append({"role": "assistant", "content": text})
        return AdapterResult(
            final_response=text,
            stop_reason=stop_reason,
            run_status="failed",
            thoughts=tuple(state.thoughts),
            response_emitted=True,
            usage=state.usage,
            side_call_failures=tuple(state.side_call_failures),
        )

    def _cancelled(self, memory: MemoryWindow, state: _TurnState) -> AdapterResult:
        snapshot = memory.snapshot()
        if not snapshot or snapshot[-1].get("role") != "assistant" or snapshot[-1].get("tool_calls"):
            memory.append(
                {
                    "role": "assistant",
                    "content": "[Cancelled by the user before the turn completed.]",
                }
            )
        result = _cancelled_result(state.thoughts)
        return AdapterResult(
            final_response=result.final_response,
            stop_reason=result.stop_reason,
            run_status=result.run_status,
            thoughts=result.thoughts,
            response_emitted=True,
            usage=state.usage,
            side_call_failures=tuple(state.side_call_failures),
        )

    # -------------------------------------------------------- tool calls

    def _run_tool_call(
        self,
        tool_call: ToolCall,
        *,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
        max_chars: int = _MAX_TOOL_RESULT_CHARS,
    ) -> dict[str, Any]:
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
        base = {"tool_name": tool_call.name, "tool_call_id": tool_call.tool_call_id}
        if tool_call.argument_error:
            self._tool_registry.report_invalid_arguments(
                tool_call, run_id=run_id, request_id=request_id
            )
            self._record_tool_failure(
                run_id=run_id,
                request_id=request_id,
                tool_call=tool_call,
                failure_reason="invalid_arguments",
                error_message=tool_call.argument_error,
            )
            result: dict[str, Any] = {
                **base,
                "status": "error",
                "error_code": "invalid_arguments",
                "error": tool_call.argument_error,
                "hint": "Re-issue the call with a JSON object containing the required arguments.",
            }
            if tool_call.raw_arguments:
                result["raw_arguments"] = tool_call.raw_arguments[:500]
            return result
        try:
            output = self._tool_registry.execute(
                tool_call,
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
            )
        except ToolRegistryError as exc:
            self._record_tool_denial(
                run_id=run_id,
                request_id=request_id,
                tool_call=tool_call,
                denial_reason=exc.failure_reason,
                error_message=str(exc),
            )
            if isinstance(exc, ToolRevisionRequested):
                return {**base, **exc.result}
            if exc.failure_reason == "approval_rejected":
                return {
                    **base,
                    "status": "rejected",
                    "reason": "user_rejected",
                    "message": (
                        "The user rejected this action. Do not retry this "
                        "requested change with another mutating tool; explain "
                        "that no change was made or ask what they prefer instead."
                    ),
                }
            return {
                **base,
                "status": "denied",
                "reason": exc.failure_reason,
                "error": str(exc),
                "hint": self._denial_hint(exc.failure_reason),
            }
        except ToolArgumentError as exc:
            self._record_tool_failure(
                run_id=run_id,
                request_id=request_id,
                tool_call=tool_call,
                failure_reason="invalid_arguments",
                error_message=str(exc),
            )
            result = {
                **base,
                "status": "error",
                "error_code": "invalid_arguments",
                "error": str(exc),
                "hint": "Fix the named argument and call the tool again.",
            }
            if exc.field:
                result["field"] = exc.field
            return result
        except PermissionError as exc:
            if getattr(exc, "errno", None) is not None and not isinstance(exc, ToolError):
                # An operating-system EACCES/EPERM, not a policy decision.
                self._record_tool_failure(
                    run_id=run_id,
                    request_id=request_id,
                    tool_call=tool_call,
                    failure_reason="permission_denied",
                    error_message=str(exc),
                )
                return {
                    **base,
                    "status": "error",
                    "error_code": "permission_denied",
                    "error": str(exc),
                    "hint": "The operating system refused access; check file permissions or choose another path.",
                }
            # Workspace-policy denials are recorded by the file/command tools
            # themselves; a second event here would double-count the study's
            # step tally.
            return {
                **base,
                "status": "denied",
                "reason": "workspace_policy",
                "error": str(exc),
                "hint": _hint_for(tool_call.name, exc),
            }
        except TimeoutError as exc:
            self._record_tool_failure(
                run_id=run_id,
                request_id=request_id,
                tool_call=tool_call,
                failure_reason="timeout",
                error_message=str(exc),
            )
            return {
                **base,
                "status": "timeout",
                "error": str(exc),
                "hint": "The tool did not finish in time; retry with a narrower request or a longer timeout.",
            }
        except Exception as exc:  # noqa: BLE001
            error_code = getattr(exc, "code", None) if isinstance(exc, ToolError) else None
            self._record_tool_failure(
                run_id=run_id,
                request_id=request_id,
                tool_call=tool_call,
                failure_reason=str(error_code or "tool_execution_error"),
                error_message=str(exc),
            )
            return {
                **base,
                "status": "error",
                "error_code": str(error_code or type(exc).__name__),
                "error": str(exc),
                "hint": _hint_for(tool_call.name, exc),
            }
        return _cap_tool_result(_finalize_tool_output(output, base), max_chars)

    def _denial_hint(self, failure_reason: str) -> str:
        if failure_reason == "tool_not_allowed":
            allowed = ", ".join(sorted(self._tool_registry.known_tool_names())) or "none"
            return f"This tool is not enabled for this session; use one of: {allowed}."
        if failure_reason == "approval_policy_denied":
            return (
                "This session is suggestion-only: describe the change as a unified diff in your "
                "reply instead of applying it."
            )
        if failure_reason.startswith("approval_"):
            return "The action was not approved; explain what you wanted to do and ask how to proceed."
        if failure_reason == "unsupported_tool":
            return "Use one of the tools listed in the system prompt."
        return "Adjust the request or explain the limitation to the user."

    # ----------------------------------------------------------- helpers

    def _provider(self) -> FakeOpenAICompatibleProvider | OpenAICompatibleProvider:
        if self._provider_instance is None:
            self._provider_instance = self._build_provider()
        return self._provider_instance

    def _build_provider(self) -> FakeOpenAICompatibleProvider | OpenAICompatibleProvider:
        if self._config.adapter.fake_provider.enabled:
            return FakeOpenAICompatibleProvider(self._config.adapter.fake_provider.script)
        provider_config = self._config.adapter.provider
        return OpenAICompatibleProvider(
            kind=provider_config.kind,
            base_url=provider_config.base_url,
            model=provider_config.model,
            api_key_env=provider_config.api_key_env,
            timeout_seconds=provider_config.timeout_seconds,
            auth_headers=provider_config.auth_headers,
            tool_definitions=self._tool_registry.definitions(),
            temperature=provider_config.temperature,
            session_id=self._config.session_id,
            managed_request=self._config.managed_request,
            max_output_tokens=getattr(provider_config, "max_output_tokens", None),
            request_deadline_seconds=getattr(provider_config, "request_deadline_seconds", None),
        )

    def _prompt_profile(self) -> str:
        harness = getattr(self._config, "harness", None)
        option = getattr(harness, "prompt_profile", "auto") if harness is not None else "auto"
        return prompting.resolve_prompt_profile(option, self._config.adapter.provider.model)

    def _project_instructions(self) -> prompting.ProjectInstructions | None:
        harness = getattr(self._config, "harness", None)
        if harness is not None and not harness.project_instructions:
            return None
        try:
            return prompting.load_project_instructions(self._config.workspace_root)
        except Exception:  # noqa: BLE001 - instructions are optional context
            logger.debug("Could not read project instruction files.", exc_info=True)
            return None

    def _reminder(
        self, calls_left: int, instructions: prompting.ProjectInstructions | None
    ) -> str:
        names = self._tool_registry.known_tool_names()
        return prompting.reminder_text(
            approval_policy=self._config.approval_policy,
            can_edit=bool(names & _WORKSPACE_MUTATIONS),
            can_run="run_command" in names and self._can_run_commands(),
            calls_left=max(1, calls_left),
            has_instructions=instructions is not None,
        )

    def _system_context(
        self,
        *,
        tool_names: Sequence[str] | None = None,
        instructions: prompting.ProjectInstructions | None = None,
        prompt_profile: str | None = None,
    ) -> str:
        config = self._config
        if tool_names is None:
            registry = getattr(self, "_tool_registry", None)
            if registry is not None:
                tool_names = sorted(registry.known_tool_names())
            elif config.allowed_tools is not None:
                tool_names = sorted(config.allowed_tools)
            elif getattr(config, "tools", None) is not None:
                tool_names = sorted(config.tools or [])
            else:
                tool_names = tool_catalog.tool_names()
        tool_names = list(tool_names)
        names = set(tool_names)
        if prompt_profile is None:
            harness = getattr(config, "harness", None)
            prompt_profile = prompting.resolve_prompt_profile(
                getattr(harness, "prompt_profile", "auto"), config.adapter.provider.model
            )
        workspace_root = config.workspace_root.as_posix()
        os_name = platform.system()
        today = date.today().isoformat()
        blocked = list(config.commands.blocked_commands)
        policy = config.approval_policy
        budget = max(1, int(config.adapter.max_iterations))

        if "run_command" in tool_names:
            shell = _usable_shell(blocked)
            shell_text = (
                f"no implicit shell (for pipes, redirects or cd run e.g. "
                f'["{shell}", "-c", "..."])'
                if shell
                else "no shell (no pipes, redirects or cd)"
            )
            commands_line = (
                "run_command runs a program installed on this machine, or a project script by path, "
                f"with an argv list and {shell_text}."
            )
            if blocked:
                commands_line += (
                    f" Blocked in this study: {', '.join(blocked)}. They are refused, also inside a "
                    "shell command; do not try to run them another way."
                )
        else:
            commands_line = "Commands cannot be run in this session."
        if policy == "per_step":
            policy_line = (
                "Approval policy: each file edit or command needs the user's approval in the IDE. "
                "If the user rejects one, do not retry it another way; explain and ask how to proceed."
            )
        elif policy == "suggestion_only":
            policy_line = (
                "Approval policy: suggestion-only. You can read and search, but you cannot modify "
                "files or run commands. Propose every change as a fenced unified diff with the "
                "workspace-relative file path plus a one-line rationale, and list the commands the "
                "user should run to verify."
            )
        else:
            policy_line = (
                "Approval policy: tools run immediately; file edits are applied as soon as you call them."
            )
        plan_line = prompting.plan_guidance(prompt_profile) if "update_plan" in names else ""
        tool_list = ", ".join(tool_names) if tool_names else "none"
        # A researcher-authored profile prompt replaces the persona paragraph
        # (ported from origin/sys_prompt). The operational instructions that
        # follow (tools, approval policy, budget, how to work) are kept: the
        # runtime cannot call tools correctly without them.
        researcher_prompt = (config.adapter.system_prompt or "").strip()
        if researcher_prompt:
            persona = [
                researcher_prompt,
                "",
                "You are working inside the user's JetBrains IDE on the project at "
                f"{workspace_root} (host OS: {os_name}; today: {today}). You act by calling tools; "
                "the user sees your text and a card for every tool call.",
            ]
        else:
            persona = [
                "You are Code4Me, a coding agent working inside the user's JetBrains IDE on the project at "
                f"{workspace_root} (host OS: {os_name}; today: {today}). You act by calling tools; the user "
                "sees your text and a card for every tool call.",
            ]
        lines = [
            *persona,
            "",
            f"Tools available in this session: {tool_list}.",
            commands_line,
            policy_line,
        ]
        if plan_line:
            lines.append(plan_line)
        lines += [
            f"Budget: at most {budget} model call{'s' if budget != 1 else ''} per user message. Each call "
            "may issue several tool calls, so batch independent reads and searches together. On the "
            "last call, tools are disabled and you must summarize.",
            "",
            "How to work",
            "1. Explore first. Locate code with glob_files, grep_files or list_files, then read_file the "
            "relevant parts. Never guess paths or file contents; if something does not exist, say so.",
            prompting.edit_guidance(prompt_profile, names),
            "3. Verify. When run_command is available, run the relevant tests, build or linter after "
            "editing and fix what you broke. If you cannot verify, say what the user should run.",
            "4. Keep going until the task is done or you are truly blocked. Do not ask for confirmation "
            "of routine steps; ask only when an ambiguity would change the outcome"
            + (", and then use ask_user." if "ask_user" in names else "."),
            '5. Every tool result is JSON with a "status". On "error" or "denied", read the message and '
            'hint, adjust, and try a different approach; never repeat an identical failing call. On '
            '"rejected", drop that change and ask what the user prefers.',
            "6. Paths are workspace-relative (for example src/app.py). Do not invent absolute or "
            "container paths.",
        ]
        notes = prompting.profile_notes(prompt_profile)
        if notes:
            lines += [f"{7 + index}. {note}" for index, note in enumerate(notes)]
        lines += [
            "",
            "Answering",
            "- For greetings or general questions, answer directly without tools.",
            "- A short sentence before a batch of tool calls is shown to the user as progress.",
            "- Your final message states what changed (files), what was verified (commands and results) "
            "and what remains or needs the user's decision. Do not paste code that tools already applied.",
        ]
        if instructions is not None:
            lines += ["", prompting.instructions_block(instructions)]
        return "\n".join(lines)

    # --------------------------------------------------------- slash commands

    def _run_slash_command(
        self,
        command: slash_commands.ParsedCommand,
        *,
        memory: MemoryWindow,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
    ) -> AdapterResult:
        state = _TurnState()
        harness = self._harness
        # Study sessions only parse /status and /undo (slash_commands.available),
        # identically in every arm. Outside studies a command whose behaviour
        # the configuration switched off answers neutrally.
        if command.name == "status":
            text = self._status_text(memory)
        elif command.name == "undo":
            text = self._undo_last_turn(memory)
        elif command.name == "compact" and not harness.context_summarization:
            text = "Compacting the conversation is not available in this session."
        elif command.name == "review" and not harness.self_review:
            text = "Reviews are not available in this session."
        elif command.name == "compact":
            text = self._compact_now(memory, state, run_id=run_id, request_id=request_id, cancellation_event=cancellation_event)
        elif command.name == "review":
            text = self._review_session(
                memory,
                state,
                focus=command.argument,
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
            )
        else:  # pragma: no cover - parse() only returns known names
            text = f"Unknown command /{command.name}."
        if text is None:
            return self._cancelled(memory, state)
        self._emit_text(text, run_id=run_id, request_id=request_id, final=True, iteration=max(1, state.model_calls))
        return AdapterResult(
            final_response=text,
            stop_reason="end_turn",
            run_status="completed",
            response_emitted=True,
            usage=state.usage,
            side_call_failures=tuple(state.side_call_failures),
        )

    def _status_text(self, memory: MemoryWindow) -> str:
        config = self._config
        harness = self._harness
        definitions = self._tool_registry.definitions()
        reserve = _estimate_text_tokens(json.dumps(definitions)) + _OUTPUT_HEADROOM_TOKENS
        used = sum(_estimate_tokens(message) for message in memory.window(reserve_tokens=reserve)) + reserve
        if config.managed_mode:
            # Study participants stay blind to their arm: no model, prompt
            # profile, budgets, tools or switches.
            percent = min(100, round(100 * used / max(1, memory.max_tokens)))
            return "\n".join(
                [
                    "Session status",
                    f"- Context: about {percent}% of the conversation window in use",
                    f"- Undo checkpoints: {self._session_state.checkpoint_count()}",
                ]
            )
        switches = [
            name
            for name in (
                "self_review",
                "verify_on_stop",
                "context_summarization",
                "parallel_tools",
                "project_instructions",
                "read_before_edit",
                "syntax_check",
                "loop_guard",
                "instruction_reminders",
                "test_output_summary",
            )
            if getattr(harness, name)
        ]
        instructions = self._project_instructions()
        lines = [
            "Session status",
            f"- Model: {config.adapter.provider.model or 'not configured'} (prompt profile: {self._prompt_profile()})",
            f"- Step budget: {config.adapter.max_iterations} model calls per message; approval policy: {config.approval_policy}",
            f"- Context: about {used:,} of {memory.max_tokens:,} tokens in use",
            f"- Tools: {', '.join(sorted(self._tool_registry.known_tool_names())) or 'none'}",
            f"- Commands: {self._commands_status()} "
            f"(default timeout {int(config.commands.timeout_seconds)} s)",
            f"- Verification: {_verification_label(harness, self._verify_command(), can_run=self._can_run_commands())}",
            f"- Enabled behaviours: {', '.join(switches) or 'none'}",
            f"- Project instructions: {', '.join(name for name, _chars in instructions.files) if instructions else 'none'}",
            f"- Undo checkpoints: {self._session_state.checkpoint_count()}",
        ]
        return "\n".join(lines)

    def _undo_last_turn(self, memory: MemoryWindow) -> str:
        outcome = self._session_state.undo_last_turn(self._tool_registry.file_tools)
        if outcome.nothing_to_undo:
            return "There are no file changes from this session to undo."
        lines = ["Undid the file changes of the last turn that changed files."]
        if outcome.restored:
            lines.append(f"- Restored: {', '.join(outcome.restored)}")
        if outcome.deleted:
            lines.append(f"- Removed (created by the agent): {', '.join(outcome.deleted)}")
        for path, reason in outcome.skipped:
            lines.append(f"- Not changed: {path} ({reason})")
        text = "\n".join(lines)
        # Kept as the user's command and its result, so the model knows the
        # files were reverted and a reopened chat replays both.
        memory.append({"role": "user", "content": "/undo"})
        memory.append({"role": "assistant", "content": text})
        return text

    def _compact_now(
        self,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
    ) -> str | None:
        definitions = self._tool_registry.definitions()
        reserve = _estimate_text_tokens(json.dumps(definitions)) + _OUTPUT_HEADROOM_TOKENS
        before = memory.estimated_tokens()
        outcome = self._maybe_compact(
            self._provider(),
            memory,
            state,
            reserve_tokens=reserve,
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
            keep_recent_units=2,
            force=True,
        )
        if outcome == "cancelled":
            return None
        if outcome == "failed":
            return "Could not summarise the conversation (the model call failed); nothing was changed."
        if outcome != "compacted":
            return "Nothing to compact yet: the conversation is still short."
        return (
            f"Summarised the earlier conversation into a checkpoint "
            f"(about {before:,} → {memory.estimated_tokens():,} tokens of history)."
        )

    def _review_session(
        self,
        memory: MemoryWindow,
        state: _TurnState,
        *,
        focus: str,
        run_id: str,
        request_id: str,
        cancellation_event: Event | None,
    ) -> str | None:
        diff = self._session_state.session_diff(max_chars=_REVIEW_DIFF_CHARS)
        if not diff.strip():
            return "There are no changes from this session to review."
        request = "Review all changes made in this session."
        if focus:
            request += f" Focus: {focus}"
        try:
            issues = self._self_review(
                self._provider(),
                state,
                prompt=request,
                answer="(review requested by the user)",
                diff=diff,
                run_id=run_id,
                request_id=request_id,
                cancellation_event=cancellation_event,
                context_budget=memory.max_tokens,
            )
        except ProviderCancelled:
            return None
        text = (
            f"Review of this session's changes found possible issues:\n{issues}"
            if issues
            else "Review of this session's changes found no important issues."
        )
        memory.append({"role": "user", "content": "/review" + (f" {focus}" if focus else "")})
        memory.append({"role": "assistant", "content": text})
        return text

    def _parse_output(
        self,
        turn: ProviderTurn,
        *,
        run_id: str,
        request_id: str,
    ) -> ParsedProviderOutput | None:
        raw_output = turn.output.get("output", turn.output) if isinstance(turn.output, dict) else None
        if not isinstance(raw_output, dict):
            self._record_parse_failure(
                run_id=run_id,
                request_id=request_id,
                output={"output": repr(turn.output)[:500]},
                failure_reason="missing_structured_output",
            )
            return None
        tool_calls_data = raw_output.get("tool_calls")
        text = raw_output.get("final_answer", raw_output.get("text"))
        if not isinstance(tool_calls_data, list) and not isinstance(text, str):
            self._record_parse_failure(
                run_id=run_id,
                request_id=request_id,
                output=raw_output,
                failure_reason="missing_structured_output",
            )
            return None
        return ParsedProviderOutput(
            text=_normalize_optional_text(text),
            tool_calls=_normalize_tool_calls(tool_calls_data),
            thought=_normalize_optional_text(raw_output.get("reasoning")),
            finish_reason=turn.finish_reason,
            reasoning_fields=_reasoning_fields(raw_output.get("reasoning_fields")),
        )

    # --------------------------------------------------------- telemetry

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
        extra_payload: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
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
                **(extra_payload or {}),
            },
            metrics=metrics,
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

    def _record_tool_denial(
        self,
        *,
        run_id: str,
        request_id: str,
        tool_call: ToolCall,
        denial_reason: str,
        error_message: str,
    ) -> None:
        self._telemetry.record(
            event_type="agent.tool.denied",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "tool_name": tool_call.name,
                "tool_call_id": tool_call.tool_call_id,
                "status": "denied",
                "denial_reason": denial_reason,
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
        purpose: str = "turn",
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
                "usage_estimated": provider_turn.usage_estimated,
                "call_purpose": purpose,
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
    session_state: SessionToolState | None = None,
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
                telemetry=telemetry,
                workspace_root=config.workspace_root,
                session_state=session_state,
                harness=getattr(config, "harness", None),
            ),
            event_sink=event_sink,
            session_state=session_state,
        )
    return DeterministicEchoAdapter()


# ------------------------------------------------------- loop helpers


def _assistant_tool_call_message(
    tool_calls: list[ToolCall], *, text: str | None, runtime: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": tool_call.tool_call_id,
                "name": tool_call.name,
                "arguments": dict(tool_call.arguments),
            }
            for tool_call in tool_calls
        ],
    }
    if runtime is not None:
        # Issued by the runtime itself (verify-on-stop), not chosen by the model.
        message["code4me_runtime"] = runtime
    return message


def _verification_label(harness: HarnessOptions, command: list[str] | None, *, can_run: bool) -> str:
    """``command``: the verify argv this session will actually run (``_verify_command``)."""
    if not harness.verify_on_stop:
        return "off"
    if not can_run:
        return "off: commands cannot run in this session"
    if command:
        return f"the runtime runs {' '.join(command)} after changes"
    if harness.verify_command:
        return (
            "the agent is asked to verify after changes "
            f"({' '.join(harness.verify_command)} cannot run in this session)"
        )
    return "the agent is asked to verify after changes"


def _asked_question(tool_calls: list[ToolCall], results: list[dict[str, Any]]) -> str | None:
    """The ask_user question of this batch, formatted as the turn's final message."""
    for call, result in zip(tool_calls, results):
        if call.name != "ask_user" or result.get("status") != "ok":
            continue
        question = str(result.get("question") or "").strip()
        options = [str(option) for option in result.get("options") or [] if str(option).strip()]
        if not question:
            continue
        if options:
            listed = "\n".join(f"{number}. {option}" for number, option in enumerate(options, start=1))
            return f"{question}\n\n{listed}"
        return question
    return None


def _review_issues(text: str | None) -> str | None:
    """The reviewer's issue bullets, or None.

    The reviewer is asked for NO_ISSUES or '- ' bullets; a reply without
    bullets (NO_ISSUES, or prose that ignored the format) is not actionable
    and must not cost the agent a fix iteration.
    """
    if not text:
        return None
    bullets = [line.strip() for line in text.splitlines() if re.match(r"^\s*[-*•]\s+\S", line)]
    if not bullets:
        return None
    return "\n".join(bullets[:5])[:2000]


def _render_units_for_summary(units: list[list[dict[str, Any]]], *, max_chars: int) -> str:
    """A compact transcript of the units to summarise, newest content kept when capped."""
    lines: list[str] = []
    for unit in units:
        for message in unit:
            role = message.get("role")
            content = str(message.get("content") or "")
            if role == "tool":
                lines.append(f"TOOL RESULT ({message.get('name', 'tool')}): {content[:1500]}")
            elif role == "assistant" and message.get("tool_calls"):
                if content.strip():
                    lines.append(f"ASSISTANT: {content[:2000]}")
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict):
                        arguments = json.dumps(call.get("arguments", {}), sort_keys=True, default=str)
                        lines.append(f"TOOL CALL {call.get('name')}: {arguments[:600]}")
            elif role == "user" and message.get("code4me_runtime") == "checkpoint":
                lines.append(f"EARLIER CHECKPOINT:\n{content[:6000]}")
            elif role == "user":
                lines.append(f"USER: {content[:4000]}")
            else:
                lines.append(f"ASSISTANT: {content[:3000]}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "[earliest part omitted]\n" + text[-max_chars:]
    return text


def _tool_message(tool_call: ToolCall, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call.tool_call_id,
        "name": tool_call.name,
        "content": json.dumps(result, sort_keys=True, default=str),
    }


def _not_run_result(tool_call: ToolCall, reason: str) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": reason,
        "tool_name": tool_call.name,
        "tool_call_id": tool_call.tool_call_id,
    }


def _cancelled_tool_result(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "status": "cancelled",
        "error": "Cancelled by user before execution",
        "tool_name": tool_call.name,
        "tool_call_id": tool_call.tool_call_id,
    }


def _finalize_tool_output(output: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    result = dict(output)
    tool_status = str(result.get("status", "")).lower()
    if result.get("timed_out") or tool_status == "timeout":
        status = "timeout"
    elif tool_status == "cancelled":
        status = "cancelled"
    elif tool_status in {"failed", "error"}:
        status = "error"
    else:
        status = "ok"
    if tool_status and tool_status not in {"ok", status}:
        result["tool_status"] = result["status"]
    result.update(base)
    result["status"] = status
    return result


_UNCAPPED_RESULT_KEYS = frozenset(
    {"tool_call_id", "tool_name", "status", "error_code", "reason", "path", "tool_status"}
)
_NUMBERED_LINE_RE = re.compile(r"^(\d+)\|")


def _cap_tool_result(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    def size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, sort_keys=True, default=str))

    if size(result) <= max_chars:
        return result
    capped = dict(result)
    for _ in range(8):
        overflow = size(capped) - max_chars
        if overflow <= 0:
            break
        key = max(
            (k for k, v in capped.items() if isinstance(v, str) and k not in _UNCAPPED_RESULT_KEYS),
            key=lambda k: len(capped[k]),
            default=None,
        )
        if key is None or len(capped[key]) < 200:
            list_key = max(
                (k for k, v in capped.items() if isinstance(v, list)),
                key=lambda k: len(json.dumps(capped[k], default=str)),
                default=None,
            )
            if list_key is None or not capped[list_key]:
                break
            items = capped[list_key]
            capped[list_key] = items[: max(1, len(items) // 2)]
            capped["truncated"] = True
            continue
        keep = max(100, len(capped[key]) - overflow - 64)
        if key == "content" and "end_line" in capped:
            _cap_numbered_content(capped, keep)
        else:
            omitted = len(capped[key]) - keep
            capped[key] = capped[key][:keep] + f"…[truncated {omitted} chars]"
        capped["truncated"] = True
    return capped


def _cap_numbered_content(capped: dict[str, Any], keep: int) -> None:
    """Cut read_file output at a line boundary and keep its paging fields honest."""
    content = str(capped["content"])
    head = content[:keep]
    lines = head.split("\n")
    complete = lines[:-1] if len(lines) > 1 else []
    last_line = None
    for line in reversed(complete):
        match = _NUMBERED_LINE_RE.match(line)
        if match:
            last_line = int(match.group(1))
            break
    if last_line is None:
        omitted = len(content) - keep
        capped["content"] = head + f"…[truncated {omitted} chars]"
        return
    kept_lines = [line for line in complete if not line.startswith("[truncated:")]
    next_offset = last_line + 1
    total = capped.get("total_lines")
    capped["content"] = "\n".join(kept_lines) + (
        f"\n[truncated to fit the context budget: showing lines {capped.get('start_line', 1)}-"
        f"{last_line}{f' of {total}' if isinstance(total, int) else ''}; call read_file with "
        f"offset={next_offset} to continue]"
    )
    capped["end_line"] = last_line
    capped["next_offset"] = next_offset
    capped["truncation_reason"] = "context_budget"


def _batch_result_cap(max_tokens: int, batch_size: int) -> int:
    """Characters allowed per tool result so a whole batch fits half the context budget."""
    per_result = (max(1, int(max_tokens)) * 4) // (2 * max(1, batch_size))
    return int(min(_MAX_TOOL_RESULT_CHARS, max(4_000, per_result)))


def _budget_exhausted_text(tool_calls: list[ToolCall], results: list[dict[str, Any]]) -> str:
    lines = [
        "I used the step budget for this message, so I could not review these results; here is "
        "what ran on the last step:"
    ]
    for tool_call, result in zip(tool_calls, results):
        summary = _tool_result_summary(tool_call.name, result) or result.get("status", "done")
        lines.append(f"- {tool_call.name}: {result.get('status', 'ok')} — {summary}")
    lines.append("Send another message to continue.")
    return "\n".join(lines)


def _stop_reason_from_finish(finish_reason: str | None) -> str:
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return "end_turn"


def _model_failure_details(
    exc: BaseException, *, started_at: float, purpose: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(payload, metrics) describing a failed model call.

    Numbers go into metrics, which the server keeps as structural data even
    without content consent; ``error_message`` is a content key.
    """
    payload: dict[str, Any] = {
        "call_purpose": purpose,
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:500],
    }
    metrics: dict[str, Any] = {"duration_ms": round((perf_counter() - started_at) * 1000, 3)}
    for key in ("status_code", "attempts"):
        value = getattr(exc, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            metrics[key] = value
    return payload, metrics


def _provider_error_text(exc: ProviderRequestFailed) -> str:
    if exc.status_code:
        return (
            f"I couldn't reach the model service (HTTP {exc.status_code} after {exc.attempts} "
            f"attempt{'s' if exc.attempts != 1 else ''}). Nothing further was changed; please send "
            "your message again."
        )
    return (
        f"I couldn't reach the model service ({exc}; {exc.attempts} attempt"
        f"{'s' if exc.attempts != 1 else ''}). Nothing further was changed; please send your "
        "message again."
    )


def _hint_for(tool_name: str, exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(exc, (ToolFileNotFoundError, FileNotFoundError)) or code == "file_not_found":
        return "Check the path with glob_files or list_files; paths are workspace-relative."
    if code in {"edit_no_match", "edit_ambiguous"} or (
        isinstance(exc, ValueError) and tool_name in {"replace_text", "edit_file"}
    ):
        return "old_text must match the file exactly (including whitespace); read_file the region and retry with the exact text."
    if isinstance(exc, FileExistsError) or code == "file_exists":
        return "Use write_file or edit_file for an existing file, or choose a different path."
    if isinstance(exc, PermissionError) or code == "outside_workspace":
        return "Stay inside the workspace, and do not run a program the study blocks."
    if code == "not_text_file":
        if tool_name == "write_file":
            return (
                "The existing file is not UTF-8 text, so it is not overwritten blindly; delete it "
                "with delete_file first if you really want to replace it."
            )
        return "This tool only works with UTF-8 text files."
    if code == "command_not_found":
        return "Use a program that is installed on this machine, or the path of a script in the project."
    if isinstance(exc, (KeyError, TypeError)):
        return f"Required argument missing or of the wrong type: {exc}."
    return "Adjust the arguments or try a different approach."


def _tool_call_id(raw_id: Any) -> str:
    """The provider's call id, or a fresh one when it sends none (or null/blank).

    A step counter would repeat every step, so telemetry and traces would merge
    the calls of different steps into one.
    """
    text = str(raw_id).strip() if raw_id is not None else ""
    return text or f"call-{uuid.uuid4().hex}"


def _normalize_tool_calls(value: Any) -> list[ToolCall]:
    if not isinstance(value, list):
        return []
    normalized: list[ToolCall] = []
    for raw_tool_call in value:
        if not isinstance(raw_tool_call, dict):
            continue
        arguments = raw_tool_call.get("arguments", {})
        if arguments is None:
            arguments = {}
        argument_error = raw_tool_call.get("argument_error")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                argument_error = argument_error or f"Tool arguments were not valid JSON: {exc}"
                arguments = {}
        if not isinstance(arguments, dict):
            if argument_error is None:
                argument_error = "Tool arguments must be a JSON object."
            arguments = {}
        name = str(raw_tool_call.get("name", "")).strip()
        if not name:
            continue
        normalized.append(
            ToolCall(
                tool_call_id=_tool_call_id(raw_tool_call.get("id")),
                name=name,
                arguments=dict(arguments),
                argument_error=str(argument_error) if argument_error else None,
                raw_arguments=_normalize_optional_text(raw_tool_call.get("raw_arguments")),
            )
        )
    return normalized


def _normalize_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


# ------------------------------------------------------------ tool cards


def _absolute_path(workspace_root: Path | None, path: str | None) -> str | None:
    if not path:
        return None
    if workspace_root is None:
        return path
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return os.path.normpath(str(candidate))
    return os.path.normpath(os.path.join(str(workspace_root), path))


def _filename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def _safe_tool_event_metadata(
    tool_name: str,
    values: dict[str, Any],
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any] | None:
    """Card metadata that never raises, even for arguments that failed validation."""
    try:
        return _tool_event_metadata(tool_name, values, workspace_root=workspace_root)
    except Exception:  # noqa: BLE001
        if tool_name in tool_catalog.NO_CARD_TOOLS:
            return None
        return {
            "kind": tool_kind(tool_name),
            "title": f"Run {tool_name}",
            "content_text": f"Run {tool_name}",
            "raw_input": None,
        }


def _tool_event_metadata(
    tool_name: str,
    values: dict[str, Any],
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any] | None:
    kind = tool_kind(tool_name)
    if tool_name in tool_catalog.NO_CARD_TOOLS:
        return None
    if tool_name == "apply_patch":
        actions = values.get("_patch") or []
        targets = [
            str(getattr(action, "move_to", None) or getattr(action, "path", ""))
            for action in actions
        ]
        targets = [target for target in targets if target]
        absolute = tuple(
            item for item in (_absolute_path(workspace_root, target) for target in targets) if item
        )
        if not targets:
            title = "Apply patch"
        elif len(targets) == 1:
            title = f"Apply patch to {targets[0]}"
        else:
            title = f"Apply patch to {targets[0]} and {len(targets) - 1} more file{'s' if len(targets) > 2 else ''}"
        return {
            "kind": kind,
            "title": title,
            "path": absolute[0] if absolute else None,
            "locations": absolute or None,
            "content_text": title,
            "raw_input": {"files": targets, "file_count": len(targets)},
        }
    if tool_name == "read_file":
        path = str(values.get("path", "")).strip()
        title = f"Read {path}" if path else "Read file"
        offset = _safe_int(values.get("offset")) or _safe_int(values.get("line_start"))
        limit = _safe_int(values.get("limit"))
        if offset and limit:
            title += f" (lines {offset}-{offset + limit - 1})"
        elif offset:
            title += f" (from line {offset})"
        raw_input: dict[str, Any] = {"path": path} if path else {}
        if offset:
            raw_input["offset"] = offset
        if limit:
            raw_input["limit"] = limit
        return {
            "kind": kind,
            "title": title,
            "path": _absolute_path(workspace_root, path),
            "content_text": f"Read file {_filename(path)}" if path else "Read file",
            "raw_input": raw_input or None,
        }
    if tool_name in {"create_file", "write_file", "replace_text", "edit_file", "delete_file"}:
        path = str(values.get("path", "")).strip()
        prefixes = {
            "create_file": ("Create", "Created file"),
            "write_file": ("Update", "Updated file"),
            "replace_text": ("Replace text in", "Replaced text in"),
            "edit_file": ("Edit", "Edited"),
            "delete_file": ("Delete", "Deleted"),
        }
        title_prefix, content_prefix = prefixes[tool_name]
        title = f"{title_prefix} {path}" if path else title_prefix
        raw_input = {"path": path} if path else {}
        if tool_name == "edit_file":
            edit_count = _safe_len(values.get("edits"))
            title += f" ({edit_count} edit{'s' if edit_count != 1 else ''})"
            raw_input["edit_count"] = edit_count
        if tool_name == "replace_text" and values.get("replace_all"):
            raw_input["replace_all"] = True
        return {
            "kind": kind,
            "title": title,
            "path": _absolute_path(workspace_root, path),
            "content_text": f"{content_prefix} {_filename(path)}" if path else content_prefix,
            "raw_input": raw_input or None,
        }
    if tool_name == "move_file":
        source = str(values.get("source_path", "")).strip()
        destination = str(values.get("destination_path", "")).strip()
        source_abs = _absolute_path(workspace_root, source)
        destination_abs = _absolute_path(workspace_root, destination)
        locations = tuple(item for item in (source_abs, destination_abs) if item)
        return {
            "kind": kind,
            "title": f"Move {source} → {destination}" if source and destination else "Move file",
            "path": source_abs,
            "locations": locations or None,
            "content_text": f"Moved {source} to {destination}",
            "raw_input": {
                "source_path": source,
                "destination_path": destination,
                "overwrite": bool(values.get("overwrite", False)),
            },
        }
    if tool_name == "list_files":
        path = str(values.get("path", ".") or ".").strip()
        return {
            "kind": kind,
            "title": f"List {path}",
            "path": _absolute_path(workspace_root, path),
            "content_text": f"Listed {path}",
            "raw_input": {"path": path, "recursive": bool(values.get("recursive", False))},
        }
    if tool_name == "glob_files":
        pattern = str(values.get("pattern", "")).strip()
        path = str(values.get("path", ".") or ".").strip()
        title = f"Find files matching {pattern}" if pattern else "Find files"
        if path and path != ".":
            title += f" in {path}"
        return {
            "kind": kind,
            "title": title,
            "path": _absolute_path(workspace_root, path),
            "content_text": title,
            "raw_input": {"pattern": pattern, "path": path},
        }
    if tool_name in {"grep_files", "search_files"}:
        pattern = str(values.get("pattern", values.get("query", ""))).strip()
        path = str(values.get("path", ".") or ".").strip()
        title = f'Search "{pattern}"' if pattern else "Search files"
        if path and path != ".":
            title += f" in {path}"
        raw_input = {"pattern": pattern, "path": path}
        if values.get("glob"):
            raw_input["glob"] = values["glob"]
        if values.get("output_mode"):
            raw_input["output_mode"] = values["output_mode"]
        return {
            "kind": kind,
            "title": title,
            "path": _absolute_path(workspace_root, path),
            "content_text": title,
            "raw_input": raw_input,
        }
    if tool_name == "run_command":
        argv = values.get("argv")
        command = " ".join(str(arg) for arg in argv) if isinstance(argv, (list, tuple)) else "command"
        raw_input: dict[str, Any] = {"argv": argv, "cwd": values.get("cwd", ".")}
        if values.get("timeout_seconds") is not None:
            raw_input["timeout_seconds"] = values["timeout_seconds"]
        return {
            "kind": kind,
            "title": f"Run {command}",
            "path": None,
            "content_text": f"Run command: {command}",
            "raw_input": raw_input,
        }
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            title = f"Call {parts[1]}: {parts[2]}"
        else:
            title = f"Call MCP tool {tool_name.removeprefix('mcp__')}"
        return {
            "kind": "other",
            "title": title,
            "content_text": title,
            "raw_input": values,
        }
    return {
        "kind": kind,
        "title": f"Run tool {tool_name}",
        "content_text": f"Run tool {tool_name}",
        "raw_input": values,
    }


def _tool_result_summary(tool_name: str, output: dict[str, Any]) -> str | None:
    try:
        if tool_name == "read_file":
            if "total_lines" in output:
                start = int(output.get("start_line") or 0)
                end = int(output.get("end_line") or 0)
                count = max(0, end - start + 1) if end >= start and start > 0 else 0
                text = f"Read {count} line{'s' if count != 1 else ''} of {_filename(str(output.get('path', '')))}"
                if output.get("truncated"):
                    text += f" (of {output['total_lines']}; truncated)"
                return text
            return None
        if tool_name == "list_files":
            entries = output.get("entries", output.get("paths"))
            if isinstance(entries, list):
                text = f"Listed {len(entries)} entr{'ies' if len(entries) != 1 else 'y'}"
                return text + (" (truncated)" if output.get("truncated") else "")
            return None
        if tool_name == "glob_files":
            files = output.get("files")
            if isinstance(files, list):
                text = f"Found {len(files)} file{'s' if len(files) != 1 else ''}"
                return text + (" (truncated)" if output.get("truncated") else "")
            return None
        if tool_name in {"grep_files", "search_files"}:
            if "match_count" in output:
                matches = int(output.get("match_count") or 0)
                files = int(output.get("file_count") or 0)
                text = f"Found {matches} match{'es' if matches != 1 else ''} in {files} file{'s' if files != 1 else ''}"
                return text + (" (truncated)" if output.get("truncated") else "")
            matches = output.get("matches")
            if isinstance(matches, list):
                return f"Found {len(matches)} match{'es' if len(matches) != 1 else ''}"
            return None
        if tool_name == "run_command":
            if output.get("timed_out"):
                text = f"Timed out after {output.get('timeout_seconds', '?')} s"
            elif output.get("status") == "cancelled":
                text = "Cancelled"
            elif "exit_code" in output:
                duration = output.get("duration_ms")
                text = f"Exit code {output.get('exit_code')}"
                if isinstance(duration, (int, float)):
                    text += f" in {duration / 1000:.1f} s"
            else:
                return None
            test_summary = output.get("test_summary")
            if isinstance(test_summary, dict):
                headline = _test_headline(test_summary)
                if headline:
                    text = f"{text}\n{headline}"
            tail = _output_tail(output)
            return f"{text}\n{tail}" if tail else text
        if tool_name == "apply_patch":
            files = output.get("files")
            if isinstance(files, list):
                return "; ".join(
                    f"{item.get('action')} {item.get('move_to') or item.get('path')}"
                    for item in files
                    if isinstance(item, dict)
                )
            return None
        if tool_name == "replace_text":
            if "replacements" in output:
                count = int(output["replacements"])
                return f"Replaced {count} occurrence{'s' if count != 1 else ''}"
            return None
        if tool_name == "edit_file":
            if "edits_applied" in output:
                count = int(output["edits_applied"])
                return f"Applied {count} edit{'s' if count != 1 else ''}"
            return None
        if tool_name == "delete_file":
            return f"Deleted {output.get('path', 'file')}"
        if tool_name == "move_file":
            return f"Moved {output.get('source_path', '')} to {output.get('destination_path', '')}"
        if tool_name.startswith("mcp__"):
            status = output.get("status")
            return f"MCP tool {status}" if status else None
    except (TypeError, ValueError):
        return None
    return None


def _test_headline(test_summary: dict[str, Any]) -> str | None:
    counts = [
        f"{test_summary[key]} {key}"
        for key in ("failed", "errors", "passed", "skipped")
        if isinstance(test_summary.get(key), int) and test_summary[key]
    ]
    framework = test_summary.get("framework") or "tests"
    text = f"{framework}: {', '.join(counts)}" if counts else None
    failures = test_summary.get("failures")
    if isinstance(failures, list) and failures:
        names = "; ".join(str(item.get("name")) for item in failures[:5] if isinstance(item, dict))
        text = f"{text or framework}\nFailing: {names}"
    return text


def _result_notes(tool_name: str, output: dict[str, Any]) -> list[str]:
    """Warnings the model and the user should notice next to a completed edit."""
    notes: list[str] = []
    strategies = output.get("match_strategies")
    if isinstance(strategies, list) and strategies:
        fuzzy = sorted({describe_strategy(item) for item in strategies if item not in ("exact", "line_endings")})
        if fuzzy:
            notes.append(f"Matched {', '.join(fuzzy)}; check the diff.")
    syntax = output.get("syntax_error")
    problems = [syntax] if isinstance(syntax, dict) else list(output.get("syntax_errors") or [])
    for problem in problems:
        if isinstance(problem, dict):
            where = f"{problem.get('path')}:" if problem.get("path") else "line "
            notes.append(
                f"Syntax error introduced ({problem.get('language')}, {where}{problem.get('line')}): "
                f"{problem.get('message')}"
            )
    return notes


def _completed_card_text(
    tool_name: str,
    output: dict[str, Any],
    metadata: dict[str, Any] | None,
    has_diff: bool,
) -> str | None:
    notes = _result_notes(tool_name, output)
    if has_diff:
        return "\n".join(notes) or None
    summary = _tool_result_summary(tool_name, output) or (metadata or {}).get("content_text")
    parts = [summary, *notes] if summary else notes
    return "\n".join(parts) or None


def _output_tail(output: dict[str, Any], max_lines: int = 40, max_chars: int = 2000) -> str:
    parts: list[str] = []
    for key in ("stdout", "stderr"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            lines = value.rstrip().splitlines()[-max_lines:]
            text = "\n".join(lines)
            if len(text) > max_chars:
                text = text[-max_chars:]
            parts.append(text if key == "stdout" else f"[stderr]\n{text}")
    return "\n".join(parts)


# ------------------------------------------------- provider wire format


def _tool_definitions() -> list[dict[str, Any]]:
    return tool_catalog.tool_definitions()


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return tool_catalog._function_tool(name, description, properties, required)


# Reasoning is sent back as one field, ``reasoning_content``: the name DeepSeek,
# Kimi, GLM and Qwen require and vLLM, SGLang and OpenRouter (as an alias)
# accept, normalised from ``reasoning`` as the AI SDK's openai-compatible
# provider does. OpenRouter's structured ``reasoning_details`` is not echoed.
# Servers that reject the field (Groq, Mistral, TensorRT-LLM) are handled by
# the provider's one-time fallback; our relay strips it before any upstream.
_REASONING_FIELD = "reasoning_content"


# Programs that inspect or move files but never run the code: after an edit,
# running only these is not verification (git status/diff was the last command
# in 44 of 68 benchmark tasks that "verified").
_NON_VERIFYING_PROGRAMS = frozenset({
    "git", "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "echo", "pwd", "which",
    "type", "file", "stat", "tree", "du", "df", "sort", "uniq", "diff", "cut", "tr", "less",
    "more", "env", "printenv", "true", "false", "date", "whoami", "id", "uname", "basename",
    "dirname", "realpath", "readlink", "nl", "sed", "awk", "rm", "mv", "cp", "mkdir", "touch",
    "chmod", "ln", "cd", "export", "set", "unset",
})
_SCRIPT_SEPARATORS = re.compile(r"&&|\|\||[;|\n]")


_INSTALL_SUBCOMMANDS = frozenset({"install", "i", "add", "ci", "uninstall", "remove", "sync", "update"})


def _is_install_command(program: str, args: list[str]) -> bool:
    """Package management (pip/uv/npm install…): changes the environment, checks nothing."""
    if program in {"pip", "pip3", "conda", "mamba", "apt", "apt-get", "brew"}:
        return True
    if program in {"python", "python3", "py"} and args[:2] == ["-m", "pip"]:
        return True
    if program == "uv":
        return not args or args[0] != "run"
    if program in {"npm", "yarn", "pnpm", "bun"}:
        return bool(args) and args[0] in _INSTALL_SUBCOMMANDS
    return False


def _is_verification_command(argv: list[object]) -> bool:
    """Whether a command run counts as checking the code (verify-on-stop).

    Shell scripts (``bash -c "cd src && pytest -q"``) count when any segment
    runs something other than inspection; an unparsable script counts, so the
    gate never nudges because of our own parsing limits.
    """
    if not argv:
        return False
    program = PurePath(str(argv[0])).name.lower()
    if program.endswith((".exe", ".bat", ".cmd")):
        program = program.rsplit(".", 1)[0]
    if program in _SHELL_PROGRAMS and len(argv) >= 3 and str(argv[1]).startswith("-") and "c" in str(argv[1]):
        script = str(argv[2])
        for segment in _SCRIPT_SEPARATORS.split(script):
            try:
                words = shlex.split(segment)
            except ValueError:
                return True
            while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
                words = words[1:]  # VAR=value prefixes
            if words and _is_verification_command(words):
                return True
        return False
    if _is_install_command(program, [str(arg) for arg in argv[1:]]):
        return False
    return program not in _NON_VERIFYING_PROGRAMS


_MORE_WORK_START = re.compile(
    r"^(?:(?:now|next|first|then|also|so|ok(?:ay)?),?\s+)?"
    r"(?:let me|let's|let us|i'll|i will|i'm going to|i am going to)\b",
    re.IGNORECASE,
)


def _announces_more_work(text: str) -> bool:
    """A final message that ends by announcing a next step it did not take.

    Checked on the last sentence only; "Let me know…" closes a turn politely and
    does not count.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(":"):
        return True
    last = re.split(r"(?<=[.!?])\s+|\n+", stripped)[-1].strip()
    if re.search(r"\blet me know\b", last, re.IGNORECASE):
        return False
    return bool(_MORE_WORK_START.match(last))


def _reasoning_fields(source: object) -> dict[str, Any] | None:
    """``{"reasoning_content": text}`` from a response message; exact text ("" kept)."""
    if not isinstance(source, dict):
        return None
    for key in (_REASONING_FIELD, "reasoning"):
        value = source.get(key)
        if isinstance(value, str):
            return {_REASONING_FIELD: value}
    return None


def _with_reasoning(message: dict[str, Any], state: "_TurnState") -> dict[str, Any]:
    """Attach the latest response's reasoning to the message holding its output.

    Consumed once, so runtime messages appended later in the same step never
    carry it. DeepSeek thinking mode with tools requires it on later requests.
    """
    if state.reasoning_fields:
        message = {**message, "provider_reasoning": dict(state.reasoning_fields)}
        state.reasoning_fields = None
    return message


def _to_openai_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role", "user"))
    converted = _to_openai_message_body(message, role)
    if role == "assistant":
        converted.update(_reasoning_fields(message.get("provider_reasoning")) or {})
    return converted


# DeepSeek V4 with tools: "the reasoning_content must be fully passed back to the
# API in all subsequent requests — even for turns where the model did not
# perform a tool call", else HTTP 400. The AI SDK and opencode fill "" on every
# assistant message for these model ids; so do we.
_DEEPSEEK_V4_MODEL = re.compile(r"(?:^|/)deepseek-(?:v4|flash|pro)", re.IGNORECASE)


def _needs_reasoning_on_every_assistant(model: str) -> bool:
    return bool(_DEEPSEEK_V4_MODEL.search(model or ""))


def _apply_reasoning_policy(messages: list[dict[str, Any]], *, echo: bool, fill: bool) -> None:
    """Strip or complete ``reasoning_content`` on outgoing assistant messages, in place."""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if not echo:
            message.pop(_REASONING_FIELD, None)
        elif fill and not isinstance(message.get(_REASONING_FIELD), str):
            message[_REASONING_FIELD] = ""


def _reasoning_rejection(exc: "ProviderRequestFailed") -> str | None:
    """How a 400 treats reasoning: ``rejected`` (unknown field) or ``required``."""
    if exc.status_code != 400:
        return None
    text = str(exc).lower()
    if "reasoning" not in text:
        return None
    if "passed back" in text:
        return "required"
    if any(word in text for word in ("extra", "not permitted", "unsupported", "unknown", "unrecognized", "not allowed")):
        return "rejected"
    return None


def _to_openai_message_body(message: dict[str, Any], role: str) -> dict[str, Any]:
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
                            tool_call.get("arguments", {}), sort_keys=True, default=str
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
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                continue
            function_data = raw_tool_call.get("function", {})
            if not isinstance(function_data, dict):
                continue
            raw_arguments = function_data.get("arguments", "{}")
            if raw_arguments is None:
                raw_arguments = "{}"
            argument_error: str | None = None
            if isinstance(raw_arguments, dict):
                arguments: dict[str, Any] = raw_arguments
                raw_arguments_text = json.dumps(raw_arguments)
            else:
                raw_arguments_text = str(raw_arguments)
                arguments = {}
                try:
                    parsed = json.loads(raw_arguments_text or "{}")
                except json.JSONDecodeError as exc:
                    argument_error = f"Tool arguments were not valid JSON: {exc}"
                else:
                    if isinstance(parsed, dict):
                        arguments = parsed
                    else:
                        argument_error = "Tool arguments must be a JSON object."
            name = str(function_data.get("name", "")).strip()
            if not name:
                continue
            entry: dict[str, Any] = {
                "id": _tool_call_id(raw_tool_call.get("id")),
                "name": name,
                "arguments": arguments,
            }
            if argument_error is not None:
                entry["argument_error"] = argument_error
                entry["raw_arguments"] = raw_arguments_text[:500]
            tool_calls.append(entry)

    final_answer = message.get("content")
    if isinstance(final_answer, list):
        final_answer = "".join(
            str(block.get("text", ""))
            for block in final_answer
            if isinstance(block, dict) and block.get("type") == "text"
        )

    normalized: dict[str, Any] = {"tool_calls": tool_calls}
    reasoning = _normalize_optional_text(
        message.get("reasoning") or message.get("reasoning_content")
    )
    if reasoning is not None:
        normalized["reasoning"] = reasoning
    reasoning_fields = _reasoning_fields(message)
    if reasoning_fields:
        normalized["reasoning_fields"] = reasoning_fields
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


def _usage_is_reported(raw_usage: Any) -> bool:
    if not isinstance(raw_usage, dict):
        return False
    return any(
        _int_or_zero(raw_usage.get(key)) > 0
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )


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

    prompt_tokens = sum(_estimate_tokens(message) for message in messages)
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
    if isinstance(tool_calls, list) and tool_calls:
        parts.append(json.dumps(tool_calls, sort_keys=True, default=str))
    text = " ".join(part for part in parts if part).strip()
    if not text:
        return 1
    return _estimate_text_tokens(text)


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
