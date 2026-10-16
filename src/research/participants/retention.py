"""Pure retention planning/execution, persistence helpers and worker entry points.

This module is deterministic and framework-free: it never imports ``App``,
FastAPI or the ORM eagerly. Given a job and the enrollment's stored events it
produces a plan, applies the published retention action (via
:func:`apply_retention` semantics) through an
injected applier, and transitions the job state.

The SQLAlchemy helpers take a caller-supplied ``Session`` (like every other
research store) and return ORM rows; :class:`SqlAlchemyRetentionStore` is a
thin, injectable wrapper that returns domain objects to the worker.

The worker processes a bounded batch with retries: a job becomes ``RUNNING``,
executes, and finishes ``COMPLETED`` or ``RETRYABLE``/``FAILED``. Exceptions
never crash the process; they are recorded on the job with a content-free
evidence digest.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Optional, Protocol, Sequence
from uuid import UUID

from sqlalchemy import select

from database.research_schemas import (
    ResearchAgentRun,
    ResearchEnrollment,
    ResearchEvent,
    ResearchRetentionJob,
)
from database.research_schemas import (
    ResearchSessionV1 as ResearchSessionRow,
)
from research.canonical import canonical_hash
from research.runtime.sessions.enums import (
    AgentRunOutcome,
    CloseReason,
    SessionState,
)
from research.study.protocol.enums import RetentionAction
from research.telemetry.ingestion.store import row_to_record

from .enums import (
    RetentionJobState,
    RetentionReasonCode,
    RetentionState,
    RetentionStepOutcome,
)
from .identity import PseudonymousRecord, apply_retention, insert_deletion_ledger
from .models import (
    DeletionLedgerEntry,
    RetentionEventOutcome,
    RetentionEvidence,
    RetentionExecution,
    RetentionJob,
    RetentionPlan,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from research.telemetry.ingestion.models import ResearchEventRecord

    from .models import Enrollment

__all__ = [
    "RetentionApplier",
    "SqlAlchemyRetentionStore",
    "apply_event_anonymization",
    "apply_event_deletion",
    "enqueue_for_enrollment",
    "event_to_pseudonymous_record",
    "execute_retention",
    "get_enrollment_study_id",
    "get_retention_job",
    "get_retention_job_for_enrollment_action",
    "is_event_visible",
    "job_summary",
    "list_candidate_events",
    "list_retention_jobs_for_enrollment",
    "plan_retention",
    "revoke_sessions",
    "row_to_retention_job",
    "run_retention_for_enrollment",
    "run_retention_job",
    "start_retention_job",
    "upsert_retention_job",
]

_LINKABLE_KEYS = ("account_id", "email", "session_token")


class RetentionApplier(Protocol):
    """Side-effecting steps the pure executor calls (implemented by the store)."""

    def apply_event_retention(
        self,
        deleted_event_ids: Sequence[UUID],
        anonymized_event_ids: Sequence[UUID],
        *,
        now: datetime,
    ) -> None:  # pragma: no cover - structural protocol
        ...

    def apply_session_retention(
        self,
        enrollment_id: UUID,
        *,
        now: datetime,
    ) -> None:  # pragma: no cover - structural protocol
        ...


class _NullApplier:
    """Default in-memory applier: plans but persists nothing."""

    def apply_event_retention(
        self,
        deleted_event_ids: Sequence[UUID],
        anonymized_event_ids: Sequence[UUID],
        *,
        now: datetime,
    ) -> None:
        return None

    def apply_session_retention(
        self,
        enrollment_id: UUID,
        *,
        now: datetime,
    ) -> None:
        return None


class _EnrollmentRef:
    """Minimal object carrying the one attribute ``apply_retention`` reads."""

    __slots__ = ("enrollment_id",)

    def __init__(self, enrollment_id: UUID) -> None:
        self.enrollment_id = enrollment_id


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _coerce_uuid(value: Any) -> Optional[UUID]:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _envelope(event: Any) -> dict[str, Any]:
    envelope = getattr(event, "envelope", None)
    return dict(envelope) if isinstance(envelope, dict) else {}


def _payload(event: Any) -> dict[str, Any]:
    # Stored records carry content only inside their canonical envelope; a
    # canonical event exposes ``payload`` directly.
    payload = _envelope(event).get("payload")
    if not isinstance(payload, dict):
        payload = getattr(event, "payload", None)
    return dict(payload) if isinstance(payload, dict) else {}


def _provenance(event: Any) -> dict[str, Any]:
    provenance = _envelope(event).get("provenance")
    if not isinstance(provenance, dict):
        provenance = getattr(event, "provenance", None)
    return dict(provenance) if isinstance(provenance, dict) else {}


def _extract_linkable(event: Any) -> dict[str, Any]:
    """Return the present account-linkable fields from payload/provenance.

    Only the presence of a value matters for linkability; the values are never
    logged or persisted by this module.
    """
    payload = _payload(event)
    provenance = _provenance(event)
    found: dict[str, Any] = {}
    for key in _LINKABLE_KEYS:
        value = payload.get(key)
        if value is None:
            value = provenance.get(key)
        if value is not None:
            found[key] = value
    return found


def is_event_visible(event: Any) -> bool:
    """Whether a stored event is still part of read models and exports.

    A ``DELETED`` tombstone is never visible; ``ANONYMIZED`` and ``RETAINED``
    events remain exportable.
    """
    state = getattr(event, "retention_state", RetentionState.RETAINED.value)
    return state != RetentionState.DELETED.value


def event_to_pseudonymous_record(event: Any) -> PseudonymousRecord:
    """Project one stored event into the identity retention vocabulary.

    Only ``account_id``, ``email`` and ``session_token`` count as linkable, so
    a normal pseudonymous event is a no-op for ``DELETE_IDENTIFIABLE`` while a
    planted linkable payload is removed or stripped exactly as
    :func:`apply_retention` specifies.
    """
    payload = _payload(event)
    provenance = _provenance(event)
    account_raw = payload.get("account_id")
    if account_raw is None:
        account_raw = provenance.get("account_id")
    email_raw = payload.get("email")
    if email_raw is None:
        email_raw = provenance.get("email")
    token_raw = payload.get("session_token")
    if token_raw is None:
        token_raw = provenance.get("session_token")

    pseudonymous_payload = {
        key: value
        for key, value in payload.items()
        if key not in _LINKABLE_KEYS
    }
    return PseudonymousRecord(
        record_id=str(event.event_id),
        enrollment_id=getattr(event, "enrollment_id", None),
        participant_code=None,
        account_id=_coerce_uuid(account_raw),
        email=str(email_raw) if email_raw is not None else None,
        session_token=str(token_raw) if token_raw is not None else None,
        pseudonymous_payload=pseudonymous_payload,
    )


def _plan_digest(
    enrollment_id: UUID,
    action: RetentionAction,
    event_outcomes: Sequence[RetentionEventOutcome],
) -> str:
    return canonical_hash(
        {
            "action": action.value,
            "enrollment_id": str(enrollment_id),
            "events": sorted(
                (str(item.event_id), item.outcome.value) for item in event_outcomes
            ),
        }
    )


def _evidence_digest(plan: RetentionPlan) -> str:
    return canonical_hash(
        {
            "plan_digest": plan.plan_digest,
            "action": plan.action.value,
            "enrollment_id": str(plan.enrollment_id),
            "affected_events": plan.affected_events,
        }
    )


def plan_retention(
    enrollment: Enrollment,
    action: RetentionAction,
    records: Iterable[ResearchEventRecord],
    *,
    now: Optional[datetime] = None,
) -> RetentionPlan:
    """Return a deterministic plan for one enrollment and retention action.

    The event outcomes reuse :func:`apply_retention`
    so ``DELETE_ALL``, ``DELETE_IDENTIFIABLE`` and ``RETAIN_ANONYMIZED`` mean
    exactly what they mean for pseudonymous records.
    """
    timestamp = _now(now)
    events = list(records)
    pseudonyms = [event_to_pseudonymous_record(event) for event in events]
    result = apply_retention(enrollment, pseudonyms, action, now=timestamp)
    retained_ids = {record.record_id for record in result.records}

    event_outcomes: list[RetentionEventOutcome] = []
    for event in events:
        record_id = str(event.event_id)
        if record_id not in retained_ids:
            outcome = RetentionStepOutcome.DELETED
        elif action == RetentionAction.RETAIN_ANONYMIZED and _extract_linkable(event):
            outcome = RetentionStepOutcome.ANONYMIZED
        else:
            outcome = RetentionStepOutcome.RETAINED
        event_outcomes.append(
            RetentionEventOutcome(event_id=event.event_id, outcome=outcome)
        )

    return RetentionPlan(
        enrollment_id=enrollment.enrollment_id,
        action=action,
        event_outcomes=event_outcomes,
        plan_digest=_plan_digest(enrollment.enrollment_id, action, event_outcomes),
    )


def start_retention_job(
    job: RetentionJob, *, now: Optional[datetime] = None
) -> RetentionJob:
    """Transition a job to ``RUNNING`` and increment its attempt counter."""
    timestamp = _now(now)
    return job.model_copy(
        update={
            "state": RetentionJobState.RUNNING,
            "attempts": job.attempts + 1,
            "started_at": job.started_at or timestamp,
            "completed_at": None,
        }
    )


def _build_evidence(
    job: RetentionJob, plan: RetentionPlan, timestamp: datetime
) -> RetentionEvidence:
    return RetentionEvidence(
        ledger_id=uuid.uuid4(),
        enrollment_id=job.enrollment_id,
        action=job.action,
        applied_at=timestamp,
        affected_events=plan.affected_events,
        evidence_digest=_evidence_digest(plan),
    )


def _evidence_from_job(job: RetentionJob, timestamp: datetime) -> RetentionEvidence:
    return RetentionEvidence(
        ledger_id=uuid.uuid4(),
        enrollment_id=job.enrollment_id,
        action=job.action,
        applied_at=job.completed_at or timestamp,
        affected_events=job.affected_events,
        evidence_digest=job.evidence_digest or "",
    )


def execute_retention(
    job: RetentionJob,
    events: Iterable[ResearchEventRecord],
    action: RetentionAction,
    now: Optional[datetime] = None,
    *,
    applier: Optional[RetentionApplier] = None,
    max_attempts: int = 3,
) -> RetentionExecution:
    """Execute (or re-execute) one retention job.

    * A ``COMPLETED`` job is a no-op returning the same job and evidence digest.
    * A ``FAILED``/``RETRYABLE``/``PENDING`` job is planned and executed; the
      attempt counter starts at the current value.
    * On an applier failure the job becomes ``RETRYABLE`` while attempts remain
      and ``FAILED`` afterwards; it is never ``COMPLETED`` and the plan/evidence
      is returned so the caller can retain the ledger.
    """
    timestamp = _now(now)

    if job.state == RetentionJobState.COMPLETED:
        return RetentionExecution(
            job=job, reused=True, evidence=_evidence_from_job(job, timestamp)
        )

    if action != job.action:
        failed = job.model_copy(
            update={
                "state": RetentionJobState.FAILED,
                "started_at": job.started_at or timestamp,
                "completed_at": timestamp,
                "last_error": RetentionReasonCode.ACTION_MISMATCH.value,
                "reason": RetentionReasonCode.ACTION_MISMATCH,
            }
        )
        return RetentionExecution(job=failed, reused=False)

    running = (
        job
        if job.state == RetentionJobState.RUNNING
        else start_retention_job(job, now=timestamp)
    )
    plan = plan_retention(
        _EnrollmentRef(running.enrollment_id),
        action,
        list(events),
        now=timestamp,
    )
    evidence = _build_evidence(running, plan, timestamp)
    deleted = plan.deleted_event_ids()
    anonymized = plan.anonymized_event_ids()
    active_applier: RetentionApplier = applier or _NullApplier()

    try:
        # DELETE_ALL also revokes the enrollment's sessions/runs, so a later
        # re-export that reads sessions independently of events cannot resurrect
        # the deleted participant's activity.
        if action == RetentionAction.DELETE_ALL:
            active_applier.apply_session_retention(
                running.enrollment_id, now=timestamp
            )
        if deleted or anonymized:
            active_applier.apply_event_retention(deleted, anonymized, now=timestamp)
    except Exception as error:  # noqa: BLE001 - any step failure is a job failure
        retryable = running.attempts < max_attempts
        state = (
            RetentionJobState.RETRYABLE if retryable else RetentionJobState.FAILED
        )
        reason = (
            RetentionReasonCode.PARTIAL_FAILURE
            if retryable
            else RetentionReasonCode.RETRY_EXHAUSTED
        )
        failed_job = running.model_copy(
            update={
                "state": state,
                "completed_at": None,
                "last_error": type(error).__name__,
                "evidence_digest": evidence.evidence_digest,
                "affected_events": plan.affected_events,
                "reason": reason,
            }
        )
        return RetentionExecution(
            job=failed_job,
            plan=plan,
            evidence=evidence,
            deleted_event_ids=deleted,
            anonymized_event_ids=anonymized,
            reused=False,
        )

    completed = running.model_copy(
        update={
            "state": RetentionJobState.COMPLETED,
            "completed_at": timestamp,
            "last_error": None,
            "evidence_digest": evidence.evidence_digest,
            "affected_events": plan.affected_events,
            "reason": RetentionReasonCode.OK,
        }
    )
    return RetentionExecution(
        job=completed,
        plan=plan,
        evidence=evidence,
        deleted_event_ids=deleted,
        anonymized_event_ids=anonymized,
        reused=False,
    )


# ---------------------------------------------------------------------------
# SQLAlchemy helpers (session supplied by the caller)
# ---------------------------------------------------------------------------


def get_enrollment_study_id(
    session: Session, enrollment_id: UUID
) -> Optional[UUID]:
    """Return the study a stored enrollment belongs to, or ``None``."""
    row = session.get(ResearchEnrollment, enrollment_id)
    return row.study_id if row is not None else None


def list_candidate_events(
    session: Session, enrollment_id: UUID
) -> list[ResearchEventRecord]:
    """Load an enrollment's non-tombstoned events as domain records."""
    statement = (
        select(ResearchEvent)
        .where(
            ResearchEvent.enrollment_id == enrollment_id,
            ResearchEvent.retention_state != RetentionState.DELETED.value,
        )
        .order_by(ResearchEvent.occurred_at.asc(), ResearchEvent.event_id.asc())
    )
    return [
        row_to_record(row)
        for row in session.execute(statement).scalars().all()
    ]


