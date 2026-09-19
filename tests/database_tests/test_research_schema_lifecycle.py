"""Fresh-schema research lifecycle constraints."""

from __future__ import annotations

import os
import json
import random
import threading
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from database import crud
from database.crud import ProfileLockedError
from research.study.lifecycle import (
    clone_stopped_research_study,
    open_study_enrollment,
    revoke_research_enrollment,
    stop_research_study,
    update_research_metadata,
)
from research.study.protocol import store as study_store
from research.canonical import canonical_hash


def _qualified_release_json(release_id: str, *, agent_id: str) -> str:
    """A PACKAGED release with real conformance evidence (ISSUE-10/ISSUE-17).

    Minimal seed rows with an empty ``release_json`` can no longer be selected:
    qualification is derived from evidence bound to the exact artifact.
    """
    digest = "a" * 64
    return json.dumps(
        {
            "agent_id": agent_id,
            "release_id": release_id,
            "version": "1.0.0",
            "source_manifest_digest": "sha256:" + digest,
            "distribution_mode": "PACKAGED",
            "artifacts": [
                {
                    "os": "macos",
                    "arch": "arm64",
                    "path": "pkg/macos-arm64.tar.gz",
                    "sha256": digest,
                    "size": 1,
                }
            ],
            "conformance": [
                {
                    "status": "PASS",
                    "artifact_digest": digest,
                    "host": {"os": "macos", "arch": "arm64"},
                    "case_results": [{"case_id": "install", "status": "PASS"}],
                }
            ],
        }
    )

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
        {"study_id": study_id, "owner_id": owner_id, "join_code": f"SCHEMA-{study_id}".upper()},
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


def test_web_consent_creates_equal_random_assignment_and_is_idempotent():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        profile_ids = [uuid.uuid4(), uuid.uuid4()]
        for index, profile_id in enumerate(profile_ids):
            session.execute(
                text(
                    "INSERT INTO public.agent_profile "
                    "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                    "VALUES (:profile_id, :owner_id, :name, 'model', '[]', 'auto', 1)"
                ),
                {"profile_id": profile_id, "owner_id": owner_id, "name": f"profile-{index}"},
            )
            session.execute(
                text(
                    "INSERT INTO public.study_agent_profile "
                    "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                    "VALUES (:study_id, :profile_id, :digest, :snapshot, :selection_order, now())"
                ),
                {
                    "study_id": study_id,
                    "profile_id": profile_id,
                    "digest": f"digest-{index}",
                    "snapshot": '{"model":"model"}',
                    "selection_order": index,
                },
            )
        session.commit()

        first = open_study_enrollment(
            session, owner_id, f"SCHEMA-{study_id}".upper(), rng=random.Random(7)
        )
        second = open_study_enrollment(session, owner_id, f"SCHEMA-{study_id}".upper())
        assert first.created is True
        assert first.reused is False
        assert second.created is False
        assert second.reused is True
        assert second.enrollment_id == first.enrollment_id
        assert second.agent_profile_id == first.agent_profile_id
        assert session.execute(
            text("SELECT count(*) FROM public.study_assignment WHERE enrollment_id = :id"),
            {"id": first.enrollment_id},
        ).scalar_one() == 1
        assert session.execute(
            text("SELECT research_status, consent_locked_at FROM public.study WHERE study_id = :id"),
            {"id": study_id},
        ).one()[0] == "ACTIVE"
    finally:
        session.close()
        engine.dispose()


