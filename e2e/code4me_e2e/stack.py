"""Lifecycle and DB helpers for the disposable e2e docker-compose stack.

All docker interaction goes through ``docker compose -p <project> -f <file>``.
The project name is always the scenario's (default ``code4me-e2e``), so this
module can never touch the developer stack's containers or volumes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from functools import lru_cache
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Scenario
from .paths import E2E_DIR

COMPOSE_FILE = E2E_DIR / "docker-compose.e2e.yml"


class StackError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def compose_command() -> List[str]:
    """Support Docker Desktop and Homebrew's standalone Compose installation."""
    for command in (["docker", "compose"], ["docker-compose"]):
        if shutil.which(command[0]):
            result = subprocess.run(command + ["version"], capture_output=True, timeout=30)
            if result.returncode == 0:
                return command
    raise StackError("Docker Compose is required (docker compose or docker-compose).")


def compose_env(scenario: Scenario) -> Dict[str, str]:
    """Interpolation variables passed to every ``docker compose`` invocation."""
    backend_image = scenario.stack.image
    if backend_image == "code4me2-server-backend:latest":
        # The tag is convenient for local builds but moves on every rebuild.
        # Resolve it once per compose invocation so this E2E run uses immutable
        # bytes even if another terminal retags latest while the suite runs.
        inspected = subprocess.run(
            ["docker", "image", "inspect", backend_image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        image_id = inspected.stdout.strip()
        if inspected.returncode == 0 and image_id.startswith("sha256:"):
            backend_image = image_id
    return {
        "E2E_BACKEND_IMAGE": backend_image,
        "E2E_BACKEND_PORT": str(scenario.stack.backend_port),
        "E2E_DB_PORT": str(scenario.stack.db_port),
        "E2E_REDIS_PORT": str(scenario.stack.redis_port),
        "E2E_DB_PASSWORD": scenario.stack.db_password,
        "E2E_DB_NAME": scenario.stack.db_name,
        "E2E_DB_USER": scenario.stack.db_user,
        "E2E_BOOTSTRAP_SIGNING_SECRET": scenario.stack.bootstrap_signing_secret,
        "E2E_STUB_API_KEY": scenario.stack.stub_secret_value,
    }


def compose(
    scenario: Scenario,
    args: List[str],
    *,
    check: bool = False,
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    """Run one ``docker compose`` command for the scenario's project."""
    command = [
        *compose_command(),
        "-p",
        scenario.stack.project_name,
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]
    env = {**os.environ, **compose_env(scenario)}
    result = subprocess.run(
        command,
        cwd=str(E2E_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise StackError(
            "docker compose " + " ".join(args) + " failed:\n"
            + (result.stderr or result.stdout)
        )
    return result


def up(scenario: Scenario) -> str:
    """Bring the stack up and wait until the backend schema is ready."""
    compose(scenario, ["up", "-d", "db", "redis"], check=True, timeout=900)
    # A bind mount does not reload Python. Recreate just our backend so every
    # invocation imports current source and applies new migrations.
    result = compose(scenario, ["up", "-d", "--force-recreate", "backend"], check=True, timeout=900)
    ready = wait_until_ready(scenario)
    return result.stdout + ("\n" + ready if ready else "")


def down(scenario: Scenario) -> str:
    """Remove only this project's containers and its named volume."""
    result = compose(scenario, ["down", "-v", "--remove-orphans"], timeout=300)
    if result.returncode != 0:
        raise StackError(result.stderr or result.stdout)
    return result.stdout + result.stderr


def ps(scenario: Scenario) -> str:
    result = compose(scenario, ["ps", "--format", "json"], timeout=120)
    return result.stdout or result.stderr


def is_up(scenario: Scenario) -> bool:
    result = compose(scenario, ["ps", "-q"], timeout=120)
    return bool(result.stdout.strip())


def backend_logs(scenario: Scenario, tail: int = 200) -> str:
    result = compose(scenario, ["logs", "--tail", str(tail), "backend"], timeout=180)
    return (result.stdout or "") + (result.stderr or "")


def psql(scenario: Scenario, sql: str, timeout: int = 60) -> str:
    """Run one SQL statement in the disposable db service (trusted config only)."""
    result = compose(
        scenario,
        [
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            scenario.stack.db_user,
            "-d",
            scenario.stack.db_name,
            "-t",
            "-A",
            "-c",
            sql,
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise StackError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def try_psql(scenario: Scenario, sql: str, timeout: int = 30) -> Tuple[bool, str]:
    try:
        return True, psql(scenario, sql, timeout=timeout)
    except (StackError, subprocess.SubprocessError) as error:
        return False, str(error)


def ping(scenario: Scenario, timeout: float = 3.0) -> bool:
    """HEAD /api/ping with no auth; True when the backend answers 200."""
    url = scenario.base_url + "/api/ping"
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def capabilities(scenario: Scenario, timeout: float = 5.0) -> Tuple[Optional[int], Any]:
    """GET /api/acp/capabilities; returns ``(status, parsed_or_raw)``."""
    url = scenario.base_url + "/api/acp/capabilities"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, None


def wait_until_ready(scenario: Scenario, timeout: Optional[int] = None) -> str:
    """Wait for liveness, then for ``schema_ready`` on /api/acp/capabilities."""
    deadline = time.monotonic() + (
        timeout if timeout is not None else scenario.timeouts.backend_ready_seconds
    )
    last: str = "no response"
    while time.monotonic() < deadline:
        status, payload = capabilities(scenario)
        if status == 200 and isinstance(payload, dict) and payload.get("schema_ready"):
            return (
                f"backend ready: schema_revision={payload.get('schema_revision')} "
                f"expected={payload.get('expected_schema_revision')}"
            )
        if status == 200:
            last = f"HTTP 200 but schema_ready=false: {payload}"
        elif status is not None:
            last = f"HTTP {status}: {payload}"
        time.sleep(3)
    raise StackError(
        f"backend did not become schema-ready within "
        f"{timeout or scenario.timeouts.backend_ready_seconds}s (last: {last})"
    )


def existing_containers() -> str:
    """Names/ports of every running container (to prove the dev stack is safe)."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()
