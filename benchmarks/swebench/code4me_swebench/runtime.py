"""Builds the agent runtime that is mounted read-only into every task container.

The packaged native runtime (PyInstaller, built on Ubuntu 24.04) needs a newer
glibc than the SWE-bench images (Ubuntu 22.04), so the agent runs from its
wheel on a portable python-build-standalone interpreter instead, with the same
locked dependencies as the release (``packaging/requirements-runtime.lock``).

The volume name is derived from the wheel, the lock and the platform, so an
agent change produces a new runtime instead of silently reusing a stale one.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from code4me_swebench import docker_cli
from code4me_swebench.settings import RUNTIME_MOUNT

SERVER_ROOT = Path(__file__).resolve().parents[3]
LOCK_FILE = SERVER_ROOT / "packaging" / "requirements-runtime.lock"
BUILDER_IMAGE = "ghcr.io/astral-sh/uv:0.9-bookworm-slim"
PYTHON_VERSION = "3.13"


@dataclass(frozen=True)
class Runtime:
    volume: str
    wheel_name: str
    # Content hash (member names and bytes), stable across rebuilds.
    wheel_sha256: str
    lock_sha256: str
    git_commit: str
    git_dirty: bool
    platform: str

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wheel_content_sha256(wheel: Path) -> str:
    """Hash of the wheel's member names and bytes, ignoring zip timestamps."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(archive.namelist()):
            digest.update(name.encode() + b"\0" + archive.read(name) + b"\0")
    return digest.hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(SERVER_ROOT), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def build_wheel(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--wheel", "--quiet", "--out-dir", str(out_dir), str(SERVER_ROOT)],
        check=True,
    )
    wheels = sorted(out_dir.glob("code4me2_agent-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one agent wheel in {out_dir}, found {wheels}")
    return wheels[0]


def ensure_runtime(*, platform: str, rebuild: bool = False) -> Runtime:
    """Build (or reuse) the runtime volume for the agent in this checkout."""
    with tempfile.TemporaryDirectory(prefix="code4me-swebench-") as scratch:
        scratch_dir = Path(scratch)
        wheel = build_wheel(scratch_dir / "wheels")
        wheel_sha = _wheel_content_sha256(wheel)
        lock_sha = _sha256(LOCK_FILE)
        key = hashlib.sha256(f"{wheel_sha}:{lock_sha}:{platform}:{PYTHON_VERSION}".encode())
        arch = platform.split("/")[-1]
        volume = f"code4me-swebench-runtime-{arch}-{key.hexdigest()[:12]}"
        runtime = Runtime(
            volume=volume,
            wheel_name=wheel.name,
            wheel_sha256=wheel_sha,
            lock_sha256=lock_sha,
            git_commit=_git("rev-parse", "HEAD"),
            git_dirty=bool(_git("status", "--porcelain", "--", "src/code4me2_agent", "pyproject.toml")),
            platform=platform,
        )
        if docker_cli.volume_exists(volume) and not rebuild:
            return runtime
        docker_cli.docker("volume", "rm", "-f", volume, check=False)
        docker_cli.docker("volume", "create", volume)
        # Inputs travel by `docker cp`, not a bind mount: Colima shares only
        # $HOME with its VM, so a macOS temp directory would mount empty.
        inputs = scratch_dir / "inputs"
        inputs.mkdir()
        shutil.copy2(wheel, inputs / wheel.name)
        shutil.copy2(LOCK_FILE, inputs / "requirements-runtime.lock")
        venv_python = f"{RUNTIME_MOUNT}/venv/bin/python"
        script = " && ".join([
            f"UV_PYTHON_INSTALL_DIR={RUNTIME_MOUNT}/python uv python install {PYTHON_VERSION}",
            f"UV_PYTHON_INSTALL_DIR={RUNTIME_MOUNT}/python uv venv {RUNTIME_MOUNT}/venv "
            f"--python {PYTHON_VERSION} --python-preference only-managed",
            f"uv pip install --python {venv_python} --compile-bytecode "
            "-r /inputs/requirements-runtime.lock",
            f"uv pip install --python {venv_python} --compile-bytecode --no-deps /inputs/{wheel.name}",
            f"{venv_python} -c 'import code4me2_agent.echo, acp, openai, mcp'",
        ])
        builder = f"{volume}-builder"
        docker_cli.remove_container(builder)
        try:
            docker_cli.docker(
                "create", "--name", builder, "--platform", platform,
                "-v", f"{volume}:{RUNTIME_MOUNT}", BUILDER_IMAGE, "sh", "-c", script,
            )
            docker_cli.copy_to(builder, inputs, "/inputs")
            docker_cli.docker("start", "-a", builder, timeout=1_800)
        except BaseException:
            docker_cli.docker("volume", "rm", "-f", volume, check=False)
            raise
        finally:
            docker_cli.remove_container(builder)
        return runtime
