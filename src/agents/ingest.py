"""Ingestion of self-reported telemetry from the built-in code4me2-agent runtime.

This is the other half of merge decision 2 ("hybrid telemetry, per agent type").
Because we *own* the ``code4me2-agent`` ReAct loop, it can report its own steps
rather than being observed from outside — which yields strictly better data than
proxy interception (it sees iterations, denials and parse failures that never
become HTTP requests).

Both paths land in the same ``agent_event`` table, so downstream analysis never
has to care which runtime produced a row. This module is the translation layer:
it maps the runtime's event envelope onto the columnar schema, promoting known
fields to typed columns and routing the rest into the JSON overflow column.

Two envelope quirks it reconciles:

* The runtime reports a model call as *two* events (``agent.model.requested``
  then ``agent.model.completed``) and a tool call as two more
  (``agent.tool.called`` / ``agent.tool.completed``), because it emits as it
  goes. The proxy path writes one row per call. To keep the two paths
  comparable, the request-side fields are merged forward onto the completion
  row (matched by ``request_id`` / ``tool_call_id``), while the request events
  are still kept as their own low-value rows so a crashed run isn't silently
  lost.
* The envelope carries a client-side ``privacy`` block including
  ``raw_capture_enabled``. That is **descriptive only** and is never used to
  decide what gets stored — per merge decision 3 the consent check is
  server-side (``resolve_store_agent_content``). The claimed block is retained
  in ``extra_json`` purely so a mismatch between what the client thought and
  what the server did is auditable.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from database import crud
from research.telemetry.adapters import (
    CanonicalIngestionFailed,
    LegacyFact,
    record_legacy_facts,
)
from research.telemetry.enums import CoverageState
from research.telemetry.models import Correlations, Coverage, EventMetrics

# Source tag written to agent_event.source for rows produced by this path.
SOURCE_SELF_REPORT = "code4me2_agent"

# Runtime event_type → unified agent_event.event_type.
#
# `model_call` and `tool_call` are the two types that carry typed columns and
# feed task aggregation, so only the *completion* events map onto them; their
# request-side counterparts get distinct types to avoid double-counting tokens
# and steps in `lifecycle.finalize_agent_task`.
EVENT_TYPE_MAP: dict[str, str] = {
    "agent.run.started": "run_started",
    "agent.run.completed": "run_completed",
    "agent.model.requested": "model_request",
    "agent.model.completed": "model_call",
    "agent.tool.called": "tool_request",
    "agent.tool.completed": "tool_call",
    "agent.tool.denied": "tool_denied",
    "agent.tool.failed": "tool_failed",
    "agent.permission.requested": "permission_requested",
    "agent.permission.decided": "permission_decided",
    "agent.adapter.loop_failed": "error",
    "agent.adapter.parse_failed": "error",
    "agent.request.received": "observation",
    "agent.response.completed": "observation",
}

# Metrics keys promoted out of the free-form `metrics` block into typed columns.
# Anything not listed stays in extra_json.
_PROMOTED_METRIC_KEYS = frozenset(
    {"duration_ms", "prompt_tokens", "completion_tokens", "total_tokens"}
)


def map_event_type(runtime_event_type: str) -> str:
    """Translate a runtime event type, defaulting unknown ones to `observation`.

    Unknown types are kept rather than rejected: the runtime evolves faster than
    this map, and a new event name should degrade to a generic row with its
    payload intact, not 400 the whole batch.
    """
    mapped = EVENT_TYPE_MAP.get(runtime_event_type)
    if mapped is None:
        logging.info(
            f"[Agent/ingest] unmapped event_type {runtime_event_type!r} "
            f"— storing as 'observation'"
        )
        return "observation"
    return mapped


def _as_int(value: Any) -> Optional[int]:
    """Coerce a reported value to int, or None if it isn't numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    """Best-effort UUID coercion. The runtime mints ids as `uuid4().hex`, which
    parses fine; anything else degrades to None rather than failing the row."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse the runtime's ISO-8601 timestamp (it emits a trailing 'Z')."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_or_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def build_correlation_index(events: list[dict]) -> tuple[dict, dict]:
    """Pre-scan a batch to collect request-side fields for forward-merging.

    Returns ``(by_request_id, by_tool_call_id)``. Only fields that belong on the
    completion row are collected, so the merge can't overwrite anything the
    completion event reports itself.
    """
    by_request_id: dict[str, dict] = {}
    by_tool_call_id: dict[str, dict] = {}

    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue

        if event_type == "agent.model.requested":
            request_id = event.get("request_id")
            if request_id:
                by_request_id[str(request_id)] = {
                    "message_count": _as_int(payload.get("message_count")),
                    # The ReAct loop's iteration number is this call's step index.
                    "step_index": _as_int(payload.get("iteration")),
                    "approx_token_count": _as_int(payload.get("approx_token_count")),
                }
        elif event_type == "agent.tool.called":
            tool_call_id = payload.get("tool_call_id")
            if tool_call_id:
                by_tool_call_id[str(tool_call_id)] = {
                    "arguments": payload.get("arguments"),
                }

    return by_request_id, by_tool_call_id


