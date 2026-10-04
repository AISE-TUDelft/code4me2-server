"""Chat traces: turns of prompts, reasoning, messages and tool calls (pure)."""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone

from research.analysis.study_traces.assemble import build_page, content_text, diff_blocks
from research.analysis.study_traces.store import TraceRow

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
_SEQUENCE = itertools.count(1)


def row(event_type, seconds, *, payload=None, correlations=None, metrics=None, lifecycle=None) -> TraceRow:
    sequence = next(_SEQUENCE)
    return TraceRow(
        event_id=f"00000000-0000-0000-0000-{sequence:012d}",
        occurred_at=T0 + timedelta(seconds=seconds),
        emitter_id="proxy-1",
        emitter_sequence=sequence,
        research_session_id="rs-1",
        event_type=event_type,
        source="acp",
        lifecycle_state=lifecycle,
        payload={"session_id": "chat-1", **(payload or {})},
        correlations=correlations or {},
        metrics=metrics or {},
    )


def prompt(seconds, turn, text=None):
    rows = [row("agent.message.started", seconds, correlations={"turn_id": turn}, payload={"acp_method": "session/prompt"})]
    if text is not None:
        rows.append(
            row(
                "agent.message.started",
                seconds,
                lifecycle="started",
                correlations={"turn_id": turn},
                payload={"message_kind": "user", "prompt": text},
            )
        )
    return rows


def chunk(seconds, kind, text, message_id="m1"):
    key = "reasoning" if kind == "thought" else "content"
    return row(
        "agent.message.started",
        seconds,
        lifecycle="started",
        payload={"message_kind": kind, "message_id": message_id, key: text},
    )


def test_content_text_reads_acp_blocks_and_reports_redaction():
    assert content_text({"type": "text", "text": "hi"}) == ("hi", False)
    assert content_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == ("a\nb", False)
    assert content_text("[REDACTED]") == (None, True)
    assert content_text({"type": "content", "content": {"type": "text", "text": "out"}}) == ("out", False)
    assert content_text({"type": "resource_link", "uri": "file:///a.py"}) == ("[file: file:///a.py]", False)
    assert content_text({"type": "image", "data_length": 10}) == ("[image]", False)
    assert content_text({"path": "a.py"}) == ('{"path": "a.py"}', False)


def test_diff_blocks_render_unified_diffs():
    diffs = diff_blocks([{"type": "diff", "path": "a.py", "oldText": "x = 1\n", "newText": "x = 2\n"}])
    assert diffs[0]["path"] == "a.py"
    assert "-x = 1" in diffs[0]["diff"] and "+x = 2" in diffs[0]["diff"]
    assert diff_blocks([{"type": "diff", "path": "a.py", "oldText": "[REDACTED]", "newText": "[REDACTED]"}]) == [
        {"path": "a.py", "diff": None, "redacted": True}
    ]


def test_a_turn_merges_chunks_and_joins_the_approval_to_its_tool_call():
    rows = [
        row("interaction.started", 0, payload={"acp_method": "session/new"}),
        *prompt(1, "1", [{"type": "text", "text": "Fix the bug"}]),
        chunk(2, "thought", {"type": "text", "text": "Let me "}),
        chunk(3, "thought", {"type": "text", "text": "look."}),
        chunk(4, "assistant", {"type": "text", "text": "Reading the file."}),
        row("tool.created", 5, payload={"tool_call_id": "t1", "tool_name": "Edit a.py", "tool_kind": "edit", "arguments": {"path": "a.py"}}),
        row("permission.requested", 6, correlations={"permission_id": "9"}, payload={"tool_call_id": "t1", "options": [{"option_id": "allow", "kind": "allow_once"}, {"option_id": "revise", "kind": "reject_once"}]}),
        row("permission.decided", 9, correlations={"permission_id": "9"}, payload={"decision": "reject", "selected_option_id": "revise"}),
        row(
            "tool.completed",
            10,
            payload={"tool_call_id": "t1", "status": "completed", "content": [{"type": "diff", "path": "a.py", "oldText": "a\n", "newText": "b\n"}]},
        ),
        chunk(11, "thought", {"type": "text", "text": "Done."}, message_id="m2"),
        row("agent.message.completed", 12, payload={"stop_reason": "end_turn"}, metrics={"usage_tokens": 321}),
    ]
    turns, next_row, next_index = build_page(rows, limit=10)
    assert next_row is None and next_index == 1
    preamble, turn = turns
    assert preamble["kind"] == "preamble"
    assert preamble["blocks"][0] == {
        "type": "lifecycle",
        "acp_method": "session/new",
        "end_reason": None,
        "at": T0.isoformat(),
        "offset_ms": 0,
    }
    assert turn["index"] == 1 and turn["turn_id"] == "1"
    assert turn["stop_reason"] == "end_turn" and turn["usage_tokens"] == 321 and turn["duration_ms"] == 11000
    types = [block["type"] for block in turn["blocks"]]
    assert types == ["prompt", "thought", "message", "tool", "thought"]
    prompt_block, thought, message, tool, last = turn["blocks"]
    assert prompt_block["text"] == "Fix the bug" and prompt_block["captured"] is True
    assert thought["text"] == "Let me look." and thought["chunks"] == 2
    assert message["text"] == "Reading the file."
    assert last["text"] == "Done."
    assert tool["title"] == "Edit a.py" and tool["status"] == "completed" and tool["duration_ms"] == 5000
    assert tool["arguments"]["text"] == '{"path": "a.py"}'
    assert tool["diffs"][0]["path"] == "a.py"
    assert tool["permission"]["decision"] == "revise"
    assert tool["permission"]["wait_ms"] == 3000
    assert [option["option_id"] for option in tool["permission"]["options"]] == ["allow", "revise"]
    assert "_raw" not in thought and "_message_id" not in thought


