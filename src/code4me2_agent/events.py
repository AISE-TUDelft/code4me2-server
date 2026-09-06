from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
    raw_input: dict[str, Any] | None = None
    raw_output: dict[str, Any] | None = None


@dataclass(frozen=True)
class ThoughtEvent:
    run_id: str
    request_id: str
    text: str
    phase: str | None = None
    duration_ms: float | None = None


class AgentEventSink(Protocol):
    def tool_call(self, event: ToolCallEvent) -> None:
        ...

    def thought(self, event: ThoughtEvent) -> None:
        ...


class NoopAgentEventSink:
    def tool_call(self, event: ToolCallEvent) -> None:
        return

    def thought(self, event: ThoughtEvent) -> None:
        return
