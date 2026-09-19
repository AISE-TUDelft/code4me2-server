"""One-command regression gate for backend, plugin fixture, real IDE and ACP."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path

from . import browser, report, stack, ui, workflow
from .steps import StepResult


def run(scenario, *, run_dir=None, keep_stack=False, as_json=False, layer="all") -> int:
    run_path = Path(run_dir).resolve() if run_dir else report.new_run_dir(workflow.E2E_DIR)
    run_path.mkdir(parents=True, exist_ok=True)
    rc = 1
    started = time.monotonic()
    try:
        print(f"E2E artifacts: {run_path}", file=sys.stderr)
        with contextlib.redirect_stdout(io.StringIO()):
            if layer == "browser":
                rc = browser.run(scenario, run_path)
            elif layer == "backend":
                rc = workflow.run_workflow(scenario, run_dir=str(run_path), keep_stack=True)
            else:
                rc = 0
                if layer == "all":
                    rc = ui.run_ui_test(scenario, run_dir=str(run_path), keep_stack=True)
                if rc == 0:
                    print("Testing live Kotlin plugin workflow", file=sys.stderr)
                    rc = workflow.run_plugin_test(scenario, run_dir=str(run_path), keep_stack=True)
                if rc == 0:
                    print("Testing HTTP inference, telemetry and enrollment revocation", file=sys.stderr)
                    rc = workflow.run_workflow(scenario, from_step="bootstrap", run_dir=str(run_path), keep_stack=True)
    except (Exception, KeyboardInterrupt) as error:
        rc = 1
        result = StepResult("suite", "FAIL", int((time.monotonic()-started)*1000),
                            {"error": str(error) or "interrupted"}, f"Inspect {run_path}")
        workflow._record_plugin_step(run_path, scenario, result)
        report.capture_backend_logs(scenario, run_path)
    finally:
        if rc == 0 and not keep_stack:
            try:
                stack.down(scenario)
            except Exception as error:
                rc = 1
                workflow._record_plugin_step(run_path, scenario,
                    StepResult("cleanup", "FAIL", 0, {"error": str(error)}, "Run stack down"))
        path = run_path / "report.json"
        if path.is_file():
            built = json.loads(path.read_text())
            built["report_path"] = str(path)
            report.write_report(run_path, built)
            report.print_summary(built, as_json)
    return rc
