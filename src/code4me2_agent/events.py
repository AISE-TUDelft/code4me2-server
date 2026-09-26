from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ApprovalDecision:
    decision: str
    scope: str = "none"

    @property
    def accepted(self) -> bool:
        return self.decision == "accepted"


@dataclass(frozen=True)
class ToolCallEvent:
    phase: str
    tool_call_id: str
    tool_name: str
    run_id: str
    request_id: str
    title: str
    kind: str
    status: str
    path: str | None = None
    content_text: str | None = None
    diff_old_text: str | None = None
    diff_new_text: str | None = None
    raw_input: dict[str, Any] | None = None
    raw_output: dict[str, Any] | None = None
    # Absolute paths for the ACP card. When omitted the sink falls back to ``path``.
    locations: tuple[str, ...] | None = None
    # Further file diffs of a multi-file change: (absolute path, old text, new text).
    extra_diffs: tuple[tuple[str, str | None, str], ...] = ()


@dataclass(frozen=True)
class ThoughtEvent:
    run_id: str
    request_id: str
    text: str
    phase: str | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class AssistantTextEvent:
    """Assistant text produced by one model call, shown to the user as it arrives."""

    run_id: str
    request_id: str
    message_id: str
    text: str
    final: bool
    iteration: int


@dataclass(frozen=True)
class PlanEntrySpec:
    content: str
    status: str = "pending"
    priority: str = "medium"


@dataclass(frozen=True)
class PlanEvent:
    run_id: str
    request_id: str
    tool_call_id: str
    entries: tuple[PlanEntrySpec, ...]


@dataclass(frozen=True)
class UsageEvent:
    run_id: str
    request_id: str
    iteration: int
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    turn_total_tokens: int
    context_budget_tokens: int


class AgentEventSink(Protocol):
    def tool_call(self, event: ToolCallEvent) -> None:
        ...

    def thought(self, event: ThoughtEvent) -> None:
        ...

    def assistant_text(self, event: AssistantTextEvent) -> None:
        ...

    def plan(self, event: PlanEvent) -> None:
        ...

    def usage(self, event: UsageEvent) -> None:
        ...


class NoopAgentEventSink:
    def tool_call(self, event: ToolCallEvent) -> None:
        return

    def thought(self, event: ThoughtEvent) -> None:
        return

    def assistant_text(self, event: AssistantTextEvent) -> None:
        return

    def plan(self, event: PlanEvent) -> None:
        return

    def usage(self, event: UsageEvent) -> None:
        return


def emit_event(sink: object, method_name: str, event: object) -> None:
    """Deliver ``event`` to ``sink.<method_name>`` when the sink implements it.

    Test doubles and older sinks implement only ``tool_call``/``request_approval``;
    newer event kinds are therefore optional on the sink side.
    """
    handler = getattr(sink, method_name, None)
    if callable(handler):
        handler(event)
