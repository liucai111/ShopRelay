"""Bounded, privacy-filtered projections for the administrator observability API."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.m1.models import (ActionAudit, GraphExecutionRecord, ObservabilityRecord,
                           PendingAction, Run, RunEvent, Trace)

TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}
_EVENT_FIELDS = {
    "started": (),
    "route_decided": ("strategy", "sub_strategy", "decision_source", "latency_ms"),
    "agent_started": ("agent", "handoff_id", "task_id"),
    "retrieval_hop": ("hop", "retrieval_index", "aspect_id", "strategy_selected", "strategy_executed",
                      "evidence_count", "knowledge_version", "degraded", "degraded_reason", "fallback_reason",
                      "latency_ms", "duration_ms"),
    "tool_started": ("tool_id", "tool", "capability", "tool_name"),
    "tool_finished": ("tool_id", "tool", "capability", "status", "error_code", "duration_ms"),
    "action_pending": ("action_id", "tool_id", "status"),
    "answer": (),
    "done": ("status",),
    "error": ("status", "error_code", "stage"),
}
RETRIEVAL_STRATEGY_ALIASES = {
    "bm25": "BM25",
    "vector": "VECTOR",
    "rrf": "RRF",
    "rrf_reranker": "HYBRID_RERANK",
    "hybrid_rerank": "HYBRID_RERANK",
    "hybrid_rerank_degraded": "HYBRID_RERANK_DEGRADED",
    "mixed": "MIXED",
}
MAX_RETRIEVAL_HOPS = 20
MAX_EVIDENCE_IDS_PER_HOP = 50


def canonical_retrieval_strategy(raw: str | None) -> str:
    """Normalize only aliases whose executed algorithms are equivalent; retain raw values elsewhere."""
    if not isinstance(raw, str) or not raw.strip():
        return "OTHER"
    return RETRIEVAL_STRATEGY_ALIASES.get(raw.strip().casefold(), "OTHER")


def _run_wall_duration_ms(run: Run) -> int | None:
    if run.started_at is None or run.finished_at is None or run.finished_at < run.started_at:
        return None
    return max(0, round((run.finished_at - run.started_at).total_seconds() * 1000))


def _resolve_orchestrator(graph_orchestrator: str | None, trace_values: list[str]) -> tuple[str, str]:
    """Return a mode only when persisted execution evidence explicitly identifies it."""
    known = {value for value in [graph_orchestrator, *trace_values] if value in {"legacy", "langgraph"}}
    if len(known) > 1:
        return "unknown", "CONFLICTING_EVIDENCE"
    if len(known) == 1:
        return next(iter(known)), "graph_execution" if graph_orchestrator in known else "orchestrator_selected_trace"
    return "unknown", "NO_RELIABLE_EVIDENCE"


def _retrieval_hop_projection(event: RunEvent) -> dict:
    payload = _json(event.payload_json)
    raw_requested = payload.get("strategy_selected")
    raw_executed = payload.get("strategy_executed")
    raw_requested_safe = raw_requested[:120] if isinstance(raw_requested, str) else None
    raw_executed_safe = raw_executed[:120] if isinstance(raw_executed, str) else None
    evidence_ids_raw = payload.get("evidence_ids")
    evidence_ids = []
    evidence_id_values_truncated = False
    if isinstance(evidence_ids_raw, list):
        for value in evidence_ids_raw[:MAX_EVIDENCE_IDS_PER_HOP]:
            if isinstance(value, str) and value:
                evidence_ids.append(value[:160])
                evidence_id_values_truncated = evidence_id_values_truncated or len(value) > 160
    evidence_count = payload.get("evidence_count")
    if not isinstance(evidence_count, int) or isinstance(evidence_count, bool) or evidence_count < 0:
        evidence_count = None
    latency = payload.get("latency_ms")
    metric_source = "latency_ms"
    if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0:
        latency = payload.get("duration_ms")
        metric_source = "duration_ms"
    if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency < 0:
        latency, metric_source = None, None
    missing = []
    for field, value in (("aspect_id", payload.get("aspect_id")),
                         ("requested_strategy", raw_requested), ("executed_strategy", raw_executed),
                         ("knowledge_version", payload.get("knowledge_version")),
                         ("evidence_count", evidence_count), ("evidence_ids", evidence_ids or None),
                         ("latency_ms", latency)):
        if value is None:
            missing.append(field)
    reason = payload.get("fallback_reason") or payload.get("degraded_reason")
    return {
        "run_id": event.run_id,
        "hop": payload.get("retrieval_index", payload.get("hop")),
        "aspect_id": payload.get("aspect_id")[:80] if isinstance(payload.get("aspect_id"), str) else None,
        "requested_strategy": raw_requested_safe,
        "requested_strategy_truncated": isinstance(raw_requested, str) and len(raw_requested) > 120,
        "canonical_requested_strategy": canonical_retrieval_strategy(raw_requested),
        "executed_strategy": raw_executed_safe,
        "executed_strategy_truncated": isinstance(raw_executed, str) and len(raw_executed) > 120,
        "canonical_executed_strategy": canonical_retrieval_strategy(raw_executed),
        "degraded": payload.get("degraded") is True,
        "degraded_reason": reason[:80] if isinstance(reason, str) else None,
        "knowledge_version": payload.get("knowledge_version")[:120] if isinstance(payload.get("knowledge_version"), str) else None,
        "evidence_count": evidence_count,
        "evidence_ids": evidence_ids,
        "evidence_ids_truncated": isinstance(evidence_ids_raw, list) and len(evidence_ids_raw) > MAX_EVIDENCE_IDS_PER_HOP,
        "evidence_id_values_truncated": evidence_id_values_truncated,
        "retrieval_latency_ms": round(float(latency), 3) if latency is not None else None,
        "latency_source_field": metric_source,
        "source": "same_run_persisted_run_event",
        "missing_fields": missing,
    }


def _json(value: str | None) -> dict:
    try:
        result = json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _time_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("time range must be ordered and timezone-aware")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if (end - start).total_seconds() > 31 * 86400:
        raise ValueError("time range must not exceed 31 days")
    return start, end


def _pctl(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))], 3)


def _run_result(run: Run) -> dict:
    return _json(run.result_json)


def _event_projection(event: RunEvent) -> dict:
    payload = _json(event.payload_json)
    allowed = _EVENT_FIELDS.get(event.event_type, ())
    return {"event_id": event.event_id, "seq": event.seq, "timestamp": event.timestamp.isoformat(),
            "type": event.event_type, "data": {key: payload[key] for key in allowed if key in payload}}


def _span_projection(row: ObservabilityRecord) -> dict | None:
    item = _json(row.payload_json)
    fields = ("trace_id", "span_id", "parent_span_id", "run_id", "node_name", "strategy",
              "start_time", "end_time", "duration_ms", "status", "error_code", "graph_version")
    if not item:
        return None
    return {key: item[key] for key in fields if key in item}


def _usage_projection(run: Run) -> dict:
    usage = _json(run.usage_json)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    token_pair = isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0 and \
        isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0
    cost = usage.get("cost_estimate_usd_peak_rate")
    if not isinstance(cost, (int, float)):
        result = _run_result(run)
        cost = result.get("estimated_cost_usd")
        if not isinstance(cost, (int, float)):
            cost = None
    model = run.model_id
    is_stub = bool(model and any(tag in model.casefold() for tag in ("stub", "mock", "test")))
    return {"model_id": model, "provider_usage_present": bool(token_pair and not is_stub),
            "stub": is_stub, "prompt_tokens": prompt if token_pair and not is_stub else None,
            "completion_tokens": completion if token_pair and not is_stub else None,
            "estimated_cost_usd": round(float(cost), 9) if isinstance(cost, (int, float)) and not is_stub else None,
            "cost_kind": "ESTIMATE_NOT_INVOICE" if cost is not None and not is_stub else None,
            "finish_reason": usage.get("finish_reason") if isinstance(usage.get("finish_reason"), str) else None}


def aggregate_admin_overview(factory: sessionmaker, start: datetime, end: datetime, *, limit: int = 5000) -> dict:
    start, end = _time_bounds(start, end)
    with factory() as db:
        runs = db.execute(select(Run).where(Run.created_at >= start, Run.created_at < end)
                          .order_by(Run.created_at).limit(limit + 1)).scalars().all()
        if len(runs) > limit:
            raise ValueError("run result cap exceeded; narrow the time range")
        run_ids = [run.run_id for run in runs]
        events = db.execute(select(RunEvent).where(RunEvent.run_id.in_(run_ids))
                            .order_by(RunEvent.run_id, RunEvent.seq).limit(max(limit * 30, 1) + 1)).scalars().all() if run_ids else []
        records = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids))
                             .order_by(ObservabilityRecord.timestamp).limit(max(limit * 30, 1) + 1)).scalars().all() if run_ids else []
        if len(events) > max(limit * 30, 1) or len(records) > max(limit * 30, 1):
            raise ValueError("observability record cap exceeded; narrow the time range")
        executions = {item.run_id: item for item in db.execute(
            select(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids))).scalars().all()} if run_ids else {}
        action_audits = db.execute(select(ActionAudit).where(
            ActionAudit.occurred_at >= start, ActionAudit.occurred_at < end
        ).order_by(ActionAudit.occurred_at).limit(150001)).scalars().all()
        if len(action_audits) > 150000:
            raise ValueError("action audit record cap exceeded; narrow the time range")
        action_audit_counts = Counter(event.event_type for event in action_audits)

    status_counts = Counter(run.status for run in runs)
    terminal = [run for run in runs if run.status in TERMINAL_RUN_STATUSES]
    success_count = status_counts["SUCCEEDED"]
    latencies = []
    wall_latencies = []
    for run in runs:
        if run.latency_ms is not None and run.latency_ms >= 0:
            latencies.append(float(run.latency_ms))
        wall_duration = _run_wall_duration_ms(run)
        if wall_duration is not None:
            wall_latencies.append(float(wall_duration))

    event_counts = Counter(event.event_type for event in events)
    event_payloads = [(event.event_type, _json(event.payload_json)) for event in events]
    route_event_by_run = {event.run_id: payload for event, (event_type, payload) in zip(events, event_payloads)
                          if event_type == "route_decided"}
    agent_counts = Counter(payload.get("agent") for event_type, payload in event_payloads
                           if event_type == "agent_started" and isinstance(payload.get("agent"), str))
    tool_starts = [(kind, payload) for kind, payload in event_payloads if kind == "tool_started"]
    tool_finishes = [(kind, payload) for kind, payload in event_payloads if kind == "tool_finished"]
    tool_counts: Counter = Counter((payload.get("tool_id") or payload.get("tool") or payload.get("capability") or "UNSPECIFIED")
                                   for _, payload in tool_starts)
    tool_failures = sum(1 for _, payload in tool_finishes
                        if payload.get("status") not in (None, "OK", "SUCCEEDED", "SUCCESS", "COMPLETED") or payload.get("error_code"))
    tool_durations = [float(payload["duration_ms"]) for _, payload in tool_finishes
                      if isinstance(payload.get("duration_ms"), (int, float)) and payload["duration_ms"] >= 0]
    tool_duration_source = "RunEvent.tool_finished.duration_ms" if tool_durations else None

    span_rows = [row for row in records if row.record_type == "span"]
    span_data = [span for row in span_rows if (span := _span_projection(row)) is not None]
    handoffs = 0
    strategies: Counter = Counter()
    retrieval_degraded = 0
    retrieval_calls = Counter()
    retrieval_requested = Counter()
    retrieval_calls_canonical = Counter()
    retrieval_requested_canonical = Counter()
    citation_count = 0
    run_agent_calls: Counter = Counter()
    for event in events:
        if event.event_type == "agent_started":
            run_agent_calls[event.run_id] += 1
    for run in runs:
        result = _run_result(run)
        citation_count += len(result.get("citations", [])) if isinstance(result.get("citations"), list) else 0
        handoffs += sum(1 for item in result.get("handoffs", []) if isinstance(item, dict))
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        strategy = result.get("strategy") or route.get("strategy") or route_event_by_run.get(run.run_id, {}).get("strategy")
        if isinstance(strategy, str):
            strategies[strategy] += 1
        retrieval = result.get("policy_retrieval") if isinstance(result.get("policy_retrieval"), dict) else {}
        retrieval = retrieval or (result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {})
        requested = retrieval.get("strategy_selected")
        if isinstance(requested, str):
            retrieval_requested[requested] += 1
            retrieval_requested_canonical[canonical_retrieval_strategy(requested)] += 1
        executed = retrieval.get("strategy_executed") or retrieval.get("mode")
        if isinstance(executed, str):
            retrieval_calls[executed] += 1
            retrieval_calls_canonical[canonical_retrieval_strategy(executed)] += 1
        if retrieval.get("degraded") is True:
            retrieval_degraded += 1

    if not tool_durations:
        for run in runs:
            result = _run_result(run)
            for call in result.get("tool_execution_status", []) if isinstance(result.get("tool_execution_status"), list) else []:
                if isinstance(call, dict) and isinstance(call.get("duration_ms"), (int, float)) and call["duration_ms"] >= 0:
                    tool_durations.append(float(call["duration_ms"]))
        if tool_durations:
            tool_duration_source = "Run.result_json.tool_execution_status.duration_ms"

    usage_rows = [_usage_projection(run) for run in runs]
    provider_usage = [item for item in usage_rows if item["provider_usage_present"]]
    stub_runs = sum(item["stub"] for item in usage_rows)
    cost_values = [item["estimated_cost_usd"] for item in usage_rows
                   if not item["stub"] and item["estimated_cost_usd"] is not None]
    run_by_id = {run.run_id: run for run in runs}
    model_requests_recorded = 0
    stub_node_requests = 0
    classified_model_node_requests = 0
    unclassified_node_requests = 0
    for row in records:
        if row.record_type != "metric":
            continue
        count = max(0, int((_json(row.payload_json)).get("model_request_count", 0) or 0))
        model_requests_recorded += count
        owner_run = run_by_id.get(row.run_id)
        if owner_run is None or not owner_run.model_id:
            unclassified_node_requests += count
        elif any(tag in owner_run.model_id.casefold() for tag in ("stub", "mock", "test")):
            stub_node_requests += count
        else:
            classified_model_node_requests += count
    model_by_id: dict[str, dict] = {}
    for run, usage in zip(runs, usage_rows):
        if not run.model_id:
            continue
        item = model_by_id.setdefault(run.model_id, {"run_count": 0, "provider_usage_run_count": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "estimated_cost_usd": 0.0, "estimated_cost_sample_count": 0})
        item["run_count"] += 1
        if usage["provider_usage_present"]:
            item["provider_usage_run_count"] += 1
            item["prompt_tokens"] += usage["prompt_tokens"] or 0
            item["completion_tokens"] += usage["completion_tokens"] or 0
        if usage["estimated_cost_usd"] is not None and not usage["stub"]:
            item["estimated_cost_sample_count"] += 1
            item["estimated_cost_usd"] += usage["estimated_cost_usd"]
    for item in model_by_id.values():
        item["estimated_cost_usd"] = round(item["estimated_cost_usd"], 9) if item["estimated_cost_sample_count"] else None
    node_metrics: dict[str, dict[str, float]] = {}
    for row in records:
        if row.record_type != "metric":
            continue
        payload = _json(row.payload_json)
        node = payload.get("node_name")
        if not isinstance(node, str) or not node:
            continue
        item = node_metrics.setdefault(node, {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                              "estimated_cost_usd": 0.0})
        item["request_count"] += int(payload.get("model_request_count", 0) or 0)
        item["prompt_tokens"] += int(payload.get("prompt_tokens", 0) or 0)
        item["completion_tokens"] += int(payload.get("completion_tokens", 0) or 0)
        item["estimated_cost_usd"] += float(payload.get("estimated_cost_usd", 0) or 0)
    degraded_reasons = Counter()
    for run in runs:
        result = _run_result(run)
        retrieval = result.get("policy_retrieval") if isinstance(result.get("policy_retrieval"), dict) else {}
        retrieval = retrieval or (result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {})
        reason = retrieval.get("degraded_reason") or retrieval.get("reason_code")
        if retrieval.get("degraded") is True and isinstance(reason, str) and len(reason) <= 80:
            degraded_reasons[reason] += 1
    agent_durations = [float(span["duration_ms"]) for span in span_data
                       if str(span.get("node_name", "")).startswith("m3.agent.")
                       and isinstance(span.get("duration_ms"), (int, float)) and span["duration_ms"] >= 0]
    sse_total = sum(1 for event in events if event.event_type in _EVENT_FIELDS)
    return {
        "window": {"from": start.isoformat().replace("+00:00", "Z"), "to": end.isoformat().replace("+00:00", "Z")},
        "run_counts": {"total": len(runs), "by_status": dict(sorted(status_counts.items())),
                       "terminal_denominator": len(terminal), "success_rate": round(success_count / len(terminal), 6) if terminal else None,
                       "failure_rate": round(sum(status_counts[s] for s in ("FAILED", "MODEL_UNAVAILABLE")) / len(terminal), 6) if terminal else None,
                       "unknown_result_count": status_counts["UNKNOWN_RESULT"]},
        "latency_ms": {"sample_count": len(latencies), "p50": _pctl(latencies, .50), "p95": _pctl(latencies, .95),
                       "source": "Run.latency_ms only; executor-reported work metric whose component scope varies by path, not HTTP end-to-end"},
        "run_wall_duration_ms": {"sample_count": len(wall_latencies), "p50": _pctl(wall_latencies, .50),
                                  "p95": _pctl(wall_latencies, .95),
                                  "source": "Run.started_at to Run.finished_at; excludes request queue time before claim and is not HTTP end-to-end"},
        "agent_execution": {"actual_agent_started_count": sum(agent_counts.values()), "by_agent": dict(sorted(agent_counts.items())),
                            "supervisor_span_count": sum("supervisor" in str(span.get("node_name", "")).casefold() for span in span_data),
                            "handoff_count": handoffs, "handoff_source": "persisted Run result; failed/incomplete handoffs may be unavailable",
                            "duration_sample_count": len(agent_durations),
                            "duration_p50_ms": _pctl(agent_durations, .50),
                            "duration_p95_ms": _pctl(agent_durations, .95),
                            "agent_calls_per_run_mean": round(sum(agent_counts.values()) / len(runs), 3) if runs else None,
                            "agent_calls_per_run_median": median([run_agent_calls[run.run_id] for run in runs]) if runs else None},
        "tools": {"started_count": len(tool_starts), "failed_count": tool_failures,
                  "by_tool": dict(sorted(tool_counts.items())), "failure_rate_denominator": len(tool_finishes),
                  "duration_sample_count": len(tool_durations), "duration_p50_ms": _pctl(tool_durations, .50),
                  "duration_p95_ms": _pctl(tool_durations, .95),
                  "duration_source": tool_duration_source if tool_durations else None},
        "model": {"recorded_node_request_count": model_requests_recorded,
                  "node_requests_attributed_by_run_model_id": {"stub": stub_node_requests,
                      "non_stub_model_id": classified_model_node_requests, "unclassified": unclassified_node_requests,
                      "note": "node metric classification via terminal Run.model_id; not a provider billing ledger"},
                  "provider_usage_run_count": len(provider_usage), "usage_coverage_denominator": sum(bool(run.model_id) for run in runs),
                  "prompt_tokens": sum(item["prompt_tokens"] or 0 for item in provider_usage),
                  "completion_tokens": sum(item["completion_tokens"] or 0 for item in provider_usage),
                  "estimated_cost_usd": round(sum(cost_values), 9) if cost_values else None,
                  "estimated_cost_sample_count": len(cost_values),
                  "cost_semantics": "provider usage peak-rate estimate; not invoice",
                  "stub_run_count": stub_runs, "unknown_or_unmetered_run_count": sum(bool(item["model_id"]) and not item["provider_usage_present"] and not item["stub"] for item in usage_rows),
                  "by_model_run_usage": dict(sorted(model_by_id.items())),
                  "by_node_metric_records": {key: {**value, "estimated_cost_usd": round(value["estimated_cost_usd"], 9),
                                                    "cost_kind": "recorded node estimate; not invoice"}
                                              for key, value in sorted(node_metrics.items())}},
        "retrieval": {"run_strategy_counts": dict(sorted(strategies.items())),
                      "requested_strategy_counts": dict(sorted(retrieval_requested.items())),
                      "executed_strategy_counts": dict(sorted(retrieval_calls.items())),
                      "canonical_requested_strategy_counts": dict(sorted(retrieval_requested_canonical.items())),
                      "canonical_executed_strategy_counts": dict(sorted(retrieval_calls_canonical.items())),
                      "canonical_strategy_aliases": dict(sorted(RETRIEVAL_STRATEGY_ALIASES.items())),
                      "canonical_counts_conserve_raw_counts": {
                          "requested": sum(retrieval_requested.values()) == sum(retrieval_requested_canonical.values()),
                          "executed": sum(retrieval_calls.values()) == sum(retrieval_calls_canonical.values())},
                      "degraded_run_count": retrieval_degraded, "degraded_reason_counts": dict(sorted(degraded_reasons.items())),
                      "bm25_count": sum(v for k, v in retrieval_calls.items() if "BM25" in k.upper()),
                      "hybrid_rerank_count": sum(v for k, v in retrieval_calls.items() if "HYBRID" in k.upper()),
                      "citation_count": citation_count, "citation_count_is_not_answer_accuracy": True},
        "events": {"persisted_sse_event_count": sse_total, "event_type_counts": dict(sorted(event_counts.items())),
                   "dedupe_basis": "unique persisted RunEvent rows (run_id, seq/event_key)"},
        "recovery_and_idempotency": {"runs_with_graph_checkpoint": sum(bool(item.checkpoint_id) for item in executions.values()),
                                     "idempotent_replay_count": None, "retry_count": None,
                                     "not_recorded": ["idempotent replay attempts", "provider retry count"]},
        "graph_executions": {"count": len(executions), "by_status": dict(sorted(Counter(x.status for x in executions.values()).items()))},
        "controlled_action_audit_events": dict(sorted(action_audit_counts.items())),
        "scope_note": "Runs are counted once by runs.created_at. Action audit events are reported separately; they are not chat Runs.",
        "sample_limit": limit,
    }


def list_admin_runs(factory: sessionmaker, start: datetime, end: datetime, *, limit: int = 100, offset: int = 0,
                    status: str | None = None, strategy: str | None = None,
                    agent: str | None = None, tool_id: str | None = None,
                    orchestrator: str | None = None) -> dict:
    start, end = _time_bounds(start, end)
    if not 1 <= limit <= 100 or offset < 0 or offset > 10000:
        raise ValueError("limit must be 1..100 and offset 0..10000")
    if orchestrator not in (None, "legacy", "langgraph", "unknown"):
        raise ValueError("orchestrator must be legacy, langgraph, or unknown")
    with factory() as db:
        query = select(Run).where(Run.created_at >= start, Run.created_at < end)
        if status:
            query = query.where(Run.status == status)
        candidates = db.execute(query.order_by(Run.created_at.desc()).limit(5001)).scalars().all()
        if len(candidates) > 5000:
            raise ValueError("run list cap exceeded; narrow the time range")
        graph_rows = db.execute(select(GraphExecutionRecord).where(
            GraphExecutionRecord.run_id.in_([row.run_id for row in candidates]))).scalars().all() if candidates else []
        orchestrator_traces = db.execute(select(Trace).where(
            Trace.run_id.in_([row.run_id for row in candidates]),
            Trace.event_type == "orchestrator_selected"
        ).order_by(Trace.created_at).limit(25001)).scalars().all() if candidates else []
        if len(orchestrator_traces) > 25000:
            raise ValueError("orchestrator evidence cap exceeded; narrow the time range")
        event_types = [name for name, enabled in (("route_decided", strategy), ("agent_started", agent),
                       ("tool_started", tool_id)) if enabled]
        matching_event_rows = db.execute(select(RunEvent).where(RunEvent.run_id.in_([row.run_id for row in candidates]),
            RunEvent.event_type.in_(event_types)
        ).limit(25001)).scalars().all() if candidates and event_types else []
        if len(matching_event_rows) > 25000:
            raise ValueError("agent/tool filter event cap exceeded; narrow the time range")
    graph_by_id = {row.run_id: row.orchestrator for row in graph_rows}
    trace_modes: dict[str, list[str]] = {}
    for trace in orchestrator_traces:
        value = _json(trace.event_json).get("orchestrator")
        if isinstance(value, str):
            trace_modes.setdefault(trace.run_id, []).append(value)
    executor_by_id = {item.run_id: _resolve_orchestrator(graph_by_id.get(item.run_id), trace_modes.get(item.run_id, []))
                      for item in candidates}
    if orchestrator:
        candidates = [item for item in candidates if executor_by_id[item.run_id][0] == orchestrator]
    strategy_by_run = {event.run_id: _json(event.payload_json).get("strategy") for event in matching_event_rows
                       if event.event_type == "route_decided"}
    if strategy:
        filtered = []
        for item in candidates:
            result = _run_result(item)
            route = result.get("route") if isinstance(result.get("route"), dict) else {}
            if (result.get("strategy") or route.get("strategy") or strategy_by_run.get(item.run_id)) == strategy:
                filtered.append(item)
        candidates = filtered
    if agent or tool_id:
        matching_agent_ids: set[str] = set()
        matching_tool_ids: set[str] = set()
        for event in matching_event_rows:
            payload = _json(event.payload_json)
            if agent and event.event_type == "agent_started" and payload.get("agent") == agent:
                matching_agent_ids.add(event.run_id)
            if tool_id and event.event_type == "tool_started" and (payload.get("tool_id") or payload.get("tool") or payload.get("capability")) == tool_id:
                matching_tool_ids.add(event.run_id)
        matching_run_ids = matching_agent_ids if agent else matching_tool_ids
        if agent and tool_id:
            matching_run_ids = matching_agent_ids & matching_tool_ids
        candidates = [item for item in candidates if item.run_id in matching_run_ids]
    has_more = len(candidates) > offset + limit
    rows = candidates[offset:offset + limit]
    items = []
    for run in rows:
        result = _run_result(run)
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        actual_strategy = result.get("strategy") or route.get("strategy") or strategy_by_run.get(run.run_id)
        if strategy and actual_strategy != strategy:
            continue
        usage = _usage_projection(run)
        executor, executor_evidence = executor_by_id[run.run_id]
        items.append({"run_id": run.run_id, "status": run.status,
                      "strategy": actual_strategy, "orchestrator": executor,
                      "orchestrator_evidence": executor_evidence,
                      "model_id": usage["model_id"], "latency_ms": run.latency_ms,
                      "wall_duration_ms": _run_wall_duration_ms(run),
                      "latency_semantics": "Run.latency_ms is an executor-reported work metric whose component scope varies by path; wall_duration_ms is started_at to finished_at, not HTTP end-to-end",
                      "error_code": run.error_code, "created_at": run.created_at.isoformat(),
                      "finished_at": run.finished_at.isoformat() if run.finished_at else None})
    return {"items": items, "limit": limit, "offset": offset, "has_more": has_more}


def admin_run_detail(factory: sessionmaker, run_id: str, *, event_limit: int = 100, span_limit: int = 200) -> dict | None:
    if not 1 <= event_limit <= 200 or not 1 <= span_limit <= 300:
        raise ValueError("event_limit must be 1..200 and span_limit 1..300")
    with factory() as db:
        run = db.get(Run, run_id)
        if run is None:
            return None
        events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                            .order_by(RunEvent.seq).limit(event_limit + 1)).scalars().all()
        spans = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id == run_id,
                            ObservabilityRecord.record_type == "span")
                            .order_by(ObservabilityRecord.timestamp).limit(span_limit + 1)).scalars().all()
        traces = db.execute(select(Trace).where(Trace.run_id == run_id)
                            .order_by(Trace.created_at).limit(200)).scalars().all()
        actions = db.execute(select(PendingAction).where(PendingAction.run_id == run_id)).scalars().all()
        audit_rows = []
        if actions:
            audit_rows = db.execute(select(ActionAudit).where(ActionAudit.action_id.in_([a.action_id for a in actions]))
                                    .order_by(ActionAudit.occurred_at).limit(100)).scalars().all()
    graph = db.get(GraphExecutionRecord, run_id)
    result = _run_result(run)
    route = result.get("route") if isinstance(result.get("route"), dict) else {}
    event_rows = [_event_projection(item) for item in events[:event_limit]]
    retrieval_rows = db.execute(select(RunEvent).where(RunEvent.run_id == run_id,
                                RunEvent.event_type == "retrieval_hop")
                                .order_by(RunEvent.seq).limit(MAX_RETRIEVAL_HOPS + 1)).scalars().all()
    retrieval_hops = [_retrieval_hop_projection(item) for item in retrieval_rows[:MAX_RETRIEVAL_HOPS]]
    span_rows = [item for row in spans[:span_limit] if (item := _span_projection(row)) is not None]
    trace_rows = []
    trace_allowed = {"policy_retrieval_completed": ("strategy_selected", "strategy_executed", "degraded", "knowledge_version", "evidence_ids"),
                     "session_memory_context": ("level", "summary_version", "summary_persisted", "source_message_count",
                                                "estimated_tokens_before", "estimated_tokens_after", "compression_ratio")}
    for row in traces:
        if row.event_type not in trace_allowed:
            continue
        payload = _json(row.event_json)
        trace_rows.append({"event_type": row.event_type, "status": row.status, "created_at": row.created_at.isoformat(),
                           "data": {key: payload[key] for key in trace_allowed[row.event_type] if key in payload}})
    trace_executor_modes = [_json(row.event_json).get("orchestrator") for row in traces
                            if row.event_type == "orchestrator_selected"]
    executor, executor_evidence = _resolve_orchestrator(
        graph.orchestrator if graph else None,
        [value for value in trace_executor_modes if isinstance(value, str)])
    return {"run": {"run_id": run.run_id, "status": run.status,
                     "strategy": result.get("strategy") or route.get("strategy"), "model_id": run.model_id,
                     "latency_ms": run.latency_ms, "wall_duration_ms": _run_wall_duration_ms(run),
                     "latency_semantics": "Run.latency_ms is an executor-reported work metric whose component scope varies by path; wall_duration_ms is started_at to finished_at, not HTTP end-to-end",
                     "error_code": run.error_code,
                     "created_at": run.created_at.isoformat(), "started_at": run.started_at.isoformat() if run.started_at else None,
                     "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                     "usage": _usage_projection(run)},
            "orchestrator": executor, "orchestrator_evidence": executor_evidence,
            "graph_execution": {"orchestrator": graph.orchestrator, "status": graph.status,
                                "graph_version": graph.graph_version, "error_code": graph.error_code} if graph else None,
            "retrieval_hops": {"items": retrieval_hops, "has_more": len(retrieval_rows) > MAX_RETRIEVAL_HOPS,
                               "source": "same-run persisted retrieval_hop RunEvents",
                               "not_inferred_from_aggregates": True},
            "events": {"items": event_rows, "has_more": len(events) > event_limit},
            "spans": {"items": span_rows, "has_more": len(spans) > span_limit},
            "trace_metadata": trace_rows,
            "audit_metadata": [{"action_id": row.action_id, "event_type": row.event_type,
                                "from_status": row.from_status, "to_status": row.to_status,
                                "occurred_at": row.occurred_at.isoformat(), "error_code": row.error_code,
                                "trace_id": row.trace_id, "has_transaction_reference": bool(row.transaction_reference)}
                               for row in audit_rows]}
