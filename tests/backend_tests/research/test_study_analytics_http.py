"""DB-backed endpoint tests for the study analytics read models (issue 03).

Covers ``GET /api/research/studies/{study_id}/analytics/participants``,
``.../analytics/participants/{enrollment_id}`` and ``.../analytics/summary``:
authorization (owner, other researcher, administrator, participant, unknown
study, foreign/unknown enrollment), an empty study, and a seeded two-arm study
with sessions and a realistic event mix (prompts, streamed chunks, tools incl. a
failure, permission allow/reject/cancel, a user cancel, relay model calls with
usage and prompt tokens over/under the context cap, errors, IDE edits, plans, a
retention tombstone), asserting concrete numbers for every block. Content
canaries and login identity must never appear in a response.

The client is deliberately not entered as a context manager: that would run the
application lifespan, whose shutdown builds the real ``App`` from ``.env`` and
flushes its Redis. Every dependency these routes use is overridden, so the tests
only ever touch ``TEST_DATABASE_URL``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.migration.migration_manager import MigrationManager
from main import app
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    Provenance,
)

from ._byoa_contract import BYOA_CONFIG_BINDINGS

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

VALID_SESSION_POLICY = {
    "idle_timeout_seconds": 600,
    "resume_grace_seconds": 120,
    "heartbeat_seconds": 30,
}
CANARY = "CANARY-CONTENT-7f3a do not leak"
BASE = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    hour=10, minute=0, second=0, microsecond=0
)
DAY = BASE.date().isoformat()
EARLY = BASE - timedelta(days=9)
EARLY_DAY = EARLY.date().isoformat()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class RuntimeApp:
    session_factory: sessionmaker

    def get_db_session(self):
        return self.session_factory()


@pytest.fixture()
def analytics_runtime():
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

    app.dependency_overrides[App.get_instance] = lambda: runtime
    app.dependency_overrides[get_current_user] = lambda: current_user["value"]
    client = TestClient(app)
    try:
        yield client, session_factory, current_user
    finally:
        client.close()
        app.dependency_overrides.pop(App.get_instance, None)
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


# -- seeding helpers ------------------------------------------------------------------


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


def _profile(session, owner_id: uuid.UUID, name: str) -> uuid.UUID:
    profile_id = uuid.uuid4()
    release_id = f"analytics-release-{uuid.uuid4()}"
    artifact_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    adapter_digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, 'analytics-agent', :source_digest, 'QUALIFIED', "
            "CAST(:release_json AS JSONB) || jsonb_build_object('tests', CAST(:tests AS JSONB)), now())"
        ),
        {
            "release_id": release_id,
            "source_digest": artifact_digest,
            "release_json": json.dumps(
                {
                    "schema_version": "1",
                    "agent_id": "analytics-agent",
                    "release_id": release_id,
                    "version": "1.0.0",
                    "source_manifest_digest": artifact_digest,
                    "distribution_mode": "BYOA_EXTERNAL",
                    "agent_command": "analytics-agent",
                    "agent_package": "analytics-agent",
                    "byoa_config": list(BYOA_CONFIG_BINDINGS),
                    "artifacts": [],
                    "adapter": {
                        "adapter_id": "analytics-adapter",
                        "version": "1.0.0",
                        "digest": adapter_digest,
                    },
                }
            ),
            "tests": json.dumps(
                [
                    {
                        "os": "macos",
                        "arch": "arm64",
                        "self_check": "PASS",
                        "acp_initialize": "PASS",
                        "ran_at": "2026-09-21T00:00:00Z",
                    }
                ]
            ),
        },
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, "
            "tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, :name, 'model', 'codex', :release_id, '[]', 'auto', 1)"
        ),
        {
            "profile_id": profile_id,
            "owner_id": owner_id,
            "release_id": release_id,
            "name": name,
        },
    )
    session.commit()
    return profile_id


def _researcher(user_id: uuid.UUID, *, admin: bool = False) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=admin,
        email="analytics-researcher@example.com",
        name="Analytics Researcher",
        can_research=True,
    )


def _participant(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="analytics-participant@example.com",
        name="Analytics Participant",
    )


def _create_study(client, profile_ids, name: str) -> dict:
    created = client.post(
        "/api/research/studies",
        json={
            "name": name,
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id) for profile_id in profile_ids],
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


def _execute(session_factory, sql: str, params: dict) -> int:
    session = session_factory()
    try:
        result = session.execute(text(sql), params)
        session.commit()
        return result.rowcount
    finally:
        session.close()


def _make_managed_arm(session_factory, *, study_id: str, profile_id, cap: int) -> None:
    """Simulate a managed-runtime arm by patching its frozen study snapshot."""
    updated = _execute(
        session_factory,
        "UPDATE public.study_agent_profile "
        "SET profile_snapshot_json = profile_snapshot_json || CAST(:patch AS JSONB) "
        "WHERE study_id = :study_id AND profile_id = :profile_id",
        {
            "patch": json.dumps(
                {"framework_version": "code4me2-agent", "max_context_tokens": cap}
            ),
            "study_id": study_id,
            "profile_id": str(profile_id),
        },
    )
    assert updated == 1


def _assign(session_factory, *, enrollment_id: str, profile_id) -> None:
    """Pin the (otherwise random) assignment to one arm's frozen snapshot."""
    updated = _execute(
        session_factory,
        "UPDATE public.study_assignment AS sa "
        "SET agent_profile_id = sap.profile_id, profile_digest = sap.profile_digest, "
        "profile_snapshot_json = sap.profile_snapshot_json "
        "FROM public.study_agent_profile AS sap "
        "WHERE sa.enrollment_id = :enrollment_id AND sap.study_id = sa.study_id "
        "AND sap.profile_id = :profile_id",
        {"enrollment_id": enrollment_id, "profile_id": str(profile_id)},
    )
    assert updated == 1


