from __future__ import annotations

import os
import sys

import pytest

from code4me2_agent.config import AgentConfig
from code4me2_agent.file_tools import (
    FileToolLimits,
    TextEdit,
    WorkspaceFileTools,
    apply_text_edits,
    glob_to_regex,
)
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import (
    DirectoryNotEmptyError,
    EditMatchError,
    FileTooLargeError,
    NotTextFileError,
    ToolArgumentError,
    ToolFileExistsError,
    ToolFileNotFoundError,
    WorkspaceBoundaryError,
)


class _FakeAcpBackend:
    def __init__(self, *, read=None, write=True, content: str | None = None, read_error=None):
        self.read_text_file_enabled = read is not False and (read or content is not None or read_error is not None)
        self.write_text_file_enabled = write
        self.session_id = "acp-session"
        self._content = content
        self._read_error = read_error
        self.reads: list[str] = []
        self.writes: list[tuple[str, str]] = []

    def read_text_file(self, absolute_path: str) -> str:
        self.reads.append(absolute_path)
        if self._read_error is not None:
            raise self._read_error
        if self._content is None:
            raise RuntimeError("file not open in IDE")
        return self._content

    def write_text_file(self, absolute_path: str, content: str) -> None:
        self.writes.append((absolute_path, content))


def _make_tools(tmp_path, *, acp_backend=None, limits=None, raw_capture=False):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(
        workspace_root=workspace.resolve(),
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
        raw_capture_enabled=raw_capture,
    )
    captured: list[dict] = []

    class Sink:
        def append(self, event):
            captured.append(event)

    tools = WorkspaceFileTools(
        config,
        acp_backend=acp_backend,
        telemetry=AgentTelemetryRecorder(config, sinks=[Sink()]),
        limits=limits,
    )
    return tools, workspace.resolve(), captured


def _write(workspace, relative, content, *, binary=False):
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


# ----------------------------------------------------------------- confinement


