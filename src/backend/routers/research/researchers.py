"""Administrator researcher enablement (P3).

Mounted under ``/api/research/researchers``. An administrator flips the single
``user.can_research`` flag that lets an account own private profiles and create
studies. There is no self-service route: no non-administrator can change
``can_research`` or ``is_admin``.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    require_admin,
)
from database import crud

router = APIRouter()


class ResearcherEnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_research: bool


def _user_payload(user) -> dict:
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "name": user.name,
        "is_admin": bool(user.is_admin),
        "can_research": bool(user.can_research),
    }


@router.get("", summary="List enabled researcher accounts (admin)")
def list_researchers(
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        return JsonResponseWithStatus(
            status_code=200,
            content={"researchers": [ _user_payload(u) for u in crud.list_researchers(db)]},
        )
    finally:
        db.close()


@router.put("/{user_id}", summary="Enable or disable a researcher account (admin)")
def set_researcher_enabled(
    user_id: uuid.UUID,
    payload: ResearcherEnableRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance),
):
    require_admin(current_user)
    db = app.get_db_session()
    try:
        user = crud.set_user_can_research(db, user_id, payload.can_research)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return JsonResponseWithStatus(
            status_code=200, content={"user": _user_payload(user)}
        )
    finally:
        db.close()
