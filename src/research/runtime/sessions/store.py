"""CRUD-style persistence helpers for research sessions and agent runs.

These functions take a caller-managed SQLAlchemy ``Session`` so the core package
never imports ``App`` or touches the application singleton. Session and agent-run
rows are distinct tables/identities; the store never treats one id as the other.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

    from .models import (
        AgentRunV1,
        ResearchSessionV1,
        SessionTransition,
    )

from database.research_schemas import ResearchAgentRun
from database.research_schemas import ResearchSessionV1 as ResearchSessionRow

from .enums import AgentRunOutcome, CloseReason, SessionState

_TERMINAL_STATES = (SessionState.ENDED.value, SessionState.REVOKED.value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_row(research_session: ResearchSessionV1) -> ResearchSessionRow:
    return ResearchSessionRow(
        session_id=research_session.research_session_id,
        enrollment_id=research_session.enrollment_id,
        study_id=research_session.study_id,
        context_id=research_session.context_id or str(research_session.research_session_id),
        state=research_session.state.value,
        opened_at=research_session.opened_at,
        last_activity_at=research_session.last_activity_at,
        closed_at=research_session.closed_at,
        close_reason=(
            research_session.close_reason.value
            if research_session.close_reason is not None
            else None
        ),
        resume_generation=research_session.resume_generation,
        manifest_digest=research_session.manifest_digest,
        environment_json={
            "environment_ref": research_session.environment_ref
        },
        created_at=_now(),
    )


def create_session(
    session: Session,
    research_session: ResearchSessionV1,
    *,
    commit: bool = True,
) -> ResearchSessionRow:
    """Insert a new research session row, atomically.

    The partial unique index ``uq_research_session_active_context`` allows at
    most one non-terminal session per ``(enrollment_id, context_id)``. A savepoint
    around the insert means a concurrent create that loses the race rolls back
    only its own insert and returns the winning session for that same context
    instead of leaving a duplicate (or aborting the caller's transaction).

    ``commit=False`` lets bootstrap own one unit of work (assignment + session
    commit together, or neither).
    """
    row = _session_row(research_session)
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        existing = get_active_session_for_context(
            session, research_session.enrollment_id, row.context_id
        )
        if existing is not None:
            return existing
        raise
    if commit:
        session.commit()
    else:
        session.flush()
    session.refresh(row)
    return row


def get_session(
    session: Session, research_session_id: uuid.UUID, *, for_update: bool = False
) -> Optional[ResearchSessionRow]:
    """Fetch a session row by id, or ``None``.

    ``for_update`` takes a row lock (``SELECT ... FOR UPDATE``) so ingestion can
    serialize concurrent batches for the same session.
    """
    if not for_update:
        return session.get(ResearchSessionRow, research_session_id)
    statement = (
        select(ResearchSessionRow)
        .where(ResearchSessionRow.session_id == research_session_id)
        .with_for_update()
    )
    return session.execute(statement).scalars().first()


def list_sessions(
    session: Session, enrollment_id: Optional[uuid.UUID] = None
) -> Sequence[ResearchSessionRow]:
    """List sessions, optionally for one enrollment, newest-first."""
    statement = select(ResearchSessionRow)
    if enrollment_id is not None:
        statement = statement.where(ResearchSessionRow.enrollment_id == enrollment_id)
    statement = statement.order_by(ResearchSessionRow.created_at.desc())
    return list(session.execute(statement).scalars().all())


def get_active_session_for_enrollment(
    session: Session, enrollment_id: uuid.UUID
) -> Optional[ResearchSessionRow]:
    """Return the newest non-terminal session for an enrollment, or ``None``."""
    statement = (
        select(ResearchSessionRow)
        .where(
            ResearchSessionRow.enrollment_id == enrollment_id,
            ResearchSessionRow.state.notin_(_TERMINAL_STATES),
        )
        .order_by(ResearchSessionRow.created_at.desc())
    )
    return session.execute(statement).scalars().first()


def get_active_session_for_context(
    session: Session, enrollment_id: uuid.UUID, context_id: str
) -> Optional[ResearchSessionRow]:
    """Return the non-terminal session for ``(enrollment, context)``, or ``None``.

    This is the idempotent-creation lookup: one authenticated execution context
    maps to at most one live session.
    """
    statement = (
        select(ResearchSessionRow)
        .where(
            ResearchSessionRow.enrollment_id == enrollment_id,
            ResearchSessionRow.context_id == context_id,
            ResearchSessionRow.state.notin_(_TERMINAL_STATES),
        )
        .order_by(ResearchSessionRow.created_at.desc())
    )
    return session.execute(statement).scalars().first()


def update_session(
    session: Session, research_session: ResearchSessionV1
) -> Optional[ResearchSessionRow]:
    """Persist a state/timestamp/close change onto an existing session row."""
    row = session.get(ResearchSessionRow, research_session.research_session_id)
    if row is None:
        return None
    row.state = research_session.state.value
    row.opened_at = research_session.opened_at
    row.last_activity_at = research_session.last_activity_at
    row.closed_at = research_session.closed_at
    row.close_reason = (
        research_session.close_reason.value
        if research_session.close_reason is not None
        else None
    )
    row.resume_generation = research_session.resume_generation
    row.manifest_digest = research_session.manifest_digest
    row.environment_json = {"environment_ref": research_session.environment_ref}
    session.commit()
    session.refresh(row)
    return row


def insert_transition(
    session: Session, transition: SessionTransition
) -> SessionTransition:
    """Append one recorded state transition to its session's log.

    The transition log is ``research_session.transitions_json`` (the session is
    the only owner of its transitions), so no child row is written.
    """
    row = session.get(ResearchSessionRow, transition.research_session_id)
    if row is None:  # pragma: no cover - guarded by the session router
        raise ValueError(f"research session {transition.research_session_id} not found")
    existing = row.transitions_json
    serialized = list(existing) if isinstance(existing, list) else []
    serialized.append(transition.model_dump(mode="json"))
    row.transitions_json = serialized
    session.add(row)
    session.commit()
    session.refresh(row)
    return transition


def create_agent_run(session: Session, run: AgentRunV1) -> ResearchAgentRun:
    """Insert a new agent-run row (distinct id from its session)."""
    row = ResearchAgentRun(
        agent_run_id=run.agent_run_id,
        research_session_id=run.research_session_id,
        agent_release_id=run.agent_release_id,
        assignment_id=run.assignment_id,
        agent_profile_id=run.agent_profile_id,
        profile_digest=run.profile_digest,
        profile_snapshot_json=run.profile_snapshot_json,
        started_at=run.started_at,
        ended_at=run.ended_at,
        outcome=run.outcome.value if run.outcome is not None else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_agent_run(
    session: Session, agent_run_id: uuid.UUID
) -> Optional[ResearchAgentRun]:
    """Fetch an agent-run row by id, or ``None``."""
    return session.get(ResearchAgentRun, agent_run_id)


def list_agent_runs(
    session: Session, research_session_id: uuid.UUID
) -> Sequence[ResearchAgentRun]:
    """List the agent runs of one session, oldest-first."""
    statement = (
        select(ResearchAgentRun)
        .where(ResearchAgentRun.research_session_id == research_session_id)
        .order_by(ResearchAgentRun.started_at.asc())
    )
    return list(session.execute(statement).scalars().all())


def row_to_session(row: ResearchSessionRow) -> ResearchSessionV1:
    """Rehydrate a session row into the domain model."""
    from .models import ResearchSessionV1 as ResearchSessionModel

    state = row.state
    close_reason = row.close_reason
    environment = row.environment_json or {}
    return ResearchSessionModel(
        research_session_id=row.session_id,
        enrollment_id=row.enrollment_id,
        study_id=row.study_id,
        context_id=row.context_id or "",
        state=state if isinstance(state, SessionState) else SessionState(state),
        opened_at=row.opened_at,
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
        close_reason=(
            close_reason
            if isinstance(close_reason, CloseReason) or close_reason is None
            else CloseReason(close_reason)
        ),
        resume_generation=row.resume_generation,
        manifest_digest=row.manifest_digest,
        environment_ref=environment.get("environment_ref"),
    )


def row_to_agent_run(row: ResearchAgentRun) -> AgentRunV1:
    """Rehydrate an agent-run row into the domain model."""
    from .models import AgentRunV1 as AgentRunModel

    outcome = row.outcome
    return AgentRunModel(
        agent_run_id=row.agent_run_id,
        research_session_id=row.research_session_id,
        agent_release_id=row.agent_release_id,
        assignment_id=row.assignment_id,
        agent_profile_id=row.agent_profile_id,
        profile_digest=row.profile_digest,
        profile_snapshot_json=row.profile_snapshot_json,
        started_at=row.started_at,
        ended_at=row.ended_at,
        outcome=(
            outcome
            if isinstance(outcome, AgentRunOutcome) or outcome is None
            else AgentRunOutcome(outcome)
        ),
    )


def row_to_transition(row: Mapping[str, Any]) -> SessionTransition:
    """Rehydrate one serialized ``transitions_json`` entry."""
    from .models import SessionTransition as SessionTransitionModel

    return SessionTransitionModel.model_validate(dict(row))


def session_summary(row: ResearchSessionRow) -> dict[str, Any]:
    """Return a compact, non-secret session summary safe for responses."""
    environment = row.environment_json or {}
    return {
        "research_session_id": str(row.session_id),
        "enrollment_id": str(row.enrollment_id),
        "study_id": str(row.study_id),
        "context_id": row.context_id,
        "state": row.state,
        "opened_at": _iso(row.opened_at),
        "last_activity_at": _iso(row.last_activity_at),
        "closed_at": _iso(row.closed_at),
        "close_reason": row.close_reason,
        "resume_generation": row.resume_generation,
        "manifest_digest": row.manifest_digest,
        "environment_ref": environment.get("environment_ref"),
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None
