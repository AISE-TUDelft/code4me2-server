"""A minimal ACP host that launches the real IDE-registered proxy over stdio.

No credentials are synthesized here: the registered agent uses the plugin's
managed authentication bridge. Only the paid model provider is deterministic.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import process, stack


class AcpClient:
    def __init__(self, child: subprocess.Popen):
        self.child = child
        self.messages = queue.Queue()
        self.chunks: list[str] = []
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


def exercise(scenario, run_path: Path, state: dict, stub) -> dict:
    home = run_path / "ide-home"
    registry = json.loads((home / ".jetbrains/acp.json").read_text())
    entries = [entry for name, entry in registry.get("agent_servers", {}).items()
               if name.startswith("Code4Me Research Proxy")]
    if len(entries) != 1:
        raise RuntimeError(f"Expected one research proxy registration, found {len(entries)}")
    entry = entries[0]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CODE4ME_", "OPENAI_", "ANTHROPIC_"))}
    env.update(entry.get("env", {}))
    env["HOME"] = str(home)
    emitter_id = "acp-proxy:e2e-" + uuid.uuid4().hex
    with (run_path / "acp-runtime.log").open("w") as log:
        child = subprocess.Popen([entry["command"], "--emitter-id", emitter_id, *entry.get("args", [])],
                                 cwd=run_path / "ui-project", env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=log, text=True, bufsize=1, start_new_session=True)
        try:
            client = AcpClient(child)
            initialized = client.request("initialize", {
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "code4me-e2e", "version": "1"},
            })
            session = client.request("session/new", {"cwd": str(run_path / "ui-project"), "mcpServers": []})
            if not session.get("sessionId"):
                raise RuntimeError("ACP session/new returned no sessionId")
            outcome = client.request("session/prompt", {
                "sessionId": session["sessionId"],
                "prompt": [{"type": "text", "text": scenario.message.prompt}],
            }, scenario.timeouts.step_seconds)
            expected = scenario.message.expected_substring or stub.token
            if expected not in "".join(client.chunks) or not stub.requests:
                raise AssertionError("ACP answer did not contain the provider's deterministic token")
            if outcome.get("stopReason") != "end_turn":
                raise AssertionError(f"Unexpected ACP stop reason: {outcome.get('stopReason')}")
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
    # Verify plugin/proxy telemetry reached the real DB through the IDE spool.
    # Require this proxy launch, never events left by a previous run/fixture.
    enrollment = str(state["enrollment_id"])
    uuid.UUID(enrollment)
    deadline = time.monotonic() + 45
    sources = set()
    while time.monotonic() < deadline:
        rows = stack.psql(scenario,
            "SELECT DISTINCT source FROM research_event WHERE enrollment_id='" + enrollment +
            "' AND (emitter_id='" + emitter_id + "' OR emitter_id LIKE 'ide:%');")
        sources = set(rows.splitlines())
        if {"ide", "acp"} <= sources:
            break
        time.sleep(1)
    if not {"ide", "acp"} <= sources:
        raise AssertionError(f"IDE and ACP telemetry were not both persisted (sources={sorted(sources)})")
    return {"protocol_version": initialized.get("protocolVersion"),
            "answer_contains_expected": True, "telemetry_sources": sorted(sources)}
