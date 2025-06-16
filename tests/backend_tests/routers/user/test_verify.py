from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    GetVerificationError,
    GetVerificationGetResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredVerificationToken,
    ResendVerificationEmailError,
    ResendVerificationEmailPostResponse,
    UserNotFoundError,
    VerifyUserError,
)
from main import app


@pytest.fixture(scope="session")
def setup_app():
    mock_app = MagicMock()
    app.dependency_overrides[App.get_instance] = lambda: mock_app
    return mock_app


@pytest.fixture(scope="function")
def client(setup_app):
    with TestClient(app) as client:
        client.mock_app = setup_app
        client.cookies.set("auth_token", str(uuid4()))
        yield client


class TestUserVerificationEndpoints:

    def test_check_missing_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": None
        }.get(key)
        client.cookies.set("auth_token", None)
        response = client.get("/api/user/verify/check")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_check_user_not_found(self, client):
        user_id = str(uuid4())
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": {"user_id": user_id}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=None):
            response = client.get("/api/user/verify/check")
            assert response.status_code == 404
            assert response.json() == UserNotFoundError()

    def test_check_verified_status(self, client):
        user_id = str(uuid4())
        user = MagicMock()
        user.verified = True

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": {"user_id": user_id}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=user):
            response = client.get("/api/user/verify/check")
            assert response.status_code == 200
            assert response.json() == GetVerificationGetResponse(user_is_verified=True)

    def test_check_exception(self, client):
        client.mock_app.get_redis_manager().get.side_effect = Exception("Redis failure")
        response = client.get("/api/user/verify/check")
        assert response.status_code == 500
        assert response.json() == GetVerificationError()

    def test_resend_missing_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": None
        }.get(key)
        response = client.post("/api/user/verify/resend")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_resend_user_not_found(self, client):
        user_id = str(uuid4())
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": {"user_id": user_id}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=None):
            response = client.post("/api/user/verify/resend")
            assert response.status_code == 404
            assert response.json() == UserNotFoundError()

    def test_resend_successful(self, client):
        user_id = str(uuid4())
        user = MagicMock(user_id=user_id, email="test@example.com", name="Test")

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "auth_token": {"user_id": user_id}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=user):
            with patch(
                "celery_app.tasks.db_tasks.send_verification_email_task.delay"
            ) as mock_task:
                response = client.post("/api/user/verify/resend")
                assert response.status_code == 200
                assert response.json() == ResendVerificationEmailPostResponse()
                mock_task.assert_called_once()

    def test_resend_exception(self, client):
        client.mock_app.get_redis_manager().get.side_effect = Exception("Redis failure")
        response = client.post("/api/user/verify/resend")
        assert response.status_code == 500
        assert response.json() == ResendVerificationEmailError()

    def test_verify_invalid_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "email_verification": None
        }.get(key)
        response = client.get("/api/user/verify/?token=some_token")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredVerificationToken()

    def test_verify_user_not_found(self, client):
        token = str(uuid4())
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token_arg: {
            "email_verification": {"user_id": token}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=None):
            response = client.get(f"/api/user/verify/?token={token}")
            assert response.status_code == 404
            assert response.json() == UserNotFoundError()

    def test_verify_successful(self, client):
        token = str(uuid4())
        user = MagicMock()
        user.name = "Test user"

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token_arg: {
            "email_verification": {"user_id": token}
        }.get(key)
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("database.crud.get_user_by_id", return_value=user):
            with patch("database.crud.update_user") as mock_update:
                response = client.get(f"/api/user/verify/?token={token}")
                assert response.status_code == 200
                mock_update.assert_called_once()

    def test_verify_exception(self, client):
        client.mock_app.get_redis_manager().get.side_effect = Exception("Redis failure")
        response = client.get("/api/user/verify/?token=abc")
        assert response.status_code == 500
        assert response.json() == VerifyUserError()
