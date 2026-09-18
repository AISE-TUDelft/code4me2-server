"""Real HTTP contracts for profile CRUD locks and stopped-study clone isolation."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from .test_research_api_contract import http_runtime
from .test_research_api_contract import _owner, _profile, _seed_user
from backend.routers.analytics.auth_utils import AuthenticatedUser


def _profile_payload(name: str, connection_id: uuid.UUID) -> dict:
    return {
        "name": name,
        "model": "model",
        "framework_version": "code4me2-agent",
        "release_id": None,
        "connection_id": str(connection_id),
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 2,
        "is_active": True,
    }


def _make_study(client, current_user, profile_id: uuid.UUID, name: str) -> str:
    response = client.post(
        "/api/research/studies",
        json={"name": name, "profile_ids": [str(profile_id)]},
    )
    assert response.status_code == 201, response.text
    return response.json()["study"]["study_id"]


def _set_study_status(session_factory, study_id: str, status: str) -> None:
    session = session_factory()
    try:
        session.execute(
            text(
                "UPDATE public.study SET is_active = :active, research_status = :status "
                "WHERE study_id = :study_id"
            ),
            {"active": status == "ACTIVE", "status": status, "study_id": study_id},
        )
        session.commit()
    finally:
        session.close()


def test_profile_crud_http_and_foreign_owner_contract(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "crud-owner@example.com", can_research=True)
        other_id = _seed_user(session, "crud-other@example.com", can_research=True)
        connection_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.provider_connection "
                "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
                "VALUES (:id, :label, 'https://provider.test', 'TEST_KEY', '[\"model\"]', true, now())"
            ),
            {"id": connection_id, "label": f"crud-{connection_id}"},
        )
        session.commit()
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    created = client.post("/api/agent/profiles", json=_profile_payload("created", connection_id))
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]
    updated = client.put(
        f"/api/agent/profiles/{profile_id}", json=_profile_payload("updated", connection_id)
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["profile"]["name"] == "updated"

    current_user["value"] = _owner(other_id)
    foreign = client.put(
        f"/api/agent/profiles/{profile_id}", json=_profile_payload("intruder", connection_id)
    )
    assert foreign.status_code == 403
    assert client.delete(f"/api/agent/profiles/{profile_id}").status_code == 403

    current_user["value"] = _owner(owner_id)
    deleted = client.delete(f"/api/agent/profiles/{profile_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["retired"] is True


def test_shared_profile_stays_locked_until_last_active_study_stops(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "shared-owner@example.com", can_research=True)
        second_owner_id = _seed_user(session, "shared-other@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
        connection_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.provider_connection "
                "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
                "VALUES (:id, :label, 'https://provider.test', 'TEST_KEY', '[\"model\"]', true, now())"
            ),
            {"id": connection_id, "label": f"shared-{connection_id}"},
        )
        session.execute(
            text("UPDATE public.agent_profile SET connection_id = :connection_id WHERE profile_id = :profile_id"),
            {"connection_id": connection_id, "profile_id": profile_id},
        )
        session.commit()
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    first = _make_study(client, current_user, profile_id, "first")
    current_user["value"] = AuthenticatedUser(
        user_id=second_owner_id,
        is_admin=True,
        can_research=True,
        email="admin-shared@example.com",
        name="Admin Shared",
    )
    second = _make_study(client, current_user, profile_id, "second")
    current_user["value"] = _owner(owner_id)
    _set_study_status(session_factory, first, "ACTIVE")
    _set_study_status(session_factory, second, "ACTIVE")

    session = session_factory()
    connection_id = session.execute(
        text("SELECT connection_id FROM public.agent_profile WHERE profile_id = :id"),
        {"id": profile_id},
    ).scalar_one_or_none()
    session.close()
    payload = _profile_payload("locked", connection_id)
    updated = client.put(f"/api/agent/profiles/{profile_id}", json=payload)
    deleted = client.delete(f"/api/agent/profiles/{profile_id}")
    for response in (updated, deleted):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "PROFILE_LOCKED"

    _set_study_status(session_factory, first, "STUDY_STOPPED")
    still_locked = client.put(f"/api/agent/profiles/{profile_id}", json=payload)
    assert still_locked.status_code == 409
    _set_study_status(session_factory, second, "STUDY_STOPPED")
    editable = client.put(f"/api/agent/profiles/{profile_id}", json=payload)
    assert editable.status_code == 200, editable.text


def test_stopped_clone_has_no_study_scoped_rows_and_preserves_source(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "clone-owner@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    source_id = _make_study(client, current_user, profile_id, "clone source")
    _set_study_status(session_factory, source_id, "STUDY_STOPPED")
    cloned = client.post(f"/api/research/studies/{source_id}/clone")
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()["study"]
    assert clone["study_id"] != source_id
    assert clone["join_code"]
    assert clone["research_status"] == "DRAFT"

    session = session_factory()
    try:
        source = session.execute(
            text("SELECT research_status, join_code FROM public.study WHERE study_id = :id"),
            {"id": source_id},
        ).one()
        assert source.research_status == "STUDY_STOPPED"
        assert session.execute(
            text("SELECT count(*) FROM public.study_agent_profile WHERE study_id = :id"),
            {"id": source_id},
        ).scalar_one() == 1
        for table in (
            "study_agent_profile",
            "research_enrollment",
            "study_assignment",
            "research_session",
            "agent_task",
            "research_event",
            "research_record",
        ):
            query = f"SELECT count(*) FROM public.{table} WHERE study_id = :id"
            assert session.execute(text(query), {"id": clone["study_id"]}).scalar_one() == 0, table
        assert session.execute(
            text(
                "SELECT count(*) FROM public.research_agent_run "
                "WHERE research_session_id IN (SELECT session_id FROM public.research_session WHERE study_id = :id)"
            ),
            {"id": clone["study_id"]},
        ).scalar_one() == 0
        assert session.execute(
            text(
                "SELECT count(*) FROM public.telemetry_batch_receipt "
                "WHERE research_session_id IN (SELECT session_id FROM public.research_session WHERE study_id = :id)"
            ),
            {"id": clone["study_id"]},
        ).scalar_one() == 0
    finally:
        session.close()