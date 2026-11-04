"""G1 five-strategy StateGraph; production APIs remain on the legacy executor."""
from __future__ import annotations

import inspect
import time
import re
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .adapters import GraphServices
from .observability import (InMemoryObservabilitySink, MetricRecord, ObservabilitySink,
                            StructuredLog, TraceSpan, elapsed_ms, start_span_id, utc_now)
from .state import GraphInput, GraphRuntimeContext, GraphState, graph_input_state, project_evidence


def _dict(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise TypeError("route/executor must return a mapping or pydantic model")


def _safe_route(route: dict[str, Any]) -> dict[str, Any]:
    allowed = ("strategy", "sub_strategy", "safety_decision", "decision_source", "latency_ms",
               "needs_clarification")
    safe = {key: route[key] for key in allowed if key in route}
    retrieval_query = route.get("retrieval_query")
    if isinstance(retrieval_query, str) and 0 < len(retrieval_query) <= 1000:
        safe["retrieval_query"] = retrieval_query
    reason = route.get("reason_code")
    safe["reason_code"] = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", reason) \
        else "ROUTE_REASON_UNAVAILABLE"
    model_id = route.get("model_id")
    if isinstance(model_id, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,96}", model_id):
        safe["model_id"] = model_id
    labels = route.get("intent_labels")
    if isinstance(labels, list) and len(labels) <= 8 and all(
        isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", item) for item in labels
    ):
        safe["intent_labels"] = list(dict.fromkeys(labels))
    fallback_error = route.get("fallback_error")
    if isinstance(fallback_error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", fallback_error):
        safe["fallback_error"] = fallback_error
    diagnostic = route.get("provider_diagnostic")
    if isinstance(diagnostic, dict) and diagnostic.get("schema_version") == "provider-failure-diagnostic-v1":
        phase = diagnostic.get("failure_phase")
        category = diagnostic.get("error_category")
        delivery = diagnostic.get("request_delivery")
        exception_types = diagnostic.get("exception_types")
        if (phase in {"PRE_REQUEST_LOCAL_FAILURE", "CONNECTION_NO_RESPONSE", "TIMEOUT_NO_RESPONSE",
                      "HTTP_RESPONSE_RECEIVED", "RESPONSE_PARSE_OR_CONTRACT_FAILURE", "FAILURE_PHASE_UNKNOWN"} and
                category in {"LOCAL_GUARD", "LOCAL_BUDGET_GUARD", "DNS_FAILURE", "TLS_FAILURE",
                             "CONNECT_FAILURE", "TIMEOUT", "HTTP_ERROR", "RESPONSE_INVALID", "UNKNOWN"} and
                type(diagnostic.get("completion_call_entered")) is bool and
                type(diagnostic.get("provider_response_received")) is bool and
                delivery in {"NOT_ATTEMPTED", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED"} and
                isinstance(exception_types, list) and len(exception_types) <= 4 and all(
                    isinstance(item, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", item)
                    for item in exception_types)):
            diag = {"schema_version": "provider-failure-diagnostic-v1", "failure_phase": phase,
                "error_category": category, "completion_call_entered": diagnostic["completion_call_entered"],
                "provider_response_received": diagnostic["provider_response_received"],
                "request_delivery": delivery, "exception_types": list(exception_types)}
            errno_value = diagnostic.get("errno")
            if isinstance(errno_value, int) and not isinstance(errno_value, bool) and -32768 <= errno_value <= 65535:
                diag["errno"] = errno_value
            timeout_stage = diagnostic.get("timeout_stage")
            if timeout_stage in {"CONNECT", "READ", "WRITE", "POOL"}:
                diag["timeout_stage"] = timeout_stage
            status = diagnostic.get("http_status")
            if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
                diag["http_status"] = status
            error_code = diagnostic.get("provider_error_code")
            if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", error_code):
                diag["provider_error_code"] = error_code
            for field in ("usage_status", "billing_status"):
                if diagnostic.get(field) == "UNKNOWN":
                    diag[field] = "UNKNOWN"
            safe["provider_diagnostic"] = diag
    capabilities = route.get("required_capabilities")
    # Keep bounded identifier-shaped values. Unknown but safe identifiers remain
    # visible so the Supervisor can reject them explicitly.
    if isinstance(capabilities, list) and len(capabilities) <= 9 and all(
        isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) for item in capabilities
    ):
        safe["required_capabilities"] = list(dict.fromkeys(capabilities))
    for key, max_len in (("skill_id", 64), ("skill_version", 32)):
        value = route.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-z0-9_.:-]{1," + str(max_len) + r"}", value):
            safe[key] = value
    hit_reason = route.get("skill_hit_reason")
    if isinstance(hit_reason, str) and re.fullmatch(r"KEYWORD_FAMILY:[a-z_]{1,64}", hit_reason):
        safe["skill_hit_reason"] = hit_reason
    skill_tools = route.get("skill_allowed_tools")
    if isinstance(skill_tools, list) and len(skill_tools) <= 16 and all(
        isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item) for item in skill_tools
    ):
        safe["skill_allowed_tools"] = list(dict.fromkeys(skill_tools))
    proposal_tools = route.get("skill_proposable_tools")
    if isinstance(proposal_tools, list) and len(proposal_tools) <= 4 and all(
        item in {"create_return_request", "create_refund_request", "cancel_order"}
        for item in proposal_tools
    ):
        safe["skill_proposable_tools"] = list(dict.fromkeys(proposal_tools))
    for key in ("skill_max_agent_calls", "skill_max_steps"):
        value = route.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 14:
            safe[key] = value
    return safe


def _safe_grade(grade: Any) -> dict[str, Any] | None:
    if not isinstance(grade, dict):
        return None
    allowed = ("sufficient", "status", "grade_status", "reason_code", "missing_condition_ids",
               "covered_condition_ids", "contradicted_condition_ids", "unknown_condition_ids")
    return {key: grade[key] for key in allowed if key in grade}


def _safe_policy_retrieval(details: Any) -> dict[str, Any] | None:
    if not isinstance(details, dict):
        return None
    allowed = ("version", "strategy_selected", "strategy_executed", "strategy_trigger_reason",
        "degraded", "knowledge_version", "index_fingerprint", "retrieval_count", "aspect_ids",
        "missing_aspect_ids", "request_mode", "evaluation_at", "requested_aspects")
    projected = {key: details.get(key) for key in allowed if key in details and key != "requested_aspects"}
    aspects = details.get("requested_aspects")
    if isinstance(aspects, list):
        projected["requested_aspects"] = [
            {"aspect_id": row["aspect_id"]}
            for row in aspects[:4]
            if isinstance(row, dict) and isinstance(row.get("aspect_id"), str)
        ]
    return projected


def build_orchestration_graph(services: GraphServices, *, sink: ObservabilitySink | None = None,
                              checkpointer=None):
    sink = sink or InMemoryObservabilitySink()
    builder = StateGraph(GraphState, context_schema=GraphRuntimeContext)

    def instrument(name: str, fn):
        def run(state: GraphState, runtime: Runtime[GraphRuntimeContext]) -> dict:
            context = runtime.context
            started_at = utc_now()
            started = time.perf_counter()
            span_id = start_span_id()
            previous_span = context.active_span_id
            spans = state.get("spans", [])
            parent = None if name == "router" else (previous_span or (spans[-1]["span_id"] if spans else None))
            context.active_span_id = span_id
            try:
                delta = fn(state, context) or {}
                failed = delta.get("terminal_status") == "FAILED"
                status = "ERROR" if failed else "OK"
                error_code = delta.get("error_code") if failed else None
                message = f"execution ended with {error_code}" if failed else "node completed"
            except Exception as exc:  # error details are classified without raw exception text
                error_code = getattr(exc, "code", None) or "NODE_EXECUTION_FAILED"
                diagnostic = getattr(exc, "diagnostic", None)
                context.failure_code = error_code
                context.failure_retryable = bool(getattr(exc, "retryable", False))
                context.failure_diagnostic = {"exception_type": type(exc).__name__}
                if isinstance(diagnostic, dict):
                    from app.m1.routing import safe_route_capability_scope_diagnostic
                    if error_code == 'ROUTE_CLASSIFICATION_FAILED' and isinstance(diagnostic.get('route'), dict):
                        from app.m1.routing import _safe_provider_diagnostic, _safe_usage
                        failed_route = _safe_route(diagnostic['route'])
                        provider_diagnostic = _safe_provider_diagnostic(diagnostic['route'].get('provider_diagnostic'))
                        if provider_diagnostic is not None:
                            failed_route['provider_diagnostic'] = provider_diagnostic
                        usage = _safe_usage(diagnostic['route'].get('usage'))
                        if usage is not None:
                            failed_route['usage'] = usage
                        context.failure_diagnostic['route'] = failed_route
                    safe_scope = safe_route_capability_scope_diagnostic(diagnostic)
                    if safe_scope is not None:
                        context.failure_diagnostic["route_capability_scope"] = safe_scope
                    # Policy execution already bounds/redacts this owner-scoped
                    # audit. Carry it to Run failure persistence, never to the
                    # generic structured log below or to a delivered answer.
                    rejection = diagnostic.get("answer_rejection_audit", diagnostic)
                    if (isinstance(rejection, dict) and error_code in {"ANSWER_SEMANTIC_VALIDATION_FAILED", "MODEL_INVALID_RESPONSE",
                                       "ANSWER_ASPECT_CITATION_MISSING"} and
                            rejection.get("diagnostic_schema_version") == "policy-answer-rejection-audit-v1" and
                            rejection.get("run_id") == state["run_id"] and
                            rejection.get("trace_id") == state["trace_id"]):
                        context.failure_diagnostic["answer_rejection_audit"] = rejection
                delta = {"terminal_status": "FAILED", "error_code": error_code}
                status = "ERROR"
                error_code = delta["error_code"]
                message = f"{type(exc).__name__}: {error_code}"
            finally:
                context.active_span_id = previous_span
            duration = elapsed_ms(started)
            span = TraceSpan(trace_id=state["trace_id"], span_id=span_id,
                parent_span_id=parent, run_id=state["run_id"], node_name=name,
                strategy=state.get("strategy"), start_time=started_at, end_time=utc_now(),
                duration_ms=duration, status=status, error_code=error_code,
                graph_version=state.get("graph_version", "g2"))
            sink.record_span(span)
            sink.record_log(StructuredLog(timestamp=utc_now(), level="INFO" if status == "OK" else "ERROR",
                trace_id=state["trace_id"], run_id=state["run_id"], node=name,
                event_type="node_completed" if status == "OK" else "node_failed",
                error_code=error_code, sanitized_message=message, span_id=span_id))
            usage = (delta.get("node_usage") or delta.get("usage") or delta.get("budget")) if isinstance(delta, dict) else None
            if not isinstance(usage, dict):
                usage = {}
            sink.record_metrics(MetricRecord(node_execution_count=1, node_error_count=int(status != "OK"),
                node_duration_ms=[duration], model_request_count=int(delta.get("model_request_count", 0) or 0),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                estimated_cost_usd=float(delta.get("node_estimated_cost_usd", delta.get("estimated_cost_usd",
                    usage.get("estimated_cost_usd", usage.get("cost_estimate_usd_peak_rate", 0)))) or 0),
                retrieval_hop_count=int(delta.get("retrieval_hop_count", 0) or 0),
                span_id=span_id, node_name=name, graph_version=state.get("graph_version", "g2")))
            return {**delta, "spans": [*(delta.get("spans") or []), span.model_dump(mode="json")]}
        return run

    def router_node(state: GraphState, context: GraphRuntimeContext) -> dict:
        try:
            accepts_memory = "context_notes" in inspect.signature(services.router).parameters
        except (TypeError, ValueError):
            accepts_memory = False
        route = _dict(services.router(context.query, context_notes=context.memory_context)
                      if accepts_memory else services.router(context.query))
        strategy = route.get("strategy")
        if strategy not in {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}:
            raise ValueError("route strategy is invalid")
        if strategy == "AGENT":
            from app.skills.registry import bind_skill
            # Skill selection is server-owned; never trust Skill fields returned
            # by a classifier/provider as a registry binding.
            route = {key: value for key, value in route.items() if not key.startswith("skill_")}
            skill_binding = bind_skill(context.query, route)
            if skill_binding:
                route = {**route, **skill_binding}
        context.route_payload = route
        if context.emit_event:
            context.event_sequence += 1
            context.emit_event("route_decided", route, f"graph:route:{context.event_sequence}")
        usage = route.get("usage") if isinstance(route.get("usage"), dict) else None
        context.model_request_count += int(route.get("decision_source") == "LLM" or bool(route.get("fallback_error")))
        return {"route": _safe_route(route), "strategy": strategy, "sub_strategy": route.get("sub_strategy"),
                "usage": usage,
                "model_request_count": int(route.get("decision_source") == "LLM" or bool(route.get("fallback_error")))}

    def execute(name: str, fn):
        def node(state: GraphState, context: GraphRuntimeContext) -> dict:
            if state.get("terminal_status") == "FAILED":
                return {}
            if name == "agent":
                if services.agent_single is not None:
                    result=services.agent_single(dict(state),context)
                    context.execution_result=result
                    context.model_request_count+=result['model_request_count']
                    return {k:result[k] for k in ('terminal_status','answer_kind')}
                if not context.m3_supervisor_enabled or services.agent_supervisor is None:
                    failed = {"terminal_status": "FAILED", "error_code": "ROUTE_EXECUTOR_UNAVAILABLE",
                              "answer_kind": None}
                    context.execution_result = failed
                    return failed
                route = context.route_payload or {}
                if route.get("skill_id"):
                    from app.skills.runtime import run_selected_skill
                    result = run_selected_skill(route,
                        lambda: _dict(services.agent_supervisor(dict(state), context)),
                        query=context.query,
                        sink=sink, run_id=state["run_id"], trace_id=state["trace_id"],
                        graph_version=state.get("graph_version", "g2"))
                else:
                    result = services.agent_supervisor(dict(state), context)
                m3_result = _dict(result)
                context.execution_result = m3_result
                # Only validated results from live, registered read adapters may
                # become user-facing deterministic business answers.
                from app.m3.order_tools import build_order_business_result
                from app.m3.product_tools import build_product_business_result
                from app.m3.policy_tools import build_policy_business_result
                from app.m3.after_sales_tools import build_after_sales_business_result
                from app.m3.composition import build_multi_agent_business_result
                route = context.route_payload or {}
                business = build_multi_agent_business_result(m3_result, route=route,
                    trusted=context.trusted_agent_context, result_store=context.tool_result_store or {}, query=context.query)
                if business is None:
                    business = build_order_business_result(m3_result, route=route,
                        trusted=context.trusted_agent_context, result_store=context.tool_result_store or {})
                if business is None:
                    business = build_product_business_result(m3_result, route=route,
                        result_store=context.tool_result_store or {})
                if business is None:
                    business = build_policy_business_result(m3_result, route=route,
                        result_store=context.tool_result_store or {})
                if business is None:
                    business = build_after_sales_business_result(m3_result, route=route,
                        result_store=context.tool_result_store or {})
                if business is not None:
                    if services.business_composer is not None:
                        business=services.business_composer(business,context)
                        context.model_request_count+=business['model_request_count']
                    context.execution_result = {**m3_result, **business, "terminal_status": "SUCCEEDED",
                                                "error_code": None}
                    fields = {"business_intent", "current_task", "agent_task_cursor", "agent_tasks",
                        "intermediate_results", "handoffs", "tool_execution_status", "agent_budget",
                        "supervisor_status", "spans"}
                    output = {key: value for key, value in m3_result.items() if key in fields}
                    output.update({"terminal_status": "SUCCEEDED", "answer_kind": business["answer_kind"],
                                   "error_code": None})
                    return output
                error_code = m3_result.get("error_code")
                if m3_result.get("supervisor_status") in {"COMPLETED", "STUB_ONLY"}:
                    error_code = "AGENT_RESULT_NOT_USER_ANSWERABLE"
                elif not error_code:
                    error_code = "AGENT_EXECUTION_NOT_IMPLEMENTED"
                fields = {"business_intent", "current_task", "agent_task_cursor", "agent_tasks",
                    "intermediate_results", "handoffs", "tool_execution_status", "agent_budget",
                    "supervisor_status", "spans"}
                output = {key: value for key, value in m3_result.items() if key in fields}
                output.update({"terminal_status": "FAILED", "error_code": error_code, "answer_kind": None})
                return output
            exec_state = {**dict(state), "query": context.query, "route": context.route_payload,
                          "_memory_context": context.memory_context,
                          "_emit_retrieval_hop": _retrieval_callback(context),
                          "_emit_policy_retrieval_audit": _policy_retrieval_audit_callback(
                              context, run_id=state.get("run_id"), trace_id=state.get("trace_id"))}
            # Record policy execution only when the policy node is actually
            # entered; route selection alone is not an agent start. Product and
            # other M3 agents emit their own event at specialist dispatch.
            if name in {"single", "multi"} and context.emit_event:
                context.event_sequence += 1
                context.emit_event("agent_started", {"agent": "policy"},
                    f"graph:agent:{context.event_sequence}")
            result = _dict(fn(exec_state))
            context.execution_result = result
            context.model_request_count += int(result.get("model_request_count", 0) or 0)
            context.retrieval_hop_count += int(result.get("retrieval_hop_count", 0) or 0)
            permitted = {"answer_kind", "citations", "evidence", "knowledge_version",
                         "grade_result", "stop_reason", "budget", "terminal_status", "error_code",
                         "usage", "node_usage", "model_request_count", "retrieval_hop_count", "retrieval",
                         "policy_retrieval", "policy_retrieval_audit", "run_budget"}
            permitted.add("estimated_cost_usd")
            permitted.add("node_estimated_cost_usd")
            output = {key: value for key, value in result.items() if key in permitted}
            if "evidence" in output:
                output["evidence"] = project_evidence(output["evidence"])
            if "citations" in output:
                output["citations"] = project_evidence(output["citations"])
            if isinstance(output.get("policy_retrieval"), dict):
                output["policy_retrieval"] = _safe_policy_retrieval(output["policy_retrieval"])
            if "terminal_status" not in output:
                answer = result.get("answer")
                output["terminal_status"] = "SUCCEEDED" if isinstance(answer, str) and answer.strip() else "FAILED"
            if output["terminal_status"] != "SUCCEEDED":
                output.setdefault("answer_kind", None)
            if "grade_result" in output:
                output["grade_result"] = _safe_grade(output["grade_result"])
            return output
        return instrument(name, node)

    builder.add_node("router", instrument("router", router_node))
    builder.add_node("direct", execute("direct", services.direct))
    builder.add_node("single", execute("single", services.single))
    builder.add_node("multi", execute("multi", services.multi))
    builder.add_node("refuse", execute("refuse", services.refuse))
    builder.add_node("agent", execute("agent", lambda _state: {}))
    builder.add_edge(START, "router")
    builder.add_conditional_edges("router", lambda state: {
        "DIRECT": "direct", "SINGLE": "single", "MULTI": "multi",
        "AGENT": "agent", "REFUSE": "refuse"}.get(state.get("strategy"), END))
    for node in ("direct", "single", "multi", "refuse", "agent"):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)


