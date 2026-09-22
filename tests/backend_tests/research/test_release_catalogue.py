"""HTTP contract for the researcher-readable release catalogue (ISSUE-11).

The catalogue must let a fresh installation author its first agent profile
before any ``AgentProfile`` row exists, so it is derived from registered
releases only, requires a researcher (not an administrator), and exposes no
secret or command material. Release import/qualification stays admin-only.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from backend.routers.analytics.auth_utils import AuthenticatedUser

from .test_research_api_contract import _seed_user, http_runtime

CATALOGUE_PATH = "/api/research/agents/release-catalogue"


def _qualified_release(session) -> str:
    """Insert a QUALIFIED BYOA release approved for one host platform."""
    release_id = f"catalogue-{uuid.uuid4()}"
    manifest_digest = "sha256:" + "b" * 64
    adapter_digest = "sha256:" + "d" * 64
    release_json = {
        "schema_version": "1",
        "agent_id": "catalogue-agent",
        "release_id": release_id,
        "version": "2.1.0",
        "source_manifest_digest": manifest_digest,
        "distribution_mode": "BYOA_EXTERNAL",
        "agent_command": "catalogue-agent",
        "agent_package": "catalogue-agent",
        "artifacts": [],
        "adapter": {
            "adapter_id": "catalogue-adapter",
            "version": "0.1.0",
            "digest": adapter_digest,
        },
    }
    tests = [
        {
            "os": "macos",
            "arch": "arm64",
            "self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z",
        }
    ]
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, "
            "created_at) "
            "VALUES (:release_id, :agent_id, :manifest, 'UNQUALIFIED', "
            "CAST(:release_json AS jsonb) || jsonb_build_object('tests', CAST(:tests AS jsonb)), now())"
        ),
        {
            "release_id": release_id,
            "agent_id": "catalogue-agent",
            "manifest": manifest_digest,
            "release_json": json.dumps(release_json),
            "tests": json.dumps(tests),
        },
    )
    session.commit()
    return release_id


def _researcher(user_id: uuid.UUID) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        is_admin=False,
        can_research=True,
        email="catalogue-researcher@example.com",
        name="Catalogue Researcher",
    )


def _catalogue_route_precedes_release_detail_routes():
    from backend.routers.research import agents as agents_router

    paths = [route.path for route in agents_router.router.routes]
    return paths.index("/release-catalogue") < paths.index("/releases/{release_id}")


def test_release_catalogue_returns_qualified_release_with_zero_profiles(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        researcher_id = _seed_user(
            session, "catalogue-researcher@example.com", can_research=True
        )
        release_id = _qualified_release(session)
        profiles = session.execute(
            text("SELECT count(*) FROM public.agent_profile")
        ).scalar_one()
    finally:
        session.close()
    assert profiles == 0
    assert _catalogue_route_precedes_release_detail_routes()

    current_user["value"] = _researcher(researcher_id)
    response = client.get(CATALOGUE_PATH)
    assert response.status_code == 200, response.text
    releases = response.json()["releases"]
    assert len(releases) == 1
    entry = releases[0]
    assert set(entry) == {
        "release_id",
        "version",
        "agent_id",
        "distribution_mode",
        "qualification_status",
        "supported_platforms",
        "verified_approval_options",
        "tests",
        "is_byoa",
    }
    assert entry["release_id"] == release_id
    assert entry["version"] == "2.1.0"
    assert entry["agent_id"] == "catalogue-agent"
    assert entry["distribution_mode"] == "BYOA_EXTERNAL"
    # Derived from the stored approvals, never the status column.
    assert entry["qualification_status"] == "QUALIFIED"
    assert entry["tests"][0]["os"] == "macos"
    assert entry["supported_platforms"] == []
    assert entry["verified_approval_options"] == ["auto", "per_step", "suggestion_only"]
    assert entry["is_byoa"] is True
    # No command, digest, endpoint or credential material leaks.
    assert "agent_command" not in response.text
    assert "source_manifest_digest" not in response.text


def test_release_catalogue_requires_a_researcher_and_keeps_admin_routes(http_runtime):
    client, session_factory, current_user = http_runtime
    session = session_factory()
    try:
        participant_id = _seed_user(session, "catalogue-participant@example.com")
        release_id = _qualified_release(session)
    finally:
        session.close()

    current_user["value"] = None
    anonymous = client.get(CATALOGUE_PATH)
    assert anonymous.status_code in (401, 403), anonymous.text

    current_user["value"] = AuthenticatedUser(
        user_id=participant_id,
        is_admin=False,
        can_research=False,
        email="catalogue-participant@example.com",
        name="Catalogue Participant",
    )
    refused = client.get(CATALOGUE_PATH)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"]["code"] == "RESEARCHER_REQUIRED"

    # Import/qualification and the existing release list stay administrator-only.
    assert client.get("/api/research/agents/releases").status_code == 403
    assert client.get(
        f"/api/research/agents/releases/{release_id}"
    ).status_code == 403
    # The import is multipart (manifest + archive bytes) and stays admin-only.
    assert client.post(
        "/api/research/agents/releases/import", data={"manifest": "{}"}
    ).status_code == 403


def test_http_import_is_immediately_usable_and_disable_is_terminal(http_runtime):
    from .test_manifest_import import ARCHIVE, MANIFEST, PAYLOAD

    client, session_factory, current_user = http_runtime
    current_user["value"] = AuthenticatedUser(user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin")
    payload = dict(MANIFEST, agents=[{
        "framework": "codex", "version": "1.2.3", "agent_command": "codex-acp",
        "adapter": {"adapter_id": "codex-acp", "version": "1.0.0"},
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="macos", arch="arm64")],
    }])
    endpoint = "/api/research/agents/releases/import"
    response = client.post(endpoint, data={"manifest": json.dumps(payload)}, files=[("archives", (ARCHIVE, PAYLOAD, "application/zip"))])
    assert response.status_code == 201, response.text
    releases = response.json()["releases"]
    assert len(releases) == 2
    assert all(release["status"] == "QUALIFIED" for release in releases)
    release_id = releases[0]["release_id"]
    disabled = client.post(f"/api/research/agents/releases/{release_id}/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["release"]["status"] == "DISABLED"
    retry = client.post(endpoint, data={"manifest": json.dumps(payload)}, files=[("archives", (ARCHIVE, PAYLOAD, "application/zip"))])
    assert retry.status_code == 200, retry.text
    assert retry.json()["release"]["status"] == "DISABLED"
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM agent_release")).scalar_one() == 2
    current_user["value"] = AuthenticatedUser(user_id=uuid.uuid4(), is_admin=False, email="participant@example.com", name="Participant")
    assert client.post(f"/api/research/agents/releases/{release_id}/disable").status_code == 403


def test_http_import_rolls_back_managed_row_when_external_identity_conflicts(http_runtime):
    from research.study.agents.manifest_import import manifest_digest
    from .test_manifest_import import ARCHIVE, MANIFEST, PAYLOAD

    client, session_factory, current_user = http_runtime
    current_user["value"] = AuthenticatedUser(user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin")
    payload = dict(MANIFEST, agents=[{
        "framework": "goose", "version": "1.0.0", "agent_command": "goose",
        "adapter": {"adapter_id": "goose", "version": "1"},
        "tests": [dict(MANIFEST["artifacts"][0]["tests"], os="linux", arch="x64")],
    }])
    conflict_id = "goose-1.0.0-" + manifest_digest(payload).removeprefix("sha256:")[:12]
    with session_factory() as session:
        session.execute(text("INSERT INTO agent_release (release_id, agent_id, source_manifest_digest, status, release_json, created_at) VALUES (:id, 'goose', 'other-digest', 'UNQUALIFIED', '{}', now())"), {"id": conflict_id})
        session.commit()
    response = client.post("/api/research/agents/releases/import", data={"manifest": json.dumps(payload)}, files=[("archives", (ARCHIVE, PAYLOAD, "application/zip"))])
    assert response.status_code == 409, response.text
    with session_factory() as session:
        assert session.execute(text("SELECT release_id FROM agent_release")).scalars().all() == [conflict_id]
