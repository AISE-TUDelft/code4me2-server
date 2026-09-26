"""Release catalogue: which runtimes may pin a release and what governs it.

Each ``/api/research/agents/release-catalogue`` entry carries
``compatible_frameworks``, ``configurable_fields`` and
``required_bindings_missing``, derived with the same rules as the executable
profile contract, exposing field names only (never binding keys, env var names
or commands).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from database.research_schemas import AgentRelease
from research.study.agents import store
from research.study.agents.distributions import (
    ProfileConfigurationError,
    release_profile_configurability,
    validate_profile_configuration,
)

from ._byoa_contract import BYOA_CONFIG_BINDINGS
from ._ui_overhaul_seed import (
    researcher_user,
    seed_account,
    seed_byoa_release,
    seed_packaged_release,
)
from .test_research_api_contract import http_runtime  # noqa: F401 - fixture

CATALOGUE_PATH = "/api/research/agents/release-catalogue"
MANAGED_FIELDS = [
    "model",
    "temperature",
    "max_steps",
    "tools",
    "approval_policy",
    "max_context_tokens",
    "system_prompt",
]
PARTIAL_GOOSE_BINDINGS = [
    {"field": "model", "transport": "env", "key": "GOOSE_SECRET_MODEL_VAR"},
    {"field": "temperature", "transport": "arg", "key": "--goose-temperature-flag"},
]


def _seed_catalogue(session) -> SimpleNamespace:
    return SimpleNamespace(
        packaged=seed_packaged_release(session, release_id="cat-packaged"),
        goose=seed_byoa_release(
            session,
            release_id="cat-goose",
            agent_id="goose",
            agent_package="goose",
            agent_command="goose",
            bindings=PARTIAL_GOOSE_BINDINGS,
        ),
        codex=seed_byoa_release(
            session,
            release_id="cat-codex",
            agent_id="codex",
            agent_package="codex",
            agent_command="/opt/bin/codex-acp-launcher",
        ),
        ambiguous=seed_byoa_release(
            session,
            release_id="cat-ambiguous",
            agent_id="catalogue-agent",
            agent_package="catalogue-agent",
            agent_command="catalogue-agent",
            bindings=[],
        ),
    )


def test_catalogue_entries_expose_per_release_configurability(http_runtime):
    client, session_factory, current_user = http_runtime
    with session_factory() as session:
        researcher = seed_account(session, "catalogue-ui@example.com", can_research=True)
        seeded = _seed_catalogue(session)
    current_user["value"] = researcher_user(researcher)

    response = client.get(CATALOGUE_PATH)
    assert response.status_code == 200, response.text
    entries = {entry["release_id"]: entry for entry in response.json()["releases"]}

    def configurability(release_id):
        entry = entries[release_id]
        return (
            entry["compatible_frameworks"],
            entry["configurable_fields"],
            entry["required_bindings_missing"],
        )

    assert configurability(seeded.packaged) == (["code4me2-agent"], MANAGED_FIELDS, [])
    # The seeded Goose release carries the gateway runtime bindings (the seed
    # helper adds them), so only the two unbound profile fields are missing.
    assert configurability(seeded.goose) == (
        ["goose"],
        ["model", "temperature"],
        ["max_steps", "approval_policy"],
    )
    # The package names the framework; the command path is not needed for it.
    assert configurability(seeded.codex) == (
        ["codex"],
        ["model", "temperature", "max_steps", "tools", "approval_policy"],
        [],
    )
    # An identity naming no known framework is never guessed.
    assert configurability(seeded.ambiguous) == (
        ["goose", "codex"],
        [],
        ["model", "max_steps", "approval_policy"],
    )

    # Field names only: no binding key, env var name or command leaks.
    for secret in (
        "GOOSE_SECRET_MODEL_VAR",
        "--goose-temperature-flag",
        "BYOA_AGENT_",
        "codex-acp-launcher",
        "agent_command",
    ):
        assert secret not in response.text


def test_identity_naming_both_frameworks_returns_both():
    release = SimpleNamespace(
        distribution_mode="BYOA_EXTERNAL",
        agent_id="codex",
        agent_package="goose",
        agent_command="goose",
        byoa_config=[],
    )
    assert release_profile_configurability(release)["compatible_frameworks"] == [
        "goose",
        "codex",
    ]
    windows = SimpleNamespace(
        distribution_mode="BYOA_EXTERNAL",
        agent_id="external",
        agent_package=None,
        agent_command="C:\\Tools\\Codex.EXE",
        byoa_config=[],
    )
    assert release_profile_configurability(windows)["compatible_frameworks"] == ["codex"]


@pytest.mark.parametrize(
    "bindings",
    [
        list(BYOA_CONFIG_BINDINGS),
        [binding for binding in BYOA_CONFIG_BINDINGS if binding["field"] != "temperature"],
        [binding for binding in BYOA_CONFIG_BINDINGS if binding["field"] != "max_steps"],
        [],
    ],
)
def test_catalogue_matches_the_executable_profile_contract(http_runtime, bindings):
    """A profile using only the advertised fields is valid iff nothing is missing."""
    _client, session_factory, _current_user = http_runtime
    with session_factory() as session:
        release_id = seed_byoa_release(
            session,
            agent_id="codex",
            agent_package="codex",
            agent_command="codex",
            bindings=bindings,
        )
        row = session.get(AgentRelease, release_id)
        release = store.row_to_release(row)
        release_json = row.release_json
    view = release_profile_configurability(release, release_json=release_json)
    (framework,) = view["compatible_frameworks"]
    profile = SimpleNamespace(
        name="contract",
        release_id=release_id,
        framework_version=framework,
        model="model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=0.5 if "temperature" in view["configurable_fields"] else None,
        max_context_tokens=None,
    )
    if view["required_bindings_missing"]:
        with pytest.raises(ProfileConfigurationError) as error:
            validate_profile_configuration(profile, release, release_json=release_json)
        profile_fields = [
            field for field in view["required_bindings_missing"]
            if field in {"model", "temperature", "max_steps", "tools", "approval_policy"}
        ]
        if profile_fields:
            # Unmapped profile fields are reported first.
            assert error.value.code == "BYOA_CONFIG_UNMAPPED"
            for field in profile_fields:
                assert field in str(error.value)
        else:
            # Only the gateway runtime bindings are missing (Goose).
            assert error.value.code == "INFERENCE_GATEWAY_UNBOUND"
    else:
        validate_profile_configuration(profile, release, release_json=release_json)
