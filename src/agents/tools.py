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

The module also owns the vocabulary and validation of the built-in runtime's
command and harness profile fields (decision D-01: ``commands_denylist``,
``command_timeout_seconds``, ``harness_options``), shared by the profile API,
the profile↔release contract, study freezing and the managed run policy.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

# Built-in code4me2-agent tools. Each is enforced inside the runtime itself
# (paths confined to the workspace, commands checked against the profile's
# blocked commands).
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
        "apply_patch",  # multi-file patch, applied atomically
        "delete_file",
        "move_file",
        # Interaction: ends the turn with a structured question to the user.
        "ask_user",
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
        # Goose 1.51's developer extension also offers this (observed in the
        # study gateway shape, 2026-09-27).
        "read_image",
        # Builds that expose the developer extension as a namespaced MCP
        # server name its tools like this.
        "developer__shell",
        "developer__text_editor",
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


# ── Built-in runtime command and harness settings (decision D-01) ────────────
#
# Optional ``code4me2-agent`` profile fields; a BYOA (goose/codex) release
# refuses all three. ``None`` always means "not set": no command is blocked (a
# user's config row may still block some), and the runtime's default command
# timeout and harness defaults apply. Every set value is frozen with the
# profile into study snapshots and travels in ``GET /api/acp/agent-config`` and
# the managed run policy.
#
# ``commands_denylist`` replaced ``commands_allowlist``: the agent may run any
# program except the listed ones (the participant still approves each command
# under per-step approval). Study snapshots frozen before the change can still
# carry ``commands_allowlist``; it only reaches runtime releases 0.0.6 and
# earlier, which predate the denylist (see ``backend/routers/acp``).

#: Profile fields, in the canonical order they join digests and snapshots.
HARNESS_PROFILE_FIELDS: tuple[str, ...] = (
    "commands_denylist",
    "command_timeout_seconds",
    "harness_options",
)

#: A bare command name, as a denylist entry or a verify command's program: no
#: directory separator, whitespace or shell metacharacter (listing ``gradlew``
#: also covers ``./gradlew``). Matched with ``fullmatch``, so a trailing newline
#: never slips through.
COMMAND_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
COMMANDS_DENYLIST_MAX_ENTRIES = 64
COMMANDS_ALLOWLIST_MAX_ENTRIES = 64
COMMAND_TIMEOUT_SECONDS_MIN = 1
COMMAND_TIMEOUT_SECONDS_MAX = 600
VERIFY_COMMAND_MAX_ARGS = 32
VERIFY_COMMAND_MAX_ARG_LENGTH = 512

#: ``harness_options.prompt_profile`` values (``auto`` derives the family from
#: the model name at runtime).
HARNESS_PROMPT_PROFILES: tuple[str, ...] = (
    "auto",
    "default",
    "openai",
    "anthropic",
    "gemini",
)
#: Boolean ``harness_options`` switches; the runtime default of each is true.
HARNESS_BOOLEAN_OPTIONS: tuple[str, ...] = (
    "self_review",
    "verify_on_stop",
    "context_summarization",
    "parallel_tools",
    "project_instructions",
    "read_before_edit",
    "syntax_check",
    "loop_guard",
    "instruction_reminders",
    "test_output_summary",
    "approval_revise",
)
#: Every key ``harness_options`` may carry; any other key is refused.
HARNESS_OPTION_KEYS: tuple[str, ...] = HARNESS_BOOLEAN_OPTIONS + (
    "verify_command",
    "prompt_profile",
)

_BARE_COMMAND_RULE = (
    "a bare command name (letters, digits, '.', '_', '+' or '-', at most 64 "
    "characters, starting with a letter or digit; no path, spaces or shell "
    "metacharacters)"
)


def _shown(value: Any) -> str:
    """A short ``repr`` of a rejected value for an error message."""
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def is_bare_command_name(value: Any) -> bool:
    """Whether ``value`` is a bare command name (a program, not a path)."""
    return isinstance(value, str) and COMMAND_NAME_PATTERN.fullmatch(value) is not None


# Blocked-command matching. Mirrors ``blocked_program`` in
# ``code4me2_agent/command_tools.py``, which enforces it in the runtime; keep
# the two in step.

