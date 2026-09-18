"""Focused tests for the distribution (AgentProfile) resolution seam.

These exercise the derived, read-only view the UI consumes and the
create/update validation of a distribution pin, without a database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.routers.agent.profiles import AgentProfilePayload
from backend.routers.research.agents import _distribution_payload
from research.study.agents.distributions import (
    distribution_supported_platforms,
    parse_command_args,
    resolve_distribution_view,
)
from research.study.agents.enums import (
    DistributionMode,
    QualificationStatus,
)
from research.study.agents.models import (
    AdapterRef,
    AgentReleaseV1,
    DistributionArtifact,
)
from research.study.protocol.enums import ReleaseResolutionStatus


def _profile(**overrides):
    base = dict(
        profile_id=uuid.uuid4(),
        name="dist",
        distribution_mode="PACKAGED",
        release_id="rel-1",
        agent_package=None,
        agent_command=None,
        agent_command_args=None,
        framework_version="code4me2-agent",
        model="gpt",
        base_url="https://example.invalid/v1",
        api_key_ref="UPSTREAM_API_KEY",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _release(*, qualified: bool, byoa: bool = False) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="codex-acp" if not byoa else "goose",
        release_id="rel-1",
        version="1.2.3",
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
                    arch="aarch64",
                    path="artifact.bin",
                    sha256="sha256:" + "a" * 64,
                    size=1,
                ),
                DistributionArtifact(
                    os="linux",
                    arch="x64",
                    path="artifact-linux.bin",
                    sha256="sha256:" + "b" * 64,
                    size=1,
                ),
            ]
        ),
        agent_command="goose" if byoa else None,
        agent_command_args=["acp"] if byoa else [],
        agent_package="goose" if byoa else None,
        adapter=AdapterRef(
            adapter_id="adapter", version="1.0.0", digest="sha256:" + "d" * 64
        ),
        qualification_status=(
            QualificationStatus.QUALIFIED if qualified else QualificationStatus.UNQUALIFIED
        ),
    )


def test_packaged_qualified_distribution_is_verified_with_a_digest():
    release = _release(qualified=True)
    view = resolve_distribution_view(_profile(), release, platform=("macos", "aarch64"))

    assert view.verified is True
    assert view.release_status == ReleaseResolutionStatus.RESOLVED
    assert view.release_id == "rel-1"
    assert view.version == "1.2.3"
    assert view.artifact_digest == "sha256:" + "a" * 64
    assert distribution_supported_platforms(release) == [
        {"os": "linux", "arch": "x64"},
        {"os": "macos", "arch": "aarch64"},
    ]


def test_packaged_unqualified_distribution_is_unverified():
    view = resolve_distribution_view(_profile(), _release(qualified=False))

    assert view.verified is False
    assert view.release_status == ReleaseResolutionStatus.UNQUALIFIED


def test_packaged_distribution_without_release_id_is_unverified_and_unresolved():
    view = resolve_distribution_view(_profile(release_id=None), None)

    assert view.verified is False
    assert view.release_id is None
    assert view.artifact_digest is None


def test_byoa_distribution_is_always_unverified_and_has_no_digest():
    release = _release(qualified=True, byoa=True)
    view = resolve_distribution_view(
        _profile(
            distribution_mode="BYOA_EXTERNAL",
            release_id=None,
            agent_package="goose",
            agent_command="goose",
            agent_command_args='["acp"]',
        ),
        release,
    )

    assert view.distribution_mode == "BYOA_EXTERNAL"
    assert view.artifact_digest is None
    assert view.verified is False
    assert view.agent_package == "goose"
    assert view.agent_command == "goose"
    assert view.agent_command_args == ["acp"]


def test_parse_command_args_accepts_json_text_and_lists():
    assert parse_command_args('["a", "b"]') == ["a", "b"]
    assert parse_command_args(["a"]) == ["a"]
    assert parse_command_args(None) == []
    assert parse_command_args("not-json") == []


def test_distribution_payload_exposes_derived_view_without_secrets():
    profile = _profile()
    release = _release(qualified=True)
    row = SimpleNamespace(release_json=release.model_dump(mode="json"))
    db = SimpleNamespace()

    with patch(
        "backend.routers.research.agents.store.get_release", return_value=row
    ), patch(
        "backend.routers.research.agents.store.row_to_release", return_value=release
    ):
        payload = _distribution_payload(db, profile)

    assert payload["distribution_id"] == str(profile.profile_id)
    assert payload["release_id"] == "rel-1"
    assert payload["release_version"] == "1.2.3"
    assert payload["verified"] is True
    assert payload["supported_platforms"] == [
        {"os": "linux", "arch": "x64"},
        {"os": "macos", "arch": "aarch64"},
    ]
    # Only non-secret identity is exposed; no endpoint or credential reference.
    assert payload["provider"]["connection_id"] is None
    assert "api_key_ref" not in payload["provider"]
    assert "base_url" not in payload["provider"]
    assert "api_key" not in payload
    assert "sk-" not in str(payload)


def test_profile_payload_packaged_requires_a_release_id():
    with pytest.raises(PydanticValidationError):
        AgentProfilePayload(
            name="n",
            model="m",
            approval_policy="auto",
            max_steps=1,
            distribution_mode="PACKAGED",
        )


def test_profile_payload_byoa_requires_an_identity():
    with pytest.raises(PydanticValidationError):
        AgentProfilePayload(
            name="n",
            model="m",
            approval_policy="auto",
            max_steps=1,
            distribution_mode="BYOA_EXTERNAL",
        )


def test_profile_payload_rejects_an_absolute_agent_command():
    with pytest.raises(PydanticValidationError):
        AgentProfilePayload(
            name="n",
            model="m",
            approval_policy="auto",
            max_steps=1,
            distribution_mode="BYOA_EXTERNAL",
            agent_package="goose",
            agent_command="/usr/local/bin/goose",
        )
