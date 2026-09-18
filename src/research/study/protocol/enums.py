"""Closed vocabularies for the versioned study protocol (Issue 02).

Every value here is part of a persisted contract (draft JSON, published
revision JSON, API responses), so members are additive only: renaming or
removing a member is a breaking change to already-stored revisions.
"""

from __future__ import annotations

from enum import Enum


class SchemaVersion(str, Enum):
    """Supported ``StudyProtocolV1.schema_version`` values."""

    V1 = "1"


class RevisionStatus(str, Enum):
    """Lifecycle of a study revision.

    ``PUBLISHED`` is immutable in content; only ``RETIRED`` is a permitted
    lifecycle transition, and it never changes the stored protocol bytes.
    """

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class ScheduleKind(str, Enum):
    """Whether a study runs to a fixed calendar window or a rolling duration."""

    FIXED = "FIXED"
    ROLLING = "ROLLING"


class AssignmentUnit(str, Enum):
    """Unit that is randomized by the assignment service.

    Issue 02 only supports enrollment-level assignment. ``SESSION`` exists as an
    additive contract value so a future protocol version can request it without
    a breaking enum change; the V1 validator rejects it explicitly.
    """

    ENROLLMENT = "ENROLLMENT"
    SESSION = "SESSION"


class AssignmentStrategy(str, Enum):
    """How an assignment service picks a condition for one unit."""

    WEIGHTED_RANDOM = "WEIGHTED_RANDOM"
    DETERMINISTIC_HASH = "DETERMINISTIC_HASH"
    STRATIFIED = "STRATIFIED"


class RetentionAction(str, Enum):
    """What happens to identifiable data when retention expires."""

    RETAIN_ANONYMIZED = "RETAIN_ANONYMIZED"
    DELETE_IDENTIFIABLE = "DELETE_IDENTIFIABLE"
    DELETE_ALL = "DELETE_ALL"


class TelemetryFieldClass(str, Enum):
    """Coarse class of telemetry fields the protocol permits capturing."""

    STRUCTURAL = "STRUCTURAL"
    CONTENT = "CONTENT"
    METRICS = "METRICS"
    DIAGNOSTICS = "DIAGNOSTICS"


class CompletionPolicyKind(str, Enum):
    """Terminal condition that ends enrollment for a study."""

    MANUAL = "MANUAL"
    TARGET_CAPACITY = "TARGET_CAPACITY"
    SCHEDULE_END = "SCHEDULE_END"


class ValidationSeverity(str, Enum):
    """Whether a validation reason blocks publication, asks for review, or warns.

    ``ERROR`` blocks publication. ``NEEDS_REVIEW`` asks an operator to look
    before proceeding and blocks an automatic publish. ``WARNING`` is
    informational only: the document may publish, but the caller is expected to
    surface the reason (for example an administrator selecting an unverified
    distribution).
    """

    ERROR = "ERROR"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    WARNING = "WARNING"


class ValidationReasonCode(str, Enum):
    """Stable machine-readable reason a protocol cannot be published."""

    # Schema / structural.
    SCHEMA_INVALID = "SCHEMA_INVALID"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    NO_CONDITIONS = "NO_CONDITIONS"

    # Conditions / weights.
    DUPLICATE_CONDITION_ID = "DUPLICATE_CONDITION_ID"
    NON_POSITIVE_WEIGHT = "NON_POSITIVE_WEIGHT"
    EMPTY_WEIGHT_TOTAL = "EMPTY_WEIGHT_TOTAL"

    # Schedule.
    FIXED_SCHEDULE_EXPIRED = "FIXED_SCHEDULE_EXPIRED"
    FIXED_SCHEDULE_INVALID = "FIXED_SCHEDULE_INVALID"
    ROLLING_DURATION_INVALID = "ROLLING_DURATION_INVALID"

    # Assignment.
    ASSIGNMENT_UNIT_NOT_ENROLLMENT = "ASSIGNMENT_UNIT_NOT_ENROLLMENT"
    ASSIGNMENT_STRATEGY_UNKNOWN = "ASSIGNMENT_STRATEGY_UNKNOWN"
    ASSIGNMENT_STRATIFICATION_MISSING = "ASSIGNMENT_STRATIFICATION_MISSING"

    # Consent / privacy.
    CONSENT_DIGEST_MISSING = "CONSENT_DIGEST_MISSING"
    # Unsupported retention authoring: the published policy retains uploaded
    # data according to consent; a participant-selected DELETE_ALL is not an
    # authorable option.
    RETENTION_UNSUPPORTED = "RETENTION_UNSUPPORTED"

    # Agent release references.
    AGENT_RELEASE_UNPINNED = "AGENT_RELEASE_UNPINNED"
    AGENT_RELEASE_LATEST = "AGENT_RELEASE_LATEST"
    MUTABLE_AGENT_COMMAND = "MUTABLE_AGENT_COMMAND"
    RELEASE_UNRESOLVED = "RELEASE_UNRESOLVED"
    RELEASE_UNQUALIFIED = "RELEASE_UNQUALIFIED"
    RELEASE_WITHDRAWN = "RELEASE_WITHDRAWN"
    RELEASE_DIGEST_MISMATCH = "RELEASE_DIGEST_MISMATCH"

    # Agent profile references (a condition may pin an existing profile).
    AGENT_PROFILE_NOT_FOUND = "AGENT_PROFILE_NOT_FOUND"

    # A distribution (AgentProfile) is not verified: its release is missing a
    # passing conformance receipt, or it is a participant-installed BYOA
    # distribution that has no artifact to bind conformance evidence to.
    DISTRIBUTION_UNVERIFIED = "DISTRIBUTION_UNVERIFIED"

    # Declared distribution contract on a release pin.
    AGENT_RELEASE_MODE_UNKNOWN = "AGENT_RELEASE_MODE_UNKNOWN"
    # A BYOA pin must carry a release identity (release_id/version), never a digest.
    AGENT_RELEASE_IDENTITY_REQUIRED = "AGENT_RELEASE_IDENTITY_REQUIRED"

    # Environment requirements.
    UNKNOWN_REQUIRED_CAPABILITY = "UNKNOWN_REQUIRED_CAPABILITY"
    UNKNOWN_ENVIRONMENT_REQUIREMENT = "UNKNOWN_ENVIRONMENT_REQUIREMENT"
    ENVIRONMENT_PROTOCOL_VERSION_MISSING = "ENVIRONMENT_PROTOCOL_VERSION_MISSING"

    # Telemetry / completion.
    TELEMETRY_FIELD_CLASSES_EMPTY = "TELEMETRY_FIELD_CLASSES_EMPTY"
    COMPLETION_TARGET_INVALID = "COMPLETION_TARGET_INVALID"

    # Document safety.
    FORBIDDEN_IDENTIFIER = "FORBIDDEN_IDENTIFIER"
    FORBIDDEN_CREDENTIAL = "FORBIDDEN_CREDENTIAL"
    FORBIDDEN_LOCAL_PATH = "FORBIDDEN_LOCAL_PATH"


class ReleaseResolutionStatus(str, Enum):
    """Outcome of resolving a pinned agent release through a resolver."""

    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    UNQUALIFIED = "UNQUALIFIED"
    WITHDRAWN = "WITHDRAWN"


class PublicationOutcome(str, Enum):
    """Terminal result of a publication attempt."""

    PUBLISHED = "PUBLISHED"
    CONFLICT = "CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
