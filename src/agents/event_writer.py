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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from agents.normalize import snippet
from agents.telemetry import InferenceRecord
from database import crud
from research.telemetry.adapters import LegacyFact, record_legacy_facts
from research.telemetry.enums import CoverageState
from research.telemetry.models import Correlations, Coverage, EventMetrics

if TYPE_CHECKING:
    from App import App

# Source tag written to agent_event.source for rows produced by this path.
SOURCE_PROXY = "proxy"

#: Coverage-capability marker for relay-observed provider usage.
_USAGE_CAPABILITY = "usage"


def _optional_int(value: object) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _model_call_fact(
    record: InferenceRecord,
    latency_ms: int,
    span: Optional[dict],
    extra: Optional[dict],
) -> LegacyFact:
    """Build the canonical fact for one relay-observed model call.

    Structural metadata only: provider-token usage keeps its coverage semantics
    (missing usage stays UNAVAILABLE/null, never zero) and provider-reported
    values are distinguishable from byte estimates by their field names.
    """
    span = span or {}
    # Payload keys must be ones the privacy classifier recognises as metadata:
    # an unrecognised key is treated as content (fail closed), and ingestion
    # then refuses the whole event under every study that does not capture
    # content (the default). Counters therefore go into ``metrics.counts``.
    counts: dict[str, int] = {}
    for key, value in (
        ("prompt_tokens", record.prompt_tokens),
        ("completion_tokens", record.completion_tokens),
        ("message_count", record.message_count),
        ("tools_kept", record.tools_kept),
        ("tools_stripped", record.tools_stripped),
        ("step_index", span.get("step_index")),
    ):
        number = _optional_int(value)
        if number is not None:
            counts[key] = number
    total = _optional_int(record.total_tokens)
    # Mirror the total into counts so context accounting can sum one block
    # without joining usage_tokens; absent when the producer didn't report it.
    if total is not None:
        counts["total_tokens"] = total
    payload: dict[str, Any] = {
        "model": record.model,
        "finish_reason": record.finish_reason,
        "upstream_status": record.upstream_status,
        "request_id": record.request_id,
        "agent_profile": record.agent_profile,
        "call_mode": (
            None
            if record.streaming is None
            else ("streaming" if record.streaming else "single")
        ),
    }
    for key in ("tool_schema_bytes", "context_window_size_bytes"):
        number = _optional_int(span.get(key))
        if number is None and isinstance(extra, dict):
            # The relay computes ``tool_schema_bytes`` into the ``extra`` bag.
            number = _optional_int(extra.get(key))
        if number is not None:
            payload[key] = number
    # The rest of the free-form ``extra`` bag is deliberately NOT copied into
    # the canonical payload: most of its keys (``wire_api``,
    # ``openai_passthrough``, ...) do not classify as metadata, so a single one
    # would get the whole event refused, and ``upstream_base_url`` is admin-only
    # server configuration that must never reach researcher-visible events. The
    # legacy row keeps it in ``extra_json``. Two values are carried under keys
    # the classifier recognises as metadata.
    if isinstance(extra, dict):
        if extra.get("requested_model"):
            payload["requested_model"] = extra["requested_model"]
        if extra.get("wire_api"):
            payload["api_kind"] = extra["wire_api"]
    return LegacyFact(
        kind="model_call",
        occurred_at=datetime.now(timezone.utc),
        payload={k: v for k, v in payload.items() if v is not None},
        metrics=EventMetrics(
            usage_tokens=total,
            usage_capability=Coverage(
                state=(
                    CoverageState.AVAILABLE
                    if total is not None
                    else CoverageState.UNAVAILABLE
                ),
                capability=_USAGE_CAPABILITY,
            ),
            latency_ms=latency_ms,
            counts=counts,
        ),
        coverage=Coverage(state=CoverageState.AVAILABLE, capability="latency"),
        correlations=Correlations(correlation_id=record.request_id),
        source_event_id=span.get("span_id") or record.request_id,
        emitter_id="relay",
    )


