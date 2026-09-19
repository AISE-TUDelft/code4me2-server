"""Typed profile selection and active-study lock API contracts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.agent.profiles import (
    AgentProfilePayload,
    create_agent_profile as create_agent_profile_endpoint,
    update_agent_profile,
)
from backend.routers.analytics.auth_utils import AuthenticatedUser
from database import crud
from research.study.agents.distributions import ProfileConfigurationError
from backend.routers.research.studies import StudyCreateRequest, create_study


def _researcher() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(),
        is_admin=False,
        can_research=True,
        email="profile-owner@example.com",
        name="Profile Owner",
    )


def test_profile_update_returns_profile_locked_error():
    app = MagicMock()
    owner = _researcher()
    profile_id = uuid.uuid4()
    payload = AgentProfilePayload(
        name="locked",
        model="model",
        connection_id=uuid.uuid4(),
        approval_policy="auto",
        max_steps=1,
    )
    existing = SimpleNamespace(profile_id=profile_id, owner_user_id=owner.user_id)
    with patch("backend.routers.agent.profiles.crud.get_agent_profile_by_id", return_value=existing), patch(
        "backend.routers.agent.profiles._authorize_connection"
    ), patch(
        "backend.routers.agent.profiles.crud.update_agent_profile",
        side_effect=crud.ProfileLockedError("profile is locked"),
    ):
        with pytest.raises(HTTPException) as error:
            update_agent_profile(profile_id, payload, owner, app)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "PROFILE_LOCKED"


def test_profile_create_returns_typed_configuration_error():
    """A profile↔release mismatch is a typed 422, not a stored profile."""
    app = MagicMock()
    owner = _researcher()
    payload = AgentProfilePayload(
        name="mismatched",
        model="model",
        framework_version="code4me2-agent",
        connection_id=uuid.uuid4(),
        release_id="rel-1",
        approval_policy="auto",
        max_steps=1,
    )
    with patch("backend.routers.agent.profiles._authorize_connection"), patch(
        "backend.routers.agent.profiles.crud.create_agent_profile",
        side_effect=ProfileConfigurationError(
            "FRAMEWORK_DISTRIBUTION_MISMATCH",
            "framework 'code4me2-agent' cannot execute a BYOA_EXTERNAL release",
        ),
    ):
        with pytest.raises(HTTPException) as error:
            create_agent_profile_endpoint(payload, owner, app)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "FRAMEWORK_DISTRIBUTION_MISMATCH"


def test_study_create_maps_a_typed_configuration_error():
    app = MagicMock()
    owner = _researcher()
    payload = StudyCreateRequest(
        name="unexecutable combination",
        session_policy={
            "idle_timeout_seconds": 600,
            "resume_grace_seconds": 120,
            "heartbeat_seconds": 30,
        },
        profile_ids=[uuid.uuid4()],
    )
    with patch(
        "backend.routers.research.studies.allocate_join_code",
        return_value="ABCD1234",
    ), patch(
        "backend.routers.research.studies.store.create_study",
        side_effect=ProfileConfigurationError(
            "FRAMEWORK_DISTRIBUTION_MISMATCH",
            "framework 'goose' cannot execute a PACKAGED release",
        ),
    ):
        with pytest.raises(HTTPException) as error:
            create_study(payload, owner, app)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "FRAMEWORK_DISTRIBUTION_MISMATCH"


def test_study_create_returns_profile_not_allowed_error():
    app = MagicMock()
    owner = _researcher()
    payload = StudyCreateRequest(
        name="unauthorized profile study",
        session_policy={
            "idle_timeout_seconds": 600,
            "resume_grace_seconds": 120,
            "heartbeat_seconds": 30,
        },
        profile_ids=[uuid.uuid4()],
    )
    with patch(
        "backend.routers.research.studies.store.create_study",
        side_effect=PermissionError("selected agent profile is not owned by the researcher"),
    ):
        with pytest.raises(HTTPException) as error:
            create_study(payload, owner, app)
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "PROFILE_NOT_ALLOWED"
