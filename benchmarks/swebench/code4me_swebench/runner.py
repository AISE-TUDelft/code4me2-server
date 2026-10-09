"""Runs the agent on SWE-bench tasks, one task container per instance.

Run directory layout::

    <run>/manifest.json         settings, agent build, dataset, instance list
    <run>/dataset.json          the selected dataset rows (graded from this file)
    <run>/predictions.jsonl     SWE-bench predictions, rebuilt after every task
    <run>/instances/<id>/       trace.jsonl (agent telemetry), result.json,
                                agent-config.json, patch.diff, agent.log,
                                telemetry.json, eval.json, eval.log
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from code4me_swebench import docker_cli, evaluate, sandbox, telemetry
from code4me_swebench.prompt import task_prompt
from code4me_swebench.settings import (
    OUT_DIR,
    RUNTIME_MOUNT,
    TASK_DIR,
    WORKSPACE,
    RunSettings,
    agent_config,
)

if TYPE_CHECKING:
    from code4me_swebench.runtime import Runtime

logger = logging.getLogger(__name__)
DRIVER = Path(__file__).resolve().parent / "driver.py"
_predictions_lock = threading.Lock()
MAX_CONSECUTIVE_OUTAGES = 3
# Model-service failures that say nothing about the agent (rate limit, overload,
# outage). Anything else, such as 400 for a malformed request, is the agent's.
RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RunOptions:
    evaluate: bool = True
    remove_images: bool = False
    force: bool = False
    memory: str | None = None
    cpus: str | None = None
    eval_timeout_s: int = 1_800
    # "swebench" (swebench 5.0.2) or "rebench" (the SWE-rebench harness fork).
    grader: str = "swebench"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def prepare_run(run_dir: Path, *, rows: list[dict], settings: RunSettings,
                runtime: Runtime, subset: str, dataset_name: str, split: str,
                grader: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    ids = [row["instance_id"] for row in rows]
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous["settings"] != settings.as_dict() or previous["instance_ids"] != ids:
            raise SystemExit(
                f"{run_dir} was started with other settings or instances; use a new --run-name"
            )
        if previous["runtime"]["wheel_sha256"] != runtime.wheel_sha256:
            logger.warning("Agent code changed since this run started (wheel %s -> %s)",
                           previous["runtime"]["wheel_sha256"][:12], runtime.wheel_sha256[:12])
        return
    _write_json(run_dir / "dataset.json", rows)
    _write_json(manifest_path, {
        "created_at": _now(),
        "subset": subset,
        "dataset": dataset_name,
        "split": split,
        "grader": grader,
        "instance_ids": ids,
        "settings": settings.as_dict(),
        "runtime": runtime.as_dict(),
    })


def write_predictions(run_dir: Path) -> None:
    with _predictions_lock:
        lines = []
        for path in sorted((run_dir / "instances").glob("*/prediction.json")):
            lines.append(json.dumps(json.loads(path.read_text()), sort_keys=True))
        tmp = run_dir / "predictions.jsonl.tmp"
        tmp.write_text("".join(line + "\n" for line in lines))
        tmp.replace(run_dir / "predictions.jsonl")


def _container_name(run_name: str, instance_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", f"c4m-swe-{run_name}-{instance_id}")[:120]


# SWE-bench images keep conda in /opt/miniconda3, SWE-rebench images in /opt/conda.
_ACTIVATE_TESTBED = (
    'for a in /opt/miniconda3/bin/activate /opt/conda/bin/activate; do '
    '[ -f "$a" ] && source "$a" testbed >/dev/null 2>&1 && break; done; '
    'printf "%s\\n%s\\n" "$PATH" "${CONDA_PREFIX:-}"'
)
# Files some SWE-rebench images ship in / that describe the task (and possibly
# its fix). The agent's shell could read them, so they go before it starts.
LEAK_FILES = ("/issue.md", "/swebench_instance.json")


def _activated_env(container: str) -> list[str]:
    """PATH/CONDA_PREFIX of the image's ``testbed`` conda env (as its eval script uses)."""
    probe = docker_cli.exec_in(container, "bash", "-c", _ACTIVATE_TESTBED)
    lines = probe.stdout.splitlines()
    if probe.returncode == 0 and len(lines) >= 2 and lines[1].endswith("/envs/testbed"):
        return [f"PATH={lines[0]}", f"CONDA_PREFIX={lines[1]}"]
    raise RuntimeError(f"{container}: no testbed conda env found")


