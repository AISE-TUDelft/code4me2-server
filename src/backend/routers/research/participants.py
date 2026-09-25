"""Participant enrollment inspection and privacy-operator retention views.

Mounted under ``/api/research/participants``. Consent is a single acceptance
recorded at join; there is no consent, re-consent or withdrawal surface here.

* ``GET /me`` — the caller's study-local enrollment projections.
* ``GET /enrollments/{id}`` — administrator inspection.
* deletion-ledger / retention-job routes — administrator privacy-operator views
  for the study-end retention path.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from backend.routers.research.join import (
    _collection_policy as consent_collection_policy,
)
from research.participants import identity as store
from research.participants import retention as ret_api
from research.participants.enums import RetentionJobState

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _my_study_payload(study_row: Any) -> Optional[dict[str, Any]]:
    """The public study facts a participant saw when joining, or ``None``.

    ``collection`` is the frozen policy the join consent text is composed from
    (``join.consent_text`` resolves it the way ingestion does). Participants who
    joined before that text listed tool titles, error messages and code
    metadata consented to the earlier, shorter notice.
    """
    if study_row is None:
        return None
    return {
        "study_id": str(study_row.study_id),
        "name": study_row.name,
        "description": study_row.description,
        "research_status": getattr(study_row, "research_status", None),
        "starts_at": _iso(getattr(study_row, "starts_at", None)),
        "ends_at": _iso(getattr(study_row, "ends_at", None)),
        "collection": consent_collection_policy(study_row),
    }


def _my_enrollment_payload(enrollment: Any, details: dict[str, Any]) -> dict[str, Any]:
    """The caller's own enrollment: projection fields plus study context.

    Additive over :func:`store.researcher_projection` (the plugin parses
    ``enrollment_id``/``study_id``/``status``). ``runtime`` names only the
    runtime kind; the assigned profile, model and digest are never returned,
    so the participant stays blind to their arm.
    """
    payload = store.researcher_projection(enrollment)
    payload["consent_accepted_at"] = _iso(enrollment.consent_accepted_at)
    payload["study"] = _my_study_payload(details.get("study_row"))
    payload["runtime"] = details.get("runtime")
    payload["sessions"] = details["sessions"]
    payload["activity"] = details["activity"]
    return payload


def _issue_payload(issue: Any) -> dict[str, Any]:
    if issue is None:
        return {}
    return issue.model_dump(mode="json")


def _owned_enrollment(
    db: Any, current_user: AuthenticatedUser, enrollment_id: uuid.UUID
):
    """Return the caller's enrollment or raise 404 without revealing ownership."""
    participant_row = store.get_participant_by_account(db, current_user.user_id)
    if participant_row is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    row = store.get_enrollment(db, enrollment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    enrollment = store.row_to_enrollment(row)
    if enrollment.participant_id != participant_row.participant_id:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return enrollment


@router.get("/me", summary="Get my study enrollment status")
def get_my_status(
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Return the caller's own enrollments with their study context.

    Each entry keeps the study-local projection and adds
    ``consent_accepted_at``, the public ``study`` facts (with the frozen
    collection policy), the ``runtime`` kind, and ``sessions``/``activity``
    summaries. Only the caller's own enrollments are read.
    """
    db = app.get_db_session()
    try:
        participant_row = store.get_participant_by_account(db, current_user.user_id)
        if participant_row is None:
            return JsonResponseWithStatus(status_code=200, content={"enrollments": []})
        rows = store.list_enrollments(db, participant_row.participant_id)
        enrollments = [store.row_to_enrollment(row) for row in rows]
        details = store.participant_enrollment_details(db, enrollments)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "enrollments": [
                    _my_enrollment_payload(
                        enrollment, details[enrollment.enrollment_id]
                    )
                    for enrollment in enrollments
                ]
            },
        )
    finally:
        db.close()


@router.get("/enrollments/{enrollment_id}", summary="Inspect an enrollment")
def get_enrollment_route(
    enrollment_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Administrator view of one pseudonymous enrollment projection."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = store.get_enrollment(db, enrollment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Enrollment not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"enrollment": store.researcher_projection(store.row_to_enrollment(row))},
        )
    finally:
        db.close()


@router.get("/deletion-ledger", summary="Read the deletion ledger")
def read_deletion_ledger(
    enrollment_id: Optional[uuid.UUID] = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Privacy-operator view of the append-only deletion ledger."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        rows = store.list_deletion_ledger(db, enrollment_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"entries": [store.deletion_ledger_summary(row) for row in rows]},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Retention execution jobs (privacy-operator)
# ---------------------------------------------------------------------------

_RETRYABLE_STATES = frozenset({RetentionJobState.FAILED, RetentionJobState.RETRYABLE})


@router.get("/retention-jobs/{job_id}", summary="Fetch one retention job's status")
def get_retention_job_status(
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Privacy-operator view of one content-free retention job summary."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = ret_api.get_retention_job(db, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Retention job not found")
        return JsonResponseWithStatus(
            status_code=200,
            content={"job": ret_api.job_summary(ret_api.row_to_retention_job(row))},
        )
    finally:
        db.close()


@router.get(
    "/enrollments/{enrollment_id}/retention-jobs",
    summary="List an enrollment's retention jobs",
)
def list_enrollment_retention_jobs(
    enrollment_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Privacy-operator view of an enrollment's retention-job history."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        rows = ret_api.list_retention_jobs_for_enrollment(db, enrollment_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "jobs": [
                    ret_api.job_summary(ret_api.row_to_retention_job(row))
                    for row in rows
                ]
            },
        )
    finally:
        db.close()


@router.post(
    "/retention-jobs/{job_id}/retry",
    summary="Re-drive a FAILED/RETRYABLE retention job",
)
def retry_retention_job(
    job_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Re-drive a failed/retryable job; a completed job is never re-executed."""
    require_admin(current_user)
    db = app.get_db_session()
    try:
        row = ret_api.get_retention_job(db, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Retention job not found")
        job = ret_api.row_to_retention_job(row)
        if job.state not in _RETRYABLE_STATES:
            raise HTTPException(
                status_code=409,
                detail={"code": "NOT_RETRYABLE", "state": job.state.value},
            )
        redriven = ret_api.run_retention_job(db, job_id, now=_now())
        return JsonResponseWithStatus(
            status_code=200, content={"job": ret_api.job_summary(redriven)}
        )
    finally:
        db.close()
