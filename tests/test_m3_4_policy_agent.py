from __future__ import annotations

from datetime import datetime, timezone
from contextlib import asynccontextmanager
from dataclasses import replace
import secrets

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run
from app.m1.services import set_demo_password
from app.m2.retrieval import RetrievalConfig, RetrievalService
from app.m3.agents import AgentRegistry, AfterSalesAgent, OrderLogisticsAgent, PolicyAgent, ProductAgent
from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.order_tools import AgentExecutionContext, ToolExecutionError
from app.m3.policy_tools import (PolicyReadService, build_policy_business_result,
                                 evidence_is_valid)
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.adapters import GraphServices
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state


def _trusted():
    return TrustedAgentContext(user_id="user-a", session_id="s-policy", run_id="r-policy",
        trace_id="t-policy", authenticated=True, run_owner_verified=True, session_owner_verified=True)


def _task(capabilities=("search_knowledge",)):
    return AgentTask(task_id="policy-task", target_agent="policy",
        task="Retrieve and explain requested policy evidence", required_capabilities=list(capabilities),
        context_refs=["run:r-policy", "session:s-policy"], evidence_refs=[])


def test_policy_read_uses_real_shared_retrieval_and_returns_verified_current_evidence():
    retrieval = RetrievalService(config=RetrievalConfig(mode="bm25"))
    service = PolicyReadService(retrieval)
    payload = service.search("退款申请 退货申请 已批准", result_ref="tool_result:policy-test")
    assert payload["source"] == "shoprelay_policy_retrieval"
    assert payload["retrieval_mode"] == "BM25"
    assert payload["knowledge_version"]
    assert payload["index_fingerprint"]
    assert payload["evidence"]
    assert all(evidence_is_valid(row, fingerprint=payload["index_fingerprint"],
        knowledge_version=payload["knowledge_version"], evaluation_at=payload["evaluation_at"])
        for row in payload["evidence"])


def test_policy_read_rejects_unregistered_retrieval_mode():
    retrieval = RetrievalService(config=RetrievalConfig(mode="bm25"))
    with pytest.raises(ToolExecutionError, match="POLICY_RETRIEVAL_MODE_NOT_ALLOWED"):
        PolicyReadService(retrieval).search("政策问题", mode="OTHER", result_ref="tool_result:invalid-mode")


def test_policy_agent_supervisor_produces_traceable_bounded_policy_answer():
    sink = InMemoryObservabilitySink()
    retrieval = RetrievalService(config=RetrievalConfig(mode="bm25"))
    registry = AgentRegistry(policy=PolicyAgent(PolicyReadService(retrieval)),
        order_logistics=OrderLogisticsAgent(), after_sales=AfterSalesAgent(), product=ProductAgent())
    graph = build_supervisor_graph(registry, sink=sink)
    result_store: dict[str, dict] = {}
    calls: list[dict] = []
    context = AgentExecutionContext(result_store=result_store, sink=sink, run_id="r-policy",
        trace_id="t-policy", parent_span_id=None, user_query="退款申请需要满足什么条件？",
        graph_version="g2-m3.4", retrieval_service=retrieval, call_records=calls)
    state = graph_input_state(GraphInput(run_id="r-policy", session_id="s-policy", trace_id="t-policy",
        query="退款申请需要满足什么条件？"), graph_version="g2-m3.4")
    state.update({"strategy": "AGENT", "route": {"strategy": "AGENT", "reason_code": "POLICY_AGENT",
        "required_capabilities": ["search_knowledge"]}, "terminal_status": "RUNNING"})
    context.route_payload = state["route"]
    context.trusted_agent_context = _trusted()
    output = invoke_supervisor(graph, state, GraphRuntimeContext(query=context.user_query,
        trusted_agent_context=_trusted(), m3_supervisor_enabled=True,
        tool_result_store=result_store, tool_execution_records=calls, retrieval_service=retrieval))
    business = build_policy_business_result(output, route=state["route"], result_store=result_store)
    assert output["supervisor_status"] == "COMPLETED", repr(output)
    assert output["handoffs"][0]["target_agent"] == "policy"
    assert output["handoffs"][0]["status"] == "COMPLETED"
    assert business and business["answer_kind"] == "POLICY_ANSWER"
    assert business["citations"] and business["knowledge_version"]
    assert all(item["executed"] for item in output["tool_execution_status"])
    assert calls[0]["retrieval_mode"] == "BM25"
    assert any(span.node_name == "m3.tool.search_knowledge" for span in sink.spans)
    assert "退款申请需要满足什么条件" not in repr(output)