def _remove_leak_files(container: str) -> list[str]:
    present = docker_cli.exec_in(container, "ls", "-1", *LEAK_FILES).stdout.split()
    if present:
        docker_cli.exec_in(container, "rm", "-f", *present, check=True)
    return present


def _git(container: str, *args: str) -> str:
    return docker_cli.exec_in(container, "git", "-C", WORKSPACE, *args, check=True).stdout


def _extract_patch(container: str, base: str) -> str:
    """Everything the agent changed, staged, as a binary-safe diff against ``base``."""
    docker_cli.exec_in(container, "git", "-C", WORKSPACE, "add", "-A", check=True)
    return docker_cli.exec_in(
        container, "git", "-C", WORKSPACE, "-c", "core.fileMode=false",
        "diff", "--cached", "--binary", "--no-color", base, check=True,
    ).stdout


def run_instance(row: dict, *, run_dir: Path, run_name: str, settings: RunSettings,
                 runtime: Runtime, options: RunOptions) -> dict:
    instance_id = row["instance_id"]
    instance_dir = run_dir / "instances" / instance_id
    done_marker = instance_dir / "prediction.json"
    if done_marker.exists() and not options.force:
        return _instance_record(instance_dir, instance_id, skipped=True)
    instance_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("trace.jsonl", "result.json", "eval.json", "telemetry.json"):
        (instance_dir / stale).unlink(missing_ok=True)

    image = row["image"]
    container = _container_name(run_name, instance_id)
    session_id = f"swebench-{run_name}-{instance_id}"
    pull_started = perf_counter()
    docker_cli.pull(image, platform=settings.platform)
    pull_s = round(perf_counter() - pull_started, 3)
    started = perf_counter()
    docker_cli.start_container(
        name=container, image=image, platform=settings.platform,
        mounts=[f"{runtime.volume}:{RUNTIME_MOUNT}:ro"],
        memory=options.memory, cpus=options.cpus, network=sandbox.NETWORK,
    )
    host: dict = {"instance_id": instance_id, "image": image, "started_at": _now(),
                  "image_pull_s": pull_s, "network": settings.network}
    container_ip = docker_cli.container_ip(container, sandbox.NETWORK)
    try:
        env = _activated_env(container)
        host["removed_leak_files"] = _remove_leak_files(container)
        base = _git(container, "rev-parse", "HEAD").strip()
        host["base_commit"] = base
        # The images commit their setup ("SWE-bench") on top of the dataset's
        # base commit; the patch is taken against the image HEAD either way.
        parent = docker_cli.exec_in(container, "git", "-C", WORKSPACE, "rev-parse", "HEAD^")
        host["head_on_dataset_base"] = row.get("base_commit") in (base, parent.stdout.strip())
        host["preexisting_changes"] = _git(container, "status", "--porcelain").strip()
        task = {
            "instance_id": instance_id,
            "run_id": f"{run_name}-{instance_id}",
            "out_dir": OUT_DIR,
            "prompt": task_prompt(row["problem_statement"], workspace=WORKSPACE),
            "agent_config": agent_config(settings, session_id=session_id),
            "allowed_tools": list(settings.tools),
        }
        _write_json(instance_dir / "task.json", task)
        docker_cli.exec_in(container, "mkdir", "-p", OUT_DIR, check=True)
        docker_cli.copy_to(container, DRIVER, f"{TASK_DIR}/driver.py")
        docker_cli.copy_to(container, instance_dir / "task.json", f"{TASK_DIR}/task.json")
        agent = docker_cli.exec_in(
            container, f"{RUNTIME_MOUNT}/venv/bin/python", f"{TASK_DIR}/driver.py",
            f"{TASK_DIR}/task.json",
            workdir=WORKSPACE,
            # `-e NAME` without a value forwards the host value: the key never
            # appears on a command line or on disk.
            env=[*env, *sandbox.proxy_env(), settings.api_key_env, "CODE4ME_AGENT_LOG_LEVEL=INFO"],
            timeout=settings.task_timeout_s,
        )
        host["agent_exit_code"] = agent.returncode
        host["agent_timed_out"] = agent.timed_out
        if agent.timed_out:
            docker_cli.exec_in(container, "pkill", "-f", f"{TASK_DIR}/driver.py")
        (instance_dir / "agent.log").write_text(agent.stdout + agent.stderr)
        patch = _extract_patch(container, base)
        docker_cli.copy_from(container, f"{OUT_DIR}/.", instance_dir)
        # Audit: connections this container tried that the proxy refused.
        host["egress_denied"] = [line for line in sandbox.denied_attempts(since=host["started_at"])
                                 if container_ip and f" {container_ip} " in line]
    finally:
        docker_cli.remove_container(container)
    host["agent_wall_s"] = round(perf_counter() - started, 3)
    (instance_dir / "patch.diff").write_text(patch)
    _write_json(instance_dir / "host.json", host)
    _write_json(instance_dir / "telemetry.json", telemetry.summarize(instance_dir / "trace.jsonl"))
    outage = provider_outage(instance_dir)
    if outage:
        # The model service failed (rate limit, outage), not the agent: no
        # prediction is written, so a resumed run retries the task.
        _write_json(instance_dir / "infra_error.json", outage)
        if options.remove_images:
            docker_cli.remove_image(image)
        return {"instance_id": instance_id, "agent_status": "infra_error", **outage}
    (instance_dir / "infra_error.json").unlink(missing_ok=True)
    _write_json(done_marker, {
        "instance_id": instance_id,
        "model_name_or_path": settings.model_name_or_path,
        "model_patch": patch,
    })
    write_predictions(run_dir)
    if options.evaluate:
        result = evaluate.evaluate_instance(
            run_dir=run_dir, run_id=run_name, model_name=settings.model_name_or_path,
            instance_id=instance_id, patch=patch, timeout_s=options.eval_timeout_s,
            grader=options.grader,
        )
        _write_json(instance_dir / "eval.json", result)
    if options.remove_images:
        docker_cli.remove_image(image)
    return _instance_record(instance_dir, instance_id)


