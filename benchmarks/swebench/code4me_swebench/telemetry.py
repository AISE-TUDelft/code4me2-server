"""Per-task summary of the agent's telemetry trace (``code4me.agent.event.v1``)."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

EDIT_TOOLS = frozenset({
    "create_file", "write_file", "replace_text", "edit_file", "apply_patch",
    "delete_file", "move_file",
})


def read_events(trace_path: Path) -> list[dict]:
    if not trace_path.exists():
        return []
    events = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _cached_prompt_tokens(event: dict) -> int:
    response = ((event.get("raw_payload") or {}).get("response") or {})
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return 0
    # OpenCode routes to upstreams that report the cache in either or both of
    # DeepSeek's and OpenAI's fields (sometimes one is present but null).
    details = usage.get("prompt_tokens_details") or {}
    return max(int(usage.get("prompt_cache_hit_tokens") or 0), int(details.get("cached_tokens") or 0))


def summarize(trace_path: Path) -> dict:
    events = read_events(trace_path)
    by_type = Counter(event.get("event_type") for event in events)
    summary: dict = {
        "events": len(events),
        "event_types": dict(sorted(by_type.items())),
        "model_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_prompt_tokens": 0,
        "usage_estimated_calls": 0,
        "model_time_s": 0.0,
        "call_purposes": {},
        "tool_calls": {},
        "tool_failures": 0,
        "tool_denials": 0,
        "commands": {},
        "test_runs": 0,
        "files_edited": [],
        "run_status": None,
        "stop_reason": None,
        "duration_s": None,
        "model_failures": 0,
        "failed_side_calls": 0,
        "context_compactions": 0,
    }
    purposes: Counter = Counter()
    tools: Counter = Counter()
    commands: Counter = Counter()
    edited: set[str] = set()
    for event in events:
        kind = event.get("event_type")
        payload = event.get("payload") or {}
        metrics = event.get("metrics") or {}
        if kind == "agent.model.completed":
            summary["model_calls"] += 1
            summary["prompt_tokens"] += int(metrics.get("prompt_tokens") or 0)
            summary["completion_tokens"] += int(metrics.get("completion_tokens") or 0)
            summary["cached_prompt_tokens"] += _cached_prompt_tokens(event)
            summary["usage_estimated_calls"] += bool(payload.get("usage_estimated"))
            summary["model_time_s"] += float(metrics.get("duration_ms") or 0) / 1000
            purposes[payload.get("call_purpose") or "unknown"] += 1
        elif kind in {"agent.tool.completed", "agent.tool.failed", "agent.tool.denied"}:
            name = payload.get("tool_name") or "unknown"
            tools[name] += 1
            if kind == "agent.tool.failed" or payload.get("status") == "failed":
                summary["tool_failures"] += 1
            if kind == "agent.tool.denied" or payload.get("status") == "denied":
                summary["tool_denials"] += 1
            argv = payload.get("argv")
            if name == "run_command" and isinstance(argv, list) and argv:
                commands[str(argv[0])] += 1
            if payload.get("test_summary"):
                summary["test_runs"] += 1
            path = payload.get("path")
            if name in EDIT_TOOLS and kind == "agent.tool.completed" and isinstance(path, str):
                edited.add(path)
        elif kind == "agent.model.requested" and metrics.get("context_elided_units"):
            summary["context_compactions"] += 1
        elif kind == "agent.adapter.loop_failed" and payload.get("call_purpose") == "turn":
            summary["model_failures"] += 1
        elif kind == "agent.response.completed":
            summary["stop_reason"] = payload.get("stop_reason")
        elif kind == "agent.run.completed":
            summary["run_status"] = payload.get("status")
            summary["failed_side_calls"] += int(metrics.get("failed_side_calls") or 0)
            duration_ms = metrics.get("duration_ms")
            summary["duration_s"] = round(float(duration_ms) / 1000, 3) if duration_ms else None
    summary["model_time_s"] = round(summary["model_time_s"], 3)
    summary["call_purposes"] = dict(purposes.most_common())
    summary["tool_calls"] = dict(tools.most_common())
    summary["commands"] = dict(commands.most_common())
    summary["files_edited"] = sorted(edited)
    return summary
