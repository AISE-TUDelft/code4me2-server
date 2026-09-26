"""MCP broker: streamable HTTP and SSE entries, per-server isolation, IntelliJ curation.

Toy servers run in-process on ephemeral 127.0.0.1 ports (``mcp_http_toy_server``);
the stdio toy is the existing ``mcp_toy_server.py``. No network beyond localhost.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import time
from pathlib import Path

import pytest
from acp.schema import (
    AcpMcpServer,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    McpServerHttp,
    McpServerSse,
    McpServerStdio,
    SseMcpServer,
)
from mcp import Client, StdioServerParameters
from mcp_http_toy_server import ToyHttpServer, build_toy_server

from code4me2_agent.mcp_tools import (
    McpServerStartupError,
    McpToolBroker,
    StdioMcpToolBroker,
    mcp_server_transport,
    serialize_mcp_servers,
)

STDIO_TOY_SERVER = Path(__file__).with_name("mcp_toy_server.py")
BROKER_LOGGER = "code4me2_agent.mcp_tools"

INTELLIJ_READ_TOOLS = {
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
}
INTELLIJ_EXECUTE_TOOLS = {"execute_run_configuration", "build_project"}
INTELLIJ_EDIT_TOOLS = {"rename_refactoring", "reformat_file"}
INTELLIJ_CURATED_TOOLS = INTELLIJ_READ_TOOLS | INTELLIJ_EXECUTE_TOOLS | INTELLIJ_EDIT_TOOLS
INTELLIJ_DROPPED_TOOLS = {
    "execute_terminal_command",
    "get_file_text_by_path",
    "replace_text_in_file",
    "create_new_file",
    "find_files_by_glob",
    "find_files_by_name_keyword",
    "get_all_open_file_paths",
    "list_directory_tree",
    "open_file_in_editor",
    "search_in_files_by_regex",
    "search_in_files_by_text",
    "get_repositories",
    "runNotebookCell",
    "xdebug_start_debugger_session",
    "list_database_connections",
    "execute_sql_query",
}


def _tool_names(broker: StdioMcpToolBroker) -> list[str]:
    return [definition["function"]["name"] for definition in broker.definitions()]


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


@pytest.fixture(autouse=True)
def _broker_logger_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Alembic's fileConfig (run by database tests in the same session) disables
    # every existing logger; the log assertions here need the broker's logger.
    monkeypatch.setattr(logging.getLogger(BROKER_LOGGER), "disabled", False)


def _broker_messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name == BROKER_LOGGER
    )


def _close(broker: StdioMcpToolBroker) -> None:
    broker.close()
    assert not broker._thread.is_alive()


def _entry(transport: str, name: str, url: str, headers: dict[str, str], *, as_model: bool):
    if as_model:
        header_models = [HttpHeader(name=key, value=value) for key, value in headers.items()]
        model = HttpMcpServer if transport == "http" else SseMcpServer
        return model(type=transport, name=name, url=url, headers=header_models)
    return {
        "type": transport,
        "name": name,
        "url": url,
        "headers": [{"name": key, "value": value} for key, value in headers.items()],
    }


def test_transport_detection_covers_acp_models_and_dicts() -> None:
    assert mcp_server_transport({"name": "a", "command": "srv", "args": [], "env": []}) == "stdio"
    assert mcp_server_transport(McpServerStdio(name="a", command="srv", args=[], env=[])) == "stdio"
    # A command wins over a declared type, as in the ACP stdio shape.
    assert mcp_server_transport({"type": "http", "name": "a", "command": "srv"}) == "stdio"
    assert mcp_server_transport({"type": "http", "name": "a", "url": "http://h"}) == "http"
    assert mcp_server_transport({"type": "HTTP", "name": "a", "url": "http://h"}) == "http"
    assert (
        mcp_server_transport(HttpMcpServer(type="http", name="a", url="http://h", headers=[]))
        == "http"
    )
    assert mcp_server_transport(McpServerHttp(name="a", url="http://h", headers=[])) == "http"
    assert mcp_server_transport({"type": "sse", "name": "a", "url": "http://h"}) == "sse"
    assert (
        mcp_server_transport(SseMcpServer(type="sse", name="a", url="http://h", headers=[]))
        == "sse"
    )
    assert mcp_server_transport(McpServerSse(name="a", url="http://h", headers=[])) == "sse"
    assert mcp_server_transport(AcpMcpServer(type="acp", name="a", server_id="x")) == "unsupported"
    assert mcp_server_transport({"name": "a", "url": "http://h"}) == "unsupported"
    assert McpToolBroker is StdioMcpToolBroker


def test_empty_server_list_opens_no_broker(tmp_path: Path) -> None:
    assert StdioMcpToolBroker.open([], cwd=tmp_path) is None


@pytest.mark.parametrize(
    ("transport", "as_model"),
    [("http", True), ("http", False), ("sse", True), ("sse", False)],
)
def test_remote_server_lists_and_calls_tools_with_headers_on_every_request(
    tmp_path: Path, transport: str, as_model: bool
) -> None:
    headers = {"Authorization": "Bearer header-secret-7f3a", "X-Code4Me-Probe": "probe-91c2"}
    with ToyHttpServer(build_toy_server("remote"), transport=transport) as server:
        entry = _entry(transport, "remote", server.url, headers, as_model=as_model)
        broker = StdioMcpToolBroker.open([entry], cwd=tmp_path, startup_timeout_seconds=15)
        assert broker is not None
        try:
            assert _tool_names(broker) == ["mcp__remote__toy_add"]
            assert broker.failures == []
            assert broker.connected_servers == [
                {
                    "name": "remote",
                    "transport": transport,
                    "tool_count": 1,
                    "dropped_tool_count": 0,
                    "intellij": False,
                }
            ]
            result = broker.execute("mcp__remote__toy_add", {"a": 2, "b": 3})
            assert result["status"] == "completed"
            assert result["server_name"] == "remote"
            assert result["tool_name"] == "toy_add"
            assert result["structured_content"] == {"result": 5}
            assert broker.tool_access("mcp__remote__toy_add") == "other"
            assert broker.is_read_only("mcp__remote__toy_add") is False
        finally:
            _close(broker)
        requests = server.requests

    # initialize, notifications/initialized, tools/list, tools/call at the least.
    assert len(requests) >= 4
    if transport == "sse":
        assert {request["path"] for request in requests} >= {"/sse", "/messages/"}
    for request in requests:
        assert request["headers"].get("authorization") == headers["Authorization"], request
        assert request["headers"].get("x-code4me-probe") == headers["X-Code4Me-Probe"], request


def test_failing_servers_are_skipped_reported_and_never_leak_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=BROKER_LOGGER)
    header_secret = "Bearer header-secret-2b8e"
    env_secret = "env-secret-5d1c"
    password_secret = "password-secret-9a4f"
    query_secret = "query-secret-0c7d"
    with ToyHttpServer(build_toy_server("good"), transport="http") as good, ToyHttpServer(
        build_toy_server("other"), transport="sse"
    ) as other:
        refused_url = (
            f"http://user:{password_secret}@127.0.0.1:{_closed_port()}/mcp?token={query_secret}"
        )
        servers = [
            {
                "type": "http",
                "name": "good",
                "url": good.url,
                "headers": [{"name": "Authorization", "value": header_secret}],
            },
            {
                "type": "http",
                "name": "refused",
                "url": refused_url,
                "headers": [{"name": "Authorization", "value": header_secret}],
            },
            SseMcpServer(
                type="sse",
                name="missing-endpoint",
                url=other.url.replace("/sse", "/not-an-endpoint"),
                headers=[HttpHeader(name="X-Api-Key", value=header_secret)],
            ),
            McpServerStdio(
                name="no-binary",
                command="/nonexistent-mcp-binary-for-code4me-tests",
                args=[],
                env=[EnvVariable(name="API_TOKEN", value=env_secret)],
            ),
            {
                "name": "toy",
                "command": sys.executable,
                "args": [str(STDIO_TOY_SERVER)],
                "env": [{"name": "API_TOKEN", "value": env_secret}],
            },
            AcpMcpServer(type="acp", name="acp-only", server_id="srv-1"),
            {"type": "http", "name": "good", "url": good.url, "headers": []},
            {
                "type": "sse",
                "name": "bad-header",
                "url": other.url,
                "headers": [{"name": "X-Api-Key", "value": f"{header_secret}\r\nX-Injected: 1"}],
            },
            {"type": "http", "name": "no-url", "headers": []},
        ]
        broker = StdioMcpToolBroker.open(servers, cwd=tmp_path, startup_timeout_seconds=15)
        assert broker is not None
        try:
            assert _tool_names(broker) == ["mcp__good__toy_add", "mcp__toy__toy_add"]
            assert broker.execute("mcp__good__toy_add", {"a": 1, "b": 1})["status"] == "completed"
            assert broker.execute("mcp__toy__toy_add", {"a": 2, "b": 2})["status"] == "completed"
            failures = broker.failures
        finally:
            _close(broker)
        good_requests = good.requests

    by_name = {(failure["name"], failure["transport"]): failure["error"] for failure in failures}
    assert set(by_name) == {
        ("refused", "http"),
        ("missing-endpoint", "sse"),
        ("no-binary", "stdio"),
        ("acp-only", "unsupported"),
        ("good", "http"),
        ("bad-header", "sse"),
        ("no-url", "http"),
    }
    assert [failure["name"] for failure in failures] == [
        "refused",
        "missing-endpoint",
        "no-binary",
        "acp-only",
        "good",
        "bad-header",
        "no-url",
    ]
    assert by_name[("missing-endpoint", "sse")] == "HTTP 404"
    assert "duplicate" in by_name[("good", "http")]
    assert "unsupported" in by_name[("acp-only", "unsupported")]
    assert by_name[("no-binary", "stdio")].startswith("FileNotFoundError")
    for failure in failures:
        assert set(failure) == {"name", "transport", "error"}
        assert 0 < len(failure["error"]) <= 200
    # Only the duplicate-free "good" entry reached the good server.
    assert good_requests
    assert all(
        request["headers"].get("authorization") == header_secret for request in good_requests
    )

    rendered = json.dumps(failures) + "\n" + _broker_messages(caplog)
    for secret in (
        header_secret,
        "header-secret-2b8e",
        env_secret,
        password_secret,
        query_secret,
        refused_url,
        "X-Injected",
    ):
        assert secret not in rendered
    assert "127.0.0.1:" not in json.dumps(failures)


def test_hung_server_times_out_without_blocking_the_others(tmp_path: Path) -> None:
    hung = socket.socket()
    hung.bind(("127.0.0.1", 0))
    hung.listen(8)  # accepts TCP connections but never answers
    try:
        with ToyHttpServer(build_toy_server("alive"), transport="sse") as alive:
            servers = [
                {
                    "type": "http",
                    "name": "hung",
                    "url": f"http://127.0.0.1:{hung.getsockname()[1]}/mcp",
                    "headers": [],
                },
                {"type": "sse", "name": "alive", "url": alive.url, "headers": []},
            ]
            started = time.monotonic()
            broker = StdioMcpToolBroker.open(servers, cwd=tmp_path, startup_timeout_seconds=2.0)
            elapsed = time.monotonic() - started
            assert broker is not None
            try:
                # Bounded by the startup timeout, not by the hung server.
                assert elapsed < 5.0
                assert _tool_names(broker) == ["mcp__alive__toy_add"]
                assert [(item["name"], item["transport"]) for item in broker.failures] == [
                    ("hung", "http")
                ]
                assert "timed out" in broker.failures[0]["error"]
                result = broker.execute("mcp__alive__toy_add", {"a": 4, "b": 5})
                assert result["structured_content"] == {"result": 9}
            finally:
                _close(broker)
    finally:
        hung.close()


def test_client_factory_receives_stdio_parameters_or_a_transport(tmp_path: Path) -> None:
    targets: list[object] = []

    def recording_factory(target: object) -> Client:
        targets.append(target)
        return Client(target, mode="legacy")

    with ToyHttpServer(build_toy_server("remote"), transport="http") as remote:
        servers = [
            {
                "name": "toy",
                "command": sys.executable,
                "args": [str(STDIO_TOY_SERVER)],
                "env": [],
            },
            {"type": "http", "name": "remote", "url": remote.url, "headers": []},
        ]
        broker = StdioMcpToolBroker.open(
            servers,
            cwd=tmp_path,
            client_factory=recording_factory,
            startup_timeout_seconds=15,
        )
        assert broker is not None
        try:
            assert _tool_names(broker) == ["mcp__toy__toy_add", "mcp__remote__toy_add"]
        finally:
            _close(broker)

    stdio_targets = [target for target in targets if isinstance(target, StdioServerParameters)]
    assert len(stdio_targets) == 1
    assert stdio_targets[0].cwd == tmp_path
    assert stdio_targets[0].args == [str(STDIO_TOY_SERVER)]
    transports = [target for target in targets if not isinstance(target, StdioServerParameters)]
    assert len(transports) == 1
    assert hasattr(transports[0], "__aenter__") and hasattr(transports[0], "__aexit__")


def test_all_servers_failing_raises_startup_error_with_failures(tmp_path: Path) -> None:
    secret = "header-secret-44e1"
    servers = [
        {
            "type": "http",
            "name": "refused",
            "url": f"http://127.0.0.1:{_closed_port()}/mcp",
            "headers": [{"name": "Authorization", "value": secret}],
        },
        {
            "name": "no-binary",
            "command": "/nonexistent-mcp-binary-for-code4me-tests",
            "args": [],
            "env": [{"name": "API_TOKEN", "value": secret}],
        },
    ]
    with pytest.raises(McpServerStartupError) as raised:
        StdioMcpToolBroker.open(servers, cwd=tmp_path, startup_timeout_seconds=15)

    error = raised.value
    assert isinstance(error, RuntimeError)
    assert [(item["name"], item["transport"]) for item in error.failures] == [
        ("refused", "http"),
        ("no-binary", "stdio"),
    ]
    assert "refused (http)" in str(error)
    assert secret not in str(error)
    assert secret not in json.dumps(error.failures)


def test_intellij_server_is_curated_and_classified(tmp_path: Path) -> None:
    full_tools = sorted(INTELLIJ_CURATED_TOOLS | INTELLIJ_DROPPED_TOOLS)
    # An older IDE build: three signature tools, only some curated ones.
    partial_tools = [
        "get_file_problems",
        "get_run_configurations",
        "execute_run_configuration",
        "search_in_files_by_text",
        "get_file_text_by_path",
        "execute_terminal_command",
    ]
    # Two signature names are not enough to be treated as IntelliJ.
    lookalike_tools = ["get_file_problems", "lint_files", "execute_terminal_command"]
    with ToyHttpServer(
        build_toy_server("idea", extra_tool_names=full_tools), transport="sse"
    ) as idea, ToyHttpServer(
        build_toy_server("idea-old", extra_tool_names=partial_tools), transport="http"
    ) as idea_old, ToyHttpServer(
        build_toy_server("lookalike", extra_tool_names=lookalike_tools), transport="http"
    ) as lookalike:
        servers = [
            {"type": "sse", "name": "idea", "url": idea.url, "headers": []},
            {"type": "http", "name": "idea-old", "url": idea_old.url, "headers": []},
            {"type": "http", "name": "lookalike", "url": lookalike.url, "headers": []},
        ]
        broker = StdioMcpToolBroker.open(servers, cwd=tmp_path, startup_timeout_seconds=15)
        assert broker is not None
        try:
            names = _tool_names(broker)
            idea_names = [name for name in names if name.startswith("mcp__idea__")]
            old_names = [name for name in names if name.startswith("mcp__idea-old__")]
            lookalike_names = [name for name in names if name.startswith("mcp__lookalike__")]
            assert idea_names == [f"mcp__idea__{tool}" for tool in sorted(INTELLIJ_CURATED_TOOLS)]
            assert old_names == [
                "mcp__idea-old__execute_run_configuration",
                "mcp__idea-old__get_file_problems",
                "mcp__idea-old__get_run_configurations",
            ]
            assert lookalike_names == [
                f"mcp__lookalike__{tool}" for tool in sorted([*lookalike_tools, "toy_add"])
            ]
            for tool in INTELLIJ_DROPPED_TOOLS | {"toy_add"}:
                assert not broker.has_tool(f"mcp__idea__{tool}")

            for tool in INTELLIJ_READ_TOOLS:
                assert broker.tool_access(f"mcp__idea__{tool}") == "read"
                assert broker.is_read_only(f"mcp__idea__{tool}") is True
            for tool in INTELLIJ_EXECUTE_TOOLS:
                assert broker.tool_access(f"mcp__idea__{tool}") == "execute"
                assert broker.is_read_only(f"mcp__idea__{tool}") is False
            for tool in INTELLIJ_EDIT_TOOLS:
                assert broker.tool_access(f"mcp__idea__{tool}") == "edit"
                assert broker.is_read_only(f"mcp__idea__{tool}") is False
            assert broker.tool_access("mcp__idea-old__get_file_problems") == "read"
            assert broker.tool_access("mcp__idea-old__execute_run_configuration") == "execute"
            for name in lookalike_names:
                assert broker.tool_access(name) == "other"
                assert broker.is_read_only(name) is False
            assert broker.tool_access("mcp__idea__execute_terminal_command") == "other"
            assert broker.tool_access("not_an_mcp_tool") == "other"

            result = broker.execute("mcp__idea__get_file_problems", {})
            assert result["status"] == "completed"
            assert result["content"][0]["text"] == "get_file_problems"
            summaries = {item["name"]: item for item in broker.connected_servers}
            assert summaries["idea"]["intellij"] is True
            assert summaries["idea"]["tool_count"] == len(INTELLIJ_CURATED_TOOLS)
            assert summaries["idea"]["dropped_tool_count"] == len(INTELLIJ_DROPPED_TOOLS) + 1
            assert summaries["idea-old"]["intellij"] is True
            assert summaries["lookalike"]["intellij"] is False
        finally:
            _close(broker)


def test_other_servers_keep_the_first_forty_tools_by_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=BROKER_LOGGER)
    extra = [f"tool_{index:02d}" for index in range(44, -1, -1)]
    offered = sorted([*extra, "toy_add"])
    with ToyHttpServer(build_toy_server("big", extra_tool_names=extra), transport="http") as big:
        broker = StdioMcpToolBroker.open(
            [{"type": "http", "name": "big", "url": big.url, "headers": []}],
            cwd=tmp_path,
            startup_timeout_seconds=15,
        )
        assert broker is not None
        try:
            names = _tool_names(broker)
            assert names == [f"mcp__big__{tool}" for tool in offered[:40]]
            assert all(broker.tool_access(name) == "other" for name in names)
            assert broker.connected_servers[0]["tool_count"] == 40
            assert broker.connected_servers[0]["dropped_tool_count"] == len(offered) - 40
        finally:
            _close(broker)

    messages = _broker_messages(caplog)
    assert "offers 46 tools" in messages
    for dropped in offered[40:]:
        assert dropped in messages


def test_serialize_mcp_servers_reports_transport_and_origin_only() -> None:
    secrets = [
        "env-secret-61aa",
        "header-secret-73bb",
        "password-secret-85cc",
        "query-secret-97dd",
        "path-secret-a9ee",
        "--token=arg-secret-bbff",
    ]
    servers = [
        {
            "name": "stdio-tool",
            "command": "/opt/tools/server",
            "args": ["--token=arg-secret-bbff"],
            "env": [{"name": "API_TOKEN", "value": "env-secret-61aa"}],
        },
        HttpMcpServer(
            type="http",
            name="remote",
            url="https://user:password-secret-85cc@MCP.Example.test:8443/path-secret-a9ee?t=query-secret-97dd",
            headers=[HttpHeader(name="Authorization", value="header-secret-73bb")],
        ),
        SseMcpServer(
            type="sse",
            name="idea",
            url="http://127.0.0.1:64342/sse",
            headers=[HttpHeader(name="X-Api-Key", value="header-secret-73bb")],
        ),
        {"type": "http", "name": "no-url", "headers": []},
        AcpMcpServer(type="acp", name="acp-only", server_id="srv-1"),
        {"name": "bare-url", "url": "http://127.0.0.1:9/mcp?t=query-secret-97dd"},
    ]

    rendered = serialize_mcp_servers(servers)

    assert json.loads(rendered) == [
        {"name": "stdio-tool", "transport": "stdio"},
        {"endpoint": "https://mcp.example.test", "name": "remote", "transport": "http"},
        {"endpoint": "http://127.0.0.1", "name": "idea", "transport": "sse"},
        {"name": "no-url", "transport": "http"},
        {"name": "acp-only", "transport": "unsupported"},
        {"name": "bare-url", "transport": "unsupported"},
    ]
    for secret in secrets:
        assert secret not in rendered
    assert serialize_mcp_servers(None) == "[]"
