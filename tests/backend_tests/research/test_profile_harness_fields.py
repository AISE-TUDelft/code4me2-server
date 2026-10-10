"""Built-in runtime command and harness profile fields (decision D-01).

Run 2026-09-26-agent-harness-tiers, issue 01; the command list became a
denylist in run 2026-10-10-command-denylist. Three optional ``agent_profile``
columns govern the managed (``code4me2-agent``) runtime; Goose and Codex refuse
them with the typed 422 ``BYOA_FIELD_UNSUPPORTED``, like ``max_context_tokens``:

* ``commands_denylist_json`` (API ``commands_denylist``): at most 64 bare
  command names, unique ignoring case, that the agent may not run; every other
  program may run. ``[]`` blocks nothing and is stored as null;
* ``command_timeout_seconds``: an integer from 1 to 600;
* ``harness_options_json`` (API ``harness_options``): known switches only, and
  a ``verify_command`` may not run a program the profile blocks.

Null keeps the default. Set values join the configuration digest, the study
selection/assignment snapshots, ``GET /api/acp/agent-config`` and the managed
run policy; profiles without them keep byte-identical digests and snapshots.
Every config and run policy carries ``commands_denylist`` (the profile's list
plus a user's config-row additions) and, for runtime releases 0.0.6 and
earlier, the legacy ``commands_allowlist``, computed as before: a snapshot
frozen before the denylist, else a config-row allowlist, else the fallback.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from agents import tools as tool_catalogue
from agents.registry import (
    FrozenAgentConfig,
    _frozen_config_from_snapshot,
    resolve_assignment_context,
)
from backend.Responses import AcpAgentConfigGetResponse
from backend.routers.acp import (
    FALLBACK_COMMANDS_ALLOWLIST,
    MANAGED_PROTOCOL_VERSION,
    _managed_policy,
    _valid_managed_policy_snapshot,
)
from backend.routers.agent.profiles import AgentProfilePayload
from database import crud
from database.db_schemas import AgentProfile
from research.canonical import canonical_hash, canonical_json
from research.study.agents.distributions import (
    ProfileConfigurationError,
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
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture
from .test_ui_overhaul_profiles import _profile as _stand_in_profile
from .test_ui_overhaul_profiles import _release as _stand_in_release
from .test_ui_overhaul_system_prompt import _agent_config

PROFILES_PATH = "/api/agent/profiles"
REPO_ROOT = Path(__file__).resolve().parents[3]

DENYLIST = ["git", "curl", "rm", "gradlew"]
OPTIONS = {
    "self_review": False,
    "verify_on_stop": True,
    "verify_command": ["pytest", "-q", "tests"],
    "prompt_profile": "anthropic",
    "parallel_tools": False,
}
HARNESS = {
    "commands_denylist": DENYLIST,
    "command_timeout_seconds": 300,
    "harness_options": OPTIONS,
}
FIELDS = ("commands_denylist", "command_timeout_seconds", "harness_options")


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


def _seed_authoring(session, email: str = "harness-owner@example.com") -> SimpleNamespace:
    owner = seed_account(session, email, can_research=True)
    return SimpleNamespace(
        owner=owner,
        connection=seed_connection(session, label=f"harness-connection-{owner.hex[:8]}"),
        packaged=seed_packaged_release(session),
        byoa=seed_byoa_release(
            session, agent_id="codex", agent_package="codex", agent_command="codex-acp"
        ),
    )


def _stored(session_factory, profile_id) -> SimpleNamespace:
    with session_factory() as session:
        return session.execute(
            text(
                "SELECT commands_denylist_json, command_timeout_seconds, "
                "harness_options_json, configuration_digest "
                "FROM public.agent_profile WHERE profile_id = :id"
            ),
            {"id": profile_id},
        ).one()


def _legacy_configuration(profile: dict) -> dict:
    """The digest input exactly as it was before the D-01 fields."""
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


def _legacy_snapshot(profile: dict) -> dict:
    """The study snapshot exactly as it was before the D-01 fields."""
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


def _base_payload_fields() -> dict:
    return {
        "name": "arm",
        "model": "model",
        "framework_version": "code4me2-agent",
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 3,
        "connection_id": uuid.uuid4(),
    }


# ---------------------------------------------------------------------------
# R1 schema
# ---------------------------------------------------------------------------


def test_migrations_create_the_nullable_columns(http_runtime):
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'agent_profile' "
                "AND column_name IN ('commands_denylist_json', 'commands_allowlist_json', "
                "'command_timeout_seconds', 'harness_options_json')"
            )
        ).all()
    columns = {row.column_name: (row.data_type, row.is_nullable) for row in rows}
    assert columns == {
        "commands_denylist_json": ("text", "YES"),
        # Retired: kept in the table so the previous backend can be rolled
        # back to, but no longer mapped or read.
        "commands_allowlist_json": ("text", "YES"),
        "command_timeout_seconds": ("integer", "YES"),
        "harness_options_json": ("text", "YES"),
    }
    table = AgentProfile.__table__
    for column in ("commands_denylist_json", "command_timeout_seconds", "harness_options_json"):
        assert table.c[column].nullable is True
    assert "commands_allowlist_json" not in table.c


def test_orm_row_decodes_the_json_columns_and_surfaces_undecodable_text():
    row = AgentProfile(commands_denylist_json='["git","curl"]', harness_options_json=None)
    assert row.commands_denylist == ["git", "curl"]
    assert row.harness_options is None
    assert not hasattr(row, "commands_allowlist")
    # Text written around the API is surfaced verbatim, so validators refuse it
    # instead of reading it as "unset".
    broken = AgentProfile(commands_denylist_json="not json", harness_options_json="{")
    assert broken.commands_denylist == "not json"
    assert broken.harness_options == "{"


def test_release_notes_list_the_statements_for_existing_databases():
    notes = (REPO_ROOT / "docs" / "research-platform" / "RELEASES.md").read_text(
        encoding="utf-8"
    )
    for column, sql_type in (
        ("commands_denylist_json", "TEXT"),
        ("command_timeout_seconds", "INTEGER"),
        ("harness_options_json", "TEXT"),
    ):
        statement = (
            "ALTER TABLE public.agent_profile ADD COLUMN IF NOT EXISTS "
            f"{column} {sql_type};"
        )
        assert statement in notes, statement
    # The denylist column ships as an Alembic revision, which `migrate` applies.
    assert "d41e7c9a2b6f" in notes


# ---------------------------------------------------------------------------
# R2 payload validation (shape; the cross-field rule runs on the merged profile)
# ---------------------------------------------------------------------------


def test_payload_accepts_valid_values_and_keeps_order():
    common = _base_payload_fields()
    plain = AgentProfilePayload(**common)
    assert (plain.commands_denylist, plain.command_timeout_seconds, plain.harness_options) == (
        None,
        None,
        None,
    )
    assert plain.model_fields_set.isdisjoint(FIELDS)

    names = [f"tool-{index}.v2_x+y" for index in range(64)]
    payload = AgentProfilePayload(
        **common,
        commands_denylist=names,
        command_timeout_seconds=600,
        harness_options={
            **{key: False for key in tool_catalogue.HARNESS_BOOLEAN_OPTIONS},
            "prompt_profile": "gemini",
            "verify_command": ["pytest"] + ["x" * 512] * 31,
        },
    )
    assert payload.commands_denylist == names
    assert payload.command_timeout_seconds == 600
    assert payload.harness_options["prompt_profile"] == "gemini"
    assert AgentProfilePayload(**common, command_timeout_seconds=1).command_timeout_seconds == 1
    # An empty denylist blocks nothing, so it is stored as null, like an empty
    # options object; the field still counts as sent (it clears on an update).
    empty_denylist = AgentProfilePayload(**common, commands_denylist=[])
    assert empty_denylist.commands_denylist is None
    assert "commands_denylist" in empty_denylist.model_fields_set
    empty_options = AgentProfilePayload(**common, harness_options={})
    assert empty_options.harness_options is None
    assert "harness_options" in empty_options.model_fields_set
    explicit_null_verify = AgentProfilePayload(
        **common, harness_options={"verify_command": None}
    )
    assert explicit_null_verify.harness_options == {"verify_command": None}
    # Shape only: whether a verify command is blocked is checked on the merged
    # profile (an update may rely on the stored denylist).
    assert AgentProfilePayload(
        **common, commands_denylist=["make"], harness_options={"verify_command": ["make", "check"]}
    ).harness_options == {"verify_command": ["make", "check"]}


def test_payload_refuses_the_retired_allowlist():
    with pytest.raises(ValueError) as error:
        AgentProfilePayload(**_base_payload_fields(), commands_allowlist=["git"])
    assert "commands_allowlist" in str(error.value)
    assert "Extra inputs are not permitted" in str(error.value)


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("git", "must be a list"),
        ({"git": True}, "must be a list"),
        (["./gradlew"], "bare command name"),
        (["/usr/bin/git"], "bare command name"),
        (["bin\\git"], "bare command name"),
        (["git status"], "bare command name"),
        (["rm;ls"], "bare command name"),
        (["a|b"], "bare command name"),
        (["$(id)"], "bare command name"),
        (["-rf"], "bare command name"),
        ([".hidden"], "bare command name"),
        ([""], "bare command name"),
        (["git\n"], "bare command name"),
        (["x" * 65], "bare command name"),
        ([7], "bare command name"),
        ([None], "bare command name"),
        (["git", "git"], "more than once"),
        (["git", "GIT"], "more than once"),
        ([f"c{index}" for index in range(65)], "at most 64"),
    ],
)
def test_payload_refuses_an_invalid_command_denylist(value, fragment):
    with pytest.raises(ValueError) as error:
        AgentProfilePayload(**_base_payload_fields(), commands_denylist=value)
    assert "commands_denylist" in str(error.value)
    assert fragment in str(error.value)


@pytest.mark.parametrize("value", [0, 601, -5, True, False, 1.5, 30.0, "30", [30], {"s": 30}])
def test_payload_refuses_an_invalid_command_timeout(value):
    with pytest.raises(ValueError) as error:
        AgentProfilePayload(**_base_payload_fields(), command_timeout_seconds=value)
    assert "command_timeout_seconds must be a whole number of seconds from 1 to 600" in str(
        error.value
    )


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (["self_review"], "must be an object"),
        ("self_review=false", "must be an object"),
        ({"self_reveiw": False}, "unknown keys: self_reveiw"),
        ({"max_steps": 3}, "unknown keys: max_steps"),
        ({"self_review": 0}, "harness_options.self_review must be true or false"),
        ({"loop_guard": "false"}, "harness_options.loop_guard must be true or false"),
        ({"syntax_check": None}, "harness_options.syntax_check must be true or false"),
        ({"prompt_profile": "gpt"}, "prompt_profile must be one of"),
        ({"prompt_profile": None}, "prompt_profile must be one of"),
        ({"prompt_profile": ["auto"]}, "prompt_profile must be one of"),
        ({"verify_command": []}, "list of 1 to 32 arguments"),
        ({"verify_command": "pytest -q"}, "list of 1 to 32 arguments"),
        ({"verify_command": ["pytest"] * 33}, "list of 1 to 32 arguments"),
        ({"verify_command": ["pytest", ""]}, "verify_command[1] must be a non-empty string"),
        ({"verify_command": ["pytest", "x" * 513]}, "at most 512 characters"),
        ({"verify_command": ["pytest", 3]}, "verify_command[1] must be a non-empty string"),
        ({"verify_command": ["pytest", "a\x00b"]}, "verify_command[1] must be a non-empty string"),
        ({"verify_command": ["./gradlew", "test"]}, "verify_command[0]"),
        ({"verify_command": ["python -m pytest"]}, "verify_command[0]"),
    ],
)
def test_payload_refuses_invalid_harness_options(value, fragment):
    with pytest.raises(ValueError) as error:
        AgentProfilePayload(**_base_payload_fields(), harness_options=value)
    assert "harness_options" in str(error.value)
    assert fragment in str(error.value)


def test_validators_are_strict_about_bool_and_trailing_newlines():
    assert tool_catalogue.is_bare_command_name("pytest")
    assert tool_catalogue.is_bare_command_name("python3.12")
    assert tool_catalogue.is_bare_command_name("g++")
    assert not tool_catalogue.is_bare_command_name("pytest\n")
    assert not tool_catalogue.is_bare_command_name(True)
    with pytest.raises(ValueError):
        tool_catalogue.validate_command_timeout_seconds(True)
    assert tool_catalogue.validate_command_timeout_seconds(120) == 120
    # The profile contract refuses a verify command that starts a blocked
    # program, also through a shell; the policy-snapshot check passes no list.
    options = {"verify_command": ["pytest", "-q"]}
    assert tool_catalogue.validate_harness_options(options) == options
    assert tool_catalogue.validate_harness_options(options, commands_denylist=["git"]) == options
    with pytest.raises(ValueError, match="'pytest', which is in the profile's commands_denylist"):
        tool_catalogue.validate_harness_options(options, commands_denylist=["git", "PyTest"])
    through_shell = {"verify_command": ["bash", "-c", "pytest -q && git diff --check"]}
    with pytest.raises(ValueError, match="runs 'git'"):
        tool_catalogue.validate_harness_options(through_shell, commands_denylist=["git"])


@pytest.mark.parametrize(
    ("argv", "blocked"),
    [
        (["git", "push"], "git"),
        (["GIT"], "git"),
        (["/usr/bin/git", "status"], "git"),
        (["C:\\Program Files\\Git\\bin\\git.exe", "status"], "git"),
        (["./git"], "git"),
        (["bash", "-c", "cd src && git status"], "git"),
        (["/bin/bash", "-lc", "/usr/bin/git diff"], "git"),
        (["sh", "-c", "echo $(git rev-parse HEAD)"], "git"),
        (["zsh", "-c", "pytest; `git log`"], "git"),
        (["cmd.exe", "/c", "git status"], "git"),
        (["powershell", "-Command", "& 'C:\\Tools\\git.exe' status"], "git"),
        (["env", "GIT_PAGER=cat", "git", "log"], "git"),
        (["xargs", "-I{}", "git", "add", "{}"], "git"),
        (["timeout", "60", "git", "fetch"], "git"),
        (["sudo", "git", "clean", "-fdx"], "git"),
        (["tcsh", "-c", "git --version"], "git"),
        (["script", "-q", "/dev/null", "git", "--version"], "git"),
        (["uv", "run", "git", "push"], "git"),
        (["uv", "--quiet", "run", "git"], "git"),
        (["uv", "--directory", "sub", "run", "git", "push"], "git"),
        (["npm", "--prefix", "web", "exec", "git", "status"], "git"),
        (["npm", "x", "-c", "git push"], "git"),
        (["npx", "git@2", "status"], "git"),
        (["uv", "pip", "install", "git"], "git"),
        (["npm", "exec", "--", "git"], "git"),
        (["su", "-c", "git push"], "git"),
        (["bash", "-c", "g''it push"], "git"),
        (["git.exe.", "push"], "git"),
        (["uv", "pip", "install", "requests"], None),
        # Not a shell, launcher or package manager: its arguments are data.
        (["grep", "-r", "git", "."], None),
        (["python", "-c", "import subprocess"], None),
        (["bash", "-c", "pytest -q"], None),
        (["gitk"], None),
        (["legit"], None),
        (["git-lfs"], None),
    ],
)
def test_blocked_command_matching(argv, blocked):
    assert tool_catalogue.blocked_command_in(argv, ["Git"]) == blocked
    assert tool_catalogue.blocked_command_in(argv, []) is None


# ---------------------------------------------------------------------------
# R1/R2 API: create / read / list / update / clone and 422s
# ---------------------------------------------------------------------------


def test_create_read_and_list_round_trip_the_fields(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "harnessed", connection_id=seeded.connection, release_id=seeded.packaged, **HARNESS
        ),
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    for field, value in HARNESS.items():
        assert profile[field] == value, field
    assert "commands_allowlist" not in profile

    stored = _stored(session_factory, profile["profile_id"])
    assert stored.commands_denylist_json == canonical_json(DENYLIST)
    assert json.loads(stored.commands_denylist_json) == DENYLIST  # order kept
    assert stored.command_timeout_seconds == 300
    assert stored.harness_options_json == canonical_json(OPTIONS)

    fetched = client.get(f"{PROFILES_PATH}/{profile['profile_id']}")
    assert fetched.status_code == 200, fetched.text
    listed = client.get(PROFILES_PATH)
    assert listed.status_code == 200, listed.text
    by_name = {item["name"]: item for item in listed.json()["profiles"]}
    for view in (fetched.json()["profile"], by_name["harnessed"]):
        for field, value in HARNESS.items():
            assert view[field] == value, field

    # The digest covers the set fields (and only those beyond the legacy input).
    assert profile["configuration_digest"] == canonical_hash(
        {**_legacy_configuration(profile), **HARNESS}
    )
    assert stored.configuration_digest == profile["configuration_digest"]


def test_omitted_fields_are_null_and_keep_the_legacy_digest(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    plain = client.post(
        PROFILES_PATH,
        json=_payload("plain", connection_id=seeded.connection, release_id=seeded.packaged),
    )
    assert plain.status_code == 201, plain.text
    profile = plain.json()["profile"]
    for field in FIELDS:
        assert field in profile
        assert profile[field] is None
    stored = _stored(session_factory, profile["profile_id"])
    assert (
        stored.commands_denylist_json,
        stored.command_timeout_seconds,
        stored.harness_options_json,
    ) == (None, None, None)
    assert profile["configuration_digest"] == canonical_hash(_legacy_configuration(profile))

    # Explicit nulls and empty values are the same profile: nothing blocked.
    explicit_empty = client.post(
        PROFILES_PATH,
        json=_payload(
            "explicit-empty",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            commands_denylist=[],
            command_timeout_seconds=None,
            harness_options={},
        ),
    )
    assert explicit_empty.status_code == 201, explicit_empty.text
    view = explicit_empty.json()["profile"]
    assert [view[field] for field in FIELDS] == [None, None, None]
    assert _stored(session_factory, view["profile_id"]).commands_denylist_json is None
    assert view["configuration_digest"] == canonical_hash(_legacy_configuration(view))


def test_put_keeps_omitted_fields_replaces_sent_ones_and_null_clears(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    def payload(**overrides):
        return _payload(
            "replaced", connection_id=seeded.connection, release_id=seeded.packaged, **overrides
        )

    created = client.post(PROFILES_PATH, json=payload(**HARNESS))
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]

    # Omitted: a client that does not show the fields must not wipe them.
    omitted = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload(max_steps=5))
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["profile"]["max_steps"] == 5
    for field, value in HARNESS.items():
        assert omitted.json()["profile"][field] == value, field

    replaced = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=payload(
            commands_denylist=["curl"],
            command_timeout_seconds=45,
            harness_options={"verify_command": ["make", "check"]},
        ),
    )
    assert replaced.status_code == 200, replaced.text
    view = replaced.json()["profile"]
    assert view["commands_denylist"] == ["curl"]
    assert view["command_timeout_seconds"] == 45
    assert view["harness_options"] == {"verify_command": ["make", "check"]}

    # One field at a time: the others are kept.
    timeout_only = client.put(
        f"{PROFILES_PATH}/{profile_id}", json=payload(command_timeout_seconds=None)
    )
    assert timeout_only.status_code == 200, timeout_only.text
    assert timeout_only.json()["profile"]["command_timeout_seconds"] is None
    assert timeout_only.json()["profile"]["commands_denylist"] == ["curl"]

    # An empty list clears like null.
    emptied = client.put(f"{PROFILES_PATH}/{profile_id}", json=payload(commands_denylist=[]))
    assert emptied.status_code == 200, emptied.text
    assert emptied.json()["profile"]["commands_denylist"] is None

    cleared = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=payload(commands_denylist=None, harness_options=None),
    )
    assert cleared.status_code == 200, cleared.text
    cleared_view = cleared.json()["profile"]
    assert [cleared_view[field] for field in FIELDS] == [None, None, None]
    stored = _stored(session_factory, profile_id)
    assert (
        stored.commands_denylist_json,
        stored.command_timeout_seconds,
        stored.harness_options_json,
    ) == (None, None, None)
    # Exactly the digest of the same profile created without the fields.
    assert stored.configuration_digest == canonical_hash(_legacy_configuration(cleared_view))


def test_partial_repository_update_keeps_the_fields_unless_flagged(http_runtime):
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        profile = crud.create_agent_profile(
            session,
            owner_user_id=seeded.owner,
            name="partial-harness",
            model="model",
            tools_json="[]",
            approval_policy="auto",
            max_steps=2,
            framework_version="code4me2-agent",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            **HARNESS,
        )
        assert profile.commands_denylist == DENYLIST
        assert profile.harness_options == OPTIONS
        updated = crud.update_agent_profile(session, profile.profile_id, model="other")
        assert updated.model == "other"
        assert (updated.commands_denylist, updated.command_timeout_seconds, updated.harness_options) == (
            DENYLIST,
            300,
            OPTIONS,
        )
        # The merged state is validated: blocking the stored verify command's
        # program is refused.
        with pytest.raises(ProfileConfigurationError) as stranded:
            crud.update_agent_profile(
                session,
                profile.profile_id,
                commands_denylist=[*DENYLIST, "pytest"],
                update_commands_denylist=True,
            )
        assert stranded.value.code == "HARNESS_OPTIONS_INVALID"
        session.rollback()
        cleared = crud.update_agent_profile(
            session,
            profile.profile_id,
            commands_denylist=None,
            command_timeout_seconds=None,
            harness_options=None,
            update_commands_denylist=True,
            update_command_timeout_seconds=True,
            update_harness_options=True,
        )
        assert (cleared.commands_denylist_json, cleared.command_timeout_seconds, cleared.harness_options_json) == (
            None,
            None,
            None,
        )


def test_invalid_values_are_a_422_and_store_nothing(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    cases = [
        ({"commands_denylist": ["./gradlew"]}, "commands_denylist"),
        ({"commands_denylist": ["git", "Git"]}, "commands_denylist"),
        ({"commands_denylist": [f"c{index}" for index in range(65)]}, "commands_denylist"),
        # The retired field is refused like any unknown one.
        ({"commands_allowlist": ["git"]}, "commands_allowlist"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"command_timeout_seconds": 601}, "command_timeout_seconds"),
        ({"command_timeout_seconds": True}, "command_timeout_seconds"),
        ({"command_timeout_seconds": "30"}, "command_timeout_seconds"),
        ({"harness_options": {"unknown_switch": True}}, "harness_options"),
        ({"harness_options": {"self_review": "no"}}, "harness_options"),
        ({"harness_options": {"prompt_profile": "llama"}}, "harness_options"),
        ({"harness_options": {"verify_command": []}}, "harness_options"),
        ({"harness_options": ["self_review"]}, "harness_options"),
    ]
    for index, (overrides, field) in enumerate(cases):
        response = client.post(
            PROFILES_PATH,
            json=_payload(
                f"invalid-{index}",
                connection_id=seeded.connection,
                release_id=seeded.packaged,
                **overrides,
            ),
        )
        assert response.status_code == 422, (overrides, response.text)
        errors = response.json()["detail"]
        assert any(error["loc"][-1] == field for error in errors), (overrides, errors)
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name LIKE 'invalid-%'")
        ).scalar_one() == 0


def test_verify_command_may_not_run_a_blocked_program(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    def payload(name="verified", **overrides):
        return _payload(
            name, connection_id=seeded.connection, release_id=seeded.packaged, **overrides
        )

    verify = {"verify_command": ["pytest", "-q"]}
    # Nothing blocked: any program may verify.
    unblocked = client.post(PROFILES_PATH, json=payload("open-verify", harness_options=verify))
    assert unblocked.status_code == 201, unblocked.text

    blocked = client.post(
        PROFILES_PATH, json=payload(commands_denylist=["Pytest"], harness_options=verify)
    )
    assert blocked.status_code == 422, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "HARNESS_OPTIONS_INVALID"
    assert detail["field"] == "harness_options"
    assert "'pytest'" in detail["message"]
    assert "commands_denylist" in detail["message"]

    through_shell = client.post(
        PROFILES_PATH,
        json=payload(
            commands_denylist=["git"],
            harness_options={"verify_command": ["bash", "-c", "pytest -q && git diff --check"]},
        ),
    )
    assert through_shell.status_code == 422, through_shell.text
    assert through_shell.json()["detail"]["code"] == "HARNESS_OPTIONS_INVALID"
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name = 'verified'")
        ).scalar_one() == 0

    created = client.post(
        PROFILES_PATH, json=payload(commands_denylist=["git"], harness_options=verify)
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]

    # An update that omits the denylist is checked against the stored one.
    refused = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=payload(harness_options={"verify_command": ["git", "diff", "--check"]}),
    )
    assert refused.status_code == 422, refused.text
    kept = client.put(
        f"{PROFILES_PATH}/{profile_id}",
        json=payload(harness_options={"verify_command": ["make", "check"]}),
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["profile"]["commands_denylist"] == ["git"]

    # Blocking the stored verify command's program is refused.
    stranded = client.put(
        f"{PROFILES_PATH}/{profile_id}", json=payload(commands_denylist=["git", "make"])
    )
    assert stranded.status_code == 422, stranded.text
    assert stranded.json()["detail"]["code"] == "HARNESS_OPTIONS_INVALID"
    stored = _stored(session_factory, profile_id)
    assert json.loads(stored.commands_denylist_json) == ["git"]
    assert json.loads(stored.harness_options_json) == {"verify_command": ["make", "check"]}


def test_clone_round_trip_preserves_the_fields(http_runtime):
    """The editor's Clone re-creates a profile from its read view."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    source = client.post(
        PROFILES_PATH,
        json=_payload(
            "clone-source", connection_id=seeded.connection, release_id=seeded.packaged, **HARNESS
        ),
    )
    assert source.status_code == 201, source.text
    read = client.get(f"{PROFILES_PATH}/{source.json()['profile']['profile_id']}").json()["profile"]

    clone_payload = _payload(
        "clone-source-copy",
        connection_id=seeded.connection,
        release_id=read["release_id"],
        **{field: read[field] for field in FIELDS},
    )
    clone = client.post(PROFILES_PATH, json=clone_payload)
    assert clone.status_code == 201, clone.text
    cloned = clone.json()["profile"]
    for field in FIELDS:
        assert cloned[field] == read[field], field
    source_stored = _stored(session_factory, read["profile_id"])
    clone_stored = _stored(session_factory, cloned["profile_id"])
    assert clone_stored.commands_denylist_json == source_stored.commands_denylist_json
    assert clone_stored.harness_options_json == source_stored.harness_options_json


