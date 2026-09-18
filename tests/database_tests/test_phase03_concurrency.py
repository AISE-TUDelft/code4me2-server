"""Phase-03 real-PostgreSQL concurrency and terminal-state tests.

These use a disposable database (``TEST_DATABASE_URL``) and real transactions to
prove the account-wide active-enrollment guard, the first-use participant insert,
per-context session uniqueness and the withdrawal/terminal transitions.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.participants import identity as identity_store
from research.participants.enums import (
    EnrollmentStatus,
    IdentityReasonCode,
    WithdrawalReason,
)
from research.participants.models import Participant, RevisionRef
from research.study.protocol.enums import RetentionAction
from research.runtime.sessions import store as session_store
from research.runtime.sessions.models import ResearchSessionV1
from research.runtime.sessions.enums import SessionState

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime.now(timezone.utc)


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


def _revision_ref(study_id: uuid.UUID) -> RevisionRef:
    return RevisionRef(
        revision_id=uuid.uuid4(),
        study_id=study_id,
        consent_document_id="consent-v1",
        consent_document_version="1",
        consent_document_digest="sha256:" + "c" * 64,
        policy_digest="sha256:" + "p" * 64,
        retention_action=RetentionAction.RETAIN_ANONYMIZED,
    )


def _make_participant(SessionFactory, account_id: uuid.UUID) -> uuid.UUID:
    session = SessionFactory()
    try:
        participant = Participant(
            participant_id=uuid.uuid4(), account_id=account_id, created_at=NOW
        )
        identity_store.create_participant(session, participant)
        return participant.participant_id
    finally:
        session.close()


def _make_enrollment(
    SessionFactory,
    participant_id: uuid.UUID,
    *,
    study_id: uuid.UUID,
    revision_id: uuid.UUID,
    status: EnrollmentStatus,
) -> uuid.UUID:
    enrollment_id = uuid.uuid4()
    session = SessionFactory()
    try:
        session.execute(
            text(
                "INSERT INTO public.research_enrollment "
                "(enrollment_id, participant_id, study_id, study_revision_id, "
                " participant_code, status, revocation_epoch, eligibility_json, "
                " enrolled_at, updated_at, expected_consent_document_id, "
                " expected_consent_version, expected_consent_digest, "
                " policy_digest, retention_action) "
                "VALUES (:eid, :pid, :sid, :rid, :code, :status, 0, "
                " '{\"eligible\": true, \"reasons\": [\"ELIGIBLE\"], "
                " \"evaluated_at\": \"2026-09-16T12:00:00+00:00\"}', "
                " :now, :now, 'd', '1', 'sha256:' || repeat('a', 64), "
                " 'sha256:' || repeat('b', 64), 'RETAIN_ANONYMIZED')"
            ),
            {
                "eid": enrollment_id,
                "pid": participant_id,
                "sid": study_id,
                "rid": revision_id,
                "code": "p_test",
                "status": status.value,
                "now": NOW,
            },
        )
        session.commit()
    finally:
        session.close()
    return enrollment_id


def _active_count(engine, participant_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM public.research_enrollment "
                "WHERE participant_id = :pid "
                "AND status IN ('ACTIVE','PENDING_CONSENT','PAUSED_RECONSENT')"
            ),
            {"pid": participant_id},
        ).scalar()


# ---------------------------------------------------------------------------
# E05 — concurrent cross-study joins produce exactly one active enrollment
# ---------------------------------------------------------------------------


def test_concurrent_cross_study_joins_yield_one_active_enrollment(engine, SessionFactory):
    account_id = uuid.uuid4()
    study_a, study_b = uuid.uuid4(), uuid.uuid4()

    def _join(study_id):
        session = SessionFactory()
        try:
            return identity_store.open_enrollment(
                session, account_id, _revision_ref(study_id), now=NOW
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_join, [study_a, study_b]))

    created = [r for r in results if r.created and r.enrollment is not None]
    refused = [r for r in results if r.issue is not None]
    assert len(created) == 1, [r.issue for r in results]
    assert len(refused) == 1
    assert refused[0].issue.code in (
        IdentityReasonCode.ALREADY_ENROLLED,
        IdentityReasonCode.DUPLICATE_ENROLLMENT,
    )

    participant_id = created[0].enrollment.participant_id
    assert _active_count(engine, participant_id) == 1


def test_concurrent_first_use_participant_insert_creates_one_mapping(engine, SessionFactory):
    account_id = uuid.uuid4()

    def _get_or_create():
        session = SessionFactory()
        try:
            row = identity_store.get_or_create_participant_row(
                session, account_id, now=NOW
            )
            return row.participant_id
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: _get_or_create(), range(4)))

    assert len(set(ids)) == 1
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM public.research_participant "
                "WHERE account_id = :aid"
            ),
            {"aid": account_id},
        ).scalar()
    assert count == 1


# ---------------------------------------------------------------------------
# G01/G02 — per-context sessions: distinct across contexts, idempotent per context
# ---------------------------------------------------------------------------


def test_two_contexts_get_two_sessions_and_one_assignment(engine, SessionFactory):
    account_id = uuid.uuid4()
    participant_id = _make_participant(SessionFactory, account_id)
    study_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    _make_enrollment(
        SessionFactory,
        participant_id,
        study_id=study_id,
        revision_id=revision_id,
        status=EnrollmentStatus.ACTIVE,
    )

    def _open(context_id):
        session = SessionFactory()
        try:
            row = session_store.create_session(
                session,
                ResearchSessionV1(
                    research_session_id=uuid.uuid4(),
                    enrollment_id=_enrollment_id_for(session, participant_id),
                    study_revision_id=revision_id,
                    context_id=context_id,
                    state=SessionState.NOT_STARTED,
                    opened_at=NOW,
                    manifest_digest="sha256:" + "m" * 64,
                ),
            )
            return row.session_id
        finally:
            session.close()

    # Two distinct contexts -> two sessions.
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(_open, ["ctx-a", "ctx-b"]))
    assert len(set(ids)) == 2

    # The same context twice -> idempotent (same session).
    first = _open("ctx-a")
    second = _open("ctx-a")
    assert first == second

    with engine.connect() as conn:
        sessions = conn.execute(
            text(
                "SELECT count(*) FROM public.research_session "
                "WHERE state NOT IN ('ended','revoked')"
            )
        ).scalar()
    assert sessions == 2


def _enrollment_id_for(session, participant_id: uuid.UUID) -> uuid.UUID:
    from database.research_schemas import ResearchEnrollment

    row = (
        session.query(ResearchEnrollment)
        .filter(ResearchEnrollment.participant_id == participant_id)
        .order_by(ResearchEnrollment.enrolled_at.desc())
        .first()
    )
    return row.enrollment_id


# ---------------------------------------------------------------------------
# F07/F08/F14 — withdrawal and terminal state
# ---------------------------------------------------------------------------


def test_withdrawal_revokes_sessions_and_blocks_rejoin_but_allows_other_study(
    engine, SessionFactory
):
    account_id = uuid.uuid4()
    participant_id = _make_participant(SessionFactory, account_id)
    study_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    enrollment_id = _make_enrollment(
        SessionFactory,
        participant_id,
        study_id=study_id,
        revision_id=revision_id,
        status=EnrollmentStatus.ACTIVE,
    )

    # A live session is revoked by the withdrawal transition.
    session = SessionFactory()
    try:
        session_store.create_session(
            session,
            ResearchSessionV1(
                research_session_id=uuid.uuid4(),
                enrollment_id=enrollment_id,
                study_revision_id=revision_id,
                context_id="ctx-1",
                state=SessionState.RUNNING,
                opened_at=NOW,
                last_activity_at=NOW,
                manifest_digest="sha256:" + "m" * 64,
            ),
        )
        row = identity_store.get_enrollment(session, enrollment_id)
        enrollment = identity_store.row_to_enrollment(row)
        result = identity_store.withdraw(
            enrollment,
            WithdrawalReason.PARTICIPANT_REQUEST,
            enrollment.retention_action,
            now=NOW,
        )
        identity_store.apply_withdrawal_transition(
            session, enrollment=result.enrollment, withdrawal=result.withdrawal, now=NOW
        )
    finally:
        session.close()

    with engine.connect() as conn:
        state = conn.execute(
            text(
                "SELECT state FROM public.research_session WHERE enrollment_id = :eid"
            ),
            {"eid": enrollment_id},
        ).scalar()
        active = conn.execute(
            text(
                "SELECT count(*) FROM public.research_enrollment "
                "WHERE participant_id = :pid AND status = 'ACTIVE'"
            ),
            {"pid": participant_id},
        ).scalar()
    assert state == SessionState.REVOKED.value
    assert active == 0

    # No self-service rejoin to the same study.
    session = SessionFactory()
    try:
        rejoin = identity_store.open_enrollment(
            session, account_id, _revision_ref(study_id), now=NOW
        )
    finally:
        session.close()
    assert rejoin.issue is not None
    assert rejoin.issue.code == IdentityReasonCode.REJOIN_NOT_ALLOWED

    # Another study remains joinable.
    session = SessionFactory()
    try:
        other = identity_store.open_enrollment(
            session, account_id, _revision_ref(uuid.uuid4()), now=NOW
        )
    finally:
        session.close()
    assert other.issue is None
    assert other.created is True


def test_terminal_study_completion_marks_enrollments_and_revokes_sessions(
    engine, SessionFactory
):
    account_id = uuid.uuid4()
    participant_id = _make_participant(SessionFactory, account_id)
    study_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    enrollment_id = _make_enrollment(
        SessionFactory,
        participant_id,
        study_id=study_id,
        revision_id=revision_id,
        status=EnrollmentStatus.ACTIVE,
    )
    session = SessionFactory()
    try:
        session_store.create_session(
            session,
            ResearchSessionV1(
                research_session_id=uuid.uuid4(),
                enrollment_id=enrollment_id,
                study_revision_id=revision_id,
                context_id="ctx-1",
                state=SessionState.RUNNING,
                opened_at=NOW,
                last_activity_at=NOW,
                manifest_digest="sha256:" + "m" * 64,
            ),
        )
        completed = identity_store.complete_enrollments_for_study(
            session, study_id, now=NOW
        )
        assert completed == 1
        # Idempotent: nothing active remains.
        assert identity_store.complete_enrollments_for_study(session, study_id, now=NOW) == 0
    finally:
        session.close()

    with engine.connect() as conn:
        status = conn.execute(
            text(
                "SELECT status, revocation_epoch FROM public.research_enrollment "
                "WHERE enrollment_id = :eid"
            ),
            {"eid": enrollment_id},
        ).one()
        state = conn.execute(
            text("SELECT state FROM public.research_session WHERE enrollment_id = :eid"),
            {"eid": enrollment_id},
        ).scalar()
    assert status.status == "COMPLETED"
    assert status.revocation_epoch == 1
    assert state == SessionState.REVOKED.value
