import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    AuthenticateUserError,
    AuthenticateUserNormalPostResponse,
    AuthenticateUserOAuthPostResponse,
    ConfigNotFound,
    InvalidEmailOrPassword,
    InvalidOrExpiredJWTToken,
)
from main import app
from response_models import ResponseUser


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
        mock_user = ResponseUser.fake(email=auth_email_query.email)
        mock_crud.get_user_by_email_password.return_value = mock_user
        mock_config = '{"config1":true}'
        mock_crud.get_config_by_id.return_value = MagicMock(config_data=mock_config)

        auth_token = str(uuid.uuid4())

        # Mock the acquire_auth_token function to return a fixed token
        with patch(
            "backend.routers.user.authenticate.acquire_auth_token",
            return_value=auth_token,
        ), patch("backend.routers.user.authenticate.crud", mock_crud):
            response = client.post(
                "/api/user/authenticate", json=auth_email_query.dict()
            )
            response_result = response.json()
            assert response.status_code == 200
            response_result["user"]["password"] = mock_user.password.get_secret_value()
            assert response_result == AuthenticateUserNormalPostResponse(
                user=mock_user, config=json.loads(mock_config)
            )
            assert response.cookies.get("auth_token") == auth_token

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
        mock_user = ResponseUser.fake()
        mock_crud.get_user_by_email.return_value = mock_user
        mock_config = '{"config1":true}'
        mock_crud.get_config_by_id.return_value = MagicMock(config_data=mock_config)
        mock_verify_jwt_token = MagicMock(return_value={"email": mock_user.email})

        auth_token = str(uuid.uuid4())

        client.mock_app.get_db_session.return_value = MagicMock()
        client.mock_app.get_redis_manager.return_value = MagicMock()

        with patch(
            "backend.routers.user.authenticate.acquire_auth_token",
            return_value=auth_token,
        ), patch("backend.routers.user.authenticate.crud", mock_crud), patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post(
                "/api/user/authenticate", json=auth_oauth_query.dict()
            )

            response_result = response.json()
            assert response.status_code == 200
            response_result["user"]["password"] = mock_user.password.get_secret_value()
            assert response_result == AuthenticateUserOAuthPostResponse(
                user=mock_user, config=json.loads(mock_config)
            )
            assert response.cookies.get("auth_token") == auth_token

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
            assert response.json() == InvalidOrExpiredJWTToken().model_dump()

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
            assert response.json() == InvalidOrExpiredJWTToken().model_dump()

    def test_authenticate_user_invalid_payload(self, client: TestClient):
        response = client.post("/api/user/authenticate", json={"email": "bad"})
        assert response.status_code == 422
        assert "detail" in response.json()

    def test_authenticate_user_config_not_found(
        self,
        client: TestClient,
        auth_email_query: Queries.AuthenticateUserEmailPassword,
    ):
        mock_crud = MagicMock()
        mock_user = ResponseUser.fake(email=auth_email_query.email)
        mock_crud.get_user_by_email_password.return_value = mock_user
        # Simulate config not found
        mock_crud.get_config_by_id.return_value = None

        auth_token = str(uuid.uuid4())
        mock_redis_manager = MagicMock()

        # Mock the acquire_auth_token function to return a fixed token
        with patch(
            "backend.routers.user.authenticate.acquire_auth_token",
            return_value=auth_token,
        ), patch("backend.routers.user.authenticate.crud", mock_crud):
            response = client.post(
                "/api/user/authenticate", json=auth_email_query.dict()
            )

            assert response.status_code == 404
            assert response.json() == ConfigNotFound()

    def test_authenticate_user_server_error(
        self,
        client: TestClient,
        auth_email_query: Queries.AuthenticateUserEmailPassword,
    ):
        mock_crud = MagicMock()
        # Simulate a server error by raising an exception
        mock_crud.get_user_by_email_password.side_effect = Exception("Database error")

        with patch("backend.routers.user.authenticate.crud", mock_crud):
            response = client.post(
                "/api/user/authenticate", json=auth_email_query.dict()
            )

            assert response.status_code == 500
            assert response.json() == AuthenticateUserError()
