from __future__ import annotations

import asyncio

from code4me_swebench import egress_proxy, sandbox
from code4me_swebench.settings import RunSettings

HOSTS = egress_proxy.parse_hosts("opencode.ai")
PORTS = frozenset({443})


def test_only_the_model_host_and_its_subdomains_on_443_are_allowed():
    def allowed(host, port=443):
        return egress_proxy.is_allowed(host, port, hosts=HOSTS, ports=PORTS)

    assert allowed("opencode.ai") and allowed("api.opencode.ai") and allowed("OpenCode.AI.")
    assert not allowed("github.com") and not allowed("pypi.org") and not allowed("files.pythonhosted.org")
    assert not allowed("evil-opencode.ai") and not allowed("opencode.ai.evil.com")
    assert not allowed("opencode.ai", 80) and not allowed("")


def test_connect_lines_are_parsed_and_plain_requests_are_not():
    assert egress_proxy.parse_connect("CONNECT opencode.ai:443 HTTP/1.1") == ("opencode.ai", 443)
    assert egress_proxy.parse_connect("CONNECT [::1]:443 HTTP/1.1") == ("::1", 443)
    assert egress_proxy.parse_connect("GET http://pypi.org/simple/ HTTP/1.1") is None
    assert egress_proxy.parse_connect("CONNECT github.com HTTP/1.1") is None


def test_the_proxy_tunnels_to_allowed_targets_and_refuses_the_rest():
    async def scenario():
        async def echo(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]
        proxy = await egress_proxy.serve("127.0.0.1", 0, hosts=frozenset({"127.0.0.1"}),
                                         ports=frozenset({up_port}))
        proxy_port = proxy.sockets[0].getsockname()[1]

        async def connect(target: str) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            await writer.drain()
            status = await reader.readuntil(b"\r\n\r\n")
            if b" 200 " in status:
                writer.write(b"ping")
                await writer.drain()
                status += await reader.read(100)
            writer.close()
            return status

        allowed = await connect(f"127.0.0.1:{up_port}")
        denied_host = await connect(f"github.com:{up_port}")
        denied_port = await connect(f"127.0.0.1:{up_port + 1}")
        proxy.close()
        upstream.close()
        return allowed, denied_host, denied_port

    allowed, denied_host, denied_port = asyncio.run(scenario())
    assert b" 200 " in allowed and allowed.endswith(b"ping")
    assert b" 403 " in denied_host and b" 403 " in denied_port


def test_task_containers_get_the_proxy_in_both_spellings_and_the_policy_is_recorded():
    env = dict(item.split("=", 1) for item in sandbox.proxy_env())
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://code4me-swebench-proxy:3128"
    assert "localhost" in env["NO_PROXY"]
    assert sandbox.model_host("https://opencode.ai/zen/go/v1") == "opencode.ai"
    assert RunSettings().as_dict()["network"] == "model-api-only"
