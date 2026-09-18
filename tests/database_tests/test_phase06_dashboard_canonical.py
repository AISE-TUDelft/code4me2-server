"""Phase-06 real-PostgreSQL dashboard isolation and count fidelity.

Proves the canonical analytics path: counts equal persisted canonical facts,
per-run/per-context attribution is isolated (interleaved events for two owners
never mix), and ownership is enforced before the joins.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.analysis.read_models.dashboard import agent_overview

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="function")
def SessionFactory():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


def _user(db, *, email):
    user_id = uuid.uuid4()
    config_id = db.execute(
        text(
            "INSERT INTO config (config_data) VALUES ('{}') RETURNING config_id"
        )
    ).scalar()
    db.execute(
        text(
            """
            INSERT INTO "user" (user_id, email, name, password, is_admin, can_research,
                joined_at, verified, is_oauth_signup, config_id)
            VALUES (:user_id, :email, 'U', 'x', false, false, :joined_at, false, false,
                :config_id)
            """
        ),
        {"user_id": user_id, "email": email, "joined_at": NOW, "config_id": config_id},
    )
    return user_id


def _task(db, *, owner, run_id, created_at=NOW):
    task_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO agent_task (task_id, source, agent_profile, model,
                approval_policy, tools_json, next_event_index, status, created_at,
                external_run_id, owner_user_id)
            VALUES (:task_id, 'plugin', 'p', 'm', 'auto', '[]', 0, 'done',
                :created_at, :run_id, :owner)
            """
        ),
        {"task_id": task_id, "created_at": created_at, "run_id": run_id, "owner": owner},
    )
    return task_id


def _event(db, *, run_id, seq, kind, event_type, latency_ms, tokens, occurred_at):
    envelope = {
        "schema_version": "1",
        "payload": {"legacy_kind": kind, "tool_name": "read"} if kind else {},
        "metrics": {"latency_ms": latency_ms, "usage_tokens": tokens},
        "provenance": {"source": "relay"},
    }
    db.execute(
        text(
            """
            INSERT INTO research_event (event_id, schema_version, event_type, source,
                emitter_id, emitter_sequence, occurred_at, envelope_json, digest,
                accepted_at, retention_state, agent_run_id)
            VALUES (:event_id, '1', :event_type, 'relay', 'relay', :seq, :occurred_at,
                CAST(:envelope AS jsonb), :digest, :occurred_at, 'RETAINED', :run_id)
            """
        ),
        {
            "event_id": uuid.uuid4(),
            "event_type": event_type,
            "seq": seq,
            "occurred_at": occurred_at,
            "envelope": json.dumps(envelope),
            "digest": uuid.uuid4().hex + uuid.uuid4().hex,
            "run_id": run_id,
        },
    )


def test_counts_equal_persisted_facts_and_owners_are_isolated(SessionFactory):
    db = SessionFactory()
    owner_a = _user(db, email="a@example.com")
    owner_b = _user(db, email="b@example.com")
    try:
        _task(db, owner=owner_a, run_id="run-a")
        _task(db, owner=owner_b, run_id="run-b")
        # Interleaved canonical events for two contexts/runs.
        _event(db, run_id="run-a", seq=1, kind="model_call",
               event_type="agent.message.completed", latency_ms=100, tokens=10, occurred_at=NOW)
        _event(db, run_id="run-b", seq=1, kind="model_call",
               event_type="agent.message.completed", latency_ms=200, tokens=99, occurred_at=NOW)
        _event(db, run_id="run-a", seq=2, kind="tool_call",
               event_type="tool.completed", latency_ms=5, tokens=None, occurred_at=NOW)
        _event(db, run_id="run-b", seq=2, kind="tool_failed",
               event_type="tool.failed", latency_ms=6, tokens=None, occurred_at=NOW)
        db.commit()

        user_a = type("U", (), {"user_id": owner_a, "is_admin": False})()
        overview_a = agent_overview(db, user_a, time_window="7d", now=NOW)
        summary_a = overview_a["summary"]

        # Counts equal the persisted canonical facts for owner A's run only.
        assert summary_a["total_tasks"] == 1
        assert summary_a["model_calls"] == 1
        assert summary_a["tool_calls"] == 1
        assert summary_a["failures"] == 0
        assert summary_a["provider_input_tokens"] is None  # no prompt tokens persisted
        assert summary_a["event_tokens"] == 10
        # Owner A never sees owner B's tool failure.
        assert all(row["failures"] == 0 for row in overview_a["recent_runs"])
    finally:
        db.close()
