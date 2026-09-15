from __future__ import annotations

import asyncio
import sys
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any
from unittest import TestCase

from acp.agent.router import build_agent_router
from acp.exceptions import RequestError
from acp.schema import TextContentBlock

from code4me2_agent.adapters import (
    FakeOpenAICompatibleProvider,
    OpenAICompatibleReactAdapter,
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
)
from code4me2_agent.acp_runtime import AcpSessionEventSink, AgentSession, create_acp_agent
from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.config import (
    AdapterConfig,
    AgentConfig,
    FakeProviderConfig,
    ServerAgentConfig,
)
from code4me2_agent.echo import EchoPromptResult
from code4me2_agent.events import ApprovalDecision, ToolCallEvent
from code4me2_agent.runtime_auth import AcpRuntimeScope, AcpSessionExpired
from code4me2_agent.telemetry import AgentTelemetryRecorder


class _FakeAuthorization:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.is_authenticated = True
        self.server_agent_config = None
        self.backend_url = None

    def authenticate(self) -> AcpRuntimeScope:
        return AcpRuntimeScope(project_id="project-1", workspace=str(self.workspace))

    def validate(self) -> AcpRuntimeScope:
        return self.authenticate()

    def telemetry_headers(self) -> dict[str, str]:
        return {}

    def authorized_json_request(
        self,
        method: str,
        path: str,
        payload: object,
    ) -> dict[str, object]:
        if method == "GET":
            return {"messages": []}
        return {}


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def session_update(
        self,
        *,
        session_id: str,
        update: object,
        **kwargs: Any,
    ) -> None:
        self.updates.append(
            {"session_id": session_id, "update": update, "metadata": kwargs}
        )


