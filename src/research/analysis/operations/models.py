"""Pydantic v2 contracts for pilot operations, retention and the release gate.

``OperationalHealthV1`` deliberately carries **no content field**: it records
metadata/health signals only (`extra="forbid"` rejects any unknown key such as a
prompt, source, or secret). ``ReleaseEvidenceV1`` is immutable; any unknown or
missing required item yields ``NO_GO``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from research.study.protocol.enums import RetentionAction  # noqa: TC001 - pydantic field type
from research.telemetry.enums import CoverageState

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

_BASE = ConfigDict(extra="forbid")
_FROZEN = ConfigDict(extra="forbid", frozen=True)

__all__ = [
    "ComponentEvidence",
    "DeleteVerification",
    "FaultScenario",
    "HealthSignal",
    "IncidentRecord",
    "KillSwitchRecord",
    "KillSwitchScope",
    "OperationalHealthV1",
    "OperationsIssue",
    "PilotRunV1",
    "ReleaseEvaluationResult",
    "ReleaseEvidenceV1",
    "ReleaseReason",
    "RetentionVerification",
    "ReviewerApproval",
]


class OperationsIssue(BaseModel):
    """One typed operations rejection reason."""

    model_config = _BASE

    code: OperationsReasonCode
    message: str
    field: str = ""


class HealthSignal(BaseModel):
    """One evaluated health signal (metadata only)."""

    model_config = _FROZEN

    name: str
    state: HealthSignalState = HealthSignalState.UNKNOWN
    threshold: Optional[str] = None
    observed_value: Optional[float] = None
    reason: Optional[str] = None


class OperationalHealthV1(BaseModel):
    """Operational health snapshot. Contains no content/prompt/secret field."""

    model_config = _FROZEN

    study_id: UUID
    window_start: datetime
    window_end: datetime
    spool_depth: Optional[int] = None
    spool_age_seconds: Optional[int] = None
    upload_ack_rate: Optional[float] = None
    proxy_start_failures: Optional[int] = None
    runtime_exit_count: Optional[int] = None
    capability_mismatch_count: Optional[int] = None
    session_state_counts: dict[str, int] = Field(default_factory=dict)
    ingestion_reject_reasons: dict[str, int] = Field(default_factory=dict)
    retention_job_status: Optional[str] = None
    export_job_health: Optional[str] = None
    signals: list[HealthSignal] = Field(default_factory=list)
    captured_at: datetime


class ReviewerApproval(BaseModel):
    """One reviewer's recorded approval (or rejection)."""

    model_config = _FROZEN

    reviewer_id: str
    role: str
    approved: bool
    approved_at: Optional[datetime] = None


class ComponentEvidence(BaseModel):
    """Per-component digest/state/expiry used by the gate."""

    model_config = _FROZEN

    name: str
    digest: Optional[str] = None
    state: EvidenceState = EvidenceState.PRESENT
    expires_at: Optional[datetime] = None


class ReleaseEvidenceV1(BaseModel):
    """Immutable release evidence consumed by the release gate."""

    model_config = _FROZEN

    release_id: UUID
    study_id: UUID
    config_digest: Optional[str] = None
    config_expires_at: Optional[datetime] = None
    # name -> artifact/receipt digest. A blank or UNKNOWN digest is not a pass.
    component_artifacts: dict[str, str] = Field(default_factory=dict)
    component_expires_at: dict[str, datetime] = Field(default_factory=dict)
    # name -> PASS/FAIL/UNKNOWN (anything other than PASS is not a pass).
    test_results: dict[str, str] = Field(default_factory=dict)
    known_limitations: list[str] = Field(default_factory=list)
    reviewer_approvals: list[ReviewerApproval] = Field(default_factory=list)
    decision: Optional[ReleaseDecision] = None
    recorded_at: Optional[datetime] = None


class ReleaseReason(BaseModel):
    """One typed gate reason."""

    model_config = _BASE

    code: OperationsReasonCode
    message: str
    field: str = ""


class ReleaseEvaluationResult(BaseModel):
    """Typed release-gate decision with its reasons."""

    model_config = _BASE

    decision: ReleaseDecision
    reasons: list[ReleaseReason] = Field(default_factory=list)
    config_digest: Optional[str] = None
    evaluated_at: datetime
    evidence: ReleaseEvidenceV1


class FaultScenario(BaseModel):
    """One injected fault and its observed outcome."""

    model_config = _FROZEN

    name: str
    injected: bool = True
    observed_outcome: str
    state: HealthSignalState = HealthSignalState.UNKNOWN


class IncidentRecord(BaseModel):
    """One pilot incident (metadata only)."""

    model_config = _FROZEN

    incident_id: str
    summary: str
    severity: str
    opened_at: datetime
    resolved_at: Optional[datetime] = None


class PilotRunV1(BaseModel):
    """A recorded synthetic/consented pilot run."""

    model_config = _FROZEN

    pilot_run_id: UUID
    classification: PilotClassification
    cohort: list[str] = Field(default_factory=list)
    window_start: datetime
    window_end: datetime
    fault_scenarios: list[FaultScenario] = Field(default_factory=list)
    metrics_snapshot_ref: Optional[str] = None
    coverage_snapshot_ref: Optional[str] = None
    incidents: list[IncidentRecord] = Field(default_factory=list)
    outcome: PilotOutcome
    recorded_at: datetime


class KillSwitchScope(BaseModel):
    """The scope a kill switch applies to."""

    model_config = _FROZEN

    kind: KillSwitchScopeKind
    scope_id: UUID

    def matches(
        self,
        *,
        study_id: Optional[UUID] = None,
        enrollment_id: Optional[UUID] = None,
    ) -> bool:
        """Whether this scope covers the requested identifiers."""
        if self.kind == KillSwitchScopeKind.STUDY:
            return study_id is not None and study_id == self.scope_id
        return enrollment_id is not None and enrollment_id == self.scope_id


class KillSwitchRecord(BaseModel):
    """One auditable kill-switch engagement (or release)."""

    model_config = _BASE

    switch_id: UUID
    scope: KillSwitchScope
    reason: str
    actor: Optional[str] = None
    engaged_at: datetime
    effective_until: Optional[datetime] = None
    released_at: Optional[datetime] = None

    def is_engaged(self, now: datetime) -> bool:
        """Whether the switch is currently engaged."""
        if self.released_at is not None:
            return False
        if self.effective_until is not None and now >= self.effective_until:
            return False
        return True


class DeleteVerification(BaseModel):
    """Per-record deletion verification evidence (metadata only)."""

    model_config = _FROZEN

    record_id: str
    expected_state: str
    observed_state: str
    verified: bool


class RetentionVerification(BaseModel):
    """Result of a deletion-retention drill for one enrollment."""

    model_config = _BASE

    verification_id: UUID
    enrollment_id: UUID
    action: RetentionAction
    status: RetentionVerificationStatus
    evidence_digest: str
    verified_at: datetime
    deleted_count: int = 0
    remaining_count: int = 0
    expected_remaining_count: int = 0
    export_exclusion_verified: Optional[bool] = None
    coverage: CoverageState = CoverageState.UNKNOWN
    close_out_allowed: bool = False
    reasons: list[OperationsIssue] = Field(default_factory=list)
    record_checks: list[DeleteVerification] = Field(default_factory=list)
