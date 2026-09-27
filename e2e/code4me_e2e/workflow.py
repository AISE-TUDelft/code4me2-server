"""Workflow orchestration: step selection, resumable execution, reporting."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import report, stack
from .config import Scenario
from .http import HttpError
from .paths import E2E_DIR, require_workspace
from .steps import STEP_ORDER, STEPS, Ctx, StepBlocked, StepFailure, StepResult

#: Steps that leave an ACTIVE enrollment the plugin test can bootstrap.
PLUGIN_PREFIX_STEPS: List[str] = STEP_ORDER[: STEP_ORDER.index("plugin_join") + 1]

#: Gradle task + test class the plugin-in-IntelliJ layer runs.
PLUGIN_TEST_TASK = ":integration-tests:test"
PLUGIN_TEST_CLASS = "integration.LiveStudyWorkflowPluginTest"
PLUGIN_TEST_STEP_ID = "plugin_test"


class WorkflowError(RuntimeError):
    pass


def _select_steps(only: Optional[str], from_step: Optional[str]) -> List[str]:
    steps = list(STEP_ORDER)
    if from_step:
        if from_step not in STEP_ORDER:
            raise WorkflowError(
                f"unknown step {from_step!r}; known steps: {', '.join(STEP_ORDER)}"
            )
        steps = steps[steps.index(from_step) :]
    if only:
        requested = [item.strip() for item in only.split(",") if item.strip()]
        for step in requested:
            if step not in STEP_ORDER:
                raise WorkflowError(
                    f"unknown step {step!r}; known steps: {', '.join(STEP_ORDER)}"
                )
        steps = [step for step in steps if step in requested]
    return steps


def _own_stack(scenario: Scenario) -> bool:
    base = (scenario.base_url or "").rstrip("/")
    return base in (
        f"http://localhost:{scenario.stack.backend_port}",
        f"http://127.0.0.1:{scenario.stack.backend_port}",
    )


def _stack_ready(scenario: Scenario) -> bool:
    status, payload = stack.capabilities(scenario)
    return status == 200 and isinstance(payload, dict) and bool(payload.get("schema_ready"))


def _run_one(ctx: Ctx, step_id: str) -> StepResult:
    start_index = len(ctx.exchange_log)
    started = time.monotonic()
    status = "PASS"
    details: Dict[str, Any] = {}
    fix_hint = ""
    try:
        details = STEPS[step_id](ctx) or {}
    except StepBlocked as error:
        status = "BLOCKED"
        details = {**error.details, "message": error.message}
        fix_hint = error.fix_hint
    except StepFailure as error:
        status = "FAIL"
        details = {**error.details, "message": error.message}
        fix_hint = error.fix_hint
    except HttpError as error:
        status = "FAIL"
        details = {"error": str(error)}
        fix_hint = "the backend could not be reached; verify the stack is up"
    except Exception as error:  # noqa: BLE001 - a crash is a failed step, never a lost run
        status = "FAIL"
        details = {"error": repr(error), "traceback": traceback.format_exc()}
        fix_hint = "unexpected harness error; see traceback in report.json"
    duration_ms = int((time.monotonic() - started) * 1000)

    exchanges = ctx.records_since(start_index, limit=1000)
    if status != "PASS" and exchanges:
        details = {**details, "exchanges": exchanges[-6:]}
    if status == "FAIL":
        server_errors = [
            record
            for record in exchanges
            if isinstance(record.get("status"), int) and record["status"] >= 500
        ]
        if server_errors:
            finding_id = f"BACKEND_5XX_{step_id.upper()}"
            if not any(item.get("id") == finding_id for item in ctx.findings):
                ctx.findings.append(
                    {
                        "id": finding_id,
                        "severity": "bug",
                        "message": (
                            f"step {step_id} received HTTP "
                            f"{server_errors[0]['status']} from "
                            f"{server_errors[0]['method']} {server_errors[0]['path']}: "
                            "an unhandled backend exception. This is an application bug, "
                            "not a harness failure; the harness keeps the exact request/"
                            "response and the backend traceback for the report."
                        ),
                        "location": "see logs/backend.log for the Python traceback",
                    }
                )
                ctx.state["findings"] = ctx.findings
    return StepResult(step_id, status, duration_ms, details, fix_hint)


def run_workflow(
    scenario: Scenario,
    *,
    only: Optional[str] = None,
    from_step: Optional[str] = None,
    run_dir: Optional[str] = None,
    keep_stack: bool = False,
    as_json: bool = False,
) -> int:
    steps = _select_steps(only, from_step)
    if not steps:
        raise WorkflowError("no steps selected")

    if run_dir:
        run_path = Path(run_dir).resolve()
    elif from_step:
        run_path = report.latest_run_dir(E2E_DIR) or report.new_run_dir(E2E_DIR)
    else:
        run_path = report.new_run_dir(E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)

    state = report.read_state(run_path)
    # Fresh accounts make repetitions independent of terminal enrollments and
    # immutable release/profile selections. Resume with the same credentials.
    if not state:
        from dataclasses import asdict
        for role in ("admin", "researcher", "participant"):
            account = getattr(scenario, role)
            local, domain = account.email.rsplit("@", 1)
            # The full path: layer state directories share names (browser-state).
            suffix = __import__("hashlib").sha256(str(run_path).encode()).hexdigest()[:10]
            account.email = f"{local[:38-len(domain)]}+{suffix}@{domain}"
        state["accounts"] = {role: asdict(getattr(scenario, role))
                             for role in ("admin", "researcher", "participant")}
    else:
        restore_accounts(scenario, state)
    started_at = report.utc_now_iso()
    run_id = run_path.name

    up_started = False
    if _own_stack(scenario):
        up_started = not _stack_ready(scenario)
        stack.up(scenario)

    ctx = Ctx(scenario, state, run_path, fresh_login="create_accounts" not in steps)
    ctx.findings = list(state.get("findings") or [])

    results: List[Dict[str, Any]] = []
    failed_step: Optional[str] = None
    blocked_by: Optional[str] = None

    for index, step_id in enumerate(steps):
        start_index = len(ctx.exchange_log)
        result = _run_one(ctx, step_id)
        report.append_http(run_path, ctx.exchange_log[start_index:])
        results.append(result.to_dict())
        state.setdefault("step_results", {})[step_id] = result.to_dict()
        ctx.snapshot_cookies()
        state["findings"] = ctx.findings
        report.write_state(run_path, state)

        if result.status in ("FAIL", "BLOCKED"):
            if result.status == "BLOCKED":
                blocked_by = step_id
            else:
                failed_step = step_id
            for remaining in steps[index + 1 :]:
                results.append(
                    StepResult(
                        remaining,
                        "SKIP",
                        0,
                        {"reason": f"not run after {step_id}"},
                        "",
                    ).to_dict()
                )
            break

    finished_at = report.utc_now_iso()
    # This invocation passes or fails on the steps it ran. Steps merged from the
    # run's shared report (another layer's result, the gate's prerequisites
    # record) stay in the report but must not decide this return code.
    own_ok = not any(item["status"] in ("FAIL", "BLOCKED") for item in results)
    previous = run_path / "report.json"
    if previous.is_file():
        old = json.loads(previous.read_text()).get("steps", [])
        selected = {item["id"] for item in results}
        results = [item for item in old if item["id"] not in selected] + results
    failed_step = next((s["id"] for s in results if s["status"] == "FAIL"), None)
    blocked_by = next((s["id"] for s in results if s["status"] == "BLOCKED"), None)
    built = report.build_report(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        scenario=scenario,
        steps=results,
        findings=ctx.findings,
        failed_step=failed_step,
        blocked_by=blocked_by,
    )
    report.write_report(run_path, built)

    if not own_ok:
        log_path = report.capture_backend_logs(scenario, run_path)
        built["backend_log"] = log_path
        report.write_report(run_path, built)

    report.print_summary(built, as_json)

    if own_ok and up_started and not keep_stack:
        try:
            stack.down(scenario)
        except Exception as error:  # noqa: BLE001
            print(f"warning: could not tear the stack down: {error}", file=sys.stderr)

    if ctx.stub:
        ctx.stub.stop()
    return 0 if own_ok else 1


def run_single_step(
    scenario: Scenario,
    step_id: str,
    *,
    run_dir: Optional[str] = None,
    as_json: bool = False,
) -> int:
    if step_id not in STEP_ORDER:
        raise WorkflowError(
            f"unknown step {step_id!r}; known steps: {', '.join(STEP_ORDER)}"
        )
    if run_dir:
        run_path = Path(run_dir).resolve()
    else:
        run_path = report.latest_run_dir(E2E_DIR) or report.new_run_dir(E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)
    state = report.read_state(run_path)
    restore_accounts(scenario, state)
    ctx = Ctx(scenario, state, run_path, fresh_login=step_id != "create_accounts")
    ctx.findings = list(state.get("findings") or [])
    result = _run_one(ctx, step_id)
    state.setdefault("step_results", {})[step_id] = result.to_dict()
    ctx.snapshot_cookies()
    state["findings"] = ctx.findings
    report.write_state(run_path, state)
    report.append_http(run_path, ctx.exchange_log)
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"{result.id}  {result.status}  ({result.duration_ms} ms)")
        if result.details:
            print(json.dumps(result.details, indent=2, default=str))
        if result.fix_hint:
            print(f"fix: {result.fix_hint}")
    if ctx.stub:
        ctx.stub.stop()
    return 0 if result.status == "PASS" else 1


def run_doctor(scenario: Scenario, *, as_json: bool = False) -> int:
    """Run only the ``doctor`` step, without creating a run directory."""
    ctx = Ctx(scenario, {}, report.runs_root(E2E_DIR))
    result = _run_one(ctx, "doctor")
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"doctor  {result.status}  ({result.duration_ms} ms)")
        print(json.dumps(result.details, indent=2, default=str))
        if result.fix_hint:
            print(f"fix: {result.fix_hint}")
    if ctx.stub:
        ctx.stub.stop()
    return 0 if result.status == "PASS" else 1


# ---------------------------------------------------------------------------
# Plugin layer: run the plugin's own Kotlin code inside an IntelliJ fixture
# against the live backend.
# ---------------------------------------------------------------------------


def restore_accounts(scenario: Scenario, state: dict) -> None:
    for role, values in state.get("accounts", {}).items():
        if role in ("admin", "researcher", "participant"):
            for key in ("email", "password", "name"):
                setattr(getattr(scenario, role), key, values[key])


def _plugin_state_usable(state: Dict[str, Any]) -> bool:
    """True when ``state`` leaves an ACTIVE enrollment for the plugin test.

    Requires a join code, an enrollment id, a PASSING ``plugin_join``, and no
    completed ``revoke`` (which revokes the enrollment).
    """
    if not state:
        return False
    if not state.get("join_code") or not state.get("enrollment_id"):
        return False
    results = state.get("step_results") or {}
    if (results.get("plugin_join") or {}).get("status") != "PASS":
        return False
    if (results.get("revoke") or {}).get("status") == "PASS":
        return False
    return True


def _find_usable_run_dir(scenario: Scenario, e2e_dir: Path) -> Optional[Path]:
    """Newest existing run whose state still leaves an ACTIVE enrollment."""
    root = report.runs_root(e2e_dir)
    if not root.is_dir():
        return None
    candidates = sorted(
        (path for path in root.iterdir() if (path / "state.json").is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        state = report.read_state(path)
        if _plugin_state_usable(state) and _state_still_live(scenario, state):
            return path
    return None


def _state_still_live(scenario: Scenario, state: Dict[str, Any]) -> bool:
    """True when the state's enrollment is still ACTIVE on the current stack.

    A run recorded before ``stack down -v`` points at a database that no longer
    exists; reusing it would send the plugin at a dead enrollment. For a stack
    the harness does not own the database cannot be probed, so the structural
    state is trusted.
    """
    enrollment_id = state.get("enrollment_id")
    if not enrollment_id:
        return False
    if not _own_stack(scenario):
        return True
    ok, out = stack.try_psql(
        scenario,
        "SELECT status FROM research_enrollment WHERE enrollment_id='"
        + str(enrollment_id).replace("'", "")
        + "';",
    )
    return ok and out.strip().upper() == "ACTIVE"


def _record_plugin_step(run_path: Path, scenario: Scenario, result: StepResult) -> Dict[str, Any]:
    """Append/replace the ``plugin_test`` step in this run's ``report.json``."""
    report_path = run_path / "report.json"
    if report_path.is_file():
        try:
            built = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            built = None
    else:
        built = None
    if not isinstance(built, dict):
        built = report.build_report(
            run_id=run_path.name,
            started_at=report.utc_now_iso(),
            finished_at=report.utc_now_iso(),
            scenario=scenario,
            steps=[],
            findings=[],
        )
    steps = [step for step in built.get("steps", []) if step.get("id") != result.id]
    steps.append(result.to_dict())
    built["steps"] = steps
    built["finished_at"] = report.utc_now_iso()
    built["failed_step"] = next((step["id"] for step in steps if step["status"] == "FAIL"), None)
    built["blocked_by"] = next((step["id"] for step in steps if step["status"] == "BLOCKED"), None)
    built["next_actions"] = report.derive_next_actions(built)
    report.write_report(run_path, built)
    return built


