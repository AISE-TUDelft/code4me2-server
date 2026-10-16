"""Stream a study's retained events for an export (no account identity)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import String, cast, or_, select

from database.research_schemas import ResearchEvent

if TYPE_CHECKING:
    import uuid
    from collections.abc import Collection, Iterator

    from sqlalchemy.orm import Session

    from research.analysis.study_analytics.models import DateWindow

__all__ = ["EVENT_CATEGORIES", "iter_export_events"]

_DELETED = "DELETED"
_BATCH_SIZE = 2_000

#: Export categories by canonical event type (prefix match; ``other`` is the rest).
EVENT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "conversation": ("agent.message.",),
    "tools": ("tool.",),
    "approvals": ("permission.",),
    "chat_lifecycle": ("interaction.",),
    "plans_usage": ("plan.", "usage."),
    "ide": ("ide.",),
    "errors": ("agent.error", "system."),
}


def _category_clause(categories: Collection[str]):
    clauses = []
    known_prefixes = [prefix for prefixes in EVENT_CATEGORIES.values() for prefix in prefixes]
    for category in categories:
        if category == "other":
            clauses.append(~or_(*(ResearchEvent.event_type.startswith(prefix) for prefix in known_prefixes)))
            continue
        clauses.extend(ResearchEvent.event_type.startswith(prefix) for prefix in EVENT_CATEGORIES[category])
    return or_(*clauses)


def iter_export_events(
    session: Session,
    study_id: uuid.UUID,
    *,
    enrollment_ids: Optional[Collection[uuid.UUID]] = None,
    window: Optional[DateWindow] = None,
    categories: Optional[Collection[str]] = None,
) -> Iterator[tuple[Any, ...]]:
    """``(event_id, occurred_at, enrollment_id, research_session_id, event_type,
    source, emitter_id, emitter_sequence, envelope)`` in deterministic order."""
    statement = select(
        cast(ResearchEvent.event_id, String),
        ResearchEvent.occurred_at,
        cast(ResearchEvent.enrollment_id, String),
        cast(ResearchEvent.research_session_id, String),
        ResearchEvent.event_type,
        ResearchEvent.source,
        ResearchEvent.emitter_id,
        ResearchEvent.emitter_sequence,
        ResearchEvent.envelope_json,
    ).where(
        ResearchEvent.study_id == study_id,
        ResearchEvent.retention_state != _DELETED,
        ResearchEvent.enrollment_id.isnot(None),
    )
    if enrollment_ids is not None:
        statement = statement.where(ResearchEvent.enrollment_id.in_(list(enrollment_ids)))
    if window is not None:
        lower, upper = window.bounds()
        if lower is not None:
            statement = statement.where(ResearchEvent.occurred_at >= lower)
        if upper is not None:
            statement = statement.where(ResearchEvent.occurred_at < upper)
    if categories:
        statement = statement.where(_category_clause(categories))
    statement = statement.order_by(
        ResearchEvent.occurred_at.asc(),
        ResearchEvent.emitter_id.asc(),
        ResearchEvent.emitter_sequence.asc(),
        ResearchEvent.event_id.asc(),
    )
    for row in session.execute(statement.execution_options(yield_per=_BATCH_SIZE)):
        yield tuple(row)
