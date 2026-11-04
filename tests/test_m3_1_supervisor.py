from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from app.m1.routing import RouteDecision
from app.m1.state import AgentState
from app.m3.agents import (AgentRegistry, DeterministicStubAgent, PolicyAgent,
    OrderLogisticsAgent, AfterSalesAgent, ProductAgent, default_agent_registry, deterministic_stub_registry)
from app.m3.contracts import AgentHandoff, AgentResult, AgentTask, TrustedAgentContext
from app.m3.graph import MAX_AGENT_CALLS, MAX_SUPERVISOR_STEPS, build_supervisor_graph, invoke_supervisor
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import strict_serializer
from app.orchestration.graph import build_orchestration_graph
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state, project_agent_state


def _trusted(run_id="r-m3", session_id="s-m3", trace_id="t-m3"):
    return TrustedAgentContext(user_id="owner-private", run_id=run_id, session_id=session_id,
        trace_id=trace_id, authenticated=True, run_owner_verified=True, session_owner_verified=True)


def _state(capabilities, *, run_id="r-m3", session_id="s-m3", trace_id="t-m3"):
    state = graph_input_state(GraphInput(run_id=run_id, session_id=session_id,
        trace_id=trace_id, query="PRIVATE QUERY must not be checkpointed"), graph_version="g2-m3.1")
    state.update({"strategy": "AGENT", "route": {"strategy": "AGENT", "reason_code": "LIVE_BUSINESS_FACT",
        "intent_labels": ["live_business"], "required_capabilities": list(capabilities)},
        "terminal_status": "RUNNING"})
    return state


def _context(trusted=None):
    return GraphRuntimeContext(query="PRIVATE QUERY must not be checkpointed",
        trusted_agent_context=trusted, m3_supervisor_enabled=True)


@pytest.mark.parametrize("capability,expected", [
    ("search_knowledge", "policy"),
    ("get_order", "order_logistics"),
    ("get_refund_status", "after_sales"),
    ("search_products", "product"),
])
def test_supervisor_maps_each_business_capability_to_its_specialist(capability, expected):
    registry = deterministic_stub_registry()
    graph = build_supervisor_graph(registry)
    output = invoke_supervisor(graph, _state([capability]), _context(_trusted()))
    task = output["agent_tasks"][0]
    assert task["target_agent"] == expected
    assert task["required_capabilities"] == [capability]
    assert output["supervisor_status"] == "STUB_ONLY"
    assert output["terminal_status"] == "RUNNING"  # no user-facing synthesis is implemented
    assert output["handoffs"][0]["status"] == "STUB_COMPLETED"
    assert output["tool_execution_status"][0]["executed"] is False


def test_cross_agent_handoff_is_typed_bounded_and_contains_only_references():
    registry = deterministic_stub_registry()
    graph = build_supervisor_graph(registry)
    output = invoke_supervisor(graph, _state(["get_order", "search_knowledge"]), _context(_trusted()))
    assert [task["target_agent"] for task in output["agent_tasks"]] == ["order_logistics", "policy"]
    assert len(output["handoffs"]) == 2
    assert output["handoffs"][1]["source_agent"] == "order_logistics"
    assert output["handoffs"][1]["target_agent"] == "policy"
    serialized = repr({key: output[key] for key in ("agent_tasks", "intermediate_results", "handoffs")})
    assert "PRIVATE QUERY" not in serialized
    assert "owner-private" not in serialized and "user_id" not in serialized
    assert all(ref.startswith(("run:", "session:")) for handoff in output["handoffs"]
               for ref in handoff["context_refs"])


def test_cross_agent_memory_context_stays_untrusted_and_separate_from_handoff_refs():
    class MemoryCaptureStub(DeterministicStubAgent):
        def __init__(self, name):
            super().__init__(name)
            self.received_memory = None

        def execute(self, task, trusted, context=None):
            self.received_memory = context.memory_context if context else None
            return super().execute(task, trusted, context)

    order = MemoryCaptureStub("order_logistics")
    policy = MemoryCaptureStub("policy")
    registry = AgentRegistry(policy=policy, order_logistics=order,
        after_sales=DeterministicStubAgent("after_sales"), product=DeterministicStubAgent("product"))
    context = _context(_trusted())
    context.memory_context = "UNVERIFIED SUMMARY; never use as order authority"
    output = invoke_supervisor(build_supervisor_graph(registry),
        _state(["get_order", "search_knowledge"]), context)
    assert order.received_memory == context.memory_context
    assert policy.received_memory == context.memory_context
    serialized_handoffs = repr(output["handoffs"])
    assert "UNVERIFIED SUMMARY" not in serialized_handoffs
    assert all(ref.startswith(("run:", "session:")) for handoff in output["handoffs"]
               for ref in handoff["context_refs"])
    assert output["agent_budget"]["agent_calls"] == 2
    assert output["agent_budget"]["write_executions"] == 0