class AcpRuntimeCompatibilityTest(TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.client = _FakeClient()
        self.authorization = _FakeAuthorization(self.workspace)
        self.agent = create_acp_agent(
            AgentConfig(
                workspace_root=self.workspace,
                trace_path=self.workspace / "agent-events.jsonl",
                session_id="bootstrap",
            ),
            authorization=self.authorization,
        )
        self.agent.on_connect(self.client)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_latest_sdk_models_and_stable_capabilities(self) -> None:
        async def scenario() -> None:
            response = await self.agent.initialize(
                protocol_version=99,
                client_capabilities=None,
            )

            self.assertEqual("0.12.1", version("agent-client-protocol"))
            self.assertEqual(1, response.protocol_version)
            self.assertFalse(response.agent_capabilities.load_session)
            self.assertIsNotNone(response.agent_capabilities.session_capabilities.resume)
            self.assertIsNotNone(response.agent_capabilities.session_capabilities.close)
            self.assertEqual("AuthMethodAgent", type(response.auth_methods[0]).__name__)
            self.assertEqual((), AcpUpdateBuilder().helper_gaps)

        asyncio.run(scenario())

    def test_permission_request_offers_supported_scopes_and_a_summary(self) -> None:
        permission = AcpUpdateBuilder().permission_request(
            session_id="session-1",
            tool_call_id="tool-1",
            title="Write file",
            kind="edit",
            summary="Write file: README.md",
            raw_input={"path": "README.md"},
            session_option_name="Allow edits for session",
        )

        self.assertEqual(
            ["allow_once", "allow_session", "reject_once"],
            [option.option_id for option in permission.options],
        )
        self.assertEqual({"path": "README.md"}, permission.tool_call.raw_input)
        self.assertEqual("Allow edits for session", permission.options[1].name)

    def test_permission_response_keeps_selected_scope(self) -> None:
        class Connection:
            async def request_permission(self, **_kwargs):
                return None

        class Runner:
            calls = 0

            def run(self, awaitable):
                self.calls += 1
                awaitable.close()
                return {"outcome": {"outcome": "selected", "optionId": "allow_session"}}

        runner = Runner()
        sink = AcpSessionEventSink(
            conn=Connection(),
            session_id="session-1",
            updates=AcpUpdateBuilder(),
            telemetry=object(),
            async_runner=runner,
        )

        decision = sink.request_approval(
            SimpleNamespace(name="write_file", tool_call_id="tool-1"),
            {"path": "README.md", "content": "private"},
        )

        self.assertTrue(decision.accepted)
        self.assertEqual("session", decision.scope)

        self.assertTrue(
            sink.request_approval(
                SimpleNamespace(name="write_file", tool_call_id="tool-2"),
                {"path": "other.md", "content": "private"},
            ).accepted
        )
        self.assertEqual(1, runner.calls)

    def test_permission_reuses_the_native_edit_diff(self) -> None:
        class Connection(_FakeClient):
            async def request_permission(self, **kwargs):
                self.permission = kwargs
                return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

        class Runner:
            def run(self, awaitable):
                return asyncio.run(awaitable)

        connection = Connection()
        sink = AcpSessionEventSink(
            conn=connection,
            session_id="session-1",
            updates=AcpUpdateBuilder(),
            telemetry=object(),
            async_runner=Runner(),
        )
        sink.tool_call(
            ToolCallEvent(
                phase="started",
                tool_call_id="tool-1",
                tool_name="write_file",
                run_id="run-1",
                request_id="request-1",
                title="Update README.md",
                kind="edit",
                status="pending",
                path="README.md",
                diff_old_text="before",
                diff_new_text="after",
            )
        )

        self.assertTrue(
            sink.request_approval(
                SimpleNamespace(name="write_file", tool_call_id="tool-1"),
                {"path": "README.md", "content": "after"},
            ).accepted
        )
        diff = connection.permission["tool_call"].content[0]
        self.assertEqual("diff", diff.type)
        self.assertEqual("README.md", diff.path)
        self.assertEqual("before", diff.old_text)
        self.assertEqual("after", diff.new_text)

    def test_server_tool_allowlist_hides_and_rejects_write_tools(self) -> None:
        config = AgentConfig(
            workspace_root=self.workspace,
            trace_path=self.workspace / "agent-events.jsonl",
            session_id="bootstrap",
        ).with_server_overrides(ServerAgentConfig(tools=["read_file"]))
        registry = ToolRegistry(
            file_tools=object(),
            command_tools=object(),
            allowed_tools=config.tools,
        )

        self.assertEqual(
            ["read_file"],
            [tool["function"]["name"] for tool in registry.definitions()],
        )
        with self.assertRaises(ToolRegistryError) as raised:
            registry.execute(
                ToolCall(
                    tool_call_id="write-denied",
                    name="write_file",
                    arguments={"path": "deneme.txt", "content": "hey"},
                ),
                run_id="run-1",
                request_id="request-1",
            )
        self.assertEqual("tool_not_allowed", raised.exception.failure_reason)

    def test_denied_tool_call_continues_with_a_tool_result(self) -> None:
        config = AgentConfig(
            workspace_root=self.workspace,
            trace_path=self.workspace / "agent-events.jsonl",
            session_id="bootstrap",
            tools=["read_file"],
            adapter=AdapterConfig(
                name="openai_compatible_react",
                fake_provider=FakeProviderConfig(
                    enabled=True,
                    script=[
                        {
                            "tool_calls": [
                                {
                                    "id": "write-denied",
                                    "name": "write_file",
                                    "arguments": {
                                        "path": "deneme.txt",
                                        "content": "hi",
                                    },
                                }
                            ]
                        },
                        {"final_answer": "I only have read access in this profile."},
                    ],
                ),
            ),
        )
        adapter = OpenAICompatibleReactAdapter(
            config,
            telemetry=AgentTelemetryRecorder(config),
            tool_registry=ToolRegistry(
                file_tools=object(),
                command_tools=object(),
                allowed_tools=config.tools,
            ),
        )

        result = adapter.handle_prompt(
            prompt="Write hi in deneme.txt",
            run_id="run-1",
            request_id="request-1",
            message_id=None,
        )

        self.assertEqual("completed", result.run_status)
        self.assertEqual("I only have read access in this profile.", result.final_response)

    def test_rejected_approval_is_explicit_in_the_model_tool_result(self) -> None:
        class RejectingSink:
            def request_approval(self, *_args):
                return ApprovalDecision("rejected")

            def tool_call(self, _event):
                return None

        class RecordingProvider(FakeOpenAICompatibleProvider):
            def __init__(self):
                super().__init__(
                    [
                        {
                            "tool_calls": [
                                {
                                    "id": "create-rejected",
                                    "name": "create_file",
                                    "arguments": {"path": "deneme.txt", "content": "hi"},
                                }
                            ]
                        },
                        {"final_answer": "No change was made."},
                    ]
                )
                self.messages: list[list[dict[str, Any]]] = []

            def generate(self, messages):
                self.messages.append(messages)
                return super().generate(messages)

        config = AgentConfig(
            workspace_root=self.workspace,
            trace_path=self.workspace / "agent-events.jsonl",
            session_id="bootstrap",
            tools=["create_file"],
            approval_policy="per_step",
        )
        provider = RecordingProvider()
        adapter = OpenAICompatibleReactAdapter(
            config,
            telemetry=AgentTelemetryRecorder(config),
            tool_registry=ToolRegistry(
                file_tools=object(),
                command_tools=object(),
                event_sink=RejectingSink(),
                allowed_tools=config.tools,
                approval_policy="per_step",
            ),
        )
        adapter._provider = lambda: provider

        result = adapter.handle_prompt(
            prompt="Create deneme.txt",
            run_id="run-1",
            request_id="request-1",
            message_id=None,
        )

        tool_result = provider.messages[1][-1]
        self.assertEqual("tool", tool_result["role"])
        self.assertIn('"reason": "user_rejected"', tool_result["content"])
        self.assertIn("Do not retry", tool_result["content"])
        self.assertEqual("No change was made.", result.final_response)

    def test_unsupported_routes_are_method_not_found_even_with_sdk_workaround(self) -> None:
        async def scenario() -> None:
            router = build_agent_router(self.agent, use_unstable_protocol=True)

            for method, params in (
                ("session/list", {}),
                ("session/load", {"cwd": str(self.workspace), "sessionId": "missing"}),
                ("session/set_model", {"sessionId": "missing", "modelId": "other"}),
                ("session/mode", {"sessionId": "missing", "modeId": "other"}),
                (
                    "session/fork",
                    {
                        "cwd": str(self.workspace),
                        "sessionId": "missing",
                        "mcpServers": [],
                    },
                ),
            ):
                with self.subTest(method=method):
                    with self.assertRaises(RequestError) as raised:
                        await router(method, params, False)
                    self.assertEqual(-32601, raised.exception.code)

        asyncio.run(scenario())

    def test_nonempty_unsupported_session_inputs_are_rejected(self) -> None:
        async def scenario() -> None:
            await self.agent.initialize(protocol_version=1, client_capabilities=None)

            with self.assertRaises(RequestError) as mcp_error:
                await self.agent.new_session(
                    cwd=str(self.workspace),
                    mcp_servers=[{"name": "tools", "command": "tools-server"}],
                )
            self.assertEqual("mcp_servers_not_supported", mcp_error.exception.data["reason"])

            with self.assertRaises(RequestError) as roots_error:
                await self.agent.new_session(
                    cwd=str(self.workspace),
                    additional_directories=[str(self.workspace / "other")],
                )
            self.assertEqual(
                "additional_directories_not_supported",
                roots_error.exception.data["reason"],
            )

        asyncio.run(scenario())

    def test_wellformed_but_unstartable_mcp_server_degrades_gracefully(self) -> None:
        async def scenario() -> None:
            await self.agent.initialize(protocol_version=1, client_capabilities=None)
            new_session = await self.agent.new_session(
                cwd=str(self.workspace),
                mcp_servers=[
                    {
                        "name": "missing",
                        "command": "/nonexistent-mcp-binary-xyz",
                        "args": [],
                        "env": [],
                    }
                ],
            )
            session = self.agent._sessions[new_session.session_id]
            self.assertIsNone(session.mcp_tools)

        asyncio.run(scenario())

    def test_working_stdio_mcp_server_attaches_tools(self) -> None:
        async def scenario() -> None:
            await self.agent.initialize(protocol_version=1, client_capabilities=None)
            toy_path = str(Path(__file__).with_name("mcp_toy_server.py"))
            new_session = await self.agent.new_session(
                cwd=str(self.workspace),
                mcp_servers=[
                    {
                        "name": "toy",
                        "command": sys.executable,
                        "args": [toy_path],
                        "env": [],
                    }
                ],
            )
            session = self.agent._sessions[new_session.session_id]
            try:
                self.assertIsNotNone(session.mcp_tools)
                names = [d["function"]["name"] for d in session.mcp_tools.definitions()]
                self.assertIn("mcp__toy__toy_add", names)
                result = session.mcp_tools.execute(
                    "mcp__toy__toy_add", {"a": 1, "b": 2}
                )
                self.assertEqual("completed", result["status"])
            finally:
                await self.agent.close_session(session_id=new_session.session_id)

        asyncio.run(scenario())

    def test_active_prompt_cancellation_is_cooperative_and_returns_cancelled(self) -> None:
        async def scenario() -> None:
            await self.agent.initialize(protocol_version=1, client_capabilities=None)
            new_session = await self.agent.new_session(cwd=str(self.workspace))
            session = self.agent._sessions[new_session.session_id]
            prompt_started = Event()

            def blocking_prompt(
                prompt: str,
                request_id: str | None = None,
                message_id: str | None = None,
                run_id: str | None = None,
                cancellation_event: Event | None = None,
                on_delta: Any | None = None,
            ) -> EchoPromptResult:
                prompt_started.set()
                if cancellation_event is None or not cancellation_event.wait(timeout=2):
                    raise AssertionError("Cancellation was not propagated to the active turn")
                return EchoPromptResult(
                    run_id=run_id or "cancelled-run",
                    final_response="",
                    stop_reason="cancelled",
                    run_status="cancelled",
                )

            session.core.handle_prompt = blocking_prompt
            prompt_task = asyncio.create_task(
                self.agent.prompt(
                    prompt=[TextContentBlock(type="text", text="wait")],
                    session_id=new_session.session_id,
                )
            )
            started = await asyncio.to_thread(prompt_started.wait, 1)
            self.assertTrue(started)

            await self.agent.cancel(session_id=new_session.session_id)
            response = await asyncio.wait_for(prompt_task, timeout=2)

            self.assertEqual("cancelled", response.stop_reason)
            self.assertEqual([], self.client.updates)
            self.assertIsNone(session.active_prompt_task)
            self.assertFalse(session.cancel_event.is_set())

        asyncio.run(scenario())

    def test_prompt_message_has_stable_message_id_and_close_releases_session(self) -> None:
        async def scenario() -> None:
            await self.agent.initialize(protocol_version=1, client_capabilities=None)
            new_session = await self.agent.new_session(cwd=str(self.workspace))

            response = await self.agent.prompt(
                prompt=[TextContentBlock(type="text", text="hello")],
                session_id=new_session.session_id,
            )

            self.assertEqual("end_turn", response.stop_reason)
            self.assertEqual(1, len(self.client.updates))
            update = self.client.updates[0]["update"]
            self.assertIsNotNone(update.message_id)

            await self.agent.close_session(session_id=new_session.session_id)
            self.assertNotIn(new_session.session_id, self.agent._sessions)

        asyncio.run(scenario())

    def test_prompt_double_401_maps_to_auth_required(self) -> None:
        """A 401 after the pre-turn reauth (double-401) must surface as an ACP
        auth error, never leak a backend exception out of prompt()."""

        class _Double401Authorization:
            def __init__(self, workspace: Path) -> None:
                self._workspace = workspace.resolve()
                self.validations = 0
                self.runs = 0

            def validate(self) -> AcpRuntimeScope:
                self.validations += 1
                if self.validations == 1:
                    raise AcpSessionExpired("token expired")
                return AcpRuntimeScope(
                    project_id="project-1", workspace=str(self._workspace)
                )

            def prepare_workspace(self, workspace: object) -> None:
                return None

            def authenticate(self) -> AcpRuntimeScope:
                return AcpRuntimeScope(
                    project_id="project-1", workspace=str(self._workspace)
                )

            def telemetry_headers(self) -> dict[str, str]:
                return {"Authorization": "Bearer refreshed"}

            def create_managed_run(self, *, run_id: str, session_id: str) -> dict:
                self.runs += 1
                raise AcpSessionExpired("token expired again")

        async def scenario() -> None:
            managed_config = AgentConfig(
                workspace_root=self.workspace,
                trace_path=self.workspace / "agent-events.jsonl",
                session_id="bootstrap",
                managed_mode=True,
            )
            agent = create_acp_agent(managed_config, authorization=self.authorization)
            agent.on_connect(self.client)
            authorization = _Double401Authorization(self.workspace)
            core_config = AgentConfig(
                workspace_root=self.workspace,
                trace_path=self.workspace / "agent-events.jsonl",
                session_id="managed-session",
            )

            def _must_not_run(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("the turn must fail before the model call")

            core = SimpleNamespace(
                _config=core_config,
                _telemetry=SimpleNamespace(),
                apply_config=lambda config: None,
                handle_prompt=_must_not_run,
            )
            agent._sessions["managed-session"] = AgentSession(
                session_id="managed-session",
                core=core,
                authorization=authorization,
            )

            with self.assertRaises(RequestError) as raised:
                await agent.prompt(
                    prompt=[TextContentBlock(type="text", text="hello")],
                    session_id="managed-session",
                )
            self.assertEqual("authorization_rejected", raised.exception.data["reason"])
            self.assertEqual(1, authorization.validations)
            self.assertEqual(1, authorization.runs)
            session = agent._sessions["managed-session"]
            self.assertIsNone(session.active_prompt_task)
            self.assertFalse(session.cancel_event.is_set())

        asyncio.run(scenario())
