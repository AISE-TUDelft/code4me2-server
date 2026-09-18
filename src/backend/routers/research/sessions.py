"""Research session lifecycle API (Issue 07).

Mounted under ``/api/research/sessions``. Endpoints are authenticated by the
short-lived, scoped session capability issued at bootstrap
(:func:`research.runtime.bootstrap.capability.verify_capability`); the enrollment's
current revocation epoch is rechecked on every call. Handlers are thin: the
state machine lives in :mod:`research.runtime.sessions.service` and persistence in
:mod:`research.runtime.sessions.store`.

Server time is authoritative for idle/resume/expiry decisions; a client clock is
never trusted.
"""

from __future__ import annotations

import uuid  # noqa: TC003 - FastAPI evaluates route annotations at runtime
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.research.bootstrap import BOOTSTRAP_SIGNING_SECRET
from database.db_schemas import ResearchStudyStatus, Study as StudyRow
from research.analysis.operations import store as operations_store
from research.participants import identity as identity_store
from research.participants.enums import EnrollmentStatus
from research.runtime.bootstrap.capability import verify_capability
from research.runtime.bootstrap.models import (
    SessionCapability,  # noqa: TC001 - FastAPI evaluates route annotations at runtime
)
from research.runtime.sessions import store as session_store
from research.runtime.sessions.enums import CloseReason, SessionReasonCode, SessionState
from research.runtime.sessions.service import (
    close,
    expire_if_idle,
    on_qualifying_activity,
    open_session,
    session_policy_from_study,
)

if TYPE_CHECKING:
    from research.runtime.sessions.models import ResearchSessionV1, SessionPolicyV1

router = APIRouter()

AUDIENCE = "research-runtime"
_SCOPE_WRITE = "telemetry:write"
_SCOPE_HEARTBEAT = "session:heartbeat"
_SCOPE_CLOSE = "session:close"


class CreateSessionRequest(BaseModel):
    """Create (or reuse) a research session for an active enrollment."""

    capability: SessionCapability
    enrollment_id: uuid.UUID
    study_id: uuid.UUID
    manifest_digest: str
    # Opaque execution-context id for this project/window (never a path).
    # Required: one context maps to one live session; different contexts differ.
    context_id: str
    environment_ref: Optional[str] = None


class HeartbeatRequest(BaseModel):
    """Heartbeat/report-activity for an existing session."""

    capability: SessionCapability
    research_session_id: uuid.UUID


class CloseSessionRequest(BaseModel):
    """Close a session with a typed reason."""

    capability: SessionCapability
    research_session_id: uuid.UUID
    reason: CloseReason


class SessionSummaryRequest(BaseModel):
    """Fetch the summary of an existing session."""

    capability: SessionCapability
    research_session_id: uuid.UUID


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _detail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return detail


def _kill_switch_for_session(db: Any, session: ResearchSessionV1) -> Any:
    """DB-backed kill-switch predicate scoped to this session's subject."""
    return operations_store.db_kill_switch_check(
        db,
        study_id=session.study_id,
        enrollment_id=session.enrollment_id,
    )


def _authorize(
    capability: SessionCapability,
    enrollment: Any,
    *,
    scope: str,
    now: datetime,
    research_session_id: Optional[uuid.UUID] = None,
    study_id: Optional[uuid.UUID] = None,
) -> None:
    """Verify the capability and recheck the enrollment's revocation epoch.

    ``research_session_id`` and ``study_id`` bind the capability to the exact
    session/revision being acted on, so a valid capability for another session
    is rejected with a typed reason.
    """
    verification = verify_capability(
        capability,
        BOOTSTRAP_SIGNING_SECRET or "",
        expected_audience=AUDIENCE,
        expected_scope=[scope],
        now=now,
        current_revocation_epoch=enrollment.revocation_epoch,
        expected_enrollment_id=enrollment.enrollment_id,
        expected_research_session_id=research_session_id,
        expected_study_id=study_id,
    )
    if not verification.ok:
        raise HTTPException(
            status_code=403,
            detail=_detail(
                SessionReasonCode.CAPABILITY_INVALID.value,
                verification.message or "session capability is invalid",
                capability_reason=verification.reason.value,
            ),
        )
    if enrollment.status != EnrollmentStatus.ACTIVE:
        raise HTTPException(
            status_code=403,
            detail=_detail(
                SessionReasonCode.ENROLLMENT_NOT_ACTIVE.value,
                f"enrollment is {enrollment.status.value}; bootstrap is blocked",
            ),
        )


