"""Field-level, cross-reference and safety validation for study protocols.

Validation never fails open. Every rejection is a typed :class:`ValidationError`
with a stable machine-readable
:class:`~research.study.protocol.enums.ValidationReasonCode` and a dotted ``field``
path. Cross-validation against the agent registry happens through an injected
:class:`ReleaseResolver`, so this package never imports the registry.

Weight normalization
--------------------

Condition weights are *relative* frequencies, not required to sum to one. The
deterministic normalization is ``w_i / sum(w)`` and is exposed through
:func:`normalized_condition_weights`. A protocol whose weights do not form a
normalizable positive total (missing, non-positive per condition, or a
non-positive/NaN total) is rejected with ``NON_POSITIVE_WEIGHT`` /
``EMPTY_WEIGHT_TOTAL`` rather than silently repaired.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Protocol, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from research.compatibility.enums import CapabilityId, CapabilityState

from .enums import (
    AssignmentStrategy,
    AssignmentUnit,
    CompletionPolicyKind,
    ReleaseResolutionStatus,
    RetentionAction,
    ValidationReasonCode,
    ValidationSeverity,
)
from .models import (
    ExplicitUnknown,
    FixedSchedule,
    ResolvedAgentConfig,
    ResolvedDistribution,
    RollingSchedule,
    StudyCondition,
    StudyProtocolV1,
)

_VALID_SCHEMA_VERSIONS = frozenset({"1"})

#: Known distribution contracts a release pin may declare.
_DISTRIBUTION_MODES = frozenset({"PACKAGED", "BYOA_EXTERNAL"})

# Key substrings that identify participant/session/account data or credentials.
# ``_SAFE_KEY_NAMES`` lists legitimate, non-secret keys that merely *contain* a
# flagged substring (e.g. a token count), so they are not false positives.
_SAFE_KEY_NAMES = frozenset(
    {
        "max_context_tokens",
        "max_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        # The funding owner is deliberately frozen (non-secret) so connection
        # authorization runs against the study owner, not the participant. It is
        # a UUID, never an email/name, and never a credential.
        "funding_owner_user_id",
    }
)
_IDENTIFIER_KEY_PARTS = (
    "participant",
    "session_id",
    "session_token",
    "user_id",
    "account_id",
    "subject_id",
)
_CREDENTIAL_KEY_PARTS = (
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "access_key",
    "client_secret",
    "cookie",
)
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
)

# Local absolute filesystem paths are never part of a portable, participant-
# independent protocol (Issue 02 section 8): they leak a machine layout and
# cannot be resolved on another host. Detection is intentionally conservative
# so URLs and relative references are not rejected.
_LOCAL_PATH_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]"),
    re.compile(
        r"^/(?:Users|home|root|Volumes|private|opt|usr|etc|var|tmp|Applications)(?:/|$)"
    ),
    re.compile(r"(?:^|[\s\"'(=])/(?:Users|home|root)/"),
)

_KNOWN_CAPABILITIES = frozenset(capability.value for capability in CapabilityId)
_KNOWN_CAPABILITY_STATES = frozenset(state.value for state in CapabilityState)


class ProtocolValidationError(ValueError):
    """Raised by imperative helpers; ``validate_protocol`` never raises for data."""


class ValidationError(BaseModel):
    """One typed reason a protocol cannot be published."""

    model_config = ConfigDict(extra="forbid")

    code: ValidationReasonCode
    field: str
    message: str
    severity: ValidationSeverity = ValidationSeverity.ERROR


class ReleaseResolution(BaseModel):
    """Outcome of resolving one pinned agent release."""

    model_config = ConfigDict(extra="forbid")

    status: ReleaseResolutionStatus
    agent_id: str
    release_id: Optional[str] = None
    version: Optional[str] = None
    artifact_digest: Optional[str] = None
    distribution_mode: str = "PACKAGED"
    message: Optional[str] = None


class ReleaseResolver(Protocol):
    """Interface the registry (or a test double) implements for cross-checks."""

    def resolve(
        self,
        agent_id: str,
        *,
        release_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> ReleaseResolution:  # pragma: no cover - structural protocol
        ...


class NullReleaseResolver:
    """Resolver that performs no external lookup.

    A reference is treated as resolved when it is *qualified* (has a
    ``release_id`` or ``version``). Digest pinning and ``latest`` are enforced
    structurally by the validator, so this is the correct default when no
    registry backend is wired up: it never invents a digest and never fails open
    on an unqualified reference.
    """

    def resolve(
        self,
        agent_id: str,
        *,
        release_id: Optional[str] = None,
        version: Optional[str] = None,
    ) -> ReleaseResolution:
        if not release_id and not version:
            return ReleaseResolution(
                status=ReleaseResolutionStatus.UNQUALIFIED,
                agent_id=agent_id,
                release_id=release_id,
                version=version,
                message="release reference names no release_id or version",
            )
        return ReleaseResolution(
            status=ReleaseResolutionStatus.RESOLVED,
            agent_id=agent_id,
            release_id=release_id,
            version=version,
        )


class DistributionResolution(BaseModel):
    """Resolved, non-secret view of one distribution (an ``AgentProfile``).

    Produced by a :class:`DistributionResolver` that has database access to the
    profile row and the release registry. The protocol validator never touches
    the database; it consumes this view to emit typed reasons. ``verified`` is
    DERIVED by the resolver from conformance evidence (never caller-supplied),
    and ``PACKAGED`` distributions additionally expose the resolved release
    identity and (platform-selected) artifact digest. ``BYOA_EXTERNAL``
    distributions expose a command/package identity and are never verified.
    """

    model_config = ConfigDict(extra="forbid")

    found: bool = False
    distribution_id: Optional[UUID] = None
    distribution_mode: str = "PACKAGED"
    release_id: Optional[str] = None
    agent_id: Optional[str] = None
    version: Optional[str] = None
    artifact_digest: Optional[str] = None
    agent_package: Optional[str] = None
    agent_command: Optional[str] = None
    agent_command_args: list[str] = Field(default_factory=list)
    verified: bool = False
    release_status: ReleaseResolutionStatus = ReleaseResolutionStatus.NOT_FOUND
    message: Optional[str] = None
    # Full non-secret profile config, frozen at publication (optional so pure
    # protocol tests and legacy revisions remain valid).
    agent_config: Optional[ResolvedAgentConfig] = None

    def to_frozen(self, *, resolved_at: Optional[datetime] = None) -> ResolvedDistribution:
        """Return the immutable pin written into a published revision."""
        return ResolvedDistribution(
            distribution_id=self.distribution_id
            or UUID(int=0),
            distribution_mode=self.distribution_mode,
            release_id=self.release_id,
            agent_id=self.agent_id,
            version=self.version,
            agent_package=self.agent_package,
            agent_command=self.agent_command,
            agent_command_args=list(self.agent_command_args),
            artifact_digest=self.artifact_digest,
            verified=self.verified,
            resolved_at=resolved_at,
            agent_config=self.agent_config,
        )


class DistributionResolver(Protocol):
    """Interface a distribution backend (or a test double) implements."""

    def resolve(self, distribution_id: UUID) -> DistributionResolution:  # pragma: no cover
        ...


class NullDistributionResolver:
    """Resolver that performs no external lookup.

    Without a wired backend a *draft* cannot be resolved, so every distribution
    is reported as not found. A published document that already carries its
    frozen ``resolved_distribution`` is validated from that pin directly (see
    :func:`_resolve_distribution`), so this default is correct for pure protocol
    tests and never fails open on an unqualified draft.
    """

    def resolve(self, distribution_id: UUID) -> DistributionResolution:
        return DistributionResolution(found=False, distribution_id=distribution_id)


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _iter_nodes(value: Any, path: str = "") -> Any:
    """Yield ``(path, key, scalar)`` triples for every node in ``value``."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, str(key), item
            yield from _iter_nodes(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            yield from _iter_nodes(item, child)


def _scan_document(data: Any) -> list[ValidationError]:
    """Reject participant/session/account identifiers and credentials."""
    errors: list[ValidationError] = []
    seen: set[tuple[str, str]] = set()

    for path, key, value in _iter_nodes(data):
        lowered = key.lower()
        if lowered in _SAFE_KEY_NAMES:
            continue
        if any(part in lowered for part in _IDENTIFIER_KEY_PARTS):
            signature = (path, "identifier")
            if signature not in seen:
                seen.add(signature)
                errors.append(
                    ValidationError(
                        code=ValidationReasonCode.FORBIDDEN_IDENTIFIER,
                        field=path,
                        message=(
                            "protocol must not contain participant/session/account "
                            f"identifiers (found key {key!r})"
                        ),
                    )
                )
        elif any(part in lowered for part in _CREDENTIAL_KEY_PARTS):
            signature = (path, "credential")
            if signature not in seen:
                seen.add(signature)
                errors.append(
                    ValidationError(
                        code=ValidationReasonCode.FORBIDDEN_CREDENTIAL,
                        field=path,
                        message=(
                            f"protocol must not contain credentials (found key {key!r})"
                        ),
                    )
                )
        elif isinstance(value, str) and any(
            pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS
        ):
            signature = (path, "credential-value")
            if signature not in seen:
                seen.add(signature)
                errors.append(
                    ValidationError(
                        code=ValidationReasonCode.FORBIDDEN_CREDENTIAL,
                        field=path,
                        message="protocol value looks like a credential",
                    )
                )
        elif isinstance(value, str) and any(
            pattern.search(value) for pattern in _LOCAL_PATH_PATTERNS
        ):
            signature = (path, "local-path")
            if signature not in seen:
                seen.add(signature)
                errors.append(
                    ValidationError(
                        code=ValidationReasonCode.FORBIDDEN_LOCAL_PATH,
                        field=path,
                        message="protocol values must not contain local absolute paths",
                    )
                )

    return errors


def _schema_error(error: PydanticValidationError) -> ValidationError:
    details = error.errors()
    first = details[0] if details else {}
    location = first.get("loc") or ()
    field = ".".join(str(part) for part in location)
    return ValidationError(
        code=ValidationReasonCode.SCHEMA_INVALID,
        field=field,
        message=str(first.get("msg", "protocol document failed schema validation")),
    )


def _is_latest(value: Optional[str]) -> bool:
    return bool(value) and str(value).strip().lower() == "latest"


def _check_conditions(protocol: StudyProtocolV1) -> list[ValidationError]:
    errors: list[ValidationError] = []
    conditions = protocol.conditions

    if not conditions:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.NO_CONDITIONS,
                field="conditions",
                message="a study protocol must declare at least one condition",
            )
        )
        return errors

    seen: set[str] = set()
    for index, condition in enumerate(conditions):
        field = f"conditions[{index}]"
        if condition.condition_id in seen:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.DUPLICATE_CONDITION_ID,
                    field=f"{field}.condition_id",
                    message=f"condition_id {condition.condition_id!r} is duplicated",
                )
            )
        seen.add(condition.condition_id)

        weight = condition.weight
        if weight is None or not math.isfinite(weight) or weight <= 0:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.NON_POSITIVE_WEIGHT,
                    field=f"{field}.weight",
                    message="condition weight must be a finite positive number",
                )
            )

    finite_weights = [
        condition.weight
        for condition in conditions
        if condition.weight is not None and math.isfinite(condition.weight)
    ]
    if not finite_weights or sum(finite_weights) <= 0:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.EMPTY_WEIGHT_TOTAL,
                field="conditions",
                message="condition weights must sum to a positive total",
            )
        )

    return errors


