import uuid
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    DeleteChatError,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    NoAccessToGetQueryError,
    QueryNotFoundError,
)
from main import app
from response_models import DeleteChatSuccessResponse


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


class TestDeleteChatEndpoint:

    def test_missing_session_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": None
        }.get(key)
        chat_id = uuid.uuid4()
        client.cookies.set("session_token", None)
        response = client.delete(f"/api/chat/delete/{chat_id}")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_missing_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": None}
        }.get(key)

        chat_id = uuid.uuid4()
        response = client.delete(f"/api/chat/delete/{chat_id}")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken()

    def test_invalid_auth_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc"},
            "auth_token": None,
        }.get(key)
        chat_id = uuid.uuid4()
        response = client.delete(f"/api/chat/delete/{chat_id}")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_invalid_project_token(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc", "project_tokens": []},
            "auth_token": {"user_id": "user123"},
            "project_token": None,
        }.get(key)

        chat_id = uuid.uuid4()
        response = client.delete(f"/api/chat/delete/{chat_id}")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredProjectToken()

    def test_project_token_not_in_session(self, client):
        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {"auth_token": "abc", "project_tokens": ["other"]},
            "auth_token": {"user_id": "user123"},
            "project_token": {"id": "project123"},
        }.get(key)

        chat_id = uuid.uuid4()
        response = client.delete(f"/api/chat/delete/{chat_id}")
        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredProjectToken()

    def test_chat_not_found(self, client):
        project_token = str(uuid4())
        auth_token = str(uuid4())
        user_id = str(uuid4())

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": user_id},
            "project_token": {"id": project_token},
        }.get(key)

        with patch("database.crud.get_chat_by_id", return_value=None):
            chat_id = uuid.uuid4()
            client.cookies.set("project_token", project_token)
            response = client.delete(f"/api/chat/delete/{chat_id}")
            assert response.status_code == 404
            assert response.json() == QueryNotFoundError()

    def test_chat_access_forbidden(self, client):
        project_token = str(uuid4())
        auth_token = str(uuid4())
        user_id = str(uuid4())

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": user_id},
            "project_token": {"id": project_token},
        }.get(key)

        fake_chat = MagicMock(user_id=str(uuid4()), project_id=str(uuid4()))
        with patch("database.crud.get_chat_by_id", return_value=fake_chat):
            chat_id = uuid.uuid4()
            client.cookies.set("project_token", project_token)
            response = client.delete(f"/api/chat/delete/{chat_id}")
            assert response.status_code == 403
            assert response.json() == NoAccessToGetQueryError()

    def test_chat_delete_failure(self, client):
        project_token = str(uuid4())
        auth_token = str(uuid4())
        user_id = str(uuid4())

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": user_id},
            "project_token": {"id": project_token},
        }.get(key)

        fake_chat = MagicMock(user_id=user_id, project_id=project_token)
        with patch("database.crud.get_chat_by_id", return_value=fake_chat):
            with patch("database.crud.delete_chat_cascade", return_value=False):
                chat_id = uuid.uuid4()
                client.cookies.set("project_token", project_token)
                response = client.delete(f"/api/chat/delete/{chat_id}")
                assert response.status_code == 500
                assert response.json() == DeleteChatError()

    def test_successful_chat_delete(self, client):
        project_token = str(uuid4())
        auth_token = str(uuid4())
        user_id = str(uuid4())

        client.mock_app.get_redis_manager().get.side_effect = lambda key, token: {
            "session_token": {
                "auth_token": auth_token,
                "project_tokens": [project_token],
            },
            "auth_token": {"user_id": user_id},
            "project_token": {"id": project_token},
        }.get(key)

        fake_chat = MagicMock(user_id=user_id, project_id=project_token)
        with patch("database.crud.get_chat_by_id", return_value=fake_chat):
            with patch("database.crud.delete_chat_cascade", return_value=True):
                chat_id = uuid.uuid4()
                client.cookies.set("project_token", project_token)
                response = client.delete(f"/api/chat/delete/{chat_id}")
                assert response.status_code == 200
                assert response.json() == DeleteChatSuccessResponse()