def test_historical_run_and_assignment_snapshots_survive_profile_edit():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        profile_id = uuid.uuid4()
        snapshot = {"profile_id": str(profile_id), "model": "before-edit"}
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'historical', 'before-edit', '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_agent_profile "
                "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                "VALUES (:study_id, :profile_id, 'digest-before', :snapshot, 0, now())"
            ),
            {
                "study_id": study_id,
                "profile_id": profile_id,
                "snapshot": json.dumps(snapshot),
            },
        )
        session.commit()
        enrollment = open_study_enrollment(session, owner_id, f"SCHEMA-{study_id}".upper())
        run_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_agent_run "
                "(agent_run_id, agent_release_id, assignment_id, agent_profile_id, "
                "profile_digest, profile_snapshot_json, started_at) "
                "SELECT :run_id, 'release-before', assignment_id, agent_profile_id, "
                "profile_digest, profile_snapshot_json, now() "
                "FROM public.study_assignment WHERE assignment_id = :assignment_id"
            ),
            {"run_id": run_id, "assignment_id": enrollment.assignment_id},
        )
        session.execute(
            text("UPDATE public.agent_profile SET model = 'after-edit' WHERE profile_id = :profile_id"),
            {"profile_id": profile_id},
        )
        session.commit()
        assignment = session.execute(
            text("SELECT profile_digest, profile_snapshot_json FROM public.study_assignment WHERE assignment_id = :id"),
            {"id": enrollment.assignment_id},
        ).one()
        run = session.execute(
            text("SELECT assignment_id, agent_profile_id, profile_digest, profile_snapshot_json FROM public.research_agent_run WHERE agent_run_id = :id"),
            {"id": run_id},
        ).one()
        assert assignment == ("digest-before", snapshot)
        assert run == (enrollment.assignment_id, profile_id, "digest-before", snapshot)
    finally:
        session.close()
        engine.dispose()


def test_concurrent_first_consent_creates_one_enrollment_and_assignment():
    engine, session = _fresh_session()
    sessions = []
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        profile_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'concurrent', 'model', '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_agent_profile "
                "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                "VALUES (:study_id, :profile_id, 'digest', '{\"model\":\"model\"}', 0, now())"
            ),
            {"study_id": study_id, "profile_id": profile_id},
        )
        session.commit()
        session.close()
        session = None
        session_factory = sessionmaker(bind=engine)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def consent():
            db = session_factory()
            sessions.append(db)
            try:
                barrier.wait(timeout=5)
                results.append(open_study_enrollment(db, owner_id, f"SCHEMA-{study_id}".upper()))
            except Exception as error:  # pragma: no cover - assertion reports the worker error
                errors.append(error)
                db.rollback()

        workers = [threading.Thread(target=consent) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            assert not worker.is_alive(), "consent worker retained a database lock"
        assert not errors
        assert len(results) == 2
        assert sum(result.created for result in results) == 1
        with session_factory() as check_session:
            assert check_session.execute(
                text("SELECT count(*) FROM public.research_enrollment WHERE participant_id = (SELECT participant_id FROM public.research_participant WHERE account_id = :account_id)"),
                {"account_id": owner_id},
            ).scalar_one() == 1
            assert check_session.execute(
                text("SELECT count(*) FROM public.study_assignment WHERE study_id = :study_id"),
                {"study_id": study_id},
            ).scalar_one() == 1
    finally:
        if session is not None:
            session.close()
        for db in sessions:
            db.close()
        engine.dispose()


def test_failed_consent_does_not_commit_first_use_participant_mapping():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        with pytest.raises(ValueError, match="no selected agent profiles"):
            open_study_enrollment(session, owner_id, f"SCHEMA-{study_id}".upper())
        session.rollback()
        assert session.execute(
            text(
                "SELECT count(*) FROM public.research_participant "
                "WHERE account_id = :account_id"
            ),
            {"account_id": owner_id},
        ).scalar_one() == 0
    finally:
        session.close()
        engine.dispose()


def test_study_creation_freezes_owned_profiles_and_rejects_foreign_profiles():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        foreign_owner_id = _create_user(session)
        foreign_profile_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'foreign', 'model', '[]', 'auto', 1)"
            ),
            {"profile_id": foreign_profile_id, "owner_id": foreign_owner_id},
        )
        session.commit()

        with pytest.raises(PermissionError):
            study_store.create_study(
                session,
                study_id=uuid.uuid4(),
                name="Rejected study",
                created_by=owner_id,
                join_code=f"REJECT-{uuid.uuid4()}".upper(),
                profile_ids=[foreign_profile_id],
            )
        session.rollback()

        owned_profile_id = uuid.uuid4()
        release_id = f"release-{uuid.uuid4()}"
        session.execute(
            text(
                "INSERT INTO public.agent_release "
                "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
                "VALUES (:release_id, 'test-agent', 'manifest-digest', 'QUALIFIED', "
                "CAST(:release_json AS jsonb), now())"
            ),
            {
                "release_id": release_id,
                "release_json": _qualified_release_json(
                    release_id, agent_id="test-agent"
                ),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'owned', 'model', :release_id, '[]', 'auto', 1)"
            ),
            {"profile_id": owned_profile_id, "owner_id": owner_id, "release_id": release_id},
        )
        session.commit()
        study = study_store.create_study(
            session,
            study_id=uuid.uuid4(),
            name="Frozen study",
            created_by=owner_id,
            join_code=f"FROZEN-{uuid.uuid4()}".upper(),
            research_config_json={"telemetry_policy": {"metadata_only": True}},
            profile_ids=[owned_profile_id],
        )
        assert study.research_config_digest == canonical_hash(
            {
                "telemetry_policy": {"metadata_only": True},
                "profile_ids": [str(owned_profile_id)],
            }
        )
        stored_profile = session.execute(
            text(
                "SELECT profile_digest, profile_snapshot_json, selection_order "
                "FROM public.study_agent_profile WHERE study_id = :study_id"
            ),
            {"study_id": study.study_id},
        ).one()
        assert stored_profile[2] == 0
        assert stored_profile[1]["profile_id"] == str(owned_profile_id)
        assert stored_profile[0]
    finally:
        session.close()
        engine.dispose()