def _check_schedule(protocol: StudyProtocolV1, now: datetime) -> list[ValidationError]:
    schedule = protocol.schedule
    if isinstance(schedule, FixedSchedule):
        if schedule.end_at is not None and schedule.end_at <= schedule.start_at:
            return [
                ValidationError(
                    code=ValidationReasonCode.FIXED_SCHEDULE_INVALID,
                    field="schedule.end_at",
                    message="fixed schedule end_at must be after start_at",
                )
            ]
        if schedule.end_at is not None and schedule.end_at < now:
            return [
                ValidationError(
                    code=ValidationReasonCode.FIXED_SCHEDULE_EXPIRED,
                    field="schedule.end_at",
                    message="fixed schedule already ended",
                )
            ]
        return []

    if isinstance(schedule, RollingSchedule):
        if schedule.duration_seconds is None or schedule.duration_seconds <= 0:
            return [
                ValidationError(
                    code=ValidationReasonCode.ROLLING_DURATION_INVALID,
                    field="schedule.duration_seconds",
                    message="rolling schedule duration_seconds must be present and > 0",
                )
            ]
        return []

    return [
        ValidationError(
            code=ValidationReasonCode.SCHEMA_INVALID,
            field="schedule.kind",
            message="schedule must be FIXED or ROLLING",
        )
    ]


