"""Study analytics read models (study owner or administrator only).

Mounted under ``/api/research/studies``:

* ``GET /{study_id}/analytics/participants`` - participants table: each
  enrollment's frozen arm, session and activity summary and health.
* ``GET /{study_id}/analytics/participants/{enrollment_id}`` - one
  participant's telemetry dashboard (metric set, daily activity, tools, turns,
  sessions, metadata-only timeline).
* ``GET /{study_id}/analytics/summary`` - study totals and the arm comparison on
  participant-level values; optional inclusive UTC ``start``/``end`` dates.

Everything is read-only, study-scoped and metadata-only: no content fields and
no login identity are ever read or returned.
"""

from __future__ import annotations

import re
import uuid  # noqa: TC003 - FastAPI resolves the path-parameter annotations at runtime
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from research.analysis.study_analytics import metrics as analytics
from research.analysis.study_analytics import store as analytics_store
from research.analysis.study_analytics.models import DateWindow
from research.study.protocol import store as study_store

router = APIRouter()

_DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _authorize(db: Any, current_user: AuthenticatedUser, study_id: uuid.UUID) -> Any:
    """404 for an unknown study, 403 ``FORBIDDEN_STUDY`` for a non-owner."""
    from backend.routers.research.access import require_study_owner

    study = study_store.get_study(db, study_id)
    if study is None:
        raise HTTPException(status_code=404, detail="Study not found")
    require_study_owner(current_user, study)
    return study


def _enrollment_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "ENROLLMENT_NOT_FOUND",
            "message": "no such enrollment in this study",
        },
    )


def _parse_day(value: Optional[str], name: str) -> Optional[date]:
    if value is None or value == "":
        return None
    if not _DAY_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DATE",
                "message": f"{name} must be a YYYY-MM-DD date",
            },
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_DATE",
                "message": f"{name} is not a valid calendar date",
            },
        ) from error


def _window(start: Optional[str], end: Optional[str]) -> DateWindow:
    window = DateWindow(start=_parse_day(start, "start"), end=_parse_day(end, "end"))
    if window.start is not None and window.end is not None and window.start > window.end:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_WINDOW",
                "message": "start must not be after end",
            },
        )
    return window


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get(
    "/{study_id}/analytics/participants",
    summary="Participants table with arm and activity (study owner/admin only)",
)
def study_participants_analytics(
    study_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        body = analytics.build_participants(
            analytics_store.load_study_frame(db, study_id),
            analytics_store.load_events(db, study_id),
            analytics_store.load_daily_event_counts(db, study_id),
            study_id=str(study_id),
            now=_now(),
        )
        return JsonResponseWithStatus(status_code=200, content=body)
    finally:
        db.close()


@router.get(
    "/{study_id}/analytics/participants/{enrollment_id}",
    summary="One participant's telemetry dashboard (study owner/admin only)",
)
def study_participant_dashboard(
    study_id: uuid.UUID,
    enrollment_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        if analytics_store.enrollment_study_id(db, enrollment_id) != study_id:
            raise _enrollment_not_found()
        body = analytics.build_participant_detail(
            analytics_store.load_study_frame(db, study_id, enrollment_id=enrollment_id),
            str(enrollment_id),
            analytics_store.load_events(db, study_id, enrollment_id=enrollment_id),
            analytics_store.load_daily_event_counts(
                db, study_id, enrollment_id=enrollment_id
            ),
            analytics_store.load_timeline(db, study_id, enrollment_id),
            study_id=str(study_id),
            now=_now(),
        )
        if body is None:
            raise _enrollment_not_found()
        return JsonResponseWithStatus(status_code=200, content=body)
    finally:
        db.close()


@router.get(
    "/{study_id}/analytics/summary",
    summary="Study totals and arm comparison (study owner/admin only)",
)
def study_analytics_summary(
    study_id: uuid.UUID,
    start: Optional[str] = Query(None, description="Inclusive UTC start date (YYYY-MM-DD)"),
    end: Optional[str] = Query(None, description="Inclusive UTC end date (YYYY-MM-DD)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    db = app.get_db_session()
    try:
        _authorize(db, current_user, study_id)
        window = _window(start, end)
        body = analytics.build_study_summary(
            analytics_store.load_study_frame(db, study_id),
            analytics_store.load_events(db, study_id, window=window),
            analytics_store.load_daily_event_counts(db, study_id, window=window),
            study_id=str(study_id),
            now=_now(),
            window=window,
        )
        return JsonResponseWithStatus(status_code=200, content=body)
    finally:
        db.close()
