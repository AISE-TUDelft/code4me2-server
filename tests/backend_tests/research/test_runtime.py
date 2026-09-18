"""Consolidated research tests (see individual section headers).

Merged from smaller modules; test functions and assertions are unchanged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError

from backend.routers.analytics.auth_utils import AuthenticatedUser
from backend.routers.research.agents import (
    ReleaseRegisterRequest,
    ResolveArtifactRequest,
    SnapshotUploadRequest,
    coverage,
    get_release,
    list_releases,
    list_snapshots,
    register_release,
    resolve_artifact,
    upload_snapshot,
)
from research.study.agents import store as store_module
from research.study.agents.distributions import resolve_distribution_view
from research.study.agents.enums import (
    CapabilityCoverageState,
    DistributionMode,
    DistributionSourceType,
    QualificationStatus,
    RegistryReasonCode,
    SnapshotCapabilityState,
)
from research.study.agents.models import (
    AdapterRef,
    AgentReleaseV1,
    CapabilityEntry,
    CapabilitySnapshotV1,
    DistributionArtifact,
)
from research.study.agents.registry import (
    AgentRegistry,
    build_capability_snapshot,
    capability_coverage,
    coverage_report,
    derive_qualification_status,
)
from research.study.agents.resolver import RegistryReleaseResolver
from research.study.packaging import (
    CaseObservation,
    ComponentEntry,
    ConformanceCaseV1,
    ConformanceRunner,
    ConformanceStatus,
    PackageReasonCode,
    PlatformTriple,
    RuntimeManifestV2,
    build_package,
    qualification_for_release,
    resolve_component,
    resolved_digest,
    sha256_of_bytes,
    verify_package,
)
from research.study.packaging import store as packaging_store
from research.study.packaging.verifier import resolve_under_root
from research.study.protocol.enums import (
    PublicationOutcome,
    ReleaseResolutionStatus,
    ValidationReasonCode,
)
from research.study.protocol.models import StudyProtocolV1
from research.study.protocol.publication import RevisionLineage, publish_revision
from research.study.protocol.validation import validate_protocol

# --------------------------------------------------------------------------
# test_runtime_packaging
# --------------------------------------------------------------------------
# Tests for runtime packaging: manifest, verifier, and resolver (Issue 11).
runtime_packaging__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research" / "packaging"
runtime_packaging__CONTENT = b"#!/bin/sh\necho code4me-runtime\n"
runtime_packaging__LICENSE = b"MIT License\n"
runtime_packaging__ADAPTER = AdapterRef(adapter_id="code4me-acp", version="1.0.0", digest="sha256:" + "d" * 64)


def runtime_packaging___adapter(digest: str = "sha256:" + "d" * 64) -> AdapterRef:
    return AdapterRef(adapter_id="code4me-acp", version="1.0.0", digest=digest)


def runtime_packaging___build(root: Path, *, sign_secret: str | None = None, adapter: AdapterRef | None = None):
    return build_package(
        root,
        release_id="rel-codex-v1",
        agent_id="codex",
        adapter_ref=adapter or runtime_packaging__ADAPTER,
        os="macos",
        arch="arm64",
        files={"bin/codex-acp": runtime_packaging__CONTENT, "LICENSE": runtime_packaging__LICENSE},
        executable="bin/codex-acp",
        args_template=["--acp", "serve"],
        sign_secret=sign_secret,
    )


def runtime_packaging___codes(result) -> set[str]:
    return {error.code.value for error in result.errors}


# ---------------------------------------------------------------------------
# Manifest parse and digest
# ---------------------------------------------------------------------------


def test_manifest_parses_and_digest_is_recomputable():
    data = json.loads((runtime_packaging__FIXTURE_DIR / "valid_manifest.json").read_text())
    manifest = RuntimeManifestV2.model_validate(data)

    assert manifest.schema_version == "2"
    assert manifest.digest_matches()
    assert manifest.component_for("macos", "arm64") is not None
    assert manifest.args_for_platform("macos", "arm64") == ["--acp", "serve"]

    tampered = manifest.model_copy(
        update={"components": [manifest.components[0].model_copy(update={"size": 99})]}
    )
    assert not tampered.digest_matches()


def test_built_manifest_component_digest_matches_content(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    component = manifest.executable_component_for("macos", "arm64")
    assert component is not None
    assert component.sha256 == sha256_of_bytes(runtime_packaging__CONTENT)
    assert component.size == len(runtime_packaging__CONTENT)
    assert manifest.digest_matches()


# ---------------------------------------------------------------------------
# Verifier: path containment
# ---------------------------------------------------------------------------


def test_valid_package_is_verified(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    result = verify_package(tmp_path, manifest)
    assert result.valid, result.errors
    assert result.verified_components == 2


def test_absolute_and_parent_component_paths_are_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    for unsafe in ("/etc/passwd", "../escape", "bin/../../escape"):
        mutated = manifest.model_copy(
            update={
                "components": [
                    manifest.components[0].model_copy(update={"relative_path": unsafe}),
                    manifest.components[1],
                ]
            }
        ).with_digest()
        result = verify_package(tmp_path, mutated)
        assert not result.valid
        assert "PATH_ESCAPE" in runtime_packaging___codes(result), unsafe


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(runtime_packaging__CONTENT)
    link = root / "payload.bin"
    link.symlink_to(outside)

    manifest = RuntimeManifestV2(
        release_id="rel",
        agent_id="codex",
        adapter_ref=runtime_packaging__ADAPTER,
        components=[
            ComponentEntry(
                name="payload",
                os="macos",
                arch="arm64",
                relative_path="payload.bin",
                sha256=sha256_of_bytes(runtime_packaging__CONTENT),
                size=len(runtime_packaging__CONTENT),
            )
        ],
        supported_platforms=[PlatformTriple(os="macos", arch="arm64")],
    ).with_digest()

    result = verify_package(root, manifest)
    assert not result.valid
    assert "PATH_ESCAPE" in runtime_packaging___codes(result)

    containment = resolve_under_root(root, "payload.bin")
    assert containment.ok is False


def test_unsafe_fixture_manifest_is_flagged_with_path_escape(tmp_path):
    data = json.loads((runtime_packaging__FIXTURE_DIR / "unsafe_path_manifest.json").read_text())
    manifest = RuntimeManifestV2.model_validate(data)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "codex-acp").write_bytes(runtime_packaging__CONTENT)
    (tmp_path / "LICENSE").write_bytes(runtime_packaging__LICENSE)

    result = verify_package(tmp_path, manifest)
    assert not result.valid
    assert PackageReasonCode.PATH_ESCAPE in result.codes


# ---------------------------------------------------------------------------
# Verifier: integrity
# ---------------------------------------------------------------------------


def test_digest_mismatch_is_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    mutated = manifest.model_copy(
        update={
            "components": [
                manifest.components[0].model_copy(update={"sha256": "sha256:" + "f" * 64}),
                manifest.components[1],
            ]
        }
    ).with_digest()
    result = verify_package(tmp_path, mutated)
    assert not result.valid
    assert "DIGEST_MISMATCH" in runtime_packaging___codes(result)


def test_size_mismatch_is_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    mutated = manifest.model_copy(
        update={
            "components": [
                manifest.components[0].model_copy(update={"size": len(runtime_packaging__CONTENT) + 5}),
                manifest.components[1],
            ]
        }
    ).with_digest()
    result = verify_package(tmp_path, mutated)
    assert not result.valid
    assert "SIZE_MISMATCH" in runtime_packaging___codes(result)


def test_digest_mismatch_fixture_manifest_is_flagged(tmp_path):
    data = json.loads((runtime_packaging__FIXTURE_DIR / "digest_mismatch_manifest.json").read_text())
    manifest = RuntimeManifestV2.model_validate(data)
    result = verify_package(tmp_path, manifest)
    assert not result.valid
    assert PackageReasonCode.DIGEST_MISMATCH in result.codes


def test_signature_mismatch_is_rejected(tmp_path):
    good = runtime_packaging___build(tmp_path, sign_secret="correct-secret")
    assert verify_package(tmp_path, good, secret="correct-secret").valid

    wrong = verify_package(tmp_path, good, secret="wrong-secret")
    assert not wrong.valid
    assert "SIGNATURE_MISMATCH" in runtime_packaging___codes(wrong)

    unsigned_verification = verify_package(tmp_path, good)
    assert not unsigned_verification.valid
    assert "SIGNATURE_MISMATCH" in runtime_packaging___codes(unsigned_verification)


def test_missing_component_file_is_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    (tmp_path / "bin" / "codex-acp").unlink()
    result = verify_package(tmp_path, manifest)
    assert not result.valid
    assert "ARTIFACT_MISSING" in runtime_packaging___codes(result)


def test_undeclared_executable_is_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    extra = tmp_path / "bin" / "extra-tool"
    extra.write_bytes(b"#!/bin/sh\n")
    extra.chmod(0o755)

    result = verify_package(tmp_path, manifest)
    assert not result.valid
    assert "UNDECLARED_EXECUTABLE" in runtime_packaging___codes(result)


def test_secret_file_is_rejected(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    (tmp_path / ".env").write_text("AWS_SECRET_ACCESS_KEY=canary\n")

    result = verify_package(tmp_path, manifest)
    assert not result.valid
    assert "SECRET_FILE_PRESENT" in runtime_packaging___codes(result)

    (tmp_path / ".env").unlink()
    (tmp_path / "credentials.json").write_text('{"token": "ghp_CANARY000000000000000000"}\n')
    result2 = verify_package(tmp_path, manifest)
    assert not result2.valid
    assert "SECRET_FILE_PRESENT" in runtime_packaging___codes(result2)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolver_requires_exact_platform_and_bootstrap_digest(tmp_path):
    manifest = runtime_packaging___build(tmp_path)
    component = manifest.executable_component_for("macos", "arm64")
    assert component is not None

    resolved = resolve_component(manifest, "macos", "arm64", component.sha256)
    assert resolved.resolved is not None
    assert resolved.resolved.arguments == ["--acp", "serve"]
    assert resolved_digest(resolved) == component.sha256.removeprefix("sha256:")

    mismatch = resolve_component(manifest, "macos", "arm64", "sha256:" + "b" * 64)
    assert mismatch.resolved is None
    assert mismatch.error is not None
    assert mismatch.error.code == PackageReasonCode.BOOTSTRAP_DIGEST_MISMATCH

    malformed = resolve_component(manifest, "macos", "arm64", "not-a-digest")
    assert malformed.error is not None
    assert malformed.error.code == PackageReasonCode.BOOTSTRAP_DIGEST_MISMATCH


def test_resolver_blocks_unsupported_platform_and_missing_component_without_fallback(tmp_path):
    manifest = runtime_packaging___build(tmp_path)

    with patch("shutil.which", return_value="/usr/local/bin/codex"):
        unsupported = resolve_component(manifest, "linux", "x64", "sha256:" + "a" * 64)
    assert unsupported.resolved is None
    assert unsupported.error is not None
    assert unsupported.error.code == PackageReasonCode.UNSUPPORTED_PLATFORM

    no_component = manifest.model_copy(
        update={"supported_platforms": [PlatformTriple(os="linux", arch="x64")]}
    ).with_digest()
    missing = resolve_component(no_component, "linux", "x64", "sha256:" + "a" * 64)
    assert missing.resolved is None
    assert missing.error is not None
    assert missing.error.code == PackageReasonCode.MISSING_COMPONENT


def test_spaced_and_non_ascii_paths_use_argument_arrays(tmp_path):
    root = tmp_path / "Runtime Dir" / "ärchive"
    manifest = build_package(
        root,
        release_id="rel",
        agent_id="codex",
        adapter_ref=runtime_packaging__ADAPTER,
        os="macos",
        arch="arm64",
        files={"bin/cödéx acp": runtime_packaging__CONTENT, "LICENSE mü": runtime_packaging__LICENSE},
        executable="bin/cödéx acp",
        args_template=["--path", "a b", "café"],
    )
    component = manifest.executable_component_for("macos", "arm64")
    assert component is not None

    resolved = resolve_component(manifest, "macos", "arm64", component.sha256)
    assert resolved.resolved is not None
    assert isinstance(resolved.resolved.arguments, list)
    assert resolved.resolved.arguments == ["--path", "a b", "café"]
    # The relative path is carried verbatim; no shell quoting/escaping is applied.
    assert resolved.resolved.relative_path == "bin/cödéx acp"
    assert " " in resolved.resolved.relative_path


def test_self_check_failure_is_typed(tmp_path):
    manifest = build_package(
        tmp_path,
        release_id="rel",
        agent_id="codex",
        adapter_ref=runtime_packaging__ADAPTER,
        os="macos",
        arch="arm64",
        files={"bin/codex-acp": runtime_packaging__CONTENT},
        executable="bin/codex-acp",
        self_check={"command": ["bin/codex-acp", "--version"], "expected_exit_code": 0},
    )
    failing = verify_package(tmp_path, manifest, self_check_runner=lambda _m, _p: 3)
    assert not failing.valid
    assert "SELF_CHECK_FAILED" in runtime_packaging___codes(failing)

    passing = verify_package(tmp_path, manifest, self_check_runner=lambda _m, _p: 0)
    assert passing.valid, passing.errors


# ---------------------------------------------------------------------------
# Store helpers and router wiring
# ---------------------------------------------------------------------------


def test_store_helpers_use_session():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    session.execute.return_value.scalars.return_value.first.return_value = None

    assert list(packaging_store.list_packages(session, "rel-1")) == []
    assert list(packaging_store.list_receipts(session)) == []
    assert packaging_store.get_package_by_digest(session, "sha256:abc") is None
    coverage = packaging_store.conformance_coverage(session)
    assert coverage["total"] == 0
    assert coverage["by_status"] == {}


def test_packages_routes_are_wired():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    for path in (
        "/research/packages",
        "/research/packages/receipts",
        "/research/packages/coverage",
        "/research/packages/{package_id}",
        "/research/packages/{package_id}/qualification",
    ):
        assert path in paths, path


def test_manifest_rejects_unknown_fields():
    from pydantic import ValidationError

    data = json.loads((runtime_packaging__FIXTURE_DIR / "valid_manifest.json").read_text())
    data["prompt"] = "CANARY"
    with pytest.raises(ValidationError):
        RuntimeManifestV2.model_validate(data)


# --------------------------------------------------------------------------
# test_conformance_suite
# --------------------------------------------------------------------------
# Tests for the capability-aware conformance suite (Issue 11).
conformance_suite__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research" / "packaging"
conformance_suite__NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
conformance_suite__HOST = PlatformTriple(os="macos", arch="arm64")
conformance_suite__ARTIFACT = "sha256:" + "a" * 64
conformance_suite__ADAPTER = "sha256:" + "d" * 64


class conformance_suite___Observer:
    """Deterministic observer used to drive each truthful-status branch."""

    def __init__(
        self,
        *,
        host_observations: list[str] | None = None,
        agent_observations: list[str] | None = None,
        cleanup_ok: bool | None = True,
        performance_ms: float | None = 5.0,
        status: ConformanceStatus | None = None,
        raise_error: bool = False,
    ) -> None:
        self.host_observations = host_observations if host_observations is not None else ["h"]
        self.agent_observations = agent_observations if agent_observations is not None else ["a"]
        self.cleanup_ok = cleanup_ok
        self.performance_ms = performance_ms
        self.status = status
        self.raise_error = raise_error

    def observe(self, case: ConformanceCaseV1) -> CaseObservation:
        if self.raise_error:
            raise RuntimeError("fixture exploded")
        return CaseObservation(
            host_observations=self.host_observations,
            agent_observations=self.agent_observations,
            cleanup_ok=self.cleanup_ok,
            performance_ms=self.performance_ms,
            status=self.status,
        )


def conformance_suite___case(case_id: str = "c1", **overrides) -> ConformanceCaseV1:
    data = {
        "case_id": case_id,
        "prerequisites": ["acp.initialize"],
        "fixture_ref": "fixture",
        "action_steps": ["run"],
        "expected_host_observations": ["h"],
        "expected_agent_observations": ["a"],
        "cleanup_assertion": "temp removed",
    }
    data.update(overrides)
    return ConformanceCaseV1(**data)


def conformance_suite___run(cases, prerequisites, observer, **overrides):
    runner = ConformanceRunner(observer)
    params = {
        "artifact_digest": conformance_suite__ARTIFACT,
        "adapter_digest": conformance_suite__ADAPTER,
        "host": conformance_suite__HOST,
        "plugin_version": "2026.1",
        "protocol_version": "1",
        "fixture_digests": {"initialize": "sha256:" + "f" * 64},
        "now": conformance_suite__NOW,
    }
    params.update(overrides)
    return runner.run(cases, prerequisites, **params)


def conformance_suite___release(*, artifact_sha: str = conformance_suite__ARTIFACT, adapter_digest: str = conformance_suite__ADAPTER) -> AgentReleaseV1:
    return AgentReleaseV1(
        agent_id="codex",
        release_id="rel-1",
        version="1.0.0",
        source_type=DistributionSourceType.EXTERNAL_REGISTRY,
        source_manifest_digest="sha256:" + "1" * 64,
        artifacts=[
            DistributionArtifact(
                os="macos", arch="arm64", path="bin/codex-acp", sha256=artifact_sha, size=10
            )
        ],
        adapter=AdapterRef(adapter_id="code4me-acp", version="1.0.0", digest=adapter_digest),
        qualification_status=QualificationStatus.DRAFT,
    )


# ---------------------------------------------------------------------------
# Truthful statuses
# ---------------------------------------------------------------------------


def test_pass_requires_established_prerequisites_and_matching_observations():
    receipt = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer())
    assert receipt.status == ConformanceStatus.PASS
    assert receipt.case_results[0].status == ConformanceStatus.PASS
    assert receipt.case_results[0].evidence_digest


def test_unsupported_and_unknown_prerequisites_are_never_pass():
    unsupported = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "UNSUPPORTED"}, conformance_suite___Observer())
    assert unsupported.case_results[0].status == ConformanceStatus.UNSUPPORTED
    assert unsupported.status == ConformanceStatus.UNSUPPORTED

    unknown = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "UNKNOWN"}, conformance_suite___Observer())
    assert unknown.case_results[0].status == ConformanceStatus.UNKNOWN

    missing = conformance_suite___run([conformance_suite___case()], {}, conformance_suite___Observer())
    assert missing.case_results[0].status == ConformanceStatus.UNKNOWN


def test_observer_exception_is_blocked():
    receipt = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer(raise_error=True))
    assert receipt.case_results[0].status == ConformanceStatus.BLOCKED
    assert receipt.status == ConformanceStatus.BLOCKED


def test_cleanup_failure_is_fail_even_with_matching_observations():
    receipt = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer(cleanup_ok=False))
    assert receipt.case_results[0].status == ConformanceStatus.FAIL
    assert "cleanup" in receipt.case_results[0].reason


def test_unverified_cleanup_assertion_is_unknown():
    receipt = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer(cleanup_ok=None))
    assert receipt.case_results[0].status == ConformanceStatus.UNKNOWN


def test_missing_expected_observation_is_fail():
    receipt = conformance_suite___run(
        [conformance_suite___case()],
        {"acp.initialize": "ESTABLISHED"},
        conformance_suite___Observer(agent_observations=[]),
    )
    assert receipt.case_results[0].status == ConformanceStatus.FAIL


def test_observer_reported_non_pass_status_is_preserved():
    receipt = conformance_suite___run(
        [conformance_suite___case()],
        {"acp.initialize": "ESTABLISHED"},
        conformance_suite___Observer(status=ConformanceStatus.UNSUPPORTED),
    )
    assert receipt.case_results[0].status == ConformanceStatus.UNSUPPORTED


def test_performance_bound_exceeded_is_fail():
    case = conformance_suite___case(max_performance_ms=1.0)
    receipt = conformance_suite___run([case], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer(performance_ms=10.0))
    assert receipt.case_results[0].status == ConformanceStatus.FAIL
    assert "performance" in receipt.case_results[0].reason


def test_receipt_status_precedence_fail_dominates():
    cases = [conformance_suite___case("c1"), conformance_suite___case("c2")]
    first = conformance_suite___run(cases, {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer())
    assert first.status == ConformanceStatus.PASS

    class MixedObserver:
        def observe(self, case: ConformanceCaseV1) -> CaseObservation:
            if case.case_id == "c1":
                return CaseObservation(cleanup_ok=False)
            return CaseObservation(status=ConformanceStatus.UNKNOWN)

    mixed = conformance_suite___run(cases, {"acp.initialize": "ESTABLISHED"}, MixedObserver())
    assert mixed.status == ConformanceStatus.FAIL


# ---------------------------------------------------------------------------
# Receipt binding
# ---------------------------------------------------------------------------


def test_receipt_binds_exact_artifact_adapter_host_plugin_protocol_and_fixtures():
    receipt = conformance_suite___run([conformance_suite___case()], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer())
    assert receipt.artifact_digest == conformance_suite__ARTIFACT
    assert receipt.adapter_digest == conformance_suite__ADAPTER
    assert receipt.host == conformance_suite__HOST
    assert receipt.host.key == "macos-arm64"
    assert receipt.plugin_version == "2026.1"
    assert receipt.protocol_version == "1"
    assert receipt.fixture_digests == {"initialize": "sha256:" + "f" * 64}
    assert receipt.created_at == conformance_suite__NOW
    assert isinstance(receipt.receipt_id, uuid.UUID)


def test_fixture_conformance_cases_load_and_validate():
    raw = json.loads((conformance_suite__FIXTURE_DIR / "conformance_cases.json").read_text())
    cases = [ConformanceCaseV1.model_validate(item) for item in raw]
    assert {case.case_id for case in cases} == {
        "initialize.session",
        "tool.read",
        "permission.cancel",
    }
    assert all(case.prerequisites for case in cases)


# ---------------------------------------------------------------------------
# Qualification linking
# ---------------------------------------------------------------------------


def conformance_suite___passing_receipt(*, artifact_digest: str = conformance_suite__ARTIFACT, adapter_digest: str = conformance_suite__ADAPTER, host: PlatformTriple = conformance_suite__HOST):
    receipt = conformance_suite___run(
        [conformance_suite___case("c1"), conformance_suite___case("c2")],
        {"acp.initialize": "ESTABLISHED"},
        conformance_suite___Observer(),
        artifact_digest=artifact_digest,
        adapter_digest=adapter_digest,
        host=host,
    )
    assert receipt.status == ConformanceStatus.PASS
    return receipt


def test_qualification_promoted_with_all_required_cases_passing():
    release = conformance_suite___release()
    receipt = conformance_suite___passing_receipt()
    decision = qualification_for_release(
        release, [receipt], required_cases=["c1", "c2"], os="macos", arch="arm64"
    )
    assert decision.promote is True
    assert decision.reason == PackageReasonCode.OK
    assert decision.receipt_id == receipt.receipt_id


def test_qualification_not_promoted_without_a_pass():
    release = conformance_suite___release()
    failing = conformance_suite___run([conformance_suite___case("c1")], {"acp.initialize": "ESTABLISHED"}, conformance_suite___Observer(cleanup_ok=False))
    assert failing.status == ConformanceStatus.FAIL

    decision = qualification_for_release(
        release, [failing], required_cases=["c1"], os="macos", arch="arm64"
    )
    assert decision.promote is False
    assert decision.reason == PackageReasonCode.CONFORMANCE_NOT_PASSED


def test_qualification_not_promoted_when_a_required_case_is_missing():
    release = conformance_suite___release()
    receipt = conformance_suite___passing_receipt()
    decision = qualification_for_release(
        release, [receipt], required_cases=["c1", "c9"], os="macos", arch="arm64"
    )
    assert decision.promote is False


def test_qualification_host_mismatch_is_typed():
    release = conformance_suite___release()
    receipt = conformance_suite___passing_receipt(host=PlatformTriple(os="linux", arch="x64"))
    decision = qualification_for_release(
        release, [receipt], required_cases=["c1"], os="macos", arch="arm64"
    )
    assert decision.promote is False
    assert decision.reason == PackageReasonCode.RECEIPT_HOST_MISMATCH


def test_qualification_artifact_digest_mismatch_is_not_promoted():
    release = conformance_suite___release(artifact_sha="sha256:" + "9" * 64)
    receipt = conformance_suite___passing_receipt()
    decision = qualification_for_release(
        release, [receipt], required_cases=["c1"], os="macos", arch="arm64"
    )
    assert decision.promote is False
    assert decision.reason == PackageReasonCode.CONFORMANCE_NOT_PASSED


def test_qualification_missing_artifact_is_typed():
    release = conformance_suite___release()
    decision = qualification_for_release(
        release, [], required_cases=["c1"], os="windows", arch="x64"
    )
    assert decision.promote is False
    assert decision.reason == PackageReasonCode.MISSING_COMPONENT


# --------------------------------------------------------------------------
# test_agent_registry
# --------------------------------------------------------------------------
# Tests for the agent registry and capability contract (Issue 04).
#
# Route handlers are exercised by calling the functions directly with a
# ``MagicMock`` app/session. No ``TestClient`` is used because this environment
# has no PostgreSQL/Redis; digest identity, platform selection, qualification and
# snapshot semantics are pure and fully unit-testable.
agent_registry__FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "research" / "agents"
agent_registry__PROTOCOL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research"
    / "protocol"
    / "approved_study_protocol_v1.json"
)

agent_registry__RELEASE_FIXTURE = "release_codex_acp_v1.json"
agent_registry__SNAPSHOT_FIXTURE = "snapshot_declared_unobserved.json"

agent_registry__NOW = datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def agent_registry___fixture(name: str) -> dict:
    return json.loads((agent_registry__FIXTURE_DIR / name).read_text())


def agent_registry___release(**overrides) -> AgentReleaseV1:
    data = agent_registry___fixture(agent_registry__RELEASE_FIXTURE)
    data.update(overrides)
    return AgentReleaseV1.model_validate(data)


def agent_registry___single_artifact_release(
    *, sha256: str = "sha256:" + "a" * 64, **overrides
) -> AgentReleaseV1:
    data = agent_registry___fixture(agent_registry__RELEASE_FIXTURE)
    data["artifacts"] = [
        {
            "os": "macOS",
            "arch": "aarch64",
            "path": "codex/1.2.3/macos-aarch64.tar.gz",
            "sha256": sha256,
            "size": 1048576,
            "executable": "bin/codex",
        }
    ]
    data.update(overrides)
    return AgentReleaseV1.model_validate(data)


def agent_registry___snapshot(**overrides) -> CapabilitySnapshotV1:
    data = agent_registry___fixture(agent_registry__SNAPSHOT_FIXTURE)
    data.update(overrides)
    return CapabilitySnapshotV1.model_validate(data)


def agent_registry___release_row(release: AgentReleaseV1) -> SimpleNamespace:
    return SimpleNamespace(
        release_id=release.release_id,
        agent_id=release.agent_id,
        source_manifest_digest=release.source_manifest_digest,
        status=release.qualification_status.value,
        release_json=release.model_dump(mode="json"),
        created_at=release.created_at,
    )


def agent_registry___snapshot_row(snapshot: CapabilitySnapshotV1) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_id=snapshot.snapshot_id,
        release_id=snapshot.release_id,
        adapter_version=snapshot.adapter_version,
        protocol_version=snapshot.protocol_version,
        snapshot_json=snapshot.model_dump(mode="json"),
        captured_at=snapshot.captured_at,
    )


def agent_registry___admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=True, email="admin@example.com", name="Admin"
    )


def agent_registry___non_admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.uuid4(), is_admin=False, email="user@example.com", name="User"
    )


def agent_registry___codes(errors) -> set:
    return {error.code for error in errors}


agent_registry__DISTRIBUTION_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000aa")


def agent_registry___protocol_referencing(release: AgentReleaseV1, digest: str) -> StudyProtocolV1:
    data = json.loads(agent_registry__PROTOCOL_FIXTURE.read_text())
    data["conditions"] = [data["conditions"][0]]
    data["conditions"][0]["distribution_id"] = str(agent_registry__DISTRIBUTION_ID)
    data["conditions"][0].pop("resolved_distribution", None)
    return StudyProtocolV1.model_validate(data)


def agent_registry___distribution_resolver(
    release: AgentReleaseV1, *, platform: tuple[str, str] = ("macOS", "aarch64")
):
    """A standalone distribution resolver over one in-memory release."""
    profile = SimpleNamespace(
        profile_id=agent_registry__DISTRIBUTION_ID,
        distribution_mode=release.distribution_mode.value,
        release_id=release.release_id,
        agent_package=release.agent_package,
        agent_command=release.agent_command,
        agent_command_args=release.agent_command_args,
    )

    class _Resolver:
        def resolve(self, distribution_id):
            return resolve_distribution_view(
                profile,
                release,
                distribution_id=distribution_id,
                platform=platform,
            )

    return _Resolver()


# ---------------------------------------------------------------------------
# Digest identity
# ---------------------------------------------------------------------------


def test_same_agent_version_with_different_digest_is_a_distinct_release():
    registry = AgentRegistry()
    first = agent_registry___release(
        release_id="rel-0001",
        source_manifest_digest="sha256:" + "1" * 64,
    )
    second = agent_registry___release(
        release_id="rel-0002",
        source_manifest_digest="sha256:" + "2" * 64,
    )

    assert registry.register_release(first).accepted is True
    assert registry.register_release(second).accepted is True

    releases = registry.list_releases(first.agent_id)
    assert len(releases) == 2
    digests = {release.source_manifest_digest for release in releases}
    assert digests == {first.source_manifest_digest, second.source_manifest_digest}
    # Same human version label, but two distinct digest-pinned records.
    assert {release.version for release in releases} == {first.version}


def test_exact_duplicate_digest_identity_is_rejected():
    registry = AgentRegistry()
    release = agent_registry___release()
    assert registry.register_release(release).accepted is True

    duplicate = registry.register_release(release)
    assert duplicate.accepted is False
    assert duplicate.issue is not None
    assert duplicate.issue.code == RegistryReasonCode.DUPLICATE_RELEASE
    assert len(registry.list_releases()) == 1


def test_same_release_id_with_changed_digest_is_rejected_without_overwriting():
    registry = AgentRegistry()
    registry.register_release(agent_registry___release(source_manifest_digest="sha256:" + "1" * 64))

    changed = registry.register_release(
        agent_registry___release(source_manifest_digest="sha256:" + "2" * 64)
    )
    assert changed.accepted is False
    assert changed.issue is not None
    assert changed.issue.code == RegistryReasonCode.DUPLICATE_RELEASE
    stored = registry.get_release("codex-acp", "rel-codex-1.2.3")
    assert stored is not None
    assert stored.source_manifest_digest == "sha256:" + "1" * 64


def test_empty_manifest_digest_is_rejected():
    registry = AgentRegistry()
    result = registry.register_release(agent_registry___release(source_manifest_digest="   "))
    assert result.accepted is False
    assert result.issue is not None
    assert result.issue.code == RegistryReasonCode.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# Platform selection
# ---------------------------------------------------------------------------


def test_platform_selection_returns_exact_matching_artifact():
    registry = AgentRegistry()
    release = agent_registry___release()

    macos = registry.resolve_artifact(release, "macOS", "aarch64")
    linux = registry.resolve_artifact(release, "Linux", "x86_64")

    assert macos.resolved is True
    assert macos.artifact is not None
    assert (macos.artifact.os, macos.artifact.arch) == ("macOS", "aarch64")
    assert macos.artifact.sha256 == release.artifacts[0].sha256

    assert linux.resolved is True
    assert linux.artifact is not None
    assert linux.artifact.sha256 == release.artifacts[1].sha256


def test_unsupported_platform_is_a_typed_block_with_no_fallback():
    registry = AgentRegistry()
    release = agent_registry___release()

    result = registry.resolve_artifact(release, "Windows", "x86_64")
    assert result.resolved is False
    assert result.artifact is None
    assert result.issue is not None
    assert result.issue.code == RegistryReasonCode.UNSUPPORTED_PLATFORM

    # An existing OS with a missing architecture is equally unsupported.
    wrong_arch = registry.resolve_artifact(release, "macOS", "x86_64")
    assert wrong_arch.resolved is False
    assert wrong_arch.issue is not None
    assert wrong_arch.issue.code == RegistryReasonCode.UNSUPPORTED_PLATFORM


def test_release_without_artifacts_is_artifact_missing():
    registry = AgentRegistry()
    release = agent_registry___release(artifacts=[])

    result = registry.resolve_artifact(release, "macOS", "aarch64")
    assert result.resolved is False
    assert result.issue is not None
    assert result.issue.code == RegistryReasonCode.ARTIFACT_MISSING


def test_artifact_with_empty_digest_is_blocked():
    registry = AgentRegistry()
    release = agent_registry___release()
    release.artifacts[0].sha256 = ""

    result = registry.resolve_artifact(release, "macOS", "aarch64")
    assert result.resolved is False
    assert result.issue is not None
    assert result.issue.code == RegistryReasonCode.DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# Qualification transitions and assessment
# ---------------------------------------------------------------------------


def _bound_passing_receipt(release) -> dict:
    """A PASS receipt bound to the fixture release's exact artifact identity."""
    artifact = release.artifacts[0]
    return {
        "receipt_id": str(uuid.uuid4()),
        "status": "PASS",
        "artifact_digest": artifact.sha256,
        "adapter_digest": release.adapter.digest,
        "host": {"os": artifact.os, "arch": artifact.arch},
        "case_results": [{"case_id": "acp.initialize", "status": "PASS"}],
    }