def _check_assignment(protocol: StudyProtocolV1) -> list[ValidationError]:
    errors: list[ValidationError] = []
    assignment = protocol.assignment

    if assignment.unit != AssignmentUnit.ENROLLMENT:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.ASSIGNMENT_UNIT_NOT_ENROLLMENT,
                field="assignment.unit",
                message="issue 02 only supports enrollment-level assignment",
            )
        )

    known_strategies = {strategy.value for strategy in AssignmentStrategy}
    if assignment.strategy not in known_strategies:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.ASSIGNMENT_STRATEGY_UNKNOWN,
                field="assignment.strategy",
                message=f"unknown assignment strategy {assignment.strategy!r}",
            )
        )
    elif (
        assignment.strategy == AssignmentStrategy.STRATIFIED.value
        and not assignment.stratification
    ):
        errors.append(
            ValidationError(
                code=ValidationReasonCode.ASSIGNMENT_STRATIFICATION_MISSING,
                field="assignment.stratification",
                message="stratified assignment requires stratification keys",
            )
        )

    return errors


def _resolve_distribution(
    condition: StudyCondition,
    resolver: Optional[DistributionResolver],
) -> DistributionResolution:
    """Resolve one condition's distribution to its non-secret view.

    A wired :class:`DistributionResolver` is authoritative. Without one, a
    published revision's frozen ``resolved_distribution`` is trusted
    structurally; a draft that only names an id is reported as not found.
    """
    if resolver is not None:
        return resolver.resolve(condition.distribution_id)
    frozen = condition.resolved_distribution
    if frozen is None:
        return DistributionResolution(
            found=False, distribution_id=condition.distribution_id
        )
    return DistributionResolution(
        found=True,
        distribution_id=frozen.distribution_id,
        distribution_mode=frozen.distribution_mode,
        release_id=frozen.release_id,
        agent_id=frozen.agent_id,
        version=frozen.version,
        artifact_digest=frozen.artifact_digest,
        agent_package=frozen.agent_package,
        agent_command=frozen.agent_command,
        agent_command_args=list(frozen.agent_command_args),
        verified=frozen.verified,
        release_status=(
            ReleaseResolutionStatus.RESOLVED
            if frozen.verified
            else ReleaseResolutionStatus.UNQUALIFIED
        ),
    )


