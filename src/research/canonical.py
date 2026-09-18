"""Deterministic canonical JSON and SHA-256 hashing (shared).

Equivalent documents must hash identically regardless of dictionary insertion
order or non-semantic whitespace. We achieve that by always serializing with
``sort_keys=True`` and compact separators over the *parsed* structure, so an
input string with incidental whitespace collapses to the same canonical bytes.

This module holds the backend-agnostic primitives shared by every research
artifact (Issue 01 receipts, Issue 02 study protocols, and later issues). The
compatibility package re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any


def _json_default(value: Any) -> Any:
    """Coerce common non-JSON types to a stable representation."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        # Pydantic models nested inside plain dicts.
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not canonical-JSON serializable")


def canonical_json(obj: Any) -> str:
    """Return the canonical JSON string for ``obj`` (sorted keys, compact)."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def canonical_bytes(obj: Any) -> bytes:
    """UTF-8 encoded :func:`canonical_json`."""
    return canonical_json(obj).encode("utf-8")


def canonical_hash(obj: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_bytes`."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
