"""ISSUE-01 regression: metadata-only studies never store managed-run content.

Exercises the real canonical ingestion writer against PostgreSQL for a
research-bound managed task: structural facts are stored, content-bearing facts
are rejected before persistence, and no content text reaches the database.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database import crud
from database.db_schemas import Study as StudyRow
from database.migration.migration_manager import MigrationManager
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus, RetentionAction
from research.participants.models import Enrollment, Participant, ResearchEligibility
from research.runtime.sessions import store as session_store
from research.runtime.sessions.service import open_session
from research.telemetry.adapters import CanonicalIngestionFailed
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore
from research.telemetry.models import CanonicalEventV1, Coverage, Provenance
from agents.ingest import ingest_event_batch

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime.now(timezone.utc)

CONTENT_SENTINEL = "secret source payload that must never be stored"


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


def _seed_user(db) -> uuid.UUID:
    config_id = db.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO public.\"user\" "
            "(user_id, joined_at, email, name, password, config_id, verified, is_admin) "
            "VALUES (:user_id, :joined_at, :email, 'Managed content', 'x', :config_id, true, false)"
        ),
        {
            "user_id": user_id,
            "joined_at": NOW,
            "email": f"managed-{user_id}@example.com",
            "config_id": config_id,
        },
    )
    return user_id


def _seed(db, *, content_capture: bool, telemetry_policy: dict | None = None):
    user_id = _seed_user(db)
    study_id = uuid.uuid4()
    db.add(
        StudyRow(
            study_id=study_id,
            name="Managed content study",
            created_by=user_id,
            starts_at=NOW,
            is_active=True,
            is_research=True,
            research_status="ACTIVE",
            research_config_json={
                "telemetry_policy": telemetry_policy
                if telemetry_policy is not None
                else (
                    {"content_capture": True}
                    if content_capture
                    else {"allowed_field_classes": ["SYSTEM", "BEHAVIORAL"]}
                ),
                "session_policy": {
                    "idle_timeout_seconds": 600,
                    "resume_grace_seconds": 120,
                },
            },
            join_code=f"MNG{uuid.uuid4().hex[:6].upper()}",
            created_at=NOW,
        )
    )
    db.commit()

    participant = identity_store.create_participant(
        db,
        Participant(participant_id=uuid.uuid4(), account_id=user_id, created_at=NOW),
    )
    enrollment_domain = Enrollment(
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
    identity_store.create_enrollment(db, enrollment_domain)

    study = db.get(StudyRow, study_id)
    session = open_session(
        enrollment_domain,
        study,
        manifest_digest="manifest-digest",
        context_id="managed-content-context",
    )
    session_row = session_store.create_session(db, session)

    task = crud.create_agent_task(
        db,
        agent_profile="managed-arm",
        model="managed-model",
        approval_policy="auto",
        tools_json="[]",
        source="code4me2_agent",
        owner_user_id=user_id,
        external_run_id=f"run-{uuid.uuid4()}",
        agent_session_id="acp-session",
        status="running",
        study_id=study_id,
        enrollment_id=enrollment_domain.enrollment_id,
        research_session_id=session_row.session_id,
    )
    return task


def _content_event() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": "run-1",
        "schema_version": "1",
        "event_type": "tool.completed",
        "timestamp": NOW.isoformat(),
        "sequence": 1,
        "source": "code4me2_agent",
        "session_id": "acp-session",
        "request_id": "req-1",
        "payload": {"tool_name": "read_file", "result": CONTENT_SENTINEL},
        "raw_payload": {"result": CONTENT_SENTINEL},
        "metrics": {"duration_ms": 5},
    }


def test_metadata_only_content_is_redacted_before_persistence(db_runtime):
    session_factory = db_runtime
    db = session_factory()
    try:
        task = _seed(db, content_capture=False)
        ingested, _skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[_content_event()],
            content_included=False,
            agent_profile="managed-arm",
        )
        assert ingested == 1

        envelope = db.execute(
            text(
                "SELECT envelope_json FROM public.research_event "
                "WHERE agent_run_id = :run_id"
            ),
            {"run_id": task.external_run_id},
        ).scalar_one()
        serialized = json.dumps(envelope)
        assert CONTENT_SENTINEL not in serialized
        assert "payload" not in envelope.get("payload", {})
        assert "legacy_kind" in envelope["payload"]
    finally:
        db.close()


def test_forced_content_under_a_metadata_only_policy_is_rejected(db_runtime):
    """Defense in depth: even if a caller claims content permission, the study
    policy is re-applied by the canonical writer and the fact is not stored."""
    session_factory = db_runtime
    db = session_factory()
    try:
        task = _seed(db, content_capture=False)
        with pytest.raises(CanonicalIngestionFailed) as error:
            ingest_event_batch(
                db,
                task_id=task.task_id,
                events=[_content_event()],
                content_included=True,
                agent_profile="managed-arm",
            )
        assert error.value.reason == "REJECTED"
        db.rollback()

        stored = db.execute(
            text(
                "SELECT count(*) FROM public.research_event "
                "WHERE agent_run_id = :run_id"
            ),
            {"run_id": task.external_run_id},
        ).scalar_one()
        assert stored == 0, "policy-blocked content must not be persisted"
    finally:
        db.close()


def test_content_enabled_policy_allows_content_capture(db_runtime):
    session_factory = db_runtime
    db = session_factory()
    try:
        task = _seed(db, content_capture=True)
        ingested, _skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[_content_event()],
            content_included=True,
            agent_profile="managed-arm",
        )
        assert ingested == 1

        envelope = db.execute(
            text(
                "SELECT envelope_json FROM public.research_event "
                "WHERE agent_run_id = :run_id"
            ),
            {"run_id": task.external_run_id},
        ).scalar_one()
        assert CONTENT_SENTINEL in json.dumps(envelope)
    finally:
        db.close()


def _structural_event() -> dict:
    event = _content_event()
    event["payload"] = {"tool_name": "read_file"}
    event.pop("raw_payload", None)
    return event


def test_a_policy_without_agent_activity_keeps_the_reports_without_it(db_runtime):
    """Usage and timings only: the built-in agent's report is stored without its
    behavioural fields instead of being refused outright."""
    session_factory = db_runtime
    db = session_factory()
    try:
        task = _seed(db, content_capture=False, telemetry_policy={"allowed_field_classes": ["METRICS"]})
        ingested, _skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[_structural_event()],
            content_included=False,
            agent_profile="managed-arm",
        )
        assert ingested == 1

        envelope = db.execute(
            text(
                "SELECT envelope_json FROM public.research_event "
                "WHERE agent_run_id = :run_id"
            ),
            {"run_id": task.external_run_id},
        ).scalar_one()
        # tool_name and legacy_kind are behavioural: excluded by the policy.
        assert "tool_name" not in envelope["payload"]
        assert "legacy_kind" not in envelope["payload"]
    finally:
        db.close()


def test_forced_content_is_still_refused_when_agent_activity_is_excluded(db_runtime):
    session_factory = db_runtime
    db = session_factory()
    try:
        task = _seed(db, content_capture=False, telemetry_policy={"allowed_field_classes": ["METRICS"]})
        with pytest.raises(CanonicalIngestionFailed) as error:
            ingest_event_batch(
                db,
                task_id=task.task_id,
                events=[_content_event()],
                content_included=True,
                agent_profile="managed-arm",
            )
        assert error.value.reason == "REJECTED"
        db.rollback()
        stored = db.execute(
            text("SELECT count(*) FROM public.research_event WHERE agent_run_id = :run_id"),
            {"run_id": task.external_run_id},
        ).scalar_one()
        assert stored == 0
    finally:
        db.close()


def test_relay_model_and_tool_calls_are_stored_under_the_default_policy(db_runtime):
    """The inference relay's own facts pass the ingestion privacy check.

    Its payload keys must be recognised metadata: an unknown key counts as
    content, and a study that does not capture content (the default) would
    refuse every relay model call and tool call.
    """
    from agents import event_writer
    from agents.telemetry import InferenceRecord
    from research.telemetry.adapters import record_legacy_facts

    session_factory = db_runtime
    db = session_factory()
    try:
        # The default policy: nothing declared, no content capture.
        task = _seed(db, content_capture=False, telemetry_policy={})
        record = InferenceRecord(
            request_id="req-1",
            model="managed-model",
            streaming=True,
            message_count=3,
            latency_ms=5,
            upstream_status=200,
            prompt_tokens=900,
            completion_tokens=50,
            total_tokens=950,
        )
        extra = {
            "wire_api": "chat_completions",
            "upstream_base_url": "https://provider.example/v1",
            "openai_passthrough": False,
            "meta_request": False,
            "requested_model": "managed-model",
            "tool_schema_bytes": 800,
        }
        arguments = '{"path": "src/app.py"}'
        facts = [
            event_writer._model_call_fact(
                record, 5, {"step_index": 2, "span_id": "span-1", "context_window_size_bytes": 12000}, extra
            ),
            event_writer._tool_call_fact(
                {"name": "read_file", "arguments": arguments, "result": "print()", "id": "call-1"}, 7, None
            ),
        ]
        result = record_legacy_facts(db, task=task, facts=facts)
        assert result is not None and result.written, result
        db.commit()

        rows = db.execute(
            text(
                "SELECT event_type, envelope_json FROM public.research_event "
                "WHERE agent_run_id = :run_id ORDER BY emitter_sequence"
            ),
            {"run_id": task.external_run_id},
        ).all()
        assert [row.event_type for row in rows] == ["agent.message.completed", "tool.completed"]
        model_call, tool_call = (row.envelope_json for row in rows)
        assert model_call["metrics"]["counts"]["step_index"] == 2
        assert model_call["metrics"]["counts"]["prompt_tokens"] == 900
        assert model_call["payload"]["call_mode"] == "streaming"
        assert model_call["payload"]["api_kind"] == "chat_completions"
        # The relay reports the tool schema size in ``extra``; the dashboard reads the payload.
        assert model_call["payload"]["tool_schema_bytes"] == 800
        assert model_call["payload"]["context_window_size_bytes"] == 12000
        assert tool_call["metrics"]["counts"]["tool_arguments_length"] == len(arguments)
        assert tool_call["metrics"]["counts"]["tool_result_length"] == len("print()")
        serialized = json.dumps([model_call, tool_call])
        assert "provider.example" not in serialized
        assert "[REDACTED]" not in serialized
    finally:
        db.close()


def _runtime_event(event_type: str, payload: dict) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": "run-1",
        "schema_version": "1",
        "event_type": event_type,
        "timestamp": NOW.isoformat(),
        "sequence": 1,
        "source": "code4me2_agent",
        "session_id": "acp-session",
        "request_id": "req-1",
        "payload": payload,
        "metrics": {"duration_ms": 5},
    }


def _permission_decided() -> dict:
    return _runtime_event(
        "agent.permission.decided",
        {
            "tool_name": "write_file",
            "tool_call_id": "tc-1",
            "kind": "edit",
            "decision": "accepted",
            "decision_scope": "once",
        },
    )


def test_self_reports_outside_a_study_keep_the_legacy_write(db_runtime):
    """A task without a research binding still stores its events in agent_event.

    The column mapping also carries keys only the canonical fact uses (run id,
    permission decision, ...); the legacy table has no column for them.
    """
    db = db_runtime()
    try:
        task = crud.create_agent_task(
            db,
            agent_profile="personal",
            model="personal-model",
            approval_policy="per_step",
            tools_json="[]",
            source="code4me2_agent",
            owner_user_id=_seed_user(db),
            external_run_id=f"run-{uuid.uuid4()}",
            agent_session_id="acp-session",
            status="running",
        )
        ingested, skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[
                _runtime_event(
                    "agent.tool.completed",
                    {"tool_name": "write_file", "tool_call_id": "tc-1"},
                ),
                _permission_decided(),
            ],
            content_included=False,
            agent_profile="personal",
        )
        assert (ingested, skipped) == (2, [])
        event_types = db.execute(
            text(
                "SELECT event_type FROM public.agent_event "
                "WHERE task_id = :task_id ORDER BY event_index"
            ),
            {"task_id": task.task_id},
        ).scalars().all()
        assert event_types == ["tool_call", "permission_decided"]
    finally:
        db.close()


def test_self_report_batches_continue_one_emitter_sequence(db_runtime):
    """Consecutive self-report batches number their canonical events without gaps."""
    db = db_runtime()
    try:
        task = _seed(db, content_capture=False)
        for size in (2, 3):
            ingested, _skipped = ingest_event_batch(
                db,
                task_id=task.task_id,
                events=[_permission_decided() for _ in range(size)],
                content_included=False,
                agent_profile="managed-arm",
            )
            assert ingested == size
        sequences = db.execute(
            text(
                "SELECT emitter_sequence FROM public.research_event "
                "WHERE agent_run_id = :run_id ORDER BY emitter_sequence"
            ),
            {"run_id": task.external_run_id},
        ).scalars().all()
        assert sequences == [1, 2, 3, 4, 5]
    finally:
        db.close()


def _store_acp_event(db, task, event_type: str, payload: dict) -> None:
    """Store one ACP-proxy event of the task's run directly."""
    event = CanonicalEventV1(
        event_id=uuid.uuid4(),
        schema_version="1",
        event_type=event_type,
        source="acp",
        study_id=task.study_id,
        enrollment_id=task.enrollment_id,
        research_session_id=task.research_session_id,
        agent_run_id=task.external_run_id,
        occurred_at=NOW,
        emitter_id="proxy-1",
        emitter_sequence=1,
        payload=payload,
        provenance=Provenance(source="acp", normalizer_version="test"),
        coverage=Coverage(state="AVAILABLE"),
    )
    context = IngestionContext(
        study_id=task.study_id,
        enrollment_id=task.enrollment_id,
        research_session_id=task.research_session_id,
        revocation_epoch=0,
    )
    SqlAlchemyIngestionStore(db).insert_events(
        [_record_from_event(event, context, compute_event_digest(event), accepted_at=NOW)]
    )


