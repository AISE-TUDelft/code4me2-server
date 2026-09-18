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


def test_acp_chat_completions_gates_lifecycle_before_provider_resolution():
    """A stopped study is refused before any provider/connection resolution."""
    import asyncio

    from backend.routers import acp as acp_router
    from backend.routers.research import access

    study_id = uuid.uuid4()
    assignment = MagicMock(study_id=study_id)
    app = MagicMock()
    scope = MagicMock(user_id=str(uuid.uuid4()))
    with patch.object(
        acp_router.registry, "resolve_assignment_context", return_value=assignment
    ), patch.object(
        acp_router.operations_store, "db_kill_switch_check", return_value=None
    ), patch.object(
        acp_router.access,
        "require_live_enrollment",
        side_effect=access.FundedAccessRefused("STUDY_STOPPED", "the study has stopped"),
    ), patch.object(
        acp_router.provider_module, "funding_owner_for_profile"
    ) as funding:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                acp_router.acp_chat_completions({"messages": []}, scope=scope, app=app)
            )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "STUDY_STOPPED"
    funding.assert_not_called()


def test_kill_switch_release_refuses_to_touch_a_stopped_study():
    from backend.routers.research import operations

    stopped = MagicMock(research_status="STUDY_STOPPED")
    switch = MagicMock()
    switch.scope = MagicMock(
        kind=operations.KillSwitchScopeKind.STUDY, scope_id=uuid.uuid4()
    )
    app = MagicMock()
    with patch.object(
        operations.operations_store, "get_kill_switch", return_value=switch
    ), patch(
        "research.study.protocol.store.get_study", return_value=stopped
    ), patch.object(
        operations.operations_store, "release_kill_switch"
    ) as release:
        with pytest.raises(HTTPException) as error:
            operations.release_kill_switch_endpoint(
                switch_id=uuid.uuid4(), current_user=MagicMock(), app=app
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "STUDY_STOPPED"
    release.assert_not_called()


def test_kill_switch_release_still_works_for_a_live_study():
    from backend.routers.research import operations

    live = MagicMock(research_status="ACTIVE")
    switch = MagicMock()
    switch.scope = MagicMock(
        kind=operations.KillSwitchScopeKind.STUDY, scope_id=uuid.uuid4()
    )
    released = MagicMock(switch_id=switch.switch_id, released_at=None)
    app = MagicMock()
    with patch.object(
        operations.operations_store, "get_kill_switch", return_value=switch
    ), patch(
        "research.study.protocol.store.get_study", return_value=live
    ), patch.object(
        operations.operations_store, "release_kill_switch", return_value=released
    ) as release:
        response = operations.release_kill_switch_endpoint(
            switch_id=switch.switch_id, current_user=MagicMock(), app=app
        )

    assert response.status_code == 200
    release.assert_called_once()


def test_acp_funded_gate_scopes_the_kill_switch_to_the_accounts_enrollment():
    """An enrollment-scoped kill switch must block ACP chat completions too."""
    from backend.routers import acp as acp_router
    from backend.routers.research import access
    from research.participants import identity as identity_store

    account_id = uuid.uuid4()
    study_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()
    participant = MagicMock(participant_id=uuid.uuid4())
    active = MagicMock(
        enrollment_id=enrollment_id, study_id=study_id, status="ACTIVE"
    )
    with patch.object(
        identity_store, "get_participant_by_account", return_value=participant
    ), patch.object(
        identity_store, "list_enrollments", return_value=[active]
    ), patch.object(
        acp_router.operations_store, "db_kill_switch_check", return_value=lambda: True
    ) as kill_switch, patch.object(
        acp_router.access,
        "require_live_enrollment",
        side_effect=access.FundedAccessRefused("KILL_SWITCH_ENGAGED", "engaged"),
    ):
        with pytest.raises(HTTPException) as error:
            acp_router._require_funded_access(
                MagicMock(), account_id=account_id, study_id=study_id
            )

    assert error.value.detail["code"] == "KILL_SWITCH_ENGAGED"
    assert kill_switch.call_args.kwargs["study_id"] == study_id
    assert kill_switch.call_args.kwargs["enrollment_id"] == enrollment_id


def test_funded_task_gate_passes_the_tasks_enrollment_scope():
    from backend.routers import acp as acp_router
    from backend.routers.research import access

    study_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()
    task = MagicMock(
        study_id=study_id, enrollment_id=enrollment_id, owner_user_id=uuid.uuid4()
    )
    with patch.object(
        acp_router.operations_store, "db_kill_switch_check", return_value=lambda: False
    ) as kill_switch, patch.object(
        acp_router.access, "require_live_enrollment", return_value=MagicMock()
    ):
        acp_router._require_funded_task(MagicMock(), task)

    assert kill_switch.call_args.kwargs["enrollment_id"] == enrollment_id


def test_session_creation_refuses_a_study_outside_its_window():
    """An ended (past ends_at) study must not open new sessions."""
    from datetime import datetime, timedelta, timezone

    from backend.routers.research import sessions as sessions_router
    from research.study.protocol import store as protocol_store

    now = datetime.now(timezone.utc)
    study = MagicMock(
        study_id=uuid.uuid4(),
        is_research=True,
        is_active=True,
        research_status="ACTIVE",
        starts_at=now - timedelta(days=2),
        ends_at=now - timedelta(days=1),
    )
    enrollment = MagicMock(
        enrollment_id=uuid.uuid4(),
        study_id=study.study_id,
        status="ACTIVE",
    )
    app = MagicMock()
    app.get_db_session.return_value.get.return_value = study
    payload = MagicMock(
        enrollment_id=enrollment.enrollment_id,
        study_id=study.study_id,
        capability=MagicMock(),
        context_id="ctx-window",
        manifest_digest="digest",
        environment_ref=None,
    )

    with patch.object(
        sessions_router, "_load_enrollment", return_value=enrollment
    ), patch.object(sessions_router, "_authorize"), patch.object(
        sessions_router, "session_policy_from_study",
        return_value=MagicMock(heartbeat_seconds=30),
    ), patch.object(
        sessions_router, "open_session"
    ) as open_session:
        with pytest.raises(HTTPException) as error:
            sessions_router.create_research_session(payload, app=app)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "STUDY_NOT_OPEN"
    open_session.assert_not_called()
    # The window check itself is the protocol store's authority.
    assert protocol_store.research_study_is_open(study, now) is False
