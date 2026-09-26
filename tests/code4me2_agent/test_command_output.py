from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from code4me2_agent.command_tools import (
    OUTPUT_HYGIENE_ENVIRONMENT,
    WorkspaceCommandTools,
    available_commands,
    clean_terminal_output,
    command_environment,
)
from code4me2_agent.config import AgentConfig, CommandConfig, HarnessOptions
from code4me2_agent.runner_output import summarize_test_output
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import CommandNotFoundError

PYTHON = Path(sys.executable)
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process semantics")


def _tools(tmp_path: Path, *, allowlist: list[str], harness: HarnessOptions | None = None):
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(
        workspace_root=workspace,
        trace_path=tmp_path / "trace.jsonl",
        session_id="s",
        commands=CommandConfig(allowlisted_commands=allowlist),
        harness=harness or HarnessOptions(),
    )
    events: list[dict] = []

    class Capture:
        def append(self, event):
            events.append(event)

    telemetry = AgentTelemetryRecorder(config, sinks=[Capture()])
    return WorkspaceCommandTools(config, telemetry=telemetry), workspace, events


# --------------------------------------------------------------- hygiene


def test_child_environment_adds_non_interactive_settings_and_keeps_the_allowlist_pure():
    child = command_environment({"PATH": "/bin", "TERM": "xterm-256color", "CODE4ME_ACP_TOKEN": "secret"})
    assert child["PATH"] == "/bin"
    assert child["TERM"] == "dumb"
    for key, value in OUTPUT_HYGIENE_ENVIRONMENT.items():
        assert child[key] == value
    assert "CODE4ME_ACP_TOKEN" not in child


def test_ansi_colours_and_progress_redraws_are_removed():
    raw = "\x1b[1;32mPASSED\x1b[0m\ndownloading 10%\rdownloading 90%\rdownloading done\r\nnext\x07"
    assert clean_terminal_output(raw) == "PASSED\ndownloading done\nnext"


def test_commands_run_with_hygiene_environment_and_clean_output(tmp_path):
    tools, _workspace, _ = _tools(tmp_path, allowlist=[PYTHON.name])
    code = (
        "import os, sys\n"
        "print(os.environ['NO_COLOR'], os.environ['TERM'], os.environ['CI'])\n"
        "sys.stdout.write('\\x1b[31mred\\x1b[0m\\n')\n"
    )
    result = tools.run_command([PYTHON.name, "-c", code], timeout_seconds=30)
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["1 dumb 1", "red"]


# --------------------------------------------------------------- wrappers


