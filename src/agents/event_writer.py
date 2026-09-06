"""Persistence of agent_event rows from the proxy telemetry path.

Both telemetry paths converge on the same table (merge decision 2), so this
module and ``agents.ingest`` are two front doors onto one
``crud.append_agent_event``. The difference is only where the data comes from:
here it's observed by the relay; there it's self-reported by the runtime.

Every write is best-effort: a telemetry failure must never break the agent the
developer is actually using, so DB errors are logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from agents.normalize import snippet
from agents.telemetry import InferenceRecord
from database import crud

if TYPE_CHECKING:
    from App import App

# Source tag written to agent_event.source for rows produced by this path.
SOURCE_PROXY = "proxy"


def write_model_call_event(
    app: App,
    task_uuid: Optional[uuid.UUID],
    record: InferenceRecord,
    latency_ms: int,
    span: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> None:
    """Persist one model_call event row.

    ``record`` carries the per-call fields; ``span`` carries the span/context
    fields (span_id, parent_span_id, step_index, session detection state).
    ``extra`` is an optional dict of non-content structural metrics with no
    typed column, serialised into ``extra_json``.

    session_id / task_id are deliberately not stored on the event — they live on
    ``agent_task``, and duplicating them here would let the two drift.
    """
    if task_uuid is None:
        return
    span = span or {}
    roles = record.role_breakdown or {}
    parent_span_id = span.get("parent_span_id")
    db = app.get_db_session()
    try:
        event_index = crud.count_agent_events_for_task(db, task_uuid)
        crud.append_agent_event(
            db,
            task_id=task_uuid,
            event_index=event_index,
            event_type="model_call",
            source=SOURCE_PROXY,
            latency_ms=latency_ms,
            # span identifiers
            span_id=span.get("span_id") or record.request_id,
            parent_span_id=_as_uuid(parent_span_id),
            request_id=record.request_id,
            chat_session_index=span.get("chat_session_index"),
            # model_call metadata
            model=record.model,
            agent_profile=record.agent_profile,
            streaming=record.streaming,
            message_count=record.message_count,
            role_system_count=roles.get("system"),
            role_user_count=roles.get("user"),
            role_assistant_count=roles.get("assistant"),
            role_tool_count=roles.get("tool"),
            tools_kept=record.tools_kept,
            tools_stripped=record.tools_stripped,
            tool_names_requested=record.tool_names_requested or None,
            max_tokens=record.max_tokens,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            finish_reason=record.finish_reason,
            upstream_status=record.upstream_status,
            step_index=span.get("step_index"),
            context_window_size_bytes=span.get("context_window_size_bytes"),
            active_file=record.active_file,
            first_message_hash=span.get("first_message_hash"),
            chat_new_session_detected=span.get("chat_new_session_detected"),
            experiment_tool_access_enabled=span.get("experiment_tool_access_enabled"),
            experiment_approval_policy=span.get("experiment_approval_policy"),
            # content — already nulled by the caller unless consent resolved True
            first_system_message=record.first_system_message,
            last_user_message=record.last_user_message,
            response_text=record.response_text,
            extra_json=json.dumps(extra, default=str) if extra else None,
        )
        logging.info(
            f"[Agent/events] model_call written — task={task_uuid} "
            f"event_index={event_index} latency_ms={latency_ms} "
            f"tokens={record.total_tokens}"
        )
    except Exception as e:
        logging.error(
            f"[Agent/events] failed to write model_call for task={task_uuid} — {e}",
            exc_info=True,
        )
    finally:
        db.close()


def write_tool_call_events(
    app: App,
    task_uuid: uuid.UUID,
    chat_session_index: int,
    tool_executions: list[dict],
    prev_call_at: Optional[datetime] = None,
    content_included: bool = False,
    parent_span_id: Optional[str] = None,
) -> None:
    """Persist one tool_call event per resolved tool execution.

    Third-party agents execute tools locally between inference calls, so there
    are no per-tool timestamps available — only the gap between the previous
    model_call and now. That gap is split evenly across the batch as an
    approximate ``latency_ms``; it is an estimate, not a measurement.

    ``parent_span_id`` is the span_id of the model_call whose response requested
    these tools, so each tool_call nests under it in the trace tree. Falls back
    to the task root when unknown (e.g. the first call of a session).

    Tool arguments and results can contain whole file contents (a "view" result,
    a patch), so they are only stored when ``content_included``; otherwise just
    their lengths are recorded, which still supports "how much context did the
    tool return" analysis without retaining the text.
    """
    if not tool_executions:
        return

    parent_span = _as_uuid(parent_span_id) or task_uuid
    latency_ms: Optional[int] = None
    if prev_call_at is not None:
        elapsed_ms = (datetime.now() - prev_call_at).total_seconds() * 1000
        latency_ms = max(0, int(elapsed_ms / len(tool_executions)))

    db = app.get_db_session()
    try:
        event_index = crud.count_agent_events_for_task(db, task_uuid)
        for offset, tc in enumerate(tool_executions):
            arguments = tc.get("arguments")
            result = tc.get("result")
            tool_arguments: Optional[str] = None
            tool_result: Optional[str] = None
            tool_arguments_length: Optional[int] = None
            tool_result_length: Optional[int] = None
            if content_included:
                tool_arguments = _as_text(arguments)
                tool_result = _as_text(result)
            # Lengths are structural, so record them either way — they're the
            # only signal left when content storage is off.
            if isinstance(arguments, str):
                tool_arguments_length = len(arguments)
            if isinstance(result, str):
                tool_result_length = len(result)

            crud.append_agent_event(
                db,
                task_id=task_uuid,
                event_index=event_index + offset,
                event_type="tool_call",
                source=SOURCE_PROXY,
                latency_ms=latency_ms,
                span_id=str(uuid.uuid4()),
                parent_span_id=parent_span,
                chat_session_index=chat_session_index,
                tool_name=tc.get("name"),
                tool_arguments=tool_arguments,
                tool_result=tool_result,
                tool_arguments_length=tool_arguments_length,
                tool_result_length=tool_result_length,
                commit=False,
            )
        db.commit()
        logging.info(
            f"[Agent/events] recorded {len(tool_executions)} tool_call event(s) "
            f"for task={task_uuid}"
        )
    except Exception as e:
        db.rollback()
        logging.error(
            f"[Agent/events] failed writing tool_call events for task={task_uuid} "
            f"— {e}",
            exc_info=True,
        )
    finally:
        db.close()


def _as_uuid(value: object) -> Optional[uuid.UUID]:
    """Best-effort UUID coercion for span ids arriving as strings.

    Returns None rather than raising: an unparseable span id should cost the
    parent link, not the whole telemetry row.
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_text(value: object) -> Optional[str]:
    """Render a tool argument/result as a truncated string for a content column."""
    if value is None:
        return None
    if isinstance(value, str):
        return snippet(value)
    try:
        return snippet(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return snippet(str(value))