# ---------------------------------------------------------------------------
# R2 BYOA refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commands_denylist", ["git"]),
        ("command_timeout_seconds", 30),
        ("harness_options", {"self_review": False}),
    ],
)
def test_validation_refuses_each_field_only_for_byoa(field, value):
    byoa = _stand_in_release(byoa=True)
    validate_profile_configuration(_stand_in_profile(), byoa)
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(_stand_in_profile(**{field: value}), byoa)
    assert error.value.code == "BYOA_FIELD_UNSUPPORTED"
    assert error.value.field == field
    assert "not forwarded to externally installed agents" in str(error.value)

    validate_profile_configuration(
        _stand_in_profile(framework_version="code4me2-agent", **{field: value}),
        _stand_in_release(byoa=False),
    )


def test_managed_validation_reports_the_invalid_field():
    packaged = _stand_in_release(byoa=False)

    def refused(**fields):
        with pytest.raises(ProfileConfigurationError) as error:
            validate_profile_configuration(
                _stand_in_profile(framework_version="code4me2-agent", **fields), packaged
            )
        return error.value

    assert refused(commands_denylist="git").code == "COMMANDS_DENYLIST_INVALID"
    assert refused(commands_denylist=["a b"]).field == "commands_denylist"
    assert refused(command_timeout_seconds=0).code == "COMMAND_TIMEOUT_INVALID"
    assert refused(command_timeout_seconds=0).field == "command_timeout_seconds"
    error = refused(commands_denylist=["pytest"], harness_options={"verify_command": ["pytest"]})
    assert (error.code, error.field) == ("HARNESS_OPTIONS_INVALID", "harness_options")
    assert str(error).startswith("HARNESS_OPTIONS_INVALID: ")
    assert refused(harness_options="{").code == "HARNESS_OPTIONS_INVALID"
    # Nothing blocked: a verify command alone is valid.
    validate_profile_configuration(
        _stand_in_profile(
            framework_version="code4me2-agent", harness_options={"verify_command": ["pytest"]}
        ),
        packaged,
    )


