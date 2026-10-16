"""Research session lifecycle and agent runs (Issue 07).

Public surface:

* :mod:`research.runtime.sessions.enums` - session states, close reasons, agent-run
  outcomes and typed reason codes.
* :mod:`research.runtime.sessions.models` - ``ResearchSessionV1``, ``AgentRunV1``,
  ``SessionPolicyV1``, transitions and typed results.
* :mod:`research.runtime.sessions.service` - the pure state machine (idle/resume values
  are injected study policy, never compiled constants).
* :mod:`research.runtime.sessions.store` - persistence adapters taking a caller-supplied
  SQLAlchemy ``Session``.

The core package never imports ``App``, FastAPI, or a session factory.
"""

from .enums import AgentRunOutcome, CloseReason, SessionReasonCode, SessionState
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
from .service import (
    can_transition,
    close,
    end,
    end_agent_run,
    expire_if_idle,
    go_offline,
    on_agent_run_crashed,
    on_qualifying_activity,
    open_session,
    policy_ref,
    record_activity,
    record_liveness,
    recover,
    resume,
    revoke,
    session_policy_from_study,
    start_agent_run,
    suspend,
)

__all__ = [
    "AgentRunOutcome",
    "AgentRunResult",
    "AgentRunV1",
    "CloseReason",
    "ResearchSessionV1",
    "ResumeResult",
    "SessionIssue",
    "SessionPolicyV1",
    "SessionReasonCode",
    "SessionResult",
    "SessionState",
    "SessionTransition",
    "can_transition",
    "close",
    "end",
    "end_agent_run",
    "expire_if_idle",
    "go_offline",
    "on_agent_run_crashed",
    "on_qualifying_activity",
    "open_session",
    "policy_ref",
    "record_activity",
    "record_liveness",
    "recover",
    "resume",
    "revoke",
    "session_policy_from_study",
    "start_agent_run",
    "suspend",
]
