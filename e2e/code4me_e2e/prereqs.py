"""Host prerequisites per layer: check them, provision what the harness owns.

Nobody (human or coding agent) should have to prepare the host by hand before
the gate. :func:`prepare` checks every prerequisite of the planned layers and,
unless ``provision=False``, provides what the harness can own itself:

* starts Colima when the Docker daemon is down and the active docker context
  is a Colima one (never creates a VM or switches contexts);
* builds the CPU backend image when it is missing or its ``requirements.txt``
  differs from the checkout (content check, not a timestamp guess);
* creates ``e2e/.venv`` for the native builds (pinned like the release CI)
  when no existing interpreter can import the build inputs;
* creates ``e2e/.browser-venv`` with Playwright and its Chromium (Playwright
  keeps browsers in its shared per-user cache);
* installs the website's npm dependencies when ``node_modules`` is absent;
* builds the plugin's vendored Codex ACP adapter (gitignored ``node_modules`` /
  ``dist``) and exposes it through a harness-owned wrapper;
* stops sandbox IDEs left behind by an earlier, interrupted harness run.

One-time host tools (Docker or Colima, a JDK, Node.js, Python, Goose) are only
detected. A prerequisite the harness cannot provide is ``BLOCKED`` with the
exact remediation and blocks the layers that need it; it is never a pass.
``setup --check`` (``provision=False``) changes nothing and reports ``MISSING``
for what a provisioning run would do.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import real_agents, stack
from .paths import E2E_DIR, WORKSPACE_ROOT

READY = "READY"
PROVISIONED = "PROVISIONED"
MISSING = "MISSING"
BLOCKED = "BLOCKED"

#: Prerequisites of each layer, in check order (a later check may rely on an
#: earlier one, e.g. the image check needs a reachable daemon).
#: Every stack layer imports the producer-built agent release, so each needs
#: the native build Python (PyInstaller + server runtime).
LAYER_REQUIREMENTS: Dict[str, Tuple[str, ...]] = {
    "backend": ("docker", "backend_image", "stack_ports", "native_python"),
    "plugin": ("docker", "backend_image", "stack_ports", "native_python", "java"),
    "ui": ("docker", "backend_image", "stack_ports", "native_python", "java",
           "gui_session", "stale_ides"),
    "agents": ("goose", "node", "codex_acp"),
    "browser": ("docker", "backend_image", "stack_ports", "native_python", "node",
                "website_deps", "browser_python"),
}

#: Image label recording which Dockerfile/requirements a harness build used.
IMAGE_LABEL = "org.code4me.e2e.deps"

#: Playwright pinned for the harness-owned browser venv (the version the
#: browser layer is verified with).
PLAYWRIGHT_VERSION = "1.63.0"

BROWSER_VENV = E2E_DIR / ".browser-venv"
SETUP_LOG_DIR = E2E_DIR / ".cache/setup"
AGENT_BIN_DIR = E2E_DIR / ".cache/agents/bin"

GOOSE_INSTALL_HINT = (
    "install Goose once (https://block.github.io/goose/docs/getting-started/installation, e.g. "
    "`curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh "
    "| CONFIGURE=false bash`) or set CODE4ME_E2E_GOOSE_EXECUTABLE"
)


@dataclass
class Check:
    id: str
    status: str
    detail: str = ""
    remediation: str = ""

    @property
    def usable(self) -> bool:
        return self.status in (READY, PROVISIONED)

    def to_dict(self) -> Dict[str, str]:
        payload = {"id": self.id, "status": self.status, "detail": self.detail}
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass
class Prerequisites:
    """Outcome of one :func:`prepare` call."""

    layers: Tuple[str, ...]
    checks: Dict[str, Check] = field(default_factory=dict)
    #: Resolved executables the layers must use (e.g. the Codex adapter).
    executables: Dict[str, str] = field(default_factory=dict)

    def blocking(self, layer: str) -> List[Check]:
        return [self.checks[name] for name in LAYER_REQUIREMENTS.get(layer, ())
                if name in self.checks and not self.checks[name].usable]

    def blocked_reason(self, layer: str) -> str:
        return "; ".join(
            f"{check.id}: {check.remediation or check.detail}" for check in self.blocking(layer)
        )

    @property
    def ready(self) -> bool:
        return all(check.usable for check in self.checks.values())

    def to_dict(self) -> Dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks.values()],
            "layers": {layer: ("BLOCKED" if self.blocking(layer) else "READY") for layer in self.layers},
        }


# ---------------------------------------------------------------------------
# Small process helpers (patched in unit tests)
# ---------------------------------------------------------------------------


def _run(command: Sequence[str], *, timeout: float = 60, cwd: Optional[Path] = None,
         env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run a bounded command; ``(127, reason)`` when it cannot start."""
    try:
        result = subprocess.run(list(command), cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout)
    except FileNotFoundError as error:
        return 127, str(error)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {int(timeout)}s"
    except OSError as error:
        return 126, str(error)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _run_logged(command: Sequence[str], log_name: str, *, timeout: float, cwd: Optional[Path] = None,
                env: Optional[Dict[str, str]] = None) -> Tuple[int, Path]:
    """Run a long provisioning command with its output in ``.cache/setup``."""
    SETUP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = SETUP_LOG_DIR / log_name
    with log_path.open("w", encoding="utf-8") as log:
        log.write("# " + " ".join(command) + "\n")
        log.flush()
        try:
            child = subprocess.Popen(list(command), cwd=cwd, env=env, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            log.write(f"could not start: {error}\n")
            return 127, log_path
        try:
            return child.wait(timeout=timeout), log_path
        except subprocess.TimeoutExpired:
            log.write(f"\n# timed out after {int(timeout)}s\n")
            return 124, log_path
        finally:
            # Never leave a detached build/install behind: an interrupted gate
            # (Ctrl-C, an agent's tool timeout) must not race the next run.
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def _say(message: str) -> None:
    print(f"setup: {message}", file=sys.stderr)


def _workspace() -> Optional[Path]:
    return WORKSPACE_ROOT


# ---------------------------------------------------------------------------
# Docker daemon and backend image
# ---------------------------------------------------------------------------


def _docker_reachable() -> Tuple[bool, str]:
    rc, output = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    return rc == 0, output.strip()[-300:]


def check_docker(provision: bool, **_: object) -> Check:
    if not shutil.which("docker"):
        return Check("docker", BLOCKED, "the docker CLI is not on PATH",
                     "install Docker Desktop, or Colima with the docker CLI and compose plugin "
                     "(`brew install colima docker docker-compose`)")
    try:
        compose = " ".join(stack.compose_command())
    except stack.StackError as error:
        return Check("docker", BLOCKED, str(error),
                     "install the Docker Compose plugin (`docker compose`) or `docker-compose`")
    ok, output = _docker_reachable()
    if ok:
        return Check("docker", READY, f"daemon {output or 'reachable'}; {compose}")
    colima = shutil.which("colima")
    profile = _colima_profile() if colima else None
    if not colima or profile is None:
        return Check("docker", BLOCKED, f"the Docker daemon is not reachable: {output}",
                     "start Docker Desktop or Colima (the harness only starts Colima when the "
                     "active docker context is a Colima one)")
    command = [colima, "start"] + ([] if profile == "default" else [profile])
    if not provision:
        shown = " ".join(["colima", *command[1:]])
        return Check("docker", MISSING, f"the Docker daemon is not reachable; setup will run `{shown}`")
    _say(f"the Docker daemon is not reachable; starting Colima profile {profile}")
    rc, log_path = _run_logged(command, "colima-start.log", timeout=900)
    ok, output = _docker_reachable()
    if ok:
        return Check("docker", PROVISIONED, f"started Colima; daemon {output}")
    return Check("docker", BLOCKED, f"`colima start` exited {rc}; the daemon is still unreachable",
                 f"inspect {log_path} and start Colima/Docker manually")


def _colima_profile() -> Optional[str]:
    """The Colima profile behind the active docker endpoint, or ``None``.

    Starting Colima is only right when Docker is already pointed at it; on a
    Docker Desktop host it would create a VM and switch the global context.
    """
    host = os.environ.get("DOCKER_HOST", "")
    if host:
        match = re.search(r"/\.colima/([^/]+)/docker\.sock", host)
        return match.group(1) if match else None
    rc, output = _run(["docker", "context", "show"], timeout=15)
    name = (output.strip().splitlines() or [""])[-1].strip() if rc == 0 else ""
    if name == "colima":
        return "default"
    if name.startswith("colima-"):
        return name[len("colima-"):]
    return None


def _server_root() -> Optional[Path]:
    root = _workspace()
    return root / "code4me2-server" if root else None


def image_fingerprint(server: Path) -> str:
    """Identity of the image's dependency inputs (Dockerfile + requirements)."""
    digest = hashlib.sha256()
    for name in ("Dockerfile.cpu", "requirements.txt"):
        digest.update(name.encode())
        digest.update((server / name).read_bytes())
    return digest.hexdigest()


def _image_labels(image: str) -> Optional[Dict[str, str]]:
    rc, output = _run(["docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"],
                      timeout=60)
    if rc != 0:
        return None
    try:
        labels = json.loads(output.strip().splitlines()[-1] or "null")
    except (ValueError, IndexError):
        return {}
    return labels if isinstance(labels, dict) else {}


def _image_requirements(image: str) -> Optional[str]:
    rc, output = _run(["docker", "run", "--rm", "--entrypoint", "cat", image, "/app/requirements.txt"],
                      timeout=120)
    return output if rc == 0 else None


def image_freshness(image: str, server: Path) -> Tuple[Optional[bool], str]:
    """``(None, why)`` when absent, else ``(fresh, why)``."""
    labels = _image_labels(image)
    if labels is None:
        return None, f"{image} is not present"
    expected = image_fingerprint(server)
    recorded = labels.get(IMAGE_LABEL)
    if recorded:
        if recorded == expected:
            return True, f"{image} was built by the harness from the current Dockerfile.cpu/requirements.txt"
        return False, f"{image} was built from an older Dockerfile.cpu/requirements.txt"
    inside = _image_requirements(image)
    if inside is None:
        return False, f"could not read /app/requirements.txt from {image}"
    if inside == (server / "requirements.txt").read_text(encoding="utf-8"):
        return True, f"{image} carries the checkout's requirements.txt"
    return False, f"{image} carries a different requirements.txt than the checkout"


def check_backend_image(provision: bool, *, scenario, **_: object) -> Check:
    server = _server_root()
    if server is None or not (server / "Dockerfile.cpu").is_file():
        return Check("backend_image", BLOCKED, "code4me2-server/Dockerfile.cpu was not found",
                     "run the harness from the Code4Me workspace (or set CODE4ME_E2E_WORKSPACE)")
    image = scenario.stack.image
    fresh, why = image_freshness(image, server)
    if fresh:
        return Check("backend_image", READY, why)
    if not provision:
        return Check("backend_image", MISSING, f"{why}; setup will build it from Dockerfile.cpu")
    _say(f"{why}; building it (the first build downloads several GB and can take a while)")
    started = time.monotonic()
    rc, log_path = _run_logged(
        ["docker", "build", "-f", "Dockerfile.cpu", "-t", image,
         "--label", f"{IMAGE_LABEL}={image_fingerprint(server)}", "."],
        "backend-image-build.log", timeout=3 * 3600, cwd=server,
    )
    if rc:
        return Check("backend_image", BLOCKED, f"docker build exited {rc}",
                     f"inspect {log_path}; fix the build and rerun")
    return Check("backend_image", PROVISIONED,
                 f"built {image} in {int(time.monotonic() - started)}s ({why})")


def _port_free(port: int) -> bool:
    """True unless something listens on the loopback port.

    A plain bind also fails on TIME_WAIT sockets left by the previous run's
    connections; Docker's forwarder and the stub bind with SO_REUSEADDR, so
    only a live listener (or a reuse-address bind failure) is a conflict.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _port_owner(port: int) -> str:
    if not shutil.which("lsof"):
        return ""
    rc, output = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=15)
    lines = [line for line in output.splitlines()[1:] if line.strip()]
    return (" (" + " ".join(lines[0].split()[:2]) + ")") if lines else ""


def check_stack_ports(provision: bool, *, scenario, **_: object) -> Check:
    ports = {
        "stack.backend_port": scenario.stack.backend_port,
        "stack.db_port": scenario.stack.db_port,
        "stack.redis_port": scenario.stack.redis_port,
        "stack.stub_port": scenario.stack.stub_port,
    }
    busy = {name: port for name, port in ports.items() if not _port_free(port)}
    if not busy:
        return Check("stack_ports", READY, "ports " + ", ".join(str(port) for port in ports.values()) + " are free")
    compose_owned = {"stack.backend_port", "stack.db_port", "stack.redis_port"}
    if set(busy) <= compose_owned:
        try:
            own = stack.is_up(scenario)
        except Exception:  # noqa: BLE001 - an unknown owner stays blocked
            own = False
        if own:
            return Check("stack_ports", READY,
                         f"ports held by this harness's own compose project {scenario.stack.project_name}")
    described = ", ".join(f"{name}={port}{_port_owner(port)}" for name, port in busy.items())
    return Check("stack_ports", BLOCKED, f"ports in use by another process: {described}",
                 "stop that process or choose free ports with --set stack.<name>_port=N")


# ---------------------------------------------------------------------------
# JDK, native build Python, GUI session, leftover IDEs
# ---------------------------------------------------------------------------


def check_java(provision: bool, **_: object) -> Check:
    java_home = os.environ.get("JAVA_HOME")
    java = str(Path(java_home) / "bin/java") if java_home else shutil.which("java")
    if not java:
        return Check("java", BLOCKED, "no java on PATH and JAVA_HOME is unset",
                     "install a JDK (17 or newer); Gradle provisions the plugin's own toolchain")
    rc, output = _run([java, "-version"], timeout=30)
    first = next((line for line in output.splitlines() if line.strip()), "")
    if rc != 0:
        return Check("java", BLOCKED, f"`java -version` failed: {first}",
                     "install a JDK (17 or newer) or point JAVA_HOME at one")
    return Check("java", READY, first.strip())


#: What the native builds import: PyInstaller, the agent runtime, and the
#: server's research package (release producer, proxy contracts).
_NATIVE_PROBE = "import PyInstaller, acp, openai, pydantic, research.study.agents.participant_release"

_native_choice: Optional[Tuple[str, str]] = None


def native_python_candidates() -> List[Tuple[str, str]]:
    """Interpreters for the native builds, in preference order."""
    override = os.environ.get("CODE4ME_E2E_PYTHON")
    if override:
        return [(override, "CODE4ME_E2E_PYTHON")]
    server = _server_root()
    found = []
    for candidate, label in (((server / ".venv/bin/python") if server else None, "code4me2-server/.venv"),
                             (E2E_DIR / ".venv/bin/python", "e2e/.venv")):
        if candidate is not None and candidate.is_file():
            found.append((str(candidate), label))
    return found


def _native_probe(python: str) -> Tuple[bool, str]:
    rc, output = _run([python, "-c", _NATIVE_PROBE], timeout=120, cwd=_server_root())
    return rc == 0, (output.strip().splitlines() or [""])[-1][:300]


def resolve_native_python() -> Optional[Tuple[str, str]]:
    """The first candidate that imports every build input (cached per process)."""
    global _native_choice
    if _native_choice is None:
        for python, source in native_python_candidates():
            if _native_probe(python)[0]:
                _native_choice = (python, source)
                break
    return _native_choice


def check_native_python(provision: bool, **_: object) -> Check:
    global _native_choice
    remediation = (
        "let `python3 -m code4me_e2e setup` create e2e/.venv, or point CODE4ME_E2E_PYTHON at an "
        "interpreter with packaging/requirements-{runtime,build}.lock and the editable server package"
    )
    candidates = native_python_candidates()
    for python, source in candidates:
        ok, why = _native_probe(python)
        if ok:
            _native_choice = (python, source)
            return Check("native_python", READY, f"{python} ({source})")
        if source == "CODE4ME_E2E_PYTHON":
            return Check("native_python", BLOCKED, f"{python} cannot import the build inputs: {why}", remediation)
    tried = ", ".join(source for _, source in candidates) or "no server venv"
    if not provision:
        return Check("native_python", MISSING, f"{tried}: cannot import the build inputs; setup will create e2e/.venv")
    return _provision_native_venv(remediation)


def _provision_native_venv(remediation: str) -> Check:
    """``e2e/.venv`` pinned like the release build CI (runtime + build locks)."""
    global _native_choice
    root = _workspace()
    server = root / "code4me2-server" if root else None
    if server is None:
        return Check("native_python", BLOCKED, "workspace not found", remediation)
    venv = E2E_DIR / ".venv"
    python = venv / "bin/python"
    _say(f"creating {venv} for the native agent/proxy builds")
    steps = []
    if not python.is_file():
        steps.append(([sys.executable, "-m", "venv", str(venv)], "native-venv.log", 300))
    steps += [
        ([str(python), "-m", "pip", "install",
          "-r", str(server / "packaging/requirements-runtime.lock"),
          "-r", str(server / "packaging/requirements-build.lock")], "native-venv-locks.log", 1800),
        # Editable server only: `research` resolves from its src/. The proxy is
        # bundled from its own directory; installing it would write egg-info
        # into the plugin checkout.
        ([str(python), "-m", "pip", "install", "--no-deps", "-e", str(server)],
         "native-venv-editable.log", 600),
    ]
    for command, log_name, timeout in steps:
        rc, log_path = _run_logged(command, log_name, timeout=timeout)
        if rc:
            return Check("native_python", BLOCKED, f"`{' '.join(command[1:4])} …` exited {rc}; see {log_path}",
                         remediation)
    ok, why = _native_probe(str(python))
    if not ok:
        return Check("native_python", BLOCKED, f"{python} still cannot import the build inputs: {why}", remediation)
    _native_choice = (str(python), "e2e/.venv")
    return Check("native_python", PROVISIONED, f"created {venv} (pinned runtime + build locks)")


def check_gui_session(provision: bool, **_: object) -> Check:
    system = platform.system()
    if system == "Darwin":
        rc, output = _run(["launchctl", "managername"], timeout=15)
        manager = output.strip()
        if rc == 0 and manager == "Aqua":
            return Check("gui_session", READY,
                         "macOS GUI session (the IDE layer must also run outside a command sandbox)")
        return Check("gui_session", BLOCKED, f"no macOS GUI login session (launchctl manager: {manager or 'unknown'})",
                     "run the IDE layer from a logged-in desktop session, not over SSH")
    if system == "Linux":
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return Check("gui_session", READY, f"display {os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')}")
        return Check("gui_session", BLOCKED, "DISPLAY is not set",
                     "start an X server (for example `Xvfb :99 & export DISPLAY=:99`) or use xvfb-run")
    return Check("gui_session", BLOCKED, f"the IDE layer is not supported on {system}")


def _harness_ide_processes() -> List[Tuple[int, str]]:
    """Sandbox IDEs started by this harness (their home is under e2e/runs)."""
    marker = f"-Duser.home={E2E_DIR / 'runs'}"
    rc, output = _run(["ps", "-ww", "-eo", "pid=,command="], timeout=30)
    found = []
    for line in output.splitlines():
        if marker in line and "-Drobot-server.port=" in line:
            pid, _, command = line.strip().partition(" ")
            if pid.isdigit() and int(pid) != os.getpid():
                found.append((int(pid), command))
    return found


def check_stale_ides(provision: bool, **_: object) -> Check:
    """A gate holds the workspace lock, so any harness IDE still running is a leftover."""
    stale = _harness_ide_processes()
    if not stale:
        return Check("stale_ides", READY, "no leftover harness IDE is running")
    if not provision:
        return Check("stale_ides", MISSING, f"{len(stale)} leftover harness IDE(s); setup will stop them")
    for pid, _ in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _harness_ide_processes():
        time.sleep(1)
    for pid, _ in _harness_ide_processes():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return Check("stale_ides", PROVISIONED, f"stopped {len(stale)} leftover harness IDE(s)")


# ---------------------------------------------------------------------------
# Node, website dependencies, browser Python
# ---------------------------------------------------------------------------


def check_node(provision: bool, **_: object) -> Check:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        return Check("node", BLOCKED, "node and npm must be on PATH",
                     "install Node.js 18 or newer (22 is verified), e.g. `brew install node@22`")
    rc, output = _run([node, "--version"], timeout=30)
    match = re.match(r"v(\d+)", output.strip())
    if rc != 0 or not match or int(match.group(1)) < 18:
        return Check("node", BLOCKED, f"node {output.strip() or 'unknown'} is too old or unusable",
                     "install Node.js 18 or newer (22 is verified)")
    return Check("node", READY, f"node {output.strip()} ({node})")


def check_website_deps(provision: bool, **_: object) -> Check:
    server = _server_root()
    website = server / "src/website" if server else None
    if website is None or not (website / "package.json").is_file():
        return Check("website_deps", BLOCKED, "src/website/package.json was not found")
    modules = website / "node_modules"
    if modules.is_dir():
        # Present dependencies are used as they are; a stale install surfaces as
        # a failed website build with its log (browser-build.log).
        return Check("website_deps", READY, f"{modules} present")
    if not provision:
        return Check("website_deps", MISSING, "src/website/node_modules is absent; setup will run `npm ci`")
    npm = shutil.which("npm")
    if not npm:
        return Check("website_deps", BLOCKED, "npm is not on PATH", "install Node.js 18 or newer")
    _say("installing the website dependencies (npm ci)")
    rc, log_path = _run_logged([npm, "ci"], "website-npm-ci.log", timeout=1800, cwd=website)
    if rc:
        return Check("website_deps", BLOCKED, f"`npm ci` exited {rc}", f"inspect {log_path}")
    return Check("website_deps", PROVISIONED, "installed src/website/node_modules (npm ci)")


def browser_python() -> Tuple[str, str]:
    """The Playwright interpreter and where it came from."""
    override = os.environ.get("CODE4ME_E2E_BROWSER_PYTHON")
    if override:
        return override, "CODE4ME_E2E_BROWSER_PYTHON"
    venv_python = BROWSER_VENV / "bin/python"
    if venv_python.is_file():
        return str(venv_python), "e2e/.browser-venv"
    return sys.executable, "harness interpreter"


_CHROMIUM_PROBE = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    browser = p.chromium.launch(headless=True)\n"
    "    browser.close()\n"
)


def _chromium_ready(python: str) -> Tuple[bool, str]:
    rc, output = _run([python, "-c", _CHROMIUM_PROBE], timeout=180)
    return rc == 0, (output.strip().splitlines() or [""])[-1][:300]


def check_browser_python(provision: bool, **_: object) -> Check:
    python, source = browser_python()
    ok, why = _chromium_ready(python)
    if ok:
        os.environ.setdefault("CODE4ME_E2E_BROWSER_PYTHON", python)
        return Check("browser_python", READY, f"Playwright Chromium launches with {python} ({source})")
    remediation = (
        f"`python3 -m venv {BROWSER_VENV} && {BROWSER_VENV}/bin/python -m pip install "
        f"playwright=={PLAYWRIGHT_VERSION} && {BROWSER_VENV}/bin/python -m playwright install chromium`"
    )
    if source == "CODE4ME_E2E_BROWSER_PYTHON":
        return Check("browser_python", BLOCKED, f"{python} cannot launch Playwright Chromium: {why}", remediation)
    if not provision:
        return Check("browser_python", MISSING,
                     f"{python} cannot launch Playwright Chromium; setup will provision {BROWSER_VENV}")
    venv_python = BROWSER_VENV / "bin/python"
    _say(f"provisioning {BROWSER_VENV} (Playwright {PLAYWRIGHT_VERSION} + Chromium)")
    steps = []
    if not venv_python.is_file():
        steps.append(([sys.executable, "-m", "venv", str(BROWSER_VENV)], "browser-venv.log", 300))
    steps += [
        ([str(venv_python), "-m", "pip", "install", f"playwright=={PLAYWRIGHT_VERSION}"],
         "browser-venv-pip.log", 900),
        ([str(venv_python), "-m", "playwright", "install", "chromium"], "browser-chromium.log", 1800),
    ]
    for command, log_name, timeout in steps:
        rc, log_path = _run_logged(command, log_name, timeout=timeout)
        if rc:
            return Check("browser_python", BLOCKED, f"`{' '.join(command[1:4])} …` exited {rc}; see {log_path}",
                         remediation)
    ok, why = _chromium_ready(str(venv_python))
    if not ok:
        return Check("browser_python", BLOCKED, f"Chromium still does not launch: {why}", remediation)
    os.environ["CODE4ME_E2E_BROWSER_PYTHON"] = str(venv_python)
    return Check("browser_python", PROVISIONED, f"created {BROWSER_VENV} with Playwright {PLAYWRIGHT_VERSION}")


# ---------------------------------------------------------------------------
# Real agents: host Goose, the plugin's vendored Codex ACP adapter
# ---------------------------------------------------------------------------


def check_goose(provision: bool, **_: object) -> Check:
    result = real_agents.probe_identity("goose")
    if result.passed and result.identity is not None:
        identity = result.identity
        return Check("goose", READY, f"{identity.path} ({identity.version or 'version unknown'}, {identity.source})")
    return Check("goose", BLOCKED, result.detail or "no goose executable found", GOOSE_INSTALL_HINT)


def vendored_codex_dir() -> Optional[Path]:
    root = _workspace()
    return root / "code4me2/dev/codex-acp-proxy/codex-acp" if root else None


def _bundled_codex_binaries(adapter: Path) -> List[Path]:
    return sorted(adapter.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))


def _adapter_build_stale(adapter: Path) -> bool:
    built = adapter / "dist/index.js"
    if not built.is_file():
        return True
    newest = max((path.stat().st_mtime for path in (adapter / "src").rglob("*") if path.is_file()), default=0)
    return newest > built.stat().st_mtime


def codex_wrapper(adapter: Path, node: str) -> Path:
    """A harness-owned ``codex-acp`` launcher for the vendored adapter build."""
    AGENT_BIN_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = AGENT_BIN_DIR / "codex-acp"
    body = ("#!/bin/sh\n"
            "# Generated by code4me_e2e prereqs: the plugin's vendored Codex ACP adapter.\n"
            f"exec \"{node}\" \"{adapter / 'dist/index.js'}\" \"$@\"\n")
    if not wrapper.is_file() or wrapper.read_text(encoding="utf-8") != body:
        wrapper.write_text(body, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def check_codex_acp(provision: bool, *, executables: Dict[str, str],
                    write_wrapper: Optional[bool] = None, **_: object) -> Check:
    """The Codex ACP executable: an explicit override, else the vendored adapter.

    ``write_wrapper`` (default: ``provision``) allows writing the harness-owned
    launcher even in a no-provisioning identity probe; ``setup --check`` never
    writes it.
    """
    write_wrapper = provision if write_wrapper is None else write_wrapper
    override = os.environ.get(real_agents.ENV_OVERRIDES["codex"])
    if override:
        result = real_agents.probe_identity("codex")
        if result.passed and result.identity is not None:
            executables["codex"] = result.identity.path
            return Check("codex_acp", READY, f"{result.identity.path} (CODE4ME_E2E_CODEX_EXECUTABLE)")
        return Check("codex_acp", BLOCKED, result.detail, "point CODE4ME_E2E_CODEX_EXECUTABLE at a codex-acp executable")
    adapter = vendored_codex_dir()
    node, npm = shutil.which("node"), shutil.which("npm")
    remediation = ("run `npm ci && npm run build` in code4me2/dev/codex-acp-proxy/codex-acp, "
                   "or set CODE4ME_E2E_CODEX_EXECUTABLE to a codex-acp executable")
    if adapter is None or not (adapter / "package.json").is_file():
        return Check("codex_acp", BLOCKED, "the vendored Codex ACP adapter was not found in the plugin checkout",
                     remediation)
    if not node or not npm:
        return Check("codex_acp", BLOCKED, "node and npm must be on PATH to run the Codex ACP adapter",
                     "install Node.js 18 or newer")
    needs_install = not (adapter / "node_modules").is_dir()
    needs_build = _adapter_build_stale(adapter)
    if needs_install or needs_build:
        if not provision:
            work = " and ".join(item for item, needed in (("npm ci", needs_install), ("npm run build", needs_build)) if needed)
            return Check("codex_acp", MISSING, f"the vendored adapter needs {work}; setup will run it")
        if needs_install:
            _say("installing the vendored Codex ACP adapter (npm ci)")
            rc, log_path = _run_logged([npm, "ci"], "codex-acp-npm-ci.log", timeout=1800, cwd=adapter)
            if rc:
                return Check("codex_acp", BLOCKED, f"`npm ci` exited {rc}; see {log_path}", remediation)
        _say("building the vendored Codex ACP adapter (npm run build)")
        rc, log_path = _run_logged([npm, "run", "build"], "codex-acp-build.log", timeout=900, cwd=adapter)
        if rc or not (adapter / "dist/index.js").is_file():
            return Check("codex_acp", BLOCKED, f"`npm run build` exited {rc}; see {log_path}", remediation)
    if not _bundled_codex_binaries(adapter):
        return Check("codex_acp", BLOCKED,
                     "the adapter's bundled native codex binary is missing (macOS XProtect can remove it)",
                     "reinstall it with `npm ci` in code4me2/dev/codex-acp-proxy/codex-acp; see its SETUP.md")
    version = json.loads((adapter / "package.json").read_text(encoding="utf-8") or "{}").get("version", "?")
    described = f"vendored adapter {version} ({adapter / 'dist/index.js'})"
    if not write_wrapper:
        existing = AGENT_BIN_DIR / "codex-acp"
        if existing.is_file() and str(adapter / "dist/index.js") in existing.read_text(encoding="utf-8"):
            executables["codex"] = str(existing)
            return Check("codex_acp", READY, f"{described} via {existing}")
        return Check("codex_acp", MISSING, f"{described}; setup will write the launcher {existing}")
    wrapper = codex_wrapper(adapter, node)
    executables["codex"] = str(wrapper)
    status = PROVISIONED if (needs_install or needs_build) else READY
    return Check("codex_acp", status, f"{described} via {wrapper}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


CHECKS: Dict[str, Callable[..., Check]] = {
    "docker": check_docker,
    "backend_image": check_backend_image,
    "stack_ports": check_stack_ports,
    "java": check_java,
    "native_python": check_native_python,
    "gui_session": check_gui_session,
    "stale_ides": check_stale_ides,
    "node": check_node,
    "website_deps": check_website_deps,
    "browser_python": check_browser_python,
    "goose": check_goose,
    "codex_acp": check_codex_acp,
}

#: A check that cannot run meaningfully while these earlier checks are unusable.
_DEPENDS_ON = {"backend_image": ("docker",), "stack_ports": ("docker",),
               "website_deps": ("node",), "codex_acp": ("node",)}


def prepare(scenario, layers: Sequence[str], *, provision: bool = True) -> Prerequisites:
    """Check (and by default provision) every prerequisite of ``layers``."""
    result = Prerequisites(tuple(layers))
    order: List[str] = []
    for layer in layers:
        for name in LAYER_REQUIREMENTS.get(layer, ()):
            if name not in order:
                order.append(name)
    for name in order:
        blockers = [dep for dep in _DEPENDS_ON.get(name, ())
                    if dep in result.checks and not result.checks[dep].usable]
        if blockers:
            result.checks[name] = Check(name, BLOCKED, "not checked: " + ", ".join(blockers) + " is not usable",
                                        result.checks[blockers[0]].remediation)
            continue
        try:
            result.checks[name] = CHECKS[name](provision, scenario=scenario, executables=result.executables)
        except Exception as error:  # noqa: BLE001 - a crashing check is a blocked prerequisite
            result.checks[name] = Check(name, BLOCKED, f"the check crashed: {error}")
    return result


def print_table(prerequisites: Prerequisites, stream=None) -> None:
    stream = stream or sys.stderr
    width = max((len(name) for name in prerequisites.checks), default=4)
    for check in prerequisites.checks.values():
        print(f"{check.id:<{width}}  {check.status:<11}  {check.detail}", file=stream)
        if check.remediation and not check.usable:
            print(f"{'':<{width}}  {'':<11}  -> {check.remediation}", file=stream)
