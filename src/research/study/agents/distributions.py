"""Resolve an ``AgentProfile`` *distribution* into a non-secret view.

An :class:`~database.db_schemas.AgentProfile` IS a distribution: it names the
exact agent artifact a study condition runs. This module turns a profile row plus
its (optional) registry release into the
:class:`~research.study.protocol.validation.DistributionResolution` the protocol
validator consumes, and into the derived, read-only shape the admin UI needs.

The resolution is deliberately database-agnostic: callers pass an already-loaded
profile and release, so this stays usable from the backend router, the seed
tooling and tests without importing ``App``.

Verification is **derived**, never stored:

* ``PACKAGED`` is verified only when its release carries a passing conformance
  receipt (``derive_qualification_status(release) == QUALIFIED``).
* ``BYOA_EXTERNAL`` is *always* unverified: a participant-installed agent has no
  artifact to bind conformance evidence to.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from research.study.protocol.enums import ReleaseResolutionStatus
from research.study.protocol.models import ResolvedAgentConfig
from research.study.protocol.validation import DistributionResolution

from .enums import DistributionMode, QualificationStatus
from .registry import AgentRegistry
from .resolver import RegistryReleaseResolver

if TYPE_CHECKING:
    from .models import AgentReleaseV1

__all__ = [
    "distribution_supported_platforms",
    "parse_command_args",
    "resolve_distribution_view",
]

_WITHDRAWN = frozenset(
    {QualificationStatus.RETIRED, QualificationStatus.BLOCKED}
)


def parse_command_args(value: Any) -> list[str]:
    """Normalize a stored ``agent_command_args`` value into a string list.

    The column is JSON text, but callers may hand a list (a freshly built
    payload) or ``None``. Anything unparseable degrades to an empty list rather
    than raising, because it is only ever an argument vector.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _packaged_digest(
    release: AgentReleaseV1, platform: Optional[tuple[str, str]]
) -> Optional[str]:
    registry = AgentRegistry()
    if not registry.register_release(release).accepted:
        return None
    resolver = RegistryReleaseResolver(registry, platform=platform)
    return resolver.artifact_digest(release)


def _agent_config(
    profile: Any,
    release: Optional[AgentReleaseV1],
    connection: Any,
    funding_owner_user_id: Any = None,
) -> Optional[ResolvedAgentConfig]:
    """Freeze the profile's non-secret executable config, when available.

    A lightweight stand-in (no ``model``/``tools_json``) has no executable config
    to freeze, so this returns ``None``; such a revision is refused at execution
    time rather than read from a mutable profile row.
    """
    if getattr(profile, "model", None) is None:
        return None
    profile_id = getattr(profile, "profile_id", None) or getattr(
        profile, "distribution_id", None
    )
    if profile_id is None:
        return None
    return ResolvedAgentConfig(
        profile_id=profile_id,
        name=str(getattr(profile, "name", "") or ""),
        model=str(getattr(profile, "model", "") or ""),
        framework_version=str(getattr(profile, "framework_version", "") or ""),
        tools_json=str(getattr(profile, "tools_json", "") or "[]"),
        approval_policy=str(getattr(profile, "approval_policy", "") or ""),
        max_steps=int(getattr(profile, "max_steps", 0) or 0),
        temperature=getattr(profile, "temperature", None),
        max_context_tokens=getattr(profile, "max_context_tokens", None),
        connection_id=getattr(profile, "connection_id", None),
        connection_label=getattr(connection, "label", None),
        funding_owner_user_id=funding_owner_user_id,
    )


def resolve_distribution_view(
    profile: Any,
    release: Optional[AgentReleaseV1],
    *,
    distribution_id: Any = None,
    platform: Optional[tuple[str, str]] = None,
    connection: Any = None,
    funding_owner_user_id: Any = None,
) -> DistributionResolution:
    """Build the validator's view of one distribution.

    ``profile`` is any object exposing ``distribution_mode``, ``release_id``,
    ``agent_package``, ``agent_command`` and ``agent_command_args`` (an
    ``AgentProfile`` row or a lightweight stand-in). ``release`` is the already
    rehydrated registry release for ``release_id``, or ``None`` when it does not
    exist.

    When ``profile`` also exposes the executable config (a real profile row),
    that non-secret config is attached as ``agent_config`` so publication can
    freeze it into the immutable revision. ``funding_owner_user_id`` is the study
    owner whose connection grant funds execution.
    """
    if distribution_id is None:
        distribution_id = getattr(profile, "profile_id", None)
    raw_mode = str(getattr(profile, "distribution_mode", "") or "PACKAGED")
    mode = raw_mode.strip().upper()
    release_id = getattr(profile, "release_id", None)
    agent_package = getattr(profile, "agent_package", None)
    agent_command = getattr(profile, "agent_command", None)
    agent_command_args = parse_command_args(
        getattr(profile, "agent_command_args", None)
    )
    agent_config = _agent_config(
        profile, release, connection, funding_owner_user_id
    )

    if mode == DistributionMode.BYOA_EXTERNAL.value:
        # A BYOA distribution is identity-pinned and can never be verified.
        identity_present = bool(
            agent_package or agent_command or (release is not None)
        )
        return DistributionResolution(
            found=True,
            distribution_id=distribution_id,
            distribution_mode=mode,
            release_id=release_id,
            agent_id=release.agent_id if release is not None else None,
            version=release.version if release is not None else None,
            agent_package=agent_package
            or (release.agent_package if release is not None else None),
            agent_command=agent_command
            or (release.agent_command if release is not None else None),
            agent_command_args=agent_command_args
            or (list(release.agent_command_args) if release is not None else []),
            artifact_digest=None,
            verified=False,
            release_status=(
                ReleaseResolutionStatus.RESOLVED
                if identity_present
                else ReleaseResolutionStatus.UNQUALIFIED
            ),
            message=(
                None
                if identity_present
                else "BYOA distribution declares neither a package nor a command"
            ),
            agent_config=agent_config,
        )

    view = DistributionResolution(
        found=True,
        distribution_id=distribution_id,
        distribution_mode=mode or DistributionMode.PACKAGED.value,
        release_id=release_id,
        agent_package=None,
        agent_command=None,
        artifact_digest=None,
        verified=False,
        agent_config=agent_config,
    )
    if not release_id:
        return view
    if release is None:
        view.message = "release is not registered"
        return view

    view.agent_id = release.agent_id
    view.version = release.version
    if release.qualification_status in _WITHDRAWN:
        view.release_status = ReleaseResolutionStatus.WITHDRAWN
        view.message = f"release is {release.qualification_status.value}"
        return view
    if release.qualification_status != QualificationStatus.QUALIFIED:
        view.release_status = ReleaseResolutionStatus.UNQUALIFIED
        view.message = "release is not qualified"
        return view

    view.verified = True
    view.release_status = ReleaseResolutionStatus.RESOLVED
    view.artifact_digest = _packaged_digest(release, platform)
    return view


def distribution_supported_platforms(
    release: Optional[AgentReleaseV1],
) -> list[dict[str, str]]:
    """The ``(os, arch)`` pairs a release publishes, in a stable order."""
    if release is None:
        return []
    artifacts = sorted(release.artifacts, key=lambda item: (item.os, item.arch))
    return [{"os": artifact.os, "arch": artifact.arch} for artifact in artifacts]