def _unverified_error(
    field: str,
    *,
    distribution: DistributionResolution,
    actor_is_admin: bool,
) -> ValidationError:
    """One typed reason a distribution is not verified.

    A non-administrator researcher may only author a condition from a VERIFIED
    distribution, so this is a hard ``ERROR`` for them. An administrator may
    deliberately select an unverified distribution: it is flagged as a
    ``WARNING`` that publish surfaces but does not block.
    """
    mode = str(distribution.distribution_mode or "PACKAGED").strip().upper()
    if mode == "BYOA_EXTERNAL":
        message = (
            "the BYOA release lacks passing tests or a supported agent identity"
        )
    else:
        message = (
            "the distribution's release has no passing producer tests (unverified)"
        )
    return ValidationError(
        code=ValidationReasonCode.DISTRIBUTION_UNVERIFIED,
        field=field,
        message=message,
        severity=(
            ValidationSeverity.WARNING
            if actor_is_admin
            else ValidationSeverity.ERROR
        ),
    )


def _check_distributions(
    protocol: StudyProtocolV1,
    resolver: Optional[DistributionResolver],
    actor_is_admin: bool,
) -> list[ValidationError]:
    errors: list[ValidationError] = []

    for index, condition in enumerate(protocol.conditions):
        field = f"conditions[{index}].distribution_id"
        frozen = condition.resolved_distribution

        distribution = _resolve_distribution(condition, resolver)
        if not distribution.found:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.AGENT_PROFILE_NOT_FOUND,
                    field=field,
                    message=(
                        f"distribution {condition.distribution_id} does not exist"
                    ),
                )
            )
            continue

        mode = str(distribution.distribution_mode or "PACKAGED").strip().upper()
        if mode not in _DISTRIBUTION_MODES:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.AGENT_RELEASE_MODE_UNKNOWN,
                    field=f"{field}.distribution_mode",
                    message=(
                        "distribution_mode must be one of "
                        + ", ".join(sorted(_DISTRIBUTION_MODES))
                    ),
                )
            )
            continue

        if _is_latest(distribution.release_id) or _is_latest(distribution.version):
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.AGENT_RELEASE_LATEST,
                    field=field,
                    message="a distribution must pin a release, not 'latest'",
                )
            )

        if mode == "BYOA_EXTERNAL":
            # A participant-installed agent is resolved from a command/package
            # identity, never from an artifact digest.
            if not (
                distribution.release_id
                or distribution.agent_package
                or distribution.agent_command
                or distribution.agent_id
            ):
                errors.append(
                    ValidationError(
                        code=ValidationReasonCode.AGENT_RELEASE_IDENTITY_REQUIRED,
                        field=field,
                        message=(
                            "a BYOA_EXTERNAL distribution must declare an "
                            "agent_package or agent_command identity"
                        ),
                    )
                )
            # No conformance evidence can be bound to a participant-installed
            # agent, so a BYOA distribution is always unverified.
            errors.append(
                _unverified_error(
                    field, distribution=distribution, actor_is_admin=actor_is_admin
                )
            )
            continue

        # PACKAGED: the distribution MUST pin a resolvable release id.
        if not distribution.release_id:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.AGENT_RELEASE_UNPINNED,
                    field=field,
                    message="a PACKAGED distribution must pin a release_id",
                )
            )
            continue

        # A PACKAGED pin must never embed a mutable executable command.
        if frozen is not None and frozen.agent_command:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.MUTABLE_AGENT_COMMAND,
                    field=f"{field}.resolved_distribution.agent_command",
                    message=(
                        "a PACKAGED distribution must not embed an executable "
                        "command"
                    ),
                )
            )

        if distribution.release_status == ReleaseResolutionStatus.NOT_FOUND:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.RELEASE_UNRESOLVED,
                    field=field,
                    message=(
                        f"release {distribution.release_id!r} could not be resolved"
                    ),
                )
            )
        elif distribution.release_status == ReleaseResolutionStatus.WITHDRAWN:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.RELEASE_WITHDRAWN,
                    field=field,
                    message=(
                        f"release {distribution.release_id!r} has been withdrawn"
                    ),
                )
            )
        elif not distribution.verified:
            # The release exists but lacks passing producer tests.
            errors.append(
                _unverified_error(
                    field, distribution=distribution, actor_is_admin=actor_is_admin
                )
            )
        elif not distribution.artifact_digest:
            # A verified PACKAGED distribution must still resolve to a concrete
            # digest-pinned artifact; without one there is nothing to launch.
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.AGENT_RELEASE_UNPINNED,
                    field=field,
                    message=(
                        f"release {distribution.release_id!r} publishes no "
                        "digest-pinned artifact"
                    ),
                )
            )

        # A frozen digest that disagrees with the registry is a hard block: the
        # immutable revision no longer describes the artifact it claims to.
        if (
            frozen is not None
            and frozen.artifact_digest
            and distribution.artifact_digest
            and frozen.artifact_digest != distribution.artifact_digest
        ):
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.RELEASE_DIGEST_MISMATCH,
                    field=f"{field}.resolved_distribution.artifact_digest",
                    message="frozen artifact digest does not match the registry",
                )
            )

    return errors


