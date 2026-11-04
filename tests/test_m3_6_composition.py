from __future__ import annotations

from app.m3.agents import AgentRegistry, AgentExecutionContext, AfterSalesAgent, OrderLogisticsAgent, PolicyAgent, ProductAgent
from app.m3.composition import combine_specialist_outputs
from app.m3.contracts import AgentResult, AgentTask, TrustedAgentContext
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.order_tools import ToolExecutionError
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state


def _trusted():
    return TrustedAgentContext(user_id="owner", session_id="s-m36", run_id="r-m36", trace_id="t-m36",
        requested_order_id="ORD-M36-001")


class _ReferenceAgent:
    def __init__(self, name: str, source: str):
        self.name = name
        self.source = source
        self.received_context_refs: list[list[str]] = []

    def execute(self, task, trusted, context):
        del trusted
        self.received_context_refs.append(list(task.context_refs))
        ref = f"tool_result:{self.name}-result"
        context.result_store[ref] = {"source": self.source, "order_id": "ORD-M36-001",
                                     "items": [{"product_id": "P-CUP"}]}
        return AgentResult(result_id=f"{task.task_id}:result", source_agent=self.name,
            status="COMPLETED", tool_result_refs=[ref])


def _run(capabilities):
    agents = {
        "order_logistics": _ReferenceAgent("order_logistics", "business_database"),
        "product": _ReferenceAgent("product", "shoprelay_product_catalog"),
        "policy": _ReferenceAgent("policy", "shoprelay_policy_retrieval"),
        "after_sales": _ReferenceAgent("after_sales", "shoprelay_after_sales_database"),
    }
    registry = AgentRegistry(policy=agents["policy"], order_logistics=agents["order_logistics"],
        product=agents["product"], after_sales=agents["after_sales"])
    state = graph_input_state(GraphInput(run_id="r-m36", session_id="s-m36", trace_id="t-m36",
        query="engineering fixture query"), graph_version="g2-m3.6")
    state.update({"strategy": "AGENT", "route": {"strategy": "AGENT", "reason_code": "M36_FIXED_SCENARIO",
        "required_capabilities": capabilities}, "terminal_status": "RUNNING"})
    context = GraphRuntimeContext(query="engineering fixture query", trusted_agent_context=_trusted(),
        tool_result_store={}, tool_execution_records=[])
    result = invoke_supervisor(build_supervisor_graph(registry, sink=InMemoryObservabilitySink()), state, context)
    return result, agents


def test_pair_and_four_agent_plans_are_minimal_ordered_and_bounded():
    pair, pair_agents = _run(["get_order_items", "get_product_detail"])
    assert [row["target_agent"] for row in pair["handoffs"]] == ["order_logistics", "product"]
    assert pair["agent_budget"]["agent_calls"] == 2
    product_refs = pair_agents["product"].received_context_refs[0]
    assert "tool_result:order_logistics-result" in product_refs

    all_agents, agents = _run(["get_order", "get_order_items", "get_product_detail",
        "search_knowledge", "get_refund_status"])
    assert [row["target_agent"] for row in all_agents["handoffs"]] == [
        "order_logistics", "product", "policy", "after_sales"]
    assert all_agents["agent_budget"]["agent_calls"] == 4
    assert all_agents["agent_budget"]["steps"] <= all_agents["agent_budget"]["max_steps"] == 14
    assert "tool_result:order_logistics-result" in agents["product"].received_context_refs[0]
    assert "tool_result:product-result" in agents["policy"].received_context_refs[0]
    after_refs = agents["after_sales"].received_context_refs[0]
    assert "tool_result:order_logistics-result" in after_refs
    assert "tool_result:policy-result" in after_refs


