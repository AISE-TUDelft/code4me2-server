"""Per-session tool state that must outlive ToolRegistry rebuilds.

Managed prompts re-apply the run policy before every prompt, which rebuilds the
file tools and the registry; anything the agent should remember for the whole
chat (which files the model has looked at, what each turn changed) therefore
lives here, owned by ``EchoAgentCore``. Nothing is written into the project:
checkpoints are kept in memory only.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

# Checkpoint limits: files above this are recorded as not restorable, and the
# oldest turns are dropped once the total or the turn count is exceeded.
MAX_CHECKPOINT_FILE_CHARS = 2 * 1024 * 1024
MAX_CHECKPOINT_TOTAL_CHARS = 32 * 1024 * 1024
MAX_CHECKPOINT_TURNS = 20


@dataclass(frozen=True)
class FileChange:
    """One successful workspace mutation reported by the file tools.

    ``before``/``after`` are the text before and after the change; ``None``
    means the path did not exist (before) or no longer exists (after).
    ``before_known`` is false when the previous content could not be captured
    (binary, oversized, a directory), which makes the change not restorable.
    """

    path: str
    before: str | None
    after: str | None
    before_known: bool = True


@dataclass
class _PathRecord:
    before: str | None
    after: str | None
    before_known: bool
    order: int


@dataclass
class TurnCheckpoint:
    turn_id: str
    prompt_preview: str
    files: dict[str, _PathRecord] = field(default_factory=dict)

    def size(self) -> int:
        total = 0
        for record in self.files.values():
            total += len(record.before or "") + len(record.after or "")
        return total


@dataclass(frozen=True)
class UndoOutcome:
    restored: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    turn_id: str | None = None

    @property
    def nothing_to_undo(self) -> bool:
        return self.turn_id is None


def normalize_workspace_path(workspace_root: Path, path: object) -> str | None:
    """Workspace-relative posix path for ``path``, or None when it escapes."""
    if not isinstance(path, str) or not path.strip():
        return None
    root = workspace_root.resolve()
    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = Path(os.path.normpath(str(candidate)))
        if resolved.exists() or resolved.is_symlink():
            resolved = resolved.resolve()
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    text = relative.as_posix()
    return text if text != "." else None


class SessionToolState:
    def __init__(self) -> None:
        self._lock = RLock()
        self._seen: set[str] = set()
        self._turns: list[TurnCheckpoint] = []
        self._current: TurnCheckpoint | None = None
        self._order = 0
        self._recording_suspended = 0

    # ------------------------------------------------------------ seen files

    def mark_seen(self, path: str | None) -> None:
        if path:
            with self._lock:
                self._seen.add(path)

    def has_seen(self, path: str | None) -> bool:
        if not path:
            return False
        with self._lock:
            return path in self._seen

    def forget_seen(self) -> None:
        with self._lock:
            self._seen.clear()

    # ------------------------------------------------------------ checkpoints

    def begin_turn(self, turn_id: str, prompt: str) -> None:
        with self._lock:
            self._current = TurnCheckpoint(
                turn_id=turn_id, prompt_preview=" ".join(prompt.split())[:120]
            )

    def end_turn(self) -> None:
        with self._lock:
            current = self._current
            self._current = None
            if current is None or not current.files:
                return
            self._turns.append(current)
            while len(self._turns) > MAX_CHECKPOINT_TURNS or (
                len(self._turns) > 1
                and sum(turn.size() for turn in self._turns) > MAX_CHECKPOINT_TOTAL_CHARS
            ):
                self._turns.pop(0)

    @contextmanager
    def recording_suspended(self) -> Iterator[None]:
        with self._lock:
            self._recording_suspended += 1
        try:
            yield
        finally:
            with self._lock:
                self._recording_suspended -= 1

    def record_change(self, change: FileChange) -> None:
        """File-tool observer: remember the first before-state per path per turn."""
        with self._lock:
            if self._recording_suspended or self._current is None:
                return
            before = change.before
            before_known = change.before_known
            if before is not None and len(before) > MAX_CHECKPOINT_FILE_CHARS:
                before, before_known = None, False
            after = change.after
            if after is not None and len(after) > MAX_CHECKPOINT_FILE_CHARS:
                after, before_known = None, False
            record = self._current.files.get(change.path)
            if record is None:
                self._order += 1
                self._current.files[change.path] = _PathRecord(
                    before=before, after=after, before_known=before_known, order=self._order
                )
            else:
                record.after = after
                if not before_known:
                    record.before_known = False

    def changed_paths(self) -> list[str]:
        """Paths changed so far in the current turn, in first-change order."""
        with self._lock:
            if self._current is None:
                return []
            return [
                path
                for path, _record in sorted(
                    self._current.files.items(), key=lambda item: item[1].order
                )
            ]

    def turn_diff(self, *, max_chars: int = 40_000) -> str:
        """Unified diff of the current turn's changes (for self-review)."""
        with self._lock:
            records = (
                sorted(self._current.files.items(), key=lambda item: item[1].order)
                if self._current is not None
                else []
            )
            return _render_diff(records, max_chars=max_chars)

    def session_diff(self, *, max_chars: int = 40_000) -> str:
        """Unified diff from each path's earliest recorded state to its latest."""
        with self._lock:
            merged: dict[str, _PathRecord] = {}
            turns = list(self._turns) + ([self._current] if self._current is not None else [])
            for turn in turns:
                for path, record in sorted(turn.files.items(), key=lambda item: item[1].order):
                    existing = merged.get(path)
                    if existing is None:
                        merged[path] = _PathRecord(
                            record.before, record.after, record.before_known, record.order
                        )
                    else:
                        existing.after = record.after
                        existing.before_known = existing.before_known and record.before_known
            return _render_diff(sorted(merged.items(), key=lambda item: item[1].order), max_chars=max_chars)

    def checkpoint_count(self) -> int:
        with self._lock:
            return len(self._turns)

    def last_turn(self) -> TurnCheckpoint | None:
        with self._lock:
            return self._turns[-1] if self._turns else None

    def undo_last_turn(self, file_tools: Any) -> UndoOutcome:
        """Restore every file the last changing turn touched.

        A file is restored only while its current content is still exactly what
        the agent left behind; anything the user edited since is skipped and
        reported, never overwritten.
        """
        with self._lock:
            if not self._turns:
                return UndoOutcome()
            turn = self._turns.pop()
        restored: list[str] = []
        deleted: list[str] = []
        skipped: list[tuple[str, str]] = []
        with self.recording_suspended():
            for path, record in sorted(
                turn.files.items(), key=lambda item: item[1].order, reverse=True
            ):
                if not record.before_known:
                    skipped.append((path, "its previous content was not captured"))
                    continue
                try:
                    current = _current_text(file_tools, path)
                except Exception as exc:  # noqa: BLE001 - unreadable now: leave it alone
                    skipped.append((path, f"it could not be read ({exc})"))
                    continue
                if current != record.after:
                    skipped.append((path, "it changed after the agent's edit"))
                    continue
                try:
                    if record.before is None:
                        if current is not None:
                            _delete(file_tools, path)
                            deleted.append(path)
                    else:
                        _write(file_tools, path, record.before)
                        restored.append(path)
                except Exception as exc:  # noqa: BLE001 - report, keep going
                    skipped.append((path, f"restoring failed: {exc}"))
        return UndoOutcome(
            restored=tuple(restored),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
            turn_id=turn.turn_id,
        )


