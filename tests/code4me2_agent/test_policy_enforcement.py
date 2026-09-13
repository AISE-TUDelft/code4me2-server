from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code4me2_agent.adapters import (
    OpenAICompatibleReactAdapter,
    ToolCall,
    ToolRegistry,
    ToolRegistryError,
)
from code4me2_agent.config import AgentConfig, MemoryWindowConfig, ServerAgentConfig
from code4me2_agent.echo import EchoAgentCore
from code4me2_agent.events import ApprovalDecision
from code4me2_agent.runtime_auth import (
    AcpBackendAuthorization,
    AcpRuntimeScope,
    AcpSessionExpired,
    ManagedBridgeAuthorization,
)
from code4me2_agent.telemetry import AgentTelemetryRecorder, ServerUploadTelemetrySink


class ApprovalSink:
    def __init__(self, decision: ApprovalDecision):
        self.decision = decision
        self.requests = []
        self.events = []

    def request_approval(self, tool_call, arguments):
        self.requests.append((tool_call, arguments))
        return self.decision

    def tool_call(self, event):
        self.events.append(event)


def test_per_step_policy_keeps_read_tools_automatic():
    file_tools = MagicMock()
    file_tools.read_file.return_value = {"content": "ok"}
    sink = ApprovalSink(ApprovalDecision("rejected"))
    registry = ToolRegistry(
        file_tools,
        MagicMock(),
        event_sink=sink,
        allowed_tools=frozenset({"read_file"}),
        approval_policy="per_step",
    )

    assert registry.execute(
        ToolCall("call-1", "read_file", {"path": "README.md"}),
        run_id="run-1",
        request_id="request-1",
    ) == {"content": "ok"}
    file_tools.read_file.assert_called_once()
    assert sink.requests == []


def test_per_step_policy_executes_mutation_after_allow_once():
    file_tools = MagicMock()
    file_tools.read_file.return_value = SimpleNamespace(content="before")
    file_tools.write_file.return_value = {"status": "ok"}
    sink = ApprovalSink(ApprovalDecision("accepted", "once"))
    registry = ToolRegistry(
        file_tools,
        MagicMock(),
        event_sink=sink,
        allowed_tools=frozenset({"write_file"}),
        approval_policy="per_step",
    )

    result = registry.execute(
        ToolCall("call-1", "write_file", {"path": "README.md", "content": "ok"}),
        run_id="run-1",
        request_id="request-1",
    )

    assert result == {"status": "ok"}
    file_tools.write_file.assert_called_once()
    assert [(event.diff_old_text, event.diff_new_text) for event in sink.events] == [
        ("before", "ok"),
        ("before", "ok"),
    ]


def test_per_step_policy_keeps_rejection_distinct_from_policy_denial():
    file_tools = MagicMock()
    sink = ApprovalSink(ApprovalDecision("rejected"))
    registry = ToolRegistry(
        file_tools,
        MagicMock(),
        event_sink=sink,
        allowed_tools=frozenset({"write_file"}),
        approval_policy="per_step",
    )

    with pytest.raises(ToolRegistryError) as error:
        registry.execute(
            ToolCall("call-1", "write_file", {"path": "README.md", "content": "no"}),
            run_id="run-1",
            request_id="request-1",
        )

    assert error.value.failure_reason == "approval_rejected"
    file_tools.write_file.assert_not_called()
    assert [(event.phase, event.status) for event in sink.events] == [
        ("started", "pending"),
        ("failed", "failed"),
    ]


def test_suggestion_only_denies_unknown_mcp_side_effects():
    mcp = MagicMock()
    mcp.has_tool.return_value = True
    registry = ToolRegistry(
        MagicMock(),
        MagicMock(),
        mcp_tools=mcp,
        allowed_tools=frozenset({"mcp__issue_tracker__create"}),
        approval_policy="suggestion_only",
    )

    with pytest.raises(ToolRegistryError) as error:
        registry.execute(
            ToolCall("call-1", "mcp__issue_tracker__create", {}),
            run_id="run-1",
            request_id="request-1",
        )

    assert error.value.failure_reason == "approval_policy_denied"
    mcp.execute.assert_not_called()


def test_managed_request_reacquires_once_after_expired_bearer(tmp_path):
    authorization = ManagedBridgeAuthorization()
    authorization._workspace_root = tmp_path
    authorization._backend_url = "https://example.test"
    authorization._acp_token = "expired"
    authorization._scope = AcpRuntimeScope("project", str(tmp_path))

    with patch.object(
        AcpBackendAuthorization,
        "authorized_json_request",
        side_effect=[AcpSessionExpired("expired"), {"ok": True}],
    ), patch.object(authorization, "prepare_workspace") as prepare, patch.object(
        authorization, "authenticate"
    ) as authenticate:
        result = authorization.authorized_json_request("POST", "/api/acp/runs", {})

    assert result == {"ok": True}
    prepare.assert_called_once_with(tmp_path)
    authenticate.assert_called_once_with()


