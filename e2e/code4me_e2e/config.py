"""Scenario configuration for the code4me-e2e harness.

Every value the harness uses is declared here, is overridable from a JSON
scenario file and from ``--set dotted.key=value``, and is documented in
``scenario.example.json`` and ``README.md``.

The defaults are chosen so ``python3 -m code4me_e2e run`` works with no arguments
against the harness's own disposable stack.
"""

from __future__ import annotations

import hashlib
import json
import platform as host_platform
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, List, Optional

#: Sentinel secret value stored in run state / reports as ``<redacted>``.
REDACTED = "<redacted>"


class AgentProbeReason(str, Enum):
    """Typed reasons a real-agent (Goose/Codex) probe can be BLOCKED.

    A missing prerequisite must surface one of these instead of a pass or a
    bare crash.
    """

    MISSING_BINARY = "missing_binary"
    MISSING_AUTH = "missing_auth"
    QUOTA = "quota"
    PROTOCOL = "protocol"


def sha256_of(*parts: str) -> str:
    """Return ``sha256:<64 lowercase hex>`` over the UTF-8 parts."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


@dataclass
class StackConfig:
    """The disposable docker-compose stack the harness provisions."""

    project_name: str = "code4me-e2e"
    backend_port: int = 28008
    db_port: int = 25432
    redis_port: int = 26379
    image: str = "code4me2-server-backend:latest"
    bootstrap_signing_secret: str = "e2e-bootstrap-secret"
    stub_secret_env: str = "E2E_STUB_API_KEY"
    stub_secret_value: str = "e2e-stub-secret-value"
    db_password: str = "postgres"
    db_name: str = "code4me_e2e"
    db_user: str = "postgres"
    #: Fixed host port for the in-process stub provider. A fixed port keeps
    #: ``--from``/``step`` resumption working across processes.
    stub_port: int = 28999


@dataclass
class AccountConfig:
    email: str
    password: str
    name: str


@dataclass
class StudyConfig:
    name: str = "E2E Synthetic Study"
    description: str = "Synthetic study created by the code4me-e2e harness."
    owner: str = "e2e-research-ops"
    start_at: Optional[str] = None
    end_at: Optional[str] = None


@dataclass
class AgentConfig:
    model: str = "e2e-stub-model"
    framework_version: str = "code4me2-agent"
    #: Supported values (see agent/profiles.py): auto | per_step | suggestion_only
    approval_policy: str = "auto"
    max_steps: int = 4
    agent_id: str = "code4me2-e2e-agent"
    release_id: str = "e2e-release-1"
    release_version: str = "1.0.0"
    artifact_digest: Optional[str] = None
    artifact_path: Optional[str] = None
    artifact_size: int = 1024
    #: Inputs of the standalone ``agent-probe`` (``--layer agents`` probes both
    #: frameworks and only honours ``CODE4ME_E2E_<FRAMEWORK>_EXECUTABLE``).
    #: ``agent_command``/``agent_package`` name a participant-installed agent the
    #: way a BYOA release declares it; ``agent_command_args`` replaces the
    #: framework's default ACP argv. ``executable`` replaces discovery
    #: (``CODE4ME_E2E_<FRAMEWORK>_EXECUTABLE`` also works) and ``agent_home``
    #: replaces the per-run isolated agent home.
    agent_command: Optional[str] = None
    agent_command_args: List[str] = field(default_factory=list)
    agent_package: Optional[str] = None
    executable: Optional[str] = None
    agent_home: Optional[str] = None
    #: Name of the agent profile created by the ``create_profile`` step. A
    #: distinct name lets one stack carry several self-consistent e2e identities
    #: (for example the ``plugin-test`` release and the ``ui-test`` release)
    #: without an existing profile pinning the wrong release artifact digest.
    profile_name: str = "e2e-profile"
    #: Tools the managed run policy allows. The managed runtime advertises its
    #: full tool set on every turn, so a profile with no tools makes the relay
    #: reject the turn with a 403 tool-policy refusal (harness scenario gap).
    tools: list = field(default_factory=lambda: [
        "read_file", "write_file", "create_file", "replace_text",
        "list_files", "search_files", "run_command",
    ])
    adapter_id: Optional[str] = None
    adapter_version: str = "1.0.0"
    adapter_digest: Optional[str] = None


@dataclass
class PlatformConfig:
    os: str = field(default_factory=lambda: {"Darwin": "macos", "Windows": "windows"}.get(host_platform.system(), "linux"))
    arch: str = field(default_factory=lambda: "aarch64" if host_platform.machine().lower() in ("arm64", "aarch64") else "x64")
    ide_build: str = "IU-243.1"
    host_kind: str = "IntelliJ IDEA"
    plugin_version: str = "e2e"


@dataclass
class MessageConfig:
    prompt: str = "Say hello in one short sentence."
    #: When null the stub provider's own deterministic token is asserted.
    expected_substring: Optional[str] = None


@dataclass
class TimeoutConfig:
    backend_ready_seconds: int = 300
    step_seconds: int = 120


@dataclass
class Scenario:
    #: Backend origin. Defaults to http://localhost:<stack.backend_port>.
    base_url: Optional[str] = None
    stack: StackConfig = field(default_factory=StackConfig)
    admin: AccountConfig = field(
        default_factory=lambda: AccountConfig(
            "e2e-admin@example.com", "AdminPass123", "E2E Admin"
        )
    )
    researcher: AccountConfig = field(
        default_factory=lambda: AccountConfig(
            "e2e-researcher@example.com", "ResearcherPass123", "E2E Researcher"
        )
    )
    participant: AccountConfig = field(
        default_factory=lambda: AccountConfig(
            "e2e-participant@example.com", "ParticipantPass123", "E2E Participant"
        )
    )
    study: StudyConfig = field(default_factory=StudyConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    message: MessageConfig = field(default_factory=MessageConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)


class ScenarioError(ValueError):
    """Raised for an unusable scenario document or override.

    ``code`` is a stable machine-readable reason (empty for plain messages).
    """

    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Loading and overrides
# ---------------------------------------------------------------------------


def _merge_into(target: Any, data: Any, path: str) -> None:
    """Recursively merge a JSON mapping onto a dataclass instance."""
    if not isinstance(data, dict):
        raise ScenarioError(f"{path or 'scenario'} must be a JSON object")
    for key, value in data.items():
        if key.startswith("$") or key.startswith("_"):
            continue  # documentation-only keys (comments) in the example scenario
        if not hasattr(target, key):
            raise ScenarioError(f"unknown scenario key {path + '.' if path else ''}{key}")
        current = getattr(target, key)
        if is_dataclass(current) and not isinstance(current, type):
            _merge_into(current, value, f"{path + '.' if path else ''}{key}")
        else:
            setattr(target, key, value)


def _coerce_override(value: str) -> Any:
    """Parse an override value as JSON when possible, else keep it a string."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def apply_overrides(scenario: Scenario, overrides: List[str]) -> None:
    """Apply ``dotted.key=value`` overrides in place."""
    for item in overrides:
        if "=" not in item:
            raise ScenarioError(f"--set expects dotted.key=value, got {item!r}")
        dotted, _, raw = item.partition("=")
        parts = dotted.strip().split(".")
        target: Any = scenario
        for part in parts[:-1]:
            if not hasattr(target, part):
                raise ScenarioError(f"unknown scenario key {dotted!r}")
            target = getattr(target, part)
        leaf = parts[-1]
        if not hasattr(target, leaf):
            raise ScenarioError(f"unknown scenario key {dotted!r}")
        setattr(target, leaf, _coerce_override(raw))


