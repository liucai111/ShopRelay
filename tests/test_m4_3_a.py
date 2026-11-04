from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import inspect, text
from alembic import command

from app.m1.db import alembic_config, upgrade_database
from app.m1.models import Base, Coupon
from app.m3.agents import AgentRegistry, AfterSalesAgent, OrderLogisticsAgent, PolicyAgent, ProductAgent
from app.m3.after_sales_tools import AfterSalesReadRepository, build_after_sales_business_result
from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.order_tools import AgentExecutionContext, SqlAlchemyOrderReadRepository
from app.m3.policy_tools import CouponReadRepository, PolicyReadService
from app.m3.product_tools import ProductReadRepository
from app.mcp import McpServerTrust, McpToolGrant, TrustedMcpClient
from app.mcp.adapters import McpCouponReadRepository
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state
from app.skills.registry import SKILL_REGISTRY, bind_skill, validate_skill_execution
from app.skills.runtime import run_selected_skill
from app.tools.catalog import TOOL_CATALOG, get_tool_contract


def _url(tmp_path):
    return f"sqlite:///{(tmp_path / 'm43a.sqlite').as_posix()}"


def test_fifteen_tool_contracts_match_implementation_and_keep_writes_closed():
    assert len(TOOL_CATALOG) == 15
    assert all(row.input_schema["type"] == "object" and row.output_schema["type"] == "object"
               for row in TOOL_CATALOG.values())
    assert get_tool_contract("get_coupon").implementation_status == "NATIVE_AND_MCP_READ_IMPLEMENTED_METADATA_ONLY"
    assert get_tool_contract("get_warranty").implementation_status == "POLICY_EVIDENCE_ONLY"
    for name in ("create_refund_request", "create_return_request", "cancel_order", "escalate_to_human"):
        row = get_tool_contract(name)
        assert row.mcp_allowed is False and row.confirmation_required
    from app.m4.actions import WRITE_POLICIES
    assert WRITE_POLICIES["get_coupon"].risk_level == "READ_ONLY"
    assert WRITE_POLICIES["get_warranty"].risk_level == "READ_ONLY"
    assert WRITE_POLICIES["escalate_to_human"].risk_level == "WRITE_LOW_RISK"
    assert WRITE_POLICIES["escalate_to_human"].execution_mode == "ISOLATED_TICKET_TRANSACTION"
    assert get_tool_contract("escalate_to_human").implementation_status == "ISOLATED_CONFIRMATION_TRANSACTION"


