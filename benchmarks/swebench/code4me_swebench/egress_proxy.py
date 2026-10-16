"""Allowlist-only HTTPS egress proxy for benchmark task containers.

Task containers sit on an internal Docker network with no route out; this proxy
is their only exit and tunnels (HTTP CONNECT) to the allowlisted hosts only, so
the agent can reach its model API but not GitHub, PyPI or anything else that
could hold the upstream fix. Every decision is logged for the audit trail.

Standalone and stdlib-only: copied into a ``python:3.12-alpine`` container.
Environment: ALLOW_HOSTS (comma-separated; a host also allows its subdomains),
ALLOW_PORTS (default 443), PROXY_PORT (default 3128).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone


def parse_hosts(value: str) -> frozenset[str]:
    return frozenset(h.strip().lower().rstrip(".") for h in value.split(",") if h.strip())


def is_allowed(host: str, port: int, *, hosts: frozenset[str], ports: frozenset[int]) -> bool:
    host = host.strip().lower().rstrip(".")
    if port not in ports or not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in hosts)


def parse_connect(request_line: str) -> tuple[str, int] | None:
    """``CONNECT host:port HTTP/1.1`` → (host, port); anything else → None."""
    parts = request_line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


def _log(decision: str, peer: str, target: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{stamp} {decision} {peer} {target}", flush=True)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _handle(client_r, client_w, *, hosts, ports) -> None:
    peer = str((client_w.get_extra_info("peername") or ("?",))[0])
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=30)
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        target = parse_connect(request_line)
        if target is None or not is_allowed(*target, hosts=hosts, ports=ports):
            _log("DENY", peer, request_line[:200])
            client_w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await client_w.drain()
            client_w.close()
            return
        upstream_r, upstream_w = await asyncio.wait_for(asyncio.open_connection(*target), timeout=30)
        _log("ALLOW", peer, f"{target[0]}:{target[1]}")
        client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await client_w.drain()
        await asyncio.gather(_pipe(client_r, upstream_w), _pipe(upstream_r, client_w))
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        client_w.close()


async def serve(host: str, port: int, *, hosts: frozenset[str], ports: frozenset[int]) -> asyncio.Server:
    return await asyncio.start_server(
        lambda r, w: _handle(r, w, hosts=hosts, ports=ports), host, port)


async def _main() -> None:
    hosts = parse_hosts(os.environ.get("ALLOW_HOSTS", ""))
    ports = frozenset(int(p) for p in os.environ.get("ALLOW_PORTS", "443").split(",") if p.strip())
    port = int(os.environ.get("PROXY_PORT", "3128"))
    if not hosts:
        sys.exit("ALLOW_HOSTS is empty: refusing to start an allow-nothing proxy by accident")
    server = await serve("0.0.0.0", port, hosts=hosts, ports=ports)
    _log("START", "-", f"allow={','.join(sorted(hosts))} ports={','.join(map(str, sorted(ports)))}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
