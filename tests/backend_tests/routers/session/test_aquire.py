from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    AcquireSessionError,
    AcquireSessionGetResponse,
    InvalidOrExpiredAuthToken,
)
from main import app


class TestAcquireSession:

    @pytest.fixture(scope="session")
    def setup_app(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        return mock_app

    @pytest.fixture(scope="function")
    def client(self, setup_app):
        with TestClient(app) as client:
            client.mock_app = setup_app
            client.cookies.set("auth_token", "valid_token")
            yield client

    def test_acquire_session_success(self, client):
        fake_user_id = "123e4567-e89b-12d3-a456-426614174000"
        fake_session_token = "abc-session"

        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = lambda key, value: (
            {"user_id": fake_user_id, "session_token": None}
            if key == "auth_token"
            else None
        )

        mock_redis_manager.set.return_value = None

        mock_config = MagicMock()
        mock_config.session_token_expires_in_seconds = 3600

        mock_db_session = MagicMock()

        client.mock_app.get_redis_manager.return_value = mock_redis_manager
        client.mock_app.get_config.return_value = mock_config
        client.mock_app.get_db_session.return_value = mock_db_session

        with patch(
            "backend.routers.session.acquire.crud.create_session"
        ) as create_mock, patch(
            "backend.routers.session.acquire.create_uuid",
            return_value=fake_session_token,
        ):
            response = client.get("/api/session/acquire")  # adjust URL as needed

        assert response.status_code == 200
        assert response.cookies.get("session_token") == fake_session_token
        assert response.json() == AcquireSessionGetResponse(
            session_token=fake_session_token
        )

    def test_acquire_session_invalid_token(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.return_value = None

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.get("/api/session/acquire")

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredAuthToken()

    def test_acquire_session_internal_error(self, client):
        mock_redis_manager = MagicMock()
        mock_redis_manager.get.side_effect = Exception("Redis failure")

        client.mock_app.get_redis_manager.return_value = mock_redis_manager

        response = client.get("/api/session/acquire")

        assert response.status_code == 500
        assert response.json() == AcquireSessionError()


class TestAcquireReusesOnlyLiveSessions:
    """The plugin acquires a session at startup and when a call is refused; a
    session recorded in the database but expired in Redis fails every project
    call, so it must not be handed out again."""

    USER_ID = "123e4567-e89b-12d3-a456-426614174000"
    SESSION = "11111111-1111-1111-1111-111111111111"

    @pytest.fixture
    def client(self):
        mock_app = MagicMock()
        app.dependency_overrides[App.get_instance] = lambda: mock_app
        try:
            with TestClient(app) as client:
                client.mock_app = mock_app
                client.cookies.set("auth_token", "valid_token")
                yield client
        finally:
            app.dependency_overrides.pop(App.get_instance, None)

    def _redis(self, client, entries):
        redis_manager = MagicMock()
        redis_manager.get.side_effect = lambda kind, key: entries.get((kind, key))
        redis_manager.touch.side_effect = lambda kind, key: (kind, key) in entries
        client.mock_app.get_redis_manager.return_value = redis_manager
        client.mock_app.get_config.return_value = MagicMock(session_token_expires_in_seconds=3600)
        return redis_manager

    def test_a_live_session_is_reused_and_kept_alive(self, client):
        redis_manager = self._redis(
            client,
            {
                ("auth_token", "valid_token"): {"user_id": self.USER_ID},
                ("user_token", self.USER_ID): {"session_token": self.SESSION},
                ("session_token", self.SESSION): {"user_token": self.USER_ID, "project_tokens": ["p1"]},
            },
        )
        with patch("backend.routers.session.acquire.crud") as crud:
            crud.get_session_by_id.return_value = object()
            response = client.get("/api/session/acquire")

        assert response.status_code == 200
        assert response.json() == AcquireSessionGetResponse(session_token=self.SESSION)
        crud.create_session.assert_not_called()
        # Expiry restarted for both, values never rewritten (no lost update).
        redis_manager.touch.assert_any_call("session_token", self.SESSION)
        redis_manager.touch.assert_any_call("user_token", self.USER_ID)
        redis_manager.set.assert_not_called()

    def test_a_session_expired_in_redis_is_replaced(self, client):
        fresh = "22222222-2222-2222-2222-222222222222"
        redis_manager = self._redis(
            client,
            {
                ("auth_token", "valid_token"): {"user_id": self.USER_ID},
                ("user_token", self.USER_ID): {"session_token": self.SESSION},
            },
        )
        with patch("backend.routers.session.acquire.crud") as crud, patch(
            "backend.routers.session.acquire.create_uuid", return_value=fresh
        ):
            crud.get_session_by_id.return_value = object()  # the database row remains
            response = client.get("/api/session/acquire")

        assert response.status_code == 200
        assert response.json() == AcquireSessionGetResponse(session_token=fresh)
        crud.create_session.assert_called_once()
        # The session is stored before the account's link names it.
        assert [c.args[:2] for c in redis_manager.set.call_args_list] == [
            ("session_token", fresh),
            ("user_token", self.USER_ID),
        ]
