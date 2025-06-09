import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    DeleteUserDeleteResponse,
    DeleteUserError,
    InvalidOrExpiredAuthToken,
    UserNotFoundError,
)
from main import app


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
            client.cookies.set("auth_token", "valid_token")
            yield client

    def test_delete_user_success(self, client: TestClient):
        fake_user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": fake_user_id}

        mock_db = MagicMock()
        mock_user = MagicMock()

        mock_crud = MagicMock()
        mock_crud.get_user_by_id.return_value = mock_user
        mock_crud.delete_user_by_id.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db

        with patch("backend.routers.user.delete.crud", mock_crud):
            response = client.delete("/api/user/delete", params={"delete_data": True})

        assert response.status_code == 200
        assert response.json() == DeleteUserDeleteResponse()
        mock_crud.get_user_by_id.assert_called_once_with(
            mock_db, uuid.UUID(fake_user_id)
        )
        mock_crud.delete_user_by_id.assert_called_once_with(
            db=mock_db, user_id=fake_user_id
        )
        mock_redis_manager.delete.assert_called_once_with(
            "auth_token", "valid_token", mock_db
        )

    def test_delete_user_invalid_auth_token(self, client: TestClient):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.delete("/api/user/delete")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_delete_user_no_cookie_provided(self, client: TestClient):
        response = client.delete("/api/user/delete")
        assert (
            response.status_code == 401
        )  # FastAPI validation error for missing cookie

    def test_delete_user_not_found(self, client: TestClient):
        fake_user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": fake_user_id}

        mock_crud = MagicMock()
        # Simulate user not found
        mock_crud.get_user_by_id.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("backend.routers.user.delete.crud", mock_crud):
            response = client.delete("/api/user/delete")

            assert response.status_code == 404
            assert response.json() == UserNotFoundError()

    def test_delete_user_server_error(self, client: TestClient):
        fake_user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": fake_user_id}

        mock_crud = MagicMock()
        # Simulate a found user
        mock_crud.get_user_by_id.return_value = MagicMock()
        # Simulate a server error by raising an exception
        mock_crud.delete_user_by_id.side_effect = Exception("Database error")

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("backend.routers.user.delete.crud", mock_crud):
            response = client.delete("/api/user/delete")

            assert response.status_code == 500
            assert response.json() == DeleteUserError()