def test_qualification_is_derived_from_passing_conformance_evidence():
    release = agent_registry___release()
    release_json = release.model_dump(mode="json")

    # No conformance evidence -> unqualified, even when a caller-supplied status
    # is present in the document.
    assert derive_qualification_status({}) == QualificationStatus.UNQUALIFIED
    assert (
        derive_qualification_status({"qualification_status": "QUALIFIED"})
        == QualificationStatus.UNQUALIFIED
    )
    # A failing receipt never promotes.
    assert (
        derive_qualification_status({"conformance": [{"status": "FAIL"}]})
        == QualificationStatus.UNQUALIFIED
    )
    # 'Any PASS receipt' is not adequate: a receipt that does not bind to the
    # release's exact artifact/adapter/platform/cases never promotes.
    assert (
        derive_qualification_status({"conformance": [{"status": "PASS"}]})
        == QualificationStatus.UNQUALIFIED
    )
    unbound = _bound_passing_receipt(release)
    unbound["artifact_digest"] = "sha256:" + "e" * 64
    assert (
        derive_qualification_status({**release_json, "conformance": [unbound]})
        == QualificationStatus.UNQUALIFIED
    )
    wrong_adapter = _bound_passing_receipt(release)
    wrong_adapter["adapter_digest"] = "sha256:" + "f" * 64
    assert (
        derive_qualification_status({**release_json, "conformance": [wrong_adapter]})
        == QualificationStatus.UNQUALIFIED
    )
    wrong_platform = _bound_passing_receipt(release)
    wrong_platform["host"] = {"os": "windows", "arch": "x86_64"}
    assert (
        derive_qualification_status({**release_json, "conformance": [wrong_platform]})
        == QualificationStatus.UNQUALIFIED
    )
    no_cases = _bound_passing_receipt(release)
    no_cases["case_results"] = []
    assert (
        derive_qualification_status({**release_json, "conformance": [no_cases]})
        == QualificationStatus.UNQUALIFIED
    )
    # A receipt bound to the exact artifact/adapter/platform with a passing case
    # is the only promotion path.
    bound = _bound_passing_receipt(release)
    assert (
        derive_qualification_status({**release_json, "conformance": [bound]})
        == QualificationStatus.QUALIFIED
    )


