"""Pure unit tests for the study analytics metrics (issue 03).

Synthetic metadata-only rows exercise turn matching (turn ids, per-emitter id
reuse, fallback without turn ids), prompt/chunk/model-call separation, tool-call
assembly and relay ownership, permissions (auto-run share, denial rate, wait),
cancellations, transitions + lift, percentiles, session time, token usage
ownership, plan completion, implementation onset, null handling, the arm
comparison and the context-cap block. No database is involved.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest

from research.analysis.study_analytics import metrics as m
from research.analysis.study_analytics.models import (
    ArmRow,
    AssignmentRow,
    DailyEventCount,
    DateWindow,
    EnrollmentRow,
    EventRow,
    SessionRow,
    StudyFrame,
)

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
_SEQUENCE = itertools.count(1)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ev(
    event_type: str,
    seconds: float,
    *,
    source: str = "acp",
    session: str | None = "s1",
    emitter: str = "proxy-1",
    enrollment: str = "e1",
    **fields,
) -> EventRow:
    return EventRow(
        event_type=event_type,
        source=source,
        occurred_at=at(seconds),
        enrollment_id=enrollment,
        session_id=session,
        emitter_id=emitter,
        emitter_sequence=next(_SEQUENCE),
        **fields,
    )


def prompt(seconds, turn_id=None, **kwargs) -> EventRow:
    return ev("agent.message.started", seconds, turn_id=turn_id, **kwargs)


def completion(seconds, turn_id=None, stop_reason="end_turn", usage=None, **kwargs) -> EventRow:
    return ev(
        "agent.message.completed",
        seconds,
        turn_id=turn_id,
        stop_reason=stop_reason,
        usage_tokens=usage,
        lifecycle_state="completed",
        **kwargs,
    )


def tool(event_type, seconds, call_id, *, kind=None, name=None, status=None, turn_id=None, **kwargs):
    return ev(
        event_type,
        seconds,
        tool_call_id=call_id,
        tool_kind=kind,
        tool_name=name,
        status=status,
        turn_id=turn_id,
        **kwargs,
    )


def model_call(seconds, usage=None, prompt_tokens=None, **kwargs) -> EventRow:
    kwargs.setdefault("emitter", "self-report")
    return ev(
        "agent.message.completed",
        seconds,
        source="relay",
        usage_tokens=usage,
        prompt_tokens=prompt_tokens,
        **kwargs,
    )


def session_row(session_id="s1", *, opened=0, closed=None, activity=None, heartbeat=None, state="ended", enrollment="e1"):
    return SessionRow(
        session_id=session_id,
        enrollment_id=enrollment,
        state=state,
        opened_at=at(opened) if opened is not None else None,
        closed_at=at(closed) if closed is not None else None,
        last_activity_at=at(activity) if activity is not None else None,
        last_heartbeat_at=at(heartbeat) if heartbeat is not None else None,
    )


# -- statistics -------------------------------------------------------------------


def test_percentiles_use_linear_interpolation_and_empty_is_none():
    assert m.percentile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)
    assert m.percentile([4, 1, 3, 2], 0.5) == pytest.approx(2.5)
    assert m.percentile([1, 2, 3, 4], 0.75) == pytest.approx(3.25)
    assert m.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.9) == pytest.approx(9.1)
    assert m.percentile([5], 0.9) == 5.0
    assert m.percentile([], 0.5) is None
    assert m.median([3, 1, 2]) == 2.0

    summary = m.metric_summary([None, 4, 1, 3, 2])
    assert summary == {
        "n": 4,
        "mean": 2.5,
        "median": 2.5,
        "p25": 1.75,
        "p75": 3.25,
        "values": [1.0, 2.0, 3.0, 4.0],
    }
    assert m.metric_summary([None]) == {
        "n": 0,
        "mean": None,
        "median": None,
        "p25": None,
        "p75": None,
        "values": [],
    }
    assert m.metric_summary([1 / 3])["values"] == [0.333]


# -- turns --------------------------------------------------------------------------


def test_turns_match_by_turn_id_per_emitter_then_fall_back_without_ids():
    rows = [
        prompt(0, "1"),
        completion(30, "1", usage=1000),
        prompt(100, "2"),
        completion(160, "2", stop_reason="cancelled"),
        # No turn ids at all: paired with the next completion in the session.
        prompt(200),
        completion(230, usage=500),
        # Open turn: never completed.
        prompt(300, "4"),
        # A second proxy process in the same session reuses JSON-RPC id "1".
        prompt(400, "1", emitter="proxy-2"),
        completion(410, "1", emitter="proxy-2", stop_reason="max_tokens"),
        # A relay model call is never a turn end.
        model_call(20, usage=99),
    ]
    analysis = m.analyze_participant(rows, [])

    assert len(analysis.prompts) == 5
    turns = sorted(analysis.turns, key=lambda turn: turn.started_at)
    assert [turn.duration_seconds for turn in turns] == [30.0, 60.0, 30.0, None, 10.0]
    assert [turn.stop_reason for turn in turns] == [
        "end_turn",
        "cancelled",
        "end_turn",
        None,
        "max_tokens",
    ]
    assert [turn.matched_by_turn_id for turn in turns] == [True, True, False, False, True]
    assert turns[4].emitter_id == "proxy-2"
    assert analysis.stop_reason_counts == {"end_turn": 2, "cancelled": 1, "max_tokens": 1}
    metrics = analysis.metrics()
    assert metrics["median_turn_seconds"] == 30.0
    assert metrics["cancel_rate"] == 0.2


def test_fallback_never_pairs_two_different_known_turn_ids_or_crosses_next_prompt():
    rows = [
        prompt(0, "7"),
        completion(5, "8"),  # a different known turn: never paired with "7"
        prompt(10),
        prompt(20),
        completion(25),  # after the next prompt: belongs to the prompt at 20
    ]
    turns = sorted(m.analyze_participant(rows, []).turns, key=lambda turn: turn.started_at)
    assert [turn.completed_at for turn in turns] == [None, None, at(25)]


def test_message_chunks_and_relay_model_calls_are_never_prompts_or_turns():
    rows = [
        prompt(0, "1"),
        ev("agent.message.started", 1, turn_id="1", message_kind="assistant", lifecycle_state="started"),
        ev("agent.message.started", 2, turn_id="1", message_kind="thought", lifecycle_state="started"),
        # Privacy-stripped chunk: no message_kind, but a "started" lifecycle.
        ev("agent.message.started", 3, turn_id="1", lifecycle_state="started"),
        model_call(4, usage=10, prompt_tokens=8),
        # Not the ACP source: never a prompt either.
        ev("agent.message.started", 5, source="relay"),
        completion(6, "1"),
    ]
    analysis = m.analyze_participant(rows, [])
    assert len(analysis.prompts) == 1
    assert len(analysis.turns) == 1
    assert len(analysis.model_calls) == 1
    assert len(analysis.completions) == 1
    assert analysis.tool_calls == []


# -- tool calls -----------------------------------------------------------------------


def test_tool_calls_group_by_id_and_final_status_decides_failure():
    rows = [
        prompt(0, "1"),
        tool("tool.created", 1, "a", kind="read", name="Read file", status="pending", turn_id="1"),
        tool("tool.started", 2, "a", status="in_progress", turn_id="1"),
        tool("tool.completed", 3, "a", status="completed", turn_id="1"),
        tool("tool.created", 4, "b", kind="EDIT", name="Edit file", turn_id="1"),
        tool("tool.failed", 5, "b", status="failed", turn_id="1"),
        tool("tool.created", 6, "c", name="mystery", turn_id="1"),
        tool("tool.started", 7, "c", status="failed", turn_id="1"),
        # ``terminal/create`` carries no tool call id: never its own tool call.
        ev("tool.started", 8, turn_id="1"),
        completion(9, "1"),
    ]
    analysis = m.analyze_participant(rows, [])

    calls = {call.tool_call_id: call for call in analysis.tool_calls}
    assert set(calls) == {"a", "b", "c"}
    assert [calls[key].failed for key in ("a", "b", "c")] == [False, True, True]
    assert calls["a"].duration_ms == 2000.0
    assert calls["b"].kind == "edit"
    assert calls["c"].kind == "other"
    assert m.tool_kind_rows(analysis.tool_calls) == [
        {"tool_kind": "edit", "calls": 1, "failures": 1},
        {"tool_kind": "other", "calls": 1, "failures": 1},
        {"tool_kind": "read", "calls": 1, "failures": 0},
    ]
    metrics = analysis.metrics()
    assert metrics["tool_calls_per_prompt"] == 3.0
    assert metrics["tool_failure_rate"] == 0.667
    turn = analysis.turns[0]
    assert len(turn.tool_calls) == 3
    assert turn.tool_failures == 2


def test_relay_tool_calls_only_count_in_sessions_without_acp_tool_telemetry():
    rows = [
        # Session s1: the proxy observed the call; the relay's copy is dropped.
        tool("tool.created", 0, "x", kind="read", session="s1"),
        tool("tool.completed", 1, "x", session="s1"),
        tool("tool.completed", 2, "x", source="relay", session="s1", emitter="relay"),
        tool("tool.completed", 3, "y", source="relay", session="s1", emitter="relay"),
        # Session s2: relay-only, one execution per event.
        ev("tool.completed", 10, source="relay", session="s2", emitter="relay", tool_name="grep"),
        ev("tool.completed", 11, source="relay", session="s2", emitter="relay", tool_name="grep"),
        tool("tool.failed", 12, "z", source="relay", session="s2", emitter="self-report"),
        tool("tool.failed", 13, "z", source="relay", session="s2", emitter="relay"),
    ]
    analysis = m.analyze_participant(rows, [])
    by_session = {}
    for call in analysis.tool_calls:
        by_session.setdefault(call.session_id, []).append(call)
    assert len(by_session["s1"]) == 1 and not by_session["s1"][0].is_relay
    # Two id-less relay executions plus one relay id reported by two emitters.
    assert len(by_session["s2"]) == 3
    assert analysis.tool_failures == 1
    # Permission flows are only observable on ACP tool calls.
    assert analysis.metrics()["auto_run_share"] == 1.0


# -- permissions, cancellations, plans ----------------------------------------------------


def test_permissions_auto_run_share_denial_rate_and_wait():
    rows = [
        prompt(0, "1"),
        tool("tool.created", 1, "t1", kind="edit", turn_id="1"),
        tool("tool.created", 2, "t2", kind="execute", turn_id="1"),
        tool("tool.created", 3, "t3", kind="read", turn_id="1"),
        ev("permission.requested", 10, permission_id="p1", tool_call_id="t1", turn_id="1"),
        ev("permission.decided", 14, permission_id="p1", tool_call_id="t1", decision="allow", turn_id="1"),
        ev("permission.requested", 20, permission_id="p2", tool_call_id="t2", turn_id="1"),
        ev("permission.decided", 26, permission_id="p2", decision="reject", turn_id="1"),
        ev("permission.requested", 30, permission_id="p3", turn_id="1"),
        ev("permission.decided", 31, permission_id="p3", decision="cancelled", turn_id="1"),
        completion(40, "1"),
        # Another proxy process reuses id "p1": paired within its own emitter.
        ev("permission.requested", 50, permission_id="p1", emitter="proxy-2"),
        ev("permission.decided", 60, permission_id="p1", decision="selected", emitter="proxy-2"),
    ]
    analysis = m.analyze_participant(rows, [])
    metrics = analysis.metrics()
    assert metrics["auto_run_share"] == 0.333  # only t3 ran without a request
    assert metrics["permission_denial_rate"] == 0.5
    assert sorted(analysis.permission_waits) == [1.0, 4.0, 6.0, 10.0]
    assert metrics["median_permission_wait_seconds"] == 5.0
    assert dict(analysis.decision_counts) == {
        "allow": 1,
        "reject": 1,
        "cancelled": 1,
        "selected": 1,
    }
    assert analysis.turns[0].permission_requests == 3


def self_report(event_type, seconds, *, decision=None, scope=None, **kwargs) -> EventRow:
    """The built-in agent's own permission report, delivered through the relay."""
    return ev(
        event_type,
        seconds,
        source="relay",
        emitter="self-report:task-1",
        decision=decision,
        decision_scope=scope,
        **kwargs,
    )


