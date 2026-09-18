"""Deterministic canonical JSON and SHA-256 hashing (Issue 01 surface).

The generic primitives live in :mod:`research.canonical` so every research
artifact (receipts, study protocols, ...) shares one serializer. This module
re-exports them under their historical names and keeps the receipt-specific
``content_hash`` helper that excludes a receipt's own hash field.
"""

from __future__ import annotations

from typing import Any

from research.canonical import (  # noqa: F401 - intentional re-export
    canonical_bytes,
    canonical_hash,
    canonical_json,
)

# ``content_hash`` is the only field excluded from its own digest. Keeping the
# name in one place avoids a silent drift between the builder and the verifier.
CONTENT_HASH_FIELD = "content_hash"


def _dump_without_hash(receipt: Any) -> dict[str, Any]:
    if hasattr(receipt, "model_dump"):
        data = receipt.model_dump(mode="json")
    elif isinstance(receipt, dict):
        data = dict(receipt)
    else:
        raise TypeError("receipt must be a Pydantic model or a mapping")
    data.pop(CONTENT_HASH_FIELD, None)
    return data


def receipt_content_hash(receipt_without_hash: Any) -> str:
    """Hash a receipt (model or mapping) ignoring its own ``content_hash``.

    The argument may either already have ``content_hash`` set (in which case it
    is stripped before hashing) or be a bare dict/model in its pre-hash form.
    """
    return canonical_hash(_dump_without_hash(receipt_without_hash))
