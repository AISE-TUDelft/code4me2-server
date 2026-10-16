"""Host prerequisite checks and provisioning decisions, without touching the host.

Every external effect (docker, colima, npm, pip, ps, ports) goes through
patched helpers; these tests never start a daemon, build an image, install a
package or signal a process.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from code4me_e2e import cli, prereqs, real_agents
from code4me_e2e.config import load_scenario


class _Calls:
    """Records every command a check would run and answers from a script."""

    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def run(self, command, **_kwargs):
        self.commands.append(list(command))
        for prefix, answer in self.answers:
            if list(command[: len(prefix)]) == list(prefix):
                return answer(command) if callable(answer) else answer
        raise AssertionError(f"unexpected command {command}")

    def ran(self, *prefix) -> bool:
        return any(command[: len(prefix)] == list(prefix) for command in self.commands)


class DockerCheckTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(prereqs.stack, "compose_command", return_value=["docker", "compose"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reachable_daemon_is_ready_without_starting_anything(self):
        calls = _Calls([(["docker", "info"], (0, "29.2.1"))])
        with patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            check = prereqs.check_docker(True)
        self.assertEqual(prereqs.READY, check.status)
        self.assertFalse(calls.ran("/bin/colima"))

    def test_down_daemon_starts_colima_only_when_provisioning(self):
        state = {"started": False}

        def info(_command):
            return (0, "29.2.1") if state["started"] else (1, "Cannot connect to the Docker daemon")

        def colima(command, log_name, **_kwargs):
            state["started"] = True
            return 0, Path("/tmp/colima-start.log")

        calls = _Calls([(["docker", "info"], info)])
        with patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs, "_run_logged", side_effect=colima) as logged, \
             patch.object(prereqs, "_colima_profile", return_value="default"), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             contextlib.redirect_stderr(io.StringIO()):
            checked = prereqs.check_docker(False)
            self.assertEqual(prereqs.MISSING, checked.status)
            logged.assert_not_called()
            provisioned = prereqs.check_docker(True)
        self.assertEqual(prereqs.PROVISIONED, provisioned.status)
        self.assertEqual(["/bin/colima", "start"], logged.call_args.args[0])

    def test_down_daemon_without_colima_is_blocked_with_a_remediation(self):
        calls = _Calls([(["docker", "info"], (1, "Cannot connect"))])
        with patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: None if name == "colima" else f"/bin/{name}"):
            check = prereqs.check_docker(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertTrue(check.remediation)

    def test_colima_is_never_started_behind_a_non_colima_context(self):
        calls = _Calls([(["docker", "info"], (1, "Cannot connect")),
                        (["docker", "context", "show"], (0, "desktop-linux\n"))])
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs, "_run_logged") as logged, \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            os.environ.pop("DOCKER_HOST", None)
            check = prereqs.check_docker(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        logged.assert_not_called()

    def test_the_colima_profile_follows_the_docker_endpoint(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "unix:///Users/x/.colima/dev/docker.sock"}):
            self.assertEqual("dev", prereqs._colima_profile())
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://127.0.0.1:2375"}):
            self.assertIsNone(prereqs._colima_profile())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCKER_HOST", None)
            for name, expected in (("colima", "default"), ("colima-dev", "dev"), ("default", None)):
                with patch.object(prereqs, "_run", return_value=(0, name + "\n")):
                    self.assertEqual(expected, prereqs._colima_profile(), name)
        started = []
        with patch.object(prereqs, "_docker_reachable", side_effect=[(False, "down"), (True, "29")]), \
             patch.object(prereqs, "_colima_profile", return_value="dev"), \
             patch.object(prereqs, "_run_logged", side_effect=lambda command, *a, **k: started.append(command) or (0, Path("/tmp/l"))), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prereqs.PROVISIONED, prereqs.check_docker(True).status)
        self.assertEqual([["/bin/colima", "start", "dev"]], started)

    def test_missing_cli_is_blocked(self):
        with patch.object(prereqs.shutil, "which", return_value=None):
            check = prereqs.check_docker(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertIn("docker CLI", check.detail)


class BackendImageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = Path(self._tmp.name)
        (self.server / "Dockerfile.cpu").write_text("FROM scratch\n")
        (self.server / "requirements.txt").write_text("fastapi==1\n")
        patcher = patch.object(prereqs, "_server_root", return_value=self.server)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scenario = load_scenario()

    def _check(self, answers, provision=True):
        calls = _Calls(answers)
        with patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs, "_run_logged", return_value=(0, Path("/tmp/build.log"))) as logged, \
             contextlib.redirect_stderr(io.StringIO()):
            check = prereqs.check_backend_image(provision, scenario=self.scenario)
        return check, calls, logged

    def test_harness_label_matching_the_inputs_is_ready(self):
        label = json.dumps({prereqs.IMAGE_LABEL: prereqs.image_fingerprint(self.server)})
        check, calls, logged = self._check([(["docker", "image", "inspect"], (0, label))])
        self.assertEqual(prereqs.READY, check.status)
        logged.assert_not_called()
        self.assertFalse(calls.ran("docker", "run"))

    def test_unlabelled_image_is_judged_by_its_requirements_content(self):
        check, _, logged = self._check([
            (["docker", "image", "inspect"], (0, "{}")),
            (["docker", "run"], (0, "fastapi==1\n")),
        ])
        self.assertEqual(prereqs.READY, check.status)
        logged.assert_not_called()

    def test_changed_requirements_rebuild_with_the_fingerprint_label(self):
        check, _, logged = self._check([
            (["docker", "image", "inspect"], (0, "{}")),
            (["docker", "run"], (0, "fastapi==0\n")),
        ])
        self.assertEqual(prereqs.PROVISIONED, check.status)
        command = logged.call_args.args[0]
        self.assertEqual(["docker", "build", "-f", "Dockerfile.cpu", "-t", self.scenario.stack.image], command[:6])
        self.assertIn(f"{prereqs.IMAGE_LABEL}={prereqs.image_fingerprint(self.server)}", command)

    def test_stale_label_and_missing_image_rebuild_but_check_mode_only_reports(self):
        stale = json.dumps({prereqs.IMAGE_LABEL: "0" * 64})
        check, _, logged = self._check([(["docker", "image", "inspect"], (0, stale))], provision=False)
        self.assertEqual(prereqs.MISSING, check.status)
        logged.assert_not_called()
        check, _, logged = self._check([(["docker", "image", "inspect"], (1, "No such image"))])
        self.assertEqual(prereqs.PROVISIONED, check.status)

    def test_failed_build_is_blocked(self):
        calls = _Calls([(["docker", "image", "inspect"], (1, "No such image"))])
        with patch.object(prereqs, "_run", side_effect=calls.run), \
             patch.object(prereqs, "_run_logged", return_value=(1, Path("/tmp/build.log"))), \
             contextlib.redirect_stderr(io.StringIO()):
            check = prereqs.check_backend_image(True, scenario=self.scenario)
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertIn("/tmp/build.log", check.remediation)


class NativePythonTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(prereqs, "_native_choice", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_first_candidate_that_imports_the_build_inputs_is_chosen(self):
        candidates = [("/srv/.venv/bin/python", "code4me2-server/.venv"), ("/e2e/.venv/bin/python", "e2e/.venv")]
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "native_python_candidates", return_value=candidates), \
             patch.object(prereqs, "_native_probe", side_effect=lambda python: (python.startswith("/e2e"), "No module named PyInstaller")), \
             patch.object(prereqs, "_run_logged") as logged:
            check = prereqs.check_native_python(False)
            self.assertEqual(prereqs.READY, check.status)
            self.assertIn("/e2e/.venv/bin/python", check.detail)
            self.assertEqual(("/e2e/.venv/bin/python", "e2e/.venv"), prereqs.resolve_native_python())
        logged.assert_not_called()

    def test_an_explicit_interpreter_is_blocked_not_replaced(self):
        with patch.object(prereqs, "native_python_candidates", return_value=[("/custom/python", "CODE4ME_E2E_PYTHON")]), \
             patch.object(prereqs, "_native_probe", return_value=(False, "No module named PyInstaller")), \
             patch.object(prereqs, "_run_logged") as logged:
            check = prereqs.check_native_python(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        logged.assert_not_called()

    def test_provisioning_is_pinned_like_the_release_ci(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(prereqs, "E2E_DIR", Path(tmp) / "e2e"), \
             patch.object(prereqs, "_workspace", return_value=Path(tmp)), \
             patch.object(prereqs, "native_python_candidates", return_value=[]), \
             patch.object(prereqs, "_native_probe", return_value=(True, "")), \
             patch.object(prereqs, "_run_logged", return_value=(0, Path("/tmp/log"))) as logged, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prereqs.MISSING, prereqs.check_native_python(False).status)
            logged.assert_not_called()
            check = prereqs.check_native_python(True)
            commands = [call.args[0] for call in logged.call_args_list]
            server = Path(tmp) / "code4me2-server"
        self.assertEqual(prereqs.PROVISIONED, check.status)
        self.assertEqual(["-m", "venv"], commands[0][1:3])
        self.assertIn(str(server / "packaging/requirements-runtime.lock"), commands[1])
        self.assertIn(str(server / "packaging/requirements-build.lock"), commands[1])
        self.assertEqual(["-m", "pip", "install", "--no-deps", "-e", str(server)], commands[2][1:])


class RunLoggedTest(unittest.TestCase):
    def test_an_interrupted_provisioning_step_leaves_no_process_behind(self):
        import _thread
        import threading
        import uuid
        marker = f"code4me-e2e-orphan-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp, patch.object(prereqs, "SETUP_LOG_DIR", Path(tmp)):
            timer = threading.Timer(1.0, _thread.interrupt_main)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    prereqs._run_logged(["/bin/sh", "-c", f"sleep 30; echo {marker}"], "interrupt.log", timeout=60)
            finally:
                timer.cancel()
        _, listing = prereqs._run(["ps", "-ww", "-eo", "pid=,command="], timeout=30)
        self.assertNotIn(marker, listing)


class TerminationTest(unittest.TestCase):
    def test_sigterm_during_provisioning_leaves_no_process_behind(self):
        import signal
        import subprocess
        import sys
        import textwrap
        import time
        import uuid
        marker = f"code4me-e2e-sigterm-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(prereqs.E2E_DIR)!r})
                from pathlib import Path
                from unittest.mock import patch
                from code4me_e2e import cli, prereqs

                def fake_main(argv):
                    prereqs._run_logged(["/bin/sh", "-c", "sleep 30; echo {marker}"], "t.log", timeout=60)
                    return 0

                with patch.object(prereqs, "SETUP_LOG_DIR", Path({tmp!r})), \\
                     patch.object(cli, "_main", side_effect=fake_main), \\
                     patch.object(cli.stack, "E2E_DIR", Path({tmp!r})):
                    sys.exit(cli.main(["setup"]))
            """)
            # A script file, not `-c`: only the build process may carry the marker.
            script_path = Path(tmp) / "terminate_me.py"
            script_path.write_text(script, encoding="utf-8")
            child = subprocess.Popen([sys.executable, str(script_path)])
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and marker not in prereqs._run(["ps", "-ww", "-eo", "command="])[1]:
                    time.sleep(0.2)
                self.assertIn(marker, prereqs._run(["ps", "-ww", "-eo", "command="])[1], "the build never started")
                child.send_signal(signal.SIGTERM)
                self.assertEqual(143, child.wait(timeout=30))
            finally:
                if child.poll() is None:
                    child.kill()
        self.assertNotIn(marker, prereqs._run(["ps", "-ww", "-eo", "command="])[1])


