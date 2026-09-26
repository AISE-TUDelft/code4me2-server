"""Isolation for the backend test run (2026-09-25 review, item 36).

Fixtures that enter ``TestClient(app)`` run the app lifespan, which builds
``App()`` from ``.env``. When ``TEST_DATABASE_URL`` names a disposable database,
the app's ``DB_*`` settings are pointed at that database as well (unless they
were set explicitly), so a test run never writes to the development database by
accident. The Redis flush on shutdown is skipped under ``TEST_MODE`` (see
``App.cleanup``); a disposable Redis is still selected with ``REDIS_HOST`` /
``REDIS_PORT`` (and the Celery broker variables) when one is available.
"""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse


def _point_app_at_test_database() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return
    parsed = urlparse(url)
    derived = {
        "DB_HOST": parsed.hostname,
        "DB_PORT": parsed.port,
        "DB_NAME": parsed.path.lstrip("/"),
        "DB_USER": unquote(parsed.username) if parsed.username else None,
        "DB_PASSWORD": unquote(parsed.password) if parsed.password else None,
    }
    for key, value in derived.items():
        if value is None or value == "" or key in os.environ:
            continue
        os.environ[key] = str(value)


_point_app_at_test_database()
