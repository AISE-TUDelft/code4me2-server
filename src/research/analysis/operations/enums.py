"""Closed vocabularies for pilot operations, retention and the release gate.

Every member is part of a persisted contract (health rows, release evidence,
kill-switch records, retention verifications); members are additive only.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "EvidenceState",
    "HealthSignalState",
    "KillSwitchScopeKind",
    "OperationsReasonCode",
    "PilotClassification",
    "PilotOutcome",
    "ReleaseDecision",
    "RetentionVerificationStatus",
]


class ReleaseDecision(str, Enum):
    """Terminal outcome of the release gate."""

    GO = "GO"
    NO_GO = "NO_GO"
    GO_WITH_LIMITS = "GO_WITH_LIMITS"
    EXPIRED = "EXPIRED"


class PilotClassification(str, Enum):
    """Whether a pilot ran on synthetic or consented data."""

    SYNTHETIC = "SYNTHETIC"
    CONSENTED = "CONSENTED"


class HealthSignalState(str, Enum):
    """State of one operational health signal."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
    CRITICAL = "CRITICAL"


class PilotOutcome(str, Enum):
    """Terminal outcome of a pilot run."""

    COMPLETED = "COMPLETED"
    COMPLETED_WITH_INCIDENTS = "COMPLETED_WITH_INCIDENTS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class EvidenceState(str, Enum):
    """Presence/validity of one required component or evidence item."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"
    CHANGED = "CHANGED"


class KillSwitchScopeKind(str, Enum):
    """The scope a kill switch applies to."""

    STUDY = "STUDY"
    REVISION = "REVISION"
    ENROLLMENT = "ENROLLMENT"


class RetentionVerificationStatus(str, Enum):
    """Result of a deletion-retention drill."""

    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class OperationsReasonCode(str, Enum):
    """Stable machine-readable reason for an operations/gate outcome."""

    OK = "OK"

    # Health signals.
    MONITORING_OUTAGE = "MONITORING_OUTAGE"
    MISSING_HEALTH_INPUT = "MISSING_HEALTH_INPUT"
    THRESHOLD_DEGRADED = "THRESHOLD_DEGRADED"
    THRESHOLD_CRITICAL = "THRESHOLD_CRITICAL"
    SPOOL_BACKLOG = "SPOOL_BACKLOG"
    UPLOAD_ACK_LOW = "UPLOAD_ACK_LOW"
    RUNTIME_EXIT_SPIKE = "RUNTIME_EXIT_SPIKE"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    RETENTION_INCOMPLETE = "RETENTION_INCOMPLETE"
    EXPORT_UNHEALTHY = "EXPORT_UNHEALTHY"
    SESSION_ANOMALY = "SESSION_ANOMALY"

    # Release gate.
    REQUIRED_COMPONENT_MISSING = "REQUIRED_COMPONENT_MISSING"
    REQUIRED_EVIDENCE_MISSING = "REQUIRED_EVIDENCE_MISSING"
    COMPONENT_RECEIPT_EXPIRED = "COMPONENT_RECEIPT_EXPIRED"
    REVISION_EXPIRED = "REVISION_EXPIRED"
    MATERIAL_CHANGE = "MATERIAL_CHANGE"
    LIMITATIONS_PRESENT = "LIMITATIONS_PRESENT"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"

    # Kill switch.
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    KILL_SWITCH_RELEASED = "KILL_SWITCH_RELEASED"

    # Retention drill.
    RETENTION_VERIFIED = "RETENTION_VERIFIED"
    RETENTION_PARTIAL = "RETENTION_PARTIAL"
    RETENTION_FAILED = "RETENTION_FAILED"
    EXPORT_EXCLUSION_UNVERIFIED = "EXPORT_EXCLUSION_UNVERIFIED"
