from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest

from code4me2_agent.adapters import (
    FakeOpenAICompatibleProvider,
    OpenAICompatibleReactAdapter,
    ToolRegistry,
)
from code4me2_agent.command_tools import WorkspaceCommandTools
from code4me2_agent.config import AdapterConfig, AgentConfig, FakeProviderConfig
from code4me2_agent.events import ApprovalDecision
from code4me2_agent.file_tools import WorkspaceFileTools
from code4me2_agent.telemetry import AgentTelemetryRecorder

KNOWN_EVENT_TYPES = {
    "agent.run.started",
    "agent.run.completed",
    "agent.model.requested",
    "agent.model.completed",
    "agent.tool.called",
    "agent.tool.completed",
    "agent.tool.denied",
    "agent.tool.failed",
    "agent.permission.requested",
    "agent.permission.decided",
    "agent.adapter.loop_failed",
    "agent.adapter.parse_failed",
    "agent.request.received",
    "agent.response.completed",
    "agent.acp.backend_fallback",
}


class RecordingSink:
    def __init__(self, decision: ApprovalDecision | None = None) -> None:
        self.decision = decision or ApprovalDecision("accepted", "once")
        self.tool_events = []
        self.thoughts = []
        self.texts = []
        self.plans = []
        self.usages = []
        self.requests = []

    def tool_call(self, event):
        self.tool_events.append(event)

    def thought(self, event):
        self.thoughts.append(event)

    def assistant_text(self, event):
        self.texts.append(event)

    def plan(self, event):
        self.plans.append(event)

    def usage(self, event):
        self.usages.append(event)

    def request_approval(self, tool_call, arguments):
        self.requests.append(tool_call.name)
        return self.decision


def _tc(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


class Harness:
    def __init__(self, tmp_path, script, *, tools=None, approval="auto", max_iterations=8, file_tools=None):
        self.workspace = (tmp_path / "ws").resolve()
        self.workspace.mkdir(exist_ok=True)
        self.config = AgentConfig(
            workspace_root=self.workspace,
            trace_path=tmp_path / "trace.jsonl",
            session_id="session-1",
            tools=list(tools) if tools is not None else None,
            approval_policy=approval,
            adapter=AdapterConfig(
                name="openai_compatible_react",
                max_iterations=max_iterations,
                fake_provider=FakeProviderConfig(enabled=True, script=list(script)),
            ),
        )
        self.captured = []

        class Capture:
            def append(inner, event):
                self.captured.append(event)

        self.telemetry = AgentTelemetryRecorder(self.config, sinks=[Capture()])
        self.sink = RecordingSink()
        self.file_tools = file_tools or WorkspaceFileTools(self.config, telemetry=self.telemetry)
        self.command_tools = WorkspaceCommandTools(self.config, telemetry=self.telemetry)
        self.registry = ToolRegistry(
            self.file_tools,
            self.command_tools,
            allowed_tools=frozenset(tools) if tools is not None else None,
            approval_policy=approval,
            event_sink=self.sink,
            telemetry=self.telemetry,
            workspace_root=self.workspace,
        )
        self.adapter = OpenAICompatibleReactAdapter(
            self.config, telemetry=self.telemetry, tool_registry=self.registry, event_sink=self.sink
        )
        self.provider = FakeOpenAICompatibleProvider(list(script))
        self.adapter._provider = lambda: self.provider
        self.cancel = Event()

    def run(self, prompt="do the thing"):
        from code4me2_agent.adapters import MemoryWindow

        self.memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=32000)
        return self.adapter.handle_prompt(
            prompt=prompt,
            run_id="run-1",
            request_id="request-1",
            message_id=None,
            memory=self.memory,
            cancellation_event=self.cancel,
        )

    def tool_results(self, call_index: int) -> list[dict]:
        messages = self.provider.calls[call_index]["messages"]
        return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]

    def event_types(self) -> set[str]:
        return {event["event_type"] for event in self.captured}


def _assert_no_orphans(snapshot: list[dict]) -> None:
    index = 0
    while index < len(snapshot):
        message = snapshot[index]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            expected = [call["id"] for call in message["tool_calls"]]
            index += 1
            got = []
            while index < len(snapshot) and snapshot[index].get("role") == "tool":
                got.append(snapshot[index]["tool_call_id"])
                index += 1
            assert got == expected
            continue
        index += 1


