"""Build current runtime sources without stamping versions or release resources.

The native agent archive and the release manifest that describes it are produced
by the same module the real release pipeline uses
(``research.study.agents.participant_release native``). The harness therefore
imports a *tested* release -- manifest plus verified archive bytes -- instead of
inventing a digest, and the plugin staging overlay reuses those exact bytes.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
from typing import TYPE_CHECKING, Tuple

from . import process
from .paths import E2E_DIR, require_workspace

if TYPE_CHECKING:
    from pathlib import Path


def _fingerprint(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for base in paths:
        for path in sorted(base.rglob("*.py") if base.is_dir() else [base]):
            if "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _harness_python(server: Path) -> str:
    """The native build interpreter the prerequisite check selects.

    CODE4ME_E2E_PYTHON, else the first of the server venv and the harness's
    e2e/.venv that imports every build input.
    """
    from .prereqs import resolve_native_python

    chosen = resolve_native_python()
    if chosen is None:
        raise RuntimeError("No native build Python imports PyInstaller and the server runtime: run "
                           "`python3 -m code4me_e2e setup` or set CODE4ME_E2E_PYTHON.")
    return chosen[0]


def _platform_tag() -> Tuple[str, str]:
    os_name = "macos" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return os_name, arch


def _ensure_binary(server: Path, python: str, run_path: Path) -> Tuple[Path, str]:
    """Build (cached by source fingerprint) the native agent binary."""
    root = require_workspace()
    cache = E2E_DIR / ".cache/agent"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "dist/code4me2-agent/code4me2-agent"
    fingerprint = _fingerprint([server / "src/code4me2_agent", server / "src/research",
                               server / "packaging/code4me2-agent.spec",
                               server / "packaging/requirements-runtime.lock", server / "pyproject.toml",
                               server / "packaging/archive_runtime.py"], root)
    stamp = cache / "source.sha256"
    if not binary.is_file() or not stamp.is_file() or stamp.read_text() != fingerprint:
        rc = process.run([python, "-m", "PyInstaller", "--noconfirm",
                          "--distpath", str(cache / "dist"), "--workpath", str(cache / "build"),
                          "packaging/code4me2-agent.spec"], cwd=server,
                         log_path=run_path / "runtime-build.log")
        if rc:
            raise RuntimeError("Agent build failed; see runtime-build.log")
        stamp.write_text(fingerprint)
    return binary, fingerprint


def _produce_release(server: Path, python: str, binary: Path, fingerprint: str,
                     run_path: Path) -> Tuple[Path, Path]:
    """Run the producer so the release manifest carries real test results."""
    cache = E2E_DIR / ".cache/agent"
    release_dir = cache / "release"
    os_name, arch = _platform_tag()
    manifest_path = release_dir / f"native-{os_name}-{arch}.json"
    stamp = cache / "release.sha256"
    if not manifest_path.is_file() or not stamp.is_file() or stamp.read_text() != fingerprint:
        shutil.rmtree(release_dir, ignore_errors=True)
        # The producer refuses an existing output directory (immutable release),
        # so only its parent is ensured here.
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        # Version stamping is a release operation: reuse the version already
        # embedded in the built executable.
        version = subprocess.check_output([str(binary), "--version"], text=True,
                                          timeout=30).strip().split()[-1]
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=server,
                                         text=True).strip()
        rc = process.run([python, "-m", "research.study.agents.participant_release", "native",
                          "--skip-build", "--version", version,
                          "--platform", f"{os_name}-{arch}", "--server-commit", commit,
                          "--bundle", str(binary.parent), "--output", str(release_dir)],
                         cwd=server, log_path=run_path / "runtime-release.log")
        if rc or not manifest_path.is_file():
            raise RuntimeError("Release manifest production failed; see runtime-release.log")
        stamp.write_text(fingerprint)
    document = json.loads(manifest_path.read_text())
    return manifest_path, manifest_path.parent / document["artifacts"][0]["archive"]


def agent_release(run_path: Path) -> Tuple[Path, Path]:
    """Return the producer manifest and its declared ZIP for the release import."""
    root = require_workspace()
    server = root / "code4me2-server"
    python = _harness_python(server)
    binary, fingerprint = _ensure_binary(server, python, run_path)
    return _produce_release(server, python, binary, fingerprint, run_path)


def prepare(run_path: Path) -> list[str]:
    """Cache native builds by source content, then supply Gradle staging inputs."""
    root = require_workspace()
    server, plugin = root / "code4me2-server", root / "code4me2"
    python = _harness_python(server)
    binary, fingerprint = _ensure_binary(server, python, run_path)
    # The Gradle proxy builder has no up-to-date inputs. Cache it here using
    # both the proxy and its shared server contracts.
    cache = E2E_DIR / ".cache/agent"
    proxy_stamp = cache / "proxy.sha256"
    proxy_fingerprint = _fingerprint([plugin / "telemetry-acp-proxy/telemetry_acp_proxy",
                                      plugin / "telemetry-acp-proxy/packaging/pyinstaller_entry.py",
                                      plugin / "telemetry-acp-proxy/pyproject.toml",
                                      plugin / "build.gradle.kts", server / "src/research"], root)
    if not proxy_stamp.is_file() or proxy_stamp.read_text() != proxy_fingerprint or not (plugin / "telemetry-acp-proxy/dist").is_dir():
        rc = process.run([str(plugin / "gradlew"), "buildResearchProxyBundle",
                          f"-PresearchProxyPython={python}", "--console=plain"], cwd=plugin,
                         log_path=run_path / "proxy-build.log")
        if rc:
            raise RuntimeError("Proxy build failed; see proxy-build.log")
        proxy_stamp.write_text(proxy_fingerprint)
    # One producer run supplies both the release manifest (imported by the
    # backend steps) and the archive staged into the plugin overlay, so the two
    # consumers can never disagree about the bytes.
    release_manifest, release_archive = _produce_release(server, python, binary, fingerprint, run_path)
    release = json.loads(release_manifest.read_text())
    artifact = release["artifacts"][0]
    overlay = cache / "resources"
    resource_root = overlay / "code4me-runtime"
    resource_root.mkdir(parents=True, exist_ok=True)
    archive = resource_root / "agent.zip"
    source_digest = hashlib.sha256(release_archive.read_bytes()).hexdigest()
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != source_digest:
        shutil.copyfile(release_archive, archive)
    # The plugin overlay keeps its historical relative-archive contract; the
    # release manifest (basename, with test results) is what the server imports.
    manifest = {"manifest_version": 1, "runtime_version": release["runtime_version"],
                "managed_protocol_version": release.get("managed_protocol_version", "1"),
                "artifacts": [{"runtime_id": artifact["runtime_id"], "version": artifact["version"],
                               "platform": artifact["platform"], "architecture": artifact["architecture"],
                               "archive": "code4me-runtime/agent.zip", "sha256": source_digest,
                               "executable": artifact["executable"], "managed_protocol": "1"}]}
    manifest_path = resource_root / "manifest.json"
    serialized = json.dumps(manifest, indent=2) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text() != serialized:
        manifest_path.write_text(serialized)
    return [f"-Pcode4me.localRuntimeDir={overlay}", "--no-configuration-cache"]
