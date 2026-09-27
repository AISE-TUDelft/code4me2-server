"""HTTP contract of the self-service privacy controls, over real PostgreSQL.

Covers ``/api/user/privacy`` (status, opt-out/in, erase), account deletion at
``DELETE /api/user/delete`` and the join refusal while opted out. Redis is a
small stand-in that knows the live session and records token revocation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from App import App
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from main import app

from ..research._ui_overhaul_seed import seed_account, seed_profile, seed_study
from ._footprint import row_counts, seed_footprint


@dataclass
class StubRedisManager:
    tokens: dict = field(default_factory=dict)
    revoked: list = field(default_factory=list)
    context_erased: list = field(default_factory=list)

    def get(self, type, token, reset_exp=False):
        return self.tokens.get((type, token))

    def revoke_user_tokens(self, user_id):
        self.revoked.append(user_id)

    def mark_context_erased(self, user_id):
        self.context_erased.append(user_id)


@dataclass
class Runtime:
    session_factory: sessionmaker
    redis: StubRedisManager

    def get_db_session(self):
        return self.session_factory()

    def get_redis_manager(self):
        return self.redis


@pytest.fixture
def api(session_factory):
    runtime = Runtime(session_factory, StubRedisManager())
    signed_in = {"user": None}

    def current_user():
        if signed_in["user"] is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return signed_in["user"]

    overrides = {App.get_instance: lambda: runtime, get_current_user: current_user}
    previous = {key: app.dependency_overrides.get(key) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        with TestClient(app) as client:
            yield client, runtime, signed_in
    finally:
        for key, value in previous.items():
            if value is None:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _as(user_id, *, email="someone@example.org", can_research=False):
    return AuthenticatedUser(
        user_id=user_id, is_admin=False, email=email, name="someone", can_research=can_research
    )


def _world(runtime):
    with runtime.session_factory() as db:
        researcher_id = seed_account(db, "researcher@example.org", can_research=True)
        study_id = seed_study(db, owner_id=researcher_id, name="Agent study")
        profile_id = seed_profile(db, owner_id=researcher_id)
        alice = seed_footprint(db, email="alice@example.org", study_id=study_id, profile_id=profile_id)
    return researcher_id, study_id, alice


def test_status_reports_collection_active_study_and_stored_data(api):
    client, runtime, signed_in = api
    _, study_id, alice = _world(runtime)
    signed_in["user"] = _as(alice.user_id, email=alice.email)

    response = client.get("/api/user/privacy")

    assert response.status_code == 200
    assert response.json() == {
        "data_collection": {"enabled": True, "opted_out_at": None},
        "active_study": {"study_id": str(study_id), "name": "Agent study"},
        "stored_data": {
            "queries": 2, "chats": 1, "agent_runs": 1, "study_enrollments": 1, "study_events": 2,
        },
        "account_deletion": {"allowed": True, "blocked_reason": None},
    }


def test_status_explains_why_a_researcher_cannot_delete_the_account(api):
    client, runtime, signed_in = api
    researcher_id, _, _ = _world(runtime)
    signed_in["user"] = _as(researcher_id, can_research=True)

    deletion = client.get("/api/user/privacy").json()["account_deletion"]

    assert deletion["allowed"] is False
    assert "1 research study and 1 agent profile" in deletion["blocked_reason"]


def test_opting_out_withdraws_the_study_and_opting_in_does_not_rejoin(api):
    client, runtime, signed_in = api
    _, _, alice = _world(runtime)
    signed_in["user"] = _as(alice.user_id)

    off = client.put("/api/user/privacy/collection", json={"enabled": False})

    assert off.status_code == 200
    assert off.json()["data_collection"]["enabled"] is False
    assert off.json()["data_collection"]["opted_out_at"]
    assert off.json()["active_study"] is None
    with runtime.session_factory() as db:
        assert db.execute(
            text("SELECT status FROM public.research_enrollment WHERE enrollment_id = :e"),
            {"e": alice.enrollment_id},
        ).scalar_one() == "WITHDRAWN"

    on = client.put("/api/user/privacy/collection", json={"enabled": True})

    assert on.status_code == 200
    assert on.json()["data_collection"] == {"enabled": True, "opted_out_at": None}
    assert on.json()["active_study"] is None
    # Withdrawal alone does not erase anything.
    assert on.json()["stored_data"]["study_events"] == 2


def test_collection_update_rejects_unknown_fields(api):
    client, runtime, signed_in = api
    _, _, alice = _world(runtime)
    signed_in["user"] = _as(alice.user_id)

    response = client.put("/api/user/privacy/collection", json={"enabled": False, "scope": "all"})

    assert response.status_code == 422


def test_erase_reports_what_went_and_keeps_the_live_session(api):
    client, runtime, signed_in = api
    _, _, alice = _world(runtime)
    signed_in["user"] = _as(alice.user_id)
    runtime.redis.tokens[("user_token", str(alice.user_id))] = {
        "session_token": str(alice.live_session_id)
    }

    response = client.post("/api/user/privacy/erase")

    assert response.status_code == 200
    body = response.json()
    assert body["erased"] == {
        "queries": 2, "chats": 1, "agent_runs": 1, "study_enrollments": 1, "study_events": 2,
    }
    assert body["status"]["data_collection"]["enabled"] is False
    assert body["status"]["active_study"] is None
    assert set(body["status"]["stored_data"].values()) == {0}
    with runtime.session_factory() as db:
        remaining = {table for table, count in row_counts(db, alice).items() if count}
    assert remaining == {"user", "live_session", "project", "project_users"}
    # The live session's cached project context is kept from being written back.
    assert runtime.redis.context_erased == [str(alice.user_id)]


def test_privacy_endpoints_require_a_signed_in_account(api):
    client, _, _ = api

    assert client.get("/api/user/privacy").status_code == 401
    assert client.put("/api/user/privacy/collection", json={"enabled": False}).status_code == 401
    assert client.post("/api/user/privacy/erase").status_code == 401


def test_joining_a_study_is_refused_while_opted_out(api):
    client, runtime, signed_in = api
    _, _, alice = _world(runtime)
    with runtime.session_factory() as db:
        bob_id = seed_account(db, "bob@example.org")
    signed_in["user"] = _as(alice.user_id)
    client.put("/api/user/privacy/collection", json={"enabled": False})

    refused = client.post("/api/research/join", json={"join_code": "NOPE", "accept_consent": True})

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "DATA_COLLECTION_OPTED_OUT"

    # A collecting account goes on to the normal join checks.
    signed_in["user"] = _as(bob_id)
    unknown = client.post("/api/research/join", json={"join_code": "NOPE", "accept_consent": True})
    assert unknown.status_code == 404


def test_account_deletion_erases_everything_and_revokes_tokens(api):
    client, runtime, _ = api
    _, study_id, alice = _world(runtime)
    runtime.redis.tokens[("auth_token", "alice-token")] = {"user_id": str(alice.user_id)}
    client.cookies.set("auth_token", "alice-token")

    response = client.delete("/api/user/delete", params={"delete_data": "false"})

    assert response.status_code == 200
    assert runtime.redis.revoked == [str(alice.user_id)]
    with runtime.session_factory() as db:
        assert {table for table, count in row_counts(db, alice).items() if count} == set()
        assert db.execute(
            text("SELECT count(*) FROM public.study WHERE study_id = :s"), {"s": study_id}
        ).scalar_one() == 1


def test_account_deletion_is_refused_for_a_researcher(api):
    client, runtime, _ = api
    researcher_id, study_id, _ = _world(runtime)
    runtime.redis.tokens[("auth_token", "researcher-token")] = {"user_id": str(researcher_id)}
    client.cookies.set("auth_token", "researcher-token")

    response = client.delete("/api/user/delete")

    assert response.status_code == 409
    assert "cannot delete itself" in response.json()["message"]
    assert runtime.redis.revoked == []
    with runtime.session_factory() as db:
        assert db.execute(
            text('SELECT count(*) FROM public."user" WHERE user_id = :u'), {"u": researcher_id}
        ).scalar_one() == 1
        assert db.execute(
            text("SELECT count(*) FROM public.study WHERE study_id = :s"), {"s": study_id}
        ).scalar_one() == 1


def test_unknown_accounts_get_a_404_from_account_deletion(api):
    client, runtime, _ = api
    runtime.redis.tokens[("auth_token", "ghost")] = {"user_id": str(uuid.uuid4())}
    client.cookies.set("auth_token", "ghost")

    assert client.delete("/api/user/delete").status_code == 404
