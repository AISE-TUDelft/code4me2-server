"""Scoped reads for the persisted kill switch.

The hot path (``is_kill_switch_engaged``) must read only the records whose scope
can match the requested identifiers, instead of loading every ``KILL_SWITCH``
record and filtering in Python. These tests pin both the exact scope semantics
(study/enrollment, released/expired) and the fact that unrelated records are
never rehydrated.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager
from research.analysis.operations import store as operations_store
from research.analysis.operations.enums import KillSwitchScopeKind
from research.analysis.operations.models import KillSwitchRecord, KillSwitchScope

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@pytest.fixture(scope="module")
def session():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    created = sessionmaker(bind=engine)()
    try:
        yield created
    finally:
        created.close()
        engine.dispose()


def _engage(
    session,
    *,
    kind: KillSwitchScopeKind,
    scope_id: uuid.UUID,
    effective_until: datetime | None = None,
    released_at: datetime | None = None,
) -> KillSwitchRecord:
    record = KillSwitchRecord(
        switch_id=uuid.uuid4(),
        scope=KillSwitchScope(kind=kind, scope_id=scope_id),
        reason="test",
        engaged_at=datetime.now(timezone.utc),
        effective_until=effective_until,
        released_at=released_at,
    )
    operations_store.engage_kill_switch(session, record)
    return record


def test_a_study_switch_blocks_only_its_study(session):
    study = uuid.uuid4()
    other_study = uuid.uuid4()
    enrollment = uuid.uuid4()
    _engage(session, kind=KillSwitchScopeKind.STUDY, scope_id=study)

    assert operations_store.is_kill_switch_engaged(session, study_id=study) is True
    assert operations_store.is_kill_switch_engaged(session, study_id=other_study) is False
    assert operations_store.is_kill_switch_engaged(session, enrollment_id=enrollment) is False
    # No identifiers at all can never match a scoped switch.
    assert operations_store.is_kill_switch_engaged(session) is False


def test_an_enrollment_switch_blocks_only_its_enrollment(session):
    study = uuid.uuid4()
    enrollment = uuid.uuid4()
    other_enrollment = uuid.uuid4()
    _engage(session, kind=KillSwitchScopeKind.ENROLLMENT, scope_id=enrollment)

    assert operations_store.is_kill_switch_engaged(session, enrollment_id=enrollment) is True
    assert (
        operations_store.is_kill_switch_engaged(session, enrollment_id=other_enrollment)
        is False
    )
    assert operations_store.is_kill_switch_engaged(session, study_id=study) is False


def test_a_released_or_expired_switch_does_not_block(session):
    released_study = uuid.uuid4()
    expired_study = uuid.uuid4()
    _engage(
        session,
        kind=KillSwitchScopeKind.STUDY,
        scope_id=released_study,
        released_at=datetime.now(timezone.utc),
    )
    _engage(
        session,
        kind=KillSwitchScopeKind.STUDY,
        scope_id=expired_study,
        effective_until=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    assert operations_store.is_kill_switch_engaged(session, study_id=released_study) is False
    assert operations_store.is_kill_switch_engaged(session, study_id=expired_study) is False


def test_only_matching_records_are_rehydrated(session, monkeypatch):
    """The SQL filter, not a Python filter, keeps the read O(matching rows)."""
    study = uuid.uuid4()
    _engage(session, kind=KillSwitchScopeKind.STUDY, scope_id=study)
    for _ in range(40):
        _engage(session, kind=KillSwitchScopeKind.STUDY, scope_id=uuid.uuid4())
    for _ in range(40):
        _engage(session, kind=KillSwitchScopeKind.ENROLLMENT, scope_id=uuid.uuid4())

    rehydrated: list[uuid.UUID] = []
    original = operations_store.row_to_kill_switch

    def counting(row):
        rehydrated.append(row.record_id)
        return original(row)

    monkeypatch.setattr(operations_store, "row_to_kill_switch", counting)

    assert operations_store.is_kill_switch_engaged(session, study_id=study) is True
    assert len(rehydrated) == 1, (
        "only the matching study switch may be rehydrated; "
        f"rehydrated {len(rehydrated)} of 81 records"
    )