def test_byoa_profile_refuses_the_fields_on_create_and_update(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    def payload(**overrides):
        return _payload(
            "byoa-harness",
            connection_id=seeded.connection,
            release_id=seeded.byoa,
            framework_version="codex",
            **overrides,
        )

    for field, value in HARNESS.items():
        refused = client.post(PROFILES_PATH, json=payload(**{field: value}))
        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert detail["code"] == "BYOA_FIELD_UNSUPPORTED"
        assert detail["field"] == field
    with session_factory() as session:
        assert session.execute(
            text("SELECT count(*) FROM public.agent_profile WHERE name = 'byoa-harness'")
        ).scalar_one() == 0

    created = client.post(PROFILES_PATH, json=payload())
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["profile_id"]
    for field, value in HARNESS.items():
        update_refused = client.put(
            f"{PROFILES_PATH}/{profile_id}", json=payload(**{field: value})
        )
        assert update_refused.status_code == 422, update_refused.text
        assert update_refused.json()["detail"]["code"] == "BYOA_FIELD_UNSUPPORTED"
        assert update_refused.json()["detail"]["field"] == field
    stored = _stored(session_factory, profile_id)
    assert (
        stored.commands_denylist_json,
        stored.command_timeout_seconds,
        stored.harness_options_json,
    ) == (None, None, None)


def test_switching_to_byoa_without_the_fields_clears_them(http_runtime):
    """The editor hides the fields for goose/codex and omits them."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    current_user["value"] = researcher_user(seeded.owner)

    created = client.post(
        PROFILES_PATH,
        json=_payload(
            "switched", connection_id=seeded.connection, release_id=seeded.packaged, **HARNESS
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
    view = switched.json()["profile"]
    assert [view[field] for field in FIELDS] == [None, None, None]
    assert _stored(session_factory, profile_id).configuration_digest == canonical_hash(
        _legacy_configuration(view)
    )


def _insert_raw_profile(session, *, owner, connection, release_id, framework, **columns) -> uuid.UUID:
    """A profile row written around the API validators (e.g. by direct SQL)."""
    profile_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO public.agent_profile "
            "(profile_id, owner_user_id, name, model, framework_version, release_id, "
            "connection_id, tools_json, approval_policy, max_steps, commands_denylist_json, "
            "commands_allowlist_json, command_timeout_seconds, harness_options_json, is_active) "
            "VALUES (:profile_id, :owner, :name, 'model', :framework, :release_id, "
            ":connection, '[]', 'auto', 1, :denylist, :allowlist, :timeout, :options, true)"
        ),
        {
            "profile_id": profile_id,
            "owner": owner,
            "name": f"raw-{profile_id.hex[:8]}",
            "framework": framework,
            "release_id": release_id,
            "connection": connection,
            "denylist": columns.get("commands_denylist_json"),
            "allowlist": columns.get("commands_allowlist_json"),
            "timeout": columns.get("command_timeout_seconds"),
            "options": columns.get("harness_options_json"),
        },
    )
    session.commit()
    return profile_id


def _create_study(client, label: str, profile_id) -> object:
    return client.post(
        "/api/research/studies",
        json={
            "name": f"harness-freeze-{label}",
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [str(profile_id)],
        },
    )


def test_study_freeze_refuses_rows_written_around_the_api(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        packaged = dict(
            owner=seeded.owner,
            connection=seeded.connection,
            release_id=seeded.packaged,
            framework="code4me2-agent",
        )
        smuggled = [
            (
                _insert_raw_profile(
                    session,
                    owner=seeded.owner,
                    connection=seeded.connection,
                    release_id=seeded.byoa,
                    framework="codex",
                    commands_denylist_json='["git"]',
                ),
                "BYOA_FIELD_UNSUPPORTED",
                "commands_denylist",
            ),
            (
                _insert_raw_profile(session, **packaged, commands_denylist_json='["./git"]'),
                "COMMANDS_DENYLIST_INVALID",
                "commands_denylist",
            ),
            (
                _insert_raw_profile(session, **packaged, command_timeout_seconds=0),
                "COMMAND_TIMEOUT_INVALID",
                "command_timeout_seconds",
            ),
            (
                _insert_raw_profile(session, **packaged, harness_options_json="{not json"),
                "HARNESS_OPTIONS_INVALID",
                "harness_options",
            ),
            (
                _insert_raw_profile(
                    session,
                    **packaged,
                    commands_denylist_json='["pytest"]',
                    harness_options_json='{"verify_command":["pytest"]}',
                ),
                "HARNESS_OPTIONS_INVALID",
                "harness_options",
            ),
        ]
    current_user["value"] = researcher_user(seeded.owner)

    for index, (profile_id, code, field) in enumerate(smuggled):
        refused = _create_study(client, str(index), profile_id)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == code
        assert field in refused.json()["detail"]["message"]
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM public.study")).scalar_one() == 0


def test_the_retired_allowlist_column_is_never_read(http_runtime):
    """A template the previous backend saved with an allowlist freezes without it."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
        profile_id = _insert_raw_profile(
            session,
            owner=seeded.owner,
            connection=seeded.connection,
            release_id=seeded.packaged,
            framework="code4me2-agent",
            commands_allowlist_json='["pwd", "ls"]',
        )
    current_user["value"] = researcher_user(seeded.owner)

    view = client.get(f"{PROFILES_PATH}/{profile_id}").json()["profile"]
    assert view["commands_denylist"] is None
    assert "commands_allowlist" not in view
    created = _create_study(client, "retired", profile_id)
    assert created.status_code == 201, created.text
    with session_factory() as session:
        snapshot = session.execute(
            text(
                "SELECT profile_snapshot_json FROM public.study_agent_profile "
                "WHERE study_id = :id"
            ),
            {"id": created.json()["study"]["study_id"]},
        ).scalar_one()
    assert not {"commands_allowlist", "commands_denylist"} & set(snapshot)


# ---------------------------------------------------------------------------
# R3 digest and frozen snapshots
# ---------------------------------------------------------------------------


UNIT_PROFILE_ID = uuid.UUID("00000000-0000-4000-8000-000000000d01")


def _unit_profile(**overrides) -> SimpleNamespace:
    base = dict(
        profile_id=UNIT_PROFILE_ID,
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
    base.update(overrides)
    return SimpleNamespace(**base)


def test_digest_input_and_lifecycle_snapshot_include_only_set_fields():
    unset = _unit_profile(commands_denylist=None, command_timeout_seconds=None, harness_options=None)
    configuration = crud._agent_profile_configuration(unset)
    assert not set(FIELDS) & set(configuration)
    assert crud._agent_profile_configuration(_unit_profile()) == configuration
    assert profile_snapshot(unset) == profile_snapshot(_unit_profile())
    assert not set(FIELDS) & set(profile_snapshot(unset))

    harnessed = _unit_profile(system_prompt="p", **HARNESS)
    assert crud._agent_profile_configuration(harnessed) == {
        **configuration,
        "system_prompt": "p",
        **HARNESS,
    }
    assert list(crud._agent_profile_configuration(harnessed))[-4:] == [
        "system_prompt",
        *FIELDS,
    ]
    assert profile_snapshot(harnessed) == {
        **profile_snapshot(unset),
        "system_prompt": "p",
        **HARNESS,
    }
    # The retired allowlist never joins a digest or a new snapshot.
    retired = _unit_profile(commands_allowlist=["ls"])
    assert crud._agent_profile_configuration(retired) == configuration
    assert profile_snapshot(retired) == profile_snapshot(unset)


def _joined_arm(client, session_factory, current_user, seeded, *, label, **fields):
    """Create a profile + study through the API, then enroll one participant."""
    current_user["value"] = researcher_user(seeded.owner)
    created = client.post(
        PROFILES_PATH,
        json=_payload(
            f"arm-{label}",
            connection_id=seeded.connection,
            release_id=seeded.packaged,
            tools_json='["run_command", "read_file"]',
            **fields,
        ),
    )
    assert created.status_code == 201, created.text
    profile = created.json()["profile"]
    study = client.post(
        "/api/research/studies",
        json={
            "name": f"harness-study-{label}",
            "default_budget_usd": "10",
            "session_policy": VALID_SESSION_POLICY,
            "profile_ids": [profile["profile_id"]],
        },
    )
    assert study.status_code == 201, study.text
    study_body = study.json()["study"]
    with session_factory() as session:
        participant = seed_account(session, f"harness-participant-{label}@example.com")
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


def _set_config_agent(session_factory, account_id, **agent) -> None:
    """Write the user's config-row ``agent`` section (an operator override)."""
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE public.config SET config_data = :data WHERE config_id = "
                '(SELECT config_id FROM public."user" WHERE user_id = :id)'
            ),
            {"data": json.dumps({"agent": agent}), "id": account_id},
        )
        session.commit()


