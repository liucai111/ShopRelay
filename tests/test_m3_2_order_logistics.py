from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
import pytest
from sqlalchemy import func, select

from app.m0.config import load_settings
from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import GraphExecutionRecord, Message, ObservabilityRecord, Order, Run, Shipment
from app.m1.routing import RouteDecision
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.m3.agents import OrderLogisticsAgent, default_agent_registry
from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.order_tools import (AgentExecutionContext, SqlAlchemyOrderReadRepository, requested_order_id,
    build_order_business_result)
from app.orchestration.observability import InMemoryObservabilitySink
from sqlalchemy.exc import SQLAlchemyError


def _route(capability: str):
    return RouteDecision(strategy="AGENT", safety_decision="ALLOW", reason_code="LIVE_BUSINESS_FACT",
        decision_source="RULE", required_capabilities=[capability]).model_dump(mode="json")


def _build(tmp_path, monkeypatch, capability_for_query=None):
    import app.m1.api as api_module
    path = tmp_path / "m3.2-order.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    pw_a, pw_b = "m3.2-demo-a-" + secrets.token_urlsafe(12), "m3.2-demo-b-" + secrets.token_urlsafe(12)
    with factory() as db:
        set_demo_password(db, "u_demo", pw_a)
        set_demo_password(db, "u_other", pw_b)
    loader = api_module.load_settings
    def m3_settings(**_kwargs):
        return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "g2-m3.3",
            "SHOPRELAY_EXECUTION_VERSION": "m3.3-order-read-v1"}, local_values={}, require_api_key=False),
            m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", m3_settings)
    route_fn = capability_for_query or (lambda _query: "get_order")
    services = GraphServices(router=lambda query: _route(route_fn(query)), direct=lambda _s: {},
        single=lambda _s: {}, multi=lambda _s: {}, refuse=lambda _s: {})
    saver = MemorySaver()

    @asynccontextmanager
    async def checkpointer_factory(_url, _schema):
        yield saver

    app = create_app(database_url=url, engine=engine, orchestrator_mode="langgraph",
        checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: True,
        graph_services=services)
    return app, factory, engine, saver, pw_a, pw_b


def _login(client: TestClient, username: str, password: str):
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def test_order_agent_reads_owned_order_and_persists_tool_result_sse_and_checkpoint(tmp_path, monkeypatch):
    app, factory, engine, saver, password, _ = _build(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            headers = _login(client, "u_demo", password)
            body = {"message": "查询订单 ORD-PAID 当前状态", "client_request_id": "m32-order-own"}
            response = client.post("/api/chat", headers=headers, json={**body, "user_id": "u_other"})
            assert response.status_code == 200
            assert response.json()["strategy"] == "AGENT"
            assert "订单 ORD-PAID 已记录为已付款" in response.json()["answer"]
            assert response.json()['business_results']['get_order']['status']=='PAID'
            assert response.json()["business_results"]["get_order"]["paid_cents"] > 0
            run_id = response.json()["run_id"]
            run_response = client.get(f"/api/runs/{run_id}")
            assert run_response.status_code == 200
            assert run_response.json()["run"]["status"] == "SUCCEEDED"
            assert run_response.json()["run"]["answer"]["answer_kind"] == "BUSINESS_FACTS"
            events_response = client.get(f"/api/runs/{run_id}/events")
            event_types = [event["type"] for event in events_response.json()["events"]]
            assert event_types == ["started", "route_decided", "agent_started", "tool_started",
                                   "tool_finished", "answer", "done"]
            before_tools = sum(event_type == "tool_started" for event_type in event_types)
            replay = client.post("/api/chat", headers=headers, json=body)
            assert replay.status_code == 200 and replay.json()["run_id"] == run_id
            replay_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
            assert sum(item["type"] == "tool_started" for item in replay_events) == before_tools
            snapshots = list(saver.list({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}))
            assert snapshots
            state_text = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
            assert "ORD-PAID" not in state_text and "u_demo" not in state_text and body["message"] not in state_text
            with factory() as db:
                run = db.get(Run, run_id)
                graph_run = db.get(GraphExecutionRecord, run_id)
                records = db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run_id)).scalars().all()
                messages = db.execute(select(Message).where(Message.session_id == run.session_id)).scalars().all()
                assert run.status == graph_run.status == "SUCCEEDED"
                assert graph_run.graph_version == "g2-m3.3" and graph_run.checkpoint_id
                assert any(item.role == "assistant" and "ORD-PAID" in item.content for item in messages)
                span_payloads = [json.loads(item.payload_json) for item in records if item.record_type == "span"]
                tool_span = next(row for row in span_payloads if row["node_name"] == "m3.tool.get_order")
                assert tool_span["parent_span_id"] and tool_span["status"] == "OK"
                log_text = " ".join(item.payload_json for item in records if item.record_type == "log")
                assert "ORD-PAID" not in log_text and "PAID" not in log_text
    finally:
        engine.dispose()


