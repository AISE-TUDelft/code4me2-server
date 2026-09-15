from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import run_awaitable_blocking
from code4me2_agent.telemetry import AgentTelemetryRecorder

if TYPE_CHECKING:
    from code4me2_agent.config import AgentConfig


_EDIT_SEQ_LOCK = threading.Lock()
# Process-local monotonic edit counter (not persisted): resets on restart.
# Telemetry uses it only to order edits within one runtime lifetime.
_EDIT_SEQ_COUNTER = 0


def _next_edit_seq() -> int:
    global _EDIT_SEQ_COUNTER
    with _EDIT_SEQ_LOCK:
        _EDIT_SEQ_COUNTER += 1
        return _EDIT_SEQ_COUNTER


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class FileReadResult:
    path: str
    content: str
    backend_type: str


@dataclass(frozen=True)
class FileWriteResult:
    path: str
    bytes_written: int
    backend_type: str
    edit_seq: int | None = None
    mtime_ns: int | None = None
    content_hash: str | None = None


@dataclass(frozen=True)
class FileListResult:
    paths: list[str]
    backend_type: str


@dataclass(frozen=True)
class FileSearchResult:
    matches: list[dict[str, object]]
    backend_type: str


class AcpFileSystemBackend:
    def __init__(
        self,
        *,
        client: object,
        session_id: str,
        read_text_file_enabled: bool,
        write_text_file_enabled: bool,
        async_runner: object | None = None,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._read_text_file_enabled = read_text_file_enabled
        self._write_text_file_enabled = write_text_file_enabled
        self._async_runner = async_runner

    @property
    def read_text_file_enabled(self) -> bool:
        return self._read_text_file_enabled

    @property
    def write_text_file_enabled(self) -> bool:
        return self._write_text_file_enabled

    @property
    def session_id(self) -> str:
        return self._session_id

    def read_text_file(
        self, absolute_path: str, *, cancellation_event: object | None = None
    ) -> str:
        response = self._run_client_call(
            self._client.read_text_file(
                path=absolute_path,
                session_id=self._session_id,
                limit=None,
                line=None,
            ),
            cancellation_event=cancellation_event,
        )
        if isinstance(response, dict):
            return str(response.get("content", ""))
        return str(getattr(response, "content", ""))

    def write_text_file(
        self, absolute_path: str, content: str, *, cancellation_event: object | None = None
    ) -> None:
        self._run_client_call(
            self._client.write_text_file(
                content=content,
                path=absolute_path,
                session_id=self._session_id,
            ),
            cancellation_event=cancellation_event,
        )

    def _run_client_call(
        self,
        result: object,
        *,
        timeout_seconds: float | None = None,
        cancellation_event: object | None = None,
    ) -> object:
        if not inspect.isawaitable(result):
            return result
        # Cancellable bridge (single-attempt, no coroutine reuse). The runner
        # may be a single-arg legacy bridge — run_with_cancellation keeps that
        # compatible with pre/post cancel checks.
        from code4me2_agent.async_bridge import run_with_cancellation

        return run_with_cancellation(
            result,
            self._async_runner,
            cancellation_event,
            timeout_seconds=timeout_seconds,
        )


def build_acp_file_system_backend(
    *,
    client: object,
    session_id: str,
    client_capabilities: object | None,
    async_runner: object | None = None,
) -> AcpFileSystemBackend | None:
    fs_capabilities = capability_value(client_capabilities, "fs")
    read_enabled = bool(
        capability_value(fs_capabilities, "readTextFile")
        or capability_value(fs_capabilities, "read_text_file")
    )
    write_enabled = bool(
        capability_value(fs_capabilities, "writeTextFile")
        or capability_value(fs_capabilities, "write_text_file")
    )
    if not read_enabled and not write_enabled:
        return None
    return AcpFileSystemBackend(
        client=client,
        session_id=session_id,
        read_text_file_enabled=read_enabled,
        write_text_file_enabled=write_enabled,
        async_runner=async_runner,
    )


class WorkspaceFileTools:
    def __init__(
        self,
        config: AgentConfig,
        acp_backend: object | None = None,
        telemetry: AgentTelemetryRecorder | None = None,
    ) -> None:
        self._config = config
        self._acp_backend = acp_backend
        self._telemetry = telemetry or AgentTelemetryRecorder(config)
    # TODO can we make these generic?
    def read_file(
        self,
        path: str,
        *,
        line_start: int | None = None,
        line_end: int | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        cancellation_event: object | None = None,
    ) -> FileReadResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "read_file",
            path,
            tool_call_id,
            run_id,
            request_id,
            started_at,
        )
        relative_path = self._relative_path(resolved_path)
        backend_type = "local"
        read_text_file = self._acp_read_text_file()
        if read_text_file is not None:
            try:
                import inspect as _inspect

                if "cancellation_event" in _inspect.signature(read_text_file).parameters:
                    content = read_text_file(
                        str(resolved_path), cancellation_event=cancellation_event
                    )
                else:
                    content = read_text_file(str(resolved_path))
                backend_type = "acp"
            except Exception as exc:  # noqa: BLE001
                if not _is_missing_acp_session_error(exc):
                    raise
                self._record_acp_backend_fallback(
                    tool_name="read_file",
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    path=relative_path,
                    started_at=started_at,
                    error_message=str(exc),
                    operation="read_text_file",
                )
                content = resolved_path.read_text(encoding="utf-8")
        else:
            content = resolved_path.read_text(encoding="utf-8")
        content = _slice_lines(str(content), line_start=line_start, line_end=line_end)
        self._record_tool_event(
            tool_name="read_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
        )
        return FileReadResult(path=relative_path, content=content, backend_type=backend_type)

    def create_file(
        self,
        path: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        cancellation_event: object | None = None,
    ) -> FileWriteResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "create_file",
            path,
            tool_call_id,
            run_id,
            request_id,
            started_at,
        )
        relative_path = self._relative_path(resolved_path)
        backend_type = "local"
        write_text_file = self._acp_write_text_file()
        if write_text_file is not None:
            # create must not overwrite: probe both the local workspace copy
            # and the ACP backend (client-owned FS) before writing. The ACP
            # probe calls the backend directly so a mere existence check does
            # not emit an extra read_file telemetry event.
            if resolved_path.exists():
                raise FileExistsError(f"File already exists: {path}")
            read_probe = self._acp_read_text_file()
            if read_probe is not None:
                try:
                    import inspect as _inspect_probe

                    if "cancellation_event" in _inspect_probe.signature(read_probe).parameters:
                        read_probe(str(resolved_path), cancellation_event=cancellation_event)
                    else:
                        read_probe(str(resolved_path))
                    raise FileExistsError(f"File already exists: {path}")
                except FileExistsError:
                    raise
                except Exception:
                    # Missing-file (or any inconclusive) probe means "not
                    # proven to exist" — the backend write stays authoritative.
                    pass
            try:
                import inspect as _inspect

                if "cancellation_event" in _inspect.signature(write_text_file).parameters:
                    write_text_file(
                        str(resolved_path), content, cancellation_event=cancellation_event
                    )
                else:
                    write_text_file(str(resolved_path), content)
                backend_type = "acp"
            except Exception as exc:  # noqa: BLE001
                if not _is_missing_acp_session_error(exc):
                    raise
                self._record_acp_backend_fallback(
                    tool_name="create_file",
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    path=relative_path,
                    started_at=started_at,
                    error_message=str(exc),
                    operation="write_text_file",
                )
                if resolved_path.exists():
                    raise FileExistsError(f"File already exists: {path}") from exc
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                resolved_path.write_text(content, encoding="utf-8")
        else:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(content, encoding="utf-8")
        edit_seq = _next_edit_seq()
        if backend_type == "acp":
            # The file lives on the ACP client; no local mtime is meaningful.
            mtime_ns = None
        else:
            try:
                mtime_ns = resolved_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
        content_hash = _content_hash(content)
        self._record_tool_event(
            tool_name="create_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={
                "bytes_written": len(content.encode("utf-8")),
                "content_capture_mode": self._content_capture_mode,
                "edit_seq": edit_seq,
                "mtime_ns": mtime_ns,
                "content_hash": content_hash,
            },
            raw_payload=self._raw_write_payload(relative_path, "", content),
        )
        return FileWriteResult(
            path=relative_path,
            bytes_written=len(content.encode("utf-8")),
            backend_type=backend_type,
            edit_seq=edit_seq,
            mtime_ns=mtime_ns,
            content_hash=content_hash,
        )

    def write_file(
        self,
        path: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        cancellation_event: object | None = None,
    ) -> FileWriteResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "write_file",
            path,
            tool_call_id,
            run_id,
            request_id,
            started_at,
        )
        before = resolved_path.read_text(encoding="utf-8") if resolved_path.exists() else ""
        relative_path = self._relative_path(resolved_path)
        backend_type = "local"
        write_text_file = self._acp_write_text_file()
        if write_text_file is not None:
            try:
                import inspect as _inspect_write

                if "cancellation_event" in _inspect_write.signature(write_text_file).parameters:
                    write_text_file(
                        str(resolved_path), content, cancellation_event=cancellation_event
                    )
                else:
                    write_text_file(str(resolved_path), content)
                backend_type = "acp"
            except Exception as exc:  # noqa: BLE001
                if not _is_missing_acp_session_error(exc):
                    raise
                self._record_acp_backend_fallback(
                    tool_name="write_file",
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    path=relative_path,
                    started_at=started_at,
                    error_message=str(exc),
                    operation="write_text_file",
                )
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                resolved_path.write_text(content, encoding="utf-8")
        else:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            resolved_path.write_text(content, encoding="utf-8")
        bytes_written = len(content.encode("utf-8"))
        edit_seq = _next_edit_seq()
        if backend_type == "acp":
            # The file lives on the ACP client; no local mtime is meaningful.
            mtime_ns = None
        else:
            try:
                mtime_ns = resolved_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
        content_hash = _content_hash(content)
        self._record_tool_event(
            tool_name="write_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={
                "bytes_written": bytes_written,
                "content_capture_mode": self._content_capture_mode,
                "edit_seq": edit_seq,
                "mtime_ns": mtime_ns,
                "content_hash": content_hash,
            },
            raw_payload=self._raw_write_payload(relative_path, before, content),
        )
        return FileWriteResult(
            path=relative_path,
            bytes_written=bytes_written,
            backend_type=backend_type,
            edit_seq=edit_seq,
            mtime_ns=mtime_ns,
            content_hash=content_hash,
        )

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
        cancellation_event: object | None = None,
    ) -> FileWriteResult:
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        current = self.read_file(
            path,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
        ).content
        if old_text not in current:
            raise ValueError("old_text was not found in the file.")
        updated = current.replace(old_text, new_text, 1)
        return self.write_file(
            path,
            updated,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            cancellation_event=cancellation_event,
        )

    def list_files(
        self,
        path: str = ".",
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileListResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "list_files",
            path,
            tool_call_id,
            run_id,
            request_id,
            started_at,
        )
        relative_path = self._relative_path(resolved_path) if resolved_path != self._config.workspace_root else "."
        backend_type = "local"
        files = [self._relative_path(candidate) for candidate in resolved_path.rglob("*") if candidate.is_file()]
        files.sort()
        self._record_tool_event(
            tool_name="list_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={"path_count": len(files)},
        )
        return FileListResult(paths=files, backend_type=backend_type)

    def search_files(
        self,
        query: str,
        *,
        path: str = ".",
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileSearchResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "search_files",
            path,
            tool_call_id,
            run_id,
            request_id,
            started_at,
        )
        relative_path = self._relative_path(resolved_path) if resolved_path != self._config.workspace_root else "."
        backend_type = "local"
        matches: list[dict[str, object]] = []
        candidates = [resolved_path] if resolved_path.is_file() else sorted(resolved_path.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(
                        {
                            "path": self._relative_path(candidate),
                            "line_number": line_number,
                            "line": line,
                        }
                    )
        self._record_tool_event(
            tool_name="search_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={"query": query, "match_count": len(matches)},
        )
        return FileSearchResult(matches=matches, backend_type=backend_type)

    def _acp_read_text_file(self) -> Any | None:
        if not bool(getattr(self._acp_backend, "read_text_file_enabled", False)):
            return None
        operation = getattr(self._acp_backend, "read_text_file", None)
        if callable(operation):
            return operation
        return None

    def _acp_write_text_file(self) -> Any | None:
        if not bool(getattr(self._acp_backend, "write_text_file_enabled", False)):
            return None
        operation = getattr(self._acp_backend, "write_text_file", None)
        if callable(operation):
            return operation
        return None

    def _resolve_workspace_path(self, path: str) -> Path:
        workspace_root = self._config.workspace_root.resolve()
        requested_path = Path(path).expanduser()
        if not requested_path.is_absolute():
            requested_path = workspace_root / requested_path
        resolved_path = requested_path.resolve()
        if resolved_path != workspace_root and workspace_root not in resolved_path.parents:
            raise PermissionError("Path is outside the configured workspace root.")
        return resolved_path

    def _resolve_or_record_denial(
        self,
        tool_name: str,
        path: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> Path:
        try:
            return self._resolve_workspace_path(path)
        except PermissionError:
            self._record_tool_event(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                path=path,
                status="denied",
                backend_type="local",
                started_at=started_at,
                denial_reason="outside_workspace_root",
            )
            raise

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self._config.workspace_root).as_posix()

    @property
    def _content_capture_mode(self) -> str:
        return "raw" if self._config.raw_capture_enabled else "redacted"

    def _raw_write_payload(self, relative_path: str, before: str, after: str) -> dict[str, str]:
        return {
            "content": after,
            "diff": "".join(
                unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=relative_path,
                    tofile=relative_path,
                )
            ),
        }

    def _record_tool_event(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        path: str,
        status: str,
        backend_type: str,
        started_at: float,
        denial_reason: str | None = None,
        extra_payload: dict[str, object] | None = None,
        raw_payload: dict[str, object] | None = None,
    ) -> None:
        event_type = "agent.tool.denied" if status == "denied" else "agent.tool.completed"
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "backend_type": backend_type,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            "status": status,
            "path": path,
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
            raw_payload=raw_payload,
        )

    def _record_acp_backend_fallback(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        path: str,
        started_at: float,
        error_message: str,
        operation: str,
    ) -> None:
        self._telemetry.record(
            event_type="agent.acp.backend_fallback",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "path": path,
                "backend_type": "acp",
                "fallback_backend_type": "local",
                "acp_session_id": getattr(self._acp_backend, "session_id", None),
                "operation": operation,
                "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                "error_message": error_message,
            },
        )


def _is_missing_acp_session_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "session" in message and "not found" in message


def _slice_lines(
    content: str,
    *,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    if line_start is None and line_end is None:
        return content

    lines = content.splitlines(keepends=True)
    start_index = 0 if line_start is None else max(0, int(line_start) - 1)
    end_index = len(lines) if line_end is None else max(start_index, int(line_end))
    return "".join(lines[start_index:end_index])
