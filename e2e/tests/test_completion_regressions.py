"""Failures observed while running the inherited harness against real agents."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import isolate_prerequisites
from code4me_e2e import acp, acp_probe, native_agents, real_agents, suite, workflow
from code4me_e2e.config import load_scenario
from code4me_e2e.steps import StepResult
from test_real_agents import _fake_agent_script, _make_executable, _write_registry


class CompletionRegressionTest(unittest.TestCase):
    def test_error_text_with_end_turn_cannot_pass_as_the_expected_model_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = _make_executable(root, "goose", _fake_agent_script("ok", "goose"))
            result = acp_probe.run_probe("goose", run_dir=root / "run", executable=str(executable),
                                        expected_substring="unique-provider-answer", timeout=5)
            self.assertFalse(result.passed)
            self.assertIn("expected provider answer", result.detail)

    def test_expected_answer_does_not_replace_the_live_provider_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            acp_probe, "run_probe", return_value=real_agents.ProbeResult.ok("goose")
        ):
            result = native_agents.probe("goose", Path(tmp))
            self.assertFalse(result.passed)
            self.assertIn("No request", result.detail)

    def test_agent_credentials_in_an_existing_home_cannot_be_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "existing"
            home.mkdir()
            (home / "config").write_text("synthetic existing configuration")
            with self.assertRaisesRegex(ValueError, "must be empty"):
                real_agents.isolated_home(Path(tmp), "codex", str(home))

    def test_digest_in_child_arguments_is_not_a_proxy_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = _make_executable(root, "proxy", "#!/bin/sh\necho test\n")
            _write_registry(root, {"Code4Me Research Proxy": {
                "command": str(executable),
                "args": ["--agent-cmd", str(executable), "--agent-digest", "a" * 64],
            }})
            with self.assertRaisesRegex(RuntimeError, "no --agent-digest before"):
                acp.entry_identity(root)

    def test_missing_layer_evidence_cannot_pass_even_with_zero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual("FAIL", suite.layer_outcome(Path(tmp), step_id="ui_test", rc=0)[0])

    def setUp(self):
        isolate_prerequisites(self)

    def test_backend_only_starts_at_account_creation_not_bootstrap(self):
        def execute(scenario, **kwargs):
            workflow._record_plugin_step(Path(kwargs["run_dir"]), scenario, StepResult("verify", "PASS", 1))
            return 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(workflow, "run_workflow", side_effect=execute) as run, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, suite.run(load_scenario(), layer="backend", run_dir=tmp, keep_stack=True))
            self.assertNotIn("from_step", run.call_args.kwargs)

    def test_unexpected_ide_exception_still_runs_independent_agents_and_browser(self):
        attempted = []
        def execute(name, scenario, run_path, executables=None):
            attempted.append(name)
            if name == "ui":
                raise RuntimeError("fixture launch crashed")
            workflow._record_plugin_step(run_path, scenario,
                StepResult(suite.LAYER_STEP_IDS[name], "PASS", 1))
            return 0
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite, "_run_layer", side_effect=execute), \
             patch.object(suite.report, "capture_backend_logs"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, suite.run(load_scenario(), run_dir=tmp, keep_stack=True))
            self.assertEqual(["ui", "agents", "browser"], attempted)
            built = json.loads((Path(tmp) / "report.json").read_text())
            self.assertEqual("suite", built["failed_step"])
            self.assertEqual(["FAIL", "NOT_ATTEMPTED", "NOT_ATTEMPTED", "PASS", "PASS"],
                             [item["status"] for item in built["layers"]])