class PortProbeTest(unittest.TestCase):
    def test_a_live_listener_is_busy_and_a_closed_port_is_free(self):
        import socket
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertFalse(prereqs._port_free(port))
        finally:
            listener.close()
        self.assertTrue(prereqs._port_free(port))

    def test_time_wait_leftovers_do_not_count_as_busy(self):
        import socket
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        accepted, _ = server.accept()
        # The server side closes first, leaving TIME_WAIT on the listening port.
        accepted.close()
        client.close()
        server.close()
        self.assertTrue(prereqs._port_free(port))


class PortsAndIdesTest(unittest.TestCase):
    def test_free_ports_are_ready_and_foreign_listeners_block(self):
        scenario = load_scenario()
        with patch.object(prereqs, "_port_free", return_value=True):
            self.assertEqual(prereqs.READY, prereqs.check_stack_ports(True, scenario=scenario).status)
        busy = {scenario.stack.stub_port}
        with patch.object(prereqs, "_port_free", side_effect=lambda port: port not in busy), \
             patch.object(prereqs, "_port_owner", return_value=" (python 4242)"):
            check = prereqs.check_stack_ports(True, scenario=scenario)
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertIn("stack.stub_port", check.detail)

    def test_ports_held_by_the_own_compose_project_are_ready(self):
        scenario = load_scenario()
        own = {scenario.stack.backend_port, scenario.stack.db_port, scenario.stack.redis_port}
        with patch.object(prereqs, "_port_free", side_effect=lambda port: port not in own), \
             patch.object(prereqs.stack, "is_up", return_value=True):
            self.assertEqual(prereqs.READY, prereqs.check_stack_ports(True, scenario=scenario).status)
        with patch.object(prereqs, "_port_free", side_effect=lambda port: port not in own), \
             patch.object(prereqs.stack, "is_up", return_value=False), \
             patch.object(prereqs, "_port_owner", return_value=""):
            self.assertEqual(prereqs.BLOCKED, prereqs.check_stack_ports(True, scenario=scenario).status)

    def test_only_harness_ides_are_selected_and_signalled(self):
        runs = prereqs.E2E_DIR / "runs"
        listing = "\n".join([
            f"101 java -Duser.home={runs}/20260101T000000Z-aaaaaa/ide-home -Drobot-server.port=5000 idea",
            "102 java -Duser.home=/Users/someone -Drobot-server.port=5001 idea",
            f"103 java -Duser.home={runs}/20260101T000000Z-bbbbbb/ide-home idea-without-robot",
        ])
        with patch.object(prereqs, "_run", return_value=(0, listing)):
            self.assertEqual([101], [pid for pid, _ in prereqs._harness_ide_processes()])
            self.assertEqual(prereqs.MISSING, prereqs.check_stale_ides(False).status)
        signalled = []
        sequence = [[(101, "idea")], []]
        with patch.object(prereqs, "_harness_ide_processes", side_effect=lambda: sequence.pop(0) if sequence else []), \
             patch.object(prereqs.os, "kill", side_effect=lambda pid, sig: signalled.append((pid, sig))):
            check = prereqs.check_stale_ides(True)
        self.assertEqual(prereqs.PROVISIONED, check.status)
        self.assertEqual([(101, prereqs.signal.SIGTERM)], signalled)


