"""A freshly migrated disposable PostgreSQL schema for the privacy suites.

Set ``TEST_DATABASE_URL`` (and the isolation variables described in
``tests/conftest.py``); every test starts from an empty schema.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.migration.migration_manager import MigrationManager

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)


@pytest.fixture()
def session_factory():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    try:
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    finally:
        engine.dispose()
