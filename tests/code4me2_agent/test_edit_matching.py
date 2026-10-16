from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from code4me2_agent.config import AgentConfig, HarnessOptions
from code4me2_agent.edit_matching import find_matches
from code4me2_agent.file_tools import (
    TextEdit,
    WorkspaceFileTools,
    apply_text_edits_detailed,
)
from code4me2_agent.session_state import FileChange, SessionToolState
from code4me2_agent.syntax_check import introduced_syntax_problem
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import EditMatchError

if TYPE_CHECKING:
    from pathlib import Path


def _tools(tmp_path: Path, *, observer=None, harness: HarnessOptions | None = None):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(
        workspace_root=workspace,
        trace_path=tmp_path / "trace.jsonl",
        session_id="s",
        harness=harness or HarnessOptions(),
    )
    events: list[dict] = []

    class Capture:
        def append(self, event):
            events.append(event)

    telemetry = AgentTelemetryRecorder(config, sinks=[Capture()])
    return WorkspaceFileTools(config, telemetry=telemetry, change_observer=observer), workspace, events


# ------------------------------------------------------------ strategy chain


def test_exact_match_wins_and_reports_exact():
    outcome = apply_text_edits_detailed("a = 1\nb = 2\n", [TextEdit("b = 2", "b = 3")], path="x.py")
    assert outcome.text == "a = 1\nb = 3\n"
    assert outcome.strategies == ("exact",)
    assert not outcome.fuzzy


def test_trailing_whitespace_is_ignored_and_new_text_kept_verbatim():
    text = "def f():   \n    return 1  \n"
    outcome = apply_text_edits_detailed(
        text, [TextEdit("def f():\n    return 1\n", "def f():\n    return 2\n")], path="x.py"
    )
    assert outcome.text == "def f():\n    return 2\n"
    assert outcome.strategies == ("trailing_whitespace",)


def test_wrong_base_indentation_is_matched_and_new_text_reindented():
    text = "class A:\n    def f(self):\n        return 1\n\n    def g(self):\n        return 2\n"
    # The model copied the method without the class indentation.
    edit = TextEdit("def g(self):\n    return 2", "def g(self):\n    return 3")
    outcome = apply_text_edits_detailed(text, [edit], path="a.py")
    assert outcome.strategies == ("indentation",)
    assert outcome.text == (
        "class A:\n    def f(self):\n        return 1\n\n    def g(self):\n        return 3\n"
    )


def test_collapsed_whitespace_matches_when_relative_indentation_differs(tmp_path):
    tools, workspace, events = _tools(tmp_path)
    (workspace / "a.py").write_text("def f():\n    return 1\n")

    result = tools.replace_text("a.py", "def f():\n  return 1", "def f():\n    return 2")

    assert (workspace / "a.py").read_text() == "def f():\n    return 2\n"
    assert result.match_strategies == ["whitespace"]
    completed = [e for e in events if e["event_type"] == "agent.tool.completed"][-1]
    assert completed["payload"]["match_strategies"] == ["whitespace"]


def test_block_anchor_tolerates_a_misremembered_middle_line():
    text = (
        "def compute(values):\n"
        "    total = 0\n"
        "    for value in values:\n"
        "        total += value * 2\n"
        "    return total\n"
    )
    old = (
        "def compute(values):\n"
        "    total = 0\n"
        "    for value in values:\n"
        "        total += value * 3\n"
        "    return total\n"
    )
    new = "def compute(values):\n    return sum(values) * 2\n"
    outcome = apply_text_edits_detailed(text, [TextEdit(old, new)], path="c.py")
    assert outcome.strategies == ("block_anchor",)
    assert outcome.text == new


def test_block_anchor_needs_similar_middle_lines():
    text = "def f():\n    alpha()\n    beta()\n    return 1\n"
    old = "def f():\n    completely()\n    different()\n    return 1\n"
    result = find_matches(text, old, "x")
    assert result.count == 0