def test_upsert_release_never_trusts_a_caller_supplied_status():
    release = agent_registry___release(
        qualification_status=QualificationStatus.QUALIFIED
    )
    session = MagicMock()
    session.get.return_value = None

    row = store_module.upsert_release(session, release)

    assert row.status == QualificationStatus.UNQUALIFIED.value
    assert row.release_json["qualification_status"] == "UNQUALIFIED"


def test_row_to_release_derives_qualified_from_stored_conformance():
    release = agent_registry___release()
    row = agent_registry___release_row(release)
    row.release_json = {
        **row.release_json,
        "conformance": [_bound_passing_receipt(release)],
    }

    restored = store_module.row_to_release(row)

    assert restored.qualification_status == QualificationStatus.QUALIFIED
    assert store_module.release_summary(row)["status"] == "QUALIFIED"


def test_row_to_release_stays_unqualified_for_an_unbound_pass_receipt():
    release = agent_registry___release()
    row = agent_registry___release_row(release)
    row.release_json = {
        **row.release_json,
        "conformance": [{"receipt_id": str(uuid.uuid4()), "status": "PASS"}],
    }

    restored = store_module.row_to_release(row)

    assert restored.qualification_status == QualificationStatus.UNQUALIFIED
    assert store_module.release_summary(row)["status"] == "UNQUALIFIED"


