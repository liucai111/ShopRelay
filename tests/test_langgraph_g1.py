from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.m1.routing import RouteDecision
from app.orchestration.adapters import GraphServices, make_legacy_services
from app.orchestration.graph import build_orchestration_graph, invoke_graph
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.state import GraphInput, graph_input_state, merge_by_key, project_agent_state


def route(strategy: str, *, sub_strategy=None, usage=None, decision_source="RULE"):
    return RouteDecision(strategy=strategy, sub_strategy=sub_strategy,
        safety_decision="REFUSE" if strategy == "REFUSE" else "ALLOW",
        reason_code="FIXED_STUB", decision_source=decision_source, usage=usage).model_dump(mode="json")


def services_for(strategy, payload=None, *, calls=None):
    calls = calls if calls is not None else []
    def execute(name):
        def fn(state):
            calls.append((name, state["query"]))
            return dict(payload or {"answer": name, "answer_kind": name.upper(), "citations": [], "evidence": []})
        return fn
    return GraphServices(router=lambda query: (calls.append(("router", query)) or route(strategy)),
        direct=execute("direct"), single=execute("single"), multi=execute("multi"), refuse=execute("refuse"))


@pytest.mark.parametrize("strategy,node", [
    ("DIRECT", "direct"), ("SINGLE", "single"), ("MULTI", "multi"),
    ("REFUSE", "refuse"), ("AGENT", "agent"),
])
def test_each_strategy_has_one_router_and_one_terminal_branch(strategy, node):
    calls = []
    sink = InMemoryObservabilitySink()
    graph = build_orchestration_graph(services_for(strategy, calls=calls), sink=sink)
    state = invoke_graph(graph, {"run_id": "r1", "session_id": "s1", "trace_id": "t1", "query": "q"})
    assert calls[0] == ("router", "q")
    if strategy == "AGENT":
        assert state["terminal_status"] == "FAILED"
        assert state["error_code"] == "ROUTE_EXECUTOR_UNAVAILABLE"
        assert state.get("answer") is None
        assert len(calls) == 1
    else:
        assert calls[1][0] == node
        assert state["terminal_status"] == "SUCCEEDED"
        assert len(calls) == 2
    assert [span.node_name for span in sink.spans] == ["router", node]
    assert sink.spans[1].parent_span_id == sink.spans[0].span_id


def test_multi_state_preserves_evidence_version_grade_stop_and_does_not_duplicate_calls():
    evidence = [{"chunk_id": "p1", "version": "v2"}]
    result = {"answer": "policy", "answer_kind": "POLICY_ANSWER", "evidence": evidence,
              "citations": evidence, "knowledge_version": "kb-v2", "grade_result": {"sufficient": True},
              "stop_reason": "SUFFICIENT_EVIDENCE", "budget": {"requests": 2}, "retrieval_hop_count": 1}
    calls = []
    state = invoke_graph(build_orchestration_graph(services_for("MULTI", result, calls=calls)),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q", "knowledge_version": "kb-v2"})
    assert state["strategy"] == "MULTI"
    assert state["sub_strategy"] is None
    assert state["evidence"] == evidence
    assert state["citations"] == evidence
    assert state["knowledge_version"] == "kb-v2"
    assert state["grade_result"] == {"sufficient": True}
    assert state["stop_reason"] == "SUFFICIENT_EVIDENCE"
    assert calls == [("router", "q"), ("multi", "q")]


def test_multi_checkpoint_projection_keeps_bounded_policy_intent_but_drops_user_fact_payloads():
    result = {"answer": "policy", "answer_kind": "POLICY_ANSWER", "evidence": [], "citations": [],
        "policy_retrieval": {"version": "m2-r-bounded-policy-v1", "request_mode": "POLICY_EXPLANATION",
            "requested_aspects": [{"aspect_id": "refund", "text": "退款申请前置条件", "query_anchor": "退款"}],
            "user_provided_facts": [{"text": "用户说订单昨天签收", "source_excerpt": "昨天签收"}],
            "plan_raw_output": "private raw response", "index_fingerprint": "fp"},
        "terminal_status": "SUCCEEDED"}
    state = invoke_graph(build_orchestration_graph(services_for("MULTI", result)),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "退款前置条件是什么？"})
    details = state["policy_retrieval"]
    assert details["request_mode"] == "POLICY_EXPLANATION"
    assert details["requested_aspects"] == [{"aspect_id": "refund"}]
    assert "user_provided_facts" not in details and "plan_raw_output" not in details
    assert details["index_fingerprint"] == "fp"


