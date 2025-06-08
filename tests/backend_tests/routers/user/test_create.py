from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    CreateUserError,
    CreateUserPostResponse,
    InvalidOrExpiredJWTToken,
    UserAlreadyExistsWithThisEmail,
)
from main import app  # Adjust this import based on your project structure
from response_models import ResponseUser


class TestCreate:

    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        # override the get_instance method cached by fastapi to return the mock app
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.mock_app = setup_app
            yield client

    @pytest.fixture(scope="function")
    def create_user_query(self):
        return Queries.CreateUser.fake()

    @pytest.fixture(scope="function")
    def create_user_oauth_query(self):
        return Queries.CreateUserOauth.fake()

    def test_create_user_success(
        self, client: TestClient, create_user_query: Queries.CreateUser
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = None  # Simulating no user found
        fake_user = ResponseUser.fake(
            email=create_user_query.email,
            name=create_user_query.name,
            password=create_user_query.password,
        )
        mock_crud.create_user.return_value = fake_user

        with patch("backend.routers.user.create.crud", mock_crud):
            response = client.post("/api/user/create", json=create_user_query.dict())

            assert response.status_code == 201
            assert response.json() == CreateUserPostResponse(user_id=fake_user.user_id)

            client.mock_app.get_db_session.assert_called_once()

    def test_create_user_already_exists(
        self, client: TestClient, create_user_query: Queries.CreateUser
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = (
            MagicMock()
        )  # Simulating a user found

        with patch("backend.routers.user.create.crud", mock_crud):
            response = client.post("/api/user/create", json=create_user_query.dict())

            assert response.status_code == 409
            assert (
                response.json() == UserAlreadyExistsWithThisEmail()
            )  # Check the correct error response

    def test_create_user_invalid_token(
        self, client: TestClient, create_user_oauth_query: Queries.CreateUserOauth
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = None  # Simulating no user found
        mock_verify_jwt_token = MagicMock(
            return_value=None
        )  # Simulating an invalid/expired token

        with (
            patch("backend.routers.user.create.crud", mock_crud),
            patch(
                "backend.routers.user.create.verify_jwt_token", mock_verify_jwt_token
            ),
        ):
            response = client.post(
                "/api/user/create", json=create_user_oauth_query.dict()
            )

            assert response.status_code == 401
            assert (
                response.json() == InvalidOrExpiredJWTToken()
            )  # Check the invalid token error response

    def test_create_user_invalid_payload(self, client: TestClient):
        # Testing invalid payload, e.g., missing fields
        invalid_payload = {
            "email": "invalidemail",
        }

        response = client.post("/api/user/create", json=invalid_payload)
        assert response.status_code == 422
        assert (
            "detail" in response.json()
        )  # FastAPI should return validation error details

    def test_create_user_oauth_email_mismatch(
        self, client: TestClient, create_user_oauth_query: Queries.CreateUserOauth
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = None  # Simulating no user found

        # Simulate JWT token verification returning a different email than the one in the request
        mock_verify_jwt_token = MagicMock(
            return_value={"email": "different_email@example.com"}
        )

        with (
            patch("backend.routers.user.create.crud", mock_crud),
            patch(
                "backend.routers.user.create.verify_jwt_token", mock_verify_jwt_token
            ),
        ):
            response = client.post(
                "/api/user/create", json=create_user_oauth_query.dict()
            )

            assert response.status_code == 401
            assert response.json() == InvalidOrExpiredJWTToken()

    def test_create_user_server_error(
        self, client: TestClient, create_user_query: Queries.CreateUser
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_email.return_value = None  # Simulating no user found
        # Simulate a server error by raising an exception
        mock_crud.create_user.side_effect = Exception("Database error")

        with patch("backend.routers.user.create.crud", mock_crud):
            response = client.post("/api/user/create", json=create_user_query.dict())

            assert response.status_code == 500
            assert response.json() == CreateUserError()