def map_event_to_columns(
    event: dict,
    *,
    content_included: bool,
    by_request_id: dict,
    by_tool_call_id: dict,
) -> dict:
    """Translate one runtime event envelope into ``crud.append_agent_event`` kwargs.

    ``content_included`` is the server-resolved consent decision. When it is
    False every content-bearing field (payload, raw_payload, tool arguments and
    results, response text) is dropped here, before it can reach the database —
    the structural columns and the non-content ``extra_json`` are unaffected.
    """
    runtime_event_type = str(event.get("event_type") or "")
    event_type = map_event_type(runtime_event_type)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    request_id = event.get("request_id")

    # Forward-merge the request-side fields onto the completion row so a
    # self-reported model_call has the same populated columns as a proxied one.
    merged: dict = {}
    if event_type == "model_call" and request_id:
        merged = by_request_id.get(str(request_id), {})
    tool_arguments_value: Any = None
    if event_type in ("tool_call", "tool_denied", "tool_failed"):
        tool_call_id = payload.get("tool_call_id")
        if tool_call_id:
            tool_arguments_value = by_tool_call_id.get(str(tool_call_id), {}).get(
                "arguments"
            )

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

    # latency: metrics.duration_ms is authoritative; tool events report it in
    # their payload instead.
    latency_ms = _as_int(metrics.get("duration_ms"))
    if latency_ms is None:
        latency_ms = _as_int(payload.get("duration_ms"))

    tool_result_value = payload.get("result") if "result" in payload else None
    tool_arguments_text = _text_or_json(tool_arguments_value)
    tool_result_text = _text_or_json(tool_result_value)

    # Non-content structural leftovers. `observability` and `privacy` are the
    # runtime's own descriptive blocks; the un-promoted metrics keys are
    # adapter-specific counters that don't warrant a column each.
    extra: dict[str, Any] = {
        "runtime_event_type": runtime_event_type,
        "sequence": event.get("sequence"),
        "message_id": event.get("message_id"),
    }
    leftover_metrics = {
        k: v for k, v in metrics.items() if k not in _PROMOTED_METRIC_KEYS
    }
    if leftover_metrics:
        extra["metrics"] = leftover_metrics
    if isinstance(event.get("observability"), dict):
        extra["observability"] = event["observability"]
    if isinstance(event.get("privacy"), dict):
        # Retained for audit only — the storage decision was made server-side.
        extra["client_claimed_privacy"] = event["privacy"]
    if merged.get("approx_token_count") is not None:
        extra["approx_token_count"] = merged["approx_token_count"]
    for key in ("status", "backend_type", "failure_reason", "denial_reason", "path"):
        if payload.get(key) is not None:
            extra[key] = payload[key]

    # Content: the payload may quote user prompts, model output or file
    # contents, and raw_payload is the unredacted form of the same. Both are
    # dropped wholesale without consent.
    payload_json: Optional[str] = None
    if content_included:
        content_blob: dict[str, Any] = {"payload": payload}
        if event.get("raw_payload") is not None:
            content_blob["raw_payload"] = event["raw_payload"]
        payload_json = _text_or_json(content_blob)

    return {
        "event_type": event_type,
        "source": SOURCE_SELF_REPORT,
        "schema_version": event.get("schema_version"),
        "run_id": event.get("run_id"),
        "message_id": event.get("message_id"),
        "occurred_at": _parse_timestamp(event.get("timestamp")),
        "latency_ms": latency_ms,
        # The runtime's own event id becomes the span id, which is what makes
        # re-ingestion of a retried batch detectable.
        "span_id": str(event["event_id"]) if event.get("event_id") else None,
        "parent_span_id": _as_uuid(event.get("parent_event_id")),
        "request_id": str(request_id) if request_id else None,
        # tool_call identity for tool/permission events (structural ids).
        "tool_call_id": (
            str(payload.get("tool_call_id"))
            if payload.get("tool_call_id") is not None
            else None
        ),
        # permission decision metadata: outcome and scope are structural
        # (BEHAVIORAL-classified), never content, so they persist under
        # metadata-only policies.
        "decision": payload.get("decision"),
        "decision_scope": payload.get("decision_scope"),
        "tool_kind": payload.get("kind"),
        # model_call fields
        "model": payload.get("model"),
        "message_count": merged.get("message_count"),
        "step_index": merged.get("step_index"),
        "prompt_tokens": _as_int(
            metrics.get("prompt_tokens") or usage.get("prompt_tokens")
        ),
        "completion_tokens": _as_int(
            metrics.get("completion_tokens") or usage.get("completion_tokens")
        ),
        "total_tokens": _as_int(
            metrics.get("total_tokens") or usage.get("total_tokens")
        ),
        "finish_reason": payload.get("finish_reason"),
        # tool_call fields
        "tool_name": payload.get("tool_name"),
        "tool_arguments": tool_arguments_text if content_included else None,
        "tool_result": tool_result_text if content_included else None,
        # Lengths are structural — recorded regardless of consent, so "how much
        # context did this tool return" stays answerable either way.
        "tool_arguments_length": (
            len(tool_arguments_text) if tool_arguments_text is not None else None
        ),
        "tool_result_length": (
            len(tool_result_text) if tool_result_text is not None else None
        ),
        # JSON overflow
        "extra_json": json.dumps(extra, default=str),
        "payload_json": payload_json,
    }


