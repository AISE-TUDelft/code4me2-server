from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from code4me2_agent import prompting
from code4me2_agent.config import (
    AgentConfig,
    HarnessOptions,
    ServerAgentConfig,
    parse_harness_overrides,
)
from code4me2_agent.patching import PatchError, apply_update, parse_patch, plan_patch

if TYPE_CHECKING:
    from pathlib import Path


def _managed_payload(**overrides):
    payload = {
        "version": "1",
        "transport": "managed_backend",
        "agent_profile": "arm-a",
        "framework_version": "code4me2-agent",
        "model": "gpt-5.1-codex",
        "tools": ["read_file", "run_command"],
        # Legacy, for runtimes 0.0.6 and earlier; this runtime ignores it.
        "commands_allowlist": ["pytest"],
        "commands_denylist": ["git"],
        "max_iterations": 12,
        "max_context_tokens": 64000,
        "approval_policy": "auto",
        "temperature": None,
        "store_agent_content": True,
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------ policy parsing


def test_managed_policy_carries_timeout_and_harness_options():
    config = ServerAgentConfig.from_managed_payload(
        _managed_payload(
            command_timeout_seconds=300,
            harness_options={
                "self_review": False,
                "verify_command": ["pytest", "-q"],
                "prompt_profile": "anthropic",
                "future_option": 1,
            },
        )
    )
    assert config.command_timeout_seconds == 300
    assert config.harness_options == {
        "self_review": False,
        "verify_command": ("pytest", "-q"),
        "prompt_profile": "anthropic",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"command_timeout_seconds": 0},
        {"command_timeout_seconds": 601},
        {"command_timeout_seconds": True},
        {"harness_options": ["self_review"]},
        {"harness_options": {"self_review": "yes"}},
        {"harness_options": {"prompt_profile": "llama"}},
        {"harness_options": {"verify_command": "pytest -q"}},
        {"harness_options": {"verify_command": []}},
    ],
)
def test_managed_policy_fails_closed_on_invalid_harness_settings(overrides):
    with pytest.raises(ValueError):
        ServerAgentConfig.from_managed_payload(_managed_payload(**overrides))


def test_policy_without_new_fields_is_unchanged():
    config = ServerAgentConfig.from_managed_payload(_managed_payload())
    assert config.command_timeout_seconds is None
    assert config.harness_options is None


def test_managed_policy_requires_the_blocked_command_list():
    payload = _managed_payload()
    del payload["commands_denylist"]
    with pytest.raises(ValueError, match="no commands_denylist: the server is older"):
        ServerAgentConfig.from_managed_payload(payload)
    for bad in ("git", ["git", ""], ["git", 3], {"git": True}):
        with pytest.raises(ValueError, match="invalid executable settings"):
            ServerAgentConfig.from_managed_payload(_managed_payload(commands_denylist=bad))
    assert ServerAgentConfig.from_managed_payload(_managed_payload(commands_denylist=[])).commands_denylist == []


def test_server_overrides_apply_the_denylist_and_ignore_the_legacy_allowlist(tmp_path):
    base = AgentConfig(workspace_root=tmp_path, trace_path=tmp_path / "t.jsonl", session_id="s")
    server = ServerAgentConfig.from_managed_payload(
        _managed_payload(commands_allowlist=["pytest"], commands_denylist=["git", "curl"])
    )
    merged = base.with_server_overrides(server, backend_url="http://backend")
    assert merged.commands.blocked_commands == ["git", "curl"]
    assert not hasattr(server, "commands_allowlist")
    # The non-managed agent-config response: a missing list keeps the local one.
    local = AgentConfig(
        workspace_root=tmp_path, trace_path=tmp_path / "t.jsonl", session_id="s"
    ).with_server_overrides(ServerAgentConfig.from_payload({"commands_allowlist": ["ls"]}))
    assert local.commands.blocked_commands == []