def test_relay_permission_reports_count_once_and_only_when_someone_was_asked():
    rows = [
        prompt(0, "1"),
        # s1: the ACP proxy observed the round-trip the agent also reports.
        ev("permission.requested", 10, permission_id="p1", tool_call_id="t1", turn_id="1"),
        ev("permission.decided", 12, permission_id="p1", tool_call_id="t1", decision="allow", turn_id="1"),
        self_report("permission.requested", 10, tool_call_id="t1"),
        self_report("permission.decided", 12, tool_call_id="t1", decision="accepted", scope="once"),
        # Approved by the approval policy: nobody was asked.
        self_report("permission.decided", 13, tool_call_id="t2", decision="accepted", scope="policy"),
        completion(20, "1"),
        # s2 has no ACP permission events: the agent's own reports count, read
        # in the ACP vocabulary.
        self_report("permission.requested", 30, session="s2", tool_call_id="t3"),
        self_report("permission.decided", 31, session="s2", tool_call_id="t3", decision="rejected", scope="once"),
        self_report("permission.decided", 32, session="s2", tool_call_id="t4", decision="rejected", scope="policy"),
    ]
    analysis = m.analyze_participant(rows, [])
    assert dict(analysis.decision_counts) == {"allow": 1, "reject": 1}
    assert len(analysis.permission_requests) == 2
    assert analysis.metrics()["permission_denial_rate"] == 0.5
    assert analysis.turns[0].permission_requests == 1


