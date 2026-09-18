"""Pure research-session state machine (Issue 07).

Idle and resume values are injected study-policy inputs
(:class:`~research.runtime.sessions.models.SessionPolicyV1`), never compiled constants.
Only the published lifecycle transitions are legal; every applied transition is
returned as a :class:`~research.runtime.sessions.models.SessionTransition` carrying the
reason and a policy/evidence reference.

State diagram::

    not_started -> running            (first qualifying activity)
    running     -> offline            (network unavailable)
    offline     -> running            (heartbeat/upload recovers)
    running     -> suspended          (IDE closes or sleeps)
    suspended   -> running            (restart within resume grace; +1 generation)
    suspended   -> ended              (resume grace expired)
    running     -> ended              (explicit completion / idle timeout)
    running     -> revoked            (withdrawal / consent pause)
    offline     -> revoked            (server revocation observed)
    ended, revoked -> (terminal)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

from .enums import (
    AgentRunOutcome,
    CloseReason,
    SessionReasonCode,
    SessionState,
)
from .models import (
    AgentRunResult,
    AgentRunV1,
    ResearchSessionV1,
    ResumeResult,
    SessionIssue,
    SessionPolicyV1,
    SessionResult,
    SessionTransition,
)

if TYPE_CHECKING:
    from research.participants.models import Enrollment
    from typing import Any

_LEGAL_TARGETS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.NOT_STARTED: frozenset({SessionState.RUNNING}),
    SessionState.RUNNING: frozenset(
        {
            SessionState.OFFLINE,
            SessionState.SUSPENDED,
            SessionState.ENDED,
            SessionState.REVOKED,
        }
    ),
    SessionState.OFFLINE: frozenset(
        {SessionState.RUNNING, SessionState.ENDED, SessionState.REVOKED}
    ),
    SessionState.SUSPENDED: frozenset({SessionState.RUNNING, SessionState.ENDED}),
    SessionState.ENDED: frozenset(),
    SessionState.REVOKED: frozenset(),
}


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _issue(code: SessionReasonCode, message: str, field: str = "") -> SessionIssue:
    return SessionIssue(code=code, message=message, field=field)


def _kill_switch_result(
    session: ResearchSessionV1, kill_switch_check: Optional[Callable[[], bool]]
) -> Optional[SessionResult]:
    """Return a non-accepted result when the injected kill switch is engaged.

    Returns ``None`` when no check is supplied or the switch is released, so the
    caller proceeds with the pure state machine unchanged.
    """
    if kill_switch_check is None or not kill_switch_check():
        return None
    return SessionResult(
        accepted=False,
        session=session,
        reason=SessionReasonCode.KILL_SWITCH_ENGAGED,
        issue=_issue(
            SessionReasonCode.KILL_SWITCH_ENGAGED,
            "an operator kill switch is engaged for this scope",
            "kill_switch",
        ),
    )


def can_transition(from_state: SessionState, to_state: SessionState) -> bool:
    """Whether ``to_state`` is a legal successor of ``from_state``."""
    return to_state in _LEGAL_TARGETS[from_state]


def policy_ref(policy: SessionPolicyV1) -> str:
    """Human-readable, stable reference to the applied policy values."""
    return (
        f"idle_timeout_seconds={policy.idle_timeout_seconds},"
        f"resume_grace_seconds={policy.resume_grace_seconds}"
    )


def session_policy_from_study(study: Any) -> Optional[SessionPolicyV1]:
    """Extract a usable :class:`SessionPolicyV1` from a study, or ``None``.

    ``None`` means the study does not declare both the idle timeout and the
    resume grace, so no timing decision can be made. The server never falls back
    to a compiled default.
    """
    session_policy = (getattr(study, "research_config_json", None) or {}).get(
        "session_policy", {}
    )
    if (
        session_policy.get("idle_timeout_seconds") is None
        or session_policy.get("resume_grace_seconds") is None
    ):
        return None
    return SessionPolicyV1(
        idle_timeout_seconds=session_policy["idle_timeout_seconds"],
        resume_grace_seconds=session_policy["resume_grace_seconds"],
        heartbeat_seconds=session_policy.get("heartbeat_seconds"),
    )


def open_session(
    enrollment: Enrollment,
    study: Any,
    *,
    manifest_digest: str,
    environment_ref: Optional[str] = None,
    context_id: str = "",
    now: Optional[datetime] = None,
) -> ResearchSessionV1:
    """Create a new, not-yet-started session bound to an enrollment/study."""
    return ResearchSessionV1(
        research_session_id=uuid.uuid4(),
        enrollment_id=enrollment.enrollment_id,
        study_id=study.study_id,
        context_id=context_id,
        state=SessionState.NOT_STARTED,
        opened_at=None,
        last_activity_at=None,
        closed_at=None,
        close_reason=None,
        resume_generation=0,
        manifest_digest=manifest_digest,
        environment_ref=environment_ref,
    )


def _apply(
    session: ResearchSessionV1,
    to_state: SessionState,
    now: datetime,
    *,
    reason: SessionReasonCode = SessionReasonCode.OK,
    close_reason: Optional[CloseReason] = None,
    policy_reference: Optional[str] = None,
    evidence_ref: Optional[str] = None,
    resume_generation: Optional[int] = None,
) -> SessionResult:
    if not can_transition(session.state, to_state):
        return SessionResult(
            accepted=False,
            session=session,
            reason=SessionReasonCode.INVALID_STATE_TRANSITION,
            issue=_issue(
                SessionReasonCode.INVALID_STATE_TRANSITION,
                f"illegal session transition {session.state.value} -> {to_state.value}",
                "state",
            ),
        )

    updates: dict = {"state": to_state, "last_activity_at": now}
    if to_state == SessionState.RUNNING:
        updates["opened_at"] = session.opened_at or now
    if to_state.is_terminal:
        updates["closed_at"] = now
        updates["close_reason"] = close_reason or (
            CloseReason.REVOKED
            if to_state == SessionState.REVOKED
            else CloseReason.EXPLICIT_COMPLETION
        )
    if resume_generation is not None:
        updates["resume_generation"] = resume_generation

    updated = session.model_copy(update=updates)
    transition = SessionTransition(
        transition_id=uuid.uuid4(),
        research_session_id=session.research_session_id,
        from_state=session.state,
        to_state=to_state,
        occurred_at=now,
        reason=reason,
        close_reason=updated.close_reason,
        policy_ref=policy_reference,
        evidence_ref=evidence_ref,
    )
    return SessionResult(
        accepted=True, session=updated, transition=transition, reason=reason
    )


def record_activity(
    session: ResearchSessionV1, now: Optional[datetime] = None
) -> SessionResult:
    """Update the last-activity marker without changing state."""
    timestamp = _now(now)
    if session.state.is_terminal:
        return SessionResult(accepted=True, session=session, reason=SessionReasonCode.OK)
    return SessionResult(
        accepted=True,
        session=session.model_copy(update={"last_activity_at": timestamp}),
        reason=SessionReasonCode.OK,
    )


def on_qualifying_activity(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    kill_switch_check: Optional[Callable[[], bool]] = None,
) -> SessionResult:
    """First qualifying activity: ``not_started -> running``."""
    timestamp = _now(now)
    blocked = _kill_switch_result(session, kill_switch_check)
    if blocked is not None:
        return blocked
    if session.state.is_terminal:
        return SessionResult(
            accepted=False,
            session=session,
            reason=SessionReasonCode.SESSION_TERMINAL,
            issue=_issue(
                SessionReasonCode.SESSION_TERMINAL,
                "a terminal session cannot accept new activity",
                "state",
            ),
        )
    if session.state == SessionState.NOT_STARTED:
        return _apply(session, SessionState.RUNNING, timestamp)
    return record_activity(session, timestamp)


def go_offline(
    session: ResearchSessionV1, now: Optional[datetime] = None
) -> SessionResult:
    """``running -> offline`` (network unavailable)."""
    return _apply(session, SessionState.OFFLINE, _now(now))


def recover(
    session: ResearchSessionV1, now: Optional[datetime] = None
) -> SessionResult:
    """``offline -> running`` (heartbeat/upload recovers)."""
    return _apply(session, SessionState.RUNNING, _now(now))


def suspend(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    evidence_ref: Optional[str] = None,
) -> SessionResult:
    """``running -> suspended`` (IDE closes or the machine sleeps)."""
    return _apply(
        session,
        SessionState.SUSPENDED,
        _now(now),
        evidence_ref=evidence_ref,
    )


def end(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    close_reason: CloseReason = CloseReason.EXPLICIT_COMPLETION,
    evidence_ref: Optional[str] = None,
) -> SessionResult:
    """End a live session (explicit completion or a caller-chosen reason)."""
    timestamp = _now(now)
    if session.state.is_terminal:
        return SessionResult(
            accepted=False,
            session=session,
            reason=SessionReasonCode.SESSION_TERMINAL,
            issue=_issue(
                SessionReasonCode.SESSION_TERMINAL,
                f"session is already {session.state.value}",
                "state",
            ),
        )
    return _apply(
        session,
        SessionState.ENDED,
        timestamp,
        close_reason=close_reason,
        evidence_ref=evidence_ref,
    )


def revoke(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    evidence_ref: Optional[str] = None,
) -> SessionResult:
    """Withdrawal/consent pause: terminal revocation."""
    timestamp = _now(now)
    if session.state.is_terminal:
        return SessionResult(
            accepted=False,
            session=session,
            reason=SessionReasonCode.SESSION_TERMINAL,
            issue=_issue(
                SessionReasonCode.SESSION_TERMINAL,
                f"session is already {session.state.value}",
                "state",
            ),
        )
    return _apply(
        session,
        SessionState.REVOKED,
        timestamp,
        close_reason=CloseReason.REVOKED,
        evidence_ref=evidence_ref,
    )


def resume(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    policy: SessionPolicyV1,
) -> ResumeResult:
    """Reopen a suspended session within its resume grace, or end it once."""
    timestamp = _now(now)

    if session.state.is_terminal:
        return ResumeResult(
            accepted=False,
            reopened=False,
            requires_new_session=True,
            session=session,
            reason=SessionReasonCode.SESSION_TERMINAL,
            issue=_issue(
                SessionReasonCode.SESSION_TERMINAL,
                f"session is already {session.state.value}",
                "state",
            ),
        )
    if session.state != SessionState.SUSPENDED:
        return ResumeResult(
            accepted=False,
            reopened=False,
            requires_new_session=False,
            session=session,
            reason=SessionReasonCode.NOT_SUSPENDED,
            issue=_issue(
                SessionReasonCode.NOT_SUSPENDED,
                f"session is {session.state.value}, not suspended",
                "state",
            ),
        )

    reference = policy_ref(policy)
    last_activity = session.last_activity_at
    within_grace = (
        last_activity is not None
        and (timestamp - last_activity).total_seconds() <= policy.resume_grace_seconds
    )

    if within_grace:
        result = _apply(
            session,
            SessionState.RUNNING,
            timestamp,
            policy_reference=reference,
            resume_generation=session.resume_generation + 1,
        )
        return ResumeResult(
            accepted=result.accepted,
            reopened=result.accepted,
            requires_new_session=False,
            session=result.session,
            transition=result.transition,
            reason=result.reason,
            issue=result.issue,
        )

    ended = session.model_copy(
        update={
            "state": SessionState.ENDED,
            "closed_at": timestamp,
            "close_reason": CloseReason.RESUME_GRACE_EXPIRED,
            "last_activity_at": timestamp,
        }
    )
    transition = SessionTransition(
        transition_id=uuid.uuid4(),
        research_session_id=session.research_session_id,
        from_state=session.state,
        to_state=SessionState.ENDED,
        occurred_at=timestamp,
        reason=SessionReasonCode.RESUME_GRACE_EXPIRED,
        close_reason=CloseReason.RESUME_GRACE_EXPIRED,
        policy_ref=reference,
    )
    return ResumeResult(
        accepted=True,
        reopened=False,
        requires_new_session=True,
        session=ended,
        transition=transition,
        reason=SessionReasonCode.RESUME_GRACE_EXPIRED,
    )


def expire_if_idle(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    policy: SessionPolicyV1,
    kill_switch_check: Optional[Callable[[], bool]] = None,
) -> SessionResult:
    """End a live session when it exceeds the revision idle timeout."""
    timestamp = _now(now)
    blocked = _kill_switch_result(session, kill_switch_check)
    if blocked is not None:
        return blocked
    if session.state.is_terminal or session.state == SessionState.NOT_STARTED:
        return SessionResult(accepted=True, session=session, reason=SessionReasonCode.OK)
    last_activity = session.last_activity_at
    if last_activity is None:
        return SessionResult(accepted=True, session=session, reason=SessionReasonCode.OK)
    if (timestamp - last_activity).total_seconds() <= policy.idle_timeout_seconds:
        return SessionResult(accepted=True, session=session, reason=SessionReasonCode.OK)
    return _apply(
        session,
        SessionState.ENDED,
        timestamp,
        reason=SessionReasonCode.IDLE_TIMEOUT,
        close_reason=CloseReason.IDLE_TIMEOUT,
        policy_reference=policy_ref(policy),
    )


def close(
    session: ResearchSessionV1,
    reason: CloseReason,
    now: Optional[datetime] = None,
    *,
    kill_switch_check: Optional[Callable[[], bool]] = None,
) -> SessionResult:
    """Apply a typed close reason to a session."""
    timestamp = _now(now)
    blocked = _kill_switch_result(session, kill_switch_check)
    if blocked is not None:
        return blocked
    if reason == CloseReason.REVOKED:
        return revoke(session, timestamp, evidence_ref=f"close_reason={reason.value}")
    if reason == CloseReason.IDLE_TIMEOUT:
        return _apply(
            session,
            SessionState.ENDED,
            timestamp,
            reason=SessionReasonCode.IDLE_TIMEOUT,
            close_reason=CloseReason.IDLE_TIMEOUT,
            evidence_ref=f"close_reason={reason.value}",
        )
    if reason == CloseReason.RESUME_GRACE_EXPIRED:
        return _apply(
            session,
            SessionState.ENDED,
            timestamp,
            reason=SessionReasonCode.RESUME_GRACE_EXPIRED,
            close_reason=CloseReason.RESUME_GRACE_EXPIRED,
            evidence_ref=f"close_reason={reason.value}",
        )
    return end(
        session,
        timestamp,
        close_reason=reason,
        evidence_ref=f"close_reason={reason.value}",
    )


def start_agent_run(
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
    *,
    agent_release_id: Optional[str] = None,
) -> AgentRunResult:
    """Start an agent run; allowed only while the session is ``RUNNING``."""
    timestamp = _now(now)
    if session.state != SessionState.RUNNING:
        return AgentRunResult(
            accepted=False,
            session=session,
            reason=SessionReasonCode.SESSION_NOT_RUNNING,
            issue=_issue(
                SessionReasonCode.SESSION_NOT_RUNNING,
                f"an agent run requires a RUNNING session, not {session.state.value}",
                "state",
            ),
        )
    run = AgentRunV1(
        agent_run_id=uuid.uuid4(),
        research_session_id=session.research_session_id,
        agent_release_id=agent_release_id,
        started_at=timestamp,
    )
    return AgentRunResult(
        accepted=True, run=run, session=session, reason=SessionReasonCode.OK
    )


def end_agent_run(
    run: AgentRunV1,
    outcome: AgentRunOutcome,
    now: Optional[datetime] = None,
) -> AgentRunResult:
    """End an agent run; idempotent once terminal."""
    timestamp = _now(now)
    if run.is_terminal:
        return AgentRunResult(
            accepted=True, run=run, session=None, reason=SessionReasonCode.AGENT_RUN_TERMINAL
        )
    ended = run.model_copy(update={"ended_at": timestamp, "outcome": outcome})
    return AgentRunResult(
        accepted=True, run=ended, session=None, reason=SessionReasonCode.OK
    )


def on_agent_run_crashed(
    run: AgentRunV1,
    session: ResearchSessionV1,
    now: Optional[datetime] = None,
) -> AgentRunResult:
    """Crash handling: end the run; the research session is preserved."""
    result = end_agent_run(run, AgentRunOutcome.CRASHED, now)
    return AgentRunResult(
        accepted=result.accepted,
        run=result.run,
        session=session,
        reason=result.reason,
        issue=result.issue,
    )
