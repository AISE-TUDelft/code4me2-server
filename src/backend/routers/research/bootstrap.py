"""Participant bootstrap API: manifest and exposure receipts.

Mounted under ``/api/research/bootstrap``. Handlers are intentionally thin: the
assignment, exposure, and bootstrap composition logic lives in
:mod:`research.runtime.assignment` and :mod:`research.runtime.bootstrap`. Participants
authenticate with the normal ``get_current_user`` flow and only ever obtain a
signed, secret-free manifest for their own active enrollment.
"""

from __future__ import annotations

import os
import random
import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import (
    AuthenticatedUser,
    get_current_user,
)
from database import crud
from research.analysis.operations import store as operations_store
from research.compatibility.enums import CapabilityId, CapabilityState
from research.compatibility.evaluate import evaluate_compatibility
from research.compatibility.models import (
    AcpCapabilityReceiptV1,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
    AgentIdentity,
    CompatibilityRequest,
    EnvironmentTuple,
    RequiredCapability,
)
from research.participants import identity as identity_store
from research.runtime.assignment import store as assignment_store
from research.runtime.assignment.enums import AllocationOutcome, ExposureOutcome
from research.runtime.assignment.exposure import record_exposure
from research.runtime.assignment.models import (
    ExposureEnvironment,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.runtime.assignment.service import allocate
from research.runtime.bootstrap.capability import verify_capability
from research.runtime.bootstrap.models import (
    BootstrapAgentProfile,
    ResearchSessionRef,
    SessionCapability,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.runtime.bootstrap.service import (
    BootstrapSigningContext,
    compose_bootstrap,
)
from research.runtime.sessions import store as session_store
from research.runtime.sessions.enums import SessionState
from research.runtime.sessions.models import ResearchSessionV1
from research.study.agents import store as registry_store
from research.study.protocol import store as protocol_store
from research.study.protocol.models import StudyProtocolV1

router = APIRouter()

# The signing secret is mandatory: there is no development fallback. If it is
# unset the router refuses to issue or verify any capability/manifest rather
# than signing with a predictable, hardcoded secret.
BOOTSTRAP_SIGNING_SECRET: Optional[str] = os.environ.get("BOOTSTRAP_SIGNING_SECRET")
_SIGNER: Optional[BootstrapSigningContext] = (
    BootstrapSigningContext(secret=BOOTSTRAP_SIGNING_SECRET)
    if BOOTSTRAP_SIGNING_SECRET
    else None
)

AUDIENCE = "research-runtime"


def _require_signer() -> BootstrapSigningContext:
    """Return the configured signer or raise a typed refusal."""
    if _SIGNER is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SIGNING_SECRET_MISSING",
                "message": (
                    "BOOTSTRAP_SIGNING_SECRET is not configured; refusing to "
                    "issue a bootstrap manifest"
                ),
            },
        )
    return _SIGNER


def _kill_switch_for_scope(
    db: Any,
    *,
    study_id: Optional[uuid.UUID],
    revision_id: Optional[uuid.UUID],
    enrollment_id: Optional[uuid.UUID],
) -> Callable[[], bool]:
    """DB-backed kill-switch predicate for one bootstrap/exposure scope."""
    return operations_store.db_kill_switch_check(
        db,
        study_id=study_id,
        revision_id=revision_id,
        enrollment_id=enrollment_id,
    )


def _verify_exposure_capability(
    capability: SessionCapability,
    *,
    enrollment: Any,
    assignment: Any,
    now: datetime,
) -> None:
    """Verify the exposure capability is bound to this enrollment/assignment."""
    if not BOOTSTRAP_SIGNING_SECRET:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SIGNING_SECRET_MISSING",
                "message": (
                    "BOOTSTRAP_SIGNING_SECRET is not configured; refusing to "
                    "verify a session capability"
                ),
            },
        )
    verification = verify_capability(
        capability,
        BOOTSTRAP_SIGNING_SECRET,
        expected_audience=AUDIENCE,
        expected_scope=[],
        now=now,
        current_revocation_epoch=enrollment.revocation_epoch,
        expected_enrollment_id=enrollment.enrollment_id,
        expected_revision_id=assignment.study_revision_id,
    )
    if not verification.ok:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CAPABILITY_INVALID",
                "message": verification.message or "session capability is invalid",
                "capability_reason": verification.reason.value,
            },
        )