def _freeze_legacy_allowlist(session_factory, arm, allowlist) -> None:
    """Make the arm look frozen before the denylist: its snapshot holds an allowlist."""
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE public.study_assignment SET profile_snapshot_json = "
                "profile_snapshot_json || CAST(:patch AS jsonb) WHERE assignment_id = :id"
            ),
            {"patch": json.dumps({"commands_allowlist": allowlist}), "id": arm.assignment_id},
        )
        session.commit()


def test_study_and_assignment_snapshots_freeze_the_fields_only_when_set(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    harnessed = _joined_arm(client, session_factory, current_user, seeded, label="set", **HARNESS)
    plain = _joined_arm(client, session_factory, current_user, seeded, label="unset")

    frozen = _frozen_rows(session_factory, harnessed)
    expected = {**_legacy_snapshot(harnessed.profile), **HARNESS}
    assert frozen.selection.profile_snapshot_json == expected
    assert frozen.selection.profile_digest == canonical_hash(expected)
    assert frozen.assignment.profile_snapshot_json == expected
    assert frozen.assignment.profile_digest == frozen.selection.profile_digest

    frozen_plain = _frozen_rows(session_factory, plain)
    legacy = _legacy_snapshot(plain.profile)
    assert frozen_plain.selection.profile_snapshot_json == legacy
    assert frozen_plain.selection.profile_digest == canonical_hash(legacy)
    assert not set(FIELDS) & set(frozen_plain.assignment.profile_snapshot_json)


def test_frozen_config_reads_the_fields_from_the_snapshot():
    snapshot = {"profile_id": str(uuid.uuid4()), "model": "model"}
    plain = _frozen_config_from_snapshot(snapshot, None)
    assert (
        plain.commands_denylist,
        plain.commands_allowlist,
        plain.command_timeout_seconds,
        plain.harness_options,
    ) == (None, None, None, None)
    frozen = _frozen_config_from_snapshot({**snapshot, **HARNESS}, None)
    assert frozen.commands_denylist == DENYLIST
    assert frozen.commands_allowlist is None
    assert frozen.command_timeout_seconds == 300
    assert frozen.harness_options == OPTIONS
    # A snapshot frozen before the denylist keeps its allowlist for old runtimes.
    legacy = _frozen_config_from_snapshot({**snapshot, "commands_allowlist": ["git"]}, None)
    assert (legacy.commands_allowlist, legacy.commands_denylist) == (["git"], None)
    # Copied verbatim: a malformed frozen value is refused by the policy
    # builders (fail closed), never silently dropped here.
    assert _frozen_config_from_snapshot({**snapshot, "command_timeout_seconds": "x"}, None).command_timeout_seconds == "x"


# ---------------------------------------------------------------------------
# R4 agent-config, run policy, config-row overrides and the snapshot validator
# ---------------------------------------------------------------------------


def test_agent_config_and_run_policy_serve_the_frozen_fields(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    harnessed = _joined_arm(client, session_factory, current_user, seeded, label="served", **HARNESS)
    plain = _joined_arm(client, session_factory, current_user, seeded, label="unserved")

    # The template changes after the freeze; the frozen values win.
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE public.agent_profile SET commands_denylist_json = '[\"ls\"]', "
                "command_timeout_seconds = 5, harness_options_json = NULL WHERE profile_id = :id"
            ),
            {"id": harnessed.profile["profile_id"]},
        )
        session.commit()

    for version in (None, MANAGED_PROTOCOL_VERSION):
        served = _agent_config(session_factory, harnessed.participant, version)
        assert served["agent_profile"] == "arm-served"
        assert served["commands_denylist"] == DENYLIST
        # Runtimes 0.0.6 and earlier keep the fallback allowlist they had.
        assert served["commands_allowlist"] == FALLBACK_COMMANDS_ALLOWLIST
        assert served["command_timeout_seconds"] == 300
        assert served["harness_options"] == OPTIONS
        unserved = _agent_config(session_factory, plain.participant, version)
        assert unserved["agent_profile"] == "arm-unserved"
        assert unserved["commands_denylist"] == []
        assert unserved["commands_allowlist"] == FALLBACK_COMMANDS_ALLOWLIST
        # Absent, not null: the config is exactly what it was before D-01.
        assert "command_timeout_seconds" not in unserved
        assert "harness_options" not in unserved

    with session_factory() as session:
        resolution = resolve_assignment_context(session, harnessed.participant)
        assert isinstance(resolution.profile, FrozenAgentConfig)
        assert resolution.profile.commands_denylist == DENYLIST
        policy = _managed_policy(
            session, harnessed.participant, resolution.profile, study_id=resolution.study_id
        )
        plain_resolution = resolve_assignment_context(session, plain.participant)
        plain_policy = _managed_policy(
            session, plain.participant, plain_resolution.profile, study_id=plain_resolution.study_id
        )
    assert policy["commands_denylist"] == DENYLIST
    assert policy["commands_allowlist"] == FALLBACK_COMMANDS_ALLOWLIST
    assert policy["command_timeout_seconds"] == 300
    assert policy["harness_options"] == OPTIONS
    assert _valid_managed_policy_snapshot(policy) is True
    assert plain_policy["commands_denylist"] == []
    assert plain_policy["commands_allowlist"] == FALLBACK_COMMANDS_ALLOWLIST
    assert "command_timeout_seconds" not in plain_policy
    assert "harness_options" not in plain_policy
    assert _valid_managed_policy_snapshot(plain_policy) is True