def _runtime_context(value: GraphInput | dict, *, emit_event=None, trusted_agent_context=None,
                     m3_supervisor_enabled: bool = False) -> GraphRuntimeContext:
    parsed = value if isinstance(value, GraphInput) else GraphInput.model_validate(value)
    return GraphRuntimeContext(query=parsed.query, emit_event=emit_event,
        trusted_agent_context=trusted_agent_context, m3_supervisor_enabled=m3_supervisor_enabled)


def _retrieval_callback(context: GraphRuntimeContext):
    def emit(payload: dict) -> None:
        if context.emit_event:
            context.event_sequence += 1
            allowed = ("retrieval_index", "aspect_id", "retrieval_mode", "strategy_selected",
                "strategy_executed", "fallback_reason", "call_id", "result_ref",
                "parameter_fingerprint", "status", "executed", "knowledge_version",
                "index_fingerprint", "evidence_ids", "evidence_count", "latency_ms",
                "degraded", "failures", "models", "diagnostics")
            safe = {key: payload[key] for key in allowed if key in payload}
            context.emit_event("retrieval_hop", safe, f"graph:retrieval:{context.event_sequence}")
    return emit


def _policy_retrieval_audit_callback(context: GraphRuntimeContext, *, run_id: str | None,
                                     trace_id: str | None):
    def emit(payload: dict) -> None:
        if context.emit_event is None:
            return
        status = payload.get("status")
        if status not in {"STARTED", "SUCCEEDED", "FAILED"}:
            raise ValueError("POLICY_RETRIEVAL_AUDIT_STATUS_INVALID")
        safe_keys = ("audit_schema_version", "source_type", "capability", "call_id", "result_ref",
            "parameter_fingerprint", "aspect_id", "retrieval_mode", "strategy_selected",
            "strategy_executed", "evaluation_at", "status", "executed", "duration_ms",
            "error_code", "knowledge_version", "policy_document_version", "index_fingerprint", "evidence_ids",
            "document_versions", "degraded")
        safe = {key: payload[key] for key in safe_keys if key in payload}
        safe["run_id"] = run_id
        safe["trace_id"] = trace_id
        call_id = safe.get("call_id")
        if not isinstance(call_id, str) or not call_id or not isinstance(safe.get("result_ref"), str):
            raise ValueError("POLICY_RETRIEVAL_AUDIT_LINKAGE_INVALID")
        kind = "policy_retrieval_started" if status == "STARTED" else "policy_retrieval_finished"
        context.emit_event(kind, safe, f"graph:policy-retrieval:{call_id}:{status.lower()}")
    return emit


