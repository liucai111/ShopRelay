"""Adapters from MCP to ShopRelay's existing synchronous read-tool protocol."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from app.m3.order_tools import ToolExecutionError
from app.orchestration.observability import (MetricRecord, ObservabilitySink, StructuredLog,
    TraceSpan, elapsed_ms, start_span_id, utc_now)

from .client import McpClientError, TrustedMcpClient


class McpProductReadRepository:
    """ProductAgent-compatible reader that delegates only detail reads via MCP.

    The MCP server uses the same ProductReadRepository implementation; this
    adapter never owns a second product store or SQL implementation.
    """
    def __init__(self, client: TrustedMcpClient, *, sink: ObservabilitySink | None = None,
                 run_id: str = "mcp-unbound", trace_id: str = "mcp-unbound",
                 graph_version: str = "m4.1") -> None:
        self.client = client
        self.sink = sink
        self.run_id = run_id
        self.trace_id = trace_id
        self.graph_version = graph_version
        self._execution_context: ContextVar[tuple[ObservabilitySink, str, str, str] | None] = ContextVar(
            f"mcp_product_repo_context_{id(self)}", default=None)

    def set_execution_context(self, context: Any) -> None:
        """Bind only the existing per-call observability context, never identity."""
        if not all(hasattr(context, key) for key in ("sink", "run_id", "trace_id", "graph_version")):
            raise McpClientError("MCP_EXECUTION_CONTEXT_INVALID")
        self._execution_context.set((context.sink, context.run_id, context.trace_id, context.graph_version))

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        started_at = utc_now()
        import time
        started = time.perf_counter()
        span_id = start_span_id()
        execution_context = self._execution_context.get()
        sink, run_id, trace_id, graph_version = execution_context or (
            self.sink, self.run_id, self.trace_id, self.graph_version)
        status, error_code = "OK", None
        node_name = f"mcp.client.{self.client.trust.server_id}.{self.client.trust.transport}.get_product_detail"
        try:
            result = self.client.call_sync("product_detail", {"product_id": product_id})
            payload = result["data"]
            if payload.get("source") != "shoprelay_product_catalog" or \
               payload.get("availability") not in {"AVAILABLE", "NOT_FOUND"}:
                raise McpClientError("MCP_BUSINESS_RESULT_INVALID")
            return payload
        except McpClientError as exc:
            status, error_code = "ERROR", exc.code
            raise ToolExecutionError(exc.code) from exc
        finally:
            duration = elapsed_ms(started)
            if sink is not None:
                sink.record_span(TraceSpan(trace_id=trace_id, span_id=span_id,
                    run_id=run_id, node_name=node_name,
                    strategy="MCP", start_time=started_at, end_time=utc_now(), duration_ms=duration,
                    status=status, error_code=error_code, graph_version=graph_version))
                sink.record_metrics(MetricRecord(node_execution_count=1,
                    node_error_count=int(status != "OK"), node_duration_ms=[duration],
                    span_id=span_id, node_name=node_name, graph_version=graph_version))
                sink.record_log(StructuredLog(timestamp=utc_now(), level="ERROR" if error_code else "INFO",
                    trace_id=trace_id, run_id=run_id, node=node_name,
                    event_type="mcp_tool_failed" if error_code else "mcp_tool_completed",
                    error_code=error_code, sanitized_message="MCP read failed" if error_code else "MCP read completed",
                    span_id=span_id))


class McpCouponReadRepository:
    """PolicyAgent-compatible adapter for the locally granted M0 coupon catalog read."""
    def __init__(self, client: TrustedMcpClient, *, sink: ObservabilitySink | None = None,
                 run_id: str = "mcp-unbound", trace_id: str = "mcp-unbound",
                 graph_version: str = "m4.3-a") -> None:
        self.client, self.sink = client, sink
        self.run_id, self.trace_id, self.graph_version = run_id, trace_id, graph_version
        self._execution_context: ContextVar[tuple[ObservabilitySink, str, str, str] | None] = ContextVar(
            f"mcp_coupon_repo_context_{id(self)}", default=None)

    def set_execution_context(self, context: Any) -> None:
        if not all(hasattr(context, key) for key in ("sink", "run_id", "trace_id", "graph_version")):
            raise McpClientError("MCP_EXECUTION_CONTEXT_INVALID")
        self._execution_context.set((context.sink, context.run_id, context.trace_id, context.graph_version))

    def get_coupon(self, coupon_code: str) -> dict[str, Any]:
        import time
        started_at, started, span_id = utc_now(), time.perf_counter(), start_span_id()
        bound = self._execution_context.get()
        sink, run_id, trace_id, graph_version = bound or (self.sink, self.run_id, self.trace_id, self.graph_version)
        node = f"mcp.client.{self.client.trust.server_id}.{self.client.trust.transport}.get_coupon"
        status, error_code = "OK", None
        try:
            result = self.client.call_sync("coupon_catalog", {"coupon_code": coupon_code})
            payload = result.get("data")
            if not isinstance(payload, dict) or payload.get("source") != "shoprelay_coupon_catalog" or \
                    payload.get("availability") not in {"AVAILABLE", "NOT_FOUND", "NOT_AVAILABLE"}:
                raise McpClientError("MCP_BUSINESS_RESULT_INVALID")
            if payload.get("availability") == "AVAILABLE" and payload.get("eligibility_status") != "UNKNOWN":
                raise McpClientError("MCP_COUPON_ELIGIBILITY_UNVERIFIED")
            return payload
        except McpClientError as exc:
            status, error_code = "ERROR", exc.code
            raise ToolExecutionError(exc.code) from exc
        finally:
            duration = elapsed_ms(started)
            if sink is not None:
                sink.record_span(TraceSpan(trace_id=trace_id, span_id=span_id, run_id=run_id,
                    node_name=node, strategy="MCP", start_time=started_at, end_time=utc_now(),
                    duration_ms=duration, status=status, error_code=error_code, graph_version=graph_version))
                sink.record_metrics(MetricRecord(node_execution_count=1,
                    node_error_count=int(status != "OK"), node_duration_ms=[duration], span_id=span_id,
                    node_name=node, graph_version=graph_version))
                sink.record_log(StructuredLog(timestamp=utc_now(), level="ERROR" if error_code else "INFO",
                    trace_id=trace_id, run_id=run_id, node=node,
                    event_type="mcp_tool_failed" if error_code else "mcp_tool_completed",
                    error_code=error_code, sanitized_message="MCP coupon read failed" if error_code
                    else "MCP coupon read completed", span_id=span_id))
