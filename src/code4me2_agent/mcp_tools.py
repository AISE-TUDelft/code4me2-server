from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Callable, Literal
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from code4me2_agent.acp_utils import capability_value

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

McpTransport = Literal["stdio", "http", "sse", "unsupported"]
McpToolAccess = Literal["read", "execute", "edit", "other"]

_MCP_STARTUP_TIMEOUT_SECONDS = 30.0
_MCP_CALL_TIMEOUT_SECONDS = 120.0
_MCP_SHUTDOWN_TIMEOUT_SECONDS = 10.0
# Timeout for HTTP connects and plain request/response exchanges.
_MCP_HTTP_TIMEOUT_SECONDS = 30.0
# Streamable HTTP responses may be held open as event streams while a tool runs.
_MCP_HTTP_READ_TIMEOUT_SECONDS = 300.0
# An SSE session carries every response on one long-lived stream; a read timeout
# there would end an idle session, so it is only a last-resort bound.
_MCP_SSE_READ_TIMEOUT_SECONDS = 24 * 60 * 60.0
_MAX_TOOL_LIST_PAGES = 100
_MAX_TOOLS_PER_SERVER = 40
_MAX_ERROR_LENGTH = 200
_MIN_REDACTED_SECRET_LENGTH = 3
_OPENAI_TOOL_NAME_LIMIT = 64
_UNSAFE_TOOL_NAME_CHARACTER_RE = re.compile(r"[^A-Za-z0-9_-]+")
_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# A server is the IntelliJ MCP server when its tool list contains at least three
# of these names; for that server only the curated tools below are exposed.
_INTELLIJ_SIGNATURE_TOOLS = frozenset(
    {
        "get_file_problems",
        "get_run_configurations",
        "execute_run_configuration",
        "search_in_files_by_text",
        "get_symbol_info",
        "rename_refactoring",
        "build_project",
        "lint_files",
        "reformat_file",
        "get_project_modules",
    }
)
_INTELLIJ_SIGNATURE_MINIMUM = 3
_INTELLIJ_TOOL_ACCESS: dict[str, McpToolAccess] = {
    **dict.fromkeys(
        (
            "get_file_problems",
            "lint_files",
            "search_symbol",
            "get_symbol_info",
            "analyze_calls",
            "get_run_configurations",
            "get_project_problems",
            "git_status",
            "get_project_modules",
            "get_project_dependencies",
        ),
        "read",
    ),
    **dict.fromkeys(("execute_run_configuration", "build_project"), "execute"),
    **dict.fromkeys(("rename_refactoring", "reformat_file"), "edit"),
}


@dataclass(frozen=True)
class _McpTool:
    public_name: str
    server_name: str
    remote_name: str
    definition: dict[str, Any]
    access: McpToolAccess = "other"


