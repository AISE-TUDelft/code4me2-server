"""Runs one SWE-bench task with the Code4Me agent, inside the task container.

This file is copied into the container and executed by the runtime's Python
(``/opt/code4me/venv``), so it may import only the standard library and
``code4me2_agent``. It drives the agent core directly: no IDE, plugin or ACP
transport. Telemetry is the agent's own canonical event stream
(``code4me.agent.event.v1``), written as JSONL to ``trace_path``.

Usage: python driver.py <task.json>
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from time import perf_counter


def build_agent_config(task: dict):
    from code4me2_agent.config import AgentConfig

    out_dir = Path(task["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    config_path = out_dir / "agent-config.json"
    config_path.write_text(json.dumps(task["agent_config"], indent=2) + "\n")
    config = AgentConfig.from_file(config_path)
    # The config file format has no tool/approval fields: those normally come
    # from the assigned profile, so they are applied here the same way.
    allowed = task.get("allowed_tools")
    return replace(
        config,
        tools=list(allowed) if allowed is not None else None,
        allowed_tools=frozenset(allowed) if allowed is not None else None,
        approval_policy="auto",
        store_agent_content=True,
    )


def main(argv: list[str]) -> int:
    # The agent logs provider retries and harness decisions; they land in agent.log.
    logging.basicConfig(
        level=getattr(logging, os.environ.get("CODE4ME_AGENT_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    task = json.loads(Path(argv[1]).read_text())
    out_dir = Path(task["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"instance_id": task["instance_id"], "status": "error"}
    started = perf_counter()
    try:
        from code4me2_agent._build_version import RUNTIME_VERSION
        from code4me2_agent.echo import EchoAgentCore

        result["runtime_version"] = RUNTIME_VERSION
        core = EchoAgentCore(build_agent_config(task))
        outcome = core.handle_prompt(task["prompt"], run_id=task["run_id"])
        result.update(
            status="completed",
            run_id=outcome.run_id,
            stop_reason=outcome.stop_reason,
            run_status=outcome.run_status,
            final_response=outcome.final_response,
            usage=outcome.usage,
        )
    except BaseException as exc:  # noqa: BLE001 - recorded for the report
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    result["duration_s"] = round(perf_counter() - started, 3)
    (out_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
