"""Lifecycle API for research studies.

A research study has one configuration authority on ``public.study``. The old
revision/draft/publication API was removed; study configuration is created once,
metadata can be edited before consent, and stop is terminal and non-destructive.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from backend.routers.research.access import require_researcher, require_study_owner
from research.runtime.sessions.models import SessionPolicyV1
from research.study.lifecycle import (
    CloneNotAllowedError,
    ResearchStudyNotFoundError,
    allocate_join_code,
    clone_stopped_research_study,
    revoke_research_enrollment,
    stop_research_study,
    update_research_metadata,
)
from research.study.protocol import store
from research.analysis.operations import store as operations_store

router = APIRouter()


class StudyCreateRequest(BaseModel):
    """Create one research study configuration."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    telemetry_policy: dict[str, Any] = Field(default_factory=dict)
    session_policy: dict[str, Any] = Field(default_factory=dict)
    # Required and non-empty: a research study is created with its fixed profile
    # selection. The clone route re-validates any selection it carries through
    # the same freeze; a clone body may omit profiles and stay profile-less.
    profile_ids: list[uuid.UUID] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class StudyCloneRequest(BaseModel):
    """Optional profile selection completing a stopped-study clone.

    Supplying ``profile_ids`` runs the same validation/freeze as create, so the
    clone is joinable. Omitting the body keeps the clone profile-less; such a
    clone is explicitly unusable and join refuses it until a profile-bearing
    clone is requested (ISSUE-12).
    """

    profile_ids: Optional[list[uuid.UUID]] = Field(default=None, min_length=1)


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


def _validated_session_policy(session_policy: dict[str, Any]) -> dict[str, Any]:
    """Validate and freeze the study's complete typed session policy.

    A study whose policy the session endpoints would reject must never be
    stored: creation fails with a typed 4xx instead of producing a study that
    can be joined but can never open an authoritative session (ISSUE-05).
    """
    try:
        parsed = SessionPolicyV1.model_validate(session_policy)
    except ValidationError as error:
        first = error.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = first.get("msg", "invalid session policy")
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SESSION_POLICY_INVALID",
                "message": f"{location}: {message}" if location else message,
            },
        ) from error
    return parsed.model_dump(mode="json")


def _validated_telemetry_policy(telemetry_policy: dict[str, Any]) -> dict[str, Any]:
    """Reject a malformed telemetry policy at creation (ISSUE-01/ISSUE-05).

    Content capture is only ever declared, never inferred: ``content_capture``
    must be a boolean and the field-class allowlist must contain only the
    known policy vocabulary.
    """
    # The study-facing policy vocabulary is ``TelemetryFieldClass``
    # (STRUCTURAL / METRICS / DIAGNOSTICS / CONTENT). The runtime privacy
    # vocabulary (SYSTEM / BEHAVIORAL / CODE_METADATA / CONTENT) is derived from
    # it by the privacy engine, so it must not be what a researcher authors.
    from research.study.protocol.enums import TelemetryFieldClass
    from research.telemetry.enums import FieldClass

    if not isinstance(telemetry_policy, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TELEMETRY_POLICY_INVALID",
                "message": "telemetry_policy must be an object",
            },
        )
    content_capture = telemetry_policy.get("content_capture")
    if content_capture is not None and not isinstance(content_capture, bool):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "TELEMETRY_POLICY_INVALID",
                "message": "content_capture must be a boolean",
            },
        )
    allowed = telemetry_policy.get("allowed_field_classes")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(
            isinstance(item, str) for item in allowed
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TELEMETRY_POLICY_INVALID",
                    "message": (
                        "allowed_field_classes must be a list of field-class names "
                        f"({', '.join(sorted({c.value for c in TelemetryFieldClass}))})"
                    ),
                },
            )
        # Accept both vocabularies: the study-facing authoring names
        # (STRUCTURAL/METRICS/DIAGNOSTICS/CONTENT) and the runtime privacy names
        # (SYSTEM/BEHAVIORAL/CODE_METADATA/CONTENT) used by older callers.
        known = {field_class.value for field_class in TelemetryFieldClass} | {
            field_class.value for field_class in FieldClass
        }
        unknown = sorted(set(allowed) - known)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TELEMETRY_POLICY_INVALID",
                    "message": f"unknown field classes: {', '.join(unknown)}",
                },
            )
    return telemetry_policy