def _clear_event_content(row: ResearchEvent, now: datetime) -> None:
    row.retention_state = RetentionState.DELETED.value
    row.anonymized_at = now
    row.enrollment_id = None
    row.research_session_id = None
    row.agent_run_id = None
    # The complete envelope is content-free too: no copy of the deleted content
    # may survive in any column or in the envelope.
    row.envelope_json = {}


def apply_event_deletion(
    session: Session, event_ids: Sequence[UUID], now: datetime
) -> int:
    """Tombstone events: clear all content and mark them deleted."""
    ids = list(event_ids)
    if not ids:
        return 0
    statement = select(ResearchEvent).where(ResearchEvent.event_id.in_(ids))
    rows = list(session.execute(statement).scalars().all())
    for row in rows:
        _clear_event_content(row, now)
    session.commit()
    return len(rows)


def _strip_linkable(mapping: Any) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    return {key: value for key, value in mapping.items() if key not in _LINKABLE_KEYS}


def apply_event_anonymization(
    session: Session, event_ids: Sequence[UUID], now: datetime
) -> int:
    """Strip account-linkable fields and mark events anonymized."""
    ids = list(event_ids)
    if not ids:
        return 0
    statement = select(ResearchEvent).where(ResearchEvent.event_id.in_(ids))
    rows = list(session.execute(statement).scalars().all())
    for row in rows:
        row.retention_state = RetentionState.ANONYMIZED.value
        row.anonymized_at = now
        # The envelope is the only content authority, so it is scrubbed in place.
        envelope = dict(getattr(row, "envelope_json", None) or {})
        for key in ("payload", "provenance", "correlations", "unknown_payload"):
            if key in envelope:
                if isinstance(envelope[key], dict):
                    envelope[key] = _strip_linkable(envelope[key])
                else:
                    envelope.pop(key, None)
        row.envelope_json = envelope
    session.commit()
    return len(rows)


