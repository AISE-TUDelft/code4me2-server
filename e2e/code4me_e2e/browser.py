"""Run the website scenarios with isolated state and persisted-outcome proof.

The browser layer is independent of the IDE layer. It provisions the study it
needs in its own ``<run>/browser-state`` directory, so the IDE layers'
enrollments/revocation state in ``<run>/state.json`` is never overwritten, and
it is attempted even when the IDE layer fails.

Website success is only reported when the exact scenario inventory ran once
each and all checks passed, the results carry no join code/credential, and the
persisted read models (edited metadata, stopped state, clone identity/profile
selection, enrollment/assignment coverage) still show the same outcomes.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import process, report, stack, workflow
from .paths import BROWSER_DIR, require_workspace
from .steps import STEP_ORDER, StepResult

BROWSER_STEP_ID = "browser_test"

#: Run-state directory (below the layer's run directory) owned by the browser
#: layer alone; the IDE layers keep using ``<run>/state.json``.
BROWSER_STATE_DIRNAME = "browser-state"

#: Every check the website suite must execute exactly once with status PASS.
#: ``scenarios.py`` runs under the browser Python (Playwright), which the
#: harness interpreter may not have, so the inventory lives here, is passed to
#: the child through the environment, and is re-validated by the parent.
EXPECTED_SCENARIO_IDS: tuple[str, ...] = (
    "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A12",
    "A13",
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8",
    "C1", "C2", "C3", "C4", "C5", "C6",
    "D1", "D2", "D3", "D4", "D5", "D6",
    "E1", "E2", "E3", "E4",
)

#: Identity the child must return so the parent can re-read the persisted
#: models itself. Values are ids/names only; never a join code or credential.
CONTEXT_KEYS: tuple[str, ...] = (
    "study_id",
    "edited_name",
    "edited_description",
    "clone_study_id",
    "clone_name",
    "profile_id",
    "enrollment_id",
    "assignment_id",
)


class PrerequisiteError(RuntimeError):
    """A missing prerequisite (Playwright, Node, reachable stack).

    Reported as BLOCKED with a reason, never as a pass and never with backend
    logs captured from a stack that was never reached.
    """


def state_directory(run_path: Path, state_dir: Optional[Path] = None) -> Path:
    """The browser layer's own run-state directory (never the layer run dir)."""
    base = Path(run_path).resolve()
    return Path(state_dir).resolve() if state_dir else base / BROWSER_STATE_DIRNAME


# ---------------------------------------------------------------------------
# Evidence validation (unit-testable without a live stack)
# ---------------------------------------------------------------------------


def validate_scenario_results(
    payload: Any, *, expected: Sequence[str] = EXPECTED_SCENARIO_IDS
) -> List[Dict[str, Any]]:
    """Validate the exact scenario inventory; return the normalized steps.

    Missing, duplicated, blocked or unexpected ids can never pass: every
    expected id must appear exactly once, with status PASS and no other id.
    """
    expected_ids = list(expected)
    steps = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(steps, list) or not steps:
        raise RuntimeError("website evidence carried no steps array")
    ids: List[str] = []
    for step in steps:
        if not isinstance(step, dict) or not str(step.get("id") or "").strip():
            raise RuntimeError("website evidence contains a step without an id")
        ids.append(str(step["id"]))
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    missing = [step_id for step_id in expected_ids if step_id not in ids]
    unexpected = [step_id for step_id in ids if step_id not in expected_ids]
    not_passing = [
        f"{step.get('id')}:{step.get('status')}"
        for step in steps
        if str(step.get("status", "")).upper() != "PASS"
    ]
    problems: List[str] = []
    if missing:
        problems.append("missing " + ",".join(missing))
    if duplicates:
        problems.append("duplicated " + ",".join(duplicates))
    if unexpected:
        problems.append("unexpected " + ",".join(unexpected))
    if len(ids) != len(expected_ids):
        problems.append(f"expected {len(expected_ids)} checks, got {len(ids)}")
    if not_passing:
        problems.append("not passing " + ",".join(not_passing))
    if problems:
        raise RuntimeError("website scenario evidence invalid: " + "; ".join(problems))
    return steps


def validate_context(payload: Any, *, keys: Sequence[str] = CONTEXT_KEYS) -> Dict[str, str]:
    """The child's identity hand-off for the parent's persisted re-reads."""
    context = payload.get("context") if isinstance(payload, dict) else None
    if not isinstance(context, dict):
        raise RuntimeError("website evidence carried no context object")
    missing = [key for key in keys if not str(context.get(key) or "").strip()]
    if missing:
        raise RuntimeError("website evidence context is missing " + ", ".join(missing))
    resolved = {key: str(context[key]) for key in keys}
    if resolved["study_id"] == resolved["clone_study_id"]:
        raise RuntimeError("website evidence reports the clone with the source study id")
    return resolved


