from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    ConfigNotFound,
    GetUserError,
    GetUserGetResponse,
    InvalidOrExpiredAuthToken,
    UserNotFoundError,
)
from main import app
from response_models import ResponseUser


@pytest.fixture(scope="function")
def setup_app():
    mock_app = MagicMock()
    app.dependency_overrides[App.get_instance] = lambda: mock_app
    return mock_app


@pytest.fixture(scope="function")
def client(setup_app):
    with TestClient(app) as client:
        client.mock_app = setup_app
        client.cookies.set("auth_token", "test_auth_token")
        yield client


class TestGetUserFromAuthToken:

    def test_invalid_token(self, client):
        client.mock_app.get_redis_manager.return_value.get.return_value = None
        response = client.get("/api/user/get")
        assert response.status_code == 401
        assert InvalidOrExpiredAuthToken().message in response.text

    def test_missing_user_id_in_token(self, client):
        client.mock_app.get_redis_manager.return_value.get.return_value = {}
        response = client.get("/api/user/get")
        assert response.status_code == 401
        assert InvalidOrExpiredAuthToken().message in response.text

    def test_user_not_found(self, client):
        user_id = str(uuid4())
        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": user_id
        }
        client.mock_app.get_db_session.return_value = MagicMock()
        with patch("database.crud.get_user_by_id", return_value=None):
            response = client.get("/api/user/get")
            assert response.status_code == 404
            assert UserNotFoundError().message in response.text

    def test_config_not_found(self, client):
        user_id = str(uuid4())
        mock_user = MagicMock()
        mock_user.user_id = user_id
        mock_user.config_id = 42

        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": user_id
        }
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=mock_user):
            with patch("database.crud.get_config_by_id", return_value=None):
                response = client.get("/api/user/get")
                assert response.status_code == 404
                assert ConfigNotFound().message in response.text

    def test_get_user_success(self, client):
        # Generate a valid fake user
        fake_user = ResponseUser.fake()
        user_id = str(fake_user.user_id)

        # Create a mock config object
        config_data = MagicMock()
        config_data.config_data = '{"theme": "dark"}'

        # Patch Redis and DB session
        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": user_id
        }
        client.mock_app.get_db_session.return_value = MagicMock()

        # Patch DB calls
        with patch(
            "backend.routers.user.get.crud.get_user_by_id", return_value=fake_user
        ):
            with patch(
                "backend.routers.user.get.crud.get_config_by_id",
                return_value=config_data,
            ):
                response = client.get("/api/user/get")

        # Check response
        assert response.status_code == 200
        response_result = response.json()

        # Replace password field with its value for equality check
        response_result["user"]["password"] = fake_user.password.get_secret_value()

        assert response_result == GetUserGetResponse(
            user=fake_user,
            config=config_data.config_data,
        )

    def test_get_user_exception(self, client):
        client.mock_app.get_redis_manager.return_value.get.side_effect = Exception(
            "Redis down"
        )
        response = client.get("/api/user/get")
        assert response.status_code == 500
        assert GetUserError().message in response.text
