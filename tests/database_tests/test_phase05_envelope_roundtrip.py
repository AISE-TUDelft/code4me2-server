"""Phase-05 real-PostgreSQL canonical-envelope round-trip (I01/I03/I05).

These use a disposable database (``TEST_DATABASE_URL``) to prove that the
*complete* validated ``CanonicalEventV1`` envelope — correlations, monotonic
time, lifecycle state, unknown-value fields, provenance, coverage and the
privacy summary — survives storage byte-for-byte, not just the flattened
searchable columns.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from database.research_schemas import ResearchEvent
from research.telemetry.ingestion.models import IngestionContext
from research.telemetry.ingestion.service import _record_from_event, compute_event_digest
from research.telemetry.ingestion.store import SqlAlchemyIngestionStore, row_to_record
from research.telemetry.models import (
    CanonicalEventV1,
    Correlations,
    Coverage,
    EventMetrics,
    PrivacySummary,
    Provenance,
)

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="function")
def engine():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def SessionFactory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _full_event() -> CanonicalEventV1:
    return CanonicalEventV1(
        event_id=uuid.uuid4(),
        schema_version="1",
        event_type="tool.completed",
        source="acp",
        occurred_at=NOW,
        monotonic_ns=987654321,
        emitter_id="acp-proxy",
        emitter_sequence=7,
        agent_run_id="run-1",
        lifecycle_state="tool.completed",
        correlations=Correlations(
            turn_id="turn-1",
            tool_call_id="tool-1",
            permission_id="perm-1",
            edit_id="edit-1",
            correlation_id="corr-1",
        ),
        payload={"tool_name": "read", "file_extension": "kt", "unknown_foo": "kept"},
        metrics=EventMetrics(usage_tokens=42, counts={"tool_calls": 2}),
        privacy=PrivacySummary(
            actions={"redacted": 1}, policy_digest="sha256:" + "p" * 64
        ),
        provenance=Provenance(
            source="acp",
            source_event_id="acp-1",
            normalizer_version="1",
            adapter_version="codex-v1",
            fidelity="exact",
            evidence_digest="sha256:" + "e" * 64,
        ),
        coverage=Coverage(state="AVAILABLE", reason=None, capability="TOOL_LIFECYCLE"),
        unknown_event_type=None,
        unknown_source=None,
        unknown_lifecycle_state=None,
    )


def _context(session) -> IngestionContext:
    """Seed the authorized study/session chain required by fresh-schema FKs."""
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    account_id = uuid.uuid4()
    study_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    enrollment_id = uuid.uuid4()
    session_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.\"user\" "
            "(user_id, joined_at, email, name, password, config_id) "
            "VALUES (:user_id, :joined_at, :email, 'Envelope', 'x', :config_id)"
        ),
        {
            "user_id": account_id,
            "joined_at": NOW,
            "email": f"envelope-{account_id}@example.com",
            "config_id": config_id,
        },
    )
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, is_active, is_research, "
            "research_status, research_config_json, created_at) "
            "VALUES (:study_id, 'Envelope study', :account_id, :started_at, true, true, "
            "'ACTIVE', '{}', :started_at)"
        ),
        {"study_id": study_id, "account_id": account_id, "started_at": NOW},
    )
    session.execute(
        text(
            "INSERT INTO public.research_participant "
            "(participant_id, account_id, created_at) VALUES (:participant_id, :account_id, :created_at)"
        ),
        {"participant_id": participant_id, "account_id": account_id, "created_at": NOW},
    )
    session.execute(
        text(
            "INSERT INTO public.research_enrollment "
            "(enrollment_id, participant_id, study_id, participant_code, status, "
            "revocation_epoch, eligibility_json, enrolled_at, updated_at, "
            "consent_accepted_at, retention_action) "
            "VALUES (:enrollment_id, :participant_id, :study_id, 'p-envelope', 'ACTIVE', 0, "
            "'{}', :started_at, :started_at, :started_at, 'RETAIN_ANONYMIZED')"
        ),
        {
            "enrollment_id": enrollment_id,
            "participant_id": participant_id,
            "study_id": study_id,
            "started_at": NOW,
        },
    )
    session.execute(
        text(
            "INSERT INTO public.research_session "
            "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
            "environment_json, transitions_json, created_at) "
            "VALUES (:session_id, :enrollment_id, :study_id, 'envelope-context', 'running', "
            "'manifest', '{}', '[]', :started_at)"
        ),
        {
            "session_id": session_id,
            "enrollment_id": enrollment_id,
            "study_id": study_id,
            "started_at": NOW,
        },
    )
    session.flush()
    return IngestionContext(
        study_id=study_id,
        enrollment_id=enrollment_id,
        research_session_id=session_id,
        revocation_epoch=0,
    )


def test_complete_envelope_survives_postgres_round_trip(SessionFactory):
    session = SessionFactory()
    try:
        event = _full_event()
        record = _record_from_event(
            event, _context(session), compute_event_digest(event), accepted_at=NOW
        )
        store = SqlAlchemyIngestionStore(session)
        store.insert_events([record])
        store.commit()

        row = session.get(ResearchEvent, event.event_id)
        assert row is not None
        reloaded = row_to_record(row)

        # The complete envelope is the authority and round-trips unchanged.
        assert reloaded.envelope == event.model_dump(mode="json")
        # Fields absent from the flattened searchable columns survive too.
        assert reloaded.envelope["monotonic_ns"] == 987654321
        assert reloaded.envelope["correlations"]["tool_call_id"] == "tool-1"
        assert reloaded.envelope["lifecycle_state"] == "tool.completed"
        # The searchable columns are derived from the same envelope.
        assert reloaded.event_type == "tool.completed"
        assert reloaded.emitter_sequence == 7
        assert reloaded.envelope["payload"]["unknown_foo"] == "kept"
        assert reloaded.envelope["metrics"]["counts"]["tool_calls"] == 2
        assert reloaded.envelope["provenance"]["adapter_version"] == "codex-v1"
        # Ingestion metadata stays outside the envelope.
        assert "digest" not in reloaded.envelope
        assert "accepted_at" not in reloaded.envelope
        assert reloaded.digest == compute_event_digest(event)
    finally:
        session.close()


def test_unknown_value_fields_are_preserved_not_reclassified(SessionFactory):
    session = SessionFactory()
    try:
        base = _full_event()
        event = base.model_copy(
            update={
                "event_type": "unknown_source_event",
                "source": "unknown",
                "unknown_event_type": "vendor.weird.thing",
                "unknown_source": "some-cli",
                "unknown_lifecycle_state": "quiescing",
            }
        )
        record = _record_from_event(
            event, _context(session), compute_event_digest(event), accepted_at=NOW
        )
        store = SqlAlchemyIngestionStore(session)
        store.insert_events([record])
        store.commit()

        reloaded = row_to_record(session.get(ResearchEvent, event.event_id))
        assert reloaded.envelope["unknown_event_type"] == "vendor.weird.thing"
        assert reloaded.envelope["unknown_source"] == "some-cli"
        assert reloaded.envelope["unknown_lifecycle_state"] == "quiescing"
    finally:
        session.close()
