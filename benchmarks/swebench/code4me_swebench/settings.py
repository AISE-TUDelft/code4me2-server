"""Run settings and the agent configuration they produce."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Programs the agent may launch inside the task container. ``bash``/``sh``
# are included, so this is not a sandbox: the container is.
DEFAULT_COMMANDS = (
    "python", "python3", "pytest", "pip", "git", "bash", "sh",
    "ls", "cat", "head", "tail", "grep", "find", "sed", "awk", "wc", "sort",
    "uniq", "diff", "echo", "pwd", "mkdir", "touch", "rm", "mv", "cp", "chmod",
    "which", "make", "env", "true",
)

# Every agent tool except ask_user: nobody answers questions in a benchmark.
DEFAULT_TOOLS = (
    "read_file", "create_file", "write_file", "replace_text", "edit_file",
    "apply_patch", "delete_file", "move_file", "list_files", "glob_files",
    "grep_files", "search_files", "run_command", "update_plan",
)

# Paths inside the task container.
WORKSPACE = "/testbed"
RUNTIME_MOUNT = "/opt/code4me"
TASK_DIR = "/tmp/code4me"
OUT_DIR = f"{TASK_DIR}/out"


@dataclass(frozen=True)
class RunSettings:
    # OpenCode Go model id. V4.1 Flash (2026-09-10) has a larger Go quota than
    # V4 Flash (2026-07-31); runs before 2026-10-09 used deepseek-v4-flash.
    model: str = "deepseek-v4.1-flash"
    base_url: str = "https://opencode.ai/zen/go/v1"
    # Name of the host environment variable holding the provider key. Only the
    # name is written to disk; the value is passed to the container at exec time.
    api_key_env: str = "OPENCODE_GO_API_KEY"
    temperature: float | None = None
    max_output_tokens: int | None = None
    provider_timeout_s: float = 300.0
    # Hard wall-clock limit per model request (retried like a timeout); the read
    # timeout alone never fires while DeepSeek sends keep-alive blank lines.
    request_deadline_s: float = 300.0
    # Model calls per task ("step budget"); summarisation calls do not count.
    max_iterations: int = 100
    # Conversation tokens kept per model request (the agent calibrates its
    # chars/4 estimate against the provider's counts). DeepSeek V4 Flash takes
    # 1M; 400k leaves trimming for genuine outliers: mini-swe-agent's largest
    # request on SWE-rebench was 156k. The agent's study default is unchanged.
    context_tokens: int = 400_000
    command_timeout_s: float = 120.0
    max_command_timeout_s: float = 600.0
    max_command_output_bytes: int = 16_384
    # Wall-clock limit for one task; the agent is killed and its diff kept.
    task_timeout_s: float = 3_600.0
    commands: tuple[str, ...] = DEFAULT_COMMANDS
    tools: tuple[str, ...] = DEFAULT_TOOLS
    # HarnessOptions overrides (empty = the agent's shipped defaults).
    harness_options: dict[str, object] = field(default_factory=dict)
    platform: str = "linux/amd64"
    # Task containers reach the model API host and nothing else (no GitHub,
    # PyPI, git remotes): agents otherwise look up the upstream fix.
    network: str = "model-api-only"

    @property
    def model_name_or_path(self) -> str:
        return f"code4me2-agent__{self.model}"

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["commands"] = list(self.commands)
        data["tools"] = list(self.tools)
        return data


def agent_config(settings: RunSettings, *, session_id: str) -> dict[str, object]:
    """The agent's config-file JSON (``AgentConfig.from_file`` format)."""
    provider: dict[str, object] = {
        "kind": "openai",
        "base_url": settings.base_url,
        "model": settings.model,
        "api_key_env": settings.api_key_env,
        "timeout_seconds": settings.provider_timeout_s,
        "request_deadline_seconds": settings.request_deadline_s,
    }
    if settings.temperature is not None:
        provider["temperature"] = settings.temperature
    if settings.max_output_tokens is not None:
        provider["max_output_tokens"] = settings.max_output_tokens
    return {
        "workspace_root": WORKSPACE,
        "session_id": session_id,
        # Headless: nobody answers, so a turn that ends announcing more work is
        # continued (at most twice). IDE sessions never do this.
        "autonomous": True,
        "adapter": {
            "name": "openai_compatible_react",
            "max_iterations": settings.max_iterations,
            "provider": provider,
            "memory_window": {
                "scope": "session",
                "strategy": "token_window",
                "max_messages": 1_000,
                "max_tokens": settings.context_tokens,
            },
        },
        "commands": {
            "allowlist": list(settings.commands),
            "timeout_seconds": settings.command_timeout_s,
            "max_timeout_seconds": settings.max_command_timeout_s,
            "max_output_bytes": settings.max_command_output_bytes,
        },
        "harness_options": dict(settings.harness_options),
        "telemetry": {
            "trace_path": f"{OUT_DIR}/trace.jsonl",
            "raw_capture_enabled": True,
        },
    }