def test_cancel_rate_counts_cancel_stop_reason_and_cancels_inside_turns():
    rows = [
        prompt(0, "1"),
        completion(10, "1", stop_reason="cancelled"),
        prompt(20, "2"),
        ev("interaction.completed", 25, turn_id="2"),
        completion(30, "2"),
        prompt(40, "3"),
        completion(50, "3"),
        prompt(60),
        ev("interaction.completed", 65),  # no turn id: inside the turn window
        completion(70),
        ev("interaction.completed", 90),  # outside every turn
        ev("interaction.completed", 91, source="relay"),  # not a user cancel
    ]
    analysis = m.analyze_participant(rows, [])
    assert len(analysis.cancels) == 3
    assert [turn.cancelled for turn in sorted(analysis.turns, key=lambda t: t.started_at)] == [
        True,
        True,
        False,
        True,
    ]
    assert analysis.metrics()["cancel_rate"] == 0.75


def test_plan_completion_rate_uses_the_last_plan_update_of_each_turn():
    rows = [
        prompt(0, "1"),
        ev("plan.updated", 1, turn_id="1", plan_size=4, plan_completed=1),
        ev("plan.updated", 2, turn_id="1", plan_size=4, plan_completed=3),
        completion(3, "1"),
        prompt(10, "2"),
        ev("plan.updated", 11, turn_id="2", plan_size=2, plan_completed=2),
        completion(12, "2"),
        prompt(20, "3"),
        completion(21, "3"),
        # Plan with no status counts: not measurable.
        prompt(30, "4"),
        ev("plan.updated", 31, turn_id="4", plan_size=3, plan_completed=None),
        completion(32, "4"),
    ]
    assert m.analyze_participant(rows, []).metrics()["plan_completion_rate"] == 0.875


