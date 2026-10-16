"""Privacy-scoped agent analytics over the canonical event authority (phase 06).

Both routes delegate to one owner-scoped canonical query path
(:mod:`research.analysis.read_models.dashboard`), which reads ``research_event``
joined to ``agent_task`` by the explicit phase-05 binding
(``research_event.agent_run_id = agent_task.external_run_id``). Ownership is
enforced before the joins; there is no ACP-session-id or time-window bridging,
and a legacy run with no canonical events renders explicit unavailable values.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from App import App
from backend.Responses import JsonResponseWithStatus
from research.analysis.read_models.dashboard import agent_overview, agent_run_detail

from .auth_utils import AuthenticatedUser, get_current_user

router = APIRouter()


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
    """Canonical, owner-scoped agent telemetry without content payloads."""
    db = app.get_db_session()
    try:
        content = agent_overview(
            db,
            current_user,
            time_window=time_window,
            framework=framework,
            model=model,
            profile=profile,
            user_id=user_id,
        )
        return JsonResponseWithStatus(status_code=200, content=content)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"Error retrieving agent analytics: {error}"
        ) from error
    finally:
        db.close()


@router.get("/runs/{task_id}")
def get_agent_run_detail(
    task_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return one authorized run's canonical event timeline, never content."""
    db = app.get_db_session()
    try:
        content = agent_run_detail(db, current_user, task_id=task_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Agent run not found")
        return JsonResponseWithStatus(status_code=200, content=content)
    except HTTPException:
        raise
    except Exception as error:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"Error retrieving agent run: {error}"
        ) from error
    finally:
        db.close()
