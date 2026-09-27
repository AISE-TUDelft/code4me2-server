"""Regression tests for the independent review of run 2026-09-26-agent-harness-tiers."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_harness_loop import PYTHON, Loop, _edit_script, _tc

from code4me2_agent.acp_runtime import _replay_updates
from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.command_tools import WorkspaceCommandTools
from code4me2_agent.config import (
    AgentConfig,
    CommandConfig,
    HarnessOptions,
    parse_harness_overrides,
)
from code4me2_agent.patching import apply_update, parse_patch
from code4me2_agent.runner_output import summarize_test_output
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import CommandNotFoundError

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


# 1 ------------------------------------------------ stale verification claim


def test_a_later_edit_voids_an_earlier_verification_pass(tmp_path):
    check = "import sys; sys.exit(0 if open('a.py').read() == 'x = 2\\n' else 1)"
    script = [
        *_edit_script("Set x to 2."),
        {"final_answer": "- a.py: the request asked for x = 3"},  # review raises an issue
        {"tool_calls": [_tc("f", "replace_text", path="a.py", old_text="x = 2", new_text="x = 3")]},
        {"final_answer": "Set x to 3."},  # final round: no budget left to verify again
    ]
    loop = Loop(
        tmp_path,
        script,
        max_iterations=6,
        harness=HarnessOptions(verify_command=(PYTHON, "-c", check), instruction_reminders=False),
        commands=[PYTHON],
    )
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run("set x")

    assert (loop.workspace / "a.py").read_text() == "x = 3\n"
    assert "Verified by the runtime" not in result.final_response


# 2 ------------------------------------------- slash commands and arm blindness


def test_managed_status_reveals_nothing_about_the_arm(tmp_path):
    loop = Loop(tmp_path, [], model="openai/gpt-5.1-codex", commands=[PYTHON])
    loop.adapter._config = replace(loop.config, managed_mode=True)

    status = loop.run("/status").final_response

    assert "Context: about" in status and "Undo checkpoints: 0" in status
    for secret in ("gpt-5.1-codex", "openai", "prompt profile", PYTHON, "self_review", "budget"):
        assert secret not in status


def test_disabled_behaviours_cannot_be_triggered_by_slash_commands(tmp_path):
    loop = Loop(
        tmp_path,
        [*_edit_script()],
        harness=HarnessOptions(self_review=False, context_summarization=False, verify_on_stop=False),
    )
    (loop.workspace / "a.py").write_text("x = 1\n")
    loop.run("change it")
    calls = len(loop.provider.calls)

    review = loop.run("/review", run_id="run-2")
    compact = loop.run("/compact", run_id="run-3")

    assert review.final_response == "Reviews are not available in this session."
    assert compact.final_response == "Compacting the conversation is not available in this session."
    assert len(loop.provider.calls) == calls


def test_status_verification_wording_follows_the_switches(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = Loop(tmp_path / "off", [], harness=HarnessOptions(verify_on_stop=False)).run("/status")
    assert "- Verification: off" in off.final_response
    on = Loop(tmp_path / "on", [], harness=HarnessOptions(verify_command=("pytest",))).run("/status")
    assert "the runtime runs pytest after changes" in on.final_response


# 3 ---------------------------------------------- test summaries: real tests only


def test_non_test_commands_get_no_summary():
    gradle_tasks = "> Task :dependencies\n" + "".join(f"+--- lib{n}\n" for n in range(60)) + "BUILD SUCCESSFUL in 2s\n"
    assert summarize_test_output(["./gradlew", "dependencies"], gradle_tasks, "") is None
    assert summarize_test_output(["go", "run", "main.go"], "ok done\n", "") is None
    assert summarize_test_output(["go", "build", "./..."], "ok  \texample.com/m\t0.1s\n", "") is None
    assert summarize_test_output(["go", "test", "./..."], "ok  \texample.com/m\t0.1s\n", "") is not None


@POSIX_ONLY
def test_successful_non_test_output_is_not_truncated(tmp_path):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    wrapper = workspace / "gradlew"
    wrapper.write_text(
        f"#!{sys.executable}\nfor n in range(62):\n    print('+--- lib', n)\nprint('BUILD SUCCESSFUL in 1s')\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    config = AgentConfig(
        workspace_root=workspace,
        trace_path=tmp_path / "t.jsonl",
        session_id="s",
        commands=CommandConfig(allowlisted_commands=["gradlew"]),
    )
    tools = WorkspaceCommandTools(config, telemetry=AgentTelemetryRecorder(config, sinks=[]))

    result = tools.run_command(["./gradlew", "dependencies"], timeout_seconds=30)

    assert result.test_summary is None
    assert "+--- lib 0" in result.stdout and "+--- lib 61" in result.stdout


# 4 -------------------------------------------------- apply_patch pure additions


def test_pure_additions_append_or_follow_their_anchor():
    text = "import os\n\n\ndef a():\n    return 1\n"
    appended = parse_patch("*** Begin Patch\n*** Update File: m.py\n@@\n+\n+\n+def b():\n+    return 2\n*** End Patch")
    assert apply_update(text, appended[0].chunks, path="m.py")[0] == text + "\n\ndef b():\n    return 2\n"
    anchored = parse_patch("*** Begin Patch\n*** Update File: m.py\n@@ import os\n+import sys\n*** End Patch")
    assert apply_update(text, anchored[0].chunks, path="m.py")[0].startswith("import os\nimport sys\n")


# 5 ------------------------------------------------------ batch-file wrappers


def test_batch_wrappers_refuse_cmd_metacharacters(tmp_path):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "gradlew.bat").write_text("@echo off\r\n")
    events: list[dict] = []

    class Capture:
        def append(self, event):
            events.append(event)

    config = AgentConfig(
        workspace_root=workspace,
        trace_path=tmp_path / "t.jsonl",
        session_id="s",
        commands=CommandConfig(allowlisted_commands=["gradlew.bat"]),
    )
    tools = WorkspaceCommandTools(config, telemetry=AgentTelemetryRecorder(config, sinks=[Capture()]))

    for argument in ("test & calc", "%PATH%", 'a"b', "x|y"):
        with pytest.raises(PermissionError, match="batch file"):
            tools.run_command(["./gradlew.bat", argument], timeout_seconds=5)
    assert {e["payload"]["denial_reason"] for e in events} == {"unsafe_batch_arguments"}


def test_a_missing_wrapper_is_one_failure_not_a_denial(tmp_path):
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc("w", "run_command", argv=["./gradlew", "test"])]}, {"final_answer": "no wrapper"}],
        commands=["gradlew"],
    )
    loop.run()
    terminal = [
        e["event_type"]
        for e in loop.events
        if e["event_type"] in ("agent.tool.denied", "agent.tool.failed", "agent.tool.completed")
        and e["payload"].get("tool_call_id") == "w"
    ]
    assert terminal == ["agent.tool.failed"]
    with pytest.raises(CommandNotFoundError):
        loop.command_tools.run_command(["./gradlew"], timeout_seconds=5)


# 6 ----------------------------------------------------- forced final round


def test_calls_returned_after_the_loop_stop_are_never_run(tmp_path):
    read = lambda n: {"tool_calls": [_tc(f"r{n}", "list_files", path=".")]}  # noqa: E731
    loop = Loop(
        tmp_path,
        [read(1), read(2), read(3), read(4), read(5), read(6)],  # ignores tool_choice="none"
        harness=HarnessOptions(instruction_reminders=False),
    )

    result = loop.run()

    listed = [e for e in loop.events if e["event_type"] == "agent.tool.called"]
    assert len(listed) == 5
    assert result.stop_reason == "loop_detected"
    assert "same step kept repeating" in result.final_response
    last_result = json.loads([m for m in loop.memory.snapshot() if m["role"] == "tool"][-1]["content"])
    assert last_result["status"] == "skipped"


# 7 ------------------------------------------------ apply_patch atomicity


def _patch_loop(tmp_path, patch):
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("r", "read_file", path="x.py"), _tc("r2", "read_file", path="y.py")]},
            {"tool_calls": [_tc("p", "apply_patch", patch=patch)]},
            {"final_answer": "done"},
        ],
        harness=HarnessOptions(self_review=False, verify_on_stop=False),
    )
    (loop.workspace / "x.py").write_text("a = 1\n")
    (loop.workspace / "y.py").write_text("b = 1\n")
    return loop


def test_the_same_file_in_two_spellings_is_refused(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: ./x.py\n@@\n-a = 1\n+a = 3\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    loop.run()
    result = loop.tool_results(2)[-1]
    assert result["status"] == "error" and "same file" in result["error"]
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"


def test_a_failing_write_restores_the_files_already_written(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-b = 1\n+b = 2\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    original = loop.file_tools._write_text
    writes = {"count": 0}

    def failing_write(path, content, **kwargs):
        if kwargs.get("tool_name") == "apply_patch" and path.name == "y.py":
            raise OSError("disk full")
        writes["count"] += 1
        return original(path, content, **kwargs)

    loop.file_tools._write_text = failing_write
    loop.run()

    result = loop.tool_results(2)[-1]
    assert result["status"] == "error" and result["error_code"] == "patch_write_failed"
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"
    assert loop.state.last_turn() is None  # nothing recorded as changed


def test_a_patch_outside_the_workspace_is_recorded_as_denied(tmp_path):
    patch = "*** Begin Patch\n*** Add File: ../escape.py\n+x = 1\n*** End Patch"
    loop = Loop(tmp_path, [{"tool_calls": [_tc("p", "apply_patch", patch=patch)]}, {"final_answer": "no"}])
    loop.run()
    denied = [e for e in loop.events if e["event_type"] == "agent.tool.denied"]
    assert denied and denied[0]["payload"]["tool_call_id"] == "p"
    assert not (tmp_path / "escape.py").exists()


# 8 --------------------------------------------------------------- telemetry


def test_call_purpose_is_on_requested_and_completed_and_side_calls_skip_usage(tmp_path):
    usages: list = []
    loop = Loop(
        tmp_path,
        [*_edit_script(), {"final_answer": "NO_ISSUES", "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}],
        harness=HarnessOptions(verify_on_stop=False),
    )
    loop.sink.usage = usages.append
    for step in loop.provider._script[:3]:
        step["usage"] = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    (loop.workspace / "a.py").write_text("x = 1\n")

    loop.run()

    completed = [e["payload"]["call_purpose"] for e in loop.events if e["event_type"] == "agent.model.completed"]
    assert completed == ["turn", "turn", "turn", "self_review"]
    assert len(usages) == 3  # the review call does not move the IDE's context meter


# 9 ---------------------------------------------------- verify_command parity


def test_whitespace_arguments_are_accepted_like_the_server_does():
    assert parse_harness_overrides({"verify_command": ["pytest", "-k", " "]}, strict=True) == {
        "verify_command": ("pytest", "-k", " ")
    }
    with pytest.raises(ValueError):
        parse_harness_overrides({"verify_command": [" ", "-q"]}, strict=True)


# 10 ---------------------------------------------------------------- replay


def test_replay_hides_provisional_answers_and_shows_undo(tmp_path):
    messages = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "Done (not shown).", "code4me_runtime": "provisional"},
        {"role": "user", "content": "[Self-review ...]", "code4me_runtime": "review"},
        {"role": "assistant", "content": "Fixed after review."},
        {"role": "user", "content": "/undo"},
        {"role": "assistant", "content": "Undid the file changes of the last turn that changed files."},
    ]
    updates = _replay_updates(messages, updates=AcpUpdateBuilder(), workspace_root=tmp_path)
    shown = [(u.session_update, u.content.text) for u in updates]
    assert shown == [
        ("user_message_chunk", "fix it"),
        ("agent_message_chunk", "Fixed after review."),
        ("user_message_chunk", "/undo"),
        ("agent_message_chunk", "Undid the file changes of the last turn that changed files."),
    ]


# gap -------------------------------------------- cancellation in parallel mode


def test_cancelling_a_parallel_batch_keeps_every_call_paired(tmp_path):
    loop = Loop(
        tmp_path,
        [{"tool_calls": [_tc(f"r{n}", "read_file", path="a.txt") for n in range(3)] + [_tc("g", "glob_files", pattern="*.txt")]}],
    )
    (loop.workspace / "a.txt").write_text("hello\n")
    original = loop.file_tools.read_file

    def read_then_cancel(*args, **kwargs):
        loop.cancel.set()
        return original(*args, **kwargs)

    loop.file_tools.read_file = read_then_cancel
    result = loop.run()

    assert result.stop_reason == "cancelled"
    snapshot = loop.memory.snapshot()
    calls = [c["id"] for m in snapshot if m.get("tool_calls") for c in m["tool_calls"]]
    results = [m["tool_call_id"] for m in snapshot if m["role"] == "tool"]
    assert calls == results == ["r0", "r1", "r2", "g"]
    assert all(json.loads(m["content"])["status"] in ("ok", "cancelled") for m in snapshot if m["role"] == "tool")



# ------------------------------------------------ re-review of the repair (round 2)


def test_batch_files_found_on_path_are_guarded_too(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    batch = bin_dir / "gradlew.bat"
    batch.write_text("@echo off\r\n")
    batch.chmod(batch.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    config = AgentConfig(
        workspace_root=workspace,
        trace_path=tmp_path / "t.jsonl",
        session_id="s",
        commands=CommandConfig(allowlisted_commands=["gradlew.bat", "npm.cmd"]),
    )
    tools = WorkspaceCommandTools(config, telemetry=AgentTelemetryRecorder(config, sinks=[]))
    with pytest.raises(PermissionError, match="batch file"):
        tools.run_command(["gradlew.bat", "test&calc"], timeout_seconds=5)
    with pytest.raises(PermissionError, match="batch file"):
        tools.run_command(["npm.cmd", "run", "a|b"], timeout_seconds=5)


def test_a_partially_written_file_is_restored(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-b = 1\n+b = 2\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    original = loop.file_tools._write_text
    failed = []

    def truncating_write(path, content, **kwargs):
        if kwargs.get("tool_name") == "apply_patch" and path.name == "y.py" and not failed:
            failed.append(path)
            path.write_text("b")  # truncated mid-write
            raise OSError("disk full")
        return original(path, content, **kwargs)

    loop.file_tools._write_text = truncating_write
    loop.run()
    assert loop.tool_results(2)[-1]["error_code"] == "patch_write_failed"
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"
    assert (loop.workspace / "y.py").read_text() == "b = 1\n"


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


def test_names_differing_only_in_case_are_the_same_file(tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("x.py and X.py are different files on a case-sensitive filesystem")
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: X.py\n@@\n-a = 1\n+a = 3\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    # force: reach the same-file check even where X.py was never read as such
    loop.provider._script[1]["tool_calls"][0]["arguments"]["force"] = True
    loop.run()
    result = loop.tool_results(2)[-1]
    assert result["status"] == "error" and "same file" in result["error"]
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"


def test_later_chunks_still_apply_after_an_end_of_file_addition():
    text = "import os\n\ndef a():\n    return 1\n"
    actions = parse_patch(
        "*** Begin Patch\n*** Update File: m.py\n@@\n+\n+def b():\n+    return 2\n"
        "@@ def a():\n-    return 1\n+    return 10\n@@\n+\n+def c():\n+    return 3\n*** End Patch"
    )
    new_text, _fuzz = apply_update(text, actions[0].chunks, path="m.py")
    assert new_text == (
        "import os\n\ndef a():\n    return 10\n\ndef b():\n    return 2\n\ndef c():\n    return 3\n"
    )


def test_study_sessions_only_offer_status_and_undo(tmp_path):
    from code4me2_agent import slash_commands

    assert [c.name for c in slash_commands.available(managed=True)] == ["status", "undo"]
    loop = Loop(tmp_path, [{"final_answer": "Here is my own review."}])
    loop.adapter._config = replace(loop.config, managed_mode=True)
    result = loop.run("/review error handling")
    # An ordinary request in every arm: the model answers, no runtime review.
    assert result.final_response == "Here is my own review."
    assert loop.provider.calls[0]["include_tools"] is True
    assert loop.provider.calls[0]["messages"][-1]["content"] == "/review error handling"


def test_ide_refactorings_count_as_changes(tmp_path):
    from unittest.mock import MagicMock

    from code4me2_agent.adapters import ToolCall, _TurnState

    loop = Loop(tmp_path, [])
    broker = MagicMock()
    broker.tool_access.side_effect = lambda name: "edit" if "rename" in name else "execute"
    loop.registry._mcp_tools = broker
    state = _TurnState(verification_note="Verified by the runtime: `pytest` passed.")

    loop.adapter._note_step(state, ToolCall("m", "mcp__idea__rename_refactoring", {}), {"status": "ok"})
    assert state.external_changes and state.last_change_step == 1 and state.verification_note is None
    loop.adapter._note_step(state, ToolCall("b", "mcp__idea__build_project", {}), {"status": "ok"})
    # An IDE build is listed for the reviewer but is no substitute for the
    # profile's verification command (round-3 review).
    assert state.last_ide_run_step == 2 and state.last_command_step == 0
    assert state.commands_run == ["IDE: build_project"]


def test_replay_after_a_loop_stop_shows_no_phantom_card(tmp_path):
    read = lambda n: {"tool_calls": [_tc(f"r{n}", "list_files", path=".")]}  # noqa: E731
    loop = Loop(
        tmp_path,
        [read(1), read(2), read(3), read(4), read(5), {"text": "Still trying.", **read(6)}],
        harness=HarnessOptions(instruction_reminders=False),
    )
    result = loop.run()
    updates = _replay_updates(loop.memory.snapshot(), updates=AcpUpdateBuilder(), workspace_root=loop.workspace)
    cards = [u for u in updates if u.session_update == "tool_call"]
    assert len(cards) == 5 and all(card.status == "completed" for card in cards)
    texts = [u.content.text for u in updates if u.session_update == "agent_message_chunk"]
    assert texts.count(result.final_response) == 1
    assert result.final_response.startswith("Still trying.")



# ------------------------------------------------------------- round-3 review


def test_rollback_does_not_report_files_it_never_changed(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-b = 1\n+b = 2\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    original = loop.file_tools._write_text

    def refusing_write(path, content, **kwargs):
        if path.name == "y.py":
            raise PermissionError("read-only file")  # fails before touching it, every time
        return original(path, content, **kwargs)

    loop.file_tools._write_text = refusing_write
    loop.run()
    result = loop.tool_results(2)[-1]
    assert "the files already changed were restored" in result["error"]
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"


def test_new_files_differing_only_in_case_are_refused_everywhere(tmp_path):
    from code4me2_agent.patching import parse_patch as parse

    loop = _patch_loop(tmp_path, "*** Begin Patch\n*** Delete File: x.py\n*** End Patch")
    actions = parse(
        "*** Begin Patch\n*** Add File: notes/README.md\n+a\n*** Add File: notes/readme.md\n+b\n*** End Patch"
    )
    # On a case-insensitive filesystem the second add would silently replace the first.
    with pytest.raises(Exception, match="same file"):
        loop.file_tools.plan_patch(actions)


def test_existing_case_variants_are_separate_files_where_the_filesystem_allows_them(tmp_path):
    if _case_insensitive(tmp_path):
        pytest.skip("x.py and X.py are one file on a case-insensitive filesystem")
    from code4me2_agent.patching import parse_patch as parse

    loop = _patch_loop(tmp_path, "*** Begin Patch\n*** Delete File: x.py\n*** End Patch")
    (loop.workspace / "X.py").write_text("a = 1\n")
    actions = parse(
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: X.py\n@@\n-a = 1\n+a = 3\n*** End Patch"
    )
    assert len(loop.file_tools.plan_patch(actions)) == 2


def test_an_ide_build_does_not_replace_the_configured_verification(tmp_path):
    from unittest.mock import MagicMock

    check = "import sys; sys.exit(0)"
    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("r", "read_file", path="a.py")]},
            {"tool_calls": [_tc("e", "replace_text", path="a.py", old_text="x = 1", new_text="x = 2")]},
            {"tool_calls": [_tc("b", "mcp__idea__build_project")]},
            {"final_answer": "Built fine."},
        ],
        harness=HarnessOptions(self_review=False, verify_command=(PYTHON, "-c", check)),
        commands=[PYTHON],
    )
    broker = MagicMock()
    broker.definitions.return_value = [
        {"type": "function", "function": {"name": "mcp__idea__build_project", "parameters": {"type": "object"}}}
    ]
    broker.has_tool.return_value = True
    broker.tool_access.return_value = "execute"
    broker.execute.return_value = {"status": "completed", "content": []}
    loop.registry._mcp_tools = broker
    (loop.workspace / "a.py").write_text("x = 1\n")

    result = loop.run()

    assert "Verified by the runtime" in result.final_response
    assert any(e.tool_call_id.startswith("verify-") for e in loop.sink.tool_events)


def test_ide_only_changes_still_reach_the_stop_gate(tmp_path):
    from unittest.mock import MagicMock

    loop = Loop(
        tmp_path,
        [
            {"tool_calls": [_tc("m", "mcp__idea__rename_refactoring", symbol="old", newName="new")]},
            {"final_answer": "Renamed."},
            {"final_answer": "Renamed; run the tests to check."},
        ],
        harness=HarnessOptions(self_review=False, instruction_reminders=False),
        commands=[PYTHON],
    )
    broker = MagicMock()
    broker.definitions.return_value = [
        {"type": "function", "function": {"name": "mcp__idea__rename_refactoring", "parameters": {"type": "object"}}}
    ]
    broker.has_tool.return_value = True
    broker.tool_access.return_value = "edit"
    broker.execute.return_value = {"status": "completed", "content": []}
    loop.registry._mcp_tools = broker

    result = loop.run()

    assert "files through IDE tools" in loop.request_text(2)
    assert result.final_response == "Renamed; run the tests to check."


def test_replay_pairs_results_per_step_even_with_repeated_ids(tmp_path):
    def step(result_status):
        return [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tool-call-1", "name": "list_files", "arguments": {"path": "."}}]},
            {"role": "tool", "tool_call_id": "tool-call-1", "content": json.dumps({"status": result_status, "reason": "nope"})},
        ]

    messages = [{"role": "user", "content": "go"}, *step("ok"), *step("denied")]
    cards = [
        u for u in _replay_updates(messages, updates=AcpUpdateBuilder(), workspace_root=tmp_path)
        if u.session_update == "tool_call"
    ]
    assert [c.status for c in cards] == ["completed", "failed"]
    assert [c.tool_call_id for c in cards] == ["tool-call-1", "tool-call-1~2"]


def test_study_sessions_announce_only_status_and_undo(tmp_path):
    import asyncio

    from code4me2_agent.acp_runtime import create_acp_agent

    class Client:
        def __init__(self):
            self.updates = []

        async def session_update(self, *, session_id, update, **kwargs):
            self.updates.append(update)

    class Authorization:
        is_authenticated = True
        server_agent_config = None
        backend_url = None

    config = AgentConfig(workspace_root=tmp_path, trace_path=tmp_path / "t", session_id="s", managed_mode=True)
    agent = create_acp_agent(config, authorization=Authorization())
    client = Client()
    agent.on_connect(client)

    async def scenario():
        agent._announce_commands_soon("session-1")
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert [c.name for c in client.updates[0].available_commands] == ["status", "undo"]



# ---------------------------------------------------------- final narrow review


def test_hard_links_are_the_same_file(tmp_path):
    patch = (
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-a = 1\n+a = 3\n*** End Patch"
    )
    loop = _patch_loop(tmp_path, patch)
    (loop.workspace / "y.py").unlink()
    os.link(loop.workspace / "x.py", loop.workspace / "y.py")
    loop.run()
    result = loop.tool_results(2)[-1]
    assert result["status"] == "error" and "same file" in result["error"]
    assert (loop.workspace / "x.py").read_text() == "a = 1\n"


def test_filesystems_without_inode_numbers_fall_back_to_paths(tmp_path, monkeypatch):
    from code4me2_agent.patching import parse_patch as parse

    loop = _patch_loop(tmp_path, "*** Begin Patch\n*** Delete File: x.py\n*** End Patch")
    real_lstat = os.lstat

    def no_inodes(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return os.stat_result((info.st_mode, 0, *tuple(info)[2:]))

    monkeypatch.setattr(os, "lstat", no_inodes)
    actions = parse(
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-b = 1\n+b = 2\n*** End Patch"
    )
    assert len(loop.file_tools.plan_patch(actions)) == 2


def test_a_failed_step_is_skipped_only_when_disk_and_ide_are_both_unchanged(tmp_path):
    from code4me2_agent.file_tools import WorkspaceFileTools

    class Ide:
        read_text_file_enabled = True
        write_text_file_enabled = True
        session_id = "s"

        def __init__(self, buffers):
            self.buffers = buffers

        def read_text_file(self, path):
            return self.buffers.get(path, Path(path).read_text())

        def write_text_file(self, path, content):
            self.buffers[path] = content

    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    target = workspace / "y.py"
    target.write_bytes(b"b")  # the local fallback truncated it (exact bytes: no CRLF on Windows)
    config = AgentConfig(workspace_root=workspace, trace_path=tmp_path / "t.jsonl", session_id="s")
    tools = WorkspaceFileTools(
        config,
        acp_backend=Ide({str(target): "b = 1\n"}),  # the IDE still shows the original
        telemetry=AgentTelemetryRecorder(config, sinks=[]),
    )
    assert tools._holds_original(target, "b = 1\n") is False
    target.write_bytes(b"b = 1\n")
    assert tools._holds_original(target, "b = 1\n") is True


def test_local_writes_keep_line_endings_exactly(tmp_path, monkeypatch):
    from code4me2_agent.file_tools import WorkspaceFileTools

    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    config = AgentConfig(workspace_root=workspace, trace_path=tmp_path / "t.jsonl", session_id="s")
    tools = WorkspaceFileTools(config, telemetry=AgentTelemetryRecorder(config, sinks=[]))
    text_mode_writes: list = []
    real_write_text = Path.write_text

    def spy(self, data, *args, **kwargs):
        if kwargs.get("newline") != "":
            text_mode_writes.append(self)
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    tools.write_file("crlf.txt", "a\r\nb\r\n")
    # Text mode on Windows would turn every "\n" into "\r\n" (a doubled CR): the
    # byte check catches that on Windows, the spy on every platform.
    assert text_mode_writes == []
    assert (workspace / "crlf.txt").read_bytes() == b"a\r\nb\r\n"


def test_rollback_restores_what_the_ide_buffer_holds(tmp_path):
    from code4me2_agent.file_tools import WorkspaceFileTools

    class BufferedIde:
        """An ACP client whose writes stay in editor buffers (never saved to disk)."""

        read_text_file_enabled = True
        write_text_file_enabled = True
        session_id = "s"

        def __init__(self):
            self.buffers: dict[str, str] = {}

        def read_text_file(self, path):
            if path in self.buffers:
                return self.buffers[path]
            return Path(path).read_text()

        def write_text_file(self, path, content):
            self.buffers[path] = content

    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "x.py").write_text("a = 1\n")
    (workspace / "y.py").write_text("b = 1\n")
    config = AgentConfig(workspace_root=workspace, trace_path=tmp_path / "t.jsonl", session_id="s")
    ide = BufferedIde()
    tools = WorkspaceFileTools(config, acp_backend=ide, telemetry=AgentTelemetryRecorder(config, sinks=[]))
    original = tools._write_text

    def failing_write(path, content, **kwargs):
        if path.name == "y.py" and kwargs.get("tool_name") == "apply_patch" and content == "b = 2\n":
            raise OSError("write failed")
        return original(path, content, **kwargs)

    tools._write_text = failing_write
    actions = parse_patch(
        "*** Begin Patch\n*** Update File: x.py\n@@\n-a = 1\n+a = 2\n"
        "*** Update File: y.py\n@@\n-b = 1\n+b = 2\n*** End Patch"
    )
    with pytest.raises(Exception, match="the files already changed were restored"):
        tools.apply_patch(actions)
    assert ide.buffers[str(workspace / "x.py")] == "a = 1\n"
