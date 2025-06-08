from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import (
    InvalidOrExpiredSessionToken,
    MultiFileContextUpdateError,
)
from main import app
from Queries import UpdateMultiFileContext


class TestUpdateMultiFileContext:

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
    def update_request(self):
        return UpdateMultiFileContext.fake(
            context_updates={
                "file1.py": [
                    {
                        "change_type": "insert",
                        "start_line": 0,
                        "end_line": 0,
                        "new_lines": ["import numpy as np", "np.array([1,2,3])"],
                    },
                    {
                        "change_type": "update",
                        "start_line": 0,
                        "end_line": 1,
                        "new_lines": ["import numpy as npp"],
                    },
                ]
            }
        )

    def test_update_success(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        redis_mock = MagicMock()
        client.mock_app.get_redis_manager.return_value = redis_mock

        redis_mock.get.side_effect = lambda key, token: {
            ("session_token", "valid_session_token"): {"auth_token": "auth123"},
            ("auth_token", "auth123"): {"user_id": "user123"},
            ("project_token", "valid_project_token"): {
                "multi_file_contexts": {"old.py": ["old line"]},
                "multi_file_context_changes": {},
            },
        }.get((key, token))

        redis_mock.set.return_value = True

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert "file1.py" in data
        assert isinstance(data["file1.py"], list)

    def test_update_invalid_session(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        redis_mock = MagicMock()
        client.mock_app.get_redis_manager.return_value = redis_mock

        # No session returned
        redis_mock.get.side_effect = lambda key, token: (
            None if key == "session_token" else {}
        )

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )

        assert response.status_code == 401
        assert response.json() == InvalidOrExpiredSessionToken().dict()

    def test_update_internal_error(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        redis_mock = MagicMock()
        client.mock_app.get_redis_manager.return_value = redis_mock

        redis_mock.get.side_effect = Exception("Redis failure")

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )

        assert response.status_code == 500
        assert response.json() == MultiFileContextUpdateError().dict()
