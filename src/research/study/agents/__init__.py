"""Agent registry and capability contract (Issue 04).

Public surface:

* :mod:`research.study.agents.enums` - closed vocabularies for qualification status,
  distribution source, snapshot capability state, coverage state, registry
  reason codes, plus the re-exported Issue 01 ``Fidelity``.
* :mod:`research.study.agents.models` - Pydantic v2 contracts for
  :class:`~research.study.agents.models.AgentReleaseV1` (distribution + adapter
  identity) and :class:`~research.study.agents.models.CapabilitySnapshotV1` (separate
  declared/observed maps with explicit ``UNKNOWN``/``UNAVAILABLE``).
* :mod:`research.study.agents.registry` - the pure :class:`~research.study.agents.registry.AgentRegistry`
  service: digest-identity registration, qualification transitions, exact
  platform resolution, qualification assessment and capability coverage.
* :mod:`research.study.agents.resolver` - :class:`~research.study.agents.resolver.RegistryReleaseResolver`,
  the bridge implementing :class:`research.study.protocol.validation.ReleaseResolver`
  so protocol publication rejects missing/unqualified/withdrawn releases.

The persistence adapters live in :mod:`research.study.agents.store` and take a
caller-supplied SQLAlchemy ``Session`` so this package stays free of any
application/App import.
"""

from .enums import (
    CapabilityCoverageState,
    DistributionSourceType,
    Fidelity,
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
    ReleaseDisplay,
    ReleaseTests,
    SnapshotFailure,
)
from .registry import (
    AgentRegistry,
    ArtifactResolution,
    QualificationAssessment,
    RegistrationResult,
    RegistryIssue,
    build_capability_snapshot,
    capability_coverage,
    coverage_report,
    derive_qualification_status,
)
from .resolver import RegistryReleaseResolver

__all__ = [
    "AdapterRef",
    "AgentRegistry",
    "AgentReleaseV1",
    "ReleaseTests",
    "ArtifactResolution",
    "CapabilityCoverage",
    "CapabilityCoverageReport",
    "CapabilityCoverageState",
    "CapabilityEntry",
    "CapabilitySnapshotV1",
    "DistributionArtifact",
    "DistributionSourceType",
    "EnvironmentRef",
    "Fidelity",
    "QualificationAssessment",
    "QualificationStatus",
    "RegistrationResult",
    "RegistryIssue",
    "RegistryReasonCode",
    "RegistryReleaseResolver",
    "ReleaseDisplay",
    "SnapshotCapabilityState",
    "SnapshotFailure",
    "build_capability_snapshot",
    "capability_coverage",
    "coverage_report",
    "derive_qualification_status",
]
