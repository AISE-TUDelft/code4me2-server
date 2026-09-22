from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class UploadConfig:
    enabled: bool = False
    ingest_url: str | None = None
    batch_size: int = 50
    timeout_seconds: float = 5.0
    auth_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandConfig:
    allowlisted_commands: list[str] = field(default_factory=list)
    timeout_seconds: float = 10.0
    max_output_bytes: int = 16384


@dataclass(frozen=True)
class MemoryWindowConfig:
    scope: str = "prompt"
    strategy: str = "last_messages"
    max_messages: int = 12
    max_tokens: int = 16000


@dataclass(frozen=True)
class FakeProviderConfig:
    enabled: bool = False
    script: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    """Where this agent sends its model calls.

    There is deliberately **no hardcoded model or provider default here.** The
    backend is authoritative: after ACP authentication the runtime fetches
    ``GET /api/acp/agent-config`` and overwrites these fields from the agent
    profile the user is assigned to (see ``AgentConfig.with_server_overrides``).
    Baking in a local Ollama default — as the original did — meant a study
    assignment could be silently ignored whenever the local config disagreed,
    which is exactly the failure the A/B design can't tolerate.

    ``kind`` selects how the call is routed:

    * ``code4me_backend`` — through the backend's own OpenAI-compatible bridge.
      Used when the assigned profile leaves ``base_url`` unset, so the backend
      picks the upstream and the call is observable server-side too.
    * ``openai`` — straight to the ``base_url`` the profile names. Any
      OpenAI-compatible endpoint qualifies (Ollama, Groq, OpenRouter, OpenAI);
      per merge decision 6 the wire format is the only requirement.

    ``api_key_env`` is the *name* of the environment variable holding the
    credential, never the credential itself: the backend sends only the name
    (``api_key_ref``) and the key is resolved here, in the agent's own process.
    """

    kind: str = "code4me_backend"
    # Empty = not yet configured. Filled from the server's agent-config; for
    # `code4me_backend` this is the backend's own root URL.
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    timeout_seconds: float = 90.0
    auth_headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        """Whether this provider has enough to make a call."""
        return bool(self.base_url.strip() and self.model.strip())


@dataclass(frozen=True)
class AdapterConfig:
    name: str = "deterministic_echo"
    max_iterations: int = 8
    memory_window: MemoryWindowConfig = field(default_factory=MemoryWindowConfig)
    fake_provider: FakeProviderConfig = field(default_factory=FakeProviderConfig)
    provider: OpenAICompatibleProviderConfig = field(
        default_factory=OpenAICompatibleProviderConfig
    )
    # Researcher-authored system prompt override. None = use the built-in
    # default; when set it fully replaces the default prompt text.
    system_prompt: str | None = None


@dataclass(frozen=True)
class ServerAgentConfig:
    """The runtime configuration the backend hands down.

    Mirrors the ``GET /api/acp/agent-config`` response, which derives every
    field from the agent profile the user is assigned to. All fields are
    optional so a partial response (or an older backend) leaves the
    corresponding local value untouched rather than blanking it.
    """

    agent_profile: str | None = None
    framework_version: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_ref: str | None = None
    commands_allowlist: list[str] | None = None
    tools: list[str] | None = None
    max_iterations: int | None = None
    max_context_tokens: int | None = None
    approval_policy: str | None = None
    temperature: float | None = None
    # Researcher-authored system prompt for the assigned arm. None = use the
    # runtime's built-in default prompt; when set it fully replaces it (the
    # runtime only appends the workspace root for technical correctness).
    system_prompt: str | None = None
    # Advisory: the server enforces content storage itself. Used only to avoid
    # transmitting content that would be discarded anyway — never to enable
    # capture, which the agent has no power to do.
    store_agent_content: bool = True

    @classmethod
    def from_payload(cls, payload: dict) -> "ServerAgentConfig":
        """Parse an agent-config response, ignoring anything malformed.

        Every field is validated independently: one bad value costs that
        override, not the whole config, because a partially-usable server
        config still beats falling back to a local guess.
        """

        def _clean_str(key: str) -> str | None:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            return None

        def _clean_str_list(key: str) -> list[str] | None:
            value = payload.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            return None

        def _clean_positive_int(key: str) -> int | None:
            value = payload.get(key)
            if isinstance(value, bool):
                return None
            if isinstance(value, int) and value >= 1:
                return value
            return None

        temperature = payload.get("temperature")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            temperature = None

        return cls(
            agent_profile=_clean_str("agent_profile"),
            framework_version=_clean_str("framework_version"),
            model=_clean_str("model"),
            base_url=_clean_str("base_url"),
            api_key_ref=_clean_str("api_key_ref"),
            commands_allowlist=_clean_str_list("commands_allowlist"),
            tools=_clean_str_list("tools"),
            max_iterations=_clean_positive_int("max_iterations"),
            max_context_tokens=_clean_positive_int("max_context_tokens"),
            approval_policy=_clean_str("approval_policy"),
            temperature=float(temperature) if temperature is not None else None,
            system_prompt=_clean_str("system_prompt"),
            store_agent_content=bool(payload.get("store_agent_content", True)),
        )

    @property
    def has_overrides(self) -> bool:
        return any(
            value is not None
            for value in (
                self.model,
                self.base_url,
                self.api_key_ref,
                self.commands_allowlist,
                self.tools,
                self.max_iterations,
                self.max_context_tokens,
                self.system_prompt,
            )
        )