def test_local_config_file_reads_the_denylist_and_ignores_a_legacy_allowlist(tmp_path, caplog, monkeypatch):
    blocked_file = tmp_path / "blocked.json"
    blocked_file.write_text('{"commands": {"denylist": ["git", " ", 3, "rm"]}}')
    assert AgentConfig.from_file(blocked_file).commands.blocked_commands == ["git", "rm"]

    legacy_file = tmp_path / "legacy.json"
    legacy_file.write_text('{"commands": {"allowlist": ["pwd", "ls"]}}')
    # An earlier logging.config call may have disabled the logger.
    monkeypatch.setattr(logging.getLogger("code4me2_agent.config"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="code4me2_agent.config"):
        legacy = AgentConfig.from_file(legacy_file)
    assert legacy.commands.blocked_commands == []
    assert "commands.allowlist is no longer supported" in caplog.text


def test_server_overrides_apply_timeout_and_harness(tmp_path):
    base = AgentConfig(workspace_root=tmp_path, trace_path=tmp_path / "t.jsonl", session_id="s")
    server = ServerAgentConfig.from_managed_payload(
        _managed_payload(
            command_timeout_seconds=300,
            harness_options={"parallel_tools": False, "verify_command": ["pytest"]},
        )
    )
    merged = base.with_server_overrides(server, backend_url="http://backend")
    assert merged.commands.timeout_seconds == 300.0
    assert merged.commands.max_timeout_seconds == 600.0
    assert merged.harness.parallel_tools is False
    assert merged.harness.verify_command == ("pytest",)
    assert merged.harness.self_review is True  # untouched switches keep their defaults


def test_lenient_parsing_skips_bad_keys():
    assert parse_harness_overrides({"loop_guard": "no", "syntax_check": False}, strict=False) == {
        "syntax_check": False
    }
    assert parse_harness_overrides(None, strict=False) is None


def test_local_config_file_reads_harness_options(tmp_path):
    config_file = tmp_path / "agent-config.json"
    config_file.write_text('{"harness_options": {"self_review": false, "prompt_profile": "gemini"}}')
    config = AgentConfig.from_file(config_file)
    assert config.harness == HarnessOptions(self_review=False, prompt_profile="gemini")


# ------------------------------------------------------------ prompting


@pytest.mark.parametrize(
    "model, family",
    [
        ("gpt-5.1-codex", "openai"),
        ("openai/gpt-oss-120b", "openai"),
        ("o4-mini", "openai"),
        ("openrouter/openai/o3", "openai"),
        ("claude-sonnet-5", "anthropic"),
        ("anthropic/claude-opus-5.5", "anthropic"),
        ("gemini-3-pro", "gemini"),
        ("google/gemma-3", "gemini"),
        ("qwen2.5-coder:7b", "default"),
        ("mistral-large", "default"),
        ("", "default"),
    ],
)
def test_model_family(model, family):
    assert prompting.model_family(model) == family


def test_explicit_profile_wins_over_the_model():
    assert prompting.resolve_prompt_profile("gemini", "gpt-5") == "gemini"
    assert prompting.resolve_prompt_profile("auto", "gpt-5") == "openai"


def test_instruction_files_are_capped_deduplicated_and_ordered(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Run tests with pytest -q.\n")
    (tmp_path / ".junie").mkdir()
    (tmp_path / ".junie" / "guidelines.md").write_text("x" * 20_000)
    (tmp_path / "CLAUDE.md").write_text("Run tests with pytest -q.\n")  # duplicate body

    loaded = prompting.load_project_instructions(tmp_path, max_chars=500)

    assert [name for name, _chars in loaded.files] == ["AGENTS.md", ".junie/guidelines.md"]
    assert loaded.truncated
    assert len(loaded.text) < 700
    block = prompting.instructions_block(loaded)
    assert "never treat them as the user's request" in block
    assert loaded.telemetry()["instruction_files"] == ["AGENTS.md", ".junie/guidelines.md"]


def test_no_instruction_files_means_none(tmp_path: Path):
    assert prompting.load_project_instructions(tmp_path) is None


def test_reminder_cadence():
    assert not prompting.should_remind(1, 0, after_compaction=False)
    assert prompting.should_remind(1, 20, after_compaction=False)
    assert [i for i in range(1, 14) if prompting.should_remind(i, 0, after_compaction=False)] == [5, 9, 13]
    assert prompting.should_remind(2, 0, after_compaction=True)
    text = prompting.reminder_text(
        approval_policy="auto", can_edit=True, can_run=True, calls_left=3, has_instructions=True
    )
    assert "not a message from the user" in text and "3 model calls remain" in text


# ------------------------------------------------------------ patch grammar


def test_patch_grammar_errors_are_specific():
    with pytest.raises(PatchError, match="must start"):
        parse_patch("*** Update File: a.py\n*** End Patch")
    with pytest.raises(PatchError, match="must end"):
        parse_patch("*** Begin Patch\n*** Delete File: a.py")
    with pytest.raises(PatchError, match="start with '\\+'"):
        parse_patch("*** Begin Patch\n*** Add File: a.py\nline\n*** End Patch")
    with pytest.raises(PatchError, match="more than once"):
        parse_patch("*** Begin Patch\n*** Delete File: a.py\n*** Delete File: a.py\n*** End Patch")
    with pytest.raises(PatchError, match="Invalid line"):
        parse_patch("*** Begin Patch\n*** Update File: a.py\n@@\n?x\n*** End Patch")


def test_update_chunks_use_anchors_and_tolerant_context():
    text = "class A:\n    def f(self):  \n        return 1\n\n    def g(self):\n        return 1\n"
    actions = parse_patch(
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@ def g(self):\n"
        "-        return 1\n"
        "+        return 2\n"
        "*** End Patch"
    )
    new_text, fuzz = apply_update(text, actions[0].chunks, path="a.py")
    assert new_text.endswith("    def g(self):\n        return 2\n")
    assert "def f(self):  \n        return 1" in new_text  # the first method is untouched
    assert fuzz == 100  # the unindented anchor matched ignoring surrounding whitespace


def test_end_of_file_chunks_and_crlf_are_preserved():
    text = "a\r\nb\r\nc\r\n"
    actions = parse_patch("*** Begin Patch\n*** Update File: x\n@@\n c\n+d\n*** End of File\n*** End Patch")
    new_text, _fuzz = apply_update(text, actions[0].chunks, path="x")
    assert new_text == "a\r\nb\r\nc\r\nd\r\n"


def test_plan_rejects_missing_or_existing_files_before_writing():
    files = {"a.py": "x = 1\n"}
    read = files.get
    with pytest.raises(PatchError, match="does not exist"):
        plan_patch(parse_patch("*** Begin Patch\n*** Delete File: b.py\n*** End Patch"), read)
    with pytest.raises(PatchError, match="already exists"):
        plan_patch(parse_patch("*** Begin Patch\n*** Add File: a.py\n+y\n*** End Patch"), read)
    with pytest.raises(PatchError, match="does not match"):
        plan_patch(
            parse_patch("*** Begin Patch\n*** Update File: a.py\n@@\n-x = 2\n+x = 3\n*** End Patch"), read
        )


def test_patch_wrappers_and_repeated_end_markers_are_tolerated():
    core = "*** Begin Patch\n*** Delete File: a.py\n*** End Patch"
    for text in (
        core + "\n*** End Patch\n",
        "```\n" + core + "\n```",
        "apply_patch <<'EOF'\n" + core + "\nEOF",
        "<<EOF\n" + core + "\n*** End Patch\nEOF\n",
    ):
        actions = parse_patch(text)
        assert [(a.action, a.path) for a in actions] == [("delete", "a.py")]
    with pytest.raises(PatchError, match="Unexpected text after"):
        parse_patch(core + "\nDone!")
