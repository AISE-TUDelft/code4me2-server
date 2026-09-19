"""Build current runtime sources without stamping versions or release resources."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path

from . import process
from .paths import E2E_DIR, PLUGIN_DIR, SERVER_DIR, WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT


def _fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for root in paths:
        for path in sorted(root.rglob("*.py") if root.is_dir() else [root]):
            if "__pycache__" not in path.parts:
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare(run_path: Path) -> list[str]:
    """Cache native builds by source content, then supply Gradle staging inputs."""
    server, plugin = SERVER_DIR, PLUGIN_DIR
    python = os.environ.get("CODE4ME_E2E_PYTHON") or str(server / ".venv/bin/python")
    if not Path(python).is_file():
        raise RuntimeError("Set CODE4ME_E2E_PYTHON to Python with the server runtime and PyInstaller installed.")
    cache = E2E_DIR / ".cache/agent"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / "dist/code4me2-agent/code4me2-agent"
    fingerprint = _fingerprint([server / "src/code4me2_agent", server / "src/research",
                               server / "packaging/code4me2-agent.spec",
                               server / "packaging/requirements-runtime.lock", server / "pyproject.toml",
                               server / "packaging/archive_runtime.py"])
    stamp = cache / "source.sha256"
    if not binary.is_file() or not stamp.is_file() or stamp.read_text() != fingerprint:
        rc = process.run([python, "-m", "PyInstaller", "--noconfirm",
                          "--distpath", str(cache / "dist"), "--workpath", str(cache / "build"),
                          "packaging/code4me2-agent.spec"], cwd=server,
                         log_path=run_path / "runtime-build.log")
        if rc:
            raise RuntimeError("Agent build failed; see runtime-build.log")
        stamp.write_text(fingerprint)
    # The Gradle proxy builder has no up-to-date inputs. Cache it here using
    # both the proxy and its shared server contracts.
    proxy_stamp = cache / "proxy.sha256"
    proxy_fingerprint = _fingerprint([plugin / "telemetry-acp-proxy/telemetry_acp_proxy",
                                      plugin / "telemetry-acp-proxy/packaging/pyinstaller_entry.py",
                                      plugin / "telemetry-acp-proxy/pyproject.toml",
                                      plugin / "build.gradle.kts", server / "src/research"])
    if not proxy_stamp.is_file() or proxy_stamp.read_text() != proxy_fingerprint or not (plugin / "telemetry-acp-proxy/dist").is_dir():
        rc = process.run([str(plugin / "gradlew"), "buildResearchProxyBundle",
                          f"-PresearchProxyPython={python}", "--console=plain"], cwd=plugin,
                         log_path=run_path / "proxy-build.log")
        if rc:
            raise RuntimeError("Proxy build failed; see proxy-build.log")
        proxy_stamp.write_text(proxy_fingerprint)
    overlay = cache / "resources"
    resource_root = overlay / "code4me-runtime"
    resource_root.mkdir(parents=True, exist_ok=True)
    archive = resource_root / "agent.zip"
    archive_stamp = cache / "archive.sha256"
    if not archive.is_file() or not archive_stamp.is_file() or archive_stamp.read_text() != fingerprint:
        rc = process.run([python, "packaging/archive_runtime.py", "--root", str(binary.parent),
                          "--platform", "macos" if platform.system() == "Darwin" else "linux",
                          "--output", str(archive)], cwd=server, log_path=run_path / "runtime-archive.log")
        if rc:
            raise RuntimeError("Runtime archive failed; see runtime-archive.log")
        archive_stamp.write_text(fingerprint)
    # Version-stamping source is a release operation. The test overlay uses the
    # version already embedded in the executable and computes its own checksum.
    import subprocess
    version = subprocess.check_output([str(binary), "--version"], text=True, timeout=30).strip().split()[-1]
    os_name = "macos" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    manifest = {"manifest_version": 1, "runtime_version": version, "managed_protocol_version": "1",
                "artifacts": [{"runtime_id": "code4me-agent", "version": version, "platform": os_name,
                               "architecture": arch, "archive": "code4me-runtime/agent.zip",
                               "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                               "executable": binary.name, "managed_protocol": "1"}]}
    manifest_path = resource_root / "manifest.json"
    serialized = json.dumps(manifest, indent=2) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text() != serialized:
        manifest_path.write_text(serialized)
    return [f"-PresearchAgentDir={binary.parent}", f"-PresearchAgentBinary={binary}",
            f"-Pcode4me.localRuntimeDir={overlay}", "--no-configuration-cache"]
