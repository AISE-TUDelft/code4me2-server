from __future__ import annotations

import json

from code4me_swebench import check


def _event(seq, kind, payload=None, **extra):
    return {"schema_version": check.SCHEMA, "event_id": f"e{seq}", "event_type": kind,
            "timestamp": f"2026-10-09T00:00:{seq:02d}Z", "sequence": seq, "run_id": "r",
            "session_id": "s", "parent_event_id": None, "payload": payload or {}, **extra}


def _write(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _good():
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    return [
        _event(1, "agent.run.started"),
        _event(2, "agent.model.requested"),
        _event(3, "agent.model.completed", metrics=usage, raw_payload={"response": {"usage": usage}}),
        _event(4, "agent.tool.called", {"tool_call_id": "t1"}),
        _event(5, "agent.tool.completed", {"tool_call_id": "t1"}),
        _event(6, "agent.model.requested", {"call_purpose": "self_review"}),
        _event(7, "agent.response.completed"),
        _event(8, "agent.run.completed", {"status": "completed"}, metrics={"failed_side_calls": 1}),
    ]


def test_a_complete_trace_has_no_problems(tmp_path):
    assert check.check_trace(_write(tmp_path / "t.jsonl", _good()), secret="sk-secret") == []


def test_unanswered_requests_unresolved_tools_and_token_drift_are_reported(tmp_path):
    events = _good()
    events[2]["metrics"] = {"prompt_tokens": 11, "completion_tokens": 2}
    events[7]["metrics"] = {}  # the failed review is no longer reported
    del events[4]  # the tool outcome
    for seq, event in enumerate(events, start=1):
        event["sequence"] = seq
    problems = check.check_trace(_write(tmp_path / "t.jsonl", events))
    assert any("model requests" in p for p in problems)
    assert any("tool calls without an outcome" in p for p in problems)
    assert any("differ from provider usage" in p for p in problems)


def test_a_leaked_key_and_gaps_in_sequence_are_reported(tmp_path):
    events = _good()
    events[3]["payload"]["stdout"] = "OPENCODE=sk-secret"
    events[5]["sequence"] = 99
    problems = check.check_trace(_write(tmp_path / "t.jsonl", events), secret="sk-secret")
    assert "provider key found in trace" in problems
    assert any("sequence" in p for p in problems)
