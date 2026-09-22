"""Typed profile-release validation: registry gate + executable contract."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from database import crud
from research.study.agents.distributions import ProfileConfigurationError

from ._byoa_contract import BYOA_CONFIG_BINDINGS


def _release_row(
    *,
    mode: str = "PACKAGED",
    agent_id: str = "codex-acp",
    with_adapter: bool = True,
) -> SimpleNamespace:
    """An ``AgentRelease`` row stand-in with a realistic ``release_json``."""
    artifact_digest = "sha256:" + "a" * 64
    adapter_digest = "sha256:" + "d" * 64
    document = {
        "schema_version": "1",
        "agent_id": agent_id,
        "release_id": "rel-1",
        "version": "1.2.3",
        "source_manifest_digest": "sha256:" + "1" * 64,
        "distribution_mode": mode,
        "artifacts": (
            []
            if mode == "BYOA_EXTERNAL"
            else [
                {
                    "os": "macos",
                    "arch": "aarch64",
                    "path": "artifact.bin",
                    "sha256": artifact_digest,
                    "size": 1,
                }
            ]
        ),
        "agent_package": "codex" if mode == "BYOA_EXTERNAL" else None,
        "agent_command": None,
        "agent_command_args": [],
        "adapter": (
            {"adapter_id": "adapter", "version": "1.0.0", "digest": adapter_digest}
            if with_adapter
            else None
        ),
    }
    if mode == "BYOA_EXTERNAL":
        document["byoa_config"] = list(BYOA_CONFIG_BINDINGS)
    return SimpleNamespace(
        status="QUALIFIED",
        release_json=dict(document, tests=[{ "os": "macos", "arch": "arm64", "self_check": "PASS", "acp_initialize": "PASS", "ran_at": "2026-09-21T00:00:00Z"}]),
    )


@pytest.mark.parametrize(
    ("release", "code"),
    [
        (None, "RELEASE_UNRESOLVED"),
        (SimpleNamespace(status="UNQUALIFIED"), "RELEASE_NOT_QUALIFIED"),
        (SimpleNamespace(status="RETIRED"), "RELEASE_WITHDRAWN"),
        (SimpleNamespace(status="BLOCKED"), "RELEASE_WITHDRAWN"),
    ],
)
def test_profile_release_validation_rejects_unavailable_releases(release, code):
    session = MagicMock()
    session.get.return_value = release

    with pytest.raises(crud.ProfileReleaseError) as error:
        crud.validate_profile_release(session, "release-1")

    assert error.value.code == code
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_profile_release_validation_accepts_qualified_release():
    session = MagicMock()
    session.get.return_value = SimpleNamespace(status="QUALIFIED")

    crud.validate_profile_release(session, "release-1")

    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_profile_release_validation_allows_nullable_release_for_crud_compatibility():
    session = MagicMock()

    crud.validate_profile_release(session, None)

    session.get.assert_not_called()
    session.commit.assert_not_called()


def test_create_profile_rejects_a_framework_release_mode_mismatch():
    session = MagicMock()
    session.get.return_value = _release_row(mode="BYOA_EXTERNAL", agent_id="codex")

    with pytest.raises(ProfileConfigurationError) as error:
        crud.create_agent_profile(
            session,
            owner_user_id=uuid.uuid4(),
            name="mismatched",
            model="model",
            tools_json="[]",
            approval_policy="auto",
            max_steps=1,
            framework_version="code4me2-agent",
            release_id="rel-1",
        )

    assert error.value.code == "FRAMEWORK_DISTRIBUTION_MISMATCH"
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_create_profile_accepts_a_qualified_packaged_release():
    session = MagicMock()
    session.get.return_value = _release_row(mode="PACKAGED")

    profile = crud.create_agent_profile(
        session,
        owner_user_id=uuid.uuid4(),
        name="managed-arm",
        model="model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        framework_version="code4me2-agent",
        release_id="rel-1",
    )

    assert profile.release_id == "rel-1"
    assert profile.framework_version == "code4me2-agent"
    assert profile.configuration_digest
    session.add.assert_called_once()
    session.commit.assert_called_once()


def test_create_profile_accepts_a_qualified_byoa_release():
    session = MagicMock()
    session.get.return_value = _release_row(mode="BYOA_EXTERNAL", agent_id="codex")

    profile = crud.create_agent_profile(
        session,
        owner_user_id=uuid.uuid4(),
        name="byoa-arm",
        model="model",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        framework_version="codex",
        release_id="rel-1",
    )

    assert profile.framework_version == "codex"
    assert profile.release_id == "rel-1"


def test_update_profile_validates_the_merged_framework_and_release():
    """Changing only the framework cannot leave an unexecutable pairing."""
    session = MagicMock()
    session.query.return_value.join.return_value.filter.return_value.first.return_value = None
    existing = SimpleNamespace(
        profile_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        name="existing",
        model="model",
        framework_version="codex",
        release_id="rel-1",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        configuration_digest="",
    )
    session.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = existing
    session.get.return_value = _release_row(mode="BYOA_EXTERNAL", agent_id="codex")

    with pytest.raises(ProfileConfigurationError) as error:
        crud.update_agent_profile(
            session,
            existing.profile_id,
            framework_version="code4me2-agent",
        )

    assert error.value.code == "FRAMEWORK_DISTRIBUTION_MISMATCH"
    session.commit.assert_not_called()


def test_create_profile_rejects_an_unmapped_byoa_field_at_crud_time():
    """ISSUE-03 Path A must fail at profile CRUD, not only at study freeze."""
    session = MagicMock()
    row = _release_row(mode="BYOA_EXTERNAL", agent_id="codex")
    document = json.loads(json.dumps(row.release_json))
    document["byoa_config"] = [
        binding for binding in document["byoa_config"] if binding["field"] != "model"
    ]
    row.release_json = document
    session.get.return_value = row

    with pytest.raises(ProfileConfigurationError) as error:
        crud.create_agent_profile(
            session,
            owner_user_id=uuid.uuid4(),
            name="unmapped",
            model="model",
            tools_json="[]",
            approval_policy="auto",
            max_steps=1,
            framework_version="codex",
            release_id="rel-1",
        )

    assert error.value.code == "BYOA_CONFIG_UNMAPPED"
    assert "model" in str(error.value)
    session.add.assert_not_called()
    session.commit.assert_not_called()
