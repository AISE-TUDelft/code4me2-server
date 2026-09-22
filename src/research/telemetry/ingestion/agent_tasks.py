"""Ensure acknowledged ACP-proxied runs have one linkable ``agent_task``.

Dashboards join ``research_event.agent_run_id`` to ``agent_task.external_run_id``
and scope ownership by ``owner_user_id``. A research-telemetry event carries a
proxy-stamped run id but no task, so the run would be invisible; this module
ensures one task per acknowledged non-null run id, idempotently. Attribution and
ownership come from the stored rows, never the client payload, and the run's
unresolvable profile is stored as non-secret placeholders.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import crud
from database.research_schemas import ResearchEvent
from research.participants import identity as identity_store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .models import TelemetryBatchAckV1, TelemetryBatchRequestV1

__all__ = ["ensure_agent_tasks_for_ack"]

logger = logging.getLogger(__name__)

ACP_TASK_SOURCE = "research-acp"

PLACEHOLDER_PROFILE = {
    "agent_profile": "acp-external",
    "model": "",
    "approval_policy": "unknown",
    "tools_json": "[]",
    "framework_version": "acp-external",
}


def _acknowledged_run_events(
    payload: TelemetryBatchRequestV1, ack: TelemetryBatchAckV1
) -> dict[str, list]:
    """Group the accepted/duplicate payload events by non-null run id."""
    by_id: dict = {}
    for event in payload.events:
        by_id.setdefault(event.event_id, event)
    grouped: dict[str, list] = {}
    seen: set = set()
    for entry in (*ack.accepted, *ack.duplicate):
        event = by_id.get(entry.event_id)
        if entry.event_id in seen or event is None or not event.agent_run_id:
            continue
        seen.add(entry.event_id)
        grouped.setdefault(event.agent_run_id, []).append(event)
    return grouped


def ensure_agent_tasks_for_ack(
    db: Session,
    payload: TelemetryBatchRequestV1,
    ack: TelemetryBatchAckV1,
) -> list:
    """Ensure one linkable ``agent_task`` exists per acknowledged run id."""
    grouped = _acknowledged_run_events(payload, ack)
    if not grouped:
        return []
    rows = db.execute(
        select(
            ResearchEvent.event_id,
            ResearchEvent.enrollment_id,
            ResearchEvent.research_session_id,
        ).where(
            ResearchEvent.event_id.in_(
                [event.event_id for events in grouped.values() for event in events]
            )
        )
    ).all()
    stored = {row.event_id: row for row in rows}
    created: list = []
    for run_id, events in grouped.items():
        row = next((stored[e.event_id] for e in events if e.event_id in stored), None)
        if row is None or row.enrollment_id is None:
            continue
        enrollment = identity_store.get_enrollment(db, row.enrollment_id)
        if enrollment is None:
            continue
        participant = identity_store.get_participant(db, enrollment.participant_id)
        if participant is None or participant.account_id is None:
            continue
        if crud.get_agent_task_by_external_run_id(db, run_id) is not None:
            continue
        try:
            task = crud.create_agent_task(
                db,
                **PLACEHOLDER_PROFILE,
                source=ACP_TASK_SOURCE,
                status="running",
                owner_user_id=participant.account_id,
                external_run_id=run_id,
                study_id=enrollment.study_id,
                research_session_id=row.research_session_id,
                enrollment_id=enrollment.enrollment_id,
            )
        except IntegrityError:
            # A concurrent batch won ``external_run_id`` (UNIQUE); roll back and
            # re-read the winner instead of failing the surrounding batch.
            db.rollback()
            crud.get_agent_task_by_external_run_id(db, run_id)
            continue
        created.append(task)
        logger.info(
            "linked ACP run %s to agent_task %s", run_id, getattr(task, "task_id", "?")
        )
    return created
