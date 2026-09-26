from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from threading import Event, Thread

import pytest

from code4me2_agent.command_tools import (
    AcpCommandExecution,
    WorkspaceCommandTools,
    _external_command_environment,
)
from code4me2_agent.config import AgentConfig, CommandConfig
from code4me2_agent.telemetry import AgentTelemetryRecorder
from code4me2_agent.tool_errors import CommandNotFoundError

PYTHON = Path(sys.executable)
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


@pytest.fixture
def command_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{PYTHON.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return _make_tools(tmp_path)


def _make_tools(tmp_path, *, allowlist=None, timeout_seconds=30.0, max_output_bytes=16384, acp_backend=None):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(
        workspace_root=workspace.resolve(),
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
        commands=CommandConfig(
            allowlisted_commands=list(allowlist or [PYTHON.name]),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        ),
    )
    captured: list[dict] = []

    class Sink:
        def append(self, event):
            captured.append(event)

    tools = WorkspaceCommandTools(
        config,
        acp_backend=acp_backend,
        telemetry=AgentTelemetryRecorder(config, sinks=[Sink()]),
    )
    tools.captured = captured  # type: ignore[attr-defined]
    return tools


def _py(*code: str) -> list[str]:
    return [PYTHON.name, "-c", "\n".join(code)]


def test_stdin_is_closed_so_reading_it_does_not_hang(command_tools):
    result = command_tools.run_command(
        _py("import sys", "print(repr(sys.stdin.read()))"), timeout_seconds=15
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "''"
    assert result.status == "completed"


def test_environment_allowlist_is_pure():
    child_env = _external_command_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/u",
            "CODE4ME_ACP_TOKEN": "secret",
            "CODE4ME_ACP_GRANT": "grant",
            "JAVA_HOME": "/jdk",
            "GRADLE_USER_HOME": "/gradle",
            "npm_config_cache": "/npm",
            "LC_ALL": "C.UTF-8",
            "PYTHONHOME": "/bundle",
            "PYTHONPATH": "/src",
            "DYLD_LIBRARY_PATH": "/bundle/lib",
            "_MEIPASS2": "/bundle",
            "LD_LIBRARY_PATH": "/bundle/lib",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib",
            "RANDOM_SECRET": "x",
        }
    )

    assert child_env == {
        "PATH": "/bin",
        "HOME": "/home/u",
        "JAVA_HOME": "/jdk",
        "GRADLE_USER_HOME": "/gradle",
        "npm_config_cache": "/npm",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "/src",
        "LD_LIBRARY_PATH": "/usr/lib",
    }


def test_child_cannot_see_code4me_secrets(command_tools, monkeypatch):
    monkeypatch.setenv("CODE4ME_ACP_TOKEN", "super-secret")
    monkeypatch.setenv("CODE4ME_ACP_BACKEND_URL", "https://backend")

    result = command_tools.run_command(
        _py("import json, os", "print(json.dumps(sorted(os.environ)))"), timeout_seconds=15
    )

    keys = json.loads(result.stdout)
    assert not any(key.startswith("CODE4ME_") for key in keys)
    assert "PATH" in keys


