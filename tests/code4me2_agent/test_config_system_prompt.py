"""The researcher-defined system prompt in the agent runtime (origin/sys_prompt port).

Covers ``code4me2_agent.config``: parsing the server's ``agent-config`` and managed
run policy payloads, ``has_overrides`` accounting, the merge that lands the prompt on
the ``AdapterConfig``, and the ReAct adapter's system context using it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code4me2_agent.adapters import OpenAICompatibleReactAdapter
from code4me2_agent.config import AdapterConfig, AgentConfig, ServerAgentConfig


def _base_config(*, system_prompt: str | None = None, workspace: Path = Path("/work")) -> AgentConfig:
    return AgentConfig(
        workspace_root=workspace,
        trace_path=workspace / ".code4me" / "trace.jsonl",
        session_id="test-session",
        adapter=AdapterConfig(system_prompt=system_prompt),
    )


def _managed_policy(**overrides):
    policy = {
        "version": "1",
        "transport": "managed_backend",
        "agent_profile": "arm-a",
        "framework_version": "code4me2-agent",
        "model": "provider/model",
        "tools": ["read_file"],
        "approval_policy": "per_step",
        "temperature": None,
        "max_iterations": 4,
        "max_context_tokens": 32000,
        "commands_allowlist": ["git"],
        "store_agent_content": False,
    }
    policy.update(overrides)
    return policy


def test_from_payload_parses_system_prompt():
    server = ServerAgentConfig.from_payload({"system_prompt": "You are a reviewer."})
    assert server.system_prompt == "You are a reviewer."


def test_from_payload_blank_and_missing_mean_none():
    assert ServerAgentConfig.from_payload({}).system_prompt is None
    assert ServerAgentConfig.from_payload({"system_prompt": "   "}).system_prompt is None


def test_managed_policy_carries_the_prompt_and_rejects_a_non_string():
    with_prompt = ServerAgentConfig.from_managed_payload(
        _managed_policy(system_prompt="Review every diff.")
    )
    assert with_prompt.system_prompt == "Review every diff."
    without = ServerAgentConfig.from_managed_payload(_managed_policy())
    assert without.system_prompt is None
    try:
        ServerAgentConfig.from_managed_payload(_managed_policy(system_prompt=42))
    except ValueError:
        pass
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a non-string system_prompt must fail closed")


def test_has_overrides_treats_the_prompt_as_an_override():
    assert ServerAgentConfig(system_prompt="You are X.").has_overrides is True
    assert ServerAgentConfig().has_overrides is False


def test_server_prompt_replaces_adapter_prompt():
    merged = _base_config(system_prompt="local prompt").with_server_overrides(
        ServerAgentConfig(system_prompt="researcher prompt")
    )
    assert merged.adapter.system_prompt == "researcher prompt"


def test_server_without_prompt_keeps_local_adapter_prompt():
    base = _base_config(system_prompt="local prompt")
    assert base.with_server_overrides(ServerAgentConfig(model="m")).adapter.system_prompt == "local prompt"
    assert (
        base.with_server_overrides(ServerAgentConfig(model="m", system_prompt=None)).adapter.system_prompt
        == "local prompt"
    )


def test_prompt_survives_other_server_overrides():
    merged = _base_config().with_server_overrides(
        ServerAgentConfig(
            system_prompt="Review every diff.",
            model="qwen2.5-coder:7b",
            base_url="http://localhost:11434/v1",
            max_iterations=4,
        )
    )
    assert merged.adapter.system_prompt == "Review every diff."
    assert merged.adapter.provider.model == "qwen2.5-coder:7b"
    assert merged.adapter.max_iterations == 4


def _system_context(config: AgentConfig) -> str:
    adapter = object.__new__(OpenAICompatibleReactAdapter)
    adapter._config = config
    with patch("code4me2_agent.command_tools.available_commands", return_value=[]):
        return adapter._system_context(tool_names=["read_file", "edit_file"])


def test_system_context_starts_with_the_researcher_prompt(tmp_path):
    context = _system_context(_base_config(system_prompt="ZEBRA-42: always sign off.", workspace=tmp_path))
    assert context.startswith("ZEBRA-42: always sign off.")
    # The built-in persona line is replaced, the operational instructions stay.
    assert "You are Code4Me, a coding agent" not in context
    assert "Tools available in this session: read_file, edit_file." in context
    assert "Approval policy:" in context
    assert tmp_path.as_posix() in context


def test_system_context_without_a_prompt_keeps_the_default_persona(tmp_path):
    context = _system_context(_base_config(workspace=tmp_path))
    assert context.startswith("You are Code4Me, a coding agent")