def revoke_sessions(session: Session, enrollment_id: UUID, now: datetime) -> int:
    """Mark an enrollment's sessions (and their runs) revoked for DELETE_ALL.

    A tombstoned enrollment must not leave live session/run rows behind: a
    later re-export reads sessions independently of events, so without this a
    deleted participant's activity could reappear in a fresh bundle.
    """
    statement = select(ResearchSessionRow).where(
        ResearchSessionRow.enrollment_id == enrollment_id
    )
    sessions = list(session.execute(statement).scalars().all())
    if not sessions:
        return 0
    session_ids = [row.session_id for row in sessions]
    for row in sessions:
        row.state = SessionState.REVOKED.value
        row.closed_at = now
        row.close_reason = CloseReason.REVOKED.value
    run_statement = select(ResearchAgentRun).where(
        ResearchAgentRun.research_session_id.in_(session_ids)
    )
    for run in session.execute(run_statement).scalars().all():
        if run.ended_at is None:
            run.ended_at = now
            run.outcome = AgentRunOutcome.REVOKED.value
    session.commit()
    return len(sessions)


def _apply_job_fields(row: ResearchRetentionJob, job: RetentionJob) -> None:
    row.enrollment_id = job.enrollment_id
    row.action = job.action.value
    row.state = job.state.value
    row.attempts = job.attempts
    row.created_at = job.created_at
    row.started_at = job.started_at
    row.completed_at = job.completed_at
    row.last_error = job.last_error
    row.evidence_digest = job.evidence_digest
    row.affected_events = job.affected_events


