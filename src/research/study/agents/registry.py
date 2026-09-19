"""Pure agent-registry service: releases, qualification and capability evidence.

This module has no database or application dependency. It operates on the
Pydantic contracts in :mod:`research.study.agents.models` and returns typed results so
an API can render field-level errors and an audit can record why an operation
was blocked.

Key invariants:

* A release's identity is ``(agent_id, release_id, source_manifest_digest)``. A
  changed digest requires a distinct release record; an exact duplicate is
  rejected with ``DUPLICATE_RELEASE``.
* Qualification is **derived** from verified conformance evidence (a passing
  receipt recorded by the packaging layer), never supplied by a caller. There is
  no reviewer transition that can mark a release qualified.
* A receipt binds one *artifact identity*: digest, platform and adapter must all
  belong to the same declared artifact (see :func:`qualified_artifact_keys`).
  A digest from one artifact and a platform from another never qualify either.
* Platform resolution is exact and never falls back to another platform.
* Declared and observed capabilities are stored in separate maps and unknown or
  unavailable measurements stay visible (``value`` stays ``None``).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    CapabilityCoverageState,
    DistributionMode,
    QualificationStatus,
    RegistryReasonCode,
    SnapshotCapabilityState,
)
from .models import (
    AdapterRef,
    AgentReleaseV1,
    CapabilityCoverage,
    CapabilityCoverageReport,
    CapabilityEntry,
    CapabilitySnapshotV1,
    DistributionArtifact,
    EnvironmentRef,
    SnapshotFailure,
    normalize_platform,
)

_BASE_CONFIG = ConfigDict(extra="forbid")

#: Conformance receipt status that backs a derived ``QUALIFIED``.
_PASSED_CONFORMANCE = "PASS"

#: One qualified artifact identity: ``(os, arch, digest, adapter_digest)``.
#:
#: ``os``/``arch`` are normalised by :func:`normalize_platform`; a BYOA identity
#: may carry empty platform values because a participant-installed agent is not
#: platform-pinned. ``digest`` is the release artifact digest for a PACKAGED
#: release and the ``source_manifest_digest`` for a BYOA release.
ArtifactKey = tuple[str, str, str, str]


def _normalize_digest(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text[7:] if text.startswith("sha256:") else text


def _same_digest(left: Any, right: Any) -> bool:
    normalized = _normalize_digest(left)
    return normalized is not None and normalized == _normalize_digest(right)

# Qualification states that may be selected by a study condition.
SELECTABLE_STATUSES = frozenset({QualificationStatus.QUALIFIED})

EntryInput = Union[CapabilityEntry, SnapshotCapabilityState, str, None]


class RegistryIssue(BaseModel):
    """One typed registry rejection reason."""

    model_config = _BASE_CONFIG

    code: RegistryReasonCode
    message: str
    field: str = ""


class RegistrationResult(BaseModel):
    """Outcome of registering a release."""

    model_config = _BASE_CONFIG

    accepted: bool
    release: Optional[AgentReleaseV1] = None
    issue: Optional[RegistryIssue] = None


class ArtifactResolution(BaseModel):
    """Outcome of selecting a distribution artifact for a platform."""

    model_config = _BASE_CONFIG

    resolved: bool
    artifact: Optional[DistributionArtifact] = None
    issue: Optional[RegistryIssue] = None


class DistributionResolution(BaseModel):
    """Outcome of resolving the distribution contract for one platform.

    A ``PACKAGED`` release resolves to a digest-pinned [artifact]; a
    ``BYOA_EXTERNAL`` release resolves to the participant-installable
    command/package identity and carries no artifact.
    """

    model_config = _BASE_CONFIG

    resolved: bool
    distribution_mode: DistributionMode = DistributionMode.PACKAGED
    artifact: Optional[DistributionArtifact] = None
    agent_command: Optional[str] = None
    agent_command_args: list[str] = Field(default_factory=list)
    agent_package: Optional[str] = None
    issue: Optional[RegistryIssue] = None


class QualificationAssessment(BaseModel):
    """Whether a release currently satisfies the qualification contract."""

    model_config = _BASE_CONFIG

    qualifiable: bool
    blockers: list[RegistryIssue] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue(code: RegistryReasonCode, message: str, field: str = "") -> RegistryIssue:
    return RegistryIssue(code=code, message=message, field=field)


def _distribution_issue(release: AgentReleaseV1) -> Optional[RegistryIssue]:
    """Validate the mode-specific distribution contract, or ``None`` if valid.

    ``PACKAGED`` requires at least one artifact with a non-empty digest;
    ``BYOA_EXTERNAL`` requires a command/package identity and is *not* expected
    to carry a digest. The check is shared by registration and qualification so
    a release can never be accepted under one contract and qualified under another.
    """
    if release.is_byoa:
        if release.byoa_identity is None:
            return _issue(
                RegistryReasonCode.AGENT_NOT_FOUND,
                "a BYOA release must declare an agent command or an agent package",
                "agent_command",
            )
        return None

    if not release.artifacts:
        return _issue(
            RegistryReasonCode.ARTIFACT_MISSING,
            "a PACKAGED release must publish at least one distribution artifact",
            "artifacts",
        )
    for index, artifact in enumerate(release.artifacts):
        if not artifact.sha256.strip():
            return _issue(
                RegistryReasonCode.DIGEST_MISMATCH,
                "a PACKAGED distribution artifact must carry a sha256 digest",
                f"artifacts[{index}].sha256",
            )
    return None


def derive_qualification_status(
    release_json: Optional[Mapping[str, Any]],
) -> QualificationStatus:
    """Derive a release's qualification from verified conformance evidence.

    Artifact identity and conformance receipts are owned by the packaging layer
    and persisted alongside the release in ``agent_release.release_json``. A
    release is ``QUALIFIED`` exactly when :func:`qualified_artifact_keys` finds
    at least one artifact identity a passing receipt is bound to. Anything else
    (no receipt, a failing receipt, a receipt bound to a different
    artifact/adapter/platform combination, a passing receipt with no case
    results, or a caller-supplied status) is ``UNQUALIFIED``. A synthetic
    fixture or any unrelated ``PASS`` receipt can therefore never qualify a real
    runtime.
    """
    return (
        QualificationStatus.QUALIFIED
        if qualified_artifact_keys(release_json)
        else QualificationStatus.UNQUALIFIED
    )


def _passing_receipts(release_json: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Conformance receipts that are ``PASS`` and carry a passing case."""
    receipts = release_json.get("conformance") or []
    if not isinstance(receipts, (list, tuple)):
        return []
    return [
        receipt
        for receipt in receipts
        if isinstance(receipt, Mapping)
        and str(receipt.get("status", "")).strip().upper() == _PASSED_CONFORMANCE
        and _receipt_has_passing_case(receipt)
    ]


