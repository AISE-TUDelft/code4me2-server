import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from App import App
from backend.Responses import CompletionPostResponse, QueryNotFoundError
from main import app


class TestCompletionGet:
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
    def valid_user_id(self):
        return str(uuid.uuid4())

    @pytest.fixture(scope="function")
    def query_id(self):
        return str(uuid.uuid4())

    @pytest.fixture(scope="function")
    def mock_generations_and_models(self, query_id):
        model1 = MagicMock(model_id=1, model_name="deepseek-1.3b")
        model2 = MagicMock(model_id=2, model_name="starcoder2-3b")

        gen1 = MagicMock(
            query_id=query_id,
            model_id=1,
            completion="completion from deepseek",
            confidence=0.9,
            generation_time=100,
        )
        gen2 = MagicMock(
            query_id=query_id,
            model_id=2,
            completion="completion from starcoder",
            confidence=0.9,
            generation_time=150,
        )

        return {"generations": [gen1, gen2], "models": {1: model1, 2: model2}}

    def test_get_completions_success(
        self, client, query_id, mock_generations_and_models
    ):
        mock_query = MagicMock()
        mock_query.user_id = "test_id"

        mock_app = client.mock_app
        mock_db = MagicMock()
        mock_app.get_db_session.return_value = mock_db

        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}
        mock_app.get_redis_manager.return_value = {
            "session_token": {"auth_token": "auth123"},
            "auth_token": {"user_id": "test_id"},
        }

        with patch(
            "backend.routers.completion.get.App.get_instance", return_value=mock_app
        ), patch.multiple(
            "backend.routers.completion.get.crud",
            get_meta_query_by_id=MagicMock(return_value=mock_query),
            get_generations_by_meta_query_id=MagicMock(
                return_value=mock_generations_and_models["generations"]
            ),
            get_model_by_id=MagicMock(
                side_effect=lambda db, mid: mock_generations_and_models["models"].get(
                    mid
                )
            ),
        ):
            response = client.get(f"/api/completion/{query_id}")
            assert response.status_code == 200

            data = response.json()
            assert (
                data["message"]
                == CompletionPostResponse.model_fields["message"].default
            )
            assert data["data"]["meta_query_id"] == query_id
            assert len(data["data"]["completions"]) == 2
            assert data["data"]["completions"][0]["model_name"] == "deepseek-1.3b"
            assert data["data"]["completions"][1]["model_name"] == "starcoder2-3b"

    def test_get_completions_query_not_found(self, client, query_id):
        mock_app = client.mock_app
        mock_db = MagicMock()
        mock_app.get_db_session.return_value = mock_db

        mock_session = MagicMock()
        mock_session.get_session.return_value = {"user_id": "test_id"}
        mock_app.get_redis_manager.return_value = {
            "session_token": {"auth_token": "auth123"},
            "auth_token": {"user_id": "test_id"},
        }

        with patch(
            "backend.routers.completion.get.App.get_instance", return_value=mock_app
        ), patch(
            "backend.routers.completion.get.crud.get_meta_query_by_id",
            return_value=None,
        ):
            response = client.get(f"/api/completion/{query_id}")
            assert response.status_code == 404
            assert response.json() == QueryNotFoundError().dict()
