"""Thin wrappers over the ``docker`` CLI (the same on macOS/Colima and Linux)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class DockerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def docker(*args: str, check: bool = True, timeout: float | None = None,
           env: dict[str, str] | None = None) -> Completed:
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, errors="replace",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        if check:
            raise DockerError(f"docker {' '.join(args[:3])} timed out after {timeout} s") from None
        return Completed(-1, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    result = Completed(proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args[:3])} failed: {proc.stderr.strip()[-2000:]}")
    return result


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def image_present(image: str) -> bool:
    return docker("image", "inspect", image, check=False).returncode == 0


def pull(image: str, *, platform: str) -> None:
    if not image_present(image):
        docker("pull", "--platform", platform, "--quiet", image)


def volume_exists(name: str) -> bool:
    return docker("volume", "inspect", name, check=False).returncode == 0


def start_container(*, name: str, image: str, platform: str, mounts: list[str],
                    memory: str | None = None, cpus: str | None = None,
                    network: str | None = None) -> None:
    docker("rm", "-f", name, check=False)
    args = ["run", "-d", "--name", name, "--platform", platform, "--entrypoint", ""]
    if network:
        args += ["--network", network]
    for mount in mounts:
        args += ["-v", mount]
    if memory:
        args += ["--memory", memory]
    if cpus:
        args += ["--cpus", cpus]
    docker(*args, image, "sleep", "infinity")


def exec_in(container: str, *argv: str, workdir: str | None = None,
            env: list[str] | None = None, timeout: float | None = None,
            check: bool = False, process_env: dict[str, str] | None = None) -> Completed:
    args = ["exec"]
    if workdir:
        args += ["-w", workdir]
    for item in env or []:
        args += ["-e", item]
    return docker(*args, container, *argv, check=check, timeout=timeout, env=process_env)


def copy_to(container: str, source: Path, destination: str) -> None:
    docker("cp", str(source), f"{container}:{destination}")


def copy_from(container: str, source: str, destination: Path) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    return docker("cp", f"{container}:{source}", str(destination), check=False).returncode == 0


def remove_container(name: str) -> None:
    docker("rm", "-f", name, check=False)


def remove_image(image: str) -> None:
    docker("image", "rm", image, check=False)


def container_ip(name: str, network: str) -> str:
    template = '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % network
    return docker("inspect", "--format", template, name, check=False).stdout.strip()
