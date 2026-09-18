"""Self-report telemetry ingestion for the built-in code4me2-agent runtime.

Endpoints (mounted under ``/api/agent``):

  POST /events/ingest    ingest a batch of self-reported events
  GET  /runs/{run_id}    read back one run and its events

This is the ingestion half of merge decision 2. Where third-party agents are
*observed* by the proxy relay, the runtime we own *reports* — and both write
into the same ``agent_event`` table, so no downstream consumer has to know which
runtime produced a row.

The runtime mints its own ``run_id`` before it ever contacts the backend, so the
first batch for a run lazily creates the backing ``agent_task``, drawing the
user's assigned A/B profile at that point (the same sticky draw the plugin path
uses). Subsequent batches attach to the existing task.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from agents import ingest as ingest_module
from agents import lifecycle, registry
from App import App
from backend.acp_authorization import AcpSessionAuthorization  # noqa: TC001 - FastAPI route
from backend.Responses import JsonResponseWithStatus
from backend.routers.agent.acp_auth import require_acp_scope
from backend.routers.agent.consent import resolve_store_agent_content_for_acp
from backend.routers.research.access import resolve_research_binding
from database import crud

router = APIRouter()

# Terminal runtime statuses that should finalize the backing task.
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "error"})


class AgentRunEnvelope(BaseModel):
    """Run-level metadata accompanying every batch.

    ``owner_user_id`` / ``owner_project_id`` are deliberately absent: ownership
    comes from the server-derived ACP scope, so a runtime can't claim someone
    else's run.
    """

    run_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    source: str = Field(default="code4me2_agent", min_length=1)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class AgentEventEnvelope(BaseModel):
    """One self-reported event, in the runtime's own envelope format.

    Kept loose on purpose: ``payload`` / ``metrics`` / ``observability`` are
    free-form because the runtime evolves faster than this schema, and
    ``agents.ingest`` decides which keys are promoted to typed columns versus
    routed into ``extra_json``. Rejecting unknown keys here would mean a
    runtime change silently dropping telemetry.
    """

    event_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    schema_version: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    timestamp: datetime
    sequence: int = Field(..., ge=1)
    source: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    parent_event_id: Optional[str] = None
    message_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_payload: Optional[dict[str, Any]] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    # Descriptive only — the storage decision is made server-side. Retained for
    # audit so a client/server mismatch is visible.
    privacy: dict[str, Any] = Field(default_factory=dict)
    observability: dict[str, Any] = Field(default_factory=dict)


class AgentEventBatchIngest(BaseModel):
    run: AgentRunEnvelope
    events: list[AgentEventEnvelope] = Field(default_factory=list)


def _resolve_or_create_task(
    db,
    *,
    run: AgentRunEnvelope,
    scope: AcpSessionAuthorization,
):
    """Find the task backing this run, creating it on the first batch.

    Returns ``(task, created)``. Raises 403 if a task exists for this run id but
    belongs to a different user or project, and 409 if it belongs to a different
    ACP session — the run id is runtime-generated, so ownership checks are the
    only thing preventing one agent process from writing into another scope's
    task.
    """
    task = crud.get_agent_task_by_external_run_id(db, run.run_id)

    try:
        owner_user_uuid = uuid.UUID(str(scope.user_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="ACP scope has no valid user")
    try:
        owner_project_uuid = uuid.UUID(str(scope.project_id))
    except (ValueError, TypeError):
        owner_project_uuid = None

    if task is not None:
        if task.owner_user_id is not None and task.owner_user_id != owner_user_uuid:
            logging.warning(
                f"[Agent/ingest] 403 — run {run.run_id} owned by another user"
            )
            raise HTTPException(
                status_code=403,
                detail="Agent run is not authorized for this ACP session.",
            )
        # Managed runs are bound to one project and one ACP session at creation
        # (/api/acp/runs). A second project reusing the same run_id — e.g. two
        # IDE windows sharing a user — must not append telemetry to another
        # project's task.
        if (
            task.owner_project_id is not None
            and owner_project_uuid is not None
            and task.owner_project_id != owner_project_uuid
        ):
            logging.warning(
                f"[Agent/ingest] 403 — run {run.run_id} owned by another project"
            )
            raise HTTPException(
                status_code=403,
                detail="Agent run is not authorized for this ACP session.",
            )
        if (
            task.agent_session_id is not None
            and run.session_id != task.agent_session_id
        ):
            logging.warning(
                f"[Agent/ingest] 409 — run {run.run_id} belongs to another ACP session"
            )
            raise HTTPException(
                status_code=409,
                detail="Agent run belongs to another ACP session.",
            )
        return task, False

    assignment = registry.resolve_assignment_context(db, owner_user_uuid)
    if assignment is None:
        raise HTTPException(
            status_code=503,
            detail="No active agent profiles are configured on the server",
        )
    profile = assignment.profile
    content_included = resolve_store_agent_content_for_acp(
        db, scope.user_id, study_id=assignment.study_id
    )
    binding = resolve_research_binding(
        db, account_id=owner_user_uuid, study_id=assignment.study_id
    )

    task = crud.create_agent_task(
        db,
        agent_profile=profile.name,
        model=profile.model,
        approval_policy=profile.approval_policy,
        tools_json=profile.tools_json,
        temperature=profile.temperature,
        framework_version=profile.framework_version,
        source="code4me2_agent",
        owner_user_id=owner_user_uuid,
        funding_owner_user_id=getattr(profile, "funding_owner_user_id", None),
        owner_project_id=owner_project_uuid,
        external_run_id=run.run_id,
        agent_session_id=run.session_id,
        status="running",
        started_at=run.started_at,
        study_id=assignment.study_id,
        study_assignment_id=assignment.assignment_id,
        profile_id=profile.profile_id,
        study_arm_name=assignment.arm_name,
        study_arm_is_baseline=assignment.is_baseline,
        consent_content_storage=content_included,
        # Explicit phase-05 attribution: resolved from the authorized account
        # and the frozen assignment, never guessed. A non-research task keeps
        # these NULL (an explicit "no research context").
        research_session_id=binding.research_session_id if binding else None,
        enrollment_id=binding.enrollment_id if binding else None,
        study_revision_id=binding.study_revision_id if binding else None,
    )
    logging.info(
        f"[Agent/ingest] created task {task.task_id} for run {run.run_id} "
        f"profile={profile.name!r} user={str(owner_user_uuid)[:8]}…"
    )
    return task, True


@router.post("/events/ingest", summary="Ingest self-reported agent telemetry")
def ingest_agent_events(
    body: AgentEventBatchIngest,
    app: App = Depends(App.get_instance),
    scope: AcpSessionAuthorization = Depends(require_acp_scope),
) -> JsonResponseWithStatus:
    """Ingest a batch of events reported by the built-in runtime.

    Idempotent by the runtime's own event ids: a retried batch reports its
    duplicates rather than double-writing, because the uploader can't tell
    whether a timed-out POST was applied.
    """
    if any(event.run_id != body.run.run_id for event in body.events):
        raise HTTPException(
            status_code=400, detail="All events must belong to the request run_id."
        )
    if body.run.source != "code4me2_agent" or any(
        event.source != "code4me2_agent" for event in body.events
    ):
        raise HTTPException(
            status_code=400,
            detail="Managed telemetry must use the code4me2_agent source.",
        )
    if any(event.session_id != body.run.session_id for event in body.events):
        raise HTTPException(
            status_code=400,
            detail="All events must belong to the request ACP session.",
        )

    db = app.get_db_session()
    try:
        task, _created = _resolve_or_create_task(db, run=body.run, scope=scope)

        # Consent is resolved from the *server's* record of the user's
        # preference, not from the `privacy` block in the payload. The research
        # enrollment gate is applied on top, so a non-ACTIVE research
        # enrollment denies content collection even when the legacy preference
        # would allow it.
        content_included = resolve_store_agent_content_for_acp(
            db, scope.user_id, study_id=task.study_id
        )

        ingested, skipped = ingest_module.ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[e.model_dump() for e in body.events],
            content_included=content_included,
            agent_profile=task.agent_profile,
        )

        # A run reporting a terminal status is finished, so aggregate it now
        # rather than waiting for a close call the runtime may never make.
        if body.run.status.strip().lower() in _TERMINAL_RUN_STATUSES:
            status = (
                "failed"
                if body.run.status.strip().lower() in ("failed", "error")
                else "done"
            )
            lifecycle.finalize_agent_task(db, task.task_id, status=status)

        return JsonResponseWithStatus(
            status_code=201,
            content={
                "run_id": body.run.run_id,
                "task_id": str(task.task_id),
                "ingested_event_count": ingested,
                "duplicate_event_ids": skipped,
                "content_stored": content_included,
            },
        )
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        logging.error(f"[Agent/ingest] error ingesting events: {error}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Server failed to ingest agent events."
        )
    finally:
        db.close()


@router.get("/runs/{run_id}", summary="Read back a self-reported agent run")
def get_agent_run(
    run_id: str,
    session_id: str = Query(..., min_length=1, max_length=500),
    app: App = Depends(App.get_instance),
    scope: AcpSessionAuthorization = Depends(require_acp_scope),
) -> JsonResponseWithStatus:
    """Return one run's task row and its events, for the owning agent process."""
    db = app.get_db_session()
    try:
        task = crud.get_agent_task_by_external_run_id(db, run_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Agent run not found.")
        try:
            owner_user_uuid = uuid.UUID(str(scope.user_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="ACP scope has no valid user")
        if task.owner_user_id != owner_user_uuid:
            raise HTTPException(
                status_code=403,
                detail="Agent run is not authorized for this ACP session.",
            )
        try:
            owner_project_uuid = uuid.UUID(str(scope.project_id))
        except (ValueError, TypeError):
            raise HTTPException(status_code=401, detail="ACP scope has no valid project")
        if task.owner_project_id != owner_project_uuid:
            raise HTTPException(
                status_code=403,
                detail="Agent run is not authorized for this ACP project.",
            )
        if task.agent_session_id != session_id:
            raise HTTPException(
                status_code=409,
                detail="Agent run belongs to another ACP session.",
            )

        events = crud.get_agent_events_by_task(db, task.task_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "run": _serialize_task(task),
                "events": [_serialize_event(e) for e in events],
            },
        )
    except HTTPException:
        raise
    except Exception as error:
        logging.error(
            f"[Agent/ingest] error retrieving run {run_id}: {error}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Server failed to retrieve agent run."
        )
    finally:
        db.close()


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _serialize_task(task) -> dict[str, Any]:
    return {
        "task_id": str(task.task_id),
        "run_id": task.external_run_id,
        "agent_session_id": task.agent_session_id,
        "status": task.status,
        "source": task.source,
        "agent_profile": task.agent_profile,
        "model": task.model,
        "framework_version": task.framework_version,
        "total_steps": task.total_steps,
        "input_tokens": task.input_tokens,
        "output_tokens": task.output_tokens,
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
        "created_at": _iso(task.created_at),
    }


def _serialize_event(event) -> dict[str, Any]:
    """Serialise an event row.

    Content columns are echoed as stored — which means null when consent was
    off. Nothing is reconstructed here, so this endpoint can't be used to read
    back content the gate refused to persist.
    """
    return {
        "event_id": str(event.event_id),
        "event_index": event.event_index,
        "event_type": event.event_type,
        "source": event.source,
        "schema_version": event.schema_version,
        "span_id": event.span_id,
        "parent_span_id": str(event.parent_span_id) if event.parent_span_id else None,
        "request_id": event.request_id,
        "model": event.model,
        "latency_ms": event.latency_ms,
        "prompt_tokens": event.prompt_tokens,
        "completion_tokens": event.completion_tokens,
        "total_tokens": event.total_tokens,
        "finish_reason": event.finish_reason,
        "tool_name": event.tool_name,
        "tool_arguments_length": event.tool_arguments_length,
        "tool_result_length": event.tool_result_length,
        "extra_json": event.extra_json,
        "occurred_at": _iso(event.occurred_at),
        "created_at": _iso(event.created_at),
    }
