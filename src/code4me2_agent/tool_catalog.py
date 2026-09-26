"""Model-facing tool catalogue: schemas, ACP kinds and approval classes.

This is the runtime half of the contract whose server half is
``src/agents/tools.py`` (``CODE4ME2_AGENT_TOOLS``). A profile freezes tool
*names*; the runtime advertises the schema for each allowed name on every turn.
Keep names stable: existing study profiles pin them.
"""

from __future__ import annotations

from typing import Any

# ACP ToolKind per tool (acp.schema.ToolKind). ``update_plan`` has no card at
# all: the ACP ``plan`` update renders natively and a card would duplicate it.
TOOL_KINDS: dict[str, str] = {
    "read_file": "read",
    "create_file": "edit",
    "write_file": "edit",
    "replace_text": "edit",
    "edit_file": "edit",
    "apply_patch": "edit",
    "delete_file": "delete",
    "move_file": "move",
    "list_files": "search",
    "glob_files": "search",
    "grep_files": "search",
    "search_files": "search",
    "run_command": "execute",
}

# Tools that change the workspace or run code; these need approval under
# ``per_step`` and are hard-denied under ``suggestion_only``.
MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "create_file",
        "write_file",
        "replace_text",
        "edit_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "run_command",
    }
)

# Approval scope class used for "allow for session" decisions.
APPROVAL_KINDS: dict[str, str] = {
    "run_command": "execute",
    "create_file": "edit",
    "write_file": "edit",
    "replace_text": "edit",
    "edit_file": "edit",
    "apply_patch": "edit",
    "delete_file": "edit",
    "move_file": "edit",
}

# ``update_plan`` renders as the ACP plan and ``ask_user`` as the agent's final
# message, so neither gets a tool card.
NO_CARD_TOOLS: frozenset[str] = frozenset({"update_plan", "ask_user"})

DEFAULT_IGNORED_DIRS_TEXT = (
    ".git, node_modules, build, dist, target, out, .idea, .gradle, .venv, __pycache__"
)


def tool_kind(name: str) -> str:
    if name.startswith("mcp__"):
        return "other"
    return TOOL_KINDS.get(name, "other")


def approval_kind(name: str) -> str:
    if name.startswith("mcp__"):
        return "other"
    return APPROVAL_KINDS.get(name, "other")


def requires_manual_approval(name: str) -> bool:
    return name in MUTATING_TOOLS or name.startswith("mcp__")


def is_read_only(name: str) -> bool:
    return not requires_manual_approval(name)


_PATH_DESCRIPTION = (
    "Workspace-relative path (preferred, e.g. src/app.py) or an absolute path inside the workspace."
)


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _path_property(description: str = _PATH_DESCRIPTION) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _force_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "Skip the read-before-edit check for a file you have not read in this session. "
            "Only when you are certain of its current content. Default false."
        ),
    }


APPLY_PATCH_DESCRIPTION = (
    "Edit, create, delete or rename files with one patch; all changes apply together or none do. "
    "Format:\n"
    "*** Begin Patch\n"
    "*** Update File: src/app.py\n"
    "@@ def handler(event):\n"
    "-    return None\n"
    "+    return event\n"
    "*** Add File: src/new.py\n"
    '+print("hello")\n'
    "*** Delete File: src/old.py\n"
    "*** End Patch\n"
    "Rules: paths are workspace-relative. In an update, '@@' starts a chunk and may name a line "
    "to find first (a def/class/function signature) so the chunk is placed after it; then lines "
    "starting with ' ' are unchanged context (include about 3 lines before and after each "
    "change), '-' lines are removed and '+' lines are added. Context must match the current file "
    "(read it first). '*** Move to: new/path' right after '*** Update File:' renames the file; "
    "'*** End of File' after a chunk pins it to the end of the file. Every line of an added file "
    "starts with '+'."
)


