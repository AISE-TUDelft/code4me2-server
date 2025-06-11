import uuid
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    QueryNotFoundError,
)
from main import app
from response_models import ChatMessageRole


@pytest.fixture(scope="session")
def setup_app():
    mock_app = MagicMock()
    app.dependency_overrides[App.get_instance] = lambda: mock_app
    return mock_app


@pytest.fixture(scope="function")
def client(setup_app):
    with TestClient(app) as client:
        client.mock_app = setup_app
        client.cookies.set("session_token", str(uuid4()))
        client.cookies.set("project_token", str(uuid4()))
        yield client


class TestChatHistoryEndpoint:

    def test_missing_session_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": None
        }.get(key)
        client.cookies.set("session_token", None)
        response = client.get("/api/chat/get/1")
        assert response.status_code == 401
        print(response.json())
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_missing_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": None}
        }.get(key)

        response = client.get("/api/chat/get/1")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_invalid_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc"},
            "auth_token": None,
        }.get(key)

        response = client.get("/api/chat/get/1")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_invalid_project_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc", "project_tokens": []},
            "auth_token": {"user_id": "user123"},
            "project_token": None,
        }.get(key)

        response = client.get("/api/chat/get/1")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredProjectToken()

    def test_project_token_not_in_session(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc", "project_tokens": ["some_other"]},
            "auth_token": {"user_id": "user123"},
            "project_token": {"id": "proj"},
        }.get(key)

        response = client.get("/api/chat/get/1")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredProjectToken()

    def test_query_not_found(self, client):
        auth_token = str(uuid.uuid4())
        project_token = str(uuid.uuid4())
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": auth_token},
            "project_token": {"id": project_token},
        }.get(key)

        with patch("database.crud.get_project_chat_history", return_value=None):
            client.cookies.set("project_token", project_token)
            response = client.get("/api/chat/get/1")
            print(response.json())
            assert response.status_code == 404
            assert response.json() == QueryNotFoundError()

    def test_successful_chat_history(self, client):
        auth_token = str(uuid4())
        project_token = str(uuid.uuid4())
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": uuid4()},
            "project_token": {"id": project_token},
        }.get(key)

        fake_model = MagicMock()
        fake_model.model_name = "GPT-4"

        chat = MagicMock(chat_id=uuid4(), title="Test Chat")
        meta_query = MagicMock(meta_query_id=uuid4(), timestamp="2024-01-01T00:00:00Z")
        context = MagicMock(prefix="Hello, world!")
        generation = MagicMock(
            model_id=1,
            completion="Hi there!",
            generation_time=100,
            confidence=0.9,
            was_accepted=True,
        )

        with patch(
            "database.crud.get_project_chat_history",
            return_value=[(chat, [(meta_query, context, [generation])])],
        ):
            with patch("database.crud.get_model_by_id", return_value=fake_model):
                client.cookies.set("project_token", project_token)
                response = client.get("/api/chat/get/1")

                assert response.status_code == 200
                result = response.json()
                assert result["per_page"] == 10
                assert result["page"] == 1
                assert len(result["items"]) == 1

                history = result["items"][0]["history"]
                assert len(history) == 1
                assert history[0]["user_message"]["content"] == "Hello, world!"
                assert history[0]["user_message"]["role"] == ChatMessageRole.USER.value
                assert history[0]["assistant_responses"][0]["completion"] == "Hi there!"
