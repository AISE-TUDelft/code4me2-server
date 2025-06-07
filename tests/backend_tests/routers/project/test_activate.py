from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries  # make sure this import is here
from App import App
from backend.Responses import (
    ActivateProjectError,
    ActivateProjectPostResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
)
from main import app  # adjust if your FastAPI app entry point is elsewhere


class TestActivateProject:

    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.mock_app = setup_app
            client.cookies.set("auth_token", "valid_token")
            yield client

    @pytest.fixture
    def fake_activate_project(self):
        # Assuming Queries.ActivateProject has a .fake() method
        return Queries.ActivateProject.fake()

    @pytest.fixture
    def fake_session_token(self):
        import uuid

        return str(uuid.uuid4())

    @pytest.fixture
    def fake_user_id(self):
        import uuid

        return str(uuid.uuid4())

    @pytest.fixture
    def fake_project_token(self):
        import uuid

        return str(uuid.uuid4())

    def test_activate_project_success(
        self, client, fake_activate_project, fake_session_token, fake_user_id
    ):
        # Use the project_token from the fake input
        project_token = fake_activate_project.project_token

        # Setup redis and db mocks
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda key, token: {
            ("auth_token", "valid_token"): {
                "session_token": fake_session_token,
                "user_id": fake_user_id,
            },
            ("session_token", fake_session_token): {"project_tokens": []},
            ("project_token", project_token): None,
        }.get((key, token))

        mock_config = MagicMock()
        mock_db_session = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        mock_project = MagicMock()
        mock_project.multi_file_contexts = "{}"
        mock_project.multi_file_context_changes = "{}"

        with patch(
            "backend.routers.project.activate.crud.get_project_by_id",
            return_value=mock_project,
        ), patch("backend.routers.project.activate.crud.create_session_project"):
            response = client.put(
                "/api/project/activate", json=fake_activate_project.model_dump()
            )

        assert response.status_code == 200
        assert response.cookies.get("project_token") == project_token
        assert response.json() == ActivateProjectPostResponse()

    def test_activate_project_invalid_auth_token(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.put(
            "/api/project/activate", json={"project_token": "dummy-token"}
        )
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_activate_project_invalid_session_token(
        self, client, fake_session_token, fake_user_id
    ):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda key, token: {
            ("auth_token", "valid_token"): {
                "session_token": fake_session_token,
                "user_id": fake_user_id,
            },
            ("session_token", fake_session_token): None,
        }.get((key, token))
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.put(
            "/api/project/activate", json={"project_token": "dummy-token"}
        )
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_activate_project_invalid_project_token(
        self, client, fake_session_token, fake_user_id, fake_project_token
    ):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda key, token: {
            ("auth_token", "valid_token"): {
                "session_token": fake_session_token,
                "user_id": fake_user_id,
            },
            ("session_token", fake_session_token): {"project_tokens": []},
            ("project_token", fake_project_token): None,
        }.get((key, token))
        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch(
            "backend.routers.project.activate.crud.get_project_by_id", return_value=None
        ):
            response = client.put(
                "/api/project/activate", json={"project_token": fake_project_token}
            )

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredProjectToken()

    def test_activate_project_internal_error(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = Exception("Redis failure")
        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put(
            "/api/project/activate", json={"project_token": "dummy-token"}
        )
        assert response.status_code == 500
        assert response.json() == ActivateProjectError()
