from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True)
class UploadConfig:
    enabled: bool = False
    ingest_url: str | None = None
    batch_size: int = 50
    timeout_seconds: float = 5.0
    auth_headers: dict[str, str] = field(default_factory=dict)
    auth_headers_provider: Callable[[], dict[str, str]] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class CommandConfig:
    allowlisted_commands: list[str] = field(default_factory=list)
    # Default per-command timeout; the model may raise it per call up to
    # ``max_timeout_seconds`` (builds and test suites routinely exceed 10 s).
    timeout_seconds: float = 120.0
    max_timeout_seconds: float = 600.0
    max_output_bytes: int = 16384


@dataclass(frozen=True)
class MemoryWindowConfig:
    scope: str = "prompt"
    strategy: str = "last_messages"
    max_messages: int = 12
    # Estimated tokens (chars/4) of conversation kept per request; the server
    # profile's max_context_tokens overrides this.
    max_tokens: int = 32000


@dataclass(frozen=True)
class FakeProviderConfig:
    enabled: bool = False
    script: list[dict[str, object]] = field(default_factory=list)


PROMPT_PROFILES = frozenset({"auto", "default", "openai", "anthropic", "gemini"})

_HARNESS_BOOL_OPTIONS = (
    "self_review",
    "verify_on_stop",
    "context_summarization",
    "parallel_tools",
    "project_instructions",
    "read_before_edit",
    "syntax_check",
    "loop_guard",
    "instruction_reminders",
    "test_output_summary",
)


@dataclass(frozen=True)
class HarnessOptions:
    """Runtime behaviour switches an agent profile can freeze (``harness_options``).

    Every default is the runtime's recommended behaviour; a profile only lists
    the switches it changes. The server validates the keys strictly; the
    runtime ignores keys it does not know so an older runtime never refuses a
    newer policy for an option it cannot honour anyway.
    """

    self_review: bool = True
    verify_on_stop: bool = True
    # argv run by the runtime before the final answer when files changed;
    # None means "nudge the model to verify" instead.
    verify_command: tuple[str, ...] | None = None
    context_summarization: bool = True
    parallel_tools: bool = True
    prompt_profile: str = "auto"
    project_instructions: bool = True
    read_before_edit: bool = True
    syntax_check: bool = True
    loop_guard: bool = True
    instruction_reminders: bool = True
    test_output_summary: bool = True

    def with_overrides(self, overrides: dict[str, object] | None) -> "HarnessOptions":
        if not overrides:
            return self
        from dataclasses import replace as _replace

        return _replace(self, **overrides)