#: Launcher suffixes ignored when names are compared (``git.exe`` is ``git``).
_LAUNCHER_SUFFIXES = (".exe", ".cmd", ".bat", ".com")
#: Shells, launchers and package managers: every word of their arguments is
#: checked too, so ``bash -c "git push"``, ``env git push`` or ``npm x git`` cannot
#: start a blocked ``git``. A package manager's subcommands and aliases that run
#: a program are many (``uv run``, ``npm exec``/``x``/``explore``, ``mise x``), so
#: all its arguments count: a package named like a blocked program is refused too.
_COMMAND_RUNNERS = frozenset(
    {
        "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox",
        "cmd", "powershell", "pwsh", "wsl",
        "env", "xargs", "sudo", "doas", "nohup", "nice", "ionice", "setsid",
        "stdbuf", "timeout", "gtimeout", "time", "watch", "command", "exec",
        "script", "arch", "caffeinate", "chroot", "su", "nix-shell", "npx", "uvx", "bunx",
        "uv", "poetry", "pipenv", "pdm", "hatch", "rye", "pixi", "conda", "mamba",
        "micromamba", "pipx", "npm", "pnpm", "yarn", "bun", "bundle", "corepack",
        "mise", "asdf", "direnv", "pyenv", "rbenv", "nodenv", "nvm", "volta",
    }
)
#: Quotes only group a word (``g''it`` is ``git``); the rest separate words,
#: including ``@`` before a version (``npx npm@10``, ``corepack pnpm@9``).
_QUOTES = re.compile(r"[\'\"]")
_WORD_SEPARATORS = re.compile(r"[\s;&|()<>`{}$=,@]+")


def command_key(name: str) -> str:
    """The comparable program name: last path component, lower-cased, no ``.exe``.

    Trailing dots and spaces are dropped, as Windows drops them (``git.exe.``
    starts ``git.exe``).
    """
    base = re.split(r"[\\/]", name.strip().rstrip("\\/"))[-1].lower().rstrip(" .")
    for suffix in _LAUNCHER_SUFFIXES:
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)].rstrip(" .")
    return base


def blocked_command_in(argv: Sequence[str], denylist: Sequence[str]) -> Optional[str]:
    """The blocked program ``argv`` would start, else ``None``.

    ``argv[0]`` is compared by name (case-insensitive, any directory and
    launcher suffix ignored). When it is a shell, a launcher or a package
    manager, every plainly written word of its arguments is compared as well
    (``npm@10`` counts as ``npm``). Programs started by scripts, interpreters
    or build tools, and names disguised with escapes, are not visible here.
    """
    blocked = {command_key(name) for name in denylist}
    blocked.discard("")
    if not argv or not blocked:
        return None
    program = command_key(argv[0])
    if program in blocked:
        return program
    if program not in _COMMAND_RUNNERS:
        return None
    for argument in argv[1:]:
        for word in _WORD_SEPARATORS.split(_QUOTES.sub("", argument)):
            if word and command_key(word) in blocked:
                return command_key(word)
    return None


def validate_commands_denylist(value: Any) -> list[str]:
    """Return a valid ``commands_denylist`` as a new list, else ``ValueError``.

    At most 64 bare command names, unique ignoring case; the order is kept (it
    is the order the runtime and the editor show). An empty list blocks nothing.
    """
    if not isinstance(value, list):
        raise ValueError("commands_denylist must be a list of command names")
    if len(value) > COMMANDS_DENYLIST_MAX_ENTRIES:
        raise ValueError(
            f"commands_denylist may list at most {COMMANDS_DENYLIST_MAX_ENTRIES} commands"
        )
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not is_bare_command_name(item):
            raise ValueError(
                f"commands_denylist[{index}] ({_shown(item)}) must be {_BARE_COMMAND_RULE}"
            )
        if item.lower() in seen:
            raise ValueError(f"commands_denylist lists {item!r} more than once")
        seen.add(item.lower())
    return list(value)


def validate_commands_allowlist(value: Any) -> list[str]:
    """Return a valid legacy ``commands_allowlist`` as a new list, else ``ValueError``.

    Only study snapshots frozen before ``commands_denylist`` carry one; it is
    checked so a malformed frozen value still fails closed. At most 64 unique
    bare command names; an empty list is valid (explicitly no commands).
    """
    if not isinstance(value, list):
        raise ValueError("commands_allowlist must be a list of command names")
    if len(value) > COMMANDS_ALLOWLIST_MAX_ENTRIES:
        raise ValueError(
            f"commands_allowlist may list at most {COMMANDS_ALLOWLIST_MAX_ENTRIES} commands"
        )
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not is_bare_command_name(item):
            raise ValueError(
                f"commands_allowlist[{index}] ({_shown(item)}) must be {_BARE_COMMAND_RULE}"
            )
        if item in seen:
            raise ValueError(f"commands_allowlist lists {item!r} more than once")
        seen.add(item)
    return list(value)


