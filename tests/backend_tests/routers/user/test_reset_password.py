from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    ErrorShowingPasswordResetForm,
    InvalidOrExpiredResetToken,
    PasswordResetGetHTMLResponse,
    PasswordResetRequestPostResponse,
    UserNotFoundError,
)
from main import app


@pytest.fixture(scope="function")
def setup_app():
    mock_app = MagicMock()
    app.dependency_overrides[App.get_instance] = lambda: mock_app
    return mock_app


@pytest.fixture(scope="function")
def client(setup_app):
    with TestClient(app) as client:
        client.mock_app = setup_app
        yield client


class TestPasswordResetEndpoints:

    def test_request_user_not_found(self, client):
        client.mock_app.get_db_session.return_value = MagicMock()
        with patch("database.crud.get_user_by_email", return_value=None):
            response = client.post(
                "/api/user/reset-password/request?email=test@example.com"
            )
            assert response.status_code == 404
            assert response.json() == UserNotFoundError()

    def test_request_success(self, client):
        user = MagicMock()
        user.user_id = str(uuid4())
        user.email = "test@example.com"
        user.name = "Test User"

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = MagicMock()

        with patch("database.crud.get_user_by_email", return_value=user), patch(
            "backend.routers.user.reset_password.send_reset_password_email"
        ) as mock_email:
            response = client.post(
                f"/api/user/reset-password/request?email={user.email}"
            )
            assert response.status_code == 200
            assert response.json() == PasswordResetRequestPostResponse()

    def test_show_form_invalid_token(self, client):
        client.mock_app.get_redis_manager.return_value.get.return_value = None
        response = client.get("/api/user/reset-password/?token=invalid_token")
        assert response.status_code == 401
        assert InvalidOrExpiredResetToken().message in response.text

    def test_show_form_success(self, client):
        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": str(uuid4())
        }
        response = client.get("/api/user/reset-password/?token=valid_token")
        assert response.status_code == 200
        assert response.text == PasswordResetGetHTMLResponse(token="valid_token").html

    def test_show_form_exception(self, client):
        client.mock_app.get_redis_manager.return_value.get.side_effect = Exception(
            "Redis failure"
        )
        response = client.get("/api/user/reset-password/?token=exception_token")
        assert response.status_code == 500
        assert ErrorShowingPasswordResetForm().message in response.text

    def test_change_password_invalid_token(self, client):
        client.mock_app.get_redis_manager.return_value.get.return_value = None
        response = client.post(
            "/api/user/reset-password/change",
            data={"token": "bad", "new_password": "securepass123"},
        )
        assert response.status_code == 401
        assert InvalidOrExpiredResetToken().message in response.text

    def test_change_password_user_not_found(self, client):
        token = str(uuid4())
        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": str(uuid4())
        }
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=None):
            response = client.post(
                "/api/user/reset-password/change",
                data={"token": token, "new_password": "password123"},
            )
            assert response.status_code == 404
            assert UserNotFoundError().message in response.text

    def test_change_password_success(self, client):
        token = str(uuid4())
        user = MagicMock()
        user.user_id = str(uuid4())
        user.email = "test@example.com"

        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": user.user_id
        }
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=user):
            with patch("database.crud.update_user") as mock_update:
                response = client.post(
                    "/api/user/reset-password/change",
                    data={"token": token, "new_password": "Newpass123"},
                )
                assert response.status_code == 200
                assert (
                    response.text
                    == PasswordResetGetHTMLResponse(token=token, success=True).html
                )
                mock_update.assert_called_once()

    def test_change_password_fail(self, client):
        token = str(uuid4())
        user = MagicMock()
        user.user_id = str(uuid4())
        user.email = "test@example.com"

        client.mock_app.get_redis_manager.return_value.get.return_value = {
            "user_id": user.user_id
        }
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=user):
            with patch("database.crud.update_user") as mock_update:
                response = client.post(
                    "/api/user/reset-password/change",
                    data={"token": token, "new_password": "newpass123"},
                )
                assert response.status_code == 422