def freeze_protocol_distributions(
    protocol: StudyProtocolV1,
    resolver: Optional[DistributionResolver],
    *,
    now: Optional[datetime] = None,
) -> StudyProtocolV1:
    """Return a copy of ``protocol`` with every condition's pin frozen.

    The resolved pin (release identity, artifact digest, distribution mode and
    the derived ``verified`` flag) is written into ``resolved_distribution`` so
    an immutable revision always records exactly which artifact produced its
    data. A condition whose distribution cannot be resolved keeps ``None`` and
    is rejected by validation rather than silently frozen.
    """
    timestamp = _now(now)
    frozen_conditions: list[StudyCondition] = []
    for condition in protocol.conditions:
        distribution = _resolve_distribution(condition, resolver)
        frozen_conditions.append(
            condition.model_copy(
                update={
                    "resolved_distribution": (
                        distribution.to_frozen(resolved_at=timestamp)
                        if distribution.found
                        else None
                    )
                }
            )
        )
    return protocol.model_copy(update={"conditions": frozen_conditions})


def _check_environment(protocol: StudyProtocolV1) -> list[ValidationError]:
    errors: list[ValidationError] = []
    environment = protocol.environment_requirements

    if not environment.expected_protocol_version:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.ENVIRONMENT_PROTOCOL_VERSION_MISSING,
                field="environment_requirements.expected_protocol_version",
                message="an expected ACP protocol version is required",
            )
        )

    for index, required in enumerate(environment.required_capabilities):
        capability = required.capability
        field = f"environment_requirements.required_capabilities[{index}]"
        if capability not in _KNOWN_CAPABILITIES:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.UNKNOWN_REQUIRED_CAPABILITY,
                    field=field,
                    severity=ValidationSeverity.NEEDS_REVIEW,
                    message=(
                        f"required capability {capability!r} is not in the known "
                        "capability vocabulary"
                    ),
                )
            )
        elif required.require_state not in _KNOWN_CAPABILITY_STATES:
            errors.append(
                ValidationError(
                    code=ValidationReasonCode.UNKNOWN_REQUIRED_CAPABILITY,
                    field=f"{field}.require_state",
                    severity=ValidationSeverity.NEEDS_REVIEW,
                    message=(
                        f"required state {required.require_state!r} is not a known "
                        "capability state"
                    ),
                )
            )

    if isinstance(environment.host_kind, ExplicitUnknown):
        errors.append(
            ValidationError(
                code=ValidationReasonCode.UNKNOWN_ENVIRONMENT_REQUIREMENT,
                field="environment_requirements.host_kind",
                severity=ValidationSeverity.NEEDS_REVIEW,
                message="host_kind is explicitly unknown and must be resolved",
            )
        )

    return errors


