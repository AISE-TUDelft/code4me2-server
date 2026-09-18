"""Runtime packaging and agent conformance suite (Issue 11).

Public surface:

* :mod:`research.study.packaging.enums` - conformance statuses and typed package
  reason codes.
* :mod:`research.study.packaging.models` - ``RuntimeManifestV2``, ``ConformanceCaseV1``,
  and ``ConformanceReceiptV1`` (digest-addressed).
* :mod:`research.study.packaging.verifier` - default-deny package verification and
  path containment.
* :mod:`research.study.packaging.resolver` - exact platform selection with a required
  bootstrap-digest match and no PATH/package-manager fallback.
* :mod:`research.study.packaging.conformance` - the capability-aware runner and the
  release-qualification link.
* :mod:`research.study.packaging.packager` - synthetic package assembly helper.
* :mod:`research.study.packaging.store` - Session-supplied persistence helpers.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .conformance import (
    CaseObservation,
    ConformanceObserver,
    ConformanceRunner,
    QualificationDecision,
    qualification_for_release,
)
from .enums import ConformanceStatus, PackageReasonCode, PrerequisiteState
from .models import (
    ComponentEntry,
    ConformanceCaseResultV1,
    ConformanceCaseV1,
    ConformanceReceiptV1,
    PackageIssue,
    PackageVerificationResult,
    PlatformTriple,
    ProtocolCompatibility,
    ResolutionResult,
    ResolvedComponent,
    RuntimeManifestV2,
    SelfCheckSpec,
    manifest_digest_of,
    normalize_sha256,
    sha256_of_bytes,
)
from .packager import build_package, write_manifest
from .resolver import resolve_component, resolved_digest
from .verifier import (
    PathContainment,
    hash_file,
    resolve_under_root,
    scan_secret_files,
    scan_undeclared_executables,
    sign_manifest,
    verify_package,
)

__all__ = [
    "CaseObservation",
    "ComponentEntry",
    "ConformanceCaseResultV1",
    "ConformanceCaseV1",
    "ConformanceObserver",
    "ConformanceReceiptV1",
    "ConformanceRunner",
    "ConformanceStatus",
    "PackageIssue",
    "PackageReasonCode",
    "PackageVerificationResult",
    "PathContainment",
    "PlatformTriple",
    "PrerequisiteState",
    "ProtocolCompatibility",
    "QualificationDecision",
    "ResolutionResult",
    "ResolvedComponent",
    "RuntimeManifestV2",
    "SelfCheckSpec",
    "build_package",
    "hash_file",
    "manifest_digest_of",
    "normalize_sha256",
    "qualification_for_release",
    "resolve_component",
    "resolve_under_root",
    "resolved_digest",
    "scan_secret_files",
    "scan_undeclared_executables",
    "sha256_of_bytes",
    "sign_manifest",
    "verify_package",
    "write_manifest",
]
