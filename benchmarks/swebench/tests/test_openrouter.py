from __future__ import annotations

import json

import pytest
from code4me_swebench import baseline_mini, check, cli, telemetry
from code4me_swebench.settings import (
    RunSettings,
    agent_config,
    openrouter_routing,
    pinned_upstreams,
)

OPENROUTER = "https://openrouter.ai/api/v1"


def _settings(*flags):
    args = cli.build_parser().parse_args(
        ["run", "--run-name", "x", "--base-url", OPENROUTER, "--model", "deepseek/deepseek-v4.1-flash", *flags])
    return cli._settings(args)


def test_openrouter_runs_must_pin_one_upstream_and_both_agents_send_it():
    with pytest.raises(SystemExit, match="openrouter-upstream"):
        _settings()
    with pytest.raises(SystemExit, match="openrouter-upstream"):
        _settings("--extra-body", '{"provider": {"order": ["deepseek"]}}')  # fallbacks still allowed
    settings = _settings("--openrouter-upstream", "deepseek")
    assert pinned_upstreams(settings.extra_body) == ["deepseek"]
    assert settings.extra_body["provider"]["allow_fallbacks"] is False
    assert settings.extra_body["reasoning"] == {"effort": "high"}  # explicit, overridable
    assert _settings("--openrouter-upstream", "deepseek",
                     "--extra-body", '{"reasoning": {"effort": "max"}}').extra_body["reasoning"] == {"effort": "max"}
    assert agent_config(settings, session_id="s")["adapter"]["provider"]["extra_body"] == settings.extra_body
    assert settings.as_dict()["extra_body"] == settings.extra_body
    overlay = baseline_mini.overlay_config(settings)
    assert overlay["model"]["model_kwargs"]["extra_body"] == settings.extra_body
    assert baseline_mini.model_class_args(settings) == []  # stock litellm: no OpenCode header
    # Runs without extra fields keep their manifest and config shape.
    plain = RunSettings()
    assert "extra_body" not in plain.as_dict()
    assert "extra_body" not in agent_config(plain, session_id="s")["adapter"]["provider"]
    assert baseline_mini.model_class_args(plain) == ["--model-class", "opencode_model.OpenCodeModel"]


def _completed(index: int, provider: str | None, cost: float) -> dict:
    response = {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": cost}}
    if provider:
        response["provider"] = provider
    return {"event_type": "agent.model.completed", "timestamp": f"2026-10-14T00:00:0{index}Z",
            "payload": {"usage_estimated": True}, "raw_payload": {"response": response}}


def test_calls_from_unpinned_upstreams_fail_the_check_and_spend_is_summed(tmp_path):
    trace = tmp_path / "trace.jsonl"
    events = [_completed(1, "DeepSeek", 0.002), _completed(2, "Novita", 0.001), _completed(3, None, 0.0)]
    trace.write_text("".join(json.dumps(event) + "\n" for event in events))

    problems = check.check_trace(trace, upstreams=openrouter_routing(["deepseek"])["provider"]["only"])
    assert "model calls served by unpinned upstreams: {'Novita': 1}" in problems
    assert "1 model calls without the serving upstream in the response" in problems
    assert not [p for p in check.check_trace(trace) if "upstream" in p]  # nothing pinned, nothing checked
    summary = telemetry.summarize(trace)
    assert summary["upstreams"] == {"DeepSeek": 1, "Novita": 1}
    assert summary["cost_usd"] == 0.003


def test_parsed_test_runs_are_counted(tmp_path):
    trace = tmp_path / "trace.jsonl"
    run = {"event_type": "agent.tool.completed", "payload": {
        "tool_name": "run_command", "argv": ["python", "tests/runtests.py"], "status": "ok",
        "test_framework": "unittest", "tests_passed": 79, "tests_failed": 0}}
    other = {"event_type": "agent.tool.completed", "payload": {
        "tool_name": "run_command", "argv": ["git", "status"], "status": "ok"}}
    trace.write_text(json.dumps(run) + "\n" + json.dumps(other) + "\n")
    assert telemetry.summarize(trace)["test_runs"] == 1
