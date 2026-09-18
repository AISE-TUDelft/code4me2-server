"""Pydantic v2 contracts for runtime packaging and conformance (Issue 11).

``RuntimeManifestV2`` is authoritative for the exact contents and integrity of a
participant runtime package. ``ConformanceCaseV1``/``ConformanceReceiptV1`` bind
observed results to the exact artifact/adapter/host/plugin/protocol/fixture
digests, and preserve ``UNSUPPORTED``/``UNKNOWN`` rather than upgrading them.

Digest convention: component payload digests and the manifest digest are
``sha256:<64 lowercase hex>`` so they compare directly with the agent
``DistributionArtifact.sha256`` and the bootstrap ``artifact_digest``. A bare hex
digest is also accepted when comparing (see :func:`normalize_sha256`).
"""

from __future__ import annotations

import hashlib
from datetime import datetime  # noqa: TC003 - pydantic resolves annotations at runtime
from typing import Optional
from uuid import UUID  # noqa: TC003 - pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from research.canonical import canonical_hash
from research.study.agents.models import AdapterRef

from .enums import (
    ConformanceStatus,
    PackageReasonCode,  # noqa: TC001 - pydantic resolves annotations at runtime
)

_BASE = ConfigDict(extra="forbid")

__all__ = [
    "AdapterRef",
    "ComponentEntry",
    "ConformanceCaseV1",
    "ConformanceCaseResultV1",
    "ConformanceReceiptV1",
    "PackageIssue",
    "PackageVerificationResult",
    "PlatformTriple",
    "ProtocolCompatibility",
    "ResolutionResult",
    "ResolvedComponent",
    "RuntimeManifestV2",
    "SelfCheckSpec",
    "manifest_digest_of",
    "normalize_sha256",
    "sha256_of_bytes",
]


def sha256_of_bytes(data: bytes) -> str:
    """Return ``sha256:<hex>`` for ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize_sha256(value: Optional[str]) -> Optional[str]:
    """Return the bare lowercase hex of ``value`` (stripping ``sha256:``), or ``None``."""
    if value is None:
        return None
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        return None
    return text


class PlatformTriple(BaseModel):
    """An ``(os, arch)`` selection key."""

    model_config = _BASE

    os: str
    arch: str

    @property
    def key(self) -> str:
        """The ``os-arch`` tuple used in diagnostics and args templates."""
        return f"{self.os}-{self.arch}"


class ComponentEntry(BaseModel):
    """One platform-specific component payload inside a package."""

    model_config = _BASE

    name: str
    os: str
    arch: str
    relative_path: str
    sha256: str
    size: int
    executable: bool = False
    license: Optional[str] = None

    @property
    def platform(self) -> PlatformTriple:
        """The component's platform triple."""
        return PlatformTriple(os=self.os, arch=self.arch)


class SelfCheckSpec(BaseModel):
    """A declared self-check command (never a shell string)."""

    model_config = _BASE

    command: list[str] = Field(default_factory=list)
    expected_exit_code: int = 0


class ProtocolCompatibility(BaseModel):
    """ACP protocol compatibility range for the packaged runtime."""

    model_config = _BASE

    min_protocol_version: Optional[str] = None
    max_protocol_version: Optional[str] = None

    def supports(self, protocol_version: Optional[str]) -> Optional[bool]:
        """Whether ``protocol_version`` is in range; ``None`` when undecidable."""
        if protocol_version is None:
            return None
        if self.min_protocol_version is None and self.max_protocol_version is None:
            return None
        current = _version_tuple(protocol_version)
        if current is None:
            return None
        if self.min_protocol_version is not None:
            minimum = _version_tuple(self.min_protocol_version)
            if minimum is not None and current < minimum:
                return False
        if self.max_protocol_version is not None:
            maximum = _version_tuple(self.max_protocol_version)
            if maximum is not None and current > maximum:
                return False
        return True


def _version_tuple(value: str) -> Optional[tuple[int, ...]]:
    try:
        parts = [int(part) for part in value.strip().split(".")]
    except (TypeError, ValueError):
        return None
    return tuple(parts) if parts else None


