"""Pure participant-level study analytics over plain rows (issue 03).

Everything here is a pure function of :mod:`research.analysis.study_analytics.models`
rows: no database, no FastAPI, no clock (``now`` is always a parameter). The
participant (enrollment) is the randomisation unit, so every arm comparison
aggregates within a participant first and then summarises the per-participant
values.

Event semantics (verified against the producers on 2026-09-24):

* ``agent.message.started`` is emitted by the shared ACP normalizer for a
  ``session/prompt`` request (the user prompt, a turn start: no
  ``payload.message_kind`` and no lifecycle state) *and* for every streamed
  ``agent_message_chunk`` / ``agent_thought_chunk`` (``payload.message_kind`` =
  ``assistant``/``thought``, lifecycle ``started``). Only the former is a prompt.
* ``agent.message.completed`` observed by the ACP proxy is the prompt response
  (``payload.stop_reason``, ``metrics.usage_tokens``). With source ``relay`` it
  is one provider model call (``payload.legacy_kind = model_call``; emitter
  ``relay`` for ``/api/agent/inference``, ``self-report`` for the managed
  runtime's ``agent.model.completed``, ``proxy`` for OTel spans) carrying exact
  provider usage and ``metrics.counts.prompt_tokens`` - never a prompt, a turn
  or a tool call. Managed ``/api/acp/inference`` relays record no event of their
  own (the runtime self-reports instead).
* ``tool.*`` with source ``relay`` are tool executions the relay reconstructs
  from model requests; the same call is normally also observed by the ACP proxy.
  ACP tool events therefore own a research session's tool metrics whenever the
  session has any, and relay tool calls count only in sessions without them.
* Turn and permission ids are JSON-RPC request ids that restart with every proxy
  process, so they are keyed by (research session, emitter, id).
* ``ide.document.changed`` is one IDE document change; its ``payload.count`` is
  the number of inserted characters, so edits are counted per event.
* ``ide.file.saved`` with source ``acp`` is an agent ``fs/write_text_file``.

A value that cannot be computed is ``None`` (JSON ``null``), never ``0``.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from research.telemetry.enums import CanonicalEventType, EventSource

from .models import (
    ArmRow,
    AssignmentRow,
    DailyEventCount,
    DateWindow,
    EnrollmentRow,
    EventRow,
    SessionRow,
    StudyFrame,
)

__all__ = [
    "ANALYTIC_EVENT_TYPES",
    "FALLBACK_MAX_CONTEXT_TOKENS",
    "METRIC_KEYS",
    "ParticipantAnalysis",
    "TIMELINE_EXCLUDED_EVENT_TYPES",
    "Turn",
    "ToolCall",
    "analyze_participant",
    "build_participant_detail",
    "build_participants",
    "build_study_summary",
    "build_turns",
    "context_block",
    "display_tool_name",
    "effective_context_cap",
    "iso",
    "median",
    "metric_summary",
    "percentile",
    "session_seconds",
    "transitions",
]

# -- vocabulary ---------------------------------------------------------------

PROMPT_EVENT = CanonicalEventType.AGENT_MESSAGE_STARTED.value
COMPLETION_EVENT = CanonicalEventType.AGENT_MESSAGE_COMPLETED.value
CANCEL_EVENT = CanonicalEventType.INTERACTION_COMPLETED.value
TOOL_CREATED_EVENT = CanonicalEventType.TOOL_CREATED.value
TOOL_STARTED_EVENT = CanonicalEventType.TOOL_STARTED.value
TOOL_COMPLETED_EVENT = CanonicalEventType.TOOL_COMPLETED.value
TOOL_FAILED_EVENT = CanonicalEventType.TOOL_FAILED.value
TOOL_EVENTS = frozenset(
    {TOOL_CREATED_EVENT, TOOL_STARTED_EVENT, TOOL_COMPLETED_EVENT, TOOL_FAILED_EVENT}
)
PERMISSION_REQUESTED_EVENT = CanonicalEventType.PERMISSION_REQUESTED.value
PERMISSION_DECIDED_EVENT = CanonicalEventType.PERMISSION_DECIDED.value
PLAN_EVENT = CanonicalEventType.PLAN_UPDATED.value
USAGE_EVENT = CanonicalEventType.USAGE_UPDATED.value
ERROR_EVENTS = frozenset(
    {
        CanonicalEventType.AGENT_ERROR.value,
        CanonicalEventType.SYSTEM_AGENT_CRASHED.value,
        CanonicalEventType.SYSTEM_PROXY_ERROR.value,
    }
)
FILE_SAVED_EVENT = CanonicalEventType.IDE_FILE_SAVED.value
DOCUMENT_CHANGED_EVENT = CanonicalEventType.IDE_DOCUMENT_CHANGED.value

#: Every event type the metric set reads (streamed message chunks excluded).
ANALYTIC_EVENT_TYPES = frozenset(
    {
        PROMPT_EVENT,
        COMPLETION_EVENT,
        CANCEL_EVENT,
        PERMISSION_REQUESTED_EVENT,
        PERMISSION_DECIDED_EVENT,
        PLAN_EVENT,
        FILE_SAVED_EVENT,
    }
    | TOOL_EVENTS
    | ERROR_EVENTS
)
#: High-volume event types never listed on the replay timeline (streamed
#: message chunks are excluded as well). They still count in ``ide_edits`` /
#: presence through the per-day aggregates.
TIMELINE_EXCLUDED_EVENT_TYPES = frozenset({DOCUMENT_CHANGED_EVENT, USAGE_EVENT})

RELAY_SOURCE = EventSource.RELAY.value
ACP_SOURCE = EventSource.ACP.value
IDE_SOURCE = EventSource.IDE.value

EDIT_TOOL_KIND = "edit"
OTHER_TOOL_KIND = "other"
FAILED_STATUSES = frozenset({"failed", "error"})
CANCELLED_STOP_REASON = "cancelled"
UNKNOWN = "unknown"
TERMINAL_SESSION_STATES = frozenset({"ended", "revoked"})
ACTIVE_ENROLLMENT_STATUS = "ACTIVE"
#: A participant is ``ACTIVE`` when their last event is this recent.
ACTIVE_WINDOW = timedelta(days=7)

#: Mirrors ``FALLBACK_MAX_CONTEXT_TOKENS`` in ``src/backend/routers/acp/__init__.py``
#: (the managed runtime's context budget when a profile sets none). Kept local:
#: importing the ACP router module would drag in the whole ACP surface.
FALLBACK_MAX_CONTEXT_TOKENS = 16_000

TOP_TOOLS_PARTICIPANT = 15
TOP_TOOLS_STUDY = 20
MAX_SESSIONS_LIST = 50
MAX_TURNS = 100
MAX_TIMELINE = 200

#: A tool identifier (``read_file``, ``developer__shell``, ``mcp__github__search``).
#: No spaces, dots or slashes: a title describes an action on a file or a
#: command ("Read src/app.py", "Run git status"), and a bare file name such as
#: "secrets.env" must not pass as a tool name either.
_TOOL_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{0,63}")

#: Participant-level metric set M, in display order.
METRIC_KEYS: tuple[str, ...] = (
    "prompts",
    "active_days",
    "session_hours",
    "prompts_per_session_hour",
    "tool_calls_per_prompt",
    "tool_failure_rate",
    "auto_run_share",
    "permission_denial_rate",
    "median_permission_wait_seconds",
    "cancel_rate",
    "median_turn_seconds",
    "tokens_per_prompt",
    "errors_per_prompt",
    "agent_writes_per_prompt",
    "seconds_to_first_agent_edit",
    "ide_edits_per_session_hour",
    "plan_completion_rate",
)
#: Count-type metrics include every assigned participant (a participant without
#: telemetry contributes 0); every other metric excludes participants without
#: telemetry and uses null-free value lists.
COUNT_METRIC_KEYS = frozenset({"prompts", "active_days", "session_hours"})

_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)
_UTC_MAX = datetime.max.replace(tzinfo=timezone.utc)


# -- small helpers ------------------------------------------------------------


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as an aware UTC datetime (naive values are UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    """ISO-8601 UTC with a ``Z`` suffix, or ``None``."""
    moment = as_utc(value)
    if moment is None:
        return None
    return moment.isoformat().replace("+00:00", "Z")


def _round(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _r3(value: Optional[float]) -> Optional[float]:
    return _round(value, 3)


def _r1(value: Optional[float]) -> Optional[float]:
    return _round(value, 1)


def percentile(values: Iterable[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy's default); ``None`` when empty."""
    data = sorted(float(value) for value in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[lower]
    return data[lower] + (data[upper] - data[lower]) * (position - lower)


def median(values: Iterable[float]) -> Optional[float]:
    return percentile(values, 0.5)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def order_key(row: EventRow) -> tuple[datetime, str, int]:
    """Deterministic event order: time, then the emitter's own sequence."""
    return (row.occurred_at, row.emitter_id or "", row.emitter_sequence or 0)


def _utc_day(value: datetime) -> date:
    return as_utc(value).date()  # type: ignore[union-attr]


def is_message_chunk(row: EventRow) -> bool:
    """A streamed assistant/thought chunk (never a prompt)."""
    return row.event_type == PROMPT_EVENT and (
        row.message_kind is not None or (row.lifecycle_state or "") == "started"
    )


def is_prompt(row: EventRow) -> bool:
    """A ``session/prompt`` observation: one user prompt and one turn start."""
    return (
        row.event_type == PROMPT_EVENT
        and row.source == ACP_SOURCE
        and not is_message_chunk(row)
    )


def is_turn_completion(row: EventRow) -> bool:
    """An ACP prompt response (never a relay-observed model call)."""
    return row.event_type == COMPLETION_EVENT and row.source == ACP_SOURCE


def is_model_call(row: EventRow) -> bool:
    """One relay-observed / self-reported provider model call."""
    return row.event_type == COMPLETION_EVENT and row.source == RELAY_SOURCE


def is_cancel(row: EventRow) -> bool:
    """An ACP ``session/cancel`` (a user interrupt)."""
    return row.event_type == CANCEL_EVENT and row.source == ACP_SOURCE


def _in_window(moment: datetime, window: Optional[DateWindow]) -> bool:
    if window is None or window.is_open:
        return True
    lower, upper = window.bounds()
    moment = as_utc(moment)  # type: ignore[assignment]
    if lower is not None and moment < lower:
        return False
    if upper is not None and moment >= upper:
        return False
    return True


def _day_in_window(day: date, window: Optional[DateWindow]) -> bool:
    if window is None:
        return True
    if window.start is not None and day < window.start:
        return False
    if window.end is not None and day > window.end:
        return False
    return True


# -- sessions -----------------------------------------------------------------


def session_interval(
    row: SessionRow, window: Optional[DateWindow] = None
) -> Optional[tuple[datetime, datetime]]:
    """``[opened_at, coalesce(closed, last activity, last heartbeat)]`` clipped.

    ``None`` for a session that never opened. A missing or earlier end yields a
    zero-length interval (negatives are clamped to 0).
    """
    start = as_utc(row.opened_at)
    if start is None:
        return None
    end = as_utc(row.closed_at or row.last_activity_at or row.last_heartbeat_at)
    if end is None or end < start:
        end = start
    if window is not None and not window.is_open:
        lower, upper = window.bounds()
        if lower is not None and start < lower:
            start = lower
        if upper is not None and end > upper:
            end = upper
        if end < start:
            end = start
    return start, end


def session_seconds(row: SessionRow, window: Optional[DateWindow] = None) -> float:
    interval = session_interval(row, window)
    if interval is None:
        return 0.0
    return max(0.0, (interval[1] - interval[0]).total_seconds())


def split_seconds_by_day(start: datetime, end: datetime) -> dict[date, float]:
    """Split ``[start, end)`` into UTC calendar days."""
    seconds: dict[date, float] = {}
    cursor = as_utc(start)
    stop = as_utc(end)
    if cursor is None or stop is None:
        return seconds
    while cursor < stop:
        midnight = datetime.combine(
            cursor.date() + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
        segment_end = min(stop, midnight)
        seconds[cursor.date()] = seconds.get(cursor.date(), 0.0) + (
            segment_end - cursor
        ).total_seconds()
        cursor = segment_end
    return seconds


def session_opened_in_window(row: SessionRow, window: Optional[DateWindow]) -> bool:
    """Without a window every session counts; with one, by its opening date."""
    if window is None or window.is_open:
        return True
    opened = as_utc(row.opened_at)
    return opened is not None and _in_window(opened, window)


# -- turns and tool calls -------------------------------------------------------


@dataclass
class ToolCall:
    """One distinct tool call assembled from its lifecycle events."""

    source: str
    session_id: Optional[str]
    emitter_id: str
    tool_call_id: Optional[str]
    first_at: datetime
    first_order: tuple
    turn_ref: Optional[str] = None
    tool_name: Optional[str] = None
    tool_kind: Optional[str] = None
    last_event_type: str = ""
    last_status: Optional[str] = None
    completed_at: Optional[datetime] = None
    terminal_at: Optional[datetime] = None
    latency_ms: Optional[int] = None
    requested_permission: bool = False
    turn: Optional["Turn"] = None

    @property
    def is_relay(self) -> bool:
        return self.source == RELAY_SOURCE

    @property
    def kind(self) -> str:
        return self.tool_kind or OTHER_TOOL_KIND

    @property
    def failed(self) -> bool:
        """Failed when the final status event is ``tool.failed``/status failed."""
        return self.last_event_type == TOOL_FAILED_EVENT or (
            self.last_status in FAILED_STATUSES
        )

    @property
    def is_agent_write(self) -> bool:
        return self.kind == EDIT_TOOL_KIND and not self.failed

    @property
    def duration_ms(self) -> Optional[float]:
        """Observed lifecycle duration, else a reported ``latency_ms``."""
        if self.terminal_at is not None and self.terminal_at > self.first_at:
            return (self.terminal_at - self.first_at).total_seconds() * 1000.0
        if self.latency_ms is not None and self.latency_ms >= 0:
            return float(self.latency_ms)
        return None


@dataclass
class Turn:
    """One prompt turn: ``agent.message.started`` to its prompt response."""

    session_id: Optional[str]
    emitter_id: str
    turn_id: Optional[str]
    started_at: datetime
    start_order: tuple
    completed_at: Optional[datetime] = None
    stop_reason: Optional[str] = None
    completion_usage: Optional[int] = None
    relay_usage: Optional[int] = None
    usage_tokens: Optional[int] = None
    matched_by_turn_id: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    permission_requests: int = 0
    cancel_events: int = 0
    last_plan: Optional[EventRow] = None

    def complete(self, completion: EventRow, *, by_turn_id: bool) -> None:
        self.completed_at = completion.occurred_at
        self.stop_reason = completion.stop_reason
        self.completion_usage = completion.usage_tokens
        self.matched_by_turn_id = by_turn_id

    @property
    def completed(self) -> bool:
        return self.completed_at is not None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return max(0.0, (self.completed_at - self.started_at).total_seconds())

    @property
    def cancelled(self) -> bool:
        return self.stop_reason == CANCELLED_STOP_REASON or self.cancel_events > 0

    @property
    def tool_failures(self) -> int:
        return sum(1 for call in self.tool_calls if call.failed)

    @property
    def plan_completion(self) -> Optional[float]:
        plan = self.last_plan
        if plan is None or not plan.plan_size or plan.plan_completed is None:
            return None
        return min(1.0, max(0.0, plan.plan_completed / plan.plan_size))


def build_turns(prompts: Iterable[EventRow], completions: Iterable[EventRow]) -> list[Turn]:
    """Pair prompts with their prompt responses.

    Pass 1 matches the same (research session, emitter, turn id). Pass 2 pairs a
    still-open turn with the next unmatched completion in the same research
    session when either side lacks a turn id, never past the session's next
    prompt and never across two different known turn ids. A prompt without a
    completion stays an open (incomplete) turn.
    """
    ordered_prompts = sorted(prompts, key=order_key)
    ordered_completions = sorted(completions, key=order_key)
    turns = [
        Turn(
            session_id=prompt.session_id,
            emitter_id=prompt.emitter_id,
            turn_id=prompt.turn_id,
            started_at=prompt.occurred_at,
            start_order=order_key(prompt),
        )
        for prompt in ordered_prompts
    ]

    keyed: dict[tuple, list[EventRow]] = defaultdict(list)
    for completion in ordered_completions:
        if completion.turn_id is not None:
            keyed[
                (completion.session_id, completion.emitter_id, completion.turn_id)
            ].append(completion)
    used: set[int] = set()
    for turn in turns:
        if turn.turn_id is None:
            continue
        for completion in keyed.get((turn.session_id, turn.emitter_id, turn.turn_id), ()):
            if id(completion) in used or order_key(completion) < turn.start_order:
                continue
            turn.complete(completion, by_turn_id=True)
            used.add(id(completion))
            break

    turns_by_session: dict[Optional[str], list[Turn]] = defaultdict(list)
    for turn in turns:
        turns_by_session[turn.session_id].append(turn)
    completions_by_session: dict[Optional[str], list[EventRow]] = defaultdict(list)
    for completion in ordered_completions:
        if id(completion) not in used:
            completions_by_session[completion.session_id].append(completion)
    for session_id, session_turns in turns_by_session.items():
        pending = completions_by_session.get(session_id, [])
        if not pending:
            continue
        for position, turn in enumerate(session_turns):
            if turn.completed:
                continue
            next_start = (
                session_turns[position + 1].start_order
                if position + 1 < len(session_turns)
                else None
            )
            for completion in pending:
                if id(completion) in used:
                    continue
                completion_order = order_key(completion)
                if completion_order < turn.start_order:
                    continue
                if next_start is not None and completion_order >= next_start:
                    break
                if turn.turn_id is not None and completion.turn_id is not None:
                    continue
                turn.complete(completion, by_turn_id=False)
                used.add(id(completion))
                break
    return turns


class TurnIndex:
    """Attributes events to turns.

    An ACP event carrying a turn id belongs to the turn with the same (session,
    emitter, turn id); one without a turn id belongs to the turn of the same
    research session whose window contains it. A relay event (different clock,
    no turn id) belongs to the latest turn of its research session started at or
    before it.
    """

    def __init__(self, turns: Iterable[Turn]) -> None:
        self.by_key: dict[tuple, Turn] = {}
        self._starts: dict[Optional[str], list[tuple]] = defaultdict(list)
        self._turns: dict[Optional[str], list[Turn]] = defaultdict(list)
        for turn in sorted(turns, key=lambda item: item.start_order):
            if turn.turn_id is not None:
                self.by_key.setdefault((turn.session_id, turn.emitter_id, turn.turn_id), turn)
            self._starts[turn.session_id].append(turn.start_order)
            self._turns[turn.session_id].append(turn)

    def latest_in_session(
        self, session_id: Optional[str], moment: datetime
    ) -> Optional[Turn]:
        starts = self._starts.get(session_id)
        if not starts:
            return None
        position = bisect.bisect_right(starts, (moment, "￿", math.inf))
        return self._turns[session_id][position - 1] if position else None

    def within_session(
        self, session_id: Optional[str], moment: datetime
    ) -> Optional[Turn]:
        turn = self.latest_in_session(session_id, moment)
        if turn is None:
            return None
        if turn.completed_at is not None and moment > turn.completed_at:
            return None
        return turn

    def for_event(self, row: EventRow) -> Optional[Turn]:
        if row.source == RELAY_SOURCE:
            return self.latest_in_session(row.session_id, row.occurred_at)
        if row.turn_id is not None:
            return self.by_key.get((row.session_id, row.emitter_id, row.turn_id))
        return self.within_session(row.session_id, row.occurred_at)

    def for_tool_call(self, call: ToolCall) -> Optional[Turn]:
        if call.is_relay:
            return self.latest_in_session(call.session_id, call.first_at)
        if call.turn_ref is not None:
            return self.by_key.get((call.session_id, call.emitter_id, call.turn_ref))
        return self.within_session(call.session_id, call.first_at)


def _tool_call_key(row: EventRow) -> Optional[tuple]:
    if row.source == RELAY_SOURCE:
        # Each relay event is one execution; an id merges the relay's emitters.
        if row.tool_call_id:
            return (True, row.session_id, "", row.tool_call_id)
        return (True, row.session_id, row.emitter_id, f"#{row.emitter_sequence}")
    if not row.tool_call_id:
        # e.g. ``terminal/create`` has no tool call id: it belongs to (and would
        # double count) the execute tool call that owns the terminal.
        return None
    return (False, row.session_id, row.emitter_id, row.tool_call_id)


def build_tool_calls(rows: Iterable[EventRow]) -> dict[tuple, ToolCall]:
    """Group ``tool.*`` events into distinct tool calls (rows in event order)."""
    calls: dict[tuple, ToolCall] = {}
    for row in sorted(rows, key=order_key):
        if row.event_type not in TOOL_EVENTS:
            continue
        key = _tool_call_key(row)
        if key is None:
            continue
        call = calls.get(key)
        if call is None:
            call = ToolCall(
                source=row.source,
                session_id=row.session_id,
                emitter_id=row.emitter_id,
                tool_call_id=row.tool_call_id,
                first_at=row.occurred_at,
                first_order=order_key(row),
            )
            calls[key] = call
        if call.turn_ref is None and row.turn_id is not None:
            call.turn_ref = row.turn_id
        if call.tool_name is None and row.tool_name:
            call.tool_name = row.tool_name
        if call.tool_kind is None and row.tool_kind:
            call.tool_kind = str(row.tool_kind).strip().lower() or None
        call.last_event_type = row.event_type
        call.last_status = (row.status or "").strip().lower() or None
        if row.event_type == TOOL_COMPLETED_EVENT and call.completed_at is None:
            call.completed_at = row.occurred_at
        if row.event_type in (TOOL_COMPLETED_EVENT, TOOL_FAILED_EVENT):
            call.terminal_at = row.occurred_at
        if row.latency_ms is not None:
            call.latency_ms = row.latency_ms
    return calls


def count_agent_writes(
    tool_calls: Iterable[ToolCall], agent_saves: Iterable[EventRow], index: TurnIndex
) -> int:
    """Files the agent wrote: per turn, the larger of its two observations.

    One write can be seen twice: as a successful ``edit`` tool call and as the
    ``fs/write_text_file`` request that call made through the IDE
    (``ide.file.saved`` from the ACP proxy). Agents that write to disk
    themselves produce only the call; a write outside any tool call produces
    only the save. Per turn (else per research session) the count is the
    larger of the two, never their sum.
    """
    edits: Counter = Counter()
    saves: Counter = Counter()
    for call in tool_calls:
        if call.is_agent_write:
            turn = index.for_tool_call(call)
            edits[id(turn) if turn is not None else ("session", call.session_id)] += 1
    for save in agent_saves:
        turn = index.for_event(save)
        saves[id(turn) if turn is not None else ("session", save.session_id)] += 1
    return sum(max(edits[key], saves[key]) for key in set(edits) | set(saves))


def transitions(turns: Iterable[Turn]) -> list[dict[str, Any]]:
    """Consecutive ``tool_kind`` pairs within turns, with lift.

    Each turn's tool calls are ordered by first occurrence. ``lift`` is
    ``P(to | from) / P(to)`` over all counted transitions (``None`` when a
    denominator is 0).
    """
    pairs: Counter[tuple[str, str]] = Counter()
    for turn in turns:
        kinds = [call.kind for call in sorted(turn.tool_calls, key=lambda call: call.first_order)]
        for source_kind, target_kind in zip(kinds, kinds[1:]):
            pairs[(source_kind, target_kind)] += 1
    total = sum(pairs.values())
    from_totals: Counter[str] = Counter()
    to_totals: Counter[str] = Counter()
    for (source_kind, target_kind), count in pairs.items():
        from_totals[source_kind] += count
        to_totals[target_kind] += count
    rows: list[dict[str, Any]] = []
    for (source_kind, target_kind), count in pairs.items():
        conditional = _ratio(count, from_totals[source_kind])
        marginal = _ratio(to_totals[target_kind], total)
        lift = _ratio(conditional, marginal) if conditional is not None else None
        rows.append(
            {"from": source_kind, "to": target_kind, "count": count, "lift": _r3(lift)}
        )
    rows.sort(key=lambda row: (-row["count"], row["from"], row["to"]))
    return rows


# -- participant analysis ---------------------------------------------------------


@dataclass
class ParticipantAnalysis:
    """Every intermediate needed for one participant's rows and metrics."""

    prompts: list[EventRow]
    turns: list[Turn]
    completions: list[EventRow]
    model_calls: list[EventRow]
    tool_calls: list[ToolCall]
    call_lookup: dict[tuple, ToolCall]
    permission_requests: list[EventRow]
    decision_counts: Counter
    permission_waits: list[float]
    cancels: list[EventRow]
    errors: list[EventRow]
    agent_saves: list[EventRow]
    agent_file_writes: int
    usage_tokens: Optional[int]
    session_seconds: float
    session_seconds_by_session: dict[str, float]
    session_seconds_by_day: dict[date, float]
    ide_edits: int
    ide_edits_by_day: dict[date, int]
    events_by_day: dict[date, int]
    event_count: int
    first_event_at: Optional[datetime]
    last_event_at: Optional[datetime]
    first_edit_seconds: list[float]

    @property
    def has_telemetry(self) -> bool:
        return self.event_count > 0

    @property
    def acp_tool_calls(self) -> list[ToolCall]:
        return [call for call in self.tool_calls if not call.is_relay]

    @property
    def tool_failures(self) -> int:
        return sum(1 for call in self.tool_calls if call.failed)

    @property
    def active_days(self) -> int:
        return len({_utc_day(prompt.occurred_at) for prompt in self.prompts})

    @property
    def stop_reason_counts(self) -> Counter:
        return Counter(
            (completion.stop_reason or UNKNOWN) for completion in self.completions
        )

    @property
    def completed_turn_seconds(self) -> list[float]:
        return [
            turn.duration_seconds
            for turn in self.turns
            if turn.duration_seconds is not None
        ]

    def metrics(self) -> dict[str, Optional[float]]:
        """The participant-level metric set M (floats rounded to 3 decimals)."""
        prompts = len(self.prompts)
        session_hours = self.session_seconds / 3600.0
        tool_calls = len(self.tool_calls)
        acp_calls = self.acp_tool_calls
        auto_run = sum(1 for call in acp_calls if not call.requested_permission)
        allow = self.decision_counts.get("allow", 0)
        reject = self.decision_counts.get("reject", 0)
        turns = len(self.turns)
        cancelled = sum(1 for turn in self.turns if turn.cancelled)
        usage = [
            turn.usage_tokens
            for turn in self.turns
            if turn.completed and turn.usage_tokens is not None
        ]
        plan_rates = [
            rate
            for rate in (turn.plan_completion for turn in self.turns)
            if rate is not None
        ]
        return {
            "prompts": prompts,
            "active_days": self.active_days,
            "session_hours": _r3(session_hours),
            "prompts_per_session_hour": _r3(_ratio(prompts, session_hours)),
            "tool_calls_per_prompt": _r3(_ratio(tool_calls, prompts)),
            "tool_failure_rate": _r3(_ratio(self.tool_failures, tool_calls)),
            "auto_run_share": _r3(_ratio(auto_run, len(acp_calls))),
            "permission_denial_rate": _r3(_ratio(reject, allow + reject)),
            "median_permission_wait_seconds": _r3(median(self.permission_waits)),
            "cancel_rate": _r3(_ratio(cancelled, turns)),
            "median_turn_seconds": _r3(median(self.completed_turn_seconds)),
            "tokens_per_prompt": _r3(_ratio(sum(usage), len(usage)) if usage else None),
            "errors_per_prompt": _r3(_ratio(len(self.errors), prompts)),
            "agent_writes_per_prompt": _r3(_ratio(self.agent_file_writes, prompts)),
            "seconds_to_first_agent_edit": _r3(median(self.first_edit_seconds)),
            "ide_edits_per_session_hour": _r3(_ratio(self.ide_edits, session_hours)),
            "plan_completion_rate": _r3(_mean(plan_rates)),
        }


def analyze_participant(
    events: Iterable[EventRow],
    sessions: Iterable[SessionRow],
    daily: Optional[Iterable[DailyEventCount]] = None,
    *,
    window: Optional[DateWindow] = None,
) -> ParticipantAnalysis:
    """Analyse one participant's metadata-only events and sessions.

    ``daily`` (per-date aggregates over *all* retained events) supplies event
    presence, first/last event time and IDE edit counts; when it is ``None``
    they are derived from ``events`` (``ide.document.changed`` rows count one
    edit each). The builders pass ``None`` for an enrollment without aggregate
    rows, which in the database means it has no retained events at all.
    """
    rows = sorted(
        (row for row in events if _in_window(row.occurred_at, window)), key=order_key
    )

    prompts = [row for row in rows if is_prompt(row)]
    completions = [row for row in rows if is_turn_completion(row)]
    model_calls = [row for row in rows if is_model_call(row)]
    turns = build_turns(prompts, completions)
    index = TurnIndex(turns)

    # Tool calls, with ACP/proxy ownership of a session's tool metrics.
    call_lookup = build_tool_calls(rows)
    # An ACP title is not a tool name. Where the relay reported the same call
    # (the built-in agent does), its tool identifier names the ACP call.
    for call in call_lookup.values():
        if call.is_relay or not call.tool_call_id or display_tool_name(call.tool_name):
            continue
        relay = call_lookup.get((True, call.session_id, "", call.tool_call_id))
        if relay is not None and display_tool_name(relay.tool_name):
            call.tool_name = relay.tool_name
    observed_sessions = {
        call.session_id for call in call_lookup.values() if not call.is_relay
    }
    tool_calls = sorted(
        (
            call
            for call in call_lookup.values()
            if not call.is_relay or call.session_id not in observed_sessions
        ),
        key=lambda call: call.first_order,
    )
    for call in tool_calls:
        call.turn = index.for_tool_call(call)
        if call.turn is not None:
            call.turn.tool_calls.append(call)

    # Permissions.
    permission_requests = [
        row for row in rows if row.event_type == PERMISSION_REQUESTED_EVENT
    ]
    decisions = [row for row in rows if row.event_type == PERMISSION_DECIDED_EVENT]
    first_request: dict[tuple, EventRow] = {}
    permission_tool_calls: set[tuple] = set()
    for request in permission_requests:
        if request.permission_id is not None:
            first_request.setdefault(
                (request.session_id, request.emitter_id, request.permission_id), request
            )
        if request.tool_call_id:
            permission_tool_calls.add(
                (request.session_id, request.emitter_id, request.tool_call_id)
            )
        turn = index.for_event(request)
        if turn is not None:
            turn.permission_requests += 1
    for call in tool_calls:
        if not call.is_relay:
            call.requested_permission = (
                call.session_id,
                call.emitter_id,
                call.tool_call_id,
            ) in permission_tool_calls
    decision_counts: Counter = Counter()
    permission_waits: list[float] = []
    decided: set[tuple] = set()
    for decision in decisions:
        decision_counts[(decision.decision or UNKNOWN)] += 1
        if decision.permission_id is None:
            continue
        key = (decision.session_id, decision.emitter_id, decision.permission_id)
        request = first_request.get(key)
        if request is None or key in decided:
            continue
        if decision.occurred_at >= request.occurred_at:
            decided.add(key)
            permission_waits.append(
                (decision.occurred_at - request.occurred_at).total_seconds()
            )

    # User interrupts (``session/cancel``) and plan updates inside turns.
    cancels = [row for row in rows if is_cancel(row)]
    for cancel in cancels:
        turn = index.for_event(cancel)
        if turn is not None:
            turn.cancel_events += 1
    for plan in (row for row in rows if row.event_type == PLAN_EVENT):
        turn = index.for_event(plan)
        if turn is not None:
            turn.last_plan = plan  # rows are in event order: the last one wins

    errors = [row for row in rows if row.event_type in ERROR_EVENTS]
    agent_saves = [
        row
        for row in rows
        if row.event_type == FILE_SAVED_EVENT and row.source == ACP_SOURCE
    ]
    agent_file_writes = count_agent_writes(tool_calls, agent_saves, index)

    # Token usage: ACP prompt-response usage owns a research session when any is
    # reported there; otherwise exact relay model-call usage is used (and
    # attributed to the latest turn of the session started before the call).
    acp_usage_sessions = {
        completion.session_id
        for completion in completions
        if completion.usage_tokens is not None
    }
    usage_total: Optional[int] = None
    for completion in completions:
        if completion.usage_tokens is not None:
            usage_total = (usage_total or 0) + completion.usage_tokens
    for call in model_calls:
        if call.usage_tokens is None or call.session_id in acp_usage_sessions:
            continue
        usage_total = (usage_total or 0) + call.usage_tokens
        turn = index.latest_in_session(call.session_id, call.occurred_at)
        if turn is not None:
            turn.relay_usage = (turn.relay_usage or 0) + call.usage_tokens
    for turn in turns:
        turn.usage_tokens = (
            turn.completion_usage
            if turn.completion_usage is not None
            else turn.relay_usage
        )

    # Sessions (clipped to the window).
    session_rows = list(sessions)
    seconds_by_session: dict[str, float] = {}
    seconds_by_day: dict[date, float] = defaultdict(float)
    for session in session_rows:
        seconds_by_session[session.session_id] = session_seconds(session, window)
        interval = session_interval(session, window)
        if interval is not None:
            for day, seconds in split_seconds_by_day(*interval).items():
                seconds_by_day[day] += seconds

    # Presence, first/last event and IDE edits.
    events_by_day: dict[date, int] = defaultdict(int)
    ide_by_day: dict[date, int] = defaultdict(int)
    first_event_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    if daily is not None:
        for bucket in daily:
            if not _day_in_window(bucket.day, window):
                continue
            events_by_day[bucket.day] += bucket.events
            if bucket.ide_edits:
                ide_by_day[bucket.day] += bucket.ide_edits
            if bucket.first_at is not None and (
                first_event_at is None or bucket.first_at < first_event_at
            ):
                first_event_at = bucket.first_at
            if bucket.last_at is not None and (
                last_event_at is None or bucket.last_at > last_event_at
            ):
                last_event_at = bucket.last_at
    else:
        for row in rows:
            events_by_day[_utc_day(row.occurred_at)] += 1
            if row.event_type == DOCUMENT_CHANGED_EVENT:
                ide_by_day[_utc_day(row.occurred_at)] += 1
        if rows:
            first_event_at = rows[0].occurred_at
            last_event_at = max(row.occurred_at for row in rows)

    # Implementation onset: first agent edit after the session's first prompt.
    first_prompt: dict[str, datetime] = {}
    for prompt in prompts:
        if prompt.session_id is not None:
            first_prompt.setdefault(prompt.session_id, prompt.occurred_at)
    edits_by_session: dict[Optional[str], list[datetime]] = defaultdict(list)
    for call in tool_calls:
        if call.is_agent_write and call.completed_at is not None:
            edits_by_session[call.session_id].append(call.completed_at)
    for save in agent_saves:
        edits_by_session[save.session_id].append(save.occurred_at)
    first_edit_seconds: list[float] = []
    for session_id, started in first_prompt.items():
        later = [moment for moment in edits_by_session.get(session_id, ()) if moment >= started]
        if later:
            first_edit_seconds.append((min(later) - started).total_seconds())

    return ParticipantAnalysis(
        prompts=prompts,
        turns=turns,
        completions=completions,
        model_calls=model_calls,
        tool_calls=tool_calls,
        call_lookup=call_lookup,
        permission_requests=permission_requests,
        decision_counts=decision_counts,
        permission_waits=permission_waits,
        cancels=cancels,
        errors=errors,
        agent_saves=agent_saves,
        agent_file_writes=agent_file_writes,
        usage_tokens=usage_total,
        session_seconds=sum(seconds_by_session.values()),
        session_seconds_by_session=seconds_by_session,
        session_seconds_by_day=dict(seconds_by_day),
        ide_edits=sum(ide_by_day.values()),
        ide_edits_by_day=dict(ide_by_day),
        events_by_day=dict(events_by_day),
        event_count=sum(events_by_day.values()),
        first_event_at=first_event_at,
        last_event_at=last_event_at,
        first_edit_seconds=first_edit_seconds,
    )


# -- shared JSON blocks -----------------------------------------------------------


def metric_summary(values: Iterable[Optional[float]]) -> dict[str, Any]:
    """``n``/mean/median/p25/p75 plus the sorted null-free values (strip plot)."""
    data = sorted(float(value) for value in values if value is not None)
    return {
        "n": len(data),
        "mean": _r3(_mean(data)),
        "median": _r3(percentile(data, 0.5)),
        "p25": _r3(percentile(data, 0.25)),
        "p75": _r3(percentile(data, 0.75)),
        "values": [_r3(value) for value in data],
    }


def _counter_rows(counter: Mapping[str, int], label: str) -> list[dict[str, Any]]:
    return [
        {label: key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if count
    ]


def tool_kind_rows(calls: Iterable[ToolCall]) -> list[dict[str, Any]]:
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for call in calls:
        stats[call.kind][0] += 1
        if call.failed:
            stats[call.kind][1] += 1
    return [
        {"tool_kind": kind, "calls": values[0], "failures": values[1]}
        for kind, values in sorted(stats.items(), key=lambda item: (-item[1][0], item[0]))
    ]


def display_tool_name(name: Any) -> Optional[str]:
    """``name`` when it is a tool identifier, else ``None``.

    ACP reports a tool call's human-readable *title* as ``tool_name``, and a
    title can name files, quote a search pattern or a whole shell command
    (``Read src/app.py``, ``Search "token"``, ``Run git status``). Dashboards
    never show a title: the call is named by the relay's identifier for the
    same call when there is one (see ``analyze_participant``), and otherwise
    counted without a name, under its kind.
    """
    if not isinstance(name, str):
        return None
    candidate = name.strip()
    return candidate if _TOOL_IDENTIFIER.fullmatch(candidate) else None


def tool_rows(
    calls: Iterable[tuple[ToolCall, Optional[str]]],
    *,
    limit: int,
    arm_ids: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Top tools by calls; with ``arm_ids`` each row carries per-arm calls.

    Calls whose name cannot be shown (see ``display_tool_name``) are grouped
    by kind under a ``None`` name.
    """
    grouped: dict[tuple[Optional[str], Optional[str]], list[tuple[ToolCall, Optional[str]]]] = defaultdict(list)
    for call, arm_id in calls:
        name = display_tool_name(call.tool_name)
        grouped[(name, None if name is not None else call.kind)].append((call, arm_id))
    rows: list[dict[str, Any]] = []
    for (name, _kind), members in grouped.items():
        kinds = Counter(call.kind for call, _ in members)
        kind = sorted(kinds.items(), key=lambda item: (-item[1], item[0]))[0][0]
        durations = [
            call.duration_ms for call, _ in members if call.duration_ms is not None
        ]
        row: dict[str, Any] = {
            "tool_name": name,
            "tool_kind": kind,
            "calls": len(members),
            "failures": sum(1 for call, _ in members if call.failed),
            "median_duration_ms": _r1(median(durations)),
        }
        if arm_ids is not None:
            by_arm = {arm_id: 0 for arm_id in arm_ids}
            for _, arm_id in members:
                if arm_id is not None and arm_id in by_arm:
                    by_arm[arm_id] += 1
            row["by_arm"] = by_arm
        rows.append(row)
    rows.sort(
        key=lambda row: (-row["calls"], row["tool_name"] is None, row["tool_name"] or "", row["tool_kind"] or "")
    )
    return rows[:limit]


def effective_context_cap(max_context_tokens: Optional[int]) -> int:
    """The frozen ``max_context_tokens``, else the managed-runtime fallback."""
    if (
        isinstance(max_context_tokens, int)
        and not isinstance(max_context_tokens, bool)
        and max_context_tokens >= 1
    ):
        return max_context_tokens
    return FALLBACK_MAX_CONTEXT_TOKENS


def context_block(model_calls: Sequence[EventRow], cap_tokens: int) -> dict[str, Any]:
    """Context-cap compliance over relay model calls' provider prompt tokens.

    A call is over the cap when its ``metrics.counts.prompt_tokens`` exceeds
    ``cap_tokens``. Arms whose runtime never uses the relay (external BYOA
    runtimes) have no model calls, so their coverage is ``UNAVAILABLE``.
    """
    prompt_tokens = [
        call.prompt_tokens for call in model_calls if call.prompt_tokens is not None
    ]
    over_cap = sum(1 for value in prompt_tokens if value > cap_tokens)
    return {
        "cap_tokens": cap_tokens,
        "model_calls": len(model_calls),
        "calls_with_prompt_tokens": len(prompt_tokens),
        "prompt_tokens_p50": _r3(percentile(prompt_tokens, 0.5)),
        "prompt_tokens_p95": _r3(percentile(prompt_tokens, 0.95)),
        "prompt_tokens_max": max(prompt_tokens) if prompt_tokens else None,
        "over_cap_calls": over_cap,
        "over_cap_share": _r3(_ratio(over_cap, len(prompt_tokens))),
        "coverage": _coverage_state(len(prompt_tokens), len(model_calls)),
    }


def _arm_labels(arm: ArmRow) -> dict[str, Any]:
    return {
        "profile_id": arm.profile_id,
        "name": arm.name,
        "model": arm.model,
        "framework_version": arm.framework_version,
    }


def arm_catalogue(frame: StudyFrame) -> list[ArmRow]:
    """The study's frozen arms by selection order, plus any assigned extra."""
    arms = sorted(
        frame.arms,
        key=lambda arm: (
            arm.selection_order is None,
            arm.selection_order or 0,
            arm.profile_id,
        ),
    )
    known = {arm.profile_id for arm in arms}
    for assignment in sorted(frame.assignments, key=lambda item: item.profile_id):
        if assignment.profile_id in known:
            continue
        known.add(assignment.profile_id)
        arms.append(
            ArmRow(
                profile_id=assignment.profile_id,
                name=assignment.name,
                model=assignment.model,
                framework_version=assignment.framework_version,
                selection_order=None,
                max_context_tokens=assignment.max_context_tokens,
            )
        )
    return arms


def _participant_arm(
    assignment: Optional[AssignmentRow], arms_by_id: Mapping[str, ArmRow]
) -> Optional[dict[str, Any]]:
    if assignment is None:
        return None
    arm = arms_by_id.get(assignment.profile_id)

    def label(own: Optional[str], attribute: str) -> Optional[str]:
        if own is not None:
            return own
        return getattr(arm, attribute) if arm is not None else None

    return {
        "profile_id": assignment.profile_id,
        "name": label(assignment.name, "name"),
        "model": label(assignment.model, "model"),
        "framework_version": label(assignment.framework_version, "framework_version"),
        "assignment_status": assignment.status,
        "assigned_at": iso(assignment.assigned_at),
    }


def health(
    status: str,
    analysis: ParticipantAnalysis,
    now: datetime,
) -> str:
    if status != ACTIVE_ENROLLMENT_STATUS:
        return "INACTIVE"
    if not analysis.has_telemetry:
        return "NO_TELEMETRY"
    last = as_utc(analysis.last_event_at)
    if last is not None and as_utc(now) - last <= ACTIVE_WINDOW:  # type: ignore[operator]
        return "ACTIVE"
    return "IDLE"


def _latest(values: Iterable[Optional[datetime]]) -> Optional[datetime]:
    present = [as_utc(value) for value in values if value is not None]
    return max(present) if present else None  # type: ignore[type-var]


def participant_header(
    enrollment: EnrollmentRow,
    assignment: Optional[AssignmentRow],
    arms_by_id: Mapping[str, ArmRow],
    sessions: Sequence[SessionRow],
    analysis: ParticipantAnalysis,
    now: datetime,
) -> dict[str, Any]:
    """One participants-table row (study-local identity only)."""
    return {
        "enrollment_id": enrollment.enrollment_id,
        "participant_code": enrollment.participant_code,
        "status": enrollment.status,
        "enrolled_at": iso(enrollment.enrolled_at),
        "consent_accepted_at": iso(enrollment.consent_accepted_at),
        "arm": _participant_arm(assignment, arms_by_id),
        "sessions": {
            "total": len(sessions),
            "active": sum(
                1 for session in sessions if session.state not in TERMINAL_SESSION_STATES
            ),
            "session_seconds": _r1(analysis.session_seconds),
            "last_activity_at": iso(_latest(session.last_activity_at for session in sessions)),
            "last_heartbeat_at": iso(
                _latest(session.last_heartbeat_at for session in sessions)
            ),
        },
        "activity": {
            "prompts": len(analysis.prompts),
            "tool_calls": len(analysis.tool_calls),
            "tool_failures": analysis.tool_failures,
            "cancellations": len(analysis.cancels),
            "permission_requests": len(analysis.permission_requests),
            "permission_denials": analysis.decision_counts.get("reject", 0),
            "errors": len(analysis.errors),
            "agent_file_writes": analysis.agent_file_writes,
            "ide_edits": analysis.ide_edits,
            "usage_tokens": analysis.usage_tokens,
            "active_days": analysis.active_days,
            "first_event_at": iso(analysis.first_event_at),
            "last_event_at": iso(analysis.last_event_at),
        },
        "health": health(enrollment.status, analysis, now),
    }


def _group(rows: Iterable[Any], attribute: str) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[getattr(row, attribute)].append(row)
    return grouped


def _enrollment_sort_key(enrollment: EnrollmentRow) -> tuple:
    return (as_utc(enrollment.enrolled_at) or _UTC_MAX, enrollment.participant_code)


# -- endpoint A: participants table --------------------------------------------------


def build_participants(
    frame: StudyFrame,
    events: Iterable[EventRow],
    daily: Iterable[DailyEventCount],
    *,
    study_id: str,
    now: datetime,
) -> dict[str, Any]:
    events_by_enrollment = _group(events, "enrollment_id")
    daily_by_enrollment = _group(daily, "enrollment_id")
    sessions_by_enrollment = _group(frame.sessions, "enrollment_id")
    assignment_by_enrollment = {
        assignment.enrollment_id: assignment for assignment in frame.assignments
    }
    arms = arm_catalogue(frame)
    arms_by_id = {arm.profile_id: arm for arm in arms}
    arm_members = Counter(assignment.profile_id for assignment in frame.assignments)

    participants = []
    for enrollment in sorted(frame.enrollments, key=_enrollment_sort_key):
        sessions = sessions_by_enrollment.get(enrollment.enrollment_id, [])
        analysis = analyze_participant(
            events_by_enrollment.get(enrollment.enrollment_id, ()),
            sessions,
            daily_by_enrollment.get(enrollment.enrollment_id) or None,
        )
        participants.append(
            participant_header(
                enrollment,
                assignment_by_enrollment.get(enrollment.enrollment_id),
                arms_by_id,
                sessions,
                analysis,
                now,
            )
        )
    return {
        "study_id": study_id,
        "generated_at": iso(now),
        "arms": [
            {
                **_arm_labels(arm),
                "selection_order": arm.selection_order,
                "participants": arm_members.get(arm.profile_id, 0),
            }
            for arm in arms
        ],
        "participants": participants,
    }


# -- endpoint B: one participant's dashboard -------------------------------------------


def _timeline_rows(
    timeline: Iterable[EventRow], analysis: ParticipantAnalysis
) -> list[dict[str, Any]]:
    relay_calls = {
        (call.session_id, call.tool_call_id): call
        for call in analysis.call_lookup.values()
        if call.is_relay and call.tool_call_id
    }
    items = [
        row
        for row in timeline
        if not is_message_chunk(row) and row.event_type not in TIMELINE_EXCLUDED_EVENT_TYPES
    ]
    items.sort(key=order_key, reverse=True)
    output = []
    for row in items[:MAX_TIMELINE]:
        tool_name, tool_kind = display_tool_name(row.tool_name), row.tool_kind
        if row.event_type in TOOL_EVENTS or row.event_type.startswith("permission."):
            call = None
            if row.tool_call_id:
                call = (
                    relay_calls.get((row.session_id, row.tool_call_id))
                    if row.source == RELAY_SOURCE
                    else analysis.call_lookup.get(
                        (False, row.session_id, row.emitter_id, row.tool_call_id)
                    )
                )
            if call is not None:
                # The call's name may come from the relay (a row's own title is
                # never shown).
                tool_name = tool_name or display_tool_name(call.tool_name)
                tool_kind = tool_kind or call.tool_kind
        output.append(
            {
                "occurred_at": iso(row.occurred_at),
                "event_type": row.event_type,
                "source": row.source,
                "session_id": row.session_id,
                "turn_id": row.turn_id,
                "tool_name": tool_name,
                "tool_kind": tool_kind,
                "status": row.status or row.lifecycle_state,
                "decision": row.decision,
                "stop_reason": row.stop_reason,
                "error_code": row.error_code,
            }
        )
    return output


def build_participant_detail(
    frame: StudyFrame,
    enrollment_id: str,
    events: Iterable[EventRow],
    daily: Iterable[DailyEventCount],
    timeline: Iterable[EventRow],
    *,
    study_id: str,
    now: datetime,
) -> Optional[dict[str, Any]]:
    """The per-participant dashboard, or ``None`` for an unknown enrollment."""
    enrollment = next(
        (row for row in frame.enrollments if row.enrollment_id == enrollment_id), None
    )
    if enrollment is None:
        return None
    sessions = [row for row in frame.sessions if row.enrollment_id == enrollment_id]
    assignment = next(
        (row for row in frame.assignments if row.enrollment_id == enrollment_id), None
    )
    arms_by_id = {arm.profile_id: arm for arm in arm_catalogue(frame)}
    analysis = analyze_participant(
        [row for row in events if row.enrollment_id in (None, enrollment_id)],
        sessions,
        [row for row in daily if row.enrollment_id == enrollment_id] or None,
    )

    prompts_by_day = Counter(_utc_day(prompt.occurred_at) for prompt in analysis.prompts)
    tools_by_day = Counter(_utc_day(call.first_at) for call in analysis.tool_calls)
    errors_by_day = Counter(_utc_day(error.occurred_at) for error in analysis.errors)
    days = (
        {day for day, count in analysis.events_by_day.items() if count}
        | {day for day, seconds in analysis.session_seconds_by_day.items() if seconds > 0}
        | set(prompts_by_day)
        | set(tools_by_day)
        | set(errors_by_day)
        | {day for day, count in analysis.ide_edits_by_day.items() if count}
    )
    daily_rows = [
        {
            "date": day.isoformat(),
            "prompts": prompts_by_day.get(day, 0),
            "tool_calls": tools_by_day.get(day, 0),
            "errors": errors_by_day.get(day, 0),
            "ide_edits": analysis.ide_edits_by_day.get(day, 0),
            "session_seconds": _r1(analysis.session_seconds_by_day.get(day, 0.0)),
        }
        for day in sorted(days)
    ]

    prompts_by_session = Counter(prompt.session_id for prompt in analysis.prompts)
    tools_by_session = Counter(call.session_id for call in analysis.tool_calls)
    errors_by_session = Counter(error.session_id for error in analysis.errors)
    ordered_sessions = sorted(
        sessions,
        key=lambda row: as_utc(row.opened_at or row.created_at) or _UTC_MIN,
        reverse=True,
    )
    sessions_list = [
        {
            "session_id": row.session_id,
            "state": row.state,
            "opened_at": iso(row.opened_at),
            "closed_at": iso(row.closed_at),
            "last_activity_at": iso(row.last_activity_at),
            "close_reason": row.close_reason,
            "session_seconds": _r1(analysis.session_seconds_by_session.get(row.session_id, 0.0)),
            "prompts": prompts_by_session.get(row.session_id, 0),
            "tool_calls": tools_by_session.get(row.session_id, 0),
            "errors": errors_by_session.get(row.session_id, 0),
        }
        for row in ordered_sessions[:MAX_SESSIONS_LIST]
    ]

    turns = [
        {
            "turn_id": turn.turn_id,
            "session_id": turn.session_id,
            "started_at": iso(turn.started_at),
            "completed_at": iso(turn.completed_at),
            "duration_seconds": _r1(turn.duration_seconds),
            "tool_calls": len(turn.tool_calls),
            "tool_failures": turn.tool_failures,
            "permission_requests": turn.permission_requests,
            "stop_reason": turn.stop_reason,
            "usage_tokens": turn.usage_tokens,
            "cancelled": turn.cancelled,
        }
        for turn in sorted(analysis.turns, key=lambda turn: turn.start_order, reverse=True)[
            :MAX_TURNS
        ]
    ]

    cap_source: Optional[int] = None
    if assignment is not None:
        cap_source = assignment.max_context_tokens
        if cap_source is None and assignment.profile_id in arms_by_id:
            cap_source = arms_by_id[assignment.profile_id].max_context_tokens

    return {
        "study_id": study_id,
        "generated_at": iso(now),
        **participant_header(enrollment, assignment, arms_by_id, sessions, analysis, now),
        "metrics": analysis.metrics(),
        "context": context_block(analysis.model_calls, effective_context_cap(cap_source)),
        "daily": daily_rows,
        "tool_kinds": tool_kind_rows(analysis.tool_calls),
        "tools": tool_rows(
            ((call, None) for call in analysis.tool_calls), limit=TOP_TOOLS_PARTICIPANT
        ),
        "stop_reasons": _counter_rows(analysis.stop_reason_counts, "stop_reason"),
        "permission_decisions": _counter_rows(analysis.decision_counts, "decision"),
        "sessions_list": sessions_list,
        "turns": turns,
        "timeline": _timeline_rows(
            [row for row in timeline if row.enrollment_id in (None, enrollment_id)],
            analysis,
        ),
    }


# -- endpoint C: study summary / arm comparison -------------------------------------------


def _coverage_state(covered: int, total: int) -> str:
    if total <= 0 or covered <= 0:
        return "UNAVAILABLE"
    if covered >= total:
        return "AVAILABLE"
    return "PARTIAL"


def build_study_summary(
    frame: StudyFrame,
    events: Iterable[EventRow],
    daily: Iterable[DailyEventCount],
    *,
    study_id: str,
    now: datetime,
    window: Optional[DateWindow] = None,
) -> dict[str, Any]:
    window = window or DateWindow()
    events_by_enrollment = _group(events, "enrollment_id")
    daily_by_enrollment = _group(daily, "enrollment_id")
    sessions_by_enrollment = _group(frame.sessions, "enrollment_id")
    assignment_by_enrollment = {
        assignment.enrollment_id: assignment for assignment in frame.assignments
    }
    arms = arm_catalogue(frame)
    arm_ids = [arm.profile_id for arm in arms]

    analyses: dict[str, ParticipantAnalysis] = {}
    for enrollment in sorted(frame.enrollments, key=_enrollment_sort_key):
        analyses[enrollment.enrollment_id] = analyze_participant(
            events_by_enrollment.get(enrollment.enrollment_id, ()),
            sessions_by_enrollment.get(enrollment.enrollment_id, ()),
            daily_by_enrollment.get(enrollment.enrollment_id) or None,
            window=window,
        )
    arm_of = {
        enrollment_id: assignment.profile_id
        for enrollment_id, assignment in assignment_by_enrollment.items()
        if enrollment_id in analyses
    }
    metrics_of = {enrollment_id: analysis.metrics() for enrollment_id, analysis in analyses.items()}
    windowed_sessions = [
        session for session in frame.sessions if session_opened_in_window(session, window)
    ]

    # Totals over every enrollment of the study.
    all_turns = [turn for analysis in analyses.values() for turn in analysis.turns]
    completed_turns = [turn for turn in all_turns if turn.completed]
    turns_with_usage = [turn for turn in completed_turns if turn.usage_tokens is not None]
    decisions: Counter = Counter()
    stop_reasons: Counter = Counter()
    usage_values = [
        analysis.usage_tokens
        for analysis in analyses.values()
        if analysis.usage_tokens is not None
    ]
    for analysis in analyses.values():
        decisions.update(analysis.decision_counts)
        stop_reasons.update(analysis.stop_reason_counts)
    usage_total = sum(usage_values) if usage_values else None
    usage_coverage = _ratio(len(turns_with_usage), len(completed_turns))
    all_calls = [
        (call, arm_of.get(enrollment_id))
        for enrollment_id, analysis in analyses.items()
        for call in analysis.tool_calls
    ]
    totals = {
        "participants_enrolled": len(frame.enrollments),
        "participants_active": sum(
            1
            for enrollment in frame.enrollments
            if enrollment.status == ACTIVE_ENROLLMENT_STATUS
        ),
        "participants_with_telemetry": sum(
            1 for analysis in analyses.values() if analysis.has_telemetry
        ),
        "sessions": len(windowed_sessions),
        "session_seconds": _r1(sum(analysis.session_seconds for analysis in analyses.values())),
        "prompts": sum(len(analysis.prompts) for analysis in analyses.values()),
        "tool_calls": len(all_calls),
        "tool_failures": sum(1 for call, _ in all_calls if call.failed),
        "cancellations": sum(len(analysis.cancels) for analysis in analyses.values()),
        "permission_requests": sum(
            len(analysis.permission_requests) for analysis in analyses.values()
        ),
        "permission_allowed": decisions.get("allow", 0),
        "permission_rejected": decisions.get("reject", 0),
        "permission_cancelled": decisions.get("cancelled", 0),
        "errors": sum(len(analysis.errors) for analysis in analyses.values()),
        "agent_file_writes": sum(
            analysis.agent_file_writes for analysis in analyses.values()
        ),
        "ide_edits": sum(analysis.ide_edits for analysis in analyses.values()),
        "usage_tokens": usage_total,
        "usage_coverage": _r3(usage_coverage),
    }

    # Arms: participant-level aggregation (the randomisation unit).
    arms_out = []
    for arm in arms:
        members = sorted(
            enrollment_id
            for enrollment_id, profile_id in arm_of.items()
            if profile_id == arm.profile_id
        )
        with_telemetry = [
            enrollment_id for enrollment_id in members if analyses[enrollment_id].has_telemetry
        ]
        metric_block = {}
        for key in METRIC_KEYS:
            population = members if key in COUNT_METRIC_KEYS else with_telemetry
            metric_block[key] = metric_summary(
                metrics_of[enrollment_id][key] for enrollment_id in population
            )
        arm_decisions: Counter = Counter()
        arm_stops: Counter = Counter()
        arm_turns: list[Turn] = []
        arm_calls: list[ToolCall] = []
        arm_model_calls: list[EventRow] = []
        for enrollment_id in members:
            analysis = analyses[enrollment_id]
            arm_decisions.update(analysis.decision_counts)
            arm_stops.update(analysis.stop_reason_counts)
            arm_turns.extend(analysis.turns)
            arm_calls.extend(analysis.tool_calls)
            arm_model_calls.extend(analysis.model_calls)
        turn_seconds = [
            turn.duration_seconds for turn in arm_turns if turn.duration_seconds is not None
        ]
        arms_out.append(
            {
                **_arm_labels(arm),
                "selection_order": arm.selection_order,
                "participants": len(members),
                "participants_with_telemetry": len(with_telemetry),
                "metrics": metric_block,
                "stop_reasons": _counter_rows(arm_stops, "stop_reason"),
                "permission_decisions": _counter_rows(arm_decisions, "decision"),
                "tool_kinds": tool_kind_rows(arm_calls),
                "turn_seconds": {
                    "p50": _r1(percentile(turn_seconds, 0.5)),
                    "p90": _r1(percentile(turn_seconds, 0.9)),
                    "n": len(turn_seconds),
                },
                "transitions": transitions(arm_turns),
                "context": context_block(
                    arm_model_calls, effective_context_cap(arm.max_context_tokens)
                ),
            }
        )

    # Daily activity (UTC dates with any activity).
    active_by_day: dict[date, set[str]] = defaultdict(set)
    prompts_by_day: Counter = Counter()
    tools_by_day: Counter = Counter()
    errors_by_day: Counter = Counter()
    sessions_by_day: Counter = Counter()
    arm_prompts: Counter = Counter()
    arm_active: dict[tuple[date, str], set[str]] = defaultdict(set)
    for enrollment_id, analysis in analyses.items():
        arm_id = arm_of.get(enrollment_id)
        for day, count in analysis.events_by_day.items():
            if count:
                active_by_day[day].add(enrollment_id)
                if arm_id is not None:
                    arm_active[(day, arm_id)].add(enrollment_id)
        for prompt in analysis.prompts:
            day = _utc_day(prompt.occurred_at)
            prompts_by_day[day] += 1
            if arm_id is not None:
                arm_prompts[(day, arm_id)] += 1
        for call in analysis.tool_calls:
            tools_by_day[_utc_day(call.first_at)] += 1
        for error in analysis.errors:
            errors_by_day[_utc_day(error.occurred_at)] += 1
    for session in windowed_sessions:
        if session.opened_at is not None:
            sessions_by_day[_utc_day(session.opened_at)] += 1
    days = (
        set(active_by_day)
        | set(prompts_by_day)
        | set(tools_by_day)
        | set(errors_by_day)
        | set(sessions_by_day)
    )
    daily_rows = [
        {
            "date": day.isoformat(),
            "active_participants": len(active_by_day.get(day, ())),
            "prompts": prompts_by_day.get(day, 0),
            "tool_calls": tools_by_day.get(day, 0),
            "errors": errors_by_day.get(day, 0),
            "sessions": sessions_by_day.get(day, 0),
            "by_arm": {
                arm_id: {
                    "prompts": arm_prompts.get((day, arm_id), 0),
                    "active_participants": len(arm_active.get((day, arm_id), ())),
                }
                for arm_id in arm_ids
            },
        }
        for day in sorted(days)
    ]

    # Coverage of the fields the arm comparison depends on.
    prompts_total = [prompt for analysis in analyses.values() for prompt in analysis.prompts]
    if usage_coverage is not None:
        usage_state = _coverage_state(len(turns_with_usage), len(completed_turns))
        if usage_state == "UNAVAILABLE" and usage_total is not None:
            usage_state = "PARTIAL"
    else:
        usage_state = "PARTIAL" if usage_total is not None else "UNAVAILABLE"
    coverage = {
        "usage_tokens": usage_state,
        "turn_correlation": _coverage_state(
            sum(1 for prompt in prompts_total if prompt.turn_id is not None),
            len(prompts_total),
        ),
        "tool_kind": _coverage_state(
            sum(1 for call, _ in all_calls if call.tool_kind is not None), len(all_calls)
        ),
    }

    return {
        "study_id": study_id,
        "generated_at": iso(now),
        "window": window.as_json(),
        "totals": totals,
        "arms": arms_out,
        "daily": daily_rows,
        "tool_kinds": tool_kind_rows(call for call, _ in all_calls),
        "tools": tool_rows(all_calls, limit=TOP_TOOLS_STUDY, arm_ids=arm_ids),
        "stop_reasons": _counter_rows(stop_reasons, "stop_reason"),
        "permission_decisions": _counter_rows(decisions, "decision"),
        "coverage": coverage,
    }
