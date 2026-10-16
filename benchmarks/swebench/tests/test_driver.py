"""driver.py drives the real agent core; a scripted provider stands in for the model."""

from __future__ import annotations

import json
from pathlib import Path

from code4me_swebench import driver, telemetry
from code4me_swebench.settings import DEFAULT_TOOLS, RunSettings, agent_config


def _call(call_id: str, name: str, **arguments: object) -> dict:
    return {"id": call_id, "name": name, "arguments": arguments}


def _task(tmp_path: Path, workspace: Path, script: list[dict]) -> Path:
    config = agent_config(RunSettings(max_iterations=6), session_id="swebench-test")
    config["workspace_root"] = str(workspace)
    config["telemetry"]["trace_path"] = str(tmp_path / "out" / "trace.jsonl")
    config["adapter"]["fake_provider"] = {"enabled": True, "script": script}
    # The real verification nudge needs a model turn the script does not have.
    config["harness_options"] = {"verify_on_stop": False, "self_review": False}
    task = {
        "instance_id": "demo__demo-1",
        "run_id": "run-demo-1",
        "out_dir": str(tmp_path / "out"),
        "prompt": "Fix the constant in a.py.",
        "agent_config": config,
        "allowed_tools": list(DEFAULT_TOOLS),
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task))
    return path


def test_agent_config_round_trips_through_the_agent_loader(tmp_path):
    from code4me2_agent.config import AgentConfig

    settings = RunSettings(model="deepseek-v4-flash", max_iterations=77, context_tokens=50_000)
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps(agent_config(settings, session_id="s-1")))
    config = AgentConfig.from_file(path)

    provider = config.adapter.provider
    assert (provider.kind, provider.base_url, provider.model) == (
        "openai", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash")
    assert provider.api_key_env == "OPENCODE_GO_API_KEY"
    assert provider.request_deadline_seconds == 300.0
    assert config.adapter.name == "openai_compatible_react"
    assert config.adapter.max_iterations == 77
    assert config.adapter.memory_window.max_tokens == 50_000
    assert config.session_id == "s-1"
    assert str(config.workspace_root) == "/testbed"
    # (/tmp resolves to /private/tmp on macOS; it stays /tmp in the container.)
    assert config.trace_path == Path("/tmp/code4me/out/trace.jsonl").resolve()
    assert config.raw_capture_enabled is True
    assert config.autonomous is True
    assert config.commands.blocked_commands == []


def test_driver_runs_the_agent_and_writes_result_and_trace(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "a.py").write_text("VALUE = 1\n")
    script = [
        {"tool_calls": [_call("c1", "read_file", path="a.py")],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
        {"tool_calls": [_call("c2", "replace_text", path="a.py", old_text="VALUE = 1",
                              new_text="VALUE = 2")]},
        {"final_answer": "Changed VALUE to 2."},
    ]

    exit_code = driver.main(["driver.py", str(_task(tmp_path, workspace, script))])

    result = json.loads((tmp_path / "out" / "result.json").read_text())
    assert exit_code == 0, result.get("traceback")
    assert result["status"] == "completed"
    assert result["run_id"] == "run-demo-1"
    assert "VALUE to 2" in result["final_response"]
    assert (workspace / "a.py").read_text() == "VALUE = 2\n"

    summary = telemetry.summarize(tmp_path / "out" / "trace.jsonl")
    assert summary["model_calls"] == 3
    assert summary["prompt_tokens"] >= 100
    assert summary["tool_calls"] == {"read_file": 1, "replace_text": 1}
    assert summary["files_edited"] == ["a.py"]
    assert summary["run_status"] == "completed"


def test_driver_keeps_ask_user_out_of_the_tool_set(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    task_path = _task(tmp_path, workspace, [{"final_answer": "done"}])
    task = json.loads(task_path.read_text())

    config = driver.build_agent_config(task)

    assert "ask_user" not in config.allowed_tools
    assert config.approval_policy == "auto"
    assert config.store_agent_content is True


def test_driver_records_failures_instead_of_raising(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    task_path = _task(tmp_path, workspace, [])
    task = json.loads(task_path.read_text())
    del task["prompt"]
    task_path.write_text(json.dumps(task))

    exit_code = driver.main(["driver.py", str(task_path)])

    result = json.loads((tmp_path / "out" / "result.json").read_text())
    assert exit_code == 1
    assert result["status"] == "error"
    assert result["error"].startswith("KeyError")


def test_a_failed_optional_review_is_reported_by_the_run_and_the_trace_checks(tmp_path):
    from code4me_swebench import check

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "a.py").write_text("VALUE = 1\n")
    script = [
        {"tool_calls": [_call("c1", "read_file", path="a.py")]},
        {"tool_calls": [_call("c2", "replace_text", path="a.py", old_text="VALUE = 1",
                              new_text="VALUE = 2")]},
        {"final_answer": "Changed VALUE to 2."},
        # No entry for the self-review call: the scripted provider raises.
    ]
    task_path = _task(tmp_path, workspace, script)
    task = json.loads(task_path.read_text())
    task["agent_config"]["harness_options"] = {"verify_on_stop": False, "self_review": True}
    task_path.write_text(json.dumps(task))

    assert driver.main(["driver.py", str(task_path)]) == 0

    trace = tmp_path / "out" / "trace.jsonl"
    events = telemetry.read_events(trace)
    run_completed = next(e for e in events if e["event_type"] == "agent.run.completed")
    assert run_completed["metrics"]["failed_side_calls"] == 1
    assert run_completed["payload"]["side_call_failures"][0]["call_purpose"] == "self_review"
    assert telemetry.summarize(trace)["failed_side_calls"] == 1
    assert check.check_trace(trace) == []