def tool_definitions() -> list[dict[str, Any]]:
    return [
        _function_tool(
            "read_file",
            (
                "Read a UTF-8 text file in the workspace. Each output line is prefixed with its "
                "1-based line number and a pipe, e.g. `12|code`; the prefix is display-only and "
                "must never be copied into old_text for replace_text or edit_file. Long files are "
                "paged: the result reports total_lines, truncated and next_offset; call again with "
                "offset=next_offset to continue. Lines longer than 2000 characters are cut with a "
                "marker. Binary files are rejected. Use glob_files or grep_files to locate files first."
            ),
            {
                "path": _path_property(),
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based line number to start from. Default 1.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Maximum number of lines to return. Default 1000.",
                },
            },
            ["path"],
        ),
        _function_tool(
            "create_file",
            (
                "Create a new UTF-8 text file with the given content. Fails if the file already "
                "exists: use write_file to overwrite it, or replace_text/edit_file to change part "
                "of it. Parent directories are created."
            ),
            {
                "path": _path_property(),
                "content": {"type": "string", "description": "Complete file content."},
            },
            ["path", "content"],
        ),
        _function_tool(
            "write_file",
            (
                "Overwrite a UTF-8 text file with the complete new content, creating it and its "
                "parent directories if needed. Prefer replace_text or edit_file for partial "
                "changes to an existing file so unrelated lines are never lost."
            ),
            {
                "path": _path_property(),
                "content": {"type": "string", "description": "Complete new file content."},
                "force": _force_property(),
            },
            ["path", "content"],
        ),
        _function_tool(
            "replace_text",
            (
                "Replace one occurrence of old_text with new_text in a file you have read. Copy "
                "old_text exactly from read_file, without the line-number prefix. It must match "
                "exactly one place: if it matches several the call fails and reports their line "
                "numbers (add surrounding lines, or set replace_all=true to change every exact "
                "occurrence). Differences only in trailing whitespace, indentation or line endings "
                "are tolerated and reported; new_text is re-indented to fit."
            ),
            {
                "path": _path_property(),
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find (copied from read_file, without the N| prefix).",
                },
                "new_text": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match. Default false.",
                },
                "force": _force_property(),
            },
            ["path", "old_text", "new_text"],
        ),
        _function_tool(
            "edit_file",
            (
                "Apply several exact text replacements to one file as a single atomic operation. "
                "Edits are applied in order, so later edits see the result of earlier ones; each "
                "old_text must match exactly once unless its replace_all is true. If any edit "
                "fails, nothing is written and the error names the failing edit. Prefer this over "
                "several replace_text calls on the same file."
            ),
            {
                "path": _path_property(),
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "Replacements to apply in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "description": "Exact text to find."},
                            "new_text": {"type": "string", "description": "Replacement text."},
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace every occurrence of this old_text. Default false.",
                            },
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
                "force": _force_property(),
            },
            ["path", "edits"],
        ),
        _function_tool(
            "apply_patch",
            APPLY_PATCH_DESCRIPTION,
            {
                "patch": {
                    "type": "string",
                    "description": "The complete patch, from '*** Begin Patch' to '*** End Patch'.",
                },
                "force": _force_property(),
            },
            ["patch"],
        ),
        _function_tool(
            "delete_file",
            (
                "Delete a file or an empty directory inside the workspace. Non-empty directories "
                "are refused. This cannot be undone."
            ),
            {"path": _path_property()},
            ["path"],
        ),
        _function_tool(
            "move_file",
            (
                "Move or rename a file or directory inside the workspace. Destination parent "
                "directories are created. Fails if the destination already exists unless "
                "overwrite=true (files only). Use this instead of read + create + delete."
            ),
            {
                "source_path": _path_property("Current path of the file or directory."),
                "destination_path": _path_property("New path."),
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing destination file. Default false.",
                },
            },
            ["source_path", "destination_path"],
        ),
        _function_tool(
            "list_files",
            (
                "List the entries of a directory. Non-recursive by default; directory names end "
                "with '/'. Set recursive=true (optionally with max_depth) to walk the tree. Common "
                f"build, VCS and dependency directories ({DEFAULT_IGNORED_DIRS_TEXT}, ...) are "
                "skipped unless include_ignored=true. Results are sorted and capped. Use "
                "glob_files to find files by name pattern."
            ),
            {
                "path": _path_property("Directory to list. Default '.' (the workspace root)."),
                "recursive": {
                    "type": "boolean",
                    "description": "Walk subdirectories. Default false.",
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "With recursive=true, the maximum directory depth. Default unlimited.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "Maximum entries to return. Default 500.",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also list build/VCS/dependency directories. Default false.",
                },
            },
            [],
        ),
        _function_tool(
            "glob_files",
            (
                "Find files whose path relative to `path` matches a glob pattern, e.g. '**/*.kt', "
                "'src/main/**/Service*.java' or '*.md' (which matches only directly under path). "
                "'**' spans directories. Results are workspace-relative, sorted by path and capped; "
                f"common build, VCS and dependency directories ({DEFAULT_IGNORED_DIRS_TEXT}, ...) "
                "are skipped unless include_ignored=true."
            ),
            {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to path; '**' matches across directories.",
                },
                "path": _path_property("Directory to search in. Default '.' (the workspace root)."),
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "description": "Maximum files to return. Default 500.",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search build/VCS/dependency directories. Default false.",
                },
            },
            ["pattern"],
        ),
        _function_tool(
            "grep_files",
            (
                "Search file contents with a Python regular expression (escape ( ) . [ ] + ? * for "
                "literal text). output_mode 'content' (default) returns matching lines with path "
                "and line number plus optional context lines; 'files_with_matches' returns only "
                "the paths; 'count' returns per-file match counts. Binary files, files over 2 MiB "
                f"and common build, VCS and dependency directories ({DEFAULT_IGNORED_DIRS_TEXT}, "
                "...) are skipped. Results are capped (truncated=true when the cap is hit): narrow "
                "with path or glob, or use count mode."
            ),
            {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression to search for.",
                },
                "path": _path_property("File or directory to search. Default '.' (the workspace root)."),
                "glob": {
                    "type": "string",
                    "description": "Only search files whose path relative to `path` matches this glob, e.g. '**/*.java'.",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Ignore case. Default false.",
                },
                "context_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10,
                    "description": "Lines of context before and after each match (content mode). Default 0.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum matches (content mode) or files to return. Default 200.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "What to return. Default 'content'.",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "Also search build/VCS/dependency directories. Default false.",
                },
            },
            ["pattern"],
        ),
        _function_tool(
            "search_files",
            (
                "Legacy literal, case-sensitive text search; prefer grep_files. Equivalent to "
                "grep_files with the query escaped as a literal pattern."
            ),
            {
                "query": {"type": "string", "description": "Literal text to find."},
                "path": _path_property("File or directory to search. Default '.' (the workspace root)."),
            },
            ["query"],
        ),
        _function_tool(
            "run_command",
            (
                "Run one allowlisted program inside the workspace without a shell and return "
                "exit_code, stdout, stderr, duration_ms and timed_out. argv[0] must be a bare "
                "command name from the allowlist (not a path). There is no shell: pipes, globs, "
                "'&&', 'cd' and redirection are not available; pass arguments as separate argv "
                "items. stdin is closed. Output is captured with head and tail truncation "
                "(output_truncated=true). Set timeout_seconds for long builds or test runs (max 600)."
            ),
            {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Program name followed by its arguments, e.g. [\"pytest\", \"-q\", \"tests\"].",
                },
                "cwd": _path_property("Working directory. Default '.' (the workspace root)."),
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "Kill the command after this many seconds. Default 120.",
                },
            },
            ["argv"],
        ),
        _function_tool(
            "ask_user",
            (
                "Ask the user a question and end your turn; their answer arrives as their next "
                "message. Use it only when a decision is genuinely ambiguous and would change the "
                "outcome, or you need information only the user has. Never use it for routine "
                "confirmation. Offer short answer options when the choice is between known "
                "alternatives."
            ),
            {
                "question": {"type": "string", "description": "One clear, specific question."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 6,
                    "description": "Optional short answer choices, most likely first.",
                },
            },
            ["question"],
        ),
        _function_tool(
            "update_plan",
            (
                "Publish or update your task plan so the user can follow progress. Send the "
                "complete list every time (it replaces the previous plan); keep at most one entry "
                "in_progress and mark finished steps completed. Use it for multi-step tasks before "
                "starting and whenever a step changes state. It has no effect on files."
            ),
            {
                "entries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 30,
                    "description": "The full plan, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Short description of the step.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "Default medium.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            ["entries"],
        ),
    ]


def tool_names() -> list[str]:
    return [definition["function"]["name"] for definition in tool_definitions()]
