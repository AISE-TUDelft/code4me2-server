"""Privacy-scoped aggregate analytics for agent invocations."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from App import App
from backend.Responses import JsonResponseWithStatus
from .auth_utils import AuthenticatedUser, get_current_user

router = APIRouter()
_TIME_WINDOWS = {"7d": 7, "30d": 30, "90d": 90}


def _as_number(value, integer: bool = False):
    if value is None:
        return 0 if integer else 0.0
    return int(value) if integer else float(value)


def _iso(value):
    return value.isoformat() if value is not None else None


@router.get("/overview")
def get_agent_overview(
    time_window: str = Query("7d", description="7d, 30d, or 90d"),
    framework: Optional[str] = Query(None),
    model: Optional[str] = Query(None),
    profile: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None, description="Admin-only user filter"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Aggregate task/event telemetry without returning content payloads."""
    days = _TIME_WINDOWS.get(time_window)
    if days is None:
        raise HTTPException(status_code=400, detail="time_window must be 7d, 30d, or 90d")

    db = app.get_db_session()
    try:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=days)
        params = {"start_time": start_time, "end_time": end_time}
        conditions = ["t.created_at BETWEEN :start_time AND :end_time"]

        if current_user.is_admin and user_id:
            params["user_id"] = user_id
            conditions.append("CAST(t.owner_user_id AS TEXT) = :user_id")
        elif not current_user.is_admin:
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

        where = " AND ".join(conditions)

        task_query = f"""
                SELECT
                    COUNT(*) AS total_tasks,
                    COUNT(*) FILTER (WHERE t.status = 'done') AS completed_tasks,
                    COUNT(*) FILTER (WHERE t.status = 'failed') AS failed_tasks,
                    COUNT(*) FILTER (WHERE t.status IN ('pending', 'running')) AS open_tasks,
                    COALESCE(AVG(t.total_steps), 0) AS avg_steps,
                    COALESCE(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) * 1000)
                        FILTER (WHERE t.started_at IS NOT NULL AND t.completed_at IS NOT NULL), 0)
                        AS avg_task_duration_ms,
                    COALESCE(SUM(t.input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(t.output_tokens), 0) AS output_tokens
                FROM agent_task t
                WHERE {where}
            """
        task = db.execute(text(task_query), params).one()

        events_query = f"""
                SELECT
                    COUNT(*) FILTER (WHERE e.event_type = 'model_request') AS model_requests,
                    COUNT(*) FILTER (WHERE e.event_type = 'model_call') AS model_calls,
                    COUNT(*) FILTER (WHERE e.event_type = 'tool_request') AS tool_requests,
                    COUNT(*) FILTER (WHERE e.event_type = 'tool_call') AS tool_calls,
                    COUNT(*) FILTER (WHERE e.event_type IN ('tool_failed', 'error')) AS failures,
                    COALESCE(AVG(e.latency_ms) FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS avg_model_latency_ms,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY e.latency_ms)
                        FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS p95_model_latency_ms,
                    COALESCE(SUM(e.prompt_tokens) FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS provider_input_tokens,
                    COALESCE(SUM(e.completion_tokens) FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS model_output_tokens,
                    COUNT(*) FILTER (WHERE e.event_type = 'model_call' AND e.upstream_status = 429)
                        AS rate_limit_retries,
                    COUNT(*) FILTER (WHERE e.event_type = 'model_call'
                        AND e.upstream_status >= 200 AND e.upstream_status < 400)
                        AS successful_model_calls,
                    COALESCE(SUM(
                        CASE WHEN e.event_type = 'model_call'
                        THEN COALESCE((NULLIF(e.extra_json, '')::jsonb ->> 'tool_schema_bytes')::bigint, 0)
                        ELSE 0 END
                    ), 0) AS tool_schema_bytes,
                    COALESCE(SUM(e.context_window_size_bytes)
                        FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS conversation_context_bytes,
                    COALESCE(SUM(e.tool_result_length)
                        FILTER (WHERE e.event_type = 'tool_call'), 0)
                        AS tool_result_bytes,
                    COALESCE(SUM(e.total_tokens) FILTER (WHERE e.event_type = 'model_call'), 0)
                        AS event_tokens
                FROM agent_event e
                JOIN agent_task t ON t.task_id = e.task_id
                WHERE {where}
            """
        events = db.execute(text(events_query), params).one()

        edits_query = f"""
                SELECT
                    COUNT(ed.edit_id) AS total_edits,
                    COUNT(ed.edit_id) FILTER (WHERE ed.was_accepted IS TRUE) AS accepted_edits,
                    COUNT(ed.edit_id) FILTER (WHERE ed.was_accepted IS FALSE) AS rejected_edits
                FROM agent_task t
                LEFT JOIN agent_edit ed ON ed.task_id = t.task_id
                WHERE {where}
            """
        edits = db.execute(text(edits_query), params).one()

        profiles_query = f"""
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
        profile_rows = db.execute(text(profiles_query), params).fetchall()

        tools_query = f"""
                SELECT
                    COALESCE(e.tool_name, 'unknown') AS tool_name,
                    COUNT(*) FILTER (WHERE e.event_type = 'tool_call') AS calls,
                    COUNT(*) FILTER (WHERE e.event_type = 'tool_failed') AS failures,
                    COALESCE(AVG(e.latency_ms), 0) AS avg_latency_ms
                FROM agent_event e
                JOIN agent_task t ON t.task_id = e.task_id
                WHERE {where} AND e.event_type IN ('tool_call', 'tool_failed')
                GROUP BY 1
                ORDER BY calls DESC, tool_name
                LIMIT 20
            """
        tool_rows = db.execute(text(tools_query), params).fetchall()

        runs_query = f"""
                SELECT
                    t.task_id,
                    t.agent_profile,
                    t.framework_version,
                    t.model,
                    t.status,
                    t.total_steps,
                    t.created_at,
                    t.started_at,
                    t.completed_at,
                    COUNT(e.event_id) FILTER (WHERE e.event_type = 'model_call') AS model_calls,
                    COUNT(e.event_id) FILTER (WHERE e.event_type = 'tool_call') AS tool_calls,
                    COUNT(e.event_id) FILTER (WHERE e.event_type IN ('tool_failed', 'error')) AS failures
                FROM agent_task t
                LEFT JOIN agent_event e ON e.task_id = t.task_id
                WHERE {where}
                GROUP BY t.task_id
                ORDER BY t.created_at DESC
                LIMIT 25
            """
        run_rows = db.execute(text(runs_query), params).fetchall()

        trend_query = f"""
            SELECT
                DATE_TRUNC('day', t.created_at) AS day,
                COUNT(*) AS runs,
                COUNT(*) FILTER (WHERE t.status = 'done') AS completed,
                COUNT(*) FILTER (WHERE t.status = 'failed') AS failed,
                COALESCE(
                    AVG(EXTRACT(EPOCH FROM (t.completed_at - t.started_at)) * 1000)
                    FILTER (WHERE t.started_at IS NOT NULL AND t.completed_at IS NOT NULL),
                    0
                ) AS avg_duration_ms
            FROM agent_task t
            WHERE {where}
            GROUP BY 1
            ORDER BY 1
        """
        trend_rows = db.execute(text(trend_query), params).fetchall()

        latency_query = f"""
                SELECT
                    CASE
                        WHEN e.latency_ms < 250 THEN '<250ms'
                        WHEN e.latency_ms < 500 THEN '250-499ms'
                        WHEN e.latency_ms < 1000 THEN '500-999ms'
                        WHEN e.latency_ms < 2000 THEN '1-2s'
                        ELSE '2s+'
                    END AS bucket,
                    CASE
                        WHEN e.latency_ms < 250 THEN 1
                        WHEN e.latency_ms < 500 THEN 2
                        WHEN e.latency_ms < 1000 THEN 3
                        WHEN e.latency_ms < 2000 THEN 4
                        ELSE 5
                    END AS bucket_order,
                    COUNT(*) AS calls
                FROM agent_event e
                JOIN agent_task t ON t.task_id = e.task_id
                WHERE {where} AND e.event_type = 'model_call' AND e.latency_ms IS NOT NULL
                GROUP BY 1, 2
                ORDER BY bucket_order
            """
        latency_rows = db.execute(text(latency_query), params).fetchall()

        event_types_query = f"""
                SELECT e.event_type, COUNT(*) AS events
                FROM agent_event e
                JOIN agent_task t ON t.task_id = e.task_id
                WHERE {where}
                GROUP BY e.event_type
                ORDER BY events DESC, e.event_type
            """
        event_type_rows = db.execute(text(event_types_query), params).fetchall()

        errors_query = f"""
                SELECT
                    CASE
                        WHEN e.upstream_status IS NOT NULL AND e.upstream_status >= 400
                            THEN 'upstream_' || e.upstream_status::text
                        ELSE e.event_type
                    END AS reason,
                    COUNT(*) AS events
                FROM agent_event e
                JOIN agent_task t ON t.task_id = e.task_id
                WHERE {where}
                  AND (e.event_type IN ('error', 'tool_failed', 'tool_denied')
                       OR e.upstream_status >= 400)
                GROUP BY 1
                ORDER BY events DESC, reason
            """
        error_rows = db.execute(text(errors_query), params).fetchall()

        total_tasks = _as_number(task.total_tasks, integer=True)
        decided_edits = _as_number(edits.accepted_edits, integer=True) + _as_number(
            edits.rejected_edits, integer=True
        )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "filters": {"time_window": time_window, "framework": framework, "model": model, "profile": profile},
                "summary": {
                    "total_tasks": total_tasks,
                    "completed_tasks": _as_number(task.completed_tasks, integer=True),
                    "failed_tasks": _as_number(task.failed_tasks, integer=True),
                    "open_tasks": _as_number(task.open_tasks, integer=True),
                    "completion_rate": _as_number(task.completed_tasks, integer=True) / max(total_tasks, 1),
                    "avg_steps": _as_number(task.avg_steps),
                    "avg_task_duration_ms": _as_number(task.avg_task_duration_ms),
                    "input_tokens": _as_number(task.input_tokens, integer=True),
                    "output_tokens": _as_number(task.output_tokens, integer=True),
                    "model_requests": _as_number(events.model_requests, integer=True),
                    "model_calls": _as_number(events.model_calls, integer=True),
                    "tool_requests": _as_number(events.tool_requests, integer=True),
                    "tool_calls": _as_number(events.tool_calls, integer=True),
                    "failures": _as_number(events.failures, integer=True),
                    "avg_model_latency_ms": _as_number(events.avg_model_latency_ms),
                    "p95_model_latency_ms": _as_number(events.p95_model_latency_ms),
                    "provider_input_tokens": _as_number(events.provider_input_tokens, integer=True),
                    "model_output_tokens": _as_number(events.model_output_tokens, integer=True),
                    "successful_model_calls": _as_number(events.successful_model_calls, integer=True),
                    "rate_limit_retries": _as_number(events.rate_limit_retries, integer=True),
                    # These are estimates based on UTF-8 bytes / 4, not provider billing tokens.
                    "tool_schema_tokens_estimated": _as_number(events.tool_schema_bytes, integer=True) // 4,
                    "conversation_tokens_estimated": _as_number(events.conversation_context_bytes, integer=True) // 4,
                    "tool_result_tokens_estimated": _as_number(events.tool_result_bytes, integer=True) // 4,
                    "event_tokens": _as_number(events.event_tokens, integer=True),
                    "total_edits": _as_number(edits.total_edits, integer=True),
                    "edit_acceptance_rate": _as_number(edits.accepted_edits, integer=True) / max(decided_edits, 1),
                },
                "profiles": [
                    {
                        "profile_name": row.profile_name,
                        "framework_version": row.framework_version,
                        "model": row.model,
                        "tasks": _as_number(row.tasks, integer=True),
                        "completed": _as_number(row.completed, integer=True),
                        "failed": _as_number(row.failed, integer=True),
                        "completion_rate": _as_number(row.completed, integer=True) / max(_as_number(row.tasks, integer=True), 1),
                        "avg_steps": _as_number(row.avg_steps),
                    }
                    for row in profile_rows
                ],
                "tools": [
                    {
                        "tool_name": row.tool_name,
                        "calls": _as_number(row.calls, integer=True),
                        "failures": _as_number(row.failures, integer=True),
                        "avg_latency_ms": _as_number(row.avg_latency_ms),
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
                        "steps": _as_number(row.total_steps, integer=True),
                        "created_at": _iso(row.created_at),
                        "started_at": _iso(row.started_at),
                        "completed_at": _iso(row.completed_at),
                        "model_calls": _as_number(row.model_calls, integer=True),
                        "tool_calls": _as_number(row.tool_calls, integer=True),
                        "failures": _as_number(row.failures, integer=True),
                    }
                    for row in run_rows
                ],
                "trend": [
                    {
                        "day": _iso(row.day),
                        "runs": _as_number(row.runs, integer=True),
                        "completed": _as_number(row.completed, integer=True),
                        "failed": _as_number(row.failed, integer=True),
                        "avg_duration_ms": _as_number(row.avg_duration_ms),
                    }
                    for row in trend_rows
                ],
                "latency_distribution": [
                    {"bucket": row.bucket, "calls": _as_number(row.calls, integer=True)}
                    for row in latency_rows
                ],
                "event_types": [
                    {"event_type": row.event_type, "events": _as_number(row.events, integer=True)}
                    for row in event_type_rows
                ],
                "errors": [
                    {"reason": row.reason, "events": _as_number(row.events, integer=True)}
                    for row in error_rows
                ],
            },
        )
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving agent analytics: {error}") from error
    finally:
        db.close()


@router.get("/runs/{task_id}")
def get_agent_run_detail(
    task_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return structural event details for one authorized run, never content."""
    db = app.get_db_session()
    try:
        params = {"task_id": task_id}
        owner_filter = ""
        if not current_user.is_admin:
            params["user_id"] = str(current_user.user_id)
            owner_filter = "AND CAST(t.owner_user_id AS TEXT) = :user_id"

        task_query = f"""
                SELECT t.task_id, t.agent_profile, t.framework_version, t.model,
                       t.status, t.total_steps, t.created_at, t.started_at, t.completed_at
                FROM agent_task t
                WHERE CAST(t.task_id AS TEXT) = :task_id {owner_filter}
            """
        task = db.execute(text(task_query), params).one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail="Agent run not found")

        events_query = """
                SELECT event_index, event_type, source, latency_ms, created_at,
                       occurred_at, span_id, parent_span_id, model, tool_name,
                       prompt_tokens, completion_tokens, total_tokens,
                       finish_reason, upstream_status, step_index
                FROM agent_event
                WHERE task_id = CAST(:task_id AS UUID)
                ORDER BY event_index
            """
        events = db.execute(text(events_query), {"task_id": task_id}).fetchall()

        return JsonResponseWithStatus(
            status_code=200,
            content={
                "run": {
                    "task_id": str(task.task_id),
                    "profile_name": task.agent_profile,
                    "framework_version": task.framework_version,
                    "model": task.model,
                    "status": task.status,
                    "steps": _as_number(task.total_steps, integer=True),
                    "created_at": _iso(task.created_at),
                    "started_at": _iso(task.started_at),
                    "completed_at": _iso(task.completed_at),
                },
                "events": [
                    {
                        "event_index": _as_number(row.event_index, integer=True),
                        "event_type": row.event_type,
                        "source": row.source,
                        "latency_ms": _as_number(row.latency_ms),
                        "created_at": _iso(row.created_at),
                        "occurred_at": _iso(row.occurred_at),
                        "span_id": row.span_id,
                        "parent_span_id": str(row.parent_span_id) if row.parent_span_id else None,
                        "model": row.model,
                        "tool_name": row.tool_name,
                        "prompt_tokens": _as_number(row.prompt_tokens, integer=True),
                        "completion_tokens": _as_number(row.completion_tokens, integer=True),
                        "total_tokens": _as_number(row.total_tokens, integer=True),
                        "finish_reason": row.finish_reason,
                        "upstream_status": row.upstream_status,
                        "step_index": row.step_index,
                    }
                    for row in events
                ],
            },
        )
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving agent run: {error}") from error
    finally:
        db.close()