from unittest.mock import ANY, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    CompletionFeedbackPostResponse,
    FeedbackRecordingError,
    GenerationNotFoundError,
)
from main import app


class TestCompletionFeedback:

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
    def completion_feedback(self):
        """Generate fake completion feedback"""
        return Queries.FeedbackCompletion.fake(
            was_accepted=True,
            ground_truth="def actual_implementation():\n    return 42",
        )

    @pytest.fixture(scope="function")
    def completion_feedback_no_ground_truth(self):
        """Generate fake completion feedback without ground truth"""
        return Queries.FeedbackCompletion.fake(was_accepted=False, ground_truth=None)

    def test_submit_feedback_success_with_ground_truth(
        self, client: TestClient, completion_feedback: Queries.FeedbackCompletion
    ):
        # Setup mocks
        mock_crud = MagicMock()

        # Mock generation exists
        mock_generation = MagicMock()
        mock_generation.query_id = completion_feedback.query_id
        mock_generation.model_id = completion_feedback.model_id
        mock_crud.get_generations_by_query_and_model_id.return_value = mock_generation

        client.mock_app.get_db_session.return_value = MagicMock()
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        client.mock_app.get_session_manager.return_value = mock_session
        with patch("backend.routers.completion.feedback.crud", mock_crud):
            response = client.post(
                "/api/completion/feedback/", json=completion_feedback.dict()
            )

        assert response.status_code == 200
        response_data = response.json()

        assert (
            response_data["message"]
            == CompletionFeedbackPostResponse.model_fields["message"].default
        )
        assert response_data["data"]["query_id"] == str(completion_feedback.query_id)
        assert response_data["data"]["model_id"] == completion_feedback.model_id

        # Fixed: Use ANY for the first argument (db_session)
        mock_crud.update_generation_acceptance.assert_called_once_with(
            ANY,
            str(completion_feedback.query_id),
            completion_feedback.model_id,
            completion_feedback.was_accepted,
        )
        mock_crud.add_ground_truth.assert_called_once()

    def test_submit_feedback_success_without_ground_truth(
        self,
        client: TestClient,
        completion_feedback_no_ground_truth: Queries.FeedbackCompletion,
    ):
        # Setup mocks
        mock_crud = MagicMock()

        # Mock generation exists
        mock_generation = MagicMock()
        mock_crud.get_generations_by_query_and_model_id.return_value = mock_generation

        client.mock_app.get_db_session.return_value = MagicMock()
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        with patch("backend.routers.completion.feedback.crud", mock_crud):
            response = client.post(
                "/api/completion/feedback/",
                json=completion_feedback_no_ground_truth.dict(),
            )

        assert response.status_code == 200

        # Verify mock calls
        mock_crud.update_generation_acceptance.assert_called_once()
        mock_crud.add_ground_truth.assert_not_called()  # No ground truth provided

    def test_submit_feedback_generation_not_found(
        self, client: TestClient, completion_feedback: Queries.FeedbackCompletion
    ):
        mock_crud = MagicMock()
        mock_crud.get_generations_by_query_and_model_id.return_value = None
        client.mock_app.get_db_session.return_value = MagicMock()
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        with patch("backend.routers.completion.feedback.crud", mock_crud):
            response = client.post(
                "/api/completion/feedback/", json=completion_feedback.dict()
            )

        assert response.status_code == 404
        expected_error = GenerationNotFoundError()
        assert response.json() == expected_error.dict()

    def test_submit_feedback_invalid_payload(self, client: TestClient):
        # Test with invalid data
        invalid_payload = {
            "query_id": "not-a-uuid",  # Invalid UUID
            "model_id": "not-an-int",  # Invalid integer
            "was_accepted": "not-a-bool",  # Invalid boolean
        }

        response = client.post("/api/completion/feedback/", json=invalid_payload)
        assert response.status_code == 422
        assert "detail" in response.json()

    def test_submit_feedback_database_error(
        self, client: TestClient, completion_feedback: Queries.FeedbackCompletion
    ):
        mock_crud = MagicMock()
        mock_crud.get_generations_by_query_and_model_id.side_effect = Exception(
            "Database error"
        )

        mock_db_session = MagicMock()
        client.mock_app.get_db_session.return_value = mock_db_session
        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}

        with patch("backend.routers.completion.feedback.crud", mock_crud):
            response = client.post(
                "/api/completion/feedback/", json=completion_feedback.dict()
            )

        assert response.status_code == 500

        expected_error = FeedbackRecordingError("Database error")
        assert response.json() == expected_error.dict()

        mock_db_session.rollback.assert_called_once()

    def test_submit_feedback_partial_update_failure(
        self, client: TestClient, completion_feedback: Queries.FeedbackCompletion
    ):
        # Test case where generation update succeeds but ground truth save fails
        mock_crud = MagicMock()

        mock_generation = MagicMock()
        mock_crud.get_generations_by_query_and_model_id.return_value = mock_generation
        mock_crud.update_generation_acceptance.return_value = True
        mock_crud.add_ground_truth.side_effect = Exception("Ground truth save failed")

        mock_db_session = MagicMock()
        client.mock_app.get_db_session.return_value = mock_db_session

        with patch("backend.routers.completion.feedback.crud", mock_crud):
            response = client.post(
                "/api/completion/feedback/", json=completion_feedback.dict()
            )

        assert response.status_code == 500
        mock_db_session.rollback.assert_called_once()
