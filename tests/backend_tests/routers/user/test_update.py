from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    InvalidOrExpiredAuthToken,
    UpdateUserPutResponse,
)
from main import app
from response_models import UserBase


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
        fake_updated_user = UserBase.fake()

        # Mock dependencies
        mock_crud = MagicMock()
        mock_crud.update_user.return_value = fake_updated_user

        mock_redis_manager = MagicMock()
        mock_redis_manager.get_user_id_by_auth_token.return_value = (
            fake_updated_user.user_id
        )

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        with patch("backend.routers.user.update.crud", mock_crud):
            response = client.put("/api/user/update", json=update_user_query.dict())

        response_result = response.json()
        response_result["user"][
            "password"
        ] = fake_updated_user.password.get_secret_value()
        assert response.status_code == 201
        assert response_result == UpdateUserPutResponse(user=fake_updated_user)

    def test_update_user_invalid_session_token(self, client, update_user_query):
        # Mock session manager returns None
        mock_redis_manager = MagicMock()
        mock_redis_manager.get_user_id_by_auth_token.return_value = None

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
