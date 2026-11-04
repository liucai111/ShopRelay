"""M3.2 read-only Order/Logistics API probe, pinned to shoprelay_m11_test.

The probe does not migrate schemas or alter business rows. It temporarily sets
demo passwords for the two seeded users, restores their exact prior credential
rows, and deletes only the sessions/runs/checkpoints it created.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from scripts.g2_test_database import resolve_test_database_url
from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import (AuthSession, ConversationSession, DemoCredential, GraphExecutionRecord, Message,
                           ObservabilityRecord, Order, OrderItem, Product, Run, RunEvent, Shipment, Trace)
from app.m1.routing import RouteDecision
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import (checkpoint_schema_ready, open_checkpointer,
    run_checkpointer_async)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = "20261021_06"
DEFAULT_OUTPUT = ROOT / "docs" / "probes" / "m3.2-order-logistics-postgres-20261021-r1.json"


async def _read_checkpoint(database_url: str, schema: str, thread_id: str) -> tuple[dict, str | None]:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        checkpoint = await saver.aget_tuple(config)
    if checkpoint is None:
        raise AssertionError("checkpoint not found after application restart")
    return checkpoint.checkpoint.get("channel_values", {}), checkpoint.config["configurable"].get("checkpoint_id")


async def _delete_checkpoint(database_url: str, schema: str, thread_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(thread_id)


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": username, "password": password})
    if response.status_code != 200:
        raise AssertionError("probe demo login failed")
    return {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _route_for(query: str) -> dict:
    normalized = query.casefold()
    capability = "get_order_items" if "items" in normalized or "明细" in query else \
        "track_shipment" if "shipment" in normalized or "物流" in query else "get_order"
    return RouteDecision(strategy="AGENT", safety_decision="ALLOW", reason_code="LIVE_BUSINESS_FACT",
        decision_source="RULE", intent_labels=["live_business"], required_capabilities=[capability]).model_dump(mode="json")


def probe() -> dict:
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result: dict = {"schema_version": "shoprelay.m3.2-order-logistics-postgres.v1",
        "started_at": started, "status": "NOT_RUN", "database": "shoprelay_m11_test",
        "deepseek_requests": 0, "business_write_executions": 0,
        "business_data_mutations": 0, "schema_migrations_executed": 0}
    engine = None
    database_url = None
    factory = None
    original_credentials: dict[str, dict | None] = {}
    temp_password = secrets.token_urlsafe(32)
    run_ids: list[str] = []
    session_ids: list[str] = []
    thread_ids: list[str] = []
    auth_token_hashes: set[str] = set()
    trace_ids: list[str] = []
    expected_failed_run_ids: set[str] = set()
    phase = "configuration"
    restore_env = {key: os.environ.get(key) for key in
        ("SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED")}
    try:
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        configured = load_settings(require_api_key=False)
        schema = configured.checkpoint_schema
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        phase = "read_only_database_and_existing_schema_check"
        with engine.connect() as connection:
            result["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
            result["database_name"] = connection.execute(text("select current_database()")).scalar_one()
            if result["database_name"] != "shoprelay_m11_test":
                raise RuntimeError("UNSAFE_DATABASE_TARGET")
        revision = current_revision(database_url)
        if revision != EXPECTED_REVISION:
            raise RuntimeError("APPLICATION_SCHEMA_REVISION_MISMATCH")
        result["application_revision"] = revision
        # Prepare only the canonical M0 fixture rows required by this isolated
        # integration test. Existing rows are reused; conflicts are never overwritten.
        phase = "canonical_test_fixture_setup"
        seed_payload = json.loads((ROOT / "data" / "seed.json").read_text(encoding="utf-8"))
        added = {"products": 0, "orders": 0, "order_items": 0}
        with factory() as db:
            for product in seed_payload["products"]:
                row = db.get(Product, product["product_id"])
                expected = (product["name"], product["category"], product["price_cents"])
                if row is None:
                    db.add(Product(product_id=product["product_id"], name=product["name"],
                        category=product["category"], price_cents=product["price_cents"]))
                    added["products"] += 1
                elif (row.name, row.category, row.price_cents) != expected:
                    raise RuntimeError("CANONICAL_PRODUCT_FIXTURE_CONFLICT")
            db.flush()
            for order_data in seed_payload["orders"]:
                row = db.get(Order, order_data["order_id"])
                expected = (order_data["user_id"], order_data["status"], order_data["paid_at"],
                    order_data["delivered_at"], order_data["paid_cents"], order_data["version"])
                if row is None:
                    db.add(Order(order_id=order_data["order_id"], user_id=order_data["user_id"],
                        status=order_data["status"], paid_at=order_data["paid_at"],
                        delivered_at=order_data["delivered_at"], paid_cents=order_data["paid_cents"],
                        version=order_data["version"]))
                    added["orders"] += 1
                elif (row.user_id, row.status, row.paid_at, row.delivered_at, row.paid_cents,
                      row.version) != expected:
                    raise RuntimeError("CANONICAL_ORDER_FIXTURE_CONFLICT")
            db.flush()
            for order_data in seed_payload["orders"]:
                row = db.get(OrderItem, (order_data["order_id"], order_data["product_id"]))
                expected = (order_data["quantity"], order_data["paid_cents"])
                if row is None:
                    db.add(OrderItem(order_id=order_data["order_id"], product_id=order_data["product_id"],
                        quantity=order_data["quantity"], paid_cents=order_data["paid_cents"]))
                    added["order_items"] += 1
                elif (row.quantity, row.paid_cents) != expected:
                    raise RuntimeError("CANONICAL_ORDER_ITEM_FIXTURE_CONFLICT")
            db.commit()
        result["canonical_fixture"] = {"snapshot_id": seed_payload["snapshot_id"],
            "added_rows": added, "source": "data/seed.json", "shipment_rows_seeded": 0}
        with engine.connect() as connection:
            result["business_row_counts_before"] = {
                "orders": connection.execute(text("select count(*) from orders")).scalar_one(),
                "order_items": connection.execute(text("select count(*) from order_items")).scalar_one(),
                "shipments": connection.execute(text("select count(*) from shipments")).scalar_one(),
            }
        if not run_checkpointer_async(checkpoint_schema_ready(database_url, schema)):
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")
        result["checkpointer_schema"] = schema
        result["checkpointer_schema_ready"] = True
        with factory() as db:
            for user_id in ("u_demo", "u_other"):
                credential = db.get(DemoCredential, user_id)
                original_credentials[user_id] = None if credential is None else {
                    "password_hash": credential.password_hash, "active": credential.active,
                    "updated_at": credential.updated_at}
                if db.get(Order, "ORD-PAID") is None:
                    raise RuntimeError("SEEDED_ORDER_DATA_MISSING")
            result["order_item_seeded"] = db.execute(select(OrderItem.order_id).where(
                OrderItem.order_id == "ORD-PAID")).first() is not None
        with factory() as db:
            set_demo_password(db, "u_demo", temp_password)
            set_demo_password(db, "u_other", temp_password)

        route_calls: list[str] = []
        def router(query: str):
            route_calls.append("AGENT")
            return _route_for(query)
        services = GraphServices(router=router, direct=lambda _state: {}, single=lambda _state: {},
            multi=lambda _state: {}, refuse=lambda _state: {})
        from app.m1.api import load_settings as api_load_settings
        original_loader = api_load_settings
        from dataclasses import replace
        def enabled_settings(**kwargs):
            return replace(original_loader(**kwargs), orchestrator="langgraph", m3_supervisor_enabled=True,
                graph_version="g2-m3.2", execution_version="m3.2-order-read-v1")

        import app.m1.api as api_module
        api_module.load_settings = enabled_settings
        result["graph_mode"] = "langgraph"
        result["graph_version"] = "g2-m3.2"
        result["router_mode"] = "deterministic fixed route for probe only"
        app = create_app(database_url=database_url, engine=engine, chat_provider=object(),
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer,
            graph_services=services)
        try:
            phase = "authenticated_order_agent_api"
            with TestClient(app) as client:
                headers = _login(client, "u_demo", temp_password)
                auth_token_hashes.add(hashlib.sha256(client.cookies.get("shoprelay_session").encode()).hexdigest())
                cases = [
                    ("get_order", "查询 ORD-PAID 当前状态", "m32pg-own-order"),
                    ("get_order_items", "查看 ORD-PAID 商品明细", "m32pg-own-items"),
                    ("track_shipment", "查询 ORD-SHIPPED 物流轨迹", "m32pg-shipment"),
                ]
                case_results = {}
                for capability, message, request_id in cases:
                    response = client.post("/api/chat", headers=headers, json={
                        "message": message, "client_request_id": request_id, "user_id": "u_other"})
                    if response.status_code != 200:
                        raise AssertionError(f"{capability} API execution failed: {response.json().get('code')}")
                    body = response.json()
                    run_id = body.get("run_id")
                    if not run_id or body.get("strategy") != "AGENT":
                        raise AssertionError("AGENT result missing its Run or route")
                    run_ids.append(run_id)
                    session_ids.append(body["session_id"])
                    thread_ids.append(run_id)
                    trace_ids.append(body["trace_id"])
                    case_results[capability] = {"run_id": run_id, "trace_id": body["trace_id"],
                        "answer_kind": body.get("answer_kind"), "tool_references": body.get("tool_references"),
                        "business_result": body.get("business_results", {}).get(capability),
                        "answer": body.get("answer")}
                order = case_results["get_order"]["business_result"]
                if not order or order.get("order_id") != "ORD-PAID" or order.get("status") != "PAID":
                    raise AssertionError("get_order did not return the actual owned database row")
                if not case_results["get_order_items"]["business_result"]["items"]:
                    raise AssertionError("get_order_items did not return actual owned order lines")
                shipment = case_results["track_shipment"]["business_result"]
                if shipment.get("availability") == "UNAVAILABLE" and shipment.get("records") != []:
                    raise AssertionError("empty shipment response was malformed")
                result["tool_cases"] = case_results

                # Persist and verify an SSE sequence via the existing asynchronous API path.
                sse = client.post("/api/chat/stream", headers=headers, json={
                    "message": "查询 ORD-PAID 当前状态", "client_request_id": "m32pg-sse"})
                if sse.status_code != 200:
                    raise AssertionError("SSE tool run did not complete")
                event_types = [line.removeprefix("event: ") for line in sse.text.splitlines()
                               if line.startswith("event: ")]
                if event_types != ["started", "route_decided", "agent_started", "tool_started",
                                   "tool_finished", "answer", "done"]:
                    raise AssertionError("SSE tool event sequence is invalid")
                sse_payloads = [json.loads(line.removeprefix("data: ")) for line in sse.text.splitlines()
                                if line.startswith("data: ")]
                sse_run_id = sse_payloads[0]["run_id"]
                sse_trace_id = sse_payloads[0]["trace_id"]
                run_ids.append(sse_run_id)
                thread_ids.append(sse_run_id)
                session_ids.append(sse_payloads[0]["session_id"])
                trace_ids.append(sse_trace_id)
                result["sse"] = {"run_id": sse_run_id, "trace_id": sse_trace_id,
                                  "events": event_types}

                # A different authenticated owner sees the same safe not-found error
                # for a foreign ID and a nonexistent ID.
                phase = "resource_owner_isolation"
                with TestClient(app) as other_client:
                    other_headers = _login(other_client, "u_other", temp_password)
                    auth_token_hashes.add(hashlib.sha256(other_client.cookies.get("shoprelay_session").encode()).hexdigest())
                    own = other_client.post("/api/chat", headers=other_headers, json={
                        "message": "查询 ORD-OTHER 当前状态", "client_request_id": "m32pg-other-own"})
                    if own.status_code != 200:
                        raise AssertionError("second demo user could not query their own order")
                    own_id = own.json()["run_id"]
                    run_ids.append(own_id); thread_ids.append(own_id); session_ids.append(own.json()["session_id"])
                    trace_ids.append(own.json()["trace_id"])
                    foreign = client.post("/api/chat", headers=headers, json={
                        "message": "查询 ORD-OTHER 当前状态", "client_request_id": "m32pg-foreign"})
                    absent = client.post("/api/chat", headers=headers, json={
                        "message": "查询 ORD-NOT-THERE 当前状态", "client_request_id": "m32pg-absent"})
                    if foreign.status_code != 502 or absent.status_code != 502 or \
                       foreign.json().get("code") != absent.json().get("code") or \
                       foreign.json().get("code") != "ORDER_NOT_FOUND":
                        raise AssertionError("foreign and absent order responses are not indistinguishable")
                    for failed_response in (foreign, absent):
                        failed_run_id = failed_response.json().get("details", {}).get("run_id")
                        if not failed_run_id:
                            raise AssertionError("failed order lookup lacks its persisted Run reference")
                        run_ids.append(failed_run_id)
                        expected_failed_run_ids.add(failed_run_id)
                        thread_ids.append(failed_run_id)
                        trace_ids.append(failed_response.headers.get("X-Trace-Id", ""))
                    result["owner_isolation"] = {"own_order": "PASS", "foreign_order": "ORDER_NOT_FOUND",
                        "nonexistent_order": "ORDER_NOT_FOUND", "forged_body_user_id_ignored": True}

            result["route_invocations"] = len(route_calls)
        finally:
            app.state.engine.dispose()
            api_module.load_settings = original_loader

        phase = "run_trace_sse_idempotency_and_checkpoint_restart"
        with factory() as db:
            runs = {row.run_id: row for row in db.execute(select(Run).where(Run.run_id.in_(run_ids))).scalars().all()}
            if len(runs) != len(set(run_ids)):
                raise AssertionError("one or more probe Run records were not persisted")
            if any(row.status != "FAILED" or row.error_code != "ORDER_NOT_FOUND"
                   for run_id, row in runs.items() if run_id in expected_failed_run_ids):
                raise AssertionError("foreign/nonexistent order did not persist the safe not-found failure")
            if any(row.status != "SUCCEEDED" for run_id, row in runs.items()
                   if run_id not in expected_failed_run_ids):
                raise AssertionError("an owned Order Agent run did not reach SUCCEEDED")
            target_run = runs[case_results["get_order"]["run_id"]]
            graph = db.get(GraphExecutionRecord, target_run.run_id)
            if graph is None or graph.status != "SUCCEEDED" or not graph.checkpoint_id:
                raise AssertionError("Run and GraphExecutionRecord are inconsistent")
            event_rows = db.execute(select(RunEvent).where(RunEvent.run_id == target_run.run_id)
                .order_by(RunEvent.seq)).scalars().all()
            if [row.event_type for row in event_rows] != ["started", "route_decided", "agent_started",
                "tool_started", "tool_finished", "answer", "done"]:
                raise AssertionError("persisted tool event sequence differs from expected order")
            records = db.execute(select(ObservabilityRecord).where(
                ObservabilityRecord.run_id == target_run.run_id)).scalars().all()
            spans = [json.loads(row.payload_json) for row in records if row.record_type == "span"]
            tool_span = next((row for row in spans if row.get("node_name") == "m3.tool.get_order"), None)
            if not tool_span or not tool_span.get("parent_span_id") or tool_span.get("status") != "OK":
                raise AssertionError("tool span is missing or not nested under the Order Agent")
            stored_logs = " ".join(row.payload_json for row in records if row.record_type == "log")
            if "ORD-PAID" in stored_logs or "u_demo" in stored_logs:
                raise AssertionError("business identifiers leaked into structured tool logs")
            result["persisted_run_status"] = target_run.status
            result["persisted_graph_version"] = graph.graph_version
            result["persisted_checkpoint_id"] = graph.checkpoint_id
            result["persisted_tool_span"] = {"node_name": tool_span["node_name"],
                "parent_span_id": tool_span["parent_span_id"], "duration_ms": tool_span["duration_ms"],
                "status": tool_span["status"]}
            result["persisted_event_types"] = [row.event_type for row in event_rows]

        # A fresh FastAPI app and database engine reopens the existing checkpoint.
        fresh_engine = make_engine(database_url)
        restart_app = create_app(database_url=database_url, engine=fresh_engine,
            chat_provider=object(), orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer,
            graph_services=services)
        try:
            with TestClient(restart_app) as restarted:
                headers = _login(restarted, "u_demo", temp_password)
                auth_token_hashes.add(hashlib.sha256(restarted.cookies.get("shoprelay_session").encode()).hexdigest())
                run_id = case_results["get_order"]["run_id"]
                read = restarted.get(f"/api/runs/{run_id}")
                if read.status_code != 200 or read.json()["run"]["status"] != "SUCCEEDED":
                    raise AssertionError("restarted app did not recover the completed business result")
                replay = restarted.post("/api/chat", headers=headers, json={
                    "message": "查询 ORD-PAID 当前状态", "client_request_id": "m32pg-own-order"})
                if replay.status_code != 200 or replay.json()["run_id"] != run_id:
                    raise AssertionError("idempotent replay did not return the stored run")
                events = restarted.get(f"/api/runs/{run_id}/events?after_seq=0").json()["events"]
                if [row["type"] for row in events].count("tool_started") != 1:
                    raise AssertionError("idempotent replay executed the tool again")
                with TestClient(restart_app) as other:
                    other_headers = _login(other, "u_other", temp_password)
                    auth_token_hashes.add(hashlib.sha256(other.cookies.get("shoprelay_session").encode()).hexdigest())
                    if other.get(f"/api/runs/{run_id}").status_code != 404 or \
                       other.get(f"/api/runs/{run_id}/events?after_seq=0").status_code != 404:
                        raise AssertionError("other user could read the run/events")
            values, checkpoint_id = run_checkpointer_async(_read_checkpoint(database_url, schema,
                case_results["get_order"]["run_id"]))
            state_text = repr(values)
            if "ORD-PAID" in state_text or "u_demo" in state_text or "查询" in state_text:
                raise AssertionError("checkpoint contains order identifier, identity, or raw query")
            if checkpoint_id != result["persisted_checkpoint_id"]:
                raise AssertionError("reopened checkpoint ID differs from persisted Run mapping")
            result["restart_recovery"] = True
            result["idempotency_no_duplicate_tool_call"] = True
            result["checkpoint_excludes_raw_query_identity_and_order_id"] = True
        finally:
            fresh_engine.dispose()

        with factory() as db:
            all_events = db.execute(select(RunEvent).where(RunEvent.run_id.in_(set(run_ids)))).scalars().all()
            started_events = [row for row in all_events if row.event_type == "tool_started"]
            finished_events = [row for row in all_events if row.event_type == "tool_finished"]
            tool_spans = [json.loads(row.payload_json) for row in db.execute(select(ObservabilityRecord).where(
                ObservabilityRecord.run_id.in_(set(run_ids)), ObservabilityRecord.record_type == "span")).scalars().all()
                if str(json.loads(row.payload_json).get("node_name", "")).startswith("m3.tool.")]
        result["business_tool_calls"] = len(started_events)
        result["tool_call_statuses"] = [json.loads(row.payload_json).get("status") for row in finished_events]
        result["tool_latency_ms"] = [row["duration_ms"] for row in tool_spans]
        result["estimated_cost_usd"] = 0.0
        with engine.connect() as connection:
            result["business_row_counts_after"] = {
                "orders": connection.execute(text("select count(*) from orders")).scalar_one(),
                "order_items": connection.execute(text("select count(*) from order_items")).scalar_one(),
                "shipments": connection.execute(text("select count(*) from shipments")).scalar_one(),
            }
        if result["business_row_counts_before"] != result["business_row_counts_after"]:
            raise AssertionError("read-only probe changed business tables")
        result["deepseek_requests"] = 0
        result["status"] = "PASS"
    except Exception as exc:
        result["status"] = "FAIL"
        result["failure_phase"] = phase
        result["failure_type"] = type(exc).__name__
        result["failure_code"] = getattr(exc, "code", None) or "PROBE_ASSERTION_OR_ENVIRONMENT_FAILURE"
    finally:
        if database_url and thread_ids:
            for thread_id in dict.fromkeys(thread_ids):
                try:
                    run_checkpointer_async(_delete_checkpoint(database_url, schema, thread_id))
                except Exception:
                    if result.get("status") == "PASS":
                        result["status"] = "PASS_WITH_CLEANUP_WARNING"
                    result.setdefault("checkpoint_cleanup_failures", []).append("CHECKPOINT_CLEANUP_FAILED")
        if factory is not None:
            try:
                with factory() as db:
                    if run_ids:
                        session_ids.extend(db.execute(select(Run.session_id).where(
                            Run.run_id.in_(set(run_ids)))).scalars().all())
                        db.execute(delete(Trace).where(Trace.run_id.in_(set(run_ids))))
                    if trace_ids:
                        db.execute(delete(Trace).where(Trace.trace_id.in_(trace_ids)))
                    if run_ids:
                        # M1's legacy PostgreSQL baseline has a non-cascading
                        # messages->sessions FK; delete only messages owned by
                        # the exact probe-created sessions before deleting them.
                        db.execute(delete(Message).where(Message.session_id.in_(set(session_ids))))
                        db.execute(delete(Run).where(Run.run_id.in_(set(run_ids))))
                    if session_ids:
                        db.execute(delete(ConversationSession).where(
                            ConversationSession.session_id.in_(set(session_ids))))
                    if auth_token_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_token_hashes)))
                    db.commit()
                result["exact_api_record_cleanup"] = True
            except Exception as cleanup_exc:
                result["exact_api_record_cleanup"] = False
                result["cleanup_failure_type"] = type(cleanup_exc).__name__
                result["cleanup_failure_code"] = getattr(cleanup_exc, "code", None)
                if result.get("status") == "PASS":
                    result["status"] = "PASS_WITH_CLEANUP_WARNING"
            try:
                with factory() as db:
                    for user_id, prior in original_credentials.items():
                        credential = db.get(DemoCredential, user_id)
                        if prior is None:
                            if credential is not None:
                                db.delete(credential)
                        elif credential is not None:
                            credential.password_hash = prior["password_hash"]
                            credential.active = prior["active"]
                            credential.updated_at = prior["updated_at"]
                    db.commit()
                result["demo_credentials_restored"] = True
            except Exception:
                result["demo_credentials_restored"] = False
                result["status"] = "FAIL"
                result["failure_code"] = "DEMO_CREDENTIAL_RESTORE_FAILED"
        if engine is not None:
            engine.dispose()
        for key, value in restore_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        result["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output already exists; choose a new versioned path")
    output = probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output["status"] not in {"PASS", "PASS_WITH_CLEANUP_WARNING"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