def run_plugin_test(scenario: Scenario, *, run_dir: Optional[str] = None,
                    keep_stack: bool = False, as_json: bool = False) -> int:
    from . import process
    from .ui import parse_gradle_xml, require_test_evidence
    plugin = require_workspace() / "code4me2"
    run_path = Path(run_dir).resolve() if run_dir else report.new_run_dir(E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    details = {}
    status = "FAIL"
    try:
        state = report.read_state(run_path)
        if not (_plugin_state_usable(state) and _state_still_live(scenario, state)):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run_workflow(scenario, only=",".join(PLUGIN_PREFIX_STEPS),
                                  run_dir=str(run_path), keep_stack=True)
            if rc:
                raise RuntimeError("Workflow prefix failed; see report.json")
            state = report.read_state(run_path)
        restore_accounts(scenario, state)
        env = {**os.environ, "CODE4ME_E2E_BASE_URL": scenario.base_url,
               "CODE4ME_E2E_EMAIL": scenario.participant.email,
               "CODE4ME_E2E_PASSWORD": scenario.participant.password,
               "CODE4ME_E2E_JOIN_CODE": state["join_code"]}
        xml_path = plugin / "integration-tests/build/test-results/test/TEST-integration.LiveStudyWorkflowPluginTest.xml"
        xml_path.unlink(missing_ok=True)
        rc = process.run([str(plugin / "gradlew"), PLUGIN_TEST_TASK, "--tests", PLUGIN_TEST_CLASS,
                          "--rerun", "--console=plain"], cwd=plugin, env=env,
                         log_path=run_path / "plugin-test.log", timeout=600)
        details = {"exit_code": rc, "junit": parse_gradle_xml(xml_path)}
        require_test_evidence(rc, details["junit"])
        status = "PASS"
    except Exception as error:
        details["error"] = str(error)
        report.capture_backend_logs(scenario, run_path)
    result = StepResult(PLUGIN_TEST_STEP_ID, status, int((time.monotonic()-started)*1000), details,
                        "" if status == "PASS" else f"Inspect {run_path}/plugin-test.log")
    built = _record_plugin_step(run_path, scenario, result)
    report.print_summary(built, as_json)
    if status == "PASS" and not keep_stack:
        stack.down(scenario)
    return 0 if status == "PASS" else 1
