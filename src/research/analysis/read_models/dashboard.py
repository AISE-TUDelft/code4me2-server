"""Canonical, owner-scoped agent analytics read path (phase 06 cutover).

One query path serves both the aggregate overview and the per-run detail, reading
the canonical ``research_event`` authority instead of the legacy ``agent_event``
store. Agent tasks join their canonical events by the explicit phase-05 binding
(``research_event.agent_run_id = agent_task.external_run_id``) — never by an ACP
session id or a time window.

Ownership is enforced before any join: a non-administrator only ever aggregates
tasks they own (``agent_task.owner_user_id``); an administrator may optionally
scope to one user. A legacy task with no canonical events renders explicit
unavailable/unknown values (``None`` for averages and counts that are genuinely
unknown), never fabricated zeros.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

_TIME_WINDOWS = {"7d": 7, "30d": 30, "90d": 90}

#: The canonical payload marker the relay/self-report adapters attach to each
#: observation, so a relay ``model_call`` is never confused with an ACP
#: ``agent.message.completed`` for the same run (source ownership).
_LEGACY_KIND = "e.envelope_json -> 'payload' ->> 'legacy_kind'"
_LATENCY = "(e.envelope_json -> 'metrics' ->> 'latency_ms')::float"
_USAGE = "(e.envelope_json -> 'metrics' ->> 'usage_tokens')::bigint"
_UPSTREAM = "e.envelope_json -> 'payload' ->> 'upstream_status'"
_PROMPT_TOKENS = "(e.envelope_json -> 'metrics' -> 'counts' ->> 'prompt_tokens')::bigint"
_COMPLETION_TOKENS = (
    "(e.envelope_json -> 'metrics' -> 'counts' ->> 'completion_tokens')::bigint"
)
_TOOL_NAME = "e.envelope_json -> 'payload' ->> 'tool_name'"
_TOOL_SCHEMA_BYTES = "(e.envelope_json -> 'payload' ->> 'tool_schema_bytes')::bigint"
_CONTEXT_BYTES = (
    "(e.envelope_json -> 'payload' ->> 'context_window_size_bytes')::bigint"
)
_TOOL_RESULT_LENGTH = (
    "(e.envelope_json -> 'payload' ->> 'tool_result_length')::bigint"
)
_DECISION = "e.envelope_json -> 'payload' ->> 'decision'"
#: A permission decision the built-in agent put to its user. As with the model
#: and tool counts, only the agent's own reports are read (for a proxied run the
#: ACP proxy observes the same round-trips), and a ``policy``-scope decision
#: (auto-approval, a suggestion-only refusal) asked no one.
_USER_DECISION = (
    f"({_LEGACY_KIND} = 'permission_decided' AND {_DECISION} IS NOT NULL "
    "AND COALESCE(e.envelope_json -> 'payload' ->> 'decision_scope', '') <> 'policy')"
)

_EVENT_JOIN = (
    "FROM research_event e "
    "JOIN agent_task t ON t.external_run_id = e.agent_run_id"
)


def _seconds_window(time_window: str) -> Optional[int]:
    days = _TIME_WINDOWS.get(time_window)
    return days


def _filters(
    current_user,
    *,
    user_id: Optional[str],
    framework: Optional[str],
    model: Optional[str],
    profile: Optional[str],
    start_time: datetime,
    end_time: datetime,
) -> tuple[str, dict[str, Any]]:
    """Owner scope first, then the optional filters (never before ownership)."""
    params: dict[str, Any] = {"start_time": start_time, "end_time": end_time}
    conditions = ["t.created_at BETWEEN :start_time AND :end_time"]
    is_admin = bool(getattr(current_user, "is_admin", False))
    if is_admin and user_id:
        params["user_id"] = user_id
        conditions.append("CAST(t.owner_user_id AS TEXT) = :user_id")
    elif not is_admin:
        params["user_id"] = str(current_user.user_id)
        conditions.append("t.owner_user_id = CAST(:user_id AS UUID)")
    if framework:
        params["framework"] = framework
        conditions.append("t.framework_version = :framework")
    if model:
        params["model"] = model
        conditions.append("t.model = :model")
    if profile:
        params["profile"] = profile
        conditions.append("t.agent_profile = :profile")
    return " AND ".join(conditions), params


def agent_overview(
    db: Any,
    current_user,
    *,
    time_window: str,
    framework: Optional[str] = None,
    model: Optional[str] = None,
    profile: Optional[str] = None,
    user_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Aggregate canonical agent events for the owner's tasks."""
    days = _seconds_window(time_window)
    if days is None:
        raise ValueError("time_window must be 7d, 30d, or 90d")
    end_time = now or datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    where, params = _filters(
        current_user,
        user_id=user_id,
        framework=framework,
        model=model,
        profile=profile,
        start_time=start_time,
        end_time=end_time,
    )

    task = db.execute(
        text(
            f"""
            SELECT
                COUNT(*) AS total_tasks,
                COUNT(*) FILTER (WHERE t.status = 'done') AS completed_tasks,
                COUNT(*) FILTER (WHERE t.status = 'failed') AS failed_tasks,
                COUNT(*) FILTER (WHERE t.status IN ('pending', 'running')) AS open_tasks,
                COALESCE(AVG(t.total_steps), 0) AS avg_steps,
                COALESCE(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) * 1000)
                    FILTER (WHERE t.started_at IS NOT NULL AND t.completed_at IS NOT NULL), 0)
                    AS avg_task_duration_ms
            FROM agent_task t
            WHERE {where}
            """
        ),
        params,
    ).one()

    events = db.execute(
        text(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'model_call') AS model_calls,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'tool_call') AS tool_calls,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'tool_failed') AS tool_failures,
                COUNT(*) FILTER (WHERE e.event_type IN ('agent.error', 'system.proxy.error',
                    'system.agent.crashed', 'unknown_source_event')
                    AND {_LEGACY_KIND} IS NULL) AS observed_failures,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'model_call'
                    AND ({_UPSTREAM})::int >= 200 AND ({_UPSTREAM})::int < 400)
                    AS successful_model_calls,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'model_call'
                    AND ({_UPSTREAM})::int = 429) AS rate_limit_retries,
                SUM({_USAGE}) FILTER (WHERE {_LEGACY_KIND} = 'model_call') AS event_tokens,
                SUM({_PROMPT_TOKENS}) FILTER (WHERE {_LEGACY_KIND} = 'model_call')
                    AS provider_input_tokens,
                SUM({_COMPLETION_TOKENS}) FILTER (WHERE {_LEGACY_KIND} = 'model_call')
                    AS model_output_tokens,
                SUM({_TOOL_SCHEMA_BYTES}) AS tool_schema_bytes,
                SUM({_CONTEXT_BYTES}) AS conversation_context_bytes,
                SUM({_TOOL_RESULT_LENGTH}) AS tool_result_bytes,
                COUNT(*) FILTER (WHERE {_USER_DECISION}) AS permission_decisions,
                COUNT(*) FILTER (WHERE {_USER_DECISION} AND {_DECISION} = 'accepted')
                    AS permission_accepted,
                AVG({_LATENCY}) FILTER (WHERE {_LEGACY_KIND} = 'model_call')
                    AS avg_model_latency_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {_LATENCY})
                    FILTER (WHERE {_LEGACY_KIND} = 'model_call' AND {_LATENCY} IS NOT NULL)
                    AS p95_model_latency_ms
            {_EVENT_JOIN}
            WHERE {where}
            """
        ),
        params,
    ).one()

    profile_rows = db.execute(
        text(
            f"""
            SELECT
                COALESCE(t.agent_profile, 'unknown') AS profile_name,
                COALESCE(t.framework_version, 'unknown') AS framework_version,
                COALESCE(t.model, 'unknown') AS model,
                COUNT(*) AS tasks,
                COUNT(*) FILTER (WHERE t.status = 'done') AS completed,
                COUNT(*) FILTER (WHERE t.status = 'failed') AS failed,
                COALESCE(AVG(t.total_steps), 0) AS avg_steps
            FROM agent_task t
            WHERE {where}
            GROUP BY 1, 2, 3
            ORDER BY tasks DESC, profile_name
            """
        ),
        params,
    ).fetchall()

    tool_rows = db.execute(
        text(
            f"""
            SELECT
                COALESCE({_TOOL_NAME}, 'unknown') AS tool_name,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'tool_call') AS calls,
                COUNT(*) FILTER (WHERE {_LEGACY_KIND} = 'tool_failed') AS failures,
                COALESCE(AVG({_LATENCY}), 0) AS avg_latency_ms
            {_EVENT_JOIN}
            WHERE {where} AND {_LEGACY_KIND} IN ('tool_call', 'tool_failed')
            GROUP BY 1
            ORDER BY calls DESC, tool_name
            LIMIT 20
            """
        ),
        params,
    ).fetchall()

    run_rows = db.execute(
        text(
            f"""
            SELECT
                t.task_id, t.agent_profile, t.framework_version, t.model, t.status,
                t.total_steps, t.created_at, t.started_at, t.completed_at,
                COUNT(e.event_id) FILTER (WHERE {_LEGACY_KIND} = 'model_call') AS model_calls,
                COUNT(e.event_id) FILTER (WHERE {_LEGACY_KIND} = 'tool_call') AS tool_calls,
                COUNT(e.event_id) FILTER (WHERE {_LEGACY_KIND} = 'tool_failed') AS failures
            FROM agent_task t
            LEFT JOIN research_event e ON e.agent_run_id = t.external_run_id
            WHERE {where}
            GROUP BY t.task_id
            ORDER BY t.created_at DESC
            LIMIT 25
            """
        ),
        params,
    ).fetchall()

    trend_rows = db.execute(
        text(
            f"""
            SELECT
                DATE_TRUNC('day', t.created_at) AS day,
                COUNT(*) AS runs,
                COUNT(*) FILTER (WHERE t.status = 'done') AS completed,
                COUNT(*) FILTER (WHERE t.status = 'failed') AS failed,
                COALESCE(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) * 1000)
                    FILTER (WHERE t.started_at IS NOT NULL AND t.completed_at IS NOT NULL), 0)
                    AS avg_duration_ms
            FROM agent_task t
            WHERE {where}
            GROUP BY 1
            ORDER BY 1
            """
        ),
        params,
    ).fetchall()

    latency_rows = db.execute(
        text(
            f"""
            SELECT
                CASE
                    WHEN {_LATENCY} < 250 THEN '<250ms'
                    WHEN {_LATENCY} < 500 THEN '250-499ms'
                    WHEN {_LATENCY} < 1000 THEN '500-999ms'
                    WHEN {_LATENCY} < 2000 THEN '1-2s'
                    ELSE '2s+'
                END AS bucket,
                CASE
                    WHEN {_LATENCY} < 250 THEN 1
                    WHEN {_LATENCY} < 500 THEN 2
                    WHEN {_LATENCY} < 1000 THEN 3
                    WHEN {_LATENCY} < 2000 THEN 4
                    ELSE 5
                END AS bucket_order,
                COUNT(*) AS calls
            {_EVENT_JOIN}
            WHERE {where} AND {_LEGACY_KIND} = 'model_call' AND {_LATENCY} IS NOT NULL
            GROUP BY 1, 2
            ORDER BY bucket_order
            """
        ),
        params,
    ).fetchall()

    event_type_rows = db.execute(
        text(
            f"""
            SELECT
                COALESCE({_LEGACY_KIND}, e.event_type) AS event_type,
                COUNT(*) AS events
            {_EVENT_JOIN}
            WHERE {where}
            GROUP BY 1
            ORDER BY events DESC, event_type
            """
        ),
        params,
    ).fetchall()

    error_rows = db.execute(
        text(
            f"""
            SELECT
                CASE
                    WHEN {_LEGACY_KIND} = 'tool_failed' THEN 'tool_failed'
                    WHEN ({_UPSTREAM})::int >= 400 THEN 'upstream_' || ({_UPSTREAM})
                    ELSE e.event_type
                END AS reason,
                COUNT(*) AS events
            {_EVENT_JOIN}
            WHERE {where}
              AND (
                {_LEGACY_KIND} = 'tool_failed'
                OR ({_UPSTREAM})::int >= 400
                OR e.event_type IN ('agent.error', 'system.proxy.error', 'system.agent.crashed')
              )
            GROUP BY 1
            ORDER BY events DESC, reason
            """
        ),
        params,
    ).fetchall()

    def num(value, integer: bool = False):
        if value is None:
            return None
        return int(value) if integer else float(value)

    total_tasks = num(task.total_tasks, integer=True) or 0
    tool_failures = num(events.tool_failures, integer=True) or 0
    observed_failures = num(events.observed_failures, integer=True) or 0
    return {
        "filters": {
            "time_window": time_window,
            "framework": framework,
            "model": model,
            "profile": profile,
        },
        "summary": {
            "total_tasks": total_tasks,
            "completed_tasks": num(task.completed_tasks, integer=True) or 0,
            "failed_tasks": num(task.failed_tasks, integer=True) or 0,
            "open_tasks": num(task.open_tasks, integer=True) or 0,
            "completion_rate": (num(task.completed_tasks, integer=True) or 0)
            / max(total_tasks, 1),
            "avg_steps": num(task.avg_steps),
            "avg_task_duration_ms": num(task.avg_task_duration_ms),
            "model_calls": num(events.model_calls, integer=True) or 0,
            "tool_calls": num(events.tool_calls, integer=True) or 0,
            "failures": tool_failures + observed_failures,
            # Canonical latencies/tokens are null when no canonical model-call
            # fact exists (a legacy run with no canonical events), never zero.
            "avg_model_latency_ms": num(events.avg_model_latency_ms),
            "p95_model_latency_ms": num(events.p95_model_latency_ms),
            "provider_input_tokens": num(events.provider_input_tokens, integer=True),
            "model_output_tokens": num(events.model_output_tokens, integer=True),
            "event_tokens": num(events.event_tokens, integer=True),
            "successful_model_calls": num(events.successful_model_calls, integer=True) or 0,
            "rate_limit_retries": num(events.rate_limit_retries, integer=True) or 0,
            "tool_schema_tokens_estimated": (
                (num(events.tool_schema_bytes, integer=True) or 0) // 4
                if events.tool_schema_bytes is not None
                else None
            ),
            "conversation_tokens_estimated": (
                (num(events.conversation_context_bytes, integer=True) or 0) // 4
                if events.conversation_context_bytes is not None
                else None
            ),
            "tool_result_tokens_estimated": (
                (num(events.tool_result_bytes, integer=True) or 0) // 4
                if events.tool_result_bytes is not None
                else None
            ),
            # Edit acceptance is computed from recorded permission decisions:
            # accepted tool executions over all decisions a user was asked for
            # (see ``_USER_DECISION``). With no such decision both stay null and
            # the dashboard renders unavailable rather than a fabricated ratio.
            "edit_acceptance_rate": (
                (num(events.permission_accepted, integer=True) or 0)
                / (num(events.permission_decisions, integer=True) or 0)
                if (num(events.permission_decisions, integer=True) or 0) > 0
                else None
            ),
            "total_edits": (
                num(events.permission_decisions, integer=True) or None
            ),
        },
        "profiles": [
            {
                "profile_name": row.profile_name,
                "framework_version": row.framework_version,
                "model": row.model,
                "tasks": num(row.tasks, integer=True),
                "completed": num(row.completed, integer=True),
                "failed": num(row.failed, integer=True),
                "completion_rate": (num(row.completed, integer=True) or 0)
                / max(num(row.tasks, integer=True) or 0, 1),
                "avg_steps": num(row.avg_steps),
            }
            for row in profile_rows
        ],
        "tools": [
            {
                "tool_name": row.tool_name,
                "calls": num(row.calls, integer=True),
                "failures": num(row.failures, integer=True),
                "avg_latency_ms": num(row.avg_latency_ms),
            }
            for row in tool_rows
        ],
        "recent_runs": [
            {
                "task_id": str(row.task_id),
                "profile_name": row.agent_profile,
                "framework_version": row.framework_version,
                "model": row.model,
                "status": row.status,
                "steps": num(row.total_steps, integer=True),
                "created_at": _iso(row.created_at),
                "started_at": _iso(row.started_at),
                "completed_at": _iso(row.completed_at),
                "model_calls": num(row.model_calls, integer=True),
                "tool_calls": num(row.tool_calls, integer=True),
                "failures": num(row.failures, integer=True),
            }
            for row in run_rows
        ],
        "trend": [
            {
                "day": _iso(row.day),
                "runs": num(row.runs, integer=True),
                "completed": num(row.completed, integer=True),
                "failed": num(row.failed, integer=True),
                "avg_duration_ms": num(row.avg_duration_ms),
            }
            for row in trend_rows
        ],
        "latency_distribution": [
            {"bucket": row.bucket, "calls": num(row.calls, integer=True)}
            for row in latency_rows
        ],
        "event_types": [
            {"event_type": row.event_type, "events": num(row.events, integer=True)}
            for row in event_type_rows
        ],
        "errors": [
            {"reason": row.reason, "events": num(row.events, integer=True)}
            for row in error_rows
        ],
    }


def agent_run_detail(
    db: Any,
    current_user,
    *,
    task_id: str,
) -> Optional[dict[str, Any]]:
    """Return one owner-scoped run and its canonical events, or ``None``."""
    params: dict[str, Any] = {"task_id": task_id}
    owner_filter = ""
    if not getattr(current_user, "is_admin", False):
        params["user_id"] = str(current_user.user_id)
        owner_filter = "AND CAST(t.owner_user_id AS TEXT) = :user_id"

    task = db.execute(
        text(
            f"""
            SELECT t.task_id, t.agent_profile, t.framework_version, t.model,
                   t.status, t.total_steps, t.created_at, t.started_at, t.completed_at,
                   t.external_run_id
            FROM agent_task t
            WHERE CAST(t.task_id AS TEXT) = :task_id {owner_filter}
            """
        ),
        params,
    ).one_or_none()
    if task is None:
        return None

    event_rows = db.execute(
        text(
            f"""
            SELECT
                COALESCE({_LEGACY_KIND}, e.event_type) AS event_type,
                e.envelope_json -> 'provenance' ->> 'source' AS source,
                {_LATENCY} AS latency_ms,
                {_UPSTREAM} AS upstream_status,
                {_TOOL_NAME} AS tool_name,
                e.envelope_json -> 'payload' ->> 'model' AS model,
                {_USAGE} AS total_tokens,
                e.envelope_json -> 'metrics' -> 'counts' ->> 'prompt_tokens' AS prompt_tokens,
                e.envelope_json -> 'metrics' -> 'counts' ->> 'completion_tokens' AS completion_tokens,
                e.envelope_json -> 'payload' ->> 'finish_reason' AS finish_reason,
                e.envelope_json -> 'metrics' -> 'counts' ->> 'step_index' AS step_index,
                e.envelope_json -> 'correlations' ->> 'trace_id' AS trace_id,
                e.envelope_json -> 'correlations' ->> 'span_id' AS span_id,
                e.envelope_json -> 'correlations' ->> 'parent_span_id' AS parent_span_id,
                e.envelope_json -> 'correlations' ->> 'model_call_id' AS model_call_id,
                e.envelope_json -> 'correlations' ->> 'message_id' AS message_id,
                e.envelope_json -> 'correlations' ->> 'request_id' AS request_id,
                e.occurred_at AS occurred_at,
                e.envelope_json -> 'correlations' ->> 'tool_call_id' AS tool_call_id,
                e.event_type AS canonical_event_type
            FROM research_event e
            WHERE e.agent_run_id = :run_id
            ORDER BY e.occurred_at, e.emitter_sequence
            """
        ),
        {"run_id": task.external_run_id},
    ).fetchall()

    events = []
    for position, row in enumerate(event_rows, start=1):
        events.append(
            {
                "event_index": position,
                "event_type": row.event_type,
                "source": row.source,
                "latency_ms": _num(row.latency_ms),
                "created_at": None,
                "occurred_at": _iso(row.occurred_at),
                "trace_id": row.trace_id,
                "span_id": row.span_id,
                "parent_span_id": row.parent_span_id,
                "model_call_id": row.model_call_id,
                "message_id": row.message_id,
                "request_id": row.request_id,
                "model": row.model,
                "tool_name": row.tool_name,
                "prompt_tokens": _num(row.prompt_tokens),
                "completion_tokens": _num(row.completion_tokens),
                "total_tokens": _num(row.total_tokens),
                "finish_reason": row.finish_reason,
                "upstream_status": _num(row.upstream_status),
                "step_index": _num(row.step_index),
                "detail": row.canonical_event_type
                if row.event_type != row.canonical_event_type
                else None,
            }
        )

    return {
        "run": {
            "task_id": str(task.task_id),
            "profile_name": task.agent_profile,
            "framework_version": task.framework_version,
            "model": task.model,
            "status": task.status,
            "steps": int(task.total_steps or 0),
            "created_at": _iso(task.created_at),
            "started_at": _iso(task.started_at),
            "completed_at": _iso(task.completed_at),
        },
        "events": events,
    }


def _iso(value):
    return value.isoformat() if value is not None else None


def _num(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
