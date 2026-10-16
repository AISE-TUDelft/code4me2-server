"""`/api/health` reports the database and Redis (production-readiness B-11).

`/api/ping` proves only that the process answers; the production compose polls
this route so a server whose database or Redis is down is marked unhealthy.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from App import App
from main import app


class _Session:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    def execute(self, statement):  # noqa: ANN001
        if self.fail:
            raise OperationalError("SELECT 1", {}, Exception("database is down"))
        return 1

    def close(self) -> None:
        self.closed = True


def _runtime(*, db_ok: bool, redis_ok: bool):
    session = _Session(fail=not db_ok)
    redis = SimpleNamespace(ping=lambda: redis_ok)
    return SimpleNamespace(
        get_db_session=lambda: session,
        get_redis_manager=lambda: redis,
        _session=session,
    )


def _health(runtime):
    app.dependency_overrides[App.get_instance] = lambda: runtime
    try:
        # No lifespan: the real App must not be constructed for this route test.
        return TestClient(app).get("/api/health")
    finally:
        app.dependency_overrides.pop(App.get_instance, None)


def test_health_is_ok_when_database_and_redis_answer():
    runtime = _runtime(db_ok=True, redis_ok=True)
    response = _health(runtime)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
    assert runtime._session.closed


def test_health_is_503_when_redis_is_down():
    response = _health(_runtime(db_ok=True, redis_ok=False))
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "ok", "redis": "error"}


def test_health_is_503_when_the_database_is_down():
    runtime = _runtime(db_ok=False, redis_ok=True)
    response = _health(runtime)
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"
    assert runtime._session.closed
