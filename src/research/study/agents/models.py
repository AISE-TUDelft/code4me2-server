"""Pydantic v2 contracts for the agent registry and capability contract.

Two documents are defined:

* :class:`AgentReleaseV1` - the immutable distribution/release identity a study
  condition pins. Its digest identity is
  ``(agent_id, release_id, source_manifest_digest)``: the same agent version
  with a different source manifest digest is a *distinct* release, never an
  overwrite.
* :class:`CapabilitySnapshotV1` - per-run declared-vs-observed capability
  evidence. ``declared`` and ``observed`` are kept in separate maps so a
  declared capability that was never observed stays ``DECLARED`` with an
  explicit ``UNAVAILABLE``/``UNKNOWN`` observation. A missing measurement is
  ``value is None`` plus a coverage state, never ``0``, ``False`` or success.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Literal, Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research.compatibility.enums import Fidelity

from .enums import (
    CapabilityCoverageState,
    DistributionMode,
    DistributionSourceType,
    QualificationStatus,
    SnapshotCapabilityState,
)

#: Frozen profile fields a BYOA release may declare a translation for. A field
#: the profile actually sets must be covered by a binding (ISSUE-03 Path A).
BYOA_CONFIG_FIELDS = (
    "model",
    "temperature",
    "max_steps",
    "tools",
    "approval_policy",
)
BYOA_CONFIG_TRANSPORTS = ("env", "arg")
BYOA_CONFIG_FORMATS = ("string", "json", "csv")

_BASE_CONFIG = ConfigDict(extra="forbid")
_FROZEN_CONFIG = ConfigDict(extra="forbid", frozen=True)

_AARCH64_ARCHS = frozenset({"aarch64", "arm64"})
_X64_ARCHS = frozenset({"x86_64", "amd64", "x64"})


def normalize_platform(os_name: str, arch: str) -> tuple[str, str]:
    """Canonicalise an ``(os, arch)`` pair for platform comparison.

    Hosts report the same platform under different names (``Darwin`` vs
    ``macos``, ``AMD64`` vs ``x64``), so the requested tuple and every stored
    artifact tuple are normalised before comparison instead of blocking on a
    cosmetic string mismatch.
    """
    lowered_os = (os_name or "").lower()
    if "mac" in lowered_os or lowered_os == "darwin":
        canonical_os = "macos"
    elif lowered_os.startswith("win"):
        canonical_os = "windows"
    elif "linux" in lowered_os:
        canonical_os = "linux"
    else:
        canonical_os = lowered_os

    lowered_arch = (arch or "").lower()
    if lowered_arch in _AARCH64_ARCHS:
        canonical_arch = "aarch64"
    elif lowered_arch in _X64_ARCHS:
        canonical_arch = "x64"
    else:
        canonical_arch = lowered_arch

    return (canonical_os, canonical_arch)


class ReleaseDisplay(BaseModel):
    """Human-facing, non-secret release metadata."""

    model_config = _BASE_CONFIG

    name: Optional[str] = None
    vendor: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None


class ExecutionFile(BaseModel):
    """One member of a packaged runtime, relative to its extraction root."""

    model_config = _BASE_CONFIG
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    executable: bool = False

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (not value or path.is_absolute() or str(path) != value
                or ".." in path.parts or "\\" in value or ":" in value
                or any(ord(c) < 32 for c in value)):
            raise ValueError("execution paths must be normalized relative paths")
        return value


class PackagedExecution(BaseModel):
    """Launch identity, distinct from the enclosing transport archive identity.

    The canonical manifest binds argv and the complete extracted inventory.
    Historical artifacts omit this field; their archive hashes retain meaning.
    """

    model_config = _BASE_CONFIG
    schema_version: Literal["1"] = "1"
    entrypoint: list[str] = Field(min_length=1)
    files: list[ExecutionFile] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_entrypoint(self) -> PackagedExecution:
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate execution file")
        if paths != sorted(paths):
            raise ValueError("execution files must be sorted by path")
        if self.entrypoint[0] not in paths:
            raise ValueError("execution entrypoint must be in the inventory")
        if not next(item for item in self.files if item.path == self.entrypoint[0]).executable:
            raise ValueError("execution entrypoint must be executable")
        return self

    @property
    def executable_sha256(self) -> str:
        return next(item.sha256 for item in self.files if item.path == self.entrypoint[0])

    @property
    def manifest_digest(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


class DistributionArtifact(BaseModel):
    """One platform-specific distribution artifact of a release.

    ``path`` is deliberately *relative*: releases must never carry a local
    absolute path or host-specific mutable registry configuration.
    """

    model_config = _BASE_CONFIG

    os: str
    arch: str
    path: str
    sha256: str
    size: int
    executable: Optional[str] = None
    license: Optional[str] = None
    license_review: Optional[str] = None
    signature: Optional[str] = None
    # sha256/path/size continue to identify the archive, never its entrypoint.
    execution: Optional[PackagedExecution] = None

    @property
    def platform(self) -> tuple[str, str]:
        """The ``(os, arch)`` platform this artifact targets."""
        return (self.os, self.arch)


class AdapterRef(BaseModel):
    """Identity of the adapter/packaging layer that drives one release."""

    model_config = _BASE_CONFIG

    adapter_id: str
    version: str
    digest: Optional[str] = None
    # Declared ranges (e.g. ``">=1.2.0,<2.0.0"``) the adapter claims to drive.
    supported_release_ranges: list[str] = Field(default_factory=list)


class AgentConfigBinding(BaseModel):
    """One declared translation of a frozen profile field for a BYOA release.

    The release owns the external agent's configuration vocabulary, so it
    declares how each frozen profile field reaches the process at launch:

    * ``transport="env"`` sets the environment variable ``key``;
    * ``transport="arg"`` appends ``[key, value]`` to the agent argv.

    ``format`` renders list values (``tools``) as ``csv`` or ``json``;
    ``value_map`` translates the server-side vocabulary to the agent's (for
    example ``per_step -> on-request``). A binding whose transport is not
    executable fails closed at profile/study creation; it is never silently
    ignored.
    """

    model_config = _BASE_CONFIG

    field: str
    transport: Literal["env", "arg"]
    key: str
    format: Literal["string", "json", "csv"] = "string"
    value_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("field")
    @classmethod
    def _known_field(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in BYOA_CONFIG_FIELDS:
            raise ValueError("field must be one of " + ", ".join(BYOA_CONFIG_FIELDS))
        return normalized

    @field_validator("key")
    @classmethod
    def _non_blank_key(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("key must not be blank")
        if "=" in normalized or any(
            ord(char) < 32 or ord(char) == 127 for char in normalized
        ):
            # ``--agent-env`` is a KEY=VALUE token: a key containing '=' or a
            # control character would silently mis-bind or corrupt the launch
            # argv.
            raise ValueError("key must not contain '=' or control characters")
        return normalized

    @model_validator(mode="after")
    def _transport_key_shape(self) -> AgentConfigBinding:
        if self.transport == "env" and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.key
        ):
            raise ValueError(
                "an env binding key must be a valid environment variable name"
            )
        return self


class AgentReleaseV1(BaseModel):
    """An immutable, digest-pinned agent release record (schema version 1).

    ``distribution_mode`` selects the distribution contract: ``PACKAGED``
    (default) requires at least one digest-pinned artifact, while
    ``BYOA_EXTERNAL`` (participant-installed) requires a command/package identity
    and carries no artifact digest. The mode is additive so already-stored
    releases without the field continue to parse as ``PACKAGED``.
    """

    model_config = _BASE_CONFIG

    schema_version: str = "1"
    agent_id: str
    # Stable, unique release identifier. It is part of the digest identity and
    # must be changed when the upstream manifest digest changes.
    release_id: str
    version: str
    display: ReleaseDisplay = Field(default_factory=ReleaseDisplay)
    source_type: DistributionSourceType = DistributionSourceType.RESEARCH_OVERLAY
    source_manifest_digest: str
    distribution_mode: DistributionMode = DistributionMode.PACKAGED
    artifacts: list[DistributionArtifact] = Field(default_factory=list)
    # BYOA only: the explicit participant-installed agent command (an executable
    # name or a portable relative reference; never a local absolute path). A
    # participant host may override it with a configured path.
    agent_command: Optional[str] = None
    agent_command_args: list[str] = Field(default_factory=list)
    # BYOA only: the logical package (``goose``/``codex``/...) used for discovery
    # when no explicit command is pinned.
    agent_package: Optional[str] = None
    # BYOA only: the declared translation of frozen profile fields into the
    # external agent's configuration (ISSUE-03 Path A). Empty means the release
    # cannot honor any profile field, so a profile that sets one is rejected.
    byoa_config: list[AgentConfigBinding] = Field(default_factory=list)
    adapter: Optional[AdapterRef] = None
    # Negotiated ACP protocol range the release is compatible with.
    min_protocol_version: Optional[str] = None
    max_protocol_version: Optional[str] = None
    # Derived server-side from verified conformance evidence
    # (``release_json.conformance[]``); callers can never supply it. Absent a
    # passing receipt a release is ``UNQUALIFIED``.
    qualification_status: QualificationStatus = QualificationStatus.UNQUALIFIED
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _unique_config_bindings(self) -> AgentReleaseV1:
        seen: set[str] = set()
        for binding in self.byoa_config:
            if binding.field in seen:
                raise ValueError(
                    f"duplicate byoa_config binding for {binding.field!r}"
                )
            seen.add(binding.field)
        if self.byoa_config and not self.is_byoa:
            raise ValueError("byoa_config is only valid for a BYOA_EXTERNAL release")
        return self

    @property
    def digest_identity(self) -> tuple[str, str, str]:
        """The identity that makes two release records the same release."""
        return (self.agent_id, self.release_id, self.source_manifest_digest)

    @property
    def is_byoa(self) -> bool:
        """True when the agent is participant-installed rather than packaged."""
        return self.distribution_mode == DistributionMode.BYOA_EXTERNAL

    @property
    def byoa_identity(self) -> Optional[str]:
        """The BYOA command/package identity, or ``None`` for a packaged release."""
        if not self.is_byoa:
            return None
        return (
            (self.agent_command or "").strip()
            or (self.agent_package or "").strip()
            or None
        )

    def artifact_for(self, os_name: str, arch: str) -> Optional[DistributionArtifact]:
        """Return the exact artifact for ``(os_name, arch)`` or ``None``.

        There is intentionally no fallback: an unavailable platform must block
        before download/launch rather than silently selecting another artifact.
        """
        requested = normalize_platform(os_name, arch)
        for artifact in self.artifacts:
            if normalize_platform(artifact.os, artifact.arch) == requested:
                return artifact
        return None


class CapabilityEntry(BaseModel):
    """One capability measurement or declaration.

    ``value`` is nullable and is *not* defaulted: a missing measurement is
    ``None`` and its meaning is carried by ``state``, the fidelity and the
    limitations. It is never coerced to ``0``, ``False`` or success.
    """

    model_config = _FROZEN_CONFIG

    state: SnapshotCapabilityState
    value: Optional[Any] = None
    fidelity: Fidelity = Fidelity.NORMALIZED
    source: str = "handshake"
    limitations: list[str] = Field(default_factory=list)
    observed_at: Optional[datetime] = None


class EnvironmentRef(BaseModel):
    """Opaque reference to the environment a snapshot was captured against."""

    model_config = _BASE_CONFIG

    environment_id: Optional[str] = None
    host_kind: Optional[str] = None
    os: Optional[str] = None
    arch: Optional[str] = None
    ide_build: Optional[str] = None
    plugin_version: Optional[str] = None


class SnapshotFailure(BaseModel):
    """Failure captured while producing a snapshot, if any."""

    model_config = _BASE_CONFIG

    reason: str
    detail: Optional[str] = None
    occurred_at: Optional[datetime] = None


class CapabilitySnapshotV1(BaseModel):
    """Immutable declared-vs-observed capability snapshot (schema version 1)."""

    model_config = _FROZEN_CONFIG

    schema_version: str = "1"
    snapshot_id: UUID
    release_id: str
    agent_id: str
    adapter_id: Optional[str] = None
    adapter_version: Optional[str] = None
    environment: EnvironmentRef = Field(default_factory=EnvironmentRef)
    protocol_version: str
    # Declared and observed are separate maps keyed by capability name. Unknown
    # capability names are preserved rather than dropped.
    declared: dict[str, CapabilityEntry] = Field(default_factory=dict)
    observed: dict[str, CapabilityEntry] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    captured_at: datetime
    failure: Optional[SnapshotFailure] = None


class CapabilityCoverage(BaseModel):
    """Declared-vs-observed coverage for one capability.

    Both sides keep their explicit state (including ``UNKNOWN`` and
    ``UNAVAILABLE``); the summary never collapses them into a boolean.
    """

    model_config = _BASE_CONFIG

    capability: str
    declared_state: SnapshotCapabilityState = SnapshotCapabilityState.UNKNOWN
    observed_state: SnapshotCapabilityState = SnapshotCapabilityState.UNKNOWN
    coverage: CapabilityCoverageState
    value_present: bool = False
    limitations: list[str] = Field(default_factory=list)


class CapabilityCoverageReport(BaseModel):
    """Coverage summary for one snapshot, with explicit state counts."""

    model_config = _BASE_CONFIG

    snapshot_id: UUID
    release_id: str
    entries: list[CapabilityCoverage] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