def _declared_identity_components(
    release_json: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """The artifact-like identities a packaged release declares.

    Release ``artifacts`` are authoritative; for a legacy/package-only release
    the package manifest's components carry the same ``(os, arch, sha256)``
    identity and are used instead. A BYOA release declares none.
    """
    artifacts = [
        item for item in (release_json.get("artifacts") or []) if isinstance(item, Mapping)
    ]
    if artifacts:
        return artifacts
    manifest = release_json.get("package_json")
    if not isinstance(manifest, Mapping):
        return []
    return [
        item for item in (manifest.get("components") or []) if isinstance(item, Mapping)
    ]


def _receipt_binds_to_component(
    receipt: Mapping[str, Any],
    component: Mapping[str, Any],
    declared_adapter_digest: Optional[str],
) -> bool:
    """Whether a PASS receipt is bound to this exact component identity.

    Digest, platform and adapter all have to belong to the *same* declared
    component. A host-less receipt binds by digest/adapter; a receipt that names
    a host must name the component's platform.
    """
    if not _same_digest(component.get("sha256"), receipt.get("artifact_digest")):
        return False
    if declared_adapter_digest is not None and not _same_digest(
        declared_adapter_digest, receipt.get("adapter_digest")
    ):
        return False
    host = receipt.get("host")
    if isinstance(host, Mapping) and host.get("os") and host.get("arch"):
        component_os = component.get("os")
        component_arch = component.get("arch")
        if not component_os or not component_arch:
            return False
        if normalize_platform(str(component_os), str(component_arch)) != normalize_platform(
            str(host.get("os")), str(host.get("arch"))
        ):
            return False
    return True


def _artifact_key(
    os_name: Any, arch: Any, digest: Any, adapter_digest: Any
) -> ArtifactKey:
    canonical_os, canonical_arch = normalize_platform(
        str(os_name or ""), str(arch or "")
    )
    return (
        canonical_os,
        canonical_arch,
        _normalize_digest(digest) or "",
        _normalize_digest(adapter_digest) or "",
    )


def artifact_key(
    os_name: Any, arch: Any, digest: Any, adapter_digest: Any = None
) -> ArtifactKey:
    """Normalise one artifact identity into its comparison key."""
    return _artifact_key(os_name, arch, digest, adapter_digest)


def qualified_artifact_keys(
    release_json: Optional[Mapping[str, Any]],
) -> set[ArtifactKey]:
    """The exact artifact identities a release's evidence qualifies.

    Every key is ``(os, arch, digest, adapter_digest)`` and is only produced
    when one ``PASS`` receipt with a passing case binds digest, platform and
    adapter to the *same* declared component:

    * ``PACKAGED`` releases bind to a declared artifact (or, for a package-only
      release, a package component) by its own ``sha256`` and ``(os, arch)``.
    * ``BYOA_EXTERNAL`` releases have no artifact, so the receipt binds to the
      release's own ``source_manifest_digest``; the host is optional and, when
      absent, both platform values are empty strings.

    An empty set means no platform/artifact combination is qualified.
    """
    if not isinstance(release_json, Mapping):
        return set()
    adapter = release_json.get("adapter")
    declared_adapter_digest = (
        _normalize_digest(adapter.get("digest")) if isinstance(adapter, Mapping) else None
    )
    components = _declared_identity_components(release_json)

    keys: set[ArtifactKey] = set()
    for receipt in _passing_receipts(release_json):
        if components:
            for component in components:
                if not _receipt_binds_to_component(
                    receipt, component, declared_adapter_digest
                ):
                    continue
                keys.add(
                    _artifact_key(
                        component.get("os"),
                        component.get("arch"),
                        component.get("sha256"),
                        declared_adapter_digest or receipt.get("adapter_digest"),
                    )
                )
            continue

        # BYOA_EXTERNAL: the receipt binds the release's own manifest digest and
        # adapter; the recorded host is optional and never platform-pins it.
        if not _same_digest(
            release_json.get("source_manifest_digest"), receipt.get("artifact_digest")
        ):
            continue
        if declared_adapter_digest is not None and not _same_digest(
            declared_adapter_digest, receipt.get("adapter_digest")
        ):
            continue
        host = receipt.get("host")
        host_os = host.get("os") if isinstance(host, Mapping) else None
        host_arch = host.get("arch") if isinstance(host, Mapping) else None
        keys.add(
            _artifact_key(
                host_os,
                host_arch,
                release_json.get("source_manifest_digest"),
                declared_adapter_digest or receipt.get("adapter_digest"),
            )
        )
    return keys


def artifact_qualified(
    release_json: Optional[Mapping[str, Any]],
    *,
    os_name: Any,
    arch: Any,
    digest: Any,
    adapter_digest: Any = None,
) -> bool:
    """Whether the exact platform artifact is covered by passing evidence."""
    return (
        artifact_key(os_name, arch, digest, adapter_digest)
        in qualified_artifact_keys(release_json)
    )


def byoa_identity_qualified(release_json: Optional[Mapping[str, Any]]) -> bool:
    """Whether a BYOA release's manifest digest/adapter is covered by evidence.

    The platform recorded on the receipt is ignored: a participant-installed
    agent is not platform-pinned, so any qualified key for the release's own
    digest (and declared adapter, when one is declared) is sufficient.
    """
    if not isinstance(release_json, Mapping):
        return False
    manifest_digest = _normalize_digest(release_json.get("source_manifest_digest"))
    if not manifest_digest:
        return False
    adapter = release_json.get("adapter")
    declared_adapter_digest = (
        _normalize_digest(adapter.get("digest")) if isinstance(adapter, Mapping) else None
    )
    for key in qualified_artifact_keys(release_json):
        if key[2] != manifest_digest:
            continue
        if declared_adapter_digest is not None and key[3] != declared_adapter_digest:
            continue
        return True
    return False


def _receipt_has_passing_case(receipt: Mapping[str, Any]) -> bool:
    """A PASS receipt with no observed case results is not execution evidence."""
    cases = receipt.get("case_results")
    if not isinstance(cases, (list, tuple)) or not cases:
        return False
    return any(
        isinstance(case, Mapping)
        and str(case.get("status", "")).strip().upper() == _PASSED_CONFORMANCE
        for case in cases
    )


# ---------------------------------------------------------------------------
# Per-release verified approval options (phase 04; folded from approvals.py)
# ---------------------------------------------------------------------------
#
# An agent profile selects an approval policy, but a study may only require an
# option the selected release's conformance evidence actually exercises. The
# baseline ``auto`` policy needs no host permission gate, so it is always
# available. ``per_step`` (ask per turn) requires a passing permission-request
# case; ``suggestion_only`` (propose edits without applying) requires a passing
# edit-proposal/diff case. Absent evidence, the gated options are **not** offered
# — a matching version string is never enough. The ``auto``/``per_step``/
# ``suggestion_only`` vocabulary has one owner: ``backend.routers.agent.profiles``
# validates the same values.

APPROVAL_AUTO = "auto"
APPROVAL_PER_STEP = "per_step"
APPROVAL_SUGGESTION_ONLY = "suggestion_only"

ALL_APPROVAL_OPTIONS: tuple[str, ...] = (
    APPROVAL_AUTO,
    APPROVAL_PER_STEP,
    APPROVAL_SUGGESTION_ONLY,
)

#: Substrings of a passing conformance case id that verify each gated option.
_PER_STEP_CASE_HINTS = ("permission",)
_SUGGESTION_CASE_HINTS = ("edit", "suggestion", "diff")


def _passed_approval_case_ids(release_json: Optional[Mapping[str, Any]]) -> set[str]:
    if not isinstance(release_json, Mapping):
        return set()
    receipts = release_json.get("conformance") or []
    if not isinstance(receipts, (list, tuple)):
        return set()
    passed: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        if str(receipt.get("status", "")).strip().upper() != _PASSED_CONFORMANCE:
            continue
        cases = receipt.get("case_results") or []
        if not isinstance(cases, (list, tuple)):
            continue
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            if str(case.get("status", "")).strip().upper() != _PASSED_CONFORMANCE:
                continue
            case_id = str(case.get("case_id", "")).strip()
            if case_id:
                passed.add(case_id)
    return passed


def verified_approval_options(release_json: Optional[Mapping[str, Any]]) -> list[str]:
    """Return the approval options the release's evidence verifies.

    Always includes ``auto``. ``per_step`` and ``suggestion_only`` are included
    only when a passing conformance case exercises the corresponding host
    behaviour, so an unsupported option is never offered to the editor.
    """
    passed = {case_id.lower() for case_id in _passed_approval_case_ids(release_json)}
    options = [APPROVAL_AUTO]
    if any(hint in case_id for case_id in passed for hint in _PER_STEP_CASE_HINTS):
        options.append(APPROVAL_PER_STEP)
    if any(hint in case_id for case_id in passed for hint in _SUGGESTION_CASE_HINTS):
        options.append(APPROVAL_SUGGESTION_ONLY)
    return options


def approval_option_verified(
    release_json: Optional[Mapping[str, Any]], option: str
) -> bool:
    """Whether ``option`` is verified for the release described by ``release_json``."""
    return (option or "").strip().lower() in verified_approval_options(release_json)


def _coerce_entry(value: EntryInput, default_state: SnapshotCapabilityState) -> CapabilityEntry:
    """Normalize a capability map value into a :class:`CapabilityEntry`.

    ``None`` becomes an entry with the default state and ``value=None``: a
    missing measurement is never turned into ``0`` or ``False``.
    """
    if isinstance(value, CapabilityEntry):
        return value
    if isinstance(value, SnapshotCapabilityState):
        return CapabilityEntry(state=value, value=None)
    if isinstance(value, str):
        try:
            state = SnapshotCapabilityState(value.strip().upper())
        except ValueError:
            state = SnapshotCapabilityState.UNKNOWN
        return CapabilityEntry(state=state, value=None)
    if value is None:
        return CapabilityEntry(state=default_state, value=None)
    return CapabilityEntry(state=default_state, value=value)


def build_capability_snapshot(
    snapshot_id: UUID,
    release: AgentReleaseV1,
    *,
    protocol_version: str,
    declared: Mapping[str, EntryInput],
    observed: Mapping[str, EntryInput],
    environment: Optional[EnvironmentRef] = None,
    adapter_id: Optional[str] = None,
    adapter_version: Optional[str] = None,
    evidence_refs: Optional[list[str]] = None,
    captured_at: Optional[datetime] = None,
    failure: Optional[SnapshotFailure] = None,
) -> CapabilitySnapshotV1:
    """Build an immutable snapshot from a declared/observed observation pair.

    The two maps are preserved independently: a capability present only in
    ``declared`` is not synthesized into ``observed``, and vice versa.
    """
    return CapabilitySnapshotV1(
        snapshot_id=snapshot_id,
        release_id=release.release_id,
        agent_id=release.agent_id,
        adapter_id=adapter_id or (release.adapter.adapter_id if release.adapter else None),
        adapter_version=adapter_version
        or (release.adapter.version if release.adapter else None),
        environment=environment or EnvironmentRef(),
        protocol_version=protocol_version,
        declared={
            capability: _coerce_entry(value, SnapshotCapabilityState.DECLARED)
            for capability, value in declared.items()
        },
        observed={
            capability: _coerce_entry(value, SnapshotCapabilityState.UNKNOWN)
            for capability, value in observed.items()
        },
        evidence_refs=list(evidence_refs or []),
        captured_at=captured_at or _now(),
        failure=failure,
    )


def _coverage_state(
    declared_state: SnapshotCapabilityState, observed_state: SnapshotCapabilityState
) -> CapabilityCoverageState:
    if (
        declared_state == SnapshotCapabilityState.BROKEN
        or observed_state == SnapshotCapabilityState.BROKEN
    ):
        return CapabilityCoverageState.BROKEN
    if observed_state == SnapshotCapabilityState.PARTIAL:
        return CapabilityCoverageState.PARTIAL
    if observed_state == SnapshotCapabilityState.UNAVAILABLE:
        return CapabilityCoverageState.UNAVAILABLE
    if observed_state == SnapshotCapabilityState.OBSERVED:
        return CapabilityCoverageState.OBSERVED
    if declared_state in (
        SnapshotCapabilityState.DECLARED,
        SnapshotCapabilityState.OBSERVED,
    ):
        return CapabilityCoverageState.DECLARED_ONLY
    return CapabilityCoverageState.UNKNOWN


def capability_coverage(snapshot: CapabilitySnapshotV1) -> list[CapabilityCoverage]:
    """Summarize declared vs observed state per capability (never a boolean)."""
    capabilities = sorted(set(snapshot.declared) | set(snapshot.observed))
    entries: list[CapabilityCoverage] = []
    for capability in capabilities:
        declared = snapshot.declared.get(capability)
        observed = snapshot.observed.get(capability)
        declared_state = (
            declared.state if declared else SnapshotCapabilityState.UNKNOWN
        )
        observed_state = (
            observed.state if observed else SnapshotCapabilityState.UNKNOWN
        )
        limitations: list[str] = []
        if declared:
            limitations.extend(declared.limitations)
        if observed:
            limitations.extend(observed.limitations)
        entries.append(
            CapabilityCoverage(
                capability=capability,
                declared_state=declared_state,
                observed_state=observed_state,
                coverage=_coverage_state(declared_state, observed_state),
                value_present=observed is not None and observed.value is not None,
                limitations=limitations,
            )
        )
    return entries


def coverage_report(snapshot: CapabilitySnapshotV1) -> CapabilityCoverageReport:
    """Build the coverage report (entries + explicit state counts)."""
    entries = capability_coverage(snapshot)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.coverage.value] = counts.get(entry.coverage.value, 0) + 1
    return CapabilityCoverageReport(
        snapshot_id=snapshot.snapshot_id,
        release_id=snapshot.release_id,
        entries=entries,
        counts=counts,
    )