def assert_no_secret_material(text: str, *, secrets: Sequence[str]) -> None:
    """Refuse to pass when evidence contains a join code or credential value."""
    for secret in secrets:
        if secret and secret in text:
            raise RuntimeError(
                "website evidence contains private material (join code or "
                "credential); refusing to pass"
            )


# ---------------------------------------------------------------------------
# Persisted read-model verification (independent of the child's own checks)
# ---------------------------------------------------------------------------


def _api_get(
    base_url: str, path: str, cookies: Dict[str, str], timeout: float = 20.0
) -> Dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={
            "Accept": "application/json",
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"status": response.status, "payload": json.loads(response.read() or b"{}")}
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        return {"status": error.code, "payload": payload}


def login_cookies(scenario, role: str = "researcher") -> Dict[str, str]:
    """A fresh session for ``role``: the browser's own sign-in may end older ones."""
    from .http import HttpClient

    account = getattr(scenario, role)
    client = HttpClient(scenario.base_url or "", label=f"browser-verify-{role}", timeout=20.0)
    response = client.post("/api/user/authenticate", {"email": account.email, "password": account.password})
    if response.status != 200:
        raise RuntimeError(f"signing in as the {role} for the persisted re-read failed (HTTP {response.status})")
    return client.export_cookies()


def verify_persisted_outcomes(scenario, state: Dict[str, Any], context: Dict[str, str],
                              *, cookies: Optional[Dict[str, str]] = None) -> Dict[str, bool]:
    """Re-read the real API models and require the persisted browser outcomes.

    This never trusts the child's report: it fetches the study, clone and
    participant-coverage read models as the researcher, with ``cookies`` from a
    fresh sign-in (the runner passes them) or the session the browser setup
    workflow persisted.
    """
    cookies = dict(cookies or (state.get("cookies") or {}).get("researcher") or {})
    if not cookies:
        raise RuntimeError("no researcher session in the browser run state; cannot re-read persisted outcomes")
    base_url = scenario.base_url or ""

    original = _api_get(base_url, f"/api/research/studies/{context['study_id']}", cookies)
    study = (original.get("payload") or {}).get("study") or {}
    clone = _api_get(base_url, f"/api/research/studies/{context['clone_study_id']}", cookies)
    clone_study = (clone.get("payload") or {}).get("study") or {}
    clone_profiles = [
        str(item.get("profile_id")) for item in (clone_study.get("profile_selections") or [])
    ]
    clone_capabilities = clone_study.get("lifecycle_capabilities") or {}
    coverage = _api_get(
        base_url,
        f"/api/research/operations/participants/coverage?study_id={context['study_id']}",
        cookies,
    )
    coverage_data = coverage.get("payload") or {}
    rows = [
        row
        for row in (coverage_data.get("participants") or [])
        if str(row.get("enrollment_id")) == context["enrollment_id"]
    ]
    assignment = (rows[0].get("assignment") or {}) if rows else {}

    checks = {
        "edited_metadata_persisted": (
            original.get("status") == 200
            and study.get("name") == context["edited_name"]
            and study.get("description") == context["edited_description"]
            and bool(study.get("consent_locked_at"))
        ),
        "stopped_state_retained": (
            study.get("research_status") == "STUDY_STOPPED"
            and study.get("enrollment_count") == 1
            and study.get("active_enrollment_count") == 0
            and study.get("assignment_count") == 1
        ),
        "clone_identity_profiles_status": (
            clone.get("status") == 200
            and context["clone_study_id"] != context["study_id"]
            and clone_study.get("name") == context["clone_name"]
            and clone_study.get("description") == context["edited_description"]
            and clone_study.get("research_status") == "DRAFT"
            and context["profile_id"] in clone_profiles
            and clone_study.get("enrollment_count") == 0
            and clone_study.get("assignment_count") == 0
            and bool(clone_capabilities.get("joinable"))
        ),
        "enrollment_assignment_coverage": (
            coverage.get("status") == 200
            and coverage_data.get("participant_count") == 1
            and len(rows) == 1
            and rows[0].get("status") == "STUDY_STOPPED"
            and str(assignment.get("assignment_id")) == context["assignment_id"]
            and str(assignment.get("agent_profile_id")) == context["profile_id"]
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(
            "persisted browser outcomes are not visible in the read models: " + ", ".join(failed)
        )
    return checks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _require_browser_prerequisites() -> tuple[str, str, str]:
    """Resolve the browser Python and Node tooling, or raise BLOCKED."""
    from .prereqs import browser_python

    python, _source = browser_python()
    try:
        probe = subprocess.run(
            [python, "-c", "import playwright.sync_api"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PrerequisiteError(
            f"the Playwright probe could not run ({error}); run `python3 -m code4me_e2e setup "
            "--layer browser` or set CODE4ME_E2E_BROWSER_PYTHON (see e2e/README.md)"
        ) from error
    if probe.returncode:
        raise PrerequisiteError(
            f"Playwright is not importable by {python}; run `python3 -m code4me_e2e setup "
            "--layer browser` or set CODE4ME_E2E_BROWSER_PYTHON (see e2e/README.md)"
        )
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        raise PrerequisiteError("Node.js and npm must be on PATH for the website scenarios")
    return python, node, npm


def _bootstrap_blocked_step(state_path: Path) -> Optional[str]:
    """The workflow step that blocked the browser bootstrap, if any."""
    try:
        built = json.loads((state_path / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(built, dict):
        return None
    return built.get("blocked_by") or built.get("failed_step")


def _workflow_touched_stack(state_path: Path) -> bool:
    """True when the bootstrap workflow actually wrote run state or a report."""
    return (state_path / "state.json").is_file() or (state_path / "report.json").is_file()


def run(scenario, run_path, *, state_dir: Optional[Path] = None) -> int:
    started = time.monotonic()
    run_path = Path(run_path)
    state_path = state_directory(run_path, state_dir)
    child = None
    status, details = "FAIL", {}
    config_path = run_path / "browser-config.json"
    try:
        python, node, npm = _require_browser_prerequisites()
        prefix = STEP_ORDER[: STEP_ORDER.index("create_study") + 1]
        # The layer seeds its own accounts, profile and study: never mutate the
        # scenario the other layers of this gate share.
        scenario = copy.deepcopy(scenario)
        scenario.study.name = f"{scenario.study.name} [{run_path.name}]"
        scenario.agent.profile_name = f"e2e-browser-{run_path.name}"
        state_path.mkdir(parents=True, exist_ok=True)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = workflow.run_workflow(
                    scenario, only=",".join(prefix), run_dir=str(state_path), keep_stack=True
                )
        except stack.StackError as error:
            raise PrerequisiteError(f"the disposable stack is unavailable: {error}") from error
        if rc:
            if _bootstrap_blocked_step(state_path) == "doctor":
                raise PrerequisiteError(
                    "the browser layer's backend is not reachable or schema-ready; "
                    "see browser-state/report.json"
                )
            raise RuntimeError("Browser setup failed; see browser-state/report.json")
        state = report.read_state(state_path)
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
                  "profile_name": scenario.agent.profile_name,
                  "researcher": asdict(scenario.researcher),
                  "admin": asdict(scenario.admin)}
        config_path.touch(mode=0o600)
        config_path.write_text(json.dumps(config))
        results_path = run_path / "browser-results.json"
        results_path.unlink(missing_ok=True)
        env = {**os.environ, "CODE4ME_E2E_BASE_URL": scenario.base_url,
               "CODE4ME_E2E_WEB_BUILD": str(web_build),
               "CODE4ME_E2E_WEB_PORT": str(port), "CODE4ME_E2E_BROWSER_CONFIG": str(config_path),
               "CODE4ME_E2E_BROWSER_RESULTS": str(results_path),
               "CODE4ME_E2E_BROWSER_EXPECTED_IDS": json.dumps(list(EXPECTED_SCENARIO_IDS))}
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
        print(f"Testing {len(EXPECTED_SCENARIO_IDS)} website study, consent and "
              "persisted-outcome checks in Chromium", file=sys.stderr)
        rc = process.run([python, "-u", str(BROWSER_DIR / "scenarios.py")], cwd=root, env=env,
                         log_path=run_path / "browser-test.log", timeout=300)
        if not results_path.is_file():
            raise RuntimeError("The website suite wrote no results; see browser-test.log")
        results_text = results_path.read_text(encoding="utf-8")
        assert_no_secret_material(
            results_text,
            secrets=[scenario.researcher.password, scenario.admin.password,
                     str(state.get("join_code") or "")],
        )
        results = json.loads(results_text)
        details["per_step"] = results.get("steps") if isinstance(results, dict) else None
        steps = validate_scenario_results(results)
        context = validate_context(results)
        if rc:
            raise RuntimeError("The website suite reported failing checks; see browser-test.log")
        details["per_step"] = steps
        details["context"] = context
        details["persisted"] = verify_persisted_outcomes(scenario, state, context,
                                                         cookies=login_cookies(scenario))
        status = "PASS"
    except PrerequisiteError as error:
        status = "BLOCKED"
        details["prerequisite"] = True
        details["error"] = str(error)
    except Exception as error:  # noqa: BLE001 - a failed check is a failed layer
        details["error"] = str(error)
        if _workflow_touched_stack(state_path):
            report.capture_backend_logs(scenario, run_path)
    finally:
        if child:
            process.stop(child)
        config_path.unlink(missing_ok=True)
    if status == "BLOCKED":
        fix_hint = str(details.get("error") or "missing prerequisite")[:240]
    elif status == "PASS":
        fix_hint = ""
    else:
        fix_hint = f"Inspect {run_path}/browser-test.log"
    workflow._record_plugin_step(run_path, scenario, StepResult(
        BROWSER_STEP_ID, status, int((time.monotonic() - started) * 1000), details, fix_hint,
    ))
    return 0 if status == "PASS" else 1
