"""Phase-07 concurrency invariants on real PostgreSQL.

Two lifecycle operations that mutate the same study must serialise on the study
row lock: a participant's first consent versus a terminal study stop, and a
profile edit versus a terminal study stop. These tests assert the observable
database invariants (one enrollment/assignment, terminal statuses, retained
rows) rather than wall-clock scheduling.
"""

from __future__ import annotations

import os
import threading
import uuid

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import pytest
from database import crud
from database.crud import ProfileLockedError
from database.migration.migration_manager import MigrationManager
from research.study.lifecycle import (
    StudyStoppedError,
    open_study_enrollment,
    stop_research_study,
)
from research.study.protocol import store as study_store

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


def _database():
    """Fresh schema plus a session factory over the disposable test database."""
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _create_account(session, *, email_prefix: str = "concurrency") -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.\"user\" "
            "(user_id, joined_at, email, name, password, config_id) "
            "VALUES (:user_id, now(), :email, 'Concurrency', 'x', :config_id)"
        ),
        {
            "user_id": user_id,
            "email": f"{email_prefix}-{user_id}@example.com",
            "config_id": config_id,
        },
    )
    session.commit()
    return user_id


def _create_study_with_profile(session, owner_id: uuid.UUID):
    """A DRAFT study that selects exactly one owned, frozen profile."""
    release_id = f"phase07-release-{uuid.uuid4()}"
    profile_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, 'phase07-agent', 'manifest-digest', 'QUALIFIED', '{}', now())"
        ),
        {"release_id": release_id},
    )
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, release_id, tools_json, approval_policy, max_steps) "
            "VALUES (:profile_id, :owner_id, 'phase07', 'model', :release_id, '[]', 'auto', 1)"
        ),
        {"profile_id": profile_id, "owner_id": owner_id, "release_id": release_id},
    )
    session.commit()
    study = study_store.create_study(
        session,
        study_id=uuid.uuid4(),
        name=f"concurrency study {uuid.uuid4()}",
        created_by=owner_id,
        join_code=f"CONC-{uuid.uuid4()}".upper(),
        profile_ids=[profile_id],
    )
    return study, profile_id


