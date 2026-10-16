"""Hunks of a proposed text change, for the "Revise…" approval form.

``diff_hunks`` splits a change into the hunks a unified diff with ``context``
lines would show; ``apply_hunks`` applies only some of them to the old text.
Lines are split after each ``\\n`` with their endings kept (the numbering the
file tools and editors use), so CRLF endings and a missing final newline
survive, and both functions are deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property
from typing import Iterable

#: The most hunks a "Revise…" form lists; a change with more (or with fewer
#: than two) gets a form that asks for instructions only.
MAX_REVISE_HUNKS = 20

_LABEL_TEXT_CHARS = 60

_Opcode = tuple[str, int, int, int, int]


@dataclass(frozen=True)
class Hunk:
    """One group of nearby changed lines (context lines excluded).

    Line numbers are 1-based and inclusive: ``old_start``..``old_end`` in the
    current text (``old_end`` is ``old_start - 1`` when the hunk only inserts
    before ``old_start``) and ``new_start``..``new_end`` in the proposed text.
    ``removed``/``added`` count the changed lines.
    """

    index: int
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    removed: int
    added: int
    label: str


def diff_hunks(old: str, new: str, context: int = 3) -> list[Hunk]:
    """The hunks of the change from ``old`` to ``new``, in file order."""
    old_lines, new_lines = _split_lines(old), _split_lines(new)
    _opcodes, groups = _diff(old_lines, new_lines, context)
    return [
        _hunk(index, changes, old_lines, new_lines) for index, changes in enumerate(groups)
    ]


def apply_hunks(old: str, new: str, keep: Iterable[int], context: int = 3) -> str:
    """``old`` with only the changes of the hunks in ``keep`` applied.

    ``keep`` holds ``Hunk.index`` values of ``diff_hunks(old, new, context)``;
    other values are ignored. Keeping every hunk gives ``new``, keeping none
    gives ``old``.
    """
    old_lines, new_lines = _split_lines(old), _split_lines(new)
    opcodes, groups = _diff(old_lines, new_lines, context)
    kept = set(keep)
    owner = {change: index for index, changes in enumerate(groups) for change in changes}
    merged: list[str] = []
    for opcode in opcodes:
        tag, i1, i2, j1, j2 = opcode
        if tag != "equal" and owner.get(opcode) in kept:
            merged.extend(new_lines[j1:j2])
        else:
            merged.extend(old_lines[i1:i2])
    return "".join(merged)


@dataclass
class RevisionOffer:
    """What "Revise…" offers on one approval request.

    A single-file text change (``path`` with its previewed ``old_text`` and
    ``new_text``; ``old_text`` is None for a new file) lets the user keep some
    of its hunks; without one (commands, deletes, moves, multi-file patches)
    the form asks for instructions only. Hunks are computed on first use, so
    an approval the user simply allows or rejects costs no diff.
    """

    path: str | None = None
    old_text: str | None = None
    new_text: str | None = None

    @cached_property
    def hunks(self) -> tuple[Hunk, ...]:
        if self.path is None or self.new_text is None:
            return ()
        return tuple(diff_hunks(self.old_text or "", self.new_text))

    @property
    def selectable_hunks(self) -> tuple[Hunk, ...]:
        """The hunks the form lists: only for a change of 2 to ``MAX_REVISE_HUNKS``."""
        hunks = self.hunks
        return hunks if 2 <= len(hunks) <= MAX_REVISE_HUNKS else ()


def _split_lines(text: str) -> list[str]:
    """``text`` split after each ``\\n``, endings kept (``"".join`` restores it)."""
    parts = text.split("\n")
    tail = parts.pop()
    lines = [part + "\n" for part in parts]
    if tail:
        lines.append(tail)
    return lines


def _diff(
    old_lines: list[str], new_lines: list[str], context: int
) -> tuple[list[_Opcode], list[list[_Opcode]]]:
    """All opcodes, and the changed (non-equal) opcodes of each hunk."""
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    # get_grouped_opcodes trims the leading and trailing equal opcodes of the
    # matcher's cached list in place: take the full list first.
    opcodes = list(matcher.get_opcodes())
    groups = [
        [change for change in group if change[0] != "equal"]
        for group in matcher.get_grouped_opcodes(context)
    ]
    return opcodes, [changes for changes in groups if changes]


def _hunk(index: int, changes: list[_Opcode], old_lines: list[str], new_lines: list[str]) -> Hunk:
    first, last = changes[0], changes[-1]
    old_start, old_end = first[1] + 1, last[2]
    removed = sum(i2 - i1 for _tag, i1, i2, _j1, _j2 in changes)
    added = sum(j2 - j1 for _tag, _i1, _i2, j1, j2 in changes)
    lines = f"L{old_start}" if old_end <= old_start else f"L{old_start}–{old_end}"
    label = f"{lines} −{removed} +{added}"
    text = _first_text(
        [line for _tag, _i1, _i2, j1, j2 in changes for line in new_lines[j1:j2]]
        + [line for _tag, i1, i2, _j1, _j2 in changes for line in old_lines[i1:i2]]
    )
    if text:
        label += f" · {text}"
    return Hunk(
        index=index,
        old_start=old_start,
        old_end=old_end,
        new_start=first[3] + 1,
        new_end=last[4],
        removed=removed,
        added=added,
        label=label,
    )


def _first_text(lines: list[str]) -> str:
    """The first non-blank line (added lines come first), whitespace collapsed."""
    for line in lines:
        text = " ".join(line.split())
        if text:
            return text if len(text) <= _LABEL_TEXT_CHARS else text[: _LABEL_TEXT_CHARS - 1] + "…"
    return ""