def _check_retention(protocol: StudyProtocolV1) -> list[ValidationError]:
    """Reject retention policies that are not supported to publish.

    The supported policy retains uploaded data according to consent. A
    participant-selected ``DELETE_ALL`` is not authorable: advertising a
    deletion policy that is not the executed one is misleading.
    """
    if protocol.privacy_policy.retention_action == RetentionAction.DELETE_ALL:
        return [
            ValidationError(
                code=ValidationReasonCode.RETENTION_UNSUPPORTED,
                field="privacy_policy.retention_action",
                message=(
                    "DELETE_ALL is not a supported published retention policy; "
                    "retain uploaded data according to consent"
                ),
            )
        ]
    return []


def _check_completion(protocol: StudyProtocolV1) -> list[ValidationError]:
    completion = protocol.completion
    if completion.policy == CompletionPolicyKind.TARGET_CAPACITY and (
        completion.target_enrollments is None or completion.target_enrollments <= 0
    ):
        return [
            ValidationError(
                code=ValidationReasonCode.COMPLETION_TARGET_INVALID,
                field="completion.target_enrollments",
                message="TARGET_CAPACITY completion requires a positive target",
            )
        ]
    return []


def _check_protocol(
    protocol: StudyProtocolV1,
    resolver: Optional[DistributionResolver],
    now: datetime,
    *,
    actor_is_admin: bool = False,
) -> list[ValidationError]:
    errors: list[ValidationError] = []

    if protocol.schema_version not in _VALID_SCHEMA_VERSIONS:
        errors.append(
            ValidationError(
                code=ValidationReasonCode.UNSUPPORTED_SCHEMA_VERSION,
                field="schema_version",
                message=(
                    f"unsupported schema_version {protocol.schema_version!r}; "
                    f"expected one of {sorted(_VALID_SCHEMA_VERSIONS)}"
                ),
            )
        )

    errors.extend(_check_conditions(protocol))
    errors.extend(_check_schedule(protocol, now))
    errors.extend(_check_assignment(protocol))
    errors.extend(_check_retention(protocol))
    errors.extend(_check_environment(protocol))
    errors.extend(_check_completion(protocol))
    errors.extend(_check_distributions(protocol, resolver, actor_is_admin))
    return errors


