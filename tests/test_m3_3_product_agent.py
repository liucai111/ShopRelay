from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
import pytest
from sqlalchemy import delete, select
from alembic import command

from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, make_engine, make_session_factory, upgrade_database
from app.m1.db import alembic_config
from app.m1.models import GraphExecutionRecord, Inventory, ObservabilityRecord, Product, Run
from app.m1.routing import decide_route
from app.m1.services import set_demo_password
from app.m3.agents import ProductAgent
from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.order_tools import AgentExecutionContext, ToolExecutionError
from app.m3.product_tools import ProductFilters, ProductReadRepository, parse_product_request, parse_product_request_many
from app.orchestration.adapters import GraphServices
from app.orchestration.observability import InMemoryObservabilitySink


def _build(tmp_path, monkeypatch):
    import app.m1.api as api_module
    path = tmp_path / "m3.3-product.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    password = "m33-demo-" + secrets.token_urlsafe(12)
    with factory() as db:
        set_demo_password(db, "u_demo", password)
    loader = api_module.load_settings
    def m3_settings(**_kwargs):
        return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "g2-m3.3",
            "SHOPRELAY_EXECUTION_VERSION": "m3.3-product-read-v1"}, local_values={}, require_api_key=False),
            m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", m3_settings)
    services = GraphServices(router=lambda q: decide_route(q).model_dump(mode="json"),
        direct=lambda _s: {}, single=lambda _s: {}, multi=lambda _s: {}, refuse=lambda _s: {})
    saver = MemorySaver()

    @asynccontextmanager
    async def checkpointer_factory(_url, _schema):
        yield saver

    app = create_app(database_url=url, engine=engine, orchestrator_mode="langgraph",
        checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: True,
        graph_services=services)
    return app, factory, engine, saver, password


def _login(client: TestClient, password: str):
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": "u_demo", "password": password})
    assert response.status_code == 200
    return {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def test_product_routing_and_bounded_filter_parser():
    route = decide_route("搜索电热水壶")
    assert route.strategy == "AGENT" and route.required_capabilities == ["search_products"]
    route = decide_route("推荐一款电热水壶，预算300元以内，有货")
    assert route.strategy == "AGENT"
    assert set(route.required_capabilities) == {"search_products", "recommend_products", "check_inventory"}
    product_id, filters, wants_stock = parse_product_request("推荐一款电热水壶，预算300元以内，有货")
    assert product_id is None and filters.category == "appliance"
    assert filters.max_price_cents == 30000 and wants_stock
    with pytest.raises(ToolExecutionError, match="PRODUCT_ID_AMBIGUOUS"):
        parse_product_request("比较 P-CUP 与 P-KETTLE")
    ids, _, _ = parse_product_request_many("比较 P-CUP 与 P-KETTLE")
    assert ids == ["P-CUP", "P-KETTLE"]
    with pytest.raises(ToolExecutionError, match="PRODUCT_LIMIT_INVALID"):
        ProductFilters(limit=21).validate()


def test_product_read_repository_uses_only_real_catalog_and_inventory_rows(tmp_path):
    path = tmp_path / "product-repository.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    try:
        repo = ProductReadRepository(factory)
        result = repo.search_products(ProductFilters(keyword="电热水壶", category="appliance"))
        assert result["count"] == 1 and result["truncated"] is False
        assert result["products"][0]["product_id"] == "P-KETTLE"
        detail = repo.get_product_detail("P-KETTLE")
        assert detail["product"]["price_cents"] == 15900
        assert detail["product"]["description_status"] == "NOT_STORED"
        inventory = repo.check_inventory("P-KETTLE")
        assert inventory["availability"] == "UNAVAILABLE"
        assert inventory["error_code"] == "INVENTORY_FRESHNESS_UNVERIFIED"
        assert inventory["records"][0]["snapshot_sellable_qty"] == 12
        assert inventory["records"][0]["freshness"] == "DATABASE_SNAPSHOT_NO_OBSERVED_AT"
        assert repo.get_product_detail("P-MISSING")["availability"] == "NOT_FOUND"
        with factory() as db:
            db.execute(delete(Inventory).where(Inventory.product_id == "P-SNACK"))
            db.commit()
        assert repo.check_inventory("P-SNACK")["availability"] == "UNAVAILABLE"
        recommendation = repo.recommend_products(ProductFilters(category="appliance", max_price_cents=7000),
            require_inventory=True)
        assert recommendation["recommendations"][0]["match_status"] == "PARTIAL_MATCH"
        stale_inventory = repo.recommend_products(ProductFilters(category="appliance", max_price_cents=30000),
            require_inventory=True)
        assert stale_inventory["recommendations"][0]["match_status"] == "UNCONFIRMED"
        assert "没有观测时间" in " ".join(stale_inventory["recommendations"][0]["match_reasons"])
        unknown = repo.recommend_products(ProductFilters(category="food"), require_inventory=True)
        assert unknown["recommendations"][0]["match_status"] == "UNCONFIRMED"
    finally:
        engine.dispose()


def test_inventory_migration_adds_missing_m0_table_without_touching_catalog(tmp_path):
    path = tmp_path / "migration-inventory.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    try:
        # Rewind the complete empty schema through Alembic rather than editing
        # only the version row; later migrations now add their own tables.
        command.downgrade(alembic_config(url), "20261021_06")
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE inventory")
        command.upgrade(alembic_config(url), "head")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == LATEST_SCHEMA_REVISION
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM products").scalar_one() == 3
            assert {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")} >= {
                "inventory", "products", "orders"}
    finally:
        engine.dispose()


def test_product_agent_rejects_untrusted_context_and_missing_product_id(tmp_path):
    path = tmp_path / "product-agent.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    agent = ProductAgent(ProductReadRepository(factory))
    task = AgentTask(task_id="product-task", target_agent="product", task="Resolve requested live product capability",
        required_capabilities=["get_product_detail"], context_refs=["run:run-1", "session:session-1"], evidence_refs=[])
    trusted = TrustedAgentContext(user_id="u_demo", session_id="session-1", run_id="run-1", trace_id="trace-1",
        authenticated=True, run_owner_verified=True, session_owner_verified=True)
    result = agent.execute(task, trusted, AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(),
        run_id="run-1", trace_id="trace-1", parent_span_id=None, user_query="查看商品详情"))
    assert result.status == "COMPLETED" and result.missing_information == ["PRODUCT_ID_REQUIRED"]
    denied = agent.execute(task, trusted.model_copy(update={"authenticated": False}),
        AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(), run_id="run-1",
            trace_id="trace-1", parent_span_id=None, user_query="P-CUP 详情"))
    assert denied.status == "FAILED" and denied.error_code == "AGENT_CONTEXT_INVALID"
    engine.dispose()