def test_the_agent_dashboard_rates_only_decisions_someone_was_asked_for(db_runtime):
    """Edit acceptance reads the agent's own reports, and only decisions someone made."""
    from research.analysis.read_models.dashboard import agent_overview

    db = db_runtime()
    try:
        task = _seed(db, content_capture=False)
        outcomes = [
            ("accepted", "once"),
            ("rejected", "once"),
            ("accepted", "policy"),
            ("accepted", "session_cached"),
            ("unavailable", "none"),
        ]
        events = [
            _runtime_event(
                "agent.permission.decided",
                {
                    "tool_name": "write_file",
                    "tool_call_id": f"tc-{index}",
                    "decision": decision,
                    "decision_scope": scope,
                },
            )
            for index, (decision, scope) in enumerate(outcomes)
        ]
        ingested, _skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=events,
            content_included=False,
            agent_profile="managed-arm",
        )
        assert ingested == len(outcomes)
        # The proxy's record of the same run's round-trip is not read here.
        _store_acp_event(db, task, "permission.decided", {"decision": "allow"})
        db.commit()

        owner = SimpleNamespace(user_id=task.owner_user_id, is_admin=False)
        # agent_task.created_at is naive server-local time; a day of slack keeps
        # the task inside the window whatever the host's time zone.
        summary = agent_overview(
            db, owner, time_window="7d", now=NOW + timedelta(days=1)
        )["summary"]
        assert summary["total_edits"] == 2
        assert summary["edit_acceptance_rate"] == 0.5
    finally:
        db.close()


@pytest.mark.parametrize(
    "allowed_field_classes",
    [["SYSTEM", "BEHAVIORAL"], ["METRICS"]],
    ids=["behavioural-kept", "behavioural-stripped"],
)
def test_the_run_detail_names_the_request_of_each_event(db_runtime, allowed_field_classes):
    """The run timeline carries the reported request id, also when the study
    policy strips the behavioural payload field that repeats it."""
    from research.analysis.read_models.dashboard import agent_run_detail

    db = db_runtime()
    try:
        task = _seed(
            db,
            content_capture=False,
            telemetry_policy={"allowed_field_classes": allowed_field_classes},
        )
        ingested, _skipped = ingest_event_batch(
            db,
            task_id=task.task_id,
            events=[_permission_decided()],
            content_included=False,
            agent_profile="managed-arm",
        )
        assert ingested == 1
        db.commit()

        owner = SimpleNamespace(user_id=task.owner_user_id, is_admin=False)
        detail = agent_run_detail(db, owner, task_id=str(task.task_id))
        assert [event["request_id"] for event in detail["events"]] == ["req-1"]
    finally:
        db.close()

