"""argparse CLI for the code4me-e2e harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import stack, suite, ui, workflow
from .config import ScenarioError, load_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code4me_e2e",
        description="Standalone end-to-end harness for the Code4Me research participant workflow.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    test = sub.add_parser("test", help="run the complete automated regression gate")
    test.add_argument("--layer", choices=["all", "backend", "plugin", "browser"], default="all")
    test.add_argument("--scenario")
    test.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    test.add_argument("--run-dir")
    test.add_argument("--json", action="store_true", dest="as_json")
    test.add_argument("--keep-stack", action="store_true")

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

    return parser


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

    if args.command != "doctor" and not workflow._own_stack(scenario):
        raise ScenarioError("Mutating tests require the disposable stack's loopback base_url; doctor can inspect other backends")

    if args.command == "test":
        return suite.run(scenario, run_dir=args.run_dir, keep_stack=args.keep_stack,
                         as_json=args.as_json, layer=args.layer)

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


def main(argv: Optional[List[str]] = None) -> int:
    import fcntl
    # The Gradle sandbox and Compose ports are shared across invocations.
    cache = stack.E2E_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    with (cache / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another e2e command is running in this workspace.", file=sys.stderr)
            return 2
        try:
            return _main(argv)
        except (ScenarioError, workflow.WorkflowError, stack.StackError, OSError) as error:
            print(f"e2e error: {error}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
