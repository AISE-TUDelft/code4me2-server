"""Locating ``old_text`` for replace_text / edit_file.

Models copy snippets imperfectly: trailing spaces, a wrong base indentation or
a slightly misremembered middle line make an exact search fail and cost a
retry. The chain below tries progressively looser comparisons, but every
strategy must still find exactly one place; a looser strategy is never used to
break a tie that a stricter one found. The strategy that matched is reported so
the caller can surface it (and the diff card shows the exact result).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

EXACT = "exact"
LINE_ENDINGS = "line_endings"
TRAILING_WHITESPACE = "trailing_whitespace"
INDENTATION = "indentation"
WHITESPACE = "whitespace"
BLOCK_ANCHOR = "block_anchor"

FUZZY_STRATEGIES = frozenset({TRAILING_WHITESPACE, INDENTATION, WHITESPACE, BLOCK_ANCHOR})

# Block-anchor matching: minimum lines, allowed line-count drift and the
# similarity the lines between the anchors must reach.
_BLOCK_MIN_LINES = 3
_BLOCK_SIMILARITY = 0.75


@dataclass(frozen=True)
class EditMatch:
    start: int
    end: int
    replacement: str
    strategy: str
    line: int


@dataclass(frozen=True)
class MatchResult:
    matches: tuple[EditMatch, ...]
    strategy: str | None
    # 1-based lines of every candidate when the search was ambiguous.
    candidate_lines: tuple[int, ...] = ()

    @property
    def count(self) -> int:
        return len(self.matches)


def find_matches(text: str, old_text: str, new_text: str, *, replace_all: bool = False) -> MatchResult:
    """Return the matches of the first strategy that finds any.

    With ``replace_all`` only exact and line-ending matching are used: a fuzzy
    comparison applied everywhere could rewrite unrelated code.
    """
    exact = _exact(text, old_text, new_text, EXACT)
    if exact:
        return MatchResult(exact, EXACT, tuple(match.line for match in exact))
    converted = _convert_line_endings(text, old_text, new_text)
    if converted is not None:
        matches = _exact(text, converted[0], converted[1], LINE_ENDINGS)
        if matches:
            return MatchResult(matches, LINE_ENDINGS, tuple(match.line for match in matches))
    if replace_all:
        return MatchResult((), None)
    lines = _Lines(text)
    old = _SnippetLines(old_text)
    if not old.lines or all(not line.strip() for line in old.lines):
        return MatchResult((), None)
    new = _SnippetLines(new_text)
    for strategy in (TRAILING_WHITESPACE, INDENTATION, WHITESPACE, BLOCK_ANCHOR):
        windows = _line_windows(lines, old, strategy)
        if not windows:
            continue
        matches = tuple(
            _line_match(lines, old, new, first, last, strategy) for first, last in windows
        )
        return MatchResult(matches, strategy, tuple(first + 1 for first, _last in windows))
    return MatchResult((), None)


def first_line_hint(text: str, old_text: str) -> str | None:
    """Where the first meaningful line of ``old_text`` occurs, for a no-match hint."""
    first = next((line.strip() for line in old_text.splitlines() if line.strip()), "")
    if len(first) < 4:
        return None
    hits = [index + 1 for index, line in enumerate(text.splitlines()) if line.strip() == first]
    if not hits:
        return None
    shown = ", ".join(str(hit) for hit in hits[:5])
    more = f" (and {len(hits) - 5} more)" if len(hits) > 5 else ""
    return f"Its first line appears at line {shown}{more}; read that region and copy the text exactly."


# --------------------------------------------------------------- exact search


def _exact(text: str, old_text: str, new_text: str, strategy: str) -> tuple[EditMatch, ...]:
    matches: list[EditMatch] = []
    position = text.find(old_text)
    while position != -1:
        matches.append(
            EditMatch(
                start=position,
                end=position + len(old_text),
                replacement=new_text,
                strategy=strategy,
                line=text.count("\n", 0, position) + 1,
            )
        )
        position = text.find(old_text, position + len(old_text))
    return tuple(matches)


def _convert_line_endings(text: str, old_text: str, new_text: str) -> tuple[str, str] | None:
    file_crlf = "\r\n" in text
    old_crlf = "\r\n" in old_text
    if file_crlf and not old_crlf and "\n" in old_text:
        return old_text.replace("\n", "\r\n"), new_text.replace("\r\n", "\n").replace("\n", "\r\n")
    if not file_crlf and old_crlf:
        return old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
    return None


# ---------------------------------------------------------- line strategies


class _Lines:
    """The file split into lines with their character offsets."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines: list[str] = []
        self.starts: list[int] = []
        self.ends: list[int] = []  # end of line content, before "\r\n" / "\n"
        self.eols: list[str] = []
        position = 0
        for raw in text.splitlines(keepends=True):
            content = raw.rstrip("\r\n")
            self.starts.append(position)
            self.ends.append(position + len(content))
            self.eols.append(raw[len(content):])
            self.lines.append(content)
            position += len(raw)

    def eol(self) -> str:
        for eol in self.eols:
            if eol:
                return eol
        return "\n"


