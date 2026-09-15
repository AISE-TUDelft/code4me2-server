from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Callable

from mcp import Client, StdioServerParameters

from code4me2_agent.acp_utils import capability_value

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_MCP_STARTUP_TIMEOUT_SECONDS = 30.0
_MCP_CALL_TIMEOUT_SECONDS = 120.0
_OPENAI_TOOL_NAME_LIMIT = 64
_UNSAFE_TOOL_NAME_CHARACTER_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class _McpTool:
    public_name: str
    server_name: str
    remote_name: str
    definition: dict[str, Any]


def _legacy_mcp_client(parameters: StdioServerParameters) -> Client:
    return Client(parameters, mode="legacy")


class StdioMcpToolBroker:
    """Own stdio MCP clients and expose their tools to the synchronous agent loop.

    MCP clients use AnyIO task groups whose context must be exited by the task that
    entered it. A dedicated owner thread keeps that lifecycle intact while the
    model/tool loop, which already runs in a worker thread, submits tool calls to
    the owner's event loop.
    """

    def __init__(
        self,
        servers: list[object],
        *,
        cwd: Path,
        client_factory: Callable[[StdioServerParameters], Client] = _legacy_mcp_client,
    ) -> None:
        self._servers = list(servers)
        self._cwd = cwd
        self._client_factory = client_factory
        self._ready = Event()
        self._closed = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._close_requested: asyncio.Event | None = None
        self._startup_error: BaseException | None = None
        self._tools: dict[str, _McpTool] = {}
        self._clients: dict[str, Client] = {}
        self._thread = Thread(
            target=self._thread_main,
            name="code4me2-mcp",
            daemon=True,
        )

    @classmethod
    def open(
        cls,
        servers: list[object],
        *,
        cwd: Path,
        client_factory: Callable[[StdioServerParameters], Client] = _legacy_mcp_client,
        startup_timeout_seconds: float = _MCP_STARTUP_TIMEOUT_SECONDS,
    ) -> StdioMcpToolBroker | None:
        if not servers:
            return None
        broker = cls(servers, cwd=cwd, client_factory=client_factory)
        broker._thread.start()
        if not broker._ready.wait(timeout=startup_timeout_seconds):
            broker.close()
            raise TimeoutError("Timed out while starting an ACP-provided MCP server.")
        if broker._startup_error is not None:
            broker.close()
            raise RuntimeError(
                f"Could not start an ACP-provided MCP server: {broker._startup_error}"
            ) from broker._startup_error
        return broker

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self._tools.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        cancellation_event: Event | None = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown MCP tool: {name}")
        loop = self._loop
        if loop is None or loop.is_closed() or self._closed.is_set():
            raise RuntimeError(f"MCP server {tool.server_name!r} is no longer available.")
        if cancellation_event is not None and cancellation_event.is_set():
            import asyncio as _asyncio

            raise _asyncio.CancelledError("MCP tool call was cancelled.")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool(tool, arguments),
            loop,
        )
        # Slice the 120s wait so cancellation aborts promptly (no blind 120s block).
        import time as _time

        deadline = _time.monotonic() + _MCP_CALL_TIMEOUT_SECONDS
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                future.cancel()
                raise asyncio.CancelledError("MCP tool call was cancelled.")
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise TimeoutError(
                    f"MCP tool {tool.remote_name!r} on server {tool.server_name!r} timed out."
                ) from None
            try:
                return future.result(timeout=min(0.2, remaining))
            except concurrent.futures.TimeoutError:
                # Slice expiry — re-poll cancel/deadline.
                continue

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        loop = self._loop
        close_requested = self._close_requested
        if loop is not None and close_requested is not None and not loop.is_closed():
            loop.call_soon_threadsafe(close_requested.set)
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            logger.warning("An ACP-provided MCP server did not stop within five seconds.")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # noqa: BLE001
            self._startup_error = exc
            self._ready.set()
        finally:
            self._closed.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._close_requested = asyncio.Event()
        async with AsyncExitStack() as stack:
            await self._connect_servers(stack)
            self._ready.set()
            await self._close_requested.wait()

    async def _connect_servers(self, stack: AsyncExitStack) -> None:
        for server in self._servers:
            server_name, parameters = _stdio_server_parameters(server, cwd=self._cwd)
            if server_name in self._clients:
                raise ValueError(f"Duplicate ACP MCP server name: {server_name!r}")
            client = await stack.enter_async_context(self._client_factory(parameters))
            self._clients[server_name] = client
            await self._discover_tools(server_name, client)

    async def _discover_tools(self, server_name: str, client: Client) -> None:
        cursor: str | None = None
        while True:
            result = await client.list_tools(cursor=cursor)
            for tool in result.tools:
                remote_name = str(tool.name)
                public_name = _public_tool_name(server_name, remote_name)
                if public_name in self._tools:
                    raise ValueError(
                        f"ACP MCP tools map to the same public name: {public_name!r}"
                    )
                parameters = dict(tool.input_schema or {"type": "object"})
                description = str(tool.description or tool.title or remote_name)
                self._tools[public_name] = _McpTool(
                    public_name=public_name,
                    server_name=server_name,
                    remote_name=remote_name,
                    definition={
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": f"MCP server {server_name}: {description}",
                            "parameters": parameters,
                        },
                    },
                )
            cursor = result.next_cursor
            if cursor is None:
                break

    async def _call_tool(
        self,
        tool: _McpTool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        client = self._clients[tool.server_name]
        result = await client.call_tool(tool.remote_name, arguments)
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {
            "status": "failed" if result.is_error else "completed",
            "server_name": tool.server_name,
            "tool_name": tool.remote_name,
            "content": payload.get("content", []),
            "structured_content": payload.get("structuredContent"),
        }


def _stdio_server_parameters(
    server: object,
    *,
    cwd: Path,
) -> tuple[str, StdioServerParameters]:
    name = str(capability_value(server, "name") or "").strip()
    command = str(capability_value(server, "command") or "").strip()
    if not name or not command:
        raise ValueError(
            "Only named stdio MCP servers are supported; HTTP, SSE, and ACP transports "
            "must not be sent when their capabilities are not advertised."
        )
    args_value = capability_value(server, "args") or []
    env_value = capability_value(server, "env") or []
    if not isinstance(args_value, (list, tuple)) or not isinstance(
        env_value, (list, tuple, dict)
    ):
        raise ValueError(f"Invalid stdio MCP configuration for server {name!r}.")
    if isinstance(env_value, dict):
        environment = {str(key): str(value) for key, value in env_value.items()}
    else:
        environment = {}
        for item in env_value:
            key = str(capability_value(item, "name") or "").strip()
            value = capability_value(item, "value")
            if not key or value is None:
                raise ValueError(f"Invalid environment entry for MCP server {name!r}.")
            environment[key] = str(value)
    return name, StdioServerParameters(
        command=command,
        args=[str(value) for value in args_value],
        env=environment,
        cwd=cwd,
    )


def _public_tool_name(server_name: str, remote_name: str) -> str:
    raw_name = f"mcp__{server_name}__{remote_name}"
    normalized = _UNSAFE_TOOL_NAME_CHARACTER_RE.sub("_", raw_name).strip("_")
    if not normalized:
        normalized = "mcp_tool"
    if len(normalized) <= _OPENAI_TOOL_NAME_LIMIT:
        return normalized
    digest = hashlib.sha256(raw_name.encode("utf-8")).hexdigest()[:10]
    prefix_length = _OPENAI_TOOL_NAME_LIMIT - len(digest) - 2
    return f"{normalized[:prefix_length]}__{digest}"


def serialize_mcp_servers(servers: list[object] | None) -> str:
    """Return a log-safe description without exposing environment values."""

    summaries = []
    for server in servers or []:
        summaries.append(
            {
                "name": capability_value(server, "name"),
                "transport": (
                    "stdio" if capability_value(server, "command") else "unsupported"
                ),
            }
        )
    return json.dumps(summaries, sort_keys=True)
