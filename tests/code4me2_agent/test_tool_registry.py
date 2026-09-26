from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code4me2_agent.adapters import (
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
    _tool_event_metadata,
    _tool_result_summary,
)
from code4me2_agent.config import AgentConfig
from code4me2_agent.events import ApprovalDecision, PlanEntrySpec
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_catalog import MUTATING_TOOLS, tool_names
from code4me2_agent.tool_errors import EditMatchError, ToolArgumentError, ToolFileNotFoundError

VALID_ARGUMENTS = {
    "read_file": {"path": "a.txt"},
    "create_file": {"path": "a.txt", "content": "x"},
    "write_file": {"path": "a.txt", "content": "x"},
    "replace_text": {"path": "a.txt", "old_text": "before", "new_text": "after"},
    "edit_file": {"path": "a.txt", "edits": [{"old_text": "before", "new_text": "after"}]},
    "delete_file": {"path": "a.txt"},
    "move_file": {"source_path": "a.txt", "destination_path": "b/a.txt"},
    "list_files": {},
    "glob_files": {"pattern": "**/*.py"},
    "grep_files": {"pattern": "TODO", "glob": "*.py", "output_mode": "count"},
    "search_files": {"query": "TODO"},
    "run_command": {"argv": ["ls", "-la"], "timeout_seconds": 30},
    "update_plan": {"entries": [{"content": "step", "status": "pending"}]},
}
HANDLER_TARGET = {
    "run_command": "command_tools",
}


class RecordingSink:
    def __init__(self, decision: ApprovalDecision | None = None) -> None:
        self.decision = decision or ApprovalDecision("accepted", "once")
        self.events = []
        self.requests = []
        self.plans = []

    def tool_call(self, event):
        self.events.append(event)

    def request_approval(self, tool_call, arguments):
        self.requests.append((tool_call.name, arguments))
        return self.decision

    def plan(self, event):
        self.plans.append(event)


def _mocks():
    file_tools = MagicMock()
    file_tools.workspace_root = Path("/ws")
    file_tools.read_text.return_value = ("before", False)
    for name in ("read_file", "create_file", "write_file", "replace_text", "edit_file",
                 "delete_file", "move_file", "list_files", "glob_files", "grep_files", "search_files"):
        getattr(file_tools, name).return_value = {"ok": True, "name": name}
    command_tools = MagicMock()
    command_tools.run_command.return_value = {"ok": True, "name": "run_command"}
    return file_tools, command_tools


def _call(name: str, arguments=None, call_id: str = "call-1") -> ToolCall:
    return ToolCall(call_id, name, dict(VALID_ARGUMENTS[name] if arguments is None else arguments))


@pytest.mark.parametrize("name", [n for n in tool_names() if n != "update_plan"])
def test_every_catalogue_tool_dispatches_to_its_handler(name):
    file_tools, command_tools = _mocks()
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    result = registry.execute(_call(name), run_id="run-1", request_id="request-1")

    target = command_tools if HANDLER_TARGET.get(name) == "command_tools" else file_tools
    getattr(target, name).assert_called_once()
    assert result == {"ok": True, "name": name}
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "in_progress"),
        ("completed", "completed"),
    ]


def test_argument_errors_name_the_field_and_emit_failed_card():
    file_tools, command_tools = _mocks()
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    with pytest.raises(ToolArgumentError) as missing_path:
        registry.execute(_call("read_file", {}), run_id="r", request_id="q")
    assert missing_path.value.field == "path"
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    assert "Invalid arguments" in sink.events[-1].content_text

    with pytest.raises(ToolArgumentError) as argv_string:
        registry.execute(_call("run_command", {"argv": "ls -la"}), run_id="r", request_id="q")
    assert argv_string.value.field == "argv"
    assert "separate argv items" in str(argv_string.value)

    with pytest.raises(ToolArgumentError) as bad_status:
        registry.execute(
            _call("update_plan", {"entries": [{"content": "x", "status": "done"}]}),
            run_id="r",
            request_id="q",
        )
    assert bad_status.value.field == "entries[0].status"
    file_tools.read_file.assert_not_called()
    command_tools.run_command.assert_not_called()


def test_replace_text_mismatch_fails_before_approval():
    file_tools, command_tools = _mocks()
    file_tools.read_text.return_value = ("actual content", False)
    sink = RecordingSink(ApprovalDecision("accepted", "once"))
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink, approval_policy="per_step")

    with pytest.raises(EditMatchError) as error:
        registry.execute(
            _call("replace_text", {"path": "a.txt", "old_text": "missing", "new_text": "x"}),
            run_id="r",
            request_id="q",
        )

    assert error.value.code == "edit_no_match"
    assert sink.requests == []
    file_tools.replace_text.assert_not_called()
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "pending"),
        ("failed", "failed"),
    ]
    assert "was not found" in sink.events[-1].content_text