def parse_harness_overrides(value: object, *, strict: bool) -> dict[str, object] | None:
    """Validate a ``harness_options`` mapping into ``HarnessOptions`` overrides.

    ``strict`` (the managed policy) raises ``ValueError`` on a known key with a
    wrong type; otherwise such a key is skipped. Unknown keys are ignored.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        if strict:
            raise ValueError("harness_options must be an object.")
        return None
    overrides: dict[str, object] = {}
    for key in _HARNESS_BOOL_OPTIONS:
        if key not in value:
            continue
        if isinstance(value[key], bool):
            overrides[key] = value[key]
        elif strict:
            raise ValueError(f"harness_options.{key} must be a boolean.")
    if "prompt_profile" in value:
        profile = value["prompt_profile"]
        if isinstance(profile, str) and profile.strip().lower() in PROMPT_PROFILES:
            overrides["prompt_profile"] = profile.strip().lower()
        elif strict:
            raise ValueError("harness_options.prompt_profile is not a known profile.")
    if "verify_command" in value:
        command = value["verify_command"]
        if command is None:
            overrides["verify_command"] = None
        elif (
            isinstance(command, list)
            and command
            and all(isinstance(item, str) and item for item in command)
            and command[0].strip()
        ):
            # Same rule as the server (agents/tools.py): non-empty arguments; a
            # whitespace argument such as " " is a legitimate argv item.
            overrides["verify_command"] = tuple(str(item) for item in command)
        elif strict:
            raise ValueError("harness_options.verify_command must be a non-empty argv list.")
    return overrides


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
    temperature: float | None = None
    auth_headers: dict[str, str] = field(default_factory=dict)
    # Forwarded as ``max_tokens`` only when set: newer OpenAI models reject it in
    # favour of ``max_completion_tokens`` and the relay hides which upstream is used.
    max_output_tokens: int | None = None

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
    # Researcher-authored system prompt override (ported from origin/sys_prompt).
    # None = the runtime's built-in prompt; when set it replaces the persona
    # paragraph, while the operational tool/policy instructions are kept.
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
    # Researcher-authored system prompt for the assigned arm (agent-config and
    # the managed run policy both carry it). None = built-in default.
    system_prompt: str | None = None
    # Default per-command timeout frozen with the profile. None = runtime default.
    command_timeout_seconds: int | None = None
    # Validated ``HarnessOptions`` overrides frozen with the profile.
    harness_options: dict[str, object] | None = None
    # Advisory: the server enforces content storage itself. Used only to avoid
    # transmitting content that would be discarded anyway — never to enable
    # capture, which the agent has no power to do.
    store_agent_content: bool = True

    @classmethod
    def from_managed_payload(cls, payload: dict) -> "ServerAgentConfig":
        """Parse a complete protocol-v1 policy, failing closed on bad fields."""
        nested_policy = payload.get("policy_snapshot") or payload.get("policy")
        merged = {**payload, **nested_policy} if isinstance(nested_policy, dict) else payload
        protocol = merged.get("managed_protocol_version", merged.get("version"))
        tools = merged.get("tools")
        commands = merged.get("commands_allowlist")
        iterations = merged.get("max_iterations")
        context_tokens = merged.get("max_context_tokens")
        temperature = merged.get("temperature")
        required_text = ("agent_profile", "framework_version", "model", "transport")
        if protocol != "1" or any(
            not isinstance(merged.get(key), str) or not merged[key].strip()
            for key in required_text
        ):
            raise ValueError("Managed agent policy metadata is incomplete.")
        if merged["framework_version"] not in {"code4me2-agent", "code4me-agent"}:
            raise ValueError("Managed agent policy selects an unsupported runtime.")
        if merged["transport"] != "managed_backend":
            raise ValueError("Managed agent policy selects an unsafe transport.")
        if (
            not isinstance(tools, list)
            or not all(isinstance(tool, str) and tool.strip() for tool in tools)
            or not isinstance(commands, list)
            or not all(isinstance(command, str) and command.strip() for command in commands)
            or isinstance(iterations, bool)
            or not isinstance(iterations, int)
            or iterations < 1
            or isinstance(context_tokens, bool)
            or not isinstance(context_tokens, int)
            or context_tokens < 1
            or merged.get("approval_policy") not in {"auto", "per_step", "suggestion_only"}
            or isinstance(temperature, bool)
            or (
                temperature is not None
                and (
                    not isinstance(temperature, (int, float))
                    or not 0.0 <= float(temperature) <= 2.0
                )
            )
            or not isinstance(merged.get("store_agent_content"), bool)
            or (
                merged.get("system_prompt") is not None
                and not isinstance(merged.get("system_prompt"), str)
            )
            or not _valid_optional_timeout(merged.get("command_timeout_seconds"))
        ):
            raise ValueError("Managed agent policy contains invalid executable settings.")
        try:
            parse_harness_overrides(merged.get("harness_options"), strict=True)
        except ValueError as exc:
            raise ValueError(
                f"Managed agent policy contains invalid executable settings: {exc}"
            ) from None
        return cls.from_payload(merged)

    @classmethod
    def from_payload(cls, payload: dict) -> "ServerAgentConfig":
        """Parse an agent-config response, ignoring anything malformed.

        Every field is validated independently: one bad value costs that
        override, not the whole config, because a partially-usable server
        config still beats falling back to a local guess.
        """

        nested_policy = payload.get("policy_snapshot") or payload.get("policy")
        if isinstance(nested_policy, dict):
            payload = {**payload, **nested_policy}

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

        command_timeout = payload.get("command_timeout_seconds")
        if not _valid_optional_timeout(command_timeout) or command_timeout is None:
            command_timeout = None

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
            command_timeout_seconds=int(command_timeout) if command_timeout is not None else None,
            harness_options=parse_harness_overrides(payload.get("harness_options"), strict=False),
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
                self.approval_policy,
                self.temperature,
                self.system_prompt,
                self.command_timeout_seconds,
                self.harness_options,
            )
        )


def _valid_optional_timeout(value: object) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= 600
    )


@dataclass(frozen=True)
class AgentConfig:
    workspace_root: Path
    trace_path: Path
    session_id: str
    tools: list[str] | None = None
    raw_capture_enabled: bool = False
    upload: UploadConfig = field(default_factory=UploadConfig)
    commands: CommandConfig = field(default_factory=CommandConfig)
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    allowed_tools: frozenset[str] | None = None
    approval_policy: str = "auto"
    store_agent_content: bool = True
    harness: HarnessOptions = field(default_factory=HarnessOptions)
    managed_mode: bool = False
    managed_request: Callable[[str, str, dict | None], dict] | None = field(
        default=None, repr=False, compare=False
    )

    def with_server_overrides(
        self, server: ServerAgentConfig, *, backend_url: str | None = None
    ) -> "AgentConfig":
        """Return a copy with the backend's assigned configuration applied.

        This is where the assigned agent profile actually takes hold of the
        runtime. Precedence is deliberate: **server wins**, and the local
        config file is only a fallback for anything the server didn't specify.

        An authenticated runtime always relays through the backend. Provider
        credentials stay in the backend environment; the local agent holds
        only its short-lived ACP bearer token.
        """
        from dataclasses import replace as _replace

        provider = self.adapter.provider

        if self.managed_mode and backend_url:
            provider = _replace(
                provider,
                kind="managed_backend",
                base_url=str(backend_url).rstrip("/"),
                api_key_env="",
                auth_headers={},
            )
        elif backend_url:
            provider = _replace(
                provider,
                kind="code4me_backend",
                base_url=str(backend_url).rstrip("/"),
            )
        elif server.base_url:
            provider = _replace(
                provider, kind="openai", base_url=server.base_url
            )

        if server.model:
            provider = _replace(provider, model=server.model)
        if server.api_key_ref and provider.kind not in ("code4me_backend", "managed_backend"):
            provider = _replace(provider, api_key_env=server.api_key_ref)
        if server.temperature is not None:
            provider = _replace(provider, temperature=server.temperature)

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
            system_prompt=(
                server.system_prompt
                if server.system_prompt is not None
                else self.adapter.system_prompt
            ),
        )

        commands = self.commands
        if server.commands_allowlist is not None:
            from code4me2_agent.command_tools import available_commands

            commands = _replace(
                commands,
                allowlisted_commands=available_commands(server.commands_allowlist),
            )
        if server.command_timeout_seconds is not None:
            timeout = float(server.command_timeout_seconds)
            commands = _replace(
                commands,
                timeout_seconds=timeout,
                max_timeout_seconds=max(timeout, commands.max_timeout_seconds),
            )

        tools = self.tools
        allowed_tools = self.allowed_tools
        if server.tools is not None:
            tools = list(server.tools)
            allowed_tools = frozenset(server.tools)

        return _replace(
            self,
            adapter=adapter,
            commands=commands,
            tools=tools,
            allowed_tools=allowed_tools,
            approval_policy=server.approval_policy or self.approval_policy,
            store_agent_content=server.store_agent_content,
            harness=self.harness.with_overrides(server.harness_options),
        )

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

        command_timeout_seconds = _positive_float(
            commands_data.get("timeout_seconds", 120.0), default=120.0
        )
        max_command_timeout_seconds = _positive_float(
            commands_data.get("max_timeout_seconds", 600.0), default=600.0
        )

        raw_max_output_bytes = commands_data.get("max_output_bytes", 16384)
        try:
            max_output_bytes = max(1, int(raw_max_output_bytes))
        except (TypeError, ValueError):
            max_output_bytes = 16384

        commands = CommandConfig(
            allowlisted_commands=allowlisted_commands,
            timeout_seconds=command_timeout_seconds,
            max_timeout_seconds=max(command_timeout_seconds, max_command_timeout_seconds),
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
        raw_max_tokens = memory_window_data.get("max_tokens", 32000)
        try:
            max_tokens = max(1, int(raw_max_tokens))
        except (TypeError, ValueError):
            max_tokens = 32000

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
                temperature=(
                    float(provider_data["temperature"])
                    if isinstance(provider_data.get("temperature"), (int, float))
                    and not isinstance(provider_data.get("temperature"), bool)
                    else None
                ),
                auth_headers=provider_auth_headers,
                max_output_tokens=(
                    int(provider_data["max_output_tokens"])
                    if isinstance(provider_data.get("max_output_tokens"), int)
                    and not isinstance(provider_data.get("max_output_tokens"), bool)
                    and provider_data["max_output_tokens"] > 0
                    else None
                ),
            ),
        )

        harness_data = data.get("harness_options", adapter_data.get("harness_options"))
        harness = HarnessOptions().with_overrides(
            parse_harness_overrides(harness_data, strict=False)
        )

        return cls(
            workspace_root=workspace_root,
            trace_path=trace_path.resolve(),
            session_id=data.get("session_id", uuid4().hex),
            raw_capture_enabled=bool(telemetry.get("raw_capture_enabled", False)),
            upload=upload,
            commands=commands,
            adapter=adapter,
            harness=harness,
        )


def _positive_float(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default