@POSIX_ONLY
def test_allowlisted_workspace_wrapper_runs_from_the_project(tmp_path):
    tools, workspace, events = _tools(tmp_path, allowlist=["gradlew"])
    wrapper = workspace / "gradlew"
    wrapper.write_text(f"#!{PYTHON}\nimport sys\nprint('wrapper', sys.argv[1:])\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    dotted = tools.run_command(["./gradlew", "test"], timeout_seconds=30)
    bare = tools.run_command(["gradlew", "check"], timeout_seconds=30)

    assert dotted.exit_code == 0 and "wrapper ['test']" in dotted.stdout
    assert bare.exit_code == 0 and "wrapper ['check']" in bare.stdout
    completed = [e for e in events if e["event_type"] == "agent.tool.completed"]
    assert completed[-1]["payload"]["command"] == "gradlew"


def test_wrapper_paths_outside_the_workspace_or_unlisted_names_are_refused(tmp_path):
    tools, workspace, _ = _tools(tmp_path, allowlist=["gradlew"])
    with pytest.raises(PermissionError):
        tools.run_command(["../gradlew"], timeout_seconds=5)
    with pytest.raises(PermissionError):
        tools.run_command(["./other-script"], timeout_seconds=5)
    with pytest.raises(CommandNotFoundError):
        tools.run_command(["./gradlew", "test"], timeout_seconds=5)


def test_available_commands_keeps_wrappers_even_when_not_on_path():
    assert available_commands(["gradlew", "mvnw", "definitely-not-a-real-command-xyz"]) == [
        "gradlew",
        "mvnw",
    ]


# ------------------------------------------------------------- streaming


@POSIX_ONLY
def test_running_output_is_streamed_while_the_command_runs(tmp_path):
    tools, _workspace, _ = _tools(tmp_path, allowlist=[PYTHON.name])
    streamed: list[str] = []
    code = "import time\nfor i in range(4):\n    print('line', i, flush=True)\n    time.sleep(0.6)\n"

    result = tools.run_command([PYTHON.name, "-c", code], timeout_seconds=30, on_output=streamed.append)

    assert result.exit_code == 0
    assert streamed, "expected at least one streamed update"
    assert all("line" in text for text in streamed)
    assert len(streamed) <= 4  # rate limited to one update per second


# --------------------------------------------------------- test summaries


PYTEST_FAILING = """\
============================= test session starts ==============================
collected 3 items

tests/test_a.py .F.                                                      [100%]

=================================== FAILURES ===================================
____________________________________ test_b ____________________________________
    def test_b():
>       assert add(1, 1) == 3
E       assert 2 == 3
=========================== short test summary info ============================
FAILED tests/test_a.py::test_b - assert 2 == 3
========================= 1 failed, 2 passed in 0.05s ==========================
"""


def test_pytest_summary_counts_and_failures():
    summary = summarize_test_output(["pytest", "-q"], PYTEST_FAILING, "")
    assert summary is not None
    result = summary.as_result()
    assert result["framework"] == "pytest"
    assert (result["failed"], result["passed"]) == (1, 2)
    assert result["failures"] == [{"name": "tests/test_a.py::test_b", "message": "assert 2 == 3"}]


def test_python_module_pytest_and_unittest_are_recognised():
    assert summarize_test_output(["python", "-m", "pytest"], PYTEST_FAILING, "").framework == "pytest"
    unittest_output = (
        "F.\n======================================================================\n"
        "FAIL: test_x (pkg.tests.TestThing.test_x)\n-----\nAssertionError\n\n"
        "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n"
    )
    summary = summarize_test_output(["python3", "-m", "unittest"], "", unittest_output)
    assert summary.framework == "unittest"
    assert (summary.failed, summary.passed) == (1, 1)
    assert summary.failures[0]["name"].endswith("test_x")


def test_gradle_maven_jest_go_and_cargo_summaries():
    gradle = (
        "> Task :test\n\nCalculatorTest > addsNumbers() FAILED\n"
        "    org.opentest4j.AssertionFailedError: expected: <3> but was: <2>\n\n"
        "5 tests completed, 1 failed\n\nBUILD FAILED in 3s\n"
    )
    summary = summarize_test_output(["./gradlew", "test"], gradle, "")
    assert (summary.framework, summary.failed, summary.passed) == ("gradle", 1, 4)
    assert summary.failures[0]["name"] == "CalculatorTest > addsNumbers()"
    assert "expected: <3>" in summary.failures[0]["message"]

    maven = (
        "[INFO] Results:\n[INFO]\n[ERROR] Failures: \n"
        "[ERROR]   AppTest.testAdd:12 expected:<3> but was:<2>\n[INFO]\n"
        "[ERROR] Tests run: 4, Failures: 1, Errors: 0, Skipped: 1\n[INFO] BUILD FAILURE\n"
    )
    summary = summarize_test_output(["mvn", "-q", "test"], maven, "")
    assert (summary.framework, summary.failed, summary.passed, summary.skipped) == ("maven", 1, 2, 1)
    assert summary.failures[0]["name"] == "AppTest.testAdd"

    jest = "  ● math › adds\n\n    expect(received).toBe(expected)\n\nTests:       1 failed, 3 passed, 4 total\n"
    summary = summarize_test_output(["npm", "test"], jest, "")
    assert (summary.framework, summary.failed, summary.passed) == ("jest", 1, 3)
    assert summary.failures[0]["name"] == "math › adds"

    go = "--- FAIL: TestAdd (0.00s)\n    add_test.go:9: got 2\nFAIL\nFAIL\texample.com/m\t0.01s\nok  \texample.com/other\t0.02s\n"
    summary = summarize_test_output(["go", "test", "./..."], go, "")
    assert (summary.framework, summary.failed) == ("go test", 1)
    assert summary.failures[0]["name"] == "TestAdd"

    cargo = "test tests::adds ... FAILED\ntest tests::subs ... ok\n\ntest result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured\n"
    summary = summarize_test_output(["cargo", "test"], cargo, "")
    assert (summary.framework, summary.failed, summary.passed) == ("cargo test", 1, 1)


def test_unrelated_commands_have_no_summary():
    assert summarize_test_output(["ls", "-la"], "total 0\n", "") is None
    assert summarize_test_output(["python", "script.py"], "hello\n", "") is None


def test_passing_runs_keep_only_the_tail_and_report_the_summary(tmp_path):
    tools, _workspace, events = _tools(tmp_path, allowlist=[PYTHON.name])
    code = (
        "for i in range(60):\n    print('noise', i)\n"
        "print('========================= 7 passed in 0.10s =========================')\n"
    )
    result = tools.run_command([PYTHON.name, "-c", code], timeout_seconds=30)

    assert result.test_summary == {
        "framework": "pytest",
        "passed": 7,
        "failed": 0,
        "errors": 0,
        "summary_line": "7 passed in 0.10s",
    }
    assert "noise 0" not in result.stdout and "7 passed" in result.stdout
    payload = [e for e in events if e["event_type"] == "agent.tool.completed"][-1]["payload"]
    assert payload["test_framework"] == "pytest" and payload["tests_passed"] == 7


def test_summaries_can_be_disabled(tmp_path):
    tools, _workspace, _ = _tools(
        tmp_path, allowlist=[PYTHON.name], harness=HarnessOptions(test_output_summary=False)
    )
    code = "print('==================== 1 passed in 0.01s ====================')"
    assert tools.run_command([PYTHON.name, "-c", code], timeout_seconds=30).test_summary is None
