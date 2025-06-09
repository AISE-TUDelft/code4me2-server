from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    AcquireSessionError,
    AcquireSessionGetResponse,
    InvalidOrExpiredAuthToken,
)
from main import app


class TestAcquireSession:

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

    def test_acquire_session_success(self, client):
        fake_user_id = "123e4567-e89b-12d3-a456-426614174000"
        fake_session_token = "abc-session"

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda key, value: (
            {"user_id": fake_user_id, "session_token": None}
            if key == "auth_token"
            else None
        )

        mock_redis_manager.set.return_value = None

        mock_config = MagicMock()
        mock_config.session_token_expires_in_seconds = 3600

        mock_db_session = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.session.acquire.crud.create_session"
        ) as create_mock, patch(
            "backend.routers.session.acquire.create_uuid",
            return_value=fake_session_token,
        ):
            response = client.get("/api/session/acquire")  # adjust URL as needed

        assert response.status_code == 200
        assert response.cookies.get("session_token") == fake_session_token
        assert response.json() == AcquireSessionGetResponse(
            session_token=fake_session_token
        )

    def test_acquire_session_invalid_token(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.get("/api/session/acquire")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_acquire_session_internal_error(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = Exception("Redis failure")

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.get("/api/session/acquire")

        assert response.status_code == 500
        assert response.json() == AcquireSessionError()
