"""Storage tasks re-check the opt-out when they run, not only when enqueued.

A request accepted just before an erase commits must not land afterwards. The
decision lives in ``privacy.collection.discard_if_opted_out`` (tested here
in-process against PostgreSQL). The Celery tasks that act on it are replaced by
mocks for the whole backend suite (``tests/backend_tests/conftest.py``), so their
wiring runs in a fresh interpreter, the way the OpenAPI snapshot test runs its
exporter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from privacy import collection

from ..research._ui_overhaul_seed import seed_account
from .conftest import TEST_DB_URL

REPO_ROOT = Path(__file__).resolve().parents[3]

# Runs the real storage tasks against the test database and reports, per task,
# whether it returned or stopped the chain (``Ignore``, Celery's "stop here"
# signal) and whether it took the account-row lock before deciding.
_TASK_RUNNER = """
import json, sys
from unittest.mock import patch
from celery.exceptions import Ignore
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from celery_app.tasks import db_tasks
from privacy import collection

factory = sessionmaker(bind=create_engine(sys.argv[1]))

class Runtime:
    def get_db_session(self):
        return factory()

locks = []
real_lock = collection.lock_collection_allowed

def recording_lock(db, user_id):
    locks.append(str(user_id))
    return real_lock(db, user_id)

results = []
with patch.object(db_tasks.App, "get_instance", return_value=Runtime()), patch.object(
    collection, "lock_collection_allowed", recording_lock
):
    for task_name, args in json.loads(sys.argv[2]):
        before = len(locks)
        try:
            getattr(db_tasks, task_name)(*args)
            outcome = "returned"
        except Ignore:
            outcome = "stopped"
        results.append([outcome, len(locks) > before])
