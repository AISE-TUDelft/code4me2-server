"""Pydantic v2 contracts for the versioned study protocol (``StudyProtocolV1``).

Semantics that matter for reproducibility:

* ``None`` means an *intentional inherited/default* value. It is never coerced
  to a concrete value, and a missing field dumps back as ``None``.
* :class:`ExplicitUnknown` is the typed *insufficient evidence* state. It is a
  distinct value from ``None`` and is never normalized to a default.
* Condition order is not meaningful for execution, so the canonical serializer
  sorts conditions (and other set-like collections) by stable identity before
  hashing. See :mod:`research.study.protocol.canonical`.

The document is draft-only metadata plus policy. It must never carry a
participant/session/account identifier or a credential; those are rejected by
:mod:`research.study.protocol.validation`.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from typing import Any, Literal, Optional, Union
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    AssignmentStrategy,
    AssignmentUnit,
    CompletionPolicyKind,
    RetentionAction,
    ScheduleKind,
    TelemetryFieldClass,
)

# Document-wide policy: unknown keys are rejected so a typo cannot silently
# drop a policy. Draft content is validated before it is ever persisted.
_BASE_CONFIG = ConfigDict(extra="forbid")


class ExplicitUnknown(BaseModel):
    """Typed "insufficient evidence" marker.

    Using a model (rather than ``None`` or the string ``"unknown"``) means an
    unknown value survives canonical serialization and round-trips as a
    distinct state, so a consumer can never mistake it for an inherited default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["UNKNOWN"] = "UNKNOWN"


class StudyMetadata(BaseModel):
    """Draft-only, non-secret descriptive metadata."""

    model_config = _BASE_CONFIG

    name: str
    description: Optional[str] = None
    owner: Optional[str] = None


class FixedSchedule(BaseModel):
    """A study that accepts data between two absolute instants."""

    model_config = _BASE_CONFIG

    kind: Literal[ScheduleKind.FIXED] = ScheduleKind.FIXED
    start_at: datetime
    end_at: Optional[datetime] = None


class RollingSchedule(BaseModel):
    """A study whose window is a duration measured from enrollment."""

    model_config = _BASE_CONFIG

    kind: Literal[ScheduleKind.ROLLING] = ScheduleKind.ROLLING
    # Required and strictly positive for publication; ``None`` is an explicit
    # missing value rather than an implicit default.
    duration_seconds: Optional[int] = None


Schedule = Union[FixedSchedule, RollingSchedule]


class EnrollmentPolicy(BaseModel):
    """How many units the study accepts and whether re-entry is allowed."""

    model_config = _BASE_CONFIG

    # ``None`` = inherit the server default capacity; ExplicitUnknown = capacity
    # is not yet known and must be resolved before publication.
    capacity: Optional[Union[int, ExplicitUnknown]] = None
    allow_reentry: Optional[bool] = None


class AssignmentPolicy(BaseModel):
    """How the assignment service maps an enrollment unit to a condition.

    Assignments are always sticky: an existing assignment for the same
    ``(enrollment_id, study_revision_id)`` is reused and never re-randomized.
    There is no runtime toggle. Reallocation, if ever required, must be defined
    by a successor revision rather than by mutating an existing assignment.
    """

    model_config = _BASE_CONFIG

    unit: AssignmentUnit = AssignmentUnit.ENROLLMENT
    # Free string, not the enum: an unknown strategy must be preserved so the
    # validator can reject it with a typed reason instead of failing to parse.
    strategy: str = AssignmentStrategy.WEIGHTED_RANDOM.value
    # Required when ``strategy`` is STRATIFIED; stable stratum key names.
    stratification: Optional[list[str]] = None


class ResolvedAgentConfig(BaseModel):
    """The frozen, non-secret execution config of a condition's profile.

    Publication copies these values into the revision so a later profile edit
    changes neither historical display nor a published study's task/bootstrap
    configuration. It deliberately contains no endpoint URL, secret reference or
    secret value.
    """

    model_config = _BASE_CONFIG

    profile_id: UUID
    name: str
    model: str
    framework_version: str
    tools_json: str
    approval_policy: str
    max_steps: int
    temperature: Optional[float] = None
    max_context_tokens: Optional[int] = None
    connection_id: Optional[UUID] = None
    connection_label: Optional[str] = None
    # The researcher who funds the connection grant for this condition: the
    # study owner (``Study.created_by``) at publication. Non-secret and never an
    # email/name, so it is safe to freeze and to include in the digest.
    funding_owner_user_id: Optional[UUID] = None


class ResolvedDistribution(BaseModel):
    """The distribution pin, frozen and written at publication.

    A draft condition only names an ``AgentProfile`` (a *distribution*) by its
    opaque ``distribution_id``. Because distributions are mutable, admin-editable
    rows, a published revision freezes the exact resolved pin here so an
    immutable revision can always answer "which artifact produced this data?".

    ``PACKAGED`` distributions resolve to a digest-pinned registry release; a
    participant-installed ``BYOA_EXTERNAL`` distribution carries the command /
    package identity and no digest. ``verified`` is derived at resolution time
    and is informational once frozen (the frozen value is never re-derived).
    """

    model_config = _BASE_CONFIG

    distribution_id: UUID
    distribution_mode: str = "PACKAGED"
    release_id: Optional[str] = None
    agent_id: Optional[str] = None
    version: Optional[str] = None
    agent_package: Optional[str] = None
    agent_command: Optional[str] = None
    agent_command_args: list[str] = Field(default_factory=list)
    artifact_digest: Optional[str] = None
    verified: bool = False
    resolved_at: Optional[datetime] = None
    # Full, non-secret profile config frozen at publication.
    agent_config: Optional[ResolvedAgentConfig] = None


