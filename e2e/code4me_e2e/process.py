"""Bounded subprocesses; only terminate process groups created by this run."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def stop(process: subprocess.Popen, timeout: int = 15) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    # A Gradle launcher may exit before its IDE child. Kill any remaining
    # members of the private session even when the launcher already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=timeout)


def run(command: list[str], *, cwd: Path, log_path: Path,
        env: dict | None = None, timeout: int = 1200) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("# " + " ".join(command) + "\n")
        log.flush()
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return child.wait(timeout=timeout)
        finally:
            stop(child)
