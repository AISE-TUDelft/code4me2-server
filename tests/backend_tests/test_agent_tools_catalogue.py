"""Server-side catalogue accepts the upgraded runtime tool set."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from agents.tools import CODE4ME2_AGENT_TOOLS, KNOWN_AGENT_TOOLS, tools_for_framework
from backend.routers.acp import (
    MANAGED_PROTOCOL_VERSION,
    _enforce_inference_tool_policy,
    _valid_managed_policy_snapshot,
)

RUNTIME_TOOLS = [
    "read_file",
    "list_files",
    "glob_files",
    "grep_files",
    "search_files",
    "create_file",
    "write_file",
    "replace_text",
    "edit_file",
    "delete_file",
    "move_file",
    "run_command",
    "update_plan",
]


def _policy(tools: list[str]) -> dict:
    return {
        "version": MANAGED_PROTOCOL_VERSION,
        "model": "gpt-test",
        "approval_policy": "per_step",
        "max_iterations": 8,
        "max_context_tokens": 32000,
        "temperature": None,
        "tools": tools,
    }


def _definition(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_catalogue_contains_every_runtime_tool_and_the_mcp_wildcard():
    assert set(RUNTIME_TOOLS) <= CODE4ME2_AGENT_TOOLS
    assert "mcp__*" in CODE4ME2_AGENT_TOOLS
    assert tools_for_framework("code4me2-agent") == CODE4ME2_AGENT_TOOLS
    assert CODE4ME2_AGENT_TOOLS <= KNOWN_AGENT_TOOLS


def test_managed_policy_snapshot_accepts_the_full_runtime_tool_set():
    assert _valid_managed_policy_snapshot(_policy(RUNTIME_TOOLS)) is True
    assert _valid_managed_policy_snapshot(_policy(RUNTIME_TOOLS + ["shell"])) is False


def test_inference_policy_accepts_new_tools_and_still_rejects_unlisted_names():
    request = {
        "tools": [_definition("grep_files"), _definition("update_plan"), _definition("edit_file")],
        "tool_choice": "none",
    }
    _enforce_inference_tool_policy(request, policy_tools=RUNTIME_TOOLS)
    assert [tool["function"]["name"] for tool in request["tools"]] == ["grep_files", "update_plan", "edit_file"]
    assert request["tool_choice"] == "none"

    with pytest.raises(HTTPException) as denied:
        _enforce_inference_tool_policy({"tools": [_definition("shell")]}, policy_tools=RUNTIME_TOOLS)
    assert denied.value.status_code == 403

    legacy_only = {"tools": [_definition("grep_files")]}
    with pytest.raises(HTTPException) as frozen:
        _enforce_inference_tool_policy(legacy_only, policy_tools=["read_file", "search_files"])
    assert frozen.value.status_code == 403
