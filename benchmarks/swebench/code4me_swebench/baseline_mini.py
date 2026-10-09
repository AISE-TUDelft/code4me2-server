"""Baseline: mini-swe-agent (bash-only) on the same tasks, model and grader.

mini-swe-agent runs with its stock ``swebench.yaml`` config; only the model
endpoint, the container platform and cost tracking are overridden. Tasks run in
batches so images can be deleted after grading (mini-swe-agent keeps them).
Results land in the same run-directory layout as the Code4Me runner, so
``report`` works unchanged; telemetry is mini-swe-agent's trajectory, summarised.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from code4me_swebench import docker_cli, evaluate, runner

if TYPE_CHECKING:
    from code4me_swebench.settings import RunSettings

logger = logging.getLogger(__name__)
MINI_VERSION = "2.4.6"
ROOT = Path(__file__).resolve().parents[1]
MINI_VENV = ROOT / ".venv-mini"
MODEL_CLASS_DIR = ROOT / "mini_baseline"
# mini-swe-agent's exit_status is the exception name when the model call failed.
# These are the model service failing (rate limit, outage), not the agent.
PROVIDER_FAILURES = frozenset({
    "RateLimitError", "APIConnectionError", "ServiceUnavailableError",
    "InternalServerError", "Timeout", "APIError", "BadGatewayError",
})


def mini_executable() -> Path:
    executable = MINI_VENV / "bin" / "mini-extra"
    if not executable.exists():
        subprocess.run(["uv", "venv", "--quiet", "--python", "3.12", str(MINI_VENV)], check=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(MINI_VENV / "bin" / "python"),
                        f"mini-swe-agent=={MINI_VERSION}"], check=True)
    return executable


def overlay_config(settings: RunSettings) -> dict:
    return {
        "model": {
            "model_name": f"openai/{settings.model}",
            # litellm has no price for these models; the cost limit cannot apply.
            "cost_tracking": "ignore_errors",
            "model_kwargs": {
                "api_base": settings.base_url,
                "extra_headers": {"User-Agent": f"code4me-research-baseline/mini-swe-agent-{MINI_VERSION}"},
                **({"temperature": settings.temperature} if settings.temperature is not None else {}),
            },
        },
        "environment": {
            # The model is called from the host, so the task container needs no
            # network at all: no upstream fixes from GitHub or PyPI.
            "run_args": ["--rm", "--platform", settings.platform, "--network", "none"],
            "pull_timeout": 1_200,
        },
    }


def _trajectory_summary(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    info = data.get("info") or {}
    stats = info.get("model_stats") or {}
    messages = data.get("messages") or []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0}
    for message in messages:
        response = (message.get("extra") or {}).get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue
        details = usage.get("prompt_tokens_details") or {}
        tokens["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        tokens["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        tokens["cached_prompt_tokens"] += max(
            int(usage.get("prompt_cache_hit_tokens") or 0), int(details.get("cached_tokens") or 0))
    return {
        "exit_status": info.get("exit_status"),
        "model_calls": stats.get("api_calls"),
        "messages": len(messages),
        **tokens,
        "mini_swe_agent_version": info.get("mini_version"),
    }


def run_baseline(rows: list[dict], *, run_dir: Path, run_name: str, settings: RunSettings,
                 grader: str, batch_size: int, workers: int, remove_images: bool) -> None:
    api_key = os.environ.get(settings.api_key_env)
    if not api_key:
        raise SystemExit(f"set {settings.api_key_env} to the provider API key first")
    executable = mini_executable()
    mini_dir = run_dir / "mini"
    data_dir = run_dir / "mini-data"
    for directory in (mini_dir, data_dir, run_dir / "mini-config"):
        directory.mkdir(parents=True, exist_ok=True)
    # mini-swe-agent reads the image from image_name; Verified rows call it image.
    (data_dir / "test.jsonl").write_text("".join(
        json.dumps({**row, "image_name": row["image"]}, default=str) + "\n" for row in rows))
    overlay = run_dir / "mini-overlay.yaml"
    overlay.write_text(yaml.safe_dump(overlay_config(settings), sort_keys=False))
    env = {
        **os.environ,
        "OPENAI_API_KEY": api_key,
        "PYTHONPATH": str(MODEL_CLASS_DIR),
        # Keep mini-swe-agent's global config out of the user's home directory.
        "MSWEA_GLOBAL_CONFIG_DIR": str(run_dir / "mini-config"),
    }
    model_name = f"mini-swe-agent__{settings.model}"
    pending = [row for row in rows
               if not (run_dir / "instances" / row["instance_id"] / "prediction.json").exists()]
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        pattern = "^(" + "|".join(re.escape(row["instance_id"]) for row in batch) + ")$"
        logger.info("mini-swe-agent batch %d-%d of %d", start + 1, start + len(batch), len(pending))
        with (run_dir / "mini.log").open("a") as log:
            subprocess.run(
                [str(executable), "swebench", "--subset", str(data_dir), "--split", "test",
                 "--filter", pattern, "-w", str(workers), "-o", str(mini_dir),
                 "-c", "swebench.yaml", "-c", str(overlay),
                 "-m", f"openai/{settings.model}",
                 "--model-class", "opencode_model.OpenCodeModel",
                 "--environment-class", "docker",
                 # Only pending tasks are in the filter; earlier outage attempts
                 # are still in preds.json and must be redone.
                 "--redo-existing"],
                env=env, stdout=log, stderr=subprocess.STDOUT, check=False,
            )
        predictions_file = mini_dir / "preds.json"
        predictions = json.loads(predictions_file.read_text()) if predictions_file.exists() else {}
        graded = []
        for row in batch:
            instance_id = row["instance_id"]
            instance_dir = run_dir / "instances" / instance_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            patch = (predictions.get(instance_id) or {}).get("model_patch") or ""
            summary = _trajectory_summary(mini_dir / instance_id / f"{instance_id}.traj.json")
            if summary.get("exit_status") in PROVIDER_FAILURES:
                (instance_dir / "infra_error.json").write_text(json.dumps({
                    "reason": "provider_request_failed", "error_type": summary["exit_status"],
                }, indent=2) + "\n")
                continue
            (instance_dir / "infra_error.json").unlink(missing_ok=True)
            graded.append(row)
            (instance_dir / "patch.diff").write_text(patch)
            (instance_dir / "telemetry.json").write_text(json.dumps(summary, indent=2) + "\n")
            (instance_dir / "result.json").write_text(json.dumps({
                "status": "completed" if instance_id in predictions else "error",
                "stop_reason": summary.get("exit_status"),
            }, indent=2) + "\n")
            (instance_dir / "prediction.json").write_text(json.dumps({
                "instance_id": instance_id, "model_name_or_path": model_name, "model_patch": patch,
            }) + "\n")
        runner.write_predictions(run_dir)
        for row in batch:
            if row not in graded and remove_images:
                docker_cli.remove_image(row["image"])
        for row in graded:
            instance_dir = run_dir / "instances" / row["instance_id"]
            patch = (instance_dir / "patch.diff").read_text()
            result = evaluate.evaluate_instance(
                run_dir=run_dir, run_id=run_name, model_name=model_name,
                instance_id=row["instance_id"], patch=patch, grader=grader,
            )
            (instance_dir / "eval.json").write_text(json.dumps(result, indent=2) + "\n")
            logger.info("%s eval=%s", row["instance_id"], result["status"])
            if remove_images:
                docker_cli.remove_image(row["image"])
        if batch and not graded:
            logger.error("Stopping: every task in this batch failed on the model service. "
                         "Re-run the same command later; finished tasks are kept.")
            break
