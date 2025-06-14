import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.celery_broker import CeleryBroker


@pytest.fixture
def broker():
    with patch("redis.Redis") as mock_redis:
        mock_redis_instance = mock_redis.return_value
        mock_redis_instance.ping.return_value = True
        mock_redis_instance.pubsub.return_value = MagicMock()
        return CeleryBroker(host="localhost", port=6379)


def test_publish_message_str(broker):
    broker.redis_client.publish = MagicMock()
    broker.publish_message("channel", "hello")
    broker.redis_client.publish.assert_called_once_with("channel", "hello")


def test_publish_message_dict(broker):
    broker.redis_client.publish = MagicMock()
    msg = {"connection_id": "abc", "result": {"msg": "done"}}
    broker.publish_message("channel", msg)
    broker.redis_client.publish.assert_called_once_with("channel", json.dumps(msg))


def test_cleanup(broker):
    broker._cancel_pubsub_tasks = MagicMock()
    broker.pubsubs_registered = True
    broker.redis_client.close = MagicMock()

    broker.cleanup()

    broker._cancel_pubsub_tasks.assert_called_once()
    broker.redis_client.close.assert_called_once()


# @pytest.mark.asyncio
# async def test__handle_message_success(broker):
#     ws_mock = AsyncMock()
#     connection_id = str(uuid.uuid4())
#     broker.active_connections[connection_id] = ws_mock
#
#     result = {"data": "test"}
#     await broker._CeleryBroker__handle_message(
#         json.dumps({"connection_id": connection_id, "result": result})
#     )
#
#     ws_mock.send_json.assert_awaited_once_with(result)


# @pytest.mark.asyncio
# async def test__handle_message_failure(broker):
#     # Should not raise exception even with bad JSON
#     await broker._CeleryBroker__handle_message("bad json")


# @pytest.mark.asyncio
# async def test__handle_pubsub_cancelled(broker):
#     with patch("redis.Redis") as mock_redis:
#         mock_pubsub = MagicMock()
#         mock_pubsub.listen.return_value = iter([])  # Simulate no messages
#         mock_pubsub.get_message.return_value = {
#             "type": "subscribe",
#             "channel": "completion_request_channel",
#         }
#
#         broker.redis_client.pubsub.return_value = mock_pubsub
#
#         task = asyncio.create_task(
#             broker._CeleryBroker__handle_pubsub("completion_request_channel")
#         )
#         await asyncio.sleep(0.1)
#         task.cancel()
#         try:
#             await task
#         except asyncio.CancelledError:
#             pass
#

# @pytest.mark.asyncio
# async def test_register_and_unregister_connection(broker):
#     ws_mock = MagicMock()
#     with patch("backend.celery_broker.asyncio.create_task") as mock_task:
#         mock_task.return_value = MagicMock()
#         conn_id = broker.register_new_connection(ws_mock)
#         assert conn_id in broker.active_connections
#
#         broker.unregister_connection(conn_id)
#         assert conn_id not in broker.active_connections