def test_qualification_assessment_reports_blockers():
    incomplete = agent_registry___release(adapter=None)
    registry = AgentRegistry()
    assessment = registry.assess_qualification(incomplete)
    assert assessment.qualifiable is False
    assert RegistryReasonCode.ADAPTER_INCOMPATIBLE in {
        blocker.code for blocker in assessment.blockers
    }

    no_artifacts = agent_registry___release(artifacts=[])
    assessment = registry.assess_qualification(no_artifacts)
    assert assessment.qualifiable is False
    assert RegistryReasonCode.ARTIFACT_MISSING in {
        blocker.code for blocker in assessment.blockers
    }

    assert registry.assess_qualification(agent_registry___release()).qualifiable is True


# ---------------------------------------------------------------------------
# Capability snapshots: declared vs observed
# ---------------------------------------------------------------------------


def test_declared_and_observed_capabilities_remain_independent():
    release = agent_registry___release()
    snapshot = agent_registry__registry_snapshot(release)

    assert set(snapshot.declared) == {"INITIALIZE", "USAGE", "PLANS"}
    assert set(snapshot.observed) == {"INITIALIZE", "USAGE"}
    # A declared-but-unobserved capability is preserved as DECLARED.
    assert snapshot.declared["PLANS"].state == SnapshotCapabilityState.DECLARED
    assert "PLANS" not in snapshot.observed
    # A declared capability with only an UNAVAILABLE observation keeps both facts.
    assert snapshot.declared["USAGE"].state == SnapshotCapabilityState.DECLARED
    assert snapshot.observed["USAGE"].state == SnapshotCapabilityState.UNAVAILABLE


