import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    GenerateCompletionsError,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
)
from main import app


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
            client.cookies.set("session_token", str(uuid.uuid4()))
            client.cookies.set("project_token", str(uuid.uuid4()))
            yield client

    @pytest.fixture
    def completion_request(self):
        return Queries.RequestCompletion.fake(model_ids=[1, 2])

    def mock_redis_get_factory(
        self, session_data=None, auth_data=None, project_data=None
    ):
        def _mock_get(key, token):
            if key == "session_token":
                return session_data
            if key == "auth_token":
                return auth_data
            if key == "project_token":
                return project_data
            return None

        return _mock_get

    @pytest.fixture
    def completion_request(self):
        """Create a fake completion request with deterministic values for reliable testing."""
        # Generate a base fake request
        fake_request = Queries.RequestCompletion.fake()

        # Override critical fields that might be None to ensure consistent behavior
        fake_request.context.file_name = (
            "test_file.py"  # Ensure file_name is never None
        )
        fake_request.model_ids = [1, 2]  # Ensure we have valid model IDs
        fake_request.store_context = True
        fake_request.store_contextual_telemetry = True
        fake_request.store_behavioral_telemetry = False

        # Ensure contextual_telemetry has valid required fields
        if fake_request.contextual_telemetry:
            fake_request.contextual_telemetry.version_id = 6
            fake_request.contextual_telemetry.trigger_type_id = 7
            fake_request.contextual_telemetry.language_id = 6
            fake_request.contextual_telemetry.document_char_length = 1

        # Ensure behavioral_telemetry has valid fields
        if fake_request.behavioral_telemetry:
            fake_request.behavioral_telemetry.time_since_last_shown = 2
            fake_request.behavioral_telemetry.time_since_last_accepted = 2

        return fake_request

    def test_request_completion_success(self, client, completion_request):
        mock_app = client.mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")
        user_id = str(uuid.uuid4())
        session_data = {
            "user_token": user_id,
            "project_tokens": [project_token],
        }
        auth_data = {"user_id": user_id}
        project_data = {
            "multi_file_contexts": {},
            "multi_file_context_changes": {},
        }

        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis
        mock_app.get_db_session.return_value = MagicMock()

        mock_model_1 = MagicMock(model_id=1, model_name="starcoder2-3b")
        mock_model_2 = MagicMock(model_id=2, model_name="deepseek-1.3b")

        def get_model_by_id(db, model_id):
            return {1: mock_model_1, 2: mock_model_2}.get(model_id, None)

        def get_model(model_name, prompt_templates=None, model_parameters=None, meta_data=None):
            mock_completion_model = MagicMock()
            mock_completion_model.invoke.return_value = {
                "completion": f"Completion from {model_name}",
                "generation_time": 123,
                "logprobs": [],
                "confidence": 0.9,
            }
            return mock_completion_model

        mock_completion_models = MagicMock()
        mock_completion_models.get_model.side_effect = get_model
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock(thread_pool_max_workers=2, server_version_id=1)
        mock_app.get_config.return_value = mock_config

        with patch.multiple(
            "database.crud",
            get_model_by_id=patch(
                "database.crud.get_model_by_id", side_effect=get_model_by_id
            ).start(),
        ), patch(
            "celery_app.tasks.db_tasks",
        ), patch(
            "celery.chain"
        ) as mock_chain:
            mock_chain.return_value.apply_async.return_value = None

            response = client.post(
                "/api/completion/request",
                json=completion_request.dict(),
            )

        assert response.status_code == 200
        completions = response.json()["data"]["completions"]
        assert len(completions) == 2
        assert all("completion" in c and "model_id" in c for c in completions)

    def test_request_completion_invalid_session_token(self, client, completion_request):
        mock_app = client.mock_app
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post(
            "/api/completion/request", json=completion_request.dict()
        )
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredSessionToken().message

    def test_request_completion_invalid_project_token(self, client, completion_request):
        mock_app = client.mock_app

        session_data = {"user_token": "user_token", "project_tokens": []}
        auth_data = {"user_id": str(uuid.uuid4())}
        mock_redis = MagicMock()

        mock_redis.get.side_effect = lambda key, _: {
            "session_token": session_data,
            "auth_token": auth_data,
        }.get(key, None)
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post(
            "/api/completion/request", json=completion_request.dict()
        )
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredProjectToken().message

    def test_request_completion_model_not_found(self, client, completion_request):
        mock_app = client.mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")
        user_id = str(uuid.uuid4())
        session_data = {
            "user_token": user_id,
            "project_tokens": [project_token],
        }
        auth_data = {"user_id": user_id}
        project_data = {
            "multi_file_contexts": {},
            "multi_file_context_changes": {},
        }
        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis
        mock_app.get_db_session.return_value = MagicMock()

        mock_completion_models = MagicMock()
        mock_completion_models.get_model.return_value = None
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock(thread_pool_max_workers=2, server_version_id=1)
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", return_value=None):
            response = client.post(
                "/api/completion/request", json=completion_request.dict()
            )

        assert response.status_code == 200
        completions = response.json()["data"]["completions"]
        assert any(c.get("model_name", "").startswith("Model ID:") for c in completions)

    def test_request_completion_exception_rolls_back(self, client, completion_request):
        mock_app = client.mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")

        session_data = {
            "user_token": "user-token",
            "project_tokens": [project_token],
        }
        auth_data = {"user_id": str(uuid.uuid4())}
        project_data = {
            "multi_file_contexts": {},
            "multi_file_context_changes": {},
        }
        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis

        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session

        mock_completion_models = MagicMock()
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock(thread_pool_max_workers=2, server_version_id=1)
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", side_effect=Exception("DB error")):
            response = client.post(
                "/api/completion/request", json=completion_request.dict()
            )

        assert response.status_code == 500
        assert response.json()["message"] == GenerateCompletionsError().message
        mock_db_session.rollback.assert_called_once()
