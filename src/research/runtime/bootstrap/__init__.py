"""Bootstrap manifest and session capability (Issue 05).

Public surface:

* :mod:`research.runtime.bootstrap.models` - ``BootstrapManifestV1`` and its sub-objects,
  ``SessionCapability``, typed reason codes and results.
* :mod:`research.runtime.bootstrap.signer` - dependency-free manifest digest + HMAC
  signature and verification.
* :mod:`research.runtime.bootstrap.capability` - scoped, short-lived capability issuance
  and server-side verification.
* :mod:`research.runtime.bootstrap.service` - :func:`compose_bootstrap`, which validates
  eligibility/compatibility before projecting and signing a secret-free manifest.
* :mod:`research.runtime.bootstrap.store` - persistence adapters taking a caller-supplied
  SQLAlchemy ``Session``.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .capability import (
    DEFAULT_AUDIENCE,
    DEFAULT_SCOPE,
    compute_capability_signature,
    issue_capability,
    verify_capability,
)
from .models import (
    BootstrapAgentRelease,
    BootstrapAssignment,
    BootstrapCompatibility,
    BootstrapIssue,
    BootstrapManifestV1,
    BootstrapOutcome,
    BootstrapPolicies,
    BootstrapPrivacyPolicy,
    BootstrapReasonCode,
    BootstrapResult,
    BootstrapSessionPolicy,
    BootstrapTelemetryPolicy,
    CapabilityReasonCode,
    CapabilityVerification,
    ManifestReasonCode,
    ManifestSignature,
    ManifestVerification,
    ResearchSessionRef,
    SessionCapability,
)
from .service import (
    BootstrapSigningContext,
    EphemeralSessionFactory,
    SessionFactory,
    compose_bootstrap,
)
from .signer import (
    canonical_manifest_bytes,
    compute_manifest_digest,
    manifest_digest_payload,
    sign_manifest,
    verify_manifest,
)

__all__ = [
    "DEFAULT_AUDIENCE",
    "DEFAULT_SCOPE",
    "BootstrapAgentRelease",
    "BootstrapAssignment",
    "BootstrapCompatibility",
    "BootstrapIssue",
    "BootstrapManifestV1",
    "BootstrapOutcome",
    "BootstrapPolicies",
    "BootstrapPrivacyPolicy",
    "BootstrapReasonCode",
    "BootstrapResult",
    "BootstrapSessionPolicy",
    "BootstrapSigningContext",
    "BootstrapTelemetryPolicy",
    "CapabilityReasonCode",
    "CapabilityVerification",
    "EphemeralSessionFactory",
    "ManifestReasonCode",
    "ManifestSignature",
    "ManifestVerification",
    "ResearchSessionRef",
    "SessionCapability",
    "SessionFactory",
    "canonical_manifest_bytes",
    "compose_bootstrap",
    "compute_capability_signature",
    "compute_manifest_digest",
    "issue_capability",
    "manifest_digest_payload",
    "sign_manifest",
    "verify_capability",
    "verify_manifest",
]
