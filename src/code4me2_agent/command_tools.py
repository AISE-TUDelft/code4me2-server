from __future__ import annotations

import inspect
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic, perf_counter
from typing import IO, TYPE_CHECKING, Any
from uuid import uuid4

from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import run_awaitable_blocking
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import CommandNotFoundError, ToolError

if TYPE_CHECKING:
    from code4me2_agent.config import AgentConfig


_POLL_SECONDS = 0.25
_KILL_GRACE_SECONDS = 5.0
_PIPE_CHUNK_BYTES = 65536


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
    duration_ms: float = 0.0
    timeout_seconds: float = 0.0
    status: str = "completed"


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
                wait_response = self._wait_for_exit(terminal_id=terminal_id, timeout_seconds=timeout_seconds)
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
        max_timeout_seconds: float | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self._config = config
        self._acp_backend = acp_backend
        self._telemetry = telemetry or AgentTelemetryRecorder(config)
        command_config = config.commands
        self._allowlisted_commands = set(allowlisted_commands or command_config.allowlisted_commands)
        selected_timeout_seconds = command_config.timeout_seconds if timeout_seconds is None else timeout_seconds
        selected_max_timeout_seconds = (
            getattr(command_config, "max_timeout_seconds", 600.0)
            if max_timeout_seconds is None
            else max_timeout_seconds
        )
        selected_max_output_bytes = command_config.max_output_bytes if max_output_bytes is None else max_output_bytes
        self._timeout_seconds = max(1.0, float(selected_timeout_seconds))
        self._max_timeout_seconds = max(self._timeout_seconds, float(selected_max_timeout_seconds))
        self._max_output_bytes = max(1, int(selected_max_output_bytes))

    @property
    def workspace_root(self) -> Path:
        return self._config.workspace_root

    def run_command(
        self,
        argv: object,
        *,
        cwd: str = ".",
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
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
        effective_timeout = self._effective_timeout(timeout_seconds)

        relative_cwd = "." if resolved_cwd == self._config.workspace_root else self._relative_path(resolved_cwd)
        acp_run_command = self._acp_run_command()
        cancelled = False
        if acp_run_command is not None:
            backend_type = "acp"
            acp_result = acp_run_command(
                command=command_name,
                args=normalized_argv[1:],
                cwd=str(resolved_cwd),
                max_output_bytes=self._max_output_bytes,
                timeout_seconds=effective_timeout,
            )
            stdout, stdout_truncated = _truncate_to_max_bytes(acp_result.stdout, self._max_output_bytes)
            stderr, stderr_truncated = _truncate_to_max_bytes(acp_result.stderr, self._max_output_bytes)
            exit_code = acp_result.exit_code
            timed_out = acp_result.timed_out
            output_truncated = acp_result.output_truncated or stdout_truncated or stderr_truncated
        else:
            backend_type = "local"
            local = self._run_local(
                argv=normalized_argv,
                cwd=resolved_cwd,
                timeout_seconds=effective_timeout,
                cancel_event=cancel_event,
            )
            stdout, stderr = local.stdout, local.stderr
            exit_code = local.exit_code
            timed_out = local.timed_out
            cancelled = local.cancelled
            output_truncated = local.output_truncated

        if cancelled:
            status = "cancelled"
        elif timed_out:
            status = "timeout"
        else:
            status = "completed"
        duration_ms = round((perf_counter() - started_at) * 1000, 3)

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
                "timeout_seconds": effective_timeout,
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
            duration_ms=duration_ms,
            timeout_seconds=effective_timeout,
            status=status,
        )

    def _effective_timeout(self, requested: float | None) -> float:
        if requested is None:
            value = self._timeout_seconds
        else:
            try:
                value = float(requested)
            except (TypeError, ValueError):
                value = self._timeout_seconds
        return min(max(value, 1.0), self._max_timeout_seconds)

    def _run_local(
        self,
        *,
        argv: list[str],
        cwd: Path,
        timeout_seconds: float,
        cancel_event: Event | None,
    ) -> "_LocalExecution":
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": _external_command_environment(),
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(argv, **popen_kwargs)
        except FileNotFoundError:
            raise CommandNotFoundError(
                f"Command not found on PATH: {argv[0]}. Only allowlisted programs that are "
                "installed on this machine can run."
            ) from None
        except PermissionError as exc:
            raise ToolError(
                f"Command is not executable: {argv[0]} ({exc.strerror or exc}).",
                code="command_not_executable",
            ) from None

        stdout_collector = _PipeCollector(process.stdout, self._max_output_bytes)
        stderr_collector = _PipeCollector(process.stderr, self._max_output_bytes)
        stdout_collector.start()
        stderr_collector.start()

        deadline = monotonic() + timeout_seconds
        timed_out = False
        cancelled = False
        exit_code: int | None = None
        while True:
            try:
                exit_code = process.wait(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                elif monotonic() >= deadline:
                    timed_out = True
                else:
                    continue
                _terminate_process_tree(process)
                try:
                    process.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = None
                break
        stdout_collector.join(timeout=_KILL_GRACE_SECONDS)
        stderr_collector.join(timeout=_KILL_GRACE_SECONDS)
        stdout_text, stdout_truncated = stdout_collector.render()
        stderr_text, stderr_truncated = stderr_collector.render()
        return _LocalExecution(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            output_truncated=stdout_truncated or stderr_truncated,
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
        allowed = ", ".join(sorted(self._allowlisted_commands)) or "none"
        raise PermissionError(
            f"Command is not in the configured allowlist: {command_name}. Allowed: {allowed}."
        )

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
        requested_path = Path(str(path)).expanduser()
        if not requested_path.is_absolute():
            requested_path = workspace_root / requested_path
        resolved_path = requested_path.resolve()
        if resolved_path != workspace_root and workspace_root not in resolved_path.parents:
            raise PermissionError("Path is outside the configured workspace root.")
        return resolved_path

    def _relative_path(self, path: Path) -> str:
        try:
            return path.relative_to(self._config.workspace_root).as_posix()
        except ValueError:
            return path.as_posix()

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


@dataclass(frozen=True)
class _LocalExecution:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output_truncated: bool


class _PipeCollector(Thread):
    """Drain a pipe keeping the first and last bytes within a fixed memory budget."""

    def __init__(self, pipe: IO[bytes] | None, max_bytes: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._max_bytes = max(1, int(max_bytes))
        self._head_limit = max(1, self._max_bytes // 3)
        self._tail_limit = max(1, self._max_bytes - self._head_limit)
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0

    def run(self) -> None:
        if self._pipe is None:
            return
        try:
            while True:
                chunk = self._pipe.read(_PIPE_CHUNK_BYTES)
                if not chunk:
                    break
                self._total += len(chunk)
                if len(self._head) < self._head_limit:
                    take = self._head_limit - len(self._head)
                    self._head += chunk[:take]
                    chunk = chunk[take:]
                if chunk:
                    self._tail += chunk
                    if len(self._tail) > 2 * self._tail_limit:
                        del self._tail[: len(self._tail) - self._tail_limit]
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._pipe.close()
            except OSError:
                pass

    def render(self) -> tuple[str, bool]:
        tail = bytes(self._tail[-self._tail_limit :]) if self._tail else b""
        if self._total <= self._max_bytes:
            return (bytes(self._head) + tail).decode("utf-8", errors="replace"), False
        dropped = self._total - len(self._head) - len(tail)
        text = (
            bytes(self._head).decode("utf-8", errors="replace")
            + f"\n[... {dropped} bytes truncated ...]\n"
            + tail.decode("utf-8", errors="replace")
        )
        return text, True


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


def available_commands(commands: list[str]) -> list[str]:
    """Return policy commands which can actually launch on this machine."""
    return [command for command in commands if shutil.which(command)]


_ENV_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "JAVA_HOME",
        "JDK_HOME",
        "KOTLIN_HOME",
        "ANDROID_HOME",
        "ANDROID_SDK_ROOT",
        "M2_HOME",
        "MAVEN_HOME",
        "MAVEN_OPTS",
        "GOPATH",
        "GOROOT",
        "NVM_DIR",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
    }
)
_ENV_PREFIXES = (
    "LC_",
    "GIT_",
    "GRADLE_",
    "JAVA_",
    "PYTHON",
    "NODE_",
    "NPM_CONFIG_",
    "SBT_",
    "DOTNET_",
    "CARGO_",
    "RUSTUP_",
)
_ENV_BLOCKED_EXACT = frozenset({"PYTHONHOME", "PYTHONEXECUTABLE", "LD_LIBRARY_PATH"})
_ENV_BLOCKED_PREFIXES = ("CODE4ME_", "_MEIPASS", "DYLD_", "PYINSTALLER")


def _external_command_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build the child environment: an allowlist of shell/toolchain variables.

    Credentials the runtime itself needs (``CODE4ME_ACP_TOKEN`` and friends) and
    PyInstaller loader variables never reach participant tools; a PyInstaller
    ``LD_LIBRARY_PATH_ORIG`` is restored as the child's ``LD_LIBRARY_PATH``.
    """
    environment = dict(os.environ if source is None else source)
    original_library_path = environment.get("LD_LIBRARY_PATH_ORIG")
    child_environment: dict[str, str] = {}
    for key, value in environment.items():
        upper = key.upper()
        if upper in _ENV_BLOCKED_EXACT or upper.startswith(_ENV_BLOCKED_PREFIXES):
            continue
        if upper in _ENV_EXACT or upper.startswith(_ENV_PREFIXES):
            child_environment[key] = value
    if original_library_path:
        child_environment["LD_LIBRARY_PATH"] = original_library_path
    return child_environment


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
