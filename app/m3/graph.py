"""Bounded LangGraph Supervisor subgraph for isolated M3.1 engineering tests."""
from __future__ import annotations

import hashlib
import inspect
import re
import time
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.orchestration.observability import (InMemoryObservabilitySink, MetricRecord,
    ObservabilitySink, StructuredLog, TraceSpan, elapsed_ms, start_span_id, utc_now)
from app.orchestration.state import GraphRuntimeContext, GraphState
from .agents import AgentRegistry, default_agent_registry
from .contracts import (AgentHandoff, AgentResult, AgentTask, CAPABILITY_AGENT, TrustedAgentContext,
    WRITE_CAPABILITIES)
from .order_tools import AgentExecutionContext

MAX_AGENT_CALLS = 4
MAX_SUPERVISOR_STEPS = 14
_CAPABILITY_AGENT = CAPABILITY_AGENT
_AGENT_ORDER = ("order_logistics", "product", "policy", "after_sales")
_TASK_LABEL = {
    "order_logistics": "Resolve requested live order or shipment capability",
    "product": "Resolve requested live product capability",
    "policy": "Retrieve and explain requested policy evidence",
    "after_sales": "Resolve requested after-sales capability under confirmation controls",
}
_UPSTREAM_DEPENDENCIES = {
    "order_logistics": frozenset(),
    "product": frozenset({"order_logistics"}),
    "policy": frozenset({"order_logistics", "product"}),
    "after_sales": frozenset({"order_logistics", "product", "policy"}),
}


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _safe_intent(route: dict[str, Any]) -> str:
    value = route.get("reason_code")
    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", value):
        return value
    labels = route.get("intent_labels")
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,48}", item):
                return item.upper()
    return "AGENT_REQUEST"


