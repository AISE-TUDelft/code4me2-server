from __future__ import annotations

import asyncio
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any
from unittest import TestCase

from acp.agent.router import build_agent_router
from acp.exceptions import RequestError
from acp.schema import TextContentBlock

from code4me2_agent.acp_runtime import create_acp_agent
from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.config import AgentConfig
from code4me2_agent.echo import EchoPromptResult
from code4me2_agent.runtime_auth import AcpRuntimeScope


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
