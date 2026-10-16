"""``RedisManager.revoke_user_tokens``: a deleted account keeps no live credential.

Runs against a small in-memory stand-in for the Redis client, so no Redis
server is needed and no database is touched (a real ``delete`` would flush
session state to the database; revocation must not).
"""

from __future__ import annotations

import fnmatch
import json
from unittest.mock import patch

import pytest

from backend.redis_manager import RedisManager


class InMemoryRedis:
    def __init__(self, *args, **kwargs):
        self.data: dict[str, str] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, keepttl=False):
        self.data[key] = value

    def setex(self, key, _seconds, value):
        self.data[key] = value

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def exists(self, key):
        return int(key in self.data)

    def scan_iter(self, match="*"):
        return [key for key in list(self.data) if fnmatch.fnmatchcase(key, match)]


@pytest.fixture
def manager():
    with patch("backend.redis_manager.Redis", InMemoryRedis):
        yield RedisManager(host="localhost", port=6379)


def _put(manager, key, value):
    manager._RedisManager__redis_client.data[key] = json.dumps(value)


def test_revoke_user_tokens_removes_only_that_accounts_credentials(manager):
    client = manager._RedisManager__redis_client
    _put(manager, "auth_token:web", {"user_id": "alice"})
    _put(manager, "auth_token:ide", {"user_id": "alice"})
    client.data["auth_token_hook:ide"] = ""
    _put(manager, "auth_token:other", {"user_id": "bob"})
    _put(manager, "acp_session:agent", {"user_id": "alice", "project_id": "p"})
    _put(manager, "password_reset:reset", {"user_id": "alice", "email": "a@example.org"})
    _put(manager, "user_token:alice", {"session_token": "s1"})
    _put(manager, "session_token:s1", {"user_token": "alice", "project_tokens": ["solo", "shared"]})
    client.data["session_token_hook:s1"] = ""
    _put(manager, "project_token:solo", {"session_tokens": ["s1"], "multi_file_contexts": {"a.py": ["x"]}})
    _put(manager, "project_token:shared", {"session_tokens": ["s1", "s2"], "multi_file_contexts": {}})
    client.data["context_erased:solo"] = "1"

    with patch("backend.redis_manager.crud") as crud:
        manager.revoke_user_tokens("alice")

    assert sorted(client.data) == ["auth_token:other", "project_token:shared"]
    assert json.loads(client.data["project_token:shared"])["session_tokens"] == ["s2"]
    assert not crud.method_calls


def test_revoke_user_tokens_without_a_live_session(manager):
    _put(manager, "auth_token:web", {"user_id": "alice"})

    manager.revoke_user_tokens("alice")

    assert manager._RedisManager__redis_client.data == {}
