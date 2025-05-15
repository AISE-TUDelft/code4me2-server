import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.main import app
from backend.Responses import (
    InvalidSessionToken,
)
from base_models import ContextBase, QueryBase, TelemetryBase


class TestCompletionRequest:

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
    def completion_request(self):
        """Generate a fake completion request with nested structure"""
        return Queries.RequestCompletion.fake(
            model_ids=[1, 2],
        )

    def test_request_completion_success(
        self, client: TestClient, completion_request: Queries.RequestCompletion
    ):
        # Setup mocks
        mock_crud = MagicMock()

        # Mock model data
        mock_model_1 = MagicMock()
        mock_model_1.model_id = 1
        mock_model_1.model_name = "starcoder2-3b"

        mock_model_2 = MagicMock()
        mock_model_2.model_id = 2
        mock_model_2.model_name = "deepseek-1.3b"

        mock_crud.get_model_by_id.side_effect = lambda db, model_id: (
            mock_model_1 if model_id == 1 else mock_model_2
        )
        mock_crud.add_context.return_value = ContextBase.fake(1)
        mock_crud.add_telemetry.return_value = TelemetryBase.fake(1)
        mock_crud.add_query.return_value = QueryBase.fake(1)

        # Mock config
        mock_config = MagicMock()
        mock_config.server_version_id = 1
        client.mock_app.get_config.return_value = mock_config

        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": str(uuid.uuid4())}

        client.mock_app.get_session_manager.return_value = mock_session
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("backend.routers.completion.request.crud", mock_crud):
            response = client.post(
                "/api/completion/request/",
                json=completion_request.dict(),  # Now uses the fixed dict() method
            )

        assert response.status_code == 200
        response_data = response.json()

        # Verify response structure
        assert "message" in response_data
        assert "data" in response_data
        assert "query_id" in response_data["data"]
        assert "completions" in response_data["data"]
        assert len(response_data["data"]["completions"]) == 2

        # Verify mock calls
        mock_crud.add_context.assert_called_once()
        mock_crud.add_telemetry.assert_called_once()
        mock_crud.add_query.assert_called_once()
        assert mock_crud.add_generation.call_count == 2

    def test_request_completion_model_not_found(
        self, client: TestClient, completion_request: Queries.RequestCompletion
    ):
        # Test case where some models don't exist
        mock_crud = MagicMock()
        mock_crud.get_model_by_id.return_value = None  # No models found

        mock_crud.add_context.return_value = ContextBase.fake(1)
        mock_crud.add_telemetry.return_value = TelemetryBase.fake(1)
        mock_crud.add_query.return_value = QueryBase.fake(1)

        mock_config = MagicMock()
        mock_config.server_version_id = 1
        client.mock_app.get_config.return_value = mock_config
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": str(uuid.uuid4())}

        client.mock_app.get_session_manager.return_value = mock_session
        client.mock_app.get_db_session.return_value = MagicMock()

        with patch("backend.routers.completion.request.crud", mock_crud):
            response = client.post(
                "/api/completion/request/",
                json=completion_request.dict(),  # Now uses the fixed dict() method
            )

        assert response.status_code == 200
        response_data = response.json()
        # Should still succeed but with no completions
        assert len(response_data["data"]["completions"]) == 0

    def test_request_completion_invalid_payload(self, client: TestClient):
        # Test with invalid nested structure
        invalid_payload = {
            "user_id": "not-a-uuid",  # Invalid UUID
            "model_ids": [],  # Empty list
            "context": {
                "prefix": "",
                # Missing required fields
            },
            "telemetry": {},  # Empty object
        }

        response = client.post("/api/completion/request/", json=invalid_payload)
        assert response.status_code == 422
        assert "detail" in response.json()

    def test_request_completion_user_invalid_session_token(
        self, client, completion_request: Queries.RequestCompletion
    ):
        # Mock session manager returns None
        mock_session = MagicMock()
        mock_session.get_session.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session

        response = client.post(
            "/api/completion/request/", json=completion_request.dict()
        )

        assert response.status_code == 401
        assert response.json() == InvalidSessionToken()

    def test_request_completion_database_error(
        self, client: TestClient, completion_request: Queries.RequestCompletion
    ):
        mock_crud = MagicMock()
        mock_crud.get_user_by_id.side_effect = Exception("Database error")
        client.mock_app.get_db_session.return_value = MagicMock()
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": str(uuid.uuid4())}

        client.mock_app.get_session_manager.return_value = mock_session

        with patch("backend.routers.completion.request.crud", mock_crud):
            response = client.post(
                "/api/completion/request/",
                json=completion_request.dict(),  # Now uses the fixed dict() method
            )

        assert response.status_code == 500
        assert "Failed to generate completions" in response.json()["message"]
