"""Field classification for the canonical privacy filter.

Classification is deterministic and fail-closed:

1. A key matching the hard secret denylist, or a value matching a well-known
   secret shape, is ``SECRET``. ``SECRET`` is never persistable regardless of
   policy or consent.
2. Keys naming free content (prompts, reasoning, diffs, arguments, raw output)
   are ``CONTENT``.
3. Keys naming file/code metadata are ``CODE_METADATA``.
4. Keys naming timing/process/schema facts are ``SYSTEM``.
5. Keys naming tool/permission/message lifecycle facts are ``BEHAVIORAL``.
6. Anything unrecognized is treated as ``CONTENT`` (fail closed): it is removed
   unless content capture is explicitly allowed. Unrecognized *containers* are
   traversed so their children can be classified individually.

The classifier is vendor-independent and never invents a class from an ACP or
IDE method name.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..enums import FieldClass

__all__ = [
    "CONTENT_TOKENS",
    "CODE_METADATA_TOKENS",
    "SECRET_KEY_PATTERNS",
    "SECRET_VALUE_PATTERNS",
    "SYSTEM_TOKENS",
    "BEHAVIORAL_TOKENS",
    "classify_event_payload",
    "classify_field",
    "contains_secret_value",
    "is_secret_key",
    "looks_secret_value",
]

# Keys that are always secret, matched on a normalized (lowercased,
# underscored) key. ``tokens`` (a count) deliberately does NOT match ``token``.
SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
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
    "auth_token",
)
_SECRET_KEY_EXACT = frozenset(
    {"token", "auth", "authorization", "secret", "password", "credential"}
)

SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

CONTENT_TOKENS = frozenset(
    {
        "content",
        "prompt",
        "prompts",
        "reasoning",
        "thought",
        "thoughts",
        "completion",
        "completions",
        "response",
        "output",
        "stdout",
        "stderr",
        "diff",
        "snippet",
        "transcript",
        "body",
        "text",
        "raw",
        "payload",
        "arguments",
        "cot",
        "chain",
        "message_text",
    }
)
CODE_METADATA_TOKENS = frozenset(
    {
        "path",
        "file",
        "filename",
        "extension",
        "language",
        "line",
        "lines",
        "symbol",
        "uri",
        "directory",
        "module",
        "package",
        "repo",
        "workspace",
        "document",
    }
)
SYSTEM_TOKENS = frozenset(
    {
        "duration",
        "latency",
        "timestamp",
        "clock",
        "process",
        "pid",
        "version",
        "schema",
        "emitter",
        "sequence",
        "count",
        "counts",
        "size",
        "bytes",
        "host",
        "os",
        "arch",
        "trace",
        "span",
        "level",
        "tokens",
        "tokens_used",
    }
)
BEHAVIORAL_TOKENS = frozenset(
    {
        "tool",
        "permission",
        "turn",
        "message",
        "event",
        "action",
        "decision",
        "status",
        "state",
        "role",
        "model",
        "result",
        "outcome",
        "error",
        "name",
        "id",
        "type",
        "call",
        "edit",
        "session",
        "run",
        "agent",
        "capability",
        "fidelity",
        "kind",
        "reason",
        "method",
    }
)


def _normalize_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _tokens(name: Any) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", str(name).lower()) if token}


def is_secret_key(name: Any) -> bool:
    """True when a mapping key names secret material."""
    normalized = _normalize_key(name)
    if normalized in _SECRET_KEY_EXACT:
        return True
    if normalized.endswith(("_token", "_secret", "_password")):
        return True
    return any(pattern in normalized for pattern in SECRET_KEY_PATTERNS)


def looks_secret_value(value: Any) -> bool:
    """True when a string value matches a well-known secret shape."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def contains_secret_value(value: Any) -> bool:
    """Recursively detect a secret-shaped value anywhere in ``value``."""
    if isinstance(value, Mapping):
        return any(
            is_secret_key(key) or contains_secret_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret_value(item) for item in value)
    return looks_secret_value(value)


def classify_field(name: Any, value: Any = None) -> FieldClass:
    """Classify a single field by name (and value for secret shapes)."""
    if is_secret_key(name) or looks_secret_value(value):
        return FieldClass.SECRET

    tokens = _tokens(name)
    if tokens & CONTENT_TOKENS:
        return FieldClass.CONTENT
    if tokens & CODE_METADATA_TOKENS:
        return FieldClass.CODE_METADATA
    if tokens & SYSTEM_TOKENS:
        return FieldClass.SYSTEM
    if tokens & BEHAVIORAL_TOKENS:
        return FieldClass.BEHAVIORAL

    if isinstance(value, (Mapping, list, tuple)):
        # Unrecognized containers are traversed; children are classified.
        return FieldClass.SYSTEM
    return FieldClass.CONTENT


def classify_event_payload(payload: Any, prefix: str = "") -> dict[str, FieldClass]:
    """Flatten a sanitized payload into ``{dotted_path: FieldClass}``."""
    classified: dict[str, FieldClass] = {}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            classified[path] = classify_field(key, value)
            if isinstance(value, (Mapping, list, tuple)):
                classified.update(classify_event_payload(value, path))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            path = f"{prefix}[{index}]"
            classified[path] = classify_field(prefix, value)
            if isinstance(value, (Mapping, list, tuple)):
                classified.update(classify_event_payload(value, path))
    return classified
