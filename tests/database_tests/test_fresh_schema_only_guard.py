"""Fresh-schema-only guard for the consolidated research migration.

This release has no data-preserving upgrade path from the previous
revision/condition research schema. Applying the consolidated revision to a
database that already holds the previous research tables must refuse loudly with
an actionable message rather than failing halfway through the DDL.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from database.migration.migration_manager import MigrationManager

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


def _empty_database():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    return engine


def _migrate() -> bool:
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    return manager.migrate()


def test_a_fresh_database_migrates_to_head():
    """Control: the supported fresh path still works (init.sql -> migration)."""
    engine = _empty_database()
    try:
        assert _migrate() is True
    finally:
        engine.dispose()


def test_a_previous_research_schema_is_refused_with_an_actionable_message(capsys):
    engine = _empty_database()
    try:
        # Simulate the previous research deployment: the legacy schema (applied
        # by init_migrations) plus a research table the consolidated revision
        # would create.
        manager = MigrationManager(use_test_db=True)
        assert manager.init_migrations() is True
        with engine.connect() as connection:
            connection.execute(
                text(
                    "CREATE TABLE public.research_enrollment ("
                    "enrollment_id uuid PRIMARY KEY)"
                )
            )
            connection.commit()

        migrated = _migrate()

        output = capsys.readouterr().out
        assert migrated is False, "an existing research schema must not migrate in place"
        assert "Fresh-schema release" in output
        assert "research_enrollment" in output
        assert "separate, explicitly approved" in output
    finally:
        engine.dispose()
