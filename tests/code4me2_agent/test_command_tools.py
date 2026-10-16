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
    blocked_program,
    command_key,
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


def _make_tools(tmp_path, *, blocked=None, timeout_seconds=30.0, max_output_bytes=16384, acp_backend=None):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = AgentConfig(
        workspace_root=workspace.resolve(),
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
        commands=CommandConfig(
            blocked_commands=list(blocked or []),
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
    # Output is normalised to "\n" on every platform (clean_terminal_output).
    assert result.stdout.startswith("line 0\n")
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
    tools = _make_tools(tmp_path)

    with pytest.raises(CommandNotFoundError, match="not found"):
        tools.run_command(["definitely-missing-cmd-xyz"])
    with pytest.raises(CommandNotFoundError, match="resolved against cwd"):
        tools.run_command(["./scripts/missing.sh"])
    # Not a policy decision: nothing is recorded as denied.
    assert not [e for e in tools.captured if e["event_type"] == "agent.tool.denied"]


def test_blocked_programs_are_refused_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{PYTHON.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    tools = _make_tools(tmp_path, blocked=["Git", "curl"])

    with pytest.raises(PermissionError, match=r"^git is blocked in this study and cannot run\. Blocked programs: Git, curl\."):
        tools.run_command(["git", "status"])
    with pytest.raises(PermissionError, match="git is blocked"):
        tools.run_command(["/usr/bin/GIT.exe", "status"])
    with pytest.raises(PermissionError, match=r"git is blocked in this study and cannot run \(through bash\)"):
        tools.run_command(["bash", "-c", "cd src && git push"])
    with pytest.raises(PermissionError, match=r"curl is blocked .* \(through env\)"):
        tools.run_command(["env", "HTTPS_PROXY=x", "curl", "https://example.com"])
    with pytest.raises(PermissionError, match="Do not try to run it another way"):
        tools.run_command(["xargs", "curl"])
    denials = [e for e in tools.captured if e["event_type"] == "agent.tool.denied"]
    assert [e["payload"]["denial_reason"] for e in denials] == ["command_blocked"] * 5

    # Anything else runs: arguments of an ordinary program are data, not commands.
    result = tools.run_command(_py("import sys", "print(sys.argv[1])", ) + ["git"])
    assert (result.exit_code, result.stdout.strip()) == (0, "git")


def test_argv_rules_unchanged(command_tools, tmp_path):
    with pytest.raises(TypeError):
        command_tools.run_command("python -c pass")
    with pytest.raises(PermissionError):
        command_tools.run_command(_py("pass"), cwd="..")
    denials = [e for e in command_tools.captured if e["event_type"] == "agent.tool.denied"]
    assert [e["payload"]["denial_reason"] for e in denials] == [
        "argv_must_be_array",
        "outside_workspace_root",
    ]


def test_programs_run_by_name_or_by_path(command_tools):
    by_path = command_tools.run_command([str(PYTHON), "-c", "print('absolute')"])
    assert (by_path.exit_code, by_path.stdout.strip()) == (0, "absolute")

    scripts = command_tools.workspace_root / "scripts"
    scripts.mkdir()
    (scripts / "hello.py").write_text("print('hello')\n")
    relative = command_tools.run_command([PYTHON.name, "hello.py"], cwd="scripts")
    assert (relative.exit_code, relative.stdout.strip(), relative.cwd) == (0, "hello", "scripts")


@POSIX_ONLY
def test_a_project_script_runs_by_relative_path_from_cwd(command_tools):
    scripts = command_tools.workspace_root / "scripts"
    scripts.mkdir()
    check = scripts / "check.sh"
    check.write_text("#!/bin/sh\necho checked \"$1\"\n")
    check.chmod(0o755)

    from_root = command_tools.run_command(["./scripts/check.sh", "a"])
    from_dir = command_tools.run_command(["./check.sh", "b"], cwd="scripts")

    assert (from_root.exit_code, from_root.stdout.strip()) == (0, "checked a")
    assert (from_dir.exit_code, from_dir.stdout.strip()) == (0, "checked b")
    assert command_tools.captured[-1]["payload"]["command"] == "check.sh"


@POSIX_ONLY
def test_a_path_keeps_its_symlink_so_a_virtualenv_python_stays_itself(command_tools):
    bin_dir = command_tools.workspace_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(PYTHON)

    executable = command_tools._executable_path(
        command_name="python", argv=[".venv/bin/python"], cwd=command_tools.workspace_root
    )

    assert executable == str(bin_dir / "python")


BLOCKING_CASES = [
    (["git", "push"], "git"),
    (["GIT"], "git"),
    (["/usr/bin/git", "status"], "git"),
    (["C:\\Program Files\\Git\\bin\\git.exe", "status"], "git"),
    (["./git"], "git"),
    (["git/"], "git"),
    (["bash", "-c", "cd src && git status"], "git"),
    (["/bin/bash", "-lc", "/usr/bin/git diff"], "git"),
    (["sh", "-c", "echo $(git rev-parse HEAD)"], "git"),
    (["zsh", "-c", "pytest; `git log`"], "git"),
    (["cmd.exe", "/c", "git status"], "git"),
    (["powershell", "-Command", "& 'C:\\Tools\\git.exe' status"], "git"),
    (["env", "GIT_PAGER=cat", "git", "log"], "git"),
    (["xargs", "-I{}", "git", "add", "{}"], "git"),
    (["timeout", "60", "git", "fetch"], "git"),
    (["sudo", "git", "clean", "-fdx"], "git"),
    # Shells that ship with macOS, and other launchers (review 2026-10-10).
    (["tcsh", "-c", "git --version"], "git"),
    (["/bin/csh", "-c", "git --version"], "git"),
    (["script", "-q", "/dev/null", "git", "--version"], "git"),
    (["busybox", "sh", "-c", "git"], "git"),
    (["wsl.exe", "git", "status"], "git"),
    (["arch", "-arm64", "git"], "git"),
    (["npx", "git"], "git"),
    (["uv", "run", "git", "push"], "git"),
    (["uv", "--quiet", "run", "git"], "git"),
    # Options before the subcommand may take values (review round 3).
    (["uv", "--directory", "sub", "run", "git", "push"], "git"),
    (["npm", "--prefix", "web", "exec", "git", "status"], "git"),
    (["pnpm", "--filter", "web", "exec", "git"], "git"),
    (["yarn", "workspace", "web", "exec", "git", "status"], "git"),
    (["hatch", "env", "run", "--", "git", "status"], "git"),
    (["poetry", "-C", ".", "run", "git", "push"], "git"),
    # Aliases and other subcommands that run a program (review round 4).
    (["npm", "x", "-c", "git push"], "git"),
    (["npm", "exe", "-c", "git push"], "git"),
    (["npm", "explore", "pkg", "--", "git", "push"], "git"),
    (["bun", "x", "git", "status"], "git"),
    (["mise", "x", "--", "git", "status"], "git"),
    (["pyenv", "exec", "git"], "git"),
    # A version suffix does not hide the program (review round 5).
    (["npx", "git@2", "status"], "git"),
    (["corepack", "git@9.0.0", "install"], "git"),
    (["pnpm", "dlx", "git@latest"], "git"),
    (["conda", "run", "-n", "env", "git"], "git"),
    (["npm", "exec", "--", "git"], "git"),
    (["npm", "run-script", "git"], "git"),
    (["mise", "exec", "--", "git", "status"], "git"),
    (["su", "-c", "git push"], "git"),
    (["nix-shell", "--run", "git log"], "git"),
    # Quotes only group a word; braces and commas separate words.
    (["bash", "-c", "g''it push"], "git"),
    (["bash", "-c", "echo {git,x}"], "git"),
    # Windows drops trailing dots and spaces from a program name.
    (["git.exe.", "push"], "git"),
    (["GIT.EXE ", "status"], "git"),
    (["grep", "-r", "git", "."], None),
    (["python", "-c", "import subprocess"], None),
    (["bash", "-c", "pytest -q"], None),
    (["bash", "-c", 'echo "a"git'], None),
    # Every argument of a package manager counts, so a package or environment
    # named like a blocked program is refused too (fail-safe).
    (["uv", "pip", "install", "git"], "git"),
    (["conda", "create", "-n", "x", "git=2.40"], "git"),
    (["npm", "install", "x", "git"], "git"),
    (["uv", "pip", "install", "requests"], None),
    (["npm", "test"], None),
    (["gitk"], None),
    (["legit"], None),
    (["git-lfs"], None),
]


@pytest.mark.parametrize(("argv", "expected"), BLOCKING_CASES)
def test_blocked_program_matching(argv, expected):
    assert blocked_program(argv, ["Git"]) == expected
    assert blocked_program(argv, []) is None


def test_blocked_program_matches_the_servers_profile_rule():
    """The backend validates verify commands with its own copy of the rule."""
    from agents.tools import blocked_command_in, command_key as server_command_key

    corpus = [argv for argv, _expected in BLOCKING_CASES] + [
        ["make", "test"],
        ["python3", "-m", "pytest"],
        ["pwsh", "-Command", "Remove-Item x; make"],
        ["nice", "-n", "5", "rm", "-rf", "build"],
        ["bash"],
        [""],
    ]
    for denied in (["git"], ["GIT", "rm"], ["python3", "make"], ["make.exe"], []):
        for argv in corpus:
            assert blocked_program(argv, denied) == blocked_command_in(argv, denied), (argv, denied)
    # The rule's constants must be the same in both copies, not just on this corpus.
    import agents.tools as server_rule
    import code4me2_agent.command_tools as runtime_rule

    assert runtime_rule._COMMAND_RUNNERS == server_rule._COMMAND_RUNNERS
    assert runtime_rule._LAUNCHER_SUFFIXES == server_rule._LAUNCHER_SUFFIXES
    assert runtime_rule._QUOTES.pattern == server_rule._QUOTES.pattern
    assert runtime_rule._WORD_SEPARATORS.pattern == server_rule._WORD_SEPARATORS.pattern
    for name in ("git", "Git.EXE", "./tools/run.cmd", "C:\\x\\y.bat", "dir/", "a.com", ".com", "",
                 "git.exe.", "npm.cmd. ", "...", "git..exe"):
        assert command_key(name) == server_command_key(name), name


def test_a_trailing_dot_does_not_hide_a_batch_file(tmp_path):
    tools = _make_tools(tmp_path)
    for program in ("npm.cmd.", "npm.cmd ", "build.BAT."):
        with pytest.raises(PermissionError, match="runs as a batch file"):
            tools.run_command([program, "x", "&", "git", "push"])
    denials = [e for e in tools.captured if e["event_type"] == "agent.tool.denied"]
    assert {e["payload"]["denial_reason"] for e in denials} == {"unsafe_batch_arguments"}


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
    tools = _make_tools(tmp_path, acp_backend=backend)

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
def test_non_executable_program_is_a_clear_tool_error(tmp_path, monkeypatch):
    from code4me2_agent.tool_errors import ToolError

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "notexec").write_text("#!/bin/sh\necho hi\n")
    (bin_dir / "notexec").chmod(0o644)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    tools = _make_tools(tmp_path)

    with pytest.raises(ToolError) as error:
        tools.run_command(["notexec"])

    assert error.value.code == "command_not_executable"
