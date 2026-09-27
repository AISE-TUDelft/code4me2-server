"""Finalizing an AgentTask: aggregating its events into task-level totals.

Called from three places:

* ``POST /api/agent/task/{task_id}/close`` — the plugin closes the previous
  session's task on startup, because it cannot reliably detect when an ACP
  agent process exits;
* session deactivation — closes any tasks left open when the user logs out;
* ``POST /api/agent/events/ingest`` — when a self-reporting runtime sends its
  ``run_completed`` event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import crud


def _canonical_totals(db: Session, task_id: uuid.UUID) -> Optional[tuple[int, int, int]]:
    """``(steps, prompt_tokens, completion_tokens)`` from ``research_event``, or None.

    Steps are the relay/self-report facts (rows stamped with ``legacy_kind``,
    the marker the canonical adapters attach); token totals come from the
    ``model_call`` facts' metrics. Advisory: any failure leaves the legacy totals.
    """
    task = crud.get_agent_task(db, task_id)
    run_id = getattr(task, "external_run_id", None) if task is not None else None
    if not run_id:
        return None
    begin_nested = getattr(db, "begin_nested", None)
    savepoint = begin_nested() if callable(begin_nested) else None
    try:
        row = db.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE e.envelope_json -> 'payload' ->> 'legacy_kind' IS NOT NULL
                    ) AS steps,
                    COALESCE(SUM(
                        (e.envelope_json -> 'metrics' -> 'counts' ->> 'prompt_tokens')::bigint
                    ) FILTER (
                        WHERE e.envelope_json -> 'payload' ->> 'legacy_kind' = 'model_call'
                    ), 0) AS prompt_tokens,
                    COALESCE(SUM(
                        (e.envelope_json -> 'metrics' -> 'counts' ->> 'completion_tokens')::bigint
                    ) FILTER (
                        WHERE e.envelope_json -> 'payload' ->> 'legacy_kind' = 'model_call'
                    ), 0) AS completion_tokens
                FROM research_event e
                WHERE e.agent_run_id = :run_id
                """
            ),
            {"run_id": str(run_id)},
        ).one()
        if savepoint is not None:
            savepoint.commit()
    except Exception as error:  # noqa: BLE001 - finalization must never fail on totals
        if savepoint is not None:
            savepoint.rollback()
        logging.warning(f"[Agent/lifecycle] canonical totals unavailable for {task_id}: {error}")
        return None
    if not row.steps:
        return None
    return int(row.steps), int(row.prompt_tokens or 0), int(row.completion_tokens or 0)


def finalize_agent_task(
    db: Session, task_id: uuid.UUID, status: str = "done"
) -> dict:
    """Aggregate a task's events into task-level totals and mark it terminal.

    Safe to call multiple times: totals are recomputed from the same events, so
    re-running is idempotent regardless of the task's current status. That
    matters because the plugin's close-on-startup call and session deactivation
    can both fire for the same task.
    """
    events = crud.get_agent_events_by_task(db, task_id)
    model_call_events = [e for e in events if e.event_type == "model_call"]

    total_prompt = 0
    total_completion = 0
    agent_profile: Optional[str] = None
    tool_names: Optional[list[str]] = None
    for event in model_call_events:
        total_prompt += event.prompt_tokens or 0
        total_completion += event.completion_tokens or 0
        if agent_profile is None and event.agent_profile:
            agent_profile = event.agent_profile
        if tool_names is None and event.tool_names_requested:
            tool_names = list(event.tool_names_requested)

    last_event = max(events, key=lambda e: e.event_index) if events else None
    total_steps = len(events)
    if not events:
        # A research-bound task's observations live in the canonical store
        # (ISSUE-07: never dual-written to agent_event); read the same relay
        # facts the dashboards read so the task row is not finalized empty.
        canonical = _canonical_totals(db, task_id)
        if canonical is not None:
            total_steps, total_prompt, total_completion = canonical
    crud.update_agent_task_status(
        db,
        task_id=task_id,
        status=status,
        completed_at=datetime.now(),
        total_steps=total_steps,
        input_tokens=total_prompt or None,
        output_tokens=total_completion or None,
        latest_event_id=last_event.event_id if last_event else None,
    )
    # Record the tools the agent actually had available, which for third-party
    # runtimes is only knowable after observing a call — the profile's
    # tools_json is our *request*, this is the observed reality.
    if tool_names is not None:
        crud.update_agent_task_tools(db, task_id=task_id, tools=tool_names)

    summary = {
        "status": status,
        "total_steps": total_steps,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "agent_profile": agent_profile,
        "tools": tool_names,
    }
    logging.info(f"[Agent/lifecycle] task {task_id} finalized — {summary}")
    return summary