def test_order_items_and_missing_shipment_are_read_from_existing_business_tables(tmp_path, monkeypatch):
    def capability(query: str) -> str:
        return "get_order_items" if "明细" in query else "track_shipment" if "物流" in query else "get_order"
    app, factory, engine, _saver, password, _ = _build(tmp_path, monkeypatch, capability)
    try:
        with TestClient(app) as client:
            headers = _login(client, "u_demo", password)
            items = client.post("/api/chat", headers=headers, json={"message": "查看 ORD-PAID 商品明细",
                "client_request_id": "m32-items"})
            assert items.status_code == 200
            item_rows = items.json()["business_results"]["get_order_items"]["items"]
            assert item_rows and all("name" in row and "quantity" in row for row in item_rows)
            shipment = client.post("/api/chat", headers=headers, json={"message": "查询 ORD-SHIPPED 物流轨迹",
                "client_request_id": "m32-shipment-empty"})
            assert shipment.status_code == 200, shipment.json()
            facts = shipment.json()["business_results"]["track_shipment"]
            assert facts["availability"] == "UNAVAILABLE" and facts["records"] == []
            answer = shipment.json()["answer"]
            assert "未找到该订单的物流记录" in answer
            assert "business_database" in answer
            assert "观测时刻和当前位置未知" in answer
            assert "无记录不表示未发货或延误" in answer
            with factory() as db:
                assert db.execute(select(func.count()).select_from(Shipment)).scalar_one() == 0
                assert db.execute(select(func.count()).select_from(Order)).scalar_one() == 4
    finally:
        engine.dispose()


def test_order_agent_owner_isolation_clarification_and_invalid_identifier(tmp_path, monkeypatch):
    app, _factory, engine, _saver, password_a, password_b = _build(tmp_path, monkeypatch)
    try:
        with TestClient(app) as owner_a, TestClient(app) as owner_b:
            headers_a = _login(owner_a, "u_demo", password_a)
            headers_b = _login(owner_b, "u_other", password_b)
            own_b = owner_b.post("/api/chat", headers=headers_b, json={"message": "查询 ORD-OTHER 状态",
                "client_request_id": "m32-b-own"})
            assert own_b.status_code == 200 and "ORD-OTHER" in own_b.json()["answer"]
            foreign = owner_a.post("/api/chat", headers=headers_a, json={"message": "查询 ORD-OTHER 状态",
                "client_request_id": "m32-a-foreign"})
            absent = owner_a.post("/api/chat", headers=headers_a, json={"message": "查询 ORD-NOT-THERE 状态",
                "client_request_id": "m32-a-absent"})
            assert foreign.status_code == absent.status_code == 502, {"foreign": foreign.json(), "absent": absent.json()}
            assert foreign.json()["code"] == absent.json()["code"] == "ORDER_NOT_FOUND"
            missing_id = owner_a.post("/api/chat", headers=headers_a, json={"message": "查询我的订单状态",
                "client_request_id": "m32-no-id"})
            assert missing_id.status_code == 200
            assert missing_id.json()["answer_kind"] == "CLARIFICATION_REQUIRED"
            assert "ORD-" not in missing_id.json()["answer"]
            invalid = owner_a.post("/api/chat", headers=headers_a, json={"message": "查询订单 ORD-!! 状态",
                "client_request_id": "m32-invalid"})
            assert invalid.status_code == 502 and invalid.json()["code"] == "INVALID_ORDER_ID"
        with TestClient(app) as anonymous:
            denied = anonymous.post("/api/chat", json={"message": "查询 ORD-PAID 当前状态",
                "client_request_id": "m32-anon"})
            assert denied.status_code == 401
    finally:
        engine.dispose()


