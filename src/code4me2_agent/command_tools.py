from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import run_awaitable_blocking
from code4me2_agent.telemetry import AgentTelemetryRecorder

if TYPE_CHECKING:
    from code4me2_agent.config import AgentConfig


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    backend_type: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


@dataclass(frozen=True)
class AcpCommandExecution:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


class AcpCommandBackend:
    def __init__(self, *, client: object, session_id: str, async_runner: object | None = None) -> None:
        self._client = client
        self._session_id = session_id
        self._async_runner = async_runner

    @property
    def terminal_enabled(self) -> bool:
        return True

    def run_command(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str,
        max_output_bytes: int,
        timeout_seconds: float,
        cancellation_event: object | None = None,
    ) -> AcpCommandExecution:
        terminal_id = self._create_terminal(
            command=command,
            args=args,
            cwd=cwd,
            output_byte_limit=max_output_bytes,
        )

        timed_out = False
        wait_response: object | None = None
        try:
            try:
                from code4me2_agent.async_bridge import run_with_cancellation as _run_cancel

                # Cancellable wait: poll in slices so cancel aborts promptly.
                _cancel = cancellation_event
                if _cancel is not None:
                    wait_response = _run_cancel(
                        self._wait_awaitable(terminal_id=terminal_id),
                        self._async_runner,
                        _cancel,
                        timeout_seconds=timeout_seconds,
                    )
                else:
                    wait_response = self._wait_for_exit(terminal_id=terminal_id, timeout_seconds=timeout_seconds)
            except asyncio.CancelledError:
                try:
                    self._kill_terminal(terminal_id=terminal_id)
                except Exception:
                    pass
                raise
            except TimeoutError:
                timed_out = True
                self._kill_terminal(terminal_id=terminal_id)

            output_response = self._terminal_output(terminal_id=terminal_id)
            stdout, stderr, output_truncated, output_exit_code = self._parse_output_response(output_response)
            exit_code = self._parse_exit_code(wait_response)
            if exit_code is None:
                exit_code = output_exit_code
            return AcpCommandExecution(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                output_truncated=output_truncated,
            )
        finally:
            self._release_terminal(terminal_id=terminal_id)

    def _create_terminal(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str,
        output_byte_limit: int,
    ) -> str:
        method = _resolve_method(self._client, ["create_terminal", "terminal_create"])
        response = self._run_client_call(
            _invoke_with_name_fallback(
                method,
                snake_kwargs={
                    "session_id": self._session_id,
                    "command": command,
                    "args": args,
                    "cwd": cwd,
                    "output_byte_limit": output_byte_limit,
                },
                camel_kwargs={
                    "sessionId": self._session_id,
                    "command": command,
                    "args": args,
                    "cwd": cwd,
                    "outputByteLimit": output_byte_limit,
                },
            )
        )
        if isinstance(response, dict):
            terminal_id = response.get("terminal_id") or response.get("terminalId")
        else:
            terminal_id = getattr(response, "terminal_id", None) or getattr(response, "terminalId", None)
        if terminal_id is None:
            raise RuntimeError("ACP terminal backend did not return a terminal ID.")
        return str(terminal_id)

    def _wait_for_exit(self, *, terminal_id: str, timeout_seconds: float) -> object:
        method = _resolve_method(
            self._client,
            ["wait_for_terminal_exit", "terminal_wait_for_exit"],
        )
        return self._run_client_call(
            _invoke_with_name_fallback(
                method,
                snake_kwargs={
                    "session_id": self._session_id,
                    "terminal_id": terminal_id,
                },
                camel_kwargs={
                    "sessionId": self._session_id,
                    "terminalId": terminal_id,
                },
            ),
            timeout_seconds=timeout_seconds,
        )

    def _wait_awaitable(self, *, terminal_id: str) -> object:
        method = _resolve_method(
            self._client,
            ["wait_for_terminal_exit", "terminal_wait_for_exit"],
        )
        return _invoke_with_name_fallback(
            method,
            snake_kwargs={
                "session_id": self._session_id,
                "terminal_id": terminal_id,
            },
            camel_kwargs={
                "sessionId": self._session_id,
                "terminalId": terminal_id,
            },
        )

    def _terminal_output(self, *, terminal_id: str) -> object:
        method = _resolve_method(
            self._client,
            ["get_terminal_output", "terminal_output"],
        )
        return self._run_client_call(
            _invoke_with_name_fallback(
                method,
                snake_kwargs={
                    "session_id": self._session_id,
                    "terminal_id": terminal_id,
                },
                camel_kwargs={
                    "sessionId": self._session_id,
                    "terminalId": terminal_id,
                },
            )
        )

    def _kill_terminal(self, *, terminal_id: str) -> None:
        method = _resolve_method(self._client, ["kill_terminal", "terminal_kill"])
        self._run_client_call(
            _invoke_with_name_fallback(
                method,
                snake_kwargs={
                    "session_id": self._session_id,
                    "terminal_id": terminal_id,
                },
                camel_kwargs={
                    "sessionId": self._session_id,
                    "terminalId": terminal_id,
                },
            )
        )

    def _release_terminal(self, *, terminal_id: str) -> None:
        method = _resolve_method(self._client, ["release_terminal", "terminal_release"])
        self._run_client_call(
            _invoke_with_name_fallback(
                method,
                snake_kwargs={
                    "session_id": self._session_id,
                    "terminal_id": terminal_id,
                },
                camel_kwargs={
                    "sessionId": self._session_id,
                    "terminalId": terminal_id,
                },
            )
        )

    def _run_client_call(self, result: object, timeout_seconds: float | None = None) -> object:
        if not inspect.isawaitable(result):
            return result
        if self._async_runner is not None:
            return self._async_runner.run(result, timeout_seconds=timeout_seconds)
        return run_awaitable_blocking(result, timeout_seconds=timeout_seconds)

    def _parse_output_response(self, response: object) -> tuple[str, str, bool, int | None]:
        if isinstance(response, dict):
            output = str(response.get("output", ""))
            stderr = str(response.get("stderr", ""))
            truncated = bool(response.get("truncated", False))
            exit_status = response.get("exit_status") or response.get("exitStatus") or {}
        else:
            output = str(getattr(response, "output", ""))
            stderr = str(getattr(response, "stderr", ""))
            truncated = bool(getattr(response, "truncated", False))
            exit_status = getattr(response, "exit_status", None) or getattr(response, "exitStatus", None) or {}
        exit_code = _extract_exit_code(exit_status)
        return output, stderr, truncated, exit_code

    def _parse_exit_code(self, response: object | None) -> int | None:
        if response is None:
            return None
        return _extract_exit_code(response)


