from unittest.mock import MagicMock, call, patch

import pytest

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


class _MemoryRedis:
    """Just enough of a Redis client to follow which keys delete() removes."""

    def __init__(self, *args, **kwargs):
        self.data = {}
        self.ttl = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, keepttl=False):
        self.data[key] = value

    def setex(self, key, seconds, value):
        self.data[key] = value
        self.ttl[key] = seconds

    def expire(self, key, seconds):
        if key not in self.data:
            return False
        self.ttl[key] = seconds
        return True

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def exists(self, key):
        return key in self.data


OLD = "11111111-1111-1111-1111-111111111111"
NEW = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def memory_manager():
    with patch("backend.redis_manager.Redis", _MemoryRedis):
        yield RedisManager(host="localhost", port=6379)


def test_an_old_session_expiring_keeps_the_accounts_newer_session(memory_manager):
    memory_manager.set("session_token", OLD, {"user_token": "user-1", "project_tokens": []})
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": []})
    memory_manager.set("user_token", "user-1", {"session_token": NEW})

    with patch("backend.redis_manager.crud"):
        memory_manager.delete("session_token", OLD, MagicMock())

    assert memory_manager.get("session_token", OLD) is None
    assert memory_manager.get("user_token", "user-1") == {"session_token": NEW}
    assert memory_manager.get("session_token", NEW) is not None


def test_the_current_session_expiring_ends_the_accounts_link(memory_manager):
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": []})
    memory_manager.set("user_token", "user-1", {"session_token": NEW})

    with patch("backend.redis_manager.crud"):
        memory_manager.delete("session_token", NEW, MagicMock())

    assert memory_manager.get("user_token", "user-1") is None


def test_app_shutdown_only_closes_redis(monkeypatch):
    # Logins and sessions live in Redis and outlive the process; a shutdown that
    # flushed them signed every user out (each worker of a multi-worker server).
    from App import App

    monkeypatch.setenv("TEST_MODE", "false")  # the production path

    instance = object.__new__(App)  # App.__new__ would hand back the live singleton
    redis_manager = MagicMock()
    instance._App__redis_manager = redis_manager
    instance._App__celery_broker = MagicMock()
    instance._App__db_session_factory = MagicMock()

    instance.cleanup()

    assert redis_manager.method_calls == [call.close()]


def test_using_a_session_keeps_the_accounts_link_to_it_alive(memory_manager):
    # Project activation rewrites the session and agent calls touch it; the link
    # alone resolves the session for those calls, so it must not expire first.
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": []})
    memory_manager.set("user_token", "user-1", {"session_token": NEW})
    client = memory_manager._RedisManager__redis_client
    client.ttl["user_token:user-1"] = 5  # about to expire

    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": ["p1"]})
    assert client.ttl["user_token:user-1"] == memory_manager.session_token_expires_in_seconds

    client.ttl["user_token:user-1"] = 5
    memory_manager.get("session_token", NEW, reset_exp=True)
    assert client.ttl["user_token:user-1"] == memory_manager.session_token_expires_in_seconds

    # A link that already names another session is left alone.
    memory_manager.set("user_token", "user-1", {"session_token": OLD})
    client.ttl["user_token:user-1"] = 5
    memory_manager.get("session_token", NEW, reset_exp=True)
    assert client.ttl["user_token:user-1"] == 5


def test_touch_restarts_expiry_without_rewriting(memory_manager):
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": ["p1"]})
    client = memory_manager._RedisManager__redis_client
    client.ttl[f"session_token:{NEW}"] = 5

    assert memory_manager.touch("session_token", NEW) is True
    assert client.ttl[f"session_token:{NEW}"] == memory_manager.session_token_expires_in_seconds
    assert memory_manager.get("session_token", NEW) == {"user_token": "user-1", "project_tokens": ["p1"]}
    assert memory_manager.touch("session_token", OLD) is False  # never recreated


def test_a_session_whose_expiry_was_missed_does_not_keep_its_project_open(memory_manager):
    # OLD expired while no listener was subscribed: its key is gone but the
    # project still lists it. When NEW, the last live session, ends, the project
    # is closed (context written, key removed) instead of staying open for good.
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": [PROJECT]})
    memory_manager.set("project_token", PROJECT, {"session_tokens": [OLD, NEW]})

    with patch("backend.redis_manager.crud"):
        memory_manager.delete("session_token", NEW, MagicMock())

    assert memory_manager.get("project_token", PROJECT) is None


def test_touch_never_extends_a_session_whose_expiry_hook_is_gone(memory_manager):
    # The hook expires first; its listener then ends the session. A session in
    # that window (or whose hook event was missed) is ending, not reusable.
    memory_manager.set("session_token", NEW, {"user_token": "user-1", "project_tokens": []})
    client = memory_manager._RedisManager__redis_client
    client.delete(f"session_token_hook:{NEW}")

    assert memory_manager.touch("session_token", NEW) is False