def test_parent_traversal_and_absolute_outside_are_denied(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    with pytest.raises(WorkspaceBoundaryError):
        tools.read_file("../outside.txt")
    with pytest.raises(PermissionError):
        tools.read_file(str(outside))

    denied = [event for event in captured if event["event_type"] == "agent.tool.denied"]
    assert len(denied) == 2
    assert denied[0]["payload"]["denial_reason"] == "outside_workspace_root"
    assert denied[0]["payload"]["tool_name"] == "read_file"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_directory_is_not_walked(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "real/a.txt", "a")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leak.txt").write_text("leak")
    os.symlink(outside, workspace / "link")

    listed = tools.list_files(".", recursive=True)
    assert listed.entries == ["real/", "real/a.txt"]
    assert tools.glob_files("**/*.txt").files == ["real/a.txt"]
    assert tools.grep_files("leak").match_count == 0


# ----------------------------------------------------------------- read_file


def test_read_file_numbers_lines_and_reports_totals(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    _write(workspace, "a.py", "alpha\nbeta\ngamma\n")

    result = tools.read_file("a.py")

    assert result.content == "1|alpha\n2|beta\n3|gamma"
    assert (result.start_line, result.end_line, result.total_lines) == (1, 3, 3)
    assert result.truncated is False and result.next_offset is None
    completed = [e for e in captured if e["event_type"] == "agent.tool.completed"]
    assert completed[-1]["payload"]["tool_name"] == "read_file"
    assert completed[-1]["payload"]["total_lines"] == 3


def test_read_file_offset_limit_pages_and_sets_next_offset(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "".join(f"line {n}\n" for n in range(1, 11)))

    result = tools.read_file("a.txt", offset=3, limit=4)

    lines = result.content.splitlines()
    assert lines[:4] == ["3|line 3", "4|line 4", "5|line 5", "6|line 6"]
    assert lines[4].startswith("[truncated: showing lines 3-6 of 10; call read_file with offset=7")
    assert result.truncated is True
    assert result.truncation_reason == "limit"
    assert result.next_offset == 7


def test_read_file_legacy_line_aliases_still_work(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "one\ntwo\nthree\nfour\n")

    result = tools.read_file("a.txt", line_start=2, line_end=3)

    assert result.content.splitlines()[:2] == ["2|two", "3|three"]


def test_read_file_offset_past_end_is_argument_error(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "one\ntwo\n")

    with pytest.raises(ToolArgumentError) as error:
        tools.read_file("a.txt", offset=120)

    assert error.value.field == "offset"
    assert "total_lines=2" in str(error.value)


def test_read_file_cuts_long_lines_with_marker(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path, limits=FileToolLimits(max_line_chars=10))
    _write(workspace, "a.txt", "x" * 50 + "\nshort\n")

    result = tools.read_file("a.txt")

    assert result.content.splitlines()[0] == "1|" + "x" * 10 + "…[line truncated]"


def test_read_file_byte_cap_truncates_with_hint(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path, limits=FileToolLimits(max_read_output_bytes=40))
    _write(workspace, "a.txt", "".join(f"row number {n}\n" for n in range(20)))

    result = tools.read_file("a.txt")

    assert result.truncated is True
    assert result.truncation_reason == "bytes"
    assert result.next_offset == result.end_line + 1
    assert f"offset={result.next_offset}" in result.content


def test_read_file_rejects_binary_and_oversized_files(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path, limits=FileToolLimits(max_file_bytes=64))
    _write(workspace, "blob.bin", b"\x00\x01\x02binary", binary=True)
    _write(workspace, "big.txt", "x" * 100)

    with pytest.raises(NotTextFileError, match="binary"):
        tools.read_file("blob.bin")
    with pytest.raises(FileTooLargeError):
        tools.read_file("big.txt")


def test_read_file_replaces_invalid_utf8_and_flags_it(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "latin.txt", b"caf\xe9\n", binary=True)

    result = tools.read_file("latin.txt")

    assert result.decoding_errors_replaced is True
    assert result.content.startswith("1|caf")


def test_read_file_missing_mentions_glob_files(tmp_path):
    tools, _, _ = _make_tools(tmp_path)

    with pytest.raises(ToolFileNotFoundError, match="glob_files"):
        tools.read_file("nope.txt")


def test_read_file_uses_acp_backend_and_falls_back_on_backend_error(tmp_path):
    backend = _FakeAcpBackend(content="from ide\n")
    tools, workspace, captured = _make_tools(tmp_path, acp_backend=backend)
    _write(workspace, "a.txt", "from disk\n")

    result = tools.read_file("a.txt")
    assert result.content == "1|from ide"
    assert result.backend_type == "acp"

    failing = _FakeAcpBackend(read_error=RuntimeError("Session not found"))
    tools, workspace, captured = _make_tools(tmp_path, acp_backend=failing)
    _write(workspace, "a.txt", "from disk\n")

    result = tools.read_file("a.txt")
    assert result.content == "1|from disk"
    assert result.backend_type == "local"
    assert any(e["event_type"] == "agent.acp.backend_fallback" for e in captured)


# ----------------------------------------------------------------- create/write


def test_create_file_refuses_existing_local_file_with_acp_write_backend(tmp_path):
    backend = _FakeAcpBackend(read=False, write=True)
    tools, workspace, _ = _make_tools(tmp_path, acp_backend=backend)
    _write(workspace, "exists.txt", "old")

    with pytest.raises(ToolFileExistsError, match="write_file"):
        tools.create_file("exists.txt", "new")

    assert backend.writes == []
    assert (workspace / "exists.txt").read_text() == "old"


def test_create_file_refuses_when_acp_read_returns_content(tmp_path):
    backend = _FakeAcpBackend(content="unsaved buffer", write=True)
    tools, workspace, _ = _make_tools(tmp_path, acp_backend=backend)

    with pytest.raises(ToolFileExistsError):
        tools.create_file("draft.txt", "new")
    assert backend.writes == []


def test_create_file_creates_parent_directories_and_reports_bytes(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)

    result = tools.create_file("deep/nested/new.txt", "héllo")

    assert (workspace / "deep/nested/new.txt").read_text(encoding="utf-8") == "héllo"
    assert result.bytes_written == len("héllo".encode("utf-8"))
    assert result.path == "deep/nested/new.txt"


def test_write_file_before_snapshot_comes_from_acp_read(tmp_path):
    backend = _FakeAcpBackend(content="ide version\n", write=False)
    tools, workspace, captured = _make_tools(tmp_path, acp_backend=backend, raw_capture=True)
    _write(workspace, "a.txt", "disk version\n")

    tools.write_file("a.txt", "new version\n")

    event = [e for e in captured if e["payload"].get("tool_name") == "write_file"][-1]
    assert "-ide version" in event["raw_payload"]["diff"]
    assert (workspace / "a.txt").read_text() == "new version\n"


def test_write_file_refuses_directory_path(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    (workspace / "dir").mkdir()

    with pytest.raises(ToolArgumentError):
        tools.write_file("dir", "x")


# ----------------------------------------------------------------- replace/edit


def test_replace_text_ambiguous_reports_count_and_leaves_file_unchanged(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "x = 1\nx = 1\nx = 1\n")

    with pytest.raises(EditMatchError) as error:
        tools.replace_text("a.py", "x = 1", "x = 2")

    assert error.value.code == "edit_ambiguous"
    assert error.value.match_count == 3
    assert "matches 3 locations" in str(error.value)
    assert (workspace / "a.py").read_text() == "x = 1\nx = 1\nx = 1\n"


def test_replace_text_no_match_hints(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "def f():\n    return 1\n")

    with pytest.raises(EditMatchError) as whitespace_error:
        tools.replace_text("a.py", "def f():\n  return 1", "def f():\n    return 2")
    assert whitespace_error.value.code == "edit_no_match"
    assert "whitespace or indentation" in str(whitespace_error.value)

    with pytest.raises(EditMatchError) as prefix_error:
        tools.replace_text("a.py", "1|def f():", "def g():")
    assert "line-number prefix" in str(prefix_error.value)

    with pytest.raises(EditMatchError) as plain_error:
        tools.replace_text("a.py", "class Missing", "class Found")
    assert "copy the exact current text" in str(plain_error.value)


def test_replace_text_replace_all_reports_replacements(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "x = 1\nx = 1\n")

    result = tools.replace_text("a.py", "x = 1", "x = 2", replace_all=True)

    assert result.replacements == 2
    assert (workspace / "a.py").read_text() == "x = 2\nx = 2\n"


def test_replace_text_matches_across_crlf_and_preserves_endings(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "win.txt", b"one\r\ntwo\r\nthree\r\n", binary=True)

    tools.replace_text("win.txt", "one\ntwo", "uno\ndos")

    assert (workspace / "win.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


def test_replace_text_rejects_undecodable_file(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "latin.txt", b"caf\xe9 x\n", binary=True)

    with pytest.raises(NotTextFileError, match="editing"):
        tools.replace_text("latin.txt", "x", "y")


def test_edit_file_applies_in_order_and_later_edits_see_earlier_results(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    _write(workspace, "a.py", "a = 1\nb = 2\n")

    result = tools.edit_file(
        "a.py",
        [
            TextEdit("a = 1", "a = 10"),
            TextEdit("a = 10\nb = 2", "a = 10\nb = 20"),
        ],
    )

    assert result.edits_applied == 2 and result.replacements == 2
    assert (workspace / "a.py").read_text() == "a = 10\nb = 20\n"
    event = [e for e in captured if e["payload"].get("tool_name") == "edit_file"][-1]
    assert event["payload"]["edits_applied"] == 2


def test_edit_file_is_atomic_when_a_later_edit_fails(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "a = 1\nb = 2\nc = 3\n")

    with pytest.raises(EditMatchError) as error:
        tools.edit_file(
            "a.py",
            [TextEdit("a = 1", "a = 9"), TextEdit("missing", "x"), TextEdit("c = 3", "c = 9")],
        )

    assert str(error.value).startswith("Edit 2 of 3:")
    assert error.value.edit_index == 1
    assert (workspace / "a.py").read_text() == "a = 1\nb = 2\nc = 3\n"


def test_apply_text_edits_rejects_empty_or_identical_edits():
    with pytest.raises(ToolArgumentError) as empty:
        apply_text_edits("abc", [TextEdit("", "x")], path="p")
    assert empty.value.field == "old_text"
    with pytest.raises(ToolArgumentError):
        apply_text_edits("abc", [TextEdit("a", "a")], path="p")


# ----------------------------------------------------------------- delete/move


def test_delete_file_removes_file_and_records_before_content(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path, raw_capture=True)
    _write(workspace, "gone.txt", b"bye\n", binary=True)

    result = tools.delete_file("gone.txt")

    assert not (workspace / "gone.txt").exists()
    assert result.was_directory is False and result.bytes_removed == 4
    event = [e for e in captured if e["payload"].get("tool_name") == "delete_file"][-1]
    assert "-bye" in event["raw_payload"]["diff"]


def test_delete_file_directory_rules(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    (workspace / "empty").mkdir()
    _write(workspace, "full/a.txt", "a")

    with pytest.raises(ToolFileNotFoundError):
        tools.delete_file("missing.txt")
    with pytest.raises(DirectoryNotEmptyError):
        tools.delete_file("full")
    assert tools.delete_file("empty").was_directory is True
    assert not (workspace / "empty").exists()
    with pytest.raises(ToolArgumentError):
        tools.delete_file(".")


def test_move_file_renames_and_creates_parents(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "content")

    result = tools.move_file("a.txt", "nested/dir/b.txt")

    assert not (workspace / "a.txt").exists()
    assert (workspace / "nested/dir/b.txt").read_text() == "content"
    assert (result.source_path, result.destination_path) == ("a.txt", "nested/dir/b.txt")
    assert result.overwritten is False


def test_move_file_refuses_existing_destination_unless_overwrite(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "A")
    _write(workspace, "b.txt", "B")

    with pytest.raises(ToolFileExistsError, match="overwrite=true"):
        tools.move_file("a.txt", "b.txt")
    assert (workspace / "b.txt").read_text() == "B"

    result = tools.move_file("a.txt", "b.txt", overwrite=True)
    assert result.overwritten is True
    assert (workspace / "b.txt").read_text() == "A"


def test_move_file_rejects_outside_destination_and_dir_into_itself(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    _write(workspace, "dir/a.txt", "a")

    with pytest.raises(PermissionError):
        tools.move_file("dir/a.txt", "../escape.txt")
    assert any(
        e["event_type"] == "agent.tool.denied" and e["payload"]["tool_name"] == "move_file"
        for e in captured
    )
    with pytest.raises(ToolArgumentError):
        tools.move_file("dir", "dir/sub")
    with pytest.raises(ToolFileNotFoundError):
        tools.move_file("missing.txt", "x.txt")


# ----------------------------------------------------------------- list/glob


def test_list_files_non_recursive_marks_directories_and_sorts(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "src/main.py", "x")
    _write(workspace, "b.txt", "b")
    _write(workspace, "a.txt", "a")

    result = tools.list_files()

    assert result.entries == ["a.txt", "b.txt", "src/"]
    assert (result.file_count, result.directory_count) == (2, 1)
    assert result.path == "."


def test_list_files_recursive_honours_max_depth(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a/b/c/deep.txt", "x")

    depth_two = tools.list_files(recursive=True, max_depth=2)
    assert depth_two.entries == ["a/", "a/b/"]
    full = tools.list_files(recursive=True)
    assert full.entries == ["a/", "a/b/", "a/b/c/", "a/b/c/deep.txt"]


def test_list_files_skips_default_ignored_dirs_unless_included(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "node_modules/pkg/index.js", "x")
    _write(workspace, ".git/HEAD", "ref")
    _write(workspace, "src/app.js", "x")

    assert tools.list_files(recursive=True).entries == ["src/", "src/app.js"]
    included = tools.list_files(recursive=True, include_ignored=True).entries
    assert ".git/" in included and "node_modules/pkg/index.js" in included


def test_list_files_caps_results_and_flags_truncation(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    for n in range(10):
        _write(workspace, f"f{n:02d}.txt", "x")

    result = tools.list_files(max_results=3)

    assert result.entries == ["f00.txt", "f01.txt", "f02.txt"]
    assert result.truncated is True


def test_list_files_rejects_file_path_and_missing_dir(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "a")

    with pytest.raises(ToolArgumentError):
        tools.list_files("a.txt")
    with pytest.raises(ToolFileNotFoundError):
        tools.list_files("missing")


def test_glob_double_star_matches_nested_and_single_star_does_not(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "top.kt", "x")
    _write(workspace, "src/main/App.kt", "x")
    _write(workspace, "src/main/Util.java", "x")

    assert tools.glob_files("**/*.kt").files == ["src/main/App.kt", "top.kt"]
    assert tools.glob_files("*.kt").files == ["top.kt"]
    assert tools.glob_files("**/App*", path="src").files == ["src/main/App.kt"]
    assert tools.glob_files("src/main/**").files == ["src/main/App.kt", "src/main/Util.java"]


def test_glob_ignores_build_dirs_caps_and_rejects_parent_patterns(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "build/out.txt", "x")
    for n in range(5):
        _write(workspace, f"f{n}.txt", "x")

    assert "build/out.txt" not in tools.glob_files("**/*.txt").files
    assert "build/out.txt" in tools.glob_files("**/*.txt", include_ignored=True).files
    capped = tools.glob_files("*.txt", max_results=2)
    assert capped.files == ["f0.txt", "f1.txt"] and capped.truncated is True
    with pytest.raises(ToolArgumentError):
        tools.glob_files("../*.txt")


def test_glob_to_regex_character_classes():
    assert glob_to_regex("file[0-9].py").match("file3.py")
    assert not glob_to_regex("file[0-9].py").match("filex.py")
    assert glob_to_regex("a?c").match("abc") and not glob_to_regex("a?c").match("a/c")


# ----------------------------------------------------------------- grep/search


def test_grep_content_mode_has_line_numbers_and_context(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "one\ntwo TODO here\nthree\nfour\n")

    result = tools.grep_files("TODO", context_lines=1)

    assert result.match_count == 1 and result.file_count == 1
    match = result.matches[0]
    assert (match["path"], match["line_number"], match["line"]) == ("a.py", 2, "two TODO here")
    assert match["context_before"] == ["one"] and match["context_after"] == ["three"]


def test_grep_files_with_matches_and_count_modes(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.py", "x\nx\n")
    _write(workspace, "b.py", "x\n")
    _write(workspace, "c.py", "y\n")

    files = tools.grep_files("x", output_mode="files_with_matches")
    assert files.files == ["a.py", "b.py"] and files.matches is None
    counts = tools.grep_files("x", output_mode="count")
    assert counts.counts == [{"path": "a.py", "count": 2}, {"path": "b.py", "count": 1}]


def test_grep_case_insensitive_and_glob_filter(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "src/A.java", "Hello\n")
    _write(workspace, "src/b.kt", "hello\n")

    assert tools.grep_files("hello").file_count == 1
    both = tools.grep_files("hello", case_insensitive=True)
    assert both.file_count == 2
    java_only = tools.grep_files("hello", case_insensitive=True, glob="**/*.java")
    assert [m["path"] for m in java_only.matches] == ["src/A.java"]


def test_grep_invalid_regex_and_bad_mode_are_argument_errors(tmp_path):
    tools, _, _ = _make_tools(tmp_path)

    with pytest.raises(ToolArgumentError) as error:
        tools.grep_files("foo(")
    assert error.value.field == "pattern"
    with pytest.raises(ToolArgumentError):
        tools.grep_files("x", output_mode="everything")


def test_grep_skips_binary_large_and_ignored_files(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path, limits=FileToolLimits(max_grep_file_bytes=32))
    _write(workspace, "blob.bin", b"needle\x00", binary=True)
    _write(workspace, "big.txt", "needle " * 20)
    _write(workspace, "node_modules/x.js", "needle")
    _write(workspace, "ok.txt", "needle")

    result = tools.grep_files("needle")

    assert [m["path"] for m in result.matches] == ["ok.txt"]
    assert result.files_skipped == 2
    assert result.files_searched == 1


def test_grep_caps_matches_and_searches_single_file(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "hit\n" * 10)
    _write(workspace, "b.txt", "hit\n")

    capped = tools.grep_files("hit", max_results=3)
    assert capped.match_count == 3 and capped.truncated is True

    single = tools.grep_files("hit", path="b.txt")
    assert single.match_count == 1 and single.path == "b.txt"


def test_search_files_is_literal(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    _write(workspace, "a.txt", "a.b\naxb\n")

    result = tools.search_files("a.b")

    assert [m["line"] for m in result.matches] == ["a.b"]
    assert result.pattern == "a.b"
    event = [e for e in captured if e["payload"].get("tool_name") == "search_files"][-1]
    assert event["payload"]["query"] == "a.b"


def test_content_keys_are_redacted_when_storage_is_off(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = AgentConfig(
        workspace_root=workspace.resolve(),
        trace_path=tmp_path / "trace.jsonl",
        session_id="s",
        store_agent_content=False,
    )
    captured = []

    class Sink:
        def append(self, event):
            captured.append(event)

    tools = WorkspaceFileTools(config, telemetry=AgentTelemetryRecorder(config, sinks=[Sink()]))
    (workspace / "a.txt").write_text("secret needle")
    tools.grep_files("needle", glob="*.txt")

    payload = captured[-1]["payload"]
    assert "pattern" not in payload and "glob" not in payload
    assert payload["match_count"] == 1


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_delete_and_move_operate_on_the_symlink_not_its_target(tmp_path):
    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "target.txt", "keep")
    os.symlink(workspace / "target.txt", workspace / "link.txt")

    result = tools.delete_file("link.txt")

    assert result.path == "link.txt" and result.was_directory is False
    assert not (workspace / "link.txt").is_symlink()
    assert (workspace / "target.txt").read_text() == "keep"

    os.symlink(workspace / "target.txt", workspace / "link2.txt")
    moved = tools.move_file("link2.txt", "renamed-link.txt")
    assert moved.source_path == "link2.txt"
    assert (workspace / "renamed-link.txt").is_symlink()
    assert (workspace / "target.txt").read_text() == "keep"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_pointing_outside_cannot_be_deleted_or_moved(tmp_path):
    tools, workspace, captured = _make_tools(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, workspace / "escape.txt")

    with pytest.raises(WorkspaceBoundaryError):
        tools.delete_file("escape.txt")
    with pytest.raises(WorkspaceBoundaryError):
        tools.move_file("escape.txt", "moved.txt")
    assert outside.read_text() == "secret"
    assert (workspace / "escape.txt").is_symlink()
    assert [e["payload"]["tool_name"] for e in captured if e["event_type"] == "agent.tool.denied"] == [
        "delete_file",
        "move_file",
    ]


def test_grep_honours_cancellation_and_skips_very_long_lines(tmp_path):
    from threading import Event

    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "needle here\n")
    _write(workspace, "minified.js", "x" * 12_000 + "needle" + "\nneedle on a short line\n")

    cancel = Event()
    cancel.set()
    cancelled = tools.grep_files("needle", cancellation_event=cancel)
    assert cancelled.cancelled is True and cancelled.truncated is True
    assert cancelled.files_searched == 0

    result = tools.grep_files("needle")
    assert [(m["path"], m["line_number"]) for m in result.matches] == [("a.txt", 1), ("minified.js", 2)]


def test_split_lines_matches_editor_numbering():
    from code4me2_agent.file_tools import split_lines

    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\r\nb") == ["a", "b"]
    assert split_lines("a\x0cb\n") == ["a\x0cb"]
    assert split_lines("") == []
    assert split_lines("\n") == [""]


def test_read_text_denial_is_attributed_to_the_caller(tmp_path):
    tools, _, captured = _make_tools(tmp_path)

    with pytest.raises(WorkspaceBoundaryError):
        tools.read_text("../x", tool_name="edit_file")

    denied = [e for e in captured if e["event_type"] == "agent.tool.denied"]
    assert [e["payload"]["tool_name"] for e in denied] == ["edit_file"]


def test_search_files_honours_cancellation(tmp_path):
    from threading import Event

    tools, workspace, _ = _make_tools(tmp_path)
    _write(workspace, "a.txt", "needle\n")
    cancel = Event()
    cancel.set()

    result = tools.search_files("needle", cancellation_event=cancel)

    assert result.cancelled is True and result.match_count == 0