def test_text_alongside_tool_calls_is_emitted_and_persisted(tmp_path):
    harness = Harness(
        tmp_path,
        [
            {"final_answer": "Reading the file first.", "tool_calls": [_tc("c1", "read_file", path="a.txt")]},
            {"final_answer": "Done."},
        ],
    )
    (harness.workspace / "a.txt").write_text("hello\n")

    result = harness.run()

    assert [(event.text, event.final) for event in harness.sink.texts] == [
        ("Reading the file first.", False),
        ("Done.", True),
    ]
    assert {event.message_id for event in harness.sink.texts} == {"request-1"}
    assert result.response_emitted is True
    assert result.stop_reason == "end_turn" and result.run_status == "completed"
    snapshot = harness.memory.snapshot()
    assistant = [m for m in snapshot if m["role"] == "assistant" and m.get("tool_calls")][0]
    assert assistant["content"] == "Reading the file first."
    assert harness.tool_results(1)[0]["status"] == "ok"
    assert harness.tool_results(1)[0]["content"] == "1|hello"
    assert [(e.phase, e.status) for e in harness.sink.tool_events] == [
        ("started", "in_progress"),
        ("completed", "completed"),
    ]


def test_tool_exception_becomes_error_result_and_loop_continues(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "read_file", path="missing.txt")]}, {"final_answer": "I could not find it."}],
    )

    result = harness.run()

    assert result.run_status == "completed"
    error = harness.tool_results(1)[0]
    assert error["status"] == "error"
    assert error["error_code"] == "file_not_found"
    assert "glob_files" in error["hint"]
    assert [(e.phase, e.status) for e in harness.sink.tool_events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    assert "File not found" in harness.sink.tool_events[-1].content_text
    assert "agent.tool.failed" in harness.event_types()
    assert harness.event_types() <= KNOWN_EVENT_TYPES


def test_replace_text_mismatch_is_recoverable(tmp_path):
    harness = Harness(
        tmp_path,
        [
            {"tool_calls": [_tc("c1", "replace_text", path="a.py", old_text="y = 2", new_text="y = 3")]},
            {"tool_calls": [_tc("c2", "replace_text", path="a.py", old_text="x = 1", new_text="x = 2")]},
            {"final_answer": "Fixed."},
        ],
    )
    (harness.workspace / "a.py").write_text("x = 1\n")

    result = harness.run()

    assert result.final_response == "Fixed."
    first = harness.tool_results(1)[0]
    assert first["status"] == "error" and first["error_code"] == "edit_no_match"
    assert "read_file" in first["hint"]
    second = harness.tool_results(2)[1]
    assert second["status"] == "ok" and second["replacements"] == 1
    assert (harness.workspace / "a.py").read_text() == "x = 2\n"


def test_malformed_tool_arguments_return_error_result_without_execution(tmp_path):
    file_tools = MagicMock()
    file_tools.workspace_root = Path("/ws")
    harness = Harness(
        tmp_path,
        [
            {"tool_calls": [{"id": "c1", "name": "read_file", "arguments": "{not json"}]},
            {"final_answer": "ok"},
        ],
        file_tools=file_tools,
    )

    harness.run()

    file_tools.read_file.assert_not_called()
    error = harness.tool_results(1)[0]
    assert error["status"] == "error" and error["error_code"] == "invalid_arguments"
    assert [(e.phase, e.status) for e in harness.sink.tool_events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]


def test_cancel_between_tool_calls_appends_synthetic_results(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "read_file", path="a.txt"), _tc("c2", "read_file", path="a.txt")]}],
    )
    (harness.workspace / "a.txt").write_text("hello\n")
    original = harness.file_tools.read_file

    def read_then_cancel(*args, **kwargs):
        harness.cancel.set()
        return original(*args, **kwargs)

    harness.file_tools.read_file = read_then_cancel

    result = harness.run()

    assert result.stop_reason == "cancelled" and result.run_status == "cancelled"
    snapshot = harness.memory.snapshot()
    _assert_no_orphans(snapshot)
    tool_messages = [m for m in snapshot if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["status"] == "ok"
    assert json.loads(tool_messages[1]["content"]) == {
        "error": "Cancelled by user before execution",
        "status": "cancelled",
        "tool_call_id": "c2",
        "tool_name": "read_file",
    }
    assert snapshot[-1] == {"role": "assistant", "content": "[Cancelled by the user before the turn completed.]"}
    assert harness.sink.texts == []


def test_final_iteration_forces_summary_with_tool_choice_none(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "list_files")]}, {"final_answer": "Here is what I did."}],
        max_iterations=2,
    )

    result = harness.run()

    assert harness.provider.calls[0]["tool_choice"] == "auto"
    assert harness.provider.calls[1]["tool_choice"] == "none"
    notice = harness.provider.calls[1]["messages"][-1]
    assert notice["role"] == "user" and "final model call" in notice["content"]
    assert all("final model call" not in m.get("content", "") for m in harness.memory.snapshot())
    assert result.stop_reason == "max_turn_requests" and result.run_status == "completed"