def load_scenario(path: Optional[str] = None, overrides: Optional[List[str]] = None) -> Scenario:
    """Build a scenario from defaults, an optional JSON file and overrides."""
    scenario = Scenario()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                # Strip keys the example uses for documentation only.
                raw = json.load(handle)
        except OSError as error:
            raise ScenarioError(f"cannot read scenario file {path!r}: {error}") from error
        except json.JSONDecodeError as error:
            raise ScenarioError(f"scenario file {path!r} is not valid JSON: {error}") from error
        if not isinstance(raw, dict):
            raise ScenarioError("scenario must be a JSON object")
        raw.pop("$schema", None)
        _merge_into(scenario, raw, "")
    apply_overrides(scenario, overrides or [])
    finalize_scenario(scenario)
    return scenario


def finalize_scenario(scenario: Scenario) -> Scenario:
    """Fill derived defaults and validate the document."""
    if not scenario.base_url:
        scenario.base_url = f"http://localhost:{scenario.stack.backend_port}"

    agent = scenario.agent
    platform = scenario.platform
    if not agent.artifact_digest:
        agent.artifact_digest = sha256_of(
            "code4me2://e2e/artifact", agent.agent_id, agent.release_id, platform.os, platform.arch
        )
    if not agent.artifact_path:
        agent.artifact_path = f"agents/{platform.os}-{platform.arch}/code4me2-agent"
    if not agent.adapter_id:
        agent.adapter_id = f"{agent.agent_id}-adapter"
    if not agent.adapter_digest:
        agent.adapter_digest = sha256_of(
            "code4me2://e2e/adapter", agent.agent_id, agent.release_version
        )

    _validate(scenario)
    return scenario