def revision_report(seconds, tool_call_id, *, action="accept", status=None, hunks=3, kept=None, blob=None) -> TraceRow:
    """The built-in agent's own relayed report of a "Revise…" form (no chat id)."""
    sequence = next(_SEQUENCE)
    payload = {"decision": "revised" if action == "accept" else "rejected", "elicitation_action": action}
    if status is not None:
        payload["revise_status"] = status
    if blob is not None:
        payload["payload"] = blob
    counts = {"hunk_count": hunks, **({"kept_hunk_count": kept} if kept is not None else {})}
    return TraceRow(
        event_id=f"00000000-0000-0000-0000-{sequence:012d}",
        occurred_at=T0 + timedelta(seconds=seconds),
        emitter_id="self-report:run-1",
        emitter_sequence=sequence,
        research_session_id="rs-1",
        event_type="permission.decided",
        source="relay",
        payload=payload,
        correlations={"tool_call_id": tool_call_id},
        metrics={"counts": counts},
    )


def _revised_tool(*report_rows):
    rows = [
        *prompt(0, "1", "Change three things"),
        row("tool.created", 1, payload={"tool_call_id": "t1", "tool_name": "Write Calc.java", "tool_kind": "edit"}),
        row("permission.requested", 2, correlations={"permission_id": "9"}, payload={"tool_call_id": "t1"}),
        row("permission.decided", 4, correlations={"permission_id": "9"}, payload={"decision": "reject", "selected_option_id": "revise"}),
        *report_rows,
        # The agent reports the proposed call as failed after the form.
        row("tool.failed", 21, payload={"tool_call_id": "t1", "status": "failed"}),
    ]
    turn = build_page(rows, limit=5)[0][0]
    return next(block for block in turn["blocks"] if block["type"] == "tool")


def test_the_agents_revise_report_joins_its_tool_call():
    blob = json.dumps({"payload": {"tool_call_id": "t1", "text": "Keep LIMIT; return value + value."}})
    tool = _revised_tool(revision_report(20, "t1", status="applied", hunks=3, kept=2, blob=blob))
    # The raw status stays; the research UI shows a sent form as "Revised".
    assert tool["status"] == "failed"
    assert tool["permission"]["decision"] == "revise"
    assert tool["permission"]["revision"] == {
        "form": "accept",
        "status": "applied",
        "hunks": 3,
        "kept_hunks": 2,
        "instructions": {"text": "Keep LIMIT; return value + value.", "redacted": False, "truncated": False},
    }


def test_revise_instructions_follow_the_content_policy_and_declines_are_kept():
    tool = _revised_tool(revision_report(20, "t1", status="instructions_only", hunks=1, kept=0, blob="[REDACTED]"))
    assert tool["permission"]["revision"]["instructions"] == {"text": None, "redacted": True, "truncated": False}
    declined = _revised_tool(revision_report(20, "t1", action="decline"))
    assert declined["permission"]["revision"] == {
        "form": "decline",
        "status": None,
        "hunks": 3,
        "kept_hunks": None,
        "instructions": None,
    }


def test_a_sent_form_stored_without_its_content_shows_instructions_as_not_captured():
    # Metadata-only studies store the agent's report without its content blob,
    # and a sent form always carries instructions.
    tool = _revised_tool(revision_report(20, "t1", status="applied", hunks=2, kept=1))
    assert tool["permission"]["revision"]["instructions"] == {"text": None, "redacted": True, "truncated": False}
    # A captured report whose instructions were blank has nothing to show.
    blank = json.dumps({"payload": {"tool_call_id": "t1"}})
    tool = _revised_tool(revision_report(20, "t1", status="applied", hunks=2, kept=1, blob=blank))
    assert tool["permission"]["revision"]["instructions"] is None