def validate_command_timeout_seconds(value: Any) -> int:
    """Return a valid ``command_timeout_seconds`` (an int in 1..600), else ``ValueError``."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not COMMAND_TIMEOUT_SECONDS_MIN <= value <= COMMAND_TIMEOUT_SECONDS_MAX
    ):
        raise ValueError(
            "command_timeout_seconds must be a whole number of seconds from "
            f"{COMMAND_TIMEOUT_SECONDS_MIN} to {COMMAND_TIMEOUT_SECONDS_MAX}"
        )
    return value


def validate_verify_command(value: Any) -> list[str]:
    """Return a valid ``harness_options.verify_command`` argv, else ``ValueError``."""
    if not isinstance(value, list) or not 1 <= len(value) <= VERIFY_COMMAND_MAX_ARGS:
        raise ValueError(
            "harness_options.verify_command must be null or a list of 1 to "
            f"{VERIFY_COMMAND_MAX_ARGS} arguments"
        )
    for index, argument in enumerate(value):
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument) > VERIFY_COMMAND_MAX_ARG_LENGTH
            or "\x00" in argument
        ):
            raise ValueError(
                f"harness_options.verify_command[{index}] must be a non-empty string "
                f"of at most {VERIFY_COMMAND_MAX_ARG_LENGTH} characters"
            )
    if not is_bare_command_name(value[0]):
        raise ValueError(
            f"harness_options.verify_command[0] ({_shown(value[0])}) must be "
            f"{_BARE_COMMAND_RULE}"
        )
    return list(value)


def validate_harness_options(
    value: Any,
    *,
    commands_denylist: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Return a valid ``harness_options`` object as a new dict, else ``ValueError``.

    Only the keys in :data:`HARNESS_OPTION_KEYS` are accepted: booleans for the
    switches, one of :data:`HARNESS_PROMPT_PROFILES` for ``prompt_profile`` and
    ``null`` or an argv for ``verify_command``. Given the profile's
    ``commands_denylist`` (the profile contract), the verify command must not
    start a blocked program. A run policy snapshot is checked without it: a
    user's config row may block more, and the runtime then skips the command.
    """
    if not isinstance(value, Mapping):
        raise ValueError("harness_options must be an object")
    unknown = sorted(str(key) for key in value if key not in HARNESS_OPTION_KEYS)
    if unknown:
        raise ValueError(
            "harness_options has unknown keys: "
            + ", ".join(unknown)
            + "; allowed keys are "
            + ", ".join(HARNESS_OPTION_KEYS)
        )
    for key in HARNESS_BOOLEAN_OPTIONS:
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"harness_options.{key} must be true or false")
    if "prompt_profile" in value and (
        not isinstance(value["prompt_profile"], str)
        or value["prompt_profile"] not in HARNESS_PROMPT_PROFILES
    ):
        raise ValueError(
            "harness_options.prompt_profile must be one of "
            + ", ".join(HARNESS_PROMPT_PROFILES)
        )
    verify_command = value.get("verify_command")
    if verify_command is not None:
        validate_verify_command(verify_command)
        blocked = blocked_command_in(verify_command, commands_denylist or ())
        if blocked is not None:
            raise ValueError(
                f"harness_options.verify_command runs {blocked!r}, which is in the "
                "profile's commands_denylist"
            )
    return dict(value)


def set_harness_profile_fields(profile: Any) -> dict[str, Any]:
    """The D-01 fields ``profile`` sets, in :data:`HARNESS_PROFILE_FIELDS` order.

    Unset (``None``) fields are omitted, so a digest or snapshot built from a
    profile without them keeps exactly the shape it had before they existed.
    Works for ORM rows, frozen configs and stand-ins alike.
    """
    values: dict[str, Any] = {}
    for field in HARNESS_PROFILE_FIELDS:
        value = getattr(profile, field, None)
        if value is not None:
            values[field] = value
    return values
