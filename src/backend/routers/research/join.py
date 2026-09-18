"""Web-only research study join and consent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from research.study.lifecycle import open_study_enrollment
from research.study.protocol import store as study_store

router = APIRouter()

GLOBAL_CONSENT_TEXT = (
    "This study collects metadata about how you use the research agent and the "
    "IDE. Provider credentials are never collected. Your study-local pseudonym "
    "is used in research data; your account identity remains private."
)


class JoinRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    join_code: str = Field(min_length=1)
    accept_consent: bool = False


def _study_from_code(db: Any, join_code: str):
    return study_store.get_study_by_join_code(db, join_code)


def _study_payload(study: Any) -> dict[str, Any]:
    return {
        "study_id": str(study.study_id),
        "name": study.name,
        "description": study.description,
        "research_status": getattr(study, "research_status", None),
    }


@router.get("/{join_code}", summary="Resolve a study-owned join code")
def resolve_join_code(
    join_code: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        study = _study_from_code(db, join_code)
        if study is None:
            raise HTTPException(status_code=404, detail="Join code not found")
        if getattr(study, "research_status", None) == "STUDY_STOPPED":
            raise HTTPException(
                status_code=409,
                detail={"code": "STUDY_STOPPED", "message": "the study has been stopped"},
            )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "join_code": study.join_code,
                "study": _study_payload(study),
                "consent": {"text": GLOBAL_CONSENT_TEXT},
            },
        )
    finally:
        db.close()


@router.post("", summary="Accept consent and enroll in a study")
def redeem_join_code(
    payload: JoinRequestBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    if not payload.accept_consent:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONSENT_REQUIRED", "message": "accept consent before joining"},
        )
    db = app.get_db_session()
    try:
        try:
            result = open_study_enrollment(db, current_user.user_id, payload.join_code)
        except PermissionError as error:
            message = str(error)
            code = (
                "STUDY_STOPPED"
                if "stopped" in message
                else "ALREADY_ENROLLED"
                if "rejoin" in message
                else "ACTIVE_ENROLLMENT_EXISTS"
            )
            raise HTTPException(status_code=409, detail={"code": code, "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=201 if result.created else 200,
            content={
                "enrollment_id": str(result.enrollment_id),
                "study_id": str(result.study_id),
                "assignment_id": str(result.assignment_id) if result.assignment_id else None,
                "agent_profile_id": str(result.agent_profile_id) if result.agent_profile_id else None,
                "status": "ACTIVE",
                "created": result.created,
                "reused": result.reused,
            },
        )
    finally:
        db.close()
