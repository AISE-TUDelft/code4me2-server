"""Fresh-schema research lifecycle constraints."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.study.lifecycle import stop_research_study

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


def _fresh_session():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    return engine, sessionmaker(bind=engine)()


def _create_user(session):
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.user "
            "(user_id, joined_at, email, name, password, config_id) "
            "VALUES (:user_id, now(), :email, 'Owner', 'x', :config_id)"
        ),
        {
            "user_id": user_id,
            "email": f"schema-{user_id}@example.com",
            "config_id": config_id,
        },
    )
    session.commit()
    return user_id


def _create_study(session, owner_id):
    study_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, is_active, is_research, "
            "research_status, research_config_json, research_config_digest, join_code, created_at) "
            "VALUES (:study_id, 'schema study', :owner_id, now(), false, true, "
            "'DRAFT', '{}', 'study-digest', :join_code, now())"
        ),
        {"study_id": study_id, "owner_id": owner_id, "join_code": f"SCHEMA-{study_id}"},
    )
    session.commit()
    return study_id


def test_one_active_enrollment_and_one_assignment_per_enrollment():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        profile_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'schema profile', 'model', '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_agent_profile "
                "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                "VALUES (:study_id, :profile_id, 'profile-digest', '{}', 0, now())"
            ),
            {"study_id": study_id, "profile_id": profile_id},
        )
        participant_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_participant "
                "(participant_id, account_id, created_at) VALUES (:participant_id, :account_id, now())"
            ),
            {"participant_id": participant_id, "account_id": owner_id},
        )
        enrollment_id = uuid.uuid4()
        enrollment_values = {
            "enrollment_id": enrollment_id,
            "participant_id": participant_id,
            "study_id": study_id,
        }
        session.execute(
            text(
                "INSERT INTO public.research_enrollment "
                "(enrollment_id, participant_id, study_id, participant_code, status, "
                "revocation_epoch, eligibility_json, enrolled_at, updated_at, "
                "consent_accepted_at, retention_action) "
                "VALUES (:enrollment_id, :participant_id, :study_id, 'p_schema', 'ACTIVE', "
                "0, '{}', now(), now(), now(), 'RETAIN_ANONYMIZED')"
            ),
            {**enrollment_values, "participant_id": participant_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_assignment "
                "(assignment_id, enrollment_id, study_id, agent_profile_id, strategy, "
                "randomization_epoch, profile_digest, profile_snapshot_json, status, assigned_at) "
                "VALUES (:assignment_id, :enrollment_id, :study_id, :profile_id, 'RANDOM_EQUAL', "
                "0, 'profile-digest', '{}', 'ACTIVE', now())"
            ),
            {
                "assignment_id": uuid.uuid4(),
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "profile_id": profile_id,
            },
        )
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO public.study_assignment "
                    "(assignment_id, enrollment_id, study_id, agent_profile_id, strategy, "
                    "randomization_epoch, profile_digest, profile_snapshot_json, status, assigned_at) "
                    "VALUES (:assignment_id, :enrollment_id, :study_id, :profile_id, 'RANDOM_EQUAL', "
                    "0, 'profile-digest', '{}', 'ACTIVE', now())"
                ),
                {
                    "assignment_id": uuid.uuid4(),
                    "enrollment_id": enrollment_id,
                    "study_id": study_id,
                    "profile_id": profile_id,
                },
            )
            session.flush()
        session.rollback()
        second_study_id = _create_study(session, owner_id)
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO public.research_enrollment "
                    "(enrollment_id, participant_id, study_id, participant_code, status, "
                    "revocation_epoch, eligibility_json, enrolled_at, updated_at, "
                    "consent_accepted_at, retention_action) "
                    "VALUES (:enrollment_id, :participant_id, :study_id, 'p_schema_2', 'ACTIVE', "
                    "0, '{}', now(), now(), now(), 'RETAIN_ANONYMIZED')"
                ),
                {
                    "enrollment_id": uuid.uuid4(),
                    "participant_id": participant_id,
                    "study_id": second_study_id,
                },
            )
            session.flush()
        session.rollback()
        session.execute(
            text(
                "UPDATE public.agent_profile SET model = 'changed-model' "
                "WHERE profile_id = :profile_id"
            ),
            {"profile_id": profile_id},
        )
        session.commit()
        assert session.execute(
            text(
                "SELECT profile_digest, profile_snapshot_json "
                "FROM public.study_assignment WHERE enrollment_id = :enrollment_id"
            ),
            {"enrollment_id": enrollment_id},
        ).one() == ("profile-digest", {})
    finally:
        session.close()
        engine.dispose()


def test_stopping_study_preserves_research_rows_and_revokes_collection():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        participant_id = uuid.uuid4()
        enrollment_id = uuid.uuid4()
        session_id = uuid.uuid4()
        run_id = uuid.uuid4()
        event_id = uuid.uuid4()
        receipt_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_participant "
                "(participant_id, account_id, created_at) VALUES (:participant_id, :account_id, now())"
            ),
            {"participant_id": participant_id, "account_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.research_enrollment "
                "(enrollment_id, participant_id, study_id, participant_code, status, "
                "revocation_epoch, eligibility_json, enrolled_at, updated_at, "
                "consent_accepted_at, retention_action) "
                "VALUES (:enrollment_id, :participant_id, :study_id, 'p_stop', 'ACTIVE', "
                "0, '{}', now(), now(), now(), 'RETAIN_ANONYMIZED')"
            ),
            {
                "enrollment_id": enrollment_id,
                "participant_id": participant_id,
                "study_id": study_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO public.research_session "
                "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, "
                "environment_json, transitions_json, created_at) "
                "VALUES (:session_id, :enrollment_id, :study_id, 'ctx-stop', 'running', "
                "'manifest', '{}', '[]', now())"
            ),
            {
                "session_id": session_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO public.research_agent_run "
                "(agent_run_id, research_session_id, started_at) "
                "VALUES (:run_id, :session_id, now())"
            ),
            {"run_id": run_id, "session_id": session_id},
        )
        session.execute(
            text(
                "INSERT INTO public.research_event "
                "(event_id, schema_version, event_type, source, study_id, enrollment_id, "
                "research_session_id, emitter_id, emitter_sequence, occurred_at, "
                "envelope_json, digest, accepted_at) "
                "VALUES (:event_id, 'v1', 'test', 'test', :study_id, :enrollment_id, "
                ":session_id, 'emitter', 1, now(), '{}', 'event-digest', now())"
            ),
            {
                "event_id": event_id,
                "study_id": study_id,
                "enrollment_id": enrollment_id,
                "session_id": session_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO public.telemetry_batch_receipt "
                "(receipt_id, batch_id, enrollment_id, research_session_id, accepted_at, receipt_json) "
                "VALUES (:receipt_id, 'batch-stop', :enrollment_id, :session_id, now(), '{}')"
            ),
            {
                "receipt_id": receipt_id,
                "enrollment_id": enrollment_id,
                "session_id": session_id,
            },
        )
        session.commit()

        summary = stop_research_study(session, study_id, actor="researcher")
        assert summary.enrollment_count == 1
        assert summary.session_count == 1

        assert session.execute(
            text("SELECT research_status FROM public.study WHERE study_id = :study_id"),
            {"study_id": study_id},
        ).scalar_one() == "STUDY_STOPPED"
        assert session.execute(
            text("SELECT status, revocation_epoch FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).one() == ("STUDY_STOPPED", 1)
        assert session.execute(
            text("SELECT state, close_reason FROM public.research_session WHERE session_id = :id"),
            {"id": session_id},
        ).one() == ("revoked", "STUDY_STOPPED")
        for table, key, value in (
            ("research_agent_run", "agent_run_id", run_id),
            ("research_event", "event_id", event_id),
            ("telemetry_batch_receipt", "receipt_id", receipt_id),
        ):
            assert session.execute(
                text(f"SELECT count(*) FROM public.{table} WHERE {key} = :value"),
                {"value": value},
            ).scalar_one() == 1
    finally:
        session.close()
        engine.dispose()
