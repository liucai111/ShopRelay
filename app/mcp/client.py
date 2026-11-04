"""Policy-enforced MCP client using the project-pinned official Python SDK.

Discovery metadata is untrusted. Every callable is authorized by local grants,
and write-capable tools are denied regardless of server annotations.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from jsonschema import Draft202012Validator
from mcp import Client, StdioServerParameters


class McpClientError(RuntimeError):
    def __init__(self, code: str, message: str = "MCP operation failed") -> None:
        super().__init__(message)
        self.code = code


def _nested_mcp_error(exc: BaseException) -> "McpClientError | None":
    if isinstance(exc, McpClientError):
        return exc
    nested = getattr(exc, "exceptions", ())
    for item in nested:
        found = _nested_mcp_error(item)
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class McpToolGrant:
    server_tool: str
    local_name: str
    read_only: bool
    enabled: bool = True


@dataclass(frozen=True)
class McpServerTrust:
    server_id: str
    transport: Literal["stdio", "streamable_http"]
    tools: tuple[McpToolGrant, ...]
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = None
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    allowed_resources: tuple[str, ...] = ()
    allowed_prompts: tuple[str, ...] = ()
    timeout_seconds: float = 8.0
    catalog_ttl_seconds: float = 5.0
    max_pages: int = 20
    max_result_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not self.server_id or len(self.server_id) > 64:
            raise ValueError("invalid MCP server id")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("MCP timeout must be between 0 and 60 seconds")
        if not 0 <= self.catalog_ttl_seconds <= 300:
            raise ValueError("invalid MCP catalog TTL")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("invalid MCP page limit")
        names = [grant.local_name for grant in self.tools]
        remote_names = [grant.server_tool for grant in self.tools]
        if len(names) != len(set(names)) or len(remote_names) != len(set(remote_names)):
            raise ValueError("duplicate local alias or server tool grant")
        if self.transport == "stdio":
            if not self.command or self.url:
                raise ValueError("stdio server requires command and forbids URL")
        else:
            parsed = urlparse(self.url or "")
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname not in self.allowed_hosts \
               or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Streamable HTTP must use an explicitly trusted host")
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("unencrypted Streamable HTTP is restricted to loopback")
            if self.command:
                raise ValueError("HTTP server does not accept a command")

    def connection(self):
        if self.transport == "stdio":
            safe = {key: value for key, value in os.environ.items()
                    if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            safe.update(self.env)
            return StdioServerParameters(command=self.command or "", args=list(self.args),
                env=safe, cwd=self.cwd, encoding="utf-8")
        return self.url


def _run(awaitable):
    """Run SDK coroutine from synchronous Agent callbacks, which run in worker threads."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise McpClientError("MCP_SYNC_IN_ASYNC_CONTEXT", "use async methods from an async context")


