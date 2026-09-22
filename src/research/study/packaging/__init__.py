"""Runtime packaging (Issue 11).

Public surface:

* :mod:`research.study.packaging.enums` - typed package reason codes.
* :mod:`research.study.packaging.models` - ``RuntimeManifestV2`` and its
  digest-addressed components.
* :mod:`research.study.packaging.verifier` - default-deny package verification and
  path containment.
* :mod:`research.study.packaging.resolver` - exact platform selection with a required
  bootstrap-digest match and no PATH/package-manager fallback.
* :mod:`research.study.packaging.packager` - synthetic package assembly helper.
* :mod:`research.study.packaging.store` - Session-supplied persistence helpers.

The core package never imports ``App``, FastAPI, or a session factory.
Release usability comes from platform tests owned by :mod:`research.study.agents`.
"""

from .enums import PackageReasonCode
from .models import (
    ComponentEntry,
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
    "ComponentEntry",
    "PackageIssue",
    "PackageReasonCode",
    "PackageVerificationResult",
    "PathContainment",
    "PlatformTriple",
    "ProtocolCompatibility",
    "ResolutionResult",
    "ResolvedComponent",
    "RuntimeManifestV2",
    "SelfCheckSpec",
    "build_package",
    "hash_file",
    "manifest_digest_of",
    "normalize_sha256",
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