def test_config_row_only_adds_blocked_commands(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    extended = _joined_arm(
        client,
        session_factory,
        current_user,
        seeded,
        label="extended",
        commands_denylist=["curl", "git"],
        harness_options={"verify_command": ["make", "check"]},
    )
    unset = _joined_arm(client, session_factory, current_user, seeded, label="unset-row")
    _set_config_agent(session_factory, extended.participant, commands_denylist=["rm", "GIT", "make"])
    _set_config_agent(session_factory, unset.participant, commands_denylist=[" rm "])

    for version in (None, MANAGED_PROTOCOL_VERSION):
        # Profile order first, then the row's other names; "GIT" is a repeat.
        served = _agent_config(session_factory, extended.participant, version)
        assert served["commands_denylist"] == ["curl", "git", "rm", "make"]
        assert _agent_config(session_factory, unset.participant, version)[
            "commands_denylist"
        ] == ["rm"]

    with session_factory() as session:
        resolution = resolve_assignment_context(session, extended.participant)
        policy = _managed_policy(
            session, extended.participant, resolution.profile, study_id=resolution.study_id
        )
    assert policy["commands_denylist"] == ["curl", "git", "rm", "make"]
    # The frozen verify command travels unchanged even though the row now
    # blocks its program (the runtime then skips it); the policy stays valid.
    assert policy["harness_options"] == {"verify_command": ["make", "check"]}
    assert _valid_managed_policy_snapshot(policy) is True


def test_legacy_allowlist_still_reaches_old_runtimes_unchanged(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    narrowed = _joined_arm(client, session_factory, current_user, seeded, label="legacy-narrowed")
    replaced = _joined_arm(client, session_factory, current_user, seeded, label="legacy-replaced")
    _freeze_legacy_allowlist(session_factory, narrowed, ["pytest", "git", "rg"])
    _set_config_agent(session_factory, narrowed.participant, commands_allowlist=["rg", "pytest", "ls"])
    _set_config_agent(session_factory, replaced.participant, commands_allowlist=["rg", "ls"])

    for version in (None, MANAGED_PROTOCOL_VERSION):
        # Intersection in the snapshot's order; "ls" is never added.
        served = _agent_config(session_factory, narrowed.participant, version)
        assert served["commands_allowlist"] == ["pytest", "rg"]
        assert served["commands_denylist"] == []
        # Without a snapshot allowlist the config row still replaces the fallback.
        assert _agent_config(session_factory, replaced.participant, version)[
            "commands_allowlist"
        ] == ["rg", "ls"]

    with session_factory() as session:
        resolution = resolve_assignment_context(session, narrowed.participant)
        assert resolution.profile.commands_allowlist == ["pytest", "git", "rg"]
        policy = _managed_policy(
            session, narrowed.participant, resolution.profile, study_id=resolution.study_id
        )
    assert policy["commands_allowlist"] == ["pytest", "rg"]
    assert policy["commands_denylist"] == []
    assert _valid_managed_policy_snapshot(policy) is True


def _stand_in_arm(**fields) -> SimpleNamespace:
    return SimpleNamespace(
        framework_version="code4me2-agent",
        name="managed-arm",
        model="managed-model",
        tools_json='["run_command"]',
        approval_policy="per_step",
        max_steps=3,
        max_context_tokens=1000,
        temperature=0.2,
        **fields,
    )


def _policy_for(profile, *, config_data=None) -> dict:
    user = None if config_data is None else SimpleNamespace(config_id=uuid.uuid4())
    config = None if config_data is None else SimpleNamespace(config_data=config_data)
    with patch("backend.routers.acp.crud.get_user_by_id", return_value=user), patch(
        "backend.routers.acp.crud.get_config_by_id", return_value=config
    ), patch("backend.routers.acp.resolve_store_agent_content_for_acp", return_value=False):
        return _managed_policy(MagicMock(), uuid.uuid4(), profile)


def test_run_policy_shape_with_and_without_the_fields():
    base_policy = {
        "version": MANAGED_PROTOCOL_VERSION,
        "transport": "managed_backend",
        "agent_profile": "managed-arm",
        "framework_version": "code4me2-agent",
        "model": "managed-model",
        "tools": ["run_command"],
        "approval_policy": "per_step",
        "temperature": 0.2,
        "max_iterations": 3,
        "max_context_tokens": 1000,
        "commands_allowlist": FALLBACK_COMMANDS_ALLOWLIST,
        # Always present: current runtimes refuse a policy without it.
        "commands_denylist": [],
        "store_agent_content": False,
    }
    for profile in (
        _stand_in_arm(),
        _stand_in_arm(commands_denylist=None, command_timeout_seconds=None, harness_options=None),
    ):
        policy = _policy_for(profile)
        assert json.dumps(policy) == json.dumps(base_policy)

    harnessed = _policy_for(_stand_in_arm(**HARNESS))
    assert harnessed == {
        **base_policy,
        "commands_denylist": DENYLIST,
        "command_timeout_seconds": 300,
        "harness_options": OPTIONS,
    }
    assert list(harnessed)[-2:] == ["command_timeout_seconds", "harness_options"]

    # A config row adds blocked commands (stripped, case-insensitive repeats dropped).
    extended = _policy_for(
        _stand_in_arm(commands_denylist=["git"]),
        config_data='{"agent":{"commands_denylist":[" rm ", "GIT"]}}',
    )
    assert extended["commands_denylist"] == ["git", "rm"]

    # Old runtimes never get a blocked command in their allowlist.
    assert _policy_for(_stand_in_arm(commands_denylist=["CAT", "rg.exe"]))[
        "commands_allowlist"
    ] == ["pwd", "ls", "grep"]
    assert _policy_for(
        _stand_in_arm(commands_denylist=["git"]),
        config_data='{"agent":{"commands_allowlist":["git", "rg"],"commands_denylist":["rm"]}}',
    )["commands_allowlist"] == ["rg"]

    # Legacy allowlist computation for runtimes 0.0.6 and earlier: the row
    # replaces the fallback, or narrows a legacy snapshot allowlist.
    replaced = _policy_for(
        _stand_in_arm(), config_data='{"agent":{"commands_allowlist":["git", " rg "]}}'
    )
    assert replaced["commands_allowlist"] == ["git", "rg"]
    narrowed = _policy_for(
        _stand_in_arm(commands_allowlist=["rg", "pytest", "git"]),
        config_data='{"agent":{"commands_allowlist":["git", "rg", "ls"]}}',
    )
    assert narrowed["commands_allowlist"] == ["rg", "git"]
    emptied = _policy_for(
        _stand_in_arm(commands_allowlist=["pytest"]),
        config_data='{"agent":{"commands_allowlist":["git"]}}',
    )
    assert emptied["commands_allowlist"] == []


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({"commands_denylist": ["./git"]}, "Assigned command policy is invalid"),
        ({"commands_denylist": "git"}, "Assigned command policy is invalid"),
        ({"commands_allowlist": ["./gradlew"]}, "Assigned command policy is invalid"),
        ({"commands_allowlist": "git"}, "Assigned command policy is invalid"),
        ({"command_timeout_seconds": 0}, "Assigned profile command timeout is invalid"),
        ({"command_timeout_seconds": True}, "Assigned profile command timeout is invalid"),
        ({"harness_options": {"bogus": True}}, "Assigned profile harness options are invalid"),
        ({"harness_options": ["self_review"]}, "Assigned profile harness options are invalid"),
    ],
)
def test_run_policy_fails_closed_on_malformed_frozen_values(fields, detail):
    with pytest.raises(HTTPException) as error:
        _policy_for(_stand_in_arm(**fields))
    assert error.value.status_code == 503
    assert error.value.detail == detail