def test_missing_or_mismatched_trusted_context_fails_before_agent_execution():
    registry = deterministic_stub_registry()
    graph = build_supervisor_graph(registry)
    missing = invoke_supervisor(graph, _state(["get_order"]), _context())
    assert missing["error_code"] == "AGENT_CONTEXT_INVALID"
    assert not registry.order_logistics.calls

    wrong = _trusted(run_id="other-run")
    mismatched = invoke_supervisor(graph, _state(["get_order"]), _context(wrong))
    assert mismatched["error_code"] == "AGENT_CONTEXT_INVALID"
    assert not registry.order_logistics.calls


def test_handoff_schema_rejects_untrusted_payloads_unknown_capability_and_status_mismatch():
    base = {"handoff_id": "h1", "source_agent": "supervisor", "target_agent": "policy",
        "task": "Retrieve and explain requested policy evidence", "required_capabilities": ["search_knowledge"],
        "context_refs": ["run:r1"], "evidence_refs": [], "status": "PENDING"}
    with pytest.raises(ValidationError):
        AgentHandoff.model_validate({**base, "required_capabilities": ["read_all_users"]})
    with pytest.raises(ValidationError):
        AgentHandoff.model_validate({**base, "task": "Please inspect this API_KEY=private value"})
    result = AgentResult(result_id="h1:result", source_agent="policy", status="STUB_COMPLETED")
    with pytest.raises(ValidationError):
        AgentHandoff.model_validate({**base, "status": "FAILED", "result": result.model_dump()})
    with pytest.raises(ValidationError):
        AgentTask(task_id="t1", target_agent="policy", task="query: private customer message",
            required_capabilities=["search_knowledge"], context_refs=["run:r1"], evidence_refs=[])
    with pytest.raises(ValidationError):
        AgentTask(task_id="t2", target_agent="policy", task="Retrieve and explain requested policy evidence",
            required_capabilities=["get_order"], context_refs=["run:r1"], evidence_refs=[])
    with pytest.raises(ValidationError):
        AgentHandoff.model_validate({**base, "status": "STUB_COMPLETED"})