def agent_registry__registry_snapshot(release: AgentReleaseV1) -> CapabilitySnapshotV1:
    registry = AgentRegistry()
    return registry.record_snapshot(
        release,
        protocol_version="1",
        declared=agent_registry___declared(),
        observed=agent_registry___observed(),
        captured_at=agent_registry__NOW,
    )


def agent_registry___declared() -> dict[str, CapabilityEntry]:
    return {
        "INITIALIZE": CapabilityEntry(
            state=SnapshotCapabilityState.DECLARED, value=True
        ),
        "USAGE": CapabilityEntry(
            state=SnapshotCapabilityState.DECLARED,
            value=None,
            limitations=["declared only"],
        ),
        "PLANS": CapabilityEntry(
            state=SnapshotCapabilityState.DECLARED, value=True
        ),
    }


def agent_registry___observed() -> dict[str, CapabilityEntry]:
    return {
        "INITIALIZE": CapabilityEntry(
            state=SnapshotCapabilityState.OBSERVED, value=True
        ),
        "USAGE": CapabilityEntry(
            state=SnapshotCapabilityState.UNAVAILABLE,
            value=None,
            limitations=["no usage event observed"],
        ),
    }


def test_snapshot_and_entries_are_immutable():
    snapshot = agent_registry___snapshot()
    with pytest.raises(PydanticValidationError):
        snapshot.agent_id = "other-agent"  # type: ignore[misc]
    with pytest.raises(PydanticValidationError):
        snapshot.declared["INITIALIZE"].value = False  # type: ignore[misc]


