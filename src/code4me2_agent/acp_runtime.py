from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from code4me2_agent.acp_updates import AcpUpdateBuilder
from code4me2_agent.acp_utils import capability_value
from code4me2_agent.async_bridge import EventLoopAsyncRunner
from code4me2_agent.command_tools import build_acp_command_backend
from code4me2_agent.echo import EchoAgentCore
from code4me2_agent.file_tools import build_acp_file_system_backend
from code4me2_agent.runtime_auth import AcpAuthorizationFailure, AcpBackendAuthorization

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from acp.interfaces import Client

    from code4me2_agent.config import AgentConfig, CommandConfig
    from code4me2_agent.events import ToolCallEvent
    from code4me2_agent.telemetry import AgentTelemetryRecorder


def _prompt_text(prompt: list[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            text = block.get("text", "")
            if block_type == "resource_link":
                text = _resource_link_text(block)
        else:
            block_type = getattr(block, "type", "")
            text = getattr(block, "text", "")
            if block_type == "resource_link":
                text = _resource_link_text(block)
        if text:
            parts.append(text)
    return "\n".join(parts)


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


def _resource_link_text(block: object) -> str:
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

def _acp_stop_reason(adapter_stop_reason: str) -> str:
    if adapter_stop_reason == "max_iterations":
        return "max_turn_requests"
    if adapter_stop_reason in {
        "end_turn",
        "max_tokens",
        "max_turn_requests",
        "refusal",
        "cancelled",
    }:
        return adapter_stop_reason
    return "refusal"


@dataclass
class AgentSession:
    session_id: str
    core: EchoAgentCore


class AcpSessionEventSink:
    def __init__(
        self,
        *,
        conn: Client,
        session_id: str,
        updates: AcpUpdateBuilder,
        telemetry: object,
        async_runner: EventLoopAsyncRunner,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._updates = updates
        self._telemetry = telemetry
        self._async_runner = async_runner

    def tool_call(self, event: ToolCallEvent) -> None:
        if event.phase == "started":
            update = self._updates.start_tool_call(
                tool_call_id=event.tool_call_id,
                title=event.title,
                kind=event.kind,
                status=event.status,
                path=event.path,
                raw_input=event.raw_input,
            )
        else:
            content = (
                [self._updates.text_tool_content(event.content_text)]
                if event.content_text
                else None
            )
            update = self._updates.update_tool_call(
                tool_call_id=event.tool_call_id,
                title=event.title,
                kind=event.kind,
                status=event.status,
                content=content,
                raw_output=event.raw_output,
            )
        self._send_update(event=event, update=update)

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
        try:
            self._async_runner.run(
                self._conn.session_update(
                    session_id=self._session_id,
                    update=update,
                    source="code4me2_agent",
                )
            )
        except Exception as exc:
            record = getattr(self._telemetry, "record", None)
            if callable(record):
                record(
                    event_type="agent.acp.update_failed",
                    run_id=getattr(event, "run_id", None),
                    request_id=getattr(event, "request_id", None),
                    parent_event_id=None,
                    payload={
                        "update_type": "agent_thought",
                        "error_message": str(exc),
                    },
                )

    def _send_update(self, *, event: ToolCallEvent, update: object) -> None:
        try:
            self._async_runner.run(
                self._conn.session_update(
                    session_id=self._session_id,
                    update=update,
                    source="code4me2_agent",
                )
            )
        except Exception as exc:
            record = getattr(self._telemetry, "record", None)
            if callable(record):
                record(
                    event_type="agent.acp.update_failed",
                    run_id=event.run_id,
                    request_id=event.request_id,
                    parent_event_id=None,
                    payload={
                        "tool_call_id": event.tool_call_id,
                        "tool_name": event.tool_name,
                        "phase": event.phase,
                        "error_message": str(exc),
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


def _should_record_runtime_context(
    *,
    client_capabilities: object | None,
    session_lookup: str = "existing",
) -> bool:
    return client_capabilities is not None or session_lookup != "existing"


def create_acp_agent(config: AgentConfig, *, authorization: Any | None = None) -> Any:
    # TODO new agent -> a lot of changes to here.
    try:
        from acp import (
            Agent,
            AuthenticateResponse,
            InitializeResponse,
            LoadSessionResponse,
            NewSessionResponse,
            PromptResponse,
            RequestError,
        )
        from acp.schema import (
            AgentCapabilities,
            AuthMethod,
            Implementation,
            McpCapabilities,
            PromptCapabilities,
            ResumeSessionResponse,
            SessionCapabilities,
            SessionResumeCapabilities,
        )
    except ImportError as exc:
        raise SystemExit(
            "The ACP SDK is required to run stdio mode. "
            "Install this package from source with agent-client-protocol available."
        ) from exc

    class Code4MeEchoAgent(Agent):
        _conn: Client
        _client_capabilities: object | None
        _updates: AcpUpdateBuilder
        _bootstrap_core: EchoAgentCore
        _sessions: dict[str, AgentSession]
        _cancelled_sessions: set[str]
        _authorization: Any
        _authorized_workspace: Path | None

        def on_connect(self, conn: Client) -> None:
            self._conn = conn
            self._client_capabilities = None
            self._updates = AcpUpdateBuilder()
            self._sessions = {}
            self._cancelled_sessions = set()
            self._authorization = (
                authorization
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
            self, *, session_config: AgentConfig, session_id: str
        ) -> AgentSession:
            async_runner = EventLoopAsyncRunner(asyncio.get_running_loop())
            event_sink = AcpSessionEventSink(
                conn=self._conn,
                session_id=session_id,
                updates=self._updates,
                telemetry=None,
                async_runner=async_runner,
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
            )
            return AgentSession(session_id=session_id, core=core)

        def _load_persisted_memory(self, session: AgentSession) -> None:
            request_json = getattr(self._authorization, "authorized_json_request", None)
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
            request_json = getattr(self._authorization, "authorized_json_request", None)
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
            self, *, workspace_root: Path, session_id: str
        ) -> AgentConfig:
            # Build from self._current_config, NOT the module-level `config`
            # closure: _apply_server_config writes the assigned agent profile
            # into _current_config, and reading the original file config here
            # would silently drop the server's model/provider/tool overrides
            # for every session created after authentication — i.e. the A/B
            # assignment would appear to work (it's logged) but never actually
            # take effect on a new session.
            base_config = self._current_config
            telemetry_headers = self._authorization.telemetry_headers()
            provider_auth_headers = (
                telemetry_headers
                if base_config.adapter.provider.kind == "code4me_backend"
                else base_config.adapter.provider.auth_headers
            )
            upload_config = base_config.upload
            backend_url = getattr(self._authorization, "backend_url", None)
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
                upload=replace(upload_config, auth_headers=telemetry_headers),
                adapter=replace(
                    base_config.adapter,
                    provider=replace(
                        base_config.adapter.provider,
                        auth_headers=provider_auth_headers,
                    ),
                ),
            )

        async def _create_or_load_session(
            self,
            *,
            cwd: str,
            session_id: str,
            event_type: str,
        ) -> AgentSession:
            await self._require_authenticated()
            workspace_root = _resolve_session_cwd(cwd)
            if workspace_root != self._authorized_workspace:
                raise RequestError.auth_required({"reason": "workspace_not_authorized"})
            session_config = self._session_config(
                workspace_root=workspace_root,
                session_id=session_id,
            )
            session = self._build_session(
                session_config=session_config, session_id=session_id
            )
            await asyncio.to_thread(self._load_persisted_memory, session)
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
                        "client_capabilities": _normalize_capabilities(
                            self._client_capabilities
                        ),
                        **_prompt_backend_state(session),
                    },
                )
            return session

        async def initialize(
            self,
            protocol_version: int,
            client_capabilities: object | None = None,
            **kwargs: Any,
        ) -> InitializeResponse:
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
                protocol_version=protocol_version,
                agent_capabilities=AgentCapabilities(
                    loadSession=True,
                    promptCapabilities=PromptCapabilities(
                        image=False,
                        audio=False,
                        embeddedContext=False,
                    ),
                    sessionCapabilities=SessionCapabilities(
                        resume=SessionResumeCapabilities(),
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
                    AuthMethod(
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
            from code4me2_agent.command_tools import WorkspaceCommandTools

            self._bootstrap_core._config = new_config
            self._bootstrap_core._adapter._config = new_config
            self._bootstrap_core.command_tools._config = new_config
            self._bootstrap_core.command_tools._allowlisted_commands = set(
                new_config.commands.allowlisted_commands
            )
            self._bootstrap_core.file_tools._config = new_config
            for session_id, session in self._sessions.items():
                session.core._config = new_config
                session.core._adapter._config = new_config
                session.core.command_tools._config = new_config
                session.core.command_tools._allowlisted_commands = set(
                    new_config.commands.allowlisted_commands
                )
                session.core.file_tools._config = new_config
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
            **kwargs: Any,
        ) -> NewSessionResponse:
            session_id = str(
                kwargs.get("session_id") or kwargs.get("sessionId") or uuid4().hex
            )
            await self._create_or_load_session(
                cwd=cwd,
                session_id=session_id,
                event_type="agent.acp.session_created",
            )
            return NewSessionResponse(session_id=session_id)

        async def load_session(
            self,
            cwd: str,
            session_id: str,
            **kwargs: Any,
        ) -> LoadSessionResponse:
            await self._create_or_load_session(
                cwd=cwd,
                session_id=session_id,
                event_type="agent.acp.session_loaded",
            )
            return LoadSessionResponse()

        async def resume_session(
            self,
            cwd: str,
            session_id: str,
            **kwargs: Any,
        ) -> ResumeSessionResponse:
            await self._create_or_load_session(
                cwd=cwd,
                session_id=session_id,
                event_type="agent.acp.session_resumed",
            )
            return ResumeSessionResponse()

        async def cancel(
            self,
            session_id: str,
            **kwargs: Any,
        ) -> None:
            await self._require_authenticated()
            self._cancelled_sessions.add(session_id)

        async def prompt(
            self,
            prompt: list[Any],
            session_id: str,
            message_id: str | None = None,
            **kwargs: Any,
        ) -> PromptResponse:
            await self._require_authenticated()
            try:
                await asyncio.to_thread(self._authorization.validate)
            except AcpAuthorizationFailure:
                self._authorized_workspace = None
                raise RequestError.auth_required(
                    {"reason": "authorization_rejected"}
                ) from None
            request_id = message_id or uuid4().hex
            if session_id in self._cancelled_sessions:
                self._cancelled_sessions.discard(session_id)
                return PromptResponse(
                    stop_reason="cancelled",
                    user_message_id=message_id,
                )
            session = self._sessions.get(session_id)
            if session is None:
                raise RequestError.auth_required({"reason": "unknown_session"})
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
            # 1
            logger.info("Prompt text: %s", prompt_text)
            result = await asyncio.to_thread(
                session.core.handle_prompt,
                prompt_text,
                request_id,
                message_id,
            )
            await asyncio.to_thread(self._save_persisted_memory, session)
            # n
            await self._conn.session_update(
                session_id=session_id,
                update=self._updates.agent_message(
                    result.final_response,
                    metadata={
                        "code4me2": {
                            "phase": "completed",
                            "durationMs": result.duration_ms,
                        }
                    },
                ),
                source="code4me2_agent",
            )
            return PromptResponse(
                stop_reason=_acp_stop_reason(result.stop_reason),
                user_message_id=message_id,
            )

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

    await run_agent(create_acp_agent(config), use_unstable_protocol=True)
