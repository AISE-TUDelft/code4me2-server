from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.main import app
from backend.models.Responses import (
    AuthenticateUserNormalPostResponse,
    AuthenticateUserOAuthPostResponse,
    InvalidEmailOrPassword,
    InvalidOrExpiredToken,
)
from base_models import UserBase


class TestAuthenticate:

    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.mock_app = setup_app
            yield client

    @pytest.fixture(scope="function")
    def auth_email_query(self):
        return Queries.AuthenticateUserEmailPassword.fake()

    @pytest.fixture(scope="function")
    def auth_oauth_query(self):
        return Queries.AuthenticateUserOAuth.fake()

    def test_authenticate_user_email_success(
        self,
        client: TestClient,
        auth_email_query: Queries.AuthenticateUserEmailPassword,
    ):
        mock_crud = MagicMock()
        mock_user = UserBase.fake(email=auth_email_query.email)
        mock_crud.get_user_by_email_password.return_value = mock_user

        session_token = "dummy_session_token"
        client.mock_app.get_session_manager.return_value.create_session.return_value = (
            session_token
        )

        with patch("backend.routers.user.authenticate.crud", mock_crud):
            response = client.post(
                "/api/user/authenticate", json=auth_email_query.dict()
            )

            assert response.status_code == 200
            assert response.json() == AuthenticateUserNormalPostResponse(user=mock_user)
            assert response.cookies.get("session_token") == session_token

    def test_authenticate_user_email_invalid(
        self,
        client: TestClient,
        auth_email_query: Queries.AuthenticateUserEmailPassword,
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email_password.return_value = None

        with patch("backend.routers.user.authenticate.crud", mock_crud):
            response = client.post(
                "/api/user/authenticate", json=auth_email_query.dict()
            )

            assert response.status_code == 401
            assert response.json() == InvalidEmailOrPassword()

    def test_authenticate_user_oauth_success(
        self, client: TestClient, auth_oauth_query: Queries.AuthenticateUserOAuth
    ):
        mock_crud = MagicMock()
        mock_user = UserBase.fake()
        mock_crud.get_user_by_email.return_value = mock_user

        mock_verify_jwt_token = MagicMock(return_value={"email": mock_user.email})
        session_token = "dummy_session_token"
        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_session_manager.return_value.create_session.return_value = (
            session_token
        )

        with patch("backend.routers.user.authenticate.crud", mock_crud), patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post(
                "/api/user/authenticate", json=auth_oauth_query.dict()
            )

            assert response.status_code == 200
            assert response.json() == AuthenticateUserOAuthPostResponse(user=mock_user)
            assert response.cookies.get("session_token") == session_token

    def test_authenticate_user_oauth_invalid_token(
        self, client: TestClient, auth_oauth_query: Queries.AuthenticateUserOAuth
    ):
        mock_crud = MagicMock()
        mock_verify_jwt_token = MagicMock(return_value=None)

        with patch("backend.routers.user.authenticate.crud", mock_crud), patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post(
                "/api/user/authenticate", json=auth_oauth_query.dict()
            )

            assert response.status_code == 401
            assert response.json() == InvalidOrExpiredToken().model_dump()

    def test_authenticate_user_oauth_user_not_found(
        self, client: TestClient, auth_oauth_query: Queries.AuthenticateUserOAuth
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = None
        mock_verify_jwt_token = MagicMock(return_value=None)

        with patch("backend.routers.user.authenticate.crud", mock_crud), patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post(
                "/api/user/authenticate", json=auth_oauth_query.dict()
            )

            assert response.status_code == 401
            assert response.json() == InvalidOrExpiredToken().model_dump()

    def test_authenticate_user_invalid_payload(self, client: TestClient):
        response = client.post("/api/user/authenticate", json={"email": "bad"})
        assert response.status_code == 422
        assert "detail" in response.json()
