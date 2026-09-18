"""Closed vocabularies for the research session lifecycle (Issue 07).

Values match the Kotlin client contract in
``code4me2/src/main/kotlin/me/code4me/research/session`` so the server and the
plugin/proxy agree on the wire vocabulary. Members are additive only.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AgentRunOutcome",
    "CloseReason",
    "SessionReasonCode",
    "SessionState",
]


class SessionState(str, Enum):
    """Participant activity-session lifecycle states."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    OFFLINE = "offline"
    SUSPENDED = "suspended"
    ENDED = "ended"
    REVOKED = "revoked"

    @property
    def is_terminal(self) -> bool:
        """Whether this state is terminal (``ENDED`` or ``REVOKED``)."""
        return self in (SessionState.ENDED, SessionState.REVOKED)


class CloseReason(str, Enum):
    """Why a research session reached a terminal state."""

    EXPLICIT_COMPLETION = "explicit_completion"
    IDLE_TIMEOUT = "idle_timeout"
    RESUME_GRACE_EXPIRED = "resume_grace_expired"
    IDE_CLOSED = "ide_closed"
    REVOKED = "revoked"
    CRASH_RECOVERED = "crash_recovered"
    MIGRATED = "migrated"


class AgentRunOutcome(str, Enum):
    """Terminal outcome of one agent run."""

    COMPLETED = "completed"
    CRASHED = "crashed"
    REVOKED = "revoked"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


class SessionReasonCode(str, Enum):
    """Stable machine-readable reason for a session/run operation outcome."""

    OK = "OK"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    SESSION_TERMINAL = "SESSION_TERMINAL"
    NOT_SUSPENDED = "NOT_SUSPENDED"
    RESUME_GRACE_EXPIRED = "RESUME_GRACE_EXPIRED"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    POLICY_MISSING = "POLICY_MISSING"
    SESSION_NOT_RUNNING = "SESSION_NOT_RUNNING"
    AGENT_RUN_TERMINAL = "AGENT_RUN_TERMINAL"
    ENROLLMENT_NOT_ACTIVE = "ENROLLMENT_NOT_ACTIVE"
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    REVOKED = "REVOKED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
