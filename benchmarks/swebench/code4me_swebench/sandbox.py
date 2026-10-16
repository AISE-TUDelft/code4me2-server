"""Network sandbox for task containers: the model API host and nothing else.

Our agent runs inside the task container, so it needs one way out (its model
API). The container joins an ``--internal`` Docker network, which has no route
to the outside, and gets HTTP(S)_PROXY pointing at the egress proxy: the only
member of both that network and the default bridge. The proxy tunnels to the
allowlisted host only, so pip, git and GitHub are unreachable, and every attempt
is logged (``docker logs code4me-swebench-proxy``).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from code4me_swebench import docker_cli

NETWORK = "code4me-swebench-internal"
PROXY = "code4me-swebench-proxy"
PROXY_IMAGE = "python:3.12-alpine"
PROXY_PORT = 3128
PROXY_SOURCE = Path(__file__).resolve().parent / "egress_proxy.py"


def model_host(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        raise ValueError(f"cannot sandbox: no host in base URL {base_url!r}")
    return host


def proxy_env() -> list[str]:
    """Exec environment for processes in a task container (also inherited by
    the agent's commands, whose env allowlist passes *_PROXY)."""
    url = f"http://{PROXY}:{PROXY_PORT}"
    pairs = {"HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "localhost,127.0.0.1"}
    return [f"{key}={value}" for name, value in pairs.items() for key in (name, name.lower())]


def ensure(base_url: str) -> str:
    """Create the internal network and an egress proxy allowing only the model
    host; reuse them when they already match. Returns the allowed host."""
    host = model_host(base_url)
    if docker_cli.docker("network", "inspect", NETWORK, check=False).returncode != 0:
        docker_cli.docker("network", "create", "--internal", NETWORK)
    current = docker_cli.docker(
        "inspect", "--format", "{{.State.Running}} {{range .Config.Env}}{{.}} {{end}}", PROXY,
        check=False,
    )
    if current.returncode == 0 and current.stdout.startswith("true") and f"ALLOW_HOSTS={host} " in current.stdout + " ":
        return host
    docker_cli.remove_container(PROXY)
    docker_cli.pull(PROXY_IMAGE, platform="linux/arm64" if _host_is_arm() else "linux/amd64")
    docker_cli.docker(
        "create", "--name", PROXY, "--network", "bridge", "--restart", "unless-stopped",
        "-e", f"ALLOW_HOSTS={host}", "-e", "ALLOW_PORTS=443", "-e", f"PROXY_PORT={PROXY_PORT}",
        PROXY_IMAGE, "python", "-u", "/egress_proxy.py",
    )
    docker_cli.copy_to(PROXY, PROXY_SOURCE, "/egress_proxy.py")
    docker_cli.docker("network", "connect", NETWORK, PROXY)
    docker_cli.docker("start", PROXY)
    return host


def _host_is_arm() -> bool:
    arch = docker_cli.docker("info", "--format", "{{.Architecture}}", check=False).stdout.strip()
    return arch in {"aarch64", "arm64"}


def denied_attempts(since: str | None = None) -> list[str]:
    """Proxy log lines of refused connections (audit trail)."""
    args = ["logs", PROXY] + (["--since", since] if since else [])
    logs = docker_cli.docker(*args, check=False)
    return [line for line in (logs.stdout + logs.stderr).splitlines() if " DENY " in line]
