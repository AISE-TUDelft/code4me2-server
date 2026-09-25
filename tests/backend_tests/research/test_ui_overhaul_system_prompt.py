"""Agent profile system prompt: stored, frozen and served (issue 04).

* ``agent_profile.system_prompt`` is a nullable ``TEXT`` column created by the
  consolidated migration (no new revision).
* The profile API accepts it (at most 4000 characters, blank -> null, stored
  trimmed). A ``PUT`` that sends the field replaces it (null or blank clears
  it). A ``PUT`` that omits it keeps the stored prompt for the managed runtime
  (a client that does not show the field must not wipe it) and clears it for a
  BYOA runtime, which can never hold one.
* The configuration digest, the study selection/assignment snapshot and the
  managed run policy carry it only when it is set, so every prompt-less profile
  keeps a byte-identical digest, snapshot and policy.
* A BYOA (goose/codex) profile refuses it with the typed 422
  ``BYOA_FIELD_UNSUPPORTED`` (field ``system_prompt``), like
  ``max_context_tokens``, at create, update and study freeze.
* The release catalogue advertises it for PACKAGED releases only.
* ``GET /api/acp/agent-config`` serves it from the frozen assignment snapshot.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from agents.registry import (
    FrozenAgentConfig,
    _frozen_config_from_snapshot,
    resolve_assignment_context,
)
from backend.routers.acp import (
    _managed_policy,
    _valid_managed_policy_snapshot,
    get_acp_agent_config,
)
from backend.routers.agent import profiles as profiles_router
from backend.routers.agent.profiles import AgentProfilePayload
from database import crud
from database.db_schemas import AgentProfile
from research.canonical import canonical_hash
from research.study.agents.distributions import (
    ProfileConfigurationError,
    release_profile_configurability,
    validate_profile_configuration,
)
from research.study.lifecycle import profile_snapshot

from ._ui_overhaul_seed import (
    VALID_SESSION_POLICY,
    participant_user,
    researcher_user,
    seed_account,
    seed_byoa_release,
    seed_connection,
    seed_packaged_release,
    seed_profile,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture
from .test_ui_overhaul_profiles import _profile as _stand_in_profile
from .test_ui_overhaul_profiles import _release as _stand_in_release

PROFILES_PATH = "/api/agent/profiles"
PROMPT = "You are assisting a study participant. Explain every change you make."


def _payload(
    name: str,
    *,
    connection_id: uuid.UUID,
    release_id: str,
    framework_version: str = "code4me2-agent",
    **overrides,
) -> dict:
    payload = {
        "name": name,
        "model": "model",
        "framework_version": framework_version,
        "connection_id": str(connection_id),
        "release_id": release_id,
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
        "is_active": True,
        "temperature": None,
        "max_context_tokens": None,
    }
    payload.update(overrides)
    return payload


def _seed_authoring(session, email: str = "prompt-owner@example.com") -> SimpleNamespace:
    owner = seed_account(session, email, can_research=True)
    return SimpleNamespace(
        owner=owner,
        connection=seed_connection(session, label=f"prompt-connection-{owner.hex[:8]}"),
        packaged=seed_packaged_release(session),
        byoa=seed_byoa_release(
            session, agent_id="codex", agent_package="codex", agent_command="codex-acp"
        ),
    )


def _stored_prompt(session_factory, profile_id) -> tuple:
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT system_prompt, configuration_digest FROM public.agent_profile "
                "WHERE profile_id = :id"
            ),
            {"id": profile_id},
        ).one()
    return row.system_prompt, row.configuration_digest


def _legacy_configuration(profile: dict) -> dict:
    """The digest input exactly as it was before the ``system_prompt`` field."""
    return {
        "profile_id": profile["profile_id"],
        "name": profile["name"],
        "model": profile["model"],
        "framework_version": profile["framework_version"],
        "connection_id": profile["connection"]["connection_id"],
        "release_id": profile["release_id"],
        "tools_json": profile["tools_json"],
        "approval_policy": profile["approval_policy"],
        "max_steps": profile["max_steps"],
        "temperature": profile["temperature"],
        "max_context_tokens": profile["max_context_tokens"],
        "is_active": profile["is_active"],
    }


# ---------------------------------------------------------------------------
# R1 schema
# ---------------------------------------------------------------------------


def test_consolidated_migration_creates_a_nullable_text_column(http_runtime):
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        column = session.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_profile' "
                "AND column_name = 'system_prompt'"
            )
        ).one_or_none()
    assert column is not None, "the migration must create agent_profile.system_prompt"
    assert column.data_type == "text"
    assert column.is_nullable == "YES"
    assert AgentProfile.__table__.c.system_prompt.nullable is True


# ---------------------------------------------------------------------------
# R2 API: create / read / update / clear, cap and normalization
# ---------------------------------------------------------------------------


def test_payload_trims_blanks_caps_and_refuses_nul():
    common = {
        "name": "arm",
        "model": "model",
        "framework_version": "code4me2-agent",
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
        "connection_id": uuid.uuid4(),
    }
    assert AgentProfilePayload(**common).system_prompt is None
    assert AgentProfilePayload(**common, system_prompt=None).system_prompt is None
    assert AgentProfilePayload(**common, system_prompt=" \n\t ").system_prompt is None
    assert AgentProfilePayload(**common, system_prompt="  keep  ").system_prompt == "keep"
    cap = profiles_router.SYSTEM_PROMPT_MAX_LENGTH
    assert cap == 4000
    exact = "p" * cap
    assert AgentProfilePayload(**common, system_prompt=exact).system_prompt == exact
    with pytest.raises(ValueError):
        AgentProfilePayload(**common, system_prompt="p" * (cap + 1))
    with pytest.raises(ValueError):
        AgentProfilePayload(**common, system_prompt="before\x00after")


def test_create_stores_the_trimmed_prompt_and_responses_include_it(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "prompted",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt=f"  {PROMPT}\n",
        ),
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    assert profile["system_prompt"] == PROMPT
    assert _stored_prompt(session_factory, profile["profile_id"])[0] == PROMPT

    fetched = client.get(f"{PROFILES_PATH}/{profile['profile_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["profile"]["system_prompt"] == PROMPT
    listed = client.get(PROFILES_PATH)
    assert listed.status_code == 200, listed.text
    by_name = {item["name"]: item for item in listed.json()["profiles"]}
    assert by_name["prompted"]["system_prompt"] == PROMPT

    blank = client.post(
        PROFILES_PATH,
        json=_payload(
            "blank",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt=" \n\t ",
        ),
    )
    assert blank.status_code == 201, blank.text
    assert blank.json()["profile"]["system_prompt"] is None
    assert _stored_prompt(session_factory, blank.json()["profile"]["profile_id"])[0] is None

    omitted = client.post(
        PROFILES_PATH,
        json=_payload("omitted", connection_id=seeded.connection, release_id=seeded.packaged),
    )
    assert omitted.status_code == 201, omitted.text
    assert "system_prompt" in omitted.json()["profile"]
    assert omitted.json()["profile"]["system_prompt"] is None


def test_prompt_longer_than_the_cap_is_a_422(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    too_long = client.post(
        PROFILES_PATH,
        json=_payload(
            "too-long",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt="p" * 4001,
        ),
    )
    assert too_long.status_code == 422, too_long.text
    assert "system_prompt" in too_long.text
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name = 'too-long'")
        ).scalar_one() == 0

    at_cap = client.post(
        PROFILES_PATH,
        json=_payload(
            "at-cap",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt="p" * 4000,
        ),
    )
    assert at_cap.status_code == 201, at_cap.text
    assert len(at_cap.json()["profile"]["system_prompt"]) == 4000


def test_put_is_a_full_replacement_that_sets_and_clears_the_prompt(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    def payload(**overrides):
        return _payload(
            "replaced",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            **overrides,
        )

    created = client.post(PROFILES_PATH, json=payload(system_prompt="first"))
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]

    replaced = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload(system_prompt="second"))
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["profile"]["system_prompt"] == "second"
    assert _stored_prompt(session_factory, profile_id)[0] == "second"

    cleared = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload(system_prompt=None))
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["profile"]["system_prompt"] is None
    stored, digest = _stored_prompt(session_factory, profile_id)
    assert stored is None
    # Exactly the digest of the same profile created without a prompt.
    assert digest == canonical_hash(_legacy_configuration(cleared.json()["profile"]))

    restored = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload(system_prompt="third"))
    assert restored.status_code == 200, restored.text
    assert _stored_prompt(session_factory, profile_id)[0] == "third"
    # Omitted keeps the stored prompt: a client that does not show the field
    # (an older website, or a release the catalogue does not list) must not
    # wipe the arm's prompt. Only an explicit null clears it (above).
    omitted = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload())
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["profile"]["system_prompt"] == "third"
    assert _stored_prompt(session_factory, profile_id)[0] == "third"


def test_partial_repository_update_keeps_the_prompt_unless_flagged(http_runtime):
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        profile = crud.create_agent_profile(
            session,
            owner_user_id=seeded.owner,
            name="partial-prompt",
            model="model",
            tools_json="[]",
            approval_policy="auto",
            max_steps=2,
            framework_version="code4me2-agent",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt="kept",
        )
        updated = crud.update_agent_profile(session, profile.profile_id, model="other")
        assert updated.model == "other"
        assert updated.system_prompt == "kept"
        cleared = crud.update_agent_profile(
            session, profile.profile_id, system_prompt=None, update_system_prompt=True
        )
        assert cleared.system_prompt is None


# ---------------------------------------------------------------------------
# R3 configuration digest stability
# ---------------------------------------------------------------------------


def test_configuration_digest_is_unchanged_for_profiles_without_a_prompt(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    plain = client.post(
        PROFILES_PATH,
        json=_payload("plain", connection_id=seeded.connection, release_id=seeded.packaged),
    )
    assert plain.status_code == 201, plain.text
    plain_profile = plain.json()["profile"]
    legacy_digest = canonical_hash(_legacy_configuration(plain_profile))
    assert plain_profile["configuration_digest"] == legacy_digest
    assert _stored_prompt(session_factory, plain_profile["profile_id"])[1] == legacy_digest

    prompted = client.post(
        PROFILES_PATH,
        json=_payload(
            "prompted-digest",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt=PROMPT,
        ),
    )
    assert prompted.status_code == 201, prompted.text
    prompted_profile = prompted.json()["profile"]
    without_prompt = _legacy_configuration(prompted_profile)
    assert prompted_profile["configuration_digest"] == canonical_hash(
        {**without_prompt, "system_prompt": PROMPT}
    )
    assert prompted_profile["configuration_digest"] != canonical_hash(without_prompt)


def test_digest_input_omits_an_unset_prompt():
    base = dict(
        profile_id=uuid.uuid4(),
        name="unit",
        model="model",
        framework_version="code4me2-agent",
        connection_id=None,
        release_id="release",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        max_context_tokens=None,
        is_active=True,
    )
    unset = crud._agent_profile_configuration(SimpleNamespace(**base, system_prompt=None))
    assert "system_prompt" not in unset
    assert list(unset) == [
        "profile_id",
        "name",
        "model",
        "framework_version",
        "connection_id",
        "release_id",
        "tools_json",
        "approval_policy",
        "max_steps",
        "temperature",
        "max_context_tokens",
        "is_active",
    ]
    assert crud._agent_profile_configuration(SimpleNamespace(**base)) == unset
    prompted = crud._agent_profile_configuration(SimpleNamespace(**base, system_prompt="p"))
    assert prompted == {**unset, "system_prompt": "p"}


# ---------------------------------------------------------------------------
# R4 BYOA refusal
# ---------------------------------------------------------------------------


def test_validation_refuses_system_prompt_only_for_byoa():
    byoa = _stand_in_release(byoa=True)
    validate_profile_configuration(_stand_in_profile(system_prompt=None), byoa)
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(_stand_in_profile(system_prompt="x"), byoa)
    assert error.value.code == "BYOA_FIELD_UNSUPPORTED"
    assert error.value.field == "system_prompt"
    assert "not forwarded to externally installed agents" in str(error.value)
    # max_context_tokens keeps being reported first when both are set.
    with pytest.raises(ProfileConfigurationError) as both:
        validate_profile_configuration(
            _stand_in_profile(system_prompt="x", max_context_tokens=1), byoa
        )
    assert both.value.field == "max_context_tokens"

    validate_profile_configuration(
        _stand_in_profile(framework_version="code4me2-agent", system_prompt="x"),
        _stand_in_release(byoa=False),
    )


def test_byoa_profile_refuses_system_prompt_on_create_and_update(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    def payload(**overrides):
        return _payload(
            "byoa-prompt",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
            **overrides,
        )

    refused = client.post(PROFILES_PATH, json=payload(system_prompt=PROMPT))
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "BYOA_FIELD_UNSUPPORTED"
    assert detail["field"] == "system_prompt"
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name = 'byoa-prompt'")
        ).scalar_one() == 0

    # A blank prompt normalizes to "none", which a BYOA runtime accepts.
    created = client.post(PROFILES_PATH, json=payload(system_prompt="   "))
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]
    assert created.json()["profile"]["system_prompt"] is None

    update_refused = client.put(
        f"{PROFILES_PATH}/{profile_id}", json=payload(system_prompt=PROMPT)
    )
    assert update_refused.status_code == 422, update_refused.text
    assert update_refused.json()["detail"]["code"] == "BYOA_FIELD_UNSUPPORTED"
    assert update_refused.json()["detail"]["field"] == "system_prompt"
    assert _stored_prompt(session_factory, profile_id)[0] is None


def test_study_freeze_refuses_a_byoa_profile_carrying_a_prompt(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        # A row written around the API validators (e.g. by direct SQL).
        smuggled = seed_profile(
            session,
            owner_id=seeded.owner,
            framework_version="codex",
            release_id=seeded.byoa,
            connection_id=seeded.connection,
            system_prompt="smuggled",
        )
    current_user["value"] = researcher_user(seeded.owner)

    refused = client.post(
        "/api/research/studies",
        json={
            "name": "byoa-prompt-freeze",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(smuggled)],
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "BYOA_FIELD_UNSUPPORTED"
    assert "system_prompt" in refused.json()["detail"]["message"]
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM public.study")).scalar_one() == 0


# ---------------------------------------------------------------------------
# R5 release catalogue
# ---------------------------------------------------------------------------


def test_only_packaged_releases_advertise_the_system_prompt(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    response = client.get("/api/research/agents/release-catalogue")
    assert response.status_code == 200, response.text
    entries = {entry["release_id"]: entry for entry in response.json()["releases"]}
    packaged_fields = entries[seeded.packaged]["configurable_fields"]
    assert packaged_fields[-2:] == ["max_context_tokens", "system_prompt"]
    # BYOA lists stay binding-derived: a full binding set still omits it.
    assert "system_prompt" not in entries[seeded.byoa]["configurable_fields"]
    assert "system_prompt" not in release_profile_configurability(
        _stand_in_release(byoa=True)
    )["configurable_fields"]


# ---------------------------------------------------------------------------
# R6 freeze snapshots, R7 serving
# ---------------------------------------------------------------------------


def _legacy_snapshot(profile: dict) -> dict:
    """The study snapshot exactly as it was before the ``system_prompt`` field."""
    return {
        "profile_id": profile["profile_id"],
        "name": profile["name"],
        "model": profile["model"],
        "framework_version": profile["framework_version"],
        "release_id": profile["release_id"],
        "connection_id": profile["connection"]["connection_id"],
        "tools_json": profile["tools_json"],
        "approval_policy": profile["approval_policy"],
        "max_steps": profile["max_steps"],
        "temperature": profile["temperature"],
        "max_context_tokens": profile["max_context_tokens"],
    }


def _joined_arm(client, session_factory, current_user, seeded, *, label, system_prompt):
    """Create a profile + study, then enroll one participant through the web join."""
    current_user["value"] = researcher_user(seeded.owner)
    created = client.post(
        PROFILES_PATH,
        json=_payload(
            f"arm-{label}",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt=system_prompt,
        ),
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    study = client.post(
        "/api/research/studies",
        json={
            "name": f"prompt-study-{label}",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [profile["profile_id"]],
        },
    )
    assert study.status_code == 201, study.text
    study_body = study.json()["study"]
    with session_factory() as session:
        participant = seed_account(session, f"prompt-participant-{label}@example.com")
    current_user["value"] = participant_user(participant)
    joined = client.post(
        "/api/research/join",
        json={"join_code": study_body["join_code"], "accept_consent": True},
    )
    assert joined.status_code == 201, joined.text
    return SimpleNamespace(
        profile=profile,
        study_id=study_body["study_id"],
        participant=participant,
        assignment_id=joined.json()["assignment_id"],
    )


def _frozen_rows(session_factory, arm) -> SimpleNamespace:
    with session_factory() as session:
        selection = session.execute(
            text(
                "SELECT profile_snapshot_json, profile_digest FROM public.study_agent_profile "
                "WHERE study_id = :id"
            ),
            {"id": arm.study_id},
        ).one()
        assignment = session.execute(
            text(
                "SELECT profile_snapshot_json, profile_digest FROM public.study_assignment "
                "WHERE assignment_id = :id"
            ),
            {"id": arm.assignment_id},
        ).one()
    return SimpleNamespace(selection=selection, assignment=assignment)


def test_study_and_assignment_snapshots_freeze_the_prompt_only_when_set(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    prompted = _joined_arm(
        client, session_factory, current_user, seeded, label="prompted", system_prompt=PROMPT
    )
    plain = _joined_arm(
        client, session_factory, current_user, seeded, label="plain", system_prompt=None
    )

    frozen = _frozen_rows(session_factory, prompted)
    expected = {**_legacy_snapshot(prompted.profile), "system_prompt": PROMPT}
    assert frozen.selection.profile_snapshot_json == expected
    assert frozen.selection.profile_digest == canonical_hash(expected)
    assert frozen.assignment.profile_snapshot_json == expected
    assert frozen.assignment.profile_digest == frozen.selection.profile_digest

    frozen_plain = _frozen_rows(session_factory, plain)
    legacy = _legacy_snapshot(plain.profile)
    assert frozen_plain.selection.profile_snapshot_json == legacy
    assert "system_prompt" not in frozen_plain.assignment.profile_snapshot_json
    # Old snapshots and digests are unchanged for prompt-less profiles.
    assert frozen_plain.selection.profile_digest == canonical_hash(legacy)


def test_lifecycle_profile_snapshot_includes_the_prompt_only_when_set():
    base = dict(
        profile_id=uuid.uuid4(),
        name="unit",
        model="model",
        framework_version="code4me2-agent",
        release_id="release",
        connection_id=None,
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        max_context_tokens=None,
    )
    plain = profile_snapshot(SimpleNamespace(**base, system_prompt=None))
    assert "system_prompt" not in plain
    assert profile_snapshot(SimpleNamespace(**base)) == plain
    assert profile_snapshot(SimpleNamespace(**base, system_prompt="p")) == {
        **plain,
        "system_prompt": "p",
    }


def test_frozen_config_reads_the_prompt_from_the_snapshot():
    snapshot = {"profile_id": str(uuid.uuid4()), "model": "model"}
    assert _frozen_config_from_snapshot(snapshot, None).system_prompt is None
    assert (
        _frozen_config_from_snapshot({**snapshot, "system_prompt": PROMPT}, None).system_prompt
        == PROMPT
    )
    assert (
        _frozen_config_from_snapshot({**snapshot, "system_prompt": 7}, None).system_prompt
        is None
    )


class _AcpRuntime:
    """The two ``App`` accessors ``GET /api/acp/agent-config`` uses."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def get_db_session(self):
        return self._session_factory()

    def get_redis_manager(self):
        return None


