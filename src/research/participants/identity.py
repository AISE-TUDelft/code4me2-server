"""Participant identity and retention persistence.

Pure enrollment and retention services plus the caller-supplied-session
persistence helpers and the researcher-safe projection. The invariants enforced
here are:

* Telemetry is accepted only for an ``ACTIVE`` enrollment; every other state
  (including unknown/absent) fails closed.
* Consent is a single acceptance recorded once at join (``consent_accepted_at``):
  there is no document identity, re-consent or withdrawal.
* One account has at most one ACTIVE enrollment across the platform.
* A study that ends marks its enrollments ``COMPLETED`` and bumps the revocation
  epoch so previously issued capabilities stop working.
* Retention actions act on pseudonymous records and always emit a ledger entry.

The persistence helpers take a caller-managed SQLAlchemy ``Session`` so the core
package never imports ``App`` or touches the application singleton. The router
is responsible for session lifecycle (``App.get_db_session`` / ``rollback`` /
``close``) and for access control over the private mapping.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from database.research_schemas import (
    RECORD_KIND_RETENTION_EVIDENCE,
    ResearchEnrollment,
    ResearchParticipant,
    ResearchRecord,
    ResearchRetentionJob,
)
from database.research_schemas import (
    ResearchSessionV1 as ResearchSessionRow,
)
from research.canonical import canonical_hash
from research.runtime.sessions.enums import (
    CloseReason,
    SessionReasonCode,
    SessionState,
)
from research.runtime.sessions.models import SessionTransition
from research.study.protocol.enums import RetentionAction

from .enums import (
    EnrollmentStatus,
    IdentityReasonCode,
)
from .models import (
    DeletionLedgerEntry,
    Enrollment,
    EnrollmentResult,
    IdentityIssue,
    Participant,
    PseudonymousRecord,
    ResearchEligibility,
    RetentionResult,
    RevisionRef,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from research.study.protocol.publication import StudyRevision

# Audit action used for the append-only deletion/retention ledger.
LEDGER_AUDIT_ACTION = "retention.deletion_ledger"

# A duplicate enrollment request for the same revision returns the existing
# ACTIVE record rather than creating a second enrollment.
_REUSABLE_ENROLLMENT_STATES = frozenset({EnrollmentStatus.ACTIVE})

_ACTIVE_SESSION_STATES = (
    SessionState.NOT_STARTED.value,
    SessionState.RUNNING.value,
    SessionState.OFFLINE.value,
    SessionState.SUSPENDED.value,
)


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _issue(code: IdentityReasonCode, message: str, field: str = "") -> IdentityIssue:
    return IdentityIssue(code=code, message=message, field=field)


def _rejected(code: IdentityReasonCode, message: str, field: str = "") -> EnrollmentResult:
    return EnrollmentResult(accepted=False, issue=_issue(code, message, field))


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


# ---------------------------------------------------------------------------
# Enrollment state machine
# ---------------------------------------------------------------------------


def generate_participant_code(*, byte_length: int = 12, prefix: str = "p") -> str:
    """Return a random, opaque, study-local participant code.

    The code is drawn from a CSPRNG and is not derived from, nor reversible to,
    the account identity. It carries no demographic or study semantics.
    """
    return f"{prefix}_{secrets.token_hex(byte_length)}"


def default_eligibility(now: Optional[datetime] = None) -> ResearchEligibility:
    """Return the permissive default eligibility result."""
    return ResearchEligibility(
        eligible=True,
        reasons=[IdentityReasonCode.ELIGIBLE],
        evaluated_at=_now(now),
    )


def revision_ref_from_mapping(
    protocol_json: Mapping,
    *,
    revision_id: UUID,
    study_id: UUID,
) -> RevisionRef:
    """Build the enrollment slice from a revision protocol document."""
    privacy = protocol_json.get("privacy_policy") or {}
    raw_action = privacy.get("retention_action")
    try:
        retention = (
            RetentionAction(raw_action)
            if raw_action
            else RetentionAction.RETAIN_ANONYMIZED
        )
    except ValueError:
        retention = RetentionAction.RETAIN_ANONYMIZED

    return RevisionRef(
        revision_id=revision_id,
        study_id=study_id,
        retention_action=retention,
    )


def revision_ref_from_study_revision(revision: StudyRevision) -> RevisionRef:
    """Build the enrollment slice from a published ``StudyRevision``."""
    return revision_ref_from_mapping(
        revision.protocol_json,
        revision_id=revision.revision_id,
        study_id=revision.study_id,
    )


def enroll(
    participant: Participant,
    revision: RevisionRef,
    *,
    existing_enrollment: Optional[Enrollment] = None,
    other_active_enrollment: Optional[Enrollment] = None,
    eligibility: Optional[ResearchEligibility] = None,
    now: Optional[datetime] = None,
) -> EnrollmentResult:
    """Create (or reuse) a study-local enrollment for ``participant``.

    Joining records the single consent acceptance (``consent_accepted_at``) and
    creates an ACTIVE enrollment directly: there is no pending-consent,
    re-consent or withdrawal state. A duplicate request for the same
    ``(participant, revision)`` returns the existing enrollment. A second active
    enrollment is never created: ``other_active_enrollment`` is the participant's
    live enrollment in another study (resolved under a participant lock by the
    caller), and is rejected with a typed ``ALREADY_ENROLLED`` naming the other
    study.
    """
    timestamp = _now(now)

    if revision is None:
        return _rejected(
            IdentityReasonCode.UNKNOWN_REVISION,
            "enrollment requires a published revision",
            "revision_id",
        )

    if existing_enrollment is not None:
        if (
            existing_enrollment.study_revision_id == revision.revision_id
            and existing_enrollment.status in _REUSABLE_ENROLLMENT_STATES
        ):
            return EnrollmentResult(
                accepted=True,
                enrollment=existing_enrollment,
                created=False,
                reused=True,
            )
        return _rejected(
            IdentityReasonCode.DUPLICATE_ENROLLMENT,
            (
                "an existing enrollment for this revision is not reusable "
                f"({existing_enrollment.status.value})"
            ),
            "study_revision_id",
        )

    # One active enrollment account-wide. Completion of another study frees
    # the slot, so only the live state counts.
    if (
        other_active_enrollment is not None
        and other_active_enrollment.status in _REUSABLE_ENROLLMENT_STATES
    ):
        return _rejected(
            IdentityReasonCode.ALREADY_ENROLLED,
            (
                "this account already has an active enrollment in study "
                f"{other_active_enrollment.study_id}"
            ),
            "enrollment_id",
        )

    result_eligibility = eligibility or default_eligibility(timestamp)
    if not result_eligibility.eligible:
        return _rejected(
            IdentityReasonCode.INELIGIBLE,
            "this account is not eligible for the study",
            "eligibility",
        )

    enrollment = Enrollment(
        enrollment_id=uuid.uuid4(),
        participant_id=participant.participant_id,
        study_id=revision.study_id,
        study_revision_id=revision.revision_id,
        participant_code=generate_participant_code(),
        status=EnrollmentStatus.ACTIVE,
        eligibility=result_eligibility,
        enrolled_at=timestamp,
        consent_accepted_at=timestamp,
        revocation_epoch=0,
        updated_at=timestamp,
        retention_action=revision.retention_action,
    )
    return EnrollmentResult(accepted=True, enrollment=enrollment, created=True)


def apply_retention(
    enrollment: Enrollment,
    records: Sequence[PseudonymousRecord],
    action: RetentionAction,
    *,
    now: Optional[datetime] = None,
) -> RetentionResult:
    """Apply a retention action to pseudonymous records and emit a ledger row.

    * ``RETAIN_ANONYMIZED`` keeps every record but strips account-linkable
      fields.
    * ``DELETE_IDENTIFIABLE`` removes records that still carry account-linkable
      fields and keeps already-pseudonymous records.
    * ``DELETE_ALL`` removes every record.
    """
    projected: list[PseudonymousRecord] = []
    affected = 0

    for record in records:
        linkable = (
            record.account_id is not None
            or record.email is not None
            or record.session_token is not None
        )

        if action == RetentionAction.RETAIN_ANONYMIZED:
            if linkable:
                affected += 1
            projected.append(
                record.model_copy(
                    update={"account_id": None, "email": None, "session_token": None}
                )
            )
        elif action == RetentionAction.DELETE_IDENTIFIABLE:
            if linkable:
                affected += 1
                continue
            projected.append(record)
        elif action == RetentionAction.DELETE_ALL:
            affected += 1
            continue
        else:  # pragma: no cover - enum is closed
            continue

    ledger = DeletionLedgerEntry(
        ledger_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        action=action,
        applied_at=_now(now),
        affected_count=affected,
        evidence_digest=canonical_hash(
            {
                "action": action.value,
                "enrollment_id": str(enrollment.enrollment_id),
                "remaining": [record.model_dump(mode="json") for record in projected],
            }
        ),
    )
    return RetentionResult(action=action, records=projected, ledger=ledger)


# ---------------------------------------------------------------------------
# Researcher-safe projection
# ---------------------------------------------------------------------------


def researcher_projection(enrollment: Enrollment) -> dict[str, Any]:
    """Return the non-identifying, researcher-visible view of an enrollment.

    Only study-local identifiers, status and timestamps are included. The
    account id, participant id, email and session tokens are deliberately
    absent.
    """
    return {
        "enrollment_id": str(enrollment.enrollment_id),
        "participant_code": enrollment.participant_code,
        "study_id": str(enrollment.study_id),
        "study_revision_id": str(enrollment.study_revision_id),
        "status": enrollment.status.value,
        "eligible": enrollment.eligibility.eligible,
        "revocation_epoch": enrollment.revocation_epoch,
        "retention_action": enrollment.retention_action.value,
        "enrolled_at": _isoformat(enrollment.enrolled_at),
        "updated_at": _isoformat(enrollment.updated_at),
    }


# ---------------------------------------------------------------------------
# Persistence helpers (caller-supplied session)
# ---------------------------------------------------------------------------


def _merge_job_evidence(
    session: Session,
    *,
    enrollment_id: uuid.UUID,
    action: RetentionAction,
    key: str,
    payload: dict[str, Any],
) -> None:
    """Merge evidence onto the retention job for ``(enrollment, action)``.

    Policy/verification evidence belongs to the job so it survives even if the
    enrollment row is later removed.
    """
    statement = select(ResearchRetentionJob).where(
        ResearchRetentionJob.enrollment_id == enrollment_id,
        ResearchRetentionJob.action == action.value,
    )
    job = session.execute(statement).scalars().first()
    if not isinstance(job, ResearchRetentionJob):
        return
    evidence = job.evidence_json if isinstance(job.evidence_json, dict) else {}
    merged = dict(evidence)
    merged[key] = payload
    job.evidence_json = merged
    session.add(job)


def create_participant(session: Session, participant: Participant) -> ResearchParticipant:
    """Insert the private account-to-participant mapping."""
    row = ResearchParticipant(
        participant_id=participant.participant_id,
        account_id=participant.account_id,
        created_at=participant.created_at or _now(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


class ActiveEnrollmentConflict(Exception):
    """Raised when the account-wide active-enrollment index would be violated.

    A typed conflict rather than a raw ``IntegrityError``, so the router can
    report ``ALREADY_ENROLLED`` (409) naming the other enrollment instead of a
    500.
    """


def lock_participant_by_account(
    session: Session, account_id: uuid.UUID
) -> Optional[ResearchParticipant]:
    """Fetch the account's participant row with ``SELECT ... FOR UPDATE``.

    Taking this lock before the account-wide active-enrollment check is what
    serializes two concurrent cross-study joins for the same account.
    """
    statement = (
        select(ResearchParticipant)
        .where(ResearchParticipant.account_id == account_id)
        .with_for_update()
    )
    return session.execute(statement).scalars().first()


def get_or_create_participant_row(
    session: Session, account_id: uuid.UUID, *, now: Optional[datetime] = None
) -> ResearchParticipant:
    """Return the account's participant row, creating it race-safely and durably.

    Concurrent first-use uses ``INSERT ... ON CONFLICT DO NOTHING`` on the unique
    ``account_id`` and then re-reads the winning row, so exactly one mapping
    exists. The mapping is committed so it is durable and visible to the other
    first-use transactions (a caller that needs the row lock re-acquires it with
    :func:`lock_participant_by_account`).
    """
    row = session.execute(
        select(ResearchParticipant).where(
            ResearchParticipant.account_id == account_id
        )
    ).scalars().first()
    if row is None:
        session.execute(
            pg_insert(ResearchParticipant)
            .values(
                participant_id=uuid.uuid4(),
                account_id=account_id,
                created_at=_now(now),
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        session.commit()
        row = session.execute(
            select(ResearchParticipant).where(
                ResearchParticipant.account_id == account_id
            )
        ).scalars().first()
    if row is None:  # pragma: no cover - the insert above guarantees the row
        raise RuntimeError("participant mapping disappeared after an upsert")
    return row


def get_active_enrollment_for_participant(
    session: Session,
    participant_id: uuid.UUID,
    *,
    exclude_enrollment_id: Optional[uuid.UUID] = None,
) -> Optional[ResearchEnrollment]:
    """Return the account's live (active/pending/paused) enrollment, if any."""
    live_states = tuple(state.value for state in _REUSABLE_ENROLLMENT_STATES)
    statement = select(ResearchEnrollment).where(
        ResearchEnrollment.participant_id == participant_id,
        ResearchEnrollment.status.in_(live_states),
    )
    if exclude_enrollment_id is not None:
        statement = statement.where(
            ResearchEnrollment.enrollment_id != exclude_enrollment_id
        )
    return session.execute(statement).scalars().first()


