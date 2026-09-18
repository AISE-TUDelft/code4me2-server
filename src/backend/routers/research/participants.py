"""Participant enrollment inspection and privacy-operator retention views.

Mounted under ``/api/research/participants``. Consent is a single acceptance
recorded at join; there is no consent, re-consent or withdrawal surface here.

* ``GET /me`` — the caller's study-local enrollment projections.
* ``POST /enrollments`` — create (or reuse) the caller's enrollment for a
  published revision; delegates to the same account-wide owner as the join code.
* ``GET /enrollments/{id}`` — administrator inspection.
* deletion-ledger / retention-job routes — administrator privacy-operator views
  for the study-end retention path.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from research.participants import identity as store
from research.participants import retention as ret_api
from research.participants.enums import RetentionJobState
from research.participants.models import RevisionRef
from research.study.protocol import store as protocol_store
from research.study.protocol.enums import RevisionStatus

router = APIRouter()


class EnrollmentRequestBody(BaseModel):
    """Request enrollment for an existing published revision."""

    study_id: uuid.UUID
    revision_id: uuid.UUID


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue_payload(issue: Any) -> dict[str, Any]:
    if issue is None:
        return {}
    return issue.model_dump(mode="json")


def _load_revision_ref(db: Any, revision_id: uuid.UUID) -> Optional[RevisionRef]:
    """Return the published revision's enrollment slice, or ``None``.

    Both entry points (join and participants) must bind enrollment to a
    *published* revision; a draft or retired revision never enrolls.
    """
    row = protocol_store.get_revision(db, revision_id)
    if row is None:
        return None
    if row.status != RevisionStatus.PUBLISHED.value:
        return None
    return store.revision_ref_from_mapping(
        row.protocol_json, revision_id=row.revision_id, study_id=row.study_id
    )


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
    """Return only study-local enrollment projections for the caller."""
    db = app.get_db_session()
    try:
        participant_row = store.get_participant_by_account(db, current_user.user_id)
        if participant_row is None:
            return JsonResponseWithStatus(status_code=200, content={"enrollments": []})
        rows = store.list_enrollments(db, participant_row.participant_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "enrollments": [
                    store.researcher_projection(store.row_to_enrollment(row))
                    for row in rows
                ]
            },
        )
    finally:
        db.close()


@router.post("/enrollments", summary="Request enrollment in a study revision")
def request_enrollment(
    payload: EnrollmentRequestBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Create (or reuse) the caller's enrollment; never a second active one."""
    db = app.get_db_session()
    try:
        revision_ref = _load_revision_ref(db, payload.revision_id)
        if revision_ref is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "REVISION_NOT_PUBLISHED",
                    "message": "study revision not found or not published",
                },
            )
        if revision_ref.study_id != payload.study_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "REVISION_STUDY_MISMATCH",
                    "message": "the revision does not belong to the given study",
                },
            )

        # Same shared account-wide enrollment owner as the join-code route.
        opened = store.open_enrollment(db, current_user.user_id, revision_ref, now=_now())
        if opened.issue is not None or opened.enrollment is None:
            raise HTTPException(status_code=409, detail=_issue_payload(opened.issue))

        return JsonResponseWithStatus(
            status_code=201 if opened.created else 200,
            content={
                "enrollment": store.researcher_projection(opened.enrollment),
                "created": opened.created,
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
