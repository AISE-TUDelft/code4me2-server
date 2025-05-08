import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.main import app  # Adjust this import based on your project structure
from backend.models.Responses import (
    InvalidOrExpiredToken,
    InvalidEmailOrPassword,
    AuthenticateUserNormalPostResponse,
    AuthenticateUserOAuthPostResponse,
)


class TestAuthenticate:
    @pytest.fixture(scope="function")
    def client(self):
        with TestClient(app) as client:
            yield client

    @pytest.fixture(scope="function")
    def normal_payload(self):
        return {
            "email": "test@example.com",
            "password": "ValidPassword123",
        }

    @pytest.fixture(scope="function")
    def oauth_payload(self):
        return {
            "token": "valid_jwt_token",
            "provider": "google",
        }

    def test_authenticate_user_success_email_password(
        self, client: TestClient, normal_payload: dict
    ):
        mock_get_user = MagicMock()
        mock_user = MagicMock()
        mock_user.user_id = uuid.uuid4()
        mock_user.email = normal_payload["email"]
        mock_user.name = "Test name"
        mock_user.joined_at = datetime.datetime.now()
        mock_user.verified = False

        mock_get_user.return_value = mock_user
        mock_session_manager = MagicMock()
        mock_session_token = uuid.uuid4()
        mock_session_manager.create_session.return_value = mock_session_token

        with patch(
            "backend.routers.user.authenticate.crud.get_user_by_email_password",
            mock_get_user,
        ), patch(
            "backend.routers.user.authenticate.App.get_session_manager",
            return_value=mock_session_manager,
        ):
            response = client.post("/api/user/authenticate", json=normal_payload)

            assert response.status_code == 200
            response_content = response.json()
            response_content["user_id"] = uuid.UUID(response_content["user_id"])
            response_content["user"]["user_id"] = uuid.UUID(
                response_content["user"]["user_id"]
            )
            response_content["user"]["joined_at"] = datetime.datetime.fromisoformat(
                response_content["user"]["joined_at"]
            )
            response_content["session_token"] = uuid.UUID(
                response_content["session_token"]
            )

            assert (
                response_content
                == AuthenticateUserNormalPostResponse(
                    user_id=mock_user.user_id,
                    session_token=mock_session_token,
                    user=mock_user,
                ).model_dump()
            )

    def test_authenticate_user_success_oauth(
        self, client: TestClient, oauth_payload: dict
    ):
        mock_get_user = MagicMock()
        mock_user = MagicMock()
        mock_user.user_id = uuid.uuid4()
        mock_user.email = "test@example.com"
        mock_user.name = "OAuth User"
        mock_user.joined_at = datetime.datetime.now()
        mock_user.verified = False
        mock_get_user.return_value = mock_user

        mock_verify_jwt_token = MagicMock(return_value={"email": mock_user.email})
        mock_session_manager = MagicMock()
        mock_session_token = uuid.uuid4()
        mock_session_manager.create_session.return_value = mock_session_token

        with patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ), patch(
            "backend.routers.user.authenticate.crud.get_user_by_email", mock_get_user
        ), patch(
            "backend.routers.user.authenticate.App.get_session_manager",
            return_value=mock_session_manager,
        ):
            response = client.post("/api/user/authenticate", json=oauth_payload)

            assert response.status_code == 200
            response_content = response.json()
            response_content["user_id"] = uuid.UUID(response_content["user_id"])
            response_content["user"]["user_id"] = uuid.UUID(
                response_content["user"]["user_id"]
            )
            response_content["user"]["joined_at"] = datetime.datetime.fromisoformat(
                response_content["user"]["joined_at"]
            )
            response_content["session_token"] = uuid.UUID(
                response_content["session_token"]
            )

            assert (
                response_content
                == AuthenticateUserOAuthPostResponse(
                    user_id=mock_user.user_id,
                    session_token=mock_session_token,
                    user=mock_user,
                ).model_dump()
            )

    def test_authenticate_user_invalid_email_or_password(
        self, client: TestClient, normal_payload: dict
    ):
        mock_get_user = MagicMock()
        mock_get_user.return_value = None  # User not found

        with patch(
            "backend.routers.user.authenticate.crud.get_user_by_email_password",
            mock_get_user,
        ):
            response = client.post("/api/user/authenticate", json=normal_payload)

            assert response.status_code == 401
            assert response.json() == InvalidEmailOrPassword().model_dump()

    def test_authenticate_user_invalid_oauth_token(
        self, client: TestClient, oauth_payload: dict
    ):
        oauth_payload["token"] = "invalidtoken"
        mock_verify_jwt_token = MagicMock(return_value=None)  # Invalid JWT token

        with patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post("/api/user/authenticate", json=oauth_payload)

            assert response.status_code == 401
            assert response.json() == InvalidOrExpiredToken().model_dump()

    def test_authenticate_user_oauth_user_not_found(
        self, client: TestClient, oauth_payload: dict
    ):
        mock_verify_jwt_token = MagicMock(return_value={"email": "test@example.com"})
        mock_get_user = MagicMock(return_value=None)  # User not found in DB

        with patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ), patch(
            "backend.routers.user.authenticate.crud.get_user_by_email", mock_get_user
        ):
            response = client.post("/api/user/authenticate", json=oauth_payload)

            assert response.status_code == 401
            assert response.json() == InvalidOrExpiredToken().model_dump()

    def test_authenticate_user_invalid_oauth_provider(
        self, client: TestClient, oauth_payload: dict
    ):
        oauth_payload["provider"] = "facebook"  # Invalid provider
        mock_verify_jwt_token = MagicMock(return_value={"email": "test@example.com"})

        with patch(
            "backend.routers.user.authenticate.verify_jwt_token", mock_verify_jwt_token
        ):
            response = client.post("/api/user/authenticate", json=oauth_payload)

            assert response.status_code == 422
            assert (
                "detail" in response.json()
            )  # FastAPI should return validation error details
