"""Study/profile-scoped researcher read models."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from research.analysis.read_models import service as read_service
from research.analysis.read_models import store as read_store
from research.study.protocol import store as study_store

operations_router = APIRouter()


def _authorize(db: Any, current_user: AuthenticatedUser, study_id: uuid.UUID) -> Any:
    from backend.routers.research.access import require_study_owner

    study = study_store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    require_study_owner(current_user, study)
    return study


@operations_router.get("/enrollments/coverage", summary="Enrollment coverage for a study")
def enrollment_coverage(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        model = read_service.build_enrollment_coverage(
            read_store.list_enrollments(db, study_id), study_id=study_id
        )
        return JsonResponseWithStatus(status_code=200, content=model.model_dump(mode="json"))
    finally:
        db.close()


@operations_router.get("/telemetry-coverage", summary="Telemetry coverage for a study")
def telemetry_coverage(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        model = read_service.build_telemetry_coverage(
            read_store.list_events(db, study_id), study_id=study_id
        )
        return JsonResponseWithStatus(status_code=200, content=model.model_dump(mode="json"))
    finally:
        db.close()
