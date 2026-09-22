"""HTTP contract for the administrator account listing (P3).

``GET /api/research/researchers`` is the admin panel's account list. It must
return *every* account (not only the already-enabled researchers), newest first,
with the fields the toggle needs, and it must stay administrator-only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.routers.analytics.auth_utils import AuthenticatedUser

from .test_research_api_contract import http_runtime

ACCOUNTS_PATH = "/api/research/researchers"


def _seed_account(
    session,
    email: str,
    *,
    can_research: bool = False,
    is_admin: bool = False,
    joined_at: datetime | None = None,
) -> uuid.UUID:
    config_id = session.execute(
        text("INSERT INTO public.config (config_data) VALUES ('{}') RETURNING config_id")
    ).scalar_one()
    user_id = uuid.uuid4()
    session.execute(
        text(
            'INSERT INTO public."user" '
            "(user_id, joined_at, email, name, password, config_id, verified, "
            "is_admin, can_research) "
            "VALUES (:user_id, :joined_at, :email, :name, 'x', :config_id, true, "
            ":is_admin, :can_research)"
        ),
        {
            "user_id": user_id,
            "joined_at": joined_at or datetime.now(timezone.utc),
            "email": email,
            "name": email.split("@", 1)[0],
            "config_id": config_id,
            "is_admin": is_admin,
            "can_research": can_research,
        },
    )
    session.commit()
    return user_id


def _admin(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=True,
        email="accounts-admin@example.com",
        name="Accounts Admin",
        can_research=True,
    )


def _participant(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        email="accounts-participant@example.com",
        name="Accounts Participant",
    )


def test_admin_lists_all_accounts_newest_first(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        now = datetime.now(timezone.utc)
        oldest = _seed_account(
            session,
            "older@example.com",
            can_research=True,
            joined_at=now - timedelta(hours=2),
        )
        middle = _seed_account(
            session, "middle@example.com", joined_at=now - timedelta(hours=1)
        )
        newest = _seed_account(
            session,
            "newest@example.com",
            can_research=True,
            is_admin=True,
            joined_at=now,
        )
    finally:
        session.close()

    current_user["value"] = _admin(oldest)
    response = client.get(ACCOUNTS_PATH)
    assert response.status_code == 200, response.text

    accounts = response.json()["researchers"]
    assert [account["user_id"] for account in accounts] == [
        str(newest),
        str(middle),
        str(oldest),
    ]
    by_id = {account["user_id"]: account for account in accounts}
    # Every account is listed, including the one that is not a researcher.
    assert set(by_id) == {str(newest), str(middle), str(oldest)}
    assert set(by_id[str(newest)]) == {
        "user_id",
        "email",
        "name",
        "is_admin",
        "can_research",
        "verified",
    }
    assert by_id[str(middle)]["can_research"] is False
    assert by_id[str(oldest)]["can_research"] is True
    assert by_id[str(newest)]["is_admin"] is True
    assert by_id[str(newest)]["verified"] is True


def test_admin_limit_is_applied(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        now = datetime.now(timezone.utc)
        admin_id = _seed_account(session, "limit-admin@example.com", is_admin=True)
        for offset in range(5):
            _seed_account(
                session,
                f"limit-{offset}@example.com",
                joined_at=now - timedelta(minutes=offset),
            )
    finally:
        session.close()

    current_user["value"] = _admin(admin_id)
    response = client.get(f"{ACCOUNTS_PATH}?limit=2")
    assert response.status_code == 200, response.text
    assert len(response.json()["researchers"]) == 2


def test_account_listing_is_admin_only(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        researcher_id = _seed_account(
            session, "not-admin@example.com", can_research=True
        )
    finally:
        session.close()

    current_user["value"] = _participant(researcher_id)
    response = client.get(ACCOUNTS_PATH)
    assert response.status_code == 403, response.text


def test_researcher_enable_still_requires_admin(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        researcher_id = _seed_account(
            session, "toggle-not-admin@example.com", can_research=True
        )
        target_id = _seed_account(session, "toggle-target@example.com")
    finally:
        session.close()

    current_user["value"] = _participant(researcher_id)
    response = client.put(
        f"{ACCOUNTS_PATH}/{target_id}", json={"can_research": True}
    )
    assert response.status_code == 403, response.text

    current_user["value"] = _admin(researcher_id)
    response = client.put(
        f"{ACCOUNTS_PATH}/{target_id}", json={"can_research": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["can_research"] is True