class AgentRegistry:
    """In-memory registry of releases and capability snapshots.

    The service is deliberately storage-agnostic. The API layer loads rows from
    the database into a registry instance (or wraps it in
    :class:`research.study.agents.resolver.RegistryReleaseResolver`) and persists the
    results through :mod:`research.study.agents.store`.
    """

    def __init__(self) -> None:
        self._by_identity: dict[tuple[str, str, str], AgentReleaseV1] = {}
        self._by_release_id: dict[tuple[str, str], AgentReleaseV1] = {}
        self._by_version: dict[tuple[str, str], list[AgentReleaseV1]] = {}
        self._snapshots: dict[UUID, CapabilitySnapshotV1] = {}

    # -- releases ---------------------------------------------------------

    def register_release(self, release: AgentReleaseV1) -> RegistrationResult:
        """Register a release, rejecting duplicate digest identities."""
        if not release.source_manifest_digest.strip():
            return RegistrationResult(
                accepted=False,
                issue=_issue(
                    RegistryReasonCode.DIGEST_MISMATCH,
                    "source_manifest_digest must be non-empty",
                    "source_manifest_digest",
                ),
            )

        distribution_issue = _distribution_issue(release)
        if distribution_issue is not None:
            return RegistrationResult(accepted=False, issue=distribution_issue)

        identity = release.digest_identity
        if identity in self._by_identity:
            return RegistrationResult(
                accepted=False,
                issue=_issue(
                    RegistryReasonCode.DUPLICATE_RELEASE,
                    "an identical release digest already exists",
                    "source_manifest_digest",
                ),
            )

        key = (release.agent_id, release.release_id)
        existing = self._by_release_id.get(key)
        if existing is not None:
            return RegistrationResult(
                accepted=False,
                issue=_issue(
                    RegistryReasonCode.DUPLICATE_RELEASE,
                    (
                        "release_id is already registered for this agent with a "
                        "different digest; a changed digest requires a new release_id"
                    ),
                    "release_id",
                ),
            )

        stored = release.model_copy(deep=True)
        self._by_identity[identity] = stored
        self._by_release_id[key] = stored
        self._by_version.setdefault((release.agent_id, release.version), []).append(
            stored
        )
        return RegistrationResult(accepted=True, release=stored.model_copy(deep=True))

    def get_release(
        self, agent_id: str, release_id: str
    ) -> Optional[AgentReleaseV1]:
        """Fetch a release by its natural ``(agent_id, release_id)`` key."""
        return self._by_release_id.get((agent_id, release_id))

    def find_release(
        self,
        agent_id: str,
        *,
        release_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Optional[AgentReleaseV1]:
        """Resolve a release by explicit id, or by a unique version label."""
        if release_id:
            return self._by_release_id.get((agent_id, release_id))
        if version:
            candidates = self._by_version.get((agent_id, version), [])
            if len(candidates) == 1:
                return candidates[0]
        return None

    def version_candidates(
        self, agent_id: str, version: str
    ) -> list[AgentReleaseV1]:
        """All releases registered for ``(agent_id, version)``."""
        return list(self._by_version.get((agent_id, version), []))

    def list_releases(self, agent_id: Optional[str] = None) -> list[AgentReleaseV1]:
        """List releases, optionally filtered by agent id, in a stable order."""
        releases = list(self._by_release_id.values())
        if agent_id is not None:
            releases = [release for release in releases if release.agent_id == agent_id]
        return sorted(
            releases,
            key=lambda release: (release.agent_id, release.version, release.release_id),
        )

    # -- platform selection / qualification ------------------------------

    def resolve_artifact(
        self, release: AgentReleaseV1, os_name: str, arch: str
    ) -> ArtifactResolution:
        """Return the exact artifact for ``(os_name, arch)`` with no fallback."""
        if not release.artifacts:
            return ArtifactResolution(
                resolved=False,
                issue=_issue(
                    RegistryReasonCode.ARTIFACT_MISSING,
                    "release publishes no distribution artifacts",
                    "artifacts",
                ),
            )

        artifact = release.artifact_for(os_name, arch)
        if artifact is None:
            return ArtifactResolution(
                resolved=False,
                issue=_issue(
                    RegistryReasonCode.UNSUPPORTED_PLATFORM,
                    f"no artifact for platform ({os_name}, {arch})",
                    "artifacts",
                ),
            )
        if not artifact.sha256.strip():
            return ArtifactResolution(
                resolved=False,
                issue=_issue(
                    RegistryReasonCode.DIGEST_MISMATCH,
                    f"artifact for ({os_name}, {arch}) has no digest",
                    "artifacts",
                ),
            )
        return ArtifactResolution(resolved=True, artifact=artifact)

    def resolve_distribution(
        self, release: AgentReleaseV1, os_name: str, arch: str
    ) -> DistributionResolution:
        """Resolve the mode-specific distribution contract with no fallback.

        A ``BYOA_EXTERNAL`` release resolves to its command/package identity (no
        artifact); a ``PACKAGED`` release delegates to :meth:`resolve_artifact` so
        a missing/undigested artifact still blocks.
        """
        if release.is_byoa:
            if release.byoa_identity is None:
                return DistributionResolution(
                    resolved=False,
                    distribution_mode=DistributionMode.BYOA_EXTERNAL,
                    issue=_issue(
                        RegistryReasonCode.AGENT_NOT_FOUND,
                        "BYOA release declares neither a command nor an agent package",
                        "agent_command",
                    ),
                )
            return DistributionResolution(
                resolved=True,
                distribution_mode=DistributionMode.BYOA_EXTERNAL,
                agent_command=release.agent_command,
                agent_command_args=list(release.agent_command_args),
                agent_package=release.agent_package,
            )

        artifact_resolution = self.resolve_artifact(release, os_name, arch)
        if not artifact_resolution.resolved:
            return DistributionResolution(
                resolved=False,
                distribution_mode=DistributionMode.PACKAGED,
                issue=artifact_resolution.issue,
            )
        return DistributionResolution(
            resolved=True,
            distribution_mode=DistributionMode.PACKAGED,
            artifact=artifact_resolution.artifact,
        )

    def assess_qualification(
        self, release: AgentReleaseV1
    ) -> QualificationAssessment:
        """Return the blockers preventing a release from being qualified."""
        blockers: list[RegistryIssue] = []

        distribution_issue = _distribution_issue(release)
        if distribution_issue is not None:
            blockers.append(distribution_issue)
        elif not release.is_byoa:
            for index, artifact in enumerate(release.artifacts):
                if not artifact.path.strip():
                    blockers.append(
                        _issue(
                            RegistryReasonCode.ARTIFACT_MISSING,
                            "distribution artifact path must be non-empty",
                            f"artifacts[{index}].path",
                        )
                    )

        adapter: Optional[AdapterRef] = release.adapter
        if adapter is None:
            blockers.append(
                _issue(
                    RegistryReasonCode.ADAPTER_INCOMPATIBLE,
                    "a qualified release must declare an adapter",
                    "adapter",
                )
            )
        else:
            if not adapter.version.strip():
                blockers.append(
                    _issue(
                        RegistryReasonCode.ADAPTER_INCOMPATIBLE,
                        "adapter version must be non-empty",
                        "adapter.version",
                    )
                )
            if not (adapter.digest or "").strip():
                blockers.append(
                    _issue(
                        RegistryReasonCode.ADAPTER_INCOMPATIBLE,
                        "adapter digest must be non-empty",
                        "adapter.digest",
                    )
                )

        minimum = release.min_protocol_version
        maximum = release.max_protocol_version
        if minimum and maximum and _version_tuple(minimum) > _version_tuple(maximum):
            blockers.append(
                _issue(
                    RegistryReasonCode.INVALID_VERSION_RANGE,
                    f"max_protocol_version {maximum!r} is below {minimum!r}",
                    "max_protocol_version",
                )
            )

        return QualificationAssessment(qualifiable=not blockers, blockers=blockers)

    # -- snapshots --------------------------------------------------------

    def record_snapshot(
        self,
        release: AgentReleaseV1,
        *,
        protocol_version: str,
        declared: Mapping[str, EntryInput],
        observed: Mapping[str, EntryInput],
        environment: Optional[EnvironmentRef] = None,
        adapter_id: Optional[str] = None,
        adapter_version: Optional[str] = None,
        evidence_refs: Optional[list[str]] = None,
        captured_at: Optional[datetime] = None,
        failure: Optional[SnapshotFailure] = None,
    ) -> CapabilitySnapshotV1:
        """Build and store an immutable capability snapshot for ``release``."""
        snapshot = build_capability_snapshot(
            uuid.uuid4(),
            release,
            protocol_version=protocol_version,
            declared=declared,
            observed=observed,
            environment=environment,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            evidence_refs=evidence_refs,
            captured_at=captured_at,
            failure=failure,
        )
        self.add_snapshot(snapshot)
        return snapshot

    def add_snapshot(self, snapshot: CapabilitySnapshotV1) -> CapabilitySnapshotV1:
        """Store an already-built snapshot (e.g. rehydrated from storage)."""
        self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    def get_snapshot(self, snapshot_id: UUID) -> Optional[CapabilitySnapshotV1]:
        """Fetch a snapshot by id, or ``None``."""
        return self._snapshots.get(snapshot_id)

    def list_snapshots(
        self, release_id: Optional[str] = None
    ) -> list[CapabilitySnapshotV1]:
        """List snapshots, optionally for one release, oldest-first."""
        snapshots = list(self._snapshots.values())
        if release_id is not None:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if snapshot.release_id == release_id
            ]
        return sorted(snapshots, key=lambda snapshot: snapshot.captured_at)

    def coverage(self, snapshot_id: UUID) -> Optional[CapabilityCoverageReport]:
        """Coverage report for one stored snapshot, or ``None`` if unknown."""
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return None
        return coverage_report(snapshot)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))
