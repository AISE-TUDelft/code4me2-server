from __future__ import annotations

from typing import Any, Sequence


class AcpUpdateBuilder:
    """Small compatibility layer over the pinned ACP Python SDK update surface."""

    def __init__(self) -> None:
        import acp
        from acp import schema

        self._acp = acp
        self._schema = schema

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

    def user_message(self, text: str, *, message_id: str | None = None) -> Any:
        """A replayed user message (``session/load``)."""
        update = self._schema.UserMessageChunk(
            session_update="user_message_chunk",
            content=self._schema.TextContentBlock(type="text", text=text),
        )
        if message_id is None:
            return update
        return update.model_copy(update={"message_id": message_id})

    def available_commands(self, commands: Sequence[Any]) -> Any:
        """``available_commands_update`` from ``slash_commands.SlashCommand`` entries."""
        available = []
        for command in commands:
            hint = getattr(command, "hint", None)
            available.append(
                self._schema.AvailableCommand(
                    name=str(command.name),
                    description=str(command.description),
                    input=(
                        self._schema.AvailableCommandInput(
                            self._schema.UnstructuredCommandInput(hint=str(hint))
                        )
                        if hint
                        else None
                    ),
                )
            )
        return self._schema.AvailableCommandsUpdate(
            session_update="available_commands_update",
            available_commands=available,
        )

    def session_info(self, *, title: str | None, updated_at: str | None = None) -> Any:
        return self._schema.SessionInfoUpdate(
            session_update="session_info_update",
            title=title,
            updated_at=updated_at,
        )

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

    def agent_plan(self, entries: Sequence[Any]) -> Any:
        """Build a ``plan`` session update from PlanEntrySpec-like objects or dicts."""
        plan_entries = []
        for entry in entries:
            if isinstance(entry, dict):
                content = str(entry.get("content", ""))
                status = str(entry.get("status", "pending"))
                priority = str(entry.get("priority", "medium"))
            else:
                content = str(getattr(entry, "content", ""))
                status = str(getattr(entry, "status", "pending"))
                priority = str(getattr(entry, "priority", "medium"))
            plan_entries.append(
                self._schema.PlanEntry(content=content, priority=priority, status=status)
            )
        return self._schema.AgentPlanUpdate(session_update="plan", entries=plan_entries)

    def usage_update(
        self,
        *,
        used: int,
        size: int,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        update = self._schema.UsageUpdate(
            session_update="usage_update",
            used=max(0, int(used)),
            size=max(0, int(size)),
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

    def start_tool_call(
        self,
        *,
        tool_call_id: str,
        title: str,
        kind: str,
        status: str = "pending",
        path: str | None = None,
        locations: Sequence[str] | None = None,
        content: Sequence[Any] | None = None,
        raw_input: Any | None = None,
        raw_output: Any | None = None,
    ) -> Any:
        location_paths = [str(item) for item in locations if item] if locations else ([path] if path else [])
        location_models = (
            [self._schema.ToolCallLocation(path=item) for item in location_paths]
            if location_paths
            else None
        )
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
                locations=location_models,
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
            locations=location_models,
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
        summary: str,
        raw_input: Any | None = None,
        session_option_name: str = "Allow for session",
        content: Sequence[Any] | None = None,
    ) -> Any:
        return self._schema.RequestPermissionRequest(
            session_id=session_id,
            tool_call=self.update_tool_call(
                tool_call_id=tool_call_id,
                title=title,
                kind=kind,
                status="pending",
                content=content or [self.text_tool_content(summary)],
                raw_input=raw_input,
            ),
            options=[
                self._schema.PermissionOption(
                    option_id="allow_once",
                    name="Allow once",
                    kind="allow_once",
                ),
                self._schema.PermissionOption(
                    option_id="allow_session",
                    name=session_option_name,
                    kind="allow_always",
                ),
                self._schema.PermissionOption(
                    option_id="reject_once",
                    name="Reject",
                    kind="reject_once",
                ),
            ],
        )
