from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

import app.mcp.client as mcp_client
from app.mcp import McpClientError, McpServerTrust, McpToolGrant, TrustedMcpClient
from app.mcp.client import McpToolRegistry
from app.mcp.adapters import McpProductReadRepository
from app.orchestration.observability import InMemoryObservabilitySink


def _stdio(*, grants=None, env=None):
    return McpServerTrust(server_id="trusted-local", transport="stdio",
        command="python", args=("server.py",), cwd=".", env=env or {},
        tools=tuple(grants or (McpToolGrant("lookup", "lookup", read_only=True),)))


def test_http_trust_rejects_external_plaintext_and_credentials():
    with pytest.raises(ValueError):
        McpServerTrust(server_id="remote", transport="streamable_http", tools=(), url="http://example.invalid/mcp")
    with pytest.raises(ValueError):
        McpServerTrust(server_id="remote", transport="streamable_http", tools=(),
            url="http://user:pass@127.0.0.1/mcp")
    with pytest.raises(ValueError):
        McpServerTrust(server_id="remote", transport="streamable_http", tools=(), url="http://127.0.0.1/mcp#frag")


def test_https_requires_explicit_host_allowlist():
    with pytest.raises(ValueError):
        McpServerTrust(server_id="remote", transport="streamable_http", tools=(), url="https://mcp.example/mcp")
    trust = McpServerTrust(server_id="remote", transport="streamable_http", tools=(),
        url="https://mcp.example/mcp", allowed_hosts=("mcp.example",))
    assert trust.connection() == "https://mcp.example/mcp"


def test_duplicate_names_and_aliases_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        _stdio(grants=(McpToolGrant("lookup", "same", True), McpToolGrant("update", "same", True)))
    with pytest.raises(ValueError, match="duplicate"):
        _stdio(grants=(McpToolGrant("lookup", "one", True), McpToolGrant("lookup", "two", True)))


def test_stdio_does_not_inherit_proxy_or_unapproved_process_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("API_SECRET_FOR_TEST", "do-not-forward")
    params = _stdio(env={"PYTHONPATH": "project-vendor"}).connection()
    assert params.env["PYTHONPATH"] == "project-vendor"
    assert "HTTP_PROXY" not in params.env and "API_SECRET_FOR_TEST" not in params.env


