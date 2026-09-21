"""Focused tests for the distribution (AgentProfile) resolution seam.

These exercise the derived, read-only view the UI consumes and the shared
profile↔release executable contract, without a database.

Profiles are schema-shaped: a real ``agent_profile`` row owns only the release
pin plus its executable config. Distribution mode and identity are derived from
the release (ISSUE-17) and the removed profile attributes are never consulted.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from backend.routers.agent.profiles import AgentProfilePayload
from backend.routers.research.agents import _distribution_payload
from research.canonical import canonical_hash
from research.participants.enums import EnrollmentStatus
from research.runtime.bootstrap import (
    BootstrapOutcome,
    BootstrapSigningContext,
    EphemeralSessionFactory,
    compose_bootstrap,
)
from research.study.agents.distributions import (
    FRAMEWORK_DISTRIBUTION_MODES,
    ProfileConfigurationError,
    distribution_supported_platforms,
    parse_command_args,
    resolve_distribution_view,
    validate_profile_configuration,
)
from research.study.agents.enums import (
    DistributionMode,
    QualificationStatus,
)
from research.study.agents.models import (
    AdapterRef,
    AgentConfigBinding,
    AgentReleaseV1,
    DistributionArtifact,
)
from research.study.protocol.enums import ReleaseResolutionStatus

from ._byoa_contract import BYOA_CONFIG_BINDINGS


def _profile(**overrides):
    """A schema-shaped ``AgentProfile`` stand-in (release pin + config only)."""
    base = dict(
        profile_id=uuid.uuid4(),
        name="dist",
        release_id="rel-1",
        framework_version="code4me2-agent",
        model="gpt",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        connection_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _release(
    *, qualified: bool, byoa: bool = False, with_adapter: bool = True
) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="codex" if byoa else "codex-acp",
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
        byoa_config=(
            [AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS]
            if byoa
            else []
        ),
        adapter=(
            AdapterRef(
                adapter_id="adapter", version="1.0.0", digest="sha256:" + "d" * 64
            )
            if with_adapter
            else None
        ),
        qualification_status=(
            QualificationStatus.QUALIFIED
            if qualified
            else QualificationStatus.UNQUALIFIED
        ),
    )


def _evidence(
    release: AgentReleaseV1,
    *,
    case_ids: tuple[str, ...] = ("acp.initialize",),
    approval_options: tuple[str, ...] = ("auto", "per_step", "suggestion_only"),
) -> dict:
    """A stored evidence document recording a passing self-check verdict."""
    document = release.model_dump(mode="json")
    document["tests"] = {
        "status": "PASS",
        "approval_options": list(approval_options),
        "cases": [{"case_id": case_id, "status": "PASS"} for case_id in case_ids],
    }
    return document


def test_packaged_qualified_distribution_is_verified_with_a_digest():
    release = _release(qualified=True)
    view = resolve_distribution_view(_profile(), release, platform=("macos", "aarch64"))

    assert view.verified is True
    assert view.release_status == ReleaseResolutionStatus.RESOLVED
    assert view.release_id == "rel-1"
    assert view.version == "1.2.3"
    assert view.distribution_mode == "PACKAGED"
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


def test_release_only_byoa_distribution_derives_mode_and_identity_from_release():
    """A BYOA profile row carries only the pin; identity comes from the release."""
    release = _release(qualified=True, byoa=True)
    view = resolve_distribution_view(_profile(release_id="rel-1"), release)

    assert view.distribution_mode == "BYOA_EXTERNAL"
    assert view.artifact_digest is None
    assert view.verified is False
    assert view.agent_package == "goose"
    assert view.agent_command == "goose"
    assert view.agent_command_args == ["acp"]
    assert view.release_status == ReleaseResolutionStatus.RESOLVED


def test_release_only_byoa_distribution_without_identity_is_unqualified():
    release = _release(qualified=True, byoa=True)
    release.agent_command = None
    release.agent_package = None

    view = resolve_distribution_view(_profile(release_id="rel-1"), release)

    assert view.distribution_mode == "BYOA_EXTERNAL"
    assert view.release_status == ReleaseResolutionStatus.UNQUALIFIED
    assert view.message


def test_release_only_packaged_distribution_is_not_reported_as_byoa():
    """A release-only profile must never fall back to a stale PACKAGED default."""
    release = _release(qualified=True)
    view = resolve_distribution_view(_profile(), release)

    assert view.distribution_mode == "PACKAGED"
    assert view.agent_package is None
    assert view.agent_command is None


def _bootstrap_manifest(profile, release):
    """Compose a real bootstrap manifest for the profile/release pair."""
    profile_id = profile.profile_id
    enrollment_id = uuid.uuid4()
    study_id = uuid.uuid4()
    snapshot = {
        "profile_id": str(profile_id),
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "release_id": release.release_id,
        "tools_json": profile.tools_json,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "temperature": profile.temperature,
    }
    enrollment = SimpleNamespace(
        enrollment_id=enrollment_id,
        study_id=study_id,
        status=EnrollmentStatus.ACTIVE,
        revocation_epoch=0,
    )
    study = SimpleNamespace(
        study_id=study_id,
        is_research=True,
        research_status="ACTIVE",
        is_active=True,
        starts_at=None,
        ends_at=None,
        research_config_json={},
        research_config_digest="digest",
    )
    assignment = SimpleNamespace(
        assignment_id=uuid.uuid4(),
        enrollment_id=enrollment_id,
        study_id=study_id,
        agent_profile_id=profile_id,
        strategy="RANDOMIZED",
        randomization_epoch=1,
        profile_snapshot_json=snapshot,
        profile_digest=canonical_hash(snapshot),
    )
    result = compose_bootstrap(
        enrollment,
        study,
        assignment,
        release,
        None,
        EphemeralSessionFactory(),
        BootstrapSigningContext(secret="distribution-view-secret"),
        platform=("macos", "aarch64"),
        release_evidence_json=_evidence(release),
    )
    assert result.outcome == BootstrapOutcome.ISSUED, result.issue
    assert result.manifest is not None
    return result.manifest


def _bootstrap_manifest_mode(profile, release) -> str:
    return _bootstrap_manifest(profile, release).agent_release.distribution_mode


@pytest.mark.parametrize("byoa", [False, True])
def test_bootstrap_manifest_mode_agrees_with_the_distribution_view(byoa):
    release = _release(qualified=True, byoa=byoa)
    profile = _profile(
        release_id="rel-1", framework_version="codex" if byoa else "code4me2-agent"
    )

    view = resolve_distribution_view(profile, release, platform=("macos", "aarch64"))

    assert _bootstrap_manifest_mode(profile, release) == view.distribution_mode


def test_bootstrap_projects_the_byoa_configuration_contract():
    """The plugin must receive exactly the mapping validation enforced."""
    release = _release(qualified=True, byoa=True)
    profile = _profile(
        release_id="rel-1",
        framework_version="goose",
        tools_json='["shell"]',
        approval_policy="per_step",
        max_steps=4,
        temperature=0.2,
    )

    manifest = _bootstrap_manifest(profile, release)

    bindings = manifest.agent_release.config_bindings
    assert {binding.field for binding in bindings} == {
        "model",
        "temperature",
        "max_steps",
        "tools",
        "approval_policy",
    }
    tools_binding = next(item for item in bindings if item.field == "tools")
    assert tools_binding.transport == "env"
    assert tools_binding.format == "csv"
    assert tools_binding.key == "BYOA_AGENT_TOOLS"
    assert manifest.agent_profile.model == "gpt"
    assert manifest.agent_profile.tools_json == '["shell"]'
    assert manifest.agent_profile.approval_policy == "per_step"
    assert manifest.agent_profile.max_steps == 4
    assert manifest.agent_profile.temperature == 0.2


def test_parse_command_args_accepts_json_text_and_lists():
    assert parse_command_args('["a", "b"]') == ["a", "b"]
    assert parse_command_args(["a"]) == ["a"]
    assert parse_command_args(None) == []
    assert parse_command_args("not-json") == []


def test_distribution_payload_exposes_derived_view_without_secrets():
    profile = _profile(connection_id=None)
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
    assert payload["distribution_mode"] == "PACKAGED"
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


def test_profile_payload_accepts_a_release_pin_without_removed_fields():
    payload = AgentProfilePayload(
        name="n",
        model="m",
        approval_policy="auto",
        max_steps=1,
        connection_id=uuid.uuid4(),
        release_id=" rel-1 ",
    )
    assert payload.release_id == "rel-1"
    assert payload.framework_version == "code4me2-agent"


def test_profile_payload_rejects_a_latest_release_reference():
    with pytest.raises(PydanticValidationError):
        AgentProfilePayload(
            name="n",
            model="m",
            approval_policy="auto",
            max_steps=1,
            connection_id=uuid.uuid4(),
            release_id="latest",
        )


# ---------------------------------------------------------------------------
# Shared executable contract (ISSUE-03): framework × mode × qualification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("framework", "mode", "qualified", "expected_code"),
    [
        ("code4me2-agent", "PACKAGED", True, None),
        ("goose", "BYOA_EXTERNAL", True, None),
        ("codex", "BYOA_EXTERNAL", True, None),
        ("code4me2-agent", "BYOA_EXTERNAL", True, "FRAMEWORK_DISTRIBUTION_MISMATCH"),
        ("goose", "PACKAGED", True, "FRAMEWORK_DISTRIBUTION_MISMATCH"),
        ("codex", "PACKAGED", True, "FRAMEWORK_DISTRIBUTION_MISMATCH"),
        ("code4me2-agent", "PACKAGED", False, "RELEASE_NOT_QUALIFIED"),
        ("goose", "BYOA_EXTERNAL", False, "RELEASE_NOT_QUALIFIED"),
        ("codex", "BYOA_EXTERNAL", False, "RELEASE_NOT_QUALIFIED"),
    ],
)
def test_profile_configuration_matrix(framework, mode, qualified, expected_code):
    release = _release(qualified=qualified, byoa=(mode == "BYOA_EXTERNAL"))
    profile = _profile(framework_version=framework, release_id=release.release_id)

    if expected_code is None:
        validate_profile_configuration(
            profile, release, release_json=_evidence(release)
        )
    else:
        with pytest.raises(ProfileConfigurationError) as error:
            validate_profile_configuration(
                profile, release, release_json=_evidence(release)
            )
        assert error.value.code == expected_code


def test_profile_configuration_requires_a_release_pin():
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(_profile(release_id=None), None)

    assert error.value.code == "RELEASE_UNRESOLVED"


def test_profile_configuration_rejects_an_unknown_framework():
    release = _release(qualified=True)
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            _profile(framework_version="claude"), release, release_json=_evidence(release)
        )

    assert error.value.code == "FRAMEWORK_UNSUPPORTED"


def test_profile_configuration_rejects_a_byoa_release_without_identity():
    release = _release(qualified=True, byoa=True)
    release.agent_command = None
    release.agent_package = None

    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            _profile(framework_version="goose"),
            release,
            release_json=_evidence(release),
        )

    assert error.value.code == "DISTRIBUTION_IDENTITY_MISSING"


def test_profile_configuration_rejects_unmapped_byoa_fields():
    """ISSUE-03 Path A: a profile field the release cannot translate is refused."""
    release = _release(qualified=True, byoa=True)
    release.byoa_config = [
        binding for binding in release.byoa_config if binding.field == "model"
    ]

    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            _profile(framework_version="goose", release_id=release.release_id),
            release,
            release_json=_evidence(release),
        )

    assert error.value.code == "BYOA_CONFIG_UNMAPPED"
    assert "approval_policy" in str(error.value)
    assert "max_steps" in str(error.value)


def test_profile_configuration_accepts_a_complete_byoa_mapping():
    release = _release(qualified=True, byoa=True)

    assert {binding.field for binding in release.byoa_config} == {
        "model",
        "temperature",
        "max_steps",
        "tools",
        "approval_policy",
    }
    validate_profile_configuration(
        _profile(framework_version="goose", release_id=release.release_id),
        release,
        release_json=_evidence(release),
    )


def test_byoa_tools_binding_requires_a_list_format():
    release = _release(qualified=True, byoa=True)
    release.byoa_config = [
        binding.model_copy(update={"format": "string"})
        if binding.field == "tools"
        else binding
        for binding in release.byoa_config
    ]

    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            _profile(
                framework_version="goose",
                release_id=release.release_id,
                tools_json='["shell"]',
            ),
            release,
            release_json=_evidence(release),
        )

    assert error.value.code == "BYOA_CONFIG_FORMAT_INVALID"


def test_release_model_rejects_duplicate_or_packaged_config_bindings():
    binding = AgentConfigBinding(field="model", transport="env", key="AGENT_MODEL")
    base = dict(
        agent_id="codex",
        release_id="rel-dup",
        version="1.0.0",
        source_manifest_digest="sha256:" + "1" * 64,
    )

    with pytest.raises(PydanticValidationError):
        AgentReleaseV1(
            **base,
            distribution_mode=DistributionMode.BYOA_EXTERNAL,
            agent_command="codex",
            byoa_config=[binding, binding],
        )

    with pytest.raises(PydanticValidationError):
        AgentReleaseV1(**base, byoa_config=[binding])


def test_profile_configuration_rejects_tools_from_another_framework():
    # `read_file` is a managed-runtime tool; a Goose profile cannot select it.
    release = _release(qualified=True, byoa=True)
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            _profile(framework_version="goose", tools_json='["read_file"]'),
            release,
            release_json=_evidence(release),
        )

    assert error.value.code == "TOOLS_NOT_SUPPORTED"


def test_profile_configuration_enforces_approval_option_evidence():
    release = _release(qualified=True)
    profile = _profile(approval_policy="per_step")

    # A recipe that does not declare the gated option does not cover it.
    with pytest.raises(ProfileConfigurationError) as error:
        validate_profile_configuration(
            profile, release, release_json=_evidence(release, approval_options=("auto",))
        )
    assert error.value.code == "APPROVAL_OPTION_UNVERIFIED"

    # A recipe that declares the exercised option does.
    validate_profile_configuration(
        profile,
        release,
        release_json=_evidence(release, approval_options=("auto", "per_step")),
    )


def test_profile_configuration_accepts_every_declared_framework_pairing():
    """The declared pairing table is exactly what framework↔mode enforces."""
    assert FRAMEWORK_DISTRIBUTION_MODES == {
        "code4me2-agent": "PACKAGED",
        "goose": "BYOA_EXTERNAL",
        "codex": "BYOA_EXTERNAL",
    }


def test_config_binding_rejects_ambiguous_or_malformed_keys():
    """A KEY=VALUE token must never silently mis-bind (ISSUE-03 Path A)."""
    for bad in ("A=B", "1BAD_NAME", "HAS SPACE", "CTRL\u0001"):
        with pytest.raises(PydanticValidationError):
            AgentConfigBinding(field="model", transport="env", key=bad)

    # Arg flags keep their leading dashes; only '='/control characters are rejected.
    binding = AgentConfigBinding(
        field="approval_policy", transport="arg", key="--ask-for-approval"
    )
    assert binding.key == "--ask-for-approval"

    with pytest.raises(PydanticValidationError):
        AgentConfigBinding(
            field="approval_policy", transport="arg", key="--approval=always"
        )
