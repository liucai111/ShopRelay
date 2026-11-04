"""Isolated G2 FastAPI + PostgreSQL Checkpointer smoke test.

Only targets shoprelay_m11_test on loopback. It never drops schemas or tables,
and all synthetic users and the exact unique checkpoint thread are cleaned up.
"""
from __future__ import annotations

import argparse
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from scripts.g2_test_database import resolve_test_database_url
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run, RunEvent, Trace, User
from app.m1.routing import RouteDecision
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import (CHECKPOINTER_VERSION, PSYCOPG_POOL_VERSION,
                                            checkpoint_schema_ready, open_checkpointer,
                                            run_checkpointer_async)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = "20261020_05"
def _route(_query: str) -> dict:
    return RouteDecision(strategy="DIRECT", sub_strategy=None, safety_decision="ALLOW",
        reason_code="G2_POSTGRES_STUB", decision_source="RULE").model_dump(mode="json")


def _services(call_counter: list[int]) -> GraphServices:
    def direct(_state: dict) -> dict:
        call_counter.append(1)
        return {"answer": "G2 PostgreSQL stub response", "answer_kind": "DIRECT",
                "terminal_status": "SUCCEEDED", "model_request_count": 0}
    return GraphServices(router=_route, direct=direct, single=lambda _state: {}, multi=lambda _state: {},
                         refuse=lambda _state: {})


def _login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: token},
                           json={"username": user_id, "password": password})
    if response.status_code != 200:
        raise AssertionError("test user could not authenticate")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf", "")}


def _cleanup_probe_users(engine, user_ids: tuple[str, str]) -> dict[str, int]:
    """Delete only this probe's synthetic identity rows in FK-safe order."""
    if len(set(user_ids)) != 2 or not all(user_id.startswith("g2probe_") for user_id in user_ids):
        raise RuntimeError("synthetic identity cleanup received unexpected identifiers")
    params = {"user_a": user_ids[0], "user_b": user_ids[1]}
    user_filter = "user_id IN (:user_a, :user_b)"
    run_filter = f"run_id IN (SELECT run_id FROM runs WHERE {user_filter})"
    session_filter = f"session_id IN (SELECT session_id FROM sessions WHERE {user_filter})"
    statements = {
        "observability_records": f"DELETE FROM observability_records WHERE {user_filter}",
        "graph_executions": f"DELETE FROM graph_executions WHERE {user_filter}",
        "run_events": f"DELETE FROM run_events WHERE {run_filter} OR {session_filter}",
        "traces": f"DELETE FROM traces WHERE {user_filter} OR {run_filter}",
        "messages": f"DELETE FROM messages WHERE {session_filter}",
        "runs": f"DELETE FROM runs WHERE {user_filter}",
        "sessions": f"DELETE FROM sessions WHERE {user_filter}",
        "auth_sessions": f"DELETE FROM auth_sessions WHERE {user_filter}",
        "demo_credentials": f"DELETE FROM demo_credentials WHERE {user_filter}",
        "users": f"DELETE FROM users WHERE {user_filter}",
    }
    counts: dict[str, int] = {}
    with engine.begin() as conn:
        found = set(conn.execute(text(f"SELECT user_id FROM users WHERE {user_filter}"), params).scalars())
        if found != set(user_ids):
            raise RuntimeError("synthetic identity cleanup did not match the exact probe users")
        orders = conn.execute(text(f"SELECT count(*) FROM orders WHERE {user_filter}"), params).scalar_one()
        if orders:
            raise RuntimeError("synthetic probe identities unexpectedly own orders; refusing cleanup")
        for table, statement in statements.items():
            counts[table] = conn.execute(text(statement), params).rowcount
    return counts


def _app(database_url, engine, services, user_id):
    return create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
        observability_admin_user_ids=(user_id,), graph_services=services)


