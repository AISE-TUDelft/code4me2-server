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
