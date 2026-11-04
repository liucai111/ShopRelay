"""Launch two independent MCP services and verify v2 client discovery/call."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/mcp_demo_server.py"


def safe_env() -> dict[str, str]:
    names = ("PATH", "SystemRoot", "WINDIR", "PYTHONPATH", "PYTHONIOENCODING")
    result = {name: os.environ[name] for name in names if name in os.environ}
    result["PYTHONPATH"] = str(ROOT / ".vendor") + os.pathsep + str(ROOT)
    result["PYTHONIOENCODING"] = "utf-8"
    return result


async def inspect_client(server) -> dict:
    async with Client(server, read_timeout_seconds=8) as client:
        tools = await client.list_tools()
        tool_names = [tool.name for tool in tools.tools]
        called = await client.call_tool("policy_tag", {"query": "退货政策"})
        resources = await client.list_resources()
        prompts = await client.list_prompts()
        resource = await client.read_resource("shoprelay://probe/info")
        prompt = await client.get_prompt("policy_question", {"topic": "退货"})
        return {"protocol_version": client.protocol_version, "tools": tool_names, "tool_call_is_error": called.is_error, "tool_call_has_content": bool(called.content), "resources": [r.uri for r in resources.resources], "resource_read_has_content": bool(resource.contents), "prompts": [p.name for p in prompts.prompts], "prompt_get_has_messages": bool(prompt.messages), "capabilities": {"tools": client.server_capabilities.tools is not None, "resources": client.server_capabilities.resources is not None, "prompts": client.server_capabilities.prompts is not None}}


async def main() -> None:
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "sdk": "mcp==2.2.0"}
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER), "stdio"], env=safe_env())
    try:
        result["stdio"] = {"status": "ok", **await inspect_client(params)}
    except Exception as exc:
        result["stdio"] = {"status": "failed", "error_type": type(exc).__name__}
    kwargs = {"cwd": str(ROOT), "env": safe_env(), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen([sys.executable, str(SERVER), "http"], **kwargs)
    try:
        ready = False
        for _ in range(40):
            with socket.socket() as sock:
                sock.settimeout(0.3)
                if sock.connect_ex(("127.0.0.1", 8765)) == 0:
                    ready = True
                    break
            time.sleep(0.25)
        if not ready:
            result["http"] = {"status": "failed", "error_type": "ServerStartTimeout", "exit_code": process.poll()}
        else:
            try:
                result["http"] = {"status": "ok", **await inspect_client("http://127.0.0.1:8765/mcp")}
            except Exception as exc:
                result["http"] = {"status": "failed", "error_type": type(exc).__name__}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    out = ROOT / "docs/probes" / f"mcp-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with out.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"path": str(out), "result": result}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