def provider_outage(instance_dir: Path) -> dict | None:
    """Details when the agent stopped because its model calls failed, else None.

    The agent ends a turn with stop_reason "error" only when the provider call
    itself failed after retries (adapters: provider_request_failed).
    """
    result_path = instance_dir / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if not (result.get("run_status") == "failed" and result.get("stop_reason") == "error"):
        return None
    # The agent reports a failed main-loop model call on agent.adapter.loop_failed
    # (error_type in the payload, status_code/attempts as metrics).
    failures = [event for event in telemetry.read_events(instance_dir / "trace.jsonl")
                if event.get("event_type") == "agent.adapter.loop_failed"
                and (event.get("payload") or {}).get("failure_reason") == "provider_request_failed"]
    last = failures[-1] if failures else {}
    status = (last.get("metrics") or {}).get("status_code")
    if status is not None and status not in RETRYABLE_STATUSES:
        # e.g. 400: the provider rejected what the agent sent. That is the
        # agent's failure and is graded like any other result.
        return None
    return {
        "reason": "provider_request_failed",
        "status_code": status,
        "error_type": (last.get("payload") or {}).get("error_type"),
        "message": (result.get("final_response") or "")[:300],
    }


def _instance_record(instance_dir: Path, instance_id: str, *, skipped: bool = False) -> dict:
    def load(name: str) -> dict:
        path = instance_dir / name
        return json.loads(path.read_text()) if path.exists() else {}

    result, evaluation = load("result.json"), load("eval.json")
    return {
        "instance_id": instance_id,
        "skipped": skipped,
        "agent_status": result.get("status", "missing"),
        "resolved": evaluation.get("resolved"),
        "eval_status": evaluation.get("status"),
    }


