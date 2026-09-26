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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import String, and_, case, cast, distinct, func, not_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from database.db_schemas import Study as StudyRow
from database.research_schemas import (
    RECORD_KIND_RETENTION_EVIDENCE,
    ResearchEnrollment,
    ResearchEvent,
    ResearchParticipant,
    ResearchRecord,
    ResearchRetentionJob,
    StudyAssignment,
)
from database.research_schemas import (
    ResearchSessionV1 as ResearchSessionRow,
)
from database.db_schemas import Study as StudyRow
from research.budget import ledger as budget_ledger
from research.canonical import canonical_hash
from research.study.agents.enums import METERED_FRAMEWORKS
from research.runtime.sessions.enums import (
    CloseReason,
    SessionReasonCode,
    SessionState,
)
from research.runtime.sessions.models import SessionTransition
from research.study.protocol.enums import RetentionAction
from research.telemetry.enums import CanonicalEventType, EventSource

from .enums import (
    EnrollmentStatus,
    IdentityReasonCode,
)
from .models import (
    DeletionLedgerEntry,
    Enrollment,
    IdentityIssue,
    Participant,
    PseudonymousRecord,
    ResearchEligibility,
    RetentionResult,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


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
        "status": enrollment.status.value,
        "eligible": enrollment.eligibility.eligible,
        "revocation_epoch": enrollment.revocation_epoch,
        "retention_action": enrollment.retention_action.value,
        "enrolled_at": _isoformat(enrollment.enrolled_at),
        "updated_at": _isoformat(enrollment.updated_at),
    }


# ---------------------------------------------------------------------------
# Participant self-view ("My studies")
# ---------------------------------------------------------------------------

#: Participant-facing runtime names. A participant learns only the runtime
#: *kind* (whether an agent must be installed), never the arm it was randomized
#: to: no profile name/id, model or digest is ever derived from here.
RUNTIME_DISPLAY_NAMES: dict[str, str] = {
    "code4me2-agent": "Code4Me agent (built-in)",
    "goose": "Goose (install on your machine)",
    "codex": "Codex (install on your machine)",
}

#: A participant prompt is one ACP ``session/prompt`` observation. Streamed
#: assistant/thought chunks share the event type but carry ``message_kind`` or
#: the ``started`` lifecycle state (the study analytics apply the same rule).
PROMPT_EVENT_TYPE = CanonicalEventType.AGENT_MESSAGE_STARTED.value
#: A tool call is counted once across all of its ``tool.*`` lifecycle events.
TOOL_CALL_EVENT_TYPES = (
    CanonicalEventType.TOOL_CREATED.value,
    CanonicalEventType.TOOL_STARTED.value,
    CanonicalEventType.TOOL_COMPLETED.value,
    CanonicalEventType.TOOL_FAILED.value,
)
_TERMINAL_SESSION_STATES = (SessionState.ENDED.value, SessionState.REVOKED.value)


def participant_runtime_view(snapshot: Any) -> Optional[dict[str, str]]:
    """Runtime kind of a frozen assignment snapshot, or ``None``.

    Only ``framework_version`` is read from the snapshot; everything else in it
    (profile identity, model, tools, digest) is arm detail the participant must
    stay blind to.
    """
    if not isinstance(snapshot, Mapping):
        return None
    framework = str(snapshot.get("framework_version") or "").strip().lower()
    if not framework:
        return None
    return {
        "framework_version": framework,
        "display_name": RUNTIME_DISPLAY_NAMES.get(framework, framework),
        # "shared": the study provides model access through its own key;
        # "own": the participant signs in with their own account (Codex).
        "credentials": "shared" if framework in METERED_FRAMEWORKS else "own",
    }


def get_studies_by_id(
    session: Session, study_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, StudyRow]:
    """Fetch study rows by id in one query (missing ids are simply absent)."""
    ids = list({study_id for study_id in study_ids if study_id is not None})
    if not ids:
        return {}
    statement = select(StudyRow).where(StudyRow.study_id.in_(ids))
    return {row.study_id: row for row in session.execute(statement).scalars().all()}