class EnvironmentReport(BaseModel):
    """The participant's installed host tuple."""

    os: str
    arch: str
    ide_build: Optional[str] = None
    plugin_version: Optional[str] = None
    host_kind: Optional[str] = None


class ResearchSessionRequest(BaseModel):
    """Request a bootstrap manifest for an active enrollment."""

    enrollment_id: uuid.UUID
    # Opaque execution-context id for this project/window. Required so two
    # simultaneous windows get distinct sessions; identical values are
    # idempotent. Never a filesystem path.
    context_id: str = Field(min_length=1, max_length=200)
    environment: EnvironmentReport
    capability_receipt: Optional[AcpCapabilityReceiptV1] = None
    compatibility_receipt_id: Optional[uuid.UUID] = None


class ExposureRequest(BaseModel):
    """Report an exposure (or a launch failure) for an assignment."""

    # The exposure is authorized by the same session capability issued at
    # bootstrap; the capability's enrollment must match the assignment's.
    capability: SessionCapability
    enrollment_id: uuid.UUID
    assignment_id: uuid.UUID
    environment: ExposureEnvironment
    outcome: ExposureOutcome
    idempotency_key: str = Field(min_length=1)
    agent_release_id: Optional[str] = None
    artifact_digest: Optional[str] = None
    adapter_version: Optional[str] = None
    observed_configuration: dict[str, Any] = Field(default_factory=dict)
    evidence_digest: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _issue_payload(issue: Any) -> dict[str, Any]:
    return issue.model_dump(mode="json") if issue is not None else {}