def test_requested_order_id_is_target_only_and_tool_repository_always_requires_owner(tmp_path):
    assert requested_order_id("查 SR-ORD-PAID") == ("ORD-PAID", None)
    assert requested_order_id("查 ORD-PAID 和 ORD-OTHER") == (None, "ORDER_ID_AMBIGUOUS")
    assert requested_order_id("查 ORD-!!") == (None, "INVALID_ORDER_ID")
    path = tmp_path / "m3.2-repository.sqlite3"
    seed(path)
    upgrade_database(f"sqlite:///{path.as_posix()}")
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    factory = make_session_factory(engine)
    try:
        repo = SqlAlchemyOrderReadRepository(factory)
        assert repo.get_order("ORD-PAID", "u_demo")["status"] == "PAID"
        with pytest.raises(Exception) as foreign:
            repo.get_order("ORD-OTHER", "u_demo")
        with pytest.raises(Exception) as absent:
            repo.get_order("ORD-NOT-THERE", "u_demo")
        assert foreign.value.code == absent.value.code == "ORDER_NOT_FOUND"
        assert repo.get_order_items("ORD-PAID", "u_demo")["items"]
        assert repo.track_shipment("ORD-SHIPPED", "u_demo")["availability"] == "UNAVAILABLE"
        # This row exists only in the isolated test database to verify reading
        # a present shipment record; it is not part of the repeatable M0 seed.
        with factory() as db:
            db.add(Shipment(shipment_id="SHIP-TEST-FIXTURE", order_id="ORD-SHIPPED",
                status="IN_TRANSIT", observed_at="2026-10-20T12:00:00Z"))
            db.commit()
        available = repo.track_shipment("ORD-SHIPPED", "u_demo")
        assert available["availability"] == "AVAILABLE"
        assert available["records"] == [{"status": "IN_TRANSIT", "observed_at": "2026-10-20T12:00:00Z"}]
    finally:
        engine.dispose()


def test_unknown_amount_is_omitted_from_text_but_retained_in_structured_order_result():
    from types import SimpleNamespace

    route = {"required_capabilities": ["get_order", "get_order_items"]}
    supervisor = {"supervisor_status": "COMPLETED", "terminal_status": "RUNNING",
        "handoffs": [{"target_agent": "order_logistics", "status": "COMPLETED",
            "required_capabilities": ["get_order", "get_order_items"],
            "result": {"source_agent": "order_logistics", "status": "COMPLETED",
                "tool_result_refs": ["order-fact", "item-fact"]}}],
        "tool_execution_status": [{"capability": "get_order", "executed": True,
                "status": "COMPLETED", "duration_ms": 1},
            {"capability": "get_order_items", "executed": True,
                "status": "COMPLETED", "duration_ms": 1}]}
    facts = {"order-fact": {"source": "business_database", "order_id": "ORD-SYN-UNKNOWN",
                "status": "PAID", "paid_cents": None, "paid_at": None, "delivered_at": None},
        "item-fact": {"source": "business_database", "order_id": "ORD-SYN-UNKNOWN",
                "items": [{"product_id": "P-SYN-A", "name": "Synthetic item", "quantity": 1,
                    "paid_cents": None}]}}
    trusted = SimpleNamespace(requested_order_id="ORD-SYN-UNKNOWN", authenticated=True,
        run_owner_verified=True, session_owner_verified=True)
    result = build_order_business_result(supervisor, route=route, trusted=trusted, result_store=facts)
    assert result is not None
    assert "None" not in result["answer"] and "null" not in result["answer"]
    assert "0 分" not in result["answer"] and "金额" not in result["answer"] and "实付" not in result["answer"]
    assert "已记录为已付款" in result["answer"]
    assert result["business_results"]["get_order"]["paid_cents"] is None
    assert result["business_results"]["get_order_items"]["items"][0]["paid_cents"] is None