def _study_payload(study: Any, db: Any) -> dict[str, Any]:
    def iso(value: Any) -> Any:
        return value.isoformat() if isinstance(value, datetime) else value

    metrics = store.get_study_read_metrics(db, study.study_id)
    stopped = getattr(study, "research_status", None) == "STUDY_STOPPED"
    config = getattr(study, "research_config_json", None) or {}
    switch = operations_store.latest_study_kill_switch(db, study.study_id)
    switch_status = None
    if switch is not None:
        switch_status = "ENGAGED" if switch.is_engaged(datetime.now(timezone.utc)) else "RELEASED"
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
        # The frozen policies are echoed so the UI can display/edit the actual
        # authority; metadata PATCH never changes them.
        "telemetry_policy": config.get("telemetry_policy") or {},
        "session_policy": config.get("session_policy") or {},
        "starts_at": iso(getattr(study, "starts_at", None)),
        "ends_at": iso(getattr(study, "ends_at", None)),
        "created_at": iso(getattr(study, "created_at", None)),
        "enrollment_count": metrics.enrollment_count,
        "active_enrollment_count": metrics.active_enrollment_count,
        "assignment_count": metrics.assignment_count,
        "active_assignment_count": metrics.active_assignment_count,
        "active_session_count": metrics.active_session_count,
        "collection_status": "STOPPED" if stopped else (
            "ACTIVE" if metrics.active_session_count else "IDLE"
        ),
        "lifecycle_capabilities": {
            "metadata_editable": not stopped and getattr(study, "consent_locked_at", None) is None,
            "stoppable": not stopped,
            "cloneable": stopped,
            "joinable": not stopped and getattr(study, "research_status", None) in {"DRAFT", "ACTIVE"},
        },
        "kill_switch": (
            {
                "switch_id": str(switch.switch_id),
                "status": "ENGAGED" if stopped else switch_status,
                "reason": switch.reason,
            }
            if switch is not None
            else None
        ),
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
        session_policy = _validated_session_policy(payload.session_policy)
        telemetry_policy = _validated_telemetry_policy(payload.telemetry_policy)
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
                "telemetry_policy": telemetry_policy,
                "session_policy": session_policy,
                "profile_ids": [str(profile_id) for profile_id in payload.profile_ids],
            },
            join_code=allocate_join_code(db),
            profile_ids=payload.profile_ids,
            allow_shared_profiles=current_user.is_admin,
        )
        return JsonResponseWithStatus(
            status_code=201,
            content=cast(Any, {"study": _study_payload(study, db)}),
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
            content=cast(Any, {"studies": [_study_payload(study, db) for study in studies]}),
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
            content=cast(Any, {"study": _study_payload(study, db)}),
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
            content=cast(Any, {"study": _study_payload(updated, db)}),
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
                "study": _study_payload(updated, db) if updated is not None else None,
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
    payload: Optional[StudyCloneRequest] = None,
):
    """Clone a stopped study, optionally completing it with profile selections.

    No body keeps the historical profile-less draft. A body carrying
    ``profile_ids`` freezes those profiles with the create-time invariants and
    inserts the selection rows atomically, so the clone is joinable (ISSUE-12).
    """
    require_researcher(current_user)
    db = app.get_db_session()
    try:
        _authorize_study(db, current_user, study_id)
        try:
            clone = clone_stopped_research_study(
                db,
                study_id,
                actor=current_user.email,
                profile_ids=payload.profile_ids if payload is not None else None,
                allow_shared_profiles=current_user.is_admin,
            )
        except CloneNotAllowedError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(
                status_code=403,
                detail={"code": "PROFILE_NOT_ALLOWED", "message": str(error)},
            ) from error
        except ResearchStudyNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            message = str(error)
            code = getattr(error, "code", None) or (
                message.split(":", 1)[0] if ":" in message else "PROFILE_NOT_ALLOWED"
            )
            raise HTTPException(
                status_code=422,
                detail={"code": code, "message": message},
            ) from error
        # Re-read through the store view so the response carries the frozen
        # profile selections this clone just inserted, not the bare ORM row.
        clone_view = store.get_study(db, clone.study_id) or clone
        return JsonResponseWithStatus(
            status_code=201,
            content=cast(Any, {"study": _study_payload(clone_view, db)}),
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