def test_local_write_policy_and_unknown_server_fail_closed(monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("untrusted call must fail before network access")
    monkeypatch.setattr(mcp_client, "Client", no_network)
    read_client = TrustedMcpClient(_stdio())
    with pytest.raises(McpClientError) as exc:
        asyncio.run(read_client.call("delete_customer_records", {"confirmation": "yes"}))
    assert exc.value.code == "MCP_TOOL_NOT_AUTHORIZED"
    write_client = TrustedMcpClient(_stdio(grants=(McpToolGrant("delete_customer_records", "delete", False),)))
    with pytest.raises(McpClientError) as exc:
        asyncio.run(write_client.call("delete", {"confirmation": "yes"}))
    assert exc.value.code == "MCP_WRITE_TOOL_BLOCKED"
    registry = McpToolRegistry(clients={"trusted-local": read_client}, aliases={
        "test/write": ("unlisted", "delete")})
    with pytest.raises(McpClientError) as exc:
        registry.resolve("test/write")
    assert exc.value.code == "MCP_SERVER_NOT_TRUSTED"
    with pytest.raises(McpClientError) as exc:
        registry.resolve("unknown")
    assert exc.value.code == "MCP_TOOL_NOT_AUTHORIZED"


def test_recovery_after_timeout_reconnects_without_retrying_tool(monkeypatch):
    instances = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.server_capabilities = SimpleNamespace(tools=object(), resources=None, prompts=None)
            self.call_attempts = 0
            instances.append(self)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def list_tools(self, **_kwargs):
            tool = SimpleNamespace(name="lookup", input_schema={"type": "object",
                "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False},
                output_schema=None)
            return SimpleNamespace(tools=[tool], next_cursor=None)
        async def call_tool(self, *_args, **_kwargs):
            self.call_attempts += 1
            if len(instances) == 1:
                raise TimeoutError("synthetic timeout")
            return SimpleNamespace(is_error=False, structured_content={"source": "synthetic"}, content=[])

    monkeypatch.setattr(mcp_client, "Client", FakeClient)
    client = TrustedMcpClient(_stdio())
    with pytest.raises(McpClientError) as exc:
        asyncio.run(client.call("lookup", {"key": "x"}))
    assert exc.value.code == "MCP_TIMEOUT"
    assert client._catalog == {}
    recovered = asyncio.run(client.call("lookup", {"key": "x"}))
    assert recovered["status"] == "COMPLETED" and recovered["untrusted"] is True
    assert client.call_count == 2  # one timed-out call plus one explicit later invocation; no SDK retry
    assert len(instances) == 2


def test_explicit_catalog_invalidation_rediscovers_changed_server_tools(monkeypatch):
    state = {"tool_name": "lookup", "discoveries": 0}

    class ChangingCatalogClient:
        server_capabilities = SimpleNamespace(tools=object(), resources=None, prompts=None)
        protocol_version = "2026-07-28"
        def __init__(self, *_args, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def list_tools(self, **_kwargs):
            state["discoveries"] += 1
            tool = SimpleNamespace(name=state["tool_name"], input_schema={"type": "object"}, output_schema=None)
            return SimpleNamespace(tools=[tool], next_cursor=None)

    monkeypatch.setattr(mcp_client, "Client", ChangingCatalogClient)
    trust = _stdio(env={})
    trust = McpServerTrust(server_id=trust.server_id, transport=trust.transport, command=trust.command,
        args=trust.args, cwd=trust.cwd, tools=trust.tools, catalog_ttl_seconds=300)
    client = TrustedMcpClient(trust)
    first = asyncio.run(client.discover())
    assert first[0]["server_tool"] == "lookup"
    state["tool_name"] = "renamed"
    cached = asyncio.run(client.discover(force=False))
    assert cached[0]["server_tool"] == "lookup" and state["discoveries"] == 1
    client.invalidate_catalog()
    refreshed = asyncio.run(client.discover(force=False))
    assert refreshed == [] and state["discoveries"] == 2


def test_result_schema_error_is_not_returned_as_business_success(monkeypatch):
    class FakeClient:
        server_capabilities = SimpleNamespace(tools=object(), resources=None, prompts=None)
        def __init__(self, *_args, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def list_tools(self, **_kwargs):
            tool = SimpleNamespace(name="lookup", input_schema={"type": "object", "properties": {},
                "additionalProperties": False}, output_schema={"type": "object", "required": ["safe"],
                "properties": {"safe": {"type": "boolean"}}, "additionalProperties": False})
            return SimpleNamespace(tools=[tool], next_cursor=None)
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(is_error=False, structured_content={"unsafe": "text"}, content=[])

    monkeypatch.setattr(mcp_client, "Client", FakeClient)
    client = TrustedMcpClient(_stdio())
    with pytest.raises(McpClientError) as exc:
        asyncio.run(client.call("lookup", {}))
    assert exc.value.code == "MCP_OUTPUT_SCHEMA_INVALID", repr(exc.value.__cause__)


def test_resource_and_prompt_allowlists_are_local_policy():
    trust = McpServerTrust(server_id="trusted", transport="stdio", command="python", args=("x",),
        tools=(), allowed_resources=("shoprelay://public",), allowed_prompts=("public_prompt",))
    client = TrustedMcpClient(trust)
    with pytest.raises(McpClientError) as exc:
        asyncio.run(client.read_resource("shoprelay://private"))
    assert exc.value.code == "MCP_RESOURCE_NOT_AUTHORIZED"
    with pytest.raises(McpClientError) as exc:
        asyncio.run(client.get_prompt("private_prompt", {}))
    assert exc.value.code == "MCP_PROMPT_NOT_AUTHORIZED"


def test_product_adapter_observability_context_is_isolated_between_concurrent_runs():
    barrier = Barrier(2)

    class ConcurrentClient:
        trust = SimpleNamespace(server_id="test-server", transport="stdio")

        def call_sync(self, _name, _arguments):
            barrier.wait(timeout=2)
            return {"data": {"source": "shoprelay_product_catalog", "availability": "AVAILABLE",
                "product_id": "P-TEST", "product": {"product_id": "P-TEST"}}}

    repo = McpProductReadRepository(ConcurrentClient())
    sinks = {run_id: InMemoryObservabilitySink() for run_id in ("run-a", "run-b")}

    def read(run_id):
        repo.set_execution_context(SimpleNamespace(sink=sinks[run_id], run_id=run_id,
            trace_id=f"trace-{run_id}", graph_version="m4.1"))
        return repo.get_product_detail("P-TEST")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read, ("run-a", "run-b")))
    assert all(item["availability"] == "AVAILABLE" for item in results)
    for run_id, sink in sinks.items():
        assert len(sink.spans) == len(sink.metrics) == len(sink.logs) == 1
        assert sink.spans[0].run_id == run_id
        assert sink.spans[0].trace_id == f"trace-{run_id}"
