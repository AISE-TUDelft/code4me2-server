"""Run-level report: resolve rate with a confidence interval, plus telemetry totals."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, median, stdev

REFERENCES = Path(__file__).resolve().parents[1] / "references.json"

COLUMNS = (
    "instance_id", "resolved", "eval_status", "agent_status", "run_status", "stop_reason",
    "model_calls", "prompt_tokens", "cached_prompt_tokens", "completion_tokens",
    "tool_calls", "tool_failures", "test_runs", "files_edited", "patch_lines",
    "agent_wall_s", "cost_usd",
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
            "cost_usd": tel.get("cost_usd"),
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
        # Provider-reported spend; total 0 when the provider reports none.
        "cost_usd": stat("cost_usd"),
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


def _window_stats(per_run: list[list[dict]], task_ids: list[str]) -> dict:
    """The SWE-rebench leaderboard's metrics over repeated runs of the same tasks:
    resolved rate = mean of the per-run rates (every task in the denominator; an
    unrun or ungraded task is unresolved); SEM = sample standard deviation of
    those rates / sqrt(runs); pass@n = tasks resolved in at least one run;
    tokens/problem = mean over task-runs of input (cached included) + output;
    cached share = mean over task-runs of cached / input."""
    wanted = set(task_ids)
    rates, solved_any = [], set()
    task_runs = []
    for rows in per_run:
        mine = [row for row in rows if row["instance_id"] in wanted]
        task_runs.extend(mine)
        resolved = {row["instance_id"] for row in mine if row["resolved"]}
        solved_any |= resolved
        rates.append(100.0 * len(resolved) / len(wanted))
    runs = len(rates)
    used = [row for row in task_runs if (row["prompt_tokens"] or 0) > 0]
    costs = [row["cost_usd"] for row in task_runs if isinstance(row.get("cost_usd"), (int, float))]
    return {
        "tasks": len(wanted),
        "runs": runs,
        "resolved_rate": round(mean(rates), 2),
        "sem": round(stdev(rates) / math.sqrt(runs), 2) if runs > 1 else None,
        "per_run_rates": [round(rate, 2) for rate in rates],
        f"pass_at_{runs}": round(100.0 * len(solved_any) / len(wanted), 2),
        "tokens_per_problem": round(mean((row["prompt_tokens"] or 0) + (row["completion_tokens"] or 0)
                                         for row in task_runs)) if task_runs else None,
        "cached_share": round(100.0 * mean((row["cached_prompt_tokens"] or 0) / row["prompt_tokens"]
                                           for row in used), 2) if used else None,
        # Billed by the provider (OpenRouter's usage.cost); None when it reports none.
        "cost_per_problem": round(mean(costs), 4) if costs and len(costs) == len(task_runs) else None,
        "model_calls_per_problem": round(mean(row["model_calls"] or 0 for row in task_runs), 1) if task_runs else None,
        "agent_status": dict(sorted(Counter(row["agent_status"] for row in task_runs).items())),
    }


def aggregate_runs(run_dirs: list[Path], *, windows: list[tuple[str, str]] = (),
                   reference: str | None = None) -> dict:
    """Leaderboard-style results for repeated runs of one task set, overall and per
    time window [start, end) by task creation date, optionally next to a published
    reference (``references.json``)."""
    manifests = [_load(run_dir / "manifest.json") for run_dir in run_dirs]
    ids = manifests[0].get("instance_ids", [])
    for run_dir, manifest in zip(run_dirs, manifests):
        if manifest.get("instance_ids") != ids or manifest.get("settings") != manifests[0].get("settings"):
            raise SystemExit(f"{run_dir.name} has other tasks or settings than {run_dirs[0].name}")
    per_run = [instance_rows(run_dir) for run_dir in run_dirs]
    created = {row["instance_id"]: str(row.get("created_at"))[:10]
               for row in json.loads((run_dirs[0] / "dataset.json").read_text())}
    span = (min(created[i] for i in ids), max(created[i] for i in ids)) if ids else (None, None)
    result = {
        "runs": [run_dir.name for run_dir in run_dirs],
        "dataset": manifests[0].get("dataset"), "split": manifests[0].get("split"),
        "dataset_revision": manifests[0].get("dataset_revision"),
        "model": manifests[0].get("settings", {}).get("model"),
        "created": span,
        "overall": _window_stats(per_run, ids),
        "windows": {},
    }
    for start, end in windows:
        window_ids = [i for i in ids if start <= created[i] < end]
        if window_ids:
            result["windows"][f"{start}..{end}"] = _window_stats(per_run, window_ids)
    if reference:
        refs = json.loads(REFERENCES.read_text())["references"]
        if reference not in refs:
            raise SystemExit(f"unknown reference {reference!r}; known: {', '.join(sorted(refs))}")
        result["reference"] = {"name": reference, **{k: v for k, v in refs[reference].items() if k != "windows"}}
        for key, ref in refs[reference]["windows"].items():
            ours = result["windows"].get(key)
            # Only the same task set is comparable (an incomplete window is not).
            if not ours or ours["tasks"] != ref["tasks"]:
                continue
            diff = ours["resolved_rate"] - ref["resolved_rate"]
            spread = math.sqrt((ours["sem"] or 0.0) ** 2 + ref["sem"] ** 2)
            ours["reference"] = {**ref, "difference": round(diff, 2),
                                 "z": round(diff / spread, 2) if spread else None}
    return result
