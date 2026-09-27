"""Regression tests: complete `all` gating, browser evidence inventory, isolation."""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import isolate_prerequisites
from code4me_e2e import browser, suite, ui, workflow
from code4me_e2e.config import load_scenario
from code4me_e2e.steps import StepResult

E2E_DIR = Path(__file__).resolve().parents[1]
SCENARIOS_PATH = E2E_DIR / "browser" / "scenarios.py"

PERSISTED_CONTEXT = {
    "study_id": "study-1",
    "edited_name": "Browser study (edited)",
    "edited_description": "Edited description",
    "clone_study_id": "study-2",
    "clone_name": "Browser study (edited) (copy)",
    "profile_id": "profile-1",
    "enrollment_id": "enrollment-1",
    "assignment_id": "assignment-1",
}


def _persisted_responses():
    return {
        "/api/research/studies/study-1": {"status": 200, "payload": {"study": {
            "name": "Browser study (edited)", "description": "Edited description",
            "consent_locked_at": "2026-01-01T00:00:00Z", "research_status": "STUDY_STOPPED",
            "enrollment_count": 1, "active_enrollment_count": 0, "assignment_count": 1,
        }}},
        "/api/research/studies/study-2": {"status": 200, "payload": {"study": {
            "name": "Browser study (edited) (copy)", "description": "Edited description",
            "research_status": "DRAFT", "profile_selections": [{"profile_id": "profile-1"}],
            "enrollment_count": 0, "assignment_count": 0,
            "lifecycle_capabilities": {"joinable": True},
        }}},
        "coverage": {"status": 200, "payload": {
            "participant_count": 1,
            "participants": [{
                "enrollment_id": "enrollment-1", "status": "STUDY_STOPPED",
                "assignment": {"assignment_id": "assignment-1",
                               "agent_profile_id": "profile-1",
                               "status": "STUDY_STOPPED"},
            }],
        }},
    }


def _fake_api(responses):
    def fake(base_url, path, cookies, timeout=20.0):
        if "/operations/participants/coverage" in path:
            return responses["coverage"]
        for key, response in responses.items():
            if key.startswith("/api/research/studies/") and path.endswith(key.rsplit("/", 1)[-1]):
                return response
        raise AssertionError(f"unexpected read: {path}")
    return fake


def _record_ui(run_dir, scenario, status, rc):
    ui.record_ui_step(Path(run_dir), scenario, StepResult("ui_test", status, 1, {}), {})
    return rc


def _record_plugin(run_dir, scenario, status, rc):
    workflow._record_plugin_step(Path(run_dir), scenario, StepResult("plugin_test", status, 1, {}))
    return rc


def _record_backend(run_dir, scenario, rc):
    workflow._record_plugin_step(Path(run_dir), scenario, StepResult("verify", "PASS", 1, {}))
    return rc


def _browser_side_effect(status, rc):
    def _run(scenario, run_path, **kwargs):
        workflow._record_plugin_step(Path(run_path), scenario, StepResult("browser_test", status, 1, {}))
        return rc
    return _run


def _record_plugin_step_for_agents(scenario, path, **_kwargs):
    workflow._record_plugin_step(Path(path), scenario, StepResult("agents_test", "PASS", 1))
    return 0


