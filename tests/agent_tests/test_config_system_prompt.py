"""Unit tests for the researcher-defined system prompt in the agent runtime.

Covers ``code4me2_agent.config``: parsing the server's ``agent-config`` payload,
``has_overrides`` accounting, and the merge that lands the prompt on the
``AdapterConfig`` consumed by the ReAct loop's message assembly.
"""

from pathlib import Path

from code4me2_agent.config import (
    AdapterConfig,
    AgentConfig,
    ServerAgentConfig,
)


def _base_config(*, system_prompt: str | None = None) -> AgentConfig:
    adapter = AdapterConfig(system_prompt=system_prompt)
    return AgentConfig(
        workspace_root=Path("/work"),
        trace_path=Path("/work/.code4me/trace.jsonl"),
        session_id="test-session",
        adapter=adapter,
    )


def test_from_payload_parses_system_prompt():
    server = ServerAgentConfig.from_payload({"system_prompt": "You are a reviewer."})
    assert server.system_prompt == "You are a reviewer."


def test_from_payload_blank_and_missing_mean_none():
    assert ServerAgentConfig.from_payload({}).system_prompt is None
    assert ServerAgentConfig.from_payload({"system_prompt": "   "}).system_prompt is None


def test_has_overrides_treated_as_override():
    assert ServerAgentConfig(system_prompt="You are X.").has_overrides is True
    assert ServerAgentConfig().has_overrides is False
    assert ServerAgentConfig(model="m").has_overrides is True


def test_server_prompt_replaces_adapter_prompt():
    base = _base_config(system_prompt="local prompt")
    merged = base.with_server_overrides(
        ServerAgentConfig(system_prompt="researcher prompt")
    )
    assert merged.adapter.system_prompt == "researcher prompt"


def test_server_missing_prompt_keeps_local_adapter_prompt():
    base = _base_config(system_prompt="local prompt")
    merged = base.with_server_overrides(ServerAgentConfig(model="some-model"))
    assert merged.adapter.system_prompt == "local prompt"


def test_server_none_prompt_keeps_local_adapter_prompt():
    base = _base_config(system_prompt="local prompt")
    merged = base.with_server_overrides(
        ServerAgentConfig(model="some-model", system_prompt=None)
    )
    assert merged.adapter.system_prompt == "local prompt"


def test_prompt_survives_other_server_overrides():
    base = _base_config()
    merged = base.with_server_overrides(
        ServerAgentConfig(
            system_prompt="Review every diff.",
            model="qwen2.5-coder:7b",
            base_url="http://localhost:11434/v1",
            max_iterations=4,
        )
    )
    assert merged.adapter.system_prompt == "Review every diff."
    assert merged.adapter.provider.model == "qwen2.5-coder:7b"
    assert merged.adapter.provider.base_url == "http://localhost:11434/v1"
    assert merged.adapter.max_iterations == 4