from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.main import app
from backend.Responses import (
    InvalidSessionToken,
    MultiFileContextUpdateError,
)
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
            client.cookies.set("session_token", "valid_token")
            yield client

    @pytest.fixture(scope="function")
    def update_request(self):
        return UpdateMultiFileContext.fake()

    def test_update_success(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        mock_session = MagicMock()
        mock_existing_context = {"old.py": "old code"}
        mock_session.get_session.return_value = {
            "data": {"context": mock_existing_context}
        }

        client.mock_app.get_session_manager.return_value = mock_session
        client.mock_app.get_db_session.return_value = MagicMock()

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )
        assert response.status_code == 200
        data = response.json()["data"]
        for file, content in (
            update_request.context_updates | mock_existing_context
        ).items():
            assert data[file] == content

    def test_update_invalid_session(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        mock_session = MagicMock()
        mock_session.get_session.return_value = None

        client.mock_app.get_session_manager.return_value = mock_session

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )
        assert response.status_code == 401
        assert response.json() == InvalidSessionToken().dict()

    def test_update_internal_error(
        self, client: TestClient, update_request: UpdateMultiFileContext
    ):
        mock_session = MagicMock()
        mock_session.get_session.side_effect = Exception("Internal error")

        client.mock_app.get_session_manager.return_value = mock_session

        response = client.post(
            "/api/completion/multi-file-context/update/", json=update_request.dict()
        )
        assert response.status_code == 500
        assert response.json() == MultiFileContextUpdateError().dict()