def test_final_iteration_without_prior_tools_is_end_turn(tmp_path):
    harness = Harness(tmp_path, [{"final_answer": "hi"}], max_iterations=1)

    result = harness.run("hello")

    assert result.stop_reason == "end_turn"
    assert harness.provider.calls[0]["tool_choice"] == "auto"


def test_tool_calls_on_the_last_step_still_run_and_get_a_summary(tmp_path):
    harness = Harness(tmp_path, [{"tool_calls": [_tc("c1", "create_file", path="new.txt", content="x")]}], max_iterations=1)

    result = harness.run()

    assert (harness.workspace / "new.txt").read_text() == "x"
    assert result.stop_reason == "max_turn_requests" and result.run_status == "completed"
    assert "step budget" in result.final_response
    assert "create_file: ok" in result.final_response
    assert harness.sink.texts[-1].final is True


def test_empty_response_is_retried_once_with_transient_nudge(tmp_path):
    harness = Harness(tmp_path, [{}, {"final_answer": "ok"}])

    result = harness.run()

    assert result.run_status == "completed"
    nudge = harness.provider.calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "empty" in nudge["content"]
    assert all("empty" not in m.get("content", "") for m in harness.memory.snapshot() if m["role"] == "user")


def test_repeated_empty_response_fails_with_error_text(tmp_path):
    harness = Harness(tmp_path, [{}, {}])

    result = harness.run()

    assert result.run_status == "failed" and result.stop_reason == "error"
    assert "empty" in harness.sink.texts[-1].text
    assert harness.memory.snapshot()[-1]["role"] == "assistant"
    assert "agent.adapter.loop_failed" in harness.event_types()


def test_update_plan_emits_plan_event_and_no_tool_card(tmp_path):
    harness = Harness(
        tmp_path,
        [
            {
                "tool_calls": [
                    _tc(
                        "c1",
                        "update_plan",
                        entries=[
                            {"content": "read", "status": "completed"},
                            {"content": "edit", "status": "in_progress", "priority": "high"},
                        ],
                    )
                ]
            },
            {"final_answer": "done"},
        ],
    )

    harness.run()

    assert [(e.content, e.status, e.priority) for e in harness.sink.plans[0].entries] == [
        ("read", "completed", "medium"),
        ("edit", "in_progress", "high"),
    ]
    assert harness.sink.tool_events == []
    assert harness.tool_results(1)[0]["status"] == "ok"


def test_denied_tool_emits_failed_card_and_denied_result_with_hint(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "write_file", path="a.txt", content="x")]}, {"final_answer": "cannot"}],
        tools=["read_file", "grep_files"],
    )

    harness.run()

    denied = harness.tool_results(1)[0]
    assert denied["status"] == "denied" and denied["reason"] == "tool_not_allowed"
    assert "grep_files" in denied["hint"] and "read_file" in denied["hint"]
    assert [(e.phase, e.status) for e in harness.sink.tool_events] == [
        ("started", "in_progress"),
        ("failed", "failed"),
    ]
    assert "agent.tool.denied" in harness.event_types()
    assert "agent.tool.failed" not in harness.event_types()