def test_unmapped_protocol_messages_are_left_out_of_the_trace():
    rows = [
        *prompt(0, "1", "Hi"),
        row("unknown_source_event", 1, payload={"unknown_method": "session/update:available_commands_update"}),
        row("ide.file.opened", 2, payload={"path": "a.py"}),
    ]
    turn = build_page(rows, limit=5)[0][0]
    assert [block["type"] for block in turn["blocks"]] == ["prompt", "event"]
    assert turn["blocks"][1]["event_type"] == "ide.file.opened"


def test_redacted_content_is_reported_as_not_captured():
    rows = [
        *prompt(0, "1", "[REDACTED]"),
        chunk(1, "thought", "[REDACTED]"),
        row("tool.created", 2, payload={"tool_call_id": "t1", "tool_name": "Run", "arguments": "[REDACTED]"}),
    ]
    turn = build_page(rows, limit=5)[0][0]
    prompt_block, thought, tool = turn["blocks"]
    assert prompt_block["text"] is None and prompt_block["redacted"] is True
    assert thought["text"] is None and thought["redacted"] is True
    assert tool["arguments"] == {"text": None, "redacted": True, "truncated": False}


def test_prompts_from_older_proxies_have_no_text_but_still_open_turns():
    turn = build_page(prompt(0, "1"), limit=5)[0][0]
    assert turn["blocks"][0] == {
        "type": "prompt",
        "text": None,
        "redacted": False,
        "truncated": False,
        "captured": False,
        "at": T0.isoformat(),
        "offset_ms": 0,
    }


def test_cancels_and_chat_ends_are_told_apart():
    rows = [
        *prompt(0, "1"),
        row("interaction.completed", 1, payload={"acp_method": "session/cancel"}),
        row("interaction.completed", 2, lifecycle="completed", payload={"acp_method": "session/close"}),
        row("interaction.completed", 3, lifecycle="completed", payload={"end_reason": "host_closed"}),
    ]
    blocks = build_page(rows, limit=5)[0][0]["blocks"]
    assert [block["type"] for block in blocks] == ["prompt", "cancel", "lifecycle", "lifecycle"]
    assert blocks[3]["end_reason"] == "host_closed"


def test_pages_stop_at_the_next_turn_and_keep_numbering():
    rows = [*prompt(0, "1"), *prompt(10, "2"), *prompt(20, "3")]
    first, next_row, next_index = build_page(rows, limit=2)
    assert [turn["index"] for turn in first] == [1, 2]
    assert next_row is rows[2] and next_index == 2
    second, end, _ = build_page(rows[2:], limit=2, start_index=next_index)
    assert [turn["index"] for turn in second] == [3] and end is None


def test_a_huge_turn_is_cut_and_continued():
    rows = [*prompt(0, "1"), *(chunk(seconds, "assistant", "x", message_id=f"m{seconds}") for seconds in range(1, 6))]
    turns, next_row, next_index = build_page(rows, limit=5, max_events=3)
    assert turns[0]["continues"] is True and next_row is rows[3] and next_index == 1
    rest, end, _ = build_page(rows[3:], limit=5, start_index=next_index)
    assert rest[0]["kind"] == "continued" and rest[0]["index"] == 1 and end is None


def test_a_prompt_shows_what_was_typed_and_names_what_the_ide_sent_along():
    # IntelliJ links the open file to every prompt; files and images can be
    # attached too. The prompt reads as typed; attachments are listed by name,
    # never by path (the path names the participant's home folder).
    blocks = [
        {"type": "text", "text": "Explain this"},
        {
            "type": "resource_link",
            "name": "Dummy.java",
            "uri": "file:///Users/someone/project/Dummy.java",
            "description": "File that is opened in the IDE and is currently viewed by the user",
        },
        {"type": "resource", "resource": {"uri": "file:///Users/someone/project/src/Calc.java", "text": "class Calc {}"}},
        {"type": "image", "mimeType": "image/png", "data_length": 12},
        {"type": "text", "text": "briefly"},
    ]
    turn = build_page(prompt(0, "1", blocks), limit=5)[0][0]
    block = turn["blocks"][0]

    assert block["text"] == "Explain this\nbriefly"
    assert block["attachments"] == [
        {"name": "Dummy.java", "type": "resource_link"},
        {"name": "Calc.java", "type": "resource"},
        {"name": "image", "type": "image"},
    ]
    assert "someone" not in json.dumps(block)


def test_a_redacted_prompt_names_no_attachments():
    block = build_page(prompt(0, "1", "[REDACTED]"), limit=5)[0][0]["blocks"][0]
    assert block["redacted"] is True and "attachments" not in block
