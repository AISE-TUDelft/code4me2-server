"""Read-only SQL loading for the study analytics read models (issue 03).

Every event query is study-scoped (``study_id``), excludes retention tombstones
(``retention_state = 'DELETED'``) and selects only scalar columns plus the
metadata JSONB paths the metric rows need (``envelope_json -> 'payload' ->>
'tool_kind'`` etc.): never a whole envelope and never a content field (prompt or
message text, tool arguments/results, reasoning, error messages). Nothing here
joins login identity (``research_participant``/``user``) and nothing writes.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import String, and_, cast, func, literal_column, not_, or_, select, text

from database.research_schemas import (
    ResearchEnrollment,
    ResearchEvent,
    StudyAgentProfile,
    StudyAssignment,
)
from database.research_schemas import ResearchSessionV1 as ResearchSessionRow

from .metrics import (
    ANALYTIC_EVENT_TYPES,
    DOCUMENT_CHANGED_EVENT,
    IDE_SOURCE,
    MAX_TIMELINE,
    PERMISSION_EVENTS,
    POLICY_DECISION_SCOPE,
    PROMPT_EVENT,
    RELAY_SOURCE,
    TIMELINE_EXCLUDED_EVENT_TYPES,
)
from .models import (
    ArmRow,
    AssignmentRow,
    DailyEventCount,
    DateWindow,
    EnrollmentRow,
    EventRow,
    SessionRow,
    StudyFrame,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.orm import Session

__all__ = [
    "enrollment_study_id",
    "load_daily_event_counts",
    "load_events",
    "load_study_frame",
    "load_timeline",
]

_ENVELOPE = ResearchEvent.envelope_json
_EVENT_BATCH_SIZE = 10_000
_LIFECYCLE = _ENVELOPE["lifecycle_state"].astext
_DELETED = "DELETED"


def _payload(key: str):
    return _ENVELOPE["payload"][key].astext


def _correlation(key: str):
    return _ENVELOPE["correlations"][key].astext


def _metric(key: str):
    return _ENVELOPE["metrics"][key].astext


def _metric_count(key: str):
    return _ENVELOPE["metrics"]["counts"][key].astext


#: The metadata-only projection of one event (order matters: see ``_event_row``).
#: Ids are read as text: the rows only compare them as strings, and building
#: UUID objects for every row costs about a third of a large study's load.
_EVENT_COLUMNS = (
    cast(ResearchEvent.enrollment_id, String),
    cast(ResearchEvent.research_session_id, String),
    ResearchEvent.event_type,
    ResearchEvent.source,
    ResearchEvent.emitter_id,
    ResearchEvent.emitter_sequence,
    ResearchEvent.occurred_at,
    _correlation("turn_id"),
    func.coalesce(_correlation("tool_call_id"), _payload("tool_call_id")),
    _correlation("permission_id"),
    _payload("tool_name"),
    _payload("tool_kind"),
    _payload("status"),
    _LIFECYCLE,
    _payload("decision"),
    _payload("decision_scope"),
    _payload("stop_reason"),
    _payload("error_code"),
    _payload("message_kind"),
    _metric("usage_tokens"),
    _metric_count("prompt_tokens"),
    _metric("latency_ms"),
    _payload("plan_size"),
    # A small list of {"status", "count"} records (plan entry text is never
    # captured); null on every other event type.
    _ENVELOPE["payload"]["plan_status_counts"],
)


def _chunk_clause():
    """A streamed assistant/thought chunk (null-safe: always true or false)."""
    return and_(
        ResearchEvent.event_type == PROMPT_EVENT,
        or_(
            _payload("message_kind").isnot(None),
            func.coalesce(_LIFECYCLE, "") == "started",
        ),
    )


def _policy_decision_clause():
    """A relay permission report no person was asked for (null-safe)."""
    return and_(
        ResearchEvent.source == RELAY_SOURCE,
        ResearchEvent.event_type.in_(sorted(PERMISSION_EVENTS)),
        func.coalesce(_payload("decision_scope"), "") == POLICY_DECISION_SCOPE,
    )


def _text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number != int(number):
        return None
    return int(number)


def _plan_completed(value: Any) -> Optional[int]:
    if not isinstance(value, list):
        return None
    completed = 0
    for entry in value:
        if isinstance(entry, dict) and entry.get("status") == "completed":
            completed += _int(entry.get("count")) or 0
    return completed


def _event_row(row: Any) -> EventRow:
    (
        enrollment_id,
        session_id,
        event_type,
        source,
        emitter_id,
        emitter_sequence,
        occurred_at,
        turn_id,
        tool_call_id,
        permission_id,
        tool_name,
        tool_kind,
        status,
        lifecycle_state,
        decision,
        decision_scope,
        stop_reason,
        error_code,
        message_kind,
        usage_tokens,
        prompt_tokens,
        latency_ms,
        plan_size,
        plan_status_counts,
    ) = tuple(row)
    return EventRow(
        event_type=event_type,
        source=source,
        occurred_at=occurred_at,
        enrollment_id=_text(enrollment_id),
        session_id=_text(session_id),
        emitter_id=emitter_id or "",
        emitter_sequence=int(emitter_sequence or 0),
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        permission_id=permission_id,
        tool_name=tool_name,
        tool_kind=tool_kind,
        status=status,
        lifecycle_state=lifecycle_state,
        decision=decision,
        decision_scope=decision_scope,
        stop_reason=stop_reason,
        error_code=error_code,
        message_kind=message_kind,
        usage_tokens=_int(usage_tokens),
        prompt_tokens=_int(prompt_tokens),
        latency_ms=_int(latency_ms),
        plan_size=_int(plan_size),
        plan_completed=_plan_completed(plan_status_counts),
    )


def _scoped(
    statement,
    study_id: uuid.UUID,
    *,
    enrollment_id: Optional[uuid.UUID] = None,
    window: Optional[DateWindow] = None,
):
    statement = statement.where(
        ResearchEvent.study_id == study_id,
        ResearchEvent.retention_state != _DELETED,
        ResearchEvent.enrollment_id.isnot(None),
    )
    if enrollment_id is not None:
        statement = statement.where(ResearchEvent.enrollment_id == enrollment_id)
    if window is not None:
        lower, upper = window.bounds()
        if lower is not None:
            statement = statement.where(ResearchEvent.occurred_at >= lower)
        if upper is not None:
            statement = statement.where(ResearchEvent.occurred_at < upper)
    return statement


def enrollment_study_id(session: Session, enrollment_id: uuid.UUID) -> Optional[uuid.UUID]:
    """The study an enrollment belongs to, or ``None`` when it does not exist."""
    return session.execute(
        select(ResearchEnrollment.study_id).where(
            ResearchEnrollment.enrollment_id == enrollment_id
        )
    ).scalar_one_or_none()


def load_study_frame(
    session: Session,
    study_id: uuid.UUID,
    *,
    enrollment_id: Optional[uuid.UUID] = None,
) -> StudyFrame:
    """Arms, enrollments, assignments and sessions (no login identity)."""
    arm_snapshot = StudyAgentProfile.profile_snapshot_json
    arms = [
        ArmRow(
            profile_id=str(profile_id),
            name=name,
            model=model,
            framework_version=framework_version,
            selection_order=selection_order,
            max_context_tokens=_int(max_context_tokens),
        )
        for (
            profile_id,
            selection_order,
            name,
            model,
            framework_version,
            max_context_tokens,
        ) in session.execute(
            select(
                StudyAgentProfile.profile_id,
                StudyAgentProfile.selection_order,
                arm_snapshot["name"].astext,
                arm_snapshot["model"].astext,
                arm_snapshot["framework_version"].astext,
                arm_snapshot["max_context_tokens"].astext,
            )
            .where(StudyAgentProfile.study_id == study_id)
            .order_by(StudyAgentProfile.selection_order.asc())
        ).all()
    ]

    enrollment_statement = select(
        ResearchEnrollment.enrollment_id,
        ResearchEnrollment.participant_code,
        ResearchEnrollment.status,
        ResearchEnrollment.enrolled_at,
        ResearchEnrollment.consent_accepted_at,
    ).where(ResearchEnrollment.study_id == study_id)
    if enrollment_id is not None:
        enrollment_statement = enrollment_statement.where(
            ResearchEnrollment.enrollment_id == enrollment_id
        )
    enrollments = [
        EnrollmentRow(
            enrollment_id=str(row_id),
            participant_code=participant_code,
            status=str(status),
            enrolled_at=enrolled_at,
            consent_accepted_at=consent_accepted_at,
        )
        for row_id, participant_code, status, enrolled_at, consent_accepted_at in session.execute(
            enrollment_statement
        ).all()
    ]

    assignment_snapshot = StudyAssignment.profile_snapshot_json
    assignment_statement = (
        select(
            StudyAssignment.enrollment_id,
            StudyAssignment.agent_profile_id,
            StudyAssignment.status,
            StudyAssignment.assigned_at,
            assignment_snapshot["name"].astext,
            assignment_snapshot["model"].astext,
            assignment_snapshot["framework_version"].astext,
            assignment_snapshot["max_context_tokens"].astext,
        )
        .join(
            ResearchEnrollment,
            StudyAssignment.enrollment_id == ResearchEnrollment.enrollment_id,
        )
        .where(ResearchEnrollment.study_id == study_id)
    )
    if enrollment_id is not None:
        assignment_statement = assignment_statement.where(
            StudyAssignment.enrollment_id == enrollment_id
        )
    assignments = [
        AssignmentRow(
            enrollment_id=str(row_enrollment_id),
            profile_id=str(profile_id),
            status=str(status),
            assigned_at=assigned_at,
            name=name,
            model=model,
            framework_version=framework_version,
            max_context_tokens=_int(max_context_tokens),
        )
        for (
            row_enrollment_id,
            profile_id,
            status,
            assigned_at,
            name,
            model,
            framework_version,
            max_context_tokens,
        ) in session.execute(assignment_statement).all()
    ]

    session_statement = (
        select(
            ResearchSessionRow.session_id,
            ResearchSessionRow.enrollment_id,
            ResearchSessionRow.state,
            ResearchSessionRow.opened_at,
            ResearchSessionRow.closed_at,
            ResearchSessionRow.last_activity_at,
            ResearchSessionRow.last_heartbeat_at,
            ResearchSessionRow.close_reason,
            ResearchSessionRow.created_at,
        )
        .join(
            ResearchEnrollment,
            ResearchSessionRow.enrollment_id == ResearchEnrollment.enrollment_id,
        )
        .where(ResearchEnrollment.study_id == study_id)
    )
    if enrollment_id is not None:
        session_statement = session_statement.where(
            ResearchSessionRow.enrollment_id == enrollment_id
        )
    sessions = [
        SessionRow(
            session_id=str(session_id),
            enrollment_id=str(row_enrollment_id),
            state=str(state),
            opened_at=opened_at,
            closed_at=closed_at,
            last_activity_at=last_activity_at,
            last_heartbeat_at=last_heartbeat_at,
            close_reason=close_reason,
            created_at=created_at,
        )
        for (
            session_id,
            row_enrollment_id,
            state,
            opened_at,
            closed_at,
            last_activity_at,
            last_heartbeat_at,
            close_reason,
            created_at,
        ) in session.execute(session_statement).all()
    ]
    return StudyFrame(
        arms=arms, enrollments=enrollments, assignments=assignments, sessions=sessions
    )


def load_events(
    session: Session,
    study_id: uuid.UUID,
    *,
    enrollment_id: Optional[uuid.UUID] = None,
    window: Optional[DateWindow] = None,
) -> list[EventRow]:
    """The metadata-only events the metric set reads (chunks excluded)."""
    statement = _scoped(
        select(*_EVENT_COLUMNS), study_id, enrollment_id=enrollment_id, window=window
    ).where(
        ResearchEvent.event_type.in_(sorted(ANALYTIC_EVENT_TYPES)),
        not_(_chunk_clause()),
    )
    statement = statement.order_by(
        ResearchEvent.occurred_at.asc(),
        ResearchEvent.emitter_id.asc(),
        ResearchEvent.emitter_sequence.asc(),
    )
    # Streamed in batches: a large study has hundreds of thousands of rows, and
    # buffering every driver row next to its EventRow doubles the memory peak.
    # Streaming uses a server-side cursor, for which PostgreSQL favours
    # fast-start plans (walking the global occurred_at index across every
    # study); this query is always read to the end, so plan it as one.
    session.execute(text("SET LOCAL cursor_tuple_fraction = 1.0"))
    result = session.execute(statement.execution_options(yield_per=_EVENT_BATCH_SIZE))
    return [_event_row(row) for row in result]


def load_timeline(
    session: Session,
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    *,
    limit: int = MAX_TIMELINE,
) -> list[EventRow]:
    """The newest metadata-only events for a participant's replay timeline.

    Streamed message chunks, per-keystroke document changes, context-usage
    updates and the built-in agent's policy-scope permission reports (one per
    tool call) are left out; they would drown every other event.
    """
    statement = (
        _scoped(select(*_EVENT_COLUMNS), study_id, enrollment_id=enrollment_id)
        .where(
            ResearchEvent.event_type.notin_(sorted(TIMELINE_EXCLUDED_EVENT_TYPES)),
            not_(_chunk_clause()),
            not_(_policy_decision_clause()),
        )
        .order_by(
            ResearchEvent.occurred_at.desc(),
            ResearchEvent.emitter_id.desc(),
            ResearchEvent.emitter_sequence.desc(),
        )
        .limit(limit)
    )
    return [_event_row(row) for row in session.execute(statement).all()]


def load_daily_event_counts(
    session: Session,
    study_id: uuid.UUID,
    *,
    enrollment_id: Optional[uuid.UUID] = None,
    window: Optional[DateWindow] = None,
) -> list[DailyEventCount]:
    """Per-enrollment, per-UTC-date counts over every retained event."""
    day = func.date(func.timezone(literal_column("'UTC'"), ResearchEvent.occurred_at))
    statement = _scoped(
        select(
            ResearchEvent.enrollment_id,
            day,
            func.count(),
            func.count().filter(
                and_(
                    ResearchEvent.event_type == DOCUMENT_CHANGED_EVENT,
                    ResearchEvent.source == IDE_SOURCE,
                )
            ),
            func.min(ResearchEvent.occurred_at),
            func.max(ResearchEvent.occurred_at),
        ),
        study_id,
        enrollment_id=enrollment_id,
        window=window,
    ).group_by(ResearchEvent.enrollment_id, day)
    return [
        DailyEventCount(
            enrollment_id=str(row_enrollment_id),
            day=row_day,
            events=int(events or 0),
            ide_edits=int(ide_edits or 0),
            first_at=first_at,
            last_at=last_at,
        )
        for row_enrollment_id, row_day, events, ide_edits, first_at, last_at in session.execute(
            statement
        ).all()
    ]