def _seed_session(
    session_factory,
    *,
    study_id: str,
    enrollment_id: str,
    state: str,
    opened_at: datetime,
    closed_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
) -> str:
    session_id = uuid.uuid4()
    _execute(
        session_factory,
        "INSERT INTO public.research_session "
        "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
        "environment_json, transitions_json, created_at, opened_at, closed_at, "
        "close_reason, last_activity_at, last_heartbeat_at) "
        "VALUES (:session_id, :enrollment_id, :study_id, :context_id, :state, 'manifest', "
        "'{}', '[]', :opened_at, :opened_at, :closed_at, :close_reason, "
        ":last_activity_at, :last_heartbeat_at)",
        {
            "session_id": session_id,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "context_id": f"analytics-ctx-{session_id.hex[:8]}",
            "state": state,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "close_reason": "explicit_completion" if closed_at is not None else None,
            "last_activity_at": last_activity_at,
            "last_heartbeat_at": last_heartbeat_at,
        },
    )
    return str(session_id)


class _Seeder:
    """Builds canonical events and persists them through the ingestion store."""

    def __init__(self, study_id: str) -> None:
        self.study_id = uuid.UUID(study_id)
        self.records = []
        self._sequences: dict[tuple, int] = defaultdict(int)

    def add(
        self,
        *,
        enrollment: str,
        session: str,
        emitter: str,
        at: datetime,
        type: str,
        source: str = "acp",
        turn: str | None = None,
        corr: dict | None = None,
        payload: dict | None = None,
        metrics: dict | None = None,
        lifecycle: str | None = None,
        retention: str = "RETAINED",
    ) -> None:
        self._sequences[(session, emitter)] += 1
        correlations = dict(corr or {})
        if turn is not None:
            correlations["turn_id"] = turn
        event = CanonicalEventV1(
            event_id=uuid.uuid4(),
            schema_version="1",
            event_type=type,
            source=source,
            study_id=self.study_id,
            enrollment_id=uuid.UUID(enrollment),
            research_session_id=uuid.UUID(session),
            occurred_at=at,
            emitter_id=emitter,
            emitter_sequence=self._sequences[(session, emitter)],
            correlations=Correlations(**correlations),
            lifecycle_state=lifecycle,
            payload=dict(payload or {}),
            metrics=EventMetrics(**(metrics or {})),
            provenance=Provenance(source=source, normalizer_version="analytics-test"),
            coverage=Coverage(state="AVAILABLE"),
        )
        context = IngestionContext(
            study_id=self.study_id,
            enrollment_id=uuid.UUID(enrollment),
            research_session_id=uuid.UUID(session),
            revocation_epoch=0,
        )
        record = _record_from_event(event, context, compute_event_digest(event), accepted_at=at)
        if retention != "RETAINED":
            record = record.model_copy(update={"retention_state": retention})
        self.records.append(record)

    def flush(self, session_factory) -> None:
        session = session_factory()
        try:
            store = SqlAlchemyIngestionStore(session)
            store.insert_events(self.records)
            store.commit()
        finally:
            session.close()


