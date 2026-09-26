"""Run artifacts and the machine-readable report.

Every run owns a directory under ``e2e/runs/<UTC timestamp>-<short>/`` containing:

* ``state.json``  - resumable workflow state (ids, cookies, step results)
* ``report.json`` - the machine-readable report
* ``http.jsonl``  - one sanitized HTTP exchange per line (diagnostics)
* ``logs/backend.log`` - captured on failure
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import stack
from .config import Scenario, sanitized_scenario

RUNS_DIR_NEW = "runs"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def runs_root(e2e_dir: Path) -> Path:
    return e2e_dir / RUNS_DIR_NEW


def new_run_dir(e2e_dir: Path) -> Path:
    run_dir = runs_root(e2e_dir) / new_run_id()
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return run_dir


def latest_run_dir(e2e_dir: Path) -> Optional[Path]:
    root = runs_root(e2e_dir)
    if not root.is_dir():
        return None
    candidates = sorted(
        (path for path in root.iterdir() if (path / "state.json").is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def write_state(run_dir: Path, state: Dict[str, Any]) -> None:
    _write_json(run_dir / "state.json", state)
    (run_dir / "state.json").chmod(0o600)


def read_state(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / "state.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def append_http(run_dir: Path, records: List[Dict[str, Any]]) -> None:
    path = run_dir / "http.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def capture_backend_logs(scenario: Scenario, run_dir: Path, tail: int = 200) -> Optional[str]:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "backend.log"
    try:
        text = stack.backend_logs(scenario, tail=tail)
    except Exception as error:  # noqa: BLE001 - diagnostics must never fail the run
        text = f"(could not capture backend logs: {error})"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def build_report(
    *,
    run_id: str,
    started_at: str,
    finished_at: str,
    scenario: Scenario,
    steps: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    failed_step: Optional[str] = None,
    blocked_by: Optional[str] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "scenario": sanitized_scenario(scenario),
        "steps": steps,
        "failed_step": failed_step,
        "blocked_by": blocked_by,
        "findings": findings,
        "next_actions": [],
    }
    report["next_actions"] = derive_next_actions(report)
    return report


def derive_next_actions(report: Dict[str, Any]) -> List[str]:
    actions: List[str] = []
    failing = next((step for step in report["steps"] if step["status"] == "FAIL"), None)
    if failing is None and report.get("blocked_by"):
        failing = next(
            (step for step in report["steps"] if step["id"] == report["blocked_by"]), None
        )
    if failing:
        if failing.get("fix_hint"):
            actions.append(failing["fix_hint"])
        actions.append(
            "inspect the sanitized exchanges in report.json and logs/backend.log for this run"
        )
    if report.get("findings"):
        actions.append(
            "review findings[] — these are application behaviors the harness refuses to hide"
        )
    if not actions:
        actions.append("all steps passed; the workflow is green")
    return actions


def write_report(run_dir: Path, report: Dict[str, Any]) -> Path:
    path = run_dir / "report.json"
    _write_json(path, report)
    return path


def print_summary(report: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        summary = {
            "run_id": report["run_id"],
            "ok": report["failed_step"] is None and not report.get("blocked_by"),
            "failed_step": report["failed_step"],
            "blocked_by": report.get("blocked_by"),
            "steps": [
                {"id": step["id"], "status": step["status"], "duration_ms": step["duration_ms"]}
                for step in report["steps"]
            ],
            "findings": [finding.get("id") for finding in report.get("findings", [])],
            "next_actions": report.get("next_actions", []),
        }
        if report.get("layers") is not None:
            summary["layers"] = report["layers"]
        if report.get("prerequisites") is not None:
            summary["prerequisites"] = report["prerequisites"]
        if report.get("report_path"):
            summary["report_path"] = report["report_path"]
        print(json.dumps(summary, indent=2, default=str))
        return
    statuses = {step["id"]: step["status"] for step in report["steps"]}
    width = max((len(step) for step in statuses), default=4)
    for step_id, status in statuses.items():
        print(f"{step_id:<{width}}  {status}")
    print()
    for layer in report.get("layers") or []:
        reason = f"  ({layer['reason']})" if layer.get("reason") else ""
        print(f"layer {layer['layer']:<8} {layer['status']}{reason}")
    if report.get("layers"):
        print()
    if report["failed_step"] or report.get("blocked_by"):
        print(f"FAILED at {report['failed_step'] or report['blocked_by']}")
    else:
        print("ALL STEPS PASSED")
    for action in report.get("next_actions", []):
        print(f"  -> {action}")


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
