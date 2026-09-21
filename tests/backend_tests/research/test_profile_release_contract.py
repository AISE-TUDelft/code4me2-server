"""Typed profile-release validation: registry gate + executable contract."""

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from database import crud
from research.study.agents.distributions import (
    ProfileConfigurationError,
    resolve_distribution_view,
)
from research.study.agents.enums import (
    DistributionMode,
    QualificationStatus,
)
from research.study.agents.models import (
    AdapterRef,
    AgentConfigBinding,
    AgentReleaseV1,
)
from research.study.agents.registry import (
    AgentRegistry,
    byoa_identity_qualified,
    derive_qualification_status,
)
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.protocol.enums import (
    ReleaseResolutionStatus,
    ValidationReasonCode,
    ValidationSeverity,
)
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.validation import (
    blocking_errors,
    is_publishable,
    validate_protocol,
)

from ._byoa_contract import BYOA_CONFIG_BINDINGS

_PROTOCOL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
    / "approved_study_protocol_v1.json"
)
_BYOA_DISTRIBUTION_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000ab")


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
    document["tests"] = {
        "status": "PASS",
        "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
    }
    return SimpleNamespace(status="QUALIFIED", release_json=document)


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


def _byoa_release(*, release_id: str, manifest_digest: str, qualified: bool) -> AgentReleaseV1:
    """A BYOA codex release; qualified only via a bound PASS receipt document."""
    return AgentReleaseV1(
        agent_id="codex",
        release_id=release_id,
        version="1.2.3",
        source_manifest_digest=manifest_digest,
        distribution_mode=DistributionMode.BYOA_EXTERNAL,
        agent_package="codex",
        byoa_config=[
            AgentConfigBinding(**item) for item in BYOA_CONFIG_BINDINGS
        ],
        adapter=AdapterRef(
            adapter_id="acp-adapter", version="0.4.0", digest="sha256:" + "d" * 64
        ),
        qualification_status=(
            QualificationStatus.QUALIFIED
            if qualified
            else QualificationStatus.UNQUALIFIED
        ),
    )


def _byoa_receipt_document(release: AgentReleaseV1) -> dict:
    """The release document with a PASS receipt bound to its manifest digest.

    This is the in-memory shape of receipt qualification (ISSUE-003): the
    receipt binds the release's own ``source_manifest_digest`` plus the adapter
    digest, so :func:`derive_qualification_status` reports QUALIFIED.
    """
    document = release.model_dump(mode="json")
    document["tests"] = {
        "status": "PASS",
        "cases": [{"case_id": "acp.initialize", "status": "PASS"}],
    }
    return document


def _byoa_profile(**overrides):
    """A schema-shaped ``AgentProfile`` stand-in pinning a BYOA release."""
    base = dict(
        profile_id=_BYOA_DISTRIBUTION_ID,
        name="byoa-dist",
        release_id="rel-byoa-qualified",
        framework_version="codex",
        model="gpt",
        tools_json="[]",
        approval_policy="auto",
        max_steps=1,
        temperature=None,
        connection_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _byoa_protocol() -> StudyProtocolV1:
    data = json.loads(_PROTOCOL_FIXTURE.read_text())
    data["conditions"] = [data["conditions"][0]]
    data["conditions"][0]["distribution_id"] = str(_BYOA_DISTRIBUTION_ID)
    data["conditions"][0].pop("resolved_distribution", None)
    return StudyProtocolV1.model_validate(data)


def test_byoa_resolver_resolves_receipt_qualified_identity_but_not_unqualified():
    """ISSUE-004: the resolver binds a receipt-qualified BYOA identity (no DB).

    The qualified release's document carries a PASS receipt bound to its own
    manifest digest, which is what derives QUALIFIED (ISSUE-003, read-only
    here); the unqualified release carries no evidence. The in-memory registry
    resolver must return RESOLVED for the former and UNQUALIFIED for the
    latter, and the two outcomes must be asserted distinct.
    """
    qualified = _byoa_release(
        release_id="rel-byoa-qualified",
        manifest_digest="sha256:" + "2" * 64,
        qualified=True,
    )
    qualified_document = _byoa_receipt_document(qualified)
    assert derive_qualification_status(qualified_document) == QualificationStatus.QUALIFIED
    assert byoa_identity_qualified(qualified_document) is True

    unqualified = _byoa_release(
        release_id="rel-byoa-unqualified",
        manifest_digest="sha256:" + "3" * 64,
        qualified=False,
    )
    assert (
        derive_qualification_status(unqualified.model_dump(mode="json"))
        == QualificationStatus.UNQUALIFIED
    )

    registry = AgentRegistry()
    assert registry.register_release(qualified).accepted is True
    assert registry.register_release(unqualified).accepted is True
    resolver = RegistryReleaseResolver(registry)

    resolved = resolver.resolve(qualified.agent_id, release_id=qualified.release_id)
    assert resolved.status == ReleaseResolutionStatus.RESOLVED
    assert resolved.distribution_mode == DistributionMode.BYOA_EXTERNAL.value
    assert resolved.release_id == qualified.release_id

    missing = resolver.resolve(
        unqualified.agent_id, release_id=unqualified.release_id
    )
    assert missing.status == ReleaseResolutionStatus.UNQUALIFIED
    assert resolved.status != missing.status


def test_byoa_publication_severity_is_role_specific():
    """ISSUE-004: a receipt-qualified BYOA distribution warns admins, blocks others.

    Qualification (a bound receipt, asserted above) is distinct from
    distribution verification: a BYOA distribution is always unverified at
    publication, so the same ``DISTRIBUTION_UNVERIFIED`` reason is a WARNING
    for an administrator (publishable) and an ERROR for a non-administrator
    (blocked). Reason codes are asserted on both paths; no database is used.
    """
    release = _byoa_release(
        release_id="rel-byoa-qualified",
        manifest_digest="sha256:" + "2" * 64,
        qualified=True,
    )
    assert byoa_identity_qualified(_byoa_receipt_document(release)) is True
    view = resolve_distribution_view(_byoa_profile(), release)
    assert view.verified is False
    assert view.release_status == ReleaseResolutionStatus.RESOLVED

    class _StubDistributionResolver:
        def resolve(self, distribution_id):
            assert distribution_id == _BYOA_DISTRIBUTION_ID
            return view

    protocol = _byoa_protocol()

    admin_errors = validate_protocol(
        protocol,
        distribution_resolver=_StubDistributionResolver(),
        actor_is_admin=True,
    )
    admin_unverified = [
        error
        for error in admin_errors
        if error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    ]
    assert len(admin_unverified) == 1
    assert admin_unverified[0].severity == ValidationSeverity.WARNING
    assert blocking_errors(admin_errors) == []
    assert is_publishable(admin_errors) is True

    non_admin_errors = validate_protocol(
        protocol,
        distribution_resolver=_StubDistributionResolver(),
        actor_is_admin=False,
    )
    non_admin_unverified = [
        error
        for error in non_admin_errors
        if error.code == ValidationReasonCode.DISTRIBUTION_UNVERIFIED
    ]
    assert len(non_admin_unverified) == 1
    assert non_admin_unverified[0].severity == ValidationSeverity.ERROR
    assert blocking_errors(non_admin_errors) != []
    assert is_publishable(non_admin_errors) is False
