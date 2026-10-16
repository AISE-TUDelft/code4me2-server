"""Dependency-free manifest signing and verification.

The manifest digest is content-addressed over the canonical JSON of the manifest
with ``manifest_digest`` and the capability signature excluded. The signature is
an HMAC-SHA256 over that digest using an injected secret, so signing needs no
new dependency and no PKI. Server-side verification is authoritative.
"""

from __future__ import annotations

import hashlib
import hmac

from research.canonical import canonical_bytes, canonical_hash

from .models import (
    BootstrapManifestV1,
    ManifestReasonCode,
    ManifestSignature,
    ManifestVerification,
)

__all__ = [
    "compute_manifest_digest",
    "manifest_digest_payload",
    "sign_manifest",
    "verify_manifest",
]


def manifest_digest_payload(manifest: BootstrapManifestV1) -> dict:
    """Return the canonical mapping covered by the manifest digest.

    Excludes the ``manifest_digest`` and ``signature`` fields, so the digest and
    its signature are stable and self-referential content is never hashed.
    """
    data = manifest.model_dump(mode="json")
    data.pop("manifest_digest", None)
    data.pop("signature", None)
    return data


def compute_manifest_digest(manifest: BootstrapManifestV1) -> str:
    """Return the SHA-256 digest of the digest-covered canonical mapping."""
    return canonical_hash(manifest_digest_payload(manifest))


def _hmac_hex(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_manifest(
    manifest: BootstrapManifestV1, secret: str
) -> ManifestSignature:
    """Return ``(digest, signature)`` for a manifest."""
    digest = compute_manifest_digest(manifest)
    return ManifestSignature(digest=digest, signature=_hmac_hex(secret, digest))


def verify_manifest(
    manifest: BootstrapManifestV1, secret: str
) -> ManifestVerification:
    """Verify the manifest digest and signature, returning a typed reason."""
    expected = compute_manifest_digest(manifest)
    if not manifest.manifest_digest:
        return ManifestVerification(
            ok=False,
            reason=ManifestReasonCode.MANIFEST_DIGEST_MISMATCH,
            expected_digest=expected,
            actual_digest="",
            message="manifest_digest is missing",
        )
    if manifest.manifest_digest != expected:
        return ManifestVerification(
            ok=False,
            reason=ManifestReasonCode.MANIFEST_DIGEST_MISMATCH,
            expected_digest=expected,
            actual_digest=manifest.manifest_digest,
            message="manifest content does not match its digest",
        )

    signature = manifest.signature
    if not signature:
        return ManifestVerification(
            ok=False,
            reason=ManifestReasonCode.MISSING_SIGNATURE,
            expected_digest=expected,
            actual_digest=manifest.manifest_digest,
            message="manifest is not signed",
        )

    if not hmac.compare_digest(signature, _hmac_hex(secret, expected)):
        return ManifestVerification(
            ok=False,
            reason=ManifestReasonCode.MANIFEST_SIGNATURE_MISMATCH,
            expected_digest=expected,
            actual_digest=manifest.manifest_digest,
            message="manifest signature does not match",
        )

    return ManifestVerification(
        ok=True,
        reason=ManifestReasonCode.OK,
        expected_digest=expected,
        actual_digest=manifest.manifest_digest,
    )


def canonical_manifest_bytes(manifest: BootstrapManifestV1) -> bytes:
    """Convenience: canonical bytes of the manifest (signature excluded)."""
    return canonical_bytes(manifest_digest_payload(manifest))
