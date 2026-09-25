"""Profile contracts: full-replace PUT clears overrides; BYOA refuses labels.

* ``PUT /api/agent/profiles/{id}`` is a full replacement, so a null
  ``temperature`` / ``max_context_tokens`` stores NULL and the digest is the one
  a profile created with NULL would get.
* A profile pinned to a ``BYOA_EXTERNAL`` release cannot carry
  ``max_context_tokens`` (it is never forwarded to an externally installed
  agent): create, update and the study freeze all refuse it with a typed 422.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from database import crud
from research.canonical import canonical_hash
from research.study.agents.distributions import (
    ProfileConfigurationError,
    validate_profile_configuration,
)
from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.models import (
    AgentConfigBinding,
    AgentReleaseV1,
    DistributionArtifact,
    ReleaseTests,
)

from ._byoa_contract import BYOA_CONFIG_BINDINGS
from ._ui_overhaul_seed import (
    VALID_SESSION_POLICY,
    researcher_user,
    seed_account,
    seed_byoa_release,
    seed_connection,
    seed_packaged_release,
    seed_profile,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

if TYPE_CHECKING:
    import uuid

PROFILES_PATH = "/api/agent/profiles"


def _payload(
    name: str,
    *,
    connection_id: uuid.UUID,
    release_id: str,
    framework_version: str = "code4me2-agent",
    temperature=None,
    max_context_tokens=None,
) -> dict:
    return {
        "name": name,
        "model": "model",
        "framework_version": framework_version,
        "connection_id": str(connection_id),
        "release_id": release_id,
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
        "is_active": True,
        "temperature": temperature,
        "max_context_tokens": max_context_tokens,
    }


def _seed_authoring(session) -> SimpleNamespace:
    owner = seed_account(session, "profile-owner@example.com", can_research=True)
    return SimpleNamespace(
        owner=owner,
        connection=seed_connection(session, label="profiles-connection"),
        packaged=seed_packaged_release(session),
        byoa=seed_byoa_release(
            session, agent_id="codex", agent_package="codex", agent_command="codex-acp"
        ),
    )


def _stored(session_factory, profile_id) -> dict:
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT temperature, max_context_tokens, configuration_digest "
                "FROM public.agent_profile WHERE profile_id = :id"
            ),
            {"id": profile_id},
        ).one()
    return {
        "temperature": row.temperature,
        "max_context_tokens": row.max_context_tokens,
        "configuration_digest": row.configuration_digest,
    }


def test_put_with_null_overrides_clears_them_and_the_digest(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "managed-arm",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            temperature=0.7,
            max_context_tokens=4096,
        ),
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    profile_id = profile["profile_id"]
    assert profile["temperature"] == 0.7
    assert profile["max_context_tokens"] == 4096
    digest_with_overrides = profile["configuration_digest"]

    cleared = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=_payload(
            "managed-arm",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            temperature=None,
            max_context_tokens=None,
        ),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["profile"]["temperature"] is None
    assert cleared.json()["profile"]["max_context_tokens"] is None

    stored = _stored(session_factory, profile_id)
    assert stored["temperature"] is None
    assert stored["max_context_tokens"] is None
    # Exactly the digest of the same profile created with NULL overrides.
    expected_digest = canonical_hash(
        {
            "profile_id": profile_id,
            "name": "managed-arm",
            "model": "model",
            "framework_version": "code4me2-agent",
            "connection_id": str(seeded.connection),
            "release_id": seeded.packaged,
            "tools_json": "[]",
            "approval_policy": "auto",
            "max_steps": 3,
            "temperature": None,
            "max_context_tokens": None,
            "is_active": True,
        }
    )
    assert stored["configuration_digest"] == expected_digest
    assert cleared.json()["profile"]["configuration_digest"] == expected_digest
    assert expected_digest != digest_with_overrides

    # A later PUT can set them again.
    restored = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=_payload(
            "managed-arm",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            temperature=0.2,
            max_context_tokens=1024,
        ),
    )
    assert restored.status_code == 200, restored.text
    assert _stored(session_factory, profile_id)["temperature"] == 0.2
    assert _stored(session_factory, profile_id)["max_context_tokens"] == 1024


def test_partial_repository_update_still_keeps_unsupplied_overrides(http_runtime):
    """Internal callers passing only some fields keep the old semantics."""
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        profile = crud.create_agent_profile(
            session,
            owner_user_id=seeded.owner,
            name="partial",
            model="model",
            tools_json="[]",
            approval_policy="auto",
            max_steps=2,
            framework_version="code4me2-agent",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            temperature=0.4,
            max_context_tokens=2048,
        )
        updated = crud.update_agent_profile(session, profile.profile_id, model="other")
        assert updated.model == "other"
        assert updated.temperature == 0.4
        assert updated.max_context_tokens == 2048
        cleared = crud.update_agent_profile(
            session,
            profile.profile_id,
            temperature=None,
            update_temperature=True,
        )
        assert cleared.temperature is None
        assert cleared.max_context_tokens == 2048


def test_byoa_profile_refuses_max_context_tokens_on_create_and_update(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    refused = client.post(
        PROFILES_PATH,
        json=_payload(
            "byoa-arm",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
            max_context_tokens=2048,
        ),
    )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "BYOA_FIELD_UNSUPPORTED"
    assert detail["field"] == "max_context_tokens"
    assert "not forwarded to externally installed agents" in detail["message"]
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name = 'byoa-arm'")
        ).scalar_one() == 0

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "byoa-arm",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
            temperature=0.5,
        ),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]

    update_refused = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=_payload(
            "byoa-arm",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
            temperature=0.5,
            max_context_tokens=2048,
        ),
    )
    assert update_refused.status_code == 422, update_refused.text
    assert update_refused.json()["detail"]["code"] == "BYOA_FIELD_UNSUPPORTED"
    assert update_refused.json()["detail"]["field"] == "max_context_tokens"
    assert _stored(session_factory, profile_id)["max_context_tokens"] is None

    # The managed runtime still accepts the context window.
    managed = client.post(
        PROFILES_PATH,
        json=_payload(
            "managed-window",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            max_context_tokens=2048,
        ),
    )
    assert managed.status_code == 201, managed.text


def test_study_freeze_refuses_a_byoa_profile_carrying_max_context_tokens(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        # Rows written before the rule existed bypass the create-time check.
        legacy = seed_profile(
            session,
            owner_id=seeded.owner,
            framework_version="codex",
            release_id=seeded.byoa,
            connection_id=seeded.connection,
            max_context_tokens=1000,
        )
        clean = seed_profile(
            session,
            owner_id=seeded.owner,
            framework_version="codex",
            release_id=seeded.byoa,
            connection_id=seeded.connection,
        )
    current_user["value"] = researcher_user(seeded.owner)

    def create_study(profile_id):
        return client.post(
            "/api/research/studies",
            json={
                "name": f"freeze-{profile_id}",
                "session_policy": VALID_SESSION_POLICY,
                "profile_ids": [str(profile_id)],
            },
        )

    refused = create_study(legacy)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "BYOA_FIELD_UNSUPPORTED"
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.study")
        ).scalar_one() == 0
    accepted = create_study(clean)
    assert accepted.status_code == 201, accepted.text


def _release(*, byoa: bool) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="codex" if byoa else "code4me2-agent",
        release_id="unit-release",
        version="1.0.0",
        source_manifest_digest="sha256:" + "1" * 64,
        distribution_mode=(
            DistributionMode.BYOA_EXTERNAL if byoa else DistributionMode.PACKAGED
        ),
        artifacts=(
            []
            if byoa
            else [
                DistributionArtifact(
                    os="macos",
                    arch="arm64",
                    path="runtime.zip",
                    sha256="sha256:" + "a" * 64,
                    size=1,
                )
            ]
        ),
        agent_package="codex" if byoa else None,
        byoa_config=(
            [AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS] if byoa else []
        ),
        tests=[
            ReleaseTests(
                os="macos",
                arch="arm64",
                self_check="PASS",
                acp_initialize="PASS",
                ran_at="2026-09-21T00:00:00Z",
            )
        ],
        qualification_status=QualificationStatus.QUALIFIED,
    )


def _profile(**overrides) -> SimpleNamespace:
    base = dict(
        name="unit",
        release_id="unit-release",
        framework_version="codex",
        model="model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        max_context_tokens=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_validation_refuses_max_context_tokens_only_for_byoa():
    byoa = _release(byoa=True)
    validate_profile_configuration(_profile(), byoa)
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(_profile(max_context_tokens=1), byoa)
    assert error.value.code == "BYOA_FIELD_UNSUPPORTED"
    assert error.value.field == "max_context_tokens"
    assert str(error.value).startswith("BYOA_FIELD_UNSUPPORTED: ")

    packaged = _release(byoa=False)
    validate_profile_configuration(
        _profile(framework_version="code4me2-agent", max_context_tokens=8192),
        packaged,
    )