def test_missing_usage_is_none_and_never_zero_false_or_success():
    snapshot = agent_registry___snapshot()
    usage = snapshot.observed["USAGE"]

    assert usage.value is None
    assert usage.value != 0
    assert usage.value is not False
    assert usage.state == SnapshotCapabilityState.UNAVAILABLE

    serialized = snapshot.model_dump(mode="json")
    assert serialized["observed"]["USAGE"]["value"] is None
    assert serialized["observed"]["USAGE"]["state"] == "UNAVAILABLE"


def test_build_snapshot_does_not_synthesize_observed_entries():
    release = agent_registry___release()
    snapshot = build_capability_snapshot(
        uuid.uuid4(),
        release,
        protocol_version="1",
        declared={"PLANS": SnapshotCapabilityState.DECLARED},
        observed={},
        captured_at=agent_registry__NOW,
    )
    assert "PLANS" in snapshot.declared
    assert snapshot.observed == {}


def test_build_snapshot_none_value_stays_none():
    release = agent_registry___release()
    snapshot = build_capability_snapshot(
        uuid.uuid4(),
        release,
        protocol_version="1",
        declared={"USAGE": None},
        observed={"USAGE": None},
        captured_at=agent_registry__NOW,
    )
    assert snapshot.declared["USAGE"].state == SnapshotCapabilityState.DECLARED
    assert snapshot.declared["USAGE"].value is None
    assert snapshot.observed["USAGE"].state == SnapshotCapabilityState.UNKNOWN
    assert snapshot.observed["USAGE"].value is None


# ---------------------------------------------------------------------------
# Coverage semantics
# ---------------------------------------------------------------------------


def test_coverage_keeps_explicit_states_not_booleans():
    entries = {entry.capability: entry for entry in capability_coverage(agent_registry___snapshot())}

    assert entries["INITIALIZE"].coverage == CapabilityCoverageState.OBSERVED
    assert entries["INITIALIZE"].observed_state == SnapshotCapabilityState.OBSERVED
    assert entries["INITIALIZE"].value_present is True

    assert entries["USAGE"].coverage == CapabilityCoverageState.UNAVAILABLE
    assert entries["USAGE"].observed_state == SnapshotCapabilityState.UNAVAILABLE
    assert entries["USAGE"].value_present is False

    assert entries["PLANS"].coverage == CapabilityCoverageState.DECLARED_ONLY
    assert entries["PLANS"].observed_state == SnapshotCapabilityState.UNKNOWN


def test_coverage_report_counts_states():
    report = coverage_report(agent_registry___snapshot())
    assert report.snapshot_id == agent_registry___snapshot().snapshot_id
    assert report.counts["OBSERVED"] == 1
    assert report.counts["UNAVAILABLE"] == 1
    assert report.counts["DECLARED_ONLY"] == 1


# ---------------------------------------------------------------------------
# RegistryReleaseResolver mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (QualificationStatus.DRAFT, ReleaseResolutionStatus.UNQUALIFIED),
        (
            QualificationStatus.CONDITIONALLY_QUALIFIED,
            ReleaseResolutionStatus.UNQUALIFIED,
        ),
        (QualificationStatus.QUALIFIED, ReleaseResolutionStatus.RESOLVED),
        (QualificationStatus.RETIRED, ReleaseResolutionStatus.WITHDRAWN),
        (QualificationStatus.BLOCKED, ReleaseResolutionStatus.WITHDRAWN),
    ],
)
def test_resolver_maps_each_qualification_status(status, expected):
    registry = AgentRegistry()
    release = agent_registry___single_artifact_release(qualification_status=status)
    registry.register_release(release)
    resolver = RegistryReleaseResolver(registry, platform=("macOS", "aarch64"))

    resolution = resolver.resolve(
        release.agent_id, release_id=release.release_id, version=release.version
    )
    assert resolution.status == expected
    if expected == ReleaseResolutionStatus.RESOLVED:
        assert resolution.artifact_digest == release.artifacts[0].sha256
    else:
        assert resolution.artifact_digest is None


def test_resolver_not_found_and_ambiguous_version():
    registry = AgentRegistry()
    registry.register_release(agent_registry___release())

    resolver = RegistryReleaseResolver(registry)
    missing = resolver.resolve("codex-acp", release_id="missing")
    assert missing.status == ReleaseResolutionStatus.NOT_FOUND

    # Two releases share a version label; resolving by version alone is ambiguous.
    registry.register_release(
        agent_registry___release(
            release_id="rel-codex-1.2.3-alt",
            source_manifest_digest="sha256:" + "9" * 64,
        )
    )
    ambiguous = resolver.resolve("codex-acp", version="1.2.3")
    assert ambiguous.status == ReleaseResolutionStatus.UNQUALIFIED


def test_resolver_treats_latest_as_unqualified():
    registry = AgentRegistry()
    registry.register_release(agent_registry___release())
    resolver = RegistryReleaseResolver(registry)

    resolution = resolver.resolve("codex-acp", version="latest")
    assert resolution.status == ReleaseResolutionStatus.UNQUALIFIED


