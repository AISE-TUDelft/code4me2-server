"""Lifecycle API for research studies.

A research study has one configuration authority on ``public.study``. The old
revision/draft/publication API was removed; study configuration is created once,
metadata can be edited before consent, and stop is terminal and non-destructive.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from backend.routers.research.access import require_researcher, require_study_owner
from research.study.lifecycle import (
    allocate_join_code,
    clone_stopped_research_study,
    revoke_research_enrollment,
    stop_research_study,
    update_research_metadata,
)
from research.study.protocol import store

router = APIRouter()


class StudyCreateRequest(BaseModel):
    """Create one research study configuration."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    telemetry_policy: dict[str, Any] = Field(default_factory=dict)
    session_policy: dict[str, Any] = Field(default_factory=dict)
    profile_ids: list[uuid.UUID] = Field(default_factory=list, min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class StudyMetadataUpdateRequest(BaseModel):
    """Metadata fields allowed before the first consent."""

    name: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None


class StopStudyRequest(BaseModel):
    actor: Optional[str] = None


class RevokeEnrollmentRequest(BaseModel):
    actor: Optional[str] = None


def _load_study(db: Any, study_id: uuid.UUID):
    study = store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    return study


def _authorize_study(
    db: Any,
    current_user: AuthenticatedUser,
    study_id: uuid.UUID,
):
    study = _load_study(db, study_id)
    require_study_owner(current_user, study)
    return study


def _study_payload(study: Any) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    return {
        "study_id": str(study.study_id),
        "name": study.name,
        "description": study.description,
        "owner": getattr(study, "owner", None),
        "created_by": str(study.created_by) if study.created_by else None,
        "is_research": bool(getattr(study, "is_research", False)),
        "is_active": bool(getattr(study, "is_active", False)),
        "research_status": getattr(study, "research_status", None),
        "research_config_digest": getattr(study, "research_config_digest", None),
        "join_code": getattr(study, "join_code", None),
        "consent_locked_at": iso(getattr(study, "consent_locked_at", None)),
        "stopped_at": iso(getattr(study, "stopped_at", None)),
        "stopped_by": getattr(study, "stopped_by", None),
        "profile_selections": list(getattr(study, "profile_selections", []) or []),
        "starts_at": iso(getattr(study, "starts_at", None)),
        "ends_at": iso(getattr(study, "ends_at", None)),
        "created_at": iso(getattr(study, "created_at", None)),
    }


@router.post("", summary="Create a research study")
def create_study(
    payload: StudyCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        study = store.create_study(
            db,
            study_id=uuid.uuid4(),
            name=payload.name,
            description=payload.description,
            created_by=current_user.user_id,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            is_research=True,
            research_config_json={
                "telemetry_policy": payload.telemetry_policy,
                "session_policy": payload.session_policy,
                "profile_ids": [str(profile_id) for profile_id in payload.profile_ids],
            },
            join_code=allocate_join_code(db),
            profile_ids=payload.profile_ids,
            allow_shared_profiles=current_user.is_admin,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content=cast(Any, {"study": _study_payload(study)}),
        )
    except PermissionError as error:
        db.rollback()
        raise HTTPException(
            status_code=403,
            detail={"code": "PROFILE_NOT_ALLOWED", "message": str(error)},
        ) from error
    except ValueError as error:
        db.rollback()
        message = str(error)
        code = getattr(error, "code", None) or (
            message.split(":", 1)[0] if ":" in message else "PROFILE_NOT_ALLOWED"
        )
        raise HTTPException(
            status_code=422,
            detail={"code": code, "message": message},
        ) from error
    finally:
        db.close()


@router.get("", summary="List the caller's research studies")
def list_studies(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        owner_id = None if current_user.is_admin else current_user.user_id
        studies = store.list_studies(db, owner_id)
        return JsonResponseWithStatus(
            status_code=200,
            content=cast(Any, {"studies": [_study_payload(study) for study in studies]}),
        )
    finally:
        db.close()


@router.get("/{study_id}", summary="Get one research study")
def get_study(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        study = _authorize_study(db, current_user, study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content=cast(Any, {"study": _study_payload(study)}),
        )
    finally:
        db.close()


@router.patch("/{study_id}/metadata", summary="Update unlocked study metadata")
def update_study_metadata(
    study_id: uuid.UUID,
    payload: StudyMetadataUpdateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    if all(value is None for value in (payload.name, payload.description, payload.starts_at, payload.ends_at)):
        raise HTTPException(status_code=422, detail="at least one metadata field is required")
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        try:
            updated = update_research_metadata(
                db,
                study_id,
                name=payload.name,
                description=payload.description,
                starts_at=payload.starts_at,
                ends_at=payload.ends_at,
            )
        except PermissionError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "STUDY_METADATA_LOCKED", "message": str(error)},
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=200,
            content=cast(Any, {"study": _study_payload(updated)}),
        )
    finally:
        db.close()


@router.post("/{study_id}/stop", summary="Stop a research study permanently")
def stop_study(
    study_id: uuid.UUID,
    payload: StopStudyRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        summary = stop_research_study(db, study_id, actor=payload.actor or current_user.email)
        updated = store.get_study(db, study_id)
        return JsonResponseWithStatus(
            status_code=200,
            content=cast(Any, {
                "study": _study_payload(updated) if updated is not None else None,
                "stopped": True,
                "enrollment_count": summary.enrollment_count,
                "assignment_count": summary.assignment_count,
                "session_count": summary.session_count,
            }),
        )
    finally:
        db.close()


@router.post("/{study_id}/clone", summary="Clone a stopped research study")
def clone_study(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        try:
            clone = clone_stopped_research_study(db, study_id, actor=current_user.email)
        except PermissionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=201,
            content=cast(Any, {"study": _study_payload(clone)}),
        )
    finally:
        db.close()


@router.post(
    "/{study_id}/enrollments/{enrollment_id}/revoke",
    summary="Revoke one participant enrollment",
)
def revoke_enrollment(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    payload: RevokeEnrollmentRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        try:
            summary = revoke_research_enrollment(db, study_id, enrollment_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=200,
            content=cast(Any, {
                "revoked": True,
                "enrollment_id": str(summary.enrollment_id),
                "session_count": summary.session_count,
            }),
        )
    finally:
        db.close()
