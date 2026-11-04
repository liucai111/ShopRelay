"""PostgreSQL integration probe for M5.3-A admin observability.

Uses the repository's isolated-test database resolver, creates uniquely tagged
synthetic observability rows and two temporary demo identities, exercises the
admin APIs through FastAPI authentication, verifies data after opening a new
engine, and deletes only rows belonging to this probe.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".vendor"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.engine import URL

from app.m0.config import LOCAL_CONFIG, load_settings, parse_env_file
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory
from app.m1.models import (ConversationSession, DemoCredential, GraphExecutionRecord,
                           ActionAudit, ObservabilityRecord, PendingAction, Run, RunEvent, Trace, User, utc_now)
from app.m1.services import password_hash
from app.orchestration.admin_observability import aggregate_admin_overview, admin_run_detail, list_admin_runs
from scripts.g2_test_database import resolve_test_database_url

EXPECTED_REVISION = "20261023_16"


def _configuration_source() -> str:
    """Return source metadata only; never render or log the credential-bearing URL."""
    if os.environ.get("DATABASE_URL", "").strip():
        return "process_environment:DATABASE_URL"
    local_values = parse_env_file(LOCAL_CONFIG.read_text(encoding="utf-8")) if LOCAL_CONFIG.exists() else {}
    if local_values.get("DATABASE_URL", "").strip():
        return "project_local_config:DATABASE_URL"
    settings = load_settings(require_api_key=False)
    return f"M0_settings:{settings.database_url_source or 'unset'}"


def _login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf")
    if csrf.status_code != 200:
        raise RuntimeError("CSRF bootstrap failed")
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf.json()["csrf_token"]},
                           json={"username": user_id, "password": password})
    if response.status_code != 200 or response.json().get("user", {}).get("user_id") != user_id:
        raise RuntimeError("temporary probe account login failed")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf", "")}


def _run(run_id: str, user_id: str, session_id: str, now: datetime, *, status: str,
         strategy: str | None = None, retrieval: dict | None = None, error_code: str | None = None,
         citations: list | None = None, handoffs: list | None = None) -> Run:
    result = {"route": {"strategy": strategy} if strategy else {},
              "retrieval": retrieval or {}, "citations": citations or [], "handoffs": handoffs or []}
    return Run(run_id=run_id, user_id=user_id, session_id=session_id,
        client_request_id=run_id, idempotency_scope="m53a-probe", request_hash="a" * 64,
        status=status, error_code=error_code, result_json=json.dumps(result), model_id="test-stub",
        usage_json=json.dumps({"prompt_tokens": 100, "completion_tokens": 20,
                               "cost_estimate_usd_peak_rate": 0.001}),
        latency_ms=42, started_at=now - timedelta(milliseconds=42), finished_at=now,
        created_at=now, updated_at=now)


def main() -> None:
    marker = "m53a-" + uuid.uuid4().hex[:12]
    run_ids = [f"{marker}-bm25", f"{marker}-hybrid", f"{marker}-unknown", f"{marker}-failed"]
    session_ids = [f"{marker}-admin-session", f"{marker}-user-session"]
    action_id = f"{marker}-action"
    admin_id, normal_id = f"{marker}-admin", f"{marker}-user"
    admin_password, normal_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(microseconds=1), now + timedelta(seconds=2)
    probe: dict = {"probe_id": marker, "status": "STARTED", "database": "shoprelay_m11_test",
                   "config_source": _configuration_source(), "created": {}, "cleanup": {},
                   "runtime": {"python": sys.executable, "working_directory": str(Path.cwd()),
                               "project_vendor_import_path": str(ROOT / ".vendor")},
                   "business_writes": 0, "deepseek_requests": 0, "mcp_writes": 0}
    url: URL | None = None
    engine = None
    connected = False
    created = False
    try:
        url = resolve_test_database_url()
        probe["connection_target"] = {"host": url.host, "port": url.port, "database": url.database,
                                      "username": url.username, "driver": url.drivername}
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("database resolver did not select the isolated test database")
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        with engine.connect() as connection:
            connected = True
            probe["postgres_version"] = connection.execute(text("SELECT current_setting('server_version')")).scalar_one()
            probe["database_verified"] = connection.execute(text("SELECT current_database()" )).scalar_one() == "shoprelay_m11_test"
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            probe["alembic_revision"] = revision
            if revision != EXPECTED_REVISION:
                raise RuntimeError("unexpected Alembic revision")
            checkpointer = connection.execute(text("""
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema='shoprelay_langgraph'
                  AND table_name IN ('checkpoints','checkpoint_blobs','checkpoint_writes')
            """)).scalar_one()
            probe["checkpointer_table_count"] = int(checkpointer)

        factory = make_session_factory(engine)
        preexisting_overview = aggregate_admin_overview(
            factory, now - timedelta(days=31), now, limit=5000)
        preexisting_model = preexisting_overview["model"]
        probe["preexisting_persisted_window"] = {
            "window_days": 31,
            "run_count": preexisting_overview["run_counts"]["total"],
            "status_counts": preexisting_overview["run_counts"]["by_status"],
            "terminal_denominator": preexisting_overview["run_counts"]["terminal_denominator"],
            "provider_usage_run_count": preexisting_model["provider_usage_run_count"],
            "usage_coverage_denominator": preexisting_model["usage_coverage_denominator"],
            "prompt_tokens_from_persisted_usage": preexisting_model["prompt_tokens"],
            "completion_tokens_from_persisted_usage": preexisting_model["completion_tokens"],
            "estimated_cost_usd": preexisting_model["estimated_cost_usd"],
            "estimated_cost_sample_count": preexisting_model["estimated_cost_sample_count"],
            "stub_run_count": preexisting_model["stub_run_count"],
            "latency_ms": preexisting_overview["latency_ms"],
            "retry_and_replay": "not persisted; returned as null"}
        # Synthetic test identities avoid changing any existing user's credentials.
        with factory.begin() as db:
            db.add_all([User(user_id=admin_id, display_name="M5.3 probe admin"),
                        User(user_id=normal_id, display_name="M5.3 probe user")])
            db.flush()
            db.add_all([DemoCredential(user_id=admin_id, password_hash=password_hash(admin_password), active=True,
                                       updated_at=utc_now()),
                        DemoCredential(user_id=normal_id, password_hash=password_hash(normal_password), active=True,
                                       updated_at=utc_now())])
            db.flush()
            db.add_all([ConversationSession(session_id=session_ids[0], user_id=admin_id, created_at=now,
                                            title=None, updated_at=now),
                        ConversationSession(session_id=session_ids[1], user_id=normal_id, created_at=now,
                                            title=None, updated_at=now)])
            db.flush()
            db.add_all([
                _run(run_ids[0], admin_id, session_ids[0], now, status="SUCCEEDED", strategy="AGENT",
                     retrieval={"strategy_selected": "BM25", "strategy_executed": "BM25", "degraded": False},
                     citations=[{"chunk_id": "synthetic-policy-chunk"}],
                     handoffs=[{"handoff_id": marker + "-handoff", "target_agent": "policy"}]),
                _run(run_ids[1], admin_id, session_ids[0], now, status="SUCCEEDED", strategy="SINGLE",
                     retrieval={"strategy_selected": "HYBRID_RERANK", "strategy_executed": "HYBRID_RERANK", "degraded": False}),
                _run(run_ids[2], admin_id, session_ids[0], now, status="UNKNOWN_RESULT", strategy="AGENT",
                     retrieval={"strategy_selected": "HYBRID_RERANK", "strategy_executed": "BM25", "degraded": True,
                                "degraded_reason": "EMBEDDING_UNAVAILABLE"}, error_code="CHECKPOINT_PERSISTENCE_FAILED"),
                _run(run_ids[3], admin_id, session_ids[0], now, status="MODEL_UNAVAILABLE", strategy="DIRECT",
                     error_code="MODEL_UNAVAILABLE")])
            db.flush()
            db.add(GraphExecutionRecord(run_id=run_ids[0], user_id=admin_id, thread_id=marker + "-thread",
                orchestrator="langgraph", graph_version="m53-probe", state_schema_version="probe-v1",
                execution_version="probe-v1", checkpoint_id=None, status="SUCCEEDED",
                started_at=now - timedelta(milliseconds=42), updated_at=now, finished_at=now))
            db.flush()
            db.add(PendingAction(action_id=action_id, owner_id=admin_id, session_id=session_ids[0],
                run_id=run_ids[0], trace_id=f"{run_ids[0]}-trace", tool_id="create_return_request",
                target_resource_id="synthetic-resource", sanitized_arguments_json="{}", arguments_hash="b" * 64,
                action_version=1, risk_level="WRITE_HIGH_RISK", status="PENDING_CONFIRMATION",
                idempotency_key=action_id, created_at=now, expires_at=now + timedelta(minutes=10),
                audit_reference=action_id))
            db.flush()
            db.add(ActionAudit(audit_id=f"{action_id}-audit", action_id=action_id, owner_id=admin_id,
                event_type="ACTION_CREATED_PENDING_CONFIRMATION", from_status=None,
                to_status="PENDING_CONFIRMATION", occurred_at=now, trace_id=f"{run_ids[0]}-trace",
                transaction_reference=None, before_summary_json=None,
                after_summary_json=json.dumps({"source": "SYNTHETIC_PROBE", "status": "PENDING_CONFIRMATION"}),
                error_code=None))
            event_payloads = [
                (run_ids[0], [
                    ("started", {}), ("route_decided", {"strategy": "AGENT", "decision_source": "SYNTHETIC_PROBE"}),
                    ("agent_started", {"agent": "policy", "handoff_id": marker + "-handoff"}),
                    ("tool_started", {"tool": "search_knowledge", "tool_id": "search_knowledge"}),
                    ("tool_finished", {"tool": "search_knowledge", "tool_id": "search_knowledge", "status": "SUCCEEDED", "duration_ms": 3}),
                    ("retrieval_hop", {"hop": 1, "strategy_selected": "BM25", "strategy_executed": "BM25",
                                        "evidence_count": 1, "knowledge_version": "synthetic-probe", "duration_ms": 4}),
                    ("done", {"status": "SUCCEEDED"})]),
                (run_ids[1], [("route_decided", {"strategy": "SINGLE"}),
                              ("retrieval_hop", {"hop": 1, "strategy_selected": "HYBRID_RERANK",
                                                  "strategy_executed": "HYBRID_RERANK", "evidence_count": 2,
                                                  "knowledge_version": "synthetic-probe", "duration_ms": 19})]),
                (run_ids[2], [("route_decided", {"strategy": "AGENT"}),
                              ("tool_started", {"tool_id": "vector_search", "tool": "vector_search"}),
                              ("tool_finished", {"tool_id": "vector_search", "tool": "vector_search",
                                                  "status": "FAILED", "error_code": "SIMULATED_TOOL_FAILURE",
                                                  "duration_ms": 8}),
                              ("retrieval_hop", {"hop": 1, "strategy_selected": "HYBRID_RERANK",
                                                  "strategy_executed": "BM25", "degraded": True,
                                                  "degraded_reason": "EMBEDDING_UNAVAILABLE"}),
                              ("error", {"status": "UNKNOWN_RESULT", "error_code": "CHECKPOINT_PERSISTENCE_FAILED"})]),
                (run_ids[3], [("error", {"status": "MODEL_UNAVAILABLE", "error_code": "MODEL_UNAVAILABLE"})])]
            for run_id, events in event_payloads:
                for seq, (kind, payload) in enumerate(events, 1):
                    db.add(RunEvent(event_id=f"{run_id}-event-{seq}", run_id=run_id,
                        session_id=session_ids[0], trace_id=f"{run_id}-trace", seq=seq,
                        timestamp=now + timedelta(microseconds=seq), event_type=kind,
                        payload_json=json.dumps(payload), event_key=f"{run_id}:{seq}"))
            span_items = [
                (run_ids[0], "supervisor", None, 1.0, "OK"),
                (run_ids[0], "m3.agent.policy", "supervisor", 5.0, "OK"),
                (run_ids[0], "tool.search_knowledge", "m3.agent.policy", 3.0, "OK"),
                (run_ids[1], "m2.retrieval.hybrid_rerank", None, 19.0, "OK"),
                (run_ids[2], "m2.retrieval.hybrid_rerank", None, 8.0, "ERROR")]
            for owner_run, node, parent, duration, status in span_items:
                span_id = f"{owner_run}-{node.replace('.', '-')}-span"
                payload = {"trace_id": f"{owner_run}-trace", "span_id": span_id,
                    "parent_span_id": f"{owner_run}-{parent.replace('.', '-')}-span" if parent else None,
                    "run_id": owner_run, "node_name": node, "strategy": "AGENT" if "agent" in node else None,
                    "start_time": now.isoformat(), "end_time": now.isoformat(), "duration_ms": duration,
                    "status": status, "error_code": "EMBEDDING_UNAVAILABLE" if status == "ERROR" else None,
                    "graph_version": "m53-probe", "prompt": "SENTINEL_PRIVATE_PROMPT", "api_key": "SENTINEL_SECRET"}
                db.add(ObservabilityRecord(record_id=f"span:{span_id}", run_id=owner_run, user_id=admin_id,
                    trace_id=f"{owner_run}-trace", span_id=span_id, parent_span_id=None,
                    graph_version="m53-probe", record_type="span", timestamp=now, payload_json=json.dumps(payload)))
            db.add(ObservabilityRecord(record_id=f"metric:{marker}", run_id=run_ids[0], user_id=admin_id,
                trace_id=f"{run_ids[0]}-trace", span_id=None, parent_span_id=None, graph_version="m53-probe",
                record_type="metric", timestamp=now, payload_json=json.dumps({"node_execution_count": 1,
                    "node_error_count": 0, "node_duration_ms": [3], "model_request_count": 1,
                    "prompt_tokens": 100, "completion_tokens": 20, "estimated_cost_usd": 0.001,
                    "retrieval_hop_count": 1, "node_name": "test.stub"})))
            for run_id, event_type, payload in [
                (run_ids[0], "policy_retrieval_completed", {"strategy_selected": "BM25", "strategy_executed": "BM25",
                    "degraded": False, "knowledge_version": "synthetic-probe", "evidence_ids": ["synthetic-policy-chunk"],
                    "prompt": "SENTINEL_PRIVATE_PROMPT"}),
                (run_ids[1], "policy_retrieval_completed", {"strategy_selected": "HYBRID_RERANK",
                    "strategy_executed": "HYBRID_RERANK", "degraded": False, "knowledge_version": "synthetic-probe",
                    "evidence_ids": ["synthetic-hybrid-chunk"]})]:
                db.add(Trace(trace_id=f"{run_id}-trace-meta", user_id=admin_id, run_id=run_id,
                    event_type=event_type, status="COMPLETED", created_at=now, event_json=json.dumps(payload)))
        created = True
        probe["created"] = {"users": 2, "credentials": 2, "sessions": 2, "runs": len(run_ids),
            "graph_executions": 1, "events": sum(len(items) for _, items in event_payloads),
            "spans": len(span_items), "metrics": 1, "trace_records": 2, "pending_actions": 1, "action_audits": 1}
        engine.dispose()

        # New engine proves the records are PostgreSQL-persisted, not process memory.
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        query_timing = {}
        for label, fn in [("aggregate", lambda: aggregate_admin_overview(factory, start, end, limit=100)),
                          ("run_detail", lambda: admin_run_detail(factory, run_ids[0])),
                          ("list_page", lambda: list_admin_runs(factory, start, end, limit=1, strategy="AGENT"))]:
            t0 = time.perf_counter()
            value = fn()
            query_timing[label] = round((time.perf_counter() - t0) * 1000, 3)
            if value is None:
                raise RuntimeError(f"persistence query failed: {label}")
        overview = aggregate_admin_overview(factory, start, end, limit=100)
        if overview["run_counts"]["total"] != 4 or overview["run_counts"]["unknown_result_count"] != 1:
            raise RuntimeError("persisted run status aggregation mismatch")
        if overview["run_counts"]["terminal_denominator"] != 4 or overview["run_counts"]["success_rate"] != 0.5:
            raise RuntimeError("UNKNOWN_RESULT or failure denominator was aggregated incorrectly")
        if overview["latency_ms"]["sample_count"] != 4 or overview["latency_ms"]["p50"] != 42 or overview["latency_ms"]["p95"] != 42:
            raise RuntimeError("persisted latency percentile aggregation mismatch")
        if overview["retrieval"]["bm25_count"] != 2 or overview["retrieval"]["hybrid_rerank_count"] != 1:
            raise RuntimeError("retrieval strategy aggregation mismatch")
        if overview["retrieval"]["degraded_run_count"] != 1:
            raise RuntimeError("retrieval degradation aggregation mismatch")
        if overview["agent_execution"]["actual_agent_started_count"] != 1 or overview["agent_execution"]["handoff_count"] != 1:
            raise RuntimeError("Agent/Handoff aggregation mismatch")
        if overview["tools"]["started_count"] != 2 or overview["tools"]["failed_count"] != 1:
            raise RuntimeError("tool aggregation mismatch")
        if overview["model"]["stub_run_count"] != 4 or overview["model"]["provider_usage_run_count"] != 0:
            raise RuntimeError("Stub was not separated from Provider usage")
        if overview["events"]["persisted_sse_event_count"] != sum(len(items) for _, items in event_payloads):
            raise RuntimeError("persisted SSE event count mismatch")
        if overview["controlled_action_audit_events"].get("ACTION_CREATED_PENDING_CONFIRMATION") != 1:
            raise RuntimeError("ActionAudit aggregation mismatch")
        detail = admin_run_detail(factory, run_ids[0])
        if not detail or detail["run"]["status"] != "SUCCEEDED" or detail["graph_execution"]["status"] != "SUCCEEDED":
            raise RuntimeError("persisted Run/GraphExecution detail was not recovered")
        serialized_detail = json.dumps(detail)
        if any(secret_marker in serialized_detail for secret_marker in ("SENTINEL_PRIVATE_PROMPT", "SENTINEL_SECRET")):
            raise RuntimeError("sensitive sentinel leaked through admin detail projection")
        if "b" * 64 in serialized_detail:
            raise RuntimeError("Pending Action argument hash leaked through admin detail projection")
        if detail["events"]["items"][-1]["type"] != "done" or len(detail["spans"]["items"]) != 3:
            raise RuntimeError("persisted event/Span detail was incomplete")
        probe["postgres_projection"] = {"run_total": overview["run_counts"]["total"],
            "statuses": overview["run_counts"]["by_status"], "success_rate": overview["run_counts"]["success_rate"],
            "latency_sample_count": overview["latency_ms"]["sample_count"],
            "latency_p50_ms": overview["latency_ms"]["p50"], "latency_p95_ms": overview["latency_ms"]["p95"],
            "unknown_result_count": overview["run_counts"]["unknown_result_count"],
            "agent_started": overview["agent_execution"]["actual_agent_started_count"],
            "handoffs": overview["agent_execution"]["handoff_count"], "tool_started": overview["tools"]["started_count"],
            "tool_failed": overview["tools"]["failed_count"],
            "action_audit_events": overview["controlled_action_audit_events"],
            "sse_events": overview["events"]["persisted_sse_event_count"],
            "stub_runs": overview["model"]["stub_run_count"],
            "provider_usage_runs": overview["model"]["provider_usage_run_count"],
            "bm25_count": overview["retrieval"]["bm25_count"],
            "hybrid_count": overview["retrieval"]["hybrid_rerank_count"],
            "degraded_count": overview["retrieval"]["degraded_run_count"],
            "estimated_cost_semantics": overview["model"]["cost_semantics"]}
        probe["query_performance_ms"] = {**query_timing, "sample_runs": 4,
            "note": "single probe execution; descriptive only, not an SLO"}

        # Exercise authentication and allowlist authorization through real FastAPI
        # login cookies, while keeping all identities temporary and probe-owned.
        app = create_app(database_url=database_url, engine=engine,
                         observability_admin_user_ids=(admin_id,))
        api_query = f"from={start.isoformat().replace('+00:00', 'Z')}&to={end.isoformat().replace('+00:00', 'Z')}"
        with TestClient(app) as anonymous:
            response = anonymous.get(f"/api/admin/observability/overview?{api_query}")
            if response.status_code != 401:
                raise RuntimeError("anonymous admin API request was not rejected")
        with TestClient(app) as admin_client:
            admin_headers = _login(admin_client, admin_id, admin_password)
            t0 = time.perf_counter()
            response = admin_client.get(f"/api/admin/observability/overview?{api_query}", headers=admin_headers)
            query_timing["admin_overview_http"] = round((time.perf_counter() - t0) * 1000, 3)
            if response.status_code != 200 or response.json()["overview"]["run_counts"]["total"] != 4:
                raise RuntimeError("allowlisted admin API did not return the persisted aggregate")
            page = admin_client.get(f"/api/admin/observability/runs?{api_query}&strategy=AGENT&limit=1",
                                    headers=admin_headers)
            if page.status_code != 200 or page.json()["runs"]["limit"] != 1 or not page.json()["runs"]["has_more"]:
                raise RuntimeError("admin Run pagination/filter failed")
            tool_filtered = admin_client.get(f"/api/admin/observability/runs?{api_query}&tool_id=vector_search&limit=10",
                                             headers=admin_headers)
            if tool_filtered.status_code != 200 or [item["run_id"] for item in tool_filtered.json()["runs"]["items"]] != [run_ids[2]]:
                raise RuntimeError("admin Tool filter did not locate the expected persisted Run")
            hybrid = admin_client.get(f"/api/admin/observability/runs?{api_query}&strategy=SINGLE&limit=10",
                                      headers=admin_headers)
            if hybrid.status_code != 200 or not any(item["run_id"] == run_ids[1]
                                                    for item in hybrid.json()["runs"]["items"]):
                raise RuntimeError("admin strategy filter did not locate the persisted Hybrid run")
            detail_response = admin_client.get(f"/api/admin/observability/runs/{run_ids[0]}", headers=admin_headers)
            if detail_response.status_code != 200 or "SENTINEL_PRIVATE_PROMPT" in detail_response.text or "SENTINEL_SECRET" in detail_response.text or "b" * 64 in detail_response.text:
                raise RuntimeError("admin HTTP detail failed or exposed a sensitive sentinel")
            metrics_compat = admin_client.get(f"/api/admin/observability/metrics?{api_query}", headers=admin_headers)
            if metrics_compat.status_code != 200 or not {"metrics", "overview"}.issubset(metrics_compat.json()):
                raise RuntimeError("backward-compatible metrics endpoint did not include both payloads")
            if admin_client.get(f"/api/admin/observability/runs?{api_query}&limit=101",
                                headers=admin_headers).status_code != 422:
                raise RuntimeError("Run page maximum was not enforced")
        with TestClient(app) as normal_client:
            normal_headers = _login(normal_client, normal_id, normal_password)
            if normal_client.get(f"/api/admin/observability/overview?{api_query}", headers=normal_headers).status_code != 403:
                raise RuntimeError("authenticated non-admin was not rejected")
        probe["authorization"] = {"anonymous": 401, "allowlisted_admin": 200,
            "ordinary_user": 403, "admin_identity_source": "temporary credential-authenticated probe account",
            "pagination_limit_101": 422, "tool_filter": 200, "metrics_compatibility": 200,
            "ordinary_run_list_filter": "one matching synthetic Run"}
        probe["query_performance_ms"] = query_timing
        probe["status"] = "PASS"
    except Exception as exc:
        probe["status"] = "BLOCKED_DATABASE_AUTH" if not connected else "FAIL_POSTGRES_PROBE"
        probe["failure_stage"] = "database_connect" if not connected else "postgres_probe"
        probe["failure_type"] = type(exc).__name__
        original = getattr(exc, "orig", None)
        diag = getattr(original, "diag", None)
        if getattr(original, "sqlstate", None):
            probe["database_error_code"] = original.sqlstate
        if getattr(diag, "constraint_name", None):
            probe["database_constraint"] = diag.constraint_name
        # Avoid echoing driver strings that might contain credential-bearing DSNs.
        probe["failure_detail"] = str(exc)[:240] if not any(token in str(exc).casefold() for token in ("password", "dsn", "postgresql://")) else "Failure detail redacted."
    finally:
        if connected and engine is not None:
            try:
                if created:
                    with engine.begin() as connection:
                        connection.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
                        connection.execute(delete(Run).where(Run.run_id.in_(run_ids)))
                        connection.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_ids)))
                        connection.execute(delete(User).where(User.user_id.in_([admin_id, normal_id])))
                with engine.connect() as connection:
                    probe["cleanup"] = {
                        "runs": int(connection.execute(text("SELECT count(*) FROM runs WHERE run_id = ANY(:ids)"), {"ids": run_ids}).scalar_one()),
                        "sessions": int(connection.execute(text("SELECT count(*) FROM sessions WHERE session_id = ANY(:ids)"), {"ids": session_ids}).scalar_one()),
                        "users": int(connection.execute(text("SELECT count(*) FROM users WHERE user_id = ANY(:ids)"), {"ids": [admin_id, normal_id]}).scalar_one()),
                        "credentials": int(connection.execute(text("SELECT count(*) FROM demo_credentials WHERE user_id = ANY(:ids)"), {"ids": [admin_id, normal_id]}).scalar_one()),
                        "run_events": int(connection.execute(text("SELECT count(*) FROM run_events WHERE run_id = ANY(:ids)"), {"ids": run_ids}).scalar_one()),
                        "observability_records": int(connection.execute(text("SELECT count(*) FROM observability_records WHERE run_id = ANY(:ids)"), {"ids": run_ids}).scalar_one()),
                        "traces": int(connection.execute(text("SELECT count(*) FROM traces WHERE run_id = ANY(:ids)"), {"ids": run_ids}).scalar_one()),
                        "graph_executions": int(connection.execute(text("SELECT count(*) FROM graph_executions WHERE run_id = ANY(:ids)"), {"ids": run_ids}).scalar_one()),
                        "pending_actions": int(connection.execute(text("SELECT count(*) FROM controlled_pending_actions WHERE action_id = :id"), {"id": action_id}).scalar_one()),
                        "action_audits": int(connection.execute(text("SELECT count(*) FROM action_audit WHERE action_id = :id"), {"id": action_id}).scalar_one()),
                        "auth_sessions": int(connection.execute(text("SELECT count(*) FROM auth_sessions WHERE user_id = ANY(:ids)"), {"ids": [admin_id, normal_id]}).scalar_one())}
                if any(probe["cleanup"].values()):
                    probe["status"] = "CLEANUP_FAILED"
            except Exception as exc:
                probe["cleanup_error_type"] = type(exc).__name__
                probe["status"] = "CLEANUP_FAILED"
        elif not connected:
            probe["cleanup"] = {"attempted": False, "reason": "database connection failed before any insert"}
        probe["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(probe, ensure_ascii=False, sort_keys=True))
        if engine is not None:
            engine.dispose()
    if probe["status"] != "PASS":
        sys.exit(2)


if __name__ == "__main__":
    main()