# -- transitions ----------------------------------------------------------------------------


def test_transitions_count_consecutive_kinds_within_turns_with_lift():
    rows = [
        prompt(0, "A"),
        tool("tool.created", 1, "a1", kind="read", turn_id="A"),
        tool("tool.created", 2, "a2", kind="read", turn_id="A"),
        tool("tool.created", 3, "a3", kind="edit", turn_id="A"),
        completion(4, "A"),
        prompt(10, "B"),
        tool("tool.created", 11, "b1", kind="read", turn_id="B"),
        tool("tool.created", 12, "b2", kind="edit", turn_id="B"),
        tool("tool.created", 13, "b3", kind="execute", turn_id="B"),
        completion(14, "B"),
        prompt(20, "C"),
        tool("tool.created", 21, "c1", kind="search", turn_id="C"),
        tool("tool.created", 22, "c2", kind="read", turn_id="C"),
        completion(23, "C"),
        # Outside any turn: never part of a transition.
        tool("tool.created", 30, "x1", kind="delete"),
    ]
    analysis = m.analyze_participant(rows, [])
    assert m.transitions(analysis.turns) == [
        {"from": "read", "to": "edit", "count": 2, "lift": 1.667},
        {"from": "edit", "to": "execute", "count": 1, "lift": 5.0},
        {"from": "read", "to": "read", "count": 1, "lift": 0.833},
        {"from": "search", "to": "read", "count": 1, "lift": 2.5},
    ]
    assert m.transitions([]) == []


# -- sessions -----------------------------------------------------------------------------------


def test_session_seconds_coalesce_clamp_window_and_daily_split():
    assert m.session_seconds(session_row(closed=3600, activity=10)) == 3600.0
    assert m.session_seconds(session_row(activity=1800, heartbeat=5)) == 1800.0
    assert m.session_seconds(session_row(heartbeat=600)) == 600.0
    assert m.session_seconds(session_row(opened=None, closed=100)) == 0.0
    assert m.session_seconds(session_row(opened=7200, closed=3600)) == 0.0
    assert m.session_seconds(session_row()) == 0.0

    # 23:30 -> 00:30 across a UTC midnight.
    late = SessionRow(
        session_id="late",
        enrollment_id="e1",
        state="ended",
        opened_at=datetime(2026, 9, 20, 23, 30, tzinfo=timezone.utc),
        closed_at=datetime(2026, 9, 21, 0, 30, tzinfo=timezone.utc),
    )
    interval = m.session_interval(late)
    assert m.split_seconds_by_day(*interval) == {
        date(2026, 9, 20): 1800.0,
        date(2026, 9, 21): 1800.0,
    }
    second_day = DateWindow(start=date(2026, 9, 21), end=date(2026, 9, 21))
    assert m.session_seconds(late, second_day) == 1800.0
    assert m.session_seconds(late, DateWindow(start=date(2026, 9, 22))) == 0.0

    # The last representable day leaves the window open above.
    assert DateWindow(end=date.max).bounds() == (None, None)
    assert m.session_seconds(late, DateWindow(start=date(2026, 9, 21), end=date.max)) == 1800.0

    analysis = m.analyze_participant([], [late, session_row("s2", opened=0, closed=900)])
    assert analysis.session_seconds == 4500.0
    assert analysis.session_seconds_by_day == {
        date(2026, 9, 20): 2700.0,
        date(2026, 9, 21): 1800.0,
    }


