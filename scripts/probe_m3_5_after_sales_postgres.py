"""M3.5 real Order + Policy + After-sales integration probe (shoprelay_m11_test only)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy import inspect

from scripts.g2_test_database import resolve_test_database_url
from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory
from app.m1.models import (AuthSession, ConversationSession, DemoCredential, GraphExecutionRecord,
                           Message, ObservabilityRecord, Run, RunEvent, Trace)
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import (checkpoint_schema_ready, open_checkpointer,
                                             run_checkpointer_async)

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _login(client: TestClient, user: str, password: str) -> tuple[dict[str, str], str]:
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": user, "password": password})
    if response.status_code != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    cookie = client.cookies.get("shoprelay_session")
    return ({"Origin": str(client.base_url).rstrip("/"),
             CSRF_HEADER: client.cookies.get("shoprelay_csrf")}, cookie)


async def _delete_checkpoint(database_url: str, schema: str, run_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(run_id)


def probe() -> dict:
    record = {"schema_version": "shoprelay.m3.5-after-sales-postgres.v1", "started_at": _utc(),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "deepseek_requests": 0, "business_write_executions": 0, "business_data_mutations": 0,
        "route_source": "deterministic AGENT three-specialist integration injection; not Router-quality evidence",
        "business_capabilities": ["get_order", "get_order_items", "search_knowledge", "get_refund_status"]}
    engine = factory = None
    database_url = None
    settings = None
    original_credentials: dict[str, dict | None] = {}
    auth_hashes: set[str] = set()
    run_ids: list[str] = []
    session_ids: list[str] = []
    trace_ids: list[str] = []
    phase = "configuration"
    api_module = None
    original_loader = None
    password = secrets.token_urlsafe(32)
    original_env = {key: os.environ.get(key) for key in (
        "SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED",
        "SHOPRELAY_GRAPH_VERSION", "SHOPRELAY_EXECUTION_VERSION")}
    try:
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "g2-m3.5"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m3.5-after-sales-v1"
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        settings = load_settings(require_api_key=False)
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        with engine.connect() as connection:
            record["database"] = connection.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
        if record["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        revision = current_revision(database_url)
        record["alembic_revision"] = {"current": revision, "expected": LATEST_SCHEMA_REVISION}
        if revision != LATEST_SCHEMA_REVISION:
            raise RuntimeError("DATABASE_REVISION_MISMATCH")
        record["checkpointer_schema"] = settings.checkpoint_schema
        record["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")
        with engine.connect() as connection:
            business_tables = set(inspect(connection).get_table_names())
            record["after_sales_schema"] = {"refunds_table_available": "refunds" in business_tables,
                "returns_table_available": "returns" in business_tables,
                "missing_tables": sorted({"refunds", "returns"} - business_tables)}
            record["business_row_counts_before"] = {
                name: connection.execute(text(f'select count(*) from "{name}"')).scalar_one()
                for name in ("orders", "order_items", "refunds", "returns", "shipments") if name in business_tables}

        phase = "temporary_credentials"
        with factory() as db:
            for user_id in ("u_demo", "u_other"):
                prior = db.get(DemoCredential, user_id)
                original_credentials[user_id] = None if prior is None else {
                    "password_hash": prior.password_hash, "active": prior.active,
                    "updated_at": prior.updated_at}
            set_demo_password(db, "u_demo", password)
            set_demo_password(db, "u_other", password)
            db.commit()

        route = {"strategy": "AGENT", "sub_strategy": "AGENT+MULTI", "safety_decision": "ALLOW",
            "reason_code": "MULTI_POLICY_CONDITIONS", "decision_source": "RULE",
            "required_capabilities": ["get_order", "get_order_items", "search_knowledge", "get_refund_status"],
            "needs_clarification": False}
        services = GraphServices(router=lambda _q: route, direct=lambda _s: {}, single=lambda _s: {},
            multi=lambda _s: {}, refuse=lambda _s: {})
        from app.m1 import api as api_module
        original_loader = api_module.load_settings
        def settings_loader(**kwargs):
            loaded = original_loader(**kwargs)
            return replace(loaded, orchestrator="langgraph", m3_supervisor_enabled=True,
                graph_version="g2-m3.5", execution_version="m3.5-after-sales-v1")
        api_module.load_settings = settings_loader
        app = create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
            checkpointer_factory=open_checkpointer, graph_services=services)
        message = "请核实订单 ORD-DELIVERED 的订单及售后状态，并根据退货质量政策与退款前置条件说明如何衔接；我认为商品存在质量问题。"
        request_id = "m35-after-sales-postgres-" + secrets.token_hex(8)
        phase = "authenticated_policy_agent_api"
        try:
            with TestClient(app) as client:
                headers, cookie = _login(client, "u_demo", password)
                auth_hashes.add(hashlib.sha256(cookie.encode()).hexdigest())
                response = client.post("/api/chat", headers=headers,
                    json={"message": message, "client_request_id": request_id, "user_id": "u_other"})
                record["api_http_status"] = response.status_code
                if response.status_code != 200:
                    try:
                        detail = response.json().get("detail", {})
                        record["api_error_code"] = detail.get("code") if isinstance(detail, dict) else None
                    except Exception:
                        record["api_error_code"] = None
                    with factory() as failed_db:
                        failed_run = failed_db.execute(select(Run).where(
                            Run.user_id == "u_demo", Run.client_request_id == request_id)).scalar_one_or_none()
                        if failed_run is not None:
                            run_ids.append(failed_run.run_id)
                            session_ids.append(failed_run.session_id)
                            trace_ids.extend(failed_db.execute(select(Trace.trace_id).where(
                                Trace.run_id == failed_run.run_id)).scalars().all())
                    raise RuntimeError("THREE_AGENT_API_FAILED")
                body = response.json()
                if body.get("status") != "SUCCEEDED" or body.get("strategy") != "AGENT" or \
                   body.get("answer_kind") not in {"POLICY_ANSWER", "CLARIFICATION_REQUIRED"}:
                    raise RuntimeError("THREE_AGENT_RUN_NOT_SUCCEEDED")
                business = body.get("business_results", {})
                if business.get("order", {}).get("order_id") != "ORD-DELIVERED" or \
                   business.get("order", {}).get("status") != "DELIVERED":
                    raise RuntimeError("ORDER_FACT_NOT_FROM_DATABASE")
                if business.get("user_claims_used_as_verified_facts") is not False:
                    raise RuntimeError("USER_CLAIM_PROMOTED_TO_VERIFIED_FACT")
                record["after_sales_data_availability"] = business.get("after_sales", {}).get("availability", "AVAILABLE")
                if record["after_sales_data_availability"] not in {"AVAILABLE", "NOT_AVAILABLE"}:
                    raise RuntimeError("AFTER_SALES_AVAILABILITY_INVALID")
                if not body.get("knowledge_version"):
                    raise RuntimeError("POLICY_VERSION_MISSING")
                if not body.get("citations") and body.get("answer_kind") == "POLICY_ANSWER":
                    raise RuntimeError("POLICY_CITATION_MISSING")
                if not body.get("citations"):
                    raise RuntimeError("THREE_AGENT_RETRIEVAL_RETURNED_NO_EVIDENCE")
                policy_meta = body.get("policy_retrieval", {})
                evidence_ids = {row.get("chunk_id") for row in body.get("citations", [])}
                if not evidence_ids.issubset(set(business.get("policy_evidence_ids", []))):
                    raise RuntimeError("CITATION_NOT_FROM_CURRENT_RETRIEVAL")
                if policy_meta.get("retrieval_mode") != "HYBRID_RERANK":
                    raise RuntimeError("MULTI_POLICY_RETRIEVAL_MODE_NOT_USED")
                conditions = business.get("conditions", [])
                if any(row.get("evaluation_result") != "UNKNOWN" for row in conditions
                       if row.get("condition_id") == "quality_return_window"):
                    raise RuntimeError("UNVERIFIED_QUALITY_CLAIM_ACCEPTED")
                if any("get_refund_status" == row.get("capability") and row.get("executed") is not True
                       for row in body.get("tool_execution_status", [])):
                    raise RuntimeError("AFTER_SALES_TOOL_NOT_EXECUTED")
                from app.m3.after_sales_tools import AfterSalesReadRepository
                from app.m3.order_tools import ToolExecutionError
                repository = AfterSalesReadRepository(factory)
                try:
                    repository.get_refund_status("ORD-DELIVERED", "u_other")
                except ToolExecutionError as exc:
                    record["foreign_order_after_sales_status"] = exc.code
                    if exc.code != "ORDER_NOT_FOUND":
                        raise RuntimeError("AFTER_SALES_OWNER_CHECK_INVALID")
                else:
                    raise RuntimeError("CROSS_USER_AFTER_SALES_ACCESS")
                run_id, session_id, trace_id = body["run_id"], body["session_id"], body["trace_id"]
                run_ids.append(run_id); session_ids.append(session_id); trace_ids.append(trace_id)
                evaluation_at = body.get("policy_retrieval", {}).get("evaluation_at")
                fingerprint = body.get("policy_retrieval", {}).get("index_fingerprint")
                at = datetime.fromisoformat(evaluation_at.replace("Z", "+00:00")) if evaluation_at else None
                citation_valid = bool(at and fingerprint) and all(
                    row.get("knowledge_version") == body["knowledge_version"] and
                    row.get("index_fingerprint") == fingerprint and
                    row.get("policy_version") == row.get("version") and
                    datetime.fromisoformat(row["effective_from"].replace("Z", "+00:00")) <= at and
                    (not row.get("effective_to") or at < datetime.fromisoformat(
                        row["effective_to"].replace("Z", "+00:00"))) for row in body["citations"])
                if not citation_valid:
                    raise RuntimeError("POLICY_CITATION_VERSION_OR_EFFECTIVE_TIME_INVALID")
                with factory() as db:
                    run = db.get(Run, run_id)
                    graph = db.get(GraphExecutionRecord, run_id)
                    records = db.execute(select(ObservabilityRecord).where(
                        ObservabilityRecord.run_id == run_id)).scalars().all()
                    spans = [json.loads(row.payload_json) for row in records if row.record_type == "span"]
                    logs = " ".join(row.payload_json for row in records if row.record_type == "log")
                    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
                    event_types = [row["type"] for row in events]
                    if event_types[0:2] != ["started", "route_decided"] or event_types[-2:] != ["answer", "done"] or \
                       event_types.count("agent_started") != 3 or event_types.count("tool_started") != 4 or \
                       event_types.count("tool_finished") != 4 or "retrieval_hop" not in event_types:
                        raise RuntimeError("SSE_EVENT_SEQUENCE_INVALID")
                    if run is None or graph is None or run.status != "SUCCEEDED" or graph.status != "SUCCEEDED":
                        raise RuntimeError("PERSISTED_RUN_STATE_MISMATCH")
                    if graph.graph_version != "g2-m3.5" or graph.execution_version != "m3.5-after-sales-v1":
                        raise RuntimeError("M3_5_EXECUTION_VERSION_NOT_PERSISTED")
                    tool_spans = [row for row in spans if row.get("node_name") in {
                        "m3.tool.get_order", "m3.tool.get_order_items", "m3.tool.search_knowledge",
                        "m3.tool.get_refund_status"}]
                    if len(tool_spans) != 4 or any(row.get("status") != "OK" for row in tool_spans):
                        raise RuntimeError("THREE_AGENT_TOOL_SPANS_INVALID")
                    if any(sensitive in logs for sensitive in (message, "u_demo", "policies/refund")):
                        raise RuntimeError("SENSITIVE_POLICY_LOG_LEAK")
                    if not graph.checkpoint_id:
                        raise RuntimeError("CHECKPOINT_ID_MISSING")
                    record.update({"run_id": run_id, "session_id": session_id, "trace_id": trace_id,
                        "checkpoint_id": graph.checkpoint_id, "run_status": run.status,
                        "graph_execution_status": graph.status, "graph_version": graph.graph_version,
                        "answer_kind": body["answer_kind"], "answer": body["answer"],
                        "knowledge_version": body["knowledge_version"],
                        "index_fingerprint": policy_meta.get("index_fingerprint"),
                        "retrieval_mode": policy_meta.get("retrieval_mode"),
                        "evaluation_at": evaluation_at,
                        "citation_ids": [row.get("chunk_id") for row in body["citations"]],
                        "citation_validity": citation_valid,
                        "sse_event_types": event_types, "tool_span_count": len(tool_spans),
                        "tool_duration_ms": tool_spans[0].get("duration_ms"),
                        "checkpoint_id_persisted": graph.checkpoint_id})
                replay = client.post("/api/chat", headers=headers,
                    json={"message": message, "client_request_id": request_id, "user_id": "u_other"})
                replay_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
                if replay.status_code != 200 or replay.json().get("run_id") != run_id or \
                   sum(row["type"] == "tool_started" for row in replay_events) != 4:
                    raise RuntimeError("IDEMPOTENT_REPLAY_REEXECUTED_TOOL")
                other = TestClient(app)
                try:
                    other_headers, other_cookie = _login(other, "u_other", password)
                    auth_hashes.add(hashlib.sha256(other_cookie.encode()).hexdigest())
                    run_access = other.get(f"/api/runs/{run_id}")
                    event_access = other.get(f"/api/runs/{run_id}/events")
                    session_access = other.get(f"/api/sessions/{session_id}")
                    if (run_access.status_code, event_access.status_code, session_access.status_code) != (404, 404, 404):
                        raise RuntimeError("CROSS_USER_RESOURCE_ACCESS")
                    record["cross_user_isolation"] = {"run": run_access.status_code,
                        "events": event_access.status_code, "session": session_access.status_code}
                finally:
                    other.close()
                record["idempotency_replay_no_second_retrieval"] = True
                record["api_citations"] = body["citations"]
                record["agent_handoffs"] = [row.get("target_agent") for row in body.get("handoffs", [])]
                record["agent_call_count"] = (body.get("agent_budget") or {}).get("agent_calls")
                record["tool_execution_status"] = body.get("tool_execution_status", [])
                record["condition_results"] = conditions
                record["run_result_persisted"] = True
                record["sensitive_logs_absent"] = True
                record["business_data_mutations"] = 0
                record["business_write_executions"] = 0
                with engine.connect() as connection:
                    record["business_row_counts_after"] = {
                        name: connection.execute(text(f'select count(*) from "{name}"')).scalar_one()
                        for name in ("orders", "order_items", "refunds", "returns", "shipments")
                        if name in business_tables}
                if record["business_row_counts_before"] != record["business_row_counts_after"]:
                    raise RuntimeError("BUSINESS_DATA_MUTATED")
        finally:
            app.state.engine.dispose()
            api_module.load_settings = original_loader

        phase = "application_restart_and_checkpoint_recovery"
        fresh_engine = make_engine(database_url)
        fresh_app = create_app(database_url=database_url, engine=fresh_engine, orchestrator_mode="langgraph",
            checkpointer_factory=open_checkpointer, graph_services=services)
        try:
            with TestClient(fresh_app) as client:
                headers, cookie = _login(client, "u_demo", password)
                auth_hashes.add(hashlib.sha256(cookie.encode()).hexdigest())
                recovered = client.get(f"/api/runs/{run_id}")
                replayed_events = client.get(f"/api/runs/{run_id}/events")
                if recovered.status_code != 200 or recovered.json()["run"]["status"] != "SUCCEEDED":
                    raise RuntimeError("RUN_NOT_RECOVERED_AFTER_RESTART")
                if replayed_events.status_code != 200 or [x["type"] for x in replayed_events.json()["events"]] != \
                   record["sse_event_types"]:
                    raise RuntimeError("EVENTS_NOT_RECOVERED_AFTER_RESTART")
            from scripts.probe_m3_3_product_postgres import _checkpoint
            checkpoint = run_checkpointer_async(_checkpoint(database_url, settings.checkpoint_schema, run_id))
            if checkpoint is None:
                raise RuntimeError("CHECKPOINT_NOT_RECOVERED_AFTER_RESTART")
            checkpoint_values = checkpoint.checkpoint.get("channel_values", {})
            checkpoint_text = repr(checkpoint_values)
            policy_excerpt = body["citations"][0].get("content", "") if body.get("citations") else ""
            if any(value and value in checkpoint_text for value in (message, "u_demo", policy_excerpt)):
                raise RuntimeError("SENSITIVE_STATE_IN_CHECKPOINT")
            record["restart_recovery"] = True
            record["checkpoint_payload_sanitized"] = True
            record["checkpoint_agent_handoff_targets"] = [
                row.get("target_agent") for row in checkpoint_values.get("handoffs", [])]
            if record["checkpoint_agent_handoff_targets"] != ["order_logistics", "policy", "after_sales"]:
                raise RuntimeError("THREE_AGENT_HANDOFFS_NOT_CHECKPOINTED")
            record["agent_handoffs"] = record["checkpoint_agent_handoff_targets"]
            record["agent_call_count"] = (checkpoint_values.get("agent_budget") or {}).get("agent_calls")
            record["tool_execution_status"] = checkpoint_values.get("tool_execution_status", [])
            record["tool_duration_ms_by_capability"] = {
                row.get("capability"): row.get("duration_ms") for row in record["tool_execution_status"]
                if row.get("executed")}
            if record["agent_call_count"] != 3 or len(record["tool_duration_ms_by_capability"]) != 4:
                raise RuntimeError("THREE_AGENT_TOOL_EXECUTION_ACCOUNTING_INVALID")
            if record["after_sales_data_availability"] == "NOT_AVAILABLE" and not any(
                    row.get("capability") == "get_refund_status" and row.get("status") == "NOT_AVAILABLE"
                    for row in record["tool_execution_status"]):
                raise RuntimeError("AFTER_SALES_NOT_AVAILABLE_STATE_NOT_PERSISTED")
        finally:
            fresh_engine.dispose()
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["failure_phase"] = phase
        record["failure_type"] = type(exc).__name__
        record["failure_code"] = getattr(exc, "code", None) or (str(exc) if str(exc).isupper() else "PROBE_FAILED")
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
                        session_ids.extend(db.execute(select(Run.session_id).where(Run.run_id.in_(run_ids))).scalars())
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
                        db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids)))
                        db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
                        db.execute(delete(Message).where(Message.session_id.in_(set(session_ids))))
                        db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
                    if trace_ids:
                        db.execute(delete(Trace).where(Trace.trace_id.in_(trace_ids)))
                    if session_ids:
                        db.execute(delete(ConversationSession).where(
                            ConversationSession.session_id.in_(set(session_ids))))
                    if auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                    db.commit()
                record["exact_api_record_cleanup"] = True
            except Exception as exc:
                record["exact_api_record_cleanup"] = False
                record["cleanup_failure_type"] = type(exc).__name__
                record["cleanup_failure_code"] = getattr(getattr(exc, "orig", None), "pgcode", None)
                if record["status"] == "PASS":
                    record["status"] = "PASS_WITH_CLEANUP_WARNING"
            try:
                with factory() as db:
                    for user_id, previous in original_credentials.items():
                        current = db.get(DemoCredential, user_id)
                        if previous is None:
                            if current is not None:
                                db.delete(current)
                        elif current is not None:
                            current.password_hash = previous["password_hash"]
                            current.active = previous["active"]
                            current.updated_at = previous["updated_at"]
                    db.commit()
                record["demo_credentials_restored"] = True
            except Exception:
                record["demo_credentials_restored"] = False
                record["status"] = "FAIL"
                record["failure_code"] = "DEMO_CREDENTIAL_RESTORE_FAILED"
        if engine:
            engine.dispose()
        if api_module is not None and original_loader is not None:
            api_module.load_settings = original_loader
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        record["finished_at"] = _utc()
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
    print(json.dumps({key: value for key, value in result.items() if key not in {"answer", "api_citations"}},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
