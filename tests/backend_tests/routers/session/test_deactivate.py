# from unittest.mock import MagicMock

# import pytest
# from fastapi.testclient import TestClient

# import Queries
# from App import App
# from backend.Responses import (
#     DeactivateSessionError,
#     DeactivateSessionPostResponse,
#     InvalidOrExpiredAuthToken,
#     InvalidOrExpiredSessionToken,
# )
# from main import app


# class TestDeactivateSession:

#     @pytest.fixture(scope="session")
#     def setup_app(self):
#         mock_app = MagicMock()
#         app.dependency_overrides[App.get_instance] = lambda: mock_app
#         return mock_app

#     @pytest.fixture(scope="function")
#     def client(self, setup_app):
#         with TestClient(app) as client:
#             client.mock_app = setup_app
#             client.cookies.set("session_token", "valid_token")
#             yield client

#     @pytest.fixture(scope="function")
#     def deactivate_session_query(self):
#         return Queries.DeactivateSession.fake()

#     def test_deactivate_session_success(self, client, deactivate_session_query):
#         fake_user_id = "user123"
#         session_token = deactivate_session_query.session_token
#         fake_session_info = {"user_id": fake_user_id, "data": {}}

#         mock_redis_manager = MagicMock()
#         mock_redis_manager.get_user_id_by_auth_token.return_value = fake_user_id
#         mock_redis_manager.get_session.return_value = fake_session_info

#         client.mock_app.get_redis_manager.return_value = mock_redis_manager
#         client.mock_app.get_db_session.return_value = MagicMock()

#         response = client.put(
#             "/api/session/deactivate",
#             json=deactivate_session_query.dict(),
#         )

#         assert response.status_code == 200
#         assert response.json() == DeactivateSessionPostResponse()

#     def test_deactivate_session_invalid_auth_token(
#         self, client, deactivate_session_query
#     ):
#         mock_redis_manager = MagicMock()
#         mock_redis_manager.get_user_id_by_auth_token.return_value = None

#         client.mock_app.get_redis_manager.return_value = mock_redis_manager

#         response = client.put(
#             "/api/session/deactivate",
#             json=deactivate_session_query.dict(),
#         )

#         assert response.status_code == 401
#         assert response.json() == InvalidOrExpiredAuthToken()

#     def test_deactivate_session_invalid_session_token(
#         self, client, deactivate_session_query
#     ):
#         fake_user_id = "user123"

#         mock_redis_manager = MagicMock()
#         mock_redis_manager.get_user_id_by_auth_token.return_value = fake_user_id
#         mock_redis_manager.get_session.return_value = None

#         client.mock_app.get_redis_manager.return_value = mock_redis_manager
#         client.mock_app.get_db_session.return_value = MagicMock()

#         response = client.put(
#             "/api/session/deactivate",
#             json=deactivate_session_query.dict(),
#         )

#         assert response.status_code == 401
#         assert response.json() == InvalidOrExpiredSessionToken()

#     def test_deactivate_session_internal_error(self, client, deactivate_session_query):
#         mock_redis_manager = MagicMock()
#         mock_redis_manager.get_user_id_by_auth_token.side_effect = Exception(
#             "Unexpected error"
#         )

#         client.mock_app.get_redis_manager.return_value = mock_redis_manager
#         client.mock_app.get_db_session.return_value = MagicMock()

#         response = client.put(
#             "/api/session/deactivate",
#             json=deactivate_session_query.dict(),
#         )

#         assert response.status_code == 500
#         assert response.json() == DeactivateSessionError()
