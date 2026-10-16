"""Legacy completion/A-B boundary: research studies must survive legacy writes.

``is_active`` is the research lifecycle projection, so the legacy admin routes
(which bulk-deactivate the previous completion study) must be scoped to
``is_research = false``. Without that scope an operator activating a completion
study silently closes every ACTIVE research study.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from database.migration.migration_manager import MigrationManager
from main import app

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def http_runtime():
    engine = create_engine(TEST_DB_URL)
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
        connection.commit()
    os.environ.setdefault("TEST_MODE", "true")
    manager = MigrationManager(use_test_db=True)
    manager.init_migrations()
    manager.migrate()
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    class RuntimeApp:
        def get_db_session(self):
            return session_factory()

    current_user = {"value": None}
    app.dependency_overrides[App.get_instance] = lambda: RuntimeApp()
    app.dependency_overrides[get_current_user] = lambda: current_user["value"]
    try:
        with TestClient(app) as client:
            yield client, session_factory, current_user
    finally:
        app.dependency_overrides.pop(App.get_instance, None)
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def _seed_user(session, email: str, *, is_admin: bool = False) -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.\"user\" "
            "(user_id, joined_at, email, name, password, config_id, verified, is_admin) "
            "VALUES (:user_id, :joined_at, :email, 'Boundary', 'x', :config_id, true, :is_admin)"
        ),
        {
            "user_id": user_id,
            "joined_at": NOW,
            "email": email,
            "config_id": config_id,
            "is_admin": is_admin,
        },
    )
    session.commit()
    return user_id


def _admin(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, is_admin=True, email="boundary-admin@example.com", name="Admin"
    )


def _seed_research_study(session, owner_id: uuid.UUID) -> uuid.UUID:
    study_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, is_active, is_research, "
            " research_status, research_config_json, research_config_digest, join_code, created_at) "
            "VALUES (:study_id, 'Research study', :owner, now(), true, true, "
            "'ACTIVE', '{}', 'digest', :join_code, now())"
        ),
        {"study_id": study_id, "owner": owner_id, "join_code": f"BOUND{uuid.uuid4().hex[:6].upper()}"},
    )
    session.commit()
    return study_id


def _seed_legacy_study(session, owner_id: uuid.UUID, config_id: int) -> uuid.UUID:
    study_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.study "
            "(study_id, name, created_by, starts_at, is_active, is_research, "
            " default_config_id, created_at) "
            "VALUES (:study_id, 'Legacy study', :owner, now(), true, false, :config_id, now())"
        ),
        {"study_id": study_id, "owner": owner_id, "config_id": config_id},
    )
    session.commit()
    return study_id


def test_legacy_activate_does_not_deactivate_an_active_research_study(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        admin_id = _seed_user(session, "boundary-admin@example.com", is_admin=True)
        research_owner = _seed_user(session, "research-owner@example.com")
        config_id = session.execute(
            text("SELECT config_id FROM public.config ORDER BY config_id LIMIT 1")
        ).scalar_one()
        research_study_id = _seed_research_study(session, research_owner)
        legacy_study_id = _seed_legacy_study(session, admin_id, config_id)
    finally:
        session.close()

    current_user["value"] = _admin(admin_id)
    # A legacy activation bulk-deactivates the previous completion study.
    activated = client.post(
        f"/api/analytics/studies/{legacy_study_id}/activate",
        json={},
    )
    assert activated.status_code in (200, 201, 409), activated.text

    session = session_factory()
    try:
        research = session.execute(
            text(
                "SELECT is_active, research_status FROM public.study WHERE study_id = :id"
            ),
            {"id": research_study_id},
        ).one()
        legacy = session.execute(
            text("SELECT is_active, is_research FROM public.study WHERE study_id = :id"),
            {"id": legacy_study_id},
        ).one()
    finally:
        session.close()

    assert research[0] is True, "the legacy route must not deactivate a research study"
    assert research[1] == "ACTIVE"
    assert legacy[1] is False
