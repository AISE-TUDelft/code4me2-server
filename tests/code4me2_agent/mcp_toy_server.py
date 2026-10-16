"""Minimal stdio MCP server used to prove the broker attach path.

Exposes a single ``toy_add`` tool. Run as a subprocess; speaks MCP over
stdio, so nothing is printed here.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="toy")


@server.tool()
def toy_add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