def test_skill_selection_is_versioned_and_cannot_widen_route_or_execute_writes():
    route = {"strategy": "AGENT", "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]}
    selected = bind_skill("请解释订单退货和退款条件", route)
    assert selected["skill_id"] == "return_handling"
    bound = {**route, **selected}
    skill = validate_skill_execution(bound, query="请解释订单退货和退款条件")
    assert skill.version == "1.0.0"
    assert skill.input_schema["required"] == ["query", "required_capabilities"]
    assert len(skill.output_schema["oneOf"]) == 2
    assert skill.output_schema["oneOf"][0]["required"] == ["supervisor_status", "agent_budget", "handoffs"]
    assert skill.output_schema["oneOf"][1]["required"] == ["supervisor_status", "error_code"]
    assert skill.max_agent_calls == 4 and skill.max_steps == 14
    assert skill.required_permissions == ("business:read",)
    with pytest.raises(ValueError, match="SKILL_TOOL_SCOPE_VIOLATION"):
        validate_skill_execution({**bound, "required_capabilities": ["cancel_order"]})
    with pytest.raises(ValueError, match="SKILL_VERSION_MISMATCH"):
        validate_skill_execution({**bound, "skill_version": "9.9.9"})
    cancellation = bind_skill("取消订单后如何退款", {"strategy": "AGENT",
        "required_capabilities": ["get_order", "get_refund_status", "search_knowledge"]})
    assert cancellation["skill_id"] == "order_cancellation"
    assert cancellation["skill_proposable_tools"] == ["cancel_order"]
    escalation = bind_skill("请转人工处理", {"strategy": "AGENT",
        "required_capabilities": ["escalate_to_human"]})
    assert escalation["skill_id"] == "human_escalation"
    assert escalation["skill_allowed_tools"] == list(SKILL_REGISTRY["human_escalation"].allowed_tools)
    assert escalation["skill_proposable_tools"] == ["escalate_to_human"]
    validate_skill_execution({"strategy": "AGENT", "required_capabilities": ["escalate_to_human"], **escalation})
    assert "return_handling" in SKILL_REGISTRY
    assert len(SKILL_REGISTRY) == 6  # specialist skills plus constrained generic-read fallback


def test_skill_runtime_records_span_and_enforces_existing_budget():
    sink = InMemoryObservabilitySink()
    route = {"strategy": "AGENT", "required_capabilities": ["get_order", "track_shipment"],
             **bind_skill("查询订单 ORD-TEST 的物流", {"strategy": "AGENT",
                 "required_capabilities": ["get_order", "track_shipment"]})}
    good_output = {"supervisor_status": "COMPLETED", "handoffs": [],
        "agent_budget": {"agent_calls": 1, "steps": 4}}
    result = run_selected_skill(route, lambda: good_output, query="查询订单 ORD-TEST 的物流",
        sink=sink, run_id="r-skill", trace_id="t-skill", graph_version="m4.3-a")
    assert result["agent_budget"]["agent_calls"] == 1
    assert sink.spans[0].node_name == "skill.order_tracking"
    bad = run_selected_skill(route, lambda: {**good_output,
        "agent_budget": {"agent_calls": 5, "steps": 15}}, query="查询订单 ORD-TEST 的物流",
        sink=sink, run_id="r-skill", trace_id="t-skill", graph_version="m4.3-a")
    assert bad["error_code"] == "SKILL_BUDGET_EXCEEDED"
    malformed = run_selected_skill(route, lambda: {"supervisor_status": "COMPLETED"},
        query="查询订单 ORD-TEST 的物流", sink=sink, run_id="r-skill", trace_id="t-skill",
        graph_version="m4.3-a")
    assert malformed["error_code"] == "SKILL_OUTPUT_SCHEMA_INVALID"
    blocked_output = {"supervisor_status": "BLOCKED", "terminal_status": "FAILED",
        "error_code": "ORDER_NOT_FOUND", "handoffs": [], "agent_budget": {"agent_calls": 1, "steps": 4}}
    blocked = run_selected_skill(route, lambda: blocked_output, query="查询订单 ORD-TEST 的物流",
        sink=sink, run_id="r-skill", trace_id="t-skill", graph_version="m4.3-a")
    assert blocked["error_code"] == "ORDER_NOT_FOUND"
    early_failure = {"supervisor_status": "FAILED", "terminal_status": "FAILED",
        "error_code": "DATABASE_UNAVAILABLE"}
    failed = run_selected_skill(route, lambda: early_failure, query="查询订单 ORD-TEST 的物流",
        sink=sink, run_id="r-skill", trace_id="t-skill", graph_version="m4.3-a")
    assert failed == early_failure
    assert sink.spans[-1].error_code == "DATABASE_UNAVAILABLE"


@pytest.mark.parametrize("existing_coupon_table", [True, False])
def test_coupon_adoption_migration_preserves_legacy_data_and_downgrades_safely(tmp_path,
                                                                               existing_coupon_table):
    database_url = _url(tmp_path)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('20261022_10')"))
        if existing_coupon_table:
            connection.execute(text("CREATE TABLE coupons (coupon_code VARCHAR(64) PRIMARY KEY, "
                "policy_id VARCHAR(128) NOT NULL, version VARCHAR(64) NOT NULL, min_spend_cents INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO coupons VALUES ('OLD-SAVE', 'p-old', 'v1', 1500)"))
    command.upgrade(alembic_config(database_url), "head")
    assert "coupons" in inspect(engine).get_table_names()
    assert "source_type" in {row["name"] for row in inspect(engine).get_columns("coupons")}
    with engine.connect() as connection:
        if existing_coupon_table:
            assert connection.execute(text("SELECT policy_id, version, min_spend_cents FROM coupons "
                "WHERE coupon_code='OLD-SAVE'")).one() == ("p-old", "v1", 1500)
            assert connection.execute(text("SELECT change_type FROM _m43_coupon_catalog_migration_marker "
                "WHERE revision='20261022_11'")).scalar_one() == "ADDED_SOURCE_TYPE"
        else:
            assert connection.execute(text("SELECT change_type FROM _m43_coupon_catalog_migration_marker "
                "WHERE revision='20261022_11'")).scalar_one() == "CREATED_TABLE"
    command.downgrade(alembic_config(database_url), "20261022_10")
    tables = set(inspect(engine).get_table_names())
    assert "_m43_coupon_catalog_migration_marker" not in tables
    if existing_coupon_table:
        assert "coupons" in tables
        assert "source_type" not in {row["name"] for row in inspect(engine).get_columns("coupons")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT policy_id, version, min_spend_cents FROM coupons "
                "WHERE coupon_code='OLD-SAVE'")).one() == ("p-old", "v1", 1500)
    else:
        assert "coupons" not in tables
    engine.dispose()