async def _checkpoint_roundtrip(database_url: str, schema: str, thread_id: str) -> dict:
    from langgraph.checkpoint.base import empty_checkpoint

    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"graph_version": "g2", "status": "probe-persisted"}
    metadata = {"source": "input", "step": 0, "parents": {}}
    async with open_checkpointer(database_url, schema) as saver:
        saved_config = await saver.aput(config, checkpoint, metadata, {})
        saved_id = saved_config["configurable"]["checkpoint_id"]
    # A new saver/connection verifies durable read after closing the writer.
    async with open_checkpointer(database_url, schema) as saver:
        loaded = await saver.aget_tuple(saved_config)
        isolated = await saver.aget_tuple({"configurable": {"thread_id": thread_id + "-other", "checkpoint_ns": ""}})
        if loaded is None or loaded.checkpoint["channel_values"].get("status") != "probe-persisted":
            raise AssertionError("checkpoint was not readable after reopening the connection")
        if isolated is not None:
            raise AssertionError("unique checkpoint thread isolation failed")
    return {"checkpoint_id": saved_id, "reopened": True, "different_thread_isolated": True}


async def _read_thread(database_url: str, schema: str, thread_id: str) -> dict:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        loaded = await saver.aget_tuple(config)
    if loaded is None:
        raise AssertionError("completed API Run checkpoint was not readable after app restart")
    values = loaded.checkpoint.get("channel_values", {})
    serialized = repr(values)
    if "G2 persistence probe" in serialized or "G2 PostgreSQL stub response" in serialized:
        raise AssertionError("query or full answer text was written to the checkpoint")
    return {"checkpoint_id": loaded.config["configurable"].get("checkpoint_id"),
            "contains_query_or_answer": False, "terminal_status": values.get("terminal_status")}


