"""Injectable, in-process observability contract; no exporter or persistence."""
from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

_SECRET = re.compile(r"(?i)(api[_ -]?key|authorization|cookie|password|secret|token)(\s*[:=]\s*)[^\s,;]+")


def sanitize_message(value: str) -> str:
    return _SECRET.sub(r"\1\2[REDACTED]", value)[:240]


class TraceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    run_id: str
    node_name: str
    strategy: str | None = None
    start_time: str
    end_time: str
    duration_ms: float = Field(ge=0)
    status: str
    error_code: str | None = None
    graph_version: str = "g1"


class MetricRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_execution_count: int = 0
    node_error_count: int = 0
    node_duration_ms: list[float] = Field(default_factory=list)
    model_request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    retrieval_hop_count: int = 0
    run_status: str | None = None
    span_id: str | None = None
    node_name: str | None = None
    graph_version: str = "g1"


class StructuredLog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: str
    level: str
    trace_id: str
    run_id: str
    node: str
    event_type: str
    error_code: str | None = None
    sanitized_message: str
    span_id: str | None = None


class ObservabilitySink(Protocol):
    def record_span(self, span: TraceSpan) -> None: ...
    def record_log(self, item: StructuredLog) -> None: ...
    def record_metrics(self, item: MetricRecord) -> None: ...


class InMemoryObservabilitySink:
    """Test sink; production exporters are intentionally outside G1."""
    def __init__(self) -> None:
        self.spans: list[TraceSpan] = []
        self.logs: list[StructuredLog] = []
        self.metrics: list[MetricRecord] = []
        self._span_ids: set[str] = set()

    def record_span(self, span: TraceSpan) -> None:
        if span.span_id not in self._span_ids:
            self._span_ids.add(span.span_id)
            self.spans.append(span)

    def record_log(self, item: StructuredLog) -> None:
        self.logs.append(item)

    def record_metrics(self, item: MetricRecord) -> None:
        self.metrics.append(item)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def start_span_id() -> str:
    return uuid.uuid4().hex


def elapsed_ms(start: float) -> float:
    return max(0.0, (time.perf_counter() - start) * 1000)