def _load_enrollment(db: Any, enrollment_id: uuid.UUID):
    row = identity_store.get_enrollment(db, enrollment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    study = db.get(StudyRow, row.study_id)
    if getattr(study, "research_status", None) == ResearchStudyStatus.STUDY_STOPPED.value:
        raise HTTPException(
            status_code=403,
            detail=_detail(
                "STUDY_STOPPED",
                "the study has been stopped and cannot accept session activity",
            ),
        )
    return identity_store.row_to_enrollment(row)


def _load_session(db: Any, research_session_id: uuid.UUID) -> ResearchSessionV1:
    row = session_store.get_session(db, research_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Research session not found")
    return session_store.row_to_session(row)


def _load_policy(db: Any, session: ResearchSessionV1) -> Optional[SessionPolicyV1]:
    study = db.get(StudyRow, session.study_id)
    return session_policy_from_study(study)


def _persist_result(db: Any, result: Any) -> None:
    """Persist a session result: append the transition (if any) and always
    update the session row.

    Updating unconditionally is what makes an *ordinary* heartbeat durable: a
    heartbeat that only refreshes ``last_activity_at`` has no transition, so
    gating the row update on ``transition is not None`` would silently drop it.
    """
    if result.transition is not None:
        session_store.insert_transition(db, result.transition)
    session_store.update_session(db, result.session)


def _next_actions(
    session: ResearchSessionV1, policy: SessionPolicyV1, now: datetime
) -> list[str]:
    if session.state == SessionState.NOT_STARTED:
        return ["report_activity"]
    if session.state == SessionState.RUNNING:
        return ["heartbeat"]
    if session.state == SessionState.OFFLINE:
        return ["recover"]
    if session.state == SessionState.SUSPENDED:
        last = session.last_activity_at
        within = (
            last is not None
            and (now - last).total_seconds() <= policy.resume_grace_seconds
        )
        return ["resume_within_grace"] if within else ["start_new_session"]
    return []


@router.post("/", summary="Create (or reuse) a research session")
def create_research_session(
    payload: CreateSessionRequest,
    app: App = Depends(App.get_instance),
):
    """Open a session for an active enrollment, or return the active one."""
    now = _now()
    db = app.get_db_session()
    try:
        enrollment = _load_enrollment(db, payload.enrollment_id)
        _authorize(
            payload.capability,
            enrollment,
            scope=_SCOPE_WRITE,
            now=now,
            study_id=payload.study_id,
        )
        if payload.study_id != enrollment.study_id:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    "STUDY_MISMATCH",
                    "enrollment is not bound to the requested study",
                ),
            )
        study = db.get(StudyRow, enrollment.study_id)
        policy = session_policy_from_study(study)
        if policy is None:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    SessionReasonCode.POLICY_MISSING.value,
                    "study does not declare idle/resume session policy",
                ),
            )

        existing_row = session_store.get_active_session_for_context(
            db, enrollment.enrollment_id, payload.context_id
        )
        if existing_row is not None:
            existing = session_store.row_to_session(existing_row)
            return JsonResponseWithStatus(
                status_code=200,
                content={
                    "created": False,
                    "session": session_store.session_summary(existing_row),
                    "next_actions": _next_actions(existing, policy, now),
                    "heartbeat_seconds": policy.heartbeat_seconds,
                },
            )

        session = open_session(
            enrollment,
            study,
            manifest_digest=payload.manifest_digest,
            environment_ref=payload.environment_ref,
            context_id=payload.context_id,
            now=now,
        )
        # An engaged kill switch blocks new funded session creation.
        kill_switch_check = operations_store.db_kill_switch_check(
            db,
            study_id=study.study_id,
            enrollment_id=enrollment.enrollment_id,
        )
        if kill_switch_check():
            raise HTTPException(
                status_code=403,
                detail=_detail(
                    "KILL_SWITCH_ENGAGED",
                    "an operator kill switch is engaged for this study",
                ),
            )
        row = session_store.create_session(db, session)
        return JsonResponseWithStatus(
            status_code=201,
            content={
                "created": True,
                "session": session_store.session_summary(row),
                "next_actions": _next_actions(session, policy, now),
                "heartbeat_seconds": policy.heartbeat_seconds,
            },
        )
    finally:
        db.close()