def _fact_from_columns(columns: dict, *, content_included: bool = False) -> "LegacyFact":
    """Build the canonical fact for one self-reported event.

    Structural metadata is always included. When the study policy allowed
    content, the mapped content columns are carried in the canonical payload
    under content-classified keys, so the ingestion privacy gate is the final
    authority before persistence (ISSUE-01).
    """
    kind = columns.get("event_type") or "observation"
    counts: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "message_count", "step_index"):
        value = columns.get(key)
        if value is not None:
            counts[key] = int(value)
    for key in ("tool_arguments_length", "tool_result_length"):
        value = columns.get(key)
        if value is not None:
            counts[key] = int(value)
    total = columns.get("total_tokens")
    if total is not None:
        counts["total_tokens"] = int(total)
    payload: dict[str, Any] = {}
    for key in ("model", "finish_reason", "tool_name", "request_id"):
        value = columns.get(key)
        if value is not None:
            payload[key] = value
    # Permission decision metadata (structural, never content).
    for key in ("tool_kind", "decision", "decision_scope"):
        value = columns.get(key)
        if value is not None:
            payload[key] = value
    if content_included:
        if columns.get("tool_arguments") is not None:
            payload["tool_arguments"] = columns["tool_arguments"]
        if columns.get("tool_result") is not None:
            # ``result_text`` carries a content token so the privacy gate
            # classifies it as CONTENT (``tool_result`` alone would not).
            payload["result_text"] = columns["tool_result"]
        if columns.get("payload_json") is not None:
            payload["payload"] = columns["payload_json"]
    return LegacyFact(
        kind=kind,
        occurred_at=columns.get("occurred_at") or datetime.now(timezone.utc),
        payload=payload,
        metrics=EventMetrics(
            usage_tokens=int(total) if total is not None else None,
            usage_capability=Coverage(
                state=(
                    CoverageState.AVAILABLE
                    if total is not None
                    else CoverageState.UNAVAILABLE
                ),
                capability="usage",
                reason=(
                    None
                    if total is not None
                    else "usage not reported by producer"
                ),
            ),
            latency_ms=columns.get("latency_ms"),
            counts=counts,
        ),
        coverage=Coverage(state=CoverageState.AVAILABLE, capability="self_report"),
        correlations=Correlations(
            correlation_id=columns.get("request_id"),
            tool_call_id=columns.get("tool_call_id"),
            message_id=columns.get("message_id"),
            # The run scopes the trace; the runtime's own event id is the
            # span and its reported parent (if any) the parent link. The
            # request id links one model invocation with the tools reported
            # under it; tools without one keep a null link rather than an
            # invented one.
            trace_id=columns.get("run_id"),
            span_id=columns.get("span_id"),
            parent_span_id=(
                str(columns["parent_span_id"])
                if columns.get("parent_span_id") is not None
                else None
            ),
            model_call_id=columns.get("request_id"),
        ),
        source_event_id=columns.get("span_id"),
        emitter_id="self-report",
    )