# -- usage, onset, nulls ------------------------------------------------------------------------------


def test_usage_prefers_acp_turn_usage_and_otherwise_attributes_relay_calls():
    rows = [
        # Session sA reports prompt-response usage: the relay copy is ignored.
        prompt(0, "1", session="sA"),
        model_call(5, usage=50, session="sA"),
        completion(10, "1", usage=1000, session="sA"),
        prompt(20, "2", session="sA"),
        completion(30, "2", usage=2000, session="sA"),
        # Session sB reports none: exact relay model-call usage is used.
        model_call(95, usage=70, session="sB"),  # before any turn: total only
        prompt(100, "1", session="sB"),
        model_call(105, usage=300, session="sB"),
        completion(120, "1", session="sB"),
        model_call(121, usage=200, session="sB"),  # clock skew after completion
        prompt(200, "2", session="sB"),
        model_call(210, usage=100, session="sB"),
        completion(220, "2", session="sB"),
        prompt(300, "3", session="sB"),  # completed, but no usage at all
        completion(310, "3", session="sB"),
    ]
    analysis = m.analyze_participant(rows, [])
    assert analysis.usage_tokens == 1000 + 2000 + 70 + 300 + 200 + 100
    usage = [turn.usage_tokens for turn in sorted(analysis.turns, key=lambda t: t.started_at)]
    assert usage == [1000, 2000, 500, 100, None]
    assert analysis.metrics()["tokens_per_prompt"] == 900.0


def test_seconds_to_first_agent_edit_and_agent_writes():
    rows = [
        prompt(0, "1", session="s1"),
        tool("tool.created", 5, "e1", kind="edit", session="s1", turn_id="1"),
        tool("tool.completed", 40, "e1", session="s1", turn_id="1"),
        ev("ide.file.saved", 25, session="s1", turn_id="1"),  # agent fs write
        ev("ide.file.saved", 26, session="s1", source="ide", emitter="ide"),  # human save
        tool("tool.created", 41, "e2", kind="edit", session="s1", turn_id="1"),
        tool("tool.failed", 42, "e2", session="s1", turn_id="1"),  # not a write
        completion(50, "1", session="s1"),
        prompt(1000, "1", session="s2", emitter="proxy-2"),
        tool("tool.created", 1050, "e3", kind="edit", session="s2", emitter="proxy-2", turn_id="1"),
        tool("tool.completed", 1100, "e3", session="s2", emitter="proxy-2", turn_id="1"),
        completion(1200, "1", session="s2", emitter="proxy-2"),
        prompt(2000, "1", session="s3", emitter="proxy-3"),
        completion(2010, "1", session="s3", emitter="proxy-3"),
    ]
    analysis = m.analyze_participant(rows, [])
    assert sorted(analysis.first_edit_seconds) == [25.0, 100.0]
    # e1 and the fs write it made (inside its interval) are one write, not two.
    assert analysis.agent_file_writes == 2
    metrics = analysis.metrics()
    assert metrics["seconds_to_first_agent_edit"] == 62.5
    assert metrics["agent_writes_per_prompt"] == 0.667


def test_agent_writes_take_the_larger_observation_per_turn():
    rows = [
        # One edit call that wrote two files through the IDE.
        prompt(0, "1", session="s1"),
        tool("tool.created", 1, "e1", kind="edit", session="s1", turn_id="1"),
        ev("ide.file.saved", 2, session="s1", turn_id="1"),
        ev("ide.file.saved", 3, session="s1", turn_id="1"),
        tool("tool.completed", 4, "e1", session="s1", turn_id="1"),
        completion(5, "1", session="s1"),
        # Two edit calls writing to disk themselves (no fs requests).
        prompt(10, "2", session="s1"),
        tool("tool.created", 11, "e2", kind="edit", session="s1", turn_id="2"),
        tool("tool.completed", 12, "e2", session="s1", turn_id="2"),
        tool("tool.created", 13, "e3", kind="edit", session="s1", turn_id="2"),
        tool("tool.completed", 14, "e3", session="s1", turn_id="2"),
        completion(15, "2", session="s1"),
        # An fs write outside any turn, in another session.
        ev("ide.file.saved", 20, session="s9"),
    ]
    assert m.analyze_participant(rows, []).agent_file_writes == 2 + 2 + 1


