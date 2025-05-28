from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    CreateSessionError,
    CreateSessionPostResponse,
    InvalidOrExpiredAuthToken,
)
from main import app


class TestCreateSession:

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

    def test_create_session_success(self, client):
        fake_user_id = "123"
        fake_session_id = "abc-session"

        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = fake_user_id
        mock_session_manager.update_session.return_value = None

        mock_config = MagicMock()
        mock_config.session_token_expires_in_seconds = 3600

        mock_db_session = MagicMock()

        mock_created_session = MagicMock()
        mock_created_session.session_id = fake_session_id

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.session.create.crud.create_session",
            return_value=mock_created_session,
        ):
            response = client.post("/api/session/create")  # adjust URL as needed

        assert response.status_code == 201
        assert response.cookies.get("session_token") == fake_session_id
        assert response.json() == CreateSessionPostResponse()

    def test_create_session_invalid_token(self, client):
        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session_manager

        response = client.post("/api/session/create")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_create_session_internal_error(self, client):
        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.side_effect = Exception(
            "DB failure"
        )

        client.mock_app.get_session_manager.return_value = mock_session_manager

        response = client.post("/api/session/create")

        assert response.status_code == 500
        assert response.json() == CreateSessionError()
