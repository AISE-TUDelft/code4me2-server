"""HTTP contract for provider-connection conflicts and profile usage counts.

A duplicate label (create or update) is one typed 409
``CONNECTION_LABEL_EXISTS``; deleting a connection that agent profiles still
reference is a typed 409 ``CONNECTION_IN_USE`` instead of a database 500, and
the request session is rolled back rather than left failed. Administrators see
how many profiles reference each connection.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from sqlalchemy import text

from database import crud

from ._ui_overhaul_seed import (
    admin_user,
    researcher_user,
    seed_account,
    seed_connection,
    seed_profile,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

CONNECTIONS_PATH = "/api/research/provider-connections"
LABEL_EXISTS = {
    "code": "CONNECTION_LABEL_EXISTS",
    "field": "label",
    "message": "A connection with that label already exists",
}


def _payload(label: str, *, models=("model",), is_active: bool = True) -> dict:
    return {
        "label": label,
        "base_url": "https://provider.test/v1",
        "secret_ref": "UI_OVERHAUL_KEY",
        "models": list(models),
        "is_active": is_active,
    }


def _labels(session_factory) -> dict[str, str]:
    with session_factory() as session:
        rows = session.execute(
            text("SELECT connection_id, label FROM public.provider_connection")
        ).all()
    return {str(connection_id): label for connection_id, label in rows}


def test_duplicate_label_is_a_typed_409_on_create_and_update(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "conn-admin@example.com", is_admin=True)
    current_user["value"] = admin_user(admin)

    first = client.post(CONNECTIONS_PATH, json=_payload("alpha"))
    assert first.status_code == 201, first.text
    assert first.json()["connection"]["profile_count"] == 0
    second = client.post(CONNECTIONS_PATH, json=_payload("beta"))
    assert second.status_code == 201, second.text
    beta_id = second.json()["connection"]["connection_id"]

    renamed = client.put(f"{CONNECTIONS_PATH}/{beta_id}", json=_payload("alpha"))
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["detail"] == LABEL_EXISTS
    assert _labels(session_factory)[beta_id] == "beta"

    duplicate = client.post(CONNECTIONS_PATH, json=_payload("alpha"))
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["detail"] == LABEL_EXISTS

    # Keeping its own label is not a conflict.
    kept = client.put(
        f"{CONNECTIONS_PATH}/{beta_id}", json=_payload("beta", models=("m1", "m2"))
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["connection"]["models"] == ["m1", "m2"]
    assert kept.json()["connection"]["profile_count"] == 0

    missing = client.put(f"{CONNECTIONS_PATH}/{uuid.uuid4()}", json=_payload("alpha"))
    assert missing.status_code == 404, missing.text


def test_label_race_hits_the_unique_constraint_and_stays_a_typed_409(http_runtime):
    """Past the pre-check, the unique index still yields the typed 409."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "race-admin@example.com", is_admin=True)
        seed_connection(session, label="alpha")
        beta_id = seed_connection(session, label="beta")
    current_user["value"] = admin_user(admin)

    with patch(
        "backend.routers.research.providers.crud.get_provider_connection_by_label",
        return_value=None,
    ):
        renamed = client.put(f"{CONNECTIONS_PATH}/{beta_id}", json=_payload("alpha"))
        created = client.post(CONNECTIONS_PATH, json=_payload("alpha"))
    assert renamed.status_code == 409, renamed.text
    assert renamed.json()["detail"] == LABEL_EXISTS
    assert created.status_code == 409, created.text
    assert created.json()["detail"] == LABEL_EXISTS
    assert _labels(session_factory)[str(beta_id)] == "beta"
    assert sorted(_labels(session_factory).values()) == ["alpha", "beta"]


def test_deleting_a_referenced_connection_is_a_typed_409(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "delete-admin@example.com", is_admin=True)
        owner = seed_account(session, "delete-owner@example.com", can_research=True)
        used = seed_connection(session, label="used")
        archived_only = seed_connection(session, label="archived-only")
        unused = seed_connection(session, label="unused")
        seed_profile(session, owner_id=owner, connection_id=used)
        seed_profile(session, owner_id=owner, connection_id=used, is_active=False)
        seed_profile(session, owner_id=owner, connection_id=archived_only, is_active=False)
    current_user["value"] = admin_user(admin)

    listed = client.get(CONNECTIONS_PATH)
    assert listed.status_code == 200, listed.text
    counts = {
        item["label"]: item["profile_count"] for item in listed.json()["connections"]
    }
    assert counts == {"used": 2, "archived-only": 1, "unused": 0}

    refused = client.delete(f"{CONNECTIONS_PATH}/{used}")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "CONNECTION_IN_USE"
    assert "2 agent profile(s)" in detail["message"]
    assert detail["profile_count"] == 2

    # Archived profiles keep their reference and block deletion too.
    archived = client.delete(f"{CONNECTIONS_PATH}/{archived_only}")
    assert archived.status_code == 409, archived.text
    assert "1 agent profile(s)" in archived.json()["detail"]["message"]

    assert str(used) in _labels(session_factory)
    deleted = client.delete(f"{CONNECTIONS_PATH}/{unused}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "connection_id": str(unused)}
    assert str(unused) not in _labels(session_factory)
    assert client.delete(f"{CONNECTIONS_PATH}/{unused}").status_code == 404


def test_delete_race_rolls_back_before_reporting_in_use(http_runtime):
    """A reference that appears after the pre-check trips the RESTRICT key.

    The handler must roll the failed flush back: the follow-up recount runs on
    the same session and would raise if the session were left failed.
    """
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "delete-race-admin@example.com", is_admin=True)
        owner = seed_account(session, "delete-race-owner@example.com", can_research=True)
        connection_id = seed_connection(session, label="raced")
        seed_profile(session, owner_id=owner, connection_id=connection_id)
    current_user["value"] = admin_user(admin)

    real_count = crud.count_profiles_for_connection
    calls: list[uuid.UUID] = []

    def stale_then_real(db, target):
        calls.append(target)
        return 0 if len(calls) == 1 else real_count(db, target)

    with patch(
        "backend.routers.research.providers.crud.count_profiles_for_connection",
        side_effect=stale_then_real,
    ):
        response = client.delete(f"{CONNECTIONS_PATH}/{connection_id}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CONNECTION_IN_USE"
    assert response.json()["detail"]["profile_count"] == 1
    assert len(calls) == 2
    assert str(connection_id) in _labels(session_factory)


def test_profile_count_is_administrator_only(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        admin = seed_account(session, "count-admin@example.com", is_admin=True)
        researcher = seed_account(session, "count-researcher@example.com", can_research=True)
        connection_id = seed_connection(session, label="shared")
        profile_id = seed_profile(session, owner_id=researcher, connection_id=connection_id)

    current_user["value"] = admin_user(admin)
    (admin_entry,) = client.get(CONNECTIONS_PATH).json()["connections"]
    assert admin_entry["profile_count"] == 1
    updated = client.put(f"{CONNECTIONS_PATH}/{connection_id}", json=_payload("shared"))
    assert updated.status_code == 200, updated.text
    assert updated.json()["connection"]["profile_count"] == 1

    current_user["value"] = researcher_user(researcher)
    listed = client.get(CONNECTIONS_PATH)
    assert listed.status_code == 200, listed.text
    (entry,) = listed.json()["connections"]
    assert entry["connection_id"] == str(connection_id)
    assert "profile_count" not in entry
    assert "base_url" not in entry
    assert "secret_ref" not in entry
    assert str(profile_id) not in listed.text
