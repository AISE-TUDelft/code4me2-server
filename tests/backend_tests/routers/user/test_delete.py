import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    DeleteUserDeleteResponse,
    InvalidOrExpiredAuthToken,
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
        fake_user_id = uuid.uuid4()

        mock_redis_manager = MagicMock()
        mock_redis_manager.get_user_id_by_auth_token.return_value = fake_user_id

        mock_db = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db

        with (
            patch("backend.routers.user.delete.crud.remove_user_by_id") as mock_remove,
            patch(
                "backend.routers.user.delete.crud.remove_session_by_user_id"
            ) as mock_remove_session,
            patch(
                "backend.routers.user.delete.crud.remove_query_by_user_id"
            ) as mock_remove_query,
        ):
            response = client.delete("/api/user/delete", params={"delete_data": True})

        assert response.status_code == 200
        assert response.json() == DeleteUserDeleteResponse()
        mock_remove.assert_called_once_with(db=mock_db, user_id=fake_user_id)
        mock_remove_session.assert_called_once_with(db=mock_db, user_id=fake_user_id)
        mock_remove_query.assert_called_once_with(db=mock_db, user_id=fake_user_id)
        mock_redis_manager.delete_user_sessions.assert_called_once_with(
            db=mock_db, user_id=fake_user_id
        )
        mock_redis_manager.delete_user_auths.assert_called_once_with(
            user_id=fake_user_id
        )

    def test_delete_user_invalid_auth_token(self, client: TestClient):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get_user_id_by_auth_token.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.delete("/api/user/delete")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_delete_user_no_cookie_provided(self, client: TestClient):
        response = client.delete("/api/user/delete")
        assert (
            response.status_code == 401
        )  # FastAPI validation error for missing cookie