def test_null_handling_for_missing_prompts_usage_and_session_time():
    empty = m.analyze_participant([], [])
    assert not empty.has_telemetry
    assert empty.usage_tokens is None
    assert empty.metrics() == {
        "prompts": 0,
        "active_days": 0,
        "session_hours": 0.0,
        "prompts_per_session_hour": None,
        "tool_calls_per_prompt": None,
        "tool_failure_rate": None,
        "auto_run_share": None,
        "permission_denial_rate": None,
        "median_permission_wait_seconds": None,
        "cancel_rate": None,
        "median_turn_seconds": None,
        "tokens_per_prompt": None,
        "errors_per_prompt": None,
        "agent_writes_per_prompt": None,
        "seconds_to_first_agent_edit": None,
        "ide_edits_per_session_hour": None,
        "plan_completion_rate": None,
    }

    # IDE-only activity: one edit per document change (payload count ignored).
    ide_only = m.analyze_participant(
        [
            ev("ide.document.changed", 10, source="ide", emitter="ide"),
            ev("ide.document.changed", 11, source="ide", emitter="ide"),
        ],
        [session_row(opened=0, closed=1800)],
    )
    metrics = ide_only.metrics()
    assert ide_only.ide_edits == 2
    assert metrics["ide_edits_per_session_hour"] == 4.0
    assert metrics["prompts_per_session_hour"] == 0.0
    assert metrics["tokens_per_prompt"] is None
    assert metrics["errors_per_prompt"] is None

    # Prompts without any reported usage keep tokens null, never 0.
    no_usage = m.analyze_participant([prompt(0, "1"), completion(5, "1")], [])
    assert no_usage.usage_tokens is None
    assert no_usage.metrics()["tokens_per_prompt"] is None
    assert no_usage.metrics()["errors_per_prompt"] == 0.0


def test_daily_aggregates_supply_presence_edits_and_first_last_event():
    daily = [
        DailyEventCount("e1", date(2026, 9, 20), events=5, ide_edits=3, first_at=at(0), last_at=at(50)),
        DailyEventCount("e1", date(2026, 9, 22), events=1, ide_edits=0, first_at=at(172800), last_at=at(172800)),
    ]
    analysis = m.analyze_participant([prompt(0, "1")], [], daily)
    assert analysis.event_count == 6
    assert analysis.ide_edits == 3
    assert analysis.first_event_at == at(0)
    assert analysis.last_event_at == at(172800)
    windowed = m.analyze_participant(
        [prompt(0, "1")], [], daily, window=DateWindow(start=date(2026, 9, 21))
    )
    assert windowed.event_count == 1
    assert windowed.prompts == []
    assert windowed.ide_edits == 0


# -- context cap (addendum) --------------------------------------------------------------------------


def test_context_block_counts_calls_over_and_under_the_cap():
    calls = [
        model_call(0, prompt_tokens=12_000),
        model_call(1, prompt_tokens=20_000),
        model_call(2, usage=5),  # no provider prompt tokens reported
    ]
    assert m.context_block(calls, 16_000) == {
        "cap_tokens": 16_000,
        "model_calls": 3,
        "calls_with_prompt_tokens": 2,
        "prompt_tokens_p50": 16_000.0,
        "prompt_tokens_p95": 19_600.0,
        "prompt_tokens_max": 20_000,
        "over_cap_calls": 1,
        "over_cap_share": 0.5,
        "coverage": "PARTIAL",
    }
    # A BYOA runtime never uses the relay: nothing to measure.
    assert m.context_block([], m.effective_context_cap(None)) == {
        "cap_tokens": 16_000,
        "model_calls": 0,
        "calls_with_prompt_tokens": 0,
        "prompt_tokens_p50": None,
        "prompt_tokens_p95": None,
        "prompt_tokens_max": None,
        "over_cap_calls": 0,
        "over_cap_share": None,
        "coverage": "UNAVAILABLE",
    }
    assert m.effective_context_cap(8000) == 8000
    assert m.effective_context_cap(0) == m.FALLBACK_MAX_CONTEXT_TOKENS == 16_000
    assert m.effective_context_cap(True) == 16_000


# -- builders -------------------------------------------------------------------------------------------


def _frame() -> StudyFrame:
    arms = [
        ArmRow("pa", name="Managed", model="m-1", framework_version="code4me2-agent", selection_order=0, max_context_tokens=8000),
        ArmRow("pb", name="Codex", model="m-2", framework_version="codex", selection_order=1),
    ]
    enrollments = [
        EnrollmentRow("e1", "P-1", "ACTIVE", enrolled_at=at(-100)),
        EnrollmentRow("e2", "P-2", "ACTIVE", enrolled_at=at(-90)),
        EnrollmentRow("e3", "P-3", "REVOKED", enrolled_at=at(-80)),
        EnrollmentRow("e4", "P-4", "ACTIVE", enrolled_at=at(-70)),
    ]
    assignments = [
        AssignmentRow("e1", "pa", "ACTIVE", assigned_at=at(-100), name="Managed", max_context_tokens=8000),
        AssignmentRow("e2", "pb", "ACTIVE", assigned_at=at(-90), name="Codex"),
        AssignmentRow("e3", "pb", "ACTIVE", assigned_at=at(-80)),  # label from the arm
    ]
    sessions = [
        session_row("s1", opened=0, closed=3600, enrollment="e1"),
        session_row("s2", opened=0, closed=1800, enrollment="e2", state="running"),
    ]
    return StudyFrame(arms=arms, enrollments=enrollments, assignments=assignments, sessions=sessions)


