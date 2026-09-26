from __future__ import annotations

import errno
import inspect
import os
import re
import shutil
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable, Iterator, Sequence
from uuid import uuid4

from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import run_awaitable_blocking
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import (
    DirectoryNotEmptyError,
    EditMatchError,
    FileTooLargeError,
    NotTextFileError,
    ToolArgumentError,
    ToolFileExistsError,
    ToolFileNotFoundError,
    WorkspaceBoundaryError,
)

if TYPE_CHECKING:
    from code4me2_agent.config import AgentConfig


DEFAULT_READ_LIMIT_LINES = 1000
MAX_READ_LIMIT_LINES = 5000
MAX_LINE_CHARS = 2000
MAX_READ_OUTPUT_BYTES = 24 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
MAX_GREP_LINE_CHARS = 500
# Lines longer than this are not matched at all: they are almost always
# minified assets, and unbounded regex backtracking on them would block the
# worker thread.
MAX_GREP_SCAN_LINE_CHARS = 10_000
MAX_WALK_ENTRIES = 100_000
DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "target",
        "out",
        ".idea",
        ".gradle",
        ".code4me",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


@dataclass(frozen=True)
class FileToolLimits:
    default_read_limit_lines: int = DEFAULT_READ_LIMIT_LINES
    max_read_limit_lines: int = MAX_READ_LIMIT_LINES
    max_line_chars: int = MAX_LINE_CHARS
    max_read_output_bytes: int = MAX_READ_OUTPUT_BYTES
    max_file_bytes: int = MAX_FILE_BYTES
    binary_sniff_bytes: int = BINARY_SNIFF_BYTES
    max_grep_file_bytes: int = MAX_GREP_FILE_BYTES
    max_grep_line_chars: int = MAX_GREP_LINE_CHARS
    max_grep_scan_line_chars: int = MAX_GREP_SCAN_LINE_CHARS
    max_walk_entries: int = MAX_WALK_ENTRIES


@dataclass(frozen=True)
class FileReadResult:
    path: str
    content: str
    backend_type: str
    start_line: int = 1
    end_line: int = 0
    total_lines: int = 0
    truncated: bool = False
    truncation_reason: str | None = None
    next_offset: int | None = None
    decoding_errors_replaced: bool = False


@dataclass(frozen=True)
class FileWriteResult:
    path: str
    bytes_written: int
    backend_type: str


@dataclass(frozen=True)
class FileEditResult:
    path: str
    bytes_written: int
    backend_type: str
    replacements: int
    edits_applied: int


@dataclass(frozen=True)
class FileDeleteResult:
    path: str
    backend_type: str
    was_directory: bool
    bytes_removed: int


@dataclass(frozen=True)
class FileMoveResult:
    source_path: str
    destination_path: str
    backend_type: str
    overwritten: bool
    was_directory: bool


@dataclass(frozen=True)
class FileListResult:
    path: str
    entries: list[str]
    backend_type: str
    file_count: int
    directory_count: int
    truncated: bool


@dataclass(frozen=True)
class FileGlobResult:
    pattern: str
    path: str
    files: list[str]
    backend_type: str
    file_count: int
    truncated: bool


@dataclass(frozen=True)
class FileGrepResult:
    pattern: str
    path: str
    output_mode: str
    backend_type: str
    matches: list[dict[str, object]] | None
    files: list[str] | None
    counts: list[dict[str, object]] | None
    match_count: int
    file_count: int
    files_searched: int
    files_skipped: int
    truncated: bool
    cancelled: bool = False


# Backwards-compatible alias: ``search_files`` now returns grep-shaped results.
FileSearchResult = FileGrepResult


