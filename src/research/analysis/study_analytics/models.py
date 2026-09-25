"""Plain row types for the study analytics read models (issue 03).

The store (:mod:`research.analysis.study_analytics.store`) loads only the
columns and JSONB metadata paths these rows carry, and the pure metric functions
(:mod:`research.analysis.study_analytics.metrics`) compute over them. No row
holds an ORM object, a whole telemetry envelope, a content field (prompt or
message text, tool arguments/results, reasoning, error messages) or any account
identity: participants are identified by their study-local enrollment id and
participant code only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Sequence

__all__ = [
    "ArmRow",
    "AssignmentRow",
    "DailyEventCount",
    "DateWindow",
    "EnrollmentRow",
    "EventRow",
    "SessionRow",
    "StudyFrame",
]


@dataclass(frozen=True)
class ArmRow:
    """One frozen study arm: ``study_agent_profile`` snapshot labels only."""

    profile_id: str
    name: Optional[str] = None
    model: Optional[str] = None
    framework_version: Optional[str] = None
    selection_order: Optional[int] = None
    max_context_tokens: Optional[int] = None


@dataclass(frozen=True)
class EnrollmentRow:
    """One study-local enrollment (never joined to a login account)."""

    enrollment_id: str
    participant_code: str
    status: str
    enrolled_at: Optional[datetime] = None
    consent_accepted_at: Optional[datetime] = None


@dataclass(frozen=True)
class AssignmentRow:
    """One sticky assignment with the labels of its own frozen snapshot."""

    enrollment_id: str
    profile_id: str
    status: str
    assigned_at: Optional[datetime] = None
    name: Optional[str] = None
    model: Optional[str] = None
    framework_version: Optional[str] = None
    max_context_tokens: Optional[int] = None


@dataclass(frozen=True)
class SessionRow:
    """One research session's lifecycle timestamps."""

    session_id: str
    enrollment_id: str
    state: str
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class EventRow:
    """One metadata-only projection of a retained canonical event.

    ``tool_call_id`` is ``correlations.tool_call_id`` falling back to
    ``payload.tool_call_id`` (the ACP normalizer only sets the payload key on
    tool events). ``plan_completed`` is the ``completed`` entry of
    ``payload.plan_status_counts`` (``None`` when the plan reported no status
    counts). ``message_kind`` is set only on streamed message/thought chunks,
    which are *not* prompts. ``prompt_tokens`` is ``metrics.counts.prompt_tokens``
    (provider-reported, carried by relay-source model-call events).
    """

    event_type: str
    source: str
    occurred_at: datetime
    enrollment_id: Optional[str] = None
    session_id: Optional[str] = None
    emitter_id: str = ""
    emitter_sequence: int = 0
    turn_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    permission_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_kind: Optional[str] = None
    status: Optional[str] = None
    lifecycle_state: Optional[str] = None
    decision: Optional[str] = None
    stop_reason: Optional[str] = None
    error_code: Optional[str] = None
    message_kind: Optional[str] = None
    usage_tokens: Optional[int] = None
    prompt_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    plan_size: Optional[int] = None
    plan_completed: Optional[int] = None


@dataclass(frozen=True)
class DailyEventCount:
    """Per-enrollment, per-UTC-date presence over *all* retained events.

    ``ide_edits`` counts ``ide.document.changed`` events from the IDE collector:
    one event is one document change (its ``payload.count`` is the number of
    inserted characters, not a number of edits).
    """

    enrollment_id: str
    day: date
    events: int
    ide_edits: int = 0
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None


@dataclass(frozen=True)
class DateWindow:
    """An inclusive ``[start, end]`` range of UTC dates (either side open)."""

    start: Optional[date] = None
    end: Optional[date] = None

    @property
    def is_open(self) -> bool:
        return self.start is None and self.end is None

    def bounds(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Half-open UTC datetime bounds ``[lower, upper)`` of the window."""
        lower = (
            datetime.combine(self.start, time.min, tzinfo=timezone.utc)
            if self.start is not None
            else None
        )
        # The last representable day has no next midnight: it leaves the
        # window open above instead of overflowing.
        upper = (
            datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=timezone.utc)
            if self.end is not None and self.end < date.max
            else None
        )
        return lower, upper

    def as_json(self) -> dict[str, Optional[str]]:
        return {
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
        }


@dataclass(frozen=True)
class StudyFrame:
    """A study's frozen arms, enrollments, assignments and sessions."""

    arms: Sequence[ArmRow] = field(default_factory=tuple)
    enrollments: Sequence[EnrollmentRow] = field(default_factory=tuple)
    assignments: Sequence[AssignmentRow] = field(default_factory=tuple)
    sessions: Sequence[SessionRow] = field(default_factory=tuple)
