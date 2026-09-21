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
    """Insert a QUALIFIED BYOA release whose evidence verifies every option."""
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
        "tests": {
            "status": "PASS",
            "approval_options": ["auto", "per_step", "suggestion_only"],
            "cases": [
                {"case_id": "acp.initialize", "status": "PASS"},
                {"case_id": "approval.permission", "status": "PASS"},
            ],
        },
    }
    session.execute(
        text(
            "INSERT INTO public.agent_release "
            "(release_id, agent_id, source_manifest_digest, status, release_json, created_at) "
            "VALUES (:release_id, :agent_id, :manifest, 'UNQUALIFIED', "
            "CAST(:release_json AS jsonb), now())"
        ),
        {
            "release_id": release_id,
            "agent_id": "catalogue-agent",
            "manifest": manifest_digest,
            "release_json": json.dumps(release_json),
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
        "is_byoa",
    }
    assert entry["release_id"] == release_id
    assert entry["version"] == "2.1.0"
    assert entry["agent_id"] == "catalogue-agent"
    assert entry["distribution_mode"] == "BYOA_EXTERNAL"
    # Derived from the stored conformance evidence, never the status column.
    assert entry["qualification_status"] == "QUALIFIED"
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
    # The import endpoint is multipart (recipe + archive bytes), so the authz
    # refusal is asserted with a well-formed request rather than a JSON body.
    assert client.get("/api/research/agents/releases").status_code == 403
    assert client.get(
        f"/api/research/agents/releases/{release_id}"
    ).status_code == 403
    assert client.post(
        "/api/research/agents/releases/import", data={"recipe": "{}"}
    ).status_code == 403
