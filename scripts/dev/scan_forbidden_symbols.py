#!/usr/bin/env python3
"""Forbidden-symbol scan for the research cleanup boundary (Task 06 §13, Task 08 §6).

Scans active research source (never build output, caches, virtualenvs or
generated clients) for revision/condition concepts that the study-lifecycle
redesign removed. The scan fails when a hit is not covered by the allowlist, so
every remaining match must be an explicitly reviewed exception.

Usage (from the repository root or anywhere):

    python scripts/dev/scan_forbidden_symbols.py [--workspace /path/to/Code4Me_Paper]

Exit codes: 0 = clean or allowlisted, 1 = unallowlisted hits, 2 = scan error.

Allowlist format (``scripts/dev/forbidden_symbols.txt``): one entry per line,
``<repo-relative path prefix> # <reason>``. Blank lines and ``#`` comments are
ignored. A hit matches when the file's repo-relative path equals the entry or
starts with it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

PATTERNS = (
    "study_revision_id",
    "StudyRevision",
    "supersede_revision",
    "publish_draft",
    "condition_exposure",
    "condition_id",
)

SKIP_DIRS = {
    "build",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".gradle",
    "generated",
    "dist",
    ".git",
}

SCAN_ROOTS = (
    "code4me2-server/src",
    "code4me2/src/main",
    "code4me2/telemetry-acp-proxy/telemetry_acp_proxy",
)

ALLOWLIST = pathlib.Path(__file__).with_name("forbidden_symbols.txt")


def _parse_allowlist(path: pathlib.Path) -> list[tuple[str, frozenset[str] | None, str]]:
    """Return ``(path_prefix, symbols|None, reason)`` entries.

    Format: ``<repo-relative path prefix> | <symbol[,symbol...]> # reason``.
    Omitting the symbol list (no ``|``) covers every scanned symbol for that path.
    A per-symbol entry is preferred: it cannot hide a *different* forbidden
    concept reappearing in the same file.
    """
    entries: list[tuple[str, frozenset[str] | None, str]] = []
    if not path.exists():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, reason = line.partition("#")
        prefix, _, symbols = body.partition("|")
        allowed = (
            frozenset(part.strip() for part in symbols.split(",") if part.strip())
            if symbols.strip()
            else None
        )
        entries.append((prefix.strip(), allowed, reason.strip()))
    return entries


def _iter_files(root: pathlib.Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".class", ".jar", ".png", ".jpg"}:
            continue
        yield path


def _allowed(relative: str, symbol: str, entries) -> str | None:
    for prefix, symbols, reason in entries:
        if not (relative == prefix or relative.startswith(prefix.rstrip("/") + "/")):
            continue
        if symbols is not None and symbol not in symbols:
            continue
        return reason or "allowlisted"
    return None


def scan(workspace: pathlib.Path) -> int:
    entries = _parse_allowlist(ALLOWLIST)
    matcher = re.compile("|".join(re.escape(pattern) for pattern in PATTERNS))
    unallowlisted: list[str] = []
    allowed_hits: list[str] = []

    missing_roots = [root for root in SCAN_ROOTS if not (workspace / root).exists()]
    if missing_roots:
        print(
            "scan error: scan root(s) missing (fail closed): "
            + ", ".join(missing_roots),
            file=sys.stderr,
        )
        return 2

    for scan_root in SCAN_ROOTS:
        root = workspace / scan_root
        for path in _iter_files(root):
            relative = path.relative_to(workspace).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as error:  # pragma: no cover - unreadable file
                print(f"scan error: {relative}: {error}", file=sys.stderr)
                return 2
            for number, line in enumerate(text.splitlines(), start=1):
                match = matcher.search(line)
                if not match:
                    continue
                hit = f"{relative}:{number}: {match.group(0)}: {line.strip()[:160]}"
                reason = _allowed(relative, match.group(0), entries)
                if reason is None:
                    unallowlisted.append(hit)
                else:
                    allowed_hits.append(f"{hit}  [allowlisted: {reason}]")

    for hit in allowed_hits:
        print(f"ALLOWED  {hit}")
    for hit in unallowlisted:
        print(f"FORBIDDEN {hit}", file=sys.stderr)

    if unallowlisted:
        print(
            f"\n{len(unallowlisted)} forbidden hit(s) outside the allowlist "
            f"({ALLOWLIST.name})",
            file=sys.stderr,
        )
        return 1

    print(f"\nclean: {len(allowed_hits)} allowlisted hit(s), 0 forbidden hits")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(pathlib.Path(__file__).resolve().parents[2].parent),
        help="workspace root containing code4me2/ and code4me2-server/",
    )
    args = parser.parse_args()
    return scan(pathlib.Path(args.workspace).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
