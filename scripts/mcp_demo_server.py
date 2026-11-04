"""M0 protocol probe server; a separate process for each transport."""
from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("ShopRelay M0 probe", version="0.0.1")


@server.tool()
def policy_tag(query: str) -> dict:
    """Return a deterministic label for a synthetic policy query."""
    return {"tag": "returns" if "退货" in query else "other", "source": "m0-probe"}


@server.resource("shoprelay://probe/info")
def probe_info() -> str:
    """Return public synthetic probe information."""
    return "ShopRelay synthetic MCP probe resource v1"


@server.prompt()
def policy_question(topic: str) -> str:
    """Create a synthetic policy question prompt."""
    return f"请查询{topic}政策。"


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "http":
        server.run("streamable-http", host="127.0.0.1", port=8765, streamable_http_path="/mcp")
    else:
        server.run("stdio")