class NodeAndBrowserTest(unittest.TestCase):
    def test_node_version_floor(self):
        with patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            with patch.object(prereqs, "_run", return_value=(0, "v22.23.2")):
                self.assertEqual(prereqs.READY, prereqs.check_node(True).status)
            with patch.object(prereqs, "_run", return_value=(0, "v16.20.0")):
                self.assertEqual(prereqs.BLOCKED, prereqs.check_node(True).status)
        with patch.object(prereqs.shutil, "which", return_value=None):
            self.assertEqual(prereqs.BLOCKED, prereqs.check_node(True).status)

    def test_explicit_browser_python_is_never_replaced(self):
        with patch.dict(os.environ, {"CODE4ME_E2E_BROWSER_PYTHON": "/custom/python"}), \
             patch.object(prereqs, "_chromium_ready", return_value=(False, "No module named playwright")), \
             patch.object(prereqs, "_run_logged") as logged:
            check = prereqs.check_browser_python(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        logged.assert_not_called()

    def test_missing_playwright_provisions_the_pinned_harness_venv(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "BROWSER_VENV", Path(tmp) / ".browser-venv"), \
             patch.object(prereqs, "_chromium_ready", side_effect=[(False, "missing"), (True, "")]), \
             patch.object(prereqs, "_run_logged", return_value=(0, Path("/tmp/log"))) as logged, \
             contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("CODE4ME_E2E_BROWSER_PYTHON", None)
            check = prereqs.check_browser_python(True)
            commands = [call.args[0] for call in logged.call_args_list]
            self.assertEqual(str(Path(tmp) / ".browser-venv/bin/python"), os.environ["CODE4ME_E2E_BROWSER_PYTHON"])
        self.assertEqual(prereqs.PROVISIONED, check.status)
        self.assertIn(f"playwright=={prereqs.PLAYWRIGHT_VERSION}", commands[1])
        self.assertEqual(["-m", "playwright", "install", "chromium"], commands[2][1:])

    def test_check_mode_never_installs(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "_chromium_ready", return_value=(False, "missing")), \
             patch.object(prereqs, "_run_logged") as logged:
            os.environ.pop("CODE4ME_E2E_BROWSER_PYTHON", None)
            self.assertEqual(prereqs.MISSING, prereqs.check_browser_python(False).status)
        logged.assert_not_called()


class AgentsTest(unittest.TestCase):
    def test_missing_goose_is_blocked_with_the_install_hint(self):
        blocked = real_agents.ProbeResult.blocked("goose", real_agents.AgentProbeReason.MISSING_BINARY,
                                                  "no goose executable found")
        with patch.object(prereqs.real_agents, "probe_identity", return_value=blocked):
            check = prereqs.check_goose(True)
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertIn("CODE4ME_E2E_GOOSE_EXECUTABLE", check.remediation)

    def _adapter(self, root: Path, *, built=True, installed=True, binary=True) -> Path:
        adapter = root / "codex-acp"
        (adapter / "src").mkdir(parents=True)
        (adapter / "src/index.ts").write_text("export {}\n")
        (adapter / "package.json").write_text("{}")
        if installed:
            (adapter / "node_modules").mkdir()
        if binary:
            native = adapter / "node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin"
            native.mkdir(parents=True)
            (native / "codex").write_text("binary")
        if built:
            (adapter / "dist").mkdir()
            (adapter / "dist/index.js").write_text("// built\n")
            later = (adapter / "src/index.ts").stat().st_mtime + 10
            os.utime(adapter / "dist/index.js", (later, later))
        return adapter

    def test_vendored_adapter_is_wrapped_and_returned(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "AGENT_BIN_DIR", Path(tmp) / "bin"), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            os.environ.pop("CODE4ME_E2E_CODEX_EXECUTABLE", None)
            adapter = self._adapter(Path(tmp))
            executables = {}
            with patch.object(prereqs, "vendored_codex_dir", return_value=adapter), \
                 patch.object(prereqs, "_run_logged") as logged:
                check = prereqs.check_codex_acp(True, executables=executables)
            logged.assert_not_called()
            wrapper = Path(executables["codex"])
            self.assertEqual(prereqs.READY, check.status)
            self.assertIn(str(adapter / "dist/index.js"), wrapper.read_text())
            self.assertTrue(os.access(wrapper, os.X_OK))

    def test_check_mode_never_writes_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "AGENT_BIN_DIR", Path(tmp) / "bin"), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            os.environ.pop("CODE4ME_E2E_CODEX_EXECUTABLE", None)
            adapter = self._adapter(Path(tmp))
            with patch.object(prereqs, "vendored_codex_dir", return_value=adapter):
                check = prereqs.check_codex_acp(False, executables={})
                self.assertEqual(prereqs.MISSING, check.status)
                self.assertFalse((Path(tmp) / "bin").exists())
                prereqs.check_codex_acp(True, executables={})
                executables = {}
                self.assertEqual(prereqs.READY, prereqs.check_codex_acp(False, executables=executables).status)
                self.assertTrue(executables["codex"].endswith("codex-acp"))

    def test_unbuilt_adapter_is_installed_and_built_only_when_provisioning(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "AGENT_BIN_DIR", Path(tmp) / "bin"), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("CODE4ME_E2E_CODEX_EXECUTABLE", None)
            adapter = self._adapter(Path(tmp), built=False, installed=False, binary=False)

            def build(command, log_name, **_kwargs):
                if command[1:] == ["ci"]:
                    native = adapter / "node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin"
                    native.mkdir(parents=True)
                    (native / "codex").write_text("binary")
                if command[1:] == ["run", "build"]:
                    (adapter / "dist").mkdir(exist_ok=True)
                    (adapter / "dist/index.js").write_text("// built\n")
                return 0, Path("/tmp/log")

            with patch.object(prereqs, "vendored_codex_dir", return_value=adapter), \
                 patch.object(prereqs, "_run_logged", side_effect=build) as logged:
                self.assertEqual(prereqs.MISSING, prereqs.check_codex_acp(False, executables={}).status)
                logged.assert_not_called()
                check = prereqs.check_codex_acp(True, executables={})
            self.assertEqual(prereqs.PROVISIONED, check.status)
            self.assertEqual([["/bin/npm", "ci"], ["/bin/npm", "run", "build"]],
                             [call.args[0] for call in logged.call_args_list])

    def test_quarantined_native_codex_binary_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(prereqs, "AGENT_BIN_DIR", Path(tmp) / "bin"), \
             patch.object(prereqs.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            os.environ.pop("CODE4ME_E2E_CODEX_EXECUTABLE", None)
            adapter = self._adapter(Path(tmp), binary=False)
            with patch.object(prereqs, "vendored_codex_dir", return_value=adapter):
                check = prereqs.check_codex_acp(True, executables={})
        self.assertEqual(prereqs.BLOCKED, check.status)
        self.assertIn("XProtect", check.detail)


class AgentProbeCliTest(unittest.TestCase):
    def test_an_unbuilt_vendored_adapter_blocks_instead_of_falling_back_to_path(self):
        missing = prereqs.Check("codex_acp", prereqs.MISSING, "the vendored adapter needs npm run build")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(cli.prereqs, "check_codex_acp", return_value=missing) as check, \
             patch.object(cli.real_agents, "probe_identity") as identity, \
             contextlib.redirect_stdout(out):
            os.environ.pop("CODE4ME_E2E_CODEX_EXECUTABLE", None)
            rc = cli.main(["agent-probe", "--framework", "codex", "--detect-only", "--json",
                           "--run-dir", tmp])
        self.assertEqual(1, rc)
        identity.assert_not_called()
        self.assertFalse(check.call_args.args[0], "the probe must never provision")
        self.assertFalse(check.call_args.kwargs["write_wrapper"], "--detect-only must not write the launcher")
        payload = json.loads(out.getvalue())
        self.assertEqual(("BLOCKED", "missing_binary"), (payload["status"], payload["reason"]))
        self.assertIn("setup --layer agents", payload["detail"])


class PrepareTest(unittest.TestCase):
    def test_layers_share_checks_and_blockers_name_their_remediation(self):
        seen = []

        def fake(name, status):
            def check(provision, **_kwargs):
                seen.append(name)
                return prereqs.Check(name, status, f"{name} detail", f"fix {name}")
            return check

        fakes = {name: fake(name, prereqs.READY) for name in prereqs.CHECKS}
        fakes["goose"] = fake("goose", prereqs.BLOCKED)
        with patch.dict(prereqs.CHECKS, fakes):
            result = prereqs.prepare(load_scenario(), ("backend", "agents"))
        self.assertEqual(len(seen), len(set(seen)), "a shared check must run once")
        self.assertFalse(result.ready)
        self.assertEqual([], result.blocking("backend"))
        self.assertEqual("goose: fix goose", result.blocked_reason("agents"))
        self.assertEqual({"backend": "READY", "agents": "BLOCKED"}, result.to_dict()["layers"])

    def test_an_unusable_daemon_blocks_dependent_checks_without_running_them(self):
        ran = []

        def fake(name, status):
            def check(provision, **_kwargs):
                ran.append(name)
                return prereqs.Check(name, status, name, "start docker" if name == "docker" else "")
            return check

        fakes = {name: fake(name, prereqs.READY) for name in prereqs.CHECKS}
        fakes["docker"] = fake("docker", prereqs.BLOCKED)
        with patch.dict(prereqs.CHECKS, fakes):
            result = prereqs.prepare(load_scenario(), ("backend",))
        self.assertNotIn("backend_image", ran)
        self.assertEqual(prereqs.BLOCKED, result.checks["backend_image"].status)
        self.assertEqual("start docker", result.checks["backend_image"].remediation)

    def test_a_crashing_check_is_blocked_not_raised(self):
        def boom(provision, **_kwargs):
            raise RuntimeError("kaboom")

        fakes = {name: (lambda provision, name=name, **_k: prereqs.Check(name, prereqs.READY)) for name in prereqs.CHECKS}
        fakes["node"] = boom
        with patch.dict(prereqs.CHECKS, fakes):
            result = prereqs.prepare(load_scenario(), ("agents",))
        self.assertEqual(prereqs.BLOCKED, result.checks["node"].status)
        self.assertIn("kaboom", result.checks["node"].detail)

    def test_setup_cli_reports_json_and_exit_status(self):
        blocked = prereqs.Prerequisites(("agents",), {"goose": prereqs.Check("goose", prereqs.BLOCKED, "absent", "install")})
        ready = prereqs.Prerequisites(("agents",), {"goose": prereqs.Check("goose", prereqs.READY, "ok")})
        for result, expected in ((blocked, 1), (ready, 0)):
            out = io.StringIO()
            with patch.object(cli.prereqs, "prepare", return_value=result) as prepare, \
                 patch.object(cli.stack, "E2E_DIR", Path(tempfile.mkdtemp())), \
                 contextlib.redirect_stdout(out):
                rc = cli.main(["setup", "--layer", "agents", "--check", "--json"])
            self.assertEqual(expected, rc)
            self.assertFalse(prepare.call_args.kwargs["provision"])
            self.assertEqual(result.ready, json.loads(out.getvalue())["ready"])


if __name__ == "__main__":
    unittest.main()