def test_suggestion_only_hides_write_tools_and_prompt_asks_for_diffs(tmp_path):
    harness = Harness(tmp_path, [{"final_answer": "proposal"}], approval="suggestion_only")

    names = {d["function"]["name"] for d in harness.registry.definitions()}
    assert "write_file" not in names and "run_command" not in names
    assert {"read_file", "grep_files", "glob_files", "update_plan"} <= names
    context = harness.adapter._system_context()
    assert "suggestion-only" in context and "unified diff" in context

    harness.run()
    assert harness.provider.calls[0]["messages"][0]["content"] == context


def test_usage_event_only_for_provider_reported_usage(tmp_path):
    reported = Harness(
        tmp_path,
        [{"final_answer": "x", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}],
    )
    result = reported.run()
    assert len(reported.sink.usages) == 1
    assert reported.sink.usages[0].context_budget_tokens == 32000
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    estimated = Harness(tmp_path, [{"final_answer": "x"}])
    result = estimated.run()
    assert estimated.sink.usages == []
    assert result.usage is None


def test_thought_emitted_only_when_reasoning_present(tmp_path):
    with_reasoning = Harness(tmp_path, [{"final_answer": "x", "reasoning": "think hard"}])
    result = with_reasoning.run()
    assert [event.text for event in with_reasoning.sink.thoughts] == ["think hard"]
    assert result.thoughts == ("think hard",)

    without = Harness(tmp_path, [{"final_answer": "x"}])
    without.run()
    assert without.sink.thoughts == []


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [("length", "max_tokens"), ("content_filter", "refusal"), ("stop", "end_turn"), (None, "end_turn")],
)
def test_finish_reason_maps_to_stop_reason(tmp_path, finish_reason, expected):
    harness = Harness(tmp_path, [{"final_answer": "x", "finish_reason": finish_reason}])

    assert harness.run().stop_reason == expected


def test_failure_text_is_emitted_and_persisted(tmp_path):
    harness = Harness(tmp_path, [])

    result = harness.run()

    assert result.run_status == "failed" and result.stop_reason == "provider_exhausted"
    assert result.response_emitted is True
    assert "fake provider" in harness.sink.texts[-1].text
    assert harness.memory.snapshot()[-1] == {"role": "assistant", "content": result.final_response}


def test_per_step_rejection_result_asks_the_model_not_to_retry(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "delete_file", path="a.txt")]}, {"final_answer": "No change was made."}],
        approval="per_step",
    )
    harness.sink.decision = ApprovalDecision("rejected")
    (harness.workspace / "a.txt").write_text("keep me")

    result = harness.run()

    assert (harness.workspace / "a.txt").exists()
    rejected = harness.tool_results(1)[0]
    assert rejected["status"] == "rejected" and rejected["reason"] == "user_rejected"
    assert "Do not retry" in rejected["message"]
    assert result.final_response == "No change was made."


def test_prompt_lists_only_session_tools_and_budget(tmp_path):
    harness = Harness(tmp_path, [{"final_answer": "x"}], tools=["read_file", "run_command"], max_iterations=5)
    config = replace(harness.config, commands=replace(harness.config.commands, allowlisted_commands=["git"]))
    harness.adapter._config = config

    context = harness.adapter._system_context()

    assert "Tools available in this session: read_file, run_command." in context
    assert "at most 5 model calls" in context
    assert "Allowlisted executables: git" in context or "Commands cannot be run" in context


def test_preview_denial_is_attributed_to_the_editing_tool_and_counted_once(tmp_path):
    (tmp_path / "secret.txt").write_text("private")
    harness = Harness(
        tmp_path,
        [
            {"tool_calls": [_tc("c1", "replace_text", path="../secret.txt", old_text="private", new_text="x")]},
            {"final_answer": "I cannot touch files outside the workspace."},
        ],
    )

    harness.run()

    denied = harness.tool_results(1)[0]
    assert denied["status"] == "denied" and denied["reason"] == "workspace_policy"
    denial_events = [e for e in harness.captured if e["event_type"] == "agent.tool.denied"]
    assert [(e["payload"]["tool_name"], e["payload"]["tool_call_id"]) for e in denial_events] == [
        ("replace_text", "c1")
    ]
    assert (tmp_path / "secret.txt").read_text() == "private"