def _validate(scenario: Scenario) -> None:
    if not re.fullmatch(r"code4me-e2e(?:-[a-z0-9-]+)?", scenario.stack.project_name):
        raise ScenarioError("stack.project_name must be code4me-e2e or code4me-e2e-<suffix> (reserved disposable projects)")
    ports = [getattr(scenario.stack, name) for name in ("backend_port", "db_port", "redis_port", "stub_port")]
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
        raise ScenarioError("stack ports must be distinct integers from 1 to 65535")
    for role in ("admin", "researcher", "participant"):
        account: AccountConfig = getattr(scenario, role)
        if not account.email or "@" not in account.email:
            raise ScenarioError(f"{role}.email must be an email address")
        # Mirror Queries.CreateUser's validator so `create_accounts` cannot be
        # rejected by a surprising 422.
        if not re.match(r"^(?=.*[A-Z])(?=.*[a-z])(?=.*\d)\S{8,50}$", account.password):
            raise ScenarioError(
                f"{role}.password must be 8-50 chars with upper, lower and a digit"
            )
        if not (3 <= len(account.name) <= 50):
            raise ScenarioError(f"{role}.name must be 3-50 characters")
    if scenario.agent.approval_policy not in ("auto", "per_step", "suggestion_only"):
        raise ScenarioError(
            "agent.approval_policy must be one of auto, per_step, suggestion_only"
        )
    if scenario.agent.framework_version not in ("code4me2-agent", "goose", "codex"):
        raise ScenarioError("agent.framework_version must be code4me2-agent, goose or codex")
    if scenario.agent.framework_version != "code4me2-agent":
        # Workflow releases come from the packaged producer manifest; Goose/Codex
        # arms are BYOA and not managed by the relay, so this would only fail
        # later at create_profile/send_message with a server 4xx.
        raise ScenarioError(
            "the workflow layers import the packaged code4me2-agent release; Goose and Codex "
            "are exercised by `--layer agents` and `agent-probe --framework goose|codex`",
            code="FRAMEWORK_NOT_IN_WORKFLOW",
        )
    if scenario.agent.max_steps < 1:
        raise ScenarioError("agent.max_steps must be >= 1")
    args = scenario.agent.agent_command_args
    if not isinstance(args, list) or any(not isinstance(item, str) or not item or "\x00" in item for item in args):
        raise ScenarioError("agent.agent_command_args must be a list of non-empty strings without NUL",
                            code="AGENT_COMMAND_ARGS_INVALID")
    for key in ("executable", "agent_home", "agent_command", "agent_package"):
        value = getattr(scenario.agent, key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ScenarioError(f"agent.{key} must be a non-empty string or null")


def scenario_dict(scenario: Scenario) -> dict:
    """Return the scenario as a plain dict (secrets intact)."""
    return asdict(scenario)


#: Case-insensitive key fragments redacted from reports/logs.
_SECRET_KEYS = (
    "password",
    "secret",
    "token",
    "grant",
    "capability",
    "signature",
)
_SAFE_KEYS = {
    "stubsecretenv",
    "maxtokens",
    "maxcontexttokens",
    "usagetokens",
    "prompttokens",
    "completiontokens",
    "totaltokens",
    "tokenhookactivationinseconds",
    "authenticationtokenexpiresinseconds",
    "sessiontokenexpiresinseconds",
    "emailverificationtokenexpiresinseconds",
    "resetpasswordtokenexpiresinseconds",
    "tokenexpiresinseconds",
}


def is_secret_key(key: str) -> bool:
    normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
    if normalized in _SAFE_KEYS:
        return False
    return any(fragment in normalized for fragment in _SECRET_KEYS)


def redact(value: Any) -> Any:
    """Recursively replace secret-shaped values with ``<redacted>``."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_secret_key(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def sanitized_scenario(scenario: Scenario) -> dict:
    """A report-safe scenario: passwords/secrets replaced with ``<redacted>``."""
    return redact(scenario_dict(scenario))