class TrustedMcpClient:
    def __init__(self, trust: McpServerTrust) -> None:
        self.trust = trust
        self._catalog: dict[str, Any] = {}
        self._catalog_loaded_at = 0.0
        self.discovery_count = 0
        self.call_count = 0
        self.last_duration_ms = 0.0
        self.last_protocol_version: str | None = None

    def invalidate_catalog(self) -> None:
        self._catalog.clear()
        self._catalog_loaded_at = 0.0

    async def _discover(self, client: Client, *, force: bool = False) -> dict[str, Any]:
        if self._catalog and not force and time.monotonic() - self._catalog_loaded_at < self.trust.catalog_ttl_seconds:
            return self._catalog
        if getattr(client.server_capabilities, "tools", None) is None:
            raise McpClientError("MCP_TOOLS_UNSUPPORTED")
        tools: dict[str, Any] = {}
        cursor = None
        for _ in range(self.trust.max_pages):
            page = await asyncio.wait_for(client.list_tools(cursor=cursor), self.trust.timeout_seconds)
            for tool in page.tools:
                if tool.name in tools:
                    raise McpClientError("MCP_DUPLICATE_REMOTE_TOOL")
                tools[tool.name] = tool
            cursor = page.next_cursor
            if not cursor:
                break
        else:
            if cursor:
                raise McpClientError("MCP_PAGE_LIMIT_EXCEEDED")
        self.discovery_count += 1
        self._catalog = tools
        self._catalog_loaded_at = time.monotonic()
        return tools

    async def discover(self, *, force: bool = True) -> list[dict[str, Any]]:
        async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
            tools = await self._discover(client, force=force)
            self.last_protocol_version = str(client.protocol_version)
        grants = {item.server_tool: item for item in self.trust.tools if item.enabled and item.read_only}
        rows = []
        for name, tool in tools.items():
            grant = grants.get(name)
            if grant:
                rows.append({"server_id": self.trust.server_id, "tool_id": f"{self.trust.server_id}/{name}",
                    "server_tool": name, "local_name": grant.local_name,
                    "input_schema": tool.input_schema, "output_schema": getattr(tool, "output_schema", None)})
        return rows

    async def call(self, local_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        grant = next((g for g in self.trust.tools if g.local_name == local_name and g.enabled), None)
        if grant is None:
            raise McpClientError("MCP_TOOL_NOT_AUTHORIZED")
        if not grant.read_only:
            raise McpClientError("MCP_WRITE_TOOL_BLOCKED")
        started = time.perf_counter()
        try:
            failure_code = None
            structured = None
            async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
                catalog = await self._discover(client)
                tool = catalog.get(grant.server_tool)
                if tool is None:
                    self.invalidate_catalog()
                    catalog = await self._discover(client, force=True)
                    tool = catalog.get(grant.server_tool)
                    if tool is None:
                        failure_code = "MCP_TOOL_NOT_FOUND"
                if failure_code is None:
                    try:
                        Draft202012Validator.check_schema(tool.input_schema)
                        Draft202012Validator(tool.input_schema).validate(arguments)
                    except Exception:
                        failure_code = "MCP_ARGUMENT_SCHEMA_INVALID"
                if failure_code is None:
                    self.call_count += 1
                    result = await asyncio.wait_for(client.call_tool(grant.server_tool, arguments),
                                                    self.trust.timeout_seconds)
                    if result.is_error:
                        failure_code = "MCP_REMOTE_TOOL_ERROR"
                    else:
                        structured = result.structured_content
                        if structured is None:
                            text_parts = [getattr(part, "text", "") for part in result.content
                                          if getattr(part, "type", None) == "text"]
                            if len(text_parts) != 1:
                                failure_code = "MCP_RESULT_NOT_STRUCTURED"
                            else:
                                try:
                                    structured = json.loads(text_parts[0])
                                except (ValueError, TypeError):
                                    failure_code = "MCP_RESULT_NOT_JSON"
                        if failure_code is None:
                            encoded = json.dumps(structured, ensure_ascii=False, default=str).encode("utf-8")
                            if len(encoded) > self.trust.max_result_bytes:
                                failure_code = "MCP_RESULT_TOO_LARGE"
                        output_schema = getattr(tool, "output_schema", None)
                        if failure_code is None and output_schema:
                            try:
                                Draft202012Validator(output_schema).validate(structured)
                            except Exception:
                                failure_code = "MCP_OUTPUT_SCHEMA_INVALID"
                        if failure_code is None and not isinstance(structured, dict):
                            failure_code = "MCP_RESULT_NOT_OBJECT"
            if failure_code:
                raise McpClientError(failure_code)
            # MCP output remains untrusted data, even when schema-valid.
            return {"server_id": self.trust.server_id, "tool_id": f"{self.trust.server_id}/{grant.server_tool}",
                "tool_name": grant.local_name, "status": "COMPLETED", "untrusted": True,
                "data": structured}
        except McpClientError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self.invalidate_catalog()
            raise McpClientError("MCP_TIMEOUT") from exc
        except Exception as exc:
            nested = _nested_mcp_error(exc)
            if nested is not None:
                raise nested
            self.invalidate_catalog()
            raise McpClientError("MCP_CONNECTION_OR_PROTOCOL_ERROR", type(exc).__name__) from exc
        finally:
            self.last_duration_ms = round((time.perf_counter() - started) * 1000, 3)

    def call_sync(self, local_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _run(self.call(local_name, arguments))

    async def list_resources(self) -> list[dict[str, str]]:
        async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
            if getattr(client.server_capabilities, "resources", None) is None:
                return []
            result, cursor, rows = None, None, []
            for _ in range(self.trust.max_pages):
                result = await asyncio.wait_for(client.list_resources(cursor=cursor), self.trust.timeout_seconds)
                rows.extend({"name": item.name, "uri": str(item.uri)} for item in result.resources)
                cursor = result.next_cursor
                if not cursor:
                    return [row for row in rows if row["uri"] in self.trust.allowed_resources]
            raise McpClientError("MCP_PAGE_LIMIT_EXCEEDED")

    async def read_resource(self, uri: str) -> list[str]:
        if uri not in self.trust.allowed_resources:
            raise McpClientError("MCP_RESOURCE_NOT_AUTHORIZED")
        async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
            if getattr(client.server_capabilities, "resources", None) is None:
                raise McpClientError("MCP_RESOURCES_UNSUPPORTED")
            result = await asyncio.wait_for(client.read_resource(uri), self.trust.timeout_seconds)
            return [getattr(item, "text", "") for item in result.contents]

    async def list_prompts(self) -> list[str]:
        async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
            if getattr(client.server_capabilities, "prompts", None) is None:
                return []
            names, cursor = [], None
            for _ in range(self.trust.max_pages):
                page = await asyncio.wait_for(client.list_prompts(cursor=cursor), self.trust.timeout_seconds)
                names.extend(item.name for item in page.prompts)
                cursor = page.next_cursor
                if not cursor:
                    return [name for name in names if name in self.trust.allowed_prompts]
            raise McpClientError("MCP_PAGE_LIMIT_EXCEEDED")

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> list[str]:
        if name not in self.trust.allowed_prompts:
            raise McpClientError("MCP_PROMPT_NOT_AUTHORIZED")
        async with Client(self.trust.connection(), read_timeout_seconds=self.trust.timeout_seconds) as client:
            if getattr(client.server_capabilities, "prompts", None) is None:
                raise McpClientError("MCP_PROMPTS_UNSUPPORTED")
            result = await asyncio.wait_for(client.get_prompt(name, arguments), self.trust.timeout_seconds)
            return [getattr(item.content, "text", "") for item in result.messages]


@dataclass
class McpToolRegistry:
    """Configured servers only; globally stable names prevent accidental collisions."""
    clients: dict[str, TrustedMcpClient]
    aliases: dict[str, tuple[str, str]]

    def resolve(self, tool_id: str) -> tuple[TrustedMcpClient, str]:
        target = self.aliases.get(tool_id)
        if target is None:
            raise McpClientError("MCP_TOOL_NOT_AUTHORIZED")
        server_id, local_name = target
        client = self.clients.get(server_id)
        if client is None:
            raise McpClientError("MCP_SERVER_NOT_TRUSTED")
        return client, local_name

    def call_sync(self, tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client, local_name = self.resolve(tool_id)
        return client.call_sync(local_name, arguments)