def _ephemeral_result(state: GraphState, context: GraphRuntimeContext) -> GraphState:
    """Expose callback output to the caller only; it was never part of checkpoint state."""
    result = context.execution_result or {}
    output = dict(state)
    for key in ("answer", "answer_kind", "citations", "evidence", "knowledge_version", "grade_result",
                "stop_reason", "budget", "retrieval_hop_count", "terminal_status", "error_code", "retrieval",
                "usage", "node_usage", "model_request_count", "estimated_cost_usd", "node_estimated_cost_usd",
                "self_rag", "policy_retrieval", "run_budget", "business_results",
                "policy_retrieval_audit", "tool_references", "tool_execution_status",
                "business_evidence", "tool_call_audit"):
        if key in result:
            output[key] = (_safe_policy_retrieval(result[key]) if key == "policy_retrieval"
                           else result[key])
    if result.get("terminal_status") != "FAILED" and isinstance(result.get("answer"), str):
        output["answer"] = result["answer"]
    elif result.get("terminal_status") == "FAILED" or state.get("terminal_status") == "FAILED":
        output["answer"] = None
    return output


def invoke_graph(graph, input_value: GraphInput | dict, *, emit_event=None, trusted_agent_context=None,
                 m3_supervisor_enabled: bool = False) -> GraphState:
    context = _runtime_context(input_value, emit_event=emit_event,
        trusted_agent_context=trusted_agent_context, m3_supervisor_enabled=m3_supervisor_enabled)
    state = graph.invoke(graph_input_state(input_value), context=context)
    return _ephemeral_result(state, context)
