"""M3.6 four-specialist integration probe, pinned to shoprelay_m11_test.

The Router is deterministically injected for engineering coverage; this is not
Router quality evidence. Only tagged SIMULATED_TEST_DATA rows are inserted and
removed by exact fixture IDs. No business write tool or DeepSeek request runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import (AfterSalesEvent, AuthSession, ConversationSession, DemoCredential,
    GraphExecutionRecord, Message, ObservabilityRecord, Order, OrderItem, Product, Refund,
    ReturnRequest, Run, RunEvent, Trace)
from app.m1.services import set_demo_password
from app.orchestration.checkpointer import checkpoint_schema_ready, open_checkpointer, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url
from scripts.probe_m3_5_after_sales_data_postgres import _fixture_set, _utc_text

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "eval/m3.6/m3.6-cooperation-scenarios-r1.lock.json"
SOURCE = "SIMULATED_TEST_DATA"
REVISION = "20261022_08"


async def _read_checkpoint_state(database_url: str, schema: str, run_id: str) -> dict:
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        snapshot = await saver.aget_tuple(config)
    return snapshot.checkpoint.get("channel_values", {}) if snapshot else {}


async def _delete_checkpoint(database_url: str, schema: str, run_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(run_id)


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: csrf}, json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return {"Origin": str(client.base_url).rstrip("/"),
            CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _counts(connection) -> dict[str, int]:
    return {name: int(connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one())
            for name in ("orders", "order_items", "products", "returns", "refunds", "after_sales_events")}


def probe(output_path: str | None = None) -> dict:
    if not LOCK_PATH.is_file():
        raise RuntimeError("SCENARIO_LOCK_MISSING")
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8-sig"))
    scenario_path = ROOT / lock["dataset_path"]
    dataset_sha = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
    record: dict = {
        "schema_version": "shoprelay.m3.6-cross-agent-postgres.v1",
        "started_at": _utc_text(), "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "expected_revision": REVISION, "fixture_source": SOURCE,
        "scenario_dataset": lock["dataset_path"], "scenario_dataset_sha256": dataset_sha,
        "router_source": "deterministic scenario injection; not Router-quality evidence",
        "deepseek_requests": 0, "business_write_executions": 0,
        "run_results": {}, "tool_latency_ms": [], "agent_calls": [],
    }
    if dataset_sha != lock.get("sha256"):
        record.update(status="FAIL", failure_code="SCENARIO_DATASET_HASH_MISMATCH")
        return record
    engine = factory = None
    database_url = None
    settings = None
    api_module = None
    original_loader = None
    fixture_ids: dict[str, str] = {}
    run_ids: list[str] = []
    session_ids: list[str] = []
    auth_hashes: set[str] = set()
    saved_credentials: dict[str, dict] = {}
    password = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(5).upper()
    old_env = {key: os.environ.get(key) for key in (
        "SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED",
        "SHOPRELAY_GRAPH_VERSION", "SHOPRELAY_EXECUTION_VERSION")}
    api = fresh_app = None
    phase = "configuration"
    try:
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        with engine.connect() as connection:
            record["database"] = connection.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
            record["fixture_counts_before"] = _counts(connection)
        if record["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        current = current_revision(database_url)
        record["alembic_revision"] = current
        if current != REVISION:
            raise RuntimeError("UNEXPECTED_ALEMBIC_REVISION")
        settings = load_settings(require_api_key=False)
        record["checkpointer_schema"] = settings.checkpoint_schema
        record["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        phase = "fixture_setup"
        with factory() as db:
            for user_id in ("u_demo", "u_other"):
                credential = db.get(DemoCredential, user_id)
                if credential is None:
                    raise RuntimeError("DEMO_CREDENTIAL_MISSING")
                saved_credentials[user_id] = {"password_hash": credential.password_hash,
                    "active": credential.active, "updated_at": credential.updated_at}
                set_demo_password(db, user_id, password)
            db.commit()
        fixture_ids = _fixture_set(factory, prefix)
        with factory.begin() as db:
            product = db.get(Product, "P-CUP")
            if product is None:
                raise RuntimeError("CANONICAL_PRODUCT_FIXTURE_MISSING")
            db.add(OrderItem(order_id=fixture_ids["approved"], product_id="P-CUP", quantity=1, paid_cents=4299))
        record["fixture_ids"] = {key: value for key, value in fixture_ids.items() if key != "prefix"}
        record["fixture_setup_tag"] = SOURCE
        with engine.connect() as connection:
            record["fixture_counts_after_setup"] = _counts(connection)

        phase = "api_setup"
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "g2-m3.6"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m3.6-cross-agent-v1"
        from app.m1 import api as api_module
        original_loader = api_module.load_settings
        api_module.load_settings = lambda **kwargs: __import__("dataclasses").replace(
            original_loader(**kwargs), orchestrator="langgraph", m3_supervisor_enabled=True,
            graph_version="g2-m3.6", execution_version="m3.6-cross-agent-v1")
        routes = {
            "M36_ORDER_PRODUCT": {"sub_strategy": "AGENT+SINGLE", "reason_code": "LIVE_BUSINESS_FACT",
                "required_capabilities": ["get_order", "get_order_items", "get_product_detail"]},
            "M36_PRODUCT_POLICY": {"sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
                "required_capabilities": ["get_product_detail", "search_knowledge"]},
            "M36_ORDER_POLICY": {"sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
                "required_capabilities": ["get_order", "search_knowledge"]},
            "M36_ORDER_POLICY_AFTER_SALES": {"sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
                "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]},
            "M36_FOUR_AGENT": {"sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
                "required_capabilities": ["get_order", "get_order_items", "get_product_detail",
                    "search_knowledge", "get_refund_status"]},
            "M36_FOREIGN_OWNER": {"sub_strategy": "AGENT+SINGLE", "reason_code": "LIVE_BUSINESS_FACT",
                "required_capabilities": ["get_order"]},
        }
        def fixed_route(query: str) -> dict:
            # Match the most specific scenario name first (e.g. the after-sales
            # route contains the shorter ORDER_POLICY marker as a prefix).
            marker = next((key for key in sorted(routes, key=len, reverse=True) if key in query), None)
            if marker is None:
                raise RuntimeError("PROBE_SCENARIO_ROUTE_MISSING")
            return {"strategy": "AGENT", "safety_decision": "ALLOW", "decision_source": "RULE",
                "needs_clarification": False, **routes[marker]}
        from app.orchestration.adapters import GraphServices
        services = GraphServices(router=fixed_route, direct=lambda _state: {}, single=lambda _state: {},
            multi=lambda _state: {}, refuse=lambda _state: {})
        app = create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
            checkpointer_factory=open_checkpointer, graph_services=services)

        phase = "multi_agent_api"
        cases = [
            ("m36-05-order-product", "M36_ORDER_PRODUCT", fixture_ids["approved"],
             "M36_ORDER_PRODUCT 请查我订单 {order} 中购买商品的详情。", ["order_logistics", "product"], "BUSINESS_FACTS"),
            ("m36-06-product-policy", "M36_PRODUCT_POLICY", fixture_ids["approved"],
             "M36_PRODUCT_POLICY 查询商品 P-CUP 的类别及其退货政策限制。", ["product", "policy"], "POLICY_ANSWER"),
            ("m36-07-order-policy", "M36_ORDER_POLICY", fixture_ids["approved"],
             "M36_ORDER_POLICY 核实订单 {order} 的签收状态及适用退款政策。",
             ["order_logistics", "policy"], "POLICY_ANSWER"),
            ("m36-04-order-policy-after-sales", "M36_ORDER_POLICY_AFTER_SALES", fixture_ids["approved"],
             "M36_ORDER_POLICY_AFTER_SALES 核实订单 {order} 退货批准和退款申请前置政策。",
             ["order_logistics", "policy", "after_sales"], "POLICY_ANSWER"),
            ("m36-08-four-agent-composite", "M36_FOUR_AGENT", fixture_ids["approved"],
             "M36_FOUR_AGENT 综合检查订单 {order} 商品、退货和退款政策及售后状态。",
             ["order_logistics", "product", "policy", "after_sales"], "POLICY_ANSWER"),
        ]
        times: list[float] = []
        try:
            with TestClient(app) as client:
                headers = _login(client, "u_demo", password)
                auth_hashes.add(hashlib.sha256(client.cookies.get("shoprelay_session").encode()).hexdigest())
                for index, (case_id, marker, order_id, template, expected_agents, expected_kind) in enumerate(cases):
                    message = template.format(order=order_id)
                    request_id = f"m36-{prefix}-{index}"
                    started = time.perf_counter()
                    response = client.post("/api/chat", headers=headers,
                        json={"message": message, "client_request_id": request_id, "user_id": "u_other"})
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                    times.append(elapsed_ms)
                    if response.status_code != 200:
                        raise RuntimeError(f"M36_CASE_HTTP_FAILED_{case_id}_{response.status_code}")
                    body = response.json()
                    run_id = body["run_id"]
                    run_ids.append(run_id)
                    session_ids.append(body["session_id"])
                    with factory() as db:
                        run = db.get(Run, run_id)
                        graph = db.get(GraphExecutionRecord, run_id)
                        observations = db.execute(select(ObservabilityRecord).where(
                            ObservabilityRecord.run_id == run_id)).scalars().all()
                    if run is None or run.status != "SUCCEEDED" or graph is None or graph.status != "SUCCEEDED":
                        raise RuntimeError(f"RUN_GRAPH_TERMINAL_MISMATCH_{case_id}")
                    checkpoint_state = run_checkpointer_async(_read_checkpoint_state(
                        database_url, settings.checkpoint_schema, run_id))
                    handoffs = checkpoint_state.get("handoffs", [])
                    targets = [row.get("target_agent") for row in handoffs]
                    if targets != expected_agents or any(row.get("status") != "COMPLETED" for row in handoffs):
                        raise RuntimeError(f"HANDOFF_SEQUENCE_INVALID_{case_id}")
                    budget = checkpoint_state.get("agent_budget") or {}
                    if budget.get("agent_calls") != len(expected_agents) or budget.get("agent_calls", 5) > 4 or \
                            budget.get("steps", 99) > 14:
                        raise RuntimeError(f"AGENT_BUDGET_INVALID_{case_id}")
                    if body.get("answer_kind") != expected_kind or not body.get("answer"):
                        raise RuntimeError(f"COMPOSITE_ANSWER_INVALID_{case_id}")
                    if "policy" in expected_agents and not body.get("citations"):
                        raise RuntimeError(f"POLICY_CITATIONS_MISSING_{case_id}")
                    event_response = client.get(f"/api/runs/{run_id}/events")
                    event_types = [row["type"] for row in event_response.json()["events"]]
                    if event_types[:2] != ["started", "route_decided"] or event_types[-2:] != ["answer", "done"] or \
                            event_types.count("agent_started") != len(expected_agents):
                        raise RuntimeError(f"SSE_SEQUENCE_INVALID_{case_id}")
                    span_values = [json.loads(row.payload_json) for row in observations if row.record_type == "span"]
                    metric_values = [json.loads(row.payload_json) for row in observations if row.record_type == "metric"]
                    log_values = [json.loads(row.payload_json) for row in observations if row.record_type == "log"]
                    agent_spans = [row for row in span_values if row.get("node_name", "").startswith("m3.agent.")]
                    tool_spans = [row for row in span_values if row.get("node_name", "").startswith("m3.tool.")]
                    if {row["node_name"].removeprefix("m3.agent.") for row in agent_spans} != set(expected_agents):
                        raise RuntimeError(f"AGENT_SPAN_SET_INVALID_{case_id}")
                    if any(row.get("status") != "OK" for row in tool_spans):
                        raise RuntimeError(f"TOOL_SPAN_STATUS_INVALID_{case_id}")
                    agent_tool_span_ids = {row["span_id"] for row in agent_spans + tool_spans}
                    metric_span_ids = {row.get("span_id") for row in metric_values if row.get("span_id")}
                    log_span_ids = {row.get("span_id") for row in log_values if row.get("span_id")}
                    if not agent_tool_span_ids <= metric_span_ids or not agent_tool_span_ids <= log_span_ids:
                        raise RuntimeError(f"OBSERVABILITY_RECORD_COVERAGE_INVALID_{case_id}")
                    sensitive_values = [request_id, order_id, password, "u_demo", "u_other"]
                    serialized_logs = json.dumps(log_values, ensure_ascii=False)
                    if any(value and value in serialized_logs for value in sensitive_values):
                        raise RuntimeError(f"STRUCTURED_LOG_LEAK_DETECTED_{case_id}")
                    result = {"case_id": case_id, "run_id": run_id, "trace_id": body.get("trace_id"),
                        "checkpoint_id": graph.checkpoint_id, "run_status": run.status,
                        "graph_status": graph.status, "route_source": "deterministic scenario injection",
                        "answer_kind": body.get("answer_kind"), "answer": body.get("answer"),
                        "citations": body.get("citations", []), "business_results": body.get("business_results", {}),
                        "tool_references": body.get("tool_references", []), "agents": targets,
                        "handoffs": [{"source_agent": row.get("source_agent"), "target_agent": row.get("target_agent"),
                            "capabilities": row.get("required_capabilities"), "status": row.get("status"),
                            "context_refs": row.get("context_refs"), "evidence_refs": row.get("evidence_refs")}
                            for row in handoffs],
                        "agent_calls": budget.get("agent_calls"), "controlled_steps": budget.get("steps"),
                        "tool_spans": tool_spans, "sse_event_types": event_types,
                        "observability_records": {"span_count": len(span_values), "metric_count": len(metric_values),
                            "log_count": len(log_values), "agent_tool_span_metric_log_coverage": True,
                            "structured_logs_sensitive_value_scan": "PASS"},
                        "request_latency_ms": elapsed_ms,
                        "retrieval": body.get("policy_retrieval"),
                        "model_requests": 0, "business_write_executions": 0}
                    if case_id == "m36-05-order-product":
                        text_answer = result["answer"]
                        if "4299" not in text_answer or "8900" not in text_answer:
                            raise RuntimeError("HISTORICAL_AND_CATALOG_PRICE_NOT_SEPARATED")
                        if "P-CUP" not in text_answer:
                            raise RuntimeError("ORDER_PRODUCT_LINK_MISSING")
                    if case_id == "m36-08-four-agent-composite":
                        after = result["business_results"].get("after_sales", {})
                        verified = (after.get("after_sales") or {}).get("refunds", {}).get("latest_request", {})
                        if verified.get("payment_settlement_verified") is not False:
                            raise RuntimeError("PAYMENT_SETTLEMENT_WAS_NOT_EXPLICITLY_UNVERIFIED")
                        if "8900" not in result["answer"] or "已通过订单数据核实" not in result["answer"]:
                            raise RuntimeError("FOUR_AGENT_COMPOSITION_CONTENT_MISSING")
                    record["run_results"][case_id] = result
                    record["agent_calls"].append(len(expected_agents))
                    record["tool_latency_ms"].extend(float(row.get("duration_ms") or 0) for row in tool_spans)
                    if index == 0:
                        replay = client.post("/api/chat", headers=headers,
                            json={"message": message, "client_request_id": request_id, "user_id": "u_other"})
                        replay_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
                        if replay.status_code != 200 or replay.json().get("run_id") != run_id or \
                                sum(row["type"] == "tool_started" for row in replay_events) != len(tool_spans):
                            raise RuntimeError("IDEMPOTENCY_REPLAY_REEXECUTED_TOOLS")
                        record["idempotency_replay"] = {"same_run": True, "new_tool_calls": 0}

                # The owner boundary is exercised through the real API. A foreign
                # order ID is deliberately requested by the authenticated demo user.
                foreign_order = fixture_ids["foreign"]
                foreign = client.post("/api/chat", headers=headers,
                    json={"message": f"M36_FOREIGN_OWNER 请查询订单 {foreign_order}。",
                          "client_request_id": f"m36-{prefix}-foreign", "user_id": "u_other"})
                if foreign.status_code not in {502, 503}:
                    raise RuntimeError("FOREIGN_ORDER_NOT_REJECTED")
                with factory() as db:
                    failed = db.execute(select(Run).where(Run.user_id == "u_demo",
                        Run.client_request_id == f"m36-{prefix}-foreign")).scalar_one_or_none()
                if failed is None or failed.error_code != "ORDER_NOT_FOUND":
                    raise RuntimeError("FOREIGN_ORDER_FAILURE_SEMANTICS_INVALID")
                run_ids.append(failed.run_id)
                session_ids.append(failed.session_id)
                record["foreign_order_access"] = {"rejected": True, "error_code": failed.error_code,
                    "sensitive_data_returned": False}

                other = TestClient(app)
                try:
                    other_headers = _login(other, "u_other", password)
                    auth_hashes.add(hashlib.sha256(other.cookies.get("shoprelay_session").encode()).hexdigest())
                    protected_run = next(iter(record["run_results"].values()))["run_id"]
                    if other.get(f"/api/runs/{protected_run}", headers=other_headers).status_code != 404 or \
                            other.get(f"/api/runs/{protected_run}/events", headers=other_headers).status_code != 404:
                        raise RuntimeError("CROSS_USER_RUN_OR_EVENT_ACCESS_ALLOWED")
                    record["cross_user_run_event_isolation"] = True
                finally:
                    other.close()
        finally:
            app.state.engine.dispose()

        with engine.connect() as connection:
            record["fixture_counts_after_api"] = _counts(connection)
        if record["fixture_counts_after_api"] != record["fixture_counts_after_setup"]:
            raise RuntimeError("READ_ONLY_API_MUTATED_BUSINESS_TABLES")
        record["api_business_data_mutations"] = 0
        record["business_write_executions"] = 0
        record["deepseek_requests"] = 0
        record["request_latency_ms"] = times
        record["request_latency_summary_ms"] = {"count": len(times), "p50": median(times),
            "p95_nearest_rank": sorted(times)[max(0, int((len(times) * .95 + .999999)) - 1)]}
        tool_times = record["tool_latency_ms"]
        record["tool_latency_summary_ms"] = {"count": len(tool_times),
            "p50": median(tool_times) if tool_times else None,
            "p95_nearest_rank": sorted(tool_times)[max(0, int((len(tool_times) * .95 + .999999)) - 1)]
                if tool_times else None}

        phase = "restart_recovery"
        fresh_engine = make_engine(database_url)
        fresh_app = create_app(database_url=database_url, engine=fresh_engine,
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer, graph_services=services)
        try:
            with TestClient(fresh_app) as client:
                _login(client, "u_demo", password)
                recovery: dict[str, bool] = {}
                for case in record["run_results"].values():
                    read_run = client.get(f"/api/runs/{case['run_id']}")
                    events = client.get(f"/api/runs/{case['run_id']}/events")
                    if read_run.status_code != 200 or read_run.json()["run"]["status"] != "SUCCEEDED" or \
                            events.status_code != 200 or [row["type"] for row in events.json()["events"]] != case["sse_event_types"]:
                        raise RuntimeError("RESTART_RUN_EVENT_RECOVERY_FAILED")
                    recovery[case["case_id"]] = True
                record["restart_recovery"] = recovery
        finally:
            fresh_app.state.engine.dispose()
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["failure_phase"] = phase
        record["failure_type"] = type(exc).__name__
        message = str(exc)
        for secret_value in [password, *fixture_ids.values(), *run_ids, *session_ids]:
            if secret_value:
                message = message.replace(secret_value, "[REDACTED]")
        record["failure_code"] = getattr(exc, "code", None) or (
            message if message.isupper() and len(message) < 120 else "PROBE_FAILED")
        record["failure_message_sanitized"] = message[:240]
    finally:
        api_module_ref = api_module
        if api_module_ref is not None and original_loader is not None:
            api_module_ref.load_settings = original_loader
        if database_url and settings:
            for run_id in run_ids:
                try:
                    run_checkpointer_async(_delete_checkpoint(database_url, settings.checkpoint_schema, run_id))
                except Exception:
                    record["checkpoint_cleanup_warning"] = True
        if factory:
            try:
                with factory() as db:
                    if run_ids:
                        session_ids.extend(db.execute(select(Run.session_id).where(Run.run_id.in_(run_ids))).scalars().all())
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
                        db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids)))
                        db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
                        if session_ids:
                            db.execute(delete(Message).where(Message.session_id.in_(set(session_ids))))
                        db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
                    if session_ids:
                        db.execute(delete(ConversationSession).where(
                            ConversationSession.session_id.in_(set(session_ids))))
                    if auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                    if fixture_ids:
                        fixture_orders = [value for key, value in fixture_ids.items() if key != "prefix"]
                        db.execute(delete(OrderItem).where(OrderItem.order_id.in_(fixture_orders)))
                        db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.source_ref == f"{SOURCE}:{prefix}"))
                        db.execute(delete(Refund).where(Refund.source_ref == f"{SOURCE}:{prefix}"))
                        # Probe fixtures have per-return source refs because the
                        # database enforces uniqueness over simulated returns.
                        db.execute(delete(ReturnRequest).where(
                            ReturnRequest.source_ref.like(f"{SOURCE}:{prefix}:%")))
                        db.execute(delete(Order).where(Order.order_id.in_(fixture_orders)))
                    db.commit()
                record["exact_run_and_fixture_cleanup"] = True
            except Exception as exc:
                record["exact_run_and_fixture_cleanup"] = False
                record["cleanup_error_type"] = type(exc).__name__
                if record["status"] == "PASS":
                    record["status"] = "PASS_WITH_CLEANUP_WARNING"
            try:
                if saved_credentials:
                    with factory() as db:
                        for user_id, prior in saved_credentials.items():
                            current = db.get(DemoCredential, user_id)
                            if current:
                                current.password_hash = prior["password_hash"]
                                current.active = prior["active"]
                                current.updated_at = prior["updated_at"]
                        db.commit()
                record["demo_credentials_restored"] = len(saved_credentials) == 2
            except Exception:
                record["demo_credentials_restored"] = False
                record["status"] = "FAIL"
                record["failure_code"] = "DEMO_CREDENTIAL_RESTORE_FAILED"
            try:
                with engine.connect() as connection:
                    record["fixture_counts_after_cleanup"] = _counts(connection)
                record["fixtures_removed"] = record.get("fixture_counts_before") == record.get("fixture_counts_after_cleanup")
                if not record["fixtures_removed"] and record["status"] == "PASS":
                    record["status"] = "PASS_WITH_CLEANUP_WARNING"
            except Exception:
                record["fixtures_removed"] = False
            if engine:
                engine.dispose()
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        record["finished_at"] = _utc_text()
        if output_path:
            path = Path(output_path)
            if path.exists():
                raise FileExistsError("probe output already exists; choose a new versioned path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/probes/m3.6-cross-agent-postgres-20261022-r2.json")
    args = parser.parse_args()
    result = probe(args.output)
    print(json.dumps({key: result.get(key) for key in (
        "status", "failure_phase", "failure_code", "database", "alembic_revision",
        "deepseek_requests", "business_write_executions", "run_results", "restart_recovery",
        "exact_run_and_fixture_cleanup", "fixtures_removed", "demo_credentials_restored")},
        ensure_ascii=False, indent=2))
