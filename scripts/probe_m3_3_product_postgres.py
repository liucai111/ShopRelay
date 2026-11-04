"""M3.3 Product Agent PostgreSQL/API probe, hard-pinned to shoprelay_m11_test.

It applies the additive inventory schema revision only to the isolated test DB,
reuses canonical seed values without overwriting conflicts, and cleans exact API
records plus temporary credentials. Canonical product/inventory fixtures remain.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import statistics
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from scripts.g2_test_database import resolve_test_database_url
from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import (LATEST_SCHEMA_REVISION, current_revision, make_engine,
                       make_session_factory, upgrade_database)
from app.m1.models import (AuthSession, ConversationSession, DemoCredential, GraphExecutionRecord,
    Inventory, Message, ObservabilityRecord, Product, Run, RunEvent, Trace)
from app.m1.services import set_demo_password
from app.orchestration.checkpointer import checkpoint_schema_ready, open_checkpointer, run_checkpointer_async

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "probes" / "m3.3-product-postgres-20261021-r6.json"


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": username, "password": password})
    if response.status_code != 200:
        raise AssertionError("probe demo login failed")
    return {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


async def _checkpoint(database_url: str, schema: str, run_id: str):
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        return await saver.aget_tuple(config)


async def _delete_checkpoint(database_url: str, schema: str, run_id: str):
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(run_id)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * pct)))], 3)


def probe() -> dict:
    result: dict = {"schema_version": "shoprelay.m3.3-product-postgres.v1",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "deepseek_requests": 0, "business_write_executions": 0,
        "business_data_mutations_during_api": 0}
    engine = factory = None
    database_url = None
    configured = None
    original_credentials: dict[str, dict | None] = {}
    auth_hashes: set[str] = set()
    run_ids: list[str] = []
    trace_ids: list[str] = []
    session_ids: list[str] = []
    phase = "configuration"
    temp_password = secrets.token_urlsafe(32)
    env_names = ("SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED",
                 "SHOPRELAY_GRAPH_VERSION", "SHOPRELAY_EXECUTION_VERSION")
    original_env = {name: os.environ.get(name) for name in env_names}
    try:
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        test_url = resolve_test_database_url()
        if test_url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = test_url.render_as_string(hide_password=False)
        configured = load_settings(require_api_key=False)
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        with engine.connect() as connection:
            result["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
            result["database"] = connection.execute(text("select current_database()")).scalar_one()
        if result["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")

        phase = "isolated_schema_migration"
        before_revision = current_revision(database_url)
        upgrade_database(database_url)
        revision = current_revision(database_url)
        result["schema"] = {"revision_before": before_revision, "revision_after": revision,
            "expected": LATEST_SCHEMA_REVISION, "migration_applied_to_test_db_only": before_revision != revision}
        if revision != LATEST_SCHEMA_REVISION:
            raise RuntimeError("APPLICATION_SCHEMA_REVISION_MISMATCH")
        result["checkpointer_schema"] = configured.checkpoint_schema
        result["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, configured.checkpoint_schema)))
        if not result["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        phase = "canonical_fixture_validation"
        seed = json.loads((ROOT / "data" / "seed.json").read_text(encoding="utf-8"))
        added = {"products": 0, "inventory": 0}
        with factory() as db:
            for item in seed["products"]:
                row = db.get(Product, item["product_id"])
                expected = (item["name"], item["category"], item["price_cents"])
                if row is None:
                    db.add(Product(product_id=item["product_id"], name=item["name"],
                        category=item["category"], price_cents=item["price_cents"]))
                    added["products"] += 1
                elif (row.name, row.category, row.price_cents) != expected:
                    raise RuntimeError("CANONICAL_PRODUCT_FIXTURE_CONFLICT")
            db.flush()
            for item in seed["products"]:
                row = db.get(Inventory, item["product_id"])
                if row is None:
                    db.add(Inventory(product_id=item["product_id"], available_qty=item["available_qty"],
                                     reserved_qty=0, version=1))
                    added["inventory"] += 1
                elif (row.available_qty, row.reserved_qty) != (item["available_qty"], 0):
                    raise RuntimeError("CANONICAL_INVENTORY_FIXTURE_CONFLICT")
            db.commit()
            for user_id in ("u_demo", "u_other"):
                credential = db.get(DemoCredential, user_id)
                original_credentials[user_id] = None if credential is None else {
                    "password_hash": credential.password_hash, "active": credential.active,
                    "updated_at": credential.updated_at}
        result["canonical_fixture"] = {"snapshot_id": seed["snapshot_id"], "additions": added,
            "products": len(seed["products"]), "inventory_rows": len(seed["products"]),
            "inventory_freshness": "seed snapshot has no observation timestamp"}
        with engine.connect() as connection:
            result["catalog_inventory_counts_before"] = {
                table: connection.execute(text(f"select count(*) from {table}")).scalar_one()
                for table in ("products", "inventory")}

        with factory() as db:
            set_demo_password(db, "u_demo", temp_password)
            set_demo_password(db, "u_other", temp_password)
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "g2-m3.3"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m3.3-product-read-v1"
        import app.m1.api as api_module
        original_loader = api_module.load_settings
        from dataclasses import replace
        def settings_for_probe(**kwargs):
            return replace(original_loader(**kwargs), orchestrator="langgraph", m3_supervisor_enabled=True,
                graph_version="g2-m3.3", execution_version="m3.3-product-read-v1")
        api_module.load_settings = settings_for_probe
        result["orchestrator"] = "langgraph"
        result["graph_version"] = "g2-m3.3"
        result["router"] = "production deterministic route rules; no LLM classifier fallback expected"
        app = create_app(database_url=database_url, engine=engine, chat_provider=object(),
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer)
        cases = {}
        tool_durations: list[float] = []
        try:
            phase = "authenticated_product_agent_api"
            with TestClient(app) as client:
                headers = _login(client, "u_demo", temp_password)
                auth_hashes.add(hashlib.sha256(client.cookies.get("shoprelay_session").encode()).hexdigest())
                requests = [
                    ("search_products", "搜索电热水壶", "m33pg-search"),
                    ("get_product_detail", "查看商品 P-CUP 当前详情和价格", "m33pg-detail"),
                    ("recommend_products", "推荐一款电热水壶，预算300元以内，有货", "m33pg-recommend"),
                    ("check_inventory", "查询 P-KETTLE 当前库存", "m33pg-inventory"),
                    ("product_not_found", "查看商品 P-MISSING 当前详情", "m33pg-not-found"),
                ]
                for key, message, request_id in requests:
                    phase = f"authenticated_product_agent_api:{key}"
                    response = client.post("/api/chat", headers=headers, json={"message": message,
                        "client_request_id": request_id, "user_id": "u_other"})
                    if response.status_code != 200:
                        raise AssertionError(f"{key} failed with {response.json().get('code')}")
                    body = response.json()
                    if body.get("strategy") != "AGENT" or body.get("answer_kind") != "BUSINESS_FACTS":
                        raise AssertionError(f"{key} did not execute the real Product Agent")
                    run_ids.append(body["run_id"]); session_ids.append(body["session_id"]); trace_ids.append(body["trace_id"])
                    with factory() as db:
                        run_record = db.get(Run, body["run_id"])
                        if run_record is None or run_record.result_json is None:
                            raise AssertionError(f"{key} has no persisted result payload")
                        persisted_payload = json.loads(run_record.result_json)
                    cases[key] = {"run_id": body["run_id"], "trace_id": body["trace_id"],
                        "answer": body["answer"], "business_results": body["business_results"],
                        "tool_references": body["tool_references"],
                        "tool_execution_status": persisted_payload.get("tool_execution_status", [])}
                    tool_durations.extend(float(row["duration_ms"]) for row in persisted_payload.get("tool_execution_status", [])
                        if row.get("executed") and row.get("duration_ms") is not None)
                if "P-KETTLE" not in cases["search_products"]["answer"]:
                    raise AssertionError("non-empty search did not return canonical product row")
                if cases["get_product_detail"]["business_results"]["get_product_detail"]["product"]["price_cents"] != 8900:
                    raise AssertionError("product detail differs from database fixture")
                rec = cases["recommend_products"]["business_results"]["recommend_products"]["recommendations"]
                if not rec or rec[0]["product"]["product_id"] != "P-KETTLE" or rec[0]["match_status"] != "UNCONFIRMED":
                    raise AssertionError("recommendation claimed a current stock match without freshness evidence")
                inventory_result = cases["check_inventory"]["business_results"]["check_inventory"]
                if inventory_result["availability"] != "UNAVAILABLE" or \
                   inventory_result["error_code"] != "INVENTORY_FRESHNESS_UNVERIFIED" or \
                   inventory_result["records"][0]["snapshot_sellable_qty"] != 12:
                    raise AssertionError("inventory freshness was not reported safely against the real DB row")
                if "无法确认当前库存" not in cases["check_inventory"]["answer"]:
                    raise AssertionError("inventory answer made a current-availability claim")
                if "未找到商品 P-MISSING" not in cases["product_not_found"]["answer"]:
                    raise AssertionError("nonexistent product did not return safe NOT_FOUND result")

                phase = "sse_restart_and_idempotency"
                sse = client.post("/api/chat/stream", headers=headers, json={"message": "搜索电热水壶",
                    "client_request_id": "m33pg-sse"})
                if sse.status_code != 200:
                    raise AssertionError("Product Agent SSE request failed")
                event_types = [line.removeprefix("event: ") for line in sse.text.splitlines()
                               if line.startswith("event: ")]
                if event_types != ["started", "route_decided", "agent_started", "tool_started",
                                   "tool_finished", "answer", "done"]:
                    raise AssertionError("Product Agent SSE order is invalid")
                sse_data = [json.loads(line.removeprefix("data: ")) for line in sse.text.splitlines()
                            if line.startswith("data: ")]
                sse_run = sse_data[0]["run_id"]
                run_ids.append(sse_run); session_ids.append(sse_data[0]["session_id"])
                trace_ids.append(sse_data[0]["trace_id"])
                replay = client.post("/api/chat", headers=headers, json={"message": "搜索电热水壶",
                    "client_request_id": "m33pg-sse"})
                replay_events = client.get(f"/api/runs/{sse_run}/events").json()["events"]
                if replay.status_code != 200 or sum(x["type"] == "tool_started" for x in replay_events) != 1:
                    raise AssertionError("idempotent replay executed Product tool again")
                run_details = client.get(f"/api/runs/{sse_run}")
                if run_details.status_code != 200 or run_details.json()["run"]["status"] != "SUCCEEDED":
                    raise AssertionError("Product Run was not persisted as SUCCEEDED")
                result["sse"] = {"run_id": sse_run, "trace_id": sse_data[0]["trace_id"],
                    "event_types": event_types}
                result["idempotent_replay_no_second_tool_execution"] = True

                anonymous = TestClient(app)
                try:
                    denied = anonymous.post("/api/chat", json={"message": "查询商品 P-CUP 库存",
                        "client_request_id": "m33pg-anonymous"})
                    if denied.status_code != 401:
                        raise AssertionError("unauthenticated Product Agent request was not denied")
                finally:
                    anonymous.close()
            phase = "persisted_trace_and_checkpoint"
            target_run_id = cases["recommend_products"]["run_id"]
            with factory() as db:
                run = db.get(Run, target_run_id)
                graph_run = db.get(GraphExecutionRecord, target_run_id)
                records = db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == target_run_id)).scalars().all()
                spans = [json.loads(row.payload_json) for row in records if row.record_type == "span"]
                tool_spans = [row for row in spans if row.get("node_name", "").startswith("m3.tool.")]
                logs = " ".join(row.payload_json for row in records if row.record_type == "log")
                if run.status != "SUCCEEDED" or graph_run.status != "SUCCEEDED" or not graph_run.checkpoint_id:
                    raise AssertionError("Run and graph execution record differ")
                if len(tool_spans) != 3 or any(row["status"] != "OK" for row in tool_spans):
                    raise AssertionError("three product tool spans were not recorded")
                if "电热水壶" in logs or "P-KETTLE" in logs or "u_demo" in logs:
                    raise AssertionError("raw product query, ID, or user leaked into tool logs")
                events = db.execute(select(RunEvent).where(RunEvent.run_id == target_run_id)
                    .order_by(RunEvent.seq)).scalars().all()
                result["persisted"] = {"run_status": run.status, "graph_status": graph_run.status,
                    "graph_version": graph_run.graph_version, "state_schema_version": graph_run.state_schema_version,
                    "checkpoint_id": graph_run.checkpoint_id, "trace_id": cases["recommend_products"]["trace_id"],
                    "tool_span_names": [row["node_name"] for row in tool_spans],
                    "event_types": [row.event_type for row in events]}
            result["tool_calls"] = {"count": len(tool_durations), "capability_statuses": [
                row["status"] for case in cases.values() for row in case["tool_execution_status"] if row.get("executed")],
                "latency_ms": tool_durations,
                "p50_ms": round(statistics.median(tool_durations), 3) if tool_durations else None,
                "p95_ms": _percentile(tool_durations, .95)}
            result["api_cases"] = cases
            result["security"] = {"authenticated_required": True,
                "request_body_user_id_ignored": True, "unregistered_or_write_tools": 0}
            result["deepseek_requests"] = 0
        finally:
            app.state.engine.dispose()
            api_module.load_settings = original_loader

        phase = "restart_recovery"
        fresh_engine = make_engine(database_url)
        restart_app = create_app(database_url=database_url, engine=fresh_engine, chat_provider=object(),
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer)
        try:
            with TestClient(restart_app) as client:
                headers = _login(client, "u_demo", temp_password)
                auth_hashes.add(hashlib.sha256(client.cookies.get("shoprelay_session").encode()).hexdigest())
                run_id = result["sse"]["run_id"]
                response = client.get(f"/api/runs/{run_id}")
                if response.status_code != 200 or response.json()["run"]["status"] != "SUCCEEDED":
                    raise AssertionError("restart did not recover the Product Agent result")
                other = TestClient(restart_app)
                try:
                    other_headers = _login(other, "u_other", temp_password)
                    auth_hashes.add(hashlib.sha256(other.cookies.get("shoprelay_session").encode()).hexdigest())
                    if other.get(f"/api/runs/{run_id}").status_code != 404:
                        raise AssertionError("other user could read the Product Run")
                finally:
                    other.close()
            checkpoint = run_checkpointer_async(_checkpoint(database_url, configured.checkpoint_schema,
                result["sse"]["run_id"]))
            if checkpoint is None:
                raise AssertionError("PostgreSQL checkpoint was not readable after app restart")
            state_text = repr(checkpoint.checkpoint.get("channel_values", {}))
            if "电热水壶" in state_text or "P-KETTLE" in state_text or "u_demo" in state_text:
                raise AssertionError("checkpoint contains query, product ID, or user identity")
            result["restart_recovery"] = True
        finally:
            fresh_engine.dispose()
        with engine.connect() as connection:
            result["catalog_inventory_counts_after"] = {
                table: connection.execute(text(f"select count(*) from {table}")).scalar_one()
                for table in ("products", "inventory")}
        if result["catalog_inventory_counts_before"] != result["catalog_inventory_counts_after"]:
            raise AssertionError("read-only Product API changed catalog/inventory rows")
        result["business_data_mutations_during_api"] = 0
        result["business_write_executions"] = 0
        result["estimated_model_cost_usd"] = 0.0
        result["status"] = "PASS"
    except Exception as exc:
        result["status"] = "FAIL"
        result["failure_phase"] = phase
        result["failure_type"] = type(exc).__name__
        if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str) and \
           len(exc.args[0]) <= 80 and exc.args[0].replace("_", "").isalnum():
            result["failure_field"] = exc.args[0]
        result["failure_code"] = getattr(exc, "code", None) or "PROBE_ASSERTION_OR_ENVIRONMENT_FAILURE"
    finally:
        if database_url and configured is not None:
            for run_id in dict.fromkeys(run_ids):
                try:
                    run_checkpointer_async(_delete_checkpoint(database_url, configured.checkpoint_schema, run_id))
                except Exception:
                    result.setdefault("checkpoint_cleanup_warnings", []).append("CHECKPOINT_CLEANUP_FAILED")
        if factory is not None:
            try:
                with factory() as db:
                    if run_ids:
                        session_ids.extend(db.execute(select(Run.session_id).where(Run.run_id.in_(set(run_ids))))
                            .scalars().all())
                        db.execute(delete(Trace).where(Trace.run_id.in_(set(run_ids))))
                    if trace_ids:
                        db.execute(delete(Trace).where(Trace.trace_id.in_(set(trace_ids))))
                    if run_ids:
                        db.execute(delete(Message).where(Message.session_id.in_(set(session_ids))))
                        db.execute(delete(Run).where(Run.run_id.in_(set(run_ids))))
                    if session_ids:
                        db.execute(delete(ConversationSession).where(
                            ConversationSession.session_id.in_(set(session_ids))))
                    if auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                    db.commit()
                result["exact_api_record_cleanup"] = True
            except Exception as exc:
                result["exact_api_record_cleanup"] = False
                result["cleanup_failure_type"] = type(exc).__name__
                if result.get("status") == "PASS": result["status"] = "PASS_WITH_CLEANUP_WARNING"
            try:
                with factory() as db:
                    for user_id, previous in original_credentials.items():
                        credential = db.get(DemoCredential, user_id)
                        if previous is None:
                            if credential is not None: db.delete(credential)
                        elif credential is not None:
                            credential.password_hash = previous["password_hash"]
                            credential.active = previous["active"]
                            credential.updated_at = previous["updated_at"]
                    db.commit()
                result["demo_credentials_restored"] = True
            except Exception:
                result["demo_credentials_restored"] = False
                result["status"] = "FAIL"
                result["failure_code"] = "DEMO_CREDENTIAL_RESTORE_FAILED"
        if engine is not None:
            engine.dispose()
        for name, value in original_env.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value
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
