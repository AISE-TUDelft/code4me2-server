"""Real HTTP contracts for profile CRUD locks and stopped-study clone isolation."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from .test_research_api_contract import http_runtime
from .test_research_api_contract import _owner, _participant, _profile, _seed_user
from backend.routers.analytics.auth_utils import AuthenticatedUser


def _profile_payload(
    name: str, connection_id: uuid.UUID, *, release_id: str, framework_version: str = "codex"
) -> dict:
    """A payload pinning the seeded release with a matching framework.

    A profile must pin a registered release and the framework has to match the
    release's distribution mode (ISSUE-03 executable contract).
    """
    return {
        "name": name,
        "model": "model",
        "framework_version": framework_version,
        "release_id": release_id,
        "connection_id": str(connection_id),
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 2,
        "is_active": True,
    }


def _seeded_release_id(session_factory, profile_id: uuid.UUID) -> str:
    """The release pinned by a seeded profile row (fixture sweep helper)."""
    session = session_factory()
    try:
        return session.execute(
            text("SELECT release_id FROM public.agent_profile WHERE profile_id = :id"),
            {"id": profile_id},
        ).scalar_one()
    finally:
        session.close()


def _make_study(client, current_user, profile_id: uuid.UUID, name: str) -> str:
    response = client.post(
        "/api/research/studies",
        json={
            "name": name,
            "session_policy": {
                "idle_timeout_seconds": 600,
                "resume_grace_seconds": 120,
                "heartbeat_seconds": 30,
            },
            "profile_ids": [str(profile_id)],
        },
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
        seeded_profile_id = _profile(session, owner_id)
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
    release_id = _seeded_release_id(session_factory, seeded_profile_id)

    current_user["value"] = _owner(owner_id)
    created = client.post(
        "/api/agent/profiles",
        json=_profile_payload("created", connection_id, release_id=release_id),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]
    updated = client.put(
        f"/api/agent/profiles/{profile_id}",
        json=_profile_payload("updated", connection_id, release_id=release_id),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["profile"]["name"] == "updated"

    current_user["value"] = _owner(other_id)
    foreign = client.put(
        f"/api/agent/profiles/{profile_id}",
        json=_profile_payload("intruder", connection_id, release_id=release_id),
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
    release_id = _seeded_release_id(session_factory, profile_id)
    payload = _profile_payload("locked", connection_id, release_id=release_id)
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


def test_stopped_clone_without_profiles_stays_isolated_and_profile_less(http_runtime):
    """The no-body clone form is the explicit profile-less contract (ISSUE-12).

    It copies no study-scoped rows and records no ``profile_ids``: the clone is
    deliberately unusable until a clone request carries selections.
    """
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
    assert clone["profile_selections"] == []

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
        clone_config = session.execute(
            text("SELECT research_config_json FROM public.study WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one()
        assert "profile_ids" not in clone_config
        assert "agent_profile_ids" not in clone_config
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


def test_stopped_clone_with_selected_profiles_is_joinable(http_runtime):
    """A clone carrying validated profiles is complete and runnable (ISSUE-12)."""
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "clone-complete-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "clone-complete-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    source_id = _make_study(client, current_user, profile_id, "clone complete source")
    _set_study_status(session_factory, source_id, "STUDY_STOPPED")
    cloned = client.post(
        f"/api/research/studies/{source_id}/clone",
        json={"profile_ids": [str(profile_id)]},
    )
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()["study"]
    assert clone["study_id"] != source_id
    assert clone["research_status"] == "DRAFT"
    assert [item["profile_id"] for item in clone["profile_selections"]] == [str(profile_id)]

    session = session_factory()
    try:
        selections = session.execute(
            text(
                "SELECT profile_id, profile_digest, selection_order "
                "FROM public.study_agent_profile WHERE study_id = :id"
            ),
            {"id": clone["study_id"]},
        ).all()
        clone_config = session.execute(
            text("SELECT research_config_json FROM public.study WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one()
    finally:
        session.close()
    assert [str(row.profile_id) for row in selections] == [str(profile_id)]
    assert selections[0].profile_digest
    assert selections[0].selection_order == 0
    assert clone_config["profile_ids"] == [str(profile_id)]

    current_user["value"] = _participant(participant_id)
    joined = client.post(
        "/api/research/join",
        json={"join_code": clone["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["agent_profile_id"] == str(profile_id)


def test_profile_less_clone_cannot_join_or_activate(http_runtime):
    """A no-body clone is refused at join with the typed existing error (ISSUE-12)."""
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "clone-empty-owner@example.com", can_research=True)
        participant_id = _seed_user(session, "clone-empty-participant@example.com")
        profile_id = _profile(session, owner_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    source_id = _make_study(client, current_user, profile_id, "clone empty source")
    _set_study_status(session_factory, source_id, "STUDY_STOPPED")
    cloned = client.post(f"/api/research/studies/{source_id}/clone")
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()["study"]
    assert clone["profile_selections"] == []

    current_user["value"] = _participant(participant_id)
    refused = client.post(
        "/api/research/join",
        json={"join_code": clone["join_code"], "accept_consent": True},
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "study has no selected agent profiles"

    session = session_factory()
    try:
        assert session.execute(
            text("SELECT research_status FROM public.study WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one() == "DRAFT"
        assert session.execute(
            text("SELECT count(*) FROM public.research_enrollment WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one() == 0
        assert session.execute(
            text("SELECT count(*) FROM public.study_assignment WHERE study_id = :id"),
            {"id": clone["study_id"]},
        ).scalar_one() == 0
    finally:
        session.close()


def test_clone_with_invalid_profile_selection_is_typed_and_atomic(http_runtime):
    """Create-time profile validation applies to the clone body (ISSUE-12)."""
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "clone-invalid-owner@example.com", can_research=True)
        other_id = _seed_user(session, "clone-invalid-other@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
        foreign_profile_id = _profile(session, other_id)
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    source_id = _make_study(client, current_user, profile_id, "clone invalid source")
    _set_study_status(session_factory, source_id, "STUDY_STOPPED")

    foreign = client.post(
        f"/api/research/studies/{source_id}/clone",
        json={"profile_ids": [str(foreign_profile_id)]},
    )
    assert foreign.status_code == 403, foreign.text
    assert foreign.json()["detail"]["code"] == "PROFILE_NOT_ALLOWED"

    missing = client.post(
        f"/api/research/studies/{source_id}/clone",
        json={"profile_ids": [str(uuid.uuid4())]},
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["code"] == "PROFILE_NOT_ALLOWED"

    # Neither failed clone left a study row: the insert is atomic with the
    # frozen selection, so no profile-less orphan can be joined.
    session = session_factory()
    try:
        assert session.execute(
            text(
                "SELECT count(*) FROM public.study WHERE name = :name"
            ),
            {"name": "clone invalid source (copy)"},
        ).scalar_one() == 0
    finally:
        session.close()


def test_draft_only_linked_study_does_not_lock_profile_edits(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        owner_id = _seed_user(session, "draft-owner@example.com", can_research=True)
        profile_id = _profile(session, owner_id)
        connection_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO public.provider_connection "
                "(connection_id, label, base_url, secret_ref, models_json, is_active, created_at) "
                "VALUES (:id, :label, 'https://provider.test', 'TEST_KEY', '[\"model\"]', true, now())"
            ),
            {"id": connection_id, "label": f"draft-{connection_id}"},
        )
        session.execute(
            text(
                "UPDATE public.agent_profile SET connection_id = :connection_id "
                "WHERE profile_id = :profile_id"
            ),
            {"connection_id": connection_id, "profile_id": profile_id},
        )
        session.commit()
    finally:
        session.close()

    current_user["value"] = _owner(owner_id)
    study_id = _make_study(client, current_user, profile_id, "draft only")

    updated = client.put(
        f"/api/agent/profiles/{profile_id}",
        json=_profile_payload(
            "draft-edited",
            connection_id,
            release_id=_seeded_release_id(session_factory, profile_id),
        ),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["profile"]["name"] == "draft-edited"

    # The edit never rewrites the DRAFT study's frozen selection.
    session = session_factory()
    try:
        assert session.execute(
            text("SELECT research_status FROM public.study WHERE study_id = :id"),
            {"id": study_id},
        ).scalar_one() == "DRAFT"
        assert session.execute(
            text(
                "SELECT count(*) FROM public.study_agent_profile "
                "WHERE study_id = :id AND profile_id = :profile_id"
            ),
            {"id": study_id, "profile_id": profile_id},
        ).scalar_one() == 1
    finally:
        session.close()