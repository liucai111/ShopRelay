"""Probe the frozen M0 MCP client against the 2025-11-25 handshake era."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import StdioServerParameters
from mcp.client import Client

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/mcp_demo_server.py"
PROTOCOL = "2025-11-25"


def safe_env() -> dict[str, str]:
    names = ("PATH", "SystemRoot", "WINDIR", "PYTHONPATH", "PYTHONIOENCODING")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PYTHONPATH"] = str(ROOT / ".vendor") + os.pathsep + str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def probe() -> dict:
    result: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "sdk": "mcp==2.2.0",
        "requested_protocol_version": PROTOCOL,
        "transport": "stdio loopback isolated ShopRelay demo server",
    }
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER), "stdio"], env=safe_env())
    started = time.perf_counter()
    try:
        # In SDK 2.2.0, legacy mode offers LATEST_HANDSHAKE_VERSION, which is
        # 2025-11-25. The API does not expose a separate protocol_version field.
        async with Client(params, mode="legacy", read_timeout_seconds=12) as client:
            result["negotiated_protocol_version"] = client.protocol_version
            result["initialize_status"] = "PASS"
            listed = await client.list_tools()
            result["tools"] = [tool.name for tool in listed.tools]
            called = await client.call_tool("policy_tag", {"query": "退货政策"})
            result["valid_tool_call"] = {
                "status": "PASS" if not called.is_error and bool(called.content) else "FAIL",
                "is_error": called.is_error,
                "content_count": len(called.content),
            }
            try:
                unknown = await client.call_tool("m0_unknown_tool", {})
                result["error_handling"] = {
                    "status": "PASS" if unknown.is_error else "FAIL",
                    "is_error": unknown.is_error,
                    "content_count": len(unknown.content),
                }
            except Exception as exc:  # sanitized: no transport headers or credentials
                result["error_handling"] = {"status": "PASS", "exception_type": type(exc).__name__}
    except Exception as exc:
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
    else:
        expected = result.get("negotiated_protocol_version") == PROTOCOL
        result["status"] = "PASS" if expected and result["valid_tool_call"]["status"] == "PASS" and result["error_handling"]["status"] == "PASS" else "FAIL"
    result["latency_ms"] = round((time.perf_counter() - started) * 1000)
    result["support_scope"] = "2025-11-25 initialize handshake tested; earlier revisions not tested"
    out_dir = ROOT / "docs/probes"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"mcp-legacy-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"record": str(out.relative_to(ROOT)), "result": result}, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    asyncio.run(probe())