def _owned_enrollment(db: Any, current_user: AuthenticatedUser, enrollment_id: uuid.UUID):
    participant_row = identity_store.get_participant_by_account(db, current_user.user_id)
    if participant_row is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    row = identity_store.get_enrollment(db, enrollment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    enrollment = identity_store.row_to_enrollment(row)
    if enrollment.participant_id != participant_row.participant_id:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return enrollment


def _evaluate_compatibility(
    payload: ResearchSessionRequest,
    protocol: StudyProtocolV1,
    release: Any,
    db: Any,
) -> tuple[Any, Optional[str]]:
    """Evaluate compatibility from an inline receipt or a stored receipt id."""
    receipt = payload.capability_receipt
    receipt_ref: Optional[str] = None
    if receipt is not None:
        receipt_ref = str(receipt.receipt_id)
    elif payload.compatibility_receipt_id is not None:
        row = _compatibility_store().get_receipt(db, payload.compatibility_receipt_id)
        if row is not None:
            receipt = _compatibility_store().row_to_receipt(row)
            receipt_ref = str(payload.compatibility_receipt_id)

    if receipt is None:
        return None, receipt_ref

    requirements: list[RequiredCapability] = []
    for expectation in protocol.environment_requirements.required_capabilities:
        try:
            capability = CapabilityId(str(expectation.capability).strip().upper())
            require_state = CapabilityState(
                str(expectation.require_state).strip().upper()
            )
        except ValueError:
            continue
        requirements.append(
            RequiredCapability(capability=capability, require_state=require_state)
        )

    expected_agent = None
    if release is not None:
        expected_agent = AgentIdentity(
            agent_id=release.agent_id,
            agent_version=release.version,
            adapter_version=release.adapter.version if release.adapter else None,
        )

    request = CompatibilityRequest(
        receipt=receipt,
        requirements=requirements,
        expected_environment=EnvironmentTuple(
            os=payload.environment.os,
            arch=payload.environment.arch,
            ide_build=payload.environment.ide_build,
            plugin_version=payload.environment.plugin_version,
            host_kind=payload.environment.host_kind,
        ),
        expected_agent=expected_agent,
        expected_protocol_version=(
            protocol.environment_requirements.expected_protocol_version
        ),
    )
    return evaluate_compatibility(request), receipt_ref


def _compatibility_store():
    """Late import so the core research packages stay backend-agnostic."""
    from research.compatibility import store as compatibility_store

    return compatibility_store


def _agent_profile_projection(
    db: Any, condition: Any
) -> Optional[BootstrapAgentProfile]:
    """Project a condition's frozen config into the secret-free manifest.

    Only non-secret provider/model identity is read, and only from the config
    frozen into the published revision: a later edit to the profile template must
    not change a published study's manifest. The connection endpoint and secret
    reference are deliberately absent — inference is relayed through the backend,
    which resolves the secret at request time.
    """
    resolved = getattr(condition, "resolved_distribution", None)
    config = getattr(resolved, "agent_config", None) if resolved is not None else None
    if config is None:
        return None
    return BootstrapAgentProfile(
        profile_id=config.profile_id,
        name=config.name,
        framework_version=config.framework_version or "",
        model=config.model,
        # Provider endpoint stays server-side; the runtime never needs it.
        base_url=None,
        temperature=config.temperature,
    )


class _PersistentSessionFactory:
    """Bootstrap session factory that persists the authoritative session row.

    Bootstrap is the first server-side component to observe an eligible
    enrollment, so it is where the durable research session is created. The
    ``research_session_id`` embedded in the manifest must resolve through
    :func:`research.runtime.sessions.store.get_session`; otherwise ingestion rejects
    every uploaded event as ``SESSION_OUT_OF_SCOPE``. Sessions are scoped to an
    opaque execution context: repeated bootstrap for the same ``(enrollment,
    context_id)`` reuses that context's non-terminal session, while different
    windows/projects get distinct sessions sharing the enrollment/assignment.
    """

    def __init__(
        self,
        db: Any,
        manifest_digest_provider: Optional[Callable[[], str]] = None,
        *,
        commit: bool = True,
    ) -> None:
        self._db = db
        self._manifest_digest_provider = manifest_digest_provider
        # Bootstrap passes commit=False so the assignment and session commit as
        # one unit of work.
        self._commit = commit

    def _manifest_digest(self) -> str:
        if self._manifest_digest_provider is None:
            return ""
        return self._manifest_digest_provider()

    def create_for_enrollment(
        self,
        enrollment: Any,
        revision: Any,
        now: datetime,
        context_id: str = "",
    ) -> ResearchSessionRef:
        existing = session_store.get_active_session_for_context(
            self._db, enrollment.enrollment_id, context_id
        )
        if existing is not None:
            return ResearchSessionRef(
                research_session_id=existing.session_id,
                opened_at=existing.opened_at or now,
            )

        session = ResearchSessionV1(
            research_session_id=uuid.uuid4(),
            enrollment_id=enrollment.enrollment_id,
            study_revision_id=revision.revision_id,
            context_id=context_id,
            state=SessionState.NOT_STARTED,
            opened_at=now,
            manifest_digest=self._manifest_digest(),
        )
        row = session_store.create_session(self._db, session, commit=self._commit)
        return ResearchSessionRef(
            research_session_id=row.session_id,
            opened_at=row.opened_at or now,
        )


@router.post(
    "/research-sessions",
    summary="Issue a signed bootstrap manifest for an active enrollment",
)
def create_research_session(
    payload: ResearchSessionRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Validate eligibility/compatibility, then sign a secret-free manifest."""
    signer = _require_signer()
    db = app.get_db_session()
    try:
        enrollment = _owned_enrollment(db, current_user, payload.enrollment_id)

        revision_row = protocol_store.get_revision(db, enrollment.study_revision_id)
        if revision_row is None:
            raise HTTPException(status_code=404, detail="Study revision not found")
        revision = protocol_store.row_to_revision(revision_row)
        protocol = StudyProtocolV1.model_validate(revision.protocol_json)

        assignment_row = assignment_store.get_assignment_for_enrollment_revision(
            db, enrollment.enrollment_id, revision.revision_id
        )
        existing = (
            assignment_store.row_to_assignment(assignment_row)
            if assignment_row is not None
            else None
        )
        allocation = allocate(
            enrollment,
            revision,
            existing=existing,
            rng=random.Random(),
            now=_now(),
        )
        if allocation.outcome not in (
            AllocationOutcome.CREATED,
            AllocationOutcome.EXISTING,
        ):
            raise HTTPException(
                status_code=409, detail=_issue_payload(allocation.issue)
            )
        assert allocation.assignment is not None

        condition = next(
            (
                candidate
                for candidate in protocol.conditions
                if candidate.condition_id == allocation.assignment.condition_id
            ),
            None,
        )
        release = None
        if condition is not None:
            # Read the release pin from the revision's frozen distribution, never
            # from the mutable profile row (a later profile edit must not change
            # a published study's manifest).
            resolved_pin = getattr(condition, "resolved_distribution", None)
            release_id = getattr(resolved_pin, "release_id", None)
            if release_id:
                release_row = registry_store.get_release(db, release_id)
                if release_row is not None:
                    release = registry_store.row_to_release(release_row)

        compatibility_result, receipt_ref = _evaluate_compatibility(
            payload, protocol, release, db
        )

        # Single unit of work (lock order: participant/enrollment already
        # resolved by the caller → sticky assignment → this context's execution
        # session). The session factory is commit=False, so every row created
        # here lands only in the one commit below; any failure rolls back.
        result = compose_bootstrap(
            enrollment,
            revision,
            allocation.assignment,
            release,
            receipt_ref,
            _PersistentSessionFactory(db, commit=False),
            signer,
            now=_now(),
            compatibility_result=compatibility_result,
            platform=(payload.environment.os, payload.environment.arch),
            agent_profile=_agent_profile_projection(db, condition),
            kill_switch_check=_kill_switch_for_scope(
                db,
                study_id=revision.study_id,
                revision_id=revision.revision_id,
                enrollment_id=enrollment.enrollment_id,
            ),
            context_id=payload.context_id,
        )
        if result.manifest is None:
            # Nothing is committed yet: a failure here (kill switch, incompatible
            # environment, unresolved release) leaves no orphan session or
            # half-assignment.
            db.rollback()
            raise HTTPException(
                status_code=409, detail=_issue_payload(result.issue)
            )

        manifest = result.manifest
        # Persist the sticky assignment only after the manifest is actually
        # issued, still inside the same transaction. A concurrent first use that
        # won the unique constraint is a retryable conflict (nothing committed).
        if allocation.created:
            winner_row = assignment_store.create_assignment(
                db, allocation.assignment, commit=False
            )
            if (
                winner_row is not None
                and winner_row.assignment_id != allocation.assignment.assignment_id
            ):
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ASSIGNMENT_RACE",
                        "message": (
                            "the sticky assignment was allocated concurrently; "
                            "retry to adopt the existing condition"
                        ),
                    },
                )
        # Single commit for assignment + session.
        db.commit()

        # The session capability is verified statelessly from its HMAC signature
        # and the enrollment's revocation epoch, so it is never persisted.
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "manifest": manifest.model_dump(mode="json"),
                "manifest_digest": manifest.manifest_digest,
                "signature": manifest.signature,
            },
        )
    finally:
        db.close()


@router.post("/exposures", summary="Record an idempotent exposure receipt")
def create_exposure(
    payload: ExposureRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Record (or idempotently replay) an exposure or launch-failure receipt."""
    now = _now()
    db = app.get_db_session()
    try:
        enrollment = _owned_enrollment(db, current_user, payload.enrollment_id)

        assignment_row = assignment_store.get_assignment(db, payload.assignment_id)
        if assignment_row is None:
            raise HTTPException(status_code=404, detail="Assignment not found")
        assignment = assignment_store.row_to_assignment(assignment_row)
        if assignment.enrollment_id != enrollment.enrollment_id:
            raise HTTPException(status_code=404, detail="Assignment not found")

        # The exposure must be authorized by a capability bound to this exact
        # enrollment (and revision): a capability for another participant can
        # never be replayed to record an exposure here.
        _verify_exposure_capability(
            payload.capability, enrollment=enrollment, assignment=assignment, now=now
        )

        existing_row = assignment_store.get_exposure_by_idempotency_key(
            db, payload.idempotency_key
        )
        existing = (
            assignment_store.row_to_exposure(existing_row)
            if existing_row is not None
            else None
        )

        result = record_exposure(
            assignment,
            payload.environment,
            payload.outcome,
            payload.idempotency_key,
            existing=existing,
            agent_release_id=payload.agent_release_id,
            artifact_digest=payload.artifact_digest,
            adapter_version=payload.adapter_version,
            observed_configuration=payload.observed_configuration,
            evidence_digest=payload.evidence_digest,
            now=now,
            kill_switch_check=_kill_switch_for_scope(
                db,
                study_id=enrollment.study_id,
                revision_id=assignment.study_revision_id,
                enrollment_id=enrollment.enrollment_id,
            ),
        )
        if not result.accepted:
            raise HTTPException(status_code=409, detail=_issue_payload(result.issue))

        receipt = None
        if result.exposure is not None and not result.reused:
            row = assignment_store.insert_exposure(db, result.exposure)
            receipt = assignment_store.exposure_summary(row)

        return JsonResponseWithStatus(
            status_code=200 if result.reused else 201,
            content={
                "accepted": True,
                "is_exposure": result.is_exposure,
                "reused": result.reused,
                "audit": result.audit,
                "exposure": receipt,
            },
        )
    finally:
        db.close()
