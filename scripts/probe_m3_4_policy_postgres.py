"""M3.4 Policy Agent PostgreSQL integration probe (shoprelay_m11_test only)."""
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
    record = {"schema_version": "shoprelay.m3.4-policy-postgres.v1", "started_at": _utc(),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "deepseek_requests": 0, "business_write_executions": 0, "business_data_mutations": 0,
        "route_source": "deterministic AGENT/search_knowledge integration injection",
        "business_capability": "search_knowledge"}
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
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "g2-m3.4"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m3.4-policy-v1"
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

        route = {"strategy": "AGENT", "sub_strategy": None, "safety_decision": "ALLOW",
            "reason_code": "POLICY_AGENT", "decision_source": "RULE",
            "required_capabilities": ["search_knowledge"], "needs_clarification": False}
        services = GraphServices(router=lambda _q: route, direct=lambda _s: {}, single=lambda _s: {},
            multi=lambda _s: {}, refuse=lambda _s: {})
        from app.m1 import api as api_module
        original_loader = api_module.load_settings
        def settings_loader(**kwargs):
            loaded = original_loader(**kwargs)
            return replace(loaded, orchestrator="langgraph", m3_supervisor_enabled=True,
                graph_version="g2-m3.4", execution_version="m3.4-policy-v1")
        api_module.load_settings = settings_loader
        app = create_app(database_url=database_url, engine=engine, orchestrator_mode="langgraph",
            checkpointer_factory=open_checkpointer, graph_services=services)
        message = "退款申请需要满足什么条件？"
        request_id = "m34-policy-postgres-" + secrets.token_hex(8)
        phase = "authenticated_policy_agent_api"
        try:
            with TestClient(app) as client:
                headers, cookie = _login(client, "u_demo", password)
                auth_hashes.add(hashlib.sha256(cookie.encode()).hexdigest())
                response = client.post("/api/chat", headers=headers,
                    json={"message": message, "client_request_id": request_id, "user_id": "u_other"})
                if response.status_code != 200:
                    raise RuntimeError("POLICY_AGENT_API_FAILED")
                body = response.json()
                if body.get("status") != "SUCCEEDED" or body.get("strategy") != "AGENT" or \
                   body.get("answer_kind") != "POLICY_ANSWER":
                    raise RuntimeError("POLICY_AGENT_NOT_SUCCEEDED")
                if not body.get("citations") or not body.get("knowledge_version"):
                    raise RuntimeError("POLICY_CITATION_MISSING")
                evidence_ids = set(body.get("business_results", {}).get("evidence_ids", []))
                if not evidence_ids or any(row.get("chunk_id") not in evidence_ids for row in body["citations"]):
                    raise RuntimeError("CITATION_NOT_FROM_CURRENT_RETRIEVAL")
                if body.get("tool_references", [{}])[0].get("source") != "shoprelay_policy_retrieval":
                    raise RuntimeError("POLICY_SOURCE_REFERENCE_INVALID")
                run_id, session_id, trace_id = body["run_id"], body["session_id"], body["trace_id"]
                run_ids.append(run_id); session_ids.append(session_id); trace_ids.append(trace_id)
                evaluation_at = body.get("policy_retrieval", {}).get("evaluation_at")
                fingerprint = body.get("retrieval", {}).get("index_fingerprint")
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
                    expected_events = ["started", "route_decided", "agent_started", "tool_started",
                        "retrieval_hop", "tool_finished", "answer", "done"]
                    if event_types != expected_events:
                        raise RuntimeError("SSE_EVENT_SEQUENCE_INVALID")
                    if run is None or graph is None or run.status != "SUCCEEDED" or graph.status != "SUCCEEDED":
                        raise RuntimeError("PERSISTED_RUN_STATE_MISMATCH")
                    if graph.graph_version != "g2-m3.4" or graph.execution_version != "m3.4-policy-v1":
                        raise RuntimeError("M3_4_EXECUTION_VERSION_NOT_PERSISTED")
                    tool_spans = [row for row in spans if row.get("node_name") == "m3.tool.search_knowledge"]
                    if len(tool_spans) != 1 or tool_spans[0].get("status") != "OK":
                        raise RuntimeError("POLICY_TOOL_SPAN_INVALID")
                    if any(sensitive in logs for sensitive in (message, "u_demo", "policies/refund")):
                        raise RuntimeError("SENSITIVE_POLICY_LOG_LEAK")
                    if not graph.checkpoint_id:
                        raise RuntimeError("CHECKPOINT_ID_MISSING")
                    record.update({"run_id": run_id, "session_id": session_id, "trace_id": trace_id,
                        "checkpoint_id": graph.checkpoint_id, "run_status": run.status,
                        "graph_execution_status": graph.status, "graph_version": graph.graph_version,
                        "answer_kind": body["answer_kind"], "answer": body["answer"],
                        "knowledge_version": body["knowledge_version"],
                        "index_fingerprint": body.get("retrieval", {}).get("index_fingerprint"),
                        "retrieval_mode": body.get("retrieval", {}).get("mode"),
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
                   sum(row["type"] == "tool_started" for row in replay_events) != 1:
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
                record["run_result_persisted"] = True
                record["sensitive_logs_absent"] = True
                record["business_data_mutations"] = 0
                record["business_write_executions"] = 0
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
            checkpoint_text = repr(checkpoint.checkpoint.get("channel_values", {}))
            policy_excerpt = body["citations"][0].get("content", "")
            if any(value and value in checkpoint_text for value in (message, "u_demo", policy_excerpt)):
                raise RuntimeError("SENSITIVE_STATE_IN_CHECKPOINT")
            record["restart_recovery"] = True
            record["checkpoint_payload_sanitized"] = True
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
