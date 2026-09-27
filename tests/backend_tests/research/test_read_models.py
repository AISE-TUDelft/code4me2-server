"""Study-owner-scoped participant coverage read model over real HTTP.

Covers ``GET /api/research/operations/participants/coverage``: the owner and an
administrator receive 200 with study-local participant rows (frozen assignment
facts, session/event counts), a non-owner receives 403, an unknown study 404,
two studies with events never leak into each other, retention tombstones are
excluded, and the payload carries no account/participant/email identity. A
regression assertion keeps the personal dashboard's ``owner_user_id`` scope
unchanged.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.migration.migration_manager import MigrationManager
from main import app
from research.analysis.read_models.dashboard import agent_overview
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import CanonicalEventV1, Coverage, Provenance

from ._byoa_contract import BYOA_CONFIG_BINDINGS

VALID_SESSION_POLICY = {
    "idle_timeout_seconds": 600,
    "resume_grace_seconds": 120,
    "heartbeat_seconds": 30,
}

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@dataclass
class RuntimeApp:
    session_factory: sessionmaker

    def get_db_session(self):
        return self.session_factory()


@pytest.fixture()
def http_runtime():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    runtime = RuntimeApp(session_factory)
    current_user = {"value": None}

    def authenticated_user():
        return current_user["value"]

    app.dependency_overrides[App.get_instance] = lambda: runtime
    app.dependency_overrides[get_current_user] = authenticated_user
    try:
        with TestClient(app) as client:
            yield client, session_factory, current_user
    finally:
        app.dependency_overrides.pop(App.get_instance, None)
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _seed_user(session, email: str, *, can_research: bool = False) -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            'INSERT INTO public."user" '
            "(user_id, joined_at, email, name, password, config_id, verified, can_research) "
            "VALUES (:user_id, :joined_at, :email, :name, 'x', :config_id, true, :can_research)"
        ),
        {
            "user_id": user_id,
            "joined_at": datetime.now(timezone.utc),
            "email": email,
            "name": email.split("@", 1)[0],
            "config_id": config_id,
            "can_research": can_research,
        },
    )
    session.commit()
    return user_id


def _profile(session, owner_id: uuid.UUID) -> uuid.UUID:
    profile_id = uuid.uuid4()
    release_id = f"coverage-release-{uuid.uuid4()}"
    artifact_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    adapter_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, "
            "created_at) "
            "VALUES (:release_id, 'coverage-agent', :source_digest, 'QUALIFIED', "
            "CAST(:release_json AS JSONB) || jsonb_build_object('tests', CAST(:tests AS JSONB)), now())"
        ),
        {
            "release_id": release_id,
            "source_digest": artifact_digest,
            "release_json": json.dumps(
                {
                    "schema_version": "1",
                    "agent_id": "coverage-agent",
                    "release_id": release_id,
                    "version": "1.0.0",
                    "source_manifest_digest": artifact_digest,
                    "distribution_mode": "BYOA_EXTERNAL",
                    "agent_command": "coverage-agent",
                    "agent_package": "coverage-agent",
                    "byoa_config": list(BYOA_CONFIG_BINDINGS),
                    "artifacts": [],
                    "adapter": {
                        "adapter_id": "coverage-adapter",
                        "version": "1.0.0",
                        "digest": adapter_digest,
                    },
                }
            ),
            "tests": json.dumps(
                [{"os": "macos", "arch": "arm64", "self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}]
            ),
        },
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, :name, 'model', 'codex', :release_id, '[]', 'auto', 1)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "release_id": release_id,
            "name": f"coverage-profile-{profile_id.hex[:8]}",
        },
    )
    session.commit()
    return profile_id


def _owner(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="coverage-owner@example.com",
        name="Coverage Owner",
        can_research=True,
    )


def _participant(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="coverage-participant@example.com",
        name="Coverage Participant",
    )


def _admin(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=True,
        email="coverage-admin@example.com",
        name="Coverage Admin",
        can_research=True,
    )


def _create_study(client, profile_id: uuid.UUID, name: str) -> dict:
    created = client.post(
        "/api/research/studies",
        json={
            "name": name,
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["study"]


def _join(client, join_code: str) -> str:
    joined = client.post(
        "/api/research/join",
        json={"join_code": join_code, "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    return joined.json()["enrollment_id"]


def _seed_session(
    session_factory,
    *,
    study_id: str,
    enrollment_id: str,
    state: str,
    last_activity_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
) -> str:
    research_session_id = uuid.uuid4()
    terminal = state in {"ended", "revoked"}
    session = session_factory()
    try:
        session.execute(
            text(
                "INSERT INTO public.research_session "
                "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
                "environment_json, transitions_json, created_at, closed_at, close_reason) "
                "VALUES (:session_id, :enrollment_id, :study_id, :context_id, :state, "
                "'manifest', '{}', '[]', now(), :closed_at, :close_reason)"
            ),
            {
                "session_id": research_session_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "context_id": f"coverage-ctx-{research_session_id.hex[:8]}",
                "state": state,
                "closed_at": datetime.now(timezone.utc) if terminal else None,
                "close_reason": "explicit_completion" if terminal else None,
            },
        )
        if last_activity_at is not None or last_heartbeat_at is not None:
            session.execute(
                text(
                    "UPDATE public.research_session "
                    "SET last_activity_at = :last_activity_at, "
                    "last_heartbeat_at = :last_heartbeat_at "
                    "WHERE session_id = :session_id"
                ),
                {
                    "session_id": research_session_id,
                    "last_activity_at": last_activity_at,
                    "last_heartbeat_at": last_heartbeat_at,
                },
            )
        session.commit()
    finally:
        session.close()
    return str(research_session_id)


def _seed_event(
    session_factory,
    *,
    study_id: str,
    enrollment_id: str,
    research_session_id: str,
    event_type: str,
    source: str,
    occurred_at: datetime,
    emitter_id: str,
    emitter_sequence: int = 1,
    retention_state: str = "RETAINED",
) -> None:
    session = session_factory()
    try:
        event = CanonicalEventV1(
            event_id=uuid.uuid4(),
            schema_version="1",
            event_type=event_type,
            source=source,
            study_id=uuid.UUID(study_id),
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=uuid.UUID(research_session_id),
            occurred_at=occurred_at,
            emitter_id=emitter_id,
            emitter_sequence=emitter_sequence,
            payload={"tool_name": "read"},
            provenance=Provenance(source=source, normalizer_version="1"),
            coverage=Coverage(state="AVAILABLE", capability="tool_lifecycle"),
        )
        context = IngestionContext(
            study_id=uuid.UUID(study_id),
            enrollment_id=uuid.UUID(enrollment_id),
            research_session_id=uuid.UUID(research_session_id),
            revocation_epoch=0,
        )
        record = _record_from_event(
            event, context, compute_event_digest(event), accepted_at=occurred_at
        )
        record = record.model_copy(update={"retention_state": retention_state})
        store = SqlAlchemyIngestionStore(session)
        store.insert_events([record])
        store.commit()
    finally:
        session.close()


def _keys(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(key)
            keys |= _keys(child)
    elif isinstance(value, list):
        for child in value:
            keys |= _keys(child)
    return keys


def test_owner_sees_study_local_participant_coverage(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "coverage-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "coverage-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, profile_id, "Coverage study")
    study_id = study["study_id"]

    current_user["value"] = _participant(participant_id)
    enrollment_id = _join(client, study["join_code"])

    activity_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    running_session = _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=enrollment_id,
        state="running",
        last_activity_at=activity_at,
        last_heartbeat_at=heartbeat_at,
    )
    _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=enrollment_id,
        state="ended",
    )
    first_event_at = datetime.now(timezone.utc) - timedelta(minutes=4)
    last_event_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    _seed_event(
        session_factory,
        study_id=study_id,
        enrollment_id=enrollment_id,
        research_session_id=running_session,
        event_type="tool.completed",
        source="ide",
        occurred_at=first_event_at,
        emitter_id="coverage-emitter-1",
    )
    _seed_event(
        session_factory,
        study_id=study_id,
        enrollment_id=enrollment_id,
        research_session_id=running_session,
        event_type="agent.message.completed",
        source="relay",
        occurred_at=last_event_at,
        emitter_id="coverage-emitter-2",
    )
    # A retention tombstone must never be counted.
    _seed_event(
        session_factory,
        study_id=study_id,
        enrollment_id=enrollment_id,
        research_session_id=running_session,
        event_type="tool.failed",
        source="ide",
        occurred_at=last_event_at,
        emitter_id="coverage-emitter-3",
        retention_state="DELETED",
    )

    current_user["value"] = _owner(owner_id)
    response = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["study_id"] == study_id
    assert body["participant_count"] == 1
    assert body["coverage"] == "AVAILABLE"

    row = body["participants"][0]
    assert row["enrollment_id"] == enrollment_id
    assert row["participant_code"].startswith("p_")
    assert row["status"] == "ACTIVE"

    assignment = row["assignment"]
    assert assignment["agent_profile_id"] == str(profile_id)
    assert assignment["strategy"] == "RANDOM_EQUAL"
    assert assignment["randomization_epoch"] == 0
    assert assignment["profile_digest"]
    assert assignment["status"] == "ACTIVE"
    assert "profile_snapshot_json" not in json.dumps(body)

    assert row["sessions"]["total"] == 2
    assert row["sessions"]["active"] == 1
    assert row["sessions"]["terminal"] == 1
    assert row["sessions"]["last_activity_at"] is not None
    assert row["sessions"]["last_heartbeat_at"] is not None

    assert row["events"]["total"] == 2
    assert row["events"]["by_event_type"] == {
        "agent.message.completed": 1,
        "tool.completed": 1,
    }
    assert row["events"]["by_source"] == {"ide": 1, "relay": 1}
    assert row["events"]["last_occurred_at"] is not None

    # No login/participant/account identity or secrets may appear anywhere.
    serialized = json.dumps(body)
    for forbidden in (
        "coverage-participant@example.com",
        "coverage-owner@example.com",
        "account_id",
        "participant_id",
        "email",
        "session_token",
        "TEST_KEY",
    ):
        assert forbidden not in serialized
    forbidden_keys = {"account_id", "participant_id", "email", "session_token"}
    assert not (forbidden_keys & _keys(body))


def test_non_owner_is_forbidden_and_unknown_study_is_404(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "coverage-owner@example.com", can_research=True)
        other_id = _seed_user(session, "coverage-other@example.com", can_research=True)
        participant_id = _seed_user(session, "coverage-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, profile_id, "Coverage study")
    study_id = study["study_id"]

    current_user["value"] = _participant(participant_id)
    _join(client, study["join_code"])

    current_user["value"] = _owner(other_id)
    forbidden = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study_id},
    )
    assert forbidden.status_code == 403, forbidden.text
    assert forbidden.json()["detail"]["code"] == "FORBIDDEN_STUDY"

    unknown = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 404, unknown.text


def test_admin_is_allowed(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "coverage-owner@example.com", can_research=True)
        admin_id = _seed_user(session, "coverage-admin@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study = _create_study(client, profile_id, "Admin coverage study")

    current_user["value"] = _admin(admin_id)
    response = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study["study_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["participant_count"] == 0


def test_studies_do_not_leak_across_each_other(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_a = _seed_user(session, "coverage-owner-a@example.com", can_research=True)
        owner_b = _seed_user(session, "coverage-owner-b@example.com", can_research=True)
        participant_a = _seed_user(session, "coverage-participant-a@example.com")
        participant_b = _seed_user(session, "coverage-participant-b@example.com")
        profile_a = _profile(session, owner_a)
        profile_b = _profile(session, owner_b)
    finally:
        session.close()

    current_user["value"] = _owner(owner_a)
    study_a = _create_study(client, profile_a, "Coverage study A")
    current_user["value"] = _owner(owner_b)
    study_b = _create_study(client, profile_b, "Coverage study B")

    current_user["value"] = _participant(participant_a)
    enrollment_a = _join(client, study_a["join_code"])
    current_user["value"] = _participant(participant_b)
    enrollment_b = _join(client, study_b["join_code"])

    session_a = _seed_session(
        session_factory,
        study_id=study_a["study_id"],
        enrollment_id=enrollment_a,
        state="running",
    )
    session_b = _seed_session(
        session_factory,
        study_id=study_b["study_id"],
        enrollment_id=enrollment_b,
        state="running",
    )
    _seed_event(
        session_factory,
        study_id=study_a["study_id"],
        enrollment_id=enrollment_a,
        research_session_id=session_a,
        event_type="tool.completed",
        source="ide",
        occurred_at=datetime.now(timezone.utc),
        emitter_id="study-a-emitter",
    )
    _seed_event(
        session_factory,
        study_id=study_b["study_id"],
        enrollment_id=enrollment_b,
        research_session_id=session_b,
        event_type="tool.failed",
        source="relay",
        occurred_at=datetime.now(timezone.utc),
        emitter_id="study-b-emitter",
    )

    current_user["value"] = _owner(owner_a)
    body_a = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study_a["study_id"]},
    ).json()
    assert body_a["participant_count"] == 1
    assert [row["enrollment_id"] for row in body_a["participants"]] == [enrollment_a]
    assert body_a["participants"][0]["events"]["by_source"] == {"ide": 1}
    assert body_a["participants"][0]["events"]["by_event_type"] == {"tool.completed": 1}

    # The other owner sees only their own study, and cannot read study A.
    current_user["value"] = _owner(owner_b)
    body_b = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study_b["study_id"]},
    ).json()
    assert [row["enrollment_id"] for row in body_b["participants"]] == [enrollment_b]
    assert body_b["participants"][0]["events"]["by_source"] == {"relay": 1}

    cross = client.get(
        "/api/research/operations/participants/coverage",
        params={"study_id": study_a["study_id"]},
    )
    assert cross.status_code == 403, cross.text


def test_personal_dashboard_owner_scope_is_unchanged():
    """Regression: the personal analytics path still scopes to owner_user_id."""
    captured: list[str] = []
    db = MagicMock()

    def execute(clause, params=None):
        captured.append(str(clause))
        result = MagicMock()
        if "COUNT(*) AS total_tasks" in str(clause):
            result.one.return_value = SimpleNamespace(
                total_tasks=0,
                completed_tasks=0,
                failed_tasks=0,
                open_tasks=0,
                avg_steps=0,
                avg_task_duration_ms=0,
            )
        elif "AS model_calls" in str(clause):
            result.one.return_value = SimpleNamespace(
                model_calls=0,
                tool_calls=0,
                tool_failures=0,
                observed_failures=0,
                successful_model_calls=0,
                rate_limit_retries=0,
                event_tokens=None,
                provider_input_tokens=None,
                model_output_tokens=None,
                tool_schema_bytes=None,
                conversation_context_bytes=None,
                tool_result_bytes=None,
                avg_model_latency_ms=None,
                p95_model_latency_ms=None,
                permission_decisions=0,
                permission_accepted=0,
            )
        else:
            result.fetchall.return_value = []
            result.one_or_none.return_value = None
        return result

    db.execute.side_effect = execute
    owner = SimpleNamespace(
        user_id="11111111-1111-1111-1111-111111111111", is_admin=False
    )
    agent_overview(db, owner, time_window="7d")

    assert captured, "expected the personal dashboard to issue queries"
    scoped = [sql for sql in captured if "FROM agent_task t" in sql]
    assert scoped, "expected owner-scoped task queries"
    assert all("t.owner_user_id = CAST(:user_id AS UUID)" in sql for sql in scoped)