def test_a_fuzzy_strategy_never_breaks_a_tie():
    text = "if a:\n    run()\nif b:\n    run()\n"
    with pytest.raises(EditMatchError) as error:
        apply_text_edits_detailed(text, [TextEdit("  run()", "  stop()")], path="t.py")
    # "  run()" is an exact substring twice: ambiguous, never resolved loosely.
    assert error.value.code == "edit_ambiguous"
    assert "(lines 2, 4)" in str(error.value)


def test_fuzzy_ambiguity_names_the_strategy_and_lines():
    text = "def f():\n    x = 1\n\ndef g():\n    x = 1\n"
    with pytest.raises(EditMatchError) as error:
        apply_text_edits_detailed(text, [TextEdit("x = 1  \n", "x = 2\n")], path="t.py")
    assert error.value.code == "edit_ambiguous"
    message = str(error.value)
    assert "ignoring" in message and "lines 2, 5" in message


def test_replace_all_never_uses_fuzzy_matching():
    text = "x  =  1\nx  =  1\n"
    with pytest.raises(EditMatchError) as error:
        apply_text_edits_detailed(text, [TextEdit("x = 1", "x = 2", replace_all=True)], path="t.py")
    assert error.value.code == "edit_no_match"


def test_crlf_files_keep_their_line_endings_under_fuzzy_matching():
    text = "def f():\r\n        return 1\r\n"
    outcome = apply_text_edits_detailed(
        text, [TextEdit("def f():\n    return 1\n", "def f():\n    return 2\n")], path="w.py"
    )
    assert outcome.text == "def f():\r\n        return 2\r\n"


def test_no_match_hint_points_at_the_first_line():
    text = "def handler(event):\n    return event\n"
    with pytest.raises(EditMatchError) as error:
        apply_text_edits_detailed(
            text, [TextEdit("def handler(event):\n    return None", "x")], path="h.py"
        )
    assert "first line appears at line 1" in str(error.value)


def test_preview_and_application_agree(tmp_path):
    tools, workspace, _ = _tools(tmp_path)
    source = "class A:\n    def g(self):\n        return 2\n"
    (workspace / "a.py").write_text(source)
    edit = TextEdit("def g(self):\n    return 2", "def g(self):\n    return 3")
    preview = apply_text_edits_detailed(source, [edit], path="a.py").text
    tools.edit_file("a.py", [edit])
    assert (workspace / "a.py").read_text() == preview


# ------------------------------------------------------------- syntax check


def test_python_syntax_error_introduced_by_an_edit_is_reported(tmp_path):
    tools, workspace, events = _tools(tmp_path)
    (workspace / "m.py").write_text("def f():\n    return 1\n")

    result = tools.replace_text("m.py", "return 1", "return (1")

    assert result.syntax_error is not None
    assert result.syntax_error["language"] == "python"
    assert result.syntax_error["line"] == 2
    # Applied anyway: the model fixes it on the next step.
    assert (workspace / "m.py").read_text() == "def f():\n    return (1\n"
    payload = [e for e in events if e["event_type"] == "agent.tool.completed"][-1]["payload"]
    assert payload["syntax_check"] == "failed"
    assert payload["syntax_error_line"] == 2


def test_pre_existing_syntax_errors_are_not_reported(tmp_path):
    tools, workspace, _ = _tools(tmp_path)
    (workspace / "m.py").write_text("def f(:\n    return 1\n")
    result = tools.replace_text("m.py", "return 1", "return 2")
    assert result.syntax_error is None


def test_json_and_toml_are_checked_and_new_files_count(tmp_path):
    tools, workspace, _ = _tools(tmp_path)
    created = tools.create_file("data.json", '{"a": 1,}')
    assert created.syntax_error is not None and created.syntax_error["language"] == "json"
    written = tools.write_file("cfg.toml", "[tool\nname = 1\n")
    assert written.syntax_error is not None and written.syntax_error["language"] == "toml"
    fine = tools.write_file("ok.toml", "[tool]\nname = 1\n")
    assert fine.syntax_error is None