class RuntimeManifestV2(BaseModel):
    """The package manifest: authoritative for contents and integrity."""

    model_config = _BASE

    schema_version: str = "2"
    manifest_digest: str = ""
    release_id: str
    agent_id: str
    adapter_ref: AdapterRef
    components: list[ComponentEntry] = Field(default_factory=list)
    supported_platforms: list[PlatformTriple] = Field(default_factory=list)
    # Argument template keyed by platform key ("macos-arm64"); always an array.
    args_template: dict[str, list[str]] = Field(default_factory=dict)
    licenses: list[str] = Field(default_factory=list)
    compatibility: ProtocolCompatibility = Field(default_factory=ProtocolCompatibility)
    self_check: Optional[SelfCheckSpec] = None
    signature: Optional[str] = None

    def platform_keys(self) -> list[str]:
        """Supported platform keys in declared order."""
        return [platform.key for platform in self.supported_platforms]

    def supports_platform(self, os: str, arch: str) -> bool:
        """Whether ``(os, arch)`` is a declared supported platform."""
        return any(
            platform.os == os and platform.arch == arch
            for platform in self.supported_platforms
        )

    def component_for(self, os: str, arch: str) -> Optional[ComponentEntry]:
        """The first component whose ``(os, arch)`` exactly matches, or ``None``."""
        return next(
            (
                component
                for component in self.components
                if component.os == os and component.arch == arch
            ),
            None,
        )

    def executable_component_for(self, os: str, arch: str) -> Optional[ComponentEntry]:
        """The runnable component for ``(os, arch)``, or the first, or ``None``."""
        matches = [
            component
            for component in self.components
            if component.os == os and component.arch == arch
        ]
        return next((c for c in matches if c.executable), matches[0] if matches else None)

    def args_for_platform(self, os: str, arch: str) -> list[str]:
        """Argument-array template for a platform (falls back to ``os`` then empty)."""
        key = f"{os}-{arch}"
        if key in self.args_template:
            return list(self.args_template[key])
        if os in self.args_template:
            return list(self.args_template[os])
        return []

    def canonical_payload(self) -> dict:
        """Manifest fields except ``manifest_digest`` and ``signature``.

        Both the digest and the HMAC are computed over this payload, so a signed
        manifest can also be digest-verified without a circular dependency.
        """
        payload = self.model_dump(mode="json")
        payload.pop("manifest_digest", None)
        payload.pop("signature", None)
        return payload

    def computed_digest(self) -> str:
        """``sha256:<hex>`` over the canonical payload."""
        return "sha256:" + canonical_hash(self.canonical_payload())

    def digest_matches(self) -> bool:
        """Whether the stored digest matches the recomputed payload digest."""
        stored = normalize_sha256(self.manifest_digest)
        return stored is not None and stored == normalize_sha256(self.computed_digest())

    def with_digest(self) -> "RuntimeManifestV2":
        """Return a copy with ``manifest_digest`` set to the computed value."""
        return self.model_copy(update={"manifest_digest": self.computed_digest()})

    def platform_conflicts(self) -> list[str]:
        """Platform keys declared by more than one component."""
        seen: dict[str, int] = {}
        for component in self.components:
            seen[component.platform.key] = seen.get(component.platform.key, 0) + 1
        return sorted(key for key, count in seen.items() if count > 1)

    def supports_protocol(self, protocol_version: Optional[str]) -> Optional[bool]:
        """Whether the packaged runtime supports ``protocol_version``."""
        return self.compatibility.supports(protocol_version)


def manifest_digest_of(payload: dict) -> str:
    """Compute the manifest digest for a raw payload dict (``manifest_digest``/``signature`` removed)."""
    trimmed = dict(payload)
    trimmed.pop("manifest_digest", None)
    trimmed.pop("signature", None)
    return "sha256:" + canonical_hash(trimmed)


class PackageIssue(BaseModel):
    """One typed package/verification finding."""

    model_config = _BASE

    code: PackageReasonCode
    message: str
    field: str = ""


class PackageVerificationResult(BaseModel):
    """Typed aggregate result of verifying a package directory."""

    model_config = _BASE

    valid: bool
    manifest_digest: Optional[str] = None
    verified_components: int = 0
    errors: list[PackageIssue] = Field(default_factory=list)

    @property
    def codes(self) -> set[PackageReasonCode]:
        """The set of reason codes observed."""
        return {error.code for error in self.errors}


class ResolvedComponent(BaseModel):
    """A component selected for one exact platform and bootstrap digest."""

    model_config = _BASE

    component: ComponentEntry
    os: str
    arch: str
    arguments: list[str] = Field(default_factory=list)
    relative_path: str


class ResolutionResult(BaseModel):
    """Typed result of resolving a component with no fallback."""

    model_config = _BASE

    resolved: Optional[ResolvedComponent] = None
    error: Optional[PackageIssue] = None


class ConformanceCaseV1(BaseModel):
    """One capability-aware conformance case."""

    model_config = _BASE

    case_id: str
    prerequisites: list[str] = Field(default_factory=list)
    fixture_ref: str = ""
    action_steps: list[str] = Field(default_factory=list)
    expected_host_observations: list[str] = Field(default_factory=list)
    expected_agent_observations: list[str] = Field(default_factory=list)
    cleanup_assertion: str = ""
    max_performance_ms: Optional[float] = None
    status: ConformanceStatus = ConformanceStatus.UNKNOWN


class ConformanceCaseResultV1(BaseModel):
    """The observed result of one conformance case."""

    model_config = _BASE

    case_id: str
    status: ConformanceStatus
    host_observations: list[str] = Field(default_factory=list)
    agent_observations: list[str] = Field(default_factory=list)
    cleanup_ok: Optional[bool] = None
    performance_ms: Optional[float] = None
    evidence_digest: str = ""
    reason: str = ""


class ConformanceReceiptV1(BaseModel):
    """Binds conformance results to exact artifact/adapter/host/plugin/protocol/fixtures."""

    model_config = _BASE

    receipt_id: UUID
    artifact_digest: str
    adapter_digest: str
    host: PlatformTriple
    plugin_version: str
    protocol_version: str
    fixture_digests: dict[str, str] = Field(default_factory=dict)
    case_results: list[ConformanceCaseResultV1] = Field(default_factory=list)
    status: ConformanceStatus = ConformanceStatus.UNKNOWN
    created_at: datetime

    def passed_cases(self) -> set[str]:
        """Case ids whose observed status is exactly ``PASS``."""
        return {
            result.case_id
            for result in self.case_results
            if result.status == ConformanceStatus.PASS
        }
