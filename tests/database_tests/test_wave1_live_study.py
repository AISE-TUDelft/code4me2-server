"""Owner-level research-study concurrency and terminal-stop contract.

The legacy F6 one-live-study-per-owner slot (partial unique index
``uq_study_owner_live_research``) is retired: ``study.is_active`` is a
compatibility projection of ``research_status``, not an owner-level publication
slot. These tests pin the replacement contract against real PostgreSQL.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.study.protocol import store as protocol_store

load_dotenv()

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="function")
def db_session():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()

    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(session) -> uuid.UUID:
    config_id = session.execute(
        text(
            "INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id"
        )
    ).scalar()
    user_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.user "
            "(user_id, joined_at, email, name, password, config_id) "
            "VALUES (:user_id, now(), :email, 'Owner', 'x', :config_id)"
        ),
        {
            "user_id": user_id,
            "email": f"owner-{user_id}@example.com",
            "config_id": config_id,
        },
    )
    session.commit()
    return user_id


def _make_study(
    session,
    owner_user_id: uuid.UUID,
    *,
    name: str,
    is_active: bool,
    research_status: str | None = None,
    ends_at: datetime | None = None,
) -> uuid.UUID:
    study_id = uuid.uuid4()
    status = (
        research_status
        if research_status is not None
        else ("ACTIVE" if is_active else "DRAFT")
    )
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, ends_at, is_active, "
            " is_research, research_status, created_at) "
            "VALUES (:study_id, :name, :created_by, :starts_at, :ends_at, "
            " :is_active, true, :research_status, now())"
        ),
        {
            "study_id": study_id,
            "name": name,
            "created_by": owner_user_id,
            "starts_at": NOW - timedelta(days=365),
            "ends_at": ends_at,
            "is_active": is_active,
            "research_status": status,
        },
    )
    session.commit()
    return study_id


def _study_row(session, study_id):
    from database.db_schemas import Study

    return session.get(Study, study_id)


def _active_count(session, owner_user_id: uuid.UUID) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM public.study "
            "WHERE created_by = :owner AND is_research AND is_active"
        ),
        {"owner": owner_user_id},
    ).scalar()


def test_owner_can_hold_multiple_active_research_studies(db_session):
    owner = _make_user(db_session)
    first = _make_study(db_session, owner, name="first", is_active=False)
    second = _make_study(db_session, owner, name="second", is_active=False)

    protocol_store.set_study_active(db_session, first, True)
    protocol_store.set_study_active(db_session, second, True)

    assert _active_count(db_session, owner) == 2
    first_row = _study_row(db_session, first)
    second_row = _study_row(db_session, second)
    assert (first_row.is_active, first_row.research_status) == (True, "ACTIVE")
    assert (second_row.is_active, second_row.research_status) == (True, "ACTIVE")

    # Deactivation is an ordinary projection transition and does not disturb
    # the owner's other active study.
    protocol_store.set_study_active(db_session, second, False)
    assert _active_count(db_session, owner) == 1
    assert _study_row(db_session, second).research_status == "DRAFT"
    assert _study_row(db_session, first).research_status == "ACTIVE"


def test_two_active_research_studies_for_one_owner_insert_cleanly(db_session):
    owner = _make_user(db_session)
    _make_study(db_session, owner, name="one", is_active=True)
    _make_study(db_session, owner, name="two", is_active=True)

    assert _active_count(db_session, owner) == 2


def test_stopped_study_cannot_be_reactivated(db_session):
    owner = _make_user(db_session)
    stopped = _make_study(
        db_session,
        owner,
        name="stopped",
        is_active=False,
        research_status="STUDY_STOPPED",
    )

    with pytest.raises(ValueError, match="cannot be reactivated"):
        protocol_store.set_study_active(db_session, stopped, True)

    db_session.rollback()
    row = _study_row(db_session, stopped)
    assert row.is_active is False
    assert row.research_status == "STUDY_STOPPED"
    assert _active_count(db_session, owner) == 0


def test_research_study_is_open_reflects_the_window():
    from types import SimpleNamespace

    open_study = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=1),
    )
    ended_study = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW - timedelta(days=2),
        ends_at=NOW - timedelta(days=1),
    )
    future_study = SimpleNamespace(
        is_research=True,
        is_active=True,
        starts_at=NOW + timedelta(days=1),
        ends_at=NOW + timedelta(days=2),
    )
    completion_study = SimpleNamespace(
        is_research=False,
        is_active=True,
        starts_at=None,
        ends_at=None,
    )

    assert protocol_store.research_study_is_open(open_study) is True
    assert protocol_store.research_study_is_open(ended_study) is False
    assert protocol_store.research_study_is_open(future_study) is False
    assert protocol_store.research_study_is_open(completion_study) is False
