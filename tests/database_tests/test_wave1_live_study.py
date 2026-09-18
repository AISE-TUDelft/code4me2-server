"""F6 — live-study slot release on expiry/retirement (real PostgreSQL).

A partial unique index on ``study.is_active`` cannot react to time passing, so an
expired study must be actively released before a new publication reserves the
owner's slot. These tests exercise the real database constraints and the store
helpers.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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
    ends_at: datetime | None,
) -> uuid.UUID:
    study_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, ends_at, is_active, "
            " is_research, created_at) "
            "VALUES (:study_id, :name, :created_by, :starts_at, :ends_at, "
            " :is_active, true, now())"
        ),
        {
            "study_id": study_id,
            "name": name,
            "created_by": owner_user_id,
            "starts_at": NOW - timedelta(days=365),
            "ends_at": ends_at,
            "is_active": is_active,
        },
    )
    session.commit()
    return study_id


def _active_count(session, owner_user_id: uuid.UUID) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM public.study "
            "WHERE created_by = :owner AND is_research AND is_active"
        ),
        {"owner": owner_user_id},
    ).scalar()


def test_expired_study_is_released_before_a_new_slot_is_reserved(db_session):
    owner = _make_user(db_session)
    expired = _make_study(
        db_session,
        owner,
        name="expired",
        is_active=True,
        ends_at=NOW - timedelta(days=1),
    )
    fresh = _make_study(
        db_session,
        owner,
        name="fresh",
        is_active=False,
        ends_at=NOW + timedelta(days=30),
    )

    released = protocol_store.deactivate_expired_research_studies(db_session, owner)
    assert released == 1

    row = _study_row(db_session, expired)
    assert row.is_active is False

    # The owner's slot is now free, so the new study can go live.
    protocol_store.set_study_active(db_session, fresh, True)
    assert _active_count(db_session, owner) == 1


def _study_row(session, study_id):
    from database.db_schemas import Study

    return session.get(Study, study_id)


def test_partial_unique_index_enforces_one_live_study_per_owner(db_session):
    owner_one = _make_user(db_session)
    owner_two = _make_user(db_session)
    first = _make_study(
        db_session, owner_one, name="one", is_active=True, ends_at=None
    )
    second = _make_study(
        db_session, owner_one, name="two", is_active=False, ends_at=None
    )
    other_owner_study = _make_study(
        db_session, owner_two, name="other", is_active=False, ends_at=None
    )

    # A second live research study for the same owner violates the index.
    with pytest.raises(IntegrityError):
        protocol_store.set_study_active(db_session, second, True)
    db_session.rollback()
    assert _active_count(db_session, owner_one) == 1

    # A different owner may be live at the same time.
    protocol_store.set_study_active(db_session, other_owner_study, True)
    assert _active_count(db_session, owner_two) == 1
    assert _active_count(db_session, owner_one) == 1
    assert db_session.get(type(_study_row(db_session, first)), first) is not None


def test_retirement_releases_the_slot(db_session):
    owner = _make_user(db_session)
    live = _make_study(db_session, owner, name="live", is_active=True, ends_at=None)

    # Retirement of the last published revision ends the study.
    protocol_store.set_study_active(db_session, live, False)
    assert _active_count(db_session, owner) == 0

    next_study = _make_study(
        db_session, owner, name="next", is_active=False, ends_at=None
    )
    protocol_store.set_study_active(db_session, next_study, True)
    assert _active_count(db_session, owner) == 1


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