def _render_diff(records: list[tuple[str, _PathRecord]], *, max_chars: int) -> str:
    parts: list[str] = []
    for path, record in records:
        if not record.before_known:
            parts.append(f"--- {path}\n+++ {path}\n(binary or oversized file changed; diff not available)\n")
            continue
        before = record.before or ""
        after = record.after or ""
        if before == after and record.before is not None and record.after is not None:
            continue
        diff = "".join(
            unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}" if record.before is not None else "/dev/null",
                tofile=f"b/{path}" if record.after is not None else "/dev/null",
            )
        )
        if diff and not diff.endswith("\n"):
            diff += "\n"
        parts.append(diff)
    text = "".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[diff truncated: {len(text) - max_chars} more characters]\n"
    return text


def _current_text(file_tools: Any, path: str) -> str | None:
    read_text = getattr(file_tools, "read_text", None)
    if not callable(read_text):
        return None
    try:
        text, _replaced = read_text(path, strict=True, tool_name="undo")
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "file_not_found":
            return None
        raise
    return text


def _write(file_tools: Any, path: str, content: str) -> None:
    write = getattr(file_tools, "restore_text", None) or getattr(file_tools, "write_file")
    write(path, content)


def _delete(file_tools: Any, path: str) -> None:
    discard = getattr(file_tools, "discard_file", None) or getattr(file_tools, "delete_file")
    discard(path)
