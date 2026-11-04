"""M3.5-DATA isolated PostgreSQL after-sales fixtures and three-agent probe.

Only targets shoprelay_m11_test. Inserted business rows are tagged
SIMULATED_TEST_DATA and removed by exact fixture identifiers in finally.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median

from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import (LATEST_SCHEMA_REVISION, current_revision, make_engine,
                       make_session_factory, upgrade_database)
from app.m1.models import (AfterSalesEvent, AuthSession, ConversationSession, DemoCredential,
    GraphExecutionRecord, Message, ObservabilityRecord, Order, Refund, ReturnRequest, Run, RunEvent, Trace)
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import (checkpoint_schema_ready, open_checkpointer,
                                             run_checkpointer_async)
from scripts.g2_test_database import resolve_test_database_url


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "SIMULATED_TEST_DATA"


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime | None = None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _login(client: TestClient, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: csrf}, json={"username": "u_demo", "password": password})
    if response.status_code != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return {"Origin": str(client.base_url).rstrip("/"),
            CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


async def _delete_checkpoint(database_url: str, schema: str, run_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(run_id)


async def _read_checkpoint_state(database_url: str, schema: str, run_id: str) -> dict:
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        snapshot = await saver.aget_tuple(config)
    return (snapshot.checkpoint.get("channel_values", {}) if snapshot else {})


def _fixture_set(factory, prefix: str) -> dict[str, str]:
    now = _utc()
    paid = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
    delivered = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
    cases = {
        "approved": ("DELIVERED", delivered, "APPROVED"),
        "pending": ("DELIVERED", delivered, "UNDER_REVIEW"),
        "rejected": ("DELIVERED", delivered, "REJECTED"),
        "paid_unshipped": ("PAID", None, None),
        "quality_unknown": ("DELIVERED", delivered, None),
        "no_application": ("DELIVERED", delivered, None),
        "foreign": ("DELIVERED", delivered, "APPROVED"),
    }
    ids = {key: f"ORD-M35D-{prefix}-{key.upper()}" for key in cases}
    ids["prefix"] = prefix
    with factory.begin() as db:
        for key, (status, delivered_at, _) in cases.items():
            db.add(Order(order_id=ids[key], user_id="u_other" if key == "foreign" else "u_demo",
                status=status, paid_at=_utc_text(paid), delivered_at=_utc_text(delivered_at) if delivered_at else None,
                paid_cents=4299, version=1))
        db.flush()
        for key in ("approved", "pending", "rejected", "foreign"):
            status = cases[key][2]
            submitted = now - timedelta(days=2)
            review = submitted + timedelta(hours=1)
            terminal = review + timedelta(hours=2)
            return_id = f"RET-{prefix}-{key.upper()}"
            row = ReturnRequest(return_id=return_id, order_id=ids[key], status=status,
                reason="SIMULATED_TEST_DATA", created_at=_utc_text(submitted),
                submitted_at=submitted, review_started_at=review,
                approved_at=terminal if status == "APPROVED" else None,
                rejected_at=terminal if status == "REJECTED" else None,
                # The current schema enforces unique simulated return source_ref;
                # keep each return fixture independently attributable.
                source_type=SOURCE, source_ref=f"{SOURCE}:{prefix}:{key}",
                evidence_ref=f"fixture:{prefix}:{key}:return")
            db.add(row)
            db.flush()
            events = [("RETURN_SUBMITTED", submitted)]
            if status in {"UNDER_REVIEW", "APPROVED", "REJECTED"}:
                events.append(("RETURN_REVIEW_STARTED", review))
            if status in {"APPROVED", "REJECTED"}:
                events.append((f"RETURN_{status}", terminal))
            for n, (event_type, occurred) in enumerate(events):
                db.add(AfterSalesEvent(event_id=f"{return_id}:EV{n}", order_id=ids[key],
                    return_id=return_id, refund_id=None, event_type=event_type,
                    occurred_at=occurred, recorded_at=occurred + timedelta(seconds=1),
                    actor_type="SIMULATED_TEST_DATA", source_type=SOURCE,
                    source_ref=f"{SOURCE}:{prefix}",
                    details_json=json.dumps({"fixture": prefix, "origin": SOURCE})))
            if key == "approved":
                refund_id = f"RFD-{prefix}-APPROVED"
                refund_submitted = terminal + timedelta(days=1)
                db.add(Refund(refund_id=refund_id, order_id=ids[key], return_id=return_id,
                    status="SUBMITTED", amount_cents=4299, reason=SOURCE,
                    created_at=_utc_text(refund_submitted), submitted_at=refund_submitted,
                    source_type=SOURCE, source_ref=f"{SOURCE}:{prefix}"))
                db.flush()
                db.add(AfterSalesEvent(event_id=f"{refund_id}:EV0", order_id=ids[key],
                    return_id=None, refund_id=refund_id, event_type="REFUND_SUBMITTED",
                    occurred_at=refund_submitted, recorded_at=refund_submitted + timedelta(seconds=1),
                    actor_type="SIMULATED_TEST_DATA", source_type=SOURCE,
                    source_ref=f"{SOURCE}:{prefix}",
                    details_json=json.dumps({"fixture": prefix, "origin": SOURCE})))
    return ids


def probe() -> dict:
    record: dict = {
        "schema_version": "shoprelay.m3.5-after-sales-data-postgres.v1",
        "started_at": _utc_text(), "status": "NOT_RUN",
        "database_expected": "shoprelay_m11_test", "data_origin": SOURCE,
        "deepseek_requests": 0, "business_write_executions": 0,
        "api_business_data_mutations": 0, "test_fixture_rows_are_setup_not_api_writes": True,
        "route_source": "deterministic AGENT+MULTI injection; not Router-quality evidence",
        "case_results": {}, "tool_latency_ms": [], "tool_latency_by_capability": {},
    }
    engine = factory = None
    database_url = None
    settings = None
    api_module = None
    original_loader = None
    password = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(5).upper()
    fixture_ids: dict[str, str] = {}
    original_credential = None
    run_ids: list[str] = []
    session_ids: list[str] = []
    auth_hashes: set[str] = set()
    old_env = {key: os.environ.get(key) for key in (
        "SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED",
        "SHOPRELAY_GRAPH_VERSION", "SHOPRELAY_EXECUTION_VERSION")}
    api = None
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
            record["postgres_version"] = connection.execute(
                text("select current_setting('server_version')")).scalar_one()
        if record["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        before_revision = current_revision(database_url)
        if before_revision not in {"20261021_07", LATEST_SCHEMA_REVISION}:
            raise RuntimeError("UNEXPECTED_DATABASE_REVISION")
        if before_revision != LATEST_SCHEMA_REVISION:
            upgrade_database(database_url)
        record["migration"] = {"before": before_revision, "after": current_revision(database_url),
                               "expected": LATEST_SCHEMA_REVISION}
        if record["migration"]["after"] != LATEST_SCHEMA_REVISION:
            raise RuntimeError("MIGRATION_REVISION_MISMATCH")
        settings = load_settings(require_api_key=False)
        record["checkpointer_schema"] = settings.checkpoint_schema
        record["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")
        with engine.connect() as connection:
            record["fixture_table_counts_before"] = {
                name: connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
                for name in ("orders", "returns", "refunds", "after_sales_events")}

        phase = "fixture_setup"
        with factory() as db:
            credential = db.get(DemoCredential, "u_demo")
            if credential is None:
                raise RuntimeError("DEMO_CREDENTIAL_MISSING")
            original_credential = {"password_hash": credential.password_hash,
                "active": credential.active, "updated_at": credential.updated_at}
            set_demo_password(db, "u_demo", password)
            db.commit()
        fixture_ids = _fixture_set(factory, prefix)
        record["simulated_fixture_ids"] = {key: value for key, value in fixture_ids.items() if key != "prefix"}
        record["simulated_fixture_statuses"] = {
            "approved": "APPROVED", "pending": "UNDER_REVIEW", "rejected": "REJECTED",
            "paid_unshipped": "PAID", "quality_unknown": "NO_QUALITY_VERIFICATION",
            "no_application": "NO_APPLICATION_RECORD", "missing_source": "NOT_AVAILABLE",
            "foreign": "OTHER_OWNER"}
        record["fixture_setup_tag"] = SOURCE

        phase = "three_agent_api"
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "g2-m3.5-data"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m3.5-after-sales-data-v1"
        route = {"strategy": "AGENT", "sub_strategy": "AGENT+MULTI", "safety_decision": "ALLOW",
            "reason_code": "MULTI_POLICY_CONDITIONS", "decision_source": "RULE",
            "required_capabilities": ["get_order", "get_order_items", "search_knowledge", "get_refund_status"],
            "needs_clarification": False}
        single_policy_route = {**route, "sub_strategy": "AGENT+SINGLE",
            "reason_code": "SINGLE_POLICY_QUESTION", "required_capabilities": ["search_knowledge"]}
        def fixed_route(query: str) -> dict:
            # The exact phrase is only a probe scenario selector; no production
            # routing rule or case-id behavior is altered.
            return single_policy_route if "probe simple-policy check" in query else route
        services = GraphServices(router=fixed_route, direct=lambda _state: {},
            single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
        from app.m1 import api as api_module
        original_loader = api_module.load_settings
        api_module.load_settings = lambda **kwargs: replace(original_loader(**kwargs),
            orchestrator="langgraph", m3_supervisor_enabled=True, graph_version="g2-m3.5-data",
            execution_version="m3.5-after-sales-data-v1")
        app = create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
            checkpointer_factory=open_checkpointer, graph_services=services)
        cases = {
            "A_APPROVED_RETURN": (fixture_ids["approved"],
                "请核实订单 {order} 的退货申请状态，并说明已签收订单申请退款需要满足的政策前置条件。",
                "refund_requires_approved_return_when_delivered", "SATISFIED"),
            "B_PENDING_REVIEW": (fixture_ids["pending"],
                "请核实订单 {order} 的退货审核状态，并判断目前是否满足已签收订单退款申请前置条件。",
                "refund_requires_approved_return_when_delivered", "NOT_SATISFIED"),
            "C_QUALITY_FACT_UNKNOWN": (fixture_ids["quality_unknown"],
                "请核实订单 {order} 的签收时间，并说明若瑕疵发生在签收30天之后政策要求如何处理；当前不能确认瑕疵发生日期。",
                "quality_return_window", "UNKNOWN"),
        }
        try:
            with TestClient(app) as client:
                headers = _login(client, password)
                auth_hashes.add(hashlib.sha256(client.cookies.get("shoprelay_session").encode()).hexdigest())
                for key, (order_id, template, condition_id, expected) in cases.items():
                    query = template.format(order=order_id)
                    client_request_id = f"m35d-{prefix}-{key}"
                    response = client.post("/api/chat", headers=headers,
                        json={"message": query, "client_request_id": client_request_id,
                              "user_id": "u_other"})
                    if response.status_code != 200:
                        with factory() as failed_db:
                            failed = failed_db.execute(select(Run).where(
                                Run.user_id == "u_demo",
                                Run.client_request_id == client_request_id)).scalar_one_or_none()
                            if failed is not None:
                                run_ids.append(failed.run_id)
                                session_ids.append(failed.session_id)
                                record.setdefault("failed_runs", []).append({
                                    "run_id": failed.run_id, "status": failed.status,
                                    "error_code": failed.error_code,
                                    "error_detail": json.loads(failed.error_detail_json)
                                        if failed.error_detail_json else None})
                        raise RuntimeError(f"API_FAILED_{key}_{response.status_code}")
                    body = response.json()
                    if body.get("status") != "SUCCEEDED" or body.get("strategy") != "AGENT":
                        raise RuntimeError(f"RUN_FAILED_{key}")
                    business = body.get("business_results") or {}
                    after = business.get("after_sales") or {}
                    condition = next((item for item in business.get("conditions", [])
                                      if item.get("condition_id") == condition_id), None)
                    if condition is None or condition.get("evaluation_result") != expected:
                        raise RuntimeError(f"CONDITION_RESULT_INVALID_{key}")
                    if business.get("order", {}).get("order_id") != order_id:
                        raise RuntimeError(f"ORDER_SOURCE_MISMATCH_{key}")
                    if business.get("user_claims_used_as_verified_facts") is not False:
                        raise RuntimeError(f"USER_CLAIM_PROMOTION_{key}")
                    if not body.get("citations") or not body.get("knowledge_version"):
                        raise RuntimeError(f"POLICY_EVIDENCE_MISSING_{key}")
                    retrieval_meta = body.get("policy_retrieval") or {}
                    evaluation_at = datetime.fromisoformat(
                        str(retrieval_meta.get("evaluation_at", "")).replace("Z", "+00:00"))
                    fingerprint = retrieval_meta.get("index_fingerprint")
                    for citation in body["citations"]:
                        start = datetime.fromisoformat(str(citation["effective_from"]).replace("Z", "+00:00"))
                        end = datetime.fromisoformat(str(citation["effective_to"]).replace("Z", "+00:00")) \
                            if citation.get("effective_to") else None
                        if citation.get("knowledge_version") != body["knowledge_version"] or \
                                citation.get("index_fingerprint") != fingerprint or \
                                citation.get("policy_version") != citation.get("version") or \
                                start > evaluation_at or (end is not None and evaluation_at >= end):
                            raise RuntimeError(f"POLICY_CITATION_VERSION_OR_TIME_INVALID_{key}")
                    channels = retrieval_meta.get("retrieval_channels") or {}
                    models = retrieval_meta.get("models") or {}
                    if retrieval_meta.get("retrieval_mode") != "HYBRID_RERANK" or \
                            not channels.get("bm25") or not channels.get("vector"):
                        raise RuntimeError(f"HYBRID_CHANNELS_NOT_EXECUTED_{key}")
                    if not models.get("reranker"):
                        raise RuntimeError(f"HYBRID_RERANKER_NOT_EXECUTED_{key}")
                    if retrieval_meta.get("degraded") and not retrieval_meta.get("failures"):
                        raise RuntimeError(f"HYBRID_DEGRADATION_REASON_MISSING_{key}")
                    if any(not item.get("policy_source_id") and item.get("condition_id") == condition_id
                           for item in business.get("conditions", [])):
                        raise RuntimeError(f"CONDITION_POLICY_SOURCE_MISSING_{key}")
                    if key == "A_APPROVED_RETURN":
                        latest = (after.get("returns") or {}).get("latest_request") or {}
                        if latest.get("status") != "APPROVED" or latest.get("verified") is not True:
                            raise RuntimeError("APPROVED_RETURN_NOT_AUDIT_VERIFIED")
                        refund = (after.get("refunds") or {}).get("latest_request") or {}
                        if refund.get("status") != "SUBMITTED" or refund.get("verified") is not True or \
                                refund.get("return_id") != latest.get("request_id") or \
                                refund.get("linked_return_status") != "APPROVED" or \
                                refund.get("linked_return_verified") is not True:
                            raise RuntimeError("REFUND_RETURN_LINK_NOT_VERIFIED")
                    if key == "B_PENDING_REVIEW":
                        latest = (after.get("returns") or {}).get("latest_request") or {}
                        if latest.get("status") != "UNDER_REVIEW" or latest.get("verified") is not True:
                            raise RuntimeError("PENDING_RETURN_NOT_AUDIT_VERIFIED")
                    if key == "C_QUALITY_FACT_UNKNOWN":
                        review = next((item for item in business.get("conditions", [])
                            if item.get("condition_id") == "return_manual_review"), None)
                        if review is None or review.get("evaluation_result") != "POLICY_REQUIRES_MANUAL_REVIEW":
                            raise RuntimeError("OVER_WINDOW_MANUAL_REVIEW_RULE_NOT_REPORTED")
                    events = client.get(f"/api/runs/{body['run_id']}/events").json()["events"]
                    event_types = [item["type"] for item in events]
                    if event_types[:2] != ["started", "route_decided"] or event_types[-2:] != ["answer", "done"]:
                        raise RuntimeError(f"SSE_ORDER_INVALID_{key}")
                    if event_types.count("agent_started") != 3 or event_types.count("tool_started") != 4:
                        raise RuntimeError(f"AGENT_TOOL_COUNT_INVALID_{key}")
                    with factory() as db:
                        run = db.get(Run, body["run_id"])
                        graph = db.get(GraphExecutionRecord, body["run_id"])
                        obs = db.execute(select(ObservabilityRecord).where(
                            ObservabilityRecord.run_id == body["run_id"])).scalars().all()
                    checkpoint_state = run_checkpointer_async(_read_checkpoint_state(
                        database_url, settings.checkpoint_schema, body["run_id"]))
                    spans = [json.loads(row.payload_json) for row in obs if row.record_type == "span"]
                    tool_spans = [item for item in spans if item.get("node_name", "").startswith("m3.tool.")]
                    if run is None or run.status != "SUCCEEDED" or graph is None or \
                            graph.status != "SUCCEEDED" or not graph.checkpoint_id or len(tool_spans) != 4:
                        raise RuntimeError(f"PERSISTENCE_INVALID_{key}")
                    if any(item.get("status") != "OK" for item in tool_spans):
                        raise RuntimeError(f"TOOL_SPAN_FAILED_{key}")
                    for item in tool_spans:
                        capability = item.get("node_name", "").removeprefix("m3.tool.")
                        duration_ms = float(item.get("duration_ms") or 0)
                        record["tool_latency_ms"].append(duration_ms)
                        record["tool_latency_by_capability"].setdefault(capability, []).append(duration_ms)
                    run_ids.append(body["run_id"])
                    session_ids.append(body["session_id"])
                    record["case_results"][key] = {
                        "order_id": order_id, "run_id": body["run_id"], "trace_id": body["trace_id"],
                        "checkpoint_id": graph.checkpoint_id, "run_status": run.status,
                        "agent_handoffs": [item.get("target_agent") for item in checkpoint_state.get("handoffs", [])],
                        "checkpoint_handoff_count": len(checkpoint_state.get("handoffs", [])),
                        "condition": condition, "after_sales_availability": after.get("availability"),
                        "all_conditions": business.get("conditions", []),
                        "application_record_state": after.get("application_record_state"),
                        "verified_return": (after.get("returns") or {}).get("latest_request"),
                        "verified_refund": (after.get("refunds") or {}).get("latest_request"),
                        "policy_version": body.get("knowledge_version"),
                        "retrieval_mode": retrieval_meta.get("retrieval_mode"),
                        "retrieval_diagnostics": retrieval_meta.get("diagnostics"),
                        "retrieval_models": models, "retrieval_channels": channels,
                        "retrieval_degraded": retrieval_meta.get("degraded"),
                        "retrieval_failures": retrieval_meta.get("failures"),
                        "answer_kind": body.get("answer_kind"), "answer": body.get("answer"),
                        "citations": body["citations"], "sse_event_types": event_types,
                        "tool_call_count": len(tool_spans), "tool_spans": tool_spans,
                    }
                    if key == "A_APPROVED_RETURN":
                        replay = client.post("/api/chat", headers=headers,
                            json={"message": query, "client_request_id": client_request_id,
                                  "user_id": "u_other"})
                        replay_events = client.get(f"/api/runs/{body['run_id']}/events").json()["events"]
                        if replay.status_code != 200 or replay.json().get("run_id") != body["run_id"] or \
                                sum(item["type"] == "tool_started" for item in replay_events) != 4:
                            raise RuntimeError("IDEMPOTENCY_REEXECUTED_TOOLS")
                        record["idempotency_replay_no_second_execution"] = True
                # Exercise the existing Policy Agent's single-policy default via
                # the same PostgreSQL-backed API, separate from the MULTI cases.
                simple_query = "probe simple-policy check：未发货订单的退款申请条件是什么？"
                simple_request_id = f"m35d-{prefix}-SINGLE_POLICY_BM25"
                simple_response = client.post("/api/chat", headers=headers,
                    json={"message": simple_query, "client_request_id": simple_request_id})
                if simple_response.status_code != 200:
                    raise RuntimeError("SINGLE_POLICY_API_FAILED")
                simple_body = simple_response.json()
                if simple_body.get("status") != "SUCCEEDED" or \
                        simple_body.get("policy_retrieval", {}).get("retrieval_mode") != "BM25" or \
                        not simple_body.get("citations"):
                    raise RuntimeError("SINGLE_POLICY_DID_NOT_USE_BM25")
                simple_meta = simple_body.get("policy_retrieval") or {}
                if any(row.get("knowledge_version") != simple_body.get("knowledge_version") or
                       row.get("index_fingerprint") != simple_meta.get("index_fingerprint")
                       for row in simple_body.get("citations", [])):
                    raise RuntimeError("SINGLE_POLICY_CITATION_SNAPSHOT_MISMATCH")
                simple_channels = simple_body.get("policy_retrieval", {}).get("retrieval_channels") or {}
                if not simple_channels.get("bm25") or simple_channels.get("vector"):
                    raise RuntimeError("SINGLE_POLICY_CHANNEL_SELECTION_INVALID")
                simple_run_id = simple_body["run_id"]
                run_ids.append(simple_run_id)
                session_ids.append(simple_body["session_id"])
                simple_events = client.get(f"/api/runs/{simple_run_id}/events").json()["events"]
                record["simple_policy_bm25"] = {
                    "run_id": simple_run_id, "trace_id": simple_body["trace_id"],
                    "run_status": simple_body["status"], "answer": simple_body["answer"],
                    "citation_ids": [item.get("chunk_id") for item in simple_body["citations"]],
                    "retrieval_mode": simple_body["policy_retrieval"]["retrieval_mode"],
                    "retrieval_channels": simple_channels,
                    "retrieval_diagnostics": simple_body.get("policy_retrieval", {}).get("diagnostics"),
                    "retrieval_models": simple_body.get("policy_retrieval", {}).get("models"),
                    "retrieval_latency_ms": simple_body["policy_retrieval"].get("latency_ms"),
                    "sse_event_types": [item["type"] for item in simple_events],
                }
                # Cross-owner status access must conceal existence.
                from app.m3.after_sales_tools import AfterSalesReadRepository
                from app.m3.order_tools import ToolExecutionError
                try:
                    AfterSalesReadRepository(factory).get_refund_status(fixture_ids["foreign"], "u_demo")
                except ToolExecutionError as exc:
                    if exc.code != "ORDER_NOT_FOUND":
                        raise
                    record["cross_owner_query"] = "ORDER_NOT_FOUND"
                else:
                    raise RuntimeError("CROSS_OWNER_AFTER_SALES_READ_ALLOWED")
        finally:
            app.state.engine.dispose()
            api_module.load_settings = original_loader
            api_module = None
            original_loader = None

        phase = "read_only_state_matrix"
        from app.m3.after_sales_tools import AfterSalesReadRepository
        repo = AfterSalesReadRepository(factory)
        direct_states = {}
        for key in ("approved", "pending", "rejected", "paid_unshipped", "quality_unknown", "no_application"):
            payload = repo.get_refund_status(fixture_ids[key], "u_demo")
            direct_states[key] = {"availability": payload.get("availability"),
                "application_record_state": payload.get("application_record_state"),
                "return_status": ((payload.get("returns") or {}).get("latest_request") or {}).get("status")}
        if direct_states["approved"]["return_status"] != "APPROVED" or \
                direct_states["pending"]["return_status"] != "UNDER_REVIEW" or \
                direct_states["rejected"]["return_status"] != "REJECTED" or \
                direct_states["no_application"]["application_record_state"] != "NO_APPLICATION_RECORD":
            raise RuntimeError("AFTER_SALES_STATE_MATRIX_INVALID")
        record["direct_state_matrix"] = direct_states
        from app.m3.after_sales_tools import evaluate_policy_conditions
        from app.m3.order_tools import SqlAlchemyOrderReadRepository
        paid_order = SqlAlchemyOrderReadRepository(factory).get_order(fixture_ids["paid_unshipped"], "u_demo")
        paid_policy = {"evidence": simple_body.get("citations", [])}
        paid_eval = evaluate_policy_conditions(paid_order, repo.get_refund_status(
            fixture_ids["paid_unshipped"], "u_demo"), paid_policy,
            query="请说明已付款未发货订单的退款申请条件。")
        paid_condition = paid_eval["conditions"][0]
        if paid_condition.get("evaluation_result") != "SATISFIED" or \
                paid_condition.get("condition_id") != "refund_request_for_paid_unshipped_order":
            raise RuntimeError("PAID_UNSHIPPED_REFUND_CONDITION_INVALID")
        record["paid_unshipped_policy_condition"] = paid_condition
        approved_case = record["case_results"]["A_APPROVED_RETURN"]
        delivered_policy = {"evidence": approved_case["citations"]}
        for case_key in ("rejected", "no_application"):
            delivered_order = SqlAlchemyOrderReadRepository(factory).get_order(
                fixture_ids[case_key], "u_demo")
            evaluation = evaluate_policy_conditions(delivered_order,
                repo.get_refund_status(fixture_ids[case_key], "u_demo"), delivered_policy,
                query="已签收订单可以申请退款吗？")
            condition = evaluation["conditions"][0]
            expected = "NOT_SATISFIED"
            if condition.get("evaluation_result") != expected:
                raise RuntimeError(f"DELIVERED_{case_key.upper()}_CONDITION_INVALID")
            record[f"{case_key}_policy_condition"] = condition
        record["missing_source_test"] = "covered by SQLite repository regression; no PostgreSQL tables dropped"
        # Verify current policy service selection was HYBRID only for MULTI; each
        # route was deliberately MULTI, so a reranker load is expected.
        record["retrieval_modes"] = sorted({item["retrieval_mode"] for item in record["case_results"].values()})

        phase = "restart_recovery"
        fresh_engine = make_engine(database_url)
        fresh_app = create_app(database_url=database_url, engine=fresh_engine,
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer, graph_services=services)
        try:
            with TestClient(fresh_app) as client:
                _login(client, password)
                recoverable = list(record["case_results"].values())
                if record.get("simple_policy_bm25"):
                    recoverable.append(record["simple_policy_bm25"])
                for case in recoverable:
                    recovered = client.get(f"/api/runs/{case['run_id']}")
                    events = client.get(f"/api/runs/{case['run_id']}/events")
                    if recovered.status_code != 200 or recovered.json()["run"]["status"] != "SUCCEEDED":
                        raise RuntimeError("RUN_RESTART_RECOVERY_FAILED")
                    if events.status_code != 200 or [item["type"] for item in events.json()["events"]] != case["sse_event_types"]:
                        raise RuntimeError("EVENT_RESTART_RECOVERY_FAILED")
            record["restart_recovery"] = True
        finally:
            fresh_app.state.engine.dispose()
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["failure_phase"] = phase
        record["failure_type"] = type(exc).__name__
        record["failure_code"] = getattr(exc, "code", None) or (
            str(exc) if str(exc).isupper() and len(str(exc)) < 100 else "PROBE_FAILED")
    finally:
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
                        session_ids.extend(db.execute(select(Run.session_id).where(
                            Run.run_id.in_(run_ids))).scalars().all())
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
                        prefix = fixture_ids["prefix"]
                        db.execute(delete(AfterSalesEvent).where(
                            AfterSalesEvent.source_ref == f"{SOURCE}:{prefix}"))
                        db.execute(delete(Refund).where(Refund.source_ref == f"{SOURCE}:{prefix}"))
                        db.execute(delete(ReturnRequest).where(
                            ReturnRequest.source_ref.like(f"{SOURCE}:{prefix}:%")))
                        db.execute(delete(Order).where(Order.order_id.in_(
                            [value for key, value in fixture_ids.items() if key != "prefix"])))
                    db.commit()
                record["exact_fixture_and_run_cleanup"] = True
            except Exception as exc:
                record["exact_fixture_and_run_cleanup"] = False
                record["cleanup_error_type"] = type(exc).__name__
                if record["status"] == "PASS":
                    record["status"] = "PASS_WITH_CLEANUP_WARNING"
            try:
                if original_credential:
                    with factory() as db:
                        current = db.get(DemoCredential, "u_demo")
                        if current is not None:
                            current.password_hash = original_credential["password_hash"]
                            current.active = original_credential["active"]
                            current.updated_at = original_credential["updated_at"]
                            db.commit()
                    record["demo_credential_restored"] = True
            except Exception:
                record["demo_credential_restored"] = False
                record["status"] = "FAIL"
                record["failure_code"] = "DEMO_CREDENTIAL_RESTORE_FAILED"
            try:
                with engine.connect() as connection:
                    record["fixture_table_counts_after_cleanup"] = {
                        name: connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
                        for name in ("orders", "returns", "refunds", "after_sales_events")}
                record["fixtures_removed"] = record.get("fixture_table_counts_before") == \
                    record.get("fixture_table_counts_after_cleanup")
                if not record["fixtures_removed"] and record["status"] == "PASS":
                    record["status"] = "PASS_WITH_CLEANUP_WARNING"
            except Exception:
                record["fixtures_removed"] = False
            if engine:
                engine.dispose()
        record["finished_at"] = _utc_text()
        samples = record.get("tool_latency_ms", [])
        record["tool_latency_summary_ms"] = ({
            "count": len(samples), "p50": median(samples),
            "p95_nearest_rank": sorted(samples)[max(0, int((len(samples) * .95 + .999999)) - 1)],
        } if samples else {"count": 0, "p50": None, "p95_nearest_rank": None})
        record["tool_latency_by_capability_summary_ms"] = {}
        for capability, values in record["tool_latency_by_capability"].items():
            ordered = sorted(values)
            record["tool_latency_by_capability_summary_ms"][capability] = {
                "count": len(values), "p50": median(values),
                "p95_nearest_rank": ordered[max(0, int(len(ordered) * .95 + .999999) - 1)]}
        record["deepseek_requests"] = 0
        record["business_write_executions"] = 0
        record["api_business_data_mutations"] = 0
    return record


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output already exists; choose a new versioned path")
    result = probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items()
                      if key not in {"case_results"}}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
