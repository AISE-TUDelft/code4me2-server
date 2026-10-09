"""Consistency checks over a run's telemetry traces.

Each task's ``trace.jsonl`` must be one complete, ordered event stream: every
model request answered or recorded as failed, every tool call resolved, token
metrics equal to the provider's own usage, and no provider key anywhere.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from code4me_swebench.telemetry import read_events

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA = "code4me.agent.event.v1"
TOOL_OUTCOMES = {"agent.tool.completed", "agent.tool.failed", "agent.tool.denied"}


def _time(event: dict) -> datetime:
    return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))


def check_trace(trace_path: Path, *, secret: str | None = None) -> list[str]:
    """Problems found in one trace (empty = consistent)."""
    if not trace_path.exists():
        return ["trace.jsonl missing"]
    raw = trace_path.read_bytes()
    problems: list[str] = []
    if secret and secret.encode() in raw:
        problems.append("provider key found in trace")
    try:
        events = read_events(trace_path)
    except json.JSONDecodeError as exc:
        return [*problems, f"unparseable line: {exc}"]
    if not events:
        return [*problems, "trace is empty"]

    kinds = Counter(event.get("event_type") for event in events)
    if any(event.get("schema_version") != SCHEMA for event in events):
        problems.append("event with another schema_version")
    for field in ("run_id", "session_id"):
        values = {event.get(field) for event in events}
        if len(values) != 1:
            problems.append(f"{len(values)} distinct {field} values")
    if [event.get("sequence") for event in events] != list(range(1, len(events) + 1)):
        problems.append("sequence numbers are not 1..N in file order")
    if any(_time(b) < _time(a) for a, b in zip(events, events[1:])):
        problems.append("timestamps go backwards")
    if events[0].get("event_type") != "agent.run.started":
        problems.append("first event is not agent.run.started")
    if kinds["agent.run.completed"] != 1:
        problems.append(f"{kinds['agent.run.completed']} agent.run.completed events")

    # A request is answered by agent.model.completed, by a main-loop failure
    # (agent.adapter.loop_failed for the "turn" call), or by a failed optional
    # side call counted on agent.run.completed (metrics.failed_side_calls).
    loop_failures = sum(1 for e in events if e["event_type"] == "agent.adapter.loop_failed"
                        and (e.get("payload") or {}).get("call_purpose") == "turn")
    side_failures = sum(int((e.get("metrics") or {}).get("failed_side_calls") or 0)
                        for e in events if e["event_type"] == "agent.run.completed")
    answered = kinds["agent.model.completed"] + loop_failures + side_failures
    if kinds["agent.model.requested"] != answered:
        problems.append(
            f"{kinds['agent.model.requested']} model requests but {answered} completed or failed")

    called = {e["payload"].get("tool_call_id") for e in events if e["event_type"] == "agent.tool.called"}
    resolved = {e["payload"].get("tool_call_id") for e in events if e["event_type"] in TOOL_OUTCOMES}
    if called - resolved:
        problems.append(f"{len(called - resolved)} tool calls without an outcome")

    ids = {event.get("event_id") for event in events}
    dangling = sum(1 for e in events if e.get("parent_event_id") and e["parent_event_id"] not in ids)
    if dangling:
        problems.append(f"{dangling} parent_event_id values point nowhere")

    mismatched = 0
    for event in events:
        if event["event_type"] != "agent.model.completed" or event["payload"].get("usage_estimated"):
            continue
        usage = ((event.get("raw_payload") or {}).get("response") or {}).get("usage")
        if not isinstance(usage, dict):
            problems.append("model.completed without raw provider usage")
            break
        metrics = event.get("metrics") or {}
        if (metrics.get("prompt_tokens"), metrics.get("completion_tokens")) != (
                usage.get("prompt_tokens"), usage.get("completion_tokens")):
            mismatched += 1
    if mismatched:
        problems.append(f"{mismatched} model calls whose token metrics differ from provider usage")
    return problems


def check_run(run_dir: Path, *, secret_env: str | None = None) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    secret = os.environ.get(secret_env or manifest.get("settings", {}).get("api_key_env") or "")
    if "baseline" in manifest:
        return {"run": run_dir.name, "skipped": "baseline run (mini-swe-agent trajectories, no agent trace)"}
    results: dict[str, list[str]] = {}
    for instance_id in manifest["instance_ids"]:
        directory = run_dir / "instances" / instance_id
        if not (directory / "prediction.json").exists():
            results[instance_id] = ["not run"]
            continue
        problems = check_trace(directory / "trace.jsonl", secret=secret or None)
        result_file = directory / "result.json"
        if not result_file.exists() or json.loads(result_file.read_text()).get("status") != "completed":
            problems.append("driver did not complete (result.json)")
        results[instance_id] = problems
    return {
        "run": run_dir.name,
        "tasks": len(results),
        "consistent": sum(1 for problems in results.values() if not problems),
        "key_checked": bool(secret),
        "problems": {iid: problems for iid, problems in results.items() if problems},
    }
