"""Agent task lifecycle and the inference relay, for the plugin-facing paths.

Endpoints (mounted under ``/api/agent``):

  POST /task                    mint a task, assigning the A/B arm server-side
  POST /task/{id}/close         finalize a task and aggregate its telemetry
  POST /task/{id}/telemetry     bulk-upload OTel spans for one finished session
  POST /inference               the authed relay third-party agents talk through

All four authenticate via the plugin's ``session_token`` cookie, and every task
operation re-checks that the task belongs to the calling session — so one
developer's plugin can't read or write another's telemetry.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents import inference, lifecycle, registry
from App import App
from backend.routers.agent.consent import resolve_store_agent_content
from database import crud

router = APIRouter(tags=["Agent"])


def require_session(
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
) -> uuid.UUID:
    """Validate the session_token cookie via Redis and return the session_id UUID.

    The cookie value *is* the session_id primary key in the DB session table.
    Raises 401 if the cookie is missing, expired, or malformed.
    """
    redis_manager = app.get_redis_manager()
    session_info = redis_manager.get("session_token", session_token)
    if session_info is None or not session_info.get("user_token"):
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    try:
        return uuid.UUID(session_token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed session token")


def _require_own_task(db, task_id: uuid.UUID, session_id: uuid.UUID):
    """Load a task, enforcing that it belongs to the calling session."""
    task = crud.get_agent_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Agent task not found")
    if task.session_id is None or task.session_id != session_id:
        logging.warning(
            f"[Agent] 403 — task {task_id} session mismatch "
            f"(task.session={str(task.session_id)[:8]}… vs "
            f"cookie={str(session_id)[:8]}…)"
        )
        raise HTTPException(
            status_code=403, detail="Task does not belong to this session"
        )
    return task


# ── Task creation ────────────────────────────────────────────────────────────


class TaskCreateRequest(BaseModel):
    task_id: Optional[uuid.UUID] = None
    task_description: Optional[str] = None
    # NOTE: the client does not choose its profile. The arm is assigned
    # server-side (see registry.resolve_assignment) so A/B buckets can't be
    # self-selected. Any `profile` field sent by an older client is ignored.


@router.post("/task")
def create_agent_task(
    body: TaskCreateRequest,
    app: App = Depends(App.get_instance),
    session_id: uuid.UUID = Depends(require_session),
) -> JSONResponse:
    """Mint an agent task with the assigned profile snapshotted onto it.

    Accepting a client-supplied ``task_id`` makes this idempotent: the plugin
    generates the id before launching the agent, and a retry after a flaky
    response returns 200 with the same task rather than creating a duplicate.
    A task id already claimed by a *different* session is a 409, not a silent
    takeover.
    """
    logging.info(
        f"[Agent/task] request — task_id={body.task_id} "
        f"session={str(session_id)[:8]}…"
    )

    db = app.get_db_session()
    try:
        if body.task_id is not None:
            existing = crud.get_agent_task(db, body.task_id)
            if existing is not None:
                if existing.session_id != session_id:
                    logging.warning(
                        f"[Agent/task] 409 — task {body.task_id} belongs to another "
                        f"session"
                    )
                    raise HTTPException(
                        status_code=409,
                        detail="Task ID already exists under a different session "
                        "— use a new task_id",
                    )
                logging.info(
                    f"[Agent/task] idempotent — task {body.task_id} already owned "
                    f"by this session"
                )
                return JSONResponse(
                    {"task_id": str(existing.task_id)}, status_code=200
                )

        session = crud.get_session_by_id(db, session_id)
        if session is None or session.user_id is None:
            raise HTTPException(
                status_code=401, detail="Session is not associated with a user"
            )

        assignment = registry.resolve_assignment_context(db, session.user_id)
        if assignment is None:
            raise HTTPException(
                status_code=503,
                detail="No active agent profiles are configured on the server",
            )
        profile = assignment.profile

        # task_description is the user's own words — content, so honour the
        # consent gate even at creation time.
        content_included = resolve_store_agent_content(db, session_id)

        task = crud.create_agent_task(
            db,
            agent_profile=profile.name,
            model=profile.model,
            approval_policy=profile.approval_policy,
            tools_json=profile.tools_json,
            temperature=profile.temperature,
            framework_version=profile.framework_version,
            task_description=body.task_description if content_included else None,
            session_id=session_id,
            owner_user_id=session.user_id,
            task_id=body.task_id,
            source="plugin",
            study_id=assignment.study_id,
            study_assignment_id=assignment.assignment_id,
            profile_id=profile.profile_id,
            study_arm_name=assignment.arm_name,
            study_arm_is_baseline=assignment.is_baseline,
            consent_content_storage=content_included,
        )
        logging.info(
            f"[Agent/task] created task_id={task.task_id} profile={profile.name!r} "
            f"runtime={profile.framework_version} model={profile.model} "
            f"user={str(session.user_id)[:8]}…"
        )
        return JSONResponse(
            {
                "task_id": str(task.task_id),
                # The plugin needs the runtime name to know which agent to launch.
                "framework_version": profile.framework_version,
                "agent_profile": profile.name,
            },
            status_code=201,
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"[Agent/task] DB error creating task — {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create task")
    finally:
        db.close()


# ── Inference relay ──────────────────────────────────────────────────────────


class InferenceRequest(BaseModel):
    task_id: uuid.UUID
    request: dict[str, Any]
    enrichment: Optional[dict[str, Any]] = None


@router.post("/inference")
async def run_agent_inference(
    body: InferenceRequest,
    app: App = Depends(App.get_instance),
    session_id: uuid.UUID = Depends(require_session),
) -> Response:
    """Authed relay for third-party agents (Goose, Codex).

    The agent talks to the plugin's local proxy, which forwards here with the
    session cookie attached. Upstream target, model, temperature and tool policy
    all come from the profile snapshotted onto the task — not from what the
    agent asked for — so the assigned A/B arm actually takes effect.
    """
    logging.info(
        f"[Agent/inference] request — task_id={body.task_id} "
        f"session={str(session_id)[:8]}… "
        f"model={body.request.get('model', '?')} "
        f"stream={body.request.get('stream', False)} "
        f"messages={len(body.request.get('messages', []))}"
    )

    db = app.get_db_session()
    try:
        task = _require_own_task(db, body.task_id, session_id)
        # Resolved server-side from the stored preference, never from the
        # request body — see backend.routers.agent.consent.
        content_included = resolve_store_agent_content(db, session_id)
        profile = task.profile or crud.get_agent_profile(db, task.agent_profile)
        base_url = profile.base_url if profile else None
        api_key_ref = profile.api_key_ref if profile else None
        task_snapshot = {
            "agent_profile": task.agent_profile,
            "model": task.model,
            "temperature": task.temperature,
            "tools_json": task.tools_json,
            "framework_version": task.framework_version
            or (profile.framework_version if profile else None),
        }
    finally:
        db.close()

    logging.info(
        f"[Agent/inference] task validated — profile={task_snapshot['agent_profile']} "
        f"runtime={task_snapshot['framework_version']} "
        f"content={content_included}"
    )
    return await inference.run_inference(
        task_uuid=body.task_id,
        session_uuid=session_id,
        openai_body=body.request,
        enrichment=body.enrichment,
        agent_profile=task_snapshot["agent_profile"],
        model=task_snapshot["model"],
        temperature=task_snapshot["temperature"],
        base_url=base_url,
        api_key_ref=api_key_ref,
        framework_version=task_snapshot["framework_version"],
        profile_tools_json=task_snapshot["tools_json"],
        content_included=content_included,
        app=app,
    )


# ── Telemetry upload ─────────────────────────────────────────────────────────

# Maps OTel span names produced by the plugin's trace collectors to the
# event_type vocabulary already used in agent_event rows.
_SPAN_EVENT_TYPE: dict[str, str] = {
    "agent.llm.invoke": "model_call",
    "agent.tool.execute": "tool_call",
    "agent.retrieval": "observation",
}


def _safe_int(value: Any) -> Optional[int]:
    """Coerce an OTel attribute value to int, or None if it isn't numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SpanPayload(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    name: str
    kind: str = "INTERNAL"
    start_time: str
    end_time: str
    duration_ms: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: str = "OK"