def test_active_study_locks_profile_edits_until_stop_and_keeps_digest():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        profile_id = uuid.uuid4()
        release_id = f"release-{uuid.uuid4()}"
        session.execute(
            text(
                "INSERT INTO public.agent_release "
                "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
                "VALUES (:release_id, 'test-agent', 'manifest-digest', 'QUALIFIED', "
                "CAST(:release_json AS jsonb), now())"
            ),
            {
                "release_id": release_id,
                "release_json": _qualified_release_json(
                    release_id, agent_id="test-agent"
                ),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'locked', 'model', :release_id, '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id, "release_id": release_id},
        )
        session.commit()
        study = study_store.create_study(
            session,
            study_id=uuid.uuid4(),
            name="Lock study",
            created_by=owner_id,
            join_code=f"LOCK-{uuid.uuid4()}".upper(),
            profile_ids=[profile_id],
        )
        open_study_enrollment(session, owner_id, study.join_code)

        with pytest.raises(ProfileLockedError):
            crud.update_agent_profile(session, profile_id, model="blocked-model")
        session.rollback()
        with pytest.raises(ProfileLockedError):
            crud.delete_agent_profile(session, profile_id)
        session.rollback()

        stop_research_study(session, study.study_id, actor="owner")
        updated = crud.update_agent_profile(session, profile_id, model="stopped-model")
        assert updated.model == "stopped-model"
        assert updated.configuration_digest
    finally:
        session.close()
        engine.dispose()


def test_profile_stays_locked_until_last_active_study_stops():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        second_owner_id = _create_user(session)
        profile_id = uuid.uuid4()
        release_id = f"release-{uuid.uuid4()}"
        session.execute(
            text(
                "INSERT INTO public.agent_release "
                "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
                "VALUES (:release_id, 'test-agent', 'manifest-digest', 'QUALIFIED', "
                "CAST(:release_json AS jsonb), now())"
            ),
            {
                "release_id": release_id,
                "release_json": _qualified_release_json(
                    release_id, agent_id="test-agent"
                ),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'shared-lock', 'model', :release_id, '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id, "release_id": release_id},
        )
        study_ids = [_create_study(session, owner_id), _create_study(session, second_owner_id)]
        for study_id in study_ids:
            session.execute(
                text(
                    "INSERT INTO public.study_agent_profile "
                    "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                    "VALUES (:study_id, :profile_id, 'digest', '{}', 0, now())"
                ),
                {"study_id": study_id, "profile_id": profile_id},
            )
            session.execute(
                text(
                    "UPDATE public.study SET is_active = true, research_status = 'ACTIVE' "
                    "WHERE study_id = :study_id"
                ),
                {"study_id": study_id},
            )
        session.commit()

        with pytest.raises(ProfileLockedError):
            crud.update_agent_profile(session, profile_id, model="blocked")
        session.rollback()
        stop_research_study(session, study_ids[0], actor="owner")
        with pytest.raises(ProfileLockedError):
            crud.update_agent_profile(session, profile_id, model="still-blocked")
        session.rollback()
        stop_research_study(session, study_ids[1], actor="owner")
        updated = crud.update_agent_profile(session, profile_id, model="unlocked")
        assert updated.model == "unlocked"
    finally:
        session.close()
        engine.dispose()


def test_agent_task_crud_uses_current_profile_bound_columns():
    engine, session = _fresh_session()
    try:
        task = crud.create_agent_task(
            session,
            agent_profile="http-profile",
            model="model",
            approval_policy="auto",
            tools_json="[]",
            source="test",
        )
        assert task.task_id is not None
        assert task.profile_id is None
        assert not hasattr(task, "study_revision_id")
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
        assignment_id = uuid.uuid4()
        profile_id = uuid.uuid4()
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
        # The assignment is the sticky terminal subject alongside the enrollment.
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'stop-profile', 'model', '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_assignment "
                "(assignment_id, enrollment_id, study_id, agent_profile_id, strategy, "
                "randomization_epoch, profile_digest, profile_snapshot_json, status, assigned_at) "
                "VALUES (:assignment_id, :enrollment_id, :study_id, :profile_id, 'RANDOM_EQUAL', "
                "0, 'stop-digest', '{\"model\":\"model\"}', 'ACTIVE', now())"
            ),
            {
                "assignment_id": assignment_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
                "profile_id": profile_id,
            },
        )
        task = crud.create_agent_task(
            session,
            agent_profile="stop-profile",
            model="model",
            approval_policy="auto",
            tools_json="[]",
            source="test",
            study_id=study_id,
            enrollment_id=enrollment_id,
            research_session_id=session_id,
            owner_user_id=owner_id,
        )
        audit_record_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.research_record "
                "(record_id, kind, scope_type, scope_id, study_id, actor, occurred_at, payload_json) "
                "VALUES (:record_id, 'RELEASE_EVIDENCE', 'study', :study_id, :study_id, "
                "'researcher', now(), '{}')"
            ),
            {"record_id": audit_record_id, "study_id": study_id},
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
        assert summary.assignment_count == 1
        assert summary.session_count == 1
        repeated = stop_research_study(session, study_id, actor="researcher")
        assert repeated.enrollment_count == 0
        assert repeated.assignment_count == 0
        assert repeated.session_count == 0

        assert session.execute(
            text("SELECT research_status FROM public.study WHERE study_id = :study_id"),
            {"study_id": study_id},
        ).scalar_one() == "STUDY_STOPPED"
        assert session.execute(
            text(
                "SELECT kind, payload_json->>'event' FROM public.research_record "
                "WHERE study_id = :study_id AND kind = 'STUDY_LIFECYCLE'"
            ),
            {"study_id": study_id},
        ).one() == ("STUDY_LIFECYCLE", "STUDY_STOPPED")
        assert session.execute(
            text("SELECT status, revocation_epoch FROM public.research_enrollment WHERE enrollment_id = :id"),
            {"id": enrollment_id},
        ).one() == ("STUDY_STOPPED", 1)
        # The sticky assignment is terminal alongside its enrollment.
        assert session.execute(
            text("SELECT status FROM public.study_assignment WHERE assignment_id = :id"),
            {"id": assignment_id},
        ).scalar_one() == "STUDY_STOPPED"
        assert session.execute(
            text("SELECT state, close_reason FROM public.research_session WHERE session_id = :id"),
            {"id": session_id},
        ).one() == ("revoked", "STUDY_STOPPED")
        for table, key, value in (
            ("research_agent_run", "agent_run_id", run_id),
            ("research_event", "event_id", event_id),
            ("telemetry_batch_receipt", "receipt_id", receipt_id),
            ("agent_task", "task_id", task.task_id),
            ("research_record", "record_id", audit_record_id),
        ):
            assert session.execute(
                text(f"SELECT count(*) FROM public.{table} WHERE {key} = :value"),
                {"value": value},
            ).scalar_one() == 1
        # The stop audit is appended, never replacing the retained audit row.
        assert session.execute(
            text(
                "SELECT count(*) FROM public.research_record WHERE study_id = :study_id"
            ),
            {"study_id": study_id},
        ).scalar_one() == 2
        # An ordinary stop is not a deletion or a retention trigger: the
        # admin/compliance retention ledger stays untouched.
        assert session.execute(
            text(
                "SELECT count(*) FROM public.research_retention_job "
                "WHERE enrollment_id = :enrollment_id"
            ),
            {"enrollment_id": enrollment_id},
        ).scalar_one() == 0
    finally:
        session.close()
        engine.dispose()


