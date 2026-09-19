"""Launch an isolated real IDE, drive its UI, then act as its ACP host."""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import acp, process, report, runtime, stack
from .config import Scenario
from .paths import E2E_DIR, require_workspace
from .steps import Ctx, StepResult
from .workflow import PLUGIN_PREFIX_STEPS, _plugin_state_usable, _state_still_live, restore_accounts, run_workflow

UI_TEST_STEP_ID = "ui_test"
UI_TEST_TASK = ":ui-tests:test"
UI_TEST_CLASS = "ui.Code4MeUiNavigationTest"
EXPECTED_UI_STEPS = {"plugin_loaded", "settings_navigation", "sign_in", "enrollment_activation",
                     "status_surface", "acp_registration", "prepare_agent"}

def parse_results_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _overall_from_results(results: Dict[str, Any]) -> Tuple[str, List[str]]:
    steps = results.get("steps") or []
    reasons: List[str] = []
    statuses = [str(step.get("status", "")).upper() for step in steps]
    for step in steps:
        status = str(step.get("status", "")).upper()
        if status in ("FAIL", "BLOCKED"):
            reasons.append(f"{step.get('id')}: {status} - {step.get('reason') or 'no reason recorded'}")
    if "FAIL" in statuses:
        return "FAIL", reasons
    if "BLOCKED" in statuses:
        return "BLOCKED", reasons
    if statuses and all(status == "PASS" for status in statuses):
        return "PASS", reasons
    return "UNKNOWN", reasons


def parse_gradle_xml(xml_path: Path) -> Dict[str, Any]:
    """Minimal JUnit XML summary (tests/failures/errors/skipped + failure text)."""
    if not xml_path.is_file():
        return {}
    try:
        import xml.etree.ElementTree as ET

        root = ET.parse(xml_path).getroot()
    except Exception:  # noqa: BLE001 - malformed output is reported as unknown
        return {}
    summary = {
        "tests": int(root.get("tests", "0") or 0),
        "failures": int(root.get("failures", "0") or 0),
        "errors": int(root.get("errors", "0") or 0),
        "skipped": int(root.get("skipped", "0") or 0),
        "failure_messages": [
            (failure.get("message") or failure.text or "").strip()
            for failure in root.iter("failure")
        ]
        + [
            (error.get("message") or error.text or "").strip()
            for error in root.iter("error")
        ],
    }
    return summary


def require_test_evidence(exit_code: int, xml: dict, results: dict | None = None, *, log_path: Path | None = None) -> None:
    if exit_code or not xml or xml.get("tests", 0) < 1 or any(
        xml.get(key, 0) for key in ("failures", "errors", "skipped")
    ):
        detail = ""
        if log_path is not None and log_path.is_file():
            # Surface the first concrete Gradle error instead of a generic hint.
            for line in log_path.read_text(errors="ignore").splitlines():
                if line.startswith(("> ", "FAILURE:", "Caused by:")) or "NoSuchFileException" in line:
                    detail = f": {line.strip()[:200]}"
                    break
        raise RuntimeError(
            "Gradle must execute passing tests with no skips; inspect the test log and JUnit XML"
            + detail
        )
    if results is not None:
        steps = results.get("steps") or []
        if {step.get("id") for step in steps} != EXPECTED_UI_STEPS or len(steps) != len(EXPECTED_UI_STEPS):
            raise RuntimeError("UI results are missing required steps")
        overall, reasons = _overall_from_results(results)
        if overall != "PASS":
            raise RuntimeError("; ".join(reasons) or "UI results did not pass")


# ---------------------------------------------------------------------------
# Report recording
# ---------------------------------------------------------------------------


def record_ui_step(run_path: Path, scenario: Scenario, result: StepResult, extra: Dict[str, Any]) -> Dict[str, Any]:
    """Append/replace the ``ui_test`` step in this run's ``report.json``."""
    report_path = run_path / "report.json"
    built: Optional[Dict[str, Any]] = None
    if report_path.is_file():
        try:
            built = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
    steps = [step for step in built.get("steps", []) if step.get("id") != UI_TEST_STEP_ID]
    payload = {**result.to_dict(), **extra}
    steps.append(payload)
    built["steps"] = steps
    built["finished_at"] = report.utc_now_iso()
    built["failed_step"] = next((step["id"] for step in steps if step["status"] == "FAIL"), None)
    built["blocked_by"] = next((step["id"] for step in steps if step["status"] == "BLOCKED"), None)
    built["next_actions"] = report.derive_next_actions(built)
    report.write_report(run_path, built)
    return built


