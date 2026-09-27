import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    DeleteUserBlockedError,
    DeleteUserDeleteResponse,
    DeleteUserError,
    InvalidOrExpiredAuthToken,
    UserNotFoundError,
)
from main import app
from privacy import erasure


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

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db

        with patch("backend.routers.user.delete.crud", mock_crud), patch(
            "backend.routers.user.delete.erasure.delete_account"
        ) as delete_account:
            response = client.delete("/api/user/delete")

        assert response.status_code == 200
        assert response.json() == DeleteUserDeleteResponse()
        mock_crud.get_user_by_id.assert_called_once_with(
            mock_db, uuid.UUID(fake_user_id)
        )
        # Data erasure and the account row go together, in one committed unit.
        delete_account.assert_called_once_with(mock_db, uuid.UUID(fake_user_id))
        mock_db.commit.assert_called_once()
        mock_redis_manager.revoke_user_tokens.assert_called_once_with(fake_user_id)
        cleared = " ".join(response.headers.get_list("set-cookie"))
        for cookie in ("auth_token=", "session_token=", "project_token="):
            assert cookie in cleared

    def test_delete_user_refused_for_research_resource_owner(self, client: TestClient):
        fake_user_id = str(uuid.uuid4())

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": fake_user_id}
        mock_db = MagicMock()
        mock_crud = MagicMock()
        mock_crud.get_user_by_id.return_value = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db

        reason = "This account owns 1 research study, so it cannot delete itself."
        with patch("backend.routers.user.delete.crud", mock_crud), patch(
            "backend.routers.user.delete.erasure.delete_account",
            side_effect=erasure.AccountDeletionBlocked(reason),
        ):
            response = client.delete("/api/user/delete")

        assert response.status_code == 409
        assert response.json() == DeleteUserBlockedError(message=reason)
        mock_db.rollback.assert_called_once()
        mock_db.commit.assert_not_called()
        mock_redis_manager.revoke_user_tokens.assert_not_called()

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
        mock_db = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_db_session.return_value = mock_db

        # Simulate a server error by raising an exception
        with patch("backend.routers.user.delete.crud", mock_crud), patch(
            "backend.routers.user.delete.erasure.delete_account",
            side_effect=Exception("Database error"),
        ):
            response = client.delete("/api/user/delete")

            assert response.status_code == 500
            assert response.json() == DeleteUserError()
        mock_db.rollback.assert_called_once()
        mock_redis_manager.revoke_user_tokens.assert_not_called()
