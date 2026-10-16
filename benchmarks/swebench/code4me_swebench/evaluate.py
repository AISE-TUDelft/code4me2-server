"""Grading with the official SWE-bench harness (``swebench.harness.run_evaluation``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path


def docker_env() -> dict[str, str]:
    """Process env whose DOCKER_HOST matches the docker CLI's current context.

    The harness uses the Docker Python SDK, which ignores CLI contexts (Colima
    serves its socket under ~/.colima, not /var/run/docker.sock).
    """
    env = dict(os.environ)
    if env.get("DOCKER_HOST"):
        return env
    host = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if host:
        env["DOCKER_HOST"] = host
    return env


# SWE-rebench grades with its own fork of the SWE-bench harness (swebench 4.0.3,
# which conflicts with our swebench 5), so it lives in a separate venv.
REBENCH_HARNESS = (
    "swebench @ git+https://github.com/SWE-rebench/SWE-bench-fork"
    "@e4907b7a90eafaa1f0a6428fd04fe31cdd8b4284"
)
REBENCH_VENV = Path(__file__).resolve().parents[1] / ".venv-rebench"
_venv_lock = threading.Lock()


def rebench_python() -> str:
    """The rebench grader's interpreter, creating its venv on first use."""
    python = REBENCH_VENV / "bin" / "python"
    with _venv_lock:
        if not python.exists():
            subprocess.run(["uv", "venv", "--quiet", "--python", "3.12", str(REBENCH_VENV)], check=True)
            subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(python),
                            REBENCH_HARNESS], check=True)
    return str(python)


def grader_command(grader: str) -> list[str]:
    if grader == "swebench":
        return [sys.executable, "-m", "swebench.harness.run_evaluation"]
    if grader == "rebench":
        # Images come from each row's image_name; keep them for --remove-images.
        return [rebench_python(), "-m", "swebench.harness.run_evaluation",
                "--cache_level", "instance", "--namespace", "swerebench"]
    raise ValueError(f"unknown grader {grader!r}")


def report_path(run_dir: Path, run_id: str, model_name: str, instance_id: str) -> Path:
    return (run_dir / "logs" / "run_evaluation" / run_id / model_name.replace("/", "__")
            / instance_id / "report.json")


def evaluate_instance(*, run_dir: Path, run_id: str, model_name: str, instance_id: str,
                      patch: str, timeout_s: int = 1_800, grader: str = "swebench") -> dict:
    """Grade one prediction; returns ``{resolved, status, ...}`` for the report."""
    if not patch.strip():
        return {"resolved": False, "status": "empty_patch"}
    instance_dir = run_dir / "instances" / instance_id
    log_path = instance_dir / "eval.log"
    # Both harnesses skip an instance that already has a report for this run
    # id, so a re-run task would silently inherit its previous verdict.
    stale = report_path(run_dir, run_id, model_name, instance_id).parent
    if stale.exists():
        shutil.rmtree(stale)
    command = [
        *grader_command(grader),
        "--dataset_name", str(run_dir / "dataset.json"),
        "--split", "test",
        "--instance_ids", instance_id,
        "--predictions_path", str(run_dir / "predictions.jsonl"),
        "--max_workers", "1",
        "--run_id", run_id,
        "--timeout", str(timeout_s),
        "--report_dir", str(instance_dir / "eval-report"),
    ]
    with log_path.open("w") as log:
        proc = subprocess.run(command, cwd=run_dir, env=docker_env(), stdout=log,
                              stderr=subprocess.STDOUT, timeout=timeout_s + 900)
    report_file = report_path(run_dir, run_id, model_name, instance_id)
    if not report_file.exists():
        return {"resolved": False, "status": "eval_error", "exit_code": proc.returncode}
    report = json.loads(report_file.read_text()).get(instance_id, {})
    return {
        "resolved": bool(report.get("resolved")),
        "status": "resolved" if report.get("resolved") else (
            "unresolved" if report.get("patch_successfully_applied") else "patch_failed"),
        "patch_applied": bool(report.get("patch_successfully_applied")),
        "tests_status": report.get("tests_status"),
    }