def test_telemetry_resolves_rotating_bearer_when_flushing():
    uploaded = []
    token = {"value": "first"}
    sink = ServerUploadTelemetrySink(
        lambda run, events, headers: uploaded.append(headers),
        auth_headers_provider=lambda: {"Authorization": f"Bearer {token['value']}"},
    )
    token["value"] = "refreshed"

    sink.append(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "source": "code4me2_agent",
            "timestamp": "2026-09-10T00:00:00Z",
            "event_type": "agent.run.completed",
            "payload": {"status": "completed"},
        }
    )

    assert uploaded == [{"Authorization": "Bearer refreshed"}]


def test_policy_refresh_rebuilds_session_memory_limit(tmp_path):
    config = AgentConfig(
        workspace_root=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
    )
    config = replace(
        config,
        adapter=replace(
            config.adapter,
            memory_window=MemoryWindowConfig(
                scope="session",
                strategy="token_window",
                max_messages=20,
                max_tokens=100,
            ),
        ),
    )
    core = EchoAgentCore(config)
    core.load_session_memory(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "second message"},
        ]
    )
    restricted = replace(
        config,
        adapter=replace(
            config.adapter,
            memory_window=replace(config.adapter.memory_window, max_tokens=2),
        ),
    )

    core.apply_config(restricted)

    assert core._session_memory is not None
    assert core._session_memory.window() == [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "second message"},
    ]


def test_managed_policy_parser_rejects_partial_or_direct_provider_config():
    with pytest.raises(ValueError, match="metadata"):
        ServerAgentConfig.from_managed_payload({"model": "model"})

    policy = {
        "version": "1",
        "transport": "openai",
        "agent_profile": "arm-a",
        "framework_version": "code4me2-agent",
        "model": "model",
        "tools": [],
        "commands_allowlist": [],
        "max_iterations": 3,
        "max_context_tokens": 1000,
        "approval_policy": "auto",
        "temperature": None,
        "store_agent_content": False,
    }
    with pytest.raises(ValueError, match="unsafe transport"):
        ServerAgentConfig.from_managed_payload(policy)


def test_system_context_reports_host_and_available_commands(tmp_path):
    config = AgentConfig(
        workspace_root=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
    )
    config = replace(
        config,
        commands=replace(config.commands, allowlisted_commands=["git", "missing"]),
    )
    adapter = object.__new__(OpenAICompatibleReactAdapter)
    adapter._config = config

    with patch("code4me2_agent.adapters.platform.system", return_value="Windows"), patch(
        "code4me2_agent.command_tools.available_commands", return_value=["git"]
    ):
        context = adapter._system_context()

    assert "operating system is Windows" in context
    assert "executable commands allowed by policy are: git" in context
    assert "Never assume Bash" in context


def test_managed_telemetry_does_not_write_project_trace(tmp_path):
    trace_path = tmp_path / ".code4me" / "acp-trace.jsonl"
    config = AgentConfig(
        workspace_root=tmp_path,
        trace_path=trace_path,
        session_id="session-1",
        managed_mode=True,
    )
    recorder = AgentTelemetryRecorder(config, sinks=[])

    recorder.record(
        event_type="agent.request.received",
        run_id="run-1",
        request_id="request-1",
        parent_event_id=None,
        payload={"content": "private participant prompt"},
        contains_user_prompt=True,
    )

    assert not trace_path.exists()


def test_content_preference_redacts_before_telemetry_sink(tmp_path):
    captured = []

    class CaptureSink:
        def append(self, event):
            captured.append(event)

    config = AgentConfig(
        workspace_root=tmp_path,
        trace_path=tmp_path / "trace.jsonl",
        session_id="session-1",
        managed_mode=True,
        store_agent_content=False,
    )
    recorder = AgentTelemetryRecorder(config, sinks=[CaptureSink()])

    recorder.record(
        event_type="agent.tool.completed",
        run_id="run-1",
        request_id="request-1",
        parent_event_id=None,
        payload={
            "tool_name": "run_command",
            "status": "completed",
            "argv": ["cat", "secret.txt"],
            "stdout": "private contents",
            "exit_code": 0,
        },
        raw_payload={"content": "private contents"},
    )

    assert captured[0]["payload"] == {
        "tool_name": "run_command",
        "status": "completed",
        "exit_code": 0,
    }
    assert captured[0]["raw_payload"] is None
