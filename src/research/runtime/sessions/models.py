"""Pydantic v2 contracts for the research session lifecycle (Issue 07).

A ``ResearchSessionV1`` is the authoritative participant-activity boundary. An
``AgentRunV1`` is a distinct child identity: its id is never the session id, and
an agent restart inside the resume grace creates a new run without ending the
session. Idle/resume values live in :class:`SessionPolicyV1` (a revision policy
input), never as compiled constants.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - pydantic resolves model annotations at runtime
from uuid import UUID  # noqa: TC003 - pydantic resolves model annotations at runtime

from pydantic import BaseModel, ConfigDict, model_validator

from .enums import (
    AgentRunOutcome,
    CloseReason,
    SessionReasonCode,
    SessionState,
)

_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BASE = ConfigDict(extra="forbid")


class SessionPolicyV1(BaseModel):
    """Study-policy timing inputs for a session (never compiled constants)."""

    model_config = _BASE

    idle_timeout_seconds: int
    resume_grace_seconds: int
    heartbeat_seconds: int | None = None

    @model_validator(mode="after")
    def _positive(self) -> SessionPolicyV1:
        if self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if self.resume_grace_seconds <= 0:
            raise ValueError("resume_grace_seconds must be positive")
        if self.heartbeat_seconds is not None and self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive when present")
        return self


class ResearchSessionV1(BaseModel):
    """One research session (authoritative participant-activity boundary)."""

    model_config = _FROZEN

    research_session_id: UUID
    enrollment_id: UUID
    study_id: UUID
    # Opaque execution-context id (project/window instance); never a path.
    context_id: str = ""
    state: SessionState = SessionState.NOT_STARTED
    opened_at: datetime | None = None
    last_activity_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: CloseReason | None = None
    resume_generation: int = 0
    manifest_digest: str = ""
    environment_ref: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> ResearchSessionV1:
        if self.resume_generation < 0:
            raise ValueError("resume_generation must not be negative")
        if self.state.is_terminal:
            if self.closed_at is None or self.close_reason is None:
                raise ValueError("a terminal session requires closed_at and close_reason")
        elif self.closed_at is not None or self.close_reason is not None:
            raise ValueError("a non-terminal session cannot have closed_at/close_reason")
        return self


class AgentRunV1(BaseModel):
    """One agent process inside a research session (distinct child identity)."""

    model_config = _FROZEN

    agent_run_id: UUID
    research_session_id: UUID
    agent_release_id: str | None = None
    assignment_id: UUID | None = None
    agent_profile_id: UUID | None = None
    profile_digest: str | None = None
    profile_snapshot_json: dict[str, object] | None = None
    started_at: datetime
    ended_at: datetime | None = None
    outcome: AgentRunOutcome | None = None

    @model_validator(mode="after")
    def _consistency(self) -> AgentRunV1:
        if self.agent_run_id == self.research_session_id:
            raise ValueError(
                "AgentRun and ResearchSession identifiers must never be interchangeable"
            )
        if self.ended_at is not None:
            if self.ended_at < self.started_at:
                raise ValueError("an agent run cannot end before it starts")
            if self.outcome is None:
                raise ValueError("a terminal agent run requires an outcome")
        elif self.outcome is not None:
            raise ValueError("a running agent run cannot have an outcome")
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether this run has ended."""
        return self.ended_at is not None


class SessionTransition(BaseModel):
    """One recorded state transition (policy/evidence referenced)."""

    model_config = _FROZEN

    transition_id: UUID
    research_session_id: UUID
    from_state: SessionState
    to_state: SessionState
    occurred_at: datetime
    reason: SessionReasonCode = SessionReasonCode.OK
    close_reason: CloseReason | None = None
    policy_ref: str | None = None
    evidence_ref: str | None = None


class SessionIssue(BaseModel):
    """One typed session/run rejection reason."""

    model_config = _BASE

    code: SessionReasonCode
    message: str
    field: str = ""


class SessionResult(BaseModel):
    """Typed outcome of a session operation (state + optional transition)."""

    model_config = _BASE

    accepted: bool
    session: ResearchSessionV1
    transition: SessionTransition | None = None
    reason: SessionReasonCode = SessionReasonCode.OK
    issue: SessionIssue | None = None


class ResumeResult(BaseModel):
    """Typed outcome of a resume attempt."""

    model_config = _BASE

    accepted: bool
    reopened: bool
    requires_new_session: bool
    session: ResearchSessionV1
    transition: SessionTransition | None = None
    reason: SessionReasonCode = SessionReasonCode.OK
    issue: SessionIssue | None = None


class AgentRunResult(BaseModel):
    """Typed outcome of starting/ending an agent run."""

    model_config = _BASE

    accepted: bool
    run: AgentRunV1 | None = None
    session: ResearchSessionV1 | None = None
    reason: SessionReasonCode = SessionReasonCode.OK
    issue: SessionIssue | None = None