def _seed_two_arm_study(client, session_factory, current_user) -> SimpleNamespace:
    """Arm A (managed, cap 8000): P1 rich + P2 idle; arm B (BYOA): P3 revoked + P4 silent."""
    session = session_factory()
    try:
        owner = _seed_user(session, "analytics-owner@example.com", can_research=True)
        other = _seed_user(session, "analytics-other@example.com", can_research=True)
        admin = _seed_user(session, "analytics-admin@example.com", can_research=True)
        participants = [
            _seed_user(session, f"analytics-p{index}@example.com") for index in range(1, 6)
        ]
        managed_profile = _profile(session, owner, "Arm A managed")
        byoa_profile = _profile(session, owner, "Arm B codex")
        foreign_profile = _profile(session, other, "Foreign arm")
    finally:
        session.close()

    current_user["value"] = _researcher(owner)
    study = _create_study(client, [managed_profile, byoa_profile], "Analytics study")
    study_id = study["study_id"]
    current_user["value"] = _researcher(other)
    foreign = _create_study(client, [foreign_profile], "Foreign study")

    enrollments = []
    for user_id in participants[:4]:
        current_user["value"] = _participant(user_id)
        enrollments.append(_join(client, study["join_code"]))
    current_user["value"] = _participant(participants[4])
    foreign_enrollment = _join(client, foreign["join_code"])
    p1, p2, p3, p4 = enrollments

    _make_managed_arm(session_factory, study_id=study_id, profile_id=managed_profile, cap=8000)
    for enrollment_id, profile_id in (
        (p1, managed_profile),
        (p2, managed_profile),
        (p3, byoa_profile),
        (p4, byoa_profile),
    ):
        _assign(session_factory, enrollment_id=enrollment_id, profile_id=profile_id)
    _execute(
        session_factory,
        "UPDATE public.research_enrollment SET status = 'REVOKED' WHERE enrollment_id = :id",
        {"id": p3},
    )

    s1a = _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=p1,
        state="ended",
        opened_at=BASE,
        closed_at=BASE + timedelta(hours=1),
    )
    base_b = BASE + timedelta(hours=4)
    s1b = _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=p1,
        state="running",
        opened_at=base_b,
        last_activity_at=base_b + timedelta(minutes=30),
    )
    s2 = _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=p2,
        state="ended",
        opened_at=EARLY,
        closed_at=EARLY + timedelta(minutes=30),
    )
    s3 = _seed_session(
        session_factory,
        study_id=study_id,
        enrollment_id=p3,
        state="running",
        opened_at=BASE - timedelta(hours=1),
        last_heartbeat_at=BASE - timedelta(minutes=40),
    )

    seed = _Seeder(study_id)

    def s(seconds: float, base: datetime = BASE) -> datetime:
        return base + timedelta(seconds=seconds)

    # P1, session S1a, proxy stream "acp-p1a".
    a = {"enrollment": p1, "session": s1a, "emitter": "acp-p1a"}
    seed.add(**a, at=s(0), type="interaction.started", payload={"session_id": "acp-1"})
    seed.add(**a, at=s(60), type="agent.message.started", turn="1", payload={"session_id": "acp-1"})
    seed.add(
        **a,
        at=s(61),
        type="agent.message.started",
        turn="1",
        lifecycle="started",
        payload={"message_kind": "assistant", "content": CANARY},
    )
    seed.add(
        **a,
        at=s(65),
        type="tool.created",
        turn="1",
        lifecycle="pending",
        payload={"tool_call_id": "t-read-1", "tool_name": "Read file", "tool_kind": "read", "status": "pending"},
    )
    seed.add(
        **a,
        at=s(66),
        type="tool.completed",
        turn="1",
        lifecycle="completed",
        payload={"tool_call_id": "t-read-1", "status": "completed", "content": CANARY},
    )
    seed.add(
        **a,
        at=s(70),
        type="tool.created",
        turn="1",
        lifecycle="pending",
        payload={
            "tool_call_id": "t-edit-1",
            "tool_name": "Edit file",
            "tool_kind": "edit",
            "status": "pending",
            "arguments": CANARY,
        },
    )
    seed.add(
        **a,
        at=s(71),
        type="permission.requested",
        turn="1",
        corr={"permission_id": "7", "tool_call_id": "t-edit-1"},
        payload={"tool_call_id": "t-edit-1", "option_count": 2},
    )
    seed.add(
        **a,
        at=s(75),
        type="permission.decided",
        turn="1",
        corr={"permission_id": "7", "tool_call_id": "t-edit-1"},
        payload={"decision": "allow", "outcome": "selected"},
    )
    seed.add(
        **a,
        at=s(80),
        type="tool.completed",
        turn="1",
        lifecycle="completed",
        payload={"tool_call_id": "t-edit-1", "status": "completed"},
    )
    seed.add(
        **a,
        at=s(85),
        type="plan.updated",
        turn="1",
        payload={
            "plan_size": 3,
            "plan_status_counts": [
                {"status": "completed", "count": 1},
                {"status": "pending", "count": 2},
            ],
        },
    )
    seed.add(
        **a,
        at=s(88),
        type="plan.updated",
        turn="1",
        payload={
            "plan_size": 3,
            "plan_status_counts": [
                {"status": "completed", "count": 2},
                {"status": "in_progress", "count": 1},
            ],
        },
    )
    seed.add(**a, at=s(90), type="ide.file.saved", turn="1", payload={"session_id": "acp-1"})
    seed.add(
        **a,
        at=s(120),
        type="agent.message.completed",
        turn="1",
        lifecycle="completed",
        payload={"stop_reason": "end_turn"},
    )
    # Managed-runtime model calls (self-reported, source relay) during turn 1.
    relay_a = {"enrollment": p1, "session": s1a, "emitter": "self-report", "source": "relay"}
    seed.add(
        **relay_a,
        at=s(63),
        type="agent.message.completed",
        payload={"legacy_kind": "model_call", "model": "model"},
        metrics={"usage_tokens": 1500, "counts": {"prompt_tokens": 7000, "completion_tokens": 200}},
    )
    seed.add(
        **relay_a,
        at=s(100),
        type="agent.message.completed",
        payload={"legacy_kind": "model_call", "model": "model"},
        metrics={"usage_tokens": 2500, "counts": {"prompt_tokens": 9000}},
    )
    # The relay's copy of an ACP-observed tool call: never double counted.
    seed.add(
        **relay_a,
        at=s(101),
        type="tool.completed",
        corr={"tool_call_id": "t-read-1"},
        payload={"tool_name": "read_file", "legacy_kind": "tool_call"},
    )
    # Turn 2: a rejected permission, a failed tool and a user cancel.
    seed.add(**a, at=s(600), type="agent.message.started", turn="2", payload={"session_id": "acp-1"})
    seed.add(
        **a,
        at=s(605),
        type="tool.created",
        turn="2",
        payload={"tool_call_id": "t-exec-1", "tool_name": "Run tests", "tool_kind": "execute", "status": "pending"},
    )
    seed.add(
        **a,
        at=s(606),
        type="permission.requested",
        turn="2",
        corr={"permission_id": "9", "tool_call_id": "t-exec-1"},
    )
    seed.add(
        **a,
        at=s(616),
        type="permission.decided",
        turn="2",
        corr={"permission_id": "9", "tool_call_id": "t-exec-1"},
        payload={"decision": "reject"},
    )
    seed.add(
        **a,
        at=s(617),
        type="tool.failed",
        turn="2",
        lifecycle="failed",
        payload={"tool_call_id": "t-exec-1", "status": "failed"},
    )
    seed.add(**a, at=s(620), type="interaction.completed", turn="2", payload={"session_id": "acp-1"})
    seed.add(**a, at=s(621), type="agent.message.completed", turn="2", payload={"stop_reason": "cancelled"})
    seed.add(
        **a,
        at=s(630),
        type="agent.error",
        payload={"error_code": -32603, "error_message": CANARY, "error_source": "agent"},
    )

    # P1, session S1b: a new proxy process reuses JSON-RPC id "1".
    b = {"enrollment": p1, "session": s1b, "emitter": "acp-p1b"}
    seed.add(**b, at=s(10, base_b), type="agent.message.started", turn="1")
    seed.add(
        **b,
        at=s(20, base_b),
        type="tool.created",
        turn="1",
        payload={"tool_call_id": "t-search-1", "tool_name": "Find usages", "tool_kind": "search"},
    )
    seed.add(**b, at=s(22, base_b), type="tool.completed", turn="1", payload={"tool_call_id": "t-search-1", "status": "completed"})
    seed.add(
        **b,
        at=s(25, base_b),
        type="tool.created",
        turn="1",
        payload={"tool_call_id": "t-read-2", "tool_name": "Read file", "tool_kind": "read"},
    )
    seed.add(**b, at=s(26, base_b), type="tool.completed", turn="1", payload={"tool_call_id": "t-read-2", "status": "completed"})
    seed.add(**b, at=s(40, base_b), type="agent.message.completed", turn="1", payload={"stop_reason": "end_turn"})
    seed.add(
        enrollment=p1,
        session=s1b,
        emitter="self-report",
        source="relay",
        at=s(41, base_b),
        type="agent.message.completed",
        payload={"legacy_kind": "model_call"},
        metrics={"usage_tokens": 1000, "counts": {"prompt_tokens": 4000}},
    )
    ide = {"enrollment": p1, "session": s1b, "emitter": "ide:ctx-1", "source": "ide"}
    seed.add(**ide, at=s(240, base_b), type="ide.file.opened", payload={"file_extension": "py", "language": "python"})
    seed.add(**ide, at=s(300, base_b), type="ide.document.changed", payload={"file_extension": "py", "count": 120})
    seed.add(**ide, at=s(360, base_b), type="ide.document.changed", payload={"count": 5})
    seed.add(**ide, at=s(420, base_b), type="ide.document.changed", payload={"count": 0})
    seed.add(
        **ide,
        at=s(480, base_b),
        type="ide.run.executed",
        payload={"action_category": "RUN", "phase": "finished", "exit_code": 1},
    )
    # A retention tombstone is excluded everywhere.
    seed.add(
        **b,
        at=s(540, base_b),
        type="tool.failed",
        payload={"tool_call_id": "t-ghost", "tool_kind": "delete"},
        retention="DELETED",
    )
    seed.add(
        **b,
        at=s(1200, base_b),
        type="system.agent.crashed",
        lifecycle="failed",
        payload={"failure_kind": "system_agent_crashed", "error_code": "system_agent_crashed"},
    )

    # P2 (arm A), nine days earlier: the agent reports prompt-response usage.
    c = {"enrollment": p2, "session": s2, "emitter": "acp-p2"}
    seed.add(**c, at=s(0, EARLY), type="agent.message.started", turn="1")
    seed.add(**c, at=s(5, EARLY), type="tool.created", turn="1", payload={"tool_call_id": "t-p2-read", "tool_name": "Read file", "tool_kind": "read"})
    seed.add(**c, at=s(6, EARLY), type="tool.completed", turn="1", payload={"tool_call_id": "t-p2-read", "status": "completed"})
    seed.add(**c, at=s(7, EARLY), type="tool.created", turn="1", payload={"tool_call_id": "t-p2-edit", "tool_name": "Edit file", "tool_kind": "edit"})
    seed.add(**c, at=s(9, EARLY), type="tool.completed", turn="1", payload={"tool_call_id": "t-p2-edit", "status": "completed"})
    seed.add(
        **c,
        at=s(30, EARLY),
        type="agent.message.completed",
        turn="1",
        payload={"stop_reason": "end_turn"},
        metrics={"usage_tokens": 800},
    )
    seed.add(
        enrollment=p2,
        session=s2,
        emitter="relay",
        source="relay",
        at=s(31, EARLY),
        type="agent.message.completed",
        payload={"legacy_kind": "model_call"},
        metrics={"usage_tokens": 300, "counts": {"prompt_tokens": 12000}},
    )
    seed.add(**c, at=s(300, EARLY), type="agent.message.started", turn="2")
    seed.add(
        **c,
        at=s(310, EARLY),
        type="agent.message.completed",
        turn="2",
        payload={"stop_reason": "end_turn"},
        metrics={"usage_tokens": 400},
    )

    # P3 (arm B, BYOA, revoked): no relay traffic at all.
    early_b = BASE - timedelta(hours=1)
    d = {"enrollment": p3, "session": s3, "emitter": "acp-p3"}
    seed.add(**d, at=s(300, early_b), type="agent.message.started", turn="1")
    seed.add(**d, at=s(305, early_b), type="tool.created", turn="1", payload={"tool_call_id": "t-p3-fetch", "tool_name": "Fetch docs", "tool_kind": "fetch"})
    seed.add(**d, at=s(307, early_b), type="tool.completed", turn="1", payload={"tool_call_id": "t-p3-fetch", "status": "completed"})
    seed.add(**d, at=s(320, early_b), type="agent.message.completed", turn="1", payload={"stop_reason": "end_turn"})
    seed.add(**d, at=s(360, early_b), type="agent.message.started", turn="2")
    seed.add(**d, at=s(370, early_b), type="permission.requested", turn="2", corr={"permission_id": "4"})
    seed.add(
        **d,
        at=s(372, early_b),
        type="permission.decided",
        turn="2",
        corr={"permission_id": "4"},
        payload={"decision": "cancelled"},
    )
    seed.add(**d, at=s(390, early_b), type="agent.message.completed", turn="2", payload={"stop_reason": "max_tokens"})

    seed.flush(session_factory)
    return SimpleNamespace(
        owner=owner,
        other=other,
        admin=admin,
        participant_user=participants[0],
        study_id=study_id,
        foreign_study_id=foreign["study_id"],
        foreign_enrollment=foreign_enrollment,
        managed_profile=str(managed_profile),
        byoa_profile=str(byoa_profile),
        p1=p1,
        p2=p2,
        p3=p3,
        p4=p4,
        s1a=s1a,
        s1b=s1b,
        base_b=base_b,
    )


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


