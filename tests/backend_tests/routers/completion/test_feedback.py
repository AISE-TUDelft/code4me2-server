import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    FeedbackRecordingError,
    GenerationNotFoundError,
    InvalidOrExpiredSessionToken,
    NoAccessToProvideFeedbackError,
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
            client.cookies.set("session_token", "valid_session_token")
            client.cookies.set("project_token", "valid_project_token")
            yield client

    @pytest.fixture(scope="function")
    def completion_feedback(self):
        return Queries.FeedbackCompletion.fake(
            was_accepted=True,
            ground_truth="def actual_implementation():\n    return 42",
        )

    def setup_redis_and_db(self, client, user_id="test_user"):
        mock_redis = MagicMock()
        client.mock_app.get_redis_manager.return_value = mock_redis
        mock_redis.get.side_effect = lambda prefix, token: {
            ("session_token", "valid_session_token"): {
                "auth_token": "valid_auth_token",
                "project_tokens": ["valid_project_token"],
            },
            ("auth_token", "valid_auth_token"): {"user_id": user_id},
            ("project_token", "valid_project_token"): {"project_id": "xyz"},
        }.get((prefix, token))

        mock_db = MagicMock()
        client.mock_app.get_db_session.return_value = mock_db
        return mock_db, mock_redis

    @patch("backend.routers.completion.feedback.db_tasks")
    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_success(
        self, mock_crud, mock_db_tasks, client, completion_feedback
    ):
        mock_user_id = str(uuid.uuid4())
        self.setup_redis_and_db(client, user_id=mock_user_id)
        mock_crud.get_meta_query_by_id.return_value = MagicMock(user_id=mock_user_id)
        mock_crud.get_generation_by_meta_query_and_model.return_value = MagicMock()

        response = client.post(
            "/api/completion/feedback/",
            json=completion_feedback.dict(),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["meta_query_id"] == str(completion_feedback.meta_query_id)
        assert data["model_id"] == completion_feedback.model_id

        mock_db_tasks.update_generation_task.apply_async.assert_called_once()
        mock_db_tasks.add_ground_truth_task.apply_async.assert_called_once()

    @patch("backend.routers.completion.feedback.db_tasks")
    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_success_without_ground_truth(
        self, mock_crud, mock_db_tasks, client, completion_feedback
    ):
        feedback = completion_feedback
        feedback.ground_truth = None
        mock_user_id = str(uuid.uuid4())
        self.setup_redis_and_db(client, user_id=mock_user_id)

        mock_crud.get_meta_query_by_id.return_value = MagicMock(user_id=mock_user_id)
        mock_crud.get_generation_by_meta_query_and_model.return_value = MagicMock()

        response = client.post("/api/completion/feedback/", json=feedback.dict())

        assert response.status_code == 200
        mock_db_tasks.update_generation_task.apply_async.assert_called_once()
        mock_db_tasks.add_ground_truth_task.apply_async.assert_not_called()

    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_query_not_found(self, mock_crud, client, completion_feedback):
        self.setup_redis_and_db(client)
        mock_crud.get_meta_query_by_id.return_value = None

        response = client.post(
            "/api/completion/feedback/", json=completion_feedback.dict()
        )
        assert response.status_code == 404
        assert response.json() == GenerationNotFoundError().dict()

    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_no_access(self, mock_crud, client, completion_feedback):
        self.setup_redis_and_db(client, user_id="wrong_user")

        mock_query = MagicMock(user_id="different_user")
        mock_crud.get_meta_query_by_id.return_value = mock_query

        response = client.post(
            "/api/completion/feedback/", json=completion_feedback.dict()
        )
        assert response.status_code == 403
        assert response.json() == NoAccessToProvideFeedbackError().dict()

    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_generation_missing(self, mock_crud, client, completion_feedback):
        mock_user_id = str(uuid.uuid4())
        self.setup_redis_and_db(client, user_id=mock_user_id)

        mock_crud.get_meta_query_by_id.return_value = MagicMock(user_id=mock_user_id)
        mock_crud.get_generation_by_meta_query_and_model.return_value = None

        response = client.post(
            "/api/completion/feedback/", json=completion_feedback.dict()
        )
        assert response.status_code == 404
        assert response.json() == GenerationNotFoundError().dict()

    def test_invalid_session_token(self, client, completion_feedback):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        client.mock_app.get_redis_manager.return_value = mock_redis

        response = client.post(
            "/api/completion/feedback/", json=completion_feedback.dict()
        )
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken().dict()

    @patch("backend.routers.completion.feedback.crud")
    def test_feedback_db_error(self, mock_crud, client, completion_feedback):
        mock_user_id = str(uuid.uuid4())
        mock_db, _ = self.setup_redis_and_db(client, user_id=mock_user_id)

        mock_crud.get_meta_query_by_id.side_effect = Exception("DB crash")

        response = client.post(
            "/api/completion/feedback/", json=completion_feedback.dict()
        )
        assert response.status_code == 500
        assert response.json() == FeedbackRecordingError().dict()

        mock_db.rollback.assert_called_once()

    def test_feedback_invalid_payload(self, client):
        invalid = {
            "meta_query_id": "not-uuid",
            "model_id": "abc",
            "was_accepted": "nope",
        }
        response = client.post("/api/completion/feedback/", json=invalid)
        assert response.status_code == 422