def test_product_api_real_agent_tools_sse_trace_checkpoint_and_idempotency(tmp_path, monkeypatch):
    app, factory, engine, saver, password = _build(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            body = {"message": "推荐一款电热水壶，预算300元以内，有货",
                    "client_request_id": "m33-product-recommend-1", "user_id": "u_other"}
            response = client.post("/api/chat", headers=headers, json=body)
            assert response.status_code == 200, response.json()
            result = response.json()
            assert result["strategy"] == "AGENT" and result["answer_kind"] == "BUSINESS_FACTS"
            assert "P-KETTLE" in result["answer"] and "UNCONFIRMED" in result["answer"]
            assert "无法确认当前库存" in result["answer"]
            assert result["business_results"]["search_products"]["products"][0]["product_id"] == "P-KETTLE"
            assert result["business_results"]["recommend_products"]["recommendations"][0]["product"]["product_id"] == "P-KETTLE"
            assert result["business_results"]["check_inventory"]["availability"] == "UNAVAILABLE"
            assert result["business_results"]["check_inventory"]["records"][0]["snapshot_sellable_qty"] == 12
            run_id = result["run_id"]
            events = client.get(f"/api/runs/{run_id}/events").json()["events"]
            event_types = [item["type"] for item in events]
            assert event_types == ["started", "route_decided", "agent_started",
                "tool_started", "tool_finished", "tool_started", "tool_finished",
                "tool_started", "tool_finished", "answer", "done"]
            assert client.post("/api/chat", headers=headers, json=body).json()["run_id"] == run_id
            assert len([item for item in client.get(f"/api/runs/{run_id}/events").json()["events"]
                        if item["type"] == "tool_started"]) == 3
            snapshots = list(saver.list({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}))
            snapshot_text = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
            assert "电热水壶" not in snapshot_text and "u_demo" not in snapshot_text
            with factory() as db:
                run = db.get(Run, run_id)
                graph_run = db.get(GraphExecutionRecord, run_id)
                spans = [json.loads(row.payload_json) for row in db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run_id, ObservabilityRecord.record_type == "span")).scalars()]
                tool_spans = [row for row in spans if row["node_name"].startswith("m3.tool.")]
                assert run.status == graph_run.status == "SUCCEEDED"
                assert graph_run.state_schema_version == "g2-m3.3-state-v1" and graph_run.checkpoint_id
                assert len(tool_spans) == 3 and all(row["status"] == "OK" for row in tool_spans)
                assert all(row["graph_version"] == "g2-m3.3" for row in tool_spans)
                log_text = " ".join(row.payload_json for row in db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run_id, ObservabilityRecord.record_type == "log")).scalars())
                assert "电热水壶" not in log_text and "P-KETTLE" not in log_text
            assert response.json()["tool_references"] and all(
                ref["source"] in {"shoprelay_product_catalog", "shoprelay_inventory"}
                for ref in response.json()["tool_references"])
            business_evidence = response.json()["business_evidence"]
            assert any(row["kind"] == "catalog_search_result" for row in business_evidence)
            assert not any(row["kind"] == "knowledge_document" for row in business_evidence)
            audit = response.json()["tool_call_audit"]
            assert len(audit) == 3 and len({row["tool_call_id"] for row in audit}) == 3
            assert all(row["run_id"] == run_id and row["trace_id"] for row in audit)
            audit_text = json.dumps(audit, ensure_ascii=False)
            assert "P-KETTLE" not in audit_text and "电热水壶" not in audit_text
            execution = client.get(f"/api/runs/{run_id}/execution").json()["execution"]
            assert execution["business_evidence"] == business_evidence
            assert execution["tool_call_audit"] == audit
            run_view = client.get(f"/api/runs/{run_id}").json()["run"]["answer"]
            assert run_view["business_evidence"] == business_evidence
            assert run_view["tool_call_audit"] == audit
        with TestClient(app) as anonymous:
            denied = anonymous.post("/api/chat", json={"message": "查询商品 P-CUP 库存",
                "client_request_id": "m33-anon"})
            assert denied.status_code == 401
    finally:
        engine.dispose()
