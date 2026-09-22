"""Participant bootstrap API: signed manifest.

Mounted under ``/api/research/bootstrap``. Handlers are intentionally thin: the
assignment and bootstrap composition logic lives in
:mod:`research.runtime.assignment` and :mod:`research.runtime.bootstrap`. Participants
authenticate with the normal ``get_current_user`` flow and only ever obtain a
signed, secret-free manifest for their own active enrollment.
"""

from __future__ import annotations

import os
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
from database.db_schemas import ResearchStudyStatus, Study as StudyRow
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
from research.runtime.assignment.enums import AllocationOutcome
from research.runtime.assignment.service import allocate
from research.runtime.bootstrap.models import (
    BootstrapAgentProfile,
    BootstrapManifestV1,
    ResearchSessionRef,
)
from research.runtime.bootstrap.capability import verify_capability
from research.runtime.bootstrap.signer import verify_manifest
from research.runtime.bootstrap.service import (
    BootstrapSigningContext,
    compose_bootstrap,
)
from research.runtime.sessions import store as session_store
from research.runtime.sessions.enums import SessionState
from research.runtime.sessions.models import ResearchSessionV1
from research.study.agents import store as registry_store
from research.study.protocol import store as protocol_store
from research.runtime.assignment.models import StudyProfileSelection

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


class ManifestVerificationRequest(BaseModel):
    manifest: BootstrapManifestV1
    enrollment_id: uuid.UUID
    context_id: str = Field(min_length=1, max_length=200)


@router.post("/verify", summary="Authenticate a manifest for its owner and execution context")
def verify_bootstrap_manifest(
    payload: ManifestVerificationRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    """Online HMAC verification; the signing secret never leaves the server.

    Consumers trust this authenticated HTTPS origin and must bind its response
    to the exact manifest digest, enrollment and context they requested.
    """
    signer = _require_signer()
    manifest = payload.manifest
    verification = verify_manifest(manifest, signer.secret)
    if not verification.ok:
        raise HTTPException(status_code=409, detail={"code": verification.reason.value})
    if manifest.enrollment_id != payload.enrollment_id:
        raise HTTPException(status_code=403, detail="Manifest enrollment mismatch")
    db = app.get_db_session()
    try:
        enrollment = _owned_enrollment(db, current_user, payload.enrollment_id)
        session = session_store.get_session(db, manifest.research_session.research_session_id)
        if (
            session is None
            or session.enrollment_id != enrollment.enrollment_id
            or session.study_id != enrollment.study_id
            or manifest.study_id != enrollment.study_id
            or session.context_id != payload.context_id
            or SessionState(session.state).is_terminal
        ):
            raise HTTPException(status_code=409, detail="Manifest session is not active in this context")
        capability = verify_capability(
            manifest.session_capability,
            signer.secret,
            expected_audience="research-runtime",
            expected_scope=["telemetry:write", "session:heartbeat", "session:close"],
            now=_now(),
            current_revocation_epoch=enrollment.revocation_epoch,
            expected_enrollment_id=enrollment.enrollment_id,
            expected_research_session_id=session.session_id,
            expected_study_id=enrollment.study_id,
        )
        if not capability.ok:
            raise HTTPException(status_code=409, detail={"code": capability.reason.value})
        if _kill_switch_for_scope(db, study_id=enrollment.study_id, enrollment_id=enrollment.enrollment_id)():
            raise HTTPException(status_code=409, detail={"code": "KILL_SWITCH_ENGAGED"})
        return {
            "verified": True,
            "manifest_digest": manifest.manifest_digest,
            "enrollment_id": str(enrollment.enrollment_id),
            "context_id": payload.context_id,
        }
    finally:
        db.close()


def _kill_switch_for_scope(
    db: Any,
    *,
    study_id: Optional[uuid.UUID],
    enrollment_id: Optional[uuid.UUID],
) -> Callable[[], bool]:
    """DB-backed kill-switch predicate for one bootstrap scope."""
    return operations_store.db_kill_switch_check(
        db,
        study_id=study_id,
        enrollment_id=enrollment_id,
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
    study = db.get(StudyRow, enrollment.study_id)
    if getattr(study, "research_status", None) == ResearchStudyStatus.STUDY_STOPPED.value:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "STUDY_STOPPED",
                "message": "the study has been stopped and cannot be bootstrapped",
            },
        )
    if getattr(enrollment.status, "value", enrollment.status) != "ACTIVE":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ENROLLMENT_NOT_ACTIVE",
                "message": "the enrollment is not active",
            },
        )
    return enrollment


def _evaluate_compatibility(
    payload: ResearchSessionRequest, release: Any, db: Any
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
        expected_protocol_version=None,
    )
    return evaluate_compatibility(request), receipt_ref


def _compatibility_store():
    """Late import so the core research packages stay backend-agnostic."""
    from research.compatibility import store as compatibility_store

    return compatibility_store


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
        study: Any,
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
            study_id=study.study_id,
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

        study = db.get(StudyRow, enrollment.study_id)
        if study is None:
            raise HTTPException(status_code=404, detail="Study not found")
        assignment_row = assignment_store.get_assignment_for_enrollment(
            db, enrollment.enrollment_id
        )
        existing = (
            assignment_store.row_to_assignment(assignment_row)
            if assignment_row is not None
            else None
        )
        if existing is not None:
            allocation = allocate(enrollment, [], existing=existing, now=_now())
        else:
            from sqlalchemy import select
            from database.research_schemas import StudyAgentProfile

            profile_rows = db.execute(
                select(StudyAgentProfile)
                .where(StudyAgentProfile.study_id == enrollment.study_id)
                .order_by(StudyAgentProfile.selection_order.asc())
            ).scalars().all()
            profiles = [
                StudyProfileSelection(
                    study_id=row.study_id,
                    agent_profile_id=row.profile_id,
                    profile_digest=row.profile_digest,
                    profile_snapshot_json=row.profile_snapshot_json,
                    selection_order=row.selection_order,
                )
                for row in profile_rows
            ]
            allocation = allocate(enrollment, profiles, now=_now())
        if allocation.outcome not in (
            AllocationOutcome.CREATED,
            AllocationOutcome.EXISTING,
        ):
            raise HTTPException(
                status_code=409, detail=_issue_payload(allocation.issue)
            )
        assert allocation.assignment is not None

        release_id = allocation.assignment.profile_snapshot_json.get("release_id")
        release = None
        if release_id:
            release_row = registry_store.get_release(db, release_id)
            if release_row is not None:
                release = registry_store.row_to_release(release_row)

        compatibility_result, receipt_ref = _evaluate_compatibility(payload, release, db)

        # Single unit of work (lock order: participant/enrollment already
        # resolved by the caller → sticky assignment → this context's execution
        # session). The session factory is commit=False, so every row created
        # here lands only in the one commit below; any failure rolls back.
        result = compose_bootstrap(
            enrollment,
            study,
            allocation.assignment,
            release,
            receipt_ref,
            _PersistentSessionFactory(db, commit=False),
            signer,
            now=_now(),
            compatibility_result=compatibility_result,
            platform=(payload.environment.os, payload.environment.arch),
            kill_switch_check=_kill_switch_for_scope(
                db,
                study_id=study.study_id,
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
                                    "retry to adopt the existing profile"
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
