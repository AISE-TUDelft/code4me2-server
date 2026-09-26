"""argparse CLI for the code4me-e2e harness."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import prereqs, real_agents, stack, suite, ui, workflow
from .config import AgentProbeReason, ScenarioError, load_scenario
from .paths import E2E_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code4me_e2e",
        description="Standalone end-to-end harness for the Code4Me research participant workflow.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    test = sub.add_parser("test", help="run the complete automated regression gate")
    test.add_argument("--layer", choices=["all", "backend", "plugin", "browser", "agents"], default="all")
    test.add_argument("--scenario")
    test.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    test.add_argument("--run-dir")
    test.add_argument("--json", action="store_true", dest="as_json")
    test.add_argument("--keep-stack", action="store_true")
    test.add_argument("--no-setup", action="store_true", dest="no_setup",
                      help="check prerequisites without provisioning anything (missing ones block their layers)")

    setup = sub.add_parser(
        "setup",
        help="check and provision every host prerequisite of a layer (idempotent)",
    )
    setup.add_argument("--layer", choices=["all", "backend", "plugin", "browser", "agents"], default="all")
    setup.add_argument("--check", action="store_true", help="report only; start, build or install nothing")
    setup.add_argument("--scenario", default=None)
    setup.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    setup.add_argument("--json", action="store_true", dest="as_json")

    stack_parser = sub.add_parser("stack", help="manage the disposable backend stack")
    stack_parser.add_argument("action", choices=["up", "down", "status"])
    stack_parser.add_argument("--project-name", default=None)
    stack_parser.add_argument("--scenario", default=None)
    stack_parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")

    doctor = sub.add_parser("doctor", help="diagnose a backend before running the workflow")
    doctor.add_argument("--base-url", default=None)
    doctor.add_argument("--scenario", default=None)
    doctor.add_argument("--project-name", default=None)
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")

    run = sub.add_parser("run", help="run the end-to-end workflow")
    run.add_argument("--scenario", default=None)
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    run.add_argument("--only", default=None, help="comma-separated step ids")
    run.add_argument("--from", dest="from_step", default=None, help="start at this step")
    run.add_argument("--json", action="store_true", dest="as_json")
    run.add_argument("--run-dir", default=None)
    run.add_argument("--keep-stack", action="store_true")

    step = sub.add_parser("step", help="run (or re-run) one workflow step")
    step.add_argument("step_id")
    step.add_argument("--state", default=None, help="path to an existing state.json")
    step.add_argument("--run-dir", default=None)
    step.add_argument("--scenario", default=None)
    step.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    step.add_argument("--json", action="store_true", dest="as_json")

    plugin_test = sub.add_parser(
        "plugin-test",
        help="run the plugin's own Kotlin code in an IntelliJ fixture against the live backend",
    )
    plugin_test.add_argument("--run-dir", default=None)
    plugin_test.add_argument("--scenario", default=None)
    plugin_test.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    plugin_test.add_argument("--json", action="store_true", dest="as_json")
    plugin_test.add_argument("--keep-stack", action="store_true")

    ui_test = sub.add_parser(
        "ui-test",
        help="boot a real IntelliJ sandbox and drive the plugin through robot-server",
    )
    ui_test.add_argument("--run-dir", default=None)
    ui_test.add_argument("--scenario", default=None)
    ui_test.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ui_test.add_argument("--json", action="store_true", dest="as_json")
    ui_test.add_argument("--keep-stack", action="store_true")
    ui_test.add_argument(
        "--keep-ide",
        action="store_true",
        help="leave the sandbox IDE running after the suite (default: stop only this harness's IDE)",
    )

    agent_probe = sub.add_parser(
        "agent-probe",
        help="identify and ACP-probe a host-installed Goose/Codex agent (no Compose/IDE)",
    )
    agent_probe.add_argument("--framework", choices=["goose", "codex"], required=True)
    agent_probe.add_argument("--scenario", default=None)
    agent_probe.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    agent_probe.add_argument("--run-dir", default=None)
    agent_probe.add_argument("--executable", default=None, help="host executable override")
    agent_probe.add_argument("--home", default=None, help="isolated agent home override")
    agent_probe.add_argument("--timeout", type=int, default=120, help="per-request probe timeout (seconds)")
    agent_probe.add_argument(
        "--detect-only",
        action="store_true",
        dest="detect_only",
        help="resolve and identify the executable; never launch a session or a model turn",
    )
    agent_probe.add_argument("--json", action="store_true", dest="as_json")
    agent_probe.add_argument("--local-provider", action="store_true",
                             help="use a local streaming model fixture and require its unique answer")
    agent_probe.add_argument("--quota-exhausted", action="store_true", dest="quota_exhausted",
                             help="with --local-provider: the fixture answers the research gateway's "
                                  "402 quota_exhausted; pass only on a typed quota block after one request")

    return parser


def run_agent_probe(
    scenario,
    framework: str,
    *,
    run_dir: Optional[str] = None,
    executable: Optional[str] = None,
    home: Optional[str] = None,
    timeout: int = 120,
    detect_only: bool = False,
    as_json: bool = False,
    local_provider: bool = False,
    quota_exhausted: bool = False,
) -> int:
    """Standalone probe for a real participant-installed agent.

    Uses no Compose stack and no IDE. ``BLOCKED`` exits nonzero: a missing
    prerequisite can never be reported as a pass.
    """
    if timeout < 1:
        raise ScenarioError("--timeout must be >= 1")
    agent = scenario.agent
    agent_executable = executable or agent.executable
    agent_home = home or agent.agent_home
    agent_args = list(agent.agent_command_args) if agent.agent_command_args else None
    explicit = agent_executable or agent.agent_command or agent.agent_package or \
        os.environ.get(real_agents.ENV_OVERRIDES.get(framework, ""), "")
    blocked = None
    if framework == "codex" and not explicit:
        # Default to the plugin's vendored Codex ACP adapter, the adapter the
        # plugin ships, never silently to whatever is on PATH. The probe runs
        # outside the workspace lock, so it never builds the adapter itself;
        # only its own launcher is (re)written, and not by --detect-only.
        resolved: dict = {}
        check = prereqs.check_codex_acp(False, executables=resolved, write_wrapper=not detect_only)
        if resolved.get("codex"):
            agent_executable = resolved["codex"]
        else:
            blocked = real_agents.ProbeResult.blocked(
                framework, AgentProbeReason.MISSING_BINARY,
                f"the vendored Codex ACP adapter is not ready ({check.status}: {check.detail}); run "
                "`python3 -m code4me_e2e setup --layer agents`, or pass --executable / set "
                "CODE4ME_E2E_CODEX_EXECUTABLE for another codex-acp",
                protocol={"adapter_check": check.to_dict()},
            )

    if blocked is not None:
        result = blocked
    elif detect_only:
        result = real_agents.probe_identity(
            framework,
            executable=agent_executable,
            command=agent.agent_command,
            package=agent.agent_package,
        )
    else:
        from . import acp_probe

        run_path = Path(run_dir).resolve() if run_dir else (
            E2E_DIR / ".cache/agent-probe" /
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{framework}"
        )
        if local_provider:
            from . import native_agents
            result = native_agents.probe(framework, run_path, executable=agent_executable, timeout=timeout,
                                        command=agent.agent_command, package=agent.agent_package,
                                        argv=agent_args, home=agent_home, quota_exhausted=quota_exhausted)
        else:
            result = acp_probe.run_probe(
                framework,
                run_dir=run_path,
                executable=agent_executable,
                command=agent.agent_command,
                package=agent.agent_package,
                argv=agent_args,
                home=agent_home,
                prompt=scenario.message.prompt,
                timeout=timeout,
            )

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"{result.framework}  {result.status}" + (f"  ({result.reason})" if result.reason else ""))
        if result.identity is not None:
            print(f"  executable: {result.identity.path}  source={result.identity.source}")
            print(f"  sha256: {result.identity.sha256}  size={result.identity.size}  version={result.identity.version}")
        if result.detail:
            print(f"  detail: {result.detail}")
        for check in result.checks:
            print(f"  check: {json.dumps(dict(check), sort_keys=True)}")
    return 0 if result.passed else 1


def _load(args: argparse.Namespace):
    overrides = list(getattr(args, "set", []) or [])
    base_url = getattr(args, "base_url", None)
    if base_url:
        overrides = [f"base_url={base_url}"] + overrides
    scenario = load_scenario(args.scenario, overrides)
    project = getattr(args, "project_name", None)
    if project:
        from .config import finalize_scenario
        scenario.stack.project_name = project
        finalize_scenario(scenario)
    return scenario


def _main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scenario = _load(args)
    except ScenarioError as error:
        print(f"scenario error: {error}", file=sys.stderr)
        return 2

    if args.command not in ("doctor", "agent-probe") and not workflow._own_stack(scenario):
        raise ScenarioError("Mutating tests require the disposable stack's loopback base_url; doctor can inspect other backends")

    if args.command == "test":
        return suite.run(scenario, run_dir=args.run_dir, keep_stack=args.keep_stack,
                         as_json=args.as_json, layer=args.layer, provision=not args.no_setup)

    if args.command == "setup":
        planned = suite.plan_layers(args.layer)
        result = prereqs.prepare(scenario, planned, provision=not args.check)
        if args.as_json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            prereqs.print_table(result, stream=sys.stdout)
            for layer_name, state in result.to_dict()["layers"].items():
                print(f"layer {layer_name:<8} {state}")
        return 0 if result.ready else 1

    if args.command == "stack":
        if args.action == "up":
            print(stack.up(scenario))
            return 0
        if args.action == "down":
            print(stack.down(scenario))
            return 0
        print(stack.ps(scenario))
        return 0

    if args.command == "doctor":
        return workflow.run_doctor(scenario, as_json=args.as_json)

    if args.command == "agent-probe":
        return run_agent_probe(
            scenario,
            args.framework,
            run_dir=args.run_dir,
            executable=args.executable,
            home=args.home,
            timeout=args.timeout,
            detect_only=args.detect_only,
            as_json=args.as_json,
            local_provider=args.local_provider,
            quota_exhausted=args.quota_exhausted,
        )

    if args.command == "run":
        if args.run_dir and str(args.run_dir).endswith("state.json"):
            args.run_dir = str(Path(args.run_dir).parent)
        return workflow.run_workflow(
            scenario,
            only=args.only,
            from_step=args.from_step,
            run_dir=args.run_dir,
            keep_stack=args.keep_stack,
            as_json=args.as_json,
        )

    if args.command == "step":
        run_dir = args.run_dir
        if args.state:
            run_dir = str(Path(args.state).parent)
        return workflow.run_single_step(
            scenario, args.step_id, run_dir=run_dir, as_json=args.as_json
        )

    if args.command == "plugin-test":
        run_dir = args.run_dir
        if run_dir and str(run_dir).endswith("state.json"):
            run_dir = str(Path(run_dir).parent)
        return workflow.run_plugin_test(
            scenario,
            run_dir=run_dir,
            keep_stack=args.keep_stack,
            as_json=args.as_json,
        )

    if args.command == "ui-test":
        run_dir = args.run_dir
        if run_dir and str(run_dir).endswith("state.json"):
            run_dir = str(Path(run_dir).parent)
        return ui.run_ui_test(
            scenario,
            run_dir=run_dir,
            keep_ide=args.keep_ide,
            keep_stack=args.keep_stack,
            as_json=args.as_json,
        )

    parser.error("unknown command")
    return 2


class _Terminated(KeyboardInterrupt):
    """SIGTERM, unwound like Ctrl-C so every ``finally`` stops what this run started."""


def _raise_terminated(signum, _frame):
    raise _Terminated(f"terminated by signal {signum}")


def main(argv: Optional[List[str]] = None) -> int:
    # A `kill` or an agent's tool timeout sends SIGTERM. Without a handler the
    # interpreter dies at once and detached builds, installs or the sandbox IDE
    # keep running; unwinding runs the cleanup that Ctrl-C already runs.
    try:
        previous = signal.signal(signal.SIGTERM, _raise_terminated)
    except ValueError:  # not the main thread: keep the default behaviour
        previous = None
    try:
        return _locked_main(argv)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _locked_main(argv: Optional[List[str]]) -> int:
    import fcntl
    # The Gradle sandbox and Compose ports are shared across invocations. The
    # agent probe uses neither, so it must run even while a gate is active.
    tokens = list(argv) if argv is not None else sys.argv[1:]
    if tokens[:1] == ["agent-probe"]:
        return _execute(argv)
    cache = stack.E2E_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    with (cache / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another e2e command is running in this workspace.", file=sys.stderr)
            return 2
        return _execute(argv)


def _execute(argv: Optional[List[str]]) -> int:
    try:
        return _main(argv)
    except (ScenarioError, workflow.WorkflowError, stack.StackError, OSError) as error:
        print(f"e2e error: {error}", file=sys.stderr)
        return 2
    except _Terminated:
        return 143
    except KeyboardInterrupt:
        return 130