def run_all(rows: list[dict], *, run_dir: Path, run_name: str, settings: RunSettings,
            runtime: Runtime, options: RunOptions, workers: int) -> list[dict]:
    if not os.environ.get(settings.api_key_env):
        raise SystemExit(f"set {settings.api_key_env} to the provider API key first")
    if settings.network != "model-api-only":
        raise SystemExit(f"unsupported network policy {settings.network!r}")
    allowed = sandbox.ensure(settings.base_url)
    logger.info("Sandbox: task containers on %s, egress only to %s:443", sandbox.NETWORK, allowed)
    records: list[dict] = []

    def one(row: dict) -> dict:
        try:
            return run_instance(row, run_dir=run_dir, run_name=run_name,
                                settings=settings, runtime=runtime, options=options)
        except Exception as exc:  # noqa: BLE001 - one broken task must not stop the run
            logger.exception("%s failed", row["instance_id"])
            return {"instance_id": row["instance_id"], "agent_status": "harness_error",
                    "error": f"{type(exc).__name__}: {exc}"}

    consecutive_outages = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, row): row["instance_id"] for row in rows}
        for done, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            logger.info("[%d/%d] %s agent=%s eval=%s", done, len(rows), record["instance_id"],
                        record.get("agent_status"), record.get("eval_status"))
            consecutive_outages = consecutive_outages + 1 if record.get("agent_status") == "infra_error" else 0
            if consecutive_outages >= MAX_CONSECUTIVE_OUTAGES:
                # A rate limit or outage would fail every remaining task the same way.
                for pending in futures:
                    pending.cancel()
                logger.error("Stopping: %d tasks in a row failed on the model service (%s). "
                             "Re-run the same command later; finished tasks are kept.",
                             consecutive_outages, record.get("status_code") or record.get("error_type"))
                break
    return records


ATTEMPT_FILES = (
    "trace.jsonl", "result.json", "agent.log", "host.json", "telemetry.json", "task.json",
    "agent-config.json", "patch.diff", "prediction.json", "eval.json", "eval.log", "infra_error.json",
)


def requeue_outages(run_dir: Path) -> list[str]:
    """Release tasks whose finished attempt was a model-service outage.

    The attempt's files move to ``attempts/<n>/`` (kept as evidence) and its
    grader report is removed, so the next ``run``/``baseline-mini`` redoes it.
    """
    from code4me_swebench.baseline_mini import PROVIDER_FAILURES

    manifest = json.loads((run_dir / "manifest.json").read_text())
    model_dirs = list((run_dir / "logs" / "run_evaluation" / run_dir.name).glob("*"))
    released = []
    for instance_id in manifest["instance_ids"]:
        directory = run_dir / "instances" / instance_id
        if not directory.exists():
            continue
        if "baseline" in manifest:
            tel = directory / "telemetry.json"
            exit_status = json.loads(tel.read_text()).get("exit_status") if tel.exists() else None
            outage = exit_status in PROVIDER_FAILURES or (directory / "infra_error.json").exists()
        else:
            if (directory / "infra_error.json").exists() and not provider_outage(directory):
                # Labelled an outage by an older rule (e.g. HTTP 400): it is the
                # agent's own failure, so keep the attempt and let `evaluate` grade it.
                (directory / "infra_error.json").unlink()
                (directory / "prediction.json").write_text(json.dumps({
                    "instance_id": instance_id,
                    "model_name_or_path": f"code4me2-agent__{manifest['settings']['model']}",
                    "model_patch": (directory / "patch.diff").read_text(),
                }, sort_keys=True))
                continue
            outage = bool(provider_outage(directory)) or (directory / "infra_error.json").exists()
        if not outage:
            continue
        attempts = directory / "attempts"
        target = attempts / str(len(list(attempts.glob("*"))) + 1 if attempts.exists() else 1)
        target.mkdir(parents=True)
        for name in ATTEMPT_FILES:
            if (directory / name).exists():
                (directory / name).rename(target / name)
        for model_dir in model_dirs:
            stale = model_dir / instance_id
            if stale.exists():
                shutil.rmtree(stale)
        released.append(instance_id)
    write_predictions(run_dir)
    return released
