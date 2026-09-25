"""Regression tests for two admin analytics routes the website depends on.

* ``/api/config/languages`` and ``/api/config/models`` were declared after
  ``/api/config/{config_id}``, so the words were parsed as a config id and the
  Config Management view always received 422. Deleting a missing config also
  surfaced as a 500 instead of 404.
* ``/api/analytics/calibration/brier-score`` nested a window function inside an
  aggregate, which PostgreSQL rejects at planning time, so the Calibration view
  always received 500 — even with no data.

The TestClient is never entered as a context manager, so the application
lifespan (which builds a real ``App()`` from ``.env``) does not run.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from database.migration.migration_manager import MigrationManager
from main import app

load_dotenv()
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/test_db"
)

ADMIN = AuthenticatedUser(
    user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
)


@pytest.fixture()
def config_client():
    mock_app = MagicMock()
    app.dependency_overrides[App.get_instance] = lambda: mock_app
    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_config_languages_route_is_not_shadowed_by_config_id(config_client):
    languages = [SimpleNamespace(language_id=1, language_name="python")]
    with patch("database.crud.get_all_programming_languages", return_value=languages):
        response = config_client.get("/api/config/languages")
    assert response.status_code == 200
    assert response.json()["languages"] == [{"language_id": 1, "language_name": "python"}]
    assert response.json()["mapping"] == {"python": 1}


def test_config_models_route_is_not_shadowed_by_config_id(config_client):
    models = [SimpleNamespace(model_id=3, model_name="org/model")]
    with patch("database.crud.get_all_model_names", return_value=models):
        response = config_client.get("/api/config/models")
    assert response.status_code == 200
    assert response.json() == {"models": [{"model_id": 3, "model_name": "org/model"}]}


def test_config_by_id_still_resolves_numeric_ids(config_client):
    row = SimpleNamespace(config_id=7, config_data='{"a": 1}')
    with patch("database.crud.get_config_by_id", return_value=row):
        response = config_client.get("/api/config/7")
    assert response.status_code == 200
    assert response.json() == {"config_id": 7, "config_data": {"a": 1}}


def test_deleting_a_missing_config_is_404_not_500(config_client):
    with patch("database.crud.delete_config", return_value=False):
        response = config_client.delete("/api/config/999")
    assert response.status_code == 404


@dataclass
class _RuntimeApp:
    session_factory: sessionmaker

    def get_db_session(self):
        return self.session_factory()


@pytest.fixture()
def calibration_runtime():
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
    app.dependency_overrides[App.get_instance] = lambda: _RuntimeApp(session_factory)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    try:
        yield SimpleNamespace(client=TestClient(app), session_factory=session_factory)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed_generations(session_factory, outcomes):
    """Insert completions with the given (confidence, was_accepted) pairs."""
    now = datetime.now(timezone.utc)
    session = session_factory()
    try:
        config_id = session.execute(text("SELECT min(config_id) FROM public.config")).scalar()
        model_id = session.execute(
            text(
                "INSERT INTO public.model_name (model_name, is_instruction_tuned, "
                "prompt_templates, model_parameters) VALUES ('calibration-model', false, '{}', '{}') "
                "RETURNING model_id"
            )
        ).scalar()
        user_id, project_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session.execute(
            text(
                'INSERT INTO public."user" (user_id, joined_at, email, name, password, config_id) '
                "VALUES (:u, :now, 'calibration@example.com', 'Calibration', 'x', :c)"
            ),
            {"u": user_id, "now": now, "c": config_id},
        )
        session.execute(
            text(
                "INSERT INTO public.project (project_id, project_name, created_at) "
                "VALUES (:p, 'calibration', :now)"
            ),
            {"p": project_id, "now": now},
        )
        session.execute(
            text(
                "INSERT INTO public.session (session_id, user_id, start_time) VALUES (:s, :u, :now)"
            ),
            {"s": session_id, "u": user_id, "now": now},
        )
        for index, (confidence, accepted) in enumerate(outcomes):
            query_id = uuid.uuid4()
            session.execute(
                text(
                    "INSERT INTO public.meta_query (meta_query_id, user_id, session_id, project_id, "
                    '"timestamp", query_type) VALUES (:q, :u, :s, :p, :ts, \'completion\')'
                ),
                {
                    "q": query_id,
                    "u": user_id,
                    "s": session_id,
                    "p": project_id,
                    "ts": now - timedelta(minutes=index + 1),
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.had_generation (meta_query_id, model_id, completion, "
                    "generation_time, shown_at, was_accepted, confidence, logprobs) VALUES "
                    "(:q, :m, 'x', 10, ARRAY[now()], :a, :c, ARRAY[]::double precision[])"
                ),
                {"q": query_id, "m": model_id, "a": accepted, "c": confidence},
            )
        session.commit()
    finally:
        session.close()


def test_brier_score_executes_on_an_empty_database(calibration_runtime):
    response = calibration_runtime.client.get(
        "/api/analytics/calibration/brier-score?group_by=model"
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


def test_brier_score_computes_per_model_scores(calibration_runtime):
    # Outcomes 1,1,0,0 with confidences .9,.7,.2,.4:
    # Brier = mean((.9-1)^2, (.7-1)^2, (.2-0)^2, (.4-0)^2) = (0.01+0.09+0.04+0.16)/4 = 0.075
    _seed_generations(
        calibration_runtime.session_factory,
        [(0.9, True), (0.7, True), (0.2, False), (0.4, False)],
    )
    response = calibration_runtime.client.get(
        "/api/analytics/calibration/brier-score?group_by=model"
    )
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["group_name"] == "calibration-model"
    assert row["sample_size"] == 4
    assert row["brier_score"] == pytest.approx(0.075)
    assert row["base_rate"] == pytest.approx(0.5)
    assert row["avg_confidence"] == pytest.approx(0.55)
    # Spread of confidence around the overall base rate 0.5:
    # mean(.16, .04, .09, .01) = 0.075
    assert row["reliability_component"] == pytest.approx(0.075)
