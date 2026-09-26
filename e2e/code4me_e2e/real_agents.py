"""Host detection and identity probe for participant-installed (BYOA) agents.

Goose and Codex are ``BYOA_EXTERNAL``: the participant installs the executable,
so the harness may only ever run an agent it can *identify*. This module owns:

* discovery (explicit/settings override, ``CODE4ME_E2E_<FW>_EXECUTABLE``, the
  release-declared command, ``PATH``, then the same known per-user install
  locations the plugin's ``DefaultByoaAgentResolver`` documents);
* the identity probe (absolute path, size, sha256, first ``--version`` line);
* an isolated agent home + sanitized environment, so probing never consumes the
  developer's personal agent configuration or ambient provider credentials;
* typed :class:`ProbeResult` outcomes. A missing or unusable prerequisite is
  ``BLOCKED`` with a reason, never a pass and never an untyped crash.

No third-party dependency; every subprocess is bounded and process-group killed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import AgentProbeReason

__all__ = [
    "FRAMEWORK_ARGS",
    "FRAMEWORK_EXECUTABLES",
    "ENV_OVERRIDES",
    "AgentIdentity",
    "ArgvPlan",
    "DetectedExecutable",
    "ProbeResult",
    "build_env",
    "candidate_names",
    "classify_failure",
    "detect",
    "executable_identity",
    "isolated_home",
    "known_locations",
    "plan_argv",
    "probe_identity",
    "redact_failure_text",
]

#: Candidate executable names per framework, in preference order. For Codex the
#: ACP-speaking adapter is preferred: the bare Codex CLI in this workspace does
#: not speak ACP on stdio (``plan_argv`` blocks it explicitly rather than
#: hanging in an interactive session).
FRAMEWORK_EXECUTABLES: Dict[str, Tuple[str, ...]] = {
    "goose": ("goose",),
    "codex": ("codex-acp", "codex"),
}

#: Default launch argv appended after the resolved executable. ``goose acp``
#: runs Goose as an ACP agent server on stdio; ``codex-acp`` is already an ACP
#: adapter and takes no subcommand.
FRAMEWORK_ARGS: Dict[str, Tuple[str, ...]] = {
    "goose": ("acp",),
    "codex": (),
}

#: Settings override environment variables, mirroring ``agent.executable``.
ENV_OVERRIDES: Dict[str, str] = {
    "goose": "CODE4ME_E2E_GOOSE_EXECUTABLE",
    "codex": "CODE4ME_E2E_CODEX_EXECUTABLE",
}

#: Environment variables stripped before an agent launch: ambient provider
#: credentials must not silently authenticate a probe (R4).
_STRIPPED_ENV_PREFIXES = (
    "CODE4ME_", "OPENAI_", "ANTHROPIC_", "GOOSE_", "CODEX_", "GEMINI_",
    "AZURE_", "AWS_", "HUGGING_FACE_", "OLLAMA_",
)

#: Quota/limit evidence. Checked before auth because ``429`` often accompanies
#: an otherwise-valid credential.
_QUOTA_PATTERNS = (
    "quota", "rate limit", "rate_limit", "ratelimit", "too many requests",
    "429", "usage limit", "insufficient credit", "out of credits",
    "credit balance", "billing", "limit reached", "exceeded your current",
    # The research gateway's 402 as Goose 1.51 reports it over ACP
    # (``data.reason: credits_exhausted``, "add more credits") and as the
    # gateway phrases it.
    "credits_exhausted", "credits exhausted", "add more credits",
    "insufficient_quota", "quota_exhausted", "budget is used up",
)
#: Authentication evidence.
_AUTH_PATTERNS = (
    "401", "403", "unauthorized", "unauthorised", "not logged in",
    "not authenticated", "login required", "please log in", "please login",
    "codex login", "goose configure", "api key", "api_key", "apikey",
    "missing auth", "auth token", "token expired", "invalid token",
    "credentials", "authentication required", "sign in", "log in",
)

_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_KEYED_TOKEN = re.compile(r"(?i)\b(sk|pk|ghp|gho|xox[baprs])[-_]?[A-Za-z0-9_\-]{8,}")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedExecutable:
    """One resolved executable and where it was found."""

    framework: str
    path: str
    source: str  # override | env | command | path | known_location


def known_locations(home: Optional[str] = None) -> List[Path]:
    """Documented per-user install locations, in discovery order.

    Mirrors ``ByoaAgentResolver.byoaKnownLocations()`` in the plugin so the
    harness resolves the same host the participant's plugin would.
    """
    base = Path(home) if home else Path.home()
    locations = [
        base / ".local" / "bin",
        base / ".cargo" / "bin",
        base / ".npm-global" / "bin",
        base / "go" / "bin",
        base / ".local" / "share" / "goose" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
    ]
    return locations


def candidate_names(framework: str, package: Optional[str] = None) -> Tuple[str, ...]:
    """Candidate executable names for a framework or a declared package.

    A package the framework does not know falls back to its own name, exactly
    like the plugin's resolver.
    """
    if package and package.strip():
        normalized = package.strip().lower()
        if normalized in FRAMEWORK_EXECUTABLES:
            return FRAMEWORK_EXECUTABLES[normalized]
        return (normalized,)
    return FRAMEWORK_EXECUTABLES.get(framework, (framework,))


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_candidate(candidate: str, path_env: Optional[str]) -> Optional[Tuple[Path, str]]:
    """Locate one candidate: a path is used directly, a bare name is searched."""
    if not candidate or not candidate.strip():
        return None
    candidate = candidate.strip()
    if os.sep in candidate or (os.altsep and os.altsep in candidate) or os.path.isabs(candidate):
        path = Path(candidate).expanduser()
        return (path, "override") if _is_executable(path) else None
    found = shutil.which(candidate, path=path_env)
    if found:
        path = Path(found)
        if _is_executable(path):
            return path, "path"
    for base in known_locations(os.environ.get("HOME")):
        path = base / candidate
        if _is_executable(path):
            return path, "known_location"
    return None


def detect(
    framework: str,
    *,
    executable: Optional[str] = None,
    command: Optional[str] = None,
    package: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    path_env: Optional[str] = None,
) -> Optional[DetectedExecutable]:
    """Resolve the executable for ``framework``, or ``None``.

    Discovery order (first match wins): explicit ``executable`` (settings
    override), the framework's env override, the release-declared ``command``,
    then each candidate file name on ``PATH`` and in the known locations.
    """
    environment = os.environ if env is None else env
    search_path = path_env if path_env is not None else environment.get("PATH")

    if executable and executable.strip():
        resolved = _resolve_candidate(executable, search_path)
        if resolved is not None:
            path, source = resolved
            return DetectedExecutable(framework, str(path), "override" if source == "override" else source)
        return None

    override_var = ENV_OVERRIDES.get(framework)
    if override_var and environment.get(override_var, "").strip():
        resolved = _resolve_candidate(environment[override_var], search_path)
        if resolved is not None:
            path, _source = resolved
            return DetectedExecutable(framework, str(path), "env")
        return None

    if command and command.strip():
        resolved = _resolve_candidate(command, search_path)
        if resolved is not None:
            path, _source = resolved
            return DetectedExecutable(framework, str(path), "command")
        return None

    for candidate in candidate_names(framework, package):
        resolved = _resolve_candidate(candidate, search_path)
        if resolved is not None:
            path, source = resolved
            return DetectedExecutable(framework, str(path), source)
    return None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentIdentity:
    """The exact executable a probe would launch."""

    framework: str
    path: str
    size: int
    sha256: str
    version: Optional[str]
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "framework": self.framework,
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "version": self.version,
            "source": self.source,
        }


@dataclass(frozen=True)
class ProbeResult:
    """Typed outcome of a detection/identity/protocol probe.

    ``status`` is ``PASS`` only when every required check ran. ``BLOCKED``
    carries an :class:`AgentProbeReason` value; absent prerequisites, missing
    authentication, quota exhaustion and protocol failures can never pass.
    """

    framework: str
    status: str
    reason: Optional[str] = None
    detail: str = ""
    identity: Optional[AgentIdentity] = None
    checks: Tuple[Dict[str, Any], ...] = ()
    protocol: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @classmethod
    def ok(
        cls,
        framework: str,
        identity: Optional[AgentIdentity] = None,
        *,
        checks: Sequence[Mapping[str, Any]] = (),
        protocol: Optional[Mapping[str, Any]] = None,
    ) -> "ProbeResult":
        return cls(
            framework=framework,
            status="PASS",
            identity=identity,
            checks=tuple(dict(check) for check in checks),
            protocol=dict(protocol or {}),
        )

    @classmethod
    def blocked(
        cls,
        framework: str,
        reason: AgentProbeReason,
        detail: str,
        *,
        identity: Optional[AgentIdentity] = None,
        checks: Sequence[Mapping[str, Any]] = (),
        protocol: Optional[Mapping[str, Any]] = None,
    ) -> "ProbeResult":
        return cls(
            framework=framework,
            status="BLOCKED",
            reason=reason.value,
            detail=detail,
            identity=identity,
            checks=tuple(dict(check) for check in checks),
            protocol=dict(protocol or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "framework": self.framework,
            "status": self.status,
            "reason": self.reason,
            "detail": self.detail,
            "checks": [dict(check) for check in self.checks],
            "protocol": dict(self.protocol),
        }
        if self.identity is not None:
            payload["identity"] = self.identity.to_dict()
        return payload


def executable_identity(
    path: Path,
    *,
    framework: str,
    source: str,
    version_timeout: float = 5.0,
) -> AgentIdentity:
    """Hash and identify an executable without launching a session."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return AgentIdentity(
        framework=framework,
        path=str(path.resolve()),
        size=path.stat().st_size,
        sha256=digest.hexdigest(),
        version=capture_version(path, timeout=version_timeout),
        source=source,
    )