def get_assignment_frameworks(
    session: Session, enrollment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Optional[dict[str, str]]]:
    """``{enrollment_id: participant runtime view}`` from the frozen assignment.

    Reads only the snapshot's framework; enrollments without an assignment are
    absent from the result.
    """
    if not enrollment_ids:
        return {}
    statement = select(
        StudyAssignment.enrollment_id,
        StudyAssignment.profile_snapshot_json["framework_version"].astext,
    ).where(StudyAssignment.enrollment_id.in_(list(enrollment_ids)))
    return {
        enrollment_id: participant_runtime_view({"framework_version": framework})
        for enrollment_id, framework in session.execute(statement).all()
    }


def summarize_enrollment_sessions(
    session: Session, enrollment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """``{enrollment_id: {total, active, last_activity_at}}`` in one query.

    ``active`` counts non-terminal sessions (neither ``ended`` nor ``revoked``),
    the same rule as the study read metrics.
    """
    if not enrollment_ids:
        return {}
    statement = (
        select(
            ResearchSessionRow.enrollment_id,
            func.count(ResearchSessionRow.session_id),
            func.count(ResearchSessionRow.session_id).filter(
                ResearchSessionRow.state.not_in(_TERMINAL_SESSION_STATES)
            ),
            func.max(ResearchSessionRow.last_activity_at),
        )
        .where(ResearchSessionRow.enrollment_id.in_(list(enrollment_ids)))
        .group_by(ResearchSessionRow.enrollment_id)
    )
    return {
        enrollment_id: {
            "total": int(total or 0),
            "active": int(active or 0),
            "last_activity_at": _isoformat(last_activity_at),
        }
        for enrollment_id, total, active, last_activity_at in session.execute(
            statement
        ).all()
    }


def summarize_enrollment_activity(
    session: Session, enrollment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """``{enrollment_id: {prompts, tool_calls, last_event_at}}`` in two queries.

    Retention tombstones (``retention_state = 'DELETED'``) are excluded. The
    counts follow the study analytics (``research.analysis.study_analytics``
    ``analyze_participant``) so a participant and their researcher see the
    same numbers:

    * ``prompts`` counts ACP ``agent.message.started`` events that are not
      streamed chunks (no ``payload.message_kind``, lifecycle not ``started``).
    * ``tool_calls`` counts distinct calls over the ``tool.*`` events. The id is
      ``correlations.tool_call_id``, else ``payload.tool_call_id`` (an empty
      id is no id). An ACP call is one (emitter, id) of its session; an id-less
      ACP event (such as ``terminal/create``) belongs to the call that owns it
      and is not counted. The inference relay counts one call per id, or per
      id-less event, and only in sessions without ACP calls: where the ACP
      proxy observed the session, its calls are the authoritative ones.
    """
    if not enrollment_ids:
        return {}
    ids = list(enrollment_ids)
    envelope = ResearchEvent.envelope_json
    retained = ResearchEvent.retention_state != "DELETED"
    is_chunk = or_(
        envelope[("payload", "message_kind")].astext.isnot(None),
        func.coalesce(envelope["lifecycle_state"].astext, "") == "started",
    )
    activity_statement = (
        select(
            ResearchEvent.enrollment_id,
            func.count(ResearchEvent.event_id).filter(
                ResearchEvent.event_type == PROMPT_EVENT_TYPE,
                ResearchEvent.source == EventSource.ACP.value,
                not_(is_chunk),
            ),
            func.max(ResearchEvent.occurred_at),
        )
        .where(ResearchEvent.enrollment_id.in_(ids), retained)
        .group_by(ResearchEvent.enrollment_id)
    )

    is_relay = ResearchEvent.source == EventSource.RELAY.value
    tool_call_id = func.nullif(
        func.coalesce(
            envelope[("correlations", "tool_call_id")].astext,
            envelope[("payload", "tool_call_id")].astext,
        ),
        "",
    )
    # JSON arrays keep the key parts unambiguous whatever the ids contain.
    acp_key = case(
        (
            and_(not_(is_relay), tool_call_id.isnot(None)),
            cast(func.jsonb_build_array(ResearchEvent.emitter_id, tool_call_id), String),
        ),
        else_=None,
    )
    relay_key = case(
        (
            and_(is_relay, tool_call_id.isnot(None)),
            cast(func.jsonb_build_array("id", tool_call_id), String),
        ),
        (
            is_relay,
            cast(
                func.jsonb_build_array(
                    "run", ResearchEvent.emitter_id, ResearchEvent.emitter_sequence
                ),
                String,
            ),
        ),
        else_=None,
    )
    per_session = (
        select(
            ResearchEvent.enrollment_id.label("enrollment_id"),
            func.count(distinct(acp_key)).label("acp_calls"),
            func.count(distinct(relay_key)).label("relay_calls"),
        )
        .where(
            ResearchEvent.enrollment_id.in_(ids),
            retained,
            ResearchEvent.event_type.in_(TOOL_CALL_EVENT_TYPES),
        )
        .group_by(ResearchEvent.enrollment_id, ResearchEvent.research_session_id)
        .subquery()
    )
    calls_statement = select(
        per_session.c.enrollment_id,
        func.sum(
            case(
                (per_session.c.acp_calls > 0, per_session.c.acp_calls),
                else_=per_session.c.relay_calls,
            )
        ),
    ).group_by(per_session.c.enrollment_id)
    tool_calls = {
        enrollment_id: int(total or 0)
        for enrollment_id, total in session.execute(calls_statement).all()
    }
    return {
        enrollment_id: {
            "prompts": int(prompts or 0),
            "tool_calls": tool_calls.get(enrollment_id, 0),
            "last_event_at": _isoformat(last_event_at),
        }
        for enrollment_id, prompts, last_event_at in session.execute(
            activity_statement
        ).all()
    }


def participant_enrollment_details(
    session: Session, enrollments: Sequence[Enrollment]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Batched per-enrollment facts for the participant's own study view.

    Five queries regardless of the number of enrollments. Each entry holds
    ``study_row`` (the study row or ``None``), ``runtime`` (the runtime-kind
    view or ``None``), ``sessions`` and ``activity`` summaries (zero-valued
    when nothing was recorded). No arm detail is selected.
    """
    enrollment_ids = [enrollment.enrollment_id for enrollment in enrollments]
    studies = get_studies_by_id(
        session, (enrollment.study_id for enrollment in enrollments)
    )
    runtimes = get_assignment_frameworks(session, enrollment_ids)
    sessions = summarize_enrollment_sessions(session, enrollment_ids)
    activity = summarize_enrollment_activity(session, enrollment_ids)
    budgets = budget_ledger.participant_views(session, enrollment_ids)
    return {
        enrollment.enrollment_id: {
            "study_row": studies.get(enrollment.study_id),
            "runtime": runtimes.get(enrollment.enrollment_id),
            # Arm-blind budget numbers (unit, limit, consumed, remaining, flags).
            "budget": budgets.get(enrollment.enrollment_id),
            "sessions": sessions.get(
                enrollment.enrollment_id,
                {"total": 0, "active": 0, "last_activity_at": None},
            ),
            "activity": activity.get(
                enrollment.enrollment_id,
                {"prompts": 0, "tool_calls": 0, "last_event_at": None},
            ),
        }
        for enrollment in enrollments
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
    session: Session,
    account_id: uuid.UUID,
    *,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> ResearchParticipant:
    """Return the account's participant row, creating it race-safely and durably.

    Concurrent first-use uses ``INSERT ... ON CONFLICT DO NOTHING`` on the unique
    ``account_id`` and then re-reads the winning row, so exactly one mapping
    exists. The mapping is committed so it is durable and visible to the other
    first-use transactions (a caller that needs the row lock re-acquires it with
    :func:`lock_participant_by_account`). ``commit=False`` keeps the mapping in
    the caller's transaction, which is required by web consent.
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
        if commit:
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
            session.flush()
            # The inference budget is born with the enrollment, in the same
            # transaction, at the study's current default (0 = refuse until set).
            study = session.get(StudyRow, enrollment.study_id)
            budget_ledger.create_balance(
                session,
                enrollment_id=row.enrollment_id,
                study_id=enrollment.study_id,
                limit_micro_usd=int(
                    getattr(study, "inference_budget_default_micro_usd", 0) or 0
                ),
                now=enrollment.enrolled_at,
            )
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