def test_metadata_locks_after_consent_and_clone_requires_stop():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        updated = update_research_metadata(
            session,
            study_id,
            name="Updated name",
            description="Updated description",
        )
        assert updated.name == "Updated name"

        session.execute(
            text(
                "UPDATE public.study SET consent_locked_at = now() "
                "WHERE study_id = :study_id"
            ),
            {"study_id": study_id},
        )
        session.commit()
        with pytest.raises(PermissionError, match="metadata is locked"):
            update_research_metadata(session, study_id, name="Rejected")
        session.rollback()

        with pytest.raises(PermissionError, match="only stopped"):
            clone_stopped_research_study(session, study_id, actor="owner")

        stop_research_study(session, study_id, actor="owner")
        clone = clone_stopped_research_study(session, study_id, actor="owner")
        assert clone.study_id != study_id
        assert clone.research_status == "DRAFT"
        assert clone.join_code
        assert clone.join_code != session.get(type(clone), study_id).join_code

        # The no-selection clone is explicitly profile-less (ISSUE-12): no
        # selection rows and no profile_ids in the copied configuration.
        assert session.execute(
            text(
                "SELECT count(*) FROM public.study_agent_profile WHERE study_id = :study_id"
            ),
            {"study_id": clone.study_id},
        ).scalar_one() == 0
        assert "profile_ids" not in (
            session.execute(
                text(
                    "SELECT research_config_json FROM public.study WHERE study_id = :study_id"
                ),
                {"study_id": clone.study_id},
            ).scalar_one()
            or {}
        )

        # A clone completed with profiles freezes them with the same create-time
        # validation and records them in the copied configuration.
        profile_id = uuid.uuid4()
        release_id = f"clone-release-{uuid.uuid4()}"
        session.execute(
            text(
                "INSERT INTO public.agent_release "
                "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
                "VALUES (:release_id, 'test-agent', 'manifest-digest', 'QUALIFIED', "
                "CAST(:release_json AS jsonb), now())"
            ),
            {
                "release_id": release_id,
                "release_json": _qualified_release_json(
                    release_id, agent_id="test-agent"
                ),
            },
        )
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'clone-profile', 'model', :release_id, '[]', 'auto', 1)"
            ),
            {
                "profile_id": profile_id,
                "owner_id": owner_id,
                "release_id": release_id,
            },
        )
        session.commit()

        completed = clone_stopped_research_study(
            session, study_id, actor="owner", profile_ids=[profile_id]
        )
        assert completed.study_id != clone.study_id
        selections = session.execute(
            text(
                "SELECT profile_id, selection_order FROM public.study_agent_profile "
                "WHERE study_id = :study_id"
            ),
            {"study_id": completed.study_id},
        ).all()
        assert [str(row.profile_id) for row in selections] == [str(profile_id)]
        assert selections[0].selection_order == 0
        completed_config = session.execute(
            text(
                "SELECT research_config_json FROM public.study WHERE study_id = :study_id"
            ),
            {"study_id": completed.study_id},
        ).scalar_one()
        assert completed_config["profile_ids"] == [str(profile_id)]
    finally:
        session.close()
        engine.dispose()


