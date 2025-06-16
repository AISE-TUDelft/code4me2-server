import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    DeactivateSessionError,
    DeactivateSessionPostResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
)
from main import app


class TestDeactivateSession:
    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.cookies.set("auth_token", "valid_auth")
            client.mock_app = setup_app
            yield client

    def test_deactivate_session_success(self, client):
        auth_token = str(uuid.uuid4())
        session_token = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda namespace, key: (
            {"user_id": user_id, "session_token": session_token}
            if namespace == "auth_token"
            else (
                {"data": "something"}
                if namespace == "session_token"
                else (
                    {"session_token": session_token}
                    if namespace == "user_token" and key == user_id
                    else None
                )
            )
        )

        mock_redis_manager.delete.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put("/api/session/deactivate")

        assert response.status_code == 200
        assert response.json() == DeactivateSessionPostResponse()

    def test_deactivate_session_invalid_auth_token(self, client):
        auth_token = "invalid_auth"

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None  # No user_id found

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put("/api/session/deactivate")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_deactivate_session_invalid_session_token(self, client):
        auth_token = str(uuid.uuid4())
        session_token = str(uuid.uuid4())
        user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda namespace, key: (
            {"user_id": user_id, "session_token": session_token}
            if namespace == "auth_token"
            else None  # No session info found
        )

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put("/api/session/deactivate")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_deactivate_session_internal_error(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = Exception("Unexpected error")

        mock_db_session = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db_session

        response = client.put("/api/session/deactivate")

        assert response.status_code == 500
        assert response.json() == DeactivateSessionError()
        mock_db_session.rollback.assert_called_once()
