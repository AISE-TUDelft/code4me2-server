"""Pydantic v2 contracts for participant identity and retention.

Consent is a single acceptance recorded once at join: there is no document
identity, no re-consent and no withdrawal. Privacy invariant: only
:class:`Participant` carries the private account-to-participant mapping.
Enrollment and every researcher projection are keyed by opaque UUIDs and a
random study-local ``participant_code`` that is never derived from, or parsed
back to, login identity.

No retention model carries raw event content, an account id, an email or a
session token. A plan references events/exports by opaque id and a step
outcome; the evidence digest is computed over that plan plus counts and is
reproducible without reading content.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    EnrollmentStatus,
    IdentityReasonCode,
    RetentionAction,
    RetentionJobState,
    RetentionReasonCode,
    RetentionStepOutcome,
)

_BASE_CONFIG = ConfigDict(extra="forbid")
_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True)

_BASE = ConfigDict(extra="forbid")
_FROZEN = ConfigDict(extra="forbid", frozen=True)

__all__ = [
    "DeletionLedgerEntry",
    "Enrollment",
    "EnrollmentResult",
    "IdentityIssue",
    "Participant",
    "PseudonymousRecord",
    "ResearchEligibility",
    "RetentionEventOutcome",
    "RetentionEvidence",
    "RetentionExecution",
    "RetentionJob",
    "RetentionPlan",
    "RetentionResult",
]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class Participant(BaseModel):
    """Private account-to-participant mapping.

    This is the only object that links a login account to a study participant.
    It is privileged: it is never returned to researchers and is never embedded
    in a researcher projection or export.
    """

    model_config = _BASE_CONFIG

    participant_id: UUID
    account_id: UUID
    created_at: datetime


class ResearchEligibility(BaseModel):
    """Typed eligibility result, evaluated before enrollment."""

    model_config = _BASE_CONFIG

    eligible: bool
    reasons: list[IdentityReasonCode] = Field(default_factory=list)
    evaluated_at: Optional[datetime] = None


class Enrollment(BaseModel):
    """One participant's study enrollment and consent identity."""

    model_config = _BASE_CONFIG

    enrollment_id: UUID
    participant_id: UUID
    study_id: UUID
    # Random, opaque, study-local. Never derived from the account id and never
    # parsed for meaning; it is the only participant handle researchers see.
    participant_code: str
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE
    eligibility: ResearchEligibility
    enrolled_at: datetime
    # Set at join: the participant accepted the global consent text once.
    consent_accepted_at: Optional[datetime] = None
    revocation_epoch: int = 0
    updated_at: datetime
    retention_action: RetentionAction = RetentionAction.RETAIN_ANONYMIZED


class DeletionLedgerEntry(BaseModel):
    """Append-only evidence that a retention action was applied."""

    model_config = _BASE_CONFIG

    ledger_id: UUID
    enrollment_id: UUID
    action: RetentionAction
    applied_at: datetime
    affected_count: int
    evidence_digest: str


class PseudonymousRecord(BaseModel):
    """A research record that may carry account-linkable fields.

    ``account_id``, ``email`` and ``session_token`` are the linkable fields a
    retention action strips or removes. ``participant_code`` and
    ``pseudonymous_payload`` are the safe research fields.
    """

    model_config = _BASE_CONFIG

    record_id: str
    enrollment_id: Optional[UUID] = None
    participant_code: Optional[str] = None
    account_id: Optional[UUID] = None
    email: Optional[str] = None
    session_token: Optional[str] = None
    pseudonymous_payload: dict[str, Any] = Field(default_factory=dict)


class IdentityIssue(BaseModel):
    """One typed identity rejection reason."""

    model_config = _BASE_CONFIG

    code: IdentityReasonCode
    message: str
    field: str = ""


class EnrollmentResult(BaseModel):
    """Typed outcome of an enrollment operation."""

    model_config = _BASE_CONFIG

    accepted: bool
    enrollment: Optional[Enrollment] = None
    created: bool = False
    reused: bool = False
    issue: Optional[IdentityIssue] = None


class RetentionResult(BaseModel):
    """Typed outcome of applying a retention action to pseudonymous records."""

    model_config = _BASE_CONFIG

    action: RetentionAction
    records: list[PseudonymousRecord] = Field(default_factory=list)
    ledger: DeletionLedgerEntry


# ---------------------------------------------------------------------------
# Retention execution
# ---------------------------------------------------------------------------


class RetentionEventOutcome(BaseModel):
    """The planned outcome for one stored event (id + step only)."""

    model_config = _FROZEN

    event_id: UUID
    outcome: RetentionStepOutcome


class RetentionPlan(BaseModel):
    """A deterministic plan for one retention action over one enrollment."""

    model_config = _FROZEN

    enrollment_id: UUID
    action: RetentionAction
    event_outcomes: list[RetentionEventOutcome] = Field(default_factory=list)
    plan_digest: str

    def _event_ids(self, outcome: RetentionStepOutcome) -> list[UUID]:
        return [
            item.event_id
            for item in self.event_outcomes
            if item.outcome == outcome
        ]

    def deleted_event_ids(self) -> list[UUID]:
        """Event ids that will be deleted/tombstoned."""
        return self._event_ids(RetentionStepOutcome.DELETED)

    def anonymized_event_ids(self) -> list[UUID]:
        """Event ids that will be kept but anonymized."""
        return self._event_ids(RetentionStepOutcome.ANONYMIZED)

    def affected_event_ids(self) -> list[UUID]:
        """Event ids whose stored state changes (deleted or anonymized)."""
        return self.deleted_event_ids() + self.anonymized_event_ids()

    @property
    def affected_events(self) -> int:
        """Number of events whose stored state changes."""
        return len(self.affected_event_ids())


class RetentionJob(BaseModel):
    """Durable, idempotent state machine for one enrollment/action."""

    model_config = _BASE

    job_id: UUID
    enrollment_id: UUID
    action: RetentionAction
    state: RetentionJobState = RetentionJobState.PENDING
    attempts: int = 0
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_error: Optional[str] = None
    evidence_digest: Optional[str] = None
    affected_events: int = 0
    reason: RetentionReasonCode = RetentionReasonCode.OK


class RetentionEvidence(BaseModel):
    """Immutable, content-free evidence that a retention action ran.

    ``ledger_id`` is persisted as a ``research_record`` row
    (``kind = RETENTION_EVIDENCE``) and merged into the matching
    ``research_retention_job.evidence_json`` so an operator can verify execution
    without reading any event content.
    """

    model_config = _FROZEN

    ledger_id: UUID
    enrollment_id: UUID
    action: RetentionAction
    applied_at: datetime
    affected_events: int
    evidence_digest: str


class RetentionExecution(BaseModel):
    """Result of executing (or re-executing) one retention job."""

    model_config = _BASE

    job: RetentionJob
    plan: Optional[RetentionPlan] = None
    evidence: Optional[RetentionEvidence] = None
    deleted_event_ids: list[UUID] = Field(default_factory=list)
    anonymized_event_ids: list[UUID] = Field(default_factory=list)
    reused: bool = False
