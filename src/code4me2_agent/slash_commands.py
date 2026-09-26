"""Slash commands the runtime answers itself (ACP ``available_commands_update``).

Clients that render ACP commands send them as ordinary prompt text starting
with ``/``; typing the same text works in any client, so the commands are
parsed from the prompt rather than from a dedicated request.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    hint: str | None = None


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("status", "Show this session's status"),
    SlashCommand(
        "undo",
        "Revert the file changes of the most recent turn that changed files",
    ),
    SlashCommand(
        "compact",
        "Summarise the earlier conversation now to free context space",
    ),
    SlashCommand(
        "review",
        "Review every change made in this session for bugs and missing verification",
        hint="optional focus, e.g. error handling",
    ),
)
_BY_NAME = {command.name: command for command in COMMANDS}
# Study (managed) sessions offer only commands that neither reveal nor change
# the assigned arm; there "/review" or "/compact" is an ordinary request.
MANAGED_COMMAND_NAMES = frozenset({"status", "undo"})


def available(*, managed: bool) -> tuple[SlashCommand, ...]:
    if not managed:
        return COMMANDS
    return tuple(command for command in COMMANDS if command.name in MANAGED_COMMAND_NAMES)


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str


def parse(prompt: str, *, managed: bool = False) -> ParsedCommand | None:
    """The command in ``prompt``, or None when it is an ordinary message."""
    text = (prompt or "").strip()
    if not text.startswith("/") or "\n" in text:
        return None
    name, _space, argument = text[1:].partition(" ")
    name = name.strip().lower()
    if name not in {command.name for command in available(managed=managed)}:
        return None
    return ParsedCommand(name=name, argument=argument.strip())
