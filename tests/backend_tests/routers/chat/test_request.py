import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.Responses import (
    GenerateChatCompletionsError,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
)
from main import app


class TestChatCompletionRequest:

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
    def chat_completion_request(self):
        return Queries.RequestChatCompletion.fake(model_ids=[1, 2])

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

    def test_request_chat_completion_success(self, client, chat_completion_request):
        mock_app = client.mock_app
        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")
        user_id = str(uuid.uuid4())
        session_data = {"user_token": user_id, "project_tokens": [project_token]}
        auth_data = {"user_id": str(uuid.uuid4())}
        project_data = {"chat_histories": {}}

        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis
        mock_app.get_db_session.return_value = MagicMock()

        mock_model_1 = MagicMock(model_id=1, model_name="chat-1")
        mock_model_2 = MagicMock(model_id=2, model_name="chat-2")

        def get_model_by_id(db, model_id):
            return {1: mock_model_1, 2: mock_model_2}.get(model_id)

        def get_model(model_name, prompt_template=None):
            mock_chat_model = MagicMock()
            mock_chat_model.invoke.return_value = {
                "completion": f"Chat from {model_name}",
                "generation_time": 50,
                "logprobs": [],
                "confidence": 0.85,
            }
            return mock_chat_model

        mock_chat_models = MagicMock()
        mock_chat_models.get_model.side_effect = get_model
        mock_app.get_chat_models.return_value = mock_chat_models
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
                "/api/chat/request",
                json=chat_completion_request.dict(),
            )

        assert response.status_code == 200

    def test_invalid_session_token(self, client, chat_completion_request):
        mock_app = client.mock_app
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post("/api/chat/request", json=chat_completion_request.dict())
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredSessionToken().message

    def test_invalid_project_token(self, client, chat_completion_request):
        mock_app = client.mock_app
        user_id = str(uuid.uuid4())
        session_data = {
            "user_token": user_id,
            "project_tokens": [],
        }
        auth_data = {"user_id": user_id}

        mock_redis = MagicMock()
        mock_redis.get.side_effect = lambda key, _: {
            "session_token": session_data,
            "auth_token": auth_data,
        }.get(key, None)
        mock_app.get_redis_manager.return_value = mock_redis

        response = client.post("/api/chat/request", json=chat_completion_request.dict())
        assert response.status_code == 401
        assert response.json()["message"] == InvalidOrExpiredProjectToken().message

    def test_model_not_found(self, client, chat_completion_request):
        mock_app = client.mock_app
        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")
        user_id = str(uuid.uuid4())
        session_data = {"user_token": user_id, "project_tokens": [project_token]}
        auth_data = {"user_id": user_id}
        project_data = {"chat_histories": {}}

        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis
        mock_app.get_db_session.return_value = MagicMock()

        mock_app.get_chat_models.return_value = MagicMock(
            get_model=MagicMock(return_value=None)
        )
        mock_config = MagicMock(thread_pool_max_workers=2, server_version_id=1)
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", return_value=None):
            response = client.post(
                "/api/chat/request", json=chat_completion_request.dict()
            )

        assert response.status_code == 200

    def test_exception_rolls_back(self, client, chat_completion_request):
        mock_app = client.mock_app
        session_token = client.cookies.get("session_token")
        project_token = client.cookies.get("project_token")
        user_id = str(uuid.uuid4())

        session_data = {"user_token": user_id, "project_tokens": [project_token]}
        auth_data = {"user_id": user_id}
        project_data = {"chat_histories": {}}

        mock_redis = MagicMock()
        mock_redis.get.side_effect = self.mock_redis_get_factory(
            session_data=session_data, auth_data=auth_data, project_data=project_data
        )
        mock_app.get_redis_manager.return_value = mock_redis

        mock_db_session = MagicMock()
        mock_app.get_db_session.return_value = mock_db_session

        mock_app.get_chat_models.return_value = MagicMock()
        mock_config = MagicMock(thread_pool_max_workers=2, server_version_id=1)
        mock_app.get_config.return_value = mock_config

        with patch("database.crud.get_model_by_id", side_effect=Exception("DB error")):
            response = client.post(
                "/api/chat/request", json=chat_completion_request.dict()
            )

        assert response.status_code == 500
        assert response.json()["message"] == GenerateChatCompletionsError().message
        mock_db_session.rollback.assert_called_once()