def test_a_frozen_verify_command_the_denylist_blocks_is_served_not_refused():
    """Saving and freezing refuse it; when serving, the runtime just skips it.

    A later change to the matching rule must not take a running arm down.
    """
    frozen = {"commands_denylist": ["pytest"], "harness_options": {"verify_command": ["uv", "run", "pytest"]}}
    policy = _policy_for(_stand_in_arm(**frozen))
    assert policy["commands_denylist"] == ["pytest"]
    assert policy["harness_options"] == {"verify_command": ["uv", "run", "pytest"]}
    assert _valid_managed_policy_snapshot(policy) is True
    with pytest.raises(ProfileConfigurationError) as refused:
        validate_profile_configuration(
            _stand_in_profile(framework_version="code4me2-agent", **frozen),
            _stand_in_release(byoa=False),
        )
    assert refused.value.code == "HARNESS_OPTIONS_INVALID"


@pytest.mark.parametrize(
    "config_data",
    [
        '{"agent":{"commands_denylist":"git"}}',
        '{"agent":{"commands_denylist":[" "]}}',
        '{"agent":{"commands_denylist":[3]}}',
        '{"agent":{"commands_denylist":["git push"]}}',
        '{"agent":{"commands_denylist":["./git"]}}',
        '{"agent":{"commands_allowlist":[""]}}',
    ],
)
def test_run_policy_fails_closed_on_a_malformed_config_row(config_data):
    with pytest.raises(HTTPException) as error:
        _policy_for(_stand_in_arm(), config_data=config_data)
    assert error.value.status_code == 503
    assert error.value.detail == "Assigned command policy is invalid"


