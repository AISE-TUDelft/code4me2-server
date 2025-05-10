# This file is intended for the current version of the codebase and may not be compatible with future versions.
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.main import app


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
            yield client

    @pytest.fixture(scope="function")
    def valid_user_id(self):
        return str(uuid.uuid4())

    @pytest.fixture(scope="function")
    def completion_request_payload(self, valid_user_id):
        return {
            "user_id": valid_user_id,
            "prefix": "def calculate_sum(a, b):",
            "suffix": "",
            "language_id": 67,
            "trigger_type_id": 1,
            "version_id": 1,
            "model_ids": [1, 2],
            "time_since_last_completion": 5000,
            "typing_speed": 120,
            "document_char_length": 500,
            "relative_document_position": 0.5,
        }

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

    @pytest.fixture(scope="function")
    def feedback_payload(self, query_id):
        return {
            "query_id": query_id,
            "model_id": 1,
            "was_accepted": True,
            "ground_truth": "def calculate_sum(a, b):\n    return a + b",
        }

    def test_request_completion_success(
        self, client, completion_request_payload, mock_models, valid_user_id
    ):
        # Mock user, add_context, add_telemetry, add_query, add_generation, and get_model_by_id
        mock_user = MagicMock()
        mock_user.user_id = valid_user_id

        mock_get_user = MagicMock(return_value=mock_user)
        mock_add_context = MagicMock()
        mock_add_telemetry = MagicMock()
        mock_add_query = MagicMock()
        mock_add_generation = MagicMock()

        def mock_get_model_side_effect(db, model_id):
            return mock_models.get(model_id)

        mock_get_model = MagicMock(side_effect=mock_get_model_side_effect)

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session
        mock_app.get_config.return_value.server_version_id = 1

        with patch(
            "backend.routers.completion.request.App.get_instance", return_value=mock_app
        ), patch(
            "backend.routers.completion.request.crud.get_user_by_id", mock_get_user
        ), patch(
            "backend.routers.completion.request.crud.add_context", mock_add_context
        ), patch(
            "backend.routers.completion.request.crud.add_telemetry", mock_add_telemetry
        ), patch(
            "backend.routers.completion.request.crud.add_query", mock_add_query
        ), patch(
            "backend.routers.completion.request.crud.add_generation",
            mock_add_generation,
        ), patch(
            "backend.routers.completion.request.crud.get_model_by_id", mock_get_model
        ), patch(
            "backend.routers.completion.request.crud.update_query_serving_time",
            MagicMock(),
        ):
            response = client.post(
                "/api/completion/request/", json=completion_request_payload
            )

            assert response.status_code == 200
            response_data = response.json()

            assert "query_id" in response_data["data"]
            assert "completions" in response_data["data"]
            assert len(response_data["data"]["completions"]) == 2
            assert response_data["data"]["completions"][0]["model_id"] == 1
            assert (
                response_data["data"]["completions"][0]["model_name"] == "deepseek-1.3b"
            )
            assert response_data["data"]["completions"][1]["model_id"] == 2
            assert (
                response_data["data"]["completions"][1]["model_name"] == "starcoder2-3b"
            )

    def test_request_completion_user_not_found(
        self, client, completion_request_payload
    ):
        # Mock get_user_by_id to return None (user not found)
        mock_get_user = MagicMock(return_value=None)

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.completion.request.App.get_instance", return_value=mock_app
        ), patch(
            "backend.routers.completion.request.crud.get_user_by_id", mock_get_user
        ):
            response = client.post(
                "/api/completion/request/", json=completion_request_payload
            )

            assert response.status_code == 404
            assert response.json()["message"] == "User not found"

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

        with patch(
            "backend.routers.completion.get.App.get_instance", return_value=mock_app
        ), patch("backend.routers.completion.get.crud.get_query_by_id", mock_get_query):
            response = client.get(f"/api/completion/{query_id}")

            assert response.status_code == 404
            assert response.json()["message"] == "Query not found"

    def test_submit_feedback_success(self, client, query_id, feedback_payload):
        # Mock generation and add_ground_truth
        mock_generation = MagicMock()
        mock_generation.query_id = query_id
        mock_generation.model_id = feedback_payload["model_id"]

        mock_get_generation = MagicMock(return_value=mock_generation)
        mock_update_generation = MagicMock()
        mock_add_ground_truth = MagicMock()

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.completion.feedback.App.get_instance",
            return_value=mock_app,
        ), patch(
            "backend.routers.completion.feedback.crud.get_generations_by_query_and_model_id",
            mock_get_generation,
        ), patch(
            "backend.routers.completion.feedback.crud.update_generation_acceptance",
            mock_update_generation,
        ), patch(
            "backend.routers.completion.feedback.crud.add_ground_truth",
            mock_add_ground_truth,
        ):
            response = client.post("/api/completion/feedback/", json=feedback_payload)

            assert response.status_code == 200
            response_data = response.json()

            assert response_data["message"] == "Feedback recorded successfully"
            assert response_data["data"]["query_id"] == query_id
            assert response_data["data"]["model_id"] == feedback_payload["model_id"]

    def test_submit_feedback_generation_not_found(self, client, feedback_payload):
        # Mock get_generations_by_query_and_model_id to return None (generation not found)
        mock_get_generation = MagicMock(return_value=None)

        # Create a mock App instance
        mock_app = MagicMock()
        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.completion.feedback.App.get_instance",
            return_value=mock_app,
        ), patch(
            "backend.routers.completion.feedback.crud.get_generations_by_query_and_model_id",
            mock_get_generation,
        ):
            response = client.post("/api/completion/feedback/", json=feedback_payload)

            assert response.status_code == 404
            assert response.json()["message"] == "Generation record not found"
