"""Runtime ↔ server tool catalogue contract.

The managed relay rejects any model request that advertises a tool name outside
``CODE4ME2_AGENT_TOOLS`` (``backend/routers/acp/__init__.py``), so every tool the
runtime can advertise must exist in the server catalogue, and every catalogue
name (apart from the MCP wildcard) must be a real runtime tool.
"""

from __future__ import annotations

from agents.tools import CODE4ME2_AGENT_TOOLS, tools_for_framework
from code4me2_agent.tool_catalog import (
    MUTATING_TOOLS,
    TOOL_KINDS,
    tool_definitions,
    tool_names,
)


def test_runtime_tool_definitions_are_a_subset_of_the_server_catalogue():
    runtime_names = set(tool_names())
    assert runtime_names <= CODE4ME2_AGENT_TOOLS
    assert CODE4ME2_AGENT_TOOLS - runtime_names == {"mcp__*"}


def test_catalogue_is_served_for_the_runtime_framework():
    assert tools_for_framework("code4me2-agent") == CODE4ME2_AGENT_TOOLS


def test_every_tool_has_a_kind_and_a_described_schema():
    for definition in tool_definitions():
        function = definition["function"]
        name = function["name"]
        assert name == "update_plan" or name in TOOL_KINDS
        assert function["description"].strip()
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        for property_name, schema in parameters["properties"].items():
            assert schema.get("description") or schema.get("enum"), (name, property_name)
        for required in parameters["required"]:
            assert required in parameters["properties"], (name, required)
    assert MUTATING_TOOLS <= set(tool_names())