def test_policy_answer_rejects_evidence_from_wrong_snapshot_or_expired_interval():
    row = {"chunk_id": "c1", "version": "v1", "policy_version": "v1",
        "knowledge_version": "k1", "index_fingerprint": "fp1",
        "effective_from": "2025-01-01T00:00:00Z", "effective_to": "2025-02-01T00:00:00Z"}
    assert not evidence_is_valid(row, fingerprint="fp1", knowledge_version="k1",
        evaluation_at="2025-03-01T00:00:00Z")
    row["effective_to"] = None
    assert not evidence_is_valid(row, fingerprint="other", knowledge_version="k1",
        evaluation_at="2025-01-20T00:00:00Z")


def test_policy_agent_rejects_unimplemented_policy_capabilities_and_untrusted_context():
    retrieval = RetrievalService(config=RetrievalConfig(mode="bm25"))
    agent = PolicyAgent(PolicyReadService(retrieval))
    context = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(), run_id="r-policy",
        trace_id="t-policy", parent_span_id=None, user_query="查询优惠券")
    unsupported = agent.execute(_task(("get_coupon",)), _trusted(), context)
    assert unsupported.status == "FAILED" and unsupported.error_code == "TOOL_NOT_ALLOWED"
    untrusted = TrustedAgentContext(user_id="user-a", session_id="s-policy", run_id="r-policy",
        trace_id="t-policy", authenticated=False, run_owner_verified=True, session_owner_verified=True)
    denied = agent.execute(_task(), untrusted, context)
    assert denied.status == "FAILED" and denied.error_code == "AGENT_CONTEXT_INVALID"


def test_policy_agent_respects_bm25_only_retrieval_config_for_cross_policy_route():
    class SpyRetrieval:
        config = RetrievalConfig(mode="bm25")
        def pin_view(self, evaluation_at=None):
            class View:
                fingerprint = "fp1"
                evaluation_at = "2026-10-22T00:00:00Z"
                index = {"knowledge_version": "k1"}
            return View()
        def retrieve(self, query, *, mode, view):
            observed.append(mode)
            return {"knowledge_version": "k1", "index_fingerprint": "fp1", "config_version": "v",
                "config_hash": "h", "degraded": False, "failures": [], "latency_ms": 1,
                "evidence": [{"chunk_id": "c1", "version": "v1", "policy_version": "v1",
                    "knowledge_version": "k1", "index_fingerprint": "fp1",
                    "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
                    "title": "政策", "content": "条款原文", "rank": 1}]}
    observed = []
    store = {}
    agent = PolicyAgent(PolicyReadService(SpyRetrieval()))
    context = AgentExecutionContext(result_store=store, sink=InMemoryObservabilitySink(),
        run_id="r-policy", trace_id="t-policy", parent_span_id=None,
        user_query="退货和退款规则如何衔接？", route_payload={"sub_strategy": "AGENT+MULTI"})
    result = agent.execute(_task(), _trusted(), context)
    assert result.status == "COMPLETED"
    # AGENT+MULTI does not expand the configured retrieval backend. A BM25-only
    # personal database must not silently invoke a reranker or embedding path.
    assert observed == ["bm25"]


def test_missing_policy_evidence_is_a_safe_clarification_not_generated_policy():
    class EmptyRetrieval:
        config = RetrievalConfig(mode="bm25")
        def pin_view(self, evaluation_at=None):
            class View:
                fingerprint = "fp-empty"
                evaluation_at = "2026-10-22T00:00:00Z"
                index = {"knowledge_version": "k-empty"}
            return View()
        def retrieve(self, query, *, mode, view):
            return {"source": "shoprelay_policy_retrieval", "result_ref": "tool_result:empty",
                "retrieval_mode": "BM25", "retrieval_count": 1, "knowledge_version": "k-empty",
                "index_fingerprint": "fp-empty", "evaluation_at": view.evaluation_at,
                "evidence": [], "degraded": False, "failures": [], "latency_ms": 0,
                "config_version": "test", "config_hash": "test"}
    retrieval = EmptyRetrieval()
    payload = PolicyReadService(retrieval).search("无关问题", result_ref="tool_result:empty")
    assert payload["evidence"] == []
    result_store = {"tool_result:empty": payload}
    agent_result = {"source_agent": "policy", "status": "COMPLETED",
        "tool_result_refs": ["tool_result:empty"], "missing_information": ["POLICY_EVIDENCE_NOT_FOUND"]}
    supervisor = {"supervisor_status": "COMPLETED", "terminal_status": "RUNNING",
        "handoffs": [{"target_agent": "policy", "status": "COMPLETED",
            "required_capabilities": ["search_knowledge"], "result": agent_result}],
        "tool_execution_status": [{"capability": "search_knowledge", "executed": True,
                                    "status": "NO_EVIDENCE"}]}
    result = build_policy_business_result(supervisor,
        route={"required_capabilities": ["search_knowledge"]}, result_store=result_store)
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert "不能给出确定的政策结论" in result["answer"]