class LayerGateTest(unittest.TestCase):
    def setUp(self):
        isolate_prerequisites(self)
        patcher = patch.object(suite.native_agents, "run", side_effect=_record_plugin_step_for_agents)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_layer_plan_keeps_the_dependent_chain_and_adds_browser(self):
        self.assertEqual(("ui", "plugin", "backend", "agents", "browser"), suite.plan_layers("all"))
        self.assertEqual(("plugin", "backend"), suite.plan_layers("plugin"))
        self.assertEqual(("backend",), suite.plan_layers("backend"))
        self.assertEqual(("browser",), suite.plan_layers("browser"))

    def test_ui_failure_cannot_skip_the_independent_browser_layer(self):
        scenario = load_scenario()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ui, "run_ui_test",
                          side_effect=lambda s, **kw: _record_ui(kw["run_dir"], s, "FAIL", 1)), \
             patch.object(workflow, "run_plugin_test") as plugin, \
             patch.object(workflow, "run_workflow") as backend, \
             patch.object(browser, "run", side_effect=_browser_side_effect("PASS", 0)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(scenario, run_dir=directory)
            built = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(1, rc)
        plugin.assert_not_called()
        backend.assert_not_called()
        self.assertEqual([item["layer"] for item in built["layers"]],
                         ["ui", "plugin", "backend", "agents", "browser"])
        layers = {item["layer"]: item for item in built["layers"]}
        self.assertEqual("FAIL", layers["ui"]["status"])
        self.assertEqual("NOT_ATTEMPTED", layers["plugin"]["status"])
        self.assertEqual("NOT_ATTEMPTED", layers["backend"]["status"])
        self.assertFalse(layers["plugin"]["attempted"])
        self.assertEqual("PASS", layers["browser"]["status"])
        self.assertTrue(layers["browser"]["attempted"])
        self.assertEqual("ui_test", built["failed_step"])

    def test_browser_failure_fails_the_gate_and_keeps_every_layer_result(self):
        scenario = load_scenario()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ui, "run_ui_test",
                          side_effect=lambda s, **kw: _record_ui(kw["run_dir"], s, "PASS", 0)), \
             patch.object(workflow, "run_plugin_test",
                          side_effect=lambda s, **kw: _record_plugin(kw["run_dir"], s, "PASS", 0)), \
             patch.object(workflow, "run_workflow",
                          side_effect=lambda s, **kw: _record_backend(kw["run_dir"], s, 0)), \
             patch.object(browser, "run", side_effect=_browser_side_effect("FAIL", 1)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(scenario, run_dir=directory)
            built = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(1, rc)
        self.assertEqual([step["id"] for step in built["steps"]],
                         ["prerequisites", "ui_test", "plugin_test", "verify", "agents_test", "browser_test"])
        self.assertEqual("browser_test", built["failed_step"])
        self.assertTrue(all(item["attempted"] for item in built["layers"]))

    def test_blocked_browser_layer_is_not_a_pass(self):
        scenario = load_scenario()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(ui, "run_ui_test",
                          side_effect=lambda s, **kw: _record_ui(kw["run_dir"], s, "PASS", 0)), \
             patch.object(workflow, "run_plugin_test",
                          side_effect=lambda s, **kw: _record_plugin(kw["run_dir"], s, "PASS", 0)), \
             patch.object(workflow, "run_workflow",
                          side_effect=lambda s, **kw: _record_backend(kw["run_dir"], s, 0)), \
             patch.object(browser, "run", side_effect=_browser_side_effect("BLOCKED", 1)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(scenario, run_dir=directory)
            built = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(1, rc)
        layers = {item["layer"]: item for item in built["layers"]}
        self.assertEqual("BLOCKED", layers["browser"]["status"])
        self.assertEqual("browser_test", built["blocked_by"])

    def test_layer_outcome_reports_blocked_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory)
            workflow._record_plugin_step(
                run_path, load_scenario(),
                StepResult("browser_test", "BLOCKED", 1, {"error": "no playwright"}, "install Playwright"),
            )
            self.assertEqual(
                ("BLOCKED", "install Playwright"),
                suite.layer_outcome(run_path, step_id="browser_test", rc=1),
            )
            self.assertEqual(("BLOCKED", "install Playwright"), suite.layer_outcome(run_path, step_id="browser_test", rc=0))

    def test_browser_only_invocation_reports_just_its_own_layer(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(browser, "run", side_effect=_browser_side_effect("PASS", 0)), \
             patch.object(suite.stack, "down"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(load_scenario(), run_dir=directory, layer="browser")
            built = json.loads((Path(directory) / "report.json").read_text())
        self.assertEqual(0, rc)
        self.assertEqual(
            [{"layer": "browser", "status": "PASS", "attempted": True, "reason": ""}],
            built["layers"],
        )


class BlockedPrerequisiteIsolationTest(unittest.TestCase):
    def test_a_missing_goose_blocks_only_the_agents_layer(self):
        from _support import ready_prerequisites
        from code4me_e2e import prereqs
        from code4me_e2e import steps as step_module

        def only_goose_blocked(scenario, layers, *, provision=True):
            result = ready_prerequisites(scenario, layers)
            result.checks["goose"] = prereqs.Check("goose", prereqs.BLOCKED, "no goose", "install Goose")
            return result

        def fake_ui(scenario, *, run_dir, keep_stack=False, **_kwargs):
            # Like ui.run_ui_test: the real workflow prefix, then the ui step.
            rc = workflow.run_workflow(scenario, only=",".join(workflow.PLUGIN_PREFIX_STEPS),
                                       run_dir=run_dir, keep_stack=True)
            return _record_ui(run_dir, scenario, "PASS" if rc == 0 else "FAIL", rc)

        passing = {step_id: (lambda ctx: {}) for step_id in step_module.STEP_ORDER}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(suite.prereqs, "prepare", side_effect=only_goose_blocked), \
             patch.dict(workflow.STEPS, passing), \
             patch.object(workflow, "_own_stack", return_value=False), \
             patch.object(ui, "run_ui_test", side_effect=fake_ui), \
             patch.object(workflow, "run_plugin_test",
                          side_effect=lambda s, **kw: _record_plugin(kw["run_dir"], s, "PASS", 0)), \
             patch.object(suite.native_agents, "run") as agents, \
             patch.object(browser, "run", side_effect=_browser_side_effect("PASS", 0)), \
             patch.object(suite.report, "capture_backend_logs"), \
             patch.object(suite.stack, "down") as down, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = suite.run(load_scenario(), run_dir=directory)
            built = json.loads((Path(directory) / "report.json").read_text())
        layers = {item["layer"]: item["status"] for item in built["layers"]}
        self.assertEqual({"ui": "PASS", "plugin": "PASS", "backend": "PASS", "agents": "BLOCKED",
                          "browser": "PASS"}, layers)
        self.assertEqual(1, rc, "a blocked layer still fails the gate")
        agents.assert_not_called()
        down.assert_not_called()
        self.assertIn("install Goose", next(item for item in built["layers"] if item["layer"] == "agents")["reason"])


class BrowserEvidenceTest(unittest.TestCase):
    def _payload(self, *, ids=None, statuses=None, context=True):
        steps = [
            {"id": step_id, "title": step_id, "status": (statuses or {}).get(step_id, "PASS"), "detail": ""}
            for step_id in (ids if ids is not None else browser.EXPECTED_SCENARIO_IDS)
        ]
        payload = {"steps": steps}
        if context:
            payload["context"] = dict(PERSISTED_CONTEXT)
        return payload

    def test_exact_inventory_passes_and_is_returned(self):
        steps = browser.validate_scenario_results(self._payload())
        self.assertEqual(len(browser.EXPECTED_SCENARIO_IDS), len(steps))

    def test_missing_duplicated_blocked_unexpected_and_failed_ids_cannot_pass(self):
        without_clone = [step_id for step_id in browser.EXPECTED_SCENARIO_IDS if step_id != "C4"]
        cases = {
            "missing": (self._payload(ids=without_clone), "missing C4"),
            "duplicated": (self._payload(ids=[*browser.EXPECTED_SCENARIO_IDS, "A1"]), "duplicated A1"),
            "blocked": (self._payload(statuses={"C4": "BLOCKED"}), "not passing C4:BLOCKED"),
            "failed": (self._payload(statuses={"B6": "FAIL"}), "not passing B6:FAIL"),
            "unexpected": (self._payload(ids=[*browser.EXPECTED_SCENARIO_IDS, "Z9"]), "unexpected Z9"),
            "empty": ({"steps": []}, "no steps array"),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name), self.assertRaises(RuntimeError) as raised:
                browser.validate_scenario_results(payload)
            self.assertIn(expected, str(raised.exception))

    def test_context_requires_identity_and_distinct_clone(self):
        self.assertEqual(PERSISTED_CONTEXT, browser.validate_context(self._payload()))
        for context in ({**PERSISTED_CONTEXT, "clone_study_id": ""},
                        {**PERSISTED_CONTEXT, "clone_study_id": "study-1"}):
            with self.subTest(context=context), self.assertRaises(RuntimeError):
                browser.validate_context(self._payload() | {"context": context})

    def test_secret_material_is_refused(self):
        browser.assert_no_secret_material("clean evidence", secrets=["JOINCODE", ""])
        with self.assertRaises(RuntimeError) as raised:
            browser.assert_no_secret_material('{"join_code": "JOINCODE"}', secrets=["JOINCODE"])
        self.assertIn("private material", str(raised.exception))

    def test_persisted_outcomes_are_re_read_from_the_api_models(self):
        state = {"cookies": {"researcher": {"auth_token": "token"}}}
        with patch.object(browser, "_api_get", side_effect=_fake_api(_persisted_responses())):
            checks = browser.verify_persisted_outcomes(load_scenario(), state, PERSISTED_CONTEXT)
        self.assertTrue(all(checks.values()))
        self.assertEqual(
            {"edited_metadata_persisted", "stopped_state_retained",
             "clone_identity_profiles_status", "enrollment_assignment_coverage"},
            set(checks),
        )

    def test_persisted_outcomes_fail_on_any_stale_read_model(self):
        state = {"cookies": {"researcher": {"auth_token": "token"}}}
        responses = _persisted_responses()
        responses["/api/research/studies/study-2"]["payload"]["study"]["research_status"] = "ACTIVE"
        with patch.object(browser, "_api_get", side_effect=_fake_api(responses)), \
             self.assertRaises(RuntimeError) as raised:
            browser.verify_persisted_outcomes(load_scenario(), state, PERSISTED_CONTEXT)
        self.assertIn("clone_identity_profiles_status", str(raised.exception))

    def test_persisted_outcomes_require_a_researcher_session(self):
        with self.assertRaises(RuntimeError):
            browser.verify_persisted_outcomes(load_scenario(), {}, PERSISTED_CONTEXT)


class ScenarioSourceTest(unittest.TestCase):
    def test_scenarios_declare_every_expected_id_exactly_once(self):
        source = SCENARIOS_PATH.read_text(encoding="utf-8")
        declared = re.findall(r'\brecord\(\s*"([A-Za-z0-9]+)"', source)
        self.assertIn("harness", declared)
        self.assertEqual(
            sorted(browser.EXPECTED_SCENARIO_IDS), sorted(set(declared) - {"harness"})
        )
        self.assertEqual(
            len(browser.EXPECTED_SCENARIO_IDS), len(set(browser.EXPECTED_SCENARIO_IDS))
        )
        blocked = re.findall(r'\brecord_blocked\(\s*"([A-Za-z0-9]+)"', source)
        self.assertTrue(set(blocked) <= set(browser.EXPECTED_SCENARIO_IDS))
        self.assertIn("CODE4ME_E2E_BROWSER_EXPECTED_IDS", source)
        self.assertNotIn("code={join_code}", source)
        self.assertNotIn("prefilled={prefilled}", source)

    def test_runner_passes_the_inventory_to_the_child(self):
        source = (E2E_DIR / "code4me_e2e" / "browser.py").read_text(encoding="utf-8")
        self.assertIn("CODE4ME_E2E_BROWSER_EXPECTED_IDS", source)
        self.assertIn("browser-state", source)


class ScenarioModuleTest(unittest.TestCase):
    def test_scenario_module_records_ids_without_a_live_browser(self):
        import importlib.util
        import sys
        from types import ModuleType

        fake_playwright = ModuleType("playwright.sync_api")
        fake_playwright.sync_playwright = lambda: None
        package = ModuleType("playwright")
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": fake_playwright}):
            spec = importlib.util.spec_from_file_location("e2e_browser_scenarios", SCENARIOS_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            module.record("A1", "a title", True)
            module.record_blocked("A2", "another title", "a prerequisite")
        self.assertEqual(["A1", "A2"], [item["id"] for item in module.results])
        self.assertEqual(["PASS", "BLOCKED"], [item["status"] for item in module.results])


class BrowserIsolationTest(unittest.TestCase):
    def test_browser_bootstrap_uses_its_own_state_and_never_the_run_state(self):
        scenario = load_scenario()
        payload = {
            "steps": [
                {"id": step_id, "title": step_id, "status": "PASS", "detail": ""}
                for step_id in browser.EXPECTED_SCENARIO_IDS
            ],
            "context": dict(PERSISTED_CONTEXT),
        }
        with tempfile.TemporaryDirectory() as directory:
            run_path = Path(directory)
            state_dir = run_path / browser.BROWSER_STATE_DIRNAME
            state_dir.mkdir()
            (state_dir / "state.json").write_text(json.dumps({
                "config_id": 7, "join_code": "JOIN-CODE-SECRET",
                "cookies": {"researcher": {"auth_token": "session-token"}},
            }))
            website = run_path / "code4me2-server" / "src/website"
            (website / "node_modules").mkdir(parents=True)
            seen = {}

            def fake_workflow(scenario_, *, only=None, run_dir=None, keep_stack=False, **kwargs):
                seen["bootstrap_run_dir"] = Path(run_dir)
                return 0

            def fake_process_run(command, *, cwd, log_path, env=None, timeout=1200):
                if len(command) > 1 and command[1] == "-u":
                    seen["scenarios_env"] = dict(env or {})
                    Path(env["CODE4ME_E2E_BROWSER_RESULTS"]).write_text(json.dumps(payload))
                return 0

            class _ReadyResponse:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *exception):
                    return False

            class _Child:
                pid = 4242

                def poll(self):
                    return None

            with patch.object(browser, "_require_browser_prerequisites",
                              return_value=("python", "node", "npm")), \
                 patch.object(browser.workflow, "run_workflow", side_effect=fake_workflow), \
                 patch.object(browser.process, "run", side_effect=fake_process_run), \
                 patch.object(browser.process, "stop"), \
                 patch.object(browser, "require_workspace", return_value=run_path), \
                 patch.object(browser.subprocess, "Popen", return_value=_Child()), \
                 patch.object(browser.urllib.request, "urlopen", return_value=_ReadyResponse()), \
                 patch.object(browser, "_api_get", side_effect=_fake_api(_persisted_responses())), \
                 patch.object(browser, "login_cookies", return_value={"auth_token": "fresh"}):
                rc = browser.run(scenario, run_path)
            self.assertEqual(0, rc)
            self.assertEqual(state_dir.resolve(), seen["bootstrap_run_dir"].resolve())
            self.assertFalse((run_path / "state.json").exists())
            self.assertEqual(
                list(browser.EXPECTED_SCENARIO_IDS),
                json.loads(seen["scenarios_env"]["CODE4ME_E2E_BROWSER_EXPECTED_IDS"]),
            )
            built = json.loads((run_path / "report.json").read_text())
            step = next(item for item in built["steps"] if item["id"] == "browser_test")
            self.assertEqual("PASS", step["status"])
            self.assertTrue(all(step["details"]["persisted"].values()))
            self.assertNotIn("JOIN-CODE-SECRET", (run_path / "browser-results.json").read_text())


if __name__ == "__main__":
    unittest.main()