def _events():
    return [
        prompt(10, "1", enrollment="e1"),
        tool("tool.created", 11, "t1", kind="read", turn_id="1", enrollment="e1"),
        model_call(12, usage=100, prompt_tokens=9000, enrollment="e1"),
        model_call(13, usage=100, prompt_tokens=7000, enrollment="e1"),
        completion(20, "1", enrollment="e1"),
        prompt(10, "1", session="s2", emitter="proxy-e2", enrollment="e2"),
        completion(40, "1", session="s2", emitter="proxy-e2", enrollment="e2"),
        prompt(50, "2", session="s2", emitter="proxy-e2", enrollment="e2"),
        completion(60, "2", session="s2", emitter="proxy-e2", enrollment="e2"),
    ]


def test_participants_builder_rows_health_and_arm_counts():
    now = at(3600)
    body = m.build_participants(_frame(), _events(), [], study_id="study", now=now)
    assert [arm["participants"] for arm in body["arms"]] == [1, 2]
    rows = {row["participant_code"]: row for row in body["participants"]}
    assert [row["participant_code"] for row in body["participants"]] == ["P-1", "P-2", "P-3", "P-4"]
    assert rows["P-1"]["health"] == "ACTIVE"
    assert rows["P-2"]["health"] == "ACTIVE"
    assert rows["P-3"]["health"] == "INACTIVE"
    assert rows["P-4"]["health"] == "NO_TELEMETRY"
    assert rows["P-4"]["arm"] is None
    assert rows["P-3"]["arm"]["name"] == "Codex"  # falls back to the arm snapshot
    assert rows["P-1"]["activity"]["usage_tokens"] == 200
    assert rows["P-2"]["sessions"] == {
        "total": 1,
        "active": 1,
        "session_seconds": 1800.0,
        "last_activity_at": None,
        "last_heartbeat_at": None,
    }
    idle = m.build_participants(_frame(), _events(), [], study_id="study", now=at(8 * 86400))
    assert {row["participant_code"]: row["health"] for row in idle["participants"]}["P-1"] == "IDLE"


def test_summary_aggregates_per_participant_and_reports_context_per_arm():
    body = m.build_study_summary(_frame(), _events(), [], study_id="study", now=at(3600))
    managed, codex = body["arms"]
    assert managed["participants"] == 1 and codex["participants"] == 2
    # Count metrics include assigned participants without telemetry (e3 here);
    # rates exclude them.
    assert codex["metrics"]["prompts"]["values"] == [0.0, 2.0]
    assert codex["metrics"]["median_turn_seconds"]["values"] == [20.0]
    assert codex["participants_with_telemetry"] == 1
    assert managed["context"]["cap_tokens"] == 8000
    assert managed["context"]["over_cap_calls"] == 1
    assert managed["context"]["coverage"] == "AVAILABLE"
    assert codex["context"]["cap_tokens"] == 16_000
    assert codex["context"]["model_calls"] == 0
    assert codex["context"]["coverage"] == "UNAVAILABLE"
    assert body["totals"]["prompts"] == 3
    assert body["totals"]["usage_tokens"] == 200
    assert body["totals"]["usage_coverage"] == 0.333
    assert body["coverage"] == {
        "usage_tokens": "PARTIAL",
        "turn_correlation": "AVAILABLE",
        "tool_kind": "AVAILABLE",
    }
    assert body["daily"][0]["by_arm"] == {
        "pa": {"prompts": 1, "active_participants": 1},
        "pb": {"prompts": 2, "active_participants": 1},
    }

    windowed = m.build_study_summary(
        _frame(),
        _events(),
        [],
        study_id="study",
        now=at(3600),
        window=DateWindow(start=date(2026, 9, 21)),
    )
    assert windowed["window"] == {"start": "2026-09-21", "end": None}
    assert windowed["totals"]["prompts"] == 0
    assert windowed["totals"]["sessions"] == 0
    assert windowed["daily"] == []