def test_agent_config_fails_closed_for_managed_and_ignores_for_legacy_callers(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    arm = _joined_arm(
        client, session_factory, current_user, seeded, label="corrupt", command_timeout_seconds=30
    )
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE public.study_assignment SET profile_snapshot_json = "
                "profile_snapshot_json || CAST(:patch AS jsonb) WHERE assignment_id = :id"
            ),
            {"patch": json.dumps({"command_timeout_seconds": "30"}), "id": arm.assignment_id},
        )
        session.commit()

    with pytest.raises(HTTPException) as refused:
        _agent_config(session_factory, arm.participant, MANAGED_PROTOCOL_VERSION)
    assert refused.value.status_code == 503

    legacy = _agent_config(session_factory, arm.participant, None)
    assert legacy["agent_profile"] == "arm-corrupt"
    assert "command_timeout_seconds" not in legacy
    assert legacy["commands_allowlist"] == FALLBACK_COMMANDS_ALLOWLIST
    assert legacy["commands_denylist"] == []


def _patch_snapshot(session_factory, arm, patch_fields) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE public.study_assignment SET profile_snapshot_json = "
                "profile_snapshot_json || CAST(:patch AS jsonb) WHERE assignment_id = :id"
            ),
            {"patch": json.dumps(patch_fields), "id": arm.assignment_id},
        )
        session.commit()


