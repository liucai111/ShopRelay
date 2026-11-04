"""Evidence-only diagnostics for real personal D1 Runs; never calls a Judge."""
from __future__ import annotations

import json


def diagnose_personal_d1_run(*, case_id: str, expected_tool_calls: list[dict], result: dict,
                             trial_budget: dict | None = None) -> dict:
    observations = result.get("observations") or {}
    runs = observations.get("runs") or []
    messages = observations.get("messages") or []
    events = observations.get("run_events") or []
    traces = observations.get("traces") or []
    run = runs[-1] if runs else {}
    event_types = [row.get("event_type") for row in events]
    tool_events = []
    for row in events:
        if row.get("event_type") not in {"tool_started", "tool_finished"}:
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        tool_events.append({"event_type": row.get("event_type"),
            "tool_id": payload.get("tool_id") or payload.get("tool") or payload.get("capability"),
            "status": payload.get("status"), "duration_ms": payload.get("duration_ms")})
    expected = sorted({row.get("tool_id") for row in expected_tool_calls if row.get("mode") == "read_only"})
    observed = sorted({row["tool_id"] for row in tool_events if row.get("tool_id")})
    assistant = [row.get("content") for row in messages if row.get("role") == "assistant"]
    app_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    persisted_result = {}
    try:
        persisted_result = json.loads(run.get("result_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    route = app_result.get("route") or persisted_result.get("route") or {}
    return {
        "adapter": "PERSONAL_D1_TRACE_DIAGNOSTICS_R1",
        "grading_status": "NOT_GRADED",
        "case_id": case_id,
        "agent_started": "agent_started" in event_types,
        "run_created": bool(result.get("run_id") or run.get("run_id")),
        "run_status": run.get("status", result.get("status", "UNKNOWN")),
        "provider_requests": (trial_budget or {}).get("calls_by_category", {}).get("provider", "UNKNOWN"),
        "provider_tokens": (trial_budget or {}).get("tokens", "UNKNOWN"),
        "provider_token_usage_unknown": (trial_budget or {}).get("unknown_tokens", True),
        "tool_events": tool_events,
        "tool_execution_correctness": "UNKNOWN",
        "expected_tool_ids": expected,
        "observed_tool_ids": observed,
        "assistant_final_answer": (assistant[-1] if assistant else
            app_result.get("answer") or persisted_result.get("answer")),
        "route_strategy": route.get("strategy"),
        "route_decision_source": route.get("decision_source"),
        "provider_error": route.get("fallback_error"),
        "provider_response_observed": route.get("decision_source") == "LLM",
        "trace_record_count": len(traces),
        "task_outcome": "UNKNOWN",
        "deterministic_score": "UNKNOWN",
        "semantic_score": "UNKNOWN",
        "evidence_support": "UNKNOWN",
        "safety_assessment": "UNKNOWN" if case_id == "D1-CROSSAGENT-033" else "NOT_FORMALLY_ASSESSED",
        "reason": "Uses persisted application Run/Trace evidence only; no formal D1 rubric, Judge, or external evaluator was invoked.",
    }


def classify_personal_d1_execution(diagnostics: dict, result: dict) -> dict:
    """Separate terminal application Runs from real Agent execution and outcomes."""
    agent_started = diagnostics.get("agent_started") is True
    run_created = diagnostics.get("run_created") is True
    run_status = diagnostics.get("run_status", "UNKNOWN")
    error = diagnostics.get("provider_error") or ""
    route = diagnostics.get("route_strategy")
    if not run_created:
        execution_status = "RUN_NOT_CREATED"
    elif not agent_started and "APIConnectionError" in error:
        execution_status = "UNSCORABLE_ENVIRONMENT_ERROR"
    elif not agent_started and route in {"DIRECT", "REFUSE"} and \
            run_status in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}:
        execution_status = "NON_AGENT_ROUTE_TERMINAL_UNSCORED"
    elif not agent_started:
        execution_status = "UNSCORABLE_NO_AGENT_START"
    elif run_status in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}:
        execution_status = "AGENT_RUN_TERMINAL_UNSCORED"
    else:
        execution_status = "UNKNOWN_RUN_STATE"
    return {"execution_status": execution_status, "run_status": run_status,
        "run_created": run_created, "agent_started": agent_started,
        "provider_error": error or None, "route_strategy": route,
        "task_outcome": "UNKNOWN", "quality_score": "UNKNOWN", "grading_status": "NOT_GRADED"}