def test_syntax_check_can_be_disabled(tmp_path):
    tools, _workspace, _ = _tools(tmp_path, harness=HarnessOptions(syntax_check=False))
    assert tools.create_file("bad.py", "def (").syntax_error is None


def test_other_languages_are_not_checked():
    assert introduced_syntax_problem("a.kt", None, "fun (") is None


# -------------------------------------------------------------- checkpoints


def test_checkpoints_record_first_before_state_per_turn_and_undo_restores(tmp_path):
    state = SessionToolState()
    tools, workspace, events = _tools(tmp_path, observer=state.record_change)
    (workspace / "a.py").write_text("one\n")
    (workspace / "gone.txt").write_text("keep me\n")

    state.begin_turn("turn-1", "change things")
    tools.replace_text("a.py", "one", "two")
    tools.replace_text("a.py", "two", "three")
    tools.create_file("new.py", "x = 1\n")
    tools.delete_file("gone.txt")
    assert state.changed_paths() == ["a.py", "new.py", "gone.txt"]
    diff = state.turn_diff()
    assert "-one" in diff and "+three" in diff and "+++ b/new.py" in diff
    state.end_turn()
    assert state.checkpoint_count() == 1

    tool_events_before = len([e for e in events if e["event_type"].startswith("agent.tool")])
    outcome = state.undo_last_turn(tools)

    assert sorted(outcome.restored) == ["a.py", "gone.txt"]
    assert outcome.deleted == ("new.py",)
    assert outcome.skipped == ()
    assert (workspace / "a.py").read_text() == "one\n"
    assert (workspace / "gone.txt").read_text() == "keep me\n"
    assert not (workspace / "new.py").exists()
    # Undo is not model activity: no tool events, no new checkpoint.
    assert len([e for e in events if e["event_type"].startswith("agent.tool")]) == tool_events_before
    assert state.checkpoint_count() == 0
    assert state.undo_last_turn(tools).nothing_to_undo


def test_undo_skips_files_the_user_changed_afterwards(tmp_path):
    state = SessionToolState()
    tools, workspace, _ = _tools(tmp_path, observer=state.record_change)
    (workspace / "a.py").write_text("one\n")
    state.begin_turn("t", "p")
    tools.replace_text("a.py", "one", "two")
    state.end_turn()
    (workspace / "a.py").write_text("user edit\n")

    outcome = state.undo_last_turn(tools)

    assert outcome.restored == ()
    assert outcome.skipped and outcome.skipped[0][0] == "a.py"
    assert (workspace / "a.py").read_text() == "user edit\n"


def test_moves_are_undone(tmp_path):
    state = SessionToolState()
    tools, workspace, _ = _tools(tmp_path, observer=state.record_change)
    (workspace / "old.py").write_text("content\n")
    state.begin_turn("t", "p")
    tools.move_file("old.py", "pkg/new.py")
    state.end_turn()

    outcome = state.undo_last_turn(tools)

    assert (workspace / "old.py").read_text() == "content\n"
    assert not (workspace / "pkg/new.py").exists()
    assert outcome.restored == ("old.py",) and outcome.deleted == ("pkg/new.py",)


def test_turns_without_changes_leave_no_checkpoint():
    state = SessionToolState()
    state.begin_turn("t", "just a question")
    state.end_turn()
    assert state.checkpoint_count() == 0


def test_oversized_before_state_is_not_restorable():
    state = SessionToolState()
    state.begin_turn("t", "p")
    state.record_change(FileChange("big.txt", "x" * (3 * 1024 * 1024), "y"))
    state.end_turn()

    class _Tools:
        def read_text(self, path, **kwargs):
            return "y", False

    outcome = state.undo_last_turn(_Tools())
    assert outcome.skipped[0][0] == "big.txt"