def _plan_tasks(state: GraphState, trusted: TrustedAgentContext) -> list[AgentTask]:
    route = state.get("route") or {}
    capabilities = route.get("required_capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("AGENT_CAPABILITIES_MISSING")
    if len(capabilities) > 8 or any(not isinstance(item, str) for item in capabilities):
        raise ValueError("AGENT_CAPABILITIES_INVALID")
    if len(set(capabilities)) != len(capabilities) or any(item not in _CAPABILITY_AGENT for item in capabilities):
        raise ValueError("AGENT_CAPABILITIES_UNSUPPORTED")
    if route.get("skill_id"):
        from app.skills.registry import validate_skill_execution
        try:
            validate_skill_execution(route)
        except ValueError as exc:
            raise ValueError("SKILL_TOOL_SCOPE_VIOLATION") from exc

    grouped: dict[str, list[str]] = {name: [] for name in _AGENT_ORDER}
    for capability in capabilities:
        grouped[_CAPABILITY_AGENT[capability]].append(capability)
    context_refs = [f"run:{trusted.run_id}", f"session:{trusted.session_id}"]
    evidence_refs = []
    for row in state.get("evidence", []):
        chunk_id = row.get("chunk_id") if isinstance(row, dict) else None
        if isinstance(chunk_id, str) and chunk_id:
            evidence_refs.append(f"evidence:{_stable_id(chunk_id)}")
    evidence_refs = list(dict.fromkeys(evidence_refs))[:16]
    tasks: list[AgentTask] = []
    for agent in _AGENT_ORDER:
        selected = grouped[agent]
        if not selected:
            continue
        ordered_caps = sorted(selected)
        task_id = _stable_id(trusted.run_id, agent, *ordered_caps)
        tasks.append(AgentTask(task_id=task_id, target_agent=agent, task=_TASK_LABEL[agent],
            required_capabilities=ordered_caps, context_refs=context_refs, evidence_refs=evidence_refs))
    if not tasks or len(tasks) > MAX_AGENT_CALLS:
        raise ValueError("SUPERVISOR_BUDGET_EXCEEDED")
    return tasks


def _span_parent(state: GraphState, context: GraphRuntimeContext, name: str) -> str | None:
    spans = state.get("spans", [])
    if name.startswith("m3.") and spans and str(spans[-1].get("node_name", "")).startswith("m3."):
        return spans[-1]["span_id"]
    return context.active_span_id or (spans[-1]["span_id"] if spans else None)


def _observed(name: str, fn, sink: ObservabilitySink):
    def node(state: GraphState, runtime: Runtime[GraphRuntimeContext]) -> dict:
        context = runtime.context
        span_id = start_span_id()
        previous = context.active_span_id
        parent = _span_parent(state, context, name)
        started_at = utc_now()
        started = time.perf_counter()
        context.active_span_id = span_id
        try:
            delta = fn(state, context) or {}
            failed = delta.get("supervisor_status") == "FAILED" or delta.get("terminal_status") == "FAILED"
            error_code = delta.get("error_code") if failed else None
            status = "ERROR" if failed else "OK"
            message = f"execution ended with {error_code}" if failed else "node completed"
        except Exception as exc:  # never persist raw provider/task exception text
            candidate_code = getattr(exc, "code", None)
            error_code = candidate_code if isinstance(candidate_code, str) and \
                re.fullmatch(r"[A-Z0-9_]{1,64}", candidate_code) else "SUPERVISOR_NODE_FAILED"
            delta = {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": error_code}
            status = "ERROR"
            message = f"{type(exc).__name__}: {error_code}"
        finally:
            context.active_span_id = previous
        span = TraceSpan(trace_id=state["trace_id"], span_id=span_id,
            parent_span_id=parent, run_id=state["run_id"], node_name=name,
            strategy="AGENT", start_time=started_at, end_time=utc_now(),
            duration_ms=elapsed_ms(started), status=status, error_code=error_code,
            graph_version=state.get("graph_version", "m3.1"))
        sink.record_span(span)
        sink.record_log(StructuredLog(timestamp=utc_now(), level="ERROR" if status == "ERROR" else "INFO",
            trace_id=state["trace_id"], run_id=state["run_id"], node=name,
            event_type="node_failed" if status == "ERROR" else "node_completed",
            error_code=error_code, sanitized_message=message, span_id=span_id))
        sink.record_metrics(MetricRecord(node_execution_count=1, node_error_count=int(status == "ERROR"),
            node_duration_ms=[span.duration_ms], retrieval_hop_count=0,
            span_id=span_id, node_name=name, graph_version=span.graph_version))
        return {**delta, "spans": [*(delta.get("spans") or []), span.model_dump(mode="json")]}
    return node


def build_supervisor_graph(registry: AgentRegistry | None = None, *, sink: ObservabilitySink | None = None):
    """Compile a bounded subgraph. The parent G2 graph supplies its checkpointer."""
    registry = registry or default_agent_registry()
    sink = sink or InMemoryObservabilitySink()
    agents = registry.as_mapping()
    builder = StateGraph(GraphState, context_schema=GraphRuntimeContext)

    def fail(code: str) -> dict:
        return {"supervisor_status": "FAILED", "terminal_status": "FAILED", "error_code": code}

    def plan(state: GraphState, context: GraphRuntimeContext) -> dict:
        trusted = context.trusted_agent_context
        if not isinstance(trusted, TrustedAgentContext) or not trusted.authenticated or \
           not trusted.run_owner_verified or not trusted.session_owner_verified or \
           (trusted.run_id, trusted.session_id, trusted.trace_id) != \
           (state.get("run_id"), state.get("session_id"), state.get("trace_id")):
            return fail("AGENT_CONTEXT_INVALID")
        try:
            tasks = _plan_tasks(state, trusted)
        except ValueError as exc:
            code = str(exc)
            if code not in {"AGENT_CAPABILITIES_MISSING", "AGENT_CAPABILITIES_INVALID",
                            "AGENT_CAPABILITIES_UNSUPPORTED", "SUPERVISOR_BUDGET_EXCEEDED"}:
                code = "AGENT_PLAN_INVALID"
            return fail(code)
        return {
            "business_intent": _safe_intent(state.get("route") or {}),
            "agent_tasks": [item.model_dump(mode="json") for item in tasks],
            "current_task": tasks[0].model_dump(mode="json"),
            "agent_task_cursor": 0,
            "agent_budget": {"max_agent_calls": MAX_AGENT_CALLS, "max_steps": MAX_SUPERVISOR_STEPS,
                             "agent_calls": 0, "steps": 1, "model_requests": 0,
                             "write_executions": 0},
            "supervisor_status": "PLANNED",
        }

    def dispatch(state: GraphState, _context: GraphRuntimeContext) -> dict:
        budget = dict(state.get("agent_budget") or {})
        if int(budget.get("steps", 0)) >= MAX_SUPERVISOR_STEPS:
            return fail("SUPERVISOR_BUDGET_EXCEEDED")
        task_value = state.get("current_task")
        if not isinstance(task_value, dict):
            return fail("SUPERVISOR_TASK_MISSING")
        try:
            task = AgentTask.model_validate(task_value)
        except Exception:
            return fail("AGENT_TASK_SCHEMA_INVALID")
        budget["steps"] = int(budget.get("steps", 0)) + 1
        return {"current_task": task.model_dump(mode="json"), "agent_budget": budget,
                "supervisor_status": "RUNNING"}

    def specialist(agent_name: str):
        def execute(state: GraphState, context: GraphRuntimeContext) -> dict:
            try:
                task = AgentTask.model_validate(state.get("current_task") or {})
            except Exception:
                return fail("AGENT_TASK_SCHEMA_INVALID")
            if task.target_agent != agent_name:
                return fail("AGENT_HANDOFF_TARGET_MISMATCH")
            trusted = context.trusted_agent_context
            if not isinstance(trusted, TrustedAgentContext):
                return fail("AGENT_CONTEXT_INVALID")
            result_id = f"{task.task_id}:result"
            cached = next((row for row in state.get("intermediate_results", [])
                           if isinstance(row, dict) and row.get("result_id") == result_id), None)
            if context.emit_event and not cached:
                context.event_sequence += 1
                handoff_id = _stable_id(state["run_id"], task.task_id, "supervisor", task.target_agent)
                context.emit_event("agent_started", {"agent": agent_name, "handoff_id": handoff_id,
                    "task_id": task.task_id}, f"m3:agent:{task.task_id}:started")
            if cached:
                result = AgentResult.model_validate(cached)
            else:
                tool_record_start = len(context.tool_execution_records or [])
                budget = state.get("agent_budget") or {}
                if int(budget.get("agent_calls", 0)) >= MAX_AGENT_CALLS or \
                   int(budget.get("steps", 0)) >= MAX_SUPERVISOR_STEPS:
                    return fail("SUPERVISOR_BUDGET_EXCEEDED")
                try:
                    execution_context = AgentExecutionContext(
                        result_store=context.tool_result_store if context.tool_result_store is not None else {},
                        sink=sink, run_id=state["run_id"], trace_id=state["trace_id"],
                        parent_span_id=context.active_span_id, user_query=context.query,
                        memory_context=context.memory_context,
                        graph_version=state.get("graph_version", "g2-m3.4"),
                        retrieval_service=context.retrieval_service,
                        retrieval_evaluation_at=context.retrieval_evaluation_at,
                        route_payload=context.route_payload or {},
                        emit_event=context.emit_event,
                        escalation_permission_checker=context.escalation_permission_checker,
                        call_records=context.tool_execution_records if context.tool_execution_records is not None else [])
                    execute = agents[agent_name].execute
                    # Keep compatibility with M3.1 external/test agents that implement
                    # the original two-argument protocol; new tool adapters accept context.
                    result = (execute(task, trusted, execution_context)
                              if len(inspect.signature(execute).parameters) >= 3 else execute(task, trusted))
                    if not isinstance(result, AgentResult):
                        result = AgentResult.model_validate(result)
                except TimeoutError:
                    result = AgentResult(result_id=result_id, source_agent=agent_name,
                        status="FAILED", error_code="AGENT_TIMEOUT")
                except Exception as exc:
                    code = getattr(exc, "code", None) or "AGENT_EXECUTION_FAILED"
                    result = AgentResult(result_id=result_id, source_agent=agent_name,
                        status="FAILED", error_code=code)
                if result.result_id != result_id or result.source_agent != agent_name:
                    return fail("AGENT_RESULT_SCOPE_MISMATCH")
                from app.m3.order_tools import not_executed_tool_audit
                call_records = context.tool_execution_records
                if call_records is not None:
                    current_calls = call_records[tool_record_start:]
                    for capability in task.required_capabilities:
                        if not any(row.get("capability") == capability for row in current_calls):
                            call_records.append(not_executed_tool_audit(capability,
                                run_id=state["run_id"], trace_id=state["trace_id"],
                                error_code=result.error_code))
                if WRITE_CAPABILITIES.intersection(task.required_capabilities) and \
                   result.status in {"COMPLETED", "STUB_COMPLETED"}:
                    result = AgentResult(result_id=result_id, source_agent=agent_name,
                        status="FAILED", error_code="ACTION_EXECUTION_DISABLED_M3_1")
            budget = dict(state.get("agent_budget") or {})
            if not cached:
                budget["agent_calls"] = int(budget.get("agent_calls", 0)) + 1
            budget["steps"] = int(budget.get("steps", 0)) + 1
            return {"intermediate_results": [result.model_dump(mode="json")],
                "agent_budget": budget,
                "agent_tasks": [{**task.model_dump(mode="json"), "status": result.status}],
                "tool_execution_status": [
                {"execution_id": f"{task.task_id}:{capability}", "capability": capability,
                     "status": (next((row.get("status") for row in (context.tool_execution_records or [])
                                      if row.get("capability") == capability), None) or
                               ("REQUIRES_CONFIRMATION" if result.status == "REQUIRES_CONFIRMATION" else
                                "NOT_IMPLEMENTED" if result.status == "NOT_IMPLEMENTED" else
                                "STUB_ONLY" if result.status == "STUB_COMPLETED" else
                                "NOT_EXECUTED" if result.status == "COMPLETED" else "FAILED")),
                     "executed": any(row.get("capability") == capability and row.get("executed")
                                     for row in (context.tool_execution_records or [])),
                     "error_code": (next((row.get("error_code") for row in (context.tool_execution_records or [])
                                          if row.get("capability") == capability), None) or result.error_code),
                     "duration_ms": next((row.get("duration_ms") for row in (context.tool_execution_records or [])
                                          if row.get("capability") == capability), None),
                     **({key: row[key] for key in ("retrieval_mode", "knowledge_version", "index_fingerprint",
                           "evidence_ids", "retrieval_count", "degraded") if key in row}
                        if (row := next((item for item in (context.tool_execution_records or [])
                                         if item.get("capability") == capability), None)) else {})}
                    for capability in task.required_capabilities],
                "supervisor_status": result.status,
                "error_code": result.error_code,
            }
        return execute

    def collect(state: GraphState, _context: GraphRuntimeContext) -> dict:
        budget = dict(state.get("agent_budget") or {})
        if int(budget.get("steps", 0)) >= MAX_SUPERVISOR_STEPS:
            return fail("SUPERVISOR_BUDGET_EXCEEDED")
        budget["steps"] = int(budget.get("steps", 0)) + 1
        tasks = [AgentTask.model_validate(item) for item in state.get("agent_tasks", [])]
        cursor = int(state.get("agent_task_cursor", 0))
        if cursor >= len(tasks) or cursor >= MAX_AGENT_CALLS:
            return fail("SUPERVISOR_BUDGET_EXCEEDED")
        task = tasks[cursor]
        result_data = next((item for item in state.get("intermediate_results", [])
                            if item.get("result_id") == f"{task.task_id}:result"), None)
        if result_data is None:
            return fail("AGENT_RESULT_MISSING")
        result = AgentResult.model_validate(result_data)
        source = tasks[cursor - 1].target_agent if cursor > 0 else "supervisor"
        handoff_id = _stable_id(state["run_id"], task.task_id, source, task.target_agent)
        handoff = AgentHandoff(handoff_id=handoff_id, source_agent=source,
            target_agent=task.target_agent, task=task.task,
            required_capabilities=task.required_capabilities, context_refs=task.context_refs,
            evidence_refs=list(dict.fromkeys([*task.evidence_refs, *result.evidence_refs]))[:16],
            result=result, status=result.status,
            error_code=result.error_code)
        if result.status not in {"COMPLETED", "STUB_COMPLETED"}:
            return {"handoffs": [handoff.model_dump(mode="json")], "agent_budget": budget,
                    "supervisor_status": "BLOCKED", "terminal_status": "FAILED",
                    "error_code": result.error_code or "AGENT_NOT_IMPLEMENTED"}
        next_cursor = cursor + 1
        if next_cursor >= len(tasks):
            status = "COMPLETED" if result.status == "COMPLETED" and all(
                row.get("status") == "COMPLETED" for row in state.get("intermediate_results", [])) else "STUB_ONLY"
            return {"handoffs": [handoff.model_dump(mode="json")], "agent_budget": budget, "agent_task_cursor": next_cursor,
                    "current_task": None, "supervisor_status": status,
                    # The Supervisor has no answer synthesizer in M3.1; parent must not claim chat success.
                    "terminal_status": "RUNNING"}
        if next_cursor >= MAX_AGENT_CALLS:
            return {"handoffs": [handoff.model_dump(mode="json")], "supervisor_status": "BLOCKED",
                    "terminal_status": "FAILED", "error_code": "SUPERVISOR_BUDGET_EXCEEDED"}
        next_task = tasks[next_cursor]
        dependencies = _UPSTREAM_DEPENDENCIES[next_task.target_agent]
        upstream_tasks = tasks[:next_cursor]
        upstream_results = {item.get("source_agent"): AgentResult.model_validate(item)
                            for item in state.get("intermediate_results", [])
                            if isinstance(item, dict)}
        relevant = [upstream_results[item.target_agent] for item in upstream_tasks
                    if item.target_agent in dependencies and item.target_agent in upstream_results]
        result_refs = [ref for item in relevant for ref in (*item.fact_refs, *item.tool_result_refs)]
        evidence_refs = [ref for item in relevant for ref in item.evidence_refs]
        if result_refs or evidence_refs:
            next_task = next_task.model_copy(update={
                "context_refs": list(dict.fromkeys([*next_task.context_refs, *result_refs]))[:8],
                "evidence_refs": list(dict.fromkeys([*next_task.evidence_refs, *evidence_refs]))[:16],
            })
        return {"handoffs": [handoff.model_dump(mode="json")], "agent_budget": budget, "agent_task_cursor": next_cursor,
                "current_task": next_task.model_dump(mode="json"), "supervisor_status": "PLANNED"}

    def finish(state: GraphState, _context: GraphRuntimeContext) -> dict:
        budget = dict(state.get("agent_budget") or {})
        budget["steps"] = min(MAX_SUPERVISOR_STEPS, int(budget.get("steps", 0)) + 1)
        if state.get("supervisor_status") in {"COMPLETED", "STUB_ONLY"}:
            return {"agent_budget": budget, "terminal_status": "RUNNING"}
        return {"agent_budget": budget, "terminal_status": "FAILED",
                "error_code": state.get("error_code") or "AGENT_EXECUTION_FAILED"}

    builder.add_node("m3.supervisor.plan", _observed("m3.supervisor.plan", plan, sink))
    builder.add_node("m3.supervisor.dispatch", _observed("m3.supervisor.dispatch", dispatch, sink))
    for agent_name in _AGENT_ORDER:
        builder.add_node(f"m3.agent.{agent_name}", _observed(
            f"m3.agent.{agent_name}", specialist(agent_name), sink))
    builder.add_node("m3.supervisor.collect", _observed("m3.supervisor.collect", collect, sink))
    builder.add_node("m3.supervisor.finish", _observed("m3.supervisor.finish", finish, sink))
    builder.add_edge(START, "m3.supervisor.plan")
    builder.add_conditional_edges("m3.supervisor.plan", lambda state:
        "m3.supervisor.dispatch" if state.get("supervisor_status") == "PLANNED" else "m3.supervisor.finish")
    builder.add_conditional_edges("m3.supervisor.dispatch", lambda state:
        "m3.supervisor.finish" if state.get("terminal_status") == "FAILED" else {
            name: f"m3.agent.{name}" for name in _AGENT_ORDER
        }.get((state.get("current_task") or {}).get("target_agent"), "m3.supervisor.finish"))
    for agent_name in _AGENT_ORDER:
        builder.add_edge(f"m3.agent.{agent_name}", "m3.supervisor.collect")
    builder.add_conditional_edges("m3.supervisor.collect", lambda state:
        "m3.supervisor.dispatch" if state.get("supervisor_status") == "PLANNED" else "m3.supervisor.finish")
    builder.add_edge("m3.supervisor.finish", END)
    return builder.compile()


def invoke_supervisor(graph, state: GraphState, context: GraphRuntimeContext) -> dict:
    """Invoke the isolated subgraph and return only its delta to the parent node."""
    prior_span_count = len(state.get("spans", []))
    output = graph.invoke(dict(state), context=context)
    return {key: output.get(key) for key in (
        "business_intent", "current_task", "agent_task_cursor", "agent_tasks", "intermediate_results",
        "handoffs", "tool_execution_status", "agent_budget", "supervisor_status", "terminal_status",
        "error_code") if key in output} | {"spans": output.get("spans", [])[prior_span_count:],
        "tool_call_audit": list(context.tool_execution_records or [])}