def _authorization_for(account_id):
    class _Authorization:
        def __init__(self, _redis_manager):
            pass

        def validate_or_refresh(self, _token):
            return SimpleNamespace(user_id=str(account_id))

    return _Authorization


def _agent_config(session_factory, account_id, managed_protocol_version):
    with patch(
        "backend.routers.acp.AcpAuthorizationService", _authorization_for(account_id)
    ):
        response = get_acp_agent_config(
            app=_AcpRuntime(session_factory),
            authorization="Bearer acp-token",
            managed_protocol_version=managed_protocol_version,
        )
    assert response.status_code == 200, response.body
    return json.loads(response.body)


def test_agent_config_and_run_policy_serve_the_frozen_prompt(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    prompted = _joined_arm(
        client, session_factory, current_user, seeded, label="served", system_prompt=PROMPT
    )
    plain = _joined_arm(
        client, session_factory, current_user, seeded, label="unserved", system_prompt=None
    )

    # The profile template changes after the freeze; the frozen prompt wins.
    with session_factory() as session:
        session.execute(
            text("UPDATE public.agent_profile SET system_prompt = 'edited later' "
                 "WHERE profile_id = :id"),
            {"id": prompted.profile["profile_id"]},
        )
        session.commit()

    for version in (None, "1"):
        served = _agent_config(session_factory, prompted.participant, version)
        assert served["agent_profile"] == "arm-served"
        assert served["system_prompt"] == PROMPT
        unserved = _agent_config(session_factory, plain.participant, version)
        assert unserved["agent_profile"] == "arm-unserved"
        assert "system_prompt" in unserved
        assert unserved["system_prompt"] is None

    with session_factory() as session:
        resolution = resolve_assignment_context(session, prompted.participant)
        assert isinstance(resolution.profile, FrozenAgentConfig)
        assert resolution.profile.system_prompt == PROMPT
        policy = _managed_policy(
            session, prompted.participant, resolution.profile, study_id=resolution.study_id
        )
        plain_resolution = resolve_assignment_context(session, plain.participant)
        plain_policy = _managed_policy(
            session,
            plain.participant,
            plain_resolution.profile,
            study_id=plain_resolution.study_id,
        )
    assert policy["system_prompt"] == PROMPT
    assert _valid_managed_policy_snapshot(policy) is True
    assert "system_prompt" not in plain_policy
    assert _valid_managed_policy_snapshot(plain_policy) is True


def test_run_policy_omits_the_key_for_stand_in_profiles_without_a_prompt():
    profile = SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json="[]",
        approval_policy="per_step",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
    )
    with patch("backend.routers.acp.crud.get_user_by_id", return_value=None), patch(
        "backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False
    ):
        policy = _managed_policy(MagicMock(), uuid.uuid4(), profile)
        prompted = _managed_policy(
            MagicMock(), uuid.uuid4(), SimpleNamespace(**vars(profile), system_prompt="p")
        )
    assert "system_prompt" not in policy
    assert prompted == {**policy, "system_prompt": "p"}


def test_switching_a_prompted_profile_to_byoa_without_the_field_clears_it(http_runtime):
    """The website omits system_prompt for a BYOA runtime (the field is hidden);
    switching a managed profile with a prompt to codex must not strand it with a
    prompt the BYOA check refuses."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "switched",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            system_prompt=PROMPT,
        ),
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]

    switched = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=_payload(
            "switched",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
        ),
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["profile"]["system_prompt"] is None
    stored, digest = _stored_prompt(session_factory, profile_id)
    assert stored is None
    # The same digest as a profile that never had a prompt.
    assert digest == canonical_hash(_legacy_configuration(switched.json()["profile"]))
