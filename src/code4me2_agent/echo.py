from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

from code4me2_agent.adapters import MemoryWindow, create_agent_adapter
from code4me2_agent.command_tools import WorkspaceCommandTools
from code4me2_agent.file_tools import WorkspaceFileTools
from code4me2_agent.telemetry import AgentTelemetryRecorder

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from code4me2_agent.config import AgentConfig
    from code4me2_agent.events import AgentEventSink
    from code4me2_agent.mcp_tools import StdioMcpToolBroker


@dataclass(frozen=True)
class EchoPromptResult:
    run_id: str
    final_response: str
    stop_reason: str
    run_status: str
    thoughts: tuple[str, ...] = ()
    duration_ms: float = 0.0


class EchoAgentCore:
    def __init__(
        self, config: AgentConfig, *, event_sink: AgentEventSink | None = None
    ) -> None:
        self._config = config
        self._event_sink = event_sink
        self._telemetry = AgentTelemetryRecorder(config)
        self.file_tools = WorkspaceFileTools(config, telemetry=self._telemetry)
        self.command_tools = WorkspaceCommandTools(config, telemetry=self._telemetry)
        self._session_memory = self._build_session_memory()
        self._adapter = create_agent_adapter(
            config,
            telemetry=self._telemetry,
            file_tools=self.file_tools,
            command_tools=self.command_tools,
            event_sink=self._event_sink,
        )

    def rebuild_tools(
        self,
        *,
        acp_file_backend: object | None = None,
        acp_command_backend: object | None = None,
        mcp_tools: StdioMcpToolBroker | None = None,
    ) -> None:
        self.file_tools = WorkspaceFileTools(
            self._config,
            acp_backend=acp_file_backend,
            telemetry=self._telemetry,
        )
        self.command_tools = WorkspaceCommandTools(
            self._config,
            acp_backend=acp_command_backend,
            telemetry=self._telemetry,
        )
        self.rebuild_adapter(mcp_tools=mcp_tools)

    def rebuild_adapter(
        self,
        *,
        mcp_tools: StdioMcpToolBroker | None = None,
    ) -> None:
        self._adapter = create_agent_adapter(
            self._config,
            telemetry=self._telemetry,
            file_tools=self.file_tools,
            command_tools=self.command_tools,
            event_sink=self._event_sink,
            mcp_tools=mcp_tools,
        )

    def _build_session_memory(self) -> MemoryWindow | None:
        memory_config = self._config.adapter.memory_window
        if memory_config.scope != "session":
            return None
        return MemoryWindow(
            strategy=memory_config.strategy,
            max_messages=memory_config.max_messages,
            max_tokens=memory_config.max_tokens,
        )

    def load_session_memory(self, messages: list[dict]) -> None:
        if self._session_memory is None:
            return
        self._session_memory.replace_messages(messages)

    def session_memory_snapshot(self) -> list[dict] | None:
        if self._session_memory is None:
            return None
        return self._session_memory.snapshot()

    def handle_prompt(
        self,
        prompt: str,
        request_id: str | None = None,
        message_id: str | None = None,
        run_id: str | None = None,
        cancellation_event: Event | None = None,
    ) -> EchoPromptResult:
        started_at = perf_counter()
        request_id = request_id or uuid4().hex
        run_id = run_id or uuid4().hex
        run_start = self._telemetry.record(
            event_type="agent.run.started",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=None,
            payload={"status": "started"},
        )
        prompt_event = self._telemetry.record(
            event_type="agent.request.received",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=run_start["event_id"],
            message_id=message_id,
            payload={"role": "user", "content": prompt},
            contains_user_prompt=True,
            raw_payload={"role": "user", "content": prompt, "message_id": message_id},
        )

        # 3
        logger.info("Adapter is: %s", self._adapter)
        adapter_result = self._adapter.handle_prompt(
            prompt=prompt,
            run_id=run_id,
            request_id=request_id,
            message_id=message_id,
            memory=self._session_memory,
            cancellation_event=cancellation_event,
        )
        # n-1
        final_response = adapter_result.final_response

        response_event = self._telemetry.record(
            event_type="agent.response.completed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=prompt_event["event_id"],
            message_id=message_id,
            payload={
                "role": "agent",
                "content": final_response,
                "stop_reason": adapter_result.stop_reason,
            },
            metrics={"response_character_count": len(final_response)},
            contains_agent_response=True,
            raw_payload={
                "role": "agent",
                "content": final_response,
                "message_id": message_id,
                "stop_reason": adapter_result.stop_reason,
            },
        )
        duration_ms = round((perf_counter() - started_at) * 1000, 3)
        self._telemetry.record(
            event_type="agent.run.completed",
            run_id=run_id,
            request_id=request_id,
            parent_event_id=response_event["event_id"],
            payload={"status": adapter_result.run_status},
            metrics={
                "duration_ms": duration_ms,
                "prompt_character_count": len(prompt),
                "response_character_count": len(final_response),
            },
        )
        return EchoPromptResult(
            run_id=run_id,
            final_response=final_response,
            stop_reason=adapter_result.stop_reason,
            run_status=adapter_result.run_status,
            thoughts=adapter_result.thoughts,
            duration_ms=duration_ms,
        )