@router.post("/heartbeat", summary="Heartbeat / report session activity")
def heartbeat(
    payload: HeartbeatRequest,
    app: App = Depends(App.get_instance),
):
    """Update last-activity (or start the session) and return next actions."""
    now = _now()
    db = app.get_db_session()
    try:
        session = _load_session(db, payload.research_session_id)
        enrollment = _load_enrollment(db, session.enrollment_id)
        _authorize(
            payload.capability,
            enrollment,
            scope=_SCOPE_HEARTBEAT,
            now=now,
            research_session_id=session.research_session_id,
            study_id=session.study_id,
        )

        if session.state.is_terminal:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    SessionReasonCode.SESSION_TERMINAL.value,
                    f"session is {session.state.value}",
                ),
            )

        policy = _load_policy(db, session)
        if policy is None:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    SessionReasonCode.POLICY_MISSING.value,
                    "study does not declare idle/resume session policy",
                ),
            )

        kill_switch_check = _kill_switch_for_session(db, session)
        idle_result = expire_if_idle(
            session, now, policy=policy, kill_switch_check=kill_switch_check
        )
        if not idle_result.accepted:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    idle_result.reason.value,
                    (
                        idle_result.issue.message
                        if idle_result.issue is not None
                        else "heartbeat rejected"
                    ),
                ),
            )
        if idle_result.transition is not None:
            _persist_result(db, idle_result)
            row = session_store.get_session(db, session.research_session_id)
            return JsonResponseWithStatus(
                status_code=200,
                content={
                    "session": session_store.session_summary(row),
                    "next_actions": [],
                    "heartbeat_seconds": policy.heartbeat_seconds,
                },
            )

        activity = on_qualifying_activity(
            idle_result.session, now, kill_switch_check=kill_switch_check
        )
        if not activity.accepted:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    activity.reason.value,
                    (
                        activity.issue.message
                        if activity.issue is not None
                        else "heartbeat rejected"
                    ),
                ),
            )
        _persist_result(db, activity)

        row = session_store.get_session(db, session.research_session_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "session": session_store.session_summary(row),
                "next_actions": _next_actions(activity.session, policy, now),
                "heartbeat_seconds": policy.heartbeat_seconds,
            },
        )
    finally:
        db.close()


@router.post("/close", summary="Close a research session")
def close_research_session(
    payload: CloseSessionRequest,
    app: App = Depends(App.get_instance),
):
    """Apply a typed terminal close reason."""
    now = _now()
    db = app.get_db_session()
    try:
        session = _load_session(db, payload.research_session_id)
        enrollment = _load_enrollment(db, session.enrollment_id)
        _authorize(
            payload.capability,
            enrollment,
            scope=_SCOPE_CLOSE,
            now=now,
            research_session_id=session.research_session_id,
            study_id=session.study_id,
        )

        if session.state.is_terminal:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    SessionReasonCode.SESSION_TERMINAL.value,
                    f"session is {session.state.value}",
                ),
            )

        result = close(
            session,
            payload.reason,
            now,
            kill_switch_check=_kill_switch_for_session(db, session),
        )
        if not result.accepted:
            raise HTTPException(
                status_code=409,
                detail=_detail(
                    result.reason.value,
                    result.issue.message if result.issue else "close rejected",
                ),
            )
        _persist_result(db, result)
        row = session_store.get_session(db, session.research_session_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"session": session_store.session_summary(row), "next_actions": []},
        )
    finally:
        db.close()


@router.post("/summary", summary="Get a research session summary")
def get_research_session(
    payload: SessionSummaryRequest,
    app: App = Depends(App.get_instance),
):
    """Return the summary of a session, authorized by the session capability."""
    now = _now()
    db = app.get_db_session()
    try:
        session = _load_session(db, payload.research_session_id)
        enrollment = _load_enrollment(db, session.enrollment_id)
        _authorize(
            payload.capability,
            enrollment,
            scope=_SCOPE_WRITE,
            now=now,
            research_session_id=session.research_session_id,
            study_id=session.study_id,
        )
        row = session_store.get_session(db, session.research_session_id)
        return JsonResponseWithStatus(
            status_code=200,
            content={"session": session_store.session_summary(row)},
        )
    finally:
        db.close()