def _assert_no_content_or_identity(body) -> None:
    serialized = json.dumps(body)
    assert CANARY not in serialized
    assert "@example.com" not in serialized
    forbidden = {
        "account_id",
        "participant_id",
        "email",
        "user_id",
        "content",
        "arguments",
        "reasoning",
        "error_message",
        "profile_snapshot_json",
        "envelope_json",
    }
    assert not (forbidden & _keys(body)), forbidden & _keys(body)


def _url(study_id: str, suffix: str) -> str:
    return f"/api/research/studies/{study_id}/analytics/{suffix}"


# -- tests ---------------------------------------------------------------------------------


def test_authorization_matrix(analytics_runtime):
    client, session_factory, current_user = analytics_runtime
    seeded = _seed_two_arm_study(client, session_factory, current_user)
    paths = [
        _url(seeded.study_id, "participants"),
        _url(seeded.study_id, f"participants/{seeded.p1}"),
        _url(seeded.study_id, "summary"),
    ]

    current_user["value"] = _researcher(seeded.owner)
    for path in paths:
        assert client.get(path).status_code == 200, path

    current_user["value"] = _researcher(seeded.admin, admin=True)
    for path in paths:
        assert client.get(path).status_code == 200, path

    for user in (_researcher(seeded.other), _participant(seeded.participant_user)):
        current_user["value"] = user
        for path in paths:
            response = client.get(path)
            assert response.status_code == 403, (path, response.text)
            assert response.json()["detail"]["code"] == "FORBIDDEN_STUDY"

    current_user["value"] = _researcher(seeded.owner)
    unknown_study = str(uuid.uuid4())
    for suffix in ("participants", f"participants/{seeded.p1}", "summary"):
        assert client.get(_url(unknown_study, suffix)).status_code == 404

    for enrollment_id in (seeded.foreign_enrollment, str(uuid.uuid4())):
        response = client.get(_url(seeded.study_id, f"participants/{enrollment_id}"))
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["code"] == "ENROLLMENT_NOT_FOUND"
    # The foreign study's owner cannot reach it through their own study either.
    current_user["value"] = _researcher(seeded.other)
    response = client.get(_url(seeded.foreign_study_id, f"participants/{seeded.p1}"))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ENROLLMENT_NOT_FOUND"
    assert (
        client.get(_url(seeded.foreign_study_id, f"participants/{seeded.foreign_enrollment}")).status_code
        == 200
    )

    current_user["value"] = _researcher(seeded.owner)
    summary = _url(seeded.study_id, "summary")
    for params, code in (
        ({"start": "2026-02-30"}, "INVALID_DATE"),
        ({"end": "2026-9-1"}, "INVALID_DATE"),
        ({"start": "yesterday"}, "INVALID_DATE"),
        ({"start": "2026-09-10", "end": "2026-09-09"}, "INVALID_WINDOW"),
    ):
        response = client.get(summary, params=params)
        assert response.status_code == 422, (params, response.text)
        assert response.json()["detail"]["code"] == code
    # The first and last representable days are valid bounds (no overflow).
    response = client.get(summary, params={"start": "0001-01-01", "end": "9999-12-31"})
    assert response.status_code == 200, response.text
    assert response.json()["window"] == {"start": "0001-01-01", "end": "9999-12-31"}


