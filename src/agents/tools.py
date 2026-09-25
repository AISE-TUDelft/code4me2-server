"""Catalogue of tool names an agent profile can select.

This backs ``GET /api/agent/available-tools``, which the admin UI uses to
populate the tool picker when creating a profile, and it is the fallback
allowlist the proxy filters against for tasks whose profile predates per-profile
tool selection.

Two runtimes contribute names:

* ``code4me2-agent`` — the built-in ReAct loop, whose tools are implemented in
  ``code4me2_agent/file_tools.py`` and ``command_tools.py``. This list is
  authoritative, because we own that code.
* ``goose`` — Block's Goose advertises its own extension tools over ACP. The
  names below are the ones observed in practice; the set genuinely varies with
  the installed Goose build and its enabled extensions, so it is a *best-known*
  catalogue rather than a contract. Anything Goose sends that isn't listed
  shows up in the ``tools_stripped`` count on the model_call event, which is how
  you discover a name that needs adding.

Codex contributes nothing here: it manages its own tool schemas over the
Responses API, and the proxy passes them through rather than filtering them
(see ``agents.normalize.normalize_responses_api_body``).
"""

from __future__ import annotations

# Built-in code4me2-agent tools. Each is allowlist-gated inside the runtime
# itself (paths confined to the workspace, commands checked against the
# profile's command allowlist).
CODE4ME2_AGENT_TOOLS: frozenset[str] = frozenset(
    {
        # Reading and discovery
        "read_file",
        "list_files",
        "glob_files",
        "grep_files",
        "search_files",  # legacy literal search; kept for frozen profiles
        # Editing
        "create_file",
        "write_file",
        "replace_text",
        "edit_file",
        "delete_file",
        "move_file",
        # Execution and planning
        "run_command",
        "update_plan",
        # Explicit opt-in for any tool exposed by client-supplied ACP MCP servers.
        # Concrete names remain filtered and approval-gated by the managed runtime.
        "mcp__*",
    }
)

# Goose tool names observed over ACP. Grouped by the extension that provides
# them, since enabling/disabling a Goose extension moves a whole group.
GOOSE_TOOLS: frozenset[str] = frozenset(
    {
        # developer extension
        "shell",
        "read",
        "write",
        "tree",
        "edit",
        "analyze",
        # extension manager
        "extensionmanager__list_resources",
        "extensionmanager__manage_extensions",
        "extensionmanager__read_resource",
        "extensionmanager__search_available_extensions",
        # apps extension
        "apps__create_app",
        "apps__delete_app",
        "apps__iterate_app",
        "apps__list_apps",
        # misc built-ins
        "delegate",
        "load",
        "load_skill",
        "todo__todo_write",
    }
)

# Union of everything a profile may legitimately name.
KNOWN_AGENT_TOOLS: frozenset[str] = CODE4ME2_AGENT_TOOLS | GOOSE_TOOLS

# Per-runtime view, for a UI that wants to show only the relevant tools once a
# framework_version has been picked.
TOOLS_BY_FRAMEWORK: dict[str, frozenset[str]] = {
    "code4me2-agent": CODE4ME2_AGENT_TOOLS,
    "goose": GOOSE_TOOLS,
    # Codex tools are self-managed and passed through untouched.
    "codex": frozenset(),
}


def tools_for_framework(framework_version: str | None) -> frozenset[str]:
    """Tools selectable for a runtime; the full catalogue when unrecognised."""
    if not framework_version:
        return KNOWN_AGENT_TOOLS
    return TOOLS_BY_FRAMEWORK.get(framework_version.strip().lower(), KNOWN_AGENT_TOOLS)