def build_acp_command_backend(
    *,
    client: object,
    session_id: str,
    client_capabilities: object | None,
    async_runner: object | None = None,
) -> AcpCommandBackend | None:
    terminal_capability = capability_value(client_capabilities, "terminal")
    if not bool(terminal_capability):
        return None

    required_method_groups = [
        ["create_terminal", "terminal_create"],
        ["wait_for_terminal_exit", "terminal_wait_for_exit"],
        ["get_terminal_output", "terminal_output"],
        ["release_terminal", "terminal_release"],
        ["kill_terminal", "terminal_kill"],
    ]
    for names in required_method_groups:
        if _resolve_method(client, names) is None:
            return None

    return AcpCommandBackend(client=client, session_id=session_id, async_runner=async_runner)


class WorkspaceCommandTools:
    def __init__(
        self,
        config: AgentConfig,
        *,
        acp_backend: object | None = None,
        telemetry: AgentTelemetryRecorder | None = None,
        allowlisted_commands: set[str] | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self._config = config
        self._acp_backend = acp_backend
        self._telemetry = telemetry or AgentTelemetryRecorder(config)
        command_config = config.commands
        self._allowlisted_commands = set(allowlisted_commands or command_config.allowlisted_commands)
        selected_timeout_seconds = command_config.timeout_seconds if timeout_seconds is None else timeout_seconds
        selected_max_output_bytes = command_config.max_output_bytes if max_output_bytes is None else max_output_bytes
        self._timeout_seconds = float(selected_timeout_seconds)
        self._max_output_bytes = max(1, int(selected_max_output_bytes))

    def run_command(
        self,
        argv: object,
        *,
        cwd: str = ".",
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        cancellation_event: object | None = None,
    ) -> CommandResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex

        normalized_argv = self._normalize_argv_or_record_denial(
            argv=argv,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        command_name = self._command_name_or_record_denial(
            argv=normalized_argv,
            cwd=cwd,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        self._validate_allowlist_or_record_denial(
            command_name=command_name,
            argv=normalized_argv,
            cwd=cwd,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        resolved_cwd = self._resolve_cwd_or_record_denial(
            cwd=cwd,
            argv=normalized_argv,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )

        relative_cwd = "." if resolved_cwd == self._config.workspace_root else self._relative_path(resolved_cwd)
        acp_run_command = self._acp_run_command()
        if acp_run_command is not None:
            backend_type = "acp"
            try:
                import inspect as _inspect

                if "cancellation_event" in _inspect.signature(acp_run_command).parameters:
                    acp_result = acp_run_command(
                        command=command_name,
                        args=normalized_argv[1:],
                        cwd=str(resolved_cwd),
                        max_output_bytes=self._max_output_bytes,
                        timeout_seconds=self._timeout_seconds,
                        cancellation_event=cancellation_event,
                    )
                else:
                    acp_result = acp_run_command(
                        command=command_name,
                        args=normalized_argv[1:],
                        cwd=str(resolved_cwd),
                        max_output_bytes=self._max_output_bytes,
                        timeout_seconds=self._timeout_seconds,
                    )
            except asyncio.CancelledError:
                raise
            stdout_raw = acp_result.stdout
            stderr_raw = acp_result.stderr
            exit_code = acp_result.exit_code
            timed_out = acp_result.timed_out
            output_truncated = acp_result.output_truncated
        else:
            backend_type = "local"
            stdout_raw, stderr_raw, exit_code, timed_out = self._run_local(
                argv=normalized_argv,
                cwd=resolved_cwd,
                cancellation_event=cancellation_event,
            )
            output_truncated = False

        stdout, stdout_truncated = _truncate_to_max_bytes(stdout_raw, self._max_output_bytes)
        stderr, stderr_truncated = _truncate_to_max_bytes(stderr_raw, self._max_output_bytes)
        output_truncated = output_truncated or stdout_truncated or stderr_truncated
        status = "timeout" if timed_out else "completed"

        self._record_tool_event(
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            argv=normalized_argv,
            cwd=relative_cwd,
            status=status,
            backend_type=backend_type,
            started_at=started_at,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=output_truncated,
            extra_payload={
                "command": command_name,
                "stdout_bytes": len(stdout.encode("utf-8")),
                "stderr_bytes": len(stderr.encode("utf-8")),
                "max_output_bytes": self._max_output_bytes,
            },
        )

        return CommandResult(
            argv=normalized_argv,
            cwd=relative_cwd,
            backend_type=backend_type,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=output_truncated,
        )

    def _run_local(
        self,
        *,
        argv: list[str],
        cwd: Path,
        cancellation_event: object | None = None,
    ) -> tuple[str, str, int | None, bool]:
        def _cancel_requested() -> bool:
            try:
                return bool(
                    cancellation_event is not None and cancellation_event.is_set()  # type: ignore[union-attr]
                )
            except Exception:
                return False

        if _cancel_requested():
            raise asyncio.CancelledError("Command execution was cancelled.")
        # Popen+poll so cancellation kills promptly (no blocking subprocess.run).
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_external_command_environment(),
            )
        except OSError as exc:
            raise RuntimeError(f"Failed to start command: {exc}") from exc
        try:
            import time as _time

            deadline = _time.monotonic() + float(self._timeout_seconds)
            while True:
                if _cancel_requested():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
                    raise asyncio.CancelledError("Command execution was cancelled.")
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        stdout, stderr = proc.communicate(timeout=2.0)
                    except Exception:
                        stdout, stderr = "", ""
                    return (
                        _coerce_output_text(stdout),
                        _coerce_output_text(stderr),
                        None,
                        True,
                    )
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.2, remaining))
                    return (
                        _coerce_output_text(stdout),
                        _coerce_output_text(stderr),
                        proc.returncode,
                        False,
                    )
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired as exc:
            return (
                _coerce_output_text(exc.stdout),
                _coerce_output_text(exc.stderr),
                None,
                True,
            )


    def _acp_run_command(self) -> Any | None:
        if not bool(getattr(self._acp_backend, "terminal_enabled", False)):
            return None
        operation = getattr(self._acp_backend, "run_command", None)
        if callable(operation):
            return operation
        return None

    def _command_name_or_record_denial(
        self,
        *,
        argv: list[str],
        cwd: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> str:
        command_name = Path(argv[0]).name
        if command_name != argv[0]:
            self._record_denial(
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
                argv=argv,
                cwd=cwd,
                denial_reason="command_path_not_allowed",
            )
            raise PermissionError("Command argv[0] must be an allowlisted command name, not a path.")
        return command_name

    def _normalize_argv_or_record_denial(
        self,
        *,
        argv: object,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> list[str]:
        if isinstance(argv, str):
            self._record_denial(
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
                argv=[argv],
                cwd=".",
                denial_reason="argv_must_be_array",
            )
            raise TypeError("Command execution requires argv as a list/tuple, not a shell string.")

        if not isinstance(argv, (list, tuple)):
            self._record_denial(
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
                argv=[],
                cwd=".",
                denial_reason="invalid_argv",
            )
            raise TypeError("argv must be a list or tuple of strings.")

        normalized: list[str] = []
        for arg in argv:
            if not isinstance(arg, str):
                self._record_denial(
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    started_at=started_at,
                    argv=[str(item) for item in argv],
                    cwd=".",
                    denial_reason="invalid_argv",
                )
                raise TypeError("argv must contain only strings.")
            normalized.append(arg)

        if not normalized or not normalized[0].strip():
            self._record_denial(
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
                argv=normalized,
                cwd=".",
                denial_reason="invalid_argv",
            )
            raise ValueError("argv must contain a non-empty command at index 0.")

        return normalized

    def _validate_allowlist_or_record_denial(
        self,
        *,
        command_name: str,
        argv: list[str],
        cwd: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> None:
        if command_name in self._allowlisted_commands:
            return
        self._record_denial(
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
            argv=argv,
            cwd=cwd,
            denial_reason="command_not_allowlisted",
        )
        raise PermissionError(f"Command is not in the configured allowlist: {command_name}")

    def _resolve_cwd_or_record_denial(
        self,
        *,
        cwd: str,
        argv: list[str],
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> Path:
        try:
            return self._resolve_workspace_path(cwd)
        except PermissionError:
            self._record_denial(
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
                argv=argv,
                cwd=cwd,
                denial_reason="outside_workspace_root",
            )
            raise

    def _resolve_workspace_path(self, path: str) -> Path:
        workspace_root = self._config.workspace_root.resolve()
        requested_path = Path(path).expanduser()
        if not requested_path.is_absolute():
            requested_path = workspace_root / requested_path
        resolved_path = requested_path.resolve()
        if resolved_path != workspace_root and workspace_root not in resolved_path.parents:
            raise PermissionError("Path is outside the configured workspace root.")
        return resolved_path

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self._config.workspace_root).as_posix()

    def _record_denial(
        self,
        *,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
        argv: list[str],
        cwd: str,
        denial_reason: str,
    ) -> None:
        self._record_tool_event(
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            argv=argv,
            cwd=cwd,
            status="denied",
            backend_type="local",
            started_at=started_at,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            output_truncated=False,
            denial_reason=denial_reason,
        )

    def _record_tool_event(
        self,
        *,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        argv: list[str],
        cwd: str,
        status: str,
        backend_type: str,
        started_at: float,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        timed_out: bool,
        output_truncated: bool,
        denial_reason: str | None = None,
        extra_payload: dict[str, object] | None = None,
    ) -> None:
        event_type = "agent.tool.denied" if status == "denied" else "agent.tool.completed"
        payload: dict[str, object] = {
            "tool_name": "run_command",
            "tool_call_id": tool_call_id,
            "backend_type": backend_type,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            "status": status,
            "argv": argv,
            "cwd": cwd,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_truncated": output_truncated,
            "stdout": stdout,
            "stderr": stderr,
        }
        payload.update(extra_payload or {})
        if denial_reason is not None:
            payload["denial_reason"] = denial_reason

        self._telemetry.record(
            event_type=event_type,
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload=payload,
            raw_payload=None,
        )


def available_commands(commands: list[str]) -> list[str]:
    """Return policy commands which can actually launch on this machine."""
    return [command for command in commands if shutil.which(command)]


def _external_command_environment() -> dict[str, str]:
    """Undo PyInstaller loader changes before spawning participant tools."""
    environment = dict(os.environ)
    environment.pop("_MEIPASS2", None)
    original_library_path = environment.pop("LD_LIBRARY_PATH_ORIG", None)
    if original_library_path is None:
        environment.pop("LD_LIBRARY_PATH", None)
    else:
        environment["LD_LIBRARY_PATH"] = original_library_path
    return environment


def _resolve_method(client: object, names: list[str]) -> Any | None:
    for name in names:
        method = getattr(client, name, None)
        if callable(method):
            return method
    return None


def _invoke_with_name_fallback(method: Any, *, snake_kwargs: dict[str, object], camel_kwargs: dict[str, object]) -> object:
    try:
        return method(**snake_kwargs)
    except TypeError:
        return method(**camel_kwargs)


def _extract_exit_code(response: object) -> int | None:
    if isinstance(response, dict):
        raw_exit_code = response.get("exit_code")
        if raw_exit_code is None:
            raw_exit_code = response.get("exitCode")
    else:
        raw_exit_code = getattr(response, "exit_code", None)
        if raw_exit_code is None:
            raw_exit_code = getattr(response, "exitCode", None)
    if raw_exit_code is None:
        return None
    try:
        return int(raw_exit_code)
    except (TypeError, ValueError):
        return None


def _truncate_to_max_bytes(value: str, max_output_bytes: int) -> tuple[str, bool]:
    raw_bytes = value.encode("utf-8")
    if len(raw_bytes) <= max_output_bytes:
        return value, False

    truncated_bytes = raw_bytes[-max_output_bytes:]
    while truncated_bytes:
        try:
            return truncated_bytes.decode("utf-8"), True
        except UnicodeDecodeError:
            truncated_bytes = truncated_bytes[1:]
    return "", True


def _coerce_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