def _tool_call_fact(
    tool_execution: dict,
    latency_ms: Optional[int],
    parent_span_id: Optional[str],
) -> LegacyFact:
    """Build the canonical fact for one relay-observed tool execution."""
    name = tool_execution.get("name")
    succeeded = tool_execution.get("success", True)
    arguments = tool_execution.get("arguments")
    result = tool_execution.get("result")
    payload: dict[str, Any] = {}
    counts: dict[str, int] = {}
    if name:
        payload["tool_name"] = name
    if isinstance(arguments, str):
        # A counter, not payload: "arguments" marks a payload key as content.
        counts["tool_arguments_length"] = len(arguments)
    if isinstance(result, str):
        # Payload for the dashboard read model, and a counter that survives a
        # policy excluding agent activity (like the arguments length).
        payload["tool_result_length"] = len(result)
        counts["tool_result_length"] = len(result)
    tool_call_id = tool_execution.get("id") or tool_execution.get("tool_call_id")
    return LegacyFact(
        kind="tool_call" if succeeded is not False else "tool_failed",
        occurred_at=datetime.now(timezone.utc),
        payload=payload,
        metrics=EventMetrics(latency_ms=latency_ms, counts=counts),
        coverage=Coverage(state=CoverageState.AVAILABLE, capability="tool_lifecycle"),
        correlations=Correlations(tool_call_id=str(tool_call_id) if tool_call_id else None),
        source_event_id=str(tool_call_id) if tool_call_id else None,
        emitter_id="relay",
    )



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
        task = crud.get_agent_task(db, task_uuid)
        if task is not None:
            result = record_legacy_facts(
                db, task=task, facts=[_model_call_fact(record, latency_ms, span, extra)]
            )
            if result is not None:
                # Research-bound: the canonical writer is the only authority.
                # A failed canonical write is never re-recorded in the legacy
                # table (ISSUE-07); it is logged and left to the producer.
                if result.written:
                    logging.info(
                        f"[Agent/events] model_call canonicalized — task={task_uuid} "
                        f"latency_ms={latency_ms} tokens={record.total_tokens}"
                    )
                else:
                    logging.warning(
                        f"[Agent/events] model_call canonical write refused — "
                        f"task={task_uuid} reason={result.reason} retryable={result.retryable}"
                    )
                return
        event_index = crud.reserve_agent_event_indexes(db, task_uuid, 1)
        source_event_id = span.get("span_id") or record.request_id
        crud.append_agent_event(
            db,
            task_id=task_uuid,
            event_index=event_index,
            event_type="model_call",
            source=SOURCE_PROXY,
            source_event_id=source_event_id,
            ignore_duplicate_source=source_event_id is not None,
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
        task = crud.get_agent_task(db, task_uuid)
        if task is not None:
            result = record_legacy_facts(
                db,
                task=task,
                facts=[
                    _tool_call_fact(tc, latency_ms, parent_span_id)
                    for tc in tool_executions
                ],
            )
            if result is not None:
                # Research-bound: never fall back to the legacy table on a
                # failed canonical write (ISSUE-07).
                if result.written:
                    logging.info(
                        f"[Agent/events] {len(tool_executions)} tool_call event(s) "
                        f"canonicalized for task={task_uuid}"
                    )
                else:
                    logging.warning(
                        f"[Agent/events] tool_call canonical write refused — "
                        f"task={task_uuid} reason={result.reason}"
                    )
                return
        event_index = crud.reserve_agent_event_indexes(db, task_uuid, len(tool_executions))
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

            source_event_id = tc.get("id") or tc.get("tool_call_id")
            crud.append_agent_event(
                db,
                task_id=task_uuid,
                event_index=event_index + offset,
                event_type="tool_call",
                source=SOURCE_PROXY,
                source_event_id=str(source_event_id) if source_event_id else None,
                ignore_duplicate_source=source_event_id is not None,
                latency_ms=latency_ms,
                span_id=str(source_event_id) if source_event_id else str(uuid.uuid4()),
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
