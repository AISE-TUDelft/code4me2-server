"""Research study configuration validation and canonicalization.

Public surface:

* :mod:`research.study.protocol.enums` - closed vocabularies for configuration,
  retention, telemetry and validation.
* :mod:`research.study.protocol.models` - Pydantic v2 contracts for
  ``StudyProtocolV1`` and its versioned sub-objects, including the explicit
  :class:`~research.study.protocol.models.ExplicitUnknown` state.
* :mod:`research.study.protocol.canonical` - deterministic, order-normalized JSON and
  SHA-256 digest of a validated protocol (reusing the Issue 01 canonical
  helpers).
* :mod:`research.study.protocol.validation` - typed field-level, cross-reference and
  document-safety validation, plus the injected
  :class:`~research.study.protocol.validation.ReleaseResolver` interface and its
  :class:`~research.study.protocol.validation.NullReleaseResolver` default.
The persistence adapters live in :mod:`research.study.protocol.store` and take a
caller-supplied SQLAlchemy ``Session`` so this package stays free of any
application/App import.
"""

from .canonical import protocol_canonical_json, protocol_digest
from .enums import (
    AssignmentStrategy,
    AssignmentUnit,
    CompletionPolicyKind,
    ReleaseResolutionStatus,
    RetentionAction,
    ScheduleKind,
    TelemetryFieldClass,
    ValidationReasonCode,
    ValidationSeverity,
)
from .models import (
    ExplicitUnknown,
    FixedSchedule,
    ResolvedDistribution,
    RollingSchedule,
    StudyProtocolV1,
)
from .validation import (
    DistributionResolution,
    DistributionResolver,
    NullDistributionResolver,
    NullReleaseResolver,
    ProtocolValidationError,
    ReleaseResolution,
    ReleaseResolver,
    ValidationError,
    blocking_errors,
    freeze_protocol_distributions,
    is_publishable,
    normalized_condition_weights,
    validate_protocol,
    warnings,
)

__all__ = [
    "AssignmentStrategy",
    "AssignmentUnit",
    "CompletionPolicyKind",
    "DistributionResolution",
    "DistributionResolver",
    "ExplicitUnknown",
    "FixedSchedule",
    "NullDistributionResolver",
    "NullReleaseResolver",
    "ProtocolValidationError",
    "ReleaseResolution",
    "ReleaseResolutionStatus",
    "ReleaseResolver",
    "ResolvedDistribution",
    "RetentionAction",
    "RollingSchedule",
    "ScheduleKind",
    "StudyProtocolV1",
    "TelemetryFieldClass",
    "ValidationError",
    "ValidationReasonCode",
    "ValidationSeverity",
    "blocking_errors",
    "freeze_protocol_distributions",
    "is_publishable",
    "normalized_condition_weights",
    "protocol_canonical_json",
    "protocol_digest",
    "validate_protocol",
    "warnings",
]
