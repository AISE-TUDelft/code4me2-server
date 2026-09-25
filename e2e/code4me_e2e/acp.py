"""A minimal ACP host that launches the real IDE-registered proxy over stdio.

No credentials are synthesized here: the registered agent uses the plugin's
managed authentication bridge. Only the paid model provider is deterministic.

Every assertion is scoped to *this* invocation: the registered entry's exact
command/argv/digest/version, the monotonic stub receipt delta for the first
prompt, and telemetry rows emitted no earlier than this launch by this emitter
(or by the IDE spool for this run).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import process, real_agents, stack

#: ACP method label -> canonical event type the proxy normalizer persists
#: (``research/telemetry/normalization/generic_acp.py``). The ACP method label
#: itself is a normalizer rule id and is not stored in ``research_event``, so
#: the proof is the canonical projection, not the label.
ACP_METHOD_EVENT_TYPES: Dict[str, str] = {
    "initialize": "interaction.started",
    "session/new": "interaction.started",
    "session/prompt": "agent.message.started",
}

#: Minimum persisted canonical counts for the ACP proxy emitter of this launch:
#: ``initialize`` + ``session/new`` -> two interaction.started; the prompt
#: request plus at least one assistant chunk -> two agent.message.started; the
#: first prompt response -> one agent.message.completed.
REQUIRED_ACP_EVENT_COUNTS: Dict[str, int] = {
    "interaction.started": 2,
    "agent.message.started": 2,
    "agent.message.completed": 1,
}

#: Sources that must both be present, scoped to this invocation's window.
REQUIRED_TELEMETRY_SOURCES = ("ide", "acp")


class AcpClient:
    def __init__(self, child: subprocess.Popen):
        self.child = child
        self.messages = queue.Queue()
        self.chunks: list[str] = []
        self.methods: List[str] = []
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.child.stdout:
                self.messages.put(json.loads(line))
        except Exception as error:
            self.messages.put(error)
        finally:
            self.messages.put(EOFError("ACP process closed stdout"))

    def send(self, payload: dict):
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}) + "\n")
        self.child.stdin.flush()

    def request(self, method: str, params: dict, timeout: int = 120) -> dict:
        self.sequence += 1
        request_id = self.sequence
        self.methods.append(method)
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if isinstance(message, Exception):
                raise message
            if message.get("method") == "session/update":
                update = message.get("params", {}).get("update", {})
                if update.get("sessionUpdate") == "agent_message_chunk":
                    self.chunks.append(update.get("content", {}).get("text", ""))
            elif "method" in message and "id" in message:
                # This deterministic turn needs no host tools. Unexpected host
                # calls fail closed instead of executing commands on the machine.
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported host method"}})
            elif message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"ACP {method} failed: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"ACP {method} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# Registered entry identity
# ---------------------------------------------------------------------------


def registered_entry(run_path: Path) -> Tuple[Path, dict]:
    """The single research-proxy ACP entry this run registered."""
    registry_path = run_path / "ide-home/.jetbrains/acp.json"
    if not registry_path.is_file():
        raise RuntimeError(f"registered ACP registry {registry_path} does not exist")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"registered ACP registry {registry_path} is unreadable: {error}") from error
    entries = [entry for name, entry in (registry.get("agent_servers") or {}).items()
               if name.startswith("Code4Me Research Proxy")]
    if len(entries) != 1:
        raise RuntimeError(f"Expected one research proxy registration, found {len(entries)}")
    return registry_path, entries[0]


def _flag_value(args: Sequence[str], flag: str) -> Optional[str]:
    """The value following ``flag`` in a proxy argv, if any."""
    for index, token in enumerate(args[:-1]):
        if token == flag:
            return args[index + 1]
    return None


def _normalize_digest(value: Optional[str]) -> Optional[str]:
    """Bare lowercase hex; ``sha256:`` is a display convention, not identity."""
    if not value:
        return None
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return text or None


def entry_identity(run_path: Path) -> Dict[str, Any]:
    """Identify and validate the registered entry and the agent it wraps.

    Records command/argv/env keys plus the executable digest and ``--version``
    of both the proxy and the real agent. Environment *values* are never
    recorded: the entry env carries the one-time spool capability.
    """
    registry_path, entry = registered_entry(run_path)
    command = str(entry.get("command") or "").strip()
    if not command:
        raise RuntimeError("the registered ACP entry has an empty command")
    executable = Path(command)
    if not executable.is_file():
        raise RuntimeError(f"the registered ACP entry command does not exist: {executable}")
    if not os.access(executable, os.X_OK):
        raise RuntimeError(f"the registered ACP entry command is not executable: {executable}")

    args = [str(item) for item in (entry.get("args") or [])]
    env = entry.get("env") or {}
    agent_arg = _flag_value(args, "--agent-cmd")
    proxy_args = args[:args.index("--agent-cmd")] if "--agent-cmd" in args else args
    agent_digest = _normalize_digest(_flag_value(proxy_args, "--agent-digest"))
    if not agent_arg:
        raise RuntimeError("the registered ACP entry declares no --agent-cmd executable")
    if not agent_arg.strip():
        raise RuntimeError("the registered ACP entry has an empty --agent-cmd executable")
    if not agent_digest:
        raise RuntimeError("the registered ACP entry declares no --agent-digest before --agent-cmd")
    agent_executable = Path(agent_arg).expanduser()
    if not agent_executable.is_file():
        raise RuntimeError(f"the registered entry's agent binary does not exist: {agent_executable}")
    if not os.access(agent_executable, os.X_OK):
        raise RuntimeError(f"the registered entry's agent binary is not executable: {agent_executable}")

    proxy_identity = real_agents.executable_identity(executable, framework="code4me2-agent", source="registered_entry")
    agent_identity = real_agents.executable_identity(agent_executable, framework="code4me2-agent", source="registered_entry")
    if agent_digest and agent_digest != agent_identity.sha256:
        raise RuntimeError(
            "the registered entry's --agent-digest does not match the agent binary on disk "
            f"({agent_digest} != {agent_identity.sha256})"
        )
    return {
        "registry": str(registry_path),
        "command": str(executable),
        "args": args,
        # Keys only: the entry env may carry a one-time capability value.
        "env_keys": sorted(str(key) for key in env),
        # Not a secret: the research session this entry reports telemetry under.
        "research_session_id": (env or {}).get("CODE4ME_RESEARCH_SESSION_ID"),
        "adapter_id": (env or {}).get("CODE4ME_AGENT_ADAPTER_ID"),
        "proxy": proxy_identity.to_dict(),
        "agent_command": str(agent_executable),
        "agent_argv": args[args.index("--agent-cmd") + 1:],
        "agent_digest_flag_matches": bool(agent_digest) and agent_digest == agent_identity.sha256,
        "agent": agent_identity.to_dict(),
    }


# ---------------------------------------------------------------------------
# Scoped telemetry
# ---------------------------------------------------------------------------


def telemetry_scope_sql(
    enrollment_id: str,
    emitter_id: str,
    started_at: str,
    research_session_id: Optional[str] = None,
) -> str:
    """Rows for this enrollment emitted by this launch, plus this run's IDE rows.

    Proxy rows are pinned to this launch's unique emitter id and its time
    window. IDE rows cannot be pinned in time: the IDE emits its own events
    during sign-in and activation, before the harness launches the proxy. They
    are pinned to the per-run enrollment and, when ``research_session_id`` is
    supplied, to that exact research session (``ide:<context>`` emitters are per
    project, not per run); the proxy rows must belong to the same session.
    """
    uuid.UUID(enrollment_id)  # fail closed on a malformed id before SQL
    safe_emitter = emitter_id.replace("'", "")
    safe_started = started_at.replace("'", "")
    session_filter = ""
    if research_session_id:
        uuid.UUID(research_session_id)  # fail closed on a malformed id before SQL
        safe_session = research_session_id.replace("'", "")
        session_filter = f"AND research_session_id='{safe_session}' "
    return (
        "SELECT emitter_id, source, event_type, count(*) FROM research_event "
        f"WHERE enrollment_id='{enrollment_id}' "
        f"{session_filter}"
        f"AND ((emitter_id='{safe_emitter}' AND occurred_at >= '{safe_started}') "
        "OR emitter_id LIKE 'ide:%') "
        "GROUP BY 1,2,3;"
    )


def parse_telemetry_rows(rows: str) -> List[Tuple[str, str, str, int]]:
    parsed: List[Tuple[str, str, str, int]] = []
    for line in rows.splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        try:
            count = int(parts[3])
        except ValueError:
            continue
        parsed.append((parts[0], parts[1], parts[2], count))
    return parsed


def evaluate_telemetry(rows: Sequence[Tuple[str, str, str, int]], emitter_id: str) -> Dict[str, Any]:
    """Fail unless this emitter's canonical event projection is complete.

    ``ide`` telemetry from this run's window must also be present.
    """
    scoped_counts: Dict[str, int] = {}
    sources = set()
    for row_emitter, source, event_type, count in rows:
        if (row_emitter == emitter_id and source == "acp") or (row_emitter.startswith("ide:") and source == "ide"):
            sources.add(source)
        if row_emitter == emitter_id and source == "acp":
            scoped_counts[event_type] = scoped_counts.get(event_type, 0) + count
    missing_sources = sorted(set(REQUIRED_TELEMETRY_SOURCES) - sources)
    missing_events = {
        event_type: {"required": minimum, "observed": scoped_counts.get(event_type, 0)}
        for event_type, minimum in REQUIRED_ACP_EVENT_COUNTS.items()
        if scoped_counts.get(event_type, 0) < minimum
    }
    if missing_sources or missing_events:
        raise AssertionError(
            "scoped ACP/IDE telemetry incomplete: "
            f"missing_sources={missing_sources} missing_events={missing_events}"
        )
    return {
        "sources": sorted(sources),
        "acp_event_counts": dict(sorted(scoped_counts.items())),
        "required_event_counts": dict(REQUIRED_ACP_EVENT_COUNTS),
    }


def wait_for_scoped_telemetry(scenario, enrollment_id: str, emitter_id: str,
                              started_at: str, research_session_id: Optional[str] = None,
                              timeout: int = 45) -> Dict[str, Any]:
    """Poll the real DB until this invocation's scoped telemetry is complete."""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        rows = parse_telemetry_rows(
            stack.psql(scenario, telemetry_scope_sql(enrollment_id, emitter_id, started_at,
                                                     research_session_id))
        )
        try:
            return evaluate_telemetry(rows, emitter_id)
        except AssertionError as error:
            last_error = str(error)
        time.sleep(1)
    raise AssertionError(last_error or "scoped telemetry did not appear before the deadline")