def test_parent_graph_route_projection_drops_free_text_before_checkpoint():
    secret = "customer=private@example.test api_key=not-a-real-key"
    services = GraphServices(router=lambda _query: {
        "strategy": "AGENT", "safety_decision": "ALLOW", "reason_code": secret,
        "decision_source": "RULE", "intent_labels": ["live_business", secret],
        "required_capabilities": ["get_order", secret], "needs_clarification": False,
    }, direct=lambda _state: {}, single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    graph = build_orchestration_graph(services)
    output = graph.invoke(_state(["get_order"]), context=_context(_trusted()))
    route = output["route"]
    assert route["reason_code"] == "ROUTE_REASON_UNAVAILABLE"
    assert "intent_labels" not in route
    assert "required_capabilities" not in route
    assert secret not in repr(output)


class _ExplodingAgent:
    name = "order_logistics"

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def execute(self, task, trusted):
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("exception,code", [(TimeoutError(), "AGENT_TIMEOUT"),
                                              (RuntimeError("secret=should-not-log"), "AGENT_EXECUTION_FAILED")])
def test_agent_timeout_and_failure_are_recorded_without_fake_success(exception, code):
    agent = _ExplodingAgent(exception)
    registry = AgentRegistry(policy=PolicyAgent(), order_logistics=agent,
        after_sales=AfterSalesAgent(), product=ProductAgent())
    sink = InMemoryObservabilitySink()
    output = invoke_supervisor(build_supervisor_graph(registry, sink=sink),
        _state(["get_order"]), _context(_trusted()))
    assert output["terminal_status"] == "FAILED"
    assert output["error_code"] == code
    assert output.get("answer") is None
    assert output["tool_execution_status"][0]["executed"] is False
    assert all("should-not-log" not in item.sanitized_message for item in sink.logs)
    assert any(item.error_code == code for item in sink.spans)


def test_supervisor_call_and_step_budgets_terminate_four_specialists_without_cycles():
    registry = deterministic_stub_registry()
    caps = ["get_order", "search_products", "search_knowledge", "get_refund_status"]
    output = invoke_supervisor(build_supervisor_graph(registry), _state(caps), _context(_trusted()))
    assert output["supervisor_status"] == "STUB_ONLY"
    assert output["agent_budget"]["max_agent_calls"] == MAX_AGENT_CALLS == 4
    assert output["agent_budget"]["agent_calls"] == 4
    assert output["agent_budget"]["max_steps"] == MAX_SUPERVISOR_STEPS
    assert output["agent_budget"]["steps"] <= MAX_SUPERVISOR_STEPS
    assert output["agent_task_cursor"] == 4
    assert len(output["handoffs"]) == 4


def test_after_sales_write_capability_never_executes_and_requires_confirmation():
    output = invoke_supervisor(build_supervisor_graph(default_agent_registry()),
        _state(["create_refund_request"]), _context(_trusted()))
    assert output["terminal_status"] == "FAILED"
    assert output["error_code"] == "ACTION_CONFIRMATION_REQUIRED"
    assert output["tool_execution_status"][0]["status"] == "REQUIRES_CONFIRMATION"
    assert output["tool_execution_status"][0]["executed"] is False
    assert output["agent_budget"]["write_executions"] == 0


def test_graph_checkpoint_keeps_agent_projection_but_excludes_query_and_identity():
    registry = default_agent_registry()
    sink = InMemoryObservabilitySink()
    supervisor = build_supervisor_graph(registry, sink=sink)
    services = GraphServices(router=lambda _query: RouteDecision(strategy="AGENT", safety_decision="ALLOW",
            reason_code="LIVE_BUSINESS_FACT", decision_source="RULE", required_capabilities=["get_order"]).model_dump(),
        direct=lambda _state: {}, single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {},
        agent_supervisor=lambda state, context: invoke_supervisor(supervisor, state, context))
    saver = MemorySaver(serde=strict_serializer())
    graph = build_orchestration_graph(services, sink=sink, checkpointer=saver)
    input_state = graph_input_state(GraphInput(run_id="r-m3", session_id="s-m3", trace_id="t-m3",
        query="PRIVATE QUERY must not be checkpointed"), graph_version="g2-m3.1")
    context = _context(_trusted())
    config = {"configurable": {"thread_id": "r-m3", "checkpoint_ns": ""}}
    result = graph.invoke(input_state, context=context, config=config)
    assert result["terminal_status"] == "FAILED"
    assert result["error_code"] == "AGENT_NOT_IMPLEMENTED"
    assert result["supervisor_status"] == "BLOCKED"
    assert result["handoffs"][0]["status"] == "NOT_IMPLEMENTED"
    assert "AGENT_RESULT_NOT_USER_ANSWERABLE" not in result["answer_kind"] if result.get("answer_kind") else True
    assert len({span["span_id"] for span in result["spans"]}) == len(result["spans"])
    assert all(span["parent_span_id"] is None or span["parent_span_id"] in {item["span_id"] for item in result["spans"]}
               for span in result["spans"])
    snapshots = list(saver.list(config))
    stored = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
    assert "PRIVATE QUERY" not in stored and "owner-private" not in stored
    assert "query" not in stored and "user_id" not in stored


def test_agent_state_projection_maps_m3_fields_without_identity():
    agent_state = AgentState(trace_id="t", session_id="s", user_id="private-user", run_id="r", turn_id="r",
        run_status="FAILED", business_intent="LIVE_BUSINESS_FACT", current_task={"target_agent": "policy"},
        handoffs=[{"handoff_id": "h"}], tool_execution_status=[{"execution_id": "x"}],
        supervisor_status="BLOCKED")
    projected = project_agent_state(agent_state)
    assert projected["business_intent"] == "LIVE_BUSINESS_FACT"
    assert projected["handoffs"] == [{"handoff_id": "h"}]
    assert projected["supervisor_status"] == "BLOCKED"
    assert "user_id" not in projected and "private-user" not in repr(projected)
