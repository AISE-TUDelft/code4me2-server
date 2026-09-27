from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import MagicMock

from acp.agent.router import build_agent_router
from acp.schema import EmbeddedResourceContentBlock, TextContentBlock, TextResourceContents

from code4me2_agent.acp_runtime import (
    AcpSessionEventSink,
    _prompt_text_async,
    _replay_updates,
    create_acp_agent,
)
from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.adapters import ToolCall, ToolRegistry
from code4me2_agent.async_bridge import EventLoopAsyncRunner
from code4me2_agent.config import (
    AdapterConfig,
    AgentConfig,
    FakeProviderConfig,
    MemoryWindowConfig,
)
from code4me2_agent.events import ToolCallEvent
from code4me2_agent.runtime_auth import AcpRuntimeScope


class _Authorization:
    def __init__(self, workspace: Path, memory: list[dict[str, Any]] | None = None) -> None:
        self.workspace = workspace.resolve()
        self.is_authenticated = True
        self.server_agent_config = None
        self.backend_url = None
        self.memory = memory or []

    def authenticate(self) -> AcpRuntimeScope:
        return AcpRuntimeScope(project_id="project-1", workspace=str(self.workspace))

    def validate(self) -> AcpRuntimeScope:
        return self.authenticate()

    def telemetry_headers(self) -> dict[str, str]:
        return {}

    def authorized_json_request(self, method: str, path: str, payload: object) -> dict[str, object]:
        if method == "GET":
            return {"messages": list(self.memory)}
        if method == "PUT" and isinstance(payload, dict):
            self.memory = list(payload.get("messages", []))
        return {}


class _Client:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, *, session_id: str, update: object, **kwargs: Any) -> None:
        self.updates.append(update)


def _agent(workspace: Path, *, memory=None, script=None):
    config = AgentConfig(
        workspace_root=workspace,
        trace_path=workspace / ".trace.jsonl",
        session_id="bootstrap",
        adapter=AdapterConfig(
            name="openai_compatible_react",
            max_iterations=4,
            memory_window=MemoryWindowConfig(scope="session", strategy="token_window", max_tokens=32000),
            fake_provider=FakeProviderConfig(enabled=script is not None, script=list(script or [])),
        ),
    )
    authorization = _Authorization(workspace, memory)
    agent = create_acp_agent(config, authorization=authorization)
    client = _Client()
    agent.on_connect(client)
    return agent, client, authorization


HISTORY = [
    {"role": "system", "content": "system prompt"},
    {"role": "user", "content": "please read a.txt"},
    {
        "role": "assistant",
        "content": "Reading it.",
        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}],
    },
    {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "read_file",
        "content": json.dumps({"status": "ok", "path": "a.txt", "total_lines": 1, "start_line": 1, "end_line": 1}),
    },
    {"role": "user", "content": "[Self-review ...]", "code4me_runtime": "review"},
    {"role": "assistant", "content": "It says hello."},
]


class AcpHarnessSurfaceTest(TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_sessions_announce_the_slash_commands_after_the_response(self) -> None:
        agent, client, _auth = _agent(self.workspace)

        async def scenario() -> None:
            await agent.initialize(protocol_version=1, client_capabilities=None)
            await agent.new_session(cwd=str(self.workspace))
            self.assertEqual([], client.updates)  # nothing before the response
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(scenario())
        kinds = [update.session_update for update in client.updates]
        self.assertEqual(["available_commands_update"], kinds)
        names = [command.name for command in client.updates[0].available_commands]
        self.assertEqual(["status", "undo", "compact", "review"], names)

    def test_load_session_replays_the_conversation_then_answers(self) -> None:
        (self.workspace / "a.txt").write_text("hello\n")
        agent, client, _auth = _agent(self.workspace, memory=HISTORY)

        async def scenario() -> None:
            await agent.initialize(protocol_version=1, client_capabilities=None)
            router = build_agent_router(agent, use_unstable_protocol=True)
            await router(
                "session/load",
                {"cwd": str(self.workspace), "sessionId": "chat-1", "mcpServers": []},
                False,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(scenario())
        kinds = [update.session_update for update in client.updates]
        self.assertEqual(
            [
                "user_message_chunk",
                "agent_message_chunk",
                "tool_call",
                "agent_message_chunk",
                "session_info_update",
                "available_commands_update",
            ],
            kinds,
        )
        card = client.updates[2]
        self.assertEqual("completed", card.status)
        self.assertEqual("Read a.txt", card.title)
        self.assertEqual("please read a.txt", client.updates[-2].title)
        # The runtime-internal review note is not replayed.
        texts = [getattr(getattr(u, "content", None), "text", "") for u in client.updates]
        self.assertFalse(any("Self-review" in text for text in texts))

    def test_http_mcp_entries_no_longer_fail_session_creation(self) -> None:
        agent, _client, _auth = _agent(self.workspace)

        async def scenario() -> str:
            await agent.initialize(protocol_version=1, client_capabilities=None)
            response = await agent.new_session(
                cwd=str(self.workspace),
                mcp_servers=[
                    {"type": "http", "name": "idea", "url": "http://127.0.0.1:9/mcp", "headers": []}
                ],
            )
            return response.session_id

        session_id = asyncio.run(scenario())
        session = agent._sessions[session_id]
        self.assertIsNone(session.mcp_tools)  # unreachable, but the chat still works

    def test_malformed_mcp_entries_are_still_rejected(self) -> None:
        from acp.exceptions import RequestError

        agent, _client, _auth = _agent(self.workspace)

        async def scenario() -> None:
            await agent.initialize(protocol_version=1, client_capabilities=None)
            with self.assertRaises(RequestError):
                await agent.new_session(
                    cwd=str(self.workspace), mcp_servers=[{"type": "http", "name": "idea"}]
                )

        asyncio.run(scenario())


def test_embedded_resources_are_inlined_with_a_budget(tmp_path):
    workspace = tmp_path.resolve()
    (workspace / "src").mkdir()
    blocks = [
        TextContentBlock(type="text", text="Explain this file"),
        EmbeddedResourceContentBlock(
            type="resource",
            resource=TextResourceContents(
                uri=(workspace / "src" / "app.py").as_uri(), mimeType="text/x-python", text="x = 1\n" * 2000
            ),
        ),
        {"type": "resource", "resource": {"uri": "file:///tmp/logo.png", "blob": "AAAA", "mimeType": "image/png"}},
    ]

    text = asyncio.run(_prompt_text_async(blocks, workspace_root=workspace, attachment_budget_chars=4000))

    assert text.startswith("Explain this file\n")
    assert '<attached_file path="src/app.py" mime="text/x-python">' in text
    assert "[attachment truncated: showing 4000 of 12000 characters" in text
    assert "[Attached binary resource: /tmp/logo.png, image/png" in text


def test_replay_skips_card_less_tools_and_marks_failures(tmp_path):
    messages = [
        {"role": "user", "content": "plan and fail"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "p", "name": "update_plan", "arguments": {"entries": []}},
                {"id": "x", "name": "run_command", "arguments": {"argv": ["pytest"]}},
            ],
        },
        {"role": "tool", "tool_call_id": "p", "content": json.dumps({"status": "ok"})},
        {"role": "tool", "tool_call_id": "x", "content": json.dumps({"status": "denied", "reason": "tool_not_allowed"})},
    ]
    updates = _replay_updates(messages, updates=AcpUpdateBuilder(), workspace_root=tmp_path)
    assert [u.session_update for u in updates] == ["user_message_chunk", "tool_call"]
    assert updates[1].status == "failed"
    assert updates[1].content[0].content.text == "tool_not_allowed"


