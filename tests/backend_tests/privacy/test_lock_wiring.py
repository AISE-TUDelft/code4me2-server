"""The writers that can race an erase take the account-row lock where it counts.

``test_serialization.py`` proves what the locks guarantee; these check that each
writer actually uses them, so a revert to an unlocked read fails here. The agent
task sites lock right after the assignment lookup (which may commit on first
use) and before the reads that decide the insert.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.acp_authorization import AcpServerAuthorization, AcpSessionAuthorization
from backend.routers import acp as acp_router
from backend.routers import agents as agents_router
from backend.routers.agent import ingest as ingest_router
from backend.routers.agent import memory as memory_router
from privacy import collection


class _StopAfterLock(Exception):
    """Ends the handler at the lock: what follows it is not under test here."""


@pytest.fixture
def order():
    """Record the assignment lookup and the account lock, in call order."""
    calls = []
    assignment = SimpleNamespace(profile=SimpleNamespace(name="arm"), study_id=None)

    def resolve(db, user_id):
        calls.append(("assign", user_id))
        return assignment

    def lock(db, user_id):
        calls.append(("lock", user_id))
        raise _StopAfterLock()

    with patch.object(collection, "lock_account", side_effect=lock), patch(
        "agents.registry.resolve_assignment_context", side_effect=resolve
    ):
        yield calls


def _run(handler):
    try:
        handler()
    except (_StopAfterLock, HTTPException):
        pass


def _decides(order, name):
    """A decision read that must only run after the lock (it never runs here)."""
    return lambda *args, **kwargs: order.append((name, None))


def test_the_plugin_task_endpoint_locks_the_account_before_deciding(order):
    user_id = uuid.uuid4()
    with patch.object(
        agents_router.crud, "get_session_by_id", return_value=SimpleNamespace(user_id=user_id)
    ), patch.object(
        agents_router.access, "resolve_research_binding", side_effect=_decides(order, "binding")
    ), patch.object(
        agents_router, "resolve_store_agent_content", side_effect=_decides(order, "consent")
    ):
        _run(lambda: agents_router.create_agent_task(
            agents_router.TaskCreateRequest(), app=MagicMock(), session_id=uuid.uuid4()
        ))

    assert order == [("assign", user_id), ("lock", user_id)]


def test_self_reported_runs_lock_the_account_before_deciding(order):
    user_id = uuid.uuid4()
    scope = AcpSessionAuthorization(
        acp_token="token", user_id=str(user_id), project_id=str(uuid.uuid4()), workspace="/work"
    )
    run = ingest_router.AgentRunEnvelope(run_id="run-1", session_id="agent-session", status="running")
    with patch.object(
        ingest_router.crud, "get_agent_task_by_external_run_id", return_value=None
    ), patch.object(
        ingest_router, "resolve_research_binding", side_effect=_decides(order, "binding")
    ), patch.object(
        ingest_router, "resolve_store_agent_content_for_acp", side_effect=_decides(order, "consent")
    ):
        _run(lambda: ingest_router._resolve_or_create_task(MagicMock(), run=run, scope=scope))

    assert order == [("assign", user_id), ("lock", user_id)]


def test_managed_runs_lock_the_account_before_deciding(order):
    user_id = uuid.uuid4()
    scope = AcpServerAuthorization(
        acp_token="token",
        user_id=str(user_id),
        session_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        project_info={},
        workspace="/work",
    )
    with patch.object(acp_router.crud, "get_agent_task_by_external_run_id", return_value=None):
        _run(lambda: acp_router.create_managed_run(
            acp_router.ManagedRunRequest(run_id="run-1", session_id="agent-session"),
            app=MagicMock(),
            scope=scope,
        ))

    assert order == [("assign", user_id), ("lock", user_id)]


def test_agent_memory_saves_check_the_opt_out_under_the_lock():
    user_id = uuid.uuid4()
    scope = AcpSessionAuthorization(
        acp_token="token", user_id=str(user_id), project_id=str(uuid.uuid4()), workspace="/work"
    )
    snapshot = memory_router.AgentMemorySnapshot(messages=[{"role": "user", "content": "secret"}])
    with patch.object(collection, "lock_collection_allowed", return_value=False) as locked, patch.object(
        memory_router.crud, "upsert_agent_memory"
    ) as upsert:
        response = memory_router.upsert_agent_memory(
            "agent-session", snapshot, app=MagicMock(), scope=scope
        )

    assert response.status_code == 200
    assert locked.call_args.args[1] == user_id
    upsert.assert_not_called()