def upsert_retention_job(
    session: Session, job: RetentionJob
) -> ResearchRetentionJob:
    """Insert or update one retention job (idempotent per enrollment/action)."""
    row = session.get(ResearchRetentionJob, job.job_id)
    if row is None:
        existing = get_retention_job_for_enrollment_action(
            session, job.enrollment_id, job.action
        )
        if existing is not None:
            return existing
        row = ResearchRetentionJob(job_id=job.job_id)
        _apply_job_fields(row, job)
        session.add(row)
    else:
        _apply_job_fields(row, job)
    session.commit()
    session.refresh(row)
    return row


def get_retention_job(
    session: Session, job_id: UUID
) -> Optional[ResearchRetentionJob]:
    """Fetch a retention job row by id, or ``None``."""
    return session.get(ResearchRetentionJob, job_id)


def get_retention_job_for_enrollment_action(
    session: Session, enrollment_id: UUID, action: RetentionAction
) -> Optional[ResearchRetentionJob]:
    """Fetch the unique job for ``(enrollment, action)``, or ``None``."""
    statement = select(ResearchRetentionJob).where(
        ResearchRetentionJob.enrollment_id == enrollment_id,
        ResearchRetentionJob.action == action.value,
    )
    return session.execute(statement).scalars().first()


def list_retention_jobs_for_enrollment(
    session: Session, enrollment_id: UUID
) -> list[ResearchRetentionJob]:
    """List an enrollment's retention jobs, oldest-first."""
    statement = (
        select(ResearchRetentionJob)
        .where(ResearchRetentionJob.enrollment_id == enrollment_id)
        .order_by(ResearchRetentionJob.created_at.asc())
    )
    return list(session.execute(statement).scalars().all())