def test_empty_study_returns_arms_with_nulls_not_zeros(analytics_runtime):
    client, session_factory, current_user = analytics_runtime
    session = session_factory()
    try:
        owner = _seed_user(session, "analytics-owner@example.com", can_research=True)
        profile_a = _profile(session, owner, "Empty A")
        profile_b = _profile(session, owner, "Empty B")
    finally:
        session.close()
    current_user["value"] = _researcher(owner)
    study = _create_study(client, [profile_a, profile_b], "Empty analytics study")

    participants = client.get(_url(study["study_id"], "participants"))
    assert participants.status_code == 200, participants.text
    body = participants.json()
    assert body["study_id"] == study["study_id"]
    assert body["participants"] == []
    assert [(arm["name"], arm["selection_order"], arm["participants"]) for arm in body["arms"]] == [
        ("Empty A", 0, 0),
        ("Empty B", 1, 0),
    ]

    response = client.get(_url(study["study_id"], "summary"))
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["window"] == {"start": None, "end": None}
    assert summary["totals"] == {
        "participants_enrolled": 0,
        "participants_active": 0,
        "participants_with_telemetry": 0,
        "sessions": 0,
        "session_seconds": 0.0,
        "prompts": 0,
        "tool_calls": 0,
        "tool_failures": 0,
        "cancellations": 0,
        "permission_requests": 0,
        "permission_allowed": 0,
        "permission_rejected": 0,
        "permission_cancelled": 0,
        "errors": 0,
        "agent_file_writes": 0,
        "ide_edits": 0,
        "usage_tokens": None,
        "usage_coverage": None,
    }
    for arm in summary["arms"]:
        assert arm["participants"] == 0
        assert all(block["n"] == 0 and block["median"] is None for block in arm["metrics"].values())
        assert arm["turn_seconds"] == {"p50": None, "p90": None, "n": 0}
        assert arm["transitions"] == []
        assert arm["context"]["model_calls"] == 0
        assert arm["context"]["coverage"] == "UNAVAILABLE"
    for key in ("daily", "tool_kinds", "tools", "stop_reasons", "permission_decisions"):
        assert summary[key] == []
    assert summary["coverage"] == {
        "usage_tokens": "UNAVAILABLE",
        "turn_correlation": "UNAVAILABLE",
        "tool_kind": "UNAVAILABLE",
    }


def test_participants_table_for_a_seeded_two_arm_study(analytics_runtime):
    client, session_factory, current_user = analytics_runtime
    seeded = _seed_two_arm_study(client, session_factory, current_user)
    current_user["value"] = _researcher(seeded.owner)

    response = client.get(_url(seeded.study_id, "participants"))
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_no_content_or_identity(body)

    assert [
        (arm["profile_id"], arm["name"], arm["framework_version"], arm["selection_order"], arm["participants"])
        for arm in body["arms"]
    ] == [
        (seeded.managed_profile, "Arm A managed", "code4me2-agent", 0, 2),
        (seeded.byoa_profile, "Arm B codex", "codex", 1, 2),
    ]
    rows = {row["enrollment_id"]: row for row in body["participants"]}
    assert [row["enrollment_id"] for row in body["participants"]] == [
        seeded.p1,
        seeded.p2,
        seeded.p3,
        seeded.p4,
    ]
    assert {key: rows[key]["health"] for key in rows} == {
        seeded.p1: "ACTIVE",
        seeded.p2: "IDLE",
        seeded.p3: "INACTIVE",
        seeded.p4: "NO_TELEMETRY",
    }

    p1 = rows[seeded.p1]
    assert p1["participant_code"].startswith("p_")
    assert p1["status"] == "ACTIVE"
    assert p1["consent_accepted_at"] is not None
    assert p1["arm"]["profile_id"] == seeded.managed_profile
    assert p1["arm"]["name"] == "Arm A managed"
    assert p1["arm"]["model"] == "model"
    assert p1["arm"]["framework_version"] == "code4me2-agent"
    assert p1["arm"]["assignment_status"] == "ACTIVE"
    assert p1["arm"]["assigned_at"] is not None
    assert p1["sessions"] == {
        "total": 2,
        "active": 1,
        "session_seconds": 5400.0,
        "last_activity_at": _iso(seeded.base_b + timedelta(minutes=30)),
        "last_heartbeat_at": None,
    }
    assert p1["activity"] == {
        "prompts": 3,
        "tool_calls": 5,
        "tool_failures": 1,
        "cancellations": 1,
        "permission_requests": 2,
        "permission_denials": 1,
        "errors": 2,
        # t-edit-1 and the ACP file save in the same turn are one write.
        "agent_file_writes": 1,
        "ide_edits": 3,
        "usage_tokens": 5000,
        "active_days": 1,
        "first_event_at": _iso(BASE),
        "last_event_at": _iso(seeded.base_b + timedelta(seconds=1200)),
    }

    p2 = rows[seeded.p2]["activity"]
    assert (p2["prompts"], p2["tool_calls"], p2["agent_file_writes"], p2["usage_tokens"]) == (2, 2, 1, 1200)
    p3 = rows[seeded.p3]
    assert p3["status"] == "REVOKED"
    assert p3["arm"]["name"] == "Arm B codex"
    assert p3["sessions"]["session_seconds"] == 1200.0
    assert p3["activity"]["usage_tokens"] is None
    assert p3["activity"]["permission_requests"] == 1
    p4 = rows[seeded.p4]
    assert p4["sessions"] == {
        "total": 0,
        "active": 0,
        "session_seconds": 0.0,
        "last_activity_at": None,
        "last_heartbeat_at": None,
    }
    assert p4["activity"]["prompts"] == 0
    assert p4["activity"]["usage_tokens"] is None
    assert p4["activity"]["first_event_at"] is None