def _run_threads(workers):
    threads = [threading.Thread(target=worker) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "a concurrent lifecycle worker retained a database lock"


def test_concurrent_stop_and_consent_serialise_on_the_study_lock():
    """A stop and a first consent race on the study row lock.

    Both workers start together; Postgres serialises them. Either serialization
    is legal, and both must leave the same terminal, partial-row-free state:
    the study is STUDY_STOPPED, there is at most one enrollment with at most one
    assignment, and a consent that lost the race is refused as stopped (never a
    half-written participant/enrollment pair).
    """
    engine, session_factory = _database()
    try:
        with session_factory() as setup:
            owner_id = _create_account(setup, email_prefix="conc-owner")
            participant_id = _create_account(setup, email_prefix="conc-participant")
            study, _profile_id = _create_study_with_profile(setup, owner_id)
            join_code = study.join_code
            study_id = study.study_id

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        consent_results: list[object] = []
        stop_summaries: list[object] = []

        def consent_worker():
            db = session_factory()
            try:
                barrier.wait(timeout=5)
                consent_results.append(open_study_enrollment(db, participant_id, join_code))
            except StudyStoppedError:
                db.rollback()
            except BaseException as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)
                db.rollback()
            finally:
                db.close()

        def stop_worker():
            db = session_factory()
            try:
                barrier.wait(timeout=5)
                stop_summaries.append(
                    stop_research_study(db, study_id, actor="concurrent-stop")
                )
            except BaseException as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)
                db.rollback()
            finally:
                db.close()

        _run_threads([consent_worker, stop_worker])

        assert errors == [], [repr(error) for error in errors]
        assert len(stop_summaries) == 1
        consent_won = len(consent_results) == 1
        with session_factory() as check:
            assert check.execute(
                text("SELECT research_status FROM public.study WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one() == "STUDY_STOPPED"
            enrollment_count = check.execute(
                text("SELECT count(*) FROM public.research_enrollment WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one()
            assignment_count = check.execute(
                text("SELECT count(*) FROM public.study_assignment WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one()
            participant_mappings = check.execute(
                text(
                    "SELECT count(*) FROM public.research_participant "
                    "WHERE participant_id = ("
                    "SELECT participant_id FROM public.research_participant LIMIT 1)"
                )
            ).scalar_one()
        if consent_won:
            assert enrollment_count == 1
            assert assignment_count == 1
            assert stop_summaries[0].enrollment_count == 1
        else:
            # Stop won: consent must not have created any partial row.
            assert enrollment_count == 0
            assert assignment_count == 0
            assert stop_summaries[0].enrollment_count == 0
        assert participant_mappings <= 1
    finally:
        engine.dispose()


def test_consent_after_stop_is_refused_without_creating_rows():
    """Deterministic ordering: a stopped study can never be joined."""
    engine, session_factory = _database()
    try:
        with session_factory() as setup:
            owner_id = _create_account(setup, email_prefix="stopped-owner")
            participant_id = _create_account(setup, email_prefix="stopped-participant")
            study, _profile_id = _create_study_with_profile(setup, owner_id)
            study_id = study.study_id
            join_code = study.join_code
            stop_research_study(setup, study_id, actor="stop-before-consent")

        with session_factory() as attempt:
            with pytest.raises(StudyStoppedError):
                open_study_enrollment(attempt, participant_id, join_code)
            attempt.rollback()

        with session_factory() as check:
            assert check.execute(
                text("SELECT count(*) FROM public.research_enrollment WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one() == 0
            assert check.execute(
                text("SELECT count(*) FROM public.study_assignment WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one() == 0
            assert check.execute(
                text(
                    "SELECT count(*) FROM public.research_participant "
                    "WHERE account_id = :account_id"
                ),
                {"account_id": participant_id},
            ).scalar_one() == 0
    finally:
        engine.dispose()


def test_profile_edit_is_locked_while_the_linked_study_is_active():
    """Deterministic lock: an ACTIVE study refuses the edit outright."""
    engine, session_factory = _database()
    try:
        with session_factory() as setup:
            owner_id = _create_account(setup, email_prefix="lock-owner")
            participant_id = _create_account(setup, email_prefix="lock-participant")
            study, profile_id = _create_study_with_profile(setup, owner_id)
            open_study_enrollment(setup, participant_id, study.join_code)

        with session_factory() as attempt:
            with pytest.raises(ProfileLockedError):
                crud.update_agent_profile(attempt, profile_id, model="blocked-model")
            attempt.rollback()

        with session_factory() as check:
            assert check.execute(
                text("SELECT model FROM public.agent_profile WHERE profile_id = :id"),
                {"id": profile_id},
            ).scalar_one() == "model"
    finally:
        engine.dispose()


def test_profile_edit_after_stop_succeeds_and_keeps_the_assignment_snapshot():
    """Deterministic release: stopping the last linked study unlocks the edit.

    The frozen assignment digest/snapshot must never be rewritten by the edit.
    """
    engine, session_factory = _database()
    try:
        with session_factory() as setup:
            owner_id = _create_account(setup, email_prefix="release-owner")
            participant_id = _create_account(setup, email_prefix="release-participant")
            study, profile_id = _create_study_with_profile(setup, owner_id)
            study_id = study.study_id
            enrollment = open_study_enrollment(setup, participant_id, study.join_code)
            stored = setup.execute(
                text(
                    "SELECT profile_digest, profile_snapshot_json FROM public.study_assignment "
                    "WHERE enrollment_id = :id"
                ),
                {"id": enrollment.enrollment_id},
            ).one()
            stored_digest, stored_snapshot = stored[0], stored[1]
            stop_research_study(setup, study_id, actor="stop-before-edit")

        with session_factory() as edit:
            profile = crud.update_agent_profile(edit, profile_id, model="post-stop-model")
            assert profile.model == "post-stop-model"
            edited_digest = profile.configuration_digest

        with session_factory() as check:
            assert check.execute(
                text("SELECT model FROM public.agent_profile WHERE profile_id = :id"),
                {"id": profile_id},
            ).scalar_one() == "post-stop-model"
            assignment = check.execute(
                text(
                    "SELECT profile_digest, profile_snapshot_json FROM public.study_assignment "
                    "WHERE enrollment_id = :id"
                ),
                {"id": enrollment.enrollment_id},
            ).one()
            assert assignment[0] == stored_digest
            assert assignment[1] == stored_snapshot
            assert assignment[0] != edited_digest
    finally:
        engine.dispose()


def test_concurrent_profile_edit_and_stop_never_rewrite_the_assignment_snapshot():
    """Genuine race: whatever the interleaving, the frozen snapshot is stable."""
    engine, session_factory = _database()
    try:
        with session_factory() as setup:
            owner_id = _create_account(setup, email_prefix="race-owner")
            participant_id = _create_account(setup, email_prefix="race-participant")
            study, profile_id = _create_study_with_profile(setup, owner_id)
            study_id = study.study_id
            enrollment = open_study_enrollment(setup, participant_id, study.join_code)
            stored = setup.execute(
                text(
                    "SELECT profile_digest, profile_snapshot_json FROM public.study_assignment "
                    "WHERE enrollment_id = :id"
                ),
                {"id": enrollment.enrollment_id},
            ).one()
            stored_digest, stored_snapshot = stored[0], stored[1]

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        outcomes: list[str] = []

        def edit_worker():
            db = session_factory()
            try:
                barrier.wait(timeout=5)
                try:
                    crud.update_agent_profile(db, profile_id, model="racing-model")
                    outcomes.append("updated")
                except ProfileLockedError:
                    db.rollback()
                    outcomes.append("locked")
            except BaseException as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)
                db.rollback()
            finally:
                db.close()

        def stop_worker():
            db = session_factory()
            try:
                barrier.wait(timeout=5)
                stop_research_study(db, study_id, actor="concurrent-stop")
            except BaseException as error:  # noqa: BLE001 - collected and asserted below
                errors.append(error)
                db.rollback()
            finally:
                db.close()

        _run_threads([edit_worker, stop_worker])

        assert errors == [], [repr(error) for error in errors]
        assert len(outcomes) == 1 and outcomes[0] in {"updated", "locked"}
        with session_factory() as check:
            assert check.execute(
                text("SELECT research_status FROM public.study WHERE study_id = :id"),
                {"id": study_id},
            ).scalar_one() == "STUDY_STOPPED"
            assignment = check.execute(
                text(
                    "SELECT profile_digest, profile_snapshot_json FROM public.study_assignment "
                    "WHERE enrollment_id = :id"
                ),
                {"id": enrollment.enrollment_id},
            ).one()
            assert assignment[0] == stored_digest
            assert assignment[1] == stored_snapshot
    finally:
        engine.dispose()
