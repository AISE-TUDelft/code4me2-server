"""Persistent per-session agent memory.

Endpoints (mounted under ``/api/agent``):

  GET    /memory/{session_id}   read the stored memory snapshot
  PUT    /memory/{session_id}   replace the stored memory snapshot
  DELETE /memory/{session_id}   forget a session's memory

This is the capability group-5 had and group-21 didn't: agent conversation state
survives an IDE (or agent process) restart, so a developer who closes their
editor mid-task can pick the conversation back up instead of starting over.

Ownership is server-derived from the ACP scope on every call, so a runtime can
only read or write memory for sessions belonging to its own user and project —
the ``session_id`` in the path is a lookup key, not an authorization claim.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from App import App
from backend.acp_authorization import AcpSessionAuthorization
from backend.Responses import JsonResponseWithStatus
from backend.routers.agent.acp_auth import require_acp_scope
from database import crud

router = APIRouter()


class AgentMemorySnapshot(BaseModel):
    """The runtime's serialized memory window.

    Messages are stored opaquely: the shape is the runtime's business, and
    schema-checking it here would couple the backend to the adapter's internal
    context format.
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)


def _scope_owns(scope: AcpSessionAuthorization, memory) -> bool:
    return (
        memory.owner_user_id == str(scope.user_id)
        and memory.owner_project_id == str(scope.project_id)
    )


def _serialize(memory) -> dict[str, Any]:
    try:
        payload = json.loads(memory.memory_json) or {}
    except (ValueError, TypeError):
        logging.warning(
            f"[Agent/memory] unparseable snapshot for session {memory.session_id}"
        )
        payload = {}
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    return {
        "session_id": memory.session_id,
        "messages": messages,
        "updated_at": _iso(memory.updated_at),
        "created_at": _iso(memory.created_at),
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


@router.get("/memory/{session_id}", summary="Read persisted agent session memory")
def get_agent_memory(
    session_id: str,
    app: App = Depends(App.get_instance),
    scope: AcpSessionAuthorization = Depends(require_acp_scope),
) -> JsonResponseWithStatus:
    db = app.get_db_session()
    try:
        memory = crud.get_agent_memory_by_session_id(db, session_id)
        if memory is None:
            # A fresh session legitimately has no memory yet — the runtime
            # treats 404 as "start empty", so this is not an error path.
            raise HTTPException(status_code=404, detail="Agent memory not found.")
        if not _scope_owns(scope, memory):
            raise HTTPException(
                status_code=403,
                detail="Agent memory is not authorized for this ACP session.",
            )
        return JsonResponseWithStatus(status_code=200, content=_serialize(memory))
    except HTTPException:
        raise
    except Exception as error:
        logging.error(
            f"[Agent/memory] error reading {session_id}: {error}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Server failed to retrieve agent memory."
        )
    finally:
        db.close()


@router.put("/memory/{session_id}", summary="Replace persisted agent session memory")
def upsert_agent_memory(
    session_id: str,
    body: AgentMemorySnapshot,
    app: App = Depends(App.get_instance),
    scope: AcpSessionAuthorization = Depends(require_acp_scope),
) -> JsonResponseWithStatus:
    """Replace the snapshot for one session.

    Note this is *not* gated by ``store_agent_content``. Memory is operational
    state the agent needs to keep working across restarts — it's the
    conversation itself, not telemetry about it, and gating it would silently
    break the feature rather than protect anything. It is scoped to its owner,
    replaced wholesale on every write, and deletable via DELETE.
    """
    db = app.get_db_session()
    try:
        existing = crud.get_agent_memory_by_session_id(db, session_id)
        if existing is not None and not _scope_owns(scope, existing):
            raise HTTPException(
                status_code=403,
                detail="Agent memory is not authorized for this ACP session.",
            )
        memory = crud.upsert_agent_memory(
            db,
            session_id=session_id,
            owner_user_id=str(scope.user_id),
            owner_project_id=str(scope.project_id),
            messages=body.messages,
        )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                **_serialize(memory),
                "message_count": len(body.messages),
            },
        )
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        logging.error(
            f"[Agent/memory] error saving {session_id}: {error}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Server failed to save agent memory."
        )
    finally:
        db.close()


@router.delete("/memory/{session_id}", summary="Forget a session's agent memory")
def delete_agent_memory(
    session_id: str,
    app: App = Depends(App.get_instance),
    scope: AcpSessionAuthorization = Depends(require_acp_scope),
) -> JsonResponseWithStatus:
    """Delete a session's memory.

    Gives the runtime (and through it the user) a way to actually discard
    conversation state, rather than only overwriting it.
    """
    db = app.get_db_session()
    try:
        existing = crud.get_agent_memory_by_session_id(db, session_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Agent memory not found.")
        if not _scope_owns(scope, existing):
            raise HTTPException(
                status_code=403,
                detail="Agent memory is not authorized for this ACP session.",
            )
        crud.delete_agent_memory(db, session_id)
        return JsonResponseWithStatus(
            status_code=200, content={"deleted": True, "session_id": session_id}
        )
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        logging.error(
            f"[Agent/memory] error deleting {session_id}: {error}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Server failed to delete agent memory."
        )
    finally:
        db.close()
