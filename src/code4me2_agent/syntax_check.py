"""Cheap syntax checks after an edit (Python, JSON, TOML).

The edit is always applied; a syntax error the edit introduced is attached to
the tool result so the model fixes it on its next step instead of discovering
it much later. Files that were already broken, or that use syntax this parser
does not know (a newer Python, JSON with comments), are left alone: only a
transition from parsing to not parsing is reported.
"""

from __future__ import annotations

import ast
import json
import sys
import warnings
from dataclasses import dataclass

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - the bundle ships 3.13
    tomllib = None  # type: ignore[assignment]

_MAX_CHECK_CHARS = 2 * 1024 * 1024


@dataclass(frozen=True)
class SyntaxProblem:
    language: str
    line: int | None
    message: str

    def as_result(self) -> dict[str, object]:
        return {"language": self.language, "line": self.line, "message": self.message}


def _language(path: str) -> str | None:
    lowered = path.lower()
    if lowered.endswith((".py", ".pyi")):
        return "python"
    if lowered.endswith(".json"):
        return "json"
    if lowered.endswith(".toml"):
        return "toml"
    return None


def _problem(language: str, text: str) -> SyntaxProblem | None:
    if language == "python":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse(text)
        except SyntaxError as exc:
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            return SyntaxProblem(
                "python",
                exc.lineno,
                f"{exc.msg} (Python {version} parser)",
            )
        except (ValueError, RecursionError, MemoryError):
            return None
        return None
    if language == "json":
        if not text.strip():
            return None
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            return SyntaxProblem("json", exc.lineno, exc.msg)
        except RecursionError:
            return None
        return None
    if language == "toml":
        if tomllib is None:
            return None
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            line = getattr(exc, "lineno", None)
            message = getattr(exc, "msg", None) or str(exc)
            return SyntaxProblem("toml", line, message)
        return None
    return None


def introduced_syntax_problem(path: str, before: str | None, after: str) -> SyntaxProblem | None:
    """The syntax problem ``after`` has that ``before`` did not, if any.

    ``before=None`` means a new file: any problem is new.
    """
    language = _language(path)
    if language is None or len(after) > _MAX_CHECK_CHARS:
        return None
    problem = _problem(language, after)
    if problem is None:
        return None
    if before is not None and len(before) <= _MAX_CHECK_CHARS and _problem(language, before) is not None:
        return None
    return problem
