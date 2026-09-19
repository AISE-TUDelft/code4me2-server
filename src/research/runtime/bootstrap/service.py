"""Bootstrap composition for an active, web-enrolled study participant."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from research.compatibility.enums import CompatibilityDecision
from research.participants.enums import EnrollmentStatus
from research.study.agents.registry import (
    artifact_qualified,
    byoa_identity_qualified,
)
from research.telemetry.enums import CoverageState
from research.canonical import canonical_hash

from .capability import issue_capability
from .models import (
    BootstrapAgentConfigBinding,
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

__all__ = [
    "BootstrapSigningContext",
    "EphemeralSessionFactory",
    "SessionFactory",
    "compose_bootstrap",
]

_BASE = ConfigDict(extra="forbid")


class SessionFactory(Protocol):
    """Creates (or reuses) a research session for an enrollment and study."""

    def create_for_enrollment(
        self,
        enrollment: Any,
        study: Any,
        now: datetime,
        context_id: str = "",
    ) -> ResearchSessionRef:  # pragma: no cover - structural protocol
        ...


class EphemeralSessionFactory:
    """Test-only factory that returns a fresh session reference."""

    def create_for_enrollment(
        self,
        enrollment: Any,
        study: Any,
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


def _study_is_open(study: Any, now: datetime) -> Optional[BootstrapIssue]:
    if study is None or not getattr(study, "is_research", False):
        return BootstrapIssue(
            code=BootstrapReasonCode.STUDY_NOT_OPEN,
            message="the study is not a research study",
            field="study",
        )
    if getattr(study, "research_status", None) == "STUDY_STOPPED":
        return BootstrapIssue(
            code=BootstrapReasonCode.STUDY_CLOSED,
            message="the study has been stopped",
            field="research_status",
        )
    if not getattr(study, "is_active", False):
        return BootstrapIssue(
            code=BootstrapReasonCode.STUDY_NOT_OPEN,
            message="the study is not active",
            field="is_active",
        )
    starts_at = getattr(study, "starts_at", None)
    ends_at = getattr(study, "ends_at", None)
    if starts_at is not None and now < starts_at:
        return BootstrapIssue(
            code=BootstrapReasonCode.STUDY_NOT_OPEN,
            message="the study has not opened yet",
            field="starts_at",
        )
    if ends_at is not None and now > ends_at:
        return BootstrapIssue(
            code=BootstrapReasonCode.STUDY_CLOSED,
            message="the study window has closed",
            field="ends_at",
        )
    return None


def _policies(study: Any) -> BootstrapPolicies:
    config = getattr(study, "research_config_json", None) or {}
    telemetry = config.get("telemetry_policy", {}) or {}
    privacy = config.get("privacy_policy", {}) or {}
    session = config.get("session_policy", {}) or {}
    allowed = telemetry.get("allowed_field_classes", [])
    return BootstrapPolicies(
        telemetry_policy=BootstrapTelemetryPolicy(
            allowed_field_classes=[str(value) for value in allowed],
            content_capture=bool(telemetry.get("content_capture", False)),
        ),
        privacy_policy=BootstrapPrivacyPolicy(
            retention_action=privacy.get("retention_action", "RETAIN_ANONYMIZED"),
            retention_days=privacy.get("retention_days"),
        ),
        session_policy=BootstrapSessionPolicy(
            idle_timeout_seconds=session.get("idle_timeout_seconds"),
            resume_grace_seconds=session.get("resume_grace_seconds"),
            heartbeat_seconds=session.get("heartbeat_seconds"),
        ),
    )


def _profile_projection(snapshot: dict[str, Any]) -> Optional[BootstrapAgentProfile]:
    profile_id = snapshot.get("profile_id")
    if not profile_id:
        return None
    return BootstrapAgentProfile(
        profile_id=uuid.UUID(str(profile_id)),
        name=str(snapshot.get("name", "assigned-profile")),
        framework_version=str(snapshot.get("framework_version", "code4me2-agent")),
        model=str(snapshot.get("model", "")),
        temperature=snapshot.get("temperature"),
        # ISSUE-03 Path A: the frozen executable fields a BYOA release may
        # translate into the external agent's configuration. Non-secret.
        tools_json=(
            snapshot.get("tools_json")
            if isinstance(snapshot.get("tools_json"), str)
            else json.dumps(snapshot.get("tools_json") or [], separators=(",", ":"))
        ),
        approval_policy=str(snapshot.get("approval_policy", "auto")),
        max_steps=int(snapshot.get("max_steps", 1) or 1),
    )


def compose_bootstrap(
    enrollment: Any,
    study: Any,
    assignment: Any,
    release: Any,
    compatibility_ref: Optional[str],
    session_factory: SessionFactory,
    signer: BootstrapSigningContext,
    now: Optional[datetime] = None,
    *,
    compatibility_result: Optional[Any] = None,
    platform: Optional[tuple[str, str]] = None,
    agent_profile: Optional[BootstrapAgentProfile] = None,
    kill_switch_check: Optional[Callable[[], bool]] = None,
    context_id: str = "",
    release_evidence_json: Optional[Mapping[str, Any]] = None,
) -> BootstrapResult:
    """Compose a signed, short-lived, secret-free bootstrap manifest.

    ``release_evidence_json`` is the release's stored evidence document
    (``agent_release.release_json``: artifacts/adapter/conformance). When
    supplied, the exact artifact selected for ``platform`` must itself be bound
    to a passing receipt — a release-level ``QUALIFIED`` that only covers a
    different platform never issues a manifest.
    """
    timestamp = _now(now)
    if kill_switch_check is not None and kill_switch_check():
        return _blocked(
            BootstrapReasonCode.KILL_SWITCH_ENGAGED,
            "an operator kill switch is engaged for this scope",
            "kill_switch",
        )
    if signer is None or not signer.secret or not signer.secret.strip():
        return _blocked(
            BootstrapReasonCode.SIGNING_SECRET_MISSING,
            "no bootstrap signing secret is configured",
            "signer.secret",
        )
    if enrollment is None or enrollment.status != EnrollmentStatus.ACTIVE:
        return _blocked(
            BootstrapReasonCode.ENROLLMENT_NOT_ACTIVE,
            "an active enrollment is required",
            "enrollment_id",
        )
    study_issue = _study_is_open(study, timestamp)
    if study_issue is not None:
        return BootstrapResult(
            outcome=BootstrapOutcome.BLOCKED,
            reason=study_issue.code,
            issue=study_issue,
        )
    if assignment is None or (
        assignment.enrollment_id != enrollment.enrollment_id
        or assignment.study_id != enrollment.study_id
    ):
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "assignment does not belong to this enrollment/study",
            "assignment_id",
        )

    snapshot = dict(assignment.profile_snapshot_json or {})
    if not assignment.profile_digest or canonical_hash(snapshot) != assignment.profile_digest:
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "assignment profile digest is missing",
            "profile_digest",
        )
    profile = agent_profile or _profile_projection(snapshot)
    if profile is None:
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "assignment has no profile snapshot",
            "profile_snapshot_json",
        )
    if str(snapshot.get("profile_id")) != str(assignment.agent_profile_id):
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "assignment profile id does not match its profile snapshot",
            "agent_profile_id",
        )
    if profile.profile_id != assignment.agent_profile_id:
        return _blocked(
            BootstrapReasonCode.ASSIGNMENT_MISMATCH,
            "bootstrap profile does not match the assigned profile",
            "agent_profile_id",
        )

    release_id = snapshot.get("release_id")
    if release is None:
        return _blocked(
            BootstrapReasonCode.RELEASE_NOT_FOUND,
            "the assigned profile release is not registered",
            "release_id",
        )
    if not release_id or str(release.release_id) != str(release_id):
        return _blocked(
            BootstrapReasonCode.ARTIFACT_MISMATCH,
            "release does not match the assigned profile snapshot",
            "release_id",
        )
    if compatibility_result is not None and compatibility_result.decision != CompatibilityDecision.COMPATIBLE:
        return _blocked(
            BootstrapReasonCode.INCOMPATIBLE_ENVIRONMENT,
            f"compatibility decision is {compatibility_result.decision.value}",
            "compatibility",
        )
    qualification = getattr(release, "qualification_status", None)
    if getattr(qualification, "value", qualification) != "QUALIFIED":
        return _blocked(
            BootstrapReasonCode.RELEASE_NOT_QUALIFIED,
            "the assigned release is not qualified",
            "release_id",
        )
    if not getattr(release, "is_byoa", False):
        if platform is not None:
            artifact = release.artifact_for(platform[0], platform[1])
        else:
            artifacts = sorted(release.artifacts, key=lambda item: (item.os, item.arch))
            artifact = artifacts[0] if len(artifacts) == 1 else None
        if artifact is None or not artifact.sha256:
            return _blocked(
                BootstrapReasonCode.ARTIFACT_UNAVAILABLE,
                "no artifact is available for this platform",
                "release.artifacts",
            )
        # A release-level QUALIFIED is not enough: the *selected* artifact must
        # itself be covered by a passing receipt bound to its digest, platform
        # and adapter (ISSUE-10).
        if release_evidence_json is not None and not artifact_qualified(
            release_evidence_json,
            os_name=artifact.os,
            arch=artifact.arch,
            digest=artifact.sha256,
            adapter_digest=getattr(getattr(release, "adapter", None), "digest", None),
        ):
            return _blocked(
                BootstrapReasonCode.ARTIFACT_NOT_QUALIFIED,
                "the selected artifact is not bound to passing conformance evidence",
                "release.artifacts",
            )
        artifact_digest = artifact.sha256
    else:
        artifact_digest = ""
        if release_evidence_json is not None and not byoa_identity_qualified(
            release_evidence_json
        ):
            return _blocked(
                BootstrapReasonCode.RELEASE_NOT_QUALIFIED,
                "the BYOA release identity is not bound to passing conformance evidence",
                "release_id",
            )

    research_session = session_factory.create_for_enrollment(
        enrollment, study, timestamp, context_id
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
        study_id=enrollment.study_id,
    )
    adapter = getattr(release, "adapter", None)
    adapter_id = getattr(adapter, "adapter_id", None)
    adapter_version = getattr(adapter, "version", None)
    draft = BootstrapManifestV1(
        generated_at=timestamp,
        study_id=enrollment.study_id,
        enrollment_id=enrollment.enrollment_id,
        research_config_digest=getattr(study, "research_config_digest", None),
        research_session=research_session,
        assignment=BootstrapAssignment(
            assignment_id=assignment.assignment_id,
            agent_profile_id=assignment.agent_profile_id,
            strategy=assignment.strategy,
            randomization_epoch=assignment.randomization_epoch,
            profile_digest=assignment.profile_digest,
        ),
        agent_release=BootstrapAgentRelease(
            agent_id=release.agent_id,
            release_id=release.release_id,
            artifact_digest=artifact_digest,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            distribution_mode=getattr(getattr(release, "distribution_mode", None), "value", "PACKAGED"),
            agent_command=release.agent_command,
            agent_command_args=list(release.agent_command_args),
            agent_package=release.agent_package,
            config_bindings=[
                BootstrapAgentConfigBinding(
                    field=binding.field,
                    transport=binding.transport,
                    key=binding.key,
                    format=binding.format,
                    value_map=dict(binding.value_map),
                )
                for binding in (getattr(release, "byoa_config", None) or [])
            ],
        ),
        agent_profile=profile,
        policies=_policies(study),
        compatibility_receipt_ref=compatibility_ref,
        compatibility=BootstrapCompatibility(
            receipt_ref=compatibility_ref,
            state=(CoverageState.AVAILABLE if compatibility_result is not None else CoverageState.UNAVAILABLE),
            reason=None if compatibility_result is not None else "NOT_REQUIRED",
        ),
        session_capability=capability,
    )
    signature = sign_manifest(draft, signer.secret)
    return BootstrapResult(
        outcome=BootstrapOutcome.ISSUED,
        reason=BootstrapReasonCode.OK,
        manifest=draft.model_copy(
            update={"manifest_digest": signature.digest, "signature": signature.signature}
        ),
    )
