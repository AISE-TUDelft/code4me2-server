"""Run the existing website scenarios with isolated, automatically seeded state."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict

from . import process, report, workflow
from .paths import BROWSER_DIR, require_workspace
from .steps import STEP_ORDER, StepResult


def run(scenario, run_path) -> int:
    started = time.monotonic()
    child = None
    status, details = "FAIL", {}
    config_path = run_path / "browser-config.json"
    try:
        python = os.environ.get("CODE4ME_E2E_BROWSER_PYTHON", sys.executable)
        probe = subprocess.run([python, "-c", "import playwright.sync_api"], capture_output=True, timeout=30)
        if probe.returncode:
            raise RuntimeError("Install Playwright and Chromium; set CODE4ME_E2E_BROWSER_PYTHON to that Python (see e2e/README.md)")
        node, npm = shutil.which("node"), shutil.which("npm")
        if not node or not npm:
            raise RuntimeError("Node.js and npm must be on PATH for website tests")
        prefix = STEP_ORDER[:STEP_ORDER.index("create_study") + 1]
        scenario.study.name = f"{scenario.study.name} [{run_path.name}]"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = workflow.run_workflow(scenario, only=",".join(prefix), run_dir=str(run_path), keep_stack=True)
        if rc:
            raise RuntimeError("Browser setup failed; see report.json")
        state = report.read_state(run_path)
        workflow.restore_accounts(scenario, state)
        root = require_workspace()
        website = root / "code4me2-server" / "src/website"
        if not (website / "node_modules").is_dir():
            if process.run([npm, "ci"], cwd=website, log_path=run_path / "browser-install.log"):
                raise RuntimeError("Website dependency installation failed; see browser-install.log")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        web_build = run_path / "website"
        # Pin every API call to our same-origin proxy, including legacy login
        # code that concatenates host and port. Never inherit production URLs.
        build_env = {**os.environ, "CI": "false", "BUILD_PATH": str(web_build),
                     "REACT_APP_BACKEND_HOST": "http://127.0.0.1", "REACT_APP_BACKEND_PORT": str(port)}
        if process.run([npm, "run", "build"], cwd=website, env=build_env, log_path=run_path / "browser-build.log"):
            raise RuntimeError("Website build failed; see browser-build.log")
        config = {"base_url": scenario.base_url, "ui_url": url,
                  "config_id": state["config_id"], "study_name": scenario.study.name,
                  "researcher": asdict(scenario.researcher),
                  "admin": asdict(scenario.admin)}
        config_path.touch(mode=0o600)
        config_path.write_text(json.dumps(config))
        results_path = run_path / "browser-results.json"
        results_path.unlink(missing_ok=True)
        env = {**os.environ, "CODE4ME_E2E_BASE_URL": scenario.base_url,
               "CODE4ME_E2E_WEB_BUILD": str(web_build),
               "CODE4ME_E2E_WEB_PORT": str(port), "CODE4ME_E2E_BROWSER_CONFIG": str(config_path),
               "CODE4ME_E2E_BROWSER_RESULTS": str(results_path)}
        with (run_path / "browser-server.log").open("w") as log:
            child = subprocess.Popen([node, str(BROWSER_DIR / "serve.js")], cwd=root,
                                     env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("Website host exited; see browser-server.log")
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError("Website host did not become ready")
        print("Testing 27 website study and consent checks in Chromium", file=sys.stderr)
        rc = process.run([python, "-u", str(BROWSER_DIR / "scenarios.py")], cwd=root, env=env,
                         log_path=run_path / "browser-test.log", timeout=300)
        results = json.loads(results_path.read_text()) if results_path.is_file() else {}
        steps = results.get("steps", [])
        details["per_step"] = steps
        if rc or len(steps) != 27 or any(step.get("status") != "PASS" for step in steps):
            raise RuntimeError("All 27 browser checks must execute and pass; see browser-test.log")
        status = "PASS"
    except Exception as error:
        details["error"] = str(error)
        report.capture_backend_logs(scenario, run_path)
    finally:
        if child:
            process.stop(child)
        config_path.unlink(missing_ok=True)
    workflow._record_plugin_step(run_path, scenario, StepResult(
        "browser_test", status, int((time.monotonic()-started)*1000), details,
        "" if status == "PASS" else f"Inspect {run_path}/browser-test.log",
    ))
    return 0 if status == "PASS" else 1