def test_curated_read_only_mcp_tools_skip_approval_and_show_as_search(tmp_path):
    broker = MagicMock()
    broker.definitions.return_value = [
        {"type": "function", "function": {"name": "mcp__idea__get_file_problems", "parameters": {}}},
        {"type": "function", "function": {"name": "mcp__idea__rename_refactoring", "parameters": {}}},
    ]
    broker.has_tool.return_value = True
    broker.tool_access.side_effect = lambda name: "read" if name.endswith("get_file_problems") else "edit"
    broker.execute.return_value = {"status": "completed", "content": []}
    file_tools = MagicMock()
    file_tools.workspace_root = tmp_path
    registry = ToolRegistry(file_tools, MagicMock(), mcp_tools=broker, approval_policy="suggestion_only")

    names = [d["function"]["name"] for d in registry.definitions()]
    assert "mcp__idea__get_file_problems" in names and "mcp__idea__rename_refactoring" not in names
    assert registry.display_kind("mcp__idea__get_file_problems") == "search"
    assert registry.is_parallel_safe("mcp__idea__get_file_problems")
    per_step = ToolRegistry(file_tools, MagicMock(), mcp_tools=broker, approval_policy="per_step")
    assert not per_step.needs_approval("mcp__idea__get_file_problems")
    assert per_step.needs_approval("mcp__idea__rename_refactoring")
    result = registry.execute(ToolCall("t", "mcp__idea__get_file_problems", {}), run_id="r", request_id="q")
    assert result["status"] == "completed"


def test_sink_renders_every_diff_notes_and_streamed_progress():
    sent: list[Any] = []

    class Client:
        async def session_update(self, *, session_id, update, **kwargs):
            sent.append(update)

    async def scenario() -> None:
        sink = AcpSessionEventSink(
            conn=Client(),
            session_id="s",
            updates=AcpUpdateBuilder(),
            telemetry=None,
            async_runner=EventLoopAsyncRunner(asyncio.get_running_loop()),
        )

        def emit() -> None:
            sink.tool_call(
                ToolCallEvent(
                    phase="completed",
                    tool_call_id="p",
                    tool_name="apply_patch",
                    run_id="r",
                    request_id="q",
                    title="Apply patch to a.py and 1 more file",
                    kind="edit",
                    status="completed",
                    path="/ws/a.py",
                    diff_old_text="a",
                    diff_new_text="b",
                    content_text="Syntax error introduced (python, line 1): invalid syntax",
                    extra_diffs=(("/ws/b.py", None, "new"),),
                )
            )
            sink.tool_call(
                ToolCallEvent(
                    phase="progress",
                    tool_call_id="c",
                    tool_name="run_command",
                    run_id="r",
                    request_id="q",
                    title="Run pytest",
                    kind="execute",
                    status="in_progress",
                    content_text="Running…\ncollected 3 items",
                )
            )

        await asyncio.to_thread(emit)

    asyncio.run(scenario())
    card, progress = sent
    assert [item.type for item in card.content] == ["diff", "diff", "content"]
    assert card.content[1].path == "/ws/b.py"
    assert progress.session_update == "tool_call_update"
    assert progress.status == "in_progress"
    assert "collected 3 items" in progress.content[0].content.text