def test_large_tool_batches_are_capped_relative_to_the_context_budget(tmp_path):
    from code4me2_agent.adapters import MemoryWindow, _batch_result_cap

    calls = [_tc(f"c{n}", "read_file", path="big.txt") for n in range(4)]
    harness = Harness(tmp_path, [{"tool_calls": calls}, {"final_answer": "done"}])
    (harness.workspace / "big.txt").write_text("".join(f"line {n} {'x' * 60}\n" for n in range(400)))

    memory = MemoryWindow(strategy="token_window", max_messages=50, max_tokens=2000)
    harness.adapter.handle_prompt(
        prompt="read it four times",
        run_id="run-1",
        request_id="request-1",
        message_id=None,
        memory=memory,
        cancellation_event=harness.cancel,
    )

    cap = _batch_result_cap(2000, 4)
    assert cap == 4000
    tool_messages = [m for m in memory.snapshot() if m["role"] == "tool"]
    assert len(tool_messages) == 4
    for message in tool_messages:
        assert len(message["content"]) <= cap + 300
        assert json.loads(message["content"])["truncated"] is True
    # The follow-up request stayed within the budget the profile allows.
    request = harness.provider.calls[1]["messages"]
    from code4me2_agent.adapters import _estimate_tokens

    assert sum(_estimate_tokens(m) for m in request) <= 2000 + 400


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX file modes")
def test_os_permission_errors_are_recorded_as_failures_not_policy_denials(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "write_file", path="ro.txt", content="new")]}, {"final_answer": "blocked"}],
    )
    target = harness.workspace / "ro.txt"
    target.write_text("old")
    target.chmod(0o400)
    try:
        harness.run()
    finally:
        target.chmod(0o600)

    result = harness.tool_results(1)[0]
    assert result["status"] == "error" and result["error_code"] == "permission_denied"
    failures = [e for e in harness.captured if e["event_type"] == "agent.tool.failed"]
    assert [e["payload"]["failure_reason"] for e in failures] == ["permission_denied"]
    assert not [e for e in harness.captured if e["event_type"] == "agent.tool.denied"]


def test_delete_outside_the_workspace_is_denied_exactly_once(tmp_path):
    (tmp_path / "secret.txt").write_text("private")
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "delete_file", path="../secret.txt")]}, {"final_answer": "no"}],
    )

    harness.run()

    denied = harness.tool_results(1)[0]
    assert denied["status"] == "denied" and denied["reason"] == "workspace_policy"
    denial_events = [e for e in harness.captured if e["event_type"] == "agent.tool.denied"]
    assert [e["payload"]["tool_name"] for e in denial_events] == ["delete_file"]
    assert (tmp_path / "secret.txt").exists()


def test_capped_read_results_keep_paging_fields_honest():
    from code4me2_agent.adapters import _cap_tool_result

    content = "\n".join(f"{n}|{'x' * 50}" for n in range(1, 401))
    result = {
        "status": "ok",
        "tool_name": "read_file",
        "tool_call_id": "c" * 300,
        "path": "big.txt",
        "content": content,
        "start_line": 1,
        "end_line": 400,
        "total_lines": 400,
        "truncated": False,
        "next_offset": None,
    }

    capped = _cap_tool_result(result, 4000)

    assert capped["tool_call_id"] == "c" * 300
    assert capped["truncated"] is True and capped["truncation_reason"] == "context_budget"
    last_line = capped["end_line"]
    assert 1 < last_line < 400
    assert capped["next_offset"] == last_line + 1
    body, marker = capped["content"].rsplit("\n", 1)
    assert body.splitlines()[-1].startswith(f"{last_line}|")
    assert f"offset={last_line + 1}" in marker
    assert len(json.dumps(capped, sort_keys=True)) <= 4000 + 400


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX file modes")
def test_unreadable_but_deletable_file_can_still_be_deleted(tmp_path):
    harness = Harness(
        tmp_path,
        [{"tool_calls": [_tc("c1", "delete_file", path="locked.txt")]}, {"final_answer": "removed"}],
    )
    target = harness.workspace / "locked.txt"
    target.write_text("x")
    target.chmod(0o000)
    try:
        harness.run()
    finally:
        if target.exists():
            target.chmod(0o600)

    assert not target.exists()
    assert harness.tool_results(1)[0]["status"] == "ok"