def validate_protocol(
    document: Union[StudyProtocolV1, Mapping[str, Any]],
    *,
    distribution_resolver: Optional[DistributionResolver] = None,
    actor_is_admin: bool = False,
    now: Optional[datetime] = None,
) -> list[ValidationError]:
    """Return every reason ``document`` cannot be published.

    An empty list means the document is publishable. A raw mapping is scanned
    for forbidden identifiers/credentials *before* schema parsing so leakage is
    always reported with a typed reason even when the extra keys also violate
    the schema.

    ``distribution_resolver`` resolves each condition's ``distribution_id`` to
    its release/verification view. ``actor_is_admin`` selects the severity of a
    ``DISTRIBUTION_UNVERIFIED`` reason: a hard error for a researcher, a warning
    for an administrator.
    """
    if isinstance(document, StudyProtocolV1):
        protocol: Optional[StudyProtocolV1] = document
        raw: Any = document.model_dump(mode="json")
    elif isinstance(document, Mapping):
        protocol = None
        raw = dict(document)
    else:
        return [
            ValidationError(
                code=ValidationReasonCode.SCHEMA_INVALID,
                field="",
                message="protocol must be a StudyProtocolV1 or a mapping",
            )
        ]

    errors = _scan_document(raw)

    if protocol is None:
        try:
            protocol = StudyProtocolV1.model_validate(raw)
        except PydanticValidationError as error:
            errors.append(_schema_error(error))
            return errors

    errors.extend(
        _check_protocol(
            protocol,
            distribution_resolver,
            _now(now),
            actor_is_admin=actor_is_admin,
        )
    )
    return errors


def scan_document_safety(
    document: Union[StudyProtocolV1, Mapping[str, Any]]
) -> list[ValidationError]:
    """Return only the forbidden-identifier/credential findings for a document.

    Used by draft creation, which must never persist a document that leaks a
    participant/session/account identifier or a credential even though the draft
    is not yet publishable.
    """
    if isinstance(document, StudyProtocolV1):
        raw: Any = document.model_dump(mode="json")
    elif isinstance(document, Mapping):
        raw = document
    else:
        return []
    return _scan_document(raw)


def blocking_errors(errors: list[ValidationError]) -> list[ValidationError]:
    """The subset of ``errors`` that must block publication."""
    return [
        error for error in errors if error.severity != ValidationSeverity.WARNING
    ]


def warnings(errors: list[ValidationError]) -> list[ValidationError]:
    """The informational, non-blocking subset of ``errors``."""
    return [error for error in errors if error.severity == ValidationSeverity.WARNING]


def is_publishable(errors: list[ValidationError]) -> bool:
    """Whether a validation result permits publication.

    ``ERROR`` and ``NEEDS_REVIEW`` both block publication (the latter asks for
    human review rather than an automatic fix); ``WARNING`` is informational and
    does not block.
    """
    return not blocking_errors(errors)


def normalized_condition_weights(protocol: StudyProtocolV1) -> dict[str, float]:
    """Deterministic ``w_i / sum(w)`` map for a valid protocol.

    Raises :class:`ProtocolValidationError` when the weights do not form a
    normalizable positive total, so a caller can never divide by zero or
    silently drop a condition.
    """
    weights = {
        condition.condition_id: condition.weight for condition in protocol.conditions
    }
    total = sum(weight for weight in weights.values() if weight is not None)
    if not weights or not math.isfinite(total) or total <= 0:
        raise ProtocolValidationError("condition weights do not sum to a positive total")
    return {
        condition_id: weight / total
        for condition_id, weight in weights.items()
    }
