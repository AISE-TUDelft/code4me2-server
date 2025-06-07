import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from backend.Responses import (
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
)
from main import app  # Assuming your router is mounted on this app


class TestCompletionRequest:

    @pytest.fixture
    def client(self):
        with TestClient(app) as c:
            # Set some default cookies for tokens
            c.cookies.set("session_token", str(uuid.uuid4()))
            c.cookies.set("project_token", str(uuid.uuid4()))
            yield c

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

    def test_request_completion_success(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides = {}
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")

        session_data = {
            "auth_token": "auth-token-123",
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
        mock_app.get_db_session.return_value = MagicMock()

        mock_model_1 = MagicMock()
        mock_model_1.model_id = 1
        mock_model_1.model_name = "starcoder2-3b"
        mock_model_2 = MagicMock()
        mock_model_2.model_id = 2
        mock_model_2.model_name = "deepseek-1.3b"

        def get_model_by_id(db, model_id):
            if model_id == 1:
                return mock_model_1
            elif model_id == 2:
                return mock_model_2
            return None

        mock_completion_models = MagicMock()

        def get_model(model_name, prompt_template=None):
            mock_completion_model = MagicMock()
            mock_completion_model.invoke.return_value = {
                "completion": f"Completion from {model_name}",
                "generation_time": 123,
                "logprobs": [],
                "confidence": 0.9,
            }
            return mock_completion_model

        mock_completion_models.get_model.side_effect = get_model
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock()
        mock_config.thread_pool_max_workers = 2
        mock_config.server_version_id = "1.0.0"
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", side_effect=get_model_by_id), patch(
            "celery_app.tasks.db_tasks.add_generation_task.si"
        ) as mock_add_generation_task, patch(
            "celery_app.tasks.db_tasks.add_context_task.si"
        ) as mock_add_context_task, patch(
            "celery_app.tasks.db_tasks.add_telemetry_task.si"
        ) as mock_add_telemetry_task, patch(
            "celery_app.tasks.db_tasks.add_completion_query_task.si"
        ) as mock_add_completion_query_task, patch(
            "celery.chain"
        ) as mock_chain:

            mock_add_generation_task.return_value = MagicMock()
            mock_add_context_task.return_value = MagicMock()
            mock_add_telemetry_task.return_value = MagicMock()
            mock_add_completion_query_task.return_value = MagicMock()
            mock_chain.return_value.apply_async.return_value = None

            response = client.post(
                "/",
                json=completion_request.dict(),
                cookies={
                    "session_token": session_token,
                    "project_token": project_token,
                },
            )

        assert response.status_code == 200
        resp_json = response.json()
        assert "data" in resp_json
        completions = resp_json["data"]["completions"]
        assert len(completions) == 2
        assert all(isinstance(c, dict) for c in completions)
        assert completions[0]["completion"].startswith("Completion from ")
        assert "model_id" in completions[0]

    def test_request_completion_invalid_session_token(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post("/", json=completion_request.dict())

        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredSessionToken().message

    def test_request_completion_invalid_auth_token(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        session_data = {
            "auth_token": "invalid-auth-token",
            "project_tokens": [client.cookies.get("project_token")],
        }
        mock_redis = MagicMock()

        def side_effect(key, token):
            if key == "session_token":
                return session_data
            if key == "auth_token":
                return None
            return None

        mock_redis.get.side_effect = side_effect
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post("/", json=completion_request.dict())
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredAuthToken().message

    def test_request_completion_invalid_project_token(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")

        session_data = {"auth_token": "valid-auth-token", "project_tokens": []}
        auth_data = {"user_id": str(uuid.uuid4())}
        mock_redis = MagicMock()

        def side_effect(key, token):
            if key == "session_token":
                return session_data
            if key == "auth_token":
                return auth_data
            if key == "project_token":
                return None
            return None

        mock_redis.get.side_effect = side_effect
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post("/", json=completion_request.dict())
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredProjectToken().message

    def test_request_completion_model_not_found(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")

        session_data = {
            "auth_token": "auth-token",
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
        mock_app.get_db_session.return_value = MagicMock()

        def get_model_by_id(db, model_id):
            return None

        mock_completion_models = MagicMock()
        mock_completion_models.get_model.return_value = None
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock()
        mock_config.thread_pool_max_workers = 2
        mock_config.server_version_id = "1.0.0"
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", side_effect=get_model_by_id):
            response = client.post("/", json=completion_request.dict())

        assert response.status_code == 200
        resp_json = response.json()
        completions = resp_json["data"]["completions"]
        assert any(c.get("model_name", "").startswith("Model ID:") for c in completions)

    def test_request_completion_exception_rolls_back(self, client, completion_request):
        mock_app = MagicMock()
        client.app.dependency_overrides["App.get_instance"] = lambda: mock_app

        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")

        session_data = {
            "auth_token": "auth-token",
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

        def get_model_by_id(db, model_id):
            raise Exception("DB error")

        mock_completion_models = MagicMock()
        mock_app.get_completion_models.return_value = mock_completion_models

        mock_config = MagicMock()
        mock_config.thread_pool_max_workers = 2
        mock_config.server_version_id = "1.0.0"
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", side_effect=get_model_by_id):
            response = client.post("/", json=completion_request.dict())

        assert response.status_code == 200
        resp_json = response.json()
        assert "message" in resp_json["data"]
        mock_db_session.rollback.assert_called_once()