def row_to_retention_job(row: ResearchRetentionJob) -> RetentionJob:
    """Rehydrate a retention job row into the domain model."""
    state = RetentionJobState(row.state)
    if state == RetentionJobState.RETRYABLE:
        reason = RetentionReasonCode.PARTIAL_FAILURE
    elif state == RetentionJobState.FAILED:
        reason = RetentionReasonCode.RETRY_EXHAUSTED
    else:
        reason = RetentionReasonCode.OK
    return RetentionJob(
        job_id=row.job_id,
        enrollment_id=row.enrollment_id,
        action=RetentionAction(row.action),
        state=state,
        attempts=row.attempts or 0,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        last_error=row.last_error,
        evidence_digest=row.evidence_digest,
        affected_events=row.affected_events or 0,
        reason=reason,
    )


def job_summary(job: RetentionJob) -> dict[str, Any]:
    """Compact, non-secret job summary safe for operator responses."""
    return {
        "job_id": str(job.job_id),
        "enrollment_id": str(job.enrollment_id),
        "action": job.action.value,
        "state": job.state.value,
        "attempts": job.attempts,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "last_error": job.last_error,
        "evidence_digest": job.evidence_digest,
        "affected_events": job.affected_events,
        "reason": job.reason.value,
    }


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def insert_retention_evidence(
    session: Session, evidence: RetentionEvidence
) -> Any:
    """Append a content-free deletion-ledger row for an executed job."""
    entry = DeletionLedgerEntry(
        ledger_id=evidence.ledger_id,
        enrollment_id=evidence.enrollment_id,
        action=evidence.action,
        applied_at=evidence.applied_at,
        affected_count=evidence.affected_events,
        evidence_digest=evidence.evidence_digest,
    )
    return insert_deletion_ledger(session, entry)


