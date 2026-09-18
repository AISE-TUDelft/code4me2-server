"""Versioned study protocol and immutable publication (Issue 02).

Public surface:

* :mod:`research.study.protocol.enums` - closed vocabularies for revision status,
  schedule kind, assignment unit/strategy, retention action, telemetry field
  classes, completion policy, validation reason codes, release-resolution
  statuses and publication outcomes.
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
* :mod:`research.study.protocol.publication` - immutable revision creation, revision
  lineage, optimistic-concurrency conflicts, retirement and audit records.

The persistence adapters live in :mod:`research.study.protocol.store` and take a
caller-supplied SQLAlchemy ``Session`` so this package stays free of any
application/App import.
"""

from .canonical import protocol_canonical_json, protocol_digest
from .enums import (
    AssignmentStrategy,
    AssignmentUnit,
    CompletionPolicyKind,
    PublicationOutcome,
    ReleaseResolutionStatus,
    RetentionAction,
    RevisionStatus,
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
from .publication import (
    AuditRecord,
    ImmutableRevisionError,
    PublicationResult,
    RetirementResult,
    RevisionConflict,
    RevisionLineage,
    StudyRevision,
    assert_revision_mutable,
    lineage_from_revisions,
    publish_revision,
    retire_revision,
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
    "AuditRecord",
    "CompletionPolicyKind",
    "DistributionResolution",
    "DistributionResolver",
    "ExplicitUnknown",
    "FixedSchedule",
    "ImmutableRevisionError",
    "NullDistributionResolver",
    "NullReleaseResolver",
    "ProtocolValidationError",
    "PublicationOutcome",
    "PublicationResult",
    "ReleaseResolution",
    "ReleaseResolutionStatus",
    "ReleaseResolver",
    "ResolvedDistribution",
    "RetentionAction",
    "RevisionConflict",
    "RevisionLineage",
    "RevisionStatus",
    "RetirementResult",
    "RollingSchedule",
    "ScheduleKind",
    "StudyProtocolV1",
    "StudyRevision",
    "TelemetryFieldClass",
    "ValidationError",
    "ValidationReasonCode",
    "ValidationSeverity",
    "assert_revision_mutable",
    "blocking_errors",
    "freeze_protocol_distributions",
    "is_publishable",
    "lineage_from_revisions",
    "normalized_condition_weights",
    "protocol_canonical_json",
    "protocol_digest",
    "publish_revision",
    "retire_revision",
    "validate_protocol",
    "warnings",
]