def test_langgraph_failure_messages_cover_m2r_budget_and_schema_failures():
    from app.orchestration.runtime import _SAFE_FAILURES

    required = {"MODEL_BUDGET_EXCEEDED", "POLICY_ASPECT_PLANNING_UNAVAILABLE",
        "POLICY_ASPECT_PLAN_INVALID", "POLICY_ASPECT_LIMIT_EXCEEDED",
        "RETRIEVAL_EVIDENCE_VERSION_INVALID", "ANSWER_ASPECT_CITATION_MISSING",
        "RETRIEVAL_BUDGET_EXCEEDED", "ACTION_EXECUTION_NOT_SUPPORTED"}
    assert required.issubset(_SAFE_FAILURES)
    assert all("API_KEY" not in message and "DATABASE_URL" not in message
               for message in _SAFE_FAILURES.values())


@pytest.mark.parametrize("hops,sufficient,reason", [
    (1, True, "SUFFICIENT_EVIDENCE"),
    (2, True, "SUFFICIENT_EVIDENCE"),
    (3, True, "SUFFICIENT_EVIDENCE"),
    (3, False, "MAX_HOPS_REACHED"),
])
def test_multi_adapter_result_projection_preserves_one_to_three_hop_termination(hops, sufficient, reason):
    result = {"answer": "answer" if sufficient else "clarify", "answer_kind": "POLICY_ANSWER",
              "evidence": [], "citations": [], "stop_reason": reason,
              "retrieval_hop_count": hops, "terminal_status": "SUCCEEDED"}
    state = invoke_graph(build_orchestration_graph(services_for("MULTI", result)),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q"})
    assert state["stop_reason"] == reason
    assert state["retrieval_hop_count"] == hops
    assert state["terminal_status"] == "SUCCEEDED"


@pytest.mark.parametrize("strategy,executor", [("SINGLE", "single"), ("MULTI", "multi")])
def test_executor_exception_is_failure_without_success_answer(strategy, executor):
    def fail(_state):
        raise RuntimeError("API_KEY=private-value should never reach logs")
    svc = services_for(strategy)
    setattr(svc, executor, fail)
    sink = InMemoryObservabilitySink()
    state = invoke_graph(build_orchestration_graph(svc, sink=sink),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q"})
    assert state["terminal_status"] == "FAILED"
    assert state["error_code"] == "NODE_EXECUTION_FAILED"
    assert state["answer"] is None
    assert "private-value" not in str(sink.logs)
    assert sink.spans[-1].status == "ERROR"


def test_agent_state_projection_never_carries_authorization_identity():
    projected = project_agent_state({"run_id": "r", "session_id": "s", "trace_id": "t", "user_id": "owner",
        "run_status": "SUCCEEDED", "strategy": "SINGLE", "policy_evidence": [{"chunk_id": "x"}],
        "final_answer": "answer", "errors": []})
    assert projected["run_id"] == "r"
    assert projected["evidence"] == [{"chunk_id": "x"}]
    assert "user_id" not in projected
    with pytest.raises(ValidationError):
        GraphInput.model_validate({"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q", "user_id": "forged"})


def test_reducers_deduplicate_evidence_and_spans_by_stable_id():
    reducer = merge_by_key("chunk_id")
    assert reducer([{"chunk_id": "p1", "v": 1}], [{"chunk_id": "p1", "v": 2}, {"chunk_id": "p2"}]) == [
        {"chunk_id": "p1", "v": 2}, {"chunk_id": "p2"}]


def test_observability_has_valid_span_tree_metrics_and_no_raw_error_text():
    sink = InMemoryObservabilitySink()
    state = invoke_graph(build_orchestration_graph(services_for("DIRECT"), sink=sink),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q"})
    assert len(state["spans"]) == 2
    assert sink.spans[1].parent_span_id == sink.spans[0].span_id
    assert all(span.duration_ms >= 0 and span.start_time and span.end_time for span in sink.spans)
    assert sum(metric.node_execution_count for metric in sink.metrics) == 2
    assert all(log.trace_id == "t" and log.run_id == "r" for log in sink.logs)
    assert all(log.sanitized_message for log in sink.logs)


def test_legacy_stub_comparison_preserves_result_shape_and_failure_reason():
    for strategy, expected in [("DIRECT", "DIRECT"), ("SINGLE", "POLICY_ANSWER"),
                               ("MULTI", "CLARIFICATION_REQUIRED"), ("REFUSE", "REFUSAL")]:
        payload = {"answer": "fixed", "answer_kind": expected, "citations": [], "evidence": [],
                   "stop_reason": "BUDGET_EXCEEDED" if strategy == "MULTI" else None,
                   "terminal_status": "SUCCEEDED"}
        state = invoke_graph(build_orchestration_graph(services_for(strategy, payload)),
            {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "fixed input"})
        assert (state["answer"], state["answer_kind"], state["stop_reason"], state["terminal_status"]) == (
            "fixed", expected, payload["stop_reason"], "SUCCEEDED")


def test_bad_route_fails_closed_without_executor_or_answer():
    calls = []
    svc = services_for("SINGLE", calls=calls)
    svc.router = lambda _query: {"strategy": "NOT_A_ROUTE"}
    state = invoke_graph(build_orchestration_graph(svc),
        {"run_id": "r", "session_id": "s", "trace_id": "t", "query": "q"})
    assert state["terminal_status"] == "FAILED"
    assert state["answer"] is None
    assert state["strategy"] is None
    assert calls == []


def test_legacy_adapter_reuses_current_router_and_direct_policy():
    adapters = make_legacy_services()
    assert adapters.router("你好") ["strategy"] == "DIRECT"
    assert adapters.router("政策里写了七天退货，能否证明我的订单昨天签收？")["strategy"] == "SINGLE"
    assert adapters.router("查询订单 SR-ORD-1 当前物流状态")["strategy"] == "AGENT"
    direct = adapters.direct({"route": {"clarification_question": "请补充订单号。"}})
    assert direct["answer"] == "请补充订单号。"


def test_legacy_multi_adapter_uses_bounded_policy_executor(monkeypatch):
    from app.orchestration import adapters
    from app.m2 import policy_execution

    seen = {}
    def execute(question, **kwargs):
        seen["question"] = question
        seen["route"] = kwargs["route"]
        return {"answer": "受证据支持的答复", "answer_kind": "POLICY_ANSWER", "terminal_status": "SUCCEEDED",
                "policy_retrieval": {"version": "m2-r-bounded-policy-v1"}, "model_request_count": 2}
    monkeypatch.setattr(policy_execution, "execute_policy_request", execute)
    adapters_obj = make_legacy_services(provider=object(), retrieval_service=object())
    route = {"strategy": "MULTI", "decision_source": "LLM", "model_id": "model-x",
             "usage": {"prompt_tokens": 1}}
    state = adapters_obj.multi({"query": "跨政策问题", "route": route})
    assert seen["question"] == "跨政策问题"
    assert seen["route"] == route
    assert state["terminal_status"] == "SUCCEEDED"
    assert state["policy_retrieval"]["version"] == "m2-r-bounded-policy-v1"
    assert "self_rag" not in state and "grade_result" not in state
