"""Focused backend tests for the revision-free study lifecycle routes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.studies import (
    RevokeEnrollmentRequest,
    StopStudyRequest,
    StudyCreateRequest,
    StudyMetadataUpdateRequest,
    clone_study,
    create_study as create_study_endpoint,
    get_study,
    list_studies,
    revoke_enrollment,
    stop_study,
    update_study_metadata,
)
from backend.routers.research.join import JoinRequestBody, redeem_join_code
from backend.routers.research.access import FundedAccessRefused, require_live_enrollment
from research.participants.enums import EnrollmentStatus
from research.study.lifecycle import EnrollmentRevokeSummary, StudyEnrollmentSummary, StudyStopSummary


def test_study_router_exposes_lifecycle_routes_not_revision_routes():
    from backend.routers.research import studies as studies_router

    paths = {route.path for route in studies_router.router.routes}
    assert "/{study_id}/stop" in paths
    assert "/{study_id}/metadata" in paths
    assert "/{study_id}/clone" in paths
    assert "/{study_id}/enrollments/{enrollment_id}/revoke" in paths
    assert "/revisions" not in paths
    assert "/revisions/{revision_id}/supersede" not in paths


def _researcher() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=False,
        can_research=True,
        email="owner@example.com",
        name="Owner",
    )


def test_stop_study_route_returns_terminal_non_destructive_summary():
    app = MagicMock()
    study_id = uuid.uuid4()
    stopped_at = datetime.now(timezone.utc)
    updated = SimpleNamespace(
        study_id=study_id,
        name="Study",
        description="Description",
        owner=None,
        created_by=_researcher().user_id,
        is_research=True,
        is_active=False,
        research_status="STUDY_STOPPED",
        research_config_digest="digest",
        join_code="JOIN-1234",
        consent_locked_at=None,
        stopped_at=stopped_at,
        stopped_by="owner@example.com",
        starts_at=None,
        ends_at=None,
        created_at=stopped_at,
    )
    summary = StudyStopSummary(
        study_id=study_id,
        enrollment_count=2,
        assignment_count=2,
        session_count=1,
    )

    with patch(
        "backend.routers.research.studies._authorize_study",
        return_value=updated,
    ), patch(
        "backend.routers.research.studies.stop_research_study",
        return_value=summary,
    ) as stop, patch(
        "backend.routers.research.studies.store.get_study",
        return_value=updated,
    ):
        response = stop_study(
            study_id,
            StopStudyRequest(),
            _researcher(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["stopped"] is True
    assert body["study"]["research_status"] == "STUDY_STOPPED"
    assert body["enrollment_count"] == 2
    assert body["assignment_count"] == 2
    assert body["session_count"] == 1
    stop.assert_called_once_with(app.get_db_session.return_value, study_id, actor="owner@example.com")
    app.get_db_session.return_value.close.assert_called_once()


def test_create_study_starts_draft_with_study_owned_join_code_and_config():
    app = MagicMock()
    owner = _researcher()
    created = SimpleNamespace(
        study_id=uuid.uuid4(),
        name="New study",
        description="Description",
        owner=None,
        created_by=owner.user_id,
        is_research=True,
        is_active=False,
        research_status="DRAFT",
        research_config_digest=None,
        join_code="ABCD1234",
        consent_locked_at=None,
        stopped_at=None,
        stopped_by=None,
        starts_at=None,
        ends_at=None,
        created_at=datetime.now(timezone.utc),
    )
    payload = StudyCreateRequest(
        name="New study",
        description="Description",
        telemetry_policy={"metadata_only": True},
        profile_ids=[profile_id := uuid.uuid4()],
    )

    with patch(
        "backend.routers.research.studies.allocate_join_code",
        return_value="ABCD1234",
    ), patch(
        "backend.routers.research.studies.store.create_study",
        return_value=created,
    ) as create:
        response = create_study_endpoint(payload, owner, app)

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["study"]["research_status"] == "DRAFT"
    assert body["study"]["join_code"] == "ABCD1234"
    assert create.call_args.kwargs["research_config_json"] == {
        "telemetry_policy": {"metadata_only": True},
        "session_policy": {},
        "profile_ids": [str(profile_id)],
    }
    assert create.call_args.kwargs["join_code"] == "ABCD1234"


def test_study_read_metadata_clone_and_revoke_routes_return_lifecycle_payloads():
    app = MagicMock()
    owner = _researcher()
    study_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()
    study = SimpleNamespace(
        study_id=study_id,
        name="Study",
        description="Description",
        owner=None,
        created_by=owner.user_id,
        is_research=True,
        is_active=False,
        research_status="STUDY_STOPPED",
        research_config_digest="digest",
        join_code="JOIN-1234",
        consent_locked_at=None,
        stopped_at=datetime.now(timezone.utc),
        stopped_by=owner.email,
        starts_at=None,
        ends_at=None,
        created_at=datetime.now(timezone.utc),
    )
    clone = SimpleNamespace(**{**study.__dict__, "study_id": uuid.uuid4(), "research_status": "DRAFT"})

    with patch("backend.routers.research.studies.store.list_studies", return_value=[study]), patch(
        "backend.routers.research.studies._authorize_study", return_value=study
    ), patch("backend.routers.research.studies.store.get_study", return_value=study), patch(
        "backend.routers.research.studies.update_research_metadata", return_value=study
    ), patch("backend.routers.research.studies.clone_stopped_research_study", return_value=clone), patch(
        "backend.routers.research.studies.revoke_research_enrollment",
        return_value=EnrollmentRevokeSummary(enrollment_id=enrollment_id, session_count=1),
    ):
        assert json.loads(list_studies(owner, app).body)["studies"][0]["study_id"] == str(study_id)
        assert json.loads(get_study(study_id, owner, app).body)["study"]["research_status"] == "STUDY_STOPPED"
        metadata_response = update_study_metadata(
            study_id,
            StudyMetadataUpdateRequest(name="Renamed"),
            owner,
            app,
        )
        assert json.loads(metadata_response.body)["study"]["study_id"] == str(study_id)
        clone_response = clone_study(study_id, owner, app)
        assert json.loads(clone_response.body)["study"]["research_status"] == "DRAFT"
        revoke_response = revoke_enrollment(
            study_id,
            enrollment_id,
            RevokeEnrollmentRequest(),
            owner,
            app,
        )
        assert json.loads(revoke_response.body)["revoked"] is True


def test_join_consent_route_returns_atomic_enrollment_assignment_result():
    app = MagicMock()
    participant = AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=False,
        can_research=False,
        email="participant@example.com",
        name="Participant",
    )
    result = StudyEnrollmentSummary(
        enrollment_id=uuid.uuid4(),
        study_id=uuid.uuid4(),
        assignment_id=uuid.uuid4(),
        agent_profile_id=uuid.uuid4(),
        created=True,
        reused=False,
    )
    with patch(
        "backend.routers.research.join.open_study_enrollment", return_value=result
    ):
        response = redeem_join_code(
            JoinRequestBody(join_code="JOIN-1234", accept_consent=True),
            participant,
            app,
        )
    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["created"] is True
    assert body["assignment_id"] == str(result.assignment_id)


def test_funded_access_rejects_stopped_study_before_provider_or_kill_switch():
    db = MagicMock()
    account_id = uuid.uuid4()
    study_id = uuid.uuid4()
    participant = SimpleNamespace(participant_id=uuid.uuid4())
    enrollment = SimpleNamespace(
        enrollment_id=uuid.uuid4(),
        participant_id=participant.participant_id,
        study_id=study_id,
        status=EnrollmentStatus.ACTIVE,
    )
    db.get.return_value = SimpleNamespace(
        is_research=True,
        research_status="STUDY_STOPPED",
    )

    with patch(
        "backend.routers.research.access.identity_store.get_participant_by_account",
        return_value=participant,
    ), patch(
        "backend.routers.research.access.identity_store.list_enrollments",
        return_value=[enrollment],
    ):
        try:
            require_live_enrollment(db, account_id=account_id, study_id=study_id)
        except FundedAccessRefused as error:
            assert error.code == "STUDY_STOPPED"
        else:
            raise AssertionError("stopped study unexpectedly passed funded access")


def test_join_resolution_rejects_stopped_study():
    from backend.routers.research.join import resolve_join_code

    app = MagicMock()
    study = SimpleNamespace(
        study_id=uuid.uuid4(),
        name="Stopped",
        description=None,
        join_code="STOPPED1",
        research_status="STUDY_STOPPED",
    )
    user = _researcher()

    with patch(
        "backend.routers.research.join._study_from_code",
        return_value=study,
    ), patch(
        "backend.routers.research.join.study_store.get_study_by_join_code",
        return_value=study,
    ):
        try:
            resolve_join_code("STOPPED1", user, app)
        except HTTPException as error:
            assert error.status_code == 409
            assert error.detail["code"] == "STUDY_STOPPED"
        else:
            raise AssertionError("stopped study unexpectedly resolved")


def test_admin_can_engage_study_scope_kill_switch():
    from backend.routers.research.operations import KillSwitchEngageRequest, engage_kill_switch
    from research.analysis.operations.enums import KillSwitchScopeKind

    app = MagicMock()
    switch_id = uuid.uuid4()
    admin = AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=True,
        can_research=True,
        email="admin@example.com",
        name="Admin",
    )
    payload = KillSwitchEngageRequest(
        scope_kind=KillSwitchScopeKind.STUDY,
        scope_id=uuid.uuid4(),
        reason="maintenance",
    )

    with patch(
        "backend.routers.research.operations.operations_store.engage_kill_switch",
        return_value=SimpleNamespace(switch_id=switch_id, reason="maintenance"),
    ):
        response = engage_kill_switch(payload, admin, app)

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["switch_id"] == str(switch_id)
    assert body["reason"] == "maintenance"


    def test_join_resolution_rejects_stopped_study():
        from backend.routers.research.join import resolve_join_code

        app = MagicMock()
        revision = SimpleNamespace(study_id=uuid.uuid4(), published_at=None)
        row = SimpleNamespace(join_code="STOPPED1")
        user = _researcher()

        with patch(
            "backend.routers.research.join._load_revision_by_code",
            return_value=(row, revision),
        ), patch(
            "backend.routers.research.join.protocol_store.get_study",
            return_value=SimpleNamespace(research_status="STUDY_STOPPED"),
        ):
            try:
                resolve_join_code("STOPPED1", user, app)
            except HTTPException as error:
                assert error.status_code == 409
                assert error.detail["code"] == "STUDY_STOPPED"
            else:
                raise AssertionError("stopped study unexpectedly resolved")