def run(args) -> dict:
    now = datetime.now(timezone.utc)
    evidence: dict = {"schema_version": "shoprelay.langgraph-g2-postgres-probe.v1",
        "started_at": now.isoformat().replace("+00:00", "Z"), "status": "NOT_RUN",
        "database": "shoprelay_m11_test", "application_revision_expected": EXPECTED_REVISION,
        "checkpointer_schema": args.schema, "langgraph_checkpoint_postgres": CHECKPOINTER_VERSION,
        "psycopg_pool": PSYCOPG_POOL_VERSION, "real_deepseek_requests": 0,
        "model_requests": 0, "provider": "deterministic test stub"}
    phase = "configuration"
    engine = None
    user_a = "g2probe_" + secrets.token_hex(10)
    user_b = "g2probe_" + secrets.token_hex(10)
    run_id = None
    database_url = None
    created_users = False
    try:
        url = resolve_test_database_url(args.database_url)
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        phase = "database_connection"
        with engine.connect() as conn:
            evidence["postgres_server_version"] = conn.execute(text("SELECT current_setting('server_version')")).scalar_one()
            evidence["current_database"] = conn.execute(text("SELECT current_database()")).scalar_one()
        revision = current_revision(database_url)
        if args.upgrade_db:
            phase = "application_schema_upgrade"
            upgrade_database(database_url)
            revision = current_revision(database_url)
            evidence["application_schema_upgrade_requested"] = True
        if revision != EXPECTED_REVISION:
            raise RuntimeError("APPLICATION_SCHEMA_NOT_UPGRADED")
        evidence["application_revision"] = revision
        phase = "checkpointer_schema_setup"
        if args.setup_checkpointer:
            from scripts.setup_langgraph_checkpointer import setup
            run_checkpointer_async(setup(database_url, args.schema))
            evidence["checkpointer_setup_requested"] = True
        ready = run_checkpointer_async(checkpoint_schema_ready(database_url, args.schema))
        if not ready:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_INITIALIZED")
        evidence["checkpointer_schema_ready"] = True

        phase = "test_identity_setup"
        factory = make_session_factory(engine)
        password = "g2-probe-" + secrets.token_urlsafe(24)
        with factory() as db:
            db.add_all([User(user_id=user_a, display_name="G2 Probe A"), User(user_id=user_b, display_name="G2 Probe B")])
            db.commit()
            set_demo_password(db, user_a, password)
            set_demo_password(db, user_b, password)
        created_users = True

        calls: list[int] = []
        services = _services(calls)
        phase = "fastapi_langgraph_execution"
        request_id = "g2-postgres-" + uuid.uuid4().hex
        first_engine = make_engine(database_url)
        try:
            first_app = _app(database_url, first_engine, services, user_a)
            with TestClient(first_app) as client:
                headers = _login(client, user_a, password)
                response = client.post("/api/chat/stream", headers=headers,
                    json={"message": "G2 persistence probe", "client_request_id": request_id})
                if response.status_code != 200:
                    raise AssertionError("LangGraph FastAPI request failed")
                events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
                          if line.startswith("data: ")]
                event_types = [line.removeprefix("event: ") for line in response.text.splitlines()
                               if line.startswith("event: ")]
                if event_types != ["started", "route_decided", "answer", "done"]:
                    raise AssertionError("SSE event order or terminal events are invalid")
                run_id = events[0]["run_id"]
                seqs = [item["seq"] for item in events]
                if seqs != sorted(set(seqs)):
                    raise AssertionError("SSE seq values are not strictly increasing")
        finally:
            first_engine.dispose()

        phase = "postgres_checkpoint_restart_read"
        checkpoint_roundtrip = run_checkpointer_async(_checkpoint_roundtrip(database_url, args.schema, run_id + "-rt"))
        phase = "database_persistence_and_authorization"
        with factory() as db:
            run = db.get(Run, run_id)
            graph = db.get(GraphExecutionRecord, run_id)
            mode_trace = db.execute(select(Trace).where(Trace.run_id == run_id,
                Trace.event_type == "orchestrator_selected")).scalar_one_or_none()
            stored_events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)).scalars().all()
            observability = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id == run_id)).scalars().all()
            if run is None or run.status != "SUCCEEDED" or graph is None or graph.status != "SUCCEEDED":
                raise AssertionError("business Run and graph status disagree")
            if graph.thread_id != run_id or not graph.checkpoint_id:
                raise AssertionError("Run-to-checkpoint mapping is incomplete")
            if mode_trace is None or json.loads(mode_trace.event_json).get("orchestrator") != "langgraph":
                raise AssertionError("actual orchestrator selection was not traced")
            if [item.event_type for item in stored_events] != ["started", "route_decided", "answer", "done"]:
                raise AssertionError("persisted SSE event order is inconsistent")
            if not observability or not {item.record_type for item in observability} >= {"span", "metric", "log"}:
                raise AssertionError("persistent observability records are incomplete")
            request_payload = json.loads(run.result_json)
            if request_payload.get("answer") != "G2 PostgreSQL stub response":
                raise AssertionError("final business answer is not persisted")
        phase = "owner_isolation_and_restart_idempotency"
        checkpoint_read = run_checkpointer_async(_read_thread(database_url, args.schema, run_id))
        second_engine = make_engine(database_url)
        try:
            restarted_app = _app(database_url, second_engine, services, user_a)
            with TestClient(restarted_app) as client:
                headers = _login(client, user_a, password)
                replay = client.get(f"/api/runs/{run_id}/events?after_seq=0")
                if replay.status_code != 200 or [item["type"] for item in replay.json()["events"]] != [
                        "started", "route_decided", "answer", "done"]:
                    raise AssertionError("restarted app could not replay persisted SSE events")
                repeated = client.post("/api/chat", headers=headers,
                    json={"message": "G2 persistence probe", "client_request_id": request_id})
                if repeated.status_code != 200 or calls != [1]:
                    raise AssertionError("restarted app duplicate request was not idempotent")
                end = datetime.now(timezone.utc) + timedelta(minutes=1)
                start = datetime.now(timezone.utc) - timedelta(minutes=1)
                metrics = client.get("/api/admin/observability/metrics?from=" +
                    start.isoformat().replace("+00:00", "Z") + "&to=" + end.isoformat().replace("+00:00", "Z"))
                if metrics.status_code != 200 or metrics.json()["metrics"]["node_execution_count"] < 2:
                    raise AssertionError("authorized metrics did not include persisted graph node data")
            with TestClient(restarted_app) as other_client:
                other_headers = _login(other_client, user_b, password)
                if other_client.get(f"/api/runs/{run_id}").status_code != 404:
                    raise AssertionError("a different owner read the graph Run")
                if other_client.get(f"/api/runs/{run_id}/events?after_seq=0").status_code != 404:
                    raise AssertionError("a different owner read the graph Run events")
        finally:
            second_engine.dispose()
        evidence.update({"checkpoint_roundtrip": checkpoint_roundtrip, "api_checkpoint_after_restart": checkpoint_read,
            "run_id": run_id,
            "sse_event_types": event_types, "sse_sequences": seqs, "persisted_event_count": len(stored_events),
            "observability_record_types": sorted({item.record_type for item in observability}),
            "stub_executor_calls": len(calls), "request_idempotency_after_restart": "verified"})
        evidence["status"] = "PASS"
    except Exception as exc:
        code = str(exc) if str(exc) in {"APPLICATION_SCHEMA_NOT_UPGRADED", "CHECKPOINTER_SCHEMA_NOT_INITIALIZED"} else None
        database_phase = phase in {"database_connection", "application_schema_upgrade", "checkpointer_schema_setup",
                                   "fastapi_langgraph_execution", "postgres_checkpoint_restart_read",
                                   "database_persistence_and_authorization", "owner_isolation_and_restart_idempotency"}
        db_unavailable = phase == "database_connection" and isinstance(exc, SQLAlchemyError)
        evidence.update({"status": "BLOCKED" if isinstance(exc, (ConnectionError, OSError)) or code or db_unavailable or database_phase and type(exc).__name__ == "RuntimeError" else "FAIL",
                         "failure_phase": phase, "error_type": type(exc).__name__})
        if code:
            evidence["blocked_reason"] = code
        elif db_unavailable:
            evidence["blocked_reason"] = "POSTGRESQL_UNAVAILABLE"
    finally:
        if database_url and run_id:
            try:
                run_checkpointer_async(_delete_probe_thread(database_url, args.schema, run_id))
                run_checkpointer_async(_delete_probe_thread(database_url, args.schema, run_id + "-rt"))
                evidence["checkpoint_probe_cleanup"] = "deleted_exact_unique_test_thread"
            except Exception as exc:
                evidence["checkpoint_probe_cleanup"] = "not_confirmed"
                evidence["checkpoint_cleanup_error_type"] = type(exc).__name__
        if engine is not None:
            try:
                if created_users:
                    evidence["synthetic_cleanup_counts"] = _cleanup_probe_users(engine, (user_a, user_b))
                engine.dispose()
                evidence["synthetic_user_cleanup"] = "complete" if created_users else "not_created"
            except Exception as exc:
                evidence["synthetic_user_cleanup"] = "not_confirmed"
                evidence["synthetic_cleanup_error_type"] = type(exc).__name__
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if evidence["status"] == "PASS" and (evidence.get("synthetic_user_cleanup") != "complete" or
                                               evidence.get("checkpoint_probe_cleanup") != "deleted_exact_unique_test_thread"):
            evidence["status"] = "FAIL"
            evidence["cleanup_failure"] = True
        output = ROOT / "docs" / "probes" / f"langgraph-g2-postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"status": evidence["status"], "failure_phase": evidence.get("failure_phase"),
                          "blocked_reason": evidence.get("blocked_reason"), "evidence_path": str(output)}, ensure_ascii=False))
    return evidence


async def _delete_probe_thread(database_url: str, schema: str, thread_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(thread_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="G2 isolated PostgreSQL/Checkpointer API smoke test")
    parser.add_argument("--database-url", help="Must name only shoprelay_m11_test; never printed")
    parser.add_argument("--schema", default="shoprelay_langgraph")
    parser.add_argument("--upgrade-db", action="store_true", help="Upgrade only shoprelay_m11_test to Alembic head")
    parser.add_argument("--setup-checkpointer", action="store_true", help="Explicitly create/setup only the configured checkpoint schema")
    args = parser.parse_args()
    result = run(args)
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