@POSIX_ONLY
def test_timeout_kills_the_whole_process_group(command_tools):
    result = command_tools.run_command(
        _py(
            "import subprocess, sys, time",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
            "print(child.pid, flush=True)",
            "time.sleep(60)",
        ),
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert result.status == "timeout"
    assert result.exit_code is None
    grandchild_pid = int(result.stdout.strip().splitlines()[0])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        # A zombie still answers signal 0 until it is reaped; wait for init.
        try:
            os.waitpid(grandchild_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.1)
    else:  # pragma: no cover - diagnostic
        pytest.fail("grandchild survived the process-group kill")


def test_timeout_is_clamped_to_configured_bounds(command_tools):
    result = command_tools.run_command(_py("pass"), timeout_seconds=99999)
    assert result.timeout_seconds == 600.0

    result = command_tools.run_command(_py("pass"), timeout_seconds=0)
    assert result.timeout_seconds == 1.0

    default = command_tools.run_command(_py("pass"))
    assert default.timeout_seconds == 30.0


def test_output_is_head_and_tail_truncated_with_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{PYTHON.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    tools = _make_tools(tmp_path, max_output_bytes=4096)

    result = tools.run_command(
        _py("for n in range(20000): print(f'line {n}')"), timeout_seconds=30
    )

    assert result.output_truncated is True
    assert result.stdout.startswith(f"line 0{os.linesep}")
    assert result.stdout.rstrip().endswith("line 19999")
    assert "bytes truncated ...]" in result.stdout
    assert len(result.stdout.encode("utf-8")) < 4096 + 64


def test_stderr_stays_separate_and_exit_code_and_duration_reported(command_tools):
    result = command_tools.run_command(
        _py("import sys", "sys.stdout.write('out')", "sys.stderr.write('err')", "sys.exit(3)")
    )

    assert (result.stdout, result.stderr, result.exit_code) == ("out", "err", 3)
    assert result.duration_ms > 0
    assert result.status == "completed"
    event = command_tools.captured[-1]
    assert event["payload"]["exit_code"] == 3
    assert event["payload"]["timeout_seconds"] == 30.0


def test_missing_executable_is_command_not_found_error(tmp_path):
    tools = _make_tools(tmp_path, allowlist=["definitely-missing-cmd-xyz"])

    with pytest.raises(CommandNotFoundError, match="not found"):
        tools.run_command(["definitely-missing-cmd-xyz"])


def test_argv_rules_unchanged(command_tools, tmp_path):
    with pytest.raises(PermissionError):
        command_tools.run_command([str(PYTHON), "-c", "pass"])
    with pytest.raises(PermissionError, match="allowlist"):
        command_tools.run_command(["bash", "-c", "true"])
    with pytest.raises(TypeError):
        command_tools.run_command("python -c pass")
    with pytest.raises(PermissionError):
        command_tools.run_command(_py("pass"), cwd="..")
    denials = [e for e in command_tools.captured if e["event_type"] == "agent.tool.denied"]
    assert [e["payload"]["denial_reason"] for e in denials] == [
        "command_path_not_allowed",
        "command_not_allowlisted",
        "argv_must_be_array",
        "outside_workspace_root",
    ]


def test_acp_terminal_backend_receives_effective_timeout(tmp_path):
    class Backend:
        terminal_enabled = True

        def __init__(self):
            self.calls = []

        def run_command(self, **kwargs):
            self.calls.append(kwargs)
            return AcpCommandExecution(
                exit_code=0, stdout="ok", stderr="", timed_out=False, output_truncated=False
            )

    backend = Backend()
    tools = _make_tools(tmp_path, allowlist=["git"], acp_backend=backend)

    result = tools.run_command(["git", "status"], timeout_seconds=300)

    assert backend.calls[0]["timeout_seconds"] == 300.0
    assert backend.calls[0]["command"] == "git"
    assert result.backend_type == "acp" and result.stdout == "ok"


@POSIX_ONLY
def test_cancel_event_stops_a_running_command(command_tools):
    cancel = Event()
    Thread(target=lambda: (time.sleep(0.6), cancel.set()), daemon=True).start()

    started = time.monotonic()
    result = command_tools.run_command(
        _py("import time", "time.sleep(30)"), timeout_seconds=30, cancel_event=cancel
    )

    assert result.status == "cancelled"
    assert result.exit_code is None
    assert time.monotonic() - started < 10


def test_environment_allowlist_passes_git_and_ssh_agent_settings():
    child_env = _external_command_environment(
        {
            "PATH": "/bin",
            "GIT_SSH_COMMAND": "ssh -i key",
            "GIT_CONFIG_GLOBAL": "/home/u/.gitconfig",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "SSH_AGENT_PID": "42",
            "CODE4ME_ACP_TOKEN": "secret",
        }
    )
    assert child_env == {
        "PATH": "/bin",
        "GIT_SSH_COMMAND": "ssh -i key",
        "GIT_CONFIG_GLOBAL": "/home/u/.gitconfig",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "SSH_AGENT_PID": "42",
    }


@POSIX_ONLY
def test_non_executable_allowlisted_program_is_a_clear_tool_error(tmp_path, monkeypatch):
    from code4me2_agent.tool_errors import ToolError

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "notexec").write_text("#!/bin/sh\necho hi\n")
    (bin_dir / "notexec").chmod(0o644)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    tools = _make_tools(tmp_path, allowlist=["notexec"])

    with pytest.raises(ToolError) as error:
        tools.run_command(["notexec"])

    assert error.value.code == "command_not_executable"
