"""Closed vocabularies for participant identity and retention.

Consent is a single acceptance recorded once at join (no document identity, no
re-consent, no withdrawal), so those vocabularies are gone. What remains is the
enrollment lifecycle, the stable rejection reasons, and the retention vocabulary
(re-exported from the study protocol so identity and retention share one
published contract).

Members are part of persisted contracts, so they stay additive.
"""

from __future__ import annotations

from enum import Enum

from research.study.protocol.enums import RetentionAction  # noqa: F401 - intentional re-export

__all__ = [
    "EnrollmentStatus",
    "IdentityReasonCode",
    "RetentionAction",
    "RetentionJobState",
    "RetentionReasonCode",
    "RetentionState",
    "RetentionStepOutcome",
]


class EnrollmentStatus(str, Enum):
    """Lifecycle of one participant's enrollment in one study revision.

    ``ACTIVE`` is the only state in which telemetry may be accepted. A study that
    ends (or is terminated) marks its enrollments ``COMPLETED``, which is
    terminal: the enrollment keeps its assignment but accepts nothing new.
    """

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class IdentityReasonCode(str, Enum):
    """Stable machine-readable reason an identity operation was rejected."""

    DUPLICATE_ENROLLMENT = "DUPLICATE_ENROLLMENT"
    # One active enrollment per account across the platform: another study is
    # already live for this participant.
    ALREADY_ENROLLED = "ALREADY_ENROLLED"
    # A completed study is terminal; the account may join a different study.
    REJOIN_NOT_ALLOWED = "REJOIN_NOT_ALLOWED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    MAPPING_FAILURE = "MAPPING_FAILURE"
    UNKNOWN_REVISION = "UNKNOWN_REVISION"
    NOT_ACTIVE = "NOT_ACTIVE"
    REVOKED = "REVOKED"

    # Eligibility.
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN_ELIGIBILITY = "UNKNOWN_ELIGIBILITY"


class RetentionJobState(str, Enum):
    """Lifecycle of one idempotent retention job.

    ``PENDING`` is enqueued when a study ends; ``RUNNING`` is written before any
    data is touched; ``COMPLETED`` is terminal and never re-executed. A partial
    failure is never ``COMPLETED``: it is ``RETRYABLE`` while attempts remain and
    ``FAILED`` once the retry budget is exhausted.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYABLE = "RETRYABLE"


class RetentionStepOutcome(str, Enum):
    """What one planned retention step did (or will do) to one item."""

    DELETED = "DELETED"
    ANONYMIZED = "ANONYMIZED"
    RETAINED = "RETAINED"
    INVALIDATED = "INVALIDATED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class RetentionReasonCode(str, Enum):
    """Stable machine-readable reason for a job outcome (content-free)."""

    OK = "OK"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    ACTION_MISMATCH = "ACTION_MISMATCH"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    NO_ENROLLMENT = "NO_ENROLLMENT"


class RetentionState(str, Enum):
    """Persisted per-event retention posture (``ResearchEvent.retention_state``).

    ``ANONYMIZED`` events have account-linkable payload/provenance fields
    stripped. ``DELETED`` rows are content-free tombstones that read models must
    exclude.
    """

    RETAINED = "RETAINED"
    ANONYMIZED = "ANONYMIZED"
    DELETED = "DELETED"