def test_coupon_catalog_reports_metadata_not_current_eligibility(tmp_path):
    engine = create_engine(_url(tmp_path))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(Coupon(coupon_code="SAVE-26", policy_id="coupon", version="2026-01", min_spend_cents=5000,
                       source_type="SIMULATED_TEST_DATA"))
    repository = CouponReadRepository(factory)
    found = repository.get_coupon("SAVE-26")
    missing = repository.get_coupon("NO-COUPON")
    assert found["availability"] == "AVAILABLE" and found["eligibility_status"] == "UNKNOWN"
    assert found["coupon"]["min_spend_cents"] == 5000
    assert found["source_type"] == "SIMULATED_TEST_DATA"
    assert missing["availability"] == "NOT_FOUND" and missing["eligibility_status"] == "UNKNOWN"
    with pytest.raises(Exception, match="COUPON_CODE_INVALID"):
        repository.get_coupon("bad code")
    engine.dispose()


def test_coupon_mcp_stdio_real_server_call_uses_local_read_grant(tmp_path):
    database_url = _url(tmp_path)
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(Coupon(coupon_code="SAVE-26", policy_id="coupon", version="2026-01", min_spend_cents=5000))
    server = os.path.abspath("scripts/mcp_m4_3_coupon_server.py")
    trust = McpServerTrust(server_id="shoprelay-coupon-test", transport="stdio",
        command=sys.executable, args=(server, "stdio"), cwd=os.getcwd(),
        env={"SHOPRELAY_MCP_COUPON_DB_URL": database_url,
             "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)},
        tools=(McpToolGrant("get_coupon", "coupon_catalog", read_only=True),))
    client = TrustedMcpClient(trust)
    discovered = __import__("asyncio").run(client.discover())
    assert [row["local_name"] for row in discovered] == ["coupon_catalog"]
    sink = InMemoryObservabilitySink()
    adapter = McpCouponReadRepository(client, sink=sink, run_id="r-mcp", trace_id="t-mcp")
    payload = adapter.get_coupon("SAVE-26")
    assert payload["source"] == "shoprelay_coupon_catalog"
    assert payload["eligibility_status"] == "UNKNOWN"
    assert client.call_count == 1 and client.discovery_count >= 1
    assert len(sink.spans) == 1 and sink.spans[0].strategy == "MCP"
    engine.dispose()


def test_coupon_skill_fastapi_supervisor_mcp_run_and_idempotent_replay(tmp_path, monkeypatch):
    import json
    import secrets
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import MemorySaver

    import app.m1.api as api_module
    from app.m0.config import load_settings
    from app.m0.data import seed
    from app.m1.api import CSRF_HEADER, create_app
    from app.m1.db import make_engine, make_session_factory, upgrade_database
    from app.m1.models import Coupon, GraphExecutionRecord, ObservabilityRecord, Run
    from app.m1.routing import RouteDecision
    from app.m1.services import set_demo_password
    from app.m3.agents import default_agent_registry
    from app.m3.graph import build_supervisor_graph, invoke_supervisor
    from app.orchestration.adapters import GraphServices

    path = tmp_path / "m43a-api.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    password = "m43a-test-" + secrets.token_urlsafe(18)
    with factory() as db:
        set_demo_password(db, "u_demo", password)
    with factory.begin() as db:
        db.add(Coupon(coupon_code="SAVE-26", policy_id="coupon", version="2026-01",
                      min_spend_cents=5000, source_type="SIMULATED_TEST_DATA"))

    server = os.path.abspath("scripts/mcp_m4_3_coupon_server.py")
    trust = McpServerTrust(server_id="shoprelay-coupon-api-test", transport="stdio",
        command=sys.executable, args=(server, "stdio"), cwd=os.getcwd(),
        env={"SHOPRELAY_MCP_COUPON_DB_URL": url,
             "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)},
        tools=(McpToolGrant("get_coupon", "coupon_catalog", read_only=True),))
    client = TrustedMcpClient(trust)
    adapter = McpCouponReadRepository(client)
    services = GraphServices(
        router=lambda _query: RouteDecision(strategy="AGENT", safety_decision="ALLOW",
            reason_code="LIVE_COUPON_CATALOG", decision_source="RULE",
            required_capabilities=["get_coupon"]).model_dump(mode="json"),
        direct=lambda _state: {}, single=lambda _state: {}, multi=lambda _state: {},
        refuse=lambda _state: {},
        agent_supervisor=lambda state, context: invoke_supervisor(build_supervisor_graph(
            default_agent_registry(coupon_repository=adapter), sink=context.observability_sink),
            state, context))
    saver = MemorySaver()

    @asynccontextmanager
    async def checkpointer_factory(_database_url, _schema):
        yield saver

    def enabled_settings(**_kwargs):
        values = load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_GRAPH_VERSION": "m4.3-a", "SHOPRELAY_EXECUTION_VERSION": "m4.3-a-test"},
            local_values={}, require_api_key=False)
        return replace(values, m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", enabled_settings)
    app = create_app(database_url=url, engine=engine, orchestrator_mode="langgraph",
        checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: True,
        graph_services=services)
    try:
        with TestClient(app) as http:
            csrf = http.get("/api/demo/csrf").json()["csrf_token"]
            login = http.post("/api/demo/login", headers={"Origin": str(http.base_url).rstrip("/"),
                CSRF_HEADER: csrf}, json={"username": "u_demo", "password": password})
            assert login.status_code == 200
            headers = {"Origin": str(http.base_url).rstrip("/"),
                CSRF_HEADER: http.cookies.get("shoprelay_csrf")}
            request = {"message": "请查询优惠券 SAVE-26 的目录规则", "client_request_id": "m43a-coupon-skill-1"}
            first = http.post("/api/chat/stream", headers=headers, json=request)
            assert first.status_code == 200, first.text
            event_types = [line.removeprefix("event: ") for line in first.text.splitlines()
                           if line.startswith("event: ")]
            assert event_types[0:3] == ["started", "route_decided", "agent_started"]
            assert "tool_started" in event_types and "tool_finished" in event_types
            assert event_types[-2:] == ["answer", "done"]
            started_payload = json.loads(next(line.removeprefix("data: ") for line in first.text.splitlines()
                if line.startswith("data: ") and json.loads(line.removeprefix("data: ")).get("type") == "started"))
            run_id = started_payload["run_id"]
            events = http.get(f"/api/runs/{run_id}/events").json()["events"]
            body = json.loads(next(line.removeprefix("data: ") for line in first.text.splitlines()
                                   if line.startswith("data: ") and json.loads(line.removeprefix("data: ")).get("type") == "answer"))
            answer = body["payload"]
            assert answer["answer_kind"] == "BUSINESS_FACTS"
            assert answer["business_results"]["eligibility_status"] == "UNKNOWN"
            assert answer["tool_references"][0]["source"] == "shoprelay_coupon_catalog"
            assert client.call_count == 1 and client.discovery_count >= 1
            replay = http.post("/api/chat/stream", headers=headers, json=request)
            assert replay.status_code == 200
            assert client.call_count == 1
            with factory() as db:
                run = db.get(Run, run_id)
                execution = db.get(GraphExecutionRecord, run_id)
                assert run.status == execution.status == "SUCCEEDED"
                assert execution.checkpoint_id
                span_rows = db.query(ObservabilityRecord).filter_by(run_id=run_id, record_type="span").all()
                span_payloads = [json.loads(row.payload_json) for row in span_rows]
                assert any(row["node_name"].endswith("get_coupon") and row["status"] == "OK"
                           for row in span_payloads)
            assert [row["type"] for row in events].count("tool_started") == 1
            record_path = os.environ.get("SHOPRELAY_M43A_PROBE_OUTPUT")
            if record_path:
                record = {
                    "record_type": "m4.3-a-coupon-skill-mcp-api-probe",
                    "data_source_marker": "SIMULATED_TEST_DATA",
                    "database": "isolated_temporary_sqlite",
                    "run_id": run_id,
                    "graph_version": execution.graph_version,
                    "status": run.status,
                    "strategy": "AGENT",
                    "skill_id": "generic_read_only",
                    "skill_version": "1.0.0",
                    "skill_reason": "FALLBACK:MINIMAL_READ_ONLY_SET",
                    "agent_calls": 1,
                    "mcp_server_id": "shoprelay-coupon-api-test",
                    "mcp_transport": "stdio",
                    "mcp_discovered_tools": ["coupon_catalog"],
                    "mcp_tool_calls": client.call_count,
                    "mcp_last_call_duration_ms": client.last_duration_ms,
                    "business_result": "coupon metadata returned; eligibility UNKNOWN",
                    "citations": 0,
                    "sse_event_types": event_types,
                    "sse_sequences": [row["seq"] for row in events],
                    "persisted_span_names": [row["node_name"] for row in span_payloads],
                    "checkpoint_id_present": bool(execution.checkpoint_id),
                    "idempotent_replay_additional_mcp_calls": 0,
                    "deepseek_requests": 0,
                    "business_writes": 0,
                    "credential_values_recorded": False,
                }
                with open(record_path, "x", encoding="utf-8") as output_file:
                    json.dump(record, output_file, ensure_ascii=False, indent=2)
    finally:
        engine.dispose()


def test_coupon_missing_source_and_human_escalation_remain_non_executing(tmp_path):
    from app.m3.escalation import create_escalation_draft

    engine = create_engine(_url(tmp_path))
    factory = sessionmaker(engine, expire_on_commit=False)
    unavailable = CouponReadRepository(factory).get_coupon("SAVE-26")
    assert unavailable["availability"] == "NOT_AVAILABLE"
    permission_check = lambda _user, _capability: False
    with pytest.raises(PermissionError, match="TOOL_NOT_AUTHORIZED"):
        create_escalation_draft(permission_checker=permission_check, trusted_user_id="u_demo", authenticated=True,
            owner_verified=True, reason_code="POLICY_CONFLICT", order_ref=None,
            summary_code="NEEDS_HUMAN_REVIEW", evidence_refs=[])
    draft = create_escalation_draft(permission_checker=lambda _user, capability:
        capability == "support.ticket.draft", trusted_user_id="u_demo", authenticated=True,
        owner_verified=True, reason_code="POLICY_CONFLICT", order_ref=None,
        summary_code="NEEDS_HUMAN_REVIEW", evidence_refs=[])
    assert draft.status == "DRAFT_ONLY" and draft.execution_status == "NOT_EXECUTED"
    assert draft.ticket_created is False and draft.notification_sent is False
    engine.dispose()


def test_real_three_agent_return_skill_executes_tools_and_keeps_claims_bounded(tmp_path):
    database_url = _url(tmp_path)
    from app.m0.data import seed
    seed(tmp_path / "m43a.sqlite")
    upgrade_database(database_url)
    engine = create_engine(database_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    retrieval = __import__("app.m2.retrieval", fromlist=["RetrievalService"]).RetrievalService()
    policy_service = PolicyReadService(retrieval)
    registry = AgentRegistry(policy=PolicyAgent(policy_service),
        order_logistics=OrderLogisticsAgent(SqlAlchemyOrderReadRepository(factory)),
        after_sales=AfterSalesAgent(AfterSalesReadRepository(factory)),
        product=ProductAgent(ProductReadRepository(factory)))
    graph = build_supervisor_graph(registry)
    query = "ORD-DELIVERED：请说明这笔订单退货与退款政策如何衔接？"
    route = {"strategy": "AGENT", "sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
        "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]}
    route.update(bind_skill(query, route))
    sink = InMemoryObservabilitySink()
    results, calls, events = {}, [], []
    trusted = TrustedAgentContext(user_id="u_demo", session_id="s-skill", run_id="r-skill-real",
        trace_id="t-skill-real", authenticated=True, run_owner_verified=True,
        session_owner_verified=True, requested_order_id="ORD-DELIVERED")
    context = AgentExecutionContext(result_store=results, sink=sink, run_id=trusted.run_id,
        trace_id=trusted.trace_id, parent_span_id=None, user_query=query, retrieval_service=retrieval,
        route_payload=route, call_records=calls,
        emit_event=lambda kind, payload, key: events.append((kind, payload, key)))
    context.route_payload = route
    graph_state = graph_input_state(GraphInput(run_id=trusted.run_id,
        session_id=trusted.session_id, trace_id=trusted.trace_id, query=query), graph_version="m4.3-a")
    graph_state.update({"strategy": "AGENT", "route": route})
    runtime_context = GraphRuntimeContext(query=query, trusted_agent_context=trusted, m3_supervisor_enabled=True,
        emit_event=lambda kind, payload, key: events.append((kind, payload, key)),
        route_payload=route, tool_result_store=results, tool_execution_records=calls,
        retrieval_service=retrieval)
    output = invoke_supervisor(graph, graph_state, runtime_context)
    business = build_after_sales_business_result(output, route=route, result_store=results)
    assert [item["target_agent"] for item in output["handoffs"]] == ["order_logistics", "policy", "after_sales"]
    assert business and business["answer_kind"] in {"POLICY_ANSWER", "CLARIFICATION_REQUIRED"}
    assert output["agent_budget"]["agent_calls"] == 3 and len(calls) == 3
    assert {row["capability"] for row in calls} == {"get_order", "search_knowledge", "get_refund_status"}
    assert business["business_results"]["user_claims_used_as_verified_facts"] is False
    skill_sink = InMemoryObservabilitySink()
    executed = run_selected_skill(route, lambda: output, query=query, sink=skill_sink, run_id=trusted.run_id,
        trace_id=trusted.trace_id, graph_version="m4.3-a")
    assert executed["supervisor_status"] == "COMPLETED"
    assert skill_sink.spans[0].node_name == "skill.return_handling"
    assert events
    engine.dispose()


def test_warranty_policy_evidence_does_not_infer_purchase_eligibility():
    class PolicyStub:
        def search(self, query, *, mode, result_ref):
            assert mode == "BM25" and "P-TEST" in query
            return {"source": "shoprelay_policy_retrieval", "result_ref": result_ref,
                "retrieval_mode": "BM25", "retrieval_count": 1, "knowledge_version": "kb-test",
                "index_fingerprint": "f" * 64, "evaluation_at": "2026-01-01T00:00:00Z",
                "evidence": [{"chunk_id": "warranty-1", "content": "商品保修期限按保修政策执行。",
                    "version": "v1", "policy_version": "v1", "effective_from": "2025-01-01T00:00:00Z",
                    "effective_to": None, "index_fingerprint": "f" * 64,
                    "knowledge_version": "kb-test"}]}

    sink = InMemoryObservabilitySink()
    store, calls = {}, []
    query = "查询商品 P-TEST 的保修政策和我的保修资格"
    trusted = TrustedAgentContext(user_id="u_demo", session_id="s-warranty", run_id="r-warranty",
        trace_id="t-warranty", authenticated=True, run_owner_verified=True, session_owner_verified=True)
    context = AgentExecutionContext(result_store=store, sink=sink, run_id=trusted.run_id,
        trace_id=trusted.trace_id, parent_span_id=None, user_query=query, call_records=calls)
    task = AgentTask(task_id="warranty-task", target_agent="policy",
        task="Retrieve and explain requested policy evidence", required_capabilities=["get_warranty"],
        context_refs=["run:r-warranty", "session:s-warranty"], evidence_refs=[])
    result = PolicyAgent(service=PolicyStub()).execute(task, trusted, context)
    payload = store[result.tool_result_refs[0]]
    assert payload["eligibility_status"] == "UNKNOWN"
    assert "PURCHASE_FACT_NOT_VERIFIED" in payload["unknown_reasons"]
    assert payload["evidence"][0]["chunk_id"] == "warranty-1"
    assert calls[0]["capability"] == "get_warranty"
