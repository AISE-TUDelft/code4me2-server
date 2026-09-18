"""Route-level stop guards for bootstrap, sessions and telemetry."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.research import bootstrap, sessions, telemetry


def _stopped_app_and_enrollment():
    app = MagicMock()
    row = MagicMock()
    row.study_id = uuid.uuid4()
    row.enrollment_id = uuid.uuid4()
    row.participant_id = uuid.uuid4()
    row.status = "ACTIVE"
    study = MagicMock()
    study.research_status = "STUDY_STOPPED"
    app.get_db_session.return_value.get.return_value = study
    return app, row


def test_research_runtime_routes_are_registered_without_revision_inputs():
    bootstrap_paths = {route.path for route in bootstrap.router.routes}
    session_paths = {route.path for route in sessions.router.routes}
    telemetry_paths = {route.path for route in telemetry.router.routes}

    assert "/research-sessions" in bootstrap_paths
    assert "/heartbeat" in session_paths
    assert "/close" in session_paths
    assert "/batches" in telemetry_paths
    assert all("revision" not in path for path in bootstrap_paths | session_paths | telemetry_paths)


def test_bootstrap_rejects_stopped_enrollment_before_manifest_creation():
    app, row = _stopped_app_and_enrollment()
    user = MagicMock(user_id=uuid.uuid4())
    enrollment = MagicMock(enrollment_id=row.enrollment_id, participant_id=row.participant_id)
    with patch.object(bootstrap.identity_store, "get_participant_by_account", return_value=row), patch.object(
        bootstrap.identity_store, "get_enrollment", return_value=row
    ), patch.object(bootstrap.identity_store, "row_to_enrollment", return_value=enrollment):
        with pytest.raises(HTTPException) as error:
            bootstrap._owned_enrollment(app.get_db_session.return_value, user, row.enrollment_id)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "STUDY_STOPPED"


def test_sessions_reject_stopped_enrollment_before_session_activity():
    app, row = _stopped_app_and_enrollment()
    db = app.get_db_session.return_value
    with patch.object(sessions.identity_store, "get_enrollment", return_value=row):
        with pytest.raises(HTTPException) as error:
            sessions._load_enrollment(db, row.enrollment_id)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "STUDY_STOPPED"


def test_telemetry_enrollment_resolver_marks_stopped_enrollment_terminal():
    app, row = _stopped_app_and_enrollment()
    db = app.get_db_session.return_value
    with patch.object(telemetry.identity_store, "get_enrollment", return_value=row), patch.object(
        telemetry.identity_store, "row_to_enrollment", return_value=row
    ):
        resolved = telemetry._enrollment_resolver(db)(row.enrollment_id)
    assert row.status == "STUDY_STOPPED"
    assert resolved is row
