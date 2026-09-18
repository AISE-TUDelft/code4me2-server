"""Closed vocabularies for enrollment-scoped assignment (Issue 05).

Every member is part of a persisted contract (assignment rows, API responses),
so members are additive only.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AllocationOutcome",
    "AssignmentReasonCode",
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
    STUDY_MISMATCH = "STUDY_MISMATCH"
    NO_PROFILES = "NO_PROFILES"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    STICKY_EXISTING = "STICKY_EXISTING"
    RANDOM_EQUAL = "RANDOM_EQUAL"
    UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"

