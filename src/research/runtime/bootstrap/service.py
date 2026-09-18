"""Bootstrap composition: validate everything, then project and sign.

Nothing is issued until the enrollment is active with consent, the study window
is open, the revision is published, the assignment matches, and the pinned agent
release is qualified and resolvable for the participant platform. A revision that
declares required capabilities additionally needs a COMPATIBLE compatibility
result; a revision that declares none may proceed without a receipt. Every
failure is a typed block; the service never fails open and never invents a
default arm or fallback artifact.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from research.canonical import canonical_hash
from research.compatibility.enums import CompatibilityDecision
from research.participants.enums import EnrollmentStatus
from research.study.protocol.enums import (
    ReleaseResolutionStatus,
    RevisionStatus,
    TelemetryFieldClass,
)
from research.study.protocol.models import FixedSchedule, RollingSchedule, StudyProtocolV1
from research.telemetry.enums import CoverageState

from .capability import issue_capability
from .models import (
    BootstrapAgentProfile,
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
    ResearchSessionRef,
)
from .signer import sign_manifest

if TYPE_CHECKING:
    from research.compatibility.models import CompatibilityResult
    from research.participants.models import Enrollment
    from research.runtime.assignment.models import AssignmentV1
    from research.study.agents.models import AgentReleaseV1
    from research.study.protocol.publication import StudyRevision
    from research.study.protocol.validation import ReleaseResolver

__all__ = [
    "BootstrapSigningContext",
    "EphemeralSessionFactory",
    "SessionFactory",
    "compose_bootstrap",
]

_BASE = ConfigDict(extra="forbid")


class SessionFactory(Protocol):
    """Creates (or reuses) a research session for an enrollment."""

    def create_for_enrollment(
        self,
        enrollment: Enrollment,
        revision: StudyRevision,
        now: datetime,
        context_id: str = "",
    ) -> ResearchSessionRef:  # pragma: no cover - structural protocol
        ...


class EphemeralSessionFactory:
    """Test-only factory: returns a fresh session ref and persists nothing.

    This is **not** a production default. The production path always injects a
    database-backed factory (:class:`backend.routers.research.bootstrap._PersistentSessionFactory`),
    so a manifest's ``research_session_id`` resolves through the session store.
    A build without a database passes this explicitly (tests); ``compose_bootstrap``
    has no default factory and never falls back to an in-memory one.
    """

    def create_for_enrollment(
        self,
        enrollment: Enrollment,
        revision: StudyRevision,
        now: datetime,
        context_id: str = "",
    ) -> ResearchSessionRef:
        return ResearchSessionRef(research_session_id=uuid.uuid4(), opened_at=now)


class BootstrapSigningContext(BaseModel):
    """Injected signing parameters for manifest and capability issuance."""

    model_config = _BASE

    secret: str
    capability_ttl_seconds: int = 900
    audience: str = "research-runtime"
    scope: list[str] = Field(
        default_factory=lambda: [
            "telemetry:write",
            "session:heartbeat",
            "session:close",
        ]
    )


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _blocked(
    code: BootstrapReasonCode, message: str, field: str = ""
) -> BootstrapResult:
    return BootstrapResult(
        outcome=BootstrapOutcome.BLOCKED,
        reason=code,
        issue=BootstrapIssue(code=code, message=message, field=field),
    )


def _schedule_issue(
    protocol: StudyProtocolV1, enrollment: Enrollment, now: datetime
) -> Optional[BootstrapIssue]:
    schedule = protocol.schedule
    if isinstance(schedule, FixedSchedule):
        if now < schedule.start_at:
            return BootstrapIssue(
                code=BootstrapReasonCode.STUDY_NOT_OPEN,
                message="the study has not opened yet",
                field="schedule.start_at",
            )
        if schedule.end_at is not None and now > schedule.end_at:
            return BootstrapIssue(
                code=BootstrapReasonCode.STUDY_CLOSED,
                message="the study window has closed",
                field="schedule.end_at",
            )
        return None
    if isinstance(schedule, RollingSchedule):
        duration = schedule.duration_seconds
        if duration is None or duration <= 0:
            return BootstrapIssue(
                code=BootstrapReasonCode.STUDY_CLOSED,
                message="rolling duration is not usable",
                field="schedule.duration_seconds",
            )
        if now > enrollment.enrolled_at + timedelta(seconds=duration):
            return BootstrapIssue(
                code=BootstrapReasonCode.STUDY_CLOSED,
                message="the rolling enrollment window has closed",
                field="schedule.duration_seconds",
            )
    return None


def compose_bootstrap(
    enrollment: Enrollment,
    revision: StudyRevision,
    assignment: AssignmentV1,
    release: AgentReleaseV1,
    compatibility_ref: Optional[str],
    session_factory: SessionFactory,
    signer: BootstrapSigningContext,
    now: Optional[datetime] = None,
    *,
    compatibility_result: Optional[CompatibilityResult] = None,
    platform: Optional[tuple[str, str]] = None,
    release_resolver: Optional[ReleaseResolver] = None,
    agent_profile: Optional[BootstrapAgentProfile] = None,
    kill_switch_check: Optional[Callable[[], bool]] = None,
    context_id: str = "",
) -> BootstrapResult:
    """Compose a signed, short-lived, secret-free bootstrap manifest."""
    timestamp = _now(now)

    # Operator kill switch (Issue 13): when engaged, no new session is issued.
    # The check is injected so this service keeps no operations dependency.
    if kill_switch_check is not None and kill_switch_check():
        return _blocked(
            BootstrapReasonCode.KILL_SWITCH_ENGAGED,
            "an operator kill switch is engaged for this scope",
            "kill_switch",
        )

    # No signing secret is configured: never fall back to a hardcoded default,
    # because a predictable secret would let anyone mint a capability.
    if signer is None or not signer.secret or not signer.secret.strip():
        return _blocked(
            BootstrapReasonCode.SIGNING_SECRET_MISSING,
            "no bootstrap signing secret is configured; refusing to issue a manifest",
            "signer.secret",
        )

    if enrollment is None:
        return _blocked(
            BootstrapReasonCode.ENROLLMENT_NOT_ACTIVE,
            "an enrollment is required",
            "enrollment_id",
        )
    if enrollment.status != EnrollmentStatus.ACTIVE:
        return _blocked(
            BootstrapReasonCode.ENROLLMENT_NOT_ACTIVE,
            f"enrollment is {enrollment.status.value}",
            "status",
        )

    if revision is None or revision.revision_id != enrollment.study_revision_id:
        return _blocked(
            BootstrapReasonCode.REVISION_MISMATCH,
            "enrollment is not bound to the requested revision",
            "revision_id",
        )
    if revision.status != RevisionStatus.PUBLISHED:
        return _blocked(
            BootstrapReasonCode.REVISION_NOT_PUBLISHED,
            f"revision is {revision.status.value}",
            "status",
        )

    protocol = StudyProtocolV1.model_validate(revision.protocol_json)

    window_issue = _schedule_issue(protocol, enrollment, timestamp)
    if window_issue is not None:
        return BootstrapResult(
            outcome=BootstrapOutcome.BLOCKED,
            reason=window_issue.code,
            issue=window_issue,
        )

    if assignment is None or (
        assignment.enrollment_id != enrollment.enrollment_id
        or assignment.study_revision_id != revision.revision_id
    ):
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "assignment does not belong to this enrollment/revision",
            "assignment_id",
        )

    condition = next(
        (
            candidate
            for candidate in protocol.conditions
            if candidate.condition_id == assignment.condition_id
        ),
        None,
    )
    if condition is None:
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            f"assigned condition {assignment.condition_id!r} is not in the revision",
            "condition_id",
        )

    pin = condition.resolved_distribution
    if pin is None:
        return _blocked(
            BootstrapReasonCode.RELEASE_NOT_FOUND,
            "the assigned condition has no frozen distribution pin",
            "resolved_distribution",
        )

    from research.study.agents.enums import DistributionMode

    is_byoa = pin.distribution_mode == DistributionMode.BYOA_EXTERNAL.value

    if release is None:
        if not is_byoa:
            return _blocked(
                BootstrapReasonCode.RELEASE_NOT_FOUND,
                "the pinned agent release is not registered",
                "resolved_distribution",
            )
    else:
        if pin.agent_id and pin.agent_id != release.agent_id:
            return _blocked(
                BootstrapReasonCode.ARTIFACT_MISMATCH,
                "release agent does not match the assigned condition pin",
                "resolved_distribution.agent_id",
            )
        if pin.release_id and pin.release_id != release.release_id:
            return _blocked(
                BootstrapReasonCode.ARTIFACT_MISMATCH,
                "release id does not match the assigned condition pin",
                "resolved_distribution.release_id",
            )
        if pin.version and pin.version != release.version:
            return _blocked(
                BootstrapReasonCode.ARTIFACT_MISMATCH,
                "release version does not match the assigned condition pin",
                "resolved_distribution.version",
            )

    from research.study.agents.enums import QualificationStatus

    if not is_byoa:
        # A PACKAGED release must be derived-qualified; a BYOA distribution is
        # deliberately always unverified (it has no artifact to bind evidence to)
        # and is launched from its frozen command/package identity instead.
        if release is None or release.qualification_status != QualificationStatus.QUALIFIED:
            qualification = (
                release.qualification_status.value
                if release is not None
                else "unregistered"
            )
            return _blocked(
                BootstrapReasonCode.RELEASE_NOT_QUALIFIED,
                f"release is {qualification}",
                "resolved_distribution",
            )

    selected_digest: Optional[str] = None
    if is_byoa:
        # BYOA: the participant installs the agent; there is no artifact to select
        # or digest to pin. The manifest carries the command/package identity.
        if not (
            pin.agent_package
            or pin.agent_command
            or (release is not None and release.byoa_identity)
        ):
            return _blocked(
                BootstrapReasonCode.ARTIFACT_UNAVAILABLE,
                "the BYOA distribution declares no command or agent package",
                "resolved_distribution",
            )
    elif release_resolver is not None:
        resolution = release_resolver.resolve(
            release.agent_id, release_id=release.release_id
        )
        if resolution.status == ReleaseResolutionStatus.NOT_FOUND:
            return _blocked(
                BootstrapReasonCode.RELEASE_NOT_FOUND,
                "the pinned release could not be resolved",
                "resolved_distribution",
            )
        if resolution.status != ReleaseResolutionStatus.RESOLVED:
            return _blocked(
                BootstrapReasonCode.RELEASE_NOT_QUALIFIED,
                f"release resolution is {resolution.status.value}",
                "resolved_distribution",
            )
        selected_digest = resolution.artifact_digest
    else:
        if platform is not None:
            artifact = release.artifact_for(platform[0], platform[1])
        else:
            artifacts = sorted(release.artifacts, key=lambda item: (item.os, item.arch))
            artifact = artifacts[0] if len(artifacts) == 1 else None
        if artifact is None or not artifact.sha256:
            return _blocked(
                BootstrapReasonCode.ARTIFACT_UNAVAILABLE,
                "no artifact is available for this platform",
                "resolved_distribution",
            )
        selected_digest = artifact.sha256

    if not is_byoa and not selected_digest:
        return _blocked(
            BootstrapReasonCode.ARTIFACT_UNAVAILABLE,
            "no resolvable artifact digest for the pinned release",
            "resolved_distribution",
        )

    pinned_digest = pin.artifact_digest
    if (
        not is_byoa
        and pinned_digest
        and pinned_digest != selected_digest
    ):
        return _blocked(
            BootstrapReasonCode.ARTIFACT_MISMATCH,
            "selected artifact does not match the frozen distribution pin",
            "resolved_distribution.artifact_digest",
        )

    required_capabilities = protocol.environment_requirements.required_capabilities
    if compatibility_result is None and required_capabilities:
        return _blocked(
            BootstrapReasonCode.COMPATIBILITY_MISSING,
            "no compatibility result was supplied; failing closed",
            "compatibility",
        )
    if (
        compatibility_result is not None
        and compatibility_result.decision != CompatibilityDecision.COMPATIBLE
    ):
        return _blocked(
            BootstrapReasonCode.INCOMPATIBLE_ENVIRONMENT,
            f"compatibility decision is {compatibility_result.decision.value}",
            "compatibility",
        )

    telemetry = protocol.telemetry_policy
    policies = BootstrapPolicies(
        telemetry_policy=BootstrapTelemetryPolicy(
            allowed_field_classes=[
                field_class.value for field_class in telemetry.allowed_field_classes
            ],
            content_capture=(
                TelemetryFieldClass.CONTENT in telemetry.allowed_field_classes
            ),
        ),
        privacy_policy=BootstrapPrivacyPolicy(
            retention_action=protocol.privacy_policy.retention_action.value,
            retention_days=protocol.privacy_policy.retention_days,
        ),
        session_policy=BootstrapSessionPolicy(
            idle_timeout_seconds=protocol.session_policy.idle_timeout_seconds,
            resume_grace_seconds=protocol.session_policy.resume_grace_seconds,
            heartbeat_seconds=protocol.session_policy.heartbeat_seconds,
        ),
    )

    research_session = session_factory.create_for_enrollment(
        enrollment, revision, timestamp, context_id
    )
    capability = issue_capability(
        audience=signer.audience,
        scope=signer.scope,
        ttl_seconds=signer.capability_ttl_seconds,
        revocation_epoch=enrollment.revocation_epoch,
        secret=signer.secret,
        now=timestamp,
        enrollment_id=enrollment.enrollment_id,
        research_session_id=research_session.research_session_id,
        revision_id=revision.revision_id,
    )

    if release is not None and release.adapter is not None:
        adapter_version = release.adapter.version
    else:
        adapter_version = condition.adapter_version

    if release is not None:
        manifest_agent_id = release.agent_id
        manifest_release_id = release.release_id
        manifest_mode = release.distribution_mode.value
        manifest_command = release.agent_command
        manifest_command_args = list(release.agent_command_args)
        manifest_package = release.agent_package
    else:
        # BYOA distribution with no registered release: launch from the frozen pin.
        manifest_agent_id = (
            pin.agent_id or pin.agent_package or pin.agent_command or ""
        )
        manifest_release_id = pin.release_id or ""
        manifest_mode = pin.distribution_mode
        manifest_command = pin.agent_command
        manifest_command_args = list(pin.agent_command_args)
        manifest_package = pin.agent_package

    draft = BootstrapManifestV1(
        generated_at=timestamp,
        study_id=revision.study_id,
        revision_id=revision.revision_id,
        revision_digest=revision.protocol_digest,
        enrollment_id=enrollment.enrollment_id,
        research_session=research_session,
        assignment=BootstrapAssignment(
            assignment_id=assignment.assignment_id,
            condition_id=assignment.condition_id,
            strategy=assignment.strategy,
            randomization_epoch=assignment.randomization_epoch,
            protocol_digest=assignment.protocol_digest,
        ),
        agent_release=BootstrapAgentRelease(
            agent_id=manifest_agent_id,
            release_id=manifest_release_id,
            artifact_digest=selected_digest or "",
            adapter_version=adapter_version,
            distribution_mode=manifest_mode,
            agent_command=manifest_command,
            agent_command_args=manifest_command_args,
            agent_package=manifest_package,
        ),
        agent_profile=agent_profile,
        policies=policies,
        compatibility_receipt_ref=compatibility_ref,
        compatibility=BootstrapCompatibility(
            receipt_ref=compatibility_ref,
            state=(
                CoverageState.AVAILABLE
                if compatibility_result is not None
                else CoverageState.UNAVAILABLE
            ),
            reason=None if compatibility_result is not None else "NOT_REQUIRED",
        ),
        session_capability=capability,
    )

    signature = sign_manifest(draft, signer.secret)
    signed = draft.model_copy(
        update={
            "manifest_digest": signature.digest,
            "signature": signature.signature,
        }
    )
    return BootstrapResult(
        outcome=BootstrapOutcome.ISSUED,
        reason=BootstrapReasonCode.OK,
        manifest=signed,
    )