def capture_version(path: Path, *, timeout: float = 5.0) -> Optional[str]:
    """First non-blank line of ``<path> --version``, or ``None``.

    Bounded and process-group killed: a wrapper that ignores ``--version`` and
    starts a server cannot stall the probe. ``stdin`` is closed so a
    stdio-reading process sees EOF and exits.
    """
    try:
        child = subprocess.Popen(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        output, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, 9)
        except ProcessLookupError:
            pass
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None
    if child.returncode != 0:
        # No ``--version`` support (e.g. an argparse usage error): no version,
        # rather than a usage line recorded as one.
        return None
    for line in (output or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return None


def probe_identity(
    framework: str,
    *,
    executable: Optional[str] = None,
    command: Optional[str] = None,
    package: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    path_env: Optional[str] = None,
) -> ProbeResult:
    """Detect + identify the host agent; ``missing_binary`` when unusable."""
    found = detect(
        framework,
        executable=executable,
        command=command,
        package=package,
        env=env,
        path_env=path_env,
    )
    if found is None:
        searched = ", ".join(candidate_names(framework, package))
        return ProbeResult.blocked(
            framework,
            AgentProbeReason.MISSING_BINARY,
            f"no {framework} executable found (searched: {searched}); install it or set "
            f"{ENV_OVERRIDES.get(framework, 'the agent executable override')}",
            protocol={"searched": searched},
        )
    path = Path(found.path)
    if not path.is_file() or not os.access(path, os.X_OK):
        return ProbeResult.blocked(
            framework,
            AgentProbeReason.MISSING_BINARY,
            f"{path} is not an executable file",
        )
    identity = executable_identity(path, framework=framework, source=found.source)
    return ProbeResult.ok(framework, identity)


# ---------------------------------------------------------------------------
# Launch plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgvPlan:
    """The exact argv a probe would spawn, or a typed block."""

    argv: List[str]
    blocked_reason: Optional[AgentProbeReason] = None
    blocked_detail: str = ""

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None


def plan_argv(
    framework: str,
    identity: AgentIdentity,
    configured: Optional[Sequence[str]] = None,
) -> ArgvPlan:
    """Build the full argv; never a shell string.

    ``goose`` default is ``["<goose>", "acp"]``. For Codex, only an ACP adapter
    (``codex-acp``) is launched by default: the bare CLI in this workspace does
    not speak ACP on stdio, so launching it would hang in an interactive
    session. An explicit ``configured`` argv (release ``agent_command_args``)
    is always honored.
    """
    if configured is not None:
        args = [str(item) for item in configured]
    else:
        args = list(FRAMEWORK_ARGS.get(framework, ()))
    argv = [identity.path, *args]
    if not argv or not argv[0].strip():
        return ArgvPlan(
            argv=[],
            blocked_reason=AgentProbeReason.MISSING_BINARY,
            blocked_detail="the resolved executable entry is empty",
        )
    name = Path(identity.path).name.lower()
    if framework == "codex" and configured is None and not name.startswith("codex-acp"):
        return ArgvPlan(
            argv=argv,
            blocked_reason=AgentProbeReason.PROTOCOL,
            blocked_detail=(
                f"resolved {identity.path!r} is the Codex CLI, which does not speak ACP "
                "on stdio; install codex-acp or point CODE4ME_E2E_CODEX_EXECUTABLE / "
                "agent.executable at it"
            ),
        )
    return ArgvPlan(argv=argv)


# ---------------------------------------------------------------------------
# Isolated environment
# ---------------------------------------------------------------------------


def isolated_home(run_dir: Path, framework: str, override: Optional[str] = None) -> Path:
    """Create the per-probe agent home; never the developer's own home."""
    home = Path(override).expanduser() if override else Path(run_dir) / "agent-home" / framework
    if home.exists() and any(home.iterdir()):
        raise ValueError("Agent home must be empty; use a fresh run directory instead of existing agent credentials")
    if home.is_symlink():
        raise ValueError("Agent home must not be a symlink")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


def build_env(
    home: Path,
    *,
    base_env: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Environment for an agent launch.

    Ambient provider/agent credentials are removed so an agent cannot be
    silently authenticated by the developer's shell; ``HOME`` and the XDG
    locations point at the isolated home. Values are never logged.
    """
    source = os.environ if base_env is None else base_env
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith(_STRIPPED_ENV_PREFIXES)
    }
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / ".codex")
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def classify_failure(text: str) -> AgentProbeReason:
    """Classify agent output into a typed blocked reason (fail closed).

    Quota evidence wins over auth (a ``429`` with a valid credential is not an
    authentication failure); anything unrecognized is a protocol failure.
    """
    lowered = (text or "").lower()
    if any(pattern in lowered for pattern in _QUOTA_PATTERNS):
        return AgentProbeReason.QUOTA
    if any(pattern in lowered for pattern in _AUTH_PATTERNS):
        return AgentProbeReason.MISSING_AUTH
    return AgentProbeReason.PROTOCOL


def redact_failure_text(text: str, *, limit: int = 400) -> str:
    """Bounded, best-effort redaction for a diagnostic snippet."""
    collapsed = " ".join((text or "").split())
    collapsed = _BEARER.sub(r"\1<redacted>", collapsed)
    collapsed = _KEYED_TOKEN.sub("<redacted>", collapsed)
    collapsed = _LONG_OPAQUE.sub("<redacted>", collapsed)
    return collapsed[:limit]


def describe(identity: Optional[AgentIdentity]) -> Dict[str, Any]:
    """A log-safe identity summary (no environment values)."""
    return asdict(identity) if identity is not None else {}