class TelemetryUploadRequest(BaseModel):
    trace_id: str
    agent_runtime: str
    experiment_tags: dict[str, str] = Field(default_factory=dict)
    spans: list[SpanPayload]
    # NOTE: no `content_included` field. Whether content is stored is resolved
    # server-side from the user's preference; a client-supplied flag here would
    # be exactly the client-trusting design merge decision 3 rejects.


@router.post("/task/{task_id}/telemetry", status_code=204)
def upload_agent_telemetry(
    task_id: uuid.UUID,
    body: TelemetryUploadRequest,
    app: App = Depends(App.get_instance),
    session_id: uuid.UUID = Depends(require_session),
) -> Response:
    """Persist OTel spans for one completed agent session.

    Span → DB mapping::

        agent.task          root span  → updates AgentTask aggregates
        agent.llm.invoke    child span → AgentEvent(event_type='model_call')
        agent.tool.execute  child span → AgentEvent(event_type='tool_call')
        agent.retrieval     child span → AgentEvent(event_type='observation')

    Known span attributes (token counts, model, tool name) map onto columns;
    the rest of the attribute bag goes into ``extra_json``.

    This is a bulk path for runtimes whose calls the proxy didn't see
    individually. Where both fire for the same session, the proxy's per-call
    rows are the more detailed record; these spans mainly recover the
    task-level totals.
    """
    logging.info(
        f"[Agent/telemetry] upload — task={task_id} trace={body.trace_id} "
        f"runtime={body.agent_runtime} spans={len(body.spans)}"
    )

    db = app.get_db_session()
    try:
        _require_own_task(db, task_id, session_id)

        root = next((s for s in body.spans if s.parent_span_id is None), None)
        child_spans = [s for s in body.spans if s.parent_span_id is not None]

        if root is not None:
            llm_spans = [s for s in child_spans if s.name == "agent.llm.invoke"]
            total_prompt = sum(
                _safe_int(s.attributes.get("llm.prompt_tokens")) or 0
                for s in llm_spans
            )
            total_completion = sum(
                _safe_int(s.attributes.get("llm.completion_tokens")) or 0
                for s in llm_spans
            )
            crud.update_agent_task_status(
                db,
                task_id=task_id,
                status="done",
                total_steps=len(child_spans),
                input_tokens=total_prompt or None,
                output_tokens=total_completion or None,
            )
            logging.info(
                f"[Agent/telemetry] task {task_id} updated — "
                f"steps={len(child_spans)} tokens={total_prompt}/{total_completion}"
            )

        base_index = (
            crud.reserve_agent_event_indexes(db, task_id, len(child_spans))
            if child_spans
            else 0
        )
        for offset, span in enumerate(child_spans):
            event_type = _SPAN_EVENT_TYPE.get(span.name, "observation")
            attrs = span.attributes or {}
            # OTel span ids are hex strings; only persist a parent link when it
            # parses as a UUID, since that's the column type.
            parent_span_uuid: Optional[uuid.UUID] = None
            if span.parent_span_id:
                try:
                    parent_span_uuid = uuid.UUID(str(span.parent_span_id))
                except (ValueError, TypeError):
                    parent_span_uuid = None

            crud.append_agent_event(
                db,
                task_id=task_id,
                event_index=base_index + offset,
                event_type=event_type,
                source="proxy",
                source_event_id=span.span_id,
                ignore_duplicate_source=True,
                latency_ms=span.duration_ms if span.duration_ms > 0 else None,
                span_id=span.span_id,
                parent_span_id=parent_span_uuid,
                model=attrs.get("llm.model") or attrs.get("model"),
                prompt_tokens=_safe_int(attrs.get("llm.prompt_tokens")),
                completion_tokens=_safe_int(attrs.get("llm.completion_tokens")),
                total_tokens=_safe_int(attrs.get("llm.total_tokens")),
                tool_name=(
                    attrs.get("tool.name") if event_type == "tool_call" else None
                ),
                extra_json=_span_extra_json(span, body),
                commit=False,
            )
        db.commit()
        logging.info(
            f"[Agent/telemetry] stored {len(child_spans)} events for task {task_id}"
        )
        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.error(
            f"[Agent/telemetry] DB error for task {task_id} — {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Failed to store telemetry")
    finally:
        db.close()


