"""Privacy filter for canonical telemetry (Issue 06).

Public surface:

* :mod:`research.telemetry.privacy.classify` - the field-classification
  taxonomy and hard ``SECRET`` denylist.
* :mod:`research.telemetry.privacy.engine` - :class:`PrivacyPolicy`,
  :func:`filter_event` and :func:`filter_payload`, which enforce data
  minimization before any spool/log/retry/export.
"""

from .classify import (
    BEHAVIORAL_TOKENS,
    CODE_METADATA_TOKENS,
    CONTENT_TOKENS,
    SECRET_KEY_PATTERNS,
    SECRET_VALUE_PATTERNS,
    SYSTEM_TOKENS,
    classify_event_payload,
    classify_field,
    contains_secret_value,
    is_secret_key,
    looks_secret_value,
)
from .engine import (
    REDACTED_MARKER,
    FilterResult,
    PrivacyPolicy,
    filter_event,
    filter_payload,
)

__all__ = [
    "BEHAVIORAL_TOKENS",
    "CODE_METADATA_TOKENS",
    "CONTENT_TOKENS",
    "FilterResult",
    "PrivacyPolicy",
    "REDACTED_MARKER",
    "SECRET_KEY_PATTERNS",
    "SECRET_VALUE_PATTERNS",
    "SYSTEM_TOKENS",
    "classify_event_payload",
    "classify_field",
    "contains_secret_value",
    "filter_event",
    "filter_payload",
    "is_secret_key",
    "looks_secret_value",
]