# ---------------------------------------------------------------------------
# Injectable SQLAlchemy wrapper
# ---------------------------------------------------------------------------


class SqlAlchemyRetentionStore:
    """Adapter exposing the retention persistence surface over one session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_enrollment_study_id(self, enrollment_id: UUID) -> Optional[UUID]:
        return get_enrollment_study_id(self.session, enrollment_id)

    def load_events(self, enrollment_id: UUID) -> list[ResearchEventRecord]:
        return list_candidate_events(self.session, enrollment_id)

    def mark_events_deleted(self, event_ids: Sequence[UUID], now: datetime) -> int:
        return apply_event_deletion(self.session, event_ids, now)

    def anonymize_events(self, event_ids: Sequence[UUID], now: datetime) -> int:
        return apply_event_anonymization(self.session, event_ids, now)

    def revoke_sessions(self, enrollment_id: UUID, now: datetime) -> int:
        return revoke_sessions(self.session, enrollment_id, now)

    def get_job(self, job_id: UUID) -> Optional[RetentionJob]:
        row = get_retention_job(self.session, job_id)
        return row_to_retention_job(row) if row is not None else None

    def get_job_for_enrollment_action(
        self, enrollment_id: UUID, action: RetentionAction
    ) -> Optional[RetentionJob]:
        row = get_retention_job_for_enrollment_action(
            self.session, enrollment_id, action
        )
        return row_to_retention_job(row) if row is not None else None

    def upsert_job(self, job: RetentionJob) -> RetentionJob:
        return row_to_retention_job(upsert_retention_job(self.session, job))

    def insert_evidence(self, evidence: RetentionEvidence) -> None:
        insert_retention_evidence(self.session, evidence)


# ---------------------------------------------------------------------------
# Worker entry points
# ---------------------------------------------------------------------------


class _StoreApplier:
    """Applies a plan through the retention persistence surface."""

    def __init__(self, api: Any) -> None:
        self._api = api

    def apply_event_retention(
        self,
        deleted_event_ids: Sequence[UUID],
        anonymized_event_ids: Sequence[UUID],
        *,
        now: datetime,
    ) -> None:
        if deleted_event_ids:
            self._api.mark_events_deleted(deleted_event_ids, now)
        if anonymized_event_ids:
            self._api.anonymize_events(anonymized_event_ids, now)

    def apply_session_retention(self, enrollment_id: UUID, *, now: datetime) -> None:
        self._api.revoke_sessions(enrollment_id, now)


def _resolve_store(session: Any, store_api: Optional[Any]) -> Any:
    if store_api is not None:
        return store_api
    if session is None:
        raise ValueError("a session or an explicit retention store is required")
    return SqlAlchemyRetentionStore(session)


def _failure_evidence(
    job: RetentionJob, error: Exception, timestamp: datetime
) -> RetentionEvidence:
    digest = canonical_hash(
        {
            "action": job.action.value,
            "enrollment_id": str(job.enrollment_id),
            "error": type(error).__name__,
        }
    )
    return RetentionEvidence(
        ledger_id=uuid.uuid4(),
        enrollment_id=job.enrollment_id,
        action=job.action,
        applied_at=timestamp,
        affected_events=job.affected_events,
        evidence_digest=digest,
    )


def _failure_execution(
    job: RetentionJob, error: Exception, *, now: datetime, max_attempts: int
) -> RetentionExecution:
    retryable = job.attempts < max_attempts
    state = RetentionJobState.RETRYABLE if retryable else RetentionJobState.FAILED
    reason = (
        RetentionReasonCode.PARTIAL_FAILURE
        if retryable
        else RetentionReasonCode.RETRY_EXHAUSTED
    )
    evidence = _failure_evidence(job, error, now)
    failed = job.model_copy(
        update={
            "state": state,
            "completed_at": None,
            "last_error": type(error).__name__,
            "evidence_digest": evidence.evidence_digest,
            "reason": reason,
        }
    )
    return RetentionExecution(job=failed, evidence=evidence, reused=False)


def enqueue_for_enrollment(
    session: Any,
    enrollment_id: UUID,
    action: RetentionAction,
    *,
    store_api: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> RetentionJob:
    """Get or create the single retention job for ``(enrollment, action)``."""
    api = _resolve_store(session, store_api)
    existing = api.get_job_for_enrollment_action(enrollment_id, action)
    if existing is not None:
        return existing
    job = RetentionJob(
        job_id=uuid.uuid4(),
        enrollment_id=enrollment_id,
        action=action,
        state=RetentionJobState.PENDING,
        attempts=0,
        created_at=now or datetime.now(timezone.utc),
    )
    return api.upsert_job(job)


def run_retention_job(
    session: Any,
    job_id: UUID,
    *,
    store_api: Optional[Any] = None,
    now: Optional[datetime] = None,
    max_attempts: int = 3,
) -> RetentionJob:
    """Execute one retention job; never raises for a step failure."""
    api = _resolve_store(session, store_api)
    timestamp = now or datetime.now(timezone.utc)
    job = api.get_job(job_id)
    if job is None:
        raise ValueError(f"retention job {job_id} not found")
    if job.state == RetentionJobState.COMPLETED:
        return job

    running = start_retention_job(job, now=timestamp)
    api.upsert_job(running)
    try:
        events = api.load_events(job.enrollment_id)
        execution = execute_retention(
            running,
            events,
            job.action,
            now=timestamp,
            applier=_StoreApplier(api),
            max_attempts=max_attempts,
        )
        persisted = api.upsert_job(execution.job)
        if execution.evidence is not None and not execution.reused:
            api.insert_evidence(execution.evidence)
        return persisted
    except Exception as error:  # noqa: BLE001 - any failure is recorded, not raised
        execution = _failure_execution(
            running, error, now=timestamp, max_attempts=max_attempts
        )
        try:
            api.upsert_job(execution.job)
            if execution.evidence is not None:
                api.insert_evidence(execution.evidence)
        except Exception:  # noqa: BLE001 - never crash the worker process
            pass
        return execution.job


def run_retention_for_enrollment(
    session: Any,
    enrollment_id: UUID,
    action: RetentionAction,
    *,
    store_api: Optional[Any] = None,
    now: Optional[datetime] = None,
    max_attempts: int = 3,
) -> RetentionJob:
    """Idempotently enqueue and execute the job for one enrollment."""
    api = _resolve_store(session, store_api)
    job = enqueue_for_enrollment(
        session, enrollment_id, action, store_api=api, now=now
    )
    if job.state == RetentionJobState.COMPLETED:
        return job
    return run_retention_job(
        session,
        job.job_id,
        store_api=api,
        now=now,
        max_attempts=max_attempts,
    )
