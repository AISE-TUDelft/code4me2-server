import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from backend.main import app  # Adjust this import based on your project structure
import uuid
from backend.models.Responses import (
    CreateUserPostResponse,
    UserAlreadyExistsWithThisEmail,
    InvalidOrExpiredToken,
    ErrorResponse,
)


class TestCreate:
    @pytest.fixture(scope="function")
    def client(self):
        with TestClient(app) as client:
            yield client

    @pytest.fixture(scope="function")
    def normal_payload(self):
        return {
            "email": "test@example.com",
            "name": "Test User",
            "password": "ValidPassword123",
        }

    @pytest.fixture(scope="function")
    def oauth_payload(self, normal_payload):
        return normal_payload | {"token": "Test token", "provider": "google"}

    def test_create_user_success(self, client: TestClient, normal_payload: dict):
        mock_get_user = MagicMock(return_value=None)  # User doesn't exist
        mock_user = MagicMock()
        user_id = uuid.uuid4()
        mock_user.user_id = user_id
        mock_create_user = MagicMock(return_value=mock_user)

        with patch(
            "backend.routers.user.create.crud.get_user_by_email", mock_get_user
        ), patch("backend.routers.user.create.crud.create_user", mock_create_user):
            response = client.post("/api/user/create", json=normal_payload)

            assert response.status_code == 201
            response_content = response.json()
            response_content["user_id"] = uuid.UUID(response_content["user_id"])
            assert (
                response_content
                == CreateUserPostResponse(user_id=user_id, session_id=None).model_dump()
            )

    def test_create_user_already_exists(self, client: TestClient, normal_payload: dict):
        mock_user = MagicMock()
        mock_get_user = MagicMock(return_value=mock_user)  # User already exists
        with patch("backend.routers.user.create.crud.get_user_by_email", mock_get_user):
            response = client.post("/api/user/create", json=normal_payload)
            assert response.status_code == 409
            assert (
                response.json() == UserAlreadyExistsWithThisEmail().model_dump()
            )  # Check the correct error response

    def test_create_user_invalid_token(self, client: TestClient, oauth_payload: dict):
        oauth_payload["token"] = "invalid token"
        mock_get_user = MagicMock(return_value=None)
        mock_user = MagicMock()
        mock_create_user = MagicMock(return_value=mock_user)
        mock_verify_jwt_token = MagicMock(
            return_value=None
        )  # Simulating an invalid/expired token

        with patch(
            "backend.routers.user.create.crud.get_user_by_email", mock_get_user
        ), patch(
            "backend.routers.user.create.crud.create_user", mock_create_user
        ), patch(
            "backend.routers.user.create.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post("/api/user/create", json=oauth_payload)

            assert response.status_code == 401
            assert (
                response.json() == InvalidOrExpiredToken().model_dump()
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
