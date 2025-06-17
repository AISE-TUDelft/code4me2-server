import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi import WebSocket, WebSocketDisconnect

# Assuming the router is in a module called chat_websocket_router
from backend.routers.ws.chat import chat_websocket  # adjust import as needed


class TestChatWebSocket(unittest.TestCase):
    def setUp(self):
        # Mock WebSocket
        self.mock_websocket = AsyncMock(spec=WebSocket)

        # Mock App instance
        self.mock_app = MagicMock()
        self.mock_redis_manager = MagicMock()
        self.mock_celery_broker = MagicMock()

        self.mock_app.get_redis_manager.return_value = self.mock_redis_manager
        self.mock_app.get_celery_broker.return_value = self.mock_celery_broker

        # Mock connection ID
        self.mock_celery_broker.register_new_connection.return_value = "conn_123"

        # Default valid tokens and info
        self.session_token = "valid_session_token"
        self.project_token = "valid_project_token"

        self.session_info = {
            "auth_token": "valid_auth_token",
            "project_tokens": ["valid_project_token"],
        }

        self.auth_info = {"user_id": "user_123"}

        self.project_info = {"project_id": "project_123"}

    def test_websocket_accept_called(self):
        """Test that websocket.accept() is called"""

        async def run_test():
            # Mock Redis manager to return None for session (will cause early return)
            self.mock_redis_manager.get.return_value = None

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.accept.assert_called_once()

    def test_invalid_session_token(self):
        """Test handling of invalid session token"""

        async def run_test():
            # Mock Redis manager to return None for session token
            self.mock_redis_manager.get.return_value = None

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                "invalid_session",
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called_once()
            self.mock_websocket.close.assert_called_once()

    def test_missing_auth_token_in_session(self):
        """Test handling of missing auth token in session info"""

        async def run_test():
            # Mock session info without auth_token
            session_info_no_auth = {"project_tokens": ["valid_project_token"]}

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return session_info_no_auth
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

    def test_invalid_auth_token(self):
        """Test handling of invalid auth token"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return None  # Invalid auth token
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

    def test_missing_user_id_in_auth(self):
        """Test handling of missing user_id in auth info"""

        async def run_test():
            auth_info_no_user = {"some_other_field": "value"}

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return auth_info_no_user
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

    def test_invalid_project_token(self):
        """Test handling of invalid project token"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return None  # Invalid project token
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

    def test_project_not_in_session(self):
        """Test handling of project token not in session project list"""

        async def run_test():
            session_info_wrong_projects = {
                "auth_token": "valid_auth_token",
                "project_tokens": ["different_project_token"],
            }

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return session_info_wrong_projects
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

    def test_invalid_websocket_request(self):
        """Test handling of invalid websocket request"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            # Mock websocket receive to return invalid request then raise disconnect
            invalid_data = {"unknown_field": "value"}
            self.mock_websocket.receive_json.side_effect = [
                invalid_data,
                WebSocketDisconnect(),
            ]

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            # Should send error for invalid request
            self.mock_websocket.send_json.assert_called_with(
                {"error": "Invalid websocket request"}
            )

    def test_websocket_disconnect_handling(self):
        """Test handling of WebSocket disconnect"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            # Mock websocket receive to raise disconnect immediately
            self.mock_websocket.receive_json.side_effect = WebSocketDisconnect()

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            # Should unregister connection
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

    def test_general_exception_handling(self):
        """Test handling of general exceptions"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            # Mock websocket receive to raise general exception
            self.mock_websocket.receive_json.side_effect = Exception("Network error")

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            # Should send error message
            self.mock_websocket.send_json.assert_called_with({"error": "Network error"})
            # Should unregister connection
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

    def test_connection_registration_and_cleanup(self):
        """Test that connection is registered and cleaned up properly"""

        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get

            # Mock websocket receive to raise disconnect
            self.mock_websocket.receive_json.side_effect = WebSocketDisconnect()

            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            # Verify connection was registered
            self.mock_celery_broker.register_new_connection.assert_called_once_with(
                self.mock_websocket
            )
            # Verify connection was unregistered
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

    # Helper method to run async tests
    def run_async_test(self, test_func):
        """Helper to run async test functions"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(test_func())
        finally:
            loop.close()

    # Override test methods to use async helper
    def test_websocket_accept_called(self):
        async def run_test():
            self.mock_redis_manager.get.return_value = None
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.accept.assert_called_once()

        self.run_async_test(run_test)

    def test_invalid_session_token(self):
        async def run_test():
            self.mock_redis_manager.get.return_value = None
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                "invalid_session",
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called_once()
            self.mock_websocket.close.assert_called_once()

        self.run_async_test(run_test)

    def test_missing_auth_token_in_session(self):
        async def run_test():
            session_info_no_auth = {"project_tokens": ["valid_project_token"]}

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return session_info_no_auth
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    def test_invalid_auth_token(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return None
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    def test_missing_user_id_in_auth(self):
        async def run_test():
            auth_info_no_user = {"some_other_field": "value"}

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return auth_info_no_user
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    def test_invalid_project_token(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return None
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    def test_project_not_in_session(self):
        async def run_test():
            session_info_wrong_projects = {
                "auth_token": "valid_auth_token",
                "project_tokens": ["different_project_token"],
            }

            def mock_get(key_type, key):
                if key_type == "session_token":
                    return session_info_wrong_projects
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    def test_invalid_websocket_request(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            invalid_data = {"unknown_field": "value"}
            self.mock_websocket.receive_json.side_effect = [
                invalid_data,
                WebSocketDisconnect(),
            ]
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called_with(
                {"error": "Invalid websocket request"}
            )

        self.run_async_test(run_test)

    def test_websocket_disconnect_handling(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            self.mock_websocket.receive_json.side_effect = WebSocketDisconnect()
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

        self.run_async_test(run_test)

    def test_general_exception_handling(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            self.mock_websocket.receive_json.side_effect = Exception("Network error")
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called_with({"error": "Network error"})
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

        self.run_async_test(run_test)

    def test_connection_registration_and_cleanup(self):
        async def run_test():
            def mock_get(key_type, key):
                if key_type == "session_token":
                    return self.session_info
                elif key_type == "auth_token":
                    return self.auth_info
                elif key_type == "project_token":
                    return self.project_info
                return None

            self.mock_redis_manager.get.side_effect = mock_get
            self.mock_websocket.receive_json.side_effect = WebSocketDisconnect()
            await chat_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_celery_broker.register_new_connection.assert_called_once_with(
                self.mock_websocket
            )
            self.mock_celery_broker.unregister_connection.assert_called_once_with(
                "conn_123"
            )

        self.run_async_test(run_test)


if __name__ == "__main__":
    unittest.main()
