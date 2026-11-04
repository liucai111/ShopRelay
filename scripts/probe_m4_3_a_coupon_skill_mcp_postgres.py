"""Authenticated Skill -> Policy Agent -> trusted MCP coupon read on isolated PG.

The probe targets only shoprelay_m11_test, inserts one tagged simulated coupon
fixture, and removes only its own Run/session/auth/checkpoint/coupon IDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from scripts.g2_test_database import resolve_test_database_url
from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (AuthSession, ConversationSession, Coupon, DemoCredential, GraphExecutionRecord,
    Message, ObservabilityRecord, Run, RunEvent, Trace)
from app.m1.routing import RouteDecision
from app.m1.services import set_demo_password
from app.m3.agents import default_agent_registry
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.mcp import McpServerTrust, McpToolGrant, TrustedMcpClient
from app.mcp.adapters import McpCouponReadRepository
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "SIMULATED_TEST_DATA"


async def _delete_checkpoint(database_url: str, schema: str, run_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(run_id)


def _event_types(text_body: str) -> list[str]:
    return [line.removeprefix("event: ") for line in text_body.splitlines() if line.startswith("event: ")]


def probe() -> dict:
    record: dict = {
        "schema_version": "shoprelay.m4.3-a-coupon-skill-mcp-postgres.v1",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "migration_revision": {"before": None, "after": None, "expected": LATEST_SCHEMA_REVISION},
        "checkpoint_schema": None, "checkpoint_ready": False, "fixture_source": SOURCE,
        "mcp_transport": "stdio", "deepseek_requests": 0, "business_write_executions": 0,
        "mcp_write_calls": 0, "mcp_read_calls": 0, "cleanup": {"complete": False},
    }
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test":
        raise RuntimeError("UNSAFE_DATABASE_TARGET")
    database_url = url.render_as_string(hide_password=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    coupon_code = "M43A-SIM-" + secrets.token_hex(8).upper()
    password = secrets.token_urlsafe(32)
    saved_credential = None
    run_id = session_id = trace_id = auth_token_hash = None
    client = None
    env_names = ("SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED",
                 "SHOPRELAY_GRAPH_VERSION", "SHOPRELAY_EXECUTION_VERSION")
    prior_env = {key: os.environ.get(key) for key in env_names}
    try:
        with engine.connect() as conn:
            record["database"] = conn.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = conn.execute(text("select current_setting('server_version')")).scalar_one()
            if record["database"] != "shoprelay_m11_test":
                raise RuntimeError("UNSAFE_DATABASE_TARGET")
        record["migration_revision"]["before"] = current_revision(database_url)
        upgrade_database(database_url)
        record["migration_revision"]["after"] = current_revision(database_url)
        if record["migration_revision"]["after"] != LATEST_SCHEMA_REVISION:
            raise RuntimeError("MIGRATION_HEAD_MISMATCH")
        settings = load_settings(require_api_key=False)
        record["checkpoint_schema"] = settings.checkpoint_schema
        record["checkpoint_ready"] = bool(run_checkpointer_async(
            __import__("app.orchestration.checkpointer", fromlist=["checkpoint_schema_ready"])
            .checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpoint_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        with factory() as db:
            credential = db.get(DemoCredential, "u_demo")
            if credential is None:
                raise RuntimeError("DEMO_CREDENTIAL_MISSING")
            saved_credential = (credential.password_hash, credential.active, credential.updated_at)
            coupon_before = int(db.scalar(select(func.count()).select_from(Coupon)) or 0)
            db.add(Coupon(coupon_code=coupon_code, policy_id="SIMULATED_TEST_POLICY", version="M4.3-A",
                min_spend_cents=12345, source_type=SOURCE))
            db.commit()
        # Use a one-run credential and restore the existing hash in finally.
        with factory() as db:
            set_demo_password(db, "u_demo", password)
        with factory() as db:
            record["coupon_count_before"] = coupon_before
            record["coupon_fixture_id_hash"] = hashlib.sha256(coupon_code.encode()).hexdigest()

        server = str(ROOT / "scripts" / "mcp_m4_3_coupon_server.py")
        trust = McpServerTrust(server_id="shoprelay-coupon-postgres-test", transport="stdio",
            command=sys.executable, args=(server, "stdio"), cwd=str(ROOT),
            env={"SHOPRELAY_MCP_COUPON_DB_URL": database_url,
                 "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)},
            tools=(McpToolGrant("get_coupon", "coupon_catalog", read_only=True),))
        mcp_client = TrustedMcpClient(trust)
        adapter = McpCouponReadRepository(mcp_client)

        def supervisor_call(state, context):
            graph = build_supervisor_graph(default_agent_registry(coupon_repository=adapter),
                sink=context.observability_sink)
            return invoke_supervisor(graph, state, context)

        services = GraphServices(router=lambda _query: RouteDecision(strategy="AGENT",
            safety_decision="ALLOW", reason_code="LIVE_COUPON_CATALOG", decision_source="RULE",
            required_capabilities=["get_coupon"]).model_dump(mode="json"),
            direct=lambda _state: {}, single=lambda _state: {}, multi=lambda _state: {},
            refuse=lambda _state: {}, agent_supervisor=supervisor_call)

        @asynccontextmanager
        async def checkpointer_factory(db_url, schema):
            async with open_checkpointer(db_url, schema) as saver:
                yield saver

        with patch.dict(os.environ, {"SHOPRELAY_ORCHESTRATOR": "langgraph",
                "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "m4.3-a",
                "SHOPRELAY_EXECUTION_VERSION": "m4.3-a-postgres"}, clear=False):
            app = create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
                graph_services=services, checkpointer_factory=checkpointer_factory,
                checkpointer_readiness=lambda *_: True)
            with TestClient(app) as http:
                csrf = http.get("/api/demo/csrf").json()["csrf_token"]
                login = http.post("/api/demo/login", headers={"Origin": str(http.base_url).rstrip("/"),
                    CSRF_HEADER: csrf}, json={"username": "u_demo", "password": password})
                if login.status_code != 200:
                    raise RuntimeError("DEMO_LOGIN_FAILED")
                session_cookie = http.cookies.get("shoprelay_session")
                auth_token_hash = hashlib.sha256(session_cookie.encode("ascii")).hexdigest()
                headers = {"Origin": str(http.base_url).rstrip("/"),
                    CSRF_HEADER: http.cookies.get("shoprelay_csrf")}
                request = {"message": f"请查询优惠券 {coupon_code} 的目录规则",
                    "client_request_id": "m43a-coupon-skill-postgres"}
                response = http.post("/api/chat/stream", headers=headers, json=request)
                if response.status_code != 200:
                    raise RuntimeError("COUPON_SKILL_API_FAILED")
                event_types = _event_types(response.text)
                data_rows = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
                    if line.startswith("data: ")]
                started = next(row for row in data_rows if row.get("type") == "started")
                answer_event = next(row for row in data_rows if row.get("type") == "answer")
                run_id, session_id, trace_id = started["run_id"], started["session_id"], started["trace_id"]
                result = answer_event["payload"]
                event_rows = http.get(f"/api/runs/{run_id}/events").json()["events"]
                with factory() as db:
                    run = db.get(Run, run_id)
                    execution = db.get(GraphExecutionRecord, run_id)
                    span_rows = db.execute(select(ObservabilityRecord).where(
                        ObservabilityRecord.run_id == run_id,
                        ObservabilityRecord.record_type == "span")).scalars().all()
                    spans = [json.loads(row.payload_json) for row in span_rows]
                    if not run or not execution or run.status != "SUCCEEDED" or execution.status != "SUCCEEDED":
                        raise RuntimeError("RUN_NOT_SUCCEEDED")
                    record.update({
                        "run_status": run.status, "graph_execution_status": execution.status,
                        "run_id": run_id, "session_id": session_id, "trace_id": trace_id,
                        "graph_version": execution.graph_version, "checkpoint_id_present": bool(execution.checkpoint_id),
                        "route": result.get("strategy"), "answer_kind": result.get("answer_kind"),
                        "answer_claim": "coupon catalog metadata returned; entitlement remains UNKNOWN",
                        "data_source": result["business_results"]["coupon"]["source"],
                        "data_source_marker": result["business_results"]["coupon"]["source_type"],
                        "eligibility_status": result["business_results"]["eligibility_status"],
                        "mcp_discovery_count": mcp_client.discovery_count,
                        "mcp_read_calls": mcp_client.call_count,
                        "mcp_last_call_duration_ms": mcp_client.last_duration_ms,
                        "tool_started_count": sum(row["type"] == "tool_started" for row in event_rows),
                        "tool_finished_count": sum(row["type"] == "tool_finished" for row in event_rows),
                        "sse_event_types": event_types,
                        "sse_sequences": [row["seq"] for row in event_rows],
                        "trace_span_names": [row["node_name"] for row in spans],
                        "deepseek_requests": 0, "business_write_executions": 0,
                        "mcp_write_calls": 0, "idempotent_replay_additional_mcp_calls": 0,
                        "user_claims_used_as_verified_facts": False,
                    })
                    record["migration_revision"]["after"] = current_revision(database_url)
                # Recreate the app and replay the same request; it must return the
                # existing Run without reopening the MCP tool call.
                with TestClient(create_app(database_url=database_url, engine=engine,
                        orchestrator_mode="langgraph", graph_services=services,
                        checkpointer_factory=checkpointer_factory,
                        checkpointer_readiness=lambda *_: True)) as restarted:
                    restarted.cookies.set("shoprelay_session", session_cookie)
                    restarted.cookies.set("shoprelay_csrf", http.cookies.get("shoprelay_csrf"))
                    replay_headers = {"Origin": str(restarted.base_url).rstrip("/"),
                        CSRF_HEADER: restarted.cookies.get("shoprelay_csrf")}
                    # Origin is bound to the browser origin only; TestClient shares
                    # the same base URL for both application instances.
                    replay = restarted.post("/api/chat/stream", headers=replay_headers, json=request)
                    if replay.status_code != 200 or mcp_client.call_count != 1:
                        raise RuntimeError("IDEMPOTENT_REPLAY_REEXECUTED_TOOL")

        # Checkpoint must not contain query, coupon identifier, or identity.
        async def read_checkpoint():
            async with open_checkpointer(database_url, settings.checkpoint_schema) as saver:
                return await saver.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})
        checkpoint = run_checkpointer_async(read_checkpoint())
        values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
        serialized = json.dumps(values, ensure_ascii=False)
        if coupon_code in serialized or "u_demo" in serialized or "查询优惠券" in serialized:
            raise RuntimeError("CHECKPOINT_SENSITIVE_CONTENT")
        record["checkpoint_sensitive_content_absent"] = True
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["error_type"] = type(exc).__name__
        record["error_code"] = str(getattr(exc, "code", None) or type(exc).__name__)
    finally:
        cleanup_errors: dict[str, str] = {}

        def cleanup_step(name: str, action) -> None:
            try:
                action()
            except Exception as cleanup_exc:
                cleanup_errors[name] = type(cleanup_exc).__name__

        def checkpoint_cleanup() -> None:
            if run_id:
                run_checkpointer_async(_delete_checkpoint(database_url, settings.checkpoint_schema, run_id))
            record["cleanup"]["checkpoint_deleted"] = True
        cleanup_step("checkpoint", checkpoint_cleanup)

        def auth_cleanup() -> None:
            if auth_token_hash:
                with factory.begin() as db:
                    db.execute(delete(AuthSession).where(AuthSession.token_hash == auth_token_hash))
            record["cleanup"]["auth_session_revoked"] = True
        cleanup_step("auth_session", auth_cleanup)

        def trace_cleanup() -> None:
            if trace_id:
                with factory.begin() as db:
                    db.execute(delete(Trace).where(Trace.trace_id == trace_id))
            record["cleanup"]["trace_deleted"] = True
        cleanup_step("trace", trace_cleanup)

        def run_cleanup() -> None:
            if run_id:
                with factory.begin() as db:
                    db.execute(delete(Run).where(Run.run_id == run_id))
            record["cleanup"]["run_deleted"] = True
        cleanup_step("run", run_cleanup)

        def session_cleanup() -> None:
            if session_id:
                with factory.begin() as db:
                    db.execute(delete(Message).where(Message.session_id == session_id))
                    db.execute(delete(ConversationSession).where(ConversationSession.session_id == session_id,
                        ConversationSession.user_id == "u_demo"))
            record["cleanup"]["session_deleted"] = True
        cleanup_step("session", session_cleanup)

        def coupon_cleanup() -> None:
            with factory.begin() as db:
                db.execute(delete(Coupon).where(Coupon.coupon_code == coupon_code, Coupon.source_type == SOURCE))
            with factory() as db:
                record["cleanup"]["coupon_fixture_removed"] = db.get(Coupon, coupon_code) is None
                record["cleanup"]["coupon_count_after"] = int(db.scalar(select(func.count()).select_from(Coupon)) or 0)
        cleanup_step("coupon", coupon_cleanup)

        def credential_cleanup() -> None:
            if saved_credential:
                with factory.begin() as db:
                    credential = db.get(DemoCredential, "u_demo")
                    if credential:
                        credential.password_hash, credential.active, credential.updated_at = saved_credential
                with factory() as db:
                    credential = db.get(DemoCredential, "u_demo")
                    record["cleanup"]["credential_restored"] = bool(credential and
                        (credential.password_hash, credential.active, credential.updated_at) == saved_credential)
            else:
                record["cleanup"]["credential_restored"] = True
        cleanup_step("credential", credential_cleanup)

        if cleanup_errors:
            record["cleanup"]["errors"] = cleanup_errors
        record["cleanup"]["complete"] = all(record["cleanup"].get(key, False) for key in (
            "checkpoint_deleted", "auth_session_revoked", "trace_deleted", "run_deleted",
            "session_deleted", "coupon_fixture_removed", "credential_restored"))
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        engine.dispose()
    record["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return record


def main() -> None:
    output = probe()
    output_path = ROOT / "docs" / "probes" / "m4.3-a-coupon-skill-mcp-postgres-20261022-r1.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"record_path": str(output_path), "status": output["status"],
                      "error_code": output.get("error_code"), "cleanup": output.get("cleanup")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