def ingest_event_batch(
    db: Session,
    *,
    task_id: uuid.UUID,
    events: list[dict],
    content_included: bool,
    agent_profile: Optional[str] = None,
) -> tuple[int, list[str]]:
    """Persist a batch of self-reported events against ``task_id``.

    Returns ``(ingested_count, skipped_span_ids)``. Already-seen event ids are
    skipped rather than erroring, which makes the endpoint safe for the runtime
    to retry after a network failure — the uploader has no way to know whether a
    timed-out POST was applied.

    The whole batch commits once. Source identity makes retries idempotent;
    task-local indexes are allocated atomically, so concurrent batches cannot
    claim the same event position.
    """
    if not events:
        return 0, []

    # Sort by the runtime's own sequence so event_index reflects real ordering
    # even if the batch arrives out of order.
    ordered = sorted(events, key=lambda e: e.get("sequence") or 0)

    source_event_ids = [str(e["event_id"]) for e in ordered if e.get("event_id")]
    already_seen = set(
        crud.find_existing_agent_event_source_ids(
            db,
            task_id=task_id,
            source=SOURCE_SELF_REPORT,
            source_event_ids=source_event_ids,
        )
    )

    by_request_id, by_tool_call_id = build_correlation_index(ordered)
    skipped: list[str] = []
    pending: list[tuple[dict, str]] = []
    seen_in_batch: set[str] = set()
    for event in ordered:
        source_event_id = str(event["event_id"]) if event.get("event_id") else None
        if source_event_id is None:
            continue
        if source_event_id in already_seen or source_event_id in seen_in_batch:
            skipped.append(source_event_id)
            continue
        seen_in_batch.add(source_event_id)
        pending.append((event, source_event_id))

    first_index = (
        crud.reserve_agent_event_indexes(db, task_id, len(pending)) if pending else 0
    )
    # The canonical adapter is tried first: a research-bound task's events are
    # persisted through the one ingestion writer and never also dual-written to
    # the legacy table. A task with no research binding keeps the legacy path.
    if pending:
        task = crud.get_agent_task(db, task_id)
        facts = []
        for event, source_event_id in pending:
            columns = map_event_to_columns(
                event,
                content_included=content_included,
                by_request_id=by_request_id,
                by_tool_call_id=by_tool_call_id,
            )
            facts.append(
                _fact_from_columns(columns, content_included=content_included)
            )
        if task is not None:
            result = record_legacy_facts(db, task=task, facts=facts)
            if result is not None:
                # Research-bound task: the canonical writer is the only
                # authority. Never fall back to the legacy table (ISSUE-07).
                if not result.written:
                    raise CanonicalIngestionFailed(
                        reason=result.reason,
                        message=result.message,
                        retryable=result.retryable,
                        ack=result.ack,
                    )
                return len(facts), skipped
    ingested = 0
    for offset, (event, source_event_id) in enumerate(pending):
        columns = map_event_to_columns(
            event,
            content_included=content_included,
            by_request_id=by_request_id,
            by_tool_call_id=by_tool_call_id,
        )
        inserted = crud.append_agent_event(
            db,
            task_id=task_id,
            event_index=first_index + offset,
            agent_profile=agent_profile,
            source_event_id=source_event_id,
            ignore_duplicate_source=True,
            commit=False,
            **columns,
        )
        if inserted is None:
            skipped.append(source_event_id)
        else:
            ingested += 1

    db.commit()
    logging.info(
        f"[Agent/ingest] task={task_id} ingested={ingested} "
        f"skipped_duplicates={len(skipped)} content={content_included}"
    )
    return ingested, skipped