print(json.dumps(results))
"""


@pytest.fixture
def request_rows(session_factory):
    ids = {name: uuid.uuid4() for name in ("session", "project", "context", "contextual", "behavioral", "chat")}
    with session_factory() as db:
        user_id = seed_account(db, "writer@example.org")
        params = {**ids, "user_id": user_id}
        for statement in (
            "INSERT INTO public.session (session_id, user_id, start_time) VALUES (:session, :user_id, now())",
            "INSERT INTO public.project (project_id, project_name, created_at) VALUES (:project, 'demo', now())",
            "INSERT INTO public.context (context_id, prefix) VALUES (:context, 'secret = ')",
            "INSERT INTO public.contextual_telemetry (contextual_telemetry_id, version_id, trigger_type_id, "
            "language_id) VALUES (:contextual, 1, 1, 1)",
            "INSERT INTO public.behavioral_telemetry (behavioral_telemetry_id) VALUES (:behavioral)",
        ):
            db.execute(text(statement), params)
        db.commit()
    return session_factory, user_id, ids


def _counts(session_factory, ids):
    tables = (
        ("context", "context_id", "context"),
        ("contextual_telemetry", "contextual_telemetry_id", "contextual"),
        ("behavioral_telemetry", "behavioral_telemetry_id", "behavioral"),
        ("chat", "chat_id", "chat"),
        ("meta_query", "session_id", "session"),
    )
    with session_factory() as db:
        return {
            table: db.execute(
                text(f"SELECT count(*) FROM public.{table} WHERE {column} = :id"), {"id": ids[key]}
            ).scalar_one()
            for table, column, key in tables
        }


def _opt_out(session_factory, user_id):
    with session_factory() as db:
        collection.opt_out(db, user_id)
        db.commit()


def _discard(session_factory, user_id, ids):
    with session_factory() as db:
        discarded = collection.discard_if_opted_out(
            db,
            user_id,
            context_id=ids["context"],
            contextual_telemetry_id=ids["contextual"],
            behavioral_telemetry_id=ids["behavioral"],
        )
        db.commit()
    return discarded


def test_a_collecting_account_keeps_its_request_rows(request_rows):
    session_factory, user_id, ids = request_rows

    assert _discard(session_factory, user_id, ids) is False
    assert _counts(session_factory, ids) == {
        "context": 1, "contextual_telemetry": 1, "behavioral_telemetry": 1, "chat": 0, "meta_query": 0,
    }


def test_a_request_accepted_before_the_opt_out_is_discarded(request_rows):
    session_factory, user_id, ids = request_rows
    _opt_out(session_factory, user_id)

    assert _discard(session_factory, user_id, ids) is True
    assert set(_counts(session_factory, ids).values()) == {0}


def _query_fields(user_id, ids):
    return {
        "user_id": str(user_id),
        "contextual_telemetry_id": str(ids["contextual"]),
        "behavioral_telemetry_id": str(ids["behavioral"]),
        "context_id": str(ids["context"]),
        "session_id": str(ids["session"]),
        "project_id": str(ids["project"]),
        "multi_file_context_changes_indexes": {},
        "total_serving_time": 1,
    }


def _run_tasks(calls):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "TEST_MODE": "true"}
    result = subprocess.run(
        [sys.executable, "-c", _TASK_RUNNER, TEST_DB_URL, json.dumps(calls)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_storage_tasks_stop_for_an_account_that_opted_out_while_queued(request_rows):
    session_factory, user_id, ids = request_rows
    _opt_out(session_factory, user_id)
    chat = {"user_id": str(user_id), "project_id": str(ids["project"]), "title": "Help me"}
    chat_query = {**_query_fields(user_id, ids), "chat_id": str(ids["chat"])}

    results = _run_tasks([
        ["get_or_create_chat_task", [chat, str(ids["chat"])]],
        ["add_chat_query_task", [chat_query, str(uuid.uuid4())]],
        ["add_completion_query_task", [_query_fields(user_id, ids), str(uuid.uuid4())]],
    ])

    # The chat task returns without writing; the query tasks stop the chain.
    # Each decided under the account-row lock.
    assert results == [["returned", True], ["stopped", True], ["stopped", True]]
    assert set(_counts(session_factory, ids).values()) == {0}


def test_the_storage_tasks_store_a_collecting_account_as_before(request_rows):
    session_factory, user_id, ids = request_rows
    chat = {"user_id": str(user_id), "project_id": str(ids["project"]), "title": "Help me"}

    query_id = str(uuid.uuid4())

    results = _run_tasks([
        ["get_or_create_chat_task", [chat, str(ids["chat"])]],
        ["add_completion_query_task", [_query_fields(user_id, ids), query_id]],
        ["add_generation_task", [_generation(), query_id]],
        ["add_ground_truth_task", [{"completion_query_id": query_id, "ground_truth": "x = 2"}]],
    ])

    assert results == [["returned", True]] * 4
    counts = _counts(session_factory, ids)
    assert counts["chat"] == 1 and counts["meta_query"] == 1
    assert counts["context"] == counts["contextual_telemetry"] == counts["behavioral_telemetry"] == 1
    assert _feedback_rows(session_factory, query_id) == (1, 1)


def test_generations_and_feedback_are_not_stored_after_an_opt_out(request_rows):
    session_factory, user_id, ids = request_rows
    query_id = str(uuid.uuid4())
    # A query stored while the account still collected, answered after it opted out.
    _run_tasks([["add_completion_query_task", [_query_fields(user_id, ids), query_id]]])
    _opt_out(session_factory, user_id)

    results = _run_tasks([
        ["add_generation_task", [_generation(), query_id]],
        ["update_generation_task", [query_id, 1, {"was_accepted": True}]],
        ["add_ground_truth_task", [{"completion_query_id": query_id, "ground_truth": "x = 2"}]],
    ])

    assert results == [["returned", True]] * 3
    assert _feedback_rows(session_factory, query_id) == (0, 0)


def _generation():
    return {
        "model_id": 1,
        "completion": "x = 1",
        "generation_time": 10,
        "shown_at": ["2026-09-27T00:00:00+00:00"],
        "was_accepted": False,
        "confidence": 0.5,
        "logprobs": [0.1],
    }


def _feedback_rows(session_factory, query_id):
    with session_factory() as db:
        generations = db.execute(
            text("SELECT count(*) FROM public.had_generation WHERE meta_query_id = :q"), {"q": query_id}
        ).scalar_one()
        truths = db.execute(
            text("SELECT count(*) FROM public.ground_truth WHERE completion_query_id = :q"), {"q": query_id}
        ).scalar_one()
    return generations, truths
