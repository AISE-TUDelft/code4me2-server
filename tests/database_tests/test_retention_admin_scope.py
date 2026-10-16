"""Admin/privacy-operator scope for the retention-job routes (Task 07 §4).

The participant router exposes read-only retention history, a retry endpoint and
the deletion ledger. There is deliberately no *create* endpoint: retention jobs
are created by the study-end/revocation path, never by an HTTP caller. These
tests pin the route table and assert the ``require_admin`` gate on every route
without needing PostgreSQL.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research import participants
from research.participants.enums import RetentionJobState
from research.participants.models import RetentionJob
from research.study.protocol.enums import RetentionAction

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

RETENTION_PATHS = {
    "/retention-jobs/{job_id}",
    "/enrollments/{enrollment_id}/retention-jobs",
    "/retention-jobs/{job_id}/retry",
}


def _user(*, is_admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=is_admin,
        can_research=is_admin,
        email="admin@example.com" if is_admin else "participant@example.com",
        name="Admin" if is_admin else "Participant",
    )


def _retryable_job() -> RetentionJob:
    return RetentionJob(
        job_id=uuid.uuid4(),
        enrollment_id=uuid.uuid4(),
        action=RetentionAction.RETAIN_ANONYMIZED,
        state=RetentionJobState.FAILED,
        attempts=1,
        created_at=NOW,
        last_error="transient",
    )


def test_participants_router_has_no_retention_create_route():
    routes = participants.router.routes
    paths = {route.path for route in routes}
    assert RETENTION_PATHS <= paths
    # Only one POST surface exists (retry); there is no collection POST/create.
    post_paths = {
        route.path for route in routes if "POST" in getattr(route, "methods", set())
    }
    assert post_paths == {"/retention-jobs/{job_id}/retry"}


def test_non_admin_cannot_read_or_retry_retention_jobs():
    non_admin = _user(is_admin=False)
    app = MagicMock()
    job_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()

    for call in (
        lambda: participants.get_retention_job_status(job_id, non_admin, app),
        lambda: participants.list_enrollment_retention_jobs(enrollment_id, non_admin, app),
        lambda: participants.retry_retention_job(job_id, non_admin, app),
        lambda: participants.read_deletion_ledger(enrollment_id, non_admin, app),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 403
        assert error.value.detail == "Admin privileges required"


def test_admin_can_list_and_retry_retention_jobs():
    admin = _user(is_admin=True)
    app = MagicMock()
    db = app.get_db_session.return_value
    enrollment_id = uuid.uuid4()
    job = _retryable_job()

    with patch(
        "backend.routers.research.participants.ret_api.list_retention_jobs_for_enrollment",
        return_value=[],
    ) as list_jobs:
        response = participants.list_enrollment_retention_jobs(
            enrollment_id, admin, app
        )
    assert response.status_code == 200
    assert json.loads(response.body)["jobs"] == []
    list_jobs.assert_called_once_with(db, enrollment_id)

    with patch(
        "backend.routers.research.participants.ret_api.get_retention_job",
        return_value=object(),
    ), patch(
        "backend.routers.research.participants.ret_api.row_to_retention_job",
        return_value=job,
    ), patch(
        "backend.routers.research.participants.ret_api.run_retention_job",
        return_value=job,
    ) as run_job:
        retried = participants.retry_retention_job(job.job_id, admin, app)
    assert retried.status_code == 200
    run_job.assert_called_once()
    assert str(job.job_id) in json.dumps(json.loads(retried.body))
