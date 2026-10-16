"""Read one ACP chat's retained events in a stable, resumable order."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import String, and_, cast, func, literal, or_, select, tuple_

from database.research_schemas import ResearchEvent

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["TraceRow", "iter_chat_events"]

_ENVELOPE = ResearchEvent.envelope_json
_DELETED = "DELETED"
_ACP_SOURCE = "acp"
_RELAY_SOURCE = "relay"
_BATCH_SIZE = 500
#: The tool call an event belongs to (correlations first, as the proxy writes it).
_TOOL_CALL_ID = func.coalesce(
    _ENVELOPE["correlations"]["tool_call_id"].astext, _ENVELOPE["payload"]["tool_call_id"].astext
)

#: Keyset order: time, then the emitter's own sequence, then the event id.
_ORDER = (
    ResearchEvent.occurred_at,
    ResearchEvent.emitter_id,
    ResearchEvent.emitter_sequence,
    ResearchEvent.event_id,
)


@dataclass(frozen=True)
class TraceRow:
    """One retained ACP event of a chat, with its stored envelope parts."""

    event_id: str
    occurred_at: datetime
    emitter_id: str
    emitter_sequence: int
    research_session_id: Optional[str]
    event_type: str
    source: str
    lifecycle_state: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    correlations: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[datetime, str, int, str]:
        return (self.occurred_at, self.emitter_id, self.emitter_sequence, self.event_id)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def iter_chat_events(
    session: Session,
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    chat_id: str,
    *,
    start_at: Optional[tuple[datetime, str, int, str]] = None,
) -> Iterator[TraceRow]:
    """The chat's events in keyset order, from ``start_at`` (inclusive) on.

    Scoped to the study and enrollment; retention tombstones never appear. A
    chat is the ACP session id the participant proxy stamps on its events. The
    built-in agent's own report of a "Revise…" form (a decision carrying
    ``elicitation_action``) has no chat id, so it joins through its tool call.
    """
    scope = (
        ResearchEvent.study_id == study_id,
        ResearchEvent.enrollment_id == enrollment_id,
        ResearchEvent.retention_state != _DELETED,
    )
    in_chat = and_(
        ResearchEvent.source == _ACP_SOURCE,
        _ENVELOPE["payload"]["session_id"].astext == chat_id,
    )
    chat_tool_calls = select(_TOOL_CALL_ID).where(*scope, in_chat, _TOOL_CALL_ID.is_not(None))
    revision_reports = and_(
        ResearchEvent.source == _RELAY_SOURCE,
        ResearchEvent.event_type == "permission.decided",
        _ENVELOPE["payload"].has_key("elicitation_action"),
        _TOOL_CALL_ID.in_(chat_tool_calls),
    )
    statement = select(
        cast(ResearchEvent.event_id, String),
        ResearchEvent.occurred_at,
        ResearchEvent.emitter_id,
        ResearchEvent.emitter_sequence,
        cast(ResearchEvent.research_session_id, String),
        ResearchEvent.event_type,
        ResearchEvent.source,
        _ENVELOPE["lifecycle_state"].astext,
        _ENVELOPE["payload"],
        _ENVELOPE["correlations"],
        _ENVELOPE["metrics"],
    ).where(*scope, or_(in_chat, revision_reports))
    if start_at is not None:
        occurred_at, emitter_id, emitter_sequence, event_id = start_at
        statement = statement.where(
            tuple_(*_ORDER)
            >= tuple_(
                literal(occurred_at),
                literal(emitter_id),
                literal(int(emitter_sequence)),
                literal(uuid.UUID(str(event_id))),
            )
        )
    statement = statement.order_by(*(column.asc() for column in _ORDER))
    for row in session.execute(statement.execution_options(yield_per=_BATCH_SIZE)):
        (
            event_id,
            occurred_at,
            emitter_id,
            emitter_sequence,
            research_session_id,
            event_type,
            source,
            lifecycle_state,
            payload,
            correlations,
            metrics,
        ) = tuple(row)
        yield TraceRow(
            event_id=event_id,
            occurred_at=occurred_at,
            emitter_id=emitter_id or "",
            emitter_sequence=int(emitter_sequence or 0),
            research_session_id=research_session_id,
            event_type=event_type,
            source=source,
            lifecycle_state=lifecycle_state,
            payload=_as_dict(payload),
            correlations=_as_dict(correlations),
            metrics=_as_dict(metrics),
        )
