"""Exact runtime component selection (Issue 11).

``resolve_component`` selects the component for one exact ``(os, arch)`` and
requires its digest to equal the digest the bootstrap manifest declared. There is
deliberately **no fallback**: no ``PATH`` lookup, no ``npm``/``npx``, no source
checkout, no other platform or version. A failure returns a typed block and no
component, so a caller can never launch an unaudited executable.
"""

from __future__ import annotations

from typing import Optional

from .enums import PackageReasonCode
from .models import (
    PackageIssue,
    ResolutionResult,
    ResolvedComponent,
    RuntimeManifestV2,
    normalize_sha256,
)

__all__ = ["resolve_component", "resolved_digest"]


def resolve_component(
    manifest: RuntimeManifestV2,
    os: str,
    arch: str,
    bootstrap_artifact_digest: str,
) -> ResolutionResult:
    """Resolve the exact component, or return a typed block (never a fallback)."""
    if not os or not arch:
        return ResolutionResult(
            error=PackageIssue(
                code=PackageReasonCode.UNSUPPORTED_PLATFORM,
                message="a non-blank os and arch are required to resolve a runtime",
            )
        )
    if not manifest.supports_platform(os, arch):
        return ResolutionResult(
            error=PackageIssue(
                code=PackageReasonCode.UNSUPPORTED_PLATFORM,
                message=f"manifest does not declare platform '{os}-{arch}'",
                field="supported_platforms",
            )
        )
    component = manifest.executable_component_for(os, arch)
    if component is None:
        return ResolutionResult(
            error=PackageIssue(
                code=PackageReasonCode.MISSING_COMPONENT,
                message=f"manifest has no component for '{os}-{arch}'",
                field="components",
            )
        )

    declared = normalize_sha256(component.sha256)
    if declared is None:
        return ResolutionResult(
            error=PackageIssue(
                code=PackageReasonCode.DIGEST_MISMATCH,
                message=f"component '{component.name}' has a malformed sha256",
                field="components.sha256",
            )
        )
    expected = normalize_sha256(bootstrap_artifact_digest)
    if expected is None or expected != declared:
        return ResolutionResult(
            error=PackageIssue(
                code=PackageReasonCode.BOOTSTRAP_DIGEST_MISMATCH,
                message=(
                    f"component '{component.name}' digest does not match the "
                    "bootstrap-declared artifact digest"
                ),
                field="artifact_digest",
            )
        )

    return ResolutionResult(
        resolved=ResolvedComponent(
            component=component,
            os=os,
            arch=arch,
            arguments=manifest.args_for_platform(os, arch),
            relative_path=component.relative_path,
        )
    )


def resolved_digest(result: ResolutionResult) -> Optional[str]:
    """Convenience: the resolved component digest, or ``None`` when blocked."""
    if result.resolved is None:
        return None
    return normalize_sha256(result.resolved.component.sha256)
