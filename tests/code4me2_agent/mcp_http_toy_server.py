"""Toy MCP servers served over streamable HTTP or SSE for the broker tests.

``ToyHttpServer`` runs uvicorn in a daemon thread on a pre-bound ephemeral
``127.0.0.1`` port (no port race, no network beyond localhost). Every HTTP
request's method, path and headers are recorded so a test can prove that the
headers of an ACP MCP entry reach the server on every request.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer

if TYPE_CHECKING:
    from collections.abc import Iterable

ToyTransport = Literal["http", "sse"]


def build_toy_server(name: str = "toy-http", *, extra_tool_names: Iterable[str] = ()) -> MCPServer:
    """An MCP server with ``toy_add`` plus one no-argument tool per extra name.

    Each extra tool returns its own name, which lets a test prove that a call
    reached the right remote tool.
    """

    server = MCPServer(name=name, log_level="WARNING")

    @server.tool()
    def toy_add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    for tool_name in extra_tool_names:
        server.add_tool(
            _named_tool(tool_name),
            name=tool_name,
            description=f"Toy stand-in for {tool_name}.",
        )
    return server


def _named_tool(tool_name: str):
    def tool() -> str:
        return tool_name

    return tool


class _RecordingApp:
    """ASGI wrapper that records every HTTP request before passing it on."""

    def __init__(self, app: Any, sink: list[dict[str, Any]], lock: threading.Lock) -> None:
        self._app = app
        self._sink = sink
        self._lock = lock

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            with self._lock:
                self._sink.append(
                    {"method": scope.get("method"), "path": scope.get("path"), "headers": headers}
                )
        await self._app(scope, receive, send)


class ToyHttpServer:
    """Serve an ``MCPServer`` over streamable HTTP (``/mcp``) or SSE (``/sse``)."""

    def __init__(self, server: MCPServer, *, transport: ToyTransport) -> None:
        if transport not in ("http", "sse"):
            raise ValueError(f"Unsupported toy transport: {transport!r}")
        self._mcp_server = server
        self._transport = transport
        self._requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ToyHttpServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        if self._socket is None:
            raise RuntimeError("The toy server is not running.")
        return int(self._socket.getsockname()[1])

    @property
    def url(self) -> str:
        path = "/mcp" if self._transport == "http" else "/sse"
        return f"http://127.0.0.1:{self.port}{path}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._requests]

    def start(self) -> None:
        if self._transport == "http":
            app = self._mcp_server.streamable_http_app()
        else:
            app = self._mcp_server.sse_app()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        self._socket = listener
        config = uvicorn.Config(
            _RecordingApp(app, self._requests, self._lock),
            log_config=None,
            log_level="warning",
            access_log=False,
            ws="none",
            lifespan="on",
            timeout_graceful_shutdown=2,
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._uvicorn.run,
            kwargs={"sockets": [listener]},
            name=f"toy-mcp-{self._transport}",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._uvicorn.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("The toy MCP server did not start.")
            time.sleep(0.01)

    def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        if self._socket is not None:
            self._socket.close()