@dataclass(frozen=True)
class TextEdit:
    old_text: str
    new_text: str
    replace_all: bool = False


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

    def read_text_file(self, absolute_path: str) -> str:
        response = self._run_client_call(
            self._client.read_text_file(
                path=absolute_path,
                session_id=self._session_id,
                limit=None,
                line=None,
            )
        )
        if isinstance(response, dict):
            return str(response.get("content", ""))
        return str(getattr(response, "content", ""))

    def write_text_file(self, absolute_path: str, content: str) -> None:
        self._run_client_call(
            self._client.write_text_file(
                content=content,
                path=absolute_path,
                session_id=self._session_id,
            )
        )

    def _run_client_call(self, result: object) -> object:
        if not inspect.isawaitable(result):
            return result
        if self._async_runner is not None:
            return self._async_runner.run(result)
        return run_awaitable_blocking(result)


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
        *,
        limits: FileToolLimits | None = None,
        ignored_dirs: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._config = config
        self._acp_backend = acp_backend
        self._telemetry = telemetry or AgentTelemetryRecorder(config)
        self._limits = limits or FileToolLimits()
        self._ignored_dirs = frozenset(ignored_dirs) if ignored_dirs is not None else DEFAULT_IGNORED_DIRS

    @property
    def workspace_root(self) -> Path:
        return self._config.workspace_root

    @property
    def limits(self) -> FileToolLimits:
        return self._limits

    # ------------------------------------------------------------------ reading

    def read_text(
        self,
        path: str,
        *,
        strict: bool = False,
        tool_name: str = "read_file",
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[str, bool]:
        """Return ``(text, decoding_errors_replaced)`` without recording a tool event.

        Used by the registry to build edit previews; ``tool_name`` attributes any
        denial or backend-fallback event to the calling tool. ``strict=True``
        rejects undecodable files instead of replacing bad bytes.
        """
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            tool_name, path, tool_call_id, run_id, request_id, started_at
        )
        text, replaced, _backend = self._read_resolved(
            resolved_path,
            strict=strict,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        return text, replaced

    def read_file(
        self,
        path: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileReadResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        # Legacy aliases kept for older profiles/histories.
        if offset is None and line_start is not None:
            offset = int(line_start)
        if limit is None and line_end is not None:
            limit = max(1, int(line_end) - (offset or 1) + 1)
        offset = 1 if offset is None else int(offset)
        if offset < 1:
            raise ToolArgumentError("offset must be a positive line number.", field="offset")
        if limit is None:
            limit = self._limits.default_read_limit_lines
        limit = max(1, min(int(limit), self._limits.max_read_limit_lines))
        resolved_path = self._resolve_or_record_denial(
            "read_file", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._relative_path(resolved_path)
        text, replaced, backend_type = self._read_resolved(
            resolved_path,
            strict=False,
            tool_name="read_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        rendered = _render_numbered(text, offset=offset, limit=limit, limits=self._limits)
        self._record_tool_event(
            tool_name="read_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={
                "start_line": rendered["start_line"],
                "end_line": rendered["end_line"],
                "total_lines": rendered["total_lines"],
                "truncated": rendered["truncated"],
            },
        )
        return FileReadResult(
            path=relative_path,
            content=rendered["content"],
            backend_type=backend_type,
            start_line=rendered["start_line"],
            end_line=rendered["end_line"],
            total_lines=rendered["total_lines"],
            truncated=rendered["truncated"],
            truncation_reason=rendered["truncation_reason"],
            next_offset=rendered["next_offset"],
            decoding_errors_replaced=replaced,
        )

    # ------------------------------------------------------------------ writing

    def create_file(
        self,
        path: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileWriteResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "create_file", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._relative_path(resolved_path)
        if self._exists_for_create(resolved_path):
            raise ToolFileExistsError(
                f"File already exists: {relative_path}. Use write_file to overwrite it or "
                "replace_text/edit_file to change part of it."
            )
        backend_type = self._write_text(
            resolved_path,
            content,
            tool_name="create_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        bytes_written = len(content.encode("utf-8"))
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
                "bytes_written": bytes_written,
                "content_capture_mode": self._content_capture_mode,
            },
            raw_payload=self._raw_write_payload(relative_path, "", content),
        )
        return FileWriteResult(path=relative_path, bytes_written=bytes_written, backend_type=backend_type)

    def write_file(
        self,
        path: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileWriteResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "write_file", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._relative_path(resolved_path)
        if resolved_path.is_dir():
            raise ToolArgumentError(
                f"{relative_path} is a directory, not a file.", field="path"
            )
        before = self._read_before(
            resolved_path,
            tool_name="write_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        backend_type = self._write_text(
            resolved_path,
            content,
            tool_name="write_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        bytes_written = len(content.encode("utf-8"))
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
            },
            raw_payload=self._raw_write_payload(relative_path, before, content),
        )
        return FileWriteResult(path=relative_path, bytes_written=bytes_written, backend_type=backend_type)

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileEditResult:
        return self._apply_edits(
            path,
            [TextEdit(old_text=old_text, new_text=new_text, replace_all=replace_all)],
            tool_name="replace_text",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def edit_file(
        self,
        path: str,
        edits: Sequence[TextEdit],
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileEditResult:
        return self._apply_edits(
            path,
            list(edits),
            tool_name="edit_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
        )

    def _apply_edits(
        self,
        path: str,
        edits: Sequence[TextEdit],
        *,
        tool_name: str,
        tool_call_id: str | None,
        run_id: str | None,
        request_id: str | None,
    ) -> FileEditResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        if not edits:
            raise ToolArgumentError("edits must contain at least one replacement.", field="edits")
        resolved_path = self._resolve_or_record_denial(
            tool_name, path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._relative_path(resolved_path)
        current, _replaced, _backend = self._read_resolved(
            resolved_path,
            strict=True,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        updated, replacements = apply_text_edits(current, edits, path=relative_path)
        backend_type = self._write_text(
            resolved_path,
            updated,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            started_at=started_at,
        )
        bytes_written = len(updated.encode("utf-8"))
        self._record_tool_event(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type=backend_type,
            started_at=started_at,
            extra_payload={
                "bytes_written": bytes_written,
                "replacements": replacements,
                "edits_applied": len(edits),
                "content_capture_mode": self._content_capture_mode,
            },
            raw_payload=self._raw_write_payload(relative_path, current, updated),
        )
        return FileEditResult(
            path=relative_path,
            bytes_written=bytes_written,
            backend_type=backend_type,
            replacements=replacements,
            edits_applied=len(edits),
        )

    def delete_file(
        self,
        path: str,
        *,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileDeleteResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_lexical_or_record_denial(
            "delete_file", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._relative_path(resolved_path)
        if resolved_path == self._config.workspace_root.resolve():
            raise ToolArgumentError("Refusing to delete the workspace root.", field="path")
        is_symlink = resolved_path.is_symlink()
        if not resolved_path.exists() and not is_symlink:
            raise ToolFileNotFoundError(
                f"File not found: {relative_path}. Use glob_files or list_files to find the right path."
            )
        was_directory = resolved_path.is_dir() and not is_symlink
        before = ""
        bytes_removed = 0
        if was_directory:
            if any(resolved_path.iterdir()):
                raise DirectoryNotEmptyError(
                    f"Directory is not empty: {relative_path}. Delete its contents first."
                )
            resolved_path.rmdir()
        elif is_symlink:
            # Remove the link itself; its target is untouched.
            resolved_path.unlink()
        else:
            try:
                bytes_removed = resolved_path.stat().st_size
                before, _replaced = self._read_local_text(resolved_path, strict=False)
            except (NotTextFileError, FileTooLargeError, OSError):
                before = ""
            resolved_path.unlink()
        self._record_tool_event(
            tool_name="delete_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type="local",
            started_at=started_at,
            extra_payload={
                "was_directory": was_directory,
                "bytes_removed": bytes_removed,
                "content_capture_mode": self._content_capture_mode,
            },
            raw_payload=self._raw_write_payload(relative_path, before, ""),
        )
        return FileDeleteResult(
            path=relative_path,
            backend_type="local",
            was_directory=was_directory,
            bytes_removed=bytes_removed,
        )

    def move_file(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileMoveResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        source = self._resolve_lexical_or_record_denial(
            "move_file", source_path, tool_call_id, run_id, request_id, started_at
        )
        destination = self._resolve_lexical_or_record_denial(
            "move_file", destination_path, tool_call_id, run_id, request_id, started_at
        )
        source_rel = self._relative_path(source)
        destination_rel = self._relative_path(destination)
        workspace_root = self._config.workspace_root.resolve()
        if source == workspace_root:
            raise ToolArgumentError("Refusing to move the workspace root.", field="source_path")
        if not source.exists() and not source.is_symlink():
            raise ToolFileNotFoundError(
                f"File not found: {source_rel}. Use glob_files or list_files to find the right path."
            )
        if source == destination:
            raise ToolArgumentError(
                "source_path and destination_path are the same.", field="destination_path"
            )
        was_directory = source.is_dir() and not source.is_symlink()
        if was_directory and (destination == source or source in destination.parents):
            raise ToolArgumentError(
                "destination_path is inside the directory being moved.", field="destination_path"
            )
        overwritten = False
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                raise ToolArgumentError(
                    f"Destination is an existing directory: {destination_rel}. Overwriting "
                    "directories is not supported; choose a new path.",
                    field="destination_path",
                )
            if not overwrite:
                raise ToolFileExistsError(
                    f"Destination already exists: {destination_rel}. Pass overwrite=true to replace it."
                )
            if was_directory:
                raise ToolArgumentError(
                    "A directory cannot overwrite an existing file.", field="destination_path"
                )
            overwritten = True
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.move(str(source), str(destination))
            else:
                raise
        self._record_tool_event(
            tool_name="move_file",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=source_rel,
            status="completed",
            backend_type="local",
            started_at=started_at,
            extra_payload={
                "destination_path": destination_rel,
                "overwritten": overwritten,
                "was_directory": was_directory,
            },
        )
        return FileMoveResult(
            source_path=source_rel,
            destination_path=destination_rel,
            backend_type="local",
            overwritten=overwritten,
            was_directory=was_directory,
        )

    # ------------------------------------------------------------- discovery

    def list_files(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        max_results: int = 500,
        include_ignored: bool = False,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileListResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        resolved_path = self._resolve_or_record_denial(
            "list_files", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._display_path(resolved_path)
        if not resolved_path.exists():
            raise ToolFileNotFoundError(f"Directory not found: {relative_path}.")
        if not resolved_path.is_dir():
            raise ToolArgumentError(
                f"{relative_path} is a file, not a directory. Use read_file to read it.",
                field="path",
            )
        max_results = max(1, int(max_results))
        depth_limit = (max_depth if recursive else 1)
        entries: list[str] = []
        file_count = 0
        directory_count = 0
        truncated = False
        try:
            for candidate, is_dir in self._walk(
                resolved_path, max_depth=depth_limit, include_ignored=include_ignored
            ):
                if len(entries) >= max_results:
                    truncated = True
                    break
                rel = self._relative_path(candidate)
                if is_dir:
                    directory_count += 1
                    entries.append(rel + "/")
                else:
                    file_count += 1
                    entries.append(rel)
        except _WalkLimitReached:
            truncated = True
        self._record_tool_event(
            tool_name="list_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type="local",
            started_at=started_at,
            extra_payload={
                "path_count": len(entries),
                "recursive": bool(recursive),
                "truncated": truncated,
            },
        )
        return FileListResult(
            path=relative_path,
            entries=entries,
            backend_type="local",
            file_count=file_count,
            directory_count=directory_count,
            truncated=truncated,
        )

    def glob_files(
        self,
        pattern: str,
        *,
        path: str = ".",
        max_results: int = 500,
        include_ignored: bool = False,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileGlobResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        matcher = glob_to_regex(pattern)
        resolved_path = self._resolve_or_record_denial(
            "glob_files", path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._display_path(resolved_path)
        if not resolved_path.is_dir():
            raise ToolArgumentError(
                f"{relative_path} is not a directory.", field="path"
            )
        max_results = max(1, int(max_results))
        matches: list[str] = []
        truncated = False
        try:
            for candidate, is_dir in self._walk(
                resolved_path, max_depth=None, include_ignored=include_ignored
            ):
                if is_dir:
                    continue
                relative_to_base = candidate.relative_to(resolved_path).as_posix()
                if matcher.match(relative_to_base):
                    matches.append(self._relative_path(candidate))
        except _WalkLimitReached:
            truncated = True
        matches.sort()
        if len(matches) > max_results:
            matches = matches[:max_results]
            truncated = True
        self._record_tool_event(
            tool_name="glob_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type="local",
            started_at=started_at,
            extra_payload={
                "pattern": pattern,
                "file_count": len(matches),
                "truncated": truncated,
            },
        )
        return FileGlobResult(
            pattern=pattern,
            path=relative_path,
            files=matches,
            backend_type="local",
            file_count=len(matches),
            truncated=truncated,
        )

    def grep_files(
        self,
        pattern: str,
        *,
        path: str = ".",
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 200,
        output_mode: str = "content",
        include_ignored: bool = False,
        cancellation_event: Any | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileGrepResult:
        return self._grep(
            pattern,
            path=path,
            glob=glob,
            case_insensitive=case_insensitive,
            context_lines=context_lines,
            max_results=max_results,
            output_mode=output_mode,
            include_ignored=include_ignored,
            tool_name="grep_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            literal_query=None,
            cancellation_event=cancellation_event,
        )

    def search_files(
        self,
        query: str,
        *,
        path: str = ".",
        cancellation_event: Any | None = None,
        tool_call_id: str | None = None,
        run_id: str | None = None,
        request_id: str | None = None,
    ) -> FileGrepResult:
        """Legacy literal search kept for frozen profiles; grep-shaped result."""
        return self._grep(
            re.escape(query),
            path=path,
            glob=None,
            case_insensitive=False,
            context_lines=0,
            max_results=200,
            output_mode="content",
            include_ignored=False,
            tool_name="search_files",
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            literal_query=query,
            cancellation_event=cancellation_event,
        )

    def _grep(
        self,
        pattern: str,
        *,
        path: str,
        glob: str | None,
        case_insensitive: bool,
        context_lines: int,
        max_results: int,
        output_mode: str,
        include_ignored: bool,
        tool_name: str,
        tool_call_id: str | None,
        run_id: str | None,
        request_id: str | None,
        literal_query: str | None,
        cancellation_event: Any | None = None,
    ) -> FileGrepResult:
        started_at = perf_counter()
        tool_call_id = tool_call_id or uuid4().hex
        run_id = run_id or uuid4().hex
        request_id = request_id or uuid4().hex
        if output_mode not in {"content", "files_with_matches", "count"}:
            raise ToolArgumentError(
                "output_mode must be one of content, files_with_matches, count.",
                field="output_mode",
            )
        if not pattern:
            raise ToolArgumentError("pattern must not be empty.", field="pattern")
        try:
            regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        except re.error as exc:
            raise ToolArgumentError(
                f"invalid regular expression: {exc}. Escape special characters for literal text.",
                field="pattern",
            ) from None
        glob_matcher = glob_to_regex(glob) if glob else None
        context_lines = max(0, min(int(context_lines), 10))
        max_results = max(1, int(max_results))
        resolved_path = self._resolve_or_record_denial(
            tool_name, path, tool_call_id, run_id, request_id, started_at
        )
        relative_path = self._display_path(resolved_path)
        if not resolved_path.exists():
            raise ToolFileNotFoundError(f"Path not found: {relative_path}.")

        matches: list[dict[str, object]] = []
        files_with_matches: list[str] = []
        counts: list[dict[str, object]] = []
        match_count = 0
        files_searched = 0
        files_skipped = 0
        truncated = False
        cancelled = False
        scan_limit = self._limits.max_grep_scan_line_chars

        def candidates() -> Iterator[Path]:
            if resolved_path.is_file():
                yield resolved_path
                return
            for candidate, is_dir in self._walk(
                resolved_path, max_depth=None, include_ignored=include_ignored
            ):
                if not is_dir:
                    yield candidate

        try:
            for candidate in candidates():
                if cancellation_event is not None and cancellation_event.is_set():
                    cancelled = True
                    truncated = True
                    break
                if glob_matcher is not None:
                    base = resolved_path if resolved_path.is_dir() else resolved_path.parent
                    if not glob_matcher.match(candidate.relative_to(base).as_posix()):
                        continue
                lines = self._grep_read_lines(candidate)
                if lines is None:
                    files_skipped += 1
                    continue
                files_searched += 1
                rel = self._relative_path(candidate)
                file_matches = 0
                for line_number, line in enumerate(lines, start=1):
                    if len(line) > scan_limit or not regex.search(line):
                        continue
                    file_matches += 1
                    match_count += 1
                    if output_mode == "content":
                        entry: dict[str, object] = {
                            "path": rel,
                            "line_number": line_number,
                            "line": _cut_line(line, self._limits.max_grep_line_chars),
                        }
                        if context_lines:
                            before_start = max(0, line_number - 1 - context_lines)
                            entry["context_before"] = [
                                _cut_line(item, self._limits.max_grep_line_chars)
                                for item in lines[before_start : line_number - 1]
                            ]
                            entry["context_after"] = [
                                _cut_line(item, self._limits.max_grep_line_chars)
                                for item in lines[line_number : line_number + context_lines]
                            ]
                        matches.append(entry)
                        if len(matches) >= max_results:
                            truncated = True
                            break
                if file_matches:
                    files_with_matches.append(rel)
                    counts.append({"path": rel, "count": file_matches})
                if truncated:
                    break
                if output_mode != "content" and len(files_with_matches) >= max_results:
                    truncated = True
                    break
        except _WalkLimitReached:
            truncated = True

        extra_payload: dict[str, object] = {
            "pattern": pattern,
            "output_mode": output_mode,
            "match_count": match_count,
            "file_count": len(files_with_matches),
            "files_searched": files_searched,
            "files_skipped": files_skipped,
            "truncated": truncated,
            "cancelled": cancelled,
        }
        if glob:
            extra_payload["glob"] = glob
        if literal_query is not None:
            extra_payload["query"] = literal_query
        self._record_tool_event(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            run_id=run_id,
            request_id=request_id,
            path=relative_path,
            status="completed",
            backend_type="local",
            started_at=started_at,
            extra_payload=extra_payload,
        )
        return FileGrepResult(
            pattern=literal_query if literal_query is not None else pattern,
            path=relative_path,
            output_mode=output_mode,
            backend_type="local",
            matches=matches if output_mode == "content" else None,
            files=files_with_matches if output_mode == "files_with_matches" else None,
            counts=counts if output_mode == "count" else None,
            match_count=match_count,
            file_count=len(files_with_matches),
            files_searched=files_searched,
            files_skipped=files_skipped,
            truncated=truncated,
            cancelled=cancelled,
        )

    def _grep_read_lines(self, candidate: Path) -> list[str] | None:
        try:
            if candidate.stat().st_size > self._limits.max_grep_file_bytes:
                return None
            data = candidate.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[: self._limits.binary_sniff_bytes]:
            return None
        return split_lines(data.decode("utf-8", errors="replace"))

    def _walk(
        self,
        root: Path,
        *,
        max_depth: int | None,
        include_ignored: bool,
    ) -> Iterator[tuple[Path, bool]]:
        return _walk(
            root,
            max_depth=max_depth,
            include_ignored=include_ignored,
            ignored_dirs=self._ignored_dirs,
            max_entries=self._limits.max_walk_entries,
        )

    # ------------------------------------------------------------- backends

    def _read_resolved(
        self,
        resolved_path: Path,
        *,
        strict: bool,
        tool_name: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> tuple[str, bool, str]:
        relative_path = self._relative_path(resolved_path)
        read_text_file = self._acp_read_text_file()
        if read_text_file is not None:
            try:
                content = str(read_text_file(str(resolved_path)))
                return content, False, "acp"
            except Exception as exc:  # noqa: BLE001
                self._record_acp_backend_fallback(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    path=relative_path,
                    started_at=started_at,
                    error_message=str(exc),
                    operation="read_text_file",
                )
        text, replaced = self._read_local_text(resolved_path, strict=strict)
        return text, replaced, "local"

    def _read_local_text(self, resolved_path: Path, *, strict: bool) -> tuple[str, bool]:
        relative_path = self._relative_path(resolved_path)
        try:
            size = resolved_path.stat().st_size
        except FileNotFoundError:
            raise ToolFileNotFoundError(
                f"File not found: {relative_path}. Use glob_files or list_files to find the right path."
            ) from None
        if resolved_path.is_dir():
            raise ToolArgumentError(
                f"{relative_path} is a directory, not a file. Use list_files to see its contents.",
                field="path",
            )
        if size > self._limits.max_file_bytes:
            raise FileTooLargeError(
                f"{relative_path} is {size} bytes, above the {self._limits.max_file_bytes} byte limit."
            )
        data = resolved_path.read_bytes()
        if b"\x00" in data[: self._limits.binary_sniff_bytes]:
            raise NotTextFileError(
                f"{relative_path} looks like a binary file and cannot be read as text."
            )
        try:
            return data.decode("utf-8"), False
        except UnicodeDecodeError:
            if strict:
                raise NotTextFileError(
                    f"{relative_path} is not valid UTF-8 text; editing it is not supported."
                ) from None
            return data.decode("utf-8", errors="replace"), True

    def _read_before(
        self,
        resolved_path: Path,
        *,
        tool_name: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> str:
        try:
            text, _replaced, _backend = self._read_resolved(
                resolved_path,
                strict=False,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                started_at=started_at,
            )
            return text
        except (ToolFileNotFoundError, NotTextFileError, FileTooLargeError, ToolArgumentError):
            return ""

    def _write_text(
        self,
        resolved_path: Path,
        content: str,
        *,
        tool_name: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> str:
        write_text_file = self._acp_write_text_file()
        if write_text_file is not None:
            try:
                write_text_file(str(resolved_path), content)
                return "acp"
            except Exception as exc:  # noqa: BLE001
                self._record_acp_backend_fallback(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    request_id=request_id,
                    path=self._relative_path(resolved_path),
                    started_at=started_at,
                    error_message=str(exc),
                    operation="write_text_file",
                )
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_path.write_bytes(content.encode("utf-8"))
        return "local"

    def _exists_for_create(self, resolved_path: Path) -> bool:
        if resolved_path.exists() or resolved_path.is_symlink():
            return True
        read_text_file = self._acp_read_text_file()
        if read_text_file is None:
            return False
        try:
            content = read_text_file(str(resolved_path))
        except Exception:  # noqa: BLE001
            return False
        return bool(str(content))

    def _acp_read_text_file(self) -> Callable[[str], str] | None:
        if not bool(getattr(self._acp_backend, "read_text_file_enabled", False)):
            return None
        operation = getattr(self._acp_backend, "read_text_file", None)
        if callable(operation):
            return operation
        return None

    def _acp_write_text_file(self) -> Callable[[str, str], None] | None:
        if not bool(getattr(self._acp_backend, "write_text_file_enabled", False)):
            return None
        operation = getattr(self._acp_backend, "write_text_file", None)
        if callable(operation):
            return operation
        return None

    # ------------------------------------------------------------- helpers

    def _resolve_workspace_path(self, path: str) -> Path:
        workspace_root = self._config.workspace_root.resolve()
        requested_path = Path(str(path)).expanduser()
        if not requested_path.is_absolute():
            requested_path = workspace_root / requested_path
        resolved_path = requested_path.resolve()
        if resolved_path != workspace_root and workspace_root not in resolved_path.parents:
            raise WorkspaceBoundaryError(
                f"Path is outside the workspace root: {path}. Use workspace-relative paths."
            )
        return resolved_path

    def _resolve_lexical_path(self, path: str) -> Path:
        """Confine ``path`` without following a symlink at its last component.

        Used by delete/move so a symlink inside the workspace is removed or
        renamed itself rather than its target. Both the link's own location
        and (for links) its target must stay inside the workspace.
        """
        workspace_root = self._config.workspace_root.resolve()
        requested_path = Path(str(path)).expanduser()
        if not requested_path.is_absolute():
            requested_path = workspace_root / requested_path
        if not requested_path.name or requested_path.name in {".", ".."}:
            return self._resolve_workspace_path(path)
        lexical = requested_path.parent.resolve() / requested_path.name
        if lexical != workspace_root and workspace_root not in lexical.parents:
            raise WorkspaceBoundaryError(
                f"Path is outside the workspace root: {path}. Use workspace-relative paths."
            )
        if lexical.is_symlink():
            target = lexical.resolve()
            if target != workspace_root and workspace_root not in target.parents:
                raise WorkspaceBoundaryError(
                    f"Symbolic link points outside the workspace root: {path}."
                )
        return lexical

    def _resolve_lexical_or_record_denial(
        self,
        tool_name: str,
        path: str,
        tool_call_id: str,
        run_id: str,
        request_id: str,
        started_at: float,
    ) -> Path:
        try:
            return self._resolve_lexical_path(path)
        except PermissionError:
            self._record_tool_event(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                run_id=run_id,
                request_id=request_id,
                path=str(path),
                status="denied",
                backend_type="local",
                started_at=started_at,
                denial_reason="outside_workspace_root",
            )
            raise

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
                path=str(path),
                status="denied",
                backend_type="local",
                started_at=started_at,
                denial_reason="outside_workspace_root",
            )
            raise

    def _relative_path(self, path: Path) -> str:
        root = self._config.workspace_root
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            try:
                return path.relative_to(root.resolve()).as_posix()
            except ValueError:
                return path.as_posix()

    def _display_path(self, resolved_path: Path) -> str:
        if resolved_path == self._config.workspace_root.resolve():
            return "."
        return self._relative_path(resolved_path)

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


# ---------------------------------------------------------------- pure helpers


class _WalkLimitReached(Exception):
    pass


def _walk(
    root: Path,
    *,
    max_depth: int | None,
    include_ignored: bool,
    ignored_dirs: frozenset[str],
    max_entries: int,
) -> Iterator[tuple[Path, bool]]:
    """Yield ``(path, is_directory)`` depth-first with children sorted by name.

    Ignored directory names are pruned at any depth unless ``include_ignored``;
    symbolic links are never followed or listed, so a walk cannot escape the
    workspace. Raises ``_WalkLimitReached`` after ``max_entries`` entries.
    """
    remaining = [max_entries]

    def visit(directory: Path, depth: int) -> Iterator[tuple[Path, bool]]:
        try:
            with os.scandir(directory) as scanner:
                children = sorted(scanner, key=lambda item: item.name)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return
        for child in children:
            try:
                if child.is_symlink():
                    continue
                is_dir = child.is_dir(follow_symlinks=False)
            except OSError:
                continue
            remaining[0] -= 1
            if remaining[0] < 0:
                raise _WalkLimitReached()
            child_path = Path(child.path)
            if is_dir:
                if not include_ignored and child.name in ignored_dirs:
                    continue
                yield child_path, True
                if max_depth is None or depth + 1 < max_depth:
                    yield from visit(child_path, depth + 1)
            else:
                yield child_path, False

    yield from visit(root, 0)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob (``*``, ``?``, ``[...]``, ``**``) into an anchored regex over posix paths."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolArgumentError("pattern must be a non-empty glob.", field="pattern")
    normalized = pattern.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        normalized.startswith("/")
        or normalized.startswith("~")
        or ".." in normalized.split("/")
        or (len(normalized) > 1 and normalized[1] == ":")
    ):
        raise ToolArgumentError(
            "pattern must be a relative glob without '..' segments.", field="pattern"
        )
    parts: list[str] = []
    index = 0
    length = len(normalized)
    while index < length:
        char = normalized[index]
        if char == "*":
            if normalized.startswith("**/", index):
                parts.append("(?:.*/)?")
                index += 3
                continue
            if normalized.startswith("**", index):
                parts.append(".*")
                index += 2
                continue
            parts.append("[^/]*")
            index += 1
            continue
        if char == "?":
            parts.append("[^/]")
            index += 1
            continue
        if char == "[":
            closing = normalized.find("]", index + 1)
            if closing == -1:
                parts.append(re.escape(char))
                index += 1
                continue
            body = normalized[index + 1 : closing]
            if body.startswith("!"):
                body = "^" + body[1:]
            body = body.replace("\\", "\\\\")
            parts.append(f"[{body}]")
            index = closing + 1
            continue
        parts.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def split_lines(text: str) -> list[str]:
    """Split on ``\n`` only (like editors), dropping the empty tail after a final newline."""
    if text == "":
        return []
    lines = text.split("\n")
    if lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _cut_line(line: str, max_chars: int) -> str:
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + "…"


def _render_numbered(
    text: str,
    *,
    offset: int,
    limit: int,
    limits: FileToolLimits,
) -> dict[str, Any]:
    lines = split_lines(text)
    total_lines = len(lines)
    if total_lines == 0:
        return {
            "content": "",
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
            "truncation_reason": None,
            "next_offset": None,
        }
    if offset > total_lines:
        raise ToolArgumentError(
            f"offset {offset} is past the end of the file (total_lines={total_lines}).",
            field="offset",
        )
    selected = lines[offset - 1 : offset - 1 + limit]
    rendered_lines: list[str] = []
    byte_total = 0
    truncation_reason: str | None = None
    end_line = offset - 1
    for position, line in enumerate(selected):
        line_number = offset + position
        if len(line) > limits.max_line_chars:
            line = line[: limits.max_line_chars] + "…[line truncated]"
        rendered = f"{line_number}|{line}"
        byte_total += len(rendered.encode("utf-8")) + 1
        if byte_total > limits.max_read_output_bytes and rendered_lines:
            truncation_reason = "bytes"
            break
        rendered_lines.append(rendered)
        end_line = line_number
    if truncation_reason is None and end_line < total_lines:
        truncation_reason = "limit"
    next_offset = None
    if truncation_reason is not None:
        next_offset = end_line + 1
        rendered_lines.append(
            f"[truncated: showing lines {offset}-{end_line} of {total_lines}; "
            f"call read_file with offset={next_offset} to continue]"
        )
    return {
        "content": "\n".join(rendered_lines),
        "start_line": offset,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": truncation_reason is not None,
        "truncation_reason": truncation_reason,
        "next_offset": next_offset,
    }


_LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\|")
_WHITESPACE_RE = re.compile(r"\s+")


def apply_text_edits(
    text: str,
    edits: Sequence[TextEdit],
    *,
    path: str,
) -> tuple[str, int]:
    """Apply exact-match edits in order; returns ``(new_text, replacements)``.

    Every edit must match exactly once unless ``replace_all``. Nothing is
    returned partially applied: the first failing edit raises and the caller
    writes nothing.
    """
    edit_count = len(edits)
    current = text
    replacements = 0
    for index, edit in enumerate(edits):
        prefix = f"Edit {index + 1} of {edit_count}: " if edit_count > 1 else ""
        field_prefix = f"edits[{index}]." if edit_count > 1 else ""
        if not isinstance(edit.old_text, str) or edit.old_text == "":
            raise ToolArgumentError(
                f"{prefix}old_text must not be empty.", field=f"{field_prefix}old_text"
            )
        if edit.old_text == edit.new_text:
            raise ToolArgumentError(
                f"{prefix}old_text and new_text are identical; nothing to change.",
                field=f"{field_prefix}new_text",
            )
        old_text, new_text = edit.old_text, edit.new_text
        count = current.count(old_text)
        if count == 0:
            converted = _match_line_endings(current, old_text, new_text)
            if converted is not None:
                old_text, new_text = converted
                count = current.count(old_text)
        if count == 0:
            raise EditMatchError(
                f"{prefix}old_text was not found in {path}. {_no_match_hint(current, edit.old_text)}",
                code="edit_no_match",
                match_count=0,
                edit_index=index,
                edit_count=edit_count,
            )
        if count > 1 and not edit.replace_all:
            raise EditMatchError(
                f"{prefix}old_text matches {count} locations in {path}; include more surrounding "
                "lines so it matches exactly once, or set replace_all=true.",
                code="edit_ambiguous",
                match_count=count,
                edit_index=index,
                edit_count=edit_count,
            )
        if edit.replace_all:
            current = current.replace(old_text, new_text)
            replacements += count
        else:
            current = current.replace(old_text, new_text, 1)
            replacements += 1
    return current, replacements


def _match_line_endings(text: str, old_text: str, new_text: str) -> tuple[str, str] | None:
    file_crlf = "\r\n" in text
    old_crlf = "\r\n" in old_text
    if file_crlf and not old_crlf and "\n" in old_text:
        return old_text.replace("\n", "\r\n"), new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    if not file_crlf and old_crlf:
        return old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
    return None


def _no_match_hint(text: str, old_text: str) -> str:
    if _LINE_NUMBER_PREFIX_RE.match(old_text):
        return (
            "old_text starts with the line-number prefix from read_file (e.g. '12|'); "
            "remove the prefix and retry."
        )
    collapsed_old = _WHITESPACE_RE.sub(" ", old_text).strip()
    if collapsed_old and collapsed_old in _WHITESPACE_RE.sub(" ", text):
        return (
            "A match exists that differs only in whitespace or indentation; copy the exact text "
            "from read_file."
        )
    return "Call read_file and copy the exact current text."
