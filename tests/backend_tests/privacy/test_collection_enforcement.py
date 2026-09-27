"""The server enforces an opt-out on every collection path, whatever clients send.

* Classic completion and chat requests still answer an opted-out account but
  enqueue no persistence at all; a collecting account gets its one chain as before.
* Agent content is denied at the content-policy authority, research-bound or not.
* A project's code context is never flushed while a member has opted out.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import Queries
from App import App
from backend.routers.agent.consent import resolve_content_policy_for_user
from main import app
from privacy import collection
from research.telemetry.content_policy import DENY_DATA_COLLECTION_OPTED_OUT

from ..research._ui_overhaul_seed import seed_account


@pytest.fixture
def mock_app():
    runtime = MagicMock()
    previous = app.dependency_overrides.get(App.get_instance)
    app.dependency_overrides[App.get_instance] = lambda: runtime
    try:
        yield runtime
    finally:
        if previous is None:
            app.dependency_overrides.pop(App.get_instance, None)
        else:
            app.dependency_overrides[App.get_instance] = previous


def _completion_request():
    request = Queries.RequestCompletion.fake()
    request.context.file_name = "main.py"
    request.model_ids = [1]
    request.store_context = True
    request.store_contextual_telemetry = True
    request.store_behavioral_telemetry = True
    return request


def _chat_request():
    request = Queries.RequestChatCompletion.fake(model_ids=[1])
    request.store_context = True
    request.store_contextual_telemetry = True
    request.store_behavioral_telemetry = True
    return request


CLASSIC_PATHS = [
    ("/api/completion/request", "backend.routers.completion.request", _completion_request),
    ("/api/chat/request", "backend.routers.chat.request", _chat_request),
]


@pytest.mark.parametrize("path, module, make_request", CLASSIC_PATHS)
@pytest.mark.parametrize("allowed", [True, False])
def test_classic_requests_persist_only_while_collection_is_allowed(
    mock_app, path, module, make_request, allowed
):
    user_id = str(uuid.uuid4())
    session_token, project_token = str(uuid.uuid4()), str(uuid.uuid4())
    session_info = {"user_token": user_id, "project_tokens": [project_token]}
    project_info = {"multi_file_contexts": {}, "multi_file_context_changes": {}}
    redis = MagicMock()
    redis.get.side_effect = lambda key, _token: {
        "session_token": session_info,
        "project_token": project_info,
    }.get(key)
    mock_app.get_redis_manager.return_value = redis
    mock_app.get_db_session.return_value = MagicMock()
    model = MagicMock()
    model.invoke.return_value = {
        "completion": "x = 1", "generation_time": 5, "logprobs": [], "confidence": 0.5,
    }
    models = MagicMock()
    models.get_model.return_value = model
    mock_app.get_completion_models.return_value = models
    mock_app.get_chat_models.return_value = models
    mock_app.get_config.return_value = MagicMock(thread_pool_max_workers=2, server_version_id=1)

    with patch(f"{module}.collection.is_collection_allowed", return_value=allowed) as gate, patch(
        f"{module}.chain"
    ) as chain, patch(f"{module}.db_tasks"), patch(
        f"{module}.crud.get_model_by_id", return_value=MagicMock(model_id=1, model_name="model")
    ), TestClient(app) as client:
        client.cookies.set("session_token", session_token)
        client.cookies.set("project_token", project_token)
        response = client.post(path, json=make_request().dict())

    assert response.status_code == 200, response.text
    assert gate.call_args.args[1] == uuid.UUID(user_id)
    assert chain.return_value.apply_async.call_count == (1 if allowed else 0)


def test_agent_content_is_denied_once_the_account_opts_out(session_factory):
    with session_factory() as db:
        user_id = seed_account(db, "agent-user@example.org")
        assert resolve_content_policy_for_user(db, user_id).allowed

        collection.opt_out(db, user_id)
        db.commit()

        for study_id in (None, uuid.uuid4()):
            decision = resolve_content_policy_for_user(db, user_id, study_id=study_id)
            assert (decision.allowed, decision.reason) == (False, DENY_DATA_COLLECTION_OPTED_OUT)


@pytest.mark.parametrize(
    "opted_out_at, preference, allowed",
    [
        (None, json.dumps({"store_context": True}), True),
        (None, json.dumps({"store_context": False}), False),
        (None, None, False),
        (None, "not json", False),
        ("2026-09-27T00:00:00Z", json.dumps({"store_context": True}), False),
    ],
)
def test_context_storage_needs_consent_and_no_opt_out(opted_out_at, preference, allowed):
    member = SimpleNamespace(data_collection_opted_out_at=opted_out_at, preference=preference)
    assert collection.allows_context_storage(member) is allowed


@pytest.mark.parametrize("member_opted_out", [False, True])
def test_project_context_is_not_flushed_while_a_member_has_opted_out(member_opted_out):
    from backend.redis_manager import RedisManager

    from .test_redis_revocation import InMemoryRedis

    with patch("backend.redis_manager.Redis", InMemoryRedis):
        manager = RedisManager(host="localhost", port=6379)
    project_token = str(uuid.uuid4())
    member_id = uuid.uuid4()
    manager._RedisManager__redis_client.data[f"project_token:{project_token}"] = json.dumps(
        {"session_tokens": [], "multi_file_contexts": {"a.py": ["x"]}, "multi_file_context_changes": {}}
    )

    with patch("backend.redis_manager.crud") as crud, patch(
        "backend.redis_manager.lock_context_storage_allowed", return_value=not member_opted_out
    ) as consent:
        crud.get_project_users.return_value = [SimpleNamespace(user_id=member_id)]
        manager.delete("project_token", project_token, MagicMock())

    assert consent.call_args.args[1] == member_id
    assert crud.update_project.called is (not member_opted_out)


def test_agent_memory_is_not_stored_once_the_account_opts_out(session_factory):
    from dataclasses import dataclass

    from sqlalchemy import text

    from backend.acp_authorization import AcpSessionAuthorization
    from backend.routers.agent.acp_auth import require_acp_scope

    @dataclass
    class Runtime:
        factory: object

        def get_db_session(self):
            return self.factory()

    with session_factory() as db:
        user_id = seed_account(db, "agent-memory@example.org")
    scope = AcpSessionAuthorization(
        acp_token="token", user_id=str(user_id), project_id=str(uuid.uuid4()), workspace="/work"
    )
    overrides = {App.get_instance: lambda: Runtime(session_factory), require_acp_scope: lambda: scope}
    previous = {key: app.dependency_overrides.get(key) for key in overrides}
    app.dependency_overrides.update(overrides)
    snapshot = {"messages": [{"role": "user", "content": "refactor my secret code"}]}

    def stored(session_id):
        with session_factory() as db:
            return db.execute(
                text("SELECT count(*) FROM public.agent_memory WHERE session_id = :s"), {"s": session_id}
            ).scalar_one()

    try:
        with TestClient(app) as client:
            before = client.put("/api/agent/memory/collecting-session", json=snapshot)
            with session_factory() as db:
                collection.opt_out(db, user_id)
                db.commit()
            after = client.put("/api/agent/memory/opted-out-session", json=snapshot)
    finally:
        for key, value in previous.items():
            if value is None:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value

    assert before.status_code == 200 and stored("collecting-session") == 1
    assert after.status_code == 200
    assert after.json()["stored"] is False and after.json()["message_count"] == 1
    assert stored("opted-out-session") == 0


def test_an_erase_marks_the_live_project_context_so_it_is_never_flushed():
    from backend.redis_manager import RedisManager

    from .test_redis_revocation import InMemoryRedis

    with patch("backend.redis_manager.Redis", InMemoryRedis):
        manager = RedisManager(host="localhost", port=6379)
    data = manager._RedisManager__redis_client.data
    project_token = str(uuid.uuid4())
    data["user_token:alice"] = json.dumps({"session_token": "s1"})
    data["session_token:s1"] = json.dumps({"user_token": "alice", "project_tokens": [project_token]})
    data[f"project_token:{project_token}"] = json.dumps(
        {"session_tokens": [], "multi_file_contexts": {"a.py": ["pre-erase code"]}}
    )

    manager.mark_context_erased("alice")
    # A context update after the erase rewrites the project entry; the marker survives it.
    manager.set("project_token", project_token, {"session_tokens": [], "multi_file_contexts": {"a.py": ["y"]}})
    with patch("backend.redis_manager.crud") as crud, patch(
        "backend.redis_manager.lock_context_storage_allowed", return_value=True
    ):
        crud.get_project_users.return_value = [SimpleNamespace(user_id=uuid.uuid4())]
        manager.delete("project_token", project_token, MagicMock())

    crud.update_project.assert_not_called()
    assert f"project_token:{project_token}" not in data
    assert f"context_erased:{project_token}" not in data
