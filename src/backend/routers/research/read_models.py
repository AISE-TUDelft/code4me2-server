"""Researcher operations read models (Issue 12).

``operations_router`` - RBAC-scoped revision, enrollment, exposure and
telemetry coverage/metric read models. It is contributed to the single
``/operations`` router (see :mod:`backend.routers.research`) rather than
mounted separately, so there is exactly one operations/read API surface.
Handler logic lives in :mod:`research.analysis.read_models`.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
)
from research.analysis.read_models import store as read_store
from research.analysis.read_models.service import (
    build_condition_exposures,
    build_enrollment_coverage,
)
from research.study.protocol import store as protocol_store

operations_router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authorize(db: Any, current_user: AuthenticatedUser, study_id: uuid.UUID) -> None:
    """Authorize study read access by ownership (admin bypasses)."""
    from backend.routers.research.access import require_study_owner

    study = protocol_store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    require_study_owner(current_user, study)


def _load_revision(db: Any, revision_id: uuid.UUID, study_id: uuid.UUID):
    row = protocol_store.get_revision(db, revision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Study revision not found")
    revision = protocol_store.row_to_revision(row)
    if revision.study_id != study_id:
        raise HTTPException(status_code=409, detail="revision does not belong to study")
    return revision


@operations_router.get(
    "/enrollments/coverage", summary="Enrollment-state coverage for a revision"
)
def enrollment_coverage(
    study_id: uuid.UUID,
    revision_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        revision = _load_revision(db, revision_id, study_id)
        enrollments = read_store.list_enrollments(db, study_id)
        model = build_enrollment_coverage(
            enrollments,
            study_id=study_id,
            revision_id=revision_id,
            revision_digest=revision.protocol_digest,
        )
        return JsonResponseWithStatus(status_code=200, content=model.model_dump(mode="json"))
    finally:
        db.close()


@operations_router.get(
    "/exposures", summary="Assignment vs exposure per condition"
)
def condition_exposures(
    study_id: uuid.UUID,
    revision_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        revision = _load_revision(db, revision_id, study_id)
        assignments = read_store.list_assignments(db, study_id)
        exposures = read_store.list_exposures(db, revision_id)
        models = build_condition_exposures(
            assignments,
            exposures,
            study_id=study_id,
            revision_id=revision_id,
            revision_digest=revision.protocol_digest,
        )
        return JsonResponseWithStatus(
            status_code=200,
            content={"conditions": [model.model_dump(mode="json") for model in models]},
        )
    finally:
        db.close()


