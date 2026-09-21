"""Bridge from the agent registry to protocol release validation.

:class:`RegistryReleaseResolver` implements the
:class:`research.study.protocol.validation.ReleaseResolver` interface so protocol
publication rejects releases that are missing, retired, blocked, or not yet
qualified:

* not found -> ``NOT_FOUND``
* ``RETIRED`` / ``BLOCKED`` / ``DISABLED`` -> ``WITHDRAWN``
* ``UNQUALIFIED`` / ``DRAFT`` / ``CONDITIONALLY_QUALIFIED`` -> ``UNQUALIFIED``
* ``QUALIFIED`` -> ``RESOLVED`` with the selected artifact's digest
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from research.study.protocol.enums import ReleaseResolutionStatus
from research.study.protocol.validation import ReleaseResolution

from .enums import QualificationStatus

if TYPE_CHECKING:
    from .models import AgentReleaseV1
    from .registry import AgentRegistry

_WITHDRAWN = frozenset(
    {
        QualificationStatus.RETIRED,
        QualificationStatus.BLOCKED,
        QualificationStatus.DISABLED,
    }
)
_UNQUALIFIED = frozenset(
    {
        QualificationStatus.UNQUALIFIED,
        QualificationStatus.DRAFT,
        QualificationStatus.CONDITIONALLY_QUALIFIED,
    }
)


class RegistryReleaseResolver:
    """Resolve a study condition's pinned release against the registry."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        platform: Optional[tuple[str, str]] = None,
    ) -> None:
        self.registry = registry
        self.platform = platform

    def resolve(
        self,
        agent_id: str,
        *,
        release_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> ReleaseResolution:
        """Map a release reference onto a typed protocol resolution."""
        if version is not None and version.strip().lower() == "latest":
            return ReleaseResolution(
                status=ReleaseResolutionStatus.UNQUALIFIED,
                agent_id=agent_id,
                release_id=release_id,
                version=version,
                message="'latest' is a mutable reference and cannot be pinned",
            )

        release = self.registry.find_release(
            agent_id, release_id=release_id, version=version
        )
        if release is None:
            if version is not None and self.registry.version_candidates(
                agent_id, version
            ):
                return ReleaseResolution(
                    status=ReleaseResolutionStatus.UNQUALIFIED,
                    agent_id=agent_id,
                    release_id=release_id,
                    version=version,
                    message=(
                        "version label is ambiguous; pin an explicit release_id"
                    ),
                )
            return ReleaseResolution(
                status=ReleaseResolutionStatus.NOT_FOUND,
                agent_id=agent_id,
                release_id=release_id,
                version=version,
                message="release is not registered",
            )

        if release.qualification_status in _WITHDRAWN:
            return ReleaseResolution(
                status=ReleaseResolutionStatus.WITHDRAWN,
                agent_id=agent_id,
                release_id=release.release_id,
                version=release.version,
                message=f"release is {release.qualification_status.value}",
            )

        if release.qualification_status in _UNQUALIFIED:
            return ReleaseResolution(
                status=ReleaseResolutionStatus.UNQUALIFIED,
                agent_id=agent_id,
                release_id=release.release_id,
                version=release.version,
                message=(
                    "release is not qualified "
                    f"({release.qualification_status.value})"
                ),
            )

        if release.is_byoa:
            # A BYOA release is pinned by its command/package identity, not by an
            # artifact digest; there is nothing to stage or verify up front.
            if release.byoa_identity is None:
                return ReleaseResolution(
                    status=ReleaseResolutionStatus.UNQUALIFIED,
                    agent_id=agent_id,
                    release_id=release.release_id,
                    version=release.version,
                    distribution_mode=release.distribution_mode.value,
                    message=(
                        "qualified BYOA release has no command or package identity"
                    ),
                )
            return ReleaseResolution(
                status=ReleaseResolutionStatus.RESOLVED,
                agent_id=agent_id,
                release_id=release.release_id,
                version=release.version,
                distribution_mode=release.distribution_mode.value,
            )

        digest = self.artifact_digest(release)
        if not digest:
            return ReleaseResolution(
                status=ReleaseResolutionStatus.UNQUALIFIED,
                agent_id=agent_id,
                release_id=release.release_id,
                version=release.version,
                message="qualified release has no resolvable artifact digest",
            )

        return ReleaseResolution(
            status=ReleaseResolutionStatus.RESOLVED,
            agent_id=agent_id,
            release_id=release.release_id,
            version=release.version,
            artifact_digest=digest,
            distribution_mode=release.distribution_mode.value,
        )

    def artifact_digest(self, release: AgentReleaseV1) -> Optional[str]:
        """Return the digest of the selected artifact, or ``None``.

        When a platform is configured it is selected exactly; otherwise the
        release must publish exactly one artifact, or the first artifact in a
        stable ``(os, arch)`` order is used.
        """
        if self.platform is not None:
            resolution = self.registry.resolve_artifact(
                release, self.platform[0], self.platform[1]
            )
            if not resolution.resolved or resolution.artifact is None:
                return None
            return resolution.artifact.sha256

        artifacts = sorted(release.artifacts, key=lambda item: (item.os, item.arch))
        if not artifacts:
            return None
        return artifacts[0].sha256
