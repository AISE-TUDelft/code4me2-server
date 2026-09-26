"""Strict ACP stdio probe for a real participant-installed (BYOA) agent.

The managed-proxy path uses :mod:`code4me_e2e.acp` with a deterministic stub
provider. This module instead launches the *real* Goose/Codex executable the
participant installed, over stdio, and exercises the ACP handshake the IDE would
perform: ``initialize`` -> ``session/new`` -> ``session/prompt``.

Every step is validated, not assumed: a non-empty executable entry, an
``initialize`` result with a protocol version and an agent-capabilities
mapping, a non-empty session id, and a completed first prompt. A missing
binary, missing authentication, exhausted quota or protocol failure is a typed
``BLOCKED`` result and can never be reported as a pass.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Any, Dict, List, Mapping, Optional, Sequence

from . import process
from .config import AgentProbeReason
from .real_agents import (
    AgentIdentity,
    ProbeResult,
    build_env,
    classify_failure,
    isolated_home,
    plan_argv,
    probe_identity,
    redact_failure_text,
)

__all__ = ["DEFAULT_PROMPT", "AcpProbeError", "AcpRemoteError", "StdioAcpSession", "run_probe"]

#: The deterministic first turn. No user content is ever sent to a real agent.
DEFAULT_PROMPT = "Say hello in one short sentence."

#: ACP JSON-RPC error code for unimplemented client methods. The probe provides
#: no filesystem/terminal host methods, so those calls fail closed.
_METHOD_NOT_FOUND = -32601

#: Bounded diagnostic transcript length (lines).
_TRANSCRIPT_LIMIT = 200


class AcpProbeError(RuntimeError):
    """The probe could not complete a step (timeout, closed pipe, bad shape)."""

    def __init__(self, message: str, *, reason: Optional[AgentProbeReason] = None):
        super().__init__(message)
        self.reason = reason


class AcpRemoteError(AcpProbeError):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, method: str, code: Any, message: str):
        super().__init__(f"ACP {method} failed (code {code}): {message}")
        self.method = method
        self.code = code
        self.remote_message = message


class StdioAcpSession:
    """Minimal ACP client over stdio. No host tools are executed."""

    def __init__(self, child: subprocess.Popen, *, timeout: float = 120.0):
        self.child = child
        self.timeout = timeout
        self.messages: "queue.Queue[Any]" = queue.Queue()
        self.chunks: List[str] = []
        self.methods: List[str] = []
        self.transcript: List[str] = []
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    # -- stdio -------------------------------------------------------------

    def _read(self) -> None:
        try:
            for line in self.child.stdout:
                self._remember(line)
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        except Exception as error:  # noqa: BLE001 - surfaced to the requester
            self.messages.put(error)
        finally:
            self.messages.put(EOFError("ACP process closed stdout"))

    def _remember(self, line: str) -> None:
        self.transcript.append(line.strip()[:500])
        del self.transcript[:-_TRANSCRIPT_LIMIT]

    def _send(self, payload: Dict[str, Any]) -> None:
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        self.child.stdin.flush()

    # -- protocol ----------------------------------------------------------

    def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send one request and wait for its result, answering host calls."""
        self.sequence += 1
        request_id = self.sequence
        self.methods.append(method)
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(message, Exception):
                raise AcpProbeError(str(message))
            if message.get("method") == "session/update":
                update = message.get("params", {}).get("update", {})
                if update.get("sessionUpdate") == "agent_message_chunk":
                    text = update.get("content", {}).get("text", "")
                    if text:
                        self.chunks.append(text)
            elif "method" in message and "id" in message:
                self._answer_host_call(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    error = message["error"] or {}
                    raise AcpRemoteError(method, error.get("code"), str(error.get("message") or ""))
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise AcpProbeError(f"ACP {method} returned a non-object result")
                return result
        raise AcpProbeError(
            f"ACP {method} did not finish within {int(self.timeout)}s",
            reason=AgentProbeReason.PROTOCOL,
        )

    def _answer_host_call(self, message: Dict[str, Any]) -> None:
        """Fail-closed host responses: no fs/terminal access, no permission."""
        method = message.get("method")
        if method == "session/request_permission":
            self._send({"id": message["id"], "result": {"outcome": {"outcome": "cancelled"}}})
            return
        self._send({
            "id": message["id"],
            "error": {"code": _METHOD_NOT_FOUND, "message": "Unsupported host method"},
        })

    def drain(self, timeout: float = 0.5) -> None:
        """Collect trailing notifications without waiting on a response."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return
            if isinstance(message, Exception):
                return
            if message.get("method") == "session/update":
                update = message.get("params", {}).get("update", {})
                if update.get("sessionUpdate") == "agent_message_chunk":
                    text = update.get("content", {}).get("text", "")
                    if text:
                        self.chunks.append(text)

    def close(self) -> None:
        try:
            if self.child.stdin and not self.child.stdin.closed:
                self.child.stdin.close()
        except OSError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        process.stop(self.child)
        # The reader thread has reached EOF (or is about to); releasing the
        # stdout pipe keeps a finished probe from leaking a file object until
        # garbage collection. A concurrent read error is already handled.
        if self.child.stdout is not None:
            try:
                self.child.stdout.close()
            except (OSError, ValueError):
                pass


def _read_tail(path: Path, limit: int = 2000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return text[-limit:]


def _diagnostic(session: Optional[StdioAcpSession], stderr_path: Path, error: Exception) -> str:
    parts = [str(error)]
    if session is not None and session.transcript:
        parts.append("transcript: " + " | ".join(session.transcript[-6:]))
    tail = _read_tail(stderr_path)
    if tail.strip():
        parts.append("stderr: " + tail)
    return redact_failure_text(" :: ".join(parts))


def _blocked_from_failure(
    framework: str,
    identity: AgentIdentity,
    session: Optional[StdioAcpSession],
    stderr_path: Path,
    error: Exception,
    *,
    checks: Sequence[Mapping[str, Any]],
) -> ProbeResult:
    diagnostic = _diagnostic(session, stderr_path, error)
    reason = classify_failure(diagnostic)
    return ProbeResult.blocked(
        framework,
        reason,
        diagnostic or str(error),
        identity=identity,
        checks=checks,
        protocol={"executable": identity.path, "methods": list(session.methods) if session else []},
    )


def run_probe(
    framework: str,
    *,
    run_dir: Path,
    executable: Optional[str] = None,
    command: Optional[str] = None,
    package: Optional[str] = None,
    argv: Optional[Sequence[str]] = None,
    home: Optional[str] = None,
    prompt: str = DEFAULT_PROMPT,
    timeout: int = 120,
    env_extra: Optional[Mapping[str, str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
    expected_substring: Optional[str] = None,
    prepare_home: Optional[Callable[[Path], None]] = None,
) -> ProbeResult:
    """Run the real ACP handshake against the host-installed agent.

    Returns a typed :class:`ProbeResult`; it never raises for a missing or
    unusable prerequisite, so a caller can render the blocked reason.

    ``prepare_home`` runs once the empty isolated home exists and before the
    environment is built, so a caller can seed it (for example with a
    deliberately poisoned agent config the launch must override).
    """
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    identity_result = probe_identity(
        framework,
        executable=executable,
        command=command,
        package=package,
        env=base_env,
    )
    if not identity_result.passed or identity_result.identity is None:
        return identity_result
    identity = identity_result.identity

    plan = plan_argv(framework, identity, argv)
    if plan.blocked:
        return ProbeResult.blocked(
            framework,
            plan.blocked_reason or AgentProbeReason.PROTOCOL,
            plan.blocked_detail,
            identity=identity,
            protocol={"argv": plan.argv},
        )

    home_path = isolated_home(run_path, framework, home)
    if prepare_home is not None:
        prepare_home(home_path)
    workspace = run_path / "agent-workspace" / framework
    workspace.mkdir(parents=True, exist_ok=True)
    env = build_env(home_path, base_env=base_env, extra=env_extra)
    stderr_path = run_path / f"agent-{framework}.stderr.log"

    checks: List[Dict[str, Any]] = []
    session: Optional[StdioAcpSession] = None
    with stderr_path.open("w", encoding="utf-8") as stderr_log:
        try:
            child = subprocess.Popen(
                plan.argv,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_log,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            return ProbeResult.blocked(
                framework,
                AgentProbeReason.MISSING_BINARY,
                f"could not execute {identity.path!r}: {error}",
                identity=identity,
                protocol={"argv": plan.argv},
                checks=checks,
            )
        try:
            session = StdioAcpSession(child, timeout=float(timeout))

            initialized = session.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "code4me-e2e-agent-probe", "version": "1"},
            })
            protocol_version = initialized.get("protocolVersion")
            capabilities = initialized.get("agentCapabilities")
            if capabilities is None:
                capabilities = initialized.get("capabilities")
            if type(protocol_version) is not int or protocol_version != 1:
                raise AcpProbeError(
                    f"initialize returned no usable protocolVersion: {protocol_version!r}",
                    reason=AgentProbeReason.PROTOCOL,
                )
            if not isinstance(capabilities, Mapping):
                raise AcpProbeError(
                    "initialize returned no agentCapabilities object",
                    reason=AgentProbeReason.PROTOCOL,
                )
            checks.append({
                "check": "initialize",
                "protocol_version": protocol_version,
                "capability_keys": sorted(str(key) for key in capabilities)[:20],
            })

            session_result = session.request("session/new", {
                "cwd": str(workspace),
                "mcpServers": [],
            })
            session_id = session_result.get("sessionId")
            if not isinstance(session_id, str) or not session_id.strip():
                raise AcpProbeError(
                    "session/new returned no non-empty sessionId",
                    reason=AgentProbeReason.PROTOCOL,
                )
            checks.append({"check": "session/new", "session_id_present": True})

            prompt_result = session.request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            }, timeout=float(timeout))
            session.drain(timeout=1.0)
            stop_reason = prompt_result.get("stopReason")
            if not isinstance(stop_reason, str) or not stop_reason:
                raise AcpProbeError(
                    "session/prompt returned no stopReason",
                    reason=AgentProbeReason.PROTOCOL,
                )
            if stop_reason != "end_turn":
                raise AcpProbeError(
                    f"session/prompt stopped with {stop_reason!r} instead of end_turn",
                    reason=AgentProbeReason.PROTOCOL,
                )
            answer = "".join(session.chunks)
            if not answer.strip():
                raise AcpProbeError(
                    "session/prompt completed without an assistant message",
                    reason=AgentProbeReason.PROTOCOL,
                )
            if expected_substring is not None and expected_substring not in answer:
                raise AcpProbeError(
                    "session/prompt did not return the expected provider answer (an error message is not a model response)",
                    reason=AgentProbeReason.PROTOCOL,
                )
            checks.append({
                "check": "session/prompt",
                "stop_reason": stop_reason,
                # Content is never stored: only the fact and size of the answer.
                "assistant_message_chars": len(answer),
            })
        except Exception as error:  # noqa: BLE001 - every failure is a typed block
            return _blocked_from_failure(framework, identity, session, stderr_path, error, checks=checks)
        finally:
            if session is not None:
                session.close()
            else:
                process.stop(child)

    return ProbeResult.ok(
        framework,
        identity,
        checks=checks,
        protocol={
            "argv": plan.argv,
            "home": str(home_path),
            "env_keys": sorted(
                key for key in env if key in (
                    "HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                    "XDG_CACHE_HOME", "XDG_STATE_HOME", "PATH", "NO_COLOR", "TERM",
                )
            ),
            "methods": list(session.methods) if session else [],
            "stderr_log": str(stderr_path),
        },
    )