def test_protocol_validation_accepts_qualified_registry_release():
    registry = AgentRegistry()
    release = agent_registry___single_artifact_release(qualification_status=QualificationStatus.QUALIFIED)
    registry.register_release(release)
    resolver = RegistryReleaseResolver(registry, platform=("macOS", "aarch64"))
    digest = resolver.artifact_digest(release)
    assert digest is not None

    protocol = agent_registry___protocol_referencing(release, digest)
    distribution_resolver = agent_registry___distribution_resolver(release)
    assert (
        validate_protocol(protocol, distribution_resolver=distribution_resolver) == []
    )


def test_protocol_validation_rejects_draft_and_retired_releases():
    for status, expected_code in (
        (QualificationStatus.DRAFT, ValidationReasonCode.DISTRIBUTION_UNVERIFIED),
        (QualificationStatus.RETIRED, ValidationReasonCode.RELEASE_WITHDRAWN),
    ):
        registry = AgentRegistry()
        release = agent_registry___single_artifact_release(qualification_status=status)
        registry.register_release(release)
        protocol = agent_registry___protocol_referencing(
            release, release.artifacts[0].sha256
        )
        distribution_resolver = agent_registry___distribution_resolver(release)

        errors = validate_protocol(
            protocol, distribution_resolver=distribution_resolver
        )
        assert expected_code in agent_registry___codes(errors)


def test_protocol_publication_cannot_use_unqualified_release():
    registry = AgentRegistry()
    draft = agent_registry___single_artifact_release(qualification_status=QualificationStatus.DRAFT)
    registry.register_release(draft)
    protocol = agent_registry___protocol_referencing(draft, draft.artifacts[0].sha256)
    distribution_resolver = agent_registry___distribution_resolver(draft)

    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=distribution_resolver,
        now=agent_registry__NOW,
    )
    assert result.outcome == PublicationOutcome.VALIDATION_FAILED
    assert ValidationReasonCode.DISTRIBUTION_UNVERIFIED in agent_registry___codes(result.errors)


def test_protocol_publication_succeeds_with_qualified_release():
    registry = AgentRegistry()
    release = agent_registry___single_artifact_release(qualification_status=QualificationStatus.QUALIFIED)
    registry.register_release(release)
    protocol = agent_registry___protocol_referencing(release, release.artifacts[0].sha256)
    distribution_resolver = agent_registry___distribution_resolver(release)

    result = publish_revision(
        protocol,
        RevisionLineage(study_id=protocol.study_id),
        distribution_resolver=distribution_resolver,
        actor_is_admin=True,
        now=agent_registry__NOW,
    )
    assert result.outcome == PublicationOutcome.PUBLISHED
    assert result.revision is not None
    assert result.revision.revision_number == 1
    frozen = result.revision.protocol_json["conditions"][0]["resolved_distribution"]
    assert frozen["release_id"] == release.release_id


# ---------------------------------------------------------------------------
# Persistence helpers (no PostgreSQL; session is a MagicMock)
# ---------------------------------------------------------------------------


def test_upsert_release_inserts_release_artifacts_and_adapter():
    release = agent_registry___release()
    session = MagicMock()
    session.get.return_value = None

    row = store_module.upsert_release(session, release)

    added = [call.args[0] for call in session.add.call_args_list]
    release_rows = [item for item in added if hasattr(item, "release_json")]
    assert len(release_rows) == 1
    # Artifacts and the adapter are part of release_json, so no child rows are
    # written.
    assert len(added) == 1
    assert row.release_id == release.release_id
    assert release_rows[0].source_manifest_digest == release.source_manifest_digest
    assert len(release_rows[0].release_json["artifacts"]) == len(release.artifacts)
    assert (
        release_rows[0].release_json["adapter"]["adapter_id"]
        == release.adapter.adapter_id
    )
    session.commit.assert_called_once()
    session.refresh.assert_called_once()


def test_row_to_release_round_trips_release_json():
    release = agent_registry___release()
    row = agent_registry___release_row(release)

    restored = store_module.row_to_release(row)
    assert isinstance(restored, AgentReleaseV1)
    assert restored.digest_identity == release.digest_identity
    assert restored.qualification_status == release.qualification_status
    # No conformance evidence was recorded, so the derived status is UNQUALIFIED.
    assert store_module.release_summary(row)["status"] == "UNQUALIFIED"


def test_insert_snapshot_and_row_to_snapshot_round_trip():
    snapshot = agent_registry___snapshot()
    session = MagicMock()
    session.get.return_value = None

    row = store_module.insert_snapshot(session, snapshot)

    # The snapshot lives on the run that captured it, keyed by snapshot id.
    assert row.agent_run_id == snapshot.snapshot_id
    assert row.snapshot_json["observed"]["USAGE"]["value"] is None
    assert row.snapshot_captured_at == snapshot.captured_at
    session.add.assert_called_once()
    session.commit.assert_called_once()

    restored = store_module.row_to_snapshot(
        SimpleNamespace(snapshot_json=snapshot.model_dump(mode="json"))
    )
    assert isinstance(restored, CapabilitySnapshotV1)
    assert restored.model_dump(mode="json") == snapshot.model_dump(mode="json")


def test_get_and_list_helpers_use_session():
    session = MagicMock()
    session.get.return_value = None
    assert store_module.get_release(session, "missing") is None

    session.execute.return_value.scalars.return_value.all.return_value = []
    assert list(store_module.list_releases(session)) == []
    assert list(store_module.list_snapshots(session, "rel-codex-1.2.3")) == []


# ---------------------------------------------------------------------------
# Router wiring and handlers
# ---------------------------------------------------------------------------


def test_agent_registry_routes_are_wired_under_the_research_prefix():
    from backend.routers import router as api_router

    paths = {route.path for route in api_router.routes}
    assert "/research/agents/releases" in paths
    assert "/research/agents/releases/{release_id}" in paths
    assert "/research/agents/releases/{release_id}/resolve" in paths
    assert "/research/agents/releases/{release_id}/snapshots" in paths
    assert "/research/agents/releases/{release_id}/coverage" in paths
    # The caller-driven qualification transition endpoint has been removed:
    # qualification is derived from conformance evidence.
    assert "/research/agents/releases/{release_id}/transition" not in paths


def test_register_release_requires_admin():
    app = MagicMock()
    payload = ReleaseRegisterRequest(release=agent_registry___release())

    with pytest.raises(HTTPException) as error:
        register_release(payload, agent_registry___non_admin(), app)

    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_list_releases_requires_admin():
    app = MagicMock()
    with pytest.raises(HTTPException) as error:
        list_releases(current_user=agent_registry___non_admin(), app=app)
    assert error.value.status_code == 403
    app.get_db_session.assert_not_called()


