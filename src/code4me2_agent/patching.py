"""The ``apply_patch`` edit format (OpenAI's V4A patch grammar).

    *** Begin Patch
    *** Update File: src/app.py
    @@ def handler(event):
    -    return None
    +    return event
    *** Add File: src/new.py
    +print("hello")
    *** Delete File: src/old.py
    *** End Patch

``*** Update File`` may be followed by ``*** Move to: <path>``; a chunk starts
at ``@@`` with an optional anchor line to seek first (several ``@@`` lines
narrow down step by step); ``' '`` lines are context, ``-`` removed, ``+``
added; ``*** End of File`` pins the chunk to the end of the file. Context is
located the way Codex does it: exact, then ignoring trailing whitespace, then
ignoring surrounding whitespace, then after normalising typographic
punctuation. Every chunk of every file is resolved before anything is written,
so a patch either applies completely or not at all.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
DELETE = "*** Delete File: "
UPDATE = "*** Update File: "
MOVE = "*** Move to: "
EOF_MARKER = "*** End of File"

MAX_PATCH_FILES = 50


class PatchError(ValueError):
    """The patch text is malformed or does not apply to the current files."""


@dataclass
class Chunk:
    anchors: list[str] = field(default_factory=list)
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    end_of_file: bool = False


@dataclass
class PatchAction:
    action: str  # "add" | "delete" | "update"
    path: str
    move_to: str | None = None
    content: str | None = None  # for "add"
    chunks: list[Chunk] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedFile:
    path: str
    action: str
    old_text: str | None
    new_text: str | None
    move_to: str | None = None
    fuzz: int = 0

    @property
    def target(self) -> str:
        return self.move_to or self.path


# ------------------------------------------------------------------ parsing


_HEREDOC_START_RE = re.compile(r"^(?:apply_patch\s+)?<<-?\s*['\"]?EOF['\"]?\s*$")


def _unwrap(lines: list[str]) -> list[str]:
    """Drop wrappers models copy from shell usage: a heredoc or a code fence."""
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    if len(lines) >= 2 and _HEREDOC_START_RE.match(lines[0].strip()) and lines[-1].strip() == "EOF":
        lines = lines[1:-1]
    return lines


def parse_patch(text: str) -> list[PatchAction]:
    if not isinstance(text, str) or not text.strip():
        raise PatchError("The patch is empty.")
    lines = _unwrap(text.replace("\r\n", "\n").split("\n"))
    if not lines or lines[0].strip() != BEGIN:
        raise PatchError(f"The patch must start with '{BEGIN}'.")
    ends = [index for index, line in enumerate(lines) if line.strip() == END]
    if not ends:
        raise PatchError(f"The patch must end with '{END}'.")
    trailing = lines[ends[0] + 1 :]
    if any(line.strip() not in ("", END, "EOF", "```") for line in trailing):
        raise PatchError(f"Unexpected text after '{END}'; send one patch per call.")
    body = lines[1 : ends[0]]
    actions: list[PatchAction] = []
    index = 0
    while index < len(body):
        line = body[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(ADD):
            path = _path(line[len(ADD):], line)
            index += 1
            content_lines: list[str] = []
            while index < len(body) and not _is_header(body[index]):
                entry = body[index]
                if not entry.startswith("+"):
                    raise PatchError(
                        f"Every line of an added file must start with '+' ({path}, got {entry[:40]!r})."
                    )
                content_lines.append(entry[1:])
                index += 1
            content = "\n".join(content_lines) + ("\n" if content_lines else "")
            actions.append(PatchAction("add", path, content=content))
            continue
        if line.startswith(DELETE):
            actions.append(PatchAction("delete", _path(line[len(DELETE):], line)))
            index += 1
            continue
        if line.startswith(UPDATE):
            path = _path(line[len(UPDATE):], line)
            index += 1
            move_to = None
            if index < len(body) and body[index].startswith(MOVE):
                move_to = _path(body[index][len(MOVE):], body[index])
                index += 1
            chunks, index = _parse_chunks(body, index, path)
            if not chunks and move_to is None:
                raise PatchError(f"Update of {path} contains no changes.")
            actions.append(PatchAction("update", path, move_to=move_to, chunks=chunks))
            continue
        raise PatchError(
            f"Unexpected line {line[:60]!r}: expected '*** Add File:', '*** Delete File:' or "
            "'*** Update File:'."
        )
    if not actions:
        raise PatchError("The patch changes no files.")
    if len(actions) > MAX_PATCH_FILES:
        raise PatchError(f"A patch may change at most {MAX_PATCH_FILES} files.")
    seen: set[str] = set()
    for action in actions:
        for path in filter(None, (action.path, action.move_to)):
            if path in seen:
                raise PatchError(f"{path} appears more than once in the patch.")
            seen.add(path)
    return actions


def _is_header(line: str) -> bool:
    return line.startswith((ADD, DELETE, UPDATE))


def _path(raw: str, line: str) -> str:
    path = raw.strip()
    if not path:
        raise PatchError(f"Missing file path in {line!r}.")
    return path


def _parse_chunks(body: list[str], index: int, path: str) -> tuple[list[Chunk], int]:
    chunks: list[Chunk] = []
    current: Chunk | None = None
    while index < len(body) and not _is_header(body[index]):
        line = body[index]
        if line.startswith("@@"):
            anchor = line[2:].strip()
            if current is None or current.old_lines or current.new_lines:
                current = Chunk()
                chunks.append(current)
            if anchor:
                current.anchors.append(anchor)
            index += 1
            continue
        if line.strip() == EOF_MARKER:
            if current is None:
                raise PatchError(f"'{EOF_MARKER}' before any change in {path}.")
            current.end_of_file = True
            index += 1
            continue
        if current is None:
            # Codex accepts the first chunk without an '@@' line.
            current = Chunk()
            chunks.append(current)
        if line == "" or line.startswith(" "):
            text = line[1:] if line else ""
            current.old_lines.append(text)
            current.new_lines.append(text)
        elif line.startswith("-"):
            current.old_lines.append(line[1:])
        elif line.startswith("+"):
            current.new_lines.append(line[1:])
        else:
            raise PatchError(
                f"Invalid line in the update of {path}: {line[:60]!r}. Lines must start with ' ', "
                "'-' or '+' (or '@@' to start a new chunk)."
            )
        index += 1
    chunks = [chunk for chunk in chunks if chunk.old_lines or chunk.new_lines]
    for chunk in chunks:
        if chunk.old_lines == chunk.new_lines:
            raise PatchError(f"A chunk in the update of {path} changes nothing.")
    return chunks, index


# ------------------------------------------------------------------ applying

_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        " ": " ",
        " ": " ",
        " ": " ",
        " ": " ",
        " ": " ",
    }
)


def _normalise(line: str) -> str:
    return unicodedata.normalize("NFC", line).translate(_PUNCTUATION).strip()


_COMPARATORS: tuple[tuple[int, Callable[[str], str]], ...] = (
    (0, lambda line: line),
    (1, lambda line: line.rstrip()),
    (100, lambda line: line.strip()),
    (1000, _normalise),
)


def _seek(lines: list[str], pattern: list[str], start: int, *, end_of_file: bool) -> tuple[int, int] | None:
    """Index of ``pattern`` in ``lines`` at or after ``start``, with its fuzz score."""
    if not pattern:
        return (len(lines) if end_of_file else start), 0
    if len(pattern) > len(lines):
        return None
    for fuzz, compare in _COMPARATORS:
        target = [compare(line) for line in pattern]
        candidates = (
            [len(lines) - len(pattern)]
            if end_of_file
            else range(start, len(lines) - len(pattern) + 1)
        )
        for position in candidates:
            if position < start:
                continue
            if [compare(line) for line in lines[position : position + len(pattern)]] == target:
                return position, fuzz
    return None


def apply_update(text: str, chunks: list[Chunk], *, path: str) -> tuple[str, int]:
    """Apply update chunks to ``text``; returns ``(new_text, fuzz)``."""
    eol = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith("\n") or text == ""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines.pop()
    elif text == "":
        lines = []
    cursor = 0
    total_fuzz = 0
    # (position, order, lines replaced, new lines); order keeps several
    # insertions at one position in patch order.
    replacements: list[tuple[int, int, int, list[str]]] = []
    for number, chunk in enumerate(chunks, start=1):
        anchor_line: int | None = None
        for anchor in chunk.anchors:
            found = _seek(lines, [anchor], cursor, end_of_file=False)
            if found is None:
                raise PatchError(
                    f"Chunk {number} of the update of {path}: the '@@ {anchor}' line was not found "
                    "after the previous chunk. Read the file and use a line that exists."
                )
            anchor_line = found[0]
            cursor = found[0] + 1
            total_fuzz += found[1]
        old = list(chunk.old_lines)
        new = list(chunk.new_lines)
        if not old:
            # A pure addition goes right after its '@@' anchor line; without an
            # anchor it is appended at the end of the file, as Codex does, and
            # later chunks are still located from the current position.
            if anchor_line is not None:
                replacements.append((cursor, len(replacements), 0, new))
            else:
                replacements.append((len(lines), len(replacements), 0, new))
            continue
        found = _seek(lines, old, cursor, end_of_file=chunk.end_of_file)
        if found is None and anchor_line is not None and old:
            # The context repeated the anchor line itself.
            found = _seek(lines, old, anchor_line, end_of_file=chunk.end_of_file)
        if found is None and old and old[-1] == "":
            # A trailing blank context line often stands for the end of the file.
            old, new = old[:-1], new[:-1] if new and new[-1] == "" else new
            found = _seek(lines, old, cursor, end_of_file=chunk.end_of_file)
        if found is None:
            preview = "\n".join(chunk.old_lines[:4])
            raise PatchError(
                f"Chunk {number} of the update of {path} does not match the file: these lines "
                f"were not found{' at the end of the file' if chunk.end_of_file else ''}:\n"
                f"{preview}\nRead the file and copy the context lines exactly."
            )
        position, fuzz = found
        total_fuzz += fuzz
        replacements.append((position, len(replacements), len(old), new))
        cursor = position + len(old)
    for position, _order, length, new in sorted(
        replacements, key=lambda item: (item[0], item[1]), reverse=True
    ):
        lines[position : position + length] = new
    new_text = eol.join(lines)
    if lines and trailing_newline:
        new_text += eol
    return new_text, total_fuzz


def plan_patch(
    actions: list[PatchAction],
    read_text: Callable[[str], str | None],
) -> list[PlannedFile]:
    """Resolve every action against the current files without writing anything.

    ``read_text(path)`` returns the current text, or None when the file does
    not exist.
    """
    planned: list[PlannedFile] = []
    for action in actions:
        current = read_text(action.path)
        if action.action == "add":
            if current is not None:
                raise PatchError(
                    f"{action.path} already exists; use '*** Update File:' to change it."
                )
            planned.append(PlannedFile(action.path, "add", None, action.content or ""))
            continue
        if current is None:
            raise PatchError(f"{action.path} does not exist.")
        if action.action == "delete":
            planned.append(PlannedFile(action.path, "delete", current, None))
            continue
        if action.move_to is not None and read_text(action.move_to) is not None:
            raise PatchError(f"Cannot move {action.path} to {action.move_to}: the destination exists.")
        new_text, fuzz = (
            apply_update(current, action.chunks, path=action.path)
            if action.chunks
            else (current, 0)
        )
        planned.append(
            PlannedFile(action.path, "update", current, new_text, move_to=action.move_to, fuzz=fuzz)
        )
    return planned
