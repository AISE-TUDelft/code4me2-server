from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from App import App
from backend.main import app
from backend.models.Responses import (
    DeleteUserDeleteResponse,
    InvalidSessionToken,
)


class TestDeleteUser:

    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.mock_app = setup_app
            client.cookies.set("session_token", "valid_token")
            yield client

    def test_delete_user_success(self, client):
        fake_user_id = "user-123"

        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = {"user_id": fake_user_id}

        mock_db = MagicMock()

        client.mock_app.get_session_manager.return_value = mock_session_manager
        client.mock_app.get_db_session.return_value = mock_db
        with patch("backend.routers.user.delete.crud.remove_user_by_id") as mock_remove:
            response = client.delete("/api/user/delete")

        assert response.status_code == 200
        assert response.json() == DeleteUserDeleteResponse()
        mock_remove.assert_called_once_with(db=mock_db, user_id=fake_user_id)
        mock_session_manager.delete_user_sessions.assert_called_once_with(
            user_id=fake_user_id
        )

    def test_delete_user_invalid_session_token(self, client):
        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session_manager

        response = client.delete("/api/user/delete")

        assert response.status_code == 401
        assert response.json() == InvalidSessionToken()

    def test_delete_user_no_cookie_provided(self, client):
        response = client.delete("/api/user/delete")
        assert (
            response.status_code == 401
        )  # FastAPI validation error for missing cookie