def test_participant_dashboard_for_a_seeded_participant(analytics_runtime):
    client, session_factory, current_user = analytics_runtime
    seeded = _seed_two_arm_study(client, session_factory, current_user)
    current_user["value"] = _researcher(seeded.owner)

    response = client.get(_url(seeded.study_id, f"participants/{seeded.p1}"))
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_no_content_or_identity(body)

    assert body["enrollment_id"] == seeded.p1
    assert body["health"] == "ACTIVE"
    assert body["activity"]["prompts"] == 3
    assert body["metrics"] == {
        "prompts": 3,
        "active_days": 1,
        "session_hours": 1.5,
        "prompts_per_session_hour": 2.0,
        "tool_calls_per_prompt": 1.667,
        "tool_failure_rate": 0.2,
        "auto_run_share": 0.6,
        "permission_denial_rate": 0.5,
        "median_permission_wait_seconds": 7.0,
        "cancel_rate": 0.333,
        "median_turn_seconds": 30.0,
        "tokens_per_prompt": 2500.0,
        "errors_per_prompt": 0.667,
        "agent_writes_per_prompt": 0.333,
        "seconds_to_first_agent_edit": 20.0,
        "ide_edits_per_session_hour": 2.0,
        "plan_completion_rate": 0.667,
    }
    assert body["context"] == {
        "cap_tokens": 8000,
        "model_calls": 3,
        "calls_with_prompt_tokens": 3,
        "prompt_tokens_p50": 7000.0,
        "prompt_tokens_p95": 8800.0,
        "prompt_tokens_max": 9000,
        "over_cap_calls": 1,
        "over_cap_share": 0.333,
        "coverage": "AVAILABLE",
    }
    assert body["daily"] == [
        {
            "date": DAY,
            "prompts": 3,
            "tool_calls": 5,
            "errors": 2,
            "ide_edits": 3,
            "session_seconds": 5400.0,
        }
    ]
    assert body["tool_kinds"] == [
        {"tool_kind": "read", "calls": 2, "failures": 0},
        {"tool_kind": "edit", "calls": 1, "failures": 0},
        {"tool_kind": "execute", "calls": 1, "failures": 1},
        {"tool_kind": "search", "calls": 1, "failures": 0},
    ]
    # ACP titles ("Read file", ...) are never shown; t-read-1 is named by the
    # relay's copy of the same call, the others are grouped by kind.
    assert body["tools"] == [
        {"tool_name": "read_file", "tool_kind": "read", "calls": 1, "failures": 0, "median_duration_ms": 1000.0},
        {"tool_name": None, "tool_kind": "edit", "calls": 1, "failures": 0, "median_duration_ms": 10000.0},
        {"tool_name": None, "tool_kind": "execute", "calls": 1, "failures": 1, "median_duration_ms": 12000.0},
        {"tool_name": None, "tool_kind": "read", "calls": 1, "failures": 0, "median_duration_ms": 1000.0},
        {"tool_name": None, "tool_kind": "search", "calls": 1, "failures": 0, "median_duration_ms": 2000.0},
    ]
    assert body["stop_reasons"] == [
        {"stop_reason": "end_turn", "count": 2},
        {"stop_reason": "cancelled", "count": 1},
    ]
    assert body["permission_decisions"] == [
        {"decision": "allow", "count": 1},
        {"decision": "reject", "count": 1},
    ]
    assert body["sessions_list"] == [
        {
            "session_id": seeded.s1b,
            "state": "running",
            "opened_at": _iso(seeded.base_b),
            "closed_at": None,
            "last_activity_at": _iso(seeded.base_b + timedelta(minutes=30)),
            "close_reason": None,
            "session_seconds": 1800.0,
            "prompts": 1,
            "tool_calls": 2,
            "errors": 1,
        },
        {
            "session_id": seeded.s1a,
            "state": "ended",
            "opened_at": _iso(BASE),
            "closed_at": _iso(BASE + timedelta(hours=1)),
            "last_activity_at": None,
            "close_reason": "explicit_completion",
            "session_seconds": 3600.0,
            "prompts": 2,
            "tool_calls": 3,
            "errors": 1,
        },
    ]
    assert body["turns"] == [
        {
            "turn_id": "1",
            "session_id": seeded.s1b,
            "started_at": _iso(seeded.base_b + timedelta(seconds=10)),
            "completed_at": _iso(seeded.base_b + timedelta(seconds=40)),
            "duration_seconds": 30.0,
            "tool_calls": 2,
            "tool_failures": 0,
            "permission_requests": 0,
            "stop_reason": "end_turn",
            "usage_tokens": 1000,
            "cancelled": False,
        },
        {
            "turn_id": "2",
            "session_id": seeded.s1a,
            "started_at": _iso(BASE + timedelta(seconds=600)),
            "completed_at": _iso(BASE + timedelta(seconds=621)),
            "duration_seconds": 21.0,
            "tool_calls": 1,
            "tool_failures": 1,
            "permission_requests": 1,
            "stop_reason": "cancelled",
            "usage_tokens": None,
            "cancelled": True,
        },
        {
            "turn_id": "1",
            "session_id": seeded.s1a,
            "started_at": _iso(BASE + timedelta(seconds=60)),
            "completed_at": _iso(BASE + timedelta(seconds=120)),
            "duration_seconds": 60.0,
            "tool_calls": 2,
            "tool_failures": 0,
            "permission_requests": 1,
            "stop_reason": "end_turn",
            "usage_tokens": 4000,
            "cancelled": False,
        },
    ]

    timeline = body["timeline"]
    # 38 seeded events - 1 tombstone - 1 streamed chunk - 3 document changes.
    assert len(timeline) == 33
    assert timeline[0]["event_type"] == "system.agent.crashed"
    assert timeline[0]["error_code"] == "system_agent_crashed"
    assert timeline == sorted(timeline, key=lambda item: item["occurred_at"], reverse=True)
    types = [item["event_type"] for item in timeline]
    assert "ide.document.changed" not in types
    assert types.count("agent.message.started") == 3
    assert "t-ghost" not in json.dumps(timeline)
    read_done = next(
        item
        for item in timeline
        if item["event_type"] == "tool.completed"
        and item["source"] == "acp"
        and item["occurred_at"] == _iso(BASE + timedelta(seconds=66))
    )
    assert (read_done["tool_name"], read_done["tool_kind"], read_done["turn_id"]) == ("read_file", "read", "1")
    assert read_done["status"] == "completed"
    error = next(item for item in timeline if item["event_type"] == "agent.error")
    assert error["error_code"] == "-32603"
    decided = [item["decision"] for item in timeline if item["event_type"] == "permission.decided"]
    assert sorted(decided) == ["allow", "reject"]

    # A BYOA participant without relay traffic: context coverage is UNAVAILABLE.
    byoa = client.get(_url(seeded.study_id, f"participants/{seeded.p3}")).json()
    assert byoa["health"] == "INACTIVE"
    assert byoa["context"]["cap_tokens"] == 16000
    assert byoa["context"]["model_calls"] == 0
    assert byoa["context"]["coverage"] == "UNAVAILABLE"
    assert byoa["metrics"]["permission_denial_rate"] is None
    assert byoa["metrics"]["median_permission_wait_seconds"] == 2.0
    assert byoa["permission_decisions"] == [{"decision": "cancelled", "count": 1}]

    silent = client.get(_url(seeded.study_id, f"participants/{seeded.p4}")).json()
    assert silent["health"] == "NO_TELEMETRY"
    assert silent["daily"] == [] and silent["turns"] == [] and silent["timeline"] == []
    assert silent["metrics"]["prompts"] == 0
    assert silent["metrics"]["tool_failure_rate"] is None


