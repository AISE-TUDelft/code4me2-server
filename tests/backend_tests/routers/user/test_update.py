from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    InvalidOrExpiredAuthToken,
    InvalidPreviousPassword,
    UpdateUserError,
    UpdateUserPutResponse,
    UserAlreadyExistsWithThisEmail,
)
from main import app
from response_models import ResponseUser


class TestUpdateUser:

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

    @pytest.fixture(scope="function")
    def update_user_query(self):
        return Queries.UpdateUser.fake()

    def test_update_user_success(
        self, client: TestClient, update_user_query: Queries.UpdateUser
    ):
        # Fake data
        fake_updated_user = ResponseUser.fake()

        # Mock dependencies
        mock_crud = MagicMock()
        mock_crud.update_user.return_value = fake_updated_user
        mock_crud.get_user_by_email.return_value = None

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": fake_updated_user.user_id}

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        with patch("backend.routers.user.update.crud", mock_crud):
            response = client.put("/api/user/update", json=update_user_query.dict())

        response_result = response.json()
        assert response.status_code == 201
        response_result["user"][
            "password"
        ] = fake_updated_user.password.get_secret_value()
        assert response_result == UpdateUserPutResponse(user=fake_updated_user)

    def test_update_user_invalid_session_token(self, client, update_user_query):
        # Mock session manager returns None
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.put("/api/user/update", json=update_user_query.dict())

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_update_user_validation_error(self, client):
        # Send payload missing required fields
        invalid_payload = {
            "name": ""
        }  # Likely to fail validation if fields are required
        response = client.put("/api/user/update", json=invalid_payload)
        assert response.status_code == 422

    def test_update_user_email_conflict(self, client, update_user_query):
        # Mock dependencies
        mock_crud = MagicMock()
        # Simulate finding a user with the same email but different ID
        existing_user = MagicMock()
        existing_user.user_id = (
            "different-user-id"  # Different from the authenticated user
        )
        mock_crud.get_user_by_email.return_value = existing_user

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": "authenticated-user-id"}

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        with patch("backend.routers.user.update.crud", mock_crud):
            response = client.put("/api/user/update", json=update_user_query.dict())

        assert response.status_code == 409
        assert response.json() == UserAlreadyExistsWithThisEmail().model_dump()

    def test_update_user_invalid_previous_password(self, client, update_user_query):
        # Mock dependencies
        mock_crud = MagicMock()
        # Simulate password validation failure
        mock_crud.get_user_by_email.return_value = None
        mock_crud.get_user_by_id_password.return_value = None

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": "user-id"}

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        with patch("backend.routers.user.update.crud", mock_crud):
            response = client.put("/api/user/update", json=update_user_query.dict())

        assert response.status_code == 401
        assert response.json() == InvalidPreviousPassword().model_dump()

    def test_update_user_server_error(self, client, update_user_query):
        # Create a query without email to avoid the email conflict check
        update_user_query.email = None

        # Mock dependencies
        mock_crud = MagicMock()
        # Simulate server error by raising an exception
        mock_crud.update_user.side_effect = Exception("Database error")
        # Make sure get_user_by_email returns None to avoid email conflict
        mock_crud.get_user_by_email.return_value = None

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = {"user_id": "user-id"}

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        with patch("backend.routers.user.update.crud", mock_crud):
            response = client.put("/api/user/update", json=update_user_query.dict())

        assert response.status_code == 500
        assert response.json() == UpdateUserError().model_dump()