def test_metadata_update_cannot_race_past_first_consent_lock():
    engine, session = _fresh_session()
    second_session = sessionmaker(bind=engine)()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        session.execute(
            text(
                "SELECT study_id FROM public.study "
                "WHERE study_id = :study_id FOR UPDATE"
            ),
            {"study_id": study_id},
        )
        session.execute(
            text(
                "UPDATE public.study SET consent_locked_at = now(), "
                "research_status = 'ACTIVE' WHERE study_id = :study_id"
            ),
            {"study_id": study_id},
        )
        second_session.execute(text("SET lock_timeout = '200ms'"))
        with pytest.raises(OperationalError):
            update_research_metadata(second_session, study_id, name="Blocked by consent lock")
        session.commit()
        second_session.rollback()
        with pytest.raises(PermissionError, match="metadata is locked"):
            update_research_metadata(second_session, study_id, name="Still blocked")
    finally:
        second_session.close()
        session.close()
        engine.dispose()


def test_revoke_enrollment_is_terminal_but_retains_identity():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        participant_id = uuid.uuid4()
        enrollment_id = uuid.uuid4()
        session_id = uuid.uuid4()
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
                "consent_accepted_at, retention_action) VALUES "
                "(:enrollment_id, :participant_id, :study_id, 'p_revoke', 'ACTIVE', 0, '{}', now(), now(), now(), 'RETAIN_ANONYMIZED')"
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
                "(session_id, enrollment_id, study_id, context_id, state, manifest_digest, environment_json, transitions_json, created_at) "
                "VALUES (:session_id, :enrollment_id, :study_id, 'ctx-revoke', 'running', 'manifest', '{}', '[]', now())"
            ),
            {
                "session_id": session_id,
                "enrollment_id": enrollment_id,
                "study_id": study_id,
            },
        )
        session.commit()

        summary = revoke_research_enrollment(session, study_id, enrollment_id)
        assert summary.session_count == 1
        assert session.execute(
            text(
                "SELECT status, revocation_epoch FROM public.research_enrollment "
                "WHERE enrollment_id = :enrollment_id"
            ),
            {"enrollment_id": enrollment_id},
        ).one() == ("REVOKED", 1)
        assert session.execute(
            text(
                "SELECT state, close_reason FROM public.research_session "
                "WHERE session_id = :session_id"
            ),
            {"session_id": session_id},
        ).one() == ("revoked", "REVOKED")
    finally:
        session.close()
        engine.dispose()


