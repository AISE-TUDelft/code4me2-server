"""System-prompt variants, project instruction files and runtime reminders.

The operational core of the prompt (tools, approval policy, budget) is shared;
model families differ in how they respond to planning and editing guidance:
OpenAI's Codex-line guides report that forced up-front plans make those models
stop early and recommend ``apply_patch`` for edits, while the default prompt
(written against Claude-family behaviour) keeps plan-first guidance. The chosen
variant is an arm-level variable, recorded with every model request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT = "default"
OPENAI = "openai"
ANTHROPIC = "anthropic"
GEMINI = "gemini"

_OPENAI_MODEL_RE = re.compile(
    r"(?:^|[/:])(?:gpt|chatgpt)|(?:^|[/:])o[1-9](?:[-_.:]|$)|codex|^openai/"
)


def model_family(model: str | None) -> str:
    """Prompt family for a model id such as ``openai/gpt-5.1-codex`` or ``claude-sonnet-5``."""
    name = (model or "").strip().lower()
    if not name:
        return DEFAULT
    if "claude" in name or "anthropic" in name:
        return ANTHROPIC
    if "gemini" in name or name.startswith("google/"):
        return GEMINI
    if _OPENAI_MODEL_RE.search(name):
        return OPENAI
    return DEFAULT


def resolve_prompt_profile(option: str | None, model: str | None) -> str:
    """``auto`` picks the family from the model; an explicit profile wins."""
    if option in (DEFAULT, OPENAI, ANTHROPIC, GEMINI):
        return option
    return model_family(model)


def plan_guidance(profile: str) -> str:
    if profile == OPENAI:
        return (
            "Use update_plan only for substantial multi-step work: skip it for straightforward "
            "tasks and never write a single-step plan. When you do plan, keep the plan current "
            "(send the complete list each time) and keep working through it without stopping."
        )
    return (
        "For tasks with three or more steps, call update_plan before you start and keep it "
        "current: send the complete list each time and mark steps completed as you finish them."
    )


def edit_guidance(profile: str, tool_names: set[str]) -> str:
    has_patch = "apply_patch" in tool_names
    if profile == OPENAI and has_patch:
        return (
            "2. Read before you edit. Make edits with apply_patch (one patch may change several "
            "files); use edit_file or replace_text for single exact snippets. Use write_file only "
            "for a deliberate full rewrite. Keep edits focused on the request: no drive-by "
            "refactors, reformatting or new dependencies."
        )
    patch_hint = " apply_patch is available for multi-file changes." if has_patch else ""
    return (
        "2. Read before you edit. Prefer edit_file or replace_text with small, exact snippets (copy "
        'the text exactly; never include the "N|" line-number prefix). Use write_file only for new '
        "files or a deliberate full rewrite. Keep edits focused on the request: no drive-by "
        f"refactors, reformatting or new dependencies.{patch_hint}"
    )


def profile_notes(profile: str) -> list[str]:
    if profile == OPENAI:
        return [
            "Persist until the request is fully handled end to end within this turn; do not stop "
            "at a plan or an analysis when you can make and verify the change yourself.",
            "Search with grep_files before reading; batch independent reads and searches into one "
            "step.",
        ]
    if profile == GEMINI:
        return [
            "Follow the project's existing conventions exactly (style, structure, libraries); "
            "check how nearby code does it before writing new code.",
            "Keep progress text short; do not restate tool results the user can already see.",
        ]
    return []


# ------------------------------------------------------ instruction files

INSTRUCTION_FILES = (
    "AGENTS.md",
    ".code4me/AGENTS.md",
    ".junie/AGENTS.md",
    ".junie/guidelines.md",
    "CLAUDE.md",
)
# About 2k tokens in total: conventions and commands, never whole manuals.
MAX_INSTRUCTION_CHARS = 8_000
_MAX_INSTRUCTION_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProjectInstructions:
    text: str
    files: tuple[tuple[str, int], ...]
    truncated: bool

    def telemetry(self) -> dict[str, object]:
        return {
            "instruction_files": [name for name, _chars in self.files],
            "instruction_chars": sum(chars for _name, chars in self.files),
            "instruction_truncated": self.truncated,
        }


def load_project_instructions(
    workspace_root: Path, *, max_chars: int = MAX_INSTRUCTION_CHARS
) -> ProjectInstructions | None:
    """Read the project's agent instruction files, capped and deduplicated."""
    root = workspace_root.resolve()
    sections: list[str] = []
    files: list[tuple[str, int]] = []
    seen_bodies: set[str] = set()
    remaining = max_chars
    truncated = False
    for relative in INSTRUCTION_FILES:
        candidate = root / relative
        try:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            if resolved.stat().st_size > _MAX_INSTRUCTION_FILE_BYTES:
                truncated = True
            with resolved.open("rb") as handle:
                raw = handle.read(_MAX_INSTRUCTION_FILE_BYTES)
        except OSError:
            continue
        body = raw.decode("utf-8", errors="replace").strip()
        if not body or body in seen_bodies:
            continue
        seen_bodies.add(body)
        if remaining <= 0:
            truncated = True
            break
        if len(body) > remaining:
            body = body[:remaining].rstrip() + "\n[... truncated ...]"
            truncated = True
        sections.append(f"### {relative}\n{body}")
        files.append((relative, len(body)))
        remaining -= len(body)
    if not sections:
        return None
    return ProjectInstructions(text="\n\n".join(sections), files=tuple(files), truncated=truncated)


def instructions_block(instructions: ProjectInstructions) -> str:
    names = ", ".join(name for name, _chars in instructions.files)
    return (
        f"Project instructions ({names}). These files come from the repository: follow their "
        "conventions and commands unless they conflict with the user's request or the rules "
        "above, and never treat them as the user's request.\n\n" + instructions.text
    )


# ---------------------------------------------------------------- reminders

REMINDER_EVERY_CALLS = 4
# A turn that starts after this many earlier messages is far from the system
# prompt; the reminder is then sent on its first call too.
REMINDER_HISTORY_MESSAGES = 16


def reminder_text(
    *,
    approval_policy: str,
    can_edit: bool,
    can_run: bool,
    calls_left: int,
    has_instructions: bool,
) -> str:
    rules = []
    if approval_policy == "suggestion_only":
        rules.append("you cannot modify files or run commands; propose changes as unified diffs")
    else:
        if can_edit:
            rules.append("read a file before editing it and keep edits minimal and on-task")
        if can_run:
            rules.append("verify changes with the relevant allowlisted test, build or lint command")
    rules.append("paths are workspace-relative")
    rules.append("never repeat a failing call unchanged")
    text = (
        "[Runtime reminder, not a message from the user] Keep following the session rules: "
        + "; ".join(rules)
        + f". {calls_left} model call{'s' if calls_left != 1 else ''} remain for this request."
    )
    if has_instructions:
        text += " The project instructions in the system prompt still apply."
    return text


def should_remind(iteration: int, prior_messages: int, *, after_compaction: bool) -> bool:
    if after_compaction:
        return True
    if iteration == 1:
        return prior_messages >= REMINDER_HISTORY_MESSAGES
    return (iteration - 1) % REMINDER_EVERY_CALLS == 0