def test_edit_preview_for_missing_file_is_empty_before():
    file_tools, command_tools = _mocks()
    file_tools.read_text.side_effect = ToolFileNotFoundError("missing")
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    registry.execute(_call("write_file", {"path": "new.txt", "content": "content"}), run_id="r", request_id="q")

    assert [(event.diff_old_text, event.diff_new_text) for event in sink.events] == [
        ("", "content"),
        ("", "content"),
    ]


def test_delete_file_preview_tolerates_directories():
    file_tools, command_tools = _mocks()
    file_tools.read_text.side_effect = ToolArgumentError("is a directory", field="path")
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    registry.execute(_call("delete_file", {"path": "empty-dir"}), run_id="r", request_id="q")

    file_tools.delete_file.assert_called_once()
    assert sink.events[-1].status == "completed"


@pytest.mark.parametrize("name", tool_names())
def test_event_metadata_kinds_titles_and_absolute_paths(name):
    workspace_root = Path("/ws")
    metadata = _tool_event_metadata(name, VALID_ARGUMENTS[name], workspace_root=workspace_root)

    if name == "update_plan":
        assert metadata is None
        return
    assert metadata["kind"] in {"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"}
    assert metadata["title"]
    if metadata.get("path"):
        Path(metadata["path"]).relative_to(workspace_root)
    if name == "move_file":
        assert metadata["locations"] == (
            str(workspace_root / "a.txt"),
            str(workspace_root / "b" / "a.txt"),
        )
        assert metadata["kind"] == "move"
    if name == "delete_file":
        assert metadata["kind"] == "delete"
    if name in {"glob_files", "grep_files", "search_files", "list_files"}:
        assert metadata["kind"] == "search"


def test_mcp_metadata_uses_server_and_tool_names():
    metadata = _tool_event_metadata("mcp__idea__find_symbol", {"q": "x"}, workspace_root=Path("/ws"))
    assert metadata["title"] == "Call idea: find_symbol"
    assert metadata["kind"] == "other"


def test_completed_card_summaries():
    assert _tool_result_summary("grep_files", {"match_count": 2, "file_count": 1}) == "Found 2 matches in 1 file"
    assert _tool_result_summary("glob_files", {"files": ["a", "b"], "truncated": True}) == "Found 2 files (truncated)"
    command = _tool_result_summary(
        "run_command", {"exit_code": 0, "duration_ms": 1200, "stdout": "all good\n", "stderr": ""}
    )
    assert command.startswith("Exit code 0 in 1.2 s")
    assert "all good" in command
    assert _tool_result_summary("run_command", {"timed_out": True, "timeout_seconds": 120}) == "Timed out after 120 s"
    assert _tool_result_summary("edit_file", {"edits_applied": 3}) == "Applied 3 edits"
    assert _tool_result_summary("read_file", {"start_line": 1, "end_line": 20, "total_lines": 50, "truncated": True, "path": "x/a.py"}) == "Read 20 lines of a.py (of 50; truncated)"


def test_failed_card_contains_error_text():
    file_tools, command_tools = _mocks()
    file_tools.read_file.side_effect = ToolFileNotFoundError("File not found: nope.txt")
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    with pytest.raises(ToolFileNotFoundError):
        registry.execute(_call("read_file", {"path": "nope.txt"}), run_id="r", request_id="q")

    assert sink.events[-1].phase == "failed"
    assert "failed: File not found: nope.txt" in sink.events[-1].content_text


