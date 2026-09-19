"""ISSUE-01 regression: metadata-only studies never store managed-run content.

Exercises the real canonical ingestion writer against PostgreSQL for a
research-bound managed task: structural facts are stored, content-bearing facts
are rejected before persistence, and no content text reaches the database.
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

from database import crud
from database.db_schemas import Study as StudyRow
from database.migration.migration_manager import MigrationManager
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus, RetentionAction
from research.participants.models import Enrollment, Participant, ResearchEligibility
from research.runtime.sessions import store as session_store
from research.runtime.sessions.service import open_session
from research.telemetry.adapters import CanonicalIngestionFailed
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


def _seed(db, *, content_capture: bool):
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
                "telemetry_policy": (
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
