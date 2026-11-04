"""Runtime wrapper for selected Skills over the existing Supervisor callback."""
from __future__ import annotations

import time
from typing import Any, Callable

from app.orchestration.observability import MetricRecord, ObservabilitySink, StructuredLog, TraceSpan, elapsed_ms, start_span_id, utc_now
from .registry import validate_skill_execution


def run_selected_skill(route: dict[str, Any], execute_supervisor: Callable[[], dict[str, Any]], *, query: str,
                       sink: ObservabilitySink, run_id: str, trace_id: str,
                       graph_version: str) -> dict[str, Any]:
    skill = validate_skill_execution(route, query=query)
    started_at, started, span_id = utc_now(), time.perf_counter(), start_span_id()
    status, error_code = "OK", None
    try:
        result = execute_supervisor()
        if isinstance(result, dict) and result.get("supervisor_status") == "FAILED":
            raw_error = result.get("error_code")
            if not isinstance(raw_error, str) or not raw_error.isidentifier() or len(raw_error) > 64:
                status, error_code = "ERROR", "SKILL_OUTPUT_SCHEMA_INVALID"
                return {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": error_code}
            status, error_code = "ERROR", raw_error
            return result
        budget = result.get("agent_budget") if isinstance(result, dict) else None
        if not isinstance(result, dict) or result.get("supervisor_status") not in {"COMPLETED", "STUB_ONLY", "BLOCKED"} or \
                not isinstance(result.get("handoffs"), list) or not isinstance(budget, dict) or \
                not isinstance(budget.get("agent_calls"), int) or not isinstance(budget.get("steps"), int):
            status, error_code = "ERROR", "SKILL_OUTPUT_SCHEMA_INVALID"
            return {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": error_code}
        if budget["agent_calls"] > skill.max_agent_calls or budget["steps"] > skill.max_steps:
            status, error_code = "ERROR", "SKILL_BUDGET_EXCEEDED"
            return {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": error_code}
        if result["supervisor_status"] in {"BLOCKED", "FAILED"}:
            status, error_code = "ERROR", result.get("error_code") or "SKILL_EXECUTION_FAILED"
        return result
    except Exception as exc:
        status = "ERROR"
        raw = getattr(exc, "code", None)
        error_code = raw if isinstance(raw, str) and raw.isidentifier() and len(raw) <= 64 else "SKILL_EXECUTION_FAILED"
        return {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": error_code}
    finally:
        duration = elapsed_ms(started)
        sink.record_span(TraceSpan(trace_id=trace_id, span_id=span_id, run_id=run_id,
            node_name=f"skill.{skill.skill_id}", strategy=skill.skill_id,
            start_time=started_at, end_time=utc_now(), duration_ms=duration, status=status,
            error_code=error_code, graph_version=graph_version))
        sink.record_metrics(MetricRecord(node_execution_count=1, node_error_count=int(status != "OK"),
            node_duration_ms=[duration], span_id=span_id, node_name=f"skill.{skill.skill_id}",
            graph_version=graph_version))
        sink.record_log(StructuredLog(timestamp=utc_now(), level="ERROR" if status != "OK" else "INFO",
            trace_id=trace_id, run_id=run_id, node=f"skill.{skill.skill_id}",
            event_type="skill_failed" if status != "OK" else "skill_completed",
            error_code=error_code, sanitized_message=f"skill {skill.skill_id} {status.lower()}", span_id=span_id))
