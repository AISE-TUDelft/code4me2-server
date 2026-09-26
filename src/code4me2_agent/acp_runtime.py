from __future__ import annotations

import asyncio
import logging
import os
import platform
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import EventLoopAsyncRunner, OperationCancelled
from code4me2_agent.command_tools import available_commands, build_acp_command_backend
from code4me2_agent.echo import EchoAgentCore
from code4me2_agent.events import (
    ApprovalDecision,
    AssistantTextEvent,
    PlanEvent,
    ThoughtEvent,
    UsageEvent,
)
from code4me2_agent.tool_catalog import approval_kind, tool_kind
from code4me2_agent.file_tools import build_acp_file_system_backend
from code4me2_agent.mcp_tools import StdioMcpToolBroker, serialize_mcp_servers
from code4me2_agent.runtime_auth import (
    AcpAuthorizationFailure,
    AcpBackendAuthorization,
    ManagedBridgeAuthorization,
)

logger = logging.getLogger(__name__)

# The runtime currently implements ACP protocol version 1.
SUPPORTED_PROTOCOL_VERSIONS: tuple[int, ...] = (1,)

# Python SDK 0.12.1 still guards the now-stable session/resume and session/close
# routes with this SDK-wide switch. Unsupported guarded routes are safe because
# the concrete agent below deliberately does not inherit the SDK Protocol stubs.
_ENABLE_STABLE_SESSION_ROUTE_WORKAROUND = True


if TYPE_CHECKING:
    from acp.interfaces import Client

    from code4me2_agent.config import AgentConfig, CommandConfig
    from code4me2_agent.events import ToolCallEvent
    from code4me2_agent.telemetry import AgentTelemetryRecorder