def run_ui_test(scenario: Scenario, *, run_dir: Optional[str] = None,
                keep_ide: bool = False, keep_stack: bool = False,
                as_json: bool = False) -> int:
    run_path = Path(run_dir).resolve() if run_dir else report.new_run_dir(E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)
    plugin = require_workspace() / "code4me2"
    home = run_path / "ide-home"
    # A fresh isolated home each time; keep previous diagnostics when resuming.
    if home.exists():
        home.rename(run_path / f"ide-home-previous-{time.time_ns()}")
    home.mkdir(mode=0o700)
    started = time.monotonic()
    child = None
    stub = None
    status = "FAIL"
    details: dict = {}
    stage = "prepare"
    port = 0
    try:
        print(f"Preparing real IDE test: {run_path}", file=__import__('sys').stderr)
        gradle_args = runtime.prepare(run_path)
        prepare_rc = process.run([str(plugin / "gradlew"), ":prepareSandbox_runIdeForUiTests",
                                  *gradle_args, "--console=plain"], cwd=plugin,
                                 log_path=run_path / "ui-prepareSandbox.log")
        if prepare_rc:
            raise RuntimeError("Sandbox preparation failed; see ui-prepareSandbox.log")
        manifest = json.loads((plugin / "build/research-runtime-staging/proxy-manifest.json").read_text())
        bundle = next(p for p in manifest["platforms"]
                      if p["os"] == scenario.platform.os and p["arch"] == scenario.platform.arch)
        if not bundle["self_contained"] or not bundle.get("agent"):
            raise RuntimeError("The staged proxy and agent must be self-contained")
        digest = bundle["agent"]["digest"]
        scenario.agent.artifact_digest = digest
        scenario.agent.release_id = "e2e-ui-" + digest[:20]
        scenario.agent.profile_name = "e2e-ui-" + digest[:20]
        stage = "workflow_prefix"
        state = report.read_state(run_path)
        if not (_plugin_state_usable(state) and _state_still_live(scenario, state)):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = run_workflow(scenario, only=",".join(PLUGIN_PREFIX_STEPS),
                                  run_dir=str(run_path), keep_stack=True)
            if rc:
                raise RuntimeError("Workflow prefix failed; see report.json")
            state = report.read_state(run_path)
        restore_accounts(scenario, state)
        ctx = Ctx(scenario, state, run_path)
        ctx.ensure_stub()
        stub = ctx.stub
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        robot_url = f"http://127.0.0.1:{port}"
        results_file = run_path / "ui-test-results.json"
        xml_path = plugin / "ui-tests/build/test-results/test/TEST-ui.Code4MeUiNavigationTest.xml"
        results_file.unlink(missing_ok=True)
        xml_path.unlink(missing_ok=True)
        env = {**os.environ, "CODE4ME_UI_TEST": "1", "CODE4ME_UI_HOME": str(home),
               "CODE4ME_E2E_BASE_URL": scenario.base_url,
               "CODE4ME_E2E_EMAIL": scenario.participant.email,
               "CODE4ME_E2E_PASSWORD": scenario.participant.password,
               "CODE4ME_E2E_JOIN_CODE": state["join_code"],
               "CODE4ME_UI_ROBOT_URL": robot_url, "CODE4ME_UI_ROBOT_PORT": str(port),
               "CODE4ME_UI_PROJECT_DIR": str(run_path / "ui-project"),
               "CODE4ME_UI_RESULT_FILE": str(results_file)}
        stage = "ide_startup"
        with (run_path / "ui-ide.log").open("w") as log:
            child = subprocess.Popen([str(plugin / "gradlew"), ":runIdeForUiTests", *gradle_args,
                                      "--no-daemon", "--console=plain"], cwd=plugin, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + int(os.environ.get("CODE4ME_UI_IDE_TIMEOUT_SECONDS", "600"))
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("IDE exited before robot-server became ready; see ui-ide.log")
            try:
                with urllib.request.urlopen(robot_url + "/hello", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise TimeoutError("IDE startup timed out; see ui-ide.log")
        stage = "ui_navigation"
        print("IDE ready; exercising login, enrollment and agent preparation", file=__import__('sys').stderr)
        rc = process.run([str(plugin / "gradlew"), UI_TEST_TASK, "--tests", UI_TEST_CLASS,
                          "--rerun", "--console=plain"], cwd=plugin, env=env,
                         log_path=run_path / "ui-test.log", timeout=900)
        results = parse_results_json(results_file)
        details["per_step"] = results.get("steps", [])
        xml = parse_gradle_xml(xml_path)
        details["junit"] = xml
        require_test_evidence(rc, xml, results, log_path=run_path / 'ui-test.log')
        stage = "acp_conversation"
        print("UI passed; testing registered ACP proxy, model turn and telemetry", file=__import__('sys').stderr)
        details["acp"] = acp.exercise(scenario, run_path, state, stub)
        status = "PASS"
    except Exception as error:
        details.update(stage=stage, error=str(error))
        report.capture_backend_logs(scenario, run_path)
    finally:
        if not keep_ide and child is not None:
            # Gradle may use a detached single-use daemon. Match both this run's
            # unique home and robot port, never another sandbox or developer IDE.
            listing = subprocess.run(["ps", "-ww", "-eo", "pid=,command="], capture_output=True, text=True).stdout
            for line in listing.splitlines():
                if f"-Duser.home={home}" in line and f"-Drobot-server.port={port}" in line:
                    try:
                        os.kill(int(line.strip().split()[0]), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            process.stop(child)
        if stub:
            stub.stop()
    result = StepResult(UI_TEST_STEP_ID, status, int((time.monotonic() - started) * 1000),
                        details, "" if status == "PASS" else f"Inspect {run_path}/ui-test.log and ui-ide.log ({stage})")
    built = record_ui_step(run_path, scenario, result, {})
    report.print_summary(built, as_json)
    if status == "PASS" and not keep_stack and not keep_ide:
        stack.down(scenario)
    return 0 if status == "PASS" else 1
