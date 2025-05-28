from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    ActivateSessionError,
    ActivateSessionPostResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
)
from main import app


class TestActivateSession:

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

    @pytest.fixture(scope="function")
    def activate_session_query(self):
        return Queries.ActivateSession.fake()

    def test_activate_session_in_redis(self, client, activate_session_query):
        fake_user_id = "user123"
        session_token = activate_session_query.session_token
        fake_session_info = {
            "user_id": fake_user_id,
            "data": {"context": {}, "context_changes": {}},
        }

        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = fake_user_id
        mock_session_manager.get_session.return_value = fake_session_info

        mock_config = MagicMock()
        mock_config.session_token_expires_in_seconds = 3600

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put(
            "/api/session/activate",
            json=activate_session_query.dict(),
        )

        assert response.status_code == 200
        assert response.cookies.get("session_token") == session_token
        assert response.json() == ActivateSessionPostResponse()

    def test_activate_session_fallback_to_db(self, client, activate_session_query):
        fake_user_id = "user123"
        session_token = activate_session_query.session_token
        session_model = MagicMock()
        session_model.user_id = fake_user_id
        session_model.multi_file_contexts = "{}"
        session_model.multi_file_context_changes = "{}"

        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = fake_user_id
        mock_session_manager.get_session.return_value = None

        mock_config = MagicMock()
        mock_config.session_token_expires_in_seconds = 3600

        mock_db_session = MagicMock()

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.session.activate.crud.get_session_by_id",
            return_value=session_model,
        ):
            response = client.put(
                "/api/session/activate",
                json=activate_session_query.dict(),
            )

        assert response.status_code == 200
        assert response.cookies.get("session_token") == session_token
        assert response.json() == ActivateSessionPostResponse()

    def test_activate_session_invalid_auth_token(self, client, activate_session_query):
        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session_manager

        response = client.put(
            "/api/session/activate",
            json=activate_session_query.dict(),
        )

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_activate_session_invalid_session_token(
        self, client, activate_session_query
    ):
        fake_user_id = "user123"

        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.return_value = fake_user_id
        mock_session_manager.get_session.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_config.return_value = MagicMock()
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch(
            "backend.routers.session.activate.crud.get_session_by_id", return_value=None
        ):
            response = client.put(
                "/api/session/activate",
                json=activate_session_query.dict(),
            )

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_activate_session_internal_error(self, client, activate_session_query):
        mock_session_manager = MagicMock()
        mock_session_manager.get_user_id_by_auth_token.side_effect = Exception(
            "Unexpected error"
        )

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.put(
            "/api/session/activate",
            json=activate_session_query.dict(),
        )

        assert response.status_code == 500
        assert response.json() == ActivateSessionError()