class StudyCondition(BaseModel):
    """One experimental arm: its weight and the distribution it runs.

    ``distribution_id`` names a server-side ``AgentProfile`` by opaque id only.
    The profile is the *distribution*: it holds the provider ``base_url``, the
    ``api_key_ref`` *name* (never the secret), the model and the artifact pin
    (``release_id`` for ``PACKAGED``; ``agent_package``/``agent_command`` for
    ``BYOA_EXTERNAL``). None of that is duplicated into the draft document.

    ``resolved_distribution`` is absent on a draft and is populated at
    publication with the frozen, resolved pin (never authored by a caller).
    """

    model_config = _BASE_CONFIG

    condition_id: str
    name: Optional[str] = None
    # Relative, strictly positive frequency. Weights are normalized
    # deterministically at assignment time (see
    # :func:`research.study.protocol.validation.normalized_condition_weights`).
    weight: float
    # Opaque reference to an existing AgentProfile/"distribution" (validated by
    # the backend, which is the only layer with database access). Never a secret.
    distribution_id: UUID
    adapter_version: Optional[str] = None
    # Condition-scoped, non-secret overrides. Null/absent means "inherit".
    declared_overrides: dict[str, Any] = Field(default_factory=dict)
    # Frozen at publish; never supplied by a caller and rejected if present on a
    # draft that has not been resolved by the server.
    resolved_distribution: Optional[ResolvedDistribution] = None


class SessionPolicy(BaseModel):
    """Timeouts that govern an enrolled session's lifecycle."""

    model_config = _BASE_CONFIG

    idle_timeout_seconds: Optional[int] = None
    resume_grace_seconds: Optional[int] = None
    heartbeat_seconds: Optional[int] = None


class TelemetryPolicy(BaseModel):
    """Which classes of telemetry fields the study permits capturing."""

    model_config = _BASE_CONFIG

    allowed_field_classes: list[TelemetryFieldClass] = Field(default_factory=list)


class PrivacyPolicy(BaseModel):
    """Retention and deletion behavior for enrolled data."""

    model_config = _BASE_CONFIG

    retention_action: RetentionAction
    retention_days: Optional[int] = None


class CapabilityExpectation(BaseModel):
    """One required host/agent capability predicate.

    ``capability`` is a free string rather than an enum so an unknown-but-
    required capability is preserved and flagged ``NEEDS_REVIEW`` instead of
    being silently dropped by parsing.
    """

    model_config = _BASE_CONFIG

    capability: str
    require_state: str = "SUPPORTED"
    evidence_ref: Optional[str] = None


class EnvironmentRequirements(BaseModel):
    """Host/agent compatibility a revision must satisfy before launch."""

    model_config = _BASE_CONFIG

    # Expected negotiated ACP protocol version. Required for publication.
    expected_protocol_version: Optional[str] = None
    required_capabilities: list[CapabilityExpectation] = Field(default_factory=list)
    # Untested-but-expected environment facts; explicit unknown is allowed and
    # blocks publication with NEEDS_REVIEW.
    host_kind: Optional[Union[str, ExplicitUnknown]] = None


class CompletionPolicy(BaseModel):
    """When the study stops accepting new enrollments."""

    model_config = _BASE_CONFIG

    policy: CompletionPolicyKind = CompletionPolicyKind.MANUAL
    target_enrollments: Optional[int] = None


class SchemaGovernance(BaseModel):
    """How the protocol schema itself may change for this study."""

    model_config = _BASE_CONFIG

    owner: Optional[str] = None
    change_policy: Optional[str] = None
    compatible_from_schema_versions: list[str] = Field(default_factory=list)


class SurveyHook(BaseModel):
    """Optional, versioned survey attachment point."""

    model_config = _BASE_CONFIG

    survey_id: str
    trigger: str
    version: Optional[str] = None


class StudyProtocolV1(BaseModel):
    """The complete, validated experimental contract (schema version ``1``)."""

    model_config = _BASE_CONFIG

    schema_version: str = "1"
    study_id: UUID
    metadata: StudyMetadata
    # Explicit discriminated union: a document must choose fixed vs rolling.
    schedule: Schedule = Field(discriminator="kind")
    enrollment: EnrollmentPolicy = Field(default_factory=EnrollmentPolicy)
    assignment: AssignmentPolicy = Field(default_factory=AssignmentPolicy)
    conditions: list[StudyCondition] = Field(default_factory=list)
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
    telemetry_policy: TelemetryPolicy = Field(default_factory=TelemetryPolicy)
    privacy_policy: PrivacyPolicy
    # Deprecated, tolerated only so existing protocol fixtures and the website
    # editor payload keep parsing: consent is now a single global text accepted
    # once at join and is never stored, versioned or checked per study. Remove
    # this field once the editor and fixtures stop sending `consent`.
    consent: Optional[dict[str, Any]] = None
    environment_requirements: EnvironmentRequirements = Field(
        default_factory=EnvironmentRequirements
    )
    completion: CompletionPolicy = Field(default_factory=CompletionPolicy)
    schema_governance: Optional[SchemaGovernance] = None
    survey_hooks: list[SurveyHook] = Field(default_factory=list)
