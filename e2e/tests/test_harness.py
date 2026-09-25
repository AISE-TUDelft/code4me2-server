"""Regression tests for false passes, isolation and resumable orchestration."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from code4me_e2e import cli, process, report, steps, suite, ui, workflow
from code4me_e2e.config import ScenarioError, load_scenario
from code4me_e2e.steps import Ctx, StepResult
from code4me_e2e.stub_provider import StubProvider


class HarnessTest(unittest.TestCase):
    def test_gradle_success_requires_executed_unskipped_tests(self):
        for evidence in ({}, {"tests": 0}, {"tests": 1, "skipped": 1},
                         {"tests": 1, "failures": 1}, {"tests": 1, "errors": 1}):
            with self.subTest(evidence=evidence), self.assertRaises(RuntimeError):
                ui.require_test_evidence(0, evidence)
        with self.assertRaises(RuntimeError):
            ui.require_test_evidence(1, {"tests": 1})

    def test_ui_gate_requires_every_step_once(self):
        steps = [{"id": name, "status": "PASS"} for name in ui.EXPECTED_UI_STEPS]
        ui.require_test_evidence(0, {"tests": 1}, {"steps": steps})
        for invalid in (steps[:-1], steps + steps[:1],
                        [{**step, "status": "BLOCKED"} for step in steps]):
            with self.subTest(steps=invalid), self.assertRaises(RuntimeError):
                ui.require_test_evidence(0, {"tests": 1}, {"steps": invalid})

    def test_resume_authenticates_once_instead_of_reusing_invalidated_login(self):
        scenario = load_scenario()
        state = {"accounts": {"participant": {}}, "cookies": {"participant": {"auth_token": "expired"}}}
        with patch("code4me_e2e.steps.HttpClient") as http:
            http.return_value.post.return_value.status = 200
            ctx = Ctx(scenario, state, Path("unused"), fresh_login=True)
            self.assertIs(ctx.client("participant"), ctx.client("participant"))
            http.return_value.post.assert_called_once_with("/api/user/authenticate", {
                "email": scenario.participant.email, "password": scenario.participant.password,
            })

    def test_report_replaces_only_its_own_step_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path, scenario = Path(directory), load_scenario()
            workflow._record_plugin_step(path, scenario, StepResult("plugin_test", "PASS", 1))
            workflow._record_plugin_step(path, scenario, StepResult("cleanup", "FAIL", 1, {"session_capability": "secret"}))
            result = json.loads((path / "report.json").read_text())
            self.assertEqual([s["id"] for s in result["steps"]], ["plugin_test", "cleanup"])
            self.assertNotIn('"secret"', (path / "report.json").read_text())

    def test_exception_after_passing_layer_cannot_return_success(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(workflow, "run_plugin_test", return_value=0), \
             patch.object(workflow, "run_workflow", side_effect=RuntimeError("backend startup failed")), \
             patch.object(report, "capture_backend_logs"), \
             patch.object(suite.stack, "down") as down, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(load_scenario(), layer="plugin", run_dir=directory)
            self.assertEqual(1, rc)
            down.assert_not_called()
            self.assertEqual("suite", json.loads((Path(directory) / "report.json").read_text())["failed_step"])

    def test_ui_failure_stops_dependent_layers(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ui, "run_ui_test", return_value=1), \
             patch.object(workflow, "run_plugin_test") as plugin, \
             patch.object(workflow, "run_workflow") as backend, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, suite.run(load_scenario(), run_dir=directory))
            plugin.assert_not_called()
            backend.assert_not_called()

    def test_cleanup_failure_is_a_failed_gate(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(workflow, "run_workflow", return_value=0), \
             patch.object(suite.stack, "down", side_effect=RuntimeError("docker unavailable")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, suite.run(load_scenario(), layer="backend", run_dir=directory))
            self.assertEqual("cleanup", json.loads((Path(directory) / "report.json").read_text())["failed_step"])

    def test_disposable_project_name_cannot_select_developer_stack(self):
        with self.assertRaises(ScenarioError):
            load_scenario(overrides=["stack.project_name=code4me2-server"])
        args = cli.build_parser().parse_args(["stack", "down", "--project-name", "code4me2-server"])
        with self.assertRaises(ScenarioError):
            cli._load(args)
        self.assertEqual("code4me-e2e-ci", load_scenario(overrides=["stack.project_name=code4me-e2e-ci"]).stack.project_name)

    def test_invalid_ports_and_non_object_scenario_fail_before_startup(self):
        for override in ("stack.stub_port=0", "stack.db_port=28008", "stack.redis_port=false"):
            with self.subTest(override=override), self.assertRaises(ScenarioError):
                load_scenario(overrides=[override])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scenario.json"
            path.write_text("[]")
            with self.assertRaises(ScenarioError):
                load_scenario(str(path))

    def test_stub_does_not_change_the_registered_port_when_it_is_busy(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with self.assertRaises(OSError):
                StubProvider().start(occupied.getsockname()[1])

    def test_timeout_reaps_the_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            code = "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                process.run([sys.executable, "-c", code], cwd=path, log_path=path / "process.log", timeout=1)
            pid = int((path / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_multipart_body_carries_the_manifest_and_archive(self):
        from code4me_e2e.http import encode_multipart
        body, content_type = encode_multipart(
            {"manifest": '{"artifacts": []}'}, [("archives", "agent.zip", b"PK\x03\x04agent")]
        )
        boundary = content_type.split("boundary=", 1)[1]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="manifest"', body)
        self.assertIn(b'{"artifacts": []}', body)
        self.assertIn(b'filename="agent.zip"', body)
        self.assertIn(b"PK\x03\x04agent", body)
        self.assertTrue(body.rstrip().endswith(f"--{boundary}--".encode()))

    def test_register_release_imports_the_producer_manifest_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "native-macos-arm64.json"
            archive = Path(directory) / "code4me-agent-macos-arm64.zip"
            manifest.write_text(json.dumps({"artifacts": [{"archive": archive.name, "sha256": "a" * 64}]}))
            archive.write_bytes(b"PK\x03\x04agent")
            ctx = Ctx(load_scenario(), {"accounts": {}, "admin_user_id": "admin"}, Path(directory))
            with patch("code4me_e2e.steps.runtime.agent_release", return_value=(manifest, archive)), \
                 patch.object(Ctx, "client") as client:
                client.return_value.post_multipart.return_value = Mock(
                    status=201,
                    json={
                        "created": True,
                        "release": {"release_id": "code4me-agent-1.0.0-abc"},
                        "verified_artifacts": [{"verified": True}],
                    },
                )
                result = steps.step_register_release(ctx)
        self.assertEqual("code4me-agent-1.0.0-abc", result["release_id"])
        self.assertEqual("code4me-agent-1.0.0-abc", ctx.state["release_id"])
        self.assertEqual("code4me-agent-1.0.0-abc", ctx.scenario.agent.release_id)
        self.assertEqual("a" * 64, ctx.scenario.agent.artifact_digest)
        call = client.return_value.post_multipart.call_args
        self.assertEqual("/api/research/agents/releases/import", call.args[0])
        self.assertEqual(["archives"], [name for name, _, _ in call.kwargs["files"]])
        self.assertEqual(archive.name, call.kwargs["files"][0][1])
        self.assertIn('"archive"', call.kwargs["fields"]["manifest"])

    def test_qualify_release_requires_producer_test_results(self):
        ctx = Ctx(load_scenario(), {"release_id": "r1", "accounts": {}}, Path("."))
        with patch.object(Ctx, "client") as client:
            client.return_value.get.return_value = Mock(
                status=200, json={"release": {"status": "UNQUALIFIED"}}
            )
            with self.assertRaises(steps.StepFailure):
                steps.step_qualify_release(ctx)
            client.return_value.get.return_value = Mock(
                status=200, json={"release": {"status": "QUALIFIED"}}
            )
            self.assertEqual("QUALIFIED", steps.step_qualify_release(ctx)["derived_status"])


    def test_send_message_names_its_research_session(self):
        # The plugin layer's Kotlin fixture opens a second active session for the
        # same account; without an explicit id the server refuses to guess.
        state = {"accounts": {}, "acp_token": "token", "research_session_id": "rs-1"}
        ctx = Ctx(load_scenario(), state, Path("."))
        with patch.object(Ctx, "client") as client:
            client.return_value.post.return_value = Mock(
                status=409, json={"detail": {"code": "RESEARCH_CONTEXT_AMBIGUOUS"}}, text=""
            )
            with self.assertRaises(steps.StepFailure):
                steps.step_send_message(ctx)
        path, body = client.return_value.post.call_args.args[:2]
        self.assertEqual("/api/acp/runs", path)
        self.assertEqual("rs-1", body["research_session_id"])


if __name__ == "__main__":
    unittest.main()
