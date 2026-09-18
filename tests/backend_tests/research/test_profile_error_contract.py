"""Typed profile selection and active-study lock API contracts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.routers.agent.profiles import AgentProfilePayload, update_agent_profile
from backend.routers.analytics.auth_utils import AuthenticatedUser
from database import crud
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


def test_study_create_returns_profile_not_allowed_error():
    app = MagicMock()
    owner = _researcher()
    payload = StudyCreateRequest(
        name="unauthorized profile study",
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