def test_study_summary_compares_arms_on_participant_level_values(analytics_runtime):
    client, session_factory, current_user = analytics_runtime
    seeded = _seed_two_arm_study(client, session_factory, current_user)
    current_user["value"] = _researcher(seeded.owner)

    response = client.get(_url(seeded.study_id, "summary"))
    assert response.status_code == 200, response.text
    body = response.json()
    _assert_no_content_or_identity(body)

    assert body["totals"] == {
        "participants_enrolled": 4,
        "participants_active": 3,
        "participants_with_telemetry": 3,
        "sessions": 4,
        "session_seconds": 8400.0,
        "prompts": 7,
        "tool_calls": 8,
        "tool_failures": 1,
        "cancellations": 1,
        "permission_requests": 3,
        "permission_allowed": 1,
        "permission_rejected": 1,
        "permission_cancelled": 1,
        "errors": 2,
        "agent_file_writes": 2,
        "ide_edits": 3,
        "usage_tokens": 6200,
        "usage_coverage": 0.571,
    }
    assert body["coverage"] == {
        "usage_tokens": "PARTIAL",
        "turn_correlation": "AVAILABLE",
        "tool_kind": "AVAILABLE",
    }

    managed, byoa = body["arms"]
    assert (managed["profile_id"], managed["participants"], managed["participants_with_telemetry"]) == (
        seeded.managed_profile,
        2,
        2,
    )
    assert managed["metrics"]["prompts"] == {
        "n": 2,
        "mean": 2.5,
        "median": 2.5,
        "p25": 2.25,
        "p75": 2.75,
        "values": [2.0, 3.0],
    }
    assert managed["metrics"]["tokens_per_prompt"]["values"] == [600.0, 2500.0]
    assert managed["metrics"]["auto_run_share"]["values"] == [0.6, 1.0]
    assert managed["metrics"]["permission_denial_rate"]["values"] == [0.5]
    assert managed["metrics"]["seconds_to_first_agent_edit"]["values"] == [9.0, 20.0]
    assert managed["metrics"]["plan_completion_rate"]["values"] == [0.667]
    assert managed["turn_seconds"] == {"p50": 30.0, "p90": 48.0, "n": 5}
    assert managed["transitions"] == [
        {"from": "read", "to": "edit", "count": 2, "lift": 1.5},
        {"from": "search", "to": "read", "count": 1, "lift": 3.0},
    ]
    assert managed["stop_reasons"] == [
        {"stop_reason": "end_turn", "count": 4},
        {"stop_reason": "cancelled", "count": 1},
    ]
    assert managed["tool_kinds"] == [
        {"tool_kind": "read", "calls": 3, "failures": 0},
        {"tool_kind": "edit", "calls": 2, "failures": 0},
        {"tool_kind": "execute", "calls": 1, "failures": 1},
        {"tool_kind": "search", "calls": 1, "failures": 0},
    ]
    assert managed["context"] == {
        "cap_tokens": 8000,
        "model_calls": 4,
        "calls_with_prompt_tokens": 4,
        "prompt_tokens_p50": 8000.0,
        "prompt_tokens_p95": 11550.0,
        "prompt_tokens_max": 12000,
        "over_cap_calls": 2,
        "over_cap_share": 0.5,
        "coverage": "AVAILABLE",
    }

    assert (byoa["participants"], byoa["participants_with_telemetry"]) == (2, 1)
    # Count metrics keep the silent participant (0); rates exclude them.
    assert byoa["metrics"]["prompts"]["values"] == [0.0, 2.0]
    assert byoa["metrics"]["session_hours"]["values"] == [0.0, 0.333]
    assert byoa["metrics"]["prompts_per_session_hour"]["values"] == [6.0]
    assert byoa["metrics"]["median_turn_seconds"]["values"] == [25.0]
    assert byoa["turn_seconds"] == {"p50": 25.0, "p90": 29.0, "n": 2}
    assert byoa["transitions"] == []
    assert byoa["permission_decisions"] == [{"decision": "cancelled", "count": 1}]
    assert byoa["context"]["cap_tokens"] == 16000
    assert byoa["context"]["model_calls"] == 0
    assert byoa["context"]["over_cap_share"] is None
    assert byoa["context"]["coverage"] == "UNAVAILABLE"

    assert body["daily"] == [
        {
            "date": EARLY_DAY,
            "active_participants": 1,
            "prompts": 2,
            "tool_calls": 2,
            "errors": 0,
            "sessions": 1,
            "by_arm": {
                seeded.managed_profile: {"prompts": 2, "active_participants": 1},
                seeded.byoa_profile: {"prompts": 0, "active_participants": 0},
            },
        },
        {
            "date": DAY,
            "active_participants": 2,
            "prompts": 5,
            "tool_calls": 6,
            "errors": 2,
            "sessions": 3,
            "by_arm": {
                seeded.managed_profile: {"prompts": 3, "active_participants": 1},
                seeded.byoa_profile: {"prompts": 2, "active_participants": 1},
            },
        },
    ]
    assert [(tool["tool_name"], tool["tool_kind"], tool["calls"], tool["by_arm"]) for tool in body["tools"]] == [
        (None, "edit", 2, {seeded.managed_profile: 2, seeded.byoa_profile: 0}),
        (None, "read", 2, {seeded.managed_profile: 2, seeded.byoa_profile: 0}),
        ("read_file", "read", 1, {seeded.managed_profile: 1, seeded.byoa_profile: 0}),
        (None, "execute", 1, {seeded.managed_profile: 1, seeded.byoa_profile: 0}),
        (None, "fetch", 1, {seeded.managed_profile: 0, seeded.byoa_profile: 1}),
        (None, "search", 1, {seeded.managed_profile: 1, seeded.byoa_profile: 0}),
    ]
    assert body["stop_reasons"] == [
        {"stop_reason": "end_turn", "count": 5},
        {"stop_reason": "cancelled", "count": 1},
        {"stop_reason": "max_tokens", "count": 1},
    ]
    assert body["permission_decisions"] == [
        {"decision": "allow", "count": 1},
        {"decision": "cancelled", "count": 1},
        {"decision": "reject", "count": 1},
    ]

    # An inclusive one-day window drops P2's activity nine days earlier.
    windowed = client.get(
        _url(seeded.study_id, "summary"), params={"start": DAY, "end": DAY}
    )
    assert windowed.status_code == 200, windowed.text
    window_body = windowed.json()
    assert window_body["window"] == {"start": DAY, "end": DAY}
    totals = window_body["totals"]
    assert (totals["participants_with_telemetry"], totals["sessions"], totals["prompts"]) == (2, 3, 5)
    assert totals["session_seconds"] == 6600.0
    assert totals["usage_tokens"] == 5000
    managed_window = window_body["arms"][0]
    assert managed_window["participants_with_telemetry"] == 1
    assert managed_window["metrics"]["prompts"]["values"] == [0.0, 3.0]
    assert managed_window["context"]["model_calls"] == 3
    assert managed_window["context"]["over_cap_calls"] == 1
    assert [row["date"] for row in window_body["daily"]] == [DAY]


