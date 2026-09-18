"""Canonical telemetry schema and privacy filter (Issue 06).

One source-neutral event vocabulary that preserves provenance and enforces data
minimization before durable storage.

Public surface:

* :mod:`research.telemetry.enums` - canonical event types, sources, field
  classes, policy actions, coverage states, canonical fidelity and lifecycle.
* :mod:`research.telemetry.models` - the immutable ``CanonicalEventV1``
  envelope and its sub-objects.
* :mod:`research.telemetry.builder` - event construction with a fresh id and a
  strictly-increasing per-emitter sequence.
* :mod:`research.telemetry.validation` - typed envelope validation.
* :mod:`research.telemetry.privacy` - field classification and the redaction
  engine that runs before any spool/log/retry/export.
* :mod:`research.telemetry.normalization` - ACP/IDE/legacy source normalizers
  and the optional adapter enrichment interface.
* :mod:`research.telemetry.schema` - the language-neutral JSON Schema.

The package never imports ``App``, FastAPI, or SQLAlchemy; it is the shared
contract that Issues 07-12 consume.
"""

from .builder import EventBuilder, SequenceAllocator, build_event
from .enums import (
    CanonicalEventType,
    CanonicalFidelity,
    CoverageState,
    EventSource,
    FieldClass,
    LifecycleState,
    PolicyAction,
)
from .models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    PrivacySummary,
    Provenance,
)
from .privacy import PrivacyPolicy, filter_event, filter_payload
from .validation import (
    TelemetryValidationCode,
    TelemetryValidationError,
    validate_canonical_event,
)

__all__ = [
    "CanonicalEventType",
    "CanonicalEventV1",
    "CanonicalFidelity",
    "Correlations",
    "Coverage",
    "CoverageState",
    "EventBuilder",
    "EventMetrics",
    "EventSource",
    "FieldClass",
    "LifecycleState",
    "PolicyAction",
    "PrivacyPolicy",
    "PrivacySummary",
    "Provenance",
    "SequenceAllocator",
    "TelemetryValidationCode",
    "TelemetryValidationError",
    "build_event",
    "filter_event",
    "filter_payload",
    "validate_canonical_event",
]