def _span_extra_json(span: SpanPayload, body: TelemetryUploadRequest) -> str:
    """Serialise the non-columnar span metadata into the JSON overflow column.

    Group-21's version dropped these attributes on the floor. Keeping them in
    ``extra_json`` is the point of the overflow column: an unrecognised span
    attribute costs a nested JSON key rather than being lost.
    """
    import json as _json

    known = {
        "llm.model",
        "model",
        "llm.prompt_tokens",
        "llm.completion_tokens",
        "llm.total_tokens",
        "tool.name",
    }
    return _json.dumps(
        {
            "otel": {
                "trace_id": span.trace_id,
                "span_name": span.name,
                "kind": span.kind,
                "status": span.status,
                "start_time": span.start_time,
                "end_time": span.end_time,
                # Raw parent id, since the column can only hold UUID-shaped ones.
                "parent_span_id": span.parent_span_id,
            },
            "agent_runtime": body.agent_runtime,
            "experiment_tags": body.experiment_tags or None,
            "attributes": {
                k: v for k, v in (span.attributes or {}).items() if k not in known
            }
            or None,
        },
        default=str,
    )


# ── Task close ───────────────────────────────────────────────────────────────


@router.post("/task/{task_id}/close", status_code=204)
def close_agent_task(
    task_id: uuid.UUID,
    app: App = Depends(App.get_instance),
    session_id: uuid.UUID = Depends(require_session),
) -> Response:
    """Mark a task done and aggregate its telemetry into task-level totals.

    Called by the plugin on startup, closing the *previous* session's task,
    because the plugin can't reliably detect when an ACP agent process exits.
    Idempotent — safe to call repeatedly.
    """
    db = app.get_db_session()
    try:
        _require_own_task(db, task_id, session_id)
        summary = lifecycle.finalize_agent_task(db, task_id)
        logging.info(f"[Agent/close] task {task_id} closed — {summary}")
        return Response(status_code=204)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.error(f"[Agent/close] DB error for task {task_id} — {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to close task")
    finally:
        db.close()