class _SnippetLines:
    def __init__(self, snippet: str) -> None:
        normalized = snippet.replace("\r\n", "\n")
        self.trailing_newline = normalized.endswith("\n")
        body = normalized[:-1] if self.trailing_newline else normalized
        self.lines = body.split("\n") if body else []


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _common_indent(lines: list[str]) -> str:
    indents = [_indent(line) for line in lines if line.strip()]
    if not indents:
        return ""
    common = indents[0]
    for indent in indents[1:]:
        while not indent.startswith(common):
            common = common[:-1]
    return common


def _dedented(lines: list[str]) -> list[str]:
    common = _common_indent(lines)
    return [line[len(common):].rstrip() if line.strip() else "" for line in lines]


def _collapsed(line: str) -> str:
    return " ".join(line.split())


def _line_windows(lines: _Lines, old: _SnippetLines, strategy: str) -> list[tuple[int, int]]:
    count = len(old.lines)
    total = len(lines.lines)
    windows: list[tuple[int, int]] = []
    if strategy == BLOCK_ANCHOR:
        if count < _BLOCK_MIN_LINES:
            return []
        first_anchor = old.lines[0].strip()
        last_anchor = old.lines[-1].strip()
        if not first_anchor or not last_anchor:
            return []
        tolerance = max(1, count // 4)
        old_middle = "\n".join(line.strip() for line in old.lines[1:-1])
        for first in range(total):
            if lines.lines[first].strip() != first_anchor:
                continue
            best: tuple[float, int] | None = None
            for length in range(max(2, count - tolerance), count + tolerance + 1):
                last = first + length - 1
                if last >= total or lines.lines[last].strip() != last_anchor:
                    continue
                middle = "\n".join(line.strip() for line in lines.lines[first + 1 : last])
                ratio = SequenceMatcher(None, old_middle, middle, autojunk=False).ratio()
                if ratio >= _BLOCK_SIMILARITY and (best is None or ratio > best[0]):
                    best = (ratio, last)
            if best is not None:
                windows.append((first, best[1]))
        return windows
    if count == 0 or count > total:
        return []
    if strategy == TRAILING_WHITESPACE:
        target = [line.rstrip() for line in old.lines]
        compare = lambda window: [line.rstrip() for line in window]  # noqa: E731
    elif strategy == INDENTATION:
        target = _dedented(old.lines)
        compare = _dedented
    elif strategy == WHITESPACE:
        target = [_collapsed(line) for line in old.lines]
        compare = lambda window: [_collapsed(line) for line in window]  # noqa: E731
    else:  # pragma: no cover - guarded by the caller
        return []
    first = 0
    while first + count <= total:
        if compare(lines.lines[first : first + count]) == target:
            windows.append((first, first + count - 1))
            first += count
        else:
            first += 1
    return windows


def _line_match(
    lines: _Lines,
    old: _SnippetLines,
    new: _SnippetLines,
    first: int,
    last: int,
    strategy: str,
) -> EditMatch:
    matched = lines.lines[first : last + 1]
    new_lines = list(new.lines)
    if strategy in (INDENTATION, WHITESPACE):
        new_lines = _adapt_indentation(new_lines, old.lines, matched)
    elif strategy == BLOCK_ANCHOR:
        new_lines = _reindent(new_lines, from_indent=_common_indent(old.lines), to_indent=_common_indent(matched))
    eol = lines.eols[first] or lines.eol()
    replacement = eol.join(new_lines)
    if new.trailing_newline and not old.trailing_newline:
        replacement += eol
    return EditMatch(
        start=lines.starts[first],
        end=lines.ends[last],
        replacement=replacement,
        strategy=strategy,
        line=first + 1,
    )


def _adapt_indentation(new_lines: list[str], old_lines: list[str], matched: list[str]) -> list[str]:
    """Carry the file's indentation over to ``new_text``.

    The snippet's lines and the matched lines pair up one to one, which gives a
    map from each indentation the model used to the one the file uses. Lines
    of new_text with a mapped indentation take the file's; other lines follow
    the pattern the map shows: a constant shift (wrong base indentation) or a
    constant width ratio (two-space snippet in a four-space file).
    """
    mapping: dict[str, str] = {}
    for old_line, file_line in zip(old_lines, matched):
        if not old_line.strip() or not file_line.strip():
            continue
        source, target = _indent(old_line), _indent(file_line)
        if mapping.setdefault(source, target) != target:
            return _reindent(
                new_lines, from_indent=_common_indent(old_lines), to_indent=_common_indent(matched)
            )
    new_indents = {_indent(line) for line in new_lines if line.strip()}
    if not new_indents <= set(mapping) and new_indents <= set(mapping.values()):
        # new_text is already written in the file's indentation.
        return list(new_lines)
    from_base, to_base = _common_indent(old_lines), _common_indent(matched)
    is_shift = all(
        source.startswith(from_base) and target == to_base + source[len(from_base):]
        for source, target in mapping.items()
    )
    ratio = None if is_shift else _space_ratio(mapping)
    adapted: list[str] = []
    for line in new_lines:
        if not line.strip():
            adapted.append("")
            continue
        indent = _indent(line)
        body = line[len(indent):]
        if indent in mapping:
            adapted.append(mapping[indent] + body)
        elif ratio is not None and set(indent) <= {" "}:
            width = len(indent) * ratio
            adapted.append((" " * int(width) if width == int(width) else indent) + body)
        elif indent.startswith(from_base):
            adapted.append(to_base + indent[len(from_base):] + body)
        else:
            adapted.append(line)
    return adapted


def _space_ratio(mapping: dict[str, str]) -> float | None:
    ratios = {
        len(target) / len(source)
        for source, target in mapping.items()
        if source and set(source) <= {" "} and set(target) <= {" "}
    }
    if len(ratios) != 1:
        return None
    if any(bool(source) != bool(target) for source, target in mapping.items()):
        return None
    return ratios.pop()


def _reindent(lines: list[str], *, from_indent: str, to_indent: str) -> list[str]:
    if from_indent == to_indent:
        return lines
    result: list[str] = []
    for line in lines:
        if not line.strip():
            result.append("")
        elif line.startswith(from_indent):
            result.append(to_indent + line[len(from_indent):])
        else:
            # Shallower than the snippet's base: keep its relative shape.
            result.append(to_indent + line.lstrip())
    return result


_LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\|")


def has_line_number_prefix(snippet: str) -> bool:
    return bool(_LINE_NUMBER_PREFIX_RE.match(snippet))
