"""Closed vocabularies for enrollment-scoped assignment and exposure (Issue 05).

Every member is part of a persisted contract (assignment rows, exposure
receipts, API responses), so members are additive only.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AllocationOutcome",
    "AssignmentReasonCode",
    "ExposureOutcome",
    "ExposureReasonCode",
]


class AllocationOutcome(str, Enum):
    """Terminal result of an allocation attempt."""

    CREATED = "CREATED"
    EXISTING = "EXISTING"
    INELIGIBLE = "INELIGIBLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICT = "CONFLICT"


class AssignmentReasonCode(str, Enum):
    """Stable machine-readable reason for an allocation outcome."""

    OK = "OK"
    NO_ENROLLMENT = "NO_ENROLLMENT"
    ENROLLMENT_NOT_ACTIVE = "ENROLLMENT_NOT_ACTIVE"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    REVOKED = "REVOKED"
    REVISION_NOT_PUBLISHED = "REVISION_NOT_PUBLISHED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    NO_CONDITIONS = "NO_CONDITIONS"
    WEIGHTS_INVALID = "WEIGHTS_INVALID"
    STICKY_EXISTING = "STICKY_EXISTING"
    WEIGHTED_DRAW = "WEIGHTED_DRAW"
    DETERMINISTIC_HASH = "DETERMINISTIC_HASH"
    UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"
    STRATIFIED_UNSUPPORTED = "STRATIFIED_UNSUPPORTED"


class ExposureOutcome(str, Enum):
    """Outcome of an attempted runtime exposure.

    ``STARTED`` and ``SUCCEEDED`` count as an exposure; ``FAILED``,
    ``RUNTIME_UNAVAILABLE`` and ``INCOMPATIBLE_ENVIRONMENT`` are recorded as
    non-exposures with evidence so a launch failure is never counted as a
    successful condition exposure.
    """

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    INCOMPATIBLE_ENVIRONMENT = "INCOMPATIBLE_ENVIRONMENT"

    @property
    def is_exposure(self) -> bool:
        """Whether this outcome represents an actual condition exposure."""
        return self in (ExposureOutcome.STARTED, ExposureOutcome.SUCCEEDED)


class ExposureReasonCode(str, Enum):
    """Stable machine-readable reason for an exposure receipt outcome."""

    OK = "OK"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    CONFLICT = "CONFLICT"
    ASSIGNMENT_MISMATCH = "ASSIGNMENT_MISMATCH"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