async def _prompt_text_async(prompt: list[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            text = block.get("text", "")
            if block_type == "resource_link":
                text = await _resource_link_text_async(block)
        else:
            block_type = getattr(block, "type", "")
            text = getattr(block, "text", "")
            if block_type == "resource_link":
                text = await _resource_link_text_async(block)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def _resource_link_text_async(block: object) -> str:
    uri = str(capability_value(block, "uri") or "").strip()
    name = str(capability_value(block, "name") or uri or "resource").strip()
    details = []
    mime_type = capability_value(block, "mimeType") or capability_value(
        block, "mime_type"
    )
    description = capability_value(block, "description")
    if mime_type:
        details.append(f"mimeType={mime_type}")
    if description:
        details.append(f"description={description}")
    suffix = f"; {'; '.join(details)}" if details else ""
    resource_link = f"[Resource link: {name} <{uri}>{suffix}]"
    return resource_link

_ACP_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)
_USAGE_UPDATES_ENABLED = os.getenv("CODE4ME_ACP_USAGE_UPDATES", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _acp_stop_reason(adapter_stop_reason: str) -> str:
    """Map adapter outcomes onto the ACP StopReason vocabulary.

    Failures (``error``, ``provider_exhausted``) have already been shown to the
    user as a final agent message, so the turn ended normally from the client's
    point of view; the cause travels in ``PromptResponse._meta``. ``refusal`` is
    reserved for a model content-filter refusal.
    """
    if adapter_stop_reason == "max_iterations":
        return "max_turn_requests"
    if adapter_stop_reason in _ACP_STOP_REASONS:
        return adapter_stop_reason
    return "end_turn"


@dataclass
class AgentSession:
    session_id: str
    core: EchoAgentCore
    mcp_tools: StdioMcpToolBroker | None = None
    cancel_event: Event = field(default_factory=Event)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_prompt_task: asyncio.Task[Any] | None = None
    authorization: Any | None = None


class AcpSessionEventSink:
    def __init__(
        self,
        *,
        conn: Client,
        session_id: str,
        updates: AcpUpdateBuilder,
        telemetry: object,
        async_runner: EventLoopAsyncRunner,
        cancel_event: Event | None = None,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._updates = updates
        self._telemetry = telemetry
        self._async_runner = async_runner
        self._cancel_event = cancel_event
        self._session_approved_kinds: set[str] = set()
        self._tool_content: dict[str, list[Any] | None] = {}

    def _run_blocking(self, awaitable: Any) -> Any:
        if self._cancel_event is None:
            return self._async_runner.run(awaitable)
        return self._async_runner.run(awaitable, cancel_event=self._cancel_event)

    def assistant_text(self, event: AssistantTextEvent) -> None:
        update = self._updates.agent_message(
            event.text,
            message_id=event.message_id,
            metadata={
                "code4me2": {
                    "phase": "final" if event.final else "progress",
                    "iteration": event.iteration,
                }
            },
        )
        self._send_session_update(
            update,
            run_id=event.run_id,
            request_id=event.request_id,
            update_type="agent_message_chunk",
        )

    def plan(self, event: PlanEvent) -> None:
        update = self._updates.agent_plan(list(event.entries))
        self._send_session_update(
            update,
            run_id=event.run_id,
            request_id=event.request_id,
            update_type="plan",
            extra={"tool_call_id": event.tool_call_id},
        )

    def usage(self, event: UsageEvent) -> None:
        if not _USAGE_UPDATES_ENABLED:
            return
        update = self._updates.usage_update(
            used=event.prompt_tokens + event.completion_tokens,
            size=event.context_budget_tokens,
            metadata={
                "code4me2": {
                    "iteration": event.iteration,
                    "model": event.model,
                    "promptTokens": event.prompt_tokens,
                    "completionTokens": event.completion_tokens,
                    "totalTokens": event.total_tokens,
                    "turnTotalTokens": event.turn_total_tokens,
                }
            },
        )
        self._send_session_update(
            update,
            run_id=event.run_id,
            request_id=event.request_id,
            update_type="usage_update",
        )

    def tool_call(self, event: ToolCallEvent) -> None:
        content = self._tool_call_content(event)
        if event.phase == "started":
            self._tool_content[event.tool_call_id] = content
            update = self._updates.start_tool_call(
                tool_call_id=event.tool_call_id,
                title=event.title,
                kind=event.kind,
                status=event.status,
                path=event.path,
                locations=event.locations,
                content=content,
                raw_input=event.raw_input,
            )
        else:
            update = self._updates.update_tool_call(
                tool_call_id=event.tool_call_id,
                title=event.title,
                kind=event.kind,
                status=event.status,
                content=content,
                raw_output=event.raw_output,
            )
        self._send_session_update(
            update,
            run_id=event.run_id,
            request_id=event.request_id,
            update_type=str(getattr(update, "session_update", "tool_call")),
            extra={
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "phase": event.phase,
            },
        )
        if event.phase != "started":
            self._tool_content.pop(event.tool_call_id, None)

    def _tool_call_content(self, event: ToolCallEvent) -> list[Any] | None:
        if event.path and event.diff_new_text is not None:
            return [
                self._updates.diff_tool_content(
                    event.path,
                    old_text=event.diff_old_text,
                    new_text=event.diff_new_text,
                )
            ]
        if event.content_text:
            return [self._updates.text_tool_content(event.content_text)]
        return None

    def thought(self, event: object) -> None:
        phase = getattr(event, "phase", None)
        duration_ms = getattr(event, "duration_ms", None)
        timing_metadata = None
        if phase is not None:
            phase_metadata: dict[str, object] = {"phase": phase}
            if duration_ms is not None:
                phase_metadata["durationMs"] = duration_ms
            timing_metadata = {"code4me2": phase_metadata}
        update = self._updates.agent_thought(
            getattr(event, "text", ""),
            metadata=timing_metadata,
        )
        self._send_session_update(
            update,
            run_id=getattr(event, "run_id", None),
            request_id=getattr(event, "request_id", None),
            update_type="agent_thought",
        )

    def request_approval(
        self, tool_call: object, arguments: dict[str, Any]
    ) -> ApprovalDecision:
        """Synchronously bridge a worker-thread tool decision to ACP/JetBrains."""
        name = str(getattr(tool_call, "name", "tool"))
        tool_call_id = str(getattr(tool_call, "tool_call_id", ""))
        metadata = approval_kind(name)
        if metadata in self._session_approved_kinds:
            # Answered by the user's earlier "allow for this session": no one is
            # asked, so telemetry must not report a new decision.
            return ApprovalDecision("accepted", "session_cached")
        summary = _approval_summary(name, arguments)
        permission = self._updates.permission_request(
            session_id=self._session_id,
            tool_call_id=tool_call_id,
            title=summary,
            kind=tool_kind(name),
            summary=summary,
            raw_input=_approval_raw_input(name, arguments),
            session_option_name=_session_option_name(metadata),
            content=self._tool_content.get(tool_call_id),
        )
        try:
            response = self._run_blocking(
                self._conn.request_permission(
                    session_id=permission.session_id,
                    tool_call=permission.tool_call,
                    options=permission.options,
                )
            )
        except OperationCancelled:
            return ApprovalDecision("cancelled")
        except Exception:  # noqa: BLE001
            logger.exception("ACP permission request failed for tool %s", name)
            return ApprovalDecision("unavailable")
        outcome = getattr(response, "outcome", None)
        if isinstance(response, dict):
            outcome = response.get("outcome")
        if isinstance(outcome, dict):
            selected = outcome.get("outcome") == "selected"
            option_id = outcome.get("optionId", outcome.get("option_id"))
        else:
            selected = getattr(outcome, "outcome", None) == "selected"
            option_id = getattr(outcome, "option_id", None)
        if not selected:
            return ApprovalDecision("cancelled")
        scope = {
            "allow_once": "once",
            "allow_session": "session",
        }.get(option_id)
        if scope == "session":
            self._session_approved_kinds.add(metadata)
        return ApprovalDecision("accepted", scope) if scope else ApprovalDecision("rejected")

    def _send_session_update(
        self,
        update: object,
        *,
        run_id: str | None,
        request_id: str | None,
        update_type: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._run_blocking(
                self._conn.session_update(
                    session_id=self._session_id,
                    update=update,
                    source="code4me2_agent",
                )
            )
        except OperationCancelled:
            return
        except Exception as exc:  # noqa: BLE001
            record = getattr(self._telemetry, "record", None)
            if callable(record):
                record(
                    event_type="agent.acp.update_failed",
                    run_id=run_id,
                    request_id=request_id,
                    parent_event_id=None,
                    payload={
                        "update_type": update_type,
                        "error_message": str(exc),
                        **(extra or {}),
                    },
                )


def _record_acp_runtime_event(
    telemetry: AgentTelemetryRecorder,
    *,
    event_type: str,
    session_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    telemetry.record(
        event_type=event_type,
        run_id=f"acp-runtime-{request_id}",
        request_id=request_id,
        parent_event_id=None,
        payload={"session_id": session_id, **payload},
    )


def _approval_raw_input(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    if name == "run_command":
        argv = arguments.get("argv")
        raw: dict[str, Any] = {
            "argv": [str(arg) for arg in argv] if isinstance(argv, (list, tuple)) else [],
            "cwd": str(arguments.get("cwd", ".")),
        }
        if arguments.get("timeout_seconds") is not None:
            raw["timeout_seconds"] = arguments["timeout_seconds"]
        return raw
    if name in {"create_file", "write_file", "replace_text", "delete_file"}:
        return {"path": str(arguments.get("path", ""))}
    if name == "edit_file":
        return {
            "path": str(arguments.get("path", "")),
            "edit_count": len(arguments.get("edits") or []),
        }
    if name == "move_file":
        return {
            "source_path": str(arguments.get("source_path", "")),
            "destination_path": str(arguments.get("destination_path", "")),
            "overwrite": bool(arguments.get("overwrite", False)),
        }
    return None


def _approval_summary(name: str, arguments: dict[str, Any]) -> str:
    if name == "run_command":
        argv = arguments.get("argv")
        command = " ".join(str(arg) for arg in argv) if isinstance(argv, (list, tuple)) else "command"
        timeout = arguments.get("timeout_seconds")
        suffix = f" (timeout {int(float(timeout))}s)" if isinstance(timeout, (int, float)) else ""
        return f"Run command: {command}{suffix}"
    if name == "move_file":
        source = str(arguments.get("source_path", "")).strip()
        destination = str(arguments.get("destination_path", "")).strip()
        return f"Move file: {source} → {destination}"
    path = str(arguments.get("path", "")).strip()
    if name == "edit_file":
        edit_count = len(arguments.get("edits") or [])
        return f"Edit file: {path} ({edit_count} edit{'s' if edit_count != 1 else ''})"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return f"Call {parts[1]}: {parts[2]}" if len(parts) == 3 else f"Run tool: {name}"
    labels = {
        "create_file": "Create file",
        "write_file": "Write file",
        "replace_text": "Edit file",
        "delete_file": "Delete file",
    }
    return f"{labels.get(name, f'Run tool: {name}')}{f': {path}' if path else ''}"


def _session_option_name(kind: str) -> str:
    return {
        "edit": "Allow edits for session",
        "execute": "Allow commands for session",
        "other": "Allow MCP tools for session",
    }[kind]


def _normalize_capabilities(value: object | None) -> object | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _normalize_capabilities(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_capabilities(item) for item in value]
    if hasattr(value, "model_dump"):
        dump = getattr(value, "model_dump")
        if callable(dump):
            return _normalize_capabilities(dump(by_alias=True, exclude_none=True))
    if hasattr(value, "__dict__"):
        return {
            str(key): _normalize_capabilities(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return repr(value)


def _prompt_backend_state(session: AgentSession) -> dict[str, Any]:
    file_backend = getattr(session.core.file_tools, "_acp_backend", None)
    command_backend = getattr(session.core.command_tools, "_acp_backend", None)
    return {
        "workspace_root": session.core._config.workspace_root.as_posix(),
        "file_backend": "acp" if file_backend is not None else "local",
        "file_backend_session_id": getattr(file_backend, "session_id", None),
        "read_text_file_enabled": bool(
            getattr(file_backend, "read_text_file_enabled", False)
        ),
        "write_text_file_enabled": bool(
            getattr(file_backend, "write_text_file_enabled", False)
        ),
        "command_backend": "acp" if command_backend is not None else "local",
        "terminal_enabled": bool(getattr(command_backend, "terminal_enabled", False)),
    }


def _persisted_memory_messages(payload: dict) -> list[dict[str, Any]]:
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def _is_supported_stdio_mcp_server(server: object) -> bool:
    """Check the 0.12.1 stdio shape (name/command/args/env) without spawning.

    HTTP, SSE and malformed entries are rejected as unsupported before the
    broker ever runs: only validated stdio reaches process startup, whose own
    failures (missing binary, timeout, duplicates) stay mcp_server_start_failed.
    """
    for key in ("name", "command", "args", "env"):
        if capability_value(server, key) is None:
            return False
    return True


def _should_record_runtime_context(
    *,
    client_capabilities: object | None,
    session_lookup: str = "existing",
) -> bool:
    return client_capabilities is not None or session_lookup != "existing"


def create_acp_agent(
    config: AgentConfig,
    *,
    authorization: Any | None = None,
) -> Any:
    try:
        from acp import (
            AuthenticateResponse,
            InitializeResponse,
            NewSessionResponse,
            PromptResponse,
            RequestError,
        )
        from acp.schema import (
            AgentCapabilities,
            AuthMethodAgent,
            Implementation,
            McpCapabilities,
            PromptCapabilities,
            ResumeSessionResponse,
            SessionCapabilities,
            SessionCloseCapabilities,
            SessionResumeCapabilities,
            Usage,
        )
    except ImportError as exc:
        raise SystemExit(
            "The ACP SDK is required to run stdio mode. "
            "Install this package from source with agent-client-protocol available."
        ) from exc

    class Code4MeEchoAgent:
        _conn: Client
        _client_capabilities: object | None
        _updates: AcpUpdateBuilder
        _bootstrap_core: EchoAgentCore
        _sessions: dict[str, AgentSession]
        _authorization: Any
        _authorized_workspace: Path | None

        def on_connect(self, conn: Client) -> None:
            self._conn = conn
            self._client_capabilities = None
            self._updates = AcpUpdateBuilder()
            self._sessions = {}
            self._authorization = (
                authorization
                or (ManagedBridgeAuthorization() if config.managed_mode else None)
                or AcpBackendAuthorization.from_environment(
                    workspace_root=config.workspace_root,
                )
            )
            self._authorized_workspace = None
            self._current_config = config
            self._bootstrap_core = EchoAgentCore(config)
            self.file_tools = self._bootstrap_core.file_tools
            self.command_tools = self._bootstrap_core.command_tools

        def _build_session(
            self,
            *,
            session_config: AgentConfig,
            session_id: str,
            mcp_tools: StdioMcpToolBroker | None,
            session_authorization: Any | None = None,
        ) -> AgentSession:
            async_runner = EventLoopAsyncRunner(asyncio.get_running_loop())
            cancel_event = Event()
            event_sink = AcpSessionEventSink(
                conn=self._conn,
                session_id=session_id,
                updates=self._updates,
                telemetry=None,
                async_runner=async_runner,
                cancel_event=cancel_event,
            )
            core = EchoAgentCore(session_config, event_sink=event_sink)
            event_sink._telemetry = core._telemetry
            core.rebuild_tools(
                acp_file_backend=build_acp_file_system_backend(
                    client=self._conn,
                    session_id=session_id,
                    client_capabilities=self._client_capabilities,
                    async_runner=async_runner,
                ),
                acp_command_backend=build_acp_command_backend(
                    client=self._conn,
                    session_id=session_id,
                    client_capabilities=self._client_capabilities,
                    async_runner=async_runner,
                ),
                mcp_tools=mcp_tools,
            )
            return AgentSession(
                session_id=session_id,
                core=core,
                mcp_tools=mcp_tools,
                cancel_event=cancel_event,
                authorization=session_authorization or self._authorization,
            )

        def _load_persisted_memory(self, session: AgentSession) -> None:
            request_json = getattr(session.authorization, "authorized_json_request", None)
            if not callable(request_json):
                return
            try:
                payload = request_json(
                    "GET",
                    f"/api/agent/memory/{session.session_id}",
                    None,
                )
            except AcpAuthorizationFailure as exc:
                logger.info("Could not load persisted ACP memory: %s", exc)
                return
            messages = _persisted_memory_messages(payload)
            if messages:
                session.core.load_session_memory(messages)

        def _save_persisted_memory(self, session: AgentSession) -> None:
            snapshot = session.core.session_memory_snapshot()
            if snapshot is None:
                return
            request_json = getattr(session.authorization, "authorized_json_request", None)
            if not callable(request_json):
                return
            try:
                request_json(
                    "PUT",
                    f"/api/agent/memory/{session.session_id}",
                    {"messages": snapshot},
                )
            except AcpAuthorizationFailure as exc:
                logger.info("Could not save persisted ACP memory: %s", exc)

        def _session_config(
            self, *, workspace_root: Path, session_id: str, session_authorization: Any
        ) -> AgentConfig:
            # Build from self._current_config, NOT the module-level `config`
            # closure: _apply_server_config writes the assigned agent profile
            # into _current_config, and reading the original file config here
            # would silently drop the server's model/provider/tool overrides
            # for every session created after authentication — i.e. the A/B
            # assignment would appear to work (it's logged) but never actually
            # take effect on a new session.
            base_config = self._current_config
            telemetry_headers = session_authorization.telemetry_headers()
            provider_auth_headers = (
                telemetry_headers
                if base_config.adapter.provider.kind == "code4me_backend"
                else base_config.adapter.provider.auth_headers
            )
            upload_config = base_config.upload
            backend_url = getattr(session_authorization, "backend_url", None)
            if backend_url and upload_config.ingest_url is None:
                # Self-reported telemetry converges on the same agent_event
                # table the inference relay writes to (merge decision 2).
                upload_config = replace(
                    upload_config,
                    enabled=True,
                    ingest_url=f"{str(backend_url).rstrip('/')}/api/agent/events/ingest",
                )
            return replace(
                base_config,
                workspace_root=workspace_root,
                session_id=session_id,
                upload=replace(
                    upload_config,
                    auth_headers=telemetry_headers,
                    auth_headers_provider=session_authorization.telemetry_headers,
                ),
                adapter=replace(
                    base_config.adapter,
                    provider=replace(
                        base_config.adapter.provider,
                        auth_headers=provider_auth_headers,
                    ),
                ),
                managed_request=(
                    session_authorization.managed_inference
                    if base_config.managed_mode
                    else base_config.managed_request
                ),
            )

        async def _create_or_resume_session(
            self,
            *,
            cwd: str,
            session_id: str,
            event_type: str,
            mcp_servers: list[Any] | None,
        ) -> AgentSession:
            workspace_root = _resolve_session_cwd(cwd)
            session_authorization = self._authorization
            if config.managed_mode:
                session_authorization = ManagedBridgeAuthorization()
                try:
                    await asyncio.to_thread(session_authorization.prepare_workspace, workspace_root)
                    scope = await asyncio.to_thread(session_authorization.authenticate)
                except AcpAuthorizationFailure:
                    raise RequestError.auth_required({"reason": "authorization_rejected"}) from None
                if _resolve_session_cwd(scope.workspace) != workspace_root:
                    raise RequestError.auth_required({"reason": "workspace_not_authorized"})
            else:
                await self._require_authenticated()
                if workspace_root != self._authorized_workspace:
                    raise RequestError.auth_required({"reason": "workspace_not_authorized"})
            base_config = self._current_config
            server_config = session_authorization.server_agent_config
            if config.managed_mode:
                # Managed runs never fall back to local defaults: a missing
                # profile, an unsupported runtime, or a malformed policy must
                # fail before study work starts.
                if server_config is None or not server_config.has_overrides:
                    raise RequestError.invalid_params(
                        {"reason": "managed_config_unavailable"}
                    )
                if (
                    server_config.framework_version
                    and server_config.framework_version
                    not in {"code4me2-agent", "code4me-agent"}
                ):
                    raise RequestError.invalid_params(
                        {"reason": "unsupported_managed_runtime", "runtime": server_config.framework_version}
                    )
                base_config = replace(base_config, workspace_root=workspace_root).with_server_overrides(
                    server_config,
                    backend_url=getattr(session_authorization, "backend_url", None),
                )
            elif server_config is not None:
                base_config = replace(base_config, workspace_root=workspace_root).with_server_overrides(
                    server_config,
                    backend_url=getattr(session_authorization, "backend_url", None),
                )
            previous_config = self._current_config
            self._current_config = base_config
            session_config = self._session_config(
                workspace_root=workspace_root,
                session_id=session_id,
                session_authorization=session_authorization,
            )
            self._current_config = previous_config
            requested_mcp_servers = list(mcp_servers or [])
            mcp_allowed_by_policy = (
                not config.managed_mode
                or (
                    base_config.approval_policy != "suggestion_only"
                    and base_config.allowed_tools is not None
                    and any(name.startswith("mcp__") for name in base_config.allowed_tools)
                )
            )
            enabled_mcp_servers = requested_mcp_servers if mcp_allowed_by_policy else []
            if requested_mcp_servers and not mcp_allowed_by_policy:
                logger.info(
                    "Assigned policy disabled %d client-supplied MCP server(s) before launch.",
                    len(requested_mcp_servers),
                )
            try:
                mcp_tools = await asyncio.to_thread(
                    StdioMcpToolBroker.open,
                    enabled_mcp_servers,
                    cwd=workspace_root,
                )
            except (RuntimeError, TimeoutError, ValueError) as exc:
                # Best effort: a failing tool sidecar (e.g. the IDE-bundled MCP
                # server binary refusing a second instance) must not take down
                # the whole chat. The session keeps its native file/terminal
                # tools; malformed entries are still rejected up front.
                logger.warning(
                    "ACP session continuing without MCP tools after broker failure %s: %s",
                    serialize_mcp_servers(mcp_servers),
                    exc,
                )
                mcp_tools = None
            try:
                session = self._build_session(
                    session_config=session_config,
                    session_id=session_id,
                    mcp_tools=mcp_tools,
                    session_authorization=session_authorization,
                )
            except BaseException:
                if mcp_tools is not None:
                    await asyncio.to_thread(mcp_tools.close)
                raise
            await asyncio.to_thread(self._load_persisted_memory, session)
            previous_session = self._sessions.get(session_id)
            if previous_session is not None:
                await self._stop_session(previous_session)
            self._sessions[session_id] = session
            self.file_tools = session.core.file_tools
            self.command_tools = session.core.command_tools
            if _should_record_runtime_context(
                client_capabilities=self._client_capabilities
            ):
                _record_acp_runtime_event(
                    session.core._telemetry,
                    event_type=event_type,
                    session_id=session_id,
                    request_id=uuid4().hex,
                    payload={
                        "cwd": session_config.workspace_root.as_posix(),
                        "runtime_os": platform.system(),
                        "runtime_architecture": platform.machine(),
                        "available_commands": available_commands(
                            session_config.commands.allowlisted_commands
                        ),
                        "client_capabilities": _normalize_capabilities(
                            self._client_capabilities
                        ),
                        "mcp_servers_requested": serialize_mcp_servers(requested_mcp_servers),
                        "mcp_servers_policy_enabled": bool(enabled_mcp_servers),
                        "mcp_backend": "broker"
                        if session.mcp_tools is not None
                        else "none",
                        **_prompt_backend_state(session),
                    },
                )
            return session

        @staticmethod
        async def _stop_active_prompt(session: AgentSession) -> None:
            session.cancel_event.set()
            active_task = session.active_prompt_task
            if active_task is None or active_task is asyncio.current_task():
                return
            try:
                await asyncio.shield(active_task)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Active ACP prompt failed while the session was being replaced or closed."
                )

        @classmethod
        async def _stop_session(cls, session: AgentSession) -> None:
            await cls._stop_active_prompt(session)
            if session.mcp_tools is not None:
                await asyncio.to_thread(session.mcp_tools.close)

        async def initialize(
            self,
            protocol_version: int,
            client_capabilities: object | None = None,
            **kwargs: Any,
        ) -> InitializeResponse:
            negotiated_protocol_version = (
                protocol_version
                if protocol_version in SUPPORTED_PROTOCOL_VERSIONS
                else max(SUPPORTED_PROTOCOL_VERSIONS)
            )
            self._client_capabilities = client_capabilities or kwargs.get(
                "clientCapabilities"
            )
            async_runner = EventLoopAsyncRunner(asyncio.get_running_loop())
            acp_file_backend = build_acp_file_system_backend(
                client=self._conn,
                session_id=config.session_id,
                client_capabilities=self._client_capabilities,
                async_runner=async_runner,
            )
            acp_command_backend = build_acp_command_backend(
                client=self._conn,
                session_id=config.session_id,
                client_capabilities=self._client_capabilities,
                async_runner=async_runner,
            )
            self._bootstrap_core.rebuild_tools(
                acp_file_backend=acp_file_backend,
                acp_command_backend=acp_command_backend,
            )
            self.file_tools = self._bootstrap_core.file_tools
            self.command_tools = self._bootstrap_core.command_tools
            initialize_auth_succeeded = False
            try:
                if not getattr(self._authorization, "needs_workspace", False):
                    await self._authenticate_if_needed()
                    initialize_auth_succeeded = True
            except RequestError:
                # Some ACP clients never invoke the custom auth handshake for local agents.
                # Try once during initialize, but keep later session/prompt calls fail-closed.
                pass
            _record_acp_runtime_event(
                self._bootstrap_core._telemetry,
                event_type="agent.acp.initialize",
                session_id=config.session_id,
                request_id=uuid4().hex,
                payload={
                    "client_capabilities": _normalize_capabilities(
                        self._client_capabilities
                    ),
                    "authorization_bootstrap_succeeded": initialize_auth_succeeded,
                    "file_backend": "acp" if acp_file_backend is not None else "local",
                    "command_backend": (
                        "acp" if acp_command_backend is not None else "local"
                    ),
                    "read_text_file_enabled": bool(
                        getattr(acp_file_backend, "read_text_file_enabled", False)
                    ),
                    "write_text_file_enabled": bool(
                        getattr(acp_file_backend, "write_text_file_enabled", False)
                    ),
                    "terminal_enabled": bool(
                        getattr(acp_command_backend, "terminal_enabled", False)
                    ),
                },
            )
            return InitializeResponse(
                protocol_version=negotiated_protocol_version,
                agent_capabilities=AgentCapabilities(
                    # This agent restores model memory, but does not yet persist the
                    # client-visible update stream required by session/load replay.
                    loadSession=False,
                    promptCapabilities=PromptCapabilities(
                        image=False,
                        audio=False,
                        embeddedContext=False,
                    ),
                    sessionCapabilities=SessionCapabilities(
                        resume=SessionResumeCapabilities(),
                        close=SessionCloseCapabilities(),
                    ),
                    mcpCapabilities=McpCapabilities(
                        http=False,
                        sse=False,
                    ),
                ),
                agent_info=Implementation(
                    name="code4me2-agent",
                    title="Code4Me Agent",
                    version="0.1.0",
                ),
                auth_methods=[
                    AuthMethodAgent(
                        id="code4me-plugin-session",
                        name="Code4Me Plugin Session",
                        description="Authenticate using a prepared Code4Me project session.",
                    )
                ],
            )

        async def authenticate(
            self, method_id: str, **kwargs: Any
        ) -> AuthenticateResponse:
            if method_id != "code4me-plugin-session":
                raise RequestError.auth_required({"reason": "unsupported_auth_method"})
            if config.managed_mode:
                # Workspace-scoped authentication completes at session/new.
                return AuthenticateResponse()
            await self._authenticate_if_needed()
            return AuthenticateResponse()

        async def _authenticate_if_needed(self, *, force: bool = False) -> None:
            if (
                not force
                and self._authorization.is_authenticated
                and self._authorized_workspace is not None
            ):
                logger.info(
                    "ACP runtime already has authenticated workspace=%s.",
                    self._authorized_workspace,
                )
                return
            # Retry with delay: the plugin may need time to write a fresh ACP handoff
            # after a full restart (Docker + IDE) before the agent can use it.
            max_attempts = 5
            for attempt in range(1, max_attempts + 1):
                try:
                    logger.info(
                        "ACP runtime starting backend authentication (attempt %d/%d).",
                        attempt,
                        max_attempts,
                    )
                    scope = await asyncio.to_thread(self._authorization.authenticate)
                except AcpAuthorizationFailure as exc:
                    if attempt < max_attempts:
                        logger.warning(
                            "ACP runtime backend authentication rejected (attempt %d/%d): %s.",
                            attempt,
                            max_attempts,
                            exc,
                        )
                        await asyncio.sleep(2.0)
                        continue
                    logger.warning(
                        "ACP runtime backend authentication rejected after %d attempts: %s.",
                        max_attempts,
                        exc,
                    )
                    raise RequestError.auth_required(
                        {"reason": "authorization_rejected"}
                    ) from None
                break
            self._authorized_workspace = Path(scope.workspace).resolve()
            logger.info(
                "ACP runtime authorized workspace=%s project_id=%s.",
                self._authorized_workspace,
                scope.project_id,
            )
            self._apply_server_config()

        def _apply_server_config(self) -> None:
            """Adopt the assigned agent profile handed down by the backend.

            The whole merge of provider settings lives in
            ``AgentConfig.with_server_overrides`` so there's one place that
            decides precedence (server wins, local file is fallback) and one
            place that decides routing (direct provider vs backend relay).
            """
            server_config = self._authorization.server_agent_config
            if server_config is None or not server_config.has_overrides:
                logger.info("No server agent config overrides to apply.")
                return

            base = self._current_config
            backend_url = getattr(self._authorization, "backend_url", None)
            new_config = base.with_server_overrides(
                server_config, backend_url=backend_url
            )
            self._current_config = new_config
            self._apply_config_to_all_cores(new_config)

            provider = new_config.adapter.provider
            logger.info(
                "Applied server agent config: profile=%s model=%s provider_kind=%s "
                "base_url=%s max_iterations=%s commands_allowlist=%s.",
                server_config.agent_profile,
                provider.model,
                provider.kind,
                provider.base_url,
                new_config.adapter.max_iterations,
                new_config.commands.allowlisted_commands,
            )
            if not provider.is_configured:
                # Loud, because the alternative is a confusing failure deep in
                # the first model call.
                logger.warning(
                    "Agent provider is still incompletely configured "
                    "(base_url=%r model=%r) — model calls will fail until the "
                    "assigned profile supplies both.",
                    provider.base_url,
                    provider.model,
                )

        def _apply_config_to_all_cores(self, new_config: Any) -> None:
            # Rebuild every enforcement point (ToolRegistry allowed_tools /
            # approval, command allowlist, adapter provider) rather than
            # mutating _config in place, which would leave already-constructed
            # adapters enforcing stale policy: apply_config refreshes allowed
            # tools plus approval/command/provider state.
            self._bootstrap_core.apply_config(new_config)
            for session in self._sessions.values():
                session.core.apply_config(new_config)
            logger.info(
                "Updated %d active session(s) with server agent config.",
                len(self._sessions),
            )

        async def _require_authenticated(self) -> None:
            if (
                not self._authorization.is_authenticated
                or self._authorized_workspace is None
            ):
                await self._authenticate_if_needed()
            if (
                not self._authorization.is_authenticated
                or self._authorized_workspace is None
            ):
                raise RequestError.auth_required()

        async def new_session(
            self,
            cwd: str,
            mcp_servers: list[Any] | None = None,
            additional_directories: list[str] | None = None,
            **kwargs: Any,
        ) -> NewSessionResponse:
            self._reject_unsupported_session_inputs(
                mcp_servers=mcp_servers,
                additional_directories=additional_directories,
            )
            session_id = str(
                kwargs.get("session_id") or kwargs.get("sessionId") or uuid4().hex
            )
            await self._create_or_resume_session(
                cwd=cwd,
                session_id=session_id,
                event_type="agent.acp.session_created",
                mcp_servers=mcp_servers,
            )
            return NewSessionResponse(session_id=session_id)

        async def resume_session(
            self,
            cwd: str,
            session_id: str,
            mcp_servers: list[Any] | None = None,
            additional_directories: list[str] | None = None,
            **kwargs: Any,
        ) -> ResumeSessionResponse:
            self._reject_unsupported_session_inputs(
                mcp_servers=mcp_servers,
                additional_directories=additional_directories,
            )
            await self._create_or_resume_session(
                cwd=cwd,
                session_id=session_id,
                event_type="agent.acp.session_resumed",
                mcp_servers=mcp_servers,
            )
            return ResumeSessionResponse()

        @staticmethod
        def _reject_unsupported_session_inputs(
            *,
            mcp_servers: list[Any] | None,
            additional_directories: list[str] | None,
        ) -> None:
            if additional_directories:
                raise RequestError.invalid_params(
                    {"reason": "additional_directories_not_supported"}
                )
            for server in mcp_servers or []:
                if not _is_supported_stdio_mcp_server(server):
                    raise RequestError.invalid_params(
                        {"reason": "mcp_servers_not_supported"}
                    )

        async def close_session(
            self,
            session_id: str,
            **kwargs: Any,
        ) -> None:
            session = self._sessions.get(session_id)
            if session is None:
                raise RequestError.invalid_params({"reason": "unknown_session"})
            try:
                await self._stop_session(session)
            finally:
                self._sessions.pop(session_id, None)

        async def cancel(
            self,
            session_id: str,
            **kwargs: Any,
        ) -> None:
            session = self._sessions.get(session_id)
            if session is not None and session.active_prompt_task is not None:
                session.cancel_event.set()

        async def prompt(
            self,
            prompt: list[Any],
            session_id: str,
            message_id: str | None = None,
            **kwargs: Any,
        ) -> PromptResponse:
            request_id = message_id or uuid4().hex
            session = self._sessions.get(session_id)
            if session is None:
                raise RequestError.auth_required({"reason": "unknown_session"})
            session_authorization = session.authorization or self._authorization
            reauthenticated = False
            try:
                await asyncio.to_thread(session_authorization.validate)
            except AcpAuthorizationFailure:
                if config.managed_mode:
                    try:
                        await asyncio.to_thread(
                            session_authorization.prepare_workspace,
                            session.core._config.workspace_root,
                        )
                        await asyncio.to_thread(session_authorization.authenticate)
                        reauthenticated = True
                    except AcpAuthorizationFailure:
                        raise RequestError.auth_required(
                            {"reason": "authorization_rejected"}
                        ) from None
                else:
                    self._authorized_workspace = None
                    raise RequestError.auth_required(
                        {"reason": "authorization_rejected"}
                    ) from None
            if reauthenticated:
                refreshed_config = replace(
                    session.core._config,
                    upload=replace(
                        session.core._config.upload,
                        auth_headers=session_authorization.telemetry_headers(),
                    ),
                )
                session.core.apply_config(refreshed_config)
            async with session.prompt_lock:
                session.cancel_event.clear()
                session.active_prompt_task = asyncio.current_task()
                try:
                    if _should_record_runtime_context(
                        client_capabilities=self._client_capabilities,
                        session_lookup="existing",
                    ):
                        _record_acp_runtime_event(
                            session.core._telemetry,
                            event_type="agent.acp.prompt_context",
                            session_id=session_id,
                            request_id=request_id,
                            payload={
                                "session_lookup": "existing",
                                "known_session_ids": sorted(self._sessions.keys()),
                                "client_capabilities": _normalize_capabilities(
                                    self._client_capabilities
                                ),
                                **_prompt_backend_state(session),
                            },
                        )
                    prompt_text = await _prompt_text_async(prompt)
                    logger.info("Prompt received (%d characters).", len(prompt_text))
                    run_id = uuid4().hex
                    if config.managed_mode:
                        run_payload = await asyncio.to_thread(
                            session_authorization.create_managed_run,
                            run_id=run_id,
                            session_id=session_id,
                        )
                        from code4me2_agent.config import ServerAgentConfig

                        try:
                            run_policy = ServerAgentConfig.from_managed_payload(run_payload)
                        except ValueError:
                            raise RequestError.invalid_params(
                                {"reason": "managed_run_policy_invalid"}
                            ) from None
                        session.core.apply_config(
                            session.core._config.with_server_overrides(
                                run_policy,
                                backend_url=session_authorization.backend_url,
                            )
                        )
                    result = await asyncio.to_thread(
                        session.core.handle_prompt,
                        prompt_text,
                        request_id,
                        message_id,
                        run_id,
                        session.cancel_event,
                    )
                    await asyncio.to_thread(self._save_persisted_memory, session)
                    if result.final_response and not getattr(result, "response_emitted", False):
                        await self._conn.session_update(
                            session_id=session_id,
                            update=self._updates.agent_message(
                                result.final_response,
                                message_id=request_id,
                                metadata={
                                    "code4me2": {
                                        "phase": "completed",
                                        "durationMs": result.duration_ms,
                                    }
                                },
                            ),
                            source="code4me2_agent",
                        )
                    usage = getattr(result, "usage", None)
                    return PromptResponse(
                        stop_reason=_acp_stop_reason(result.stop_reason),
                        usage=Usage(
                            total_tokens=int(usage.get("total_tokens", 0)),
                            input_tokens=int(usage.get("prompt_tokens", 0)),
                            output_tokens=int(usage.get("completion_tokens", 0)),
                        )
                        if isinstance(usage, dict)
                        else None,
                        field_meta={
                            "code4me2": {
                                "outcome": result.run_status,
                                "adapterStopReason": result.stop_reason,
                                "durationMs": result.duration_ms,
                            }
                        },
                    )
                finally:
                    session.active_prompt_task = None
                    session.cancel_event.clear()

    return Code4MeEchoAgent()


def _resolve_session_cwd(cwd: str) -> Path:
    workspace_root = Path(cwd).expanduser()
    if not workspace_root.is_absolute():
        raise ValueError("ACP session cwd must be an absolute path.")
    return workspace_root.resolve()


async def run_acp_stdio(config: AgentConfig) -> None:
    try:
        from acp import run_agent
    except ImportError as exc:
        raise SystemExit(
            "The ACP SDK is required to run stdio mode. "
            "Install this package from source with agent-client-protocol available."
        ) from exc

    await run_agent(
        create_acp_agent(config),
        use_unstable_protocol=_ENABLE_STABLE_SESSION_ROUTE_WORKAROUND,
    )
