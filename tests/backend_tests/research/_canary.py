"""Test-only leak-canary helpers for researcher projections and exports.

The leakage scanner and the export builder are exercised only by tests, so they
live here rather than in the production ``research.participants`` package. The
behavior is unchanged: ``researcher_export`` delegates to the production
``researcher_projection``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from research.participants import identity as identity_module

if TYPE_CHECKING:
    from research.participants.models import Enrollment

# Fields that must never appear in a researcher projection or export.
_FORBIDDEN_PROJECTION_FIELDS = frozenset(
    {
        "account_id",
        "email",
        "session_token",
        "session_id",
        "user_id",
        "participant_id",
        "oauth_subject",
    }
)


def researcher_export(enrollments: Sequence[Enrollment]) -> list[dict[str, Any]]:
    """Build the researcher export rows for ``enrollments`` (no login identity)."""
    return [identity_module.researcher_projection(enrollment) for enrollment in enrollments]


def leak_scan(obj: Any, forbidden_values: Iterable[Any]) -> list[str]:
    """Return the forbidden values found in the serialized form of ``obj``.

    An empty list means the projection/export is clean. The comparison is exact
    substring matching over canonical JSON, so a canary account UUID, email or
    session token is detected wherever it appears.
    """
    serialized = json.dumps(obj, sort_keys=True, default=str)
    leaks: list[str] = []
    for value in forbidden_values:
        if value is None:
            continue
        text = str(value)
        if text and text in serialized:
            leaks.append(text)
    return leaks


def has_forbidden_field(obj: Any) -> bool:
    """Recursively detect a forbidden projection field name in ``obj``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower() in _FORBIDDEN_PROJECTION_FIELDS:
                return True
            if has_forbidden_field(value):
                return True
    elif isinstance(obj, (list, tuple)):
        return any(has_forbidden_field(item) for item in obj)
    return False
