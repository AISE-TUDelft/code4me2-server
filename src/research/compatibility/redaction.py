"""Recursive, non-mutating secret redaction.

Two layers of defence:

1. **Key-based** - any mapping key whose lower-cased name contains a known
   secret marker (``password``, ``token``, ``api_key`` ...) has its whole value
   replaced by ``[REDACTED]``.
2. **Value-based** - string scalars are scanned for well-known inline secret
   shapes (``Bearer <token>``, ``sk-...``, ``ghp_...``, ``AKIA...``) even when
   they appear under an innocuous key.

The input is never mutated: only fresh dicts/lists/strings are returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

REDACTED = "[REDACTED]"

# Case-insensitive substrings that mark a key as secret-bearing.
SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "private_key",
    "access_key",
    "client_secret",
    "cookie",
    "session_token",
)

# Inline value shapes. The bearer case preserves the scheme for readability.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{4,}"), "Bearer " + REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bghp_[A-Za-z0-9]{8,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), REDACTED),
)


@dataclass
class RedactionResult:
    """A redacted copy plus counts of what was removed.

    ``value`` is a structurally independent copy; the original object passed to
    :func:`redact` is untouched.
    """

    value: Any
    key_redactions: int = 0
    value_redactions: int = 0
    paths: list[str] = field(default_factory=list)

    @property
    def redaction_count(self) -> int:
        return self.key_redactions + self.value_redactions

    @property
    def changed(self) -> bool:
        return self.redaction_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_redactions": self.key_redactions,
            "value_redactions": self.value_redactions,
            "redaction_count": self.redaction_count,
            "paths": list(self.paths),
        }


def is_secret_key(key: Any) -> bool:
    """True when a mapping key name marks its value as secret material."""
    lowered = str(key).lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


def redact_string(value: str) -> tuple[str, int]:
    """Return ``(redacted, count)`` for inline secret shapes in one string."""
    result = value
    count = 0
    for pattern, replacement in _VALUE_PATTERNS:
        result, substitutions = pattern.subn(replacement, result)
        count += substitutions
    return result, count


def redact(value: Any) -> RedactionResult:
    """Recursively redact ``value`` and return a :class:`RedactionResult`."""
    result = RedactionResult(value=None)
    result.value = _walk(value, result, path="$")
    return result


def _walk(value: Any, result: RedactionResult, path: str) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if is_secret_key(key):
                redacted[key] = REDACTED
                result.key_redactions += 1
                result.paths.append(child_path)
            else:
                redacted[key] = _walk(item, result, child_path)
        return redacted
    if isinstance(value, (list, tuple)):
        return [
            _walk(item, result, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        redacted_value, count = redact_string(value)
        if count:
            result.value_redactions += count
            result.paths.append(path)
        return redacted_value
    return value