def exercise(scenario, run_path: Path, state: dict, stub) -> dict:
    _, registered = registered_entry(run_path)
    entry = entry_identity(run_path)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CODE4ME_", "OPENAI_", "ANTHROPIC_"))}
    env.update({str(key): str(value) for key, value in (registered.get("env") or {}).items()})
    env["HOME"] = str(run_path / "ide-home")
    emitter_id = "acp-proxy:e2e-" + uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    stub_before = stub.request_count() if stub is not None else 0
    with (run_path / "acp-runtime.log").open("w") as log:
        child = subprocess.Popen([entry["command"], "--emitter-id", emitter_id, *entry["args"]],
                                 cwd=run_path / "ui-project", env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=log, text=True, bufsize=1, start_new_session=True)
        try:
            client = AcpClient(child)
            initialized = client.request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "code4me-e2e", "version": "1"},
            })
            protocol_version = initialized.get("protocolVersion")
            if type(protocol_version) is not int or protocol_version != 1:
                raise AssertionError(f"ACP initialize returned no usable protocolVersion: {protocol_version!r}")
            session = client.request("session/new", {"cwd": str(run_path / "ui-project"), "mcpServers": []})
            if not session.get("sessionId"):
                raise RuntimeError("ACP session/new returned no sessionId")
            outcome = client.request("session/prompt", {
                "sessionId": session["sessionId"],
                "prompt": [{"type": "text", "text": scenario.message.prompt}],
            }, scenario.timeouts.step_seconds)
            expected = scenario.message.expected_substring or stub.token
            if expected not in "".join(client.chunks):
                raise AssertionError("ACP answer did not contain the provider's deterministic token")
            if outcome.get("stopReason") != "end_turn":
                raise AssertionError(f"Unexpected ACP stop reason: {outcome.get('stopReason')}")
            # Per-turn proof: the first prompt must be exactly one new provider
            # request. A request left by an earlier UI/prefix run cannot satisfy it.
            stub_after = stub.request_count() if stub is not None else 0
            if stub is None or stub_after - stub_before != 1:
                raise AssertionError(
                    "the first ACP prompt did not produce exactly one provider request "
                    f"(before={stub_before}, after={stub_after})"
                )
        finally:
            # The proxy forwards the response before observing it. Give EOF
            # handling time to flush observations into the IDE's durable spool.
            # Killing the process group immediately can lose that final event.
            try:
                child.stdin.close()
                child.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                process.stop(child)
                child.stdout.close()
    # Verify plugin/proxy telemetry reached the real DB through the IDE spool.
    # Require this proxy launch and this run's IDE window, never events left by
    # a previous run/fixture.
    enrollment = str(state["enrollment_id"])
    # The registered entry names the IDE's research session; the HTTP workflow
    # state only knows one after its own acp_prepare step.
    research_session_id = entry.get("research_session_id") or state.get("research_session_id")
    telemetry = wait_for_scoped_telemetry(scenario, enrollment, emitter_id, started_at,
                                          research_session_id)
    telemetry["research_session_scoped"] = bool(research_session_id)
    return {"protocol_version": protocol_version,
            "acp_methods": list(client.methods),
            "required_method_event_types": dict(ACP_METHOD_EVENT_TYPES),
            "entry": entry,
            "stub_request_delta": stub_after - stub_before,
            "telemetry_window_started_at": started_at,
            "answer_contains_expected": True,
            "telemetry": telemetry}
