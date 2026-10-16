"""Pilot operations, retention drills, and the release gate (Issue 13).

Public surface:

* :mod:`research.analysis.operations.enums` - release/pilot/health/retention vocabularies
  and typed reason codes.
* :mod:`research.analysis.operations.models` - ``OperationalHealthV1`` (metadata only),
  immutable ``ReleaseEvidenceV1``, ``PilotRunV1``, kill-switch and retention
  verification contracts.
* :mod:`research.analysis.operations.health` - pure health aggregation with explicit
  ``UNKNOWN`` on a monitoring outage (health is never invented).
* :mod:`research.analysis.operations.release_gate` - ``evaluate_release``.
* :mod:`research.analysis.operations.kill_switch` - auditable scoped kill switch and the
  injected predicate used by bootstrap/ingestion.
* :mod:`research.analysis.operations.retention` - deletion-drill verification.
* :mod:`research.analysis.operations.store` - Session-supplied persistence helpers.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from . import store
from .enums import (
    EvidenceState,
    HealthSignalState,
    KillSwitchScopeKind,
    OperationsReasonCode,
    PilotClassification,
    PilotOutcome,
    ReleaseDecision,
    RetentionVerificationStatus,
)
from .health import HealthInputs, HealthThresholds, aggregate_health, health_summary
from .kill_switch import (
    KillSwitchRegistry,
    engage_kill_switch,
    is_engaged,
    kill_switch_check,
    kill_switch_issue,
    release_kill_switch,
)
from .models import (
    ComponentEvidence,
    DeleteVerification,
    FaultScenario,
    HealthSignal,
    IncidentRecord,
    KillSwitchRecord,
    KillSwitchScope,
    OperationalHealthV1,
    OperationsIssue,
    PilotRunV1,
    ReleaseEvaluationResult,
    ReleaseEvidenceV1,
    ReleaseReason,
    RetentionVerification,
    ReviewerApproval,
)
from .release_gate import evaluate_release, record_decision
from .retention import verify_deletion

__all__ = [
    "ComponentEvidence",
    "DeleteVerification",
    "EvidenceState",
    "FaultScenario",
    "HealthInputs",
    "HealthSignal",
    "HealthSignalState",
    "HealthThresholds",
    "IncidentRecord",
    "KillSwitchRecord",
    "KillSwitchRegistry",
    "KillSwitchScope",
    "KillSwitchScopeKind",
    "OperationalHealthV1",
    "OperationsIssue",
    "OperationsReasonCode",
    "PilotClassification",
    "PilotOutcome",
    "PilotRunV1",
    "ReleaseDecision",
    "ReleaseEvaluationResult",
    "ReleaseEvidenceV1",
    "ReleaseReason",
    "RetentionVerification",
    "RetentionVerificationStatus",
    "ReviewerApproval",
    "aggregate_health",
    "engage_kill_switch",
    "evaluate_release",
    "health_summary",
    "is_engaged",
    "kill_switch_check",
    "kill_switch_issue",
    "record_decision",
    "release_kill_switch",
    "store",
    "verify_deletion",
]
