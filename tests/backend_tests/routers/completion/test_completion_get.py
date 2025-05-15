# This file is intended for the current version of the codebase and may not be compatible with future versions.
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.main import app
from backend.Responses import (
    CompletionPostResponse,
    QueryNotFoundError,
)


class TestCompletionRoutes:

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
            client.cookies.set("session_token", "valid_token")
            yield client

    @pytest.fixture(scope="function")
    def valid_user_id(self):
        return str(uuid.uuid4())

    @pytest.fixture(scope="function")
    def mock_models(self):
        model1 = MagicMock()
        model1.model_id = 1
        model1.model_name = "deepseek-1.3b"

        model2 = MagicMock()
        model2.model_id = 2
        model2.model_name = "starcoder2-3b"

        return {1: model1, 2: model2}

    @pytest.fixture(scope="function")
    def query_id(self):
        return str(uuid.uuid4())

    def test_get_completions_success(self, client, query_id, mock_models):
        # Mock query, generations, and get_model_by_id
        mock_query = MagicMock()
        mock_query.query_id = query_id

        mock_generation1 = MagicMock()
        mock_generation1.query_id = query_id
        mock_generation1.model_id = 1
        mock_generation1.completion = (
            "def example_function():\n    # Completion from deepseek-1.3b\n    pass"
        )
        mock_generation1.confidence = 0.85

        mock_generation2 = MagicMock()
        mock_generation2.query_id = query_id
        mock_generation2.model_id = 2
        mock_generation2.completion = (
            "def example_function():\n    # Completion from starcoder2-3b\n    pass"
        )
        mock_generation2.confidence = 0.85

        mock_get_query = MagicMock(return_value=mock_query)
        mock_get_generations = MagicMock(
            return_value=[mock_generation1, mock_generation2]
        )

        def mock_get_model_side_effect(db, model_id):
            return mock_models.get(model_id)

        mock_get_model = MagicMock(side_effect=mock_get_model_side_effect)

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        client.mock_app.get_session_manager.return_value = mock_session
        with patch(
            "backend.routers.completion.get.App.get_instance", return_value=mock_app
        ), patch(
            "backend.routers.completion.get.crud.get_query_by_id", mock_get_query
        ), patch(
            "backend.routers.completion.get.crud.get_generations_by_query_id",
            mock_get_generations,
        ), patch(
            "backend.routers.completion.get.crud.get_model_by_id", mock_get_model
        ):
            response = client.get(f"/api/completion/{query_id}")

            assert response.status_code == 200
            response_data = response.json()

            assert (
                response_data["message"]
                == CompletionPostResponse.model_fields["message"].default
            )
            assert response_data["data"]["query_id"] == query_id
            assert len(response_data["data"]["completions"]) == 2
            assert response_data["data"]["completions"][0]["model_id"] == 1
            assert (
                response_data["data"]["completions"][0]["model_name"] == "deepseek-1.3b"
            )
            assert response_data["data"]["completions"][1]["model_id"] == 2
            assert (
                response_data["data"]["completions"][1]["model_name"] == "starcoder2-3b"
            )

    def test_get_completions_query_not_found(self, client, query_id):
        # Mock get_query_by_id to return None (query not found)
        mock_get_query = MagicMock(return_value=None)

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        client.mock_app.get_session_manager.return_value = mock_session

        with patch(
            "backend.routers.completion.get.App.get_instance", return_value=mock_app
        ), patch("backend.routers.completion.get.crud.get_query_by_id", mock_get_query):
            response = client.get(f"/api/completion/{query_id}")

            assert response.status_code == 404
            expected_error = QueryNotFoundError()
            assert response.json() == expected_error.dict()