def test_update_plan_reaches_sink_without_approval_under_every_policy(tmp_path):
    config = AgentConfig(workspace_root=tmp_path, trace_path=tmp_path / "t.jsonl", session_id="s")
    captured = []

    class Capture:
        def append(self, event):
            captured.append(event)

    recorder = AgentTelemetryRecorder(config, sinks=[Capture()])
    for policy in ("auto", "per_step", "suggestion_only"):
        file_tools, command_tools = _mocks()
        sink = RecordingSink(ApprovalDecision("rejected"))
        registry = ToolRegistry(
            file_tools, command_tools, event_sink=sink, approval_policy=policy, telemetry=recorder
        )
        result = registry.execute(
            _call(
                "update_plan",
                {
                    "entries": [
                        {"content": "read code", "status": "completed", "priority": "high"},
                        {"content": "edit", "status": "in_progress"},
                    ]
                },
            ),
            run_id="r",
            request_id="q",
        )
        assert result["status"] == "ok" and result["entry_count"] == 2
        assert sink.requests == []
        assert sink.events == []
        assert sink.plans[-1].entries == (
            PlanEntrySpec("read code", "completed", "high"),
            PlanEntrySpec("edit", "in_progress", "medium"),
        )
    tool_events = [e for e in captured if e["event_type"] == "agent.tool.completed"]
    assert [e["payload"]["tool_name"] for e in tool_events] == ["update_plan"] * 3
    # The only other record is the approval policy's own decision: no one is asked.
    decisions = [e for e in captured if e["event_type"] == "agent.permission.decided"]
    assert [
        (e["payload"]["decision"], e["payload"]["decision_scope"]) for e in decisions
    ] == [("accepted", "policy")] * 3
    assert len(captured) == len(tool_events) + len(decisions)


@pytest.mark.parametrize("name", sorted(MUTATING_TOOLS))
def test_mutating_tools_prompt_under_per_step_and_are_denied_under_suggestion_only(name):
    file_tools, command_tools = _mocks()
    sink = RecordingSink(ApprovalDecision("accepted", "once"))
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink, approval_policy="per_step")
    registry.execute(_call(name), run_id="r", request_id="q")
    assert [request[0] for request in sink.requests] == [name]

    file_tools, command_tools = _mocks()
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink, approval_policy="suggestion_only")
    with pytest.raises(ToolRegistryError) as error:
        registry.execute(_call(name), run_id="r", request_id="q")
    assert error.value.failure_reason == "approval_policy_denied"
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    assert name not in {d["function"]["name"] for d in registry.definitions()}


@pytest.mark.parametrize("name", ["read_file", "list_files", "glob_files", "grep_files", "search_files"])
def test_read_only_tools_never_prompt(name):
    file_tools, command_tools = _mocks()
    sink = RecordingSink(ApprovalDecision("rejected"))
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink, approval_policy="per_step")

    registry.execute(_call(name), run_id="r", request_id="q")

    assert sink.requests == []


def test_definitions_never_exceed_frozen_profile_names():
    legacy = ["read_file", "write_file", "create_file", "replace_text", "list_files", "search_files", "run_command"]
    file_tools, command_tools = _mocks()
    registry = ToolRegistry(file_tools, command_tools, allowed_tools=frozenset(legacy))

    assert sorted(d["function"]["name"] for d in registry.definitions()) == sorted(legacy)
    assert registry.known_tool_names() == set(legacy)


def test_denied_calls_emit_started_and_failed_cards():
    file_tools, command_tools = _mocks()
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink, allowed_tools=frozenset({"read_file"}))

    with pytest.raises(ToolRegistryError) as error:
        registry.execute(_call("write_file"), run_id="r", request_id="q")

    assert error.value.failure_reason == "tool_not_allowed"
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    assert sink.events[-1].content_text.startswith("Not run:")
    file_tools.write_file.assert_not_called()


def test_run_command_forwards_timeout_and_cancel_event():
    file_tools, command_tools = _mocks()
    registry = ToolRegistry(file_tools, command_tools)
    from threading import Event

    cancel = Event()
    registry.execute(
        _call("run_command", {"argv": ["ls"], "timeout_seconds": 45}),
        run_id="r",
        request_id="q",
        cancellation_event=cancel,
    )

    kwargs = command_tools.run_command.call_args.kwargs
    assert kwargs["timeout_seconds"] == 45.0
    assert kwargs["cancel_event"] is cancel
    assert kwargs["argv"] == ["ls"]


def test_unparseable_raw_arguments_still_get_cards_and_a_field_error():
    """Card metadata is built from raw arguments before validation; it must never raise."""
    file_tools, command_tools = _mocks()
    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)

    with pytest.raises(ToolArgumentError) as error:
        registry.execute(
            _call("read_file", {"path": "a.txt", "offset": "abc", "limit": 5}),
            run_id="r",
            request_id="q",
        )

    assert error.value.field == "offset"
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    file_tools.read_file.assert_not_called()

    sink = RecordingSink()
    registry = ToolRegistry(file_tools, command_tools, event_sink=sink)
    with pytest.raises(ToolArgumentError):
        registry.execute(_call("edit_file", {"path": "a.txt", "edits": 7}), run_id="r", request_id="q")
    assert sink.events[-1].phase == "failed"