def create_enrollment(session: Session, enrollment: Enrollment) -> ResearchEnrollment:
    """Insert an enrollment row, translating the one-active-index conflict.

    The partial unique index ``uq_research_enrollment_active_participant`` is the
    database guard; a violation (e.g. a race that slipped past the participant
    lock) is reported as a typed :class:`ActiveEnrollmentConflict`, never a 500.
    """
    row = ResearchEnrollment(
        enrollment_id=enrollment.enrollment_id,
        participant_id=enrollment.participant_id,
        study_id=enrollment.study_id,
        study_revision_id=enrollment.study_revision_id,
        participant_code=enrollment.participant_code,
        status=enrollment.status.value,
        revocation_epoch=enrollment.revocation_epoch,
        eligibility_json=enrollment.eligibility.model_dump(mode="json"),
        enrolled_at=enrollment.enrolled_at,
        updated_at=enrollment.updated_at,
        consent_accepted_at=enrollment.consent_accepted_at,
        retention_action=enrollment.retention_action.value,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError as error:
        raise ActiveEnrollmentConflict(
            "this account already has an active enrollment"
        ) from error
    session.commit()
    session.refresh(row)
    return row


def get_participant(
    session: Session, participant_id: uuid.UUID
) -> Optional[ResearchParticipant]:
    """Fetch a participant mapping by id, or ``None``."""
    return session.get(ResearchParticipant, participant_id)


def get_participant_by_account(
    session: Session, account_id: uuid.UUID
) -> Optional[ResearchParticipant]:
    """Fetch the private mapping for a login account, or ``None``."""
    statement = select(ResearchParticipant).where(
        ResearchParticipant.account_id == account_id
    )
    return session.execute(statement).scalars().first()


def get_enrollment(
    session: Session, enrollment_id: uuid.UUID, *, for_update: bool = False
) -> Optional[ResearchEnrollment]:
    """Fetch an enrollment by id, or ``None``.

    ``for_update`` takes a row lock (``SELECT ... FOR UPDATE``) so a caller can
    serialize with a concurrent transaction that touches the same enrollment.
    """
    if not for_update:
        return session.get(ResearchEnrollment, enrollment_id)
    statement = (
        select(ResearchEnrollment)
        .where(ResearchEnrollment.enrollment_id == enrollment_id)
        .with_for_update()
    )
    return session.execute(statement).scalars().first()


def get_enrollment_for_participant_revision(
    session: Session, participant_id: uuid.UUID, study_revision_id: uuid.UUID
) -> Optional[ResearchEnrollment]:
    """Fetch the unique enrollment for ``(participant, revision)``, or ``None``."""
    statement = select(ResearchEnrollment).where(
        ResearchEnrollment.participant_id == participant_id,
        ResearchEnrollment.study_revision_id == study_revision_id,
    )
    return session.execute(statement).scalars().first()


def get_enrollment_for_participant_study(
    session: Session, participant_id: uuid.UUID, study_id: uuid.UUID
) -> Optional[ResearchEnrollment]:
    """Fetch the participant's newest enrollment in one study, or ``None``.

    Used to refuse a self-service rejoin to a study the account already withdrew
    from or completed, including via a different revision of that study.
    """
    statement = (
        select(ResearchEnrollment)
        .where(
            ResearchEnrollment.participant_id == participant_id,
            ResearchEnrollment.study_id == study_id,
        )
        .order_by(ResearchEnrollment.enrolled_at.desc())
    )
    return session.execute(statement).scalars().first()


#: Terminal states from which a self-service rejoin to the same study is refused.
_TERMINAL_ENROLLMENT_STATES = frozenset({EnrollmentStatus.COMPLETED})


@dataclass(frozen=True)
class EnrollmentOpenResult:
    """Result of the shared account-wide enrollment entry point."""

    participant: Optional[Participant]
    enrollment: Optional[Enrollment]
    created: bool
    reused: bool
    issue: Optional[IdentityIssue]


def open_enrollment(
    session: Session,
    account_id: uuid.UUID,
    revision: RevisionRef,
    *,
    now: Optional[datetime] = None,
) -> EnrollmentOpenResult:
    """Create or reuse an enrollment under the account-wide single-active rule.

    This is the single shared entry point used by both the join-code and the
    participants routers. It locks the participant row first, so concurrent
    cross-study joins serialize, then rejects:

    * a second live enrollment for the account (``ALREADY_ENROLLED``);
    * a rejoin to a study the account already withdrew from or completed
      (``REJOIN_NOT_ALLOWED``), including via a different revision.

    Repeat enrollment in the same live revision is idempotent.
    """
    timestamp = _now(now)
    participant_row = get_or_create_participant_row(session, account_id, now=timestamp)
    # Commit the mapping durably, then hold a row lock for the whole
    # account-wide check-and-insert so concurrent cross-study joins serialize.
    locked_row = lock_participant_by_account(session, account_id)
    if locked_row is not None:
        participant_row = locked_row
    session.flush()
    participant = row_to_participant(participant_row)

    prior_study_row = get_enrollment_for_participant_study(
        session, participant.participant_id, revision.study_id
    )
    if prior_study_row is not None:
        prior_status = EnrollmentStatus(prior_study_row.status)
        if prior_status in _TERMINAL_ENROLLMENT_STATES:
            return EnrollmentOpenResult(
                participant=participant,
                enrollment=None,
                created=False,
                reused=False,
                issue=_issue(
                    IdentityReasonCode.REJOIN_NOT_ALLOWED,
                    "this account already left or completed this study; rejoin is "
                    "not available",
                    "enrollment_id",
                ),
            )

    existing_row = get_enrollment_for_participant_revision(
        session, participant.participant_id, revision.revision_id
    )
    existing = row_to_enrollment(existing_row) if existing_row is not None else None

    other_row = get_active_enrollment_for_participant(
        session,
        participant.participant_id,
        exclude_enrollment_id=existing.enrollment_id if existing is not None else None,
    )
    other = row_to_enrollment(other_row) if other_row is not None else None

    result = enroll(
        participant,
        revision,
        existing_enrollment=existing,
        other_active_enrollment=other,
        now=timestamp,
    )
    if not result.accepted or result.enrollment is None:
        return EnrollmentOpenResult(
            participant=participant,
            enrollment=None,
            created=False,
            reused=False,
            issue=result.issue,
        )

    if result.created:
        try:
            create_enrollment(session, result.enrollment)
        except ActiveEnrollmentConflict:
            return EnrollmentOpenResult(
                participant=participant,
                enrollment=None,
                created=False,
                reused=False,
                issue=_issue(
                    IdentityReasonCode.ALREADY_ENROLLED,
                    "this account already has an active enrollment",
                    "enrollment_id",
                ),
            )
    return EnrollmentOpenResult(
        participant=participant,
        enrollment=result.enrollment,
        created=result.created,
        reused=result.reused,
        issue=None,
    )


def list_enrollments(
    session: Session, participant_id: uuid.UUID
) -> Sequence[ResearchEnrollment]:
    """List a participant's enrollments, oldest-first."""
    statement = (
        select(ResearchEnrollment)
        .where(ResearchEnrollment.participant_id == participant_id)
        .order_by(ResearchEnrollment.enrolled_at.asc())
    )
    return list(session.execute(statement).scalars().all())


def _apply_enrollment_fields(
    row: ResearchEnrollment, enrollment: Enrollment
) -> None:
    row.status = enrollment.status.value
    row.revocation_epoch = enrollment.revocation_epoch
    row.updated_at = enrollment.updated_at
    row.retention_action = enrollment.retention_action.value
    row.eligibility_json = enrollment.eligibility.model_dump(mode="json")


def update_enrollment(
    session: Session, enrollment: Enrollment
) -> Optional[ResearchEnrollment]:
    """Persist a status/epoch change onto an existing enrollment row."""
    row = session.get(ResearchEnrollment, enrollment.enrollment_id)
    if row is None:
        return None
    _apply_enrollment_fields(row, enrollment)
    session.commit()
    session.refresh(row)
    return row


def lock_enrollment(
    session: Session, enrollment_id: uuid.UUID
) -> Optional[ResearchEnrollment]:
    """Fetch an enrollment row with a ``SELECT ... FOR UPDATE`` row lock.

    The lock serializes concurrent enrollment transitions for one
    enrollment; the caller owns the surrounding transaction and must commit (or
    roll back) it.
    """
    statement = (
        select(ResearchEnrollment)
        .where(ResearchEnrollment.enrollment_id == enrollment_id)
        .with_for_update()
    )
    return session.execute(statement).scalars().first()


def revoke_active_sessions(
    session: Session, enrollment_id: uuid.UUID, *, now: datetime
) -> int:
    """Mark every non-terminal session of an enrollment ``REVOKED``.

    Records a transition on each session and does not commit, so it can join the
    caller's single transaction.
    """
    statement = select(ResearchSessionRow).where(
        ResearchSessionRow.enrollment_id == enrollment_id,
        ResearchSessionRow.state.in_(_ACTIVE_SESSION_STATES),
    )
    rows = list(session.execute(statement).scalars().all())
    for row in rows:
        from_state = SessionState(row.state)
        transition = SessionTransition(
            transition_id=uuid.uuid4(),
            research_session_id=row.session_id,
            from_state=from_state,
            to_state=SessionState.REVOKED,
            occurred_at=now,
            reason=SessionReasonCode.REVOKED,
            close_reason=CloseReason.REVOKED,
            evidence_ref="study_end",
        )
        existing = row.transitions_json
        serialized = list(existing) if isinstance(existing, list) else []
        serialized.append(transition.model_dump(mode="json"))
        row.transitions_json = serialized
        row.state = SessionState.REVOKED.value
        row.last_activity_at = now
        row.closed_at = now
        row.close_reason = CloseReason.REVOKED.value
        session.add(row)
    return len(rows)


def complete_enrollments_for_study(
    session: Session, study_id: uuid.UUID, *, now: Optional[datetime] = None
) -> int:
    """Mark a study's ACTIVE enrollments COMPLETED and revoke their sessions.

    This is the terminal transition for study end/expiry: the enrollment becomes
    ``COMPLETED`` (terminal), its revocation epoch is bumped so
    previously issued capabilities are rejected, and every non-terminal session is
    revoked. Commits once. Idempotent: already-terminal enrollments are skipped.
    """
    timestamp = _now(now)
    statement = select(ResearchEnrollment).where(
        ResearchEnrollment.study_id == study_id,
        ResearchEnrollment.status == EnrollmentStatus.ACTIVE.value,
    ).with_for_update()
    rows = list(session.execute(statement).scalars().all())
    for row in rows:
        revoke_active_sessions(session, row.enrollment_id, now=timestamp)
        row.status = EnrollmentStatus.COMPLETED.value
        row.revocation_epoch = row.revocation_epoch + 1
        row.updated_at = timestamp
        session.add(row)
    session.commit()
    return len(rows)


def _enqueue_retention_row(
    session: Session, *, enrollment_id: uuid.UUID, action: RetentionAction, now: datetime
) -> Optional[ResearchRetentionJob]:
    """Insert the PENDING retention job for ``(enrollment, action)`` if absent.

    Does not commit, so the enqueue joins the caller's transaction and the whole
    workflow commits once.
    """
    statement = select(ResearchRetentionJob).where(
        ResearchRetentionJob.enrollment_id == enrollment_id,
        ResearchRetentionJob.action == action.value,
    )
    existing = session.execute(statement).scalars().first()
    if existing is not None:
        return existing
    row = ResearchRetentionJob(
        job_id=uuid.uuid4(),
        enrollment_id=enrollment_id,
        action=action.value,
        state="PENDING",
        attempts=0,
        created_at=now,
        affected_events=0,
        evidence_json={},
    )
    session.add(row)
    return row


def _retention_evidence_payload(
    action: str, entry: DeletionLedgerEntry
) -> dict[str, Any]:
    return {
        "action": action,
        "enrollment_id": str(entry.enrollment_id),
        "record": entry.model_dump(mode="json"),
    }


def insert_deletion_ledger(
    session: Session, entry: DeletionLedgerEntry
) -> DeletionLedgerEntry:
    """Append a deletion/retention ledger entry.

    The evidence is written to the generic ``research_record`` table
    (``kind = RETENTION_EVIDENCE``) and, when a retention job exists for
    ``(enrollment, action)``, merged into its ``evidence_json``.
    """
    row = ResearchRecord(
        record_id=entry.ledger_id,
        kind=RECORD_KIND_RETENTION_EVIDENCE,
        scope_type="enrollment",
        scope_id=entry.enrollment_id,
        study_id=None,
        actor=None,
        occurred_at=entry.applied_at,
        payload_json=_retention_evidence_payload(LEDGER_AUDIT_ACTION, entry),
    )
    session.add(row)
    _merge_job_evidence(
        session,
        enrollment_id=entry.enrollment_id,
        action=entry.action,
        key="ledger",
        payload=entry.model_dump(mode="json"),
    )
    session.commit()
    return entry


def list_deletion_ledger(
    session: Session, enrollment_id: Optional[uuid.UUID] = None
) -> Sequence[DeletionLedgerEntry]:
    """List deletion ledger entries (optionally for one enrollment)."""
    statement = select(ResearchRecord).where(
        ResearchRecord.kind == RECORD_KIND_RETENTION_EVIDENCE,
        ResearchRecord.payload_json["action"].astext == LEDGER_AUDIT_ACTION,
    )
    if enrollment_id is not None:
        statement = statement.where(
            ResearchRecord.payload_json["enrollment_id"].astext == str(enrollment_id)
        )
    statement = statement.order_by(ResearchRecord.occurred_at.asc())
    return [
        row_to_deletion_ledger(row)
        for row in session.execute(statement).scalars().all()
    ]


def row_to_participant(row: ResearchParticipant) -> Participant:
    """Rehydrate the private participant mapping."""
    from .models import Participant as ParticipantModel

    return ParticipantModel(
        participant_id=row.participant_id,
        account_id=row.account_id,
        created_at=row.created_at,
    )


def row_to_enrollment(row: ResearchEnrollment) -> Enrollment:
    """Rehydrate an enrollment row into the domain model."""
    from .models import Enrollment as EnrollmentModel

    status = row.status
    eligibility = ResearchEligibility.model_validate(row.eligibility_json or {})
    return EnrollmentModel(
        enrollment_id=row.enrollment_id,
        participant_id=row.participant_id,
        study_id=row.study_id,
        study_revision_id=row.study_revision_id,
        participant_code=row.participant_code,
        status=status
        if isinstance(status, EnrollmentStatus)
        else EnrollmentStatus(status),
        eligibility=eligibility,
        enrolled_at=row.enrolled_at,
        consent_accepted_at=row.consent_accepted_at,
        revocation_epoch=row.revocation_epoch,
        updated_at=row.updated_at,
        retention_action=RetentionAction(row.retention_action),
    )


def row_to_deletion_ledger(row: ResearchRecord) -> DeletionLedgerEntry:
    """Rehydrate a deletion ledger record row into the domain model."""
    from .models import DeletionLedgerEntry as DeletionLedgerEntryModel

    return DeletionLedgerEntryModel.model_validate(row.payload_json["record"])


def deletion_ledger_summary(row: DeletionLedgerEntry) -> dict[str, Any]:
    """Return a compact, non-secret deletion ledger summary."""
    applied_at = row.applied_at
    action = row.action
    return {
        "ledger_id": str(row.ledger_id),
        "enrollment_id": str(row.enrollment_id),
        "action": action.value if isinstance(action, RetentionAction) else action,
        "applied_at": (
            applied_at.isoformat() if isinstance(applied_at, datetime) else None
        ),
        "affected_count": row.affected_count,
        "evidence_digest": row.evidence_digest,
    }
