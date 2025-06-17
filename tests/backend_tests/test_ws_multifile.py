import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import WebSocket, WebSocketDisconnect

# Assuming the router is in a module called multi_file_context_websocket_router
from backend.routers.ws.multi_file_context import (
    multi_file_context_websocket,
)  # adjust import as needed


class TestMultiFileContextWebSocket(unittest.TestCase):
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

    # Helper method to run async tests
    def run_async_test(self, test_func):
        """Helper to run async test functions"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(test_func())
        finally:
            loop.close()

    def test_websocket_accept_called(self):
        async def run_test():
            self.mock_redis_manager.get.return_value = None
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )
            self.mock_websocket.send_json.assert_called()
            self.mock_websocket.close.assert_called()

        self.run_async_test(run_test)

    @patch(
        "backend.routers.ws.multi_file_context.llm_tasks.update_multi_file_context_task"
    )
    def test_multi_file_context_update_task_submission(self, mock_update_task):
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

            mock_task = MagicMock()
            mock_task.id = "task_123"
            mock_update_task.apply_async.return_value = mock_task

            update_data = {
                "multi_file_context_update": {
                    "files": ["file1.py", "file2.py"],
                    "context": "test context",
                }
            }
            self.mock_websocket.receive_json.side_effect = [
                update_data,
                WebSocketDisconnect(),
            ]

            await multi_file_context_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            mock_update_task.apply_async.assert_called_once()
            call_args = mock_update_task.apply_async.call_args
            self.assertEqual(call_args[1]["queue"], "llm")

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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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
            await multi_file_context_websocket(
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

    def test_multi_file_context_update_args_passed_correctly(self):
        """Test that multi-file context update args are passed correctly to Celery task"""

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

            with patch(
                "backend.routers.ws.multi_file_context.llm_tasks.update_multi_file_context_task"
            ) as mock_task:
                mock_task_result = MagicMock()
                mock_task_result.id = "task_789"
                mock_task.apply_async.return_value = mock_task_result

                update_data = {
                    "multi_file_context_update": {
                        "files": ["main.py", "utils.py"],
                        "action": "add",
                    }
                }
                self.mock_websocket.receive_json.side_effect = [
                    update_data,
                    WebSocketDisconnect(),
                ]

                await multi_file_context_websocket(
                    self.mock_websocket,
                    self.mock_app,
                    self.session_token,
                    self.project_token,
                )

                # Verify the task was called with correct arguments
                mock_task.apply_async.assert_called_once()
                call_args = mock_task.apply_async.call_args[1]["args"]
                self.assertEqual(call_args[0], "conn_123")  # connection_id
                self.assertEqual(call_args[1], self.session_token)
                self.assertEqual(call_args[2], self.project_token)
                self.assertEqual(call_args[3], update_data["multi_file_context_update"])

        self.run_async_test(run_test)

    def test_multiple_context_updates_same_connection(self):
        """Test handling multiple context updates on the same connection"""

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

            with patch(
                "backend.routers.ws.multi_file_context.llm_tasks.update_multi_file_context_task"
            ) as mock_task:
                mock_task.apply_async.return_value = MagicMock(id="multi_task")

                update_data1 = {"multi_file_context_update": {"files": ["file1.py"]}}
                update_data2 = {"multi_file_context_update": {"files": ["file2.py"]}}

                self.mock_websocket.receive_json.side_effect = [
                    update_data1,
                    update_data2,
                    WebSocketDisconnect(),
                ]

                await multi_file_context_websocket(
                    self.mock_websocket,
                    self.mock_app,
                    self.session_token,
                    self.project_token,
                )

                # Task should have been called twice
                self.assertEqual(mock_task.apply_async.call_count, 2)

        self.run_async_test(run_test)

    def test_mixed_valid_invalid_requests(self):
        """Test handling of mix of valid and invalid requests"""

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

            with patch(
                "backend.routers.ws.multi_file_context.llm_tasks.update_multi_file_context_task"
            ) as mock_task:
                mock_task.apply_async.return_value = MagicMock(id="mixed_task")

                valid_data = {"multi_file_context_update": {"files": ["test.py"]}}
                invalid_data = {"invalid_field": "value"}

                self.mock_websocket.receive_json.side_effect = [
                    valid_data,
                    invalid_data,
                    WebSocketDisconnect(),
                ]

                await multi_file_context_websocket(
                    self.mock_websocket,
                    self.mock_app,
                    self.session_token,
                    self.project_token,
                )

                # Task should have been called once for valid data
                mock_task.apply_async.assert_called_once()
                # Error should have been sent for invalid data
                self.mock_websocket.send_json.assert_called_with(
                    {"error": "Invalid websocket request"}
                )

        self.run_async_test(run_test)

    def test_null_multi_file_context_update(self):
        """Test handling when multi_file_context_update is null/None"""

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

            # Test with null value
            null_data = {"multi_file_context_update": None}
            self.mock_websocket.receive_json.side_effect = [
                null_data,
                WebSocketDisconnect(),
            ]

            await multi_file_context_websocket(
                self.mock_websocket,
                self.mock_app,
                self.session_token,
                self.project_token,
            )

            # Should send error for null data
            self.mock_websocket.send_json.assert_called_with(
                {"error": "Invalid websocket request"}
            )

        self.run_async_test(run_test)


if __name__ == "__main__":
    unittest.main()