def test_policy_agent_real_retrieval_end_to_end_api_sqlite(tmp_path, monkeypatch):
    import app.m1.api as api_module
    path = tmp_path / "m3.4-policy.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    password = "m34-demo-" + secrets.token_urlsafe(12)
    with factory() as db:
        set_demo_password(db, "u_demo", password)
    loader = api_module.load_settings
    def m3_settings(**_kwargs):
        return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "g2-m3.4",
            "SHOPRELAY_EXECUTION_VERSION": "m3.4-policy-v1"}, local_values={}, require_api_key=False),
            m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", m3_settings)
    services = GraphServices(router=lambda _q: {"strategy": "AGENT", "sub_strategy": None,
        "reason_code": "POLICY_AGENT", "safety_decision": "ALLOW", "decision_source": "RULE",
        "required_capabilities": ["search_knowledge"], "needs_clarification": False},
        direct=lambda _s: {}, single=lambda _s: {}, multi=lambda _s: {}, refuse=lambda _s: {})
    saver = MemorySaver()
    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield saver
    app = create_app(database_url=url, engine=engine, orchestrator_mode="langgraph",
        checkpointer_factory=checkpoint_factory, checkpointer_readiness=lambda *_: True,
        graph_services=services)
    try:
        with TestClient(app) as client:
            csrf = client.get("/api/demo/csrf").json()["csrf_token"]
            login = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
                CSRF_HEADER: csrf}, json={"username": "u_demo", "password": password})
            assert login.status_code == 200
            headers = {"Origin": str(client.base_url).rstrip("/"),
                CSRF_HEADER: client.cookies.get("shoprelay_csrf")}
            response = client.post("/api/chat", headers=headers, json={
                "message": "退款申请需要满足什么条件？", "client_request_id": "m34-policy-real-1"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "SUCCEEDED" and body["strategy"] == "AGENT"
            assert body["answer_kind"] == "POLICY_ANSWER"
            assert body["citations"] and body["knowledge_version"]
            assert body["tool_references"][0]["source"] == "shoprelay_policy_retrieval"
            assert all(row["chunk_id"] in body["business_results"]["evidence_ids"]
                       for row in body["citations"])
            run_id = body["run_id"]
            event_types = [row["type"] for row in client.get(f"/api/runs/{run_id}/events").json()["events"]]
            assert event_types == ["started", "route_decided", "agent_started", "tool_started",
                "policy_retrieval_started", "retrieval_hop", "policy_retrieval_finished",
                "tool_finished", "answer", "done"]
            replay = client.post("/api/chat", headers=headers, json={
                "message": "退款申请需要满足什么条件？", "client_request_id": "m34-policy-real-1"})
            assert replay.status_code == 200 and replay.json()["run_id"] == run_id
            assert [row["type"] for row in client.get(f"/api/runs/{run_id}/events").json()["events"]].count("tool_started") == 1
            with factory() as db:
                run = db.get(Run, run_id)
                execution = db.get(GraphExecutionRecord, run_id)
                spans = [__import__("json").loads(row.payload_json) for row in db.execute(select(
                    ObservabilityRecord).where(ObservabilityRecord.run_id == run_id,
                    ObservabilityRecord.record_type == "span")).scalars()]
                tool_spans = [row for row in spans if row["node_name"] == "m3.tool.search_knowledge"]
                assert run.status == "SUCCEEDED" and execution.status == "SUCCEEDED"
                assert execution.graph_version == "g2-m3.4"
                assert execution.checkpoint_id and len(tool_spans) == 1 and tool_spans[0]["status"] == "OK"
                log_rows = [row.payload_json for row in db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run_id, ObservabilityRecord.record_type == "log")).scalars()]
                assert all("退款申请需要满足" not in value for value in log_rows)
            checkpoint_text = repr([item.checkpoint.get("channel_values", {}) for item in saver.list(
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})])
            assert "退款申请需要满足" not in checkpoint_text
            assert "退货申请需要满足" not in checkpoint_text
    finally:
        engine.dispose()