def test_diagnostic_checker_detects_historical_unknown_amount_sentinel():
    from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record
    from pathlib import Path
    from eval.m6_1.personal_dev_diagnostic_checks import load_locked_rules
    root = Path(__file__).resolve().parents[1]
    locked, _sha = load_locked_rules(root / "docs/eval/d1-diagnostic-r3-20261027/locked-check-rules-r3.json",
        case_id="D1-ORDER-007")
    # A compact isolated record verifies the detector using only an explicit unknown field.
    archive = {"result": {"status": "SUCCEEDED", "run_id": "run-r7-text", "result": {
            "strategy": "AGENT", "route": {"strategy": "AGENT"}, "trace_id": "trace-r7-text",
            "answer": "订单记录金额：None 分。", "business_results": {}, "tool_references": []},
        "observations": {"runs": [{"run_id": "run-r7-text", "status": "SUCCEEDED", "user_id": "d1-personal-a"}],
            "run_events": [{"event_type": "route_decided", "run_id": "run-r7-text", "trace_id": "trace-r7-text",
                "payload_json": json.dumps({"strategy": "AGENT"})}]}}}
    result = evaluate_diagnostic_record(case_id="D1-ORDER-007", archived=archive,
        plan={"case_id": "D1-ORDER-007", "owner_id": "d1-personal-a"}, locked_rules=locked,
        budget_snapshot={"budget_snapshot_schema_version": "m6.1-budget-snapshot-v2", "snapshot_kind": "FINAL",
            "complete": True, "settled": True, "active_reservations": 0, "integrity_errors": [],
            "provider_requests": 0, "embedding_requests": 0, "judge_requests": 0,
            "calls_by_category": {"provider": 0, "tool": 0, "embedding": 0, "judge": 0},
            "tokens_conservative_budget_consumption": 0, "estimated_usd_conservative_budget_consumption": 0.0},
        cleanup_evidence={"cleanup_status": "CLEANED_AND_RELEASED", "lease_released": True,
            "pending_actions": 0, "residual_case_rows": 0, "database_identity": "synthetic"})
    assert result["evidence_checks"]["unknown_amount_not_rendered_as_value"] == "FAIL"


def test_order_privacy_refusal_has_safe_account_guidance_without_credential_copy():
    from app.m1.chat_responses import refusal_response
    from app.m1.routing import decide_route

    route = decide_route("查询我同事的订单 ORD-SYN-OTHER 状态")
    assert route.strategy == "REFUSE"
    assert route.reason_code == "THIRD_PARTY_ORDER_ACCESS"
    refusal = refusal_response(reason_code=route.reason_code)
    assert refusal["answer_kind"] == "REFUSAL"
    assert "不能查询、确认或披露他人订单信息" in refusal["answer"]
    assert "完成本人身份验证的账户" in refusal["answer"]
    assert "密钥" not in refusal["answer"] and "密码" not in refusal["answer"]
    assert "ORD-SYN-OTHER" not in refusal["answer"]


@pytest.mark.parametrize(("repository", "expected"), [
    (type("TimeoutRepo", (), {"get_order": lambda self, _order, _owner: (_ for _ in ()).throw(TimeoutError())})(),
     "TOOL_TIMEOUT"),
    (type("DatabaseFailureRepo", (), {"get_order": lambda self, _order, _owner: (_ for _ in ()).throw(SQLAlchemyError("db"))})(),
     "TOOL_DATABASE_UNAVAILABLE"),
])
def test_order_agent_dependency_failures_never_create_business_success(repository, expected):
    task = AgentTask(task_id="task-1", target_agent="order_logistics",
        task="Resolve requested live order or shipment capability", required_capabilities=["get_order"],
        context_refs=["run:run-1", "session:session-1"], evidence_refs=[])
    trusted = TrustedAgentContext(user_id="u_demo", session_id="session-1", run_id="run-1",
        trace_id="trace-1", requested_order_id="ORD-PAID")
    records = []
    execution = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(), run_id="run-1",
        trace_id="trace-1", parent_span_id=None, call_records=records)
    result = OrderLogisticsAgent(repository).execute(task, trusted, execution)
    assert result.status == "FAILED" and result.error_code == expected
    assert records[0]["status"] == "FAILED" and records[0]["executed"] is True
    assert execution.result_store == {}


def test_unconfigured_order_agent_cannot_claim_a_tool_success():
    agent = default_agent_registry().order_logistics
    task = AgentTask(task_id="task-2", target_agent="order_logistics",
        task="Resolve requested live order or shipment capability", required_capabilities=["get_order"],
        context_refs=["run:run-2", "session:session-2"], evidence_refs=[])
    trusted = TrustedAgentContext(user_id="u_demo", session_id="session-2", run_id="run-2",
        trace_id="trace-2", requested_order_id="ORD-PAID")
    result = agent.execute(task, trusted, None)
    assert result.status == "NOT_IMPLEMENTED" and not result.tool_result_refs
