"""Closed vocabularies for the agent registry and capability contract (Issue 04).

Every member is part of a persisted contract (release JSON, snapshot JSON,
registry responses), so members are additive only: renaming or removing a
member is a breaking change to already-stored releases and snapshots.

``Fidelity`` is re-exported from :mod:`research.compatibility.enums` so the
registry shares the exact Issue 01 fidelity vocabulary instead of inventing a
parallel one.
"""

from __future__ import annotations

from enum import Enum

from research.compatibility.enums import Fidelity  # noqa: F401 - intentional re-export

__all__ = [
    "CapabilityCoverageState",
    "DistributionMode",
    "DistributionSourceType",
    "Fidelity",
    "MANAGED_RUNTIME_FRAMEWORK",
    "QualificationStatus",
    "RegistryReasonCode",
    "SnapshotCapabilityState",
]

#: The one agent runtime the managed protocol runs in this release. This is a
#: readiness default, not a binding ban, and the three concepts stay distinct:
#: a BYOA release is *usable* when its recipe self-check passed; a BYOA
#: *distribution* still verifies as unverified at publication (admin WARNING,
#: non-admin ERROR; ISSUE-004); and a participant host without the installed
#: agent blocks with AGENT_NOT_FOUND at resolution.
#: Qualification never implies verification or readiness, or vice versa.
MANAGED_RUNTIME_FRAMEWORK = "code4me2-agent"


class DistributionMode(str, Enum):
    """How a release's agent reaches the participant host.

    ``PACKAGED`` (the default) is the historical, digest-pinned contract: the
    agent is a packaged artifact shipped inside the plugin runtime and the
    release carries a verified ``sha256`` per platform. ``BYOA_EXTERNAL`` means
    "bring your own agent": the participant installs the agent (Goose, Codex, ...)
    and the release pins a command/package identity instead of an artifact digest.
    The two modes are persisted on the release and embedded in every bootstrap
    manifest so the plugin never silently mixes them.
    """

    PACKAGED = "PACKAGED"
    BYOA_EXTERNAL = "BYOA_EXTERNAL"


class QualificationStatus(str, Enum):
    """Review status of one agent release.

    Distribution, protocol capability and observability capability are
    separate contracts. ``QUALIFIED`` is **derived** from the imported recipe's
    self-check verdict (``tests.status == "PASS"``); a release without a passing
    recipe is ``UNQUALIFIED``. ``DISABLED`` is a one-way administrator action.
    ``DRAFT``, ``CONDITIONALLY_QUALIFIED``, ``RETIRED`` and ``BLOCKED`` remain in
    the persisted vocabulary for historical rows and for the withdrawal mapping.
    """

    UNQUALIFIED = "UNQUALIFIED"
    DRAFT = "DRAFT"
    QUALIFIED = "QUALIFIED"
    CONDITIONALLY_QUALIFIED = "CONDITIONALLY_QUALIFIED"
    RETIRED = "RETIRED"
    BLOCKED = "BLOCKED"
    DISABLED = "DISABLED"


class DistributionSourceType(str, Enum):
    """Where a release's distribution metadata came from."""

    EXTERNAL_REGISTRY = "EXTERNAL_REGISTRY"
    RESEARCH_OVERLAY = "RESEARCH_OVERLAY"
    BUNDLED = "BUNDLED"


class SnapshotCapabilityState(str, Enum):
    """Capability state captured in a runtime snapshot.

    ``UNKNOWN``, ``DECLARED``, ``UNAVAILABLE``, ``PARTIAL`` and ``BROKEN`` are
    deliberately distinct from ``OBSERVED``: declared intent and observed
    behavior are separate facts, and an absent observation is never promoted to
    a supported one.
    """

    UNKNOWN = "UNKNOWN"
    DECLARED = "DECLARED"
    OBSERVED = "OBSERVED"
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"
    BROKEN = "BROKEN"


class CapabilityCoverageState(str, Enum):
    """Aggregate observability of one capability across declared vs observed."""

    OBSERVED = "OBSERVED"
    DECLARED_ONLY = "DECLARED_ONLY"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"
    BROKEN = "BROKEN"


class RegistryReasonCode(str, Enum):
    """Stable machine-readable reason a registry operation was rejected."""

    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    WITHDRAWN = "WITHDRAWN"
    DUPLICATE_RELEASE = "DUPLICATE_RELEASE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    INCOMPLETE_ARTIFACTS = "INCOMPLETE_ARTIFACTS"
    ADAPTER_INCOMPATIBLE = "ADAPTER_INCOMPATIBLE"
    INVALID_VERSION_RANGE = "INVALID_VERSION_RANGE"
    SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
    # A BYOA release declares neither a command nor a package identity to resolve.
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
