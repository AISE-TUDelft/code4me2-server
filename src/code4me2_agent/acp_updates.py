from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any, Sequence


class AcpUpdateBuilder:
    """Small compatibility layer over the pinned ACP Python SDK update surface."""

    def __init__(self) -> None:
        import acp
        from acp import schema

        self._acp = acp
        self._schema = schema

    @property
    def sdk_version(self) -> str:
        try:
            return version("agent-client-protocol")
        except PackageNotFoundError:
            return "unknown"

    @property
    def helper_gaps(self) -> tuple[str, ...]:
        required_helpers = (
            "text_block",
            "tool_content",
            "tool_diff_content",
            "tool_terminal_ref",
            "update_agent_message",
            "update_agent_thought",
            "start_tool_call",
            "start_read_tool_call",
            "start_edit_tool_call",
            "update_tool_call",
        )
        return tuple(name for name in required_helpers if not hasattr(self._acp, name))

    def agent_message(
        self,
        text: str,
        *,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        text_block = getattr(self._acp, "text_block", None)
        update_agent_message = getattr(self._acp, "update_agent_message", None)
        if text_block is not None and update_agent_message is not None:
            update = update_agent_message(text_block(text))
        else:
            update = self._schema.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=self._schema.TextContentBlock(type="text", text=text),
            )
        model_updates: dict[str, Any] = {}
        if message_id is not None:
            model_updates["message_id"] = message_id
        if metadata is not None:
            model_updates["field_meta"] = metadata
        if not model_updates:
            return update
        return update.model_copy(update=model_updates)

    def agent_thought(self, text: str, *, metadata: dict[str, Any] | None = None) -> Any:
        text_block = getattr(self._acp, "text_block", None)
        update_agent_thought = getattr(self._acp, "update_agent_thought", None)
        if text_block is not None and update_agent_thought is not None:
            update = update_agent_thought(text_block(text))
        else:
            update = self._schema.AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=self._schema.TextContentBlock(type="text", text=text),
            )
        if metadata is None:
            return update
        return update.model_copy(update={"field_meta": metadata})

    def text_tool_content(self, text: str) -> Any:
        text_block = getattr(self._acp, "text_block", None)
        tool_content = getattr(self._acp, "tool_content", None)
        if text_block is not None and tool_content is not None:
            return tool_content(text_block(text))
        return self._schema.ContentToolCallContent(
            type="content",
            content=self._schema.TextContentBlock(type="text", text=text),
        )

    def diff_tool_content(self, path: str, *, new_text: str, old_text: str | None = None) -> Any:
        tool_diff_content = getattr(self._acp, "tool_diff_content", None)
        if tool_diff_content is not None:
            return tool_diff_content(path, new_text, old_text)
        return self._schema.FileEditToolCallContent(
            type="diff",
            path=path,
            old_text=old_text,
            new_text=new_text,
        )

    def terminal_tool_content(self, terminal_id: str) -> Any:
        tool_terminal_ref = getattr(self._acp, "tool_terminal_ref", None)
        if tool_terminal_ref is not None:
            return tool_terminal_ref(terminal_id)
        return self._schema.TerminalToolCallContent(
            type="terminal",
            terminal_id=terminal_id,
        )

    def start_tool_call(
        self,
        *,
        tool_call_id: str,
        title: str,
        kind: str,
        status: str = "pending",
        path: str | None = None,
        content: Sequence[Any] | None = None,
        raw_input: Any | None = None,
        raw_output: Any | None = None,
    ) -> Any:
        locations = [self._schema.ToolCallLocation(path=path)] if path else None
        if raw_input is None and path is not None:
            raw_input = {"path": path}

        start_tool_call = getattr(self._acp, "start_tool_call", None)
        if start_tool_call is not None:
            return start_tool_call(
                tool_call_id,
                title,
                kind=kind,
                status=status,
                content=content,
                locations=locations,
                raw_input=raw_input,
                raw_output=raw_output,
            )
        return self._schema.ToolCallStart(
            session_update="tool_call",
            tool_call_id=tool_call_id,
            title=title,
            kind=kind,
            status=status,
            content=list(content) if content is not None else None,
            locations=locations,
            raw_input=raw_input,
            raw_output=raw_output,
        )

    def update_tool_call(
        self,
        *,
        tool_call_id: str,
        title: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        content: Sequence[Any] | None = None,
        raw_input: Any | None = None,
        raw_output: Any | None = None,
    ) -> Any:
        update_tool_call = getattr(self._acp, "update_tool_call", None)
        if update_tool_call is not None:
            return update_tool_call(
                tool_call_id,
                title=title,
                kind=kind,
                status=status,
                content=content,
                raw_input=raw_input,
                raw_output=raw_output,
            )
        return self._schema.ToolCallProgress(
            session_update="tool_call_update",
            tool_call_id=tool_call_id,
            title=title,
            kind=kind,
            status=status,
            content=list(content) if content is not None else None,
            raw_input=raw_input,
            raw_output=raw_output,
        )

    def permission_request(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        title: str,
        kind: str,
    ) -> Any:
        return self._schema.RequestPermissionRequest(
            session_id=session_id,
            tool_call=self.update_tool_call(
                tool_call_id=tool_call_id,
                title=title,
                kind=kind,
                status="pending",
            ),
            options=[
                self._schema.PermissionOption(
                    option_id="allow_once",
                    name="Allow once",
                    kind="allow_once",
                ),
                self._schema.PermissionOption(
                    option_id="reject_once",
                    name="Reject once",
                    kind="reject_once",
                ),
            ],
        )
