"""Run-level report: resolve rate with a confidence interval, plus telemetry totals."""

from __future__ import annotations

import csv
import json
import math
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

COLUMNS = (
    "instance_id", "resolved", "eval_status", "agent_status", "run_status", "stop_reason",
    "model_calls", "prompt_tokens", "cached_prompt_tokens", "completion_tokens",
    "tool_calls", "tool_failures", "test_runs", "files_edited", "patch_lines",
    "agent_wall_s",
)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def instance_rows(run_dir: Path) -> list[dict]:
    manifest = _load(run_dir / "manifest.json")
    rows = []
    for instance_id in manifest.get("instance_ids", []):
        directory = run_dir / "instances" / instance_id
        result, evaluation = _load(directory / "result.json"), _load(directory / "eval.json")
        tel, host = _load(directory / "telemetry.json"), _load(directory / "host.json")
        patch = (directory / "patch.diff").read_text() if (directory / "patch.diff").exists() else ""
        if (directory / "infra_error.json").exists():
            agent_status = "infra_error"
        elif not (directory / "prediction.json").exists():
            agent_status = "not_run"
        elif host.get("agent_timed_out"):
            agent_status = "timeout"
        else:
            agent_status = result.get("status", "missing")
        rows.append({
            "instance_id": instance_id,
            "resolved": evaluation.get("resolved"),
            "eval_status": evaluation.get("status"),
            "agent_status": agent_status,
            "run_status": result.get("run_status"),
            "stop_reason": result.get("stop_reason"),
            "model_calls": tel.get("model_calls", 0),
            "prompt_tokens": tel.get("prompt_tokens", 0),
            "cached_prompt_tokens": tel.get("cached_prompt_tokens", 0),
            "completion_tokens": tel.get("completion_tokens", 0),
            "tool_calls": sum((tel.get("tool_calls") or {}).values()),
            "tool_failures": tel.get("tool_failures", 0),
            "test_runs": tel.get("test_runs", 0),
            "files_edited": len(tel.get("files_edited") or []),
            "patch_lines": sum(1 for line in patch.splitlines()
                               if line[:1] in "+-" and line[:3] not in ("+++", "---")),
            "agent_wall_s": host.get("agent_wall_s"),
        })
    return rows


def build_report(run_dir: Path) -> dict:
    manifest = _load(run_dir / "manifest.json")
    rows = instance_rows(run_dir)
    graded = [row for row in rows if row["resolved"] is not None]
    resolved = sum(1 for row in graded if row["resolved"])
    total = len(rows)
    low, high = wilson_interval(resolved, total)

    def counts(key: str) -> dict:
        out: dict = {}
        for row in rows:
            out[str(row[key])] = out.get(str(row[key]), 0) + 1
        return dict(sorted(out.items()))

    def stat(key: str) -> dict:
        values = [row[key] for row in rows if isinstance(row[key], (int, float))]
        return {"total": round(sum(values), 3), "median": median(values) if values else None}

    report = {
        "run": run_dir.name,
        "model": manifest.get("settings", {}).get("model"),
        "subset": manifest.get("subset"),
        "instances": total,
        "graded": len(graded),
        # Ungraded tasks count as unresolved: the rate is over the whole subset.
        "resolved": resolved,
        "resolve_rate": round(resolved / total, 4) if total else None,
        "resolve_rate_95ci": [round(low, 4), round(high, 4)],
        "agent_status": counts("agent_status"),
        "run_status": counts("run_status"),
        "eval_status": counts("eval_status"),
        "model_calls": stat("model_calls"),
        "prompt_tokens": stat("prompt_tokens"),
        "cached_prompt_tokens": stat("cached_prompt_tokens"),
        "completion_tokens": stat("completion_tokens"),
        "tool_calls": stat("tool_calls"),
        "agent_wall_s": stat("agent_wall_s"),
        "runtime": manifest.get("runtime"),
        "settings": manifest.get("settings"),
    }
    return report


def write_report(run_dir: Path) -> dict:
    report = build_report(run_dir)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    with (run_dir / "instances.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(instance_rows(run_dir))
    return report


def mcnemar_exact_p(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar test on the discordant pairs."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def compare_runs(run_a: Path, run_b: Path) -> dict:
    """Paired comparison of two runs over the tasks both contain."""
    rows_a = {row["instance_id"]: row for row in instance_rows(run_a)}
    rows_b = {row["instance_id"]: row for row in instance_rows(run_b)}
    shared = sorted(set(rows_a) & set(rows_b))
    cells = {"both": 0, "only_a": 0, "only_b": 0, "neither": 0}
    for instance_id in shared:
        a, b = bool(rows_a[instance_id]["resolved"]), bool(rows_b[instance_id]["resolved"])
        cells["both" if a and b else "only_a" if a else "only_b" if b else "neither"] += 1

    def total(rows: dict, key: str) -> int:
        return sum(int(rows[i][key] or 0) for i in shared)

    return {
        "a": run_a.name, "b": run_b.name, "tasks": len(shared),
        "resolved_a": cells["both"] + cells["only_a"],
        "resolved_b": cells["both"] + cells["only_b"],
        **cells,
        "mcnemar_p": round(mcnemar_exact_p(cells["only_a"], cells["only_b"]), 4),
        "prompt_tokens_a": total(rows_a, "prompt_tokens"),
        "prompt_tokens_b": total(rows_b, "prompt_tokens"),
        "model_calls_a": total(rows_a, "model_calls"),
        "model_calls_b": total(rows_b, "model_calls"),
    }
