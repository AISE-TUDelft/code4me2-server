"""Pure agent-registry service: releases, qualification and capability evidence.

This module has no database or application dependency. It operates on the
Pydantic contracts in :mod:`research.study.agents.models` and returns typed results so
an API can render field-level errors and an audit can record why an operation
was blocked.

Key invariants:

* A release's identity is ``(agent_id, release_id, source_manifest_digest)``. A
  changed digest requires a distinct release record; an exact duplicate is
  rejected with ``DUPLICATE_RELEASE``.
* Qualification is **derived** from the imported recipe's self-check verdict
  (``tests.status == "PASS"``), never supplied by a caller. There is no separate
  approval or conformance-receipt step; an administrator may only disable a
  release one-way.
* Artifact identity is the ZIP fingerprint (``sha256``) plus the adapter name,
  never an extracted-file inventory (see :func:`qualified_artifact_keys`).
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

#: Recipe self-check status that backs a derived ``QUALIFIED``.
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
    """Derive a release's usability from the imported recipe's self-check.

    There is no separate approval or conformance-receipt step: the producer
    (the participant-release CLI / CI) runs the agent self-check and writes the
    result into the single recipe document, and the import records that verdict
    on the release. A release is ``QUALIFIED`` (usable) exactly when its stored
    recipe declares ``tests.status == "PASS"``. An administrator may disable a
    release one-way, which is ``DISABLED`` and terminal. Anything else is
    ``UNQUALIFIED``: a caller-supplied status or an unrelated status can never
    make a release usable.
    """
    if not isinstance(release_json, Mapping):
        return QualificationStatus.UNQUALIFIED
    if release_json.get("disabled") is True:
        return QualificationStatus.DISABLED
    if _recipe_tests_passed(release_json):
        return QualificationStatus.QUALIFIED
    return QualificationStatus.UNQUALIFIED


def _recipe_tests(release_json: Mapping[str, Any]) -> Mapping[str, Any]:
    tests = release_json.get("tests")
    return tests if isinstance(tests, Mapping) else {}


def _recipe_tests_passed(release_json: Mapping[str, Any]) -> bool:
    return (
        str(_recipe_tests(release_json).get("status", "")).strip().upper()
        == _PASSED_CONFORMANCE
    )


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
    """The exact artifact identities a usable release pins.

    Every key is ``(os, arch, digest, adapter_digest)``. Identity is the ZIP
    fingerprint (``sha256``) plus the adapter name; there is no separate
    extracted-file inventory. A release that is not ``QUALIFIED`` pins nothing.

    * ``PACKAGED`` releases pin each declared platform artifact by its own
      ``sha256`` and ``(os, arch)``.
    * ``BYOA_EXTERNAL`` releases have no artifact, so the identity is the
      release's own ``source_manifest_digest``; both platform values are empty.
    """
    if derive_qualification_status(release_json) is not QualificationStatus.QUALIFIED:
        return set()
    adapter = release_json.get("adapter")
    declared_adapter_digest = (
        _normalize_digest(adapter.get("digest")) if isinstance(adapter, Mapping) else None
    )
    components = _declared_identity_components(release_json)
    if components:
        return {
            _artifact_key(
                component.get("os"),
                component.get("arch"),
                component.get("sha256"),
                declared_adapter_digest,
            )
            for component in components
        }
    return {
        _artifact_key(
            None,
            None,
            release_json.get("source_manifest_digest"),
            declared_adapter_digest,
        )
    }


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
    """Whether a BYOA release's own manifest identity is usable.

    A participant-installed agent has no platform artifact, so it is usable when
    the release is ``QUALIFIED`` (its recipe self-check passed) and it declares
    no packaged artifacts.
    """
    if not isinstance(release_json, Mapping):
        return False
    if derive_qualification_status(release_json) is not QualificationStatus.QUALIFIED:
        return False
    return not _declared_identity_components(release_json)


# ---------------------------------------------------------------------------
# Per-release approval options (phase 04; folded from approvals.py)
# ---------------------------------------------------------------------------
#
# An agent profile selects an approval policy. The producer's recipe declares
# which options its self-check actually exercised; the editor must not offer an
# option the recipe does not declare. The baseline ``auto`` policy needs no host
# permission gate, so it is always available. The ``auto``/``per_step``/
# ``suggestion_only`` vocabulary has one owner:
# ``backend.routers.agent.profiles`` validates the same values.

APPROVAL_AUTO = "auto"
APPROVAL_PER_STEP = "per_step"
APPROVAL_SUGGESTION_ONLY = "suggestion_only"

ALL_APPROVAL_OPTIONS: tuple[str, ...] = (
    APPROVAL_AUTO,
    APPROVAL_PER_STEP,
    APPROVAL_SUGGESTION_ONLY,
)


def _declared_approval_options(
    release_json: Optional[Mapping[str, Any]],
) -> set[str]:
    if not isinstance(release_json, Mapping):
        return set()
    declared = release_json.get("approval_options")
    if declared is None:
        declared = _recipe_tests(release_json).get("approval_options")
    if not isinstance(declared, (list, tuple)):
        return set()
    return {
        str(option).strip().lower()
        for option in declared
        if str(option).strip().lower() in ALL_APPROVAL_OPTIONS
    }


def verified_approval_options(release_json: Optional[Mapping[str, Any]]) -> list[str]:
    """Return the approval options the release's recipe verifies.

    Always includes ``auto``. ``per_step`` and ``suggestion_only`` are included
    only when the recipe declares the corresponding option as exercised, so an
    unsupported option is never offered to the editor.
    """
    declared = _declared_approval_options(release_json)
    declared.add(APPROVAL_AUTO)
    return [option for option in ALL_APPROVAL_OPTIONS if option in declared]


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
