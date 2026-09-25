"""Regression tests for real-agent (BYOA) setup and scoped ACP evidence.

These tests never launch a real Goose/Codex agent, Compose, an IDE, a browser or
a network provider. They use temporary directories, temporary fake executables
that speak a recorded ACP transcript over stdio, and the server's own normalizer
and profile/release validation modules to pin the harness contracts:

* discovery order (explicit override, env override, declared command, PATH,
  known per-user install location) and ``missing_binary`` reporting;
* executable identity (path, size, sha256, bounded ``--version`` capture) and
  ``ProbeResult`` typing;
* the Codex ACP guard in :func:`code4me_e2e.real_agents.plan_argv`;
* typed BLOCKED reasons from recorded transcripts (auth, quota, protocol);
* scoping of :func:`code4me_e2e.acp.evaluate_telemetry` to this emitter and the
  required canonical event counts (checked against the real
  ``research.telemetry.normalization.generic_acp`` normalizer);
* workflow scenarios refusing Goose/Codex arms (they are BYOA; the agents
  layer covers them);
* the stub provider's monotonic receipt delta;
* registered-entry identity failures (empty/missing command);
* the shared-lock bypass being limited to ``agent-probe``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from code4me_e2e import acp, cli, real_agents, stack
from code4me_e2e.acp_probe import run_probe
from code4me_e2e.config import AgentProbeReason, ScenarioError, load_scenario
from code4me_e2e.stub_provider import StubProvider

# The server's own contracts are verified where the server package is
# importable (the harness's documented venv, which the run's verification
# command uses). A bare interpreter that runs only the harness unit tests
# cannot import ``research.*``; those checks are then reported as skipped with
# this reason, never as a pass.
_SERVER_CONTRACT_SKIP = "the editable server package (research.*) is not installed"
try:
    from research.telemetry.normalization.generic_acp import GenericAcpNormalizer

    _SERVER_CONTRACT_AVAILABLE = True
except ImportError:
    _SERVER_CONTRACT_AVAILABLE = False

_FAKE_ANSWER = "hello from fake agent"

#: A recorded ACP transcript. The fake only ever exists in a temp directory and
#: is never a real agent: each mode drives one probe outcome.
_FAKE_AGENT_TEMPLATE = '''#!__PYTHON__
"""Recorded ACP transcript (test fixture; not a real agent)."""
import json
import sys

MODE = "__MODE__"
FRAMEWORK = "__FRAMEWORK__"
ANSWER = "__ANSWER__"

if "--version" in sys.argv:
    print("fake-%s 9.9.9" % FRAMEWORK)
    raise SystemExit(0)


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    if method == "initialize":
        if MODE == "auth":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "error": {"code": 401, "message": "401 Unauthorized: not logged in"}})
        elif MODE == "quota":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "error": {"code": -32000,
                            "message": "429 Too Many Requests: usage limit reached"}})
        elif MODE == "protocol":
            send({"jsonrpc": "2.0", "id": message["id"],
                  "result": {"protocolVersion": 0}})
        else:
            send({"jsonrpc": "2.0", "id": message["id"],
                  "result": {"protocolVersion": 1,
                             "agentCapabilities": {"promptCapabilities": {}}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"sessionId": "fake-session-1"}})
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"update": {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": ANSWER}}}})
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"stopReason": "end_turn"}})
'''


def _make_executable(directory, name: str, body: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _version_script(framework: str) -> str:
    return "#!/bin/sh\necho fake-%s 9.9.9\n" % framework


def _fake_agent_script(mode: str, framework: str) -> str:
    return (
        _FAKE_AGENT_TEMPLATE.replace("__PYTHON__", sys.executable)
        .replace("__MODE__", mode)
        .replace("__FRAMEWORK__", framework)
        .replace("__ANSWER__", _FAKE_ANSWER)
    )


def _write_registry(run_dir, entries) -> Path:
    registry = Path(run_dir) / "ide-home" / ".jetbrains" / "acp.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"agent_servers": entries}), encoding="utf-8")
    return registry


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DetectionTest(TempDirTest):
    def test_detection_order_override_env_command_path(self):
        explicit = _make_executable(self.root / "a", "goose", _version_script("goose"))
        env_exe = _make_executable(self.root / "b", "goose", _version_script("goose"))
        command_exe = _make_executable(self.root / "c", "goose-cmd", _version_script("goose"))
        path_exe = _make_executable(self.root / "d", "goose", _version_script("goose"))
        override_env = {real_agents.ENV_OVERRIDES["goose"]: str(env_exe)}

        found = real_agents.detect(
            "goose", executable=str(explicit), command=str(command_exe),
            env=override_env, path_env=str(self.root / "d"),
        )
        self.assertEqual(("override", str(explicit)), (found.source, found.path))

        found = real_agents.detect(
            "goose", command=str(command_exe), env=override_env,
            path_env=str(self.root / "d"),
        )
        self.assertEqual(("env", str(env_exe)), (found.source, found.path))

        found = real_agents.detect(
            "goose", command=str(command_exe), env={}, path_env=str(self.root / "d")
        )
        self.assertEqual(("command", str(command_exe)), (found.source, found.path))

        found = real_agents.detect("goose", env={}, path_env=str(self.root / "d"))
        self.assertEqual(("path", str(path_exe)), (found.source, found.path))

    def test_missing_binary_is_none_and_never_searches_the_developer_home(self):
        empty = self.root / "empty"
        empty.mkdir()
        with patch.object(real_agents, "known_locations", return_value=[]):
            self.assertIsNone(real_agents.detect("goose", env={}, path_env=str(empty)))

    def test_known_location_fallback_matches_a_plugin_location(self):
        location = self.root / "fake-home" / ".local" / "bin"
        exe = _make_executable(location, "goose", _version_script("goose"))
        empty = self.root / "empty"
        empty.mkdir()
        with patch.object(real_agents, "known_locations", return_value=[location]):
            found = real_agents.detect("goose", env={}, path_env=str(empty))
        self.assertEqual(("known_location", str(exe)), (found.source, found.path))

    def test_non_executable_override_does_not_silently_launch_another_agent(self):
        not_executable = self.root / "bad" / "goose"
        not_executable.parent.mkdir(parents=True)
        not_executable.write_text("not executable", encoding="utf-8")
        path_dir = self.root / "d"
        _make_executable(path_dir, "goose", _version_script("goose"))  # must not be chosen
        found = real_agents.detect(
            "goose", executable=str(not_executable), env={}, path_env=str(path_dir)
        )
        self.assertIsNone(found)

    def test_declared_package_name_falls_back_to_its_own_executable(self):
        package_dir = self.root / "pkg"
        exe = _make_executable(package_dir, "custom-agent", _version_script("custom-agent"))
        self.assertEqual(("custom-agent",), real_agents.candidate_names("goose", "custom-agent"))
        found = real_agents.detect(
            "goose", package="custom-agent", env={}, path_env=str(package_dir)
        )
        self.assertEqual(("path", str(exe)), (found.source, found.path))


# ---------------------------------------------------------------------------
# Identity and ProbeResult typing
# ---------------------------------------------------------------------------


class IdentityTest(TempDirTest):
    def test_identity_records_path_size_sha256_and_version(self):
        exe = _make_executable(self.root, "goose", _version_script("goose"))
        identity = real_agents.executable_identity(exe, framework="goose", source="override")
        self.assertEqual(str(exe.resolve()), identity.path)
        self.assertEqual(exe.stat().st_size, identity.size)
        self.assertEqual(hashlib.sha256(exe.read_bytes()).hexdigest(), identity.sha256)
        self.assertEqual("fake-goose 9.9.9", identity.version)
        self.assertEqual("override", identity.source)
        self.assertEqual(
            {
                "framework": "goose",
                "path": identity.path,
                "size": identity.size,
                "sha256": identity.sha256,
                "version": identity.version,
                "source": "override",
            },
            identity.to_dict(),
        )

    def test_a_failing_version_flag_records_no_version(self):
        exe = _make_executable(self.root, "proxy", "#!/bin/sh\necho 'usage: proxy --agent-cmd ...' >&2\nexit 2\n")
        self.assertIsNone(real_agents.capture_version(exe))

    def test_version_capture_is_bounded_when_the_fake_ignores_the_flag(self):
        exe = _make_executable(self.root, "hanger", "#!/bin/sh\nsleep 30\n")
        started = time.monotonic()
        self.assertIsNone(real_agents.capture_version(exe, timeout=0.5))
        self.assertLess(time.monotonic() - started, 10)

    def test_reason_vocabulary_and_typed_probe_result(self):
        self.assertEqual(
            {"missing_binary", "missing_auth", "quota", "protocol"},
            {reason.value for reason in AgentProbeReason},
        )
        blocked = real_agents.ProbeResult.blocked(
            "goose", AgentProbeReason.MISSING_AUTH, "no credential"
        )
        self.assertEqual("BLOCKED", blocked.status)
        self.assertFalse(blocked.passed)
        self.assertEqual("missing_auth", blocked.reason)
        self.assertIsInstance(blocked.reason, str)
        self.assertNotIn("identity", blocked.to_dict())

        identity = real_agents.AgentIdentity("goose", "/tmp/goose", 3, "a" * 64, "v", "override")
        passed = real_agents.ProbeResult.ok("goose", identity)
        self.assertTrue(passed.passed)
        self.assertEqual(identity.to_dict(), passed.to_dict()["identity"])

    def test_probe_identity_reports_missing_binary_with_the_override_variable(self):
        empty = self.root / "empty"
        empty.mkdir()
        with patch.object(real_agents, "known_locations", return_value=[]):
            result = real_agents.probe_identity("goose", env={}, path_env=str(empty))
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("missing_binary", result.reason)
        self.assertIn(real_agents.ENV_OVERRIDES["goose"], result.detail)
        self.assertEqual("goose", result.protocol["searched"])
        self.assertIsNone(result.identity)

    def test_build_env_pins_an_isolated_home_and_strips_ambient_credentials(self):
        home = self.root / "home"
        home.mkdir()
        env = real_agents.build_env(
            home,
            base_env={
                "PATH": "/bin",
                "FOO": "bar",
                "OPENAI_API_KEY": "sk-secret",
                "CODE4ME_AGENT": "x",
                "GOOSE_MODEL": "y",
            },
        )
        self.assertEqual(str(home), env["HOME"])
        self.assertEqual(str(home / ".codex"), env["CODEX_HOME"])
        self.assertEqual("/bin", env["PATH"])
        self.assertEqual("bar", env["FOO"])
        for stripped in ("OPENAI_API_KEY", "CODE4ME_AGENT", "GOOSE_MODEL"):
            self.assertNotIn(stripped, env)

    def test_isolated_home_is_created_under_the_run_directory(self):
        home = real_agents.isolated_home(self.root / "run", "goose")
        self.assertEqual(self.root / "run" / "agent-home" / "goose", home)
        self.assertTrue(home.is_dir())
        override = real_agents.isolated_home(self.root / "run", "goose", str(self.root / "elsewhere"))
        self.assertEqual(self.root / "elsewhere", override)

    def test_failure_text_redacts_bearer_and_keyed_tokens(self):
        secret = "sk-" + "a" * 24
        text = real_agents.redact_failure_text(
            "initialize failed: Authorization: Bearer %s 401 unauthorized" % secret
        )
        self.assertNotIn(secret, text)
        self.assertIn("<redacted>", text)
        self.assertIn("401 unauthorized", text)


# ---------------------------------------------------------------------------
# Launch plan
# ---------------------------------------------------------------------------


class PlanArgvTest(TempDirTest):
    def test_goose_plan_appends_acp_and_honors_a_configured_argv(self):
        identity = real_agents.AgentIdentity("goose", str(self.root / "goose"), 1, "a" * 64, "v", "override")
        plan = real_agents.plan_argv("goose", identity)
        self.assertFalse(plan.blocked)
        self.assertEqual([identity.path, "acp"], plan.argv)
        configured = real_agents.plan_argv("goose", identity, ["--debug", "acp"])
        self.assertFalse(configured.blocked)
        self.assertEqual([identity.path, "--debug", "acp"], configured.argv)

    def test_codex_guard_blocks_the_bare_cli_but_allows_codex_acp(self):
        bare = real_agents.AgentIdentity("codex", str(self.root / "codex"), 1, "a" * 64, "v", "override")
        blocked = real_agents.plan_argv("codex", bare)
        self.assertTrue(blocked.blocked)
        self.assertEqual(AgentProbeReason.PROTOCOL, blocked.blocked_reason)
        self.assertIn("codex-acp", blocked.blocked_detail)

        adapter = real_agents.AgentIdentity("codex", str(self.root / "codex-acp"), 1, "a" * 64, "v", "override")
        self.assertEqual([adapter.path], real_agents.plan_argv("codex", adapter).argv)

        configured = real_agents.plan_argv("codex", bare, ["--experimental-acp"])
        self.assertFalse(configured.blocked)
        self.assertEqual([bare.path, "--experimental-acp"], configured.argv)

    def test_empty_executable_entry_is_a_missing_binary_block(self):
        empty = real_agents.AgentIdentity("goose", "", 0, "", None, "override")
        plan = real_agents.plan_argv("goose", empty)
        self.assertTrue(plan.blocked)
        self.assertEqual(AgentProbeReason.MISSING_BINARY, plan.blocked_reason)
        self.assertEqual([], plan.argv)


# ---------------------------------------------------------------------------
# Real ACP probe against recorded transcripts
# ---------------------------------------------------------------------------


class RecordedProbeTest(TempDirTest):
    def _fake(self, name: str, mode: str, framework: str = "goose") -> Path:
        return _make_executable(self.root / "bin", name, _fake_agent_script(mode, framework))

    def test_recorded_transcripts_classify_blocked_reasons(self):
        for mode, expected in (
            ("auth", "missing_auth"),
            ("quota", "quota"),
            ("protocol", "protocol"),
        ):
            with self.subTest(mode=mode):
                exe = self._fake("goose", mode)
                result = run_probe(
                    "goose", run_dir=self.root / ("run-" + mode),
                    executable=str(exe), timeout=10,
                )
                self.assertEqual("BLOCKED", result.status)
                self.assertFalse(result.passed)
                self.assertEqual(expected, result.reason)
                self.assertIsNotNone(result.identity)
                self.assertEqual("fake-goose 9.9.9", result.identity.version)
                self.assertEqual(["initialize"], result.protocol["methods"])
                self.assertIsInstance(result.detail, str)

    def test_successful_fake_session_is_a_pass_with_an_isolated_home(self):
        exe = self._fake("goose", "ok")
        run_dir = self.root / "run-ok"
        result = run_probe("goose", run_dir=run_dir, executable=str(exe), timeout=10)
        self.assertEqual("PASS", result.status)
        self.assertEqual(
            ["initialize", "session/new", "session/prompt"],
            [check["check"] for check in result.checks],
        )
        self.assertEqual(1, result.checks[0]["protocol_version"])
        self.assertTrue(result.checks[1]["session_id_present"])
        self.assertEqual("end_turn", result.checks[2]["stop_reason"])
        self.assertEqual(len(_FAKE_ANSWER), result.checks[2]["assistant_message_chars"])
        self.assertEqual(["initialize", "session/new", "session/prompt"], result.protocol["methods"])
        home = run_dir / "agent-home" / "goose"
        self.assertEqual(str(home), result.protocol["home"])
        self.assertTrue(home.is_dir())
        self.assertNotEqual(str(Path.home()), result.protocol["home"])
        self.assertTrue((run_dir / "agent-goose.stderr.log").is_file())

    def test_codex_guard_blocks_the_bare_cli_before_any_launch(self):
        exe = self._fake("codex", "ok", framework="codex")
        run_dir = self.root / "run-codex"
        result = run_probe("codex", run_dir=run_dir, executable=str(exe), timeout=10)
        self.assertEqual("BLOCKED", result.status)
        self.assertEqual("protocol", result.reason)
        self.assertIn("codex-acp", result.detail)
        self.assertFalse((run_dir / "agent-home").exists())

    def test_codex_acp_adapter_is_launched_without_a_subcommand(self):
        exe = self._fake("codex-acp", "ok", framework="codex")
        result = run_probe(
            "codex", run_dir=self.root / "run-codex-acp", executable=str(exe), timeout=10
        )
        self.assertEqual("PASS", result.status)
        self.assertEqual([str(exe.resolve())], result.protocol["argv"])


# ---------------------------------------------------------------------------
# Scoped telemetry
# ---------------------------------------------------------------------------


class TelemetryScopeTest(unittest.TestCase):
    EMITTER = "acp-proxy:e2e-test"

    @staticmethod
    def _rows(emitter, counts):
        return [(emitter, "acp", event_type, count) for event_type, count in counts.items()]

    @unittest.skipUnless(_SERVER_CONTRACT_AVAILABLE, _SERVER_CONTRACT_SKIP)
    def test_required_counts_match_the_server_normalizer(self):
        """The required constants are derived from the real normalizer."""
        normalizer = GenericAcpNormalizer()
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp"}},
            {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"sessionId": "s1"}},
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hi"},
                    }
                },
            },
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ]
        counts = {}
        for frame in frames:
            for candidate in normalizer.normalize(frame).candidates:
                counts[candidate.event_type.value] = counts.get(candidate.event_type.value, 0) + 1
        self.assertEqual(dict(acp.REQUIRED_ACP_EVENT_COUNTS), counts)
        self.assertEqual(
            {
                "initialize": "interaction.started",
                "session/new": "interaction.started",
                "session/prompt": "agent.message.started",
            },
            dict(acp.ACP_METHOD_EVENT_TYPES),
        )

    def test_scope_sql_bounds_rows_and_sanitizes_literals(self):
        sql = acp.telemetry_scope_sql(
            "11111111-1111-1111-1111-111111111111",
            "acp-proxy:a'b",
            "2026-01-01T00:00:00+00:00",
        )
        self.assertIn("enrollment_id='11111111-1111-1111-1111-111111111111'", sql)
        self.assertIn("occurred_at >=", sql)
        self.assertIn("emitter_id LIKE 'ide:%'", sql)
        self.assertIn("acp-proxy:ab", sql)
        self.assertNotIn("a'b", sql)
        with self.assertRaises(ValueError):
            acp.telemetry_scope_sql("not-a-uuid", self.EMITTER, "2026-01-01T00:00:00+00:00")

    def test_ide_rows_are_session_scoped_but_not_time_windowed(self):
        """The IDE emits its own events before the harness launches the proxy."""
        session = "22222222-2222-2222-2222-222222222222"
        sql = acp.telemetry_scope_sql(
            "11111111-1111-1111-1111-111111111111",
            "acp-proxy:e2e",
            "2026-01-01T00:00:00+00:00",
            research_session_id=session,
        )
        self.assertIn("(emitter_id='acp-proxy:e2e' AND occurred_at >= '2026-01-01T00:00:00+00:00')", sql)
        self.assertIn("OR emitter_id LIKE 'ide:%')", sql)
        # The session filter applies to both branches, the time window only to the proxy.
        self.assertLess(sql.index(f"research_session_id='{session}'"), sql.index("AND (("))

    def test_evaluate_telemetry_requires_this_emitters_counts_and_ide_source(self):
        complete = self._rows(self.EMITTER, acp.REQUIRED_ACP_EVENT_COUNTS) + [
            ("ide:run-1", "ide", "interaction.started", 4),
        ]
        result = acp.evaluate_telemetry(complete, self.EMITTER)
        self.assertEqual(["acp", "ide"], result["sources"])
        self.assertEqual(dict(acp.REQUIRED_ACP_EVENT_COUNTS), result["required_event_counts"])

        # Counts split across rows accumulate.
        split = [
            (self.EMITTER, "acp", "interaction.started", 1),
            (self.EMITTER, "acp", "interaction.started", 1),
            (self.EMITTER, "acp", "agent.message.started", 2),
            (self.EMITTER, "acp", "agent.message.completed", 1),
            ("ide:run-1", "ide", "interaction.started", 1),
        ]
        acp.evaluate_telemetry(split, self.EMITTER)

        # Another proxy's rows must not satisfy this emitter's requirement.
        other = self._rows("acp-proxy:other", acp.REQUIRED_ACP_EVENT_COUNTS) + [
            ("ide:run-1", "ide", "interaction.started", 4),
        ]
        with self.assertRaises(AssertionError) as caught:
            acp.evaluate_telemetry(other, self.EMITTER)
        self.assertIn("missing_events", str(caught.exception))

        # Without this run's IDE source the projection is incomplete.
        with self.assertRaises(AssertionError) as caught:
            acp.evaluate_telemetry(self._rows(self.EMITTER, acp.REQUIRED_ACP_EVENT_COUNTS), self.EMITTER)
        self.assertIn("missing_sources", str(caught.exception))
        self.assertIn("ide", str(caught.exception))

    def test_parse_telemetry_rows_skips_malformed_lines(self):
        rows = acp.parse_telemetry_rows(
            "ide:1|ide|interaction.started|3\nmalformed\nacp|acp|agent.message.started|nope\n"
        )
        self.assertEqual([("ide:1", "ide", "interaction.started", 3)], rows)

    def test_scope_sql_requires_the_run_session_for_ide_rows(self):
        session = "22222222-2222-2222-2222-222222222222"
        sql = acp.telemetry_scope_sql(
            "11111111-1111-1111-1111-111111111111",
            "acp-proxy:e2e",
            "2026-01-01T00:00:00+00:00",
            research_session_id=session,
        )
        self.assertIn(f"research_session_id='{session}'", sql)
        self.assertIn("emitter_id LIKE 'ide:%'", sql)
        # Without a session id there is no session filter at all (enrollment scope only).
        unscoped = acp.telemetry_scope_sql(
            "11111111-1111-1111-1111-111111111111",
            "acp-proxy:e2e",
            "2026-01-01T00:00:00+00:00",
        )
        self.assertNotIn("research_session_id", unscoped)
        with self.assertRaises(ValueError):
            acp.telemetry_scope_sql(
                "11111111-1111-1111-1111-111111111111",
                "acp-proxy:e2e",
                "2026-01-01T00:00:00+00:00",
                research_session_id="not-a-uuid",
            )


# ---------------------------------------------------------------------------
# Workflow scenarios and BYOA frameworks
# ---------------------------------------------------------------------------


class WorkflowFrameworkTest(unittest.TestCase):
    def test_goose_and_codex_arms_are_refused_by_the_workflow_scenario(self):
        for framework in ("goose", "codex"):
            with self.subTest(framework=framework), self.assertRaises(ScenarioError) as caught:
                load_scenario(overrides=[f"agent.framework_version={framework}"])
            self.assertEqual("FRAMEWORK_NOT_IN_WORKFLOW", caught.exception.code)
            self.assertIn("--layer agents", str(caught.exception))

    def test_probe_inputs_are_validated(self):
        with self.assertRaises(ScenarioError) as caught:
            load_scenario(overrides=['agent.agent_command_args=["acp", ""]'])
        self.assertEqual("AGENT_COMMAND_ARGS_INVALID", caught.exception.code)
        with self.assertRaises(ScenarioError):
            load_scenario(overrides=['agent.executable=" "'])
        scenario = load_scenario(overrides=['agent.agent_command_args=["--debug", "acp"]'])
        self.assertEqual(["--debug", "acp"], scenario.agent.agent_command_args)


# ---------------------------------------------------------------------------
# Stub provider receipts
# ---------------------------------------------------------------------------


class StubReceiptTest(unittest.TestCase):
    def test_request_receipts_are_monotonic_and_the_delta_is_per_turn(self):
        stub = StubProvider()
        self.assertEqual(0, stub.request_count())
        stub.record({"model": "m", "message_count": 1, "stream": False})
        first = stub.request_count()
        stub.record({"model": "m", "message_count": 1, "stream": False})
        second = stub.request_count()
        self.assertEqual((1, 2), (first, second))
        self.assertEqual([1, 2], [entry["receipt"] for entry in stub.requests])
        self.assertEqual(2, len(stub.requests_since(0)))
        self.assertEqual(1, len(stub.requests_since(first)))
        self.assertEqual([], stub.requests_since(second))

        # The ACP exercise asserts exactly this delta for one first prompt: a
        # receipt left by any earlier UI/prefix run cannot satisfy it.
        before = stub.request_count()
        stub.record({"model": "m", "message_count": 2, "stream": False})
        self.assertEqual(1, stub.request_count() - before)

    def test_completion_body_carries_the_deterministic_token_only(self):
        stub = StubProvider(token="E2E_TEST_TOKEN")
        body = stub.completion_body("e2e-stub-model")
        self.assertIn("E2E_TEST_TOKEN", body["choices"][0]["message"]["content"])
        self.assertNotEqual(StubProvider().token, StubProvider().token)


# ---------------------------------------------------------------------------
# Registered ACP entry identity
# ---------------------------------------------------------------------------


class EntryIdentityTest(TempDirTest):
    def _proxy(self) -> Path:
        return _make_executable(self.root / "bin", "proxy", "#!/bin/sh\necho fake-proxy 1.0\n")

    def _agent(self) -> Path:
        return _make_executable(self.root / "bin", "agent", "#!/bin/sh\necho fake-agent 1.0\n")

    def test_missing_registry_or_research_entry_fails_closed(self):
        with self.assertRaises(RuntimeError):
            acp.entry_identity(self.root)
        _write_registry(self.root, {"Other Agent": {"command": "/bin/sh"}})
        with self.assertRaises(RuntimeError) as caught:
            acp.entry_identity(self.root)
        self.assertIn("Expected one research proxy registration", str(caught.exception))

    def test_empty_or_missing_command_is_refused(self):
        for entry in ({"args": []}, {"command": "", "args": []}, {"command": "   ", "args": []}):
            with self.subTest(entry=entry):
                _write_registry(self.root, {"Code4Me Research Proxy": entry})
                with self.assertRaises(RuntimeError) as caught:
                    acp.entry_identity(self.root)
                self.assertIn("empty command", str(caught.exception))

    def test_nonexistent_command_is_refused(self):
        _write_registry(
            self.root,
            {"Code4Me Research Proxy": {"command": str(self.root / "missing"), "args": []}},
        )
        with self.assertRaises(RuntimeError) as caught:
            acp.entry_identity(self.root)
        self.assertIn("does not exist", str(caught.exception))

    def test_missing_agent_cmd_is_refused(self):
        proxy = self._proxy()
        _write_registry(
            self.root,
            {"Code4Me Research Proxy": {"command": str(proxy), "args": ["--emitter-id", "e"]}},
        )
        with self.assertRaises(RuntimeError) as caught:
            acp.entry_identity(self.root)
        self.assertIn("--agent-cmd", str(caught.exception))

    def test_agent_digest_mismatch_is_refused(self):
        proxy, agent = self._proxy(), self._agent()
        _write_registry(
            self.root,
            {
                "Code4Me Research Proxy": {
                    "command": str(proxy),
                    "args": ["--agent-digest", "deadbeef", "--agent-cmd", str(agent)],
                }
            },
        )
        with self.assertRaises(RuntimeError) as caught:
            acp.entry_identity(self.root)
        self.assertIn("does not match", str(caught.exception))

    def test_identity_binds_the_proxy_and_the_agent_digest(self):
        proxy, agent = self._proxy(), self._agent()
        digest = hashlib.sha256(agent.read_bytes()).hexdigest()
        _write_registry(
            self.root,
            {
                "Code4Me Research Proxy": {
                    "command": str(proxy),
                    "args": ["--agent-digest", digest, "--agent-cmd", str(agent)],
                    "env": {"CODE4ME_AGENT_ADAPTER_ID": "adapter-1"},
                }
            },
        )
        entry = acp.entry_identity(self.root)
        self.assertEqual(digest, entry["agent"]["sha256"])
        self.assertTrue(entry["agent_digest_flag_matches"])
        self.assertEqual(["CODE4ME_AGENT_ADAPTER_ID"], entry["env_keys"])
        self.assertEqual("adapter-1", entry["adapter_id"])
        self.assertEqual(str(agent), entry["agent_argv"][0])
        self.assertEqual(str(agent.resolve()), entry["agent"]["path"])

    def test_prefixed_agent_digest_matches_the_bare_hex(self):
        proxy, agent = self._proxy(), self._agent()
        digest = hashlib.sha256(agent.read_bytes()).hexdigest()
        _write_registry(
            self.root,
            {
                "Code4Me Research Proxy": {
                    "command": str(proxy),
                    "args": ["--agent-digest", "sha256:" + digest, "--agent-cmd", str(agent)],
                }
            },
        )
        entry = acp.entry_identity(self.root)
        self.assertTrue(entry["agent_digest_flag_matches"])
        self.assertEqual(digest, entry["agent"]["sha256"])

    def test_whitespace_agent_cmd_is_refused_as_empty(self):
        proxy = self._proxy()
        _write_registry(
            self.root,
            {"Code4Me Research Proxy": {"command": str(proxy), "args": ["--agent-cmd", "   "]}},
        )
        with self.assertRaises(RuntimeError) as caught:
            acp.entry_identity(self.root)
        self.assertIn("empty --agent-cmd executable", str(caught.exception))


# ---------------------------------------------------------------------------
# CLI lock bypass
# ---------------------------------------------------------------------------


class CliLockTest(TempDirTest):
    def test_agent_probe_is_the_only_shared_lock_bypass(self):
        cache = self.root / ".cache"
        cache.mkdir()
        lock_path = cache / "run.lock"
        with patch.object(stack, "E2E_DIR", self.root), lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                probe_rc = cli.main(
                    [
                        "agent-probe",
                        "--framework",
                        "goose",
                        "--executable",
                        sys.executable,
                        "--detect-only",
                        "--json",
                    ]
                )
                blocked_rc = cli.main(["stack", "status"])
        self.assertEqual(0, probe_rc, stdout.getvalue())
        self.assertEqual(2, blocked_rc)
        self.assertIn("Another e2e command is running", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