def test_legacy_callers_keep_the_blocked_commands_or_lose_run_command(http_runtime):
    """Ignoring a malformed setting must never unblock a command (review 2026-10-10)."""
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    arm = _joined_arm(
        client,
        session_factory,
        current_user,
        seeded,
        label="blocked-corrupt",
        commands_denylist=["git"],
        command_timeout_seconds=30,
    )

    # Another frozen setting is malformed: the valid denylist is still served.
    _patch_snapshot(session_factory, arm, {"command_timeout_seconds": "30"})
    served = _agent_config(session_factory, arm.participant, None)
    assert served["commands_denylist"] == ["git"]
    assert "run_command" in served["tools"]

    # The denylist itself is malformed: no command may run at all.
    _patch_snapshot(session_factory, arm, {"commands_denylist": "git"})
    withheld = _agent_config(session_factory, arm.participant, None)
    assert "run_command" not in withheld["tools"]
    with pytest.raises(HTTPException) as refused:
        _agent_config(session_factory, arm.participant, MANAGED_PROTOCOL_VERSION)
    assert refused.value.status_code == 503


def test_a_failed_resolution_withholds_run_command_from_legacy_callers(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    arm = _joined_arm(
        client, session_factory, current_user, seeded, label="unresolved", commands_denylist=["git"]
    )
    assert "run_command" in _agent_config(session_factory, arm.participant, None)["tools"]

    with patch("backend.routers.acp.crud.get_user_by_id", side_effect=RuntimeError("db down")):
        legacy = _agent_config(session_factory, arm.participant, None)
        with pytest.raises(HTTPException) as refused:
            _agent_config(session_factory, arm.participant, MANAGED_PROTOCOL_VERSION)
    # The blocked commands were never read: no command may run.
    assert "run_command" not in legacy["tools"]
    assert legacy["commands_denylist"] == []
    assert refused.value.status_code == 503


def test_a_malformed_config_row_denylist_fails_closed_everywhere(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        seeded = _seed_authoring(session)
    arm = _joined_arm(client, session_factory, current_user, seeded, label="row-corrupt")
    _set_config_agent(session_factory, arm.participant, commands_denylist="rm")

    with pytest.raises(HTTPException) as refused:
        _agent_config(session_factory, arm.participant, MANAGED_PROTOCOL_VERSION)
    assert refused.value.status_code == 503
    legacy = _agent_config(session_factory, arm.participant, None)
    assert "run_command" not in legacy["tools"]
    with session_factory() as session:
        resolution = resolve_assignment_context(session, arm.participant)
        with pytest.raises(HTTPException) as policy_refused:
            _managed_policy(
                session, arm.participant, resolution.profile, study_id=resolution.study_id
            )
    assert policy_refused.value.status_code == 503


def test_agent_config_response_omits_unset_fields():
    plain = json.loads(json.dumps(AcpAgentConfigGetResponse(model="m").model_dump(mode="json")))
    assert "command_timeout_seconds" not in plain
    assert "harness_options" not in plain
    assert "system_prompt" in plain  # unchanged: still serialized as null
    assert plain["commands_denylist"] == []  # always sent
    served = AcpAgentConfigGetResponse(
        model="m",
        command_timeout_seconds=30,
        harness_options={"loop_guard": False},
        commands_denylist=["git"],
    ).model_dump(mode="json")
    assert served["command_timeout_seconds"] == 30
    assert served["harness_options"] == {"loop_guard": False}
    assert served["commands_denylist"] == ["git"]


def _snapshot(**extra) -> dict:
    return {
        "version": MANAGED_PROTOCOL_VERSION,
        "model": "gpt-test",
        "approval_policy": "per_step",
        "max_iterations": 8,
        "max_context_tokens": 32000,
        "temperature": None,
        "tools": ["run_command"],
        "commands_allowlist": ["git"],
        **extra,
    }


def test_policy_snapshot_validator_accepts_optional_keys_and_rejects_wrong_types():
    # Policies stored before the denylist have no commands_denylist key.
    assert _valid_managed_policy_snapshot(_snapshot()) is True
    assert _valid_managed_policy_snapshot(_snapshot(commands_denylist=[])) is True
    assert _valid_managed_policy_snapshot(_snapshot(commands_denylist=["git", "rm"])) is True
    assert _valid_managed_policy_snapshot(
        _snapshot(command_timeout_seconds=None, harness_options=None)
    ) is True
    assert _valid_managed_policy_snapshot(
        _snapshot(command_timeout_seconds=600, harness_options=OPTIONS)
    ) is True
    # The effective denylist may be longer than the profile's: the verify
    # command is not re-checked against it.
    assert _valid_managed_policy_snapshot(
        _snapshot(commands_denylist=["pytest"], harness_options={"verify_command": ["pytest"]})
    ) is True
    for bad in (
        {"commands_denylist": "git"},
        {"commands_denylist": [3]},
        {"commands_denylist": [""]},
        {"commands_denylist": [" "]},
        {"command_timeout_seconds": "30"},
        {"command_timeout_seconds": True},
        {"command_timeout_seconds": 0},
        {"command_timeout_seconds": 601},
        {"command_timeout_seconds": 1.5},
        {"harness_options": ["self_review"]},
        {"harness_options": "{}"},
        {"harness_options": {"bogus": True}},
        {"harness_options": {"self_review": "yes"}},
        {"harness_options": {"verify_command": []}},
        {"harness_options": {"verify_command": ["/bin/sh", "-c", "x"]}},
    ):
        assert _valid_managed_policy_snapshot(_snapshot(**bad)) is False, bad


def test_catalogue_constants_match_decision_d01():
    assert tool_catalogue.HARNESS_PROFILE_FIELDS == FIELDS
    assert set(tool_catalogue.HARNESS_OPTION_KEYS) == {
        "self_review",
        "verify_on_stop",
        "verify_command",
        "context_summarization",
        "parallel_tools",
        "prompt_profile",
        "project_instructions",
        "read_before_edit",
        "syntax_check",
        "loop_guard",
        "instruction_reminders",
        "test_output_summary",
        # Run 2026-10-03-pilot-feedback C1: offer "Revise…" on approvals.
        "approval_revise",
    }
    assert tool_catalogue.HARNESS_PROMPT_PROFILES == (
        "auto",
        "default",
        "openai",
        "anthropic",
        "gemini",
    )
    assert tool_catalogue.COMMAND_NAME_PATTERN.pattern == r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
    assert re.fullmatch(tool_catalogue.COMMAND_NAME_PATTERN, "x" * 64)
