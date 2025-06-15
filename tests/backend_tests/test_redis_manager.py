import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.redis_manager import RedisManager  # adjust import path


@pytest.fixture
def redis_manager():
    # Patch Redis so no real Redis is needed
    with patch("backend.redis_manager.Redis") as mock_redis_cls:
        mock_redis = MagicMock()
        mock_redis_cls.return_value = mock_redis

        # Make ping() succeed
        mock_redis.ping.return_value = True
        # get returns dummy JSON string
        mock_redis.get.return_value = '{"session_token": "sess123", "project_tokens": [], "auth_token": "auth123"}'
        # keys returns dummy keys
        mock_redis.keys.return_value = [
            "auth_token:auth123",
            "session_token:sess123",
            "project_token:proj123",
        ]
        yield RedisManager(host="localhost", port=6379)


def test_get_exp_and_reset_exp(redis_manager):
    assert (
        redis_manager._RedisManager__get_exp("auth_token")
        == redis_manager.auth_token_expires_in_seconds
    )
    assert (
        redis_manager._RedisManager__get_exp("session_token")
        == redis_manager.session_token_expires_in_seconds
    )
    assert redis_manager._RedisManager__get_exp("project_token") == -1
    assert redis_manager._RedisManager__get_exp("email_verification") == 86400
    assert redis_manager._RedisManager__get_exp("unknown_type") == 3600

    assert redis_manager._RedisManager__get_reset_exp("session_token") is True
    assert redis_manager._RedisManager__get_reset_exp("auth_token") is False

    assert redis_manager._RedisManager__get_set_hook("session_token") is True
    assert redis_manager._RedisManager__get_set_hook("auth_token") is True
    assert redis_manager._RedisManager__get_set_hook("project_token") is False


def test_set_and_get(redis_manager):
    # Test set with force_reset_exp True
    redis_manager.set("session_token", "token123", {"foo": "bar"}, force_reset_exp=True)
    redis_manager.set("auth_token", "token123", {"foo": "bar"})

    # Test get with reset_exp True and False
    data = redis_manager.get("session_token", "token123", reset_exp=True)
    assert isinstance(data, dict)

    data_none = redis_manager.get("session_token", "", reset_exp=True)
    assert data_none is None