def test_revoked_enrollment_cannot_rejoin_same_study():
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        study_id = _create_study(session, owner_id)
        profile_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.agent_profile "
                "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                "VALUES (:profile_id, :owner_id, 'rejoin profile', 'model', '[]', 'auto', 1)"
            ),
            {"profile_id": profile_id, "owner_id": owner_id},
        )
        session.execute(
            text(
                "INSERT INTO public.study_agent_profile "
                "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                "VALUES (:study_id, :profile_id, 'digest', '{}', 0, now())"
            ),
            {"study_id": study_id, "profile_id": profile_id},
        )
        session.commit()
        first = open_study_enrollment(session, owner_id, f"SCHEMA-{study_id}".upper())
        revoke_research_enrollment(session, study_id, first.enrollment_id)
        with pytest.raises(PermissionError, match="cannot rejoin"):
            open_study_enrollment(session, owner_id, f"SCHEMA-{study_id}".upper())
    finally:
        session.close()
        engine.dispose()


def test_second_study_first_consent_keeps_both_studies_active():
    """Regression: no owner-level live-study slot blocks a second consent.

    Under the retired ``uq_study_owner_live_research`` index, activating the
    second study raised ``psycopg2.errors.UniqueViolation`` and surfaced as an
    unhandled HTTP 500 on the first web consent.
    """
    engine, session = _fresh_session()
    try:
        owner_id = _create_user(session)
        first_study_id = _create_study(session, owner_id)
        second_study_id = _create_study(session, owner_id)
        for index, study_id in enumerate((first_study_id, second_study_id)):
            profile_id = uuid.uuid4()
            session.execute(
                text(
                    "INSERT INTO public.agent_profile "
                    "(profile_id, owner_user_id, name, model, tools_json, approval_policy, max_steps) "
                    "VALUES (:profile_id, :owner_id, :name, 'model', '[]', 'auto', 1)"
                ),
                {
                    "profile_id": profile_id,
                    "owner_id": owner_id,
                    "name": f"consent-{index}",
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.study_agent_profile "
                    "(study_id, profile_id, profile_digest, profile_snapshot_json, selection_order, created_at) "
                    "VALUES (:study_id, :profile_id, 'digest', '{}', 0, now())"
                ),
                {"study_id": study_id, "profile_id": profile_id},
            )
        session.commit()

        # The owner already has one published, live research study.
        study_store.set_study_active(session, first_study_id, True)

        # The second study's first consent must succeed and project it ACTIVE.
        summary = open_study_enrollment(
            session, owner_id, f"SCHEMA-{second_study_id}".upper()
        )
        assert summary.created is True
        assert summary.reused is False

        statuses = {
            row[0]: row[1]
            for row in session.execute(
                text(
                    "SELECT study_id, research_status FROM public.study "
                    "WHERE study_id IN (:first, :second)"
                ),
                {"first": first_study_id, "second": second_study_id},
            ).all()
        }
        assert statuses[first_study_id] == "ACTIVE"
        assert statuses[second_study_id] == "ACTIVE"
        assert session.execute(
            text(
                "SELECT count(*) FROM public.study WHERE created_by = :owner "
                "AND is_research AND is_active"
            ),
            {"owner": owner_id},
        ).scalar_one() == 2
    finally:
        session.close()
        engine.dispose()