def test_composite_output_keeps_sources_and_policy_citations_without_claim_upgrade():
    order = {"answer": "订单来自数据库。", "answer_kind": "BUSINESS_FACTS",
        "business_results": {"status": "DELIVERED"}, "tool_references": [
            {"tool": "get_order", "source": "business_database", "result_ref": "tool_result:order"}],
        "tool_execution_status": [], "latency_ms": 3}
    product = {"answer": "目录价格与历史实付分开。", "answer_kind": "BUSINESS_FACTS",
        "business_results": {"catalog": {"price_cents": 9000}}, "tool_references": [
            {"tool": "get_product_detail", "source": "shoprelay_product_catalog", "result_ref": "tool_result:product"}],
        "tool_execution_status": [], "latency_ms": 4}
    policy = {"answer": "有效政策原文。", "answer_kind": "POLICY_ANSWER",
        "business_results": {"evidence_ids": ["p1"]}, "tool_references": [
            {"tool": "search_knowledge", "source": "shoprelay_policy_retrieval", "result_ref": "tool_result:policy"}],
        "citations": [{"chunk_id": "p1", "knowledge_version": "v1"}], "knowledge_version": "v1",
        "policy_retrieval": {"retrieval_mode": "BM25"}, "tool_execution_status": [], "latency_ms": 2}
    result = combine_specialist_outputs([("order_logistics", order), ("product", product), ("policy", policy)],
        tool_execution_status=[{"executed": True, "duration_ms": 9}])
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert "订单来自数据库" in result["answer"] and "目录价格" in result["answer"] and "有效政策原文" in result["answer"]
    assert result["citations"] == [{"chunk_id": "p1", "knowledge_version": "v1"}]
    assert len(result["tool_references"]) == 3
    assert result["business_results"]["order_logistics"] == {"status": "DELIVERED"}
    assert result["latency_ms"] == 9


def test_composite_rejects_duplicate_specialist_outputs():
    payload = {"answer": "x", "answer_kind": "BUSINESS_FACTS", "tool_references": [{"result_ref": "x"}]}
    assert combine_specialist_outputs([("product", payload), ("product", payload)],
        tool_execution_status=[]) is None


class _ProductRepository:
    def __init__(self):
        self.requested_ids = []

    def get_product_detail(self, product_id):
        self.requested_ids.append(product_id)
        if product_id != "P-CUP":
            raise ToolExecutionError("PRODUCT_NOT_FOUND", status="NOT_FOUND")
        return {"availability": "AVAILABLE", "product": {"product_id": product_id},
                "source": "shoprelay_product_catalog"}


def test_product_agent_uses_only_owned_order_item_reference_for_missing_product_id():
    repository = _ProductRepository()
    agent = ProductAgent(repository)
    task = AgentTask(task_id="product-task", target_agent="product",
        task="Resolve requested live product capability", required_capabilities=["get_product_detail"],
        context_refs=["run:r1", "session:s1", "tool_result:owned-items"], evidence_refs=[])
    store = {"tool_result:owned-items": {"source": "business_database",
        "items": [{"product_id": "P-CUP"}]}}
    context = AgentExecutionContext(result_store=store, sink=InMemoryObservabilitySink(), run_id="r1",
        trace_id="t1", parent_span_id=None, user_query="查询我买过的商品详情")
    result = agent.execute(task, _trusted(), context)
    assert result.status == "COMPLETED" and repository.requested_ids == ["P-CUP"], (result.model_dump(), repository.requested_ids)


def test_product_agent_does_not_guess_when_owned_order_has_multiple_products():
    repository = _ProductRepository()
    agent = ProductAgent(repository)
    task = AgentTask(task_id="product-task", target_agent="product",
        task="Resolve requested live product capability", required_capabilities=["get_product_detail"],
        context_refs=["run:r1", "session:s1", "tool_result:owned-items"], evidence_refs=[])
    store = {"tool_result:owned-items": {"source": "business_database",
        "items": [{"product_id": "P-CUP"}, {"product_id": "P-KETTLE"}]}}
    context = AgentExecutionContext(result_store=store, sink=InMemoryObservabilitySink(), run_id="r1",
        trace_id="t1", parent_span_id=None, user_query="查询我买过的商品详情")
    result = agent.execute(task, _trusted(), context)
    assert result.status == "COMPLETED" and result.missing_information == ["PRODUCT_ID_AMBIGUOUS"], (result.model_dump(), repository.requested_ids)
    assert repository.requested_ids == []