def test_detail_builder_timeline_enriches_tool_names_and_skips_noise():
    frame = _frame()
    events = _events() + [tool("tool.completed", 15, "t1", turn_id="1", enrollment="e1")]
    timeline = events + [
        ev("agent.message.started", 16, turn_id="1", message_kind="assistant", enrollment="e1"),
        ev("ide.document.changed", 17, source="ide", emitter="ide", enrollment="e1"),
        ev("usage.updated", 18, enrollment="e1"),
    ]
    body = m.build_participant_detail(
        frame, "e1", events, [], timeline, study_id="study", now=at(3600)
    )
    assert body["participant_code"] == "P-1"
    assert body["context"]["cap_tokens"] == 8000
    assert body["context"]["model_calls"] == 2
    types = [item["event_type"] for item in body["timeline"]]
    assert "ide.document.changed" not in types and "usage.updated" not in types
    assert types.count("agent.message.started") == 1
    completed = next(item for item in body["timeline"] if item["event_type"] == "tool.completed")
    # The completion update carries no name/kind: enriched from its tool call.
    assert completed["tool_name"] is None
    assert completed["tool_kind"] == "read"
    assert body["turns"][0]["tool_calls"] == 1
    assert body["daily"] == [
        {
            "date": "2026-09-20",
            "prompts": 1,
            "tool_calls": 1,
            "errors": 0,
            "ide_edits": 0,
            "session_seconds": 3600.0,
        }
    ]
    assert m.build_participant_detail(frame, "missing", [], [], [], study_id="s", now=at(0)) is None


def test_timeline_lists_each_permission_decision_once_in_the_acp_vocabulary():
    events = _events() + [
        ev("permission.decided", 14, permission_id="p1", decision="allow", enrollment="e1"),
        self_report("permission.decided", 14, decision="accepted", scope="once", enrollment="e1"),
        self_report("permission.decided", 15, decision="accepted", scope="policy", enrollment="e1"),
        self_report("permission.decided", 16, session="s9", decision="rejected", scope="once", enrollment="e1"),
    ]
    body = m.build_participant_detail(
        _frame(), "e1", events, [], events, study_id="study", now=at(3600)
    )
    decisions = sorted(
        (item["source"], item["decision"])
        for item in body["timeline"]
        if item["event_type"] == "permission.decided"
    )
    assert decisions == [("acp", "allow"), ("relay", "reject")]


# -- tool names ------------------------------------------------------------------------------------


def test_tool_titles_naming_files_patterns_or_commands_are_never_shown():
    # Tool identifiers are shown as they are.
    for shown in ("read_file", "developer__shell", "mcp__github__search", "run_command"):
        assert m.display_tool_name(shown) == shown
    # ACP titles embed paths, search patterns and commands (the built-in agent
    # reports e.g. ``Read {path}`` and ``Search "{pattern}"``); a bare file name
    # is not an identifier either.
    for hidden in (
        "Read src/app/secrets.py",
        'Search "api_key"',
        "Find files matching *.env",
        "Run git status",
        "Delete credentials",
        "Read file",
        "Call github: create_issue",
        "secrets.env",
        "mcp.github.search",
        "",
        None,
        42,
    ):
        assert m.display_tool_name(hidden) is None, hidden

    calls = m.build_tool_calls(
        [
            tool("tool.created", 1, "t1", kind="read", name="Read src/a.py"),
            tool("tool.created", 2, "t2", kind="read", name="Read src/b.py"),
            tool("tool.created", 3, "t3", kind="execute", name="Run make deploy-prod"),
            tool("tool.created", 4, "t4", kind="read", name="read_file"),
        ]
    )
    rows = m.tool_rows(((call, None) for call in calls.values()), limit=10)
    assert [(row["tool_name"], row["tool_kind"], row["calls"]) for row in rows] == [
        (None, "read", 2),
        ("read_file", "read", 1),
        (None, "execute", 1),
    ]


def test_the_relay_names_an_acp_call_it_also_reported():
    rows = [
        prompt(0, "1"),
        tool("tool.created", 1, "t1", kind="read", name="Read src/secret_plan.md", turn_id="1"),
        tool("tool.completed", 2, "t1", status="completed", turn_id="1"),
        # The built-in agent's relay self-report of the same call.
        ev("tool.completed", 2, source="relay", emitter="self-report", tool_call_id="t1", tool_name="read_file"),
        tool("tool.created", 3, "t2", kind="execute", name="Run rm -rf build", turn_id="1"),
        completion(5, "1"),
    ]
    analysis = m.analyze_participant(rows, [])
    # The relay copy names the call but is not counted again.
    assert len(analysis.tool_calls) == 2
    named = m.tool_rows(((call, None) for call in analysis.tool_calls), limit=10)
    assert [(row["tool_name"], row["tool_kind"]) for row in named] == [("read_file", "read"), (None, "execute")]