@dataclass(frozen=True)
class AgentConfig:
    workspace_root: Path
    trace_path: Path
    session_id: str
    raw_capture_enabled: bool = False
    upload: UploadConfig = field(default_factory=UploadConfig)
    commands: CommandConfig = field(default_factory=CommandConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)

    def with_server_overrides(
        self, server: ServerAgentConfig, *, backend_url: str | None = None
    ) -> "AgentConfig":
        """Return a copy with the backend's assigned configuration applied.

        This is where the assigned agent profile actually takes hold of the
        runtime. Precedence is deliberate: **server wins**, and the local
        config file is only a fallback for anything the server didn't specify.

        Routing follows from whether the profile named a ``base_url``:

        * it did → talk to that endpoint directly (``kind="openai"``), which is
          what makes "any OpenAI-compatible provider" work per merge decision 6;
        * it didn't → relay through the backend (``kind="code4me_backend"``), so
          the backend chooses the upstream and also observes the call.
        """
        from dataclasses import replace as _replace

        provider = self.adapter.provider

        if server.base_url:
            provider = _replace(
                provider, kind="openai", base_url=server.base_url
            )
        elif backend_url:
            provider = _replace(
                provider,
                kind="code4me_backend",
                base_url=str(backend_url).rstrip("/"),
            )

        if server.model:
            provider = _replace(provider, model=server.model)
        if server.api_key_ref:
            provider = _replace(provider, api_key_env=server.api_key_ref)

        memory_window = self.adapter.memory_window
        if server.max_context_tokens is not None:
            memory_window = _replace(
                memory_window, max_tokens=server.max_context_tokens
            )

        adapter = _replace(
            self.adapter,
            provider=provider,
            memory_window=memory_window,
            max_iterations=server.max_iterations or self.adapter.max_iterations,
            system_prompt=server.system_prompt if server.system_prompt is not None else self.adapter.system_prompt,
        )

        commands = self.commands
        if server.commands_allowlist is not None:
            commands = _replace(
                commands, allowlisted_commands=list(server.commands_allowlist)
            )

        return _replace(self, adapter=adapter, commands=commands)

    @classmethod
    def from_file(cls, config_path: str | Path) -> "AgentConfig":
        path = Path(config_path).expanduser().resolve()
        data = json.loads(path.read_text())

        workspace_root_str = data.get("workspace_root", ".")
        workspace_root = Path(str(workspace_root_str)).expanduser()
        if not workspace_root.is_absolute():
            workspace_root = Path.cwd() / workspace_root
        workspace_root = workspace_root.resolve()

        telemetry = data.get("telemetry", {})
        raw_trace_path = telemetry.get(
            "trace_path", data.get("trace_path", ".code4me/acp-trace.jsonl")
        )
        trace_path = Path(raw_trace_path).expanduser()
        if not trace_path.is_absolute():
            trace_path = workspace_root / trace_path

        upload_data = telemetry.get("upload", {})
        raw_headers = upload_data.get("auth_headers", {})
        auth_headers: dict[str, str] = {}
        if isinstance(raw_headers, dict):
            auth_headers = {
                str(header): str(value)
                for header, value in raw_headers.items()
                if header and value is not None
            }
        bearer_token = upload_data.get("bearer_token")
        if bearer_token:
            auth_headers["Authorization"] = f"Bearer {bearer_token}"

        raw_batch_size = upload_data.get("batch_size", 50)
        try:
            batch_size = max(1, int(raw_batch_size))
        except (TypeError, ValueError):
            batch_size = 50

        raw_timeout_seconds = upload_data.get("timeout_seconds", 5.0)
        try:
            timeout_seconds = float(raw_timeout_seconds)
            if timeout_seconds <= 0:
                timeout_seconds = 5.0
        except (TypeError, ValueError):
            timeout_seconds = 5.0

        raw_ingest_url = upload_data.get("ingest_url")
        ingest_url = str(raw_ingest_url).strip() if raw_ingest_url else None
        if not ingest_url:
            ingest_url = None

        upload = UploadConfig(
            enabled=bool(upload_data.get("enabled", False)),
            ingest_url=ingest_url,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            auth_headers=auth_headers,
        )

        commands_data = data.get("commands", data.get("command_tools", {}))
        raw_allowlist = commands_data.get(
            "allowlist", commands_data.get("allowlisted_commands", [])
        )
        allowlisted_commands: list[str] = []
        if isinstance(raw_allowlist, list):
            allowlisted_commands = [
                str(command).strip()
                for command in raw_allowlist
                if isinstance(command, str) and command.strip()
            ]

        raw_command_timeout = commands_data.get("timeout_seconds", 10.0)
        try:
            command_timeout_seconds = float(raw_command_timeout)
            if command_timeout_seconds <= 0:
                command_timeout_seconds = 10.0
        except (TypeError, ValueError):
            command_timeout_seconds = 10.0

        raw_max_output_bytes = commands_data.get("max_output_bytes", 16384)
        try:
            max_output_bytes = max(1, int(raw_max_output_bytes))
        except (TypeError, ValueError):
            max_output_bytes = 16384

        commands = CommandConfig(
            allowlisted_commands=allowlisted_commands,
            timeout_seconds=command_timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

        adapter_data = data.get("adapter", {})
        if not isinstance(adapter_data, dict):
            adapter_data = {}

        raw_max_iterations = adapter_data.get("max_iterations", 8)
        try:
            max_iterations = max(1, int(raw_max_iterations))
        except (TypeError, ValueError):
            max_iterations = 8

        memory_window_data = adapter_data.get("memory_window", {})
        if not isinstance(memory_window_data, dict):
            memory_window_data = {}
        memory_scope = str(memory_window_data.get("scope", "prompt")).strip()
        if memory_scope not in {"prompt", "session"}:
            memory_scope = "prompt"
        memory_strategy = str(
            memory_window_data.get("strategy", "last_messages")
        ).strip()
        if memory_strategy not in {"last_messages", "token_window"}:
            memory_strategy = "last_messages"
        raw_max_messages = memory_window_data.get("max_messages", 12)
        try:
            max_messages = max(1, int(raw_max_messages))
        except (TypeError, ValueError):
            max_messages = 12
        raw_max_tokens = memory_window_data.get("max_tokens", 4000)
        try:
            max_tokens = max(1, int(raw_max_tokens))
        except (TypeError, ValueError):
            max_tokens = 4000

        fake_provider_data = adapter_data.get("fake_provider", {})
        if not isinstance(fake_provider_data, dict):
            fake_provider_data = {}
        raw_script = fake_provider_data.get("script", [])
        script = raw_script if isinstance(raw_script, list) else []

        provider_data = adapter_data.get("provider", {})
        if not isinstance(provider_data, dict):
            provider_data = {}
        raw_provider_headers = provider_data.get("auth_headers", {})
        provider_auth_headers: dict[str, str] = {}
        if isinstance(raw_provider_headers, dict):
            provider_auth_headers = {
                str(header): str(value)
                for header, value in raw_provider_headers.items()
                if header and value is not None
            }

        adapter = AdapterConfig(
            name=str(adapter_data.get("name", "deterministic_echo")).strip()
            or "deterministic_echo",
            max_iterations=max_iterations,
            memory_window=MemoryWindowConfig(
                scope=memory_scope,
                strategy=memory_strategy,
                max_messages=max_messages,
                max_tokens=max_tokens,
            ),
            fake_provider=FakeProviderConfig(
                enabled=bool(fake_provider_data.get("enabled", False)),
                script=script,
            ),
            # No provider defaults are invented here. Anything the config file
            # omits stays empty and is filled from the backend's agent-config
            # after authentication (see AgentConfig.with_server_overrides), so
            # the assigned profile — not a local fallback — decides the model
            # and endpoint.
            provider=OpenAICompatibleProviderConfig(
                kind=str(provider_data.get("kind", "code4me_backend")).strip()
                or "code4me_backend",
                base_url=str(provider_data.get("base_url", "")).strip(),
                model=str(provider_data.get("model", "")).strip(),
                api_key_env=str(provider_data.get("api_key_env", "")).strip(),
                timeout_seconds=_positive_float(
                    provider_data.get("timeout_seconds", 90.0),
                    default=90.0,
                ),
                auth_headers=provider_auth_headers,
            ),
        )

        return cls(
            workspace_root=workspace_root,
            trace_path=trace_path.resolve(),
            session_id=data.get("session_id", uuid4().hex),
            raw_capture_enabled=bool(telemetry.get("raw_capture_enabled", False)),
            upload=upload,
            commands=commands,
            adapter=adapter,
        )


def _positive_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default
