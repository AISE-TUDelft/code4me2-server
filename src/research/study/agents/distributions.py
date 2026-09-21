"""Resolve an ``AgentProfile`` *distribution* into a non-secret view.

An :class:`~database.db_schemas.AgentProfile` IS a distribution: it pins the
exact agent release a study condition runs. This module turns a profile row plus
its (optional) registry release into the
:class:`~research.study.protocol.validation.DistributionResolution` the protocol
validator consumes, and into the derived, read-only shape the admin UI needs.

A profile row owns only the pin (``profile_id``/``release_id``) and the
executable config. Distribution mode, command/package identity and argument
vector are derived **exclusively from the release** — the profile-level
``distribution_mode``/``agent_package``/``agent_command``/``agent_command_args``
attributes no longer exist and are never read.

The module also owns the shared, pure profile↔release executable contract
(:func:`validate_profile_configuration`) used by profile CRUD and study
creation, so an unexecutable combination is rejected before enrollment.

Verification is **derived**, never stored:

* ``PACKAGED`` is verified only when its release is usable
  (``derive_qualification_status(release) == QUALIFIED``), i.e. the imported
  recipe's self-check passed.
* ``BYOA_EXTERNAL`` is *always* unverified: a participant-installed agent has no
  artifact whose bytes the server can pin.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping, Optional

from agents.tools import tools_for_framework
from research.study.protocol.enums import ReleaseResolutionStatus
from research.study.protocol.models import ResolvedAgentConfig
from research.study.protocol.validation import DistributionResolution

from .enums import (
    MANAGED_RUNTIME_FRAMEWORK,
    DistributionMode,
    QualificationStatus,
)
from .models import BYOA_CONFIG_FIELDS, BYOA_CONFIG_TRANSPORTS
from .registry import (
    ALL_APPROVAL_OPTIONS,
    AgentRegistry,
    approval_option_verified,
    derive_qualification_status,
)
from .resolver import RegistryReleaseResolver

if TYPE_CHECKING:
    from .models import AgentReleaseV1

__all__ = [
    "FRAMEWORK_DISTRIBUTION_MODES",
    "ProfileConfigurationError",
    "distribution_supported_platforms",
    "parse_command_args",
    "resolve_distribution_view",
    "validate_profile_configuration",
]

_WITHDRAWN = frozenset(
    {
        QualificationStatus.RETIRED,
        QualificationStatus.BLOCKED,
        QualificationStatus.DISABLED,
    }
)

#: The distribution mode each supported framework can execute (ISSUE-03).
#:
#: The managed built-in runtime is digest-pinned (``PACKAGED``); Goose and Codex
#: are participant-installed BYOA identities. Framework and release are
#: validated together so an arm's label always matches what will run.
FRAMEWORK_DISTRIBUTION_MODES: dict[str, str] = {
    MANAGED_RUNTIME_FRAMEWORK: DistributionMode.PACKAGED.value,
    "goose": DistributionMode.BYOA_EXTERNAL.value,
    "codex": DistributionMode.BYOA_EXTERNAL.value,
}


class ProfileConfigurationError(ValueError):
    """Typed profile/release executable-contract violation.

    ``str(error)`` is ``"CODE: message"`` (the convention the study router
    already parses) and ``code`` is the machine-readable reason.
    """

    def __init__(self, code: str, message: str, field: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.field = field


def _release_document(
    release: Any, release_json: Optional[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """The raw evidence document for a release model, row or mapping."""
    if isinstance(release_json, Mapping):
        return release_json
    candidate = getattr(release, "release_json", None)
    if isinstance(candidate, Mapping):
        return candidate
    if isinstance(release, Mapping):
        return release
    return None


def _framework(value: Any) -> str:
    return str(value or "").strip().lower()


def _release_mode(release: Any) -> str:
    mode = getattr(release, "distribution_mode", None)
    value = getattr(mode, "value", mode)
    return str(value or "").strip().upper()


def _qualification_value(release: Any, document: Optional[Mapping[str, Any]]) -> str:
    status = getattr(release, "qualification_status", None)
    value = str(getattr(status, "value", status) or "").strip().upper()
    if value:
        return value
    if document is not None:
        return derive_qualification_status(document).value
    return QualificationStatus.UNQUALIFIED.value


def _profile_tools(profile: Any) -> set[str]:
    raw = getattr(profile, "tools_json", None)
    if isinstance(raw, (list, tuple)):
        return {str(item) for item in raw}
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError) as error:
        raise ProfileConfigurationError(
            "TOOLS_INVALID",
            "tools_json must be a JSON array of tool names",
            "tools_json",
        ) from error
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ProfileConfigurationError(
            "TOOLS_INVALID",
            "tools_json must be a JSON array of tool names",
            "tools_json",
        )
    return set(parsed)


def _byoa_bindings(release: Any, document: Optional[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """The declared field→transport bindings of a BYOA release, keyed by field."""
    raw: Any = None
    if release is not None and getattr(release, "byoa_config", None):
        raw = []
        for binding in release.byoa_config:
            raw.append(
                binding.model_dump(mode="json")
                if hasattr(binding, "model_dump")
                else binding
            )
    elif document is not None:
        raw = document.get("byoa_config")
    bindings: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            field = str(item.get("field") or "").strip().lower()
            transport = str(item.get("transport") or "").strip().lower()
            key = str(item.get("key") or "").strip()
            if field in BYOA_CONFIG_FIELDS and transport in BYOA_CONFIG_TRANSPORTS and key:
                bindings[field] = item
    return bindings


def validate_profile_configuration(
    profile: Any,
    release: Optional[AgentReleaseV1],
    *,
    release_json: Optional[Mapping[str, Any]] = None,
) -> None:
    """Reject a profile/release combination that cannot execute (ISSUE-03).

    One shared invariant for profile create/update and the study-creation freeze:

    * the profile must pin a registered release;
    * ``code4me2-agent`` requires a ``PACKAGED`` release; ``goose``/``codex``
      require a ``BYOA_EXTERNAL`` release;
    * a BYOA release must declare a command/package identity;
    * the selected release must be qualified (never withdrawn);
    * selected tools must belong to the framework's catalogue;
    * the approval option must be covered by the release's conformance evidence.

    Raises :class:`ProfileConfigurationError` (a ``ValueError`` whose ``str`` is
    ``"CODE: message"``). Nothing is mutated and no database session is needed.
    """
    framework = _framework(getattr(profile, "framework_version", None))
    document = _release_document(release, release_json)

    if release is None:
        raise ProfileConfigurationError(
            "RELEASE_UNRESOLVED",
            "the profile pins no registered release to execute",
            "release_id",
        )

    expected_mode = FRAMEWORK_DISTRIBUTION_MODES.get(framework)
    if expected_mode is None:
        raise ProfileConfigurationError(
            "FRAMEWORK_UNSUPPORTED",
            "framework_version must be one of "
            + ", ".join(sorted(FRAMEWORK_DISTRIBUTION_MODES)),
            "framework_version",
        )

    qualification = _qualification_value(release, document)
    if qualification in {status.value for status in _WITHDRAWN}:
        raise ProfileConfigurationError(
            "RELEASE_WITHDRAWN",
            f"release {getattr(release, 'release_id', '')!r} is withdrawn",
            "release_id",
        )
    if qualification != QualificationStatus.QUALIFIED.value:
        raise ProfileConfigurationError(
            "RELEASE_NOT_QUALIFIED",
            f"release {getattr(release, 'release_id', '')!r} is not qualified",
            "release_id",
        )

    mode = _release_mode(release)
    if not mode and document is not None:
        mode = str(
            document.get("distribution_mode") or DistributionMode.PACKAGED.value
        ).strip().upper()
    if mode != expected_mode:
        raise ProfileConfigurationError(
            "FRAMEWORK_DISTRIBUTION_MISMATCH",
            f"framework {framework!r} cannot execute a {mode} release; "
            f"it requires {expected_mode}",
            "release_id",
        )

    if mode == DistributionMode.BYOA_EXTERNAL.value:
        identity = getattr(release, "byoa_identity", None)
        if identity is None and document is not None:
            identity = (
                str(document.get("agent_command") or "").strip()
                or str(document.get("agent_package") or "").strip()
                or None
            )
        if not identity:
            raise ProfileConfigurationError(
                "DISTRIBUTION_IDENTITY_MISSING",
                "a BYOA release must declare an agent_command or agent_package identity",
                "release_id",
            )

        # ISSUE-03 Path A: every frozen profile field the profile actually sets
        # must be covered by a declared translation. A field with no binding is
        # refused here, before enrollment, instead of becoming an experimental
        # label that does not govern the external process. The provider
        # connection is deliberately *not* translatable: a participant-installed
        # agent uses the participant's own credentials.
        bindings = _byoa_bindings(release, document)
        tools = _profile_tools(profile)
        required_fields = {
            "model": bool(str(getattr(profile, "model", "") or "").strip()),
            "temperature": getattr(profile, "temperature", None) is not None,
            "max_steps": getattr(profile, "max_steps", None) is not None,
            "tools": bool(tools),
            "approval_policy": bool(
                str(getattr(profile, "approval_policy", "") or "").strip()
            ),
        }
        unmapped = sorted(
            field for field, needed in required_fields.items() if needed and field not in bindings
        )
        if unmapped:
            raise ProfileConfigurationError(
                "BYOA_CONFIG_UNMAPPED",
                "the release declares no configuration translation for "
                + ", ".join(unmapped)
                + "; the profile's values would not govern the external agent",
                "release_id",
            )
        for field, binding in bindings.items():
            if field != "tools":
                continue
            if str(binding.get("format") or "string").strip().lower() not in (
                "csv",
                "json",
            ):
                raise ProfileConfigurationError(
                    "BYOA_CONFIG_FORMAT_INVALID",
                    "a tools binding must use format 'csv' or 'json' to render a list",
                    "release_id",
                )

    unknown_tools = _profile_tools(profile) - set(tools_for_framework(framework))
    if unknown_tools:
        raise ProfileConfigurationError(
            "TOOLS_NOT_SUPPORTED",
            f"tools_json contains tools unsupported by {framework}: "
            + ", ".join(sorted(unknown_tools)),
            "tools_json",
        )

    option = str(getattr(profile, "approval_policy", None) or "").strip().lower()
    if option not in ALL_APPROVAL_OPTIONS:
        raise ProfileConfigurationError(
            "APPROVAL_POLICY_UNSUPPORTED",
            "approval_policy must be one of " + ", ".join(ALL_APPROVAL_OPTIONS),
            "approval_policy",
        )
    if not approval_option_verified(document, option):
        raise ProfileConfigurationError(
            "APPROVAL_OPTION_UNVERIFIED",
            f"approval option {option!r} is not covered by the release's "
            "conformance evidence",
            "approval_policy",
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

    ``profile`` is an ``AgentProfile`` row (or a schema-shaped stand-in) owning
    the pin (``profile_id``/``release_id``) and its executable config; the
    distribution mode, package/command identity and argument vector come from
    ``release`` alone. ``release`` is the already rehydrated registry release
    for ``release_id``, or ``None`` when it does not exist.

    When ``profile`` also exposes the executable config (a real profile row),
    that non-secret config is attached as ``agent_config`` so publication can
    freeze it into the immutable revision. ``funding_owner_user_id`` is the study
    owner whose connection grant funds execution.
    """
    if distribution_id is None:
        distribution_id = getattr(profile, "profile_id", None)
    release_id = getattr(profile, "release_id", None)
    agent_config = _agent_config(
        profile, release, connection, funding_owner_user_id
    )

    if release is not None and release.is_byoa:
        # A BYOA distribution is identity-pinned (from the release) and can
        # never be verified: there is no artifact to bind evidence to.
        identity_present = release.byoa_identity is not None
        return DistributionResolution(
            found=True,
            distribution_id=distribution_id,
            distribution_mode=DistributionMode.BYOA_EXTERNAL.value,
            release_id=release.release_id,
            agent_id=release.agent_id,
            version=release.version,
            agent_package=release.agent_package,
            agent_command=release.agent_command,
            agent_command_args=list(release.agent_command_args),
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

    mode = (
        _release_mode(release)
        if release is not None
        else DistributionMode.PACKAGED.value
    ) or DistributionMode.PACKAGED.value
    view = DistributionResolution(
        found=True,
        distribution_id=distribution_id,
        distribution_mode=mode,
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
