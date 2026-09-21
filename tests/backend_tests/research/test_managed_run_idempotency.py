"""ISSUE-002: managed-run race recovery narrowed to the unique violation.

Real-DB coverage (disposable PostgreSQL, independent sessions) for
``POST /api/acp/runs`` idempotency:

* two threads racing one run id collapse to exactly one row with one
  consistent task identity, and the loser's captured error is the psycopg2
  ``23505`` violation on the ``external_run_id`` constraint;
* only that violation replays: foreign-scope, unfunded, policy-missing,
  unrelated-integrity and generic failures never replay;
* a post-commit failure (e.g. a refresh error after the insert committed)
  is NOT a unique violation, so it 500s even though the row exists — while a
  retry replays 200 through the pre-read path. The narrowed handler cannot
  distinguish "row committed then failed" from "row never written", which is
  why the retry path (not the recovery branch) owns that case.

Persistence runs against the real schema (including the ``agent_task``
foreign keys), so each test seeds the referenced user/study/enrollment/
research-session/parent-session graph; only the assignment and funding
context are stubbed, exactly as the mocked ``test_managed_acp.py`` does.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dotenv import load_dotenv
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.acp_authorization import AcpServerAuthorization
from backend.routers.acp import ManagedRunRequest, create_managed_run
from database import crud
from database.db_schemas import Study as StudyRow
from database.migration.migration_manager import MigrationManager
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus, RetentionAction
from research.participants.models import Enrollment, Participant, ResearchEligibility
from research.runtime.sessions import store as session_store
from research.runtime.sessions.service import open_session

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db_runtime():
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
    try:
        yield session_factory
    finally:
        engine.dispose()


def _seed_graph(session_factory) -> SimpleNamespace:
    """Seed the rows ``agent_task`` foreign keys reference.

    Returns the user/parent-session/study/enrollment/research-session ids the
    managed-run path must reference for the insert to reach the database.
    """
    db = session_factory()
    try:
        config_id = db.execute(
            text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
        ).scalar_one()
        user_id = uuid.uuid4()
        db.execute(
            text(
                "INSERT INTO public.\"user\" "
                "(user_id, joined_at, email, name, password, config_id, verified, is_admin) "
                "VALUES (:user_id, :joined_at, :email, 'Managed race', 'x', :config_id, true, false)"
            ),
            {
                "user_id": user_id,
                "joined_at": NOW,
                "email": f"managed-{user_id}@example.com",
                "config_id": config_id,
            },
        )
        parent_session_id = uuid.uuid4()
        db.execute(
            text(
                "INSERT INTO public.session (session_id, user_id, start_time) "
                "VALUES (:session_id, :user_id, :start_time)"
            ),
            {"session_id": parent_session_id, "user_id": user_id, "start_time": NOW},
        )
        study_id = uuid.uuid4()
        db.add(
            StudyRow(
                study_id=study_id,
                name="Managed race study",
                created_by=user_id,
                starts_at=NOW,
                is_active=True,
                is_research=True,
                research_status="ACTIVE",
                research_config_json={
                    "telemetry_policy": {"allowed_field_classes": ["SYSTEM", "BEHAVIORAL"]},
                    "session_policy": {
                        "idle_timeout_seconds": 600,
                        "resume_grace_seconds": 120,
                    },
                },
                join_code=f"RAC{uuid.uuid4().hex[:6].upper()}",
                created_at=NOW,
            )
        )
        db.commit()

        participant = identity_store.create_participant(
            db,
            Participant(participant_id=uuid.uuid4(), account_id=user_id, created_at=NOW),
        )
        enrollment = Enrollment(
            enrollment_id=uuid.uuid4(),
            participant_id=participant.participant_id,
            study_id=study_id,
            participant_code=f"P-{uuid.uuid4().hex[:8]}",
            status=EnrollmentStatus.ACTIVE,
            eligibility=ResearchEligibility(eligible=True, evaluated_at=NOW),
            enrolled_at=NOW,
            consent_accepted_at=NOW,
            updated_at=NOW,
            retention_action=RetentionAction.RETAIN_ANONYMIZED,
        )
        identity_store.create_enrollment(db, enrollment)
        study = db.get(StudyRow, study_id)
        session = open_session(
            enrollment,
            study,
            manifest_digest="manifest-digest",
            context_id=f"managed-race-{uuid.uuid4()}",
        )
        session_row = session_store.create_session(db, session)
        return SimpleNamespace(
            user_id=user_id,
            parent_session_id=parent_session_id,
            study_id=study_id,
            enrollment_id=enrollment.enrollment_id,
            research_session_id=session_row.session_id,
        )
    finally:
        db.close()


@contextlib.contextmanager
def _managed_stubs(graph, *, funded_side_effect=None):
    """Stub assignment/funding context around the seeded graph.

    Persistence stays on the real DB. The fake assignment carries no
    ``profile_id``/``assignment_id`` so no profile/assignment rows are needed;
    the binding references the seeded enrollment/research session.
    """
    profile = SimpleNamespace(
        name="managed-arm",
        model="managed-model",
        approval_policy="auto",
        tools_json="[]",
        temperature=None,
        framework_version="code4me2-agent",
        profile_id=None,
        funding_owner_user_id=None,
        max_steps=3,
        max_context_tokens=1000,
    )
    assignment = SimpleNamespace(
        profile=profile, study_id=graph.study_id, assignment_id=None
    )
    binding = SimpleNamespace(
        enrollment_id=graph.enrollment_id,
        study_id=graph.study_id,
        research_session_id=graph.research_session_id,
    )
    policy = {
        "version": "1",
        "transport": "managed_backend",
        "agent_profile": "managed-arm",
        "model": "managed-model",
        "tools": [],
        "approval_policy": "auto",
        "max_iterations": 3,
        "max_context_tokens": 1000,
        "commands_allowlist": [],
        "store_agent_content": False,
    }
    with (
        patch(
            "agents.registry.resolve_assignment_context",
            return_value=assignment,
        ),
        patch("backend.routers.acp._require_funded_access", return_value=None),
        patch(
            "backend.routers.acp.access.resolve_research_binding",
            return_value=binding,
        ),
        patch("backend.routers.acp._managed_policy", return_value=policy),
        patch(
            "backend.routers.acp._require_funded_task",
            side_effect=funded_side_effect,
        ),
    ):
        yield SimpleNamespace(assignment=assignment, binding=binding, policy=policy)


def _scope_for(graph, *, project_id=None):
    return AcpServerAuthorization(
        acp_token="acp-token",
        user_id=str(graph.user_id),
        session_id=str(graph.parent_session_id),
        project_id=str(project_id or uuid.uuid4()),
        project_info={},
        workspace="/workspace",
    )


def _payload(response):
    return json.loads(response.body)


def _row_count(session_factory, run_id) -> int:
    db = session_factory()
    try:
        return db.execute(
            text(
                "SELECT count(*) FROM public.agent_task "
                "WHERE external_run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()
    finally:
        db.close()


class _OneShotRendezvous:
    """Rendezvous ONLY the first pre-read pair of the shared lookup.

    Recovery and scope-check lookups (calls 3+) pass through unwrapped — a
    reusable Barrier(2) on the shared lookup would deadlock those. Two-phase:
    both threads must finish their pre-read (both seeing no row) before
    either proceeds to the racing insert, so the loser's unique violation
    fires deterministically.
    """

    def __init__(self, real_lookup):
        self._real = real_lookup
        self._lock = threading.Lock()
        self._count = 0
        self._done = 0
        self._arrived = threading.Event()
        self._departed = threading.Event()

    def __call__(self, db, run_id, *args, **kwargs):
        with self._lock:
            self._count += 1
            call = self._count
            if call == 2:
                self._arrived.set()
        if call > 2:
            return self._real(db, run_id, *args, **kwargs)
        if call == 1:
            assert self._arrived.wait(timeout=30), "race partner never arrived"
        result = self._real(db, run_id, *args, **kwargs)
        with self._lock:
            self._done += 1
            if self._done == 2:
                self._departed.set()
        assert self._departed.wait(timeout=30), "race partner never finished pre-read"
        return result


def test_concurrent_identical_run_ids_collapse_to_one_row(db_runtime):
    session_factory = db_runtime
    engine = session_factory.kw["bind"]
    assert engine.dialect.driver == "psycopg2", "race must run on the pinned psycopg2 dialect"
    graph = _seed_graph(session_factory)

    run_id = f"race-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    gate = _OneShotRendezvous(crud.get_agent_task_by_external_run_id)
    real_create = crud.create_agent_task
    captured: list = []

    def recording_create(*args, **kwargs):
        try:
            return real_create(*args, **kwargs)
        except Exception as error:
            captured.append(error)
            raise

    outcomes: dict = {}

    def attempt(name):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        try:
            response = create_managed_run(body, app, scope)
            outcomes[name] = ("ok", response.status_code, _payload(response)["task_id"])
        except Exception as error:  # pragma: no cover - asserted below
            outcomes[name] = ("error", error)

    with (
        _managed_stubs(graph),
        patch(
            "database.crud.get_agent_task_by_external_run_id", new=gate
        ),
        patch("database.crud.create_agent_task", new=recording_create),
    ):
        threads = [
            threading.Thread(target=attempt, args=(name,), name=f"race-{name}")
            for name in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads), "race threads hung"
    assert sorted(outcomes) == ["a", "b"]
    assert all(outcome[0] == "ok" for outcome in outcomes.values()), outcomes
    statuses = sorted(outcome[1] for outcome in outcomes.values())
    assert statuses == [200, 201], f"expected one create plus one replay, got {statuses}"
    task_ids = {outcome[2] for outcome in outcomes.values()}
    assert len(task_ids) == 1, f"one consistent task identity required, got {task_ids}"

    assert len(captured) == 1, f"the race capture must fire exactly once, got {captured}"
    violation = captured[0]
    assert isinstance(violation, IntegrityError), type(violation)
    assert getattr(violation.orig, "pgcode", None) == "23505"
    constraint_name = getattr(getattr(violation.orig, "diag", None), "constraint_name", None)
    assert isinstance(constraint_name, str) and "external_run_id" in constraint_name

    assert _row_count(session_factory, run_id) == 1, "exactly one persisted row required"


def test_replay_rejects_foreign_scope(db_runtime):
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"scope-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    with _managed_stubs(graph):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        created = create_managed_run(body, app, scope)
        assert created.status_code == 201, created.body

        foreign = _scope_for(graph, project_id=uuid.uuid4())
        foreign_app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, foreign_app, foreign)
    assert error.value.status_code == 409
    assert _row_count(session_factory, run_id) == 1


def test_replay_refuses_unfunded_task(db_runtime):
    """A replay must still satisfy funded state: 403, never a 200 replay."""
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"unfunded-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    with _managed_stubs(graph):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        created = create_managed_run(body, app, scope)
        assert created.status_code == 201, created.body

    refused = HTTPException(
        status_code=403,
        detail={"code": "ENROLLMENT_REVOKED", "message": "enrollment is not active"},
    )
    with _managed_stubs(graph, funded_side_effect=refused):
        retry_app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, retry_app, scope)
    assert error.value.status_code == 403
    assert _row_count(session_factory, run_id) == 1


def test_replay_refuses_missing_policy_snapshot(db_runtime):
    """A replay without a dict policy snapshot is a 503, never a 200 replay."""
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"policy-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    with _managed_stubs(graph):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        created = create_managed_run(body, app, scope)
        assert created.status_code == 201, created.body

    db = session_factory()
    try:
        db.execute(
            text(
                "UPDATE public.agent_task SET policy_snapshot = '\"corrupt\"' "
                "WHERE external_run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        db.commit()
    finally:
        db.close()

    with _managed_stubs(graph):
        retry_app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, retry_app, scope)
    assert error.value.status_code == 503
    assert _row_count(session_factory, run_id) == 1


def _integrity_error(*, pgcode, constraint_name):
    orig = SimpleNamespace(
        pgcode=pgcode, diag=SimpleNamespace(constraint_name=constraint_name)
    )
    return IntegrityError("INSERT INTO public.agent_task", {}, orig)


@pytest.mark.parametrize(
    "failure",
    [
        _integrity_error(pgcode="23505", constraint_name="agent_task_pkey"),
        _integrity_error(pgcode="23503", constraint_name="agent_task_owner_user_id_fkey"),
        IntegrityError("INSERT INTO public.agent_task", {}, Exception("boom")),
    ],
    ids=["unique-other-constraint", "foreign-key", "no-pgcode"],
)
def test_unrelated_integrity_violation_never_replays(db_runtime, failure):
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"unrelated-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    def raise_failure(*args, **kwargs):
        raise failure

    with (
        _managed_stubs(graph),
        patch("database.crud.create_agent_task", new=raise_failure),
    ):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, app, scope)
    assert error.value.status_code == 500
    assert _row_count(session_factory, run_id) == 0


def test_generic_failure_never_replays(db_runtime):
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"generic-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")

    def raise_generic(*args, **kwargs):
        raise RuntimeError("boom")

    with (
        _managed_stubs(graph),
        patch("database.crud.create_agent_task", new=raise_generic),
    ):
        app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, app, scope)
    assert error.value.status_code == 500
    assert _row_count(session_factory, run_id) == 0


def test_post_commit_failure_then_retry_replays(db_runtime):
    """A failure after commit 500s (not a unique violation); a retry replays.

    The narrowed recovery branch cannot tell "row committed, then refresh
    failed" from "row never written" — both are non-23505 errors — so the
    first call is a logged 500 even though the row exists. The retry hits the
    pre-read path and replays 200 with the same task identity.
    """
    session_factory = db_runtime
    graph = _seed_graph(session_factory)
    run_id = f"postcommit-{uuid.uuid4()}"
    scope = _scope_for(graph)
    body = ManagedRunRequest(run_id=run_id, session_id=f"acp-session-{uuid.uuid4()}")
    real_create = crud.create_agent_task
    state = {"failed": False}

    def flaky_create(*args, **kwargs):
        task = real_create(*args, **kwargs)
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("simulated post-commit refresh failure")
        return task

    with (
        _managed_stubs(graph),
        patch("database.crud.create_agent_task", new=flaky_create),
    ):
        first_app = SimpleNamespace(get_db_session=lambda: session_factory())
        with pytest.raises(HTTPException) as error:
            create_managed_run(body, first_app, scope)
        assert error.value.status_code == 500
        assert _row_count(session_factory, run_id) == 1

        retry_app = SimpleNamespace(get_db_session=lambda: session_factory())
        retry = create_managed_run(body, retry_app, scope)
    assert retry.status_code == 200, retry.body

    db = session_factory()
    try:
        task_id = db.execute(
            text(
                "SELECT task_id FROM public.agent_task WHERE external_run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()
    finally:
        db.close()
    assert _payload(retry)["task_id"] == str(task_id)