def test_register_release_returns_typed_response():
    app = MagicMock()
    release = agent_registry___release()
    fake_row = agent_registry___release_row(release)

    with patch(
        "backend.routers.research.agents.store.list_releases", return_value=[]
    ), patch(
        "backend.routers.research.agents.store.upsert_release",
        return_value=fake_row,
    ) as upsert, patch(
        "backend.routers.research.agents.store.release_summary",
        return_value={"release_id": release.release_id, "status": "QUALIFIED"},
    ):
        response = register_release(
            ReleaseRegisterRequest(release=release), agent_registry___admin(), app
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["accepted"] is True
    assert body["release"]["release_id"] == release.release_id
    upsert.assert_called_once()
    app.get_db_session.return_value.close.assert_called_once()


def test_register_release_rejects_duplicate_digest_identity():
    app = MagicMock()
    release = agent_registry___release()

    with patch(
        "backend.routers.research.agents.store.list_releases",
        return_value=[agent_registry___release_row(release)],
    ), patch(
        "backend.routers.research.agents.store.upsert_release"
    ) as upsert:
        with pytest.raises(HTTPException) as error:
            register_release(ReleaseRegisterRequest(release=release), agent_registry___admin(), app)

    assert error.value.status_code == 409
    assert error.value.detail["code"] == RegistryReasonCode.DUPLICATE_RELEASE.value
    upsert.assert_not_called()


def test_register_release_rejects_caller_supplied_qualification():
    app = MagicMock()
    release = agent_registry___release(
        qualification_status=QualificationStatus.QUALIFIED
    )

    with pytest.raises(HTTPException) as error:
        register_release(
            ReleaseRegisterRequest(release=release), agent_registry___admin(), app
        )

    assert error.value.status_code == 422
    app.get_db_session.assert_not_called()


def test_resolve_endpoint_returns_matching_artifact():
    app = MagicMock()
    release = agent_registry___release()
    fake_row = agent_registry___release_row(release)

    with patch(
        "backend.routers.research.agents.store.get_release", return_value=fake_row
    ):
        response = resolve_artifact(
            release.release_id,
            ResolveArtifactRequest(os="macOS", arch="aarch64"),
            agent_registry___admin(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["resolved"] is True
    assert body["artifact"]["sha256"] == release.artifacts[0].sha256


def agent_registry___byoa_release(**overrides) -> AgentReleaseV1:
    data = {
        "agent_id": "goose",
        "release_id": "rel-byoa",
        "version": "0.9.0",
        "source_manifest_digest": "sha256:" + "2" * 64,
        "distribution_mode": DistributionMode.BYOA_EXTERNAL,
        "agent_command": "goose",
        "agent_command_args": ["acp"],
        "agent_package": "goose",
        "adapter": {
            "adapter_id": "goose-adapter",
            "version": "1.0.0",
            "digest": "sha256:" + "d" * 64,
        },
    }
    data.update(overrides)
    return AgentReleaseV1.model_validate(data)


def test_register_endpoint_accepts_a_byoa_release_without_a_digest():
    app = MagicMock()
    db = MagicMock()
    app.get_db_session.return_value = db
    release = agent_registry___byoa_release()

    with (
        patch("backend.routers.research.agents.store.list_releases", return_value=[]),
        patch(
            "backend.routers.research.agents.store.upsert_release",
            return_value=agent_registry___release_row(release),
        ),
    ):
        response = register_release(
            ReleaseRegisterRequest(release=release), agent_registry___admin(), app
        )

    assert response.status_code == 201
    assert json.loads(response.body)["accepted"] is True


def test_resolve_endpoint_returns_the_byoa_identity_not_an_artifact():
    app = MagicMock()
    release = agent_registry___byoa_release()
    fake_row = agent_registry___release_row(release)

    with patch(
        "backend.routers.research.agents.store.get_release", return_value=fake_row
    ):
        response = resolve_artifact(
            release.release_id,
            ResolveArtifactRequest(os="macOS", arch="aarch64"),
            agent_registry___admin(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["resolved"] is True
    assert body["distribution_mode"] == DistributionMode.BYOA_EXTERNAL.value
    assert body["artifact"] is None
    assert body["agent_command"] == "goose"
    assert body["agent_command_args"] == ["acp"]
    assert body["agent_package"] == "goose"


def test_resolve_endpoint_blocks_unsupported_platform():
    app = MagicMock()
    release = agent_registry___release()
    fake_row = agent_registry___release_row(release)

    with patch(
        "backend.routers.research.agents.store.get_release", return_value=fake_row
    ):
        with pytest.raises(HTTPException) as error:
            resolve_artifact(
                release.release_id,
                ResolveArtifactRequest(os="Windows", arch="x86_64"),
                agent_registry___admin(),
                app,
            )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == RegistryReasonCode.UNSUPPORTED_PLATFORM.value


def test_upload_snapshot_rejects_mismatched_release_id():
    app = MagicMock()
    snapshot = agent_registry___snapshot()

    with pytest.raises(HTTPException) as error:
        upload_snapshot(
            "some-other-release",
            SnapshotUploadRequest(snapshot=snapshot),
            agent_registry___admin(),
            app,
        )

    assert error.value.status_code == 422
    app.get_db_session.assert_not_called()


def test_upload_snapshot_persists_and_returns_summary():
    app = MagicMock()
    snapshot = agent_registry___snapshot()
    release = agent_registry___release()
    fake_row = agent_registry___snapshot_row(snapshot)

    with patch(
        "backend.routers.research.agents.store.get_release",
        return_value=agent_registry___release_row(release),
    ), patch(
        "backend.routers.research.agents.store.insert_snapshot",
        return_value=fake_row,
    ) as insert, patch(
        "backend.routers.research.agents.store.snapshot_summary",
        return_value={"snapshot_id": str(snapshot.snapshot_id)},
    ):
        response = upload_snapshot(
            snapshot.release_id,
            SnapshotUploadRequest(snapshot=snapshot),
            agent_registry___admin(),
            app,
        )

    body = json.loads(response.body)
    assert response.status_code == 201
    assert body["snapshot"]["snapshot_id"] == str(snapshot.snapshot_id)
    insert.assert_called_once()


def test_coverage_endpoint_returns_typed_coverage():
    app = MagicMock()
    release = agent_registry___release()
    snapshot = agent_registry___snapshot()

    with patch(
        "backend.routers.research.agents.store.get_release",
        return_value=agent_registry___release_row(release),
    ), patch(
        "backend.routers.research.agents.store.list_snapshots",
        return_value=[agent_registry___snapshot_row(snapshot)],
    ), patch(
        "backend.routers.research.agents.store.row_to_snapshot",
        return_value=snapshot,
    ):
        response = coverage(release.release_id, None, agent_registry___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["counts"]["UNAVAILABLE"] == 1
    usage = next(
        entry for entry in body["entries"] if entry["capability"] == "USAGE"
    )
    assert usage["observed_state"] == "UNAVAILABLE"
    assert usage["value_present"] is False


def test_get_release_endpoint_returns_model():
    app = MagicMock()
    release = agent_registry___release()

    with patch(
        "backend.routers.research.agents.store.get_release",
        return_value=agent_registry___release_row(release),
    ):
        response = get_release(release.release_id, agent_registry___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["model"]["release_id"] == release.release_id
    assert body["model"]["source_type"] == DistributionSourceType.EXTERNAL_REGISTRY.value


def test_list_snapshots_endpoint_returns_summaries():
    app = MagicMock()
    release = agent_registry___release()
    snapshot = agent_registry___snapshot()

    with patch(
        "backend.routers.research.agents.store.get_release",
        return_value=agent_registry___release_row(release),
    ), patch(
        "backend.routers.research.agents.store.list_snapshots",
        return_value=[agent_registry___snapshot_row(snapshot)],
    ), patch(
        "backend.routers.research.agents.store.snapshot_summary",
        return_value={"snapshot_id": str(snapshot.snapshot_id)},
    ):
        response = list_snapshots(release.release_id, agent_registry___admin(), app)

    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["snapshots"][0]["snapshot_id"] == str(snapshot.snapshot_id)


def test_adapter_reference_is_required_for_qualified_assessment():
    release = agent_registry___release(
        adapter=AdapterRef(adapter_id="a", version="1.0.0", digest=None).model_dump()
    )
    registry = AgentRegistry()
    assessment = registry.assess_qualification(release)
    assert assessment.qualifiable is False
    assert RegistryReasonCode.ADAPTER_INCOMPATIBLE in {
        blocker.code for blocker in assessment.blockers
    }


def test_distribution_artifact_platform_property():
    artifact = DistributionArtifact(
        os="macOS", arch="aarch64", path="a", sha256="sha256:" + "a" * 64, size=1
    )
    assert artifact.platform == ("macOS", "aarch64")
