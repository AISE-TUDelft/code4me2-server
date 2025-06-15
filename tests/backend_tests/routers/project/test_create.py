import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    CreateProjectError,
    CreateProjectPostResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
)
from main import app


class TestCreateProject:

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
    def fake_create_project(self):
        # Assuming your Queries.CreateProject has a .fake() method to generate dummy data
        return Queries.CreateProject.fake()

    @pytest.fixture
    def fake_session_token(self):
        return str(uuid.uuid4())

    @pytest.fixture
    def fake_user_id(self):
        return str(uuid.uuid4())

    @pytest.fixture
    def fake_project_id(self):
        return uuid.uuid4()

    def test_create_project_success(
        self,
        client,
        fake_create_project,
        fake_session_token,
        fake_user_id,
        fake_project_id,
    ):
        # Setup Redis and DB mocks
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda key, token: {
            ("user_token", fake_user_id): {"session_token": fake_session_token},
            ("auth_token", "valid_token"): {
                "user_id": fake_user_id,
            },
            ("session_token", fake_session_token): {"project_tokens": []},
        }.get((key, token))
        mock_redis.set = MagicMock()

        mock_db_session = MagicMock()
        mock_config = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        mock_project = MagicMock()
        mock_project.project_id = fake_project_id

        with patch(
            "backend.routers.session.acquire.crud.create_project",
            return_value=mock_project,
        ), patch("backend.routers.session.acquire.crud.create_session_project"):
            response = client.post(
                "/api/project/create/", json=fake_create_project.model_dump()
            )

        assert response.status_code == 201
        assert response.cookies.get("project_token") == str(fake_project_id)
        assert response.json() == CreateProjectPostResponse(
            project_token=str(fake_project_id)
        )

    def test_create_project_invalid_auth_token(self, client, fake_create_project):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        client.mock_app.get_redis_manager.return_value = mock_redis

        response = client.post(
            "/api/project/create/", json=fake_create_project.model_dump()
        )
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_create_project_invalid_session_token(
        self, client, fake_create_project, fake_session_token, fake_user_id
    ):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda key, token: {
            ("auth_token", "valid_token"): {
                "session_token": fake_session_token,
                "user_id": fake_user_id,
            },
            ("session_token", fake_session_token): None,
        }.get((key, token))
        client.mock_app.get_redis_manager.return_value = mock_redis

        response = client.post(
            "/api/project/create/", json=fake_create_project.model_dump()
        )
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_create_project_internal_error(self, client, fake_create_project):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis crash")
        client.mock_app.get_redis_manager.return_value = mock_redis
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.post(
            "/api/project/create/", json=fake_create_project.model_dump()
        )
        assert response.status_code == 500
        assert response.json() == CreateProjectError()