def test_the_built_in_agents_own_permission_reports_count_once(analytics_runtime):
    """The relay's permission reports never repeat a decision the ACP proxy
    observed, and decisions the approval policy made without asking are not the
    participant's: adding both leaves P1's permission numbers and timeline as
    they were."""
    client, session_factory, current_user = analytics_runtime
    seeded = _seed_two_arm_study(client, session_factory, current_user)
    current_user["value"] = _researcher(seeded.owner)
    url = _url(seeded.study_id, f"participants/{seeded.p1}")
    before = client.get(url).json()

    seed = _Seeder(seeded.study_id)
    relay = {"enrollment": seeded.p1, "emitter": "self-report:task-1", "source": "relay"}
    # s1a: the proxy already observed this round-trip.
    seed.add(
        **relay,
        session=seeded.s1a,
        at=BASE + timedelta(seconds=41),
        type="permission.requested",
        payload={"legacy_kind": "permission_requested", "tool_call_id": "t-edit-1"},
    )
    seed.add(
        **relay,
        session=seeded.s1a,
        at=BASE + timedelta(seconds=42),
        type="permission.decided",
        payload={
            "legacy_kind": "permission_decided",
            "tool_call_id": "t-edit-1",
            "decision": "accepted",
            "decision_scope": "once",
        },
    )
    # s1b has no ACP permission events; these were auto-approved by policy.
    for offset in range(3):
        seed.add(
            **relay,
            session=seeded.s1b,
            at=seeded.base_b + timedelta(seconds=60 + offset),
            type="permission.decided",
            payload={
                "legacy_kind": "permission_decided",
                "tool_call_id": f"t-auto-{offset}",
                "decision": "accepted",
                "decision_scope": "policy",
            },
        )
    seed.flush(session_factory)

    response = client.get(url)
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["metrics"] == before["metrics"]
    assert after["permission_decisions"] == before["permission_decisions"]
    permission_rows = [
        item for item in after["timeline"] if item["event_type"].startswith("permission.")
    ]
    assert permission_rows
    assert {item["source"] for item in permission_rows} == {"acp"}