@dataclass(frozen=True)
class _ServerSpec:
    """A validated ACP MCP entry. Never log it: it carries headers and env values."""

    name: str
    transport: McpTransport
    index: int = 0
    stdio_parameters: StdioServerParameters | None = field(default=None, repr=False)
    url: str = field(default="", repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def client_target(self) -> object:
        if self.transport == "stdio":
            return self.stdio_parameters
        if self.transport == "http":
            return _streamable_http_transport(self.url, self.headers)
        return sse_client(
            self.url,
            headers=dict(self.headers),
            timeout=_MCP_HTTP_TIMEOUT_SECONDS,
            sse_read_timeout=_MCP_SSE_READ_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True)
class _StartupOutcome:
    client: Client | None = None
    tools: list[Any] = field(default_factory=list)
    error: str | None = None


class _ServerConfigError(ValueError):
    """An ACP MCP entry that cannot be used; the message never contains values."""


class McpServerStartupError(RuntimeError):
    """No ACP-provided MCP server could be used; ``failures`` says why for each one."""

    def __init__(self, failures: list[dict[str, str]]) -> None:
        self.failures = [dict(item) for item in failures]
        summary = "; ".join(
            f"{item['name'] or '<unnamed>'} ({item['transport']}): {item['error']}"
            for item in self.failures
        )
        super().__init__(
            f"No ACP-provided MCP server could be started: {summary or 'no server entries'}"
        )


def _legacy_mcp_client(target: object) -> Client:
    """Build a client that uses the pre-2026 ``initialize`` handshake.

    ``target`` is a ``StdioServerParameters`` for stdio entries and a transport
    async context manager for streamable HTTP and SSE entries.
    """

    return Client(target, mode="legacy")  # type: ignore[arg-type]


@asynccontextmanager
async def _streamable_http_transport(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
    # The HTTP client's default headers are sent on every request of the session.
    async with httpx2.AsyncClient(
        headers=dict(headers),
        timeout=httpx2.Timeout(_MCP_HTTP_TIMEOUT_SECONDS, read=_MCP_HTTP_READ_TIMEOUT_SECONDS),
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams


class StdioMcpToolBroker:
    """Own ACP-provided MCP clients and expose their tools to the synchronous agent loop.

    Entries may use stdio, streamable HTTP or SSE (see ``mcp_server_transport``).
    Each server connects in its own task with its own startup deadline: a server
    that cannot be started, connected or listed is skipped and reported through
    ``failures`` while the others stay usable.

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
        client_factory: Callable[[Any], Client] = _legacy_mcp_client,
        startup_timeout_seconds: float = _MCP_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._servers = list(servers)
        self._cwd = cwd
        self._client_factory = client_factory
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        # Leave headroom inside the overall startup bound to publish the servers
        # that did connect before ``open`` stops waiting.
        self._connect_timeout_seconds = max(
            0.05,
            self._startup_timeout_seconds - min(1.0, self._startup_timeout_seconds / 10),
        )
        self._ready = Event()
        self._closed = Event()
        self._close_flag = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._close_requested: asyncio.Event | None = None
        self._startup_error: BaseException | None = None
        self._tools: dict[str, _McpTool] = {}
        self._clients: dict[str, Client] = {}
        self._failures: list[tuple[int, dict[str, str]]] = []
        self._connected: list[dict[str, Any]] = []
        self._unavailable: set[str] = set()
        self._startup_scopes: dict[str, anyio.CancelScope] = {}
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
        client_factory: Callable[[Any], Client] = _legacy_mcp_client,
        startup_timeout_seconds: float = _MCP_STARTUP_TIMEOUT_SECONDS,
    ) -> StdioMcpToolBroker | None:
        """Connect every entry and return a broker, or None when ``servers`` is empty.

        Servers that fail are skipped and listed in ``failures``. When not a
        single server connects, the broker is closed and ``McpServerStartupError``
        (a ``RuntimeError`` carrying ``failures``) is raised instead of returning
        an empty broker. ``TimeoutError`` is raised only if the owner thread misses
        the overall startup bound.
        """

        if not servers:
            return None
        broker = cls(
            servers,
            cwd=cwd,
            client_factory=client_factory,
            startup_timeout_seconds=startup_timeout_seconds,
        )
        broker._thread.start()
        if not broker._ready.wait(timeout=startup_timeout_seconds):
            broker.close()
            raise TimeoutError("Timed out while starting ACP-provided MCP servers.")
        if broker._startup_error is not None:
            broker.close()
            raise RuntimeError(
                "Could not start ACP-provided MCP servers: "
                f"{type(broker._startup_error).__name__}"
            ) from broker._startup_error
        if not broker._clients:
            failures = broker.failures
            broker.close()
            raise McpServerStartupError(failures)
        return broker

    @property
    def failures(self) -> list[dict[str, str]]:
        """Servers skipped at startup as ``{"name", "transport", "error"}`` (log-safe)."""

        return [dict(item) for _index, item in sorted(self._failures, key=lambda pair: pair[0])]

    @property
    def connected_servers(self) -> list[dict[str, Any]]:
        """Connected servers as ``{"name", "transport", "tool_count", "dropped_tool_count", "intellij"}``."""

        return [dict(item) for item in self._connected]

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition for tool in self._tools.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def tool_access(self, name: str) -> McpToolAccess:
        """``read``/``execute``/``edit`` for curated IntelliJ tools, ``other`` for any other name."""

        tool = self._tools.get(name)
        return tool.access if tool is not None else "other"

    def is_read_only(self, name: str) -> bool:
        return self.tool_access(name) == "read"

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown MCP tool: {name}")
        loop = self._loop
        if (
            loop is None
            or loop.is_closed()
            or self._closed.is_set()
            or tool.server_name in self._unavailable
        ):
            raise RuntimeError(f"MCP server {tool.server_name!r} is no longer available.")
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool(tool, arguments),
            loop,
        )
        try:
            return future.result(timeout=_MCP_CALL_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"MCP tool {tool.remote_name!r} on server {tool.server_name!r} timed out."
            ) from None

    def close(self) -> None:
        # Set before reading the loop: an owner loop that is not running yet sees
        # the flag once its startup ends instead of waiting for a signal forever.
        self._close_flag.set()
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
        stop_servers = asyncio.Event()
        specs = self._resolve_specs()
        outcomes: dict[str, asyncio.Future[_StartupOutcome]] = {}
        tasks: list[asyncio.Task[None]] = []
        try:
            for spec in specs:
                outcome: asyncio.Future[_StartupOutcome] = self._loop.create_future()
                outcomes[spec.name] = outcome
                tasks.append(
                    asyncio.create_task(
                        self._serve(spec, outcome, stop_servers),
                        name=f"code4me2-mcp-{spec.transport}",
                    )
                )
            if outcomes:
                await asyncio.wait(
                    list(outcomes.values()), timeout=self._connect_timeout_seconds
                )
            self._publish(specs, outcomes)
            self._ready.set()
            if not self._close_flag.is_set():
                await self._close_requested.wait()
        finally:
            stop_servers.set()
            for scope in list(self._startup_scopes.values()):
                scope.cancel()
            if tasks:
                done, _pending = await asyncio.wait(
                    tasks, timeout=_MCP_SHUTDOWN_TIMEOUT_SECONDS
                )
                for task in done:
                    if not task.cancelled():
                        task.exception()

    def _resolve_specs(self) -> list[_ServerSpec]:
        specs: list[_ServerSpec] = []
        names: set[str] = set()
        for index, server in enumerate(self._servers):
            name = _server_name(server)
            transport = mcp_server_transport(server)
            try:
                spec = _server_spec(
                    server, index=index, name=name, transport=transport, cwd=self._cwd
                )
            except _ServerConfigError as exc:
                self._record_failure(index, name, transport, str(exc))
                continue
            if name in names:
                self._record_failure(index, name, transport, "duplicate MCP server name")
                continue
            names.add(name)
            specs.append(spec)
        if any(spec.transport in ("http", "sse") for spec in specs):
            _quiet_http_request_logs()
        return specs

    async def _serve(
        self,
        spec: _ServerSpec,
        outcome: asyncio.Future[_StartupOutcome],
        stop_servers: asyncio.Event,
    ) -> None:
        """Connect one server, report the outcome, then hold it open until close."""

        connected = False
        scope = anyio.CancelScope(
            deadline=anyio.current_time() + self._connect_timeout_seconds
        )
        self._startup_scopes[spec.name] = scope
        try:
            with scope:
                async with self._client_factory(spec.client_target()) as client:
                    tools = await _list_all_tools(client)
                    scope.deadline = math.inf
                    self._startup_scopes.pop(spec.name, None)
                    if outcome.done():
                        return
                    connected = True
                    outcome.set_result(_StartupOutcome(client=client, tools=tools))
                    await stop_servers.wait()
        except Exception as exc:  # noqa: BLE001 - one server must not stop the others
            error = _describe_error(exc, spec.secrets)
            if not connected:
                if not outcome.done():
                    outcome.set_result(_StartupOutcome(error=error))
                return
            self._unavailable.add(spec.name)
            if stop_servers.is_set():
                logger.debug(
                    "ACP-provided MCP server %r (%s) closed with: %s",
                    spec.name,
                    spec.transport,
                    error,
                )
            else:
                logger.warning(
                    "ACP-provided MCP server %r (%s) stopped: %s",
                    spec.name,
                    spec.transport,
                    error,
                )
            return
        finally:
            self._startup_scopes.pop(spec.name, None)
        if not connected and not outcome.done():
            outcome.set_result(_StartupOutcome(error=self._timeout_error()))

    def _publish(
        self,
        specs: list[_ServerSpec],
        outcomes: dict[str, asyncio.Future[_StartupOutcome]],
    ) -> None:
        for spec in specs:
            outcome = outcomes[spec.name]
            if not outcome.done():
                outcome.set_result(_StartupOutcome(error=self._timeout_error()))
                scope = self._startup_scopes.get(spec.name)
                if scope is not None:
                    scope.cancel()
            result = outcome.result()
            if result.client is None:
                self._record_failure(
                    spec.index, spec.name, spec.transport, result.error or "unknown error"
                )
                continue
            self._clients[spec.name] = result.client
            self._register_tools(spec, result.tools)

    def _register_tools(self, spec: _ServerSpec, tools: list[Any]) -> None:
        listed = [tool for tool in tools if str(getattr(tool, "name", "") or "")]
        remote_names = {str(tool.name) for tool in listed}
        intellij = len(remote_names & _INTELLIJ_SIGNATURE_TOOLS) >= _INTELLIJ_SIGNATURE_MINIMUM
        if intellij:
            selected = [tool for tool in listed if str(tool.name) in _INTELLIJ_TOOL_ACCESS]
            logger.info(
                "MCP server %r (%s) looks like the IntelliJ MCP server; exposing %d curated "
                "tool(s) and dropping %d.",
                spec.name,
                spec.transport,
                len(selected),
                len(listed) - len(selected),
            )
        else:
            selected = listed
        selected = sorted(selected, key=lambda tool: str(tool.name))
        if len(selected) > _MAX_TOOLS_PER_SERVER:
            logger.warning(
                "MCP server %r (%s) offers %d tools; keeping the first %d by name and "
                "dropping: %s",
                spec.name,
                spec.transport,
                len(selected),
                _MAX_TOOLS_PER_SERVER,
                ", ".join(str(tool.name) for tool in selected[_MAX_TOOLS_PER_SERVER:]),
            )
            selected = selected[:_MAX_TOOLS_PER_SERVER]
        exposed = 0
        for tool in selected:
            remote_name = str(tool.name)
            public_name = _public_tool_name(spec.name, remote_name)
            if public_name in self._tools:
                logger.warning(
                    "Skipping MCP tool %r on server %r: its public name %r is already taken.",
                    remote_name,
                    spec.name,
                    public_name,
                )
                continue
            parameters = dict(tool.input_schema or {"type": "object"})
            description = str(tool.description or tool.title or remote_name)
            self._tools[public_name] = _McpTool(
                public_name=public_name,
                server_name=spec.name,
                remote_name=remote_name,
                definition={
                    "type": "function",
                    "function": {
                        "name": public_name,
                        "description": f"MCP server {spec.name}: {description}",
                        "parameters": parameters,
                    },
                },
                access=_INTELLIJ_TOOL_ACCESS.get(remote_name, "other") if intellij else "other",
            )
            exposed += 1
        self._connected.append(
            {
                "name": spec.name,
                "transport": spec.transport,
                "tool_count": exposed,
                "dropped_tool_count": len(listed) - exposed,
                "intellij": intellij,
            }
        )

    def _record_failure(self, index: int, name: str, transport: str, error: str) -> None:
        self._failures.append((index, {"name": name, "transport": transport, "error": error}))
        logger.warning(
            "Skipping ACP-provided MCP server %r (%s): %s", name or "<unnamed>", transport, error
        )

    def _timeout_error(self) -> str:
        return f"timed out after {self._connect_timeout_seconds:g}s while connecting"

    async def _call_tool(
        self,
        tool: _McpTool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        client = self._clients.get(tool.server_name)
        if client is None or tool.server_name in self._unavailable:
            raise RuntimeError(f"MCP server {tool.server_name!r} is no longer available.")
        result = await client.call_tool(tool.remote_name, arguments)
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        return {
            "status": "failed" if result.is_error else "completed",
            "server_name": tool.server_name,
            "tool_name": tool.remote_name,
            "content": payload.get("content", []),
            "structured_content": payload.get("structuredContent"),
        }


McpToolBroker = StdioMcpToolBroker


async def _list_all_tools(client: Client) -> list[Any]:
    tools: list[Any] = []
    cursor: str | None = None
    for _page in range(_MAX_TOOL_LIST_PAGES):
        result = await client.list_tools(cursor=cursor)
        tools.extend(result.tools)
        cursor = result.next_cursor
        if cursor is None:
            break
    return tools


def mcp_server_transport(server: object) -> McpTransport:
    """Classify an ACP ``session/new`` MCP entry (pydantic model or dict).

    stdio when ``command`` is set; http when ``type == "http"`` or the entry is an
    ACP HTTP server model; sse when ``type == "sse"`` or an ACP SSE server model;
    anything else (ACP-transport servers, bare URLs) is unsupported.
    """

    if capability_value(server, "command"):
        return "stdio"
    kind = capability_value(server, "type")
    kind_text = kind.strip().lower() if isinstance(kind, str) else ""
    model_names = {cls.__name__ for cls in type(server).__mro__}
    if kind_text == "http" or model_names & {"McpServerHttp", "HttpMcpServer"}:
        return "http"
    if kind_text == "sse" or model_names & {"McpServerSse", "SseMcpServer"}:
        return "sse"
    return "unsupported"


def _server_name(server: object) -> str:
    name = capability_value(server, "name")
    return name.strip() if isinstance(name, str) else ""


def _server_spec(
    server: object,
    *,
    index: int,
    name: str,
    transport: McpTransport,
    cwd: Path,
) -> _ServerSpec:
    if transport == "unsupported":
        raise _ServerConfigError(
            "unsupported MCP transport; only stdio, http and sse entries are supported"
        )
    if not name:
        raise _ServerConfigError("MCP server entry has no name")
    if transport == "stdio":
        try:
            _name, parameters = _stdio_server_parameters(server, cwd=cwd)
        except ValueError:
            raise _ServerConfigError("invalid stdio MCP server configuration") from None
        secrets = tuple((parameters.env or {}).values())
        return _ServerSpec(
            name=name,
            transport=transport,
            index=index,
            stdio_parameters=parameters,
            secrets=secrets,
        )
    url = capability_value(server, "url")
    if not isinstance(url, str) or not url.strip():
        raise _ServerConfigError("MCP server entry has no URL")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        raise _ServerConfigError("invalid MCP server URL") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise _ServerConfigError("MCP server URL must be an absolute http(s) URL")
    headers = _http_headers(capability_value(server, "headers"))
    secrets = (
        *headers.values(),
        url,
        parts.netloc.rpartition("@")[0],
        parts.username or "",
        parts.password or "",
        parts.path,
        parts.query,
        parts.fragment,
    )
    return _ServerSpec(
        name=name,
        transport=transport,
        index=index,
        url=url,
        headers=headers,
        secrets=tuple(secret for secret in secrets if secret),
    )


def _http_headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = [
            (capability_value(item, "name"), capability_value(item, "value")) for item in value
        ]
    else:
        raise _ServerConfigError("invalid MCP server headers")
    headers: dict[str, str] = {}
    for key, header_value in items:
        if not isinstance(key, str) or not _HEADER_NAME_RE.match(key.strip()):
            raise _ServerConfigError("invalid MCP server header name")
        if header_value is None or any(
            character in str(header_value) for character in ("\r", "\n", "\0")
        ):
            raise _ServerConfigError(f"invalid value for MCP server header {key.strip()!r}")
        headers[key.strip()] = str(header_value)
    return headers


def _stdio_server_parameters(
    server: object,
    *,
    cwd: Path,
) -> tuple[str, StdioServerParameters]:
    name = str(capability_value(server, "name") or "").strip()
    command = str(capability_value(server, "command") or "").strip()
    if not name or not command:
        raise ValueError("Named stdio MCP servers need a name and a command.")
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


def _quiet_http_request_logs() -> None:
    # httpx2 logs every request URL at INFO; MCP URLs may carry tokens in the query.
    http_logger = logging.getLogger("httpx2")
    if http_logger.level == logging.NOTSET:
        http_logger.setLevel(logging.WARNING)


def _describe_error(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """A short, log-safe description: no header/env values and no full URLs."""

    leaf = _leaf_exception(exc)
    if isinstance(leaf, httpx2.HTTPStatusError):
        text = f"HTTP {leaf.response.status_code}"
    else:
        message = " ".join(str(leaf).split())
        text = f"{type(leaf).__name__}: {message}" if message else type(leaf).__name__
    for secret in sorted(
        {secret for secret in secrets if len(secret) >= _MIN_REDACTED_SECRET_LENGTH},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    text = _URL_IN_TEXT_RE.sub(lambda match: _url_origin(match.group(0)) or "<url>", text)
    if len(text) > _MAX_ERROR_LENGTH:
        text = text[: _MAX_ERROR_LENGTH - 3] + "..."
    return text


def _leaf_exception(exc: BaseException) -> BaseException:
    """The first informative member of (nested) exception groups raised by task groups."""

    while True:
        members = getattr(exc, "exceptions", None)
        if not isinstance(members, tuple) or not members:
            return exc
        informative = [
            member for member in members if not isinstance(member, asyncio.CancelledError)
        ]
        exc = (informative or list(members))[0]


def _url_origin(url: object) -> str | None:
    """``scheme://host`` of a URL, without credentials, port, path or query."""

    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}"


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
    """Return a log-safe description: name, transport and, for HTTP/SSE, the URL's
    ``scheme://host`` only. Never commands, args, env values, headers or full URLs."""

    summaries = []
    for server in servers or []:
        transport = mcp_server_transport(server)
        name = capability_value(server, "name")
        summary: dict[str, Any] = {
            "name": name if name is None or isinstance(name, str) else str(name),
            "transport": transport,
        }
        if transport in ("http", "sse"):
            origin = _url_origin(capability_value(server, "url"))
            if origin is not None:
                summary["endpoint"] = origin
        summaries.append(summary)
    return json.dumps(summaries, sort_keys=True)
