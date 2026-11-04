from __future__ import annotations

"""M5.1 PostgreSQL acceptance probe; only touches shoprelay_m11_test and its tagged rows."""
import asyncio
import json
import os
import secrets
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential, Message,
                           GraphExecutionRecord, PendingAction, Run, RunEvent, SessionMemorySummary,
                           Trace, User, utc_now)
from app.m1.services import claim_run, create_run, finish_run, set_demo_password
from app.m5.memory import (MemoryBudget, persist_auto_summary, prepare_memory_context,
                           read_session_summary)
from scripts.g2_test_database import resolve_test_database_url


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf")
    token = csrf.json()["csrf_token"]
    response = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
        CSRF_HEADER: token}, json={"username": user_id, "password": password})
    if response.status_code != 200:
        raise RuntimeError("PROBE_LOGIN_FAILED")
    return {"Origin": str(client.base_url).rstrip("/"),
            CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}


def _budget() -> MemoryBudget:
    return MemoryBudget(model_window_tokens=3524, output_reserve_tokens=512,
        safety_margin_tokens=512, base_prompt_reserve_tokens=0, recent_message_count=6)


def _add_messages(factory, session_id: str, start: int, stop: int, marker: str) -> int:
    now = utc_now()
    with factory() as db:
        for sequence in range(start, stop + 1):
            if sequence > stop - 6:
                content = f"{marker} 合成对话上下文 " + "x" * 14000
            else:
                content = f"{marker} 合成的一般历史背景 " + "x" * 700
            db.add(Message(message_id=f"{session_id[:24]}-{sequence:04d}", session_id=session_id,
                role="user" if sequence % 2 else "assistant", content=content,
                created_at=now, sequence=sequence, status="RECORDED"))
        db.commit()
    return stop - start + 1


def _create_successful_run(factory, owner_id: str, session_id: str, client_request_id: str) -> Run:
    with factory() as db:
        run, same = create_run(db, owner_id, session_id, client_request_id,
                               {"message": "synthetic follow-up"})
        if run is None or not same or not claim_run(db, run, execution_owner_id="m5.1-probe"):
            raise RuntimeError("PROBE_RUN_CLAIM_FAILED")
        finish_run(db, run, status="SUCCEEDED", result={"answer": "ok", "citations": []},
                   trace_id=str(uuid.uuid4()))
        db.refresh(run)
        return run


def _delete_checkpoint(database_url: str, schema: str, run_id: str) -> None:
    from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async

    async def delete() -> None:
        async with open_checkpointer(database_url, schema) as saver:
            await saver.adelete_thread(run_id)

    run_checkpointer_async(delete())


def _checkpoint_values(database_url: str, schema: str, run_id: str) -> dict:
    from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async

    async def read() -> dict:
        async with open_checkpointer(database_url, schema) as saver:
            snapshot = await saver.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})
            return dict(snapshot.checkpoint or {}) if snapshot is not None else {}

    return run_checkpointer_async(read())


def _counts(engine, user_ids: tuple[str, ...], session_ids: tuple[str, ...]) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "probe_users": conn.execute(text("select count(*) from users where user_id = any(:ids)"),
                                        {"ids": list(user_ids)}).scalar_one(),
            "probe_sessions": conn.execute(text("select count(*) from sessions where session_id = any(:ids)"),
                                           {"ids": list(session_ids)}).scalar_one(),
            "probe_messages": conn.execute(text("select count(*) from messages where session_id = any(:ids)"),
                                           {"ids": list(session_ids)}).scalar_one(),
            "probe_runs": conn.execute(text("select count(*) from runs where session_id = any(:ids)"),
                                       {"ids": list(session_ids)}).scalar_one(),
            "probe_run_events": conn.execute(text("select count(*) from run_events where session_id = any(:ids)"),
                                              {"ids": list(session_ids)}).scalar_one(),
            "probe_graph_executions": conn.execute(text("select count(*) from graph_executions where run_id in (select run_id from runs where session_id = any(:ids))"),
                                                    {"ids": list(session_ids)}).scalar_one(),
            "probe_observability": conn.execute(text("select count(*) from observability_records where run_id in (select run_id from runs where session_id = any(:ids))"),
                                                {"ids": list(session_ids)}).scalar_one(),
            "probe_summaries": conn.execute(text("select count(*) from session_memory_summaries where session_id = any(:ids)"),
                                             {"ids": list(session_ids)}).scalar_one(),
            "probe_pending_actions": conn.execute(text("select count(*) from controlled_pending_actions where session_id = any(:ids)"),
                                                   {"ids": list(session_ids)}).scalar_one(),
            "probe_audits": conn.execute(text("select count(*) from action_audit where owner_id = any(:ids)"),
                                          {"ids": list(user_ids)}).scalar_one(),
            "probe_traces": conn.execute(text("select count(*) from traces where user_id = any(:ids)"),
                                          {"ids": list(user_ids)}).scalar_one(),
        }


def _contains_key(value, name: str) -> bool:
    if isinstance(value, dict):
        return name in value or any(_contains_key(child, name) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, name) for child in value)
    return False


def probe() -> dict:
    marker = "M51Q1-" + secrets.token_hex(7).upper()
    owner_id, other_id = marker.lower() + "-owner", marker.lower() + "-other"
    session_ids: list[str] = []
    run_ids: list[str] = []
    checkpoint_ids: list[str] = []
    engine = factory = None
    app = graph_app = recovery_app = None
    db_url = None
    preexisting_counts = None
    result: dict = {"schema_version": "shoprelay.m5.1-postgres.v1", "probe_id": marker,
        "started_at": _utc(), "status": "RUNNING", "database_expected": "shoprelay_m11_test",
        "provider_calls": 0, "summary_model_calls": 0, "business_write_operations": 0,
        "container_before": "exited; existing container started for this probe",
        "cleanup": {"completed": False}}
    phase = "configuration"
    passwords = {owner_id: secrets.token_urlsafe(32), other_id: secrets.token_urlsafe(32)}
    original_env = {key: os.environ.get(key) for key in
        ("SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED", "SHOPRELAY_GRAPH_VERSION",
         "SHOPRELAY_EXECUTION_VERSION", "SHOPRELAY_CONTEXT_WINDOW_TOKENS")}
    injected_handler = None
    try:
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        db_url = url.render_as_string(hide_password=False)
        settings = load_settings(require_api_key=False)
        result["database"] = url.database
        result["database_host"] = url.host
        result["database_url_secret_logged"] = False
        engine = make_engine(db_url)
        factory = make_session_factory(engine)
        with engine.connect() as conn:
            result["postgres_version"] = conn.execute(text("show server_version")).scalar_one()
            result["database_actual"] = conn.execute(text("select current_database()")).scalar_one()
            if result["database_actual"] != "shoprelay_m11_test":
                raise RuntimeError("UNSAFE_DATABASE_TARGET")
        before_rev = current_revision(db_url)
        upgrade_database(db_url)
        first_rev = current_revision(db_url)
        upgrade_database(db_url)
        second_rev = current_revision(db_url)
        result["migration"] = {"before_probe": before_rev, "first_upgrade_result": first_rev,
            "repeat_upgrade_result": second_rev, "idempotent": first_rev == second_rev == "20261023_16",
            "applied_transition_this_run": before_rev != first_rev}
        if second_rev != "20261023_16":
            raise RuntimeError("MIGRATION_REVISION_MISMATCH")
        inspector = inspect(engine)
        if "session_memory_summaries" not in inspector.get_table_names():
            raise RuntimeError("SUMMARY_TABLE_MISSING")
        unique = inspector.get_unique_constraints("session_memory_summaries")
        indexes = inspector.get_indexes("session_memory_summaries")
        result["schema"] = {"columns": [col["name"] for col in inspector.get_columns("session_memory_summaries")],
            "unique_constraints": unique, "indexes": indexes}
        constraint_names = {row.get("name") for row in unique}
        unique_columns = {tuple(row.get("column_names", [])) for row in unique}
        if ("session_id", "version") not in unique_columns:
            raise RuntimeError("SUMMARY_VERSION_UNIQUE_CONSTRAINT_MISSING")
        if not any("source_run_id" in columns for columns in unique_columns):
            raise RuntimeError("SUMMARY_SOURCE_RUN_UNIQUE_CONSTRAINT_MISSING")
        if not any(item.get("name") == "ix_session_memory_owner_session_version" for item in indexes):
            raise RuntimeError("SUMMARY_OWNER_SESSION_INDEX_MISSING")
        from app.orchestration.checkpointer import checkpoint_schema_ready, run_checkpointer_async
        result["checkpointer"] = {"schema": settings.checkpoint_schema,
            "ready": bool(run_checkpointer_async(checkpoint_schema_ready(db_url, settings.checkpoint_schema)))}
        if not result["checkpointer"]["ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        tracked_tables = ("sessions", "messages", "runs", "run_events", "traces", "graph_executions",
                          "observability_records", "controlled_pending_actions", "action_audit")
        with engine.connect() as conn:
            preexisting_counts = {name: conn.execute(text(f"select count(*) from {name}")).scalar_one()
                                  for name in tracked_tables}
            result["preexisting_row_counts"] = preexisting_counts
        phase = "temporary_probe_accounts"
        with factory() as db:
            db.add_all([User(user_id=owner_id, display_name="M5.1 isolated probe owner"),
                        User(user_id=other_id, display_name="M5.1 isolated probe other")])
            db.commit()
            set_demo_password(db, owner_id, passwords[owner_id])
            set_demo_password(db, other_id, passwords[other_id])

        os.environ["SHOPRELAY_CONTEXT_WINDOW_TOKENS"] = "3524"  # 2,500 effective tokens, matching the approved auto probe.
        app = create_app(database_url=db_url, engine=engine, orchestrator_mode="legacy")
        phase = "api_session_and_summary_v1_v2"
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = _login(client, owner_id, passwords[owner_id])
            created = client.post("/api/sessions", headers=headers, json={"title": "M5.1 isolated memory probe"})
            if created.status_code != 200:
                raise RuntimeError("PROBE_SESSION_CREATE_FAILED")
            session_id = created.json()["session"]["session_id"]
            session_ids.append(session_id)
            _add_messages(factory, session_id, 1, 80, marker + "-OLD-PRIVATE")
            response1 = client.post("/api/chat", headers=headers, json={"session_id": session_id,
                "message": "你好", "client_request_id": marker + "-run-1"})
            result["api_run_1"] = {"http_status": response1.status_code,
                "run_status": response1.json().get("status") if response1.headers.get("content-type", "").startswith("application/json") else None,
                "context_memory": response1.json().get("context_memory") if response1.headers.get("content-type", "").startswith("application/json") else None}
            if response1.status_code != 200 or response1.json().get("status") != "SUCCEEDED":
                raise RuntimeError("API_AUTO_COMPACT_RUN_1_FAILED")
            run1 = response1.json()["run_id"]
            run_ids.append(run1)
            with factory() as db:
                summary1 = db.execute(select(SessionMemorySummary).where(
                    SessionMemorySummary.session_id == session_id, SessionMemorySummary.is_clear_marker.is_(False)
                ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one()
                trace1 = db.execute(select(Trace).where(Trace.run_id == run1,
                    Trace.event_type == "session_memory_context").order_by(Trace.created_at.desc()).limit(1)).scalar_one()
                trace_payload1 = json.loads(trace1.event_json)
                result["summary_v1"] = {"version": summary1.version, "source_run_id_matches": summary1.source_run_id == run1,
                    "source_through_sequence": summary1.source_through_sequence,
                    "source_message_count": len(json.loads(summary1.source_message_ids_json)),
                    "trigger": trace_payload1.get("level"), "trace_summary_persisted": trace_payload1.get("summary_persisted"),
                    "run_status": response1.json().get("status")}
                if summary1.version != 1 or summary1.source_run_id != run1 or summary1.is_clear_marker:
                    raise RuntimeError("SUMMARY_V1_INVALID")

            # Run 1 appends both the user question and assistant answer at 81-82.
            _add_messages(factory, session_id, 83, 155, marker + "-OLD-PRIVATE")
            response2 = client.post("/api/chat", headers=headers, json={"session_id": session_id,
                "message": "你好", "client_request_id": marker + "-run-2"})
            if response2.status_code != 200 or response2.json().get("status") != "SUCCEEDED":
                raise RuntimeError("API_AUTO_COMPACT_RUN_2_FAILED")
            run2 = response2.json()["run_id"]
            run_ids.append(run2)
            with factory() as db:
                summary2 = db.execute(select(SessionMemorySummary).where(
                    SessionMemorySummary.session_id == session_id, SessionMemorySummary.is_clear_marker.is_(False)
                ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one()
                trace2 = db.execute(select(Trace).where(Trace.run_id == run2,
                    Trace.event_type == "session_memory_context").order_by(Trace.created_at.desc()).limit(1)).scalar_one()
                trace_payload2 = json.loads(trace2.event_json)
                result["summary_v2"] = {"version": summary2.version, "source_run_id_matches": summary2.source_run_id == run2,
                    "source_through_sequence": summary2.source_through_sequence,
                    "source_message_count": len(json.loads(summary2.source_message_ids_json)),
                    "trigger": trace_payload2.get("level"), "trace_summary_persisted": trace_payload2.get("summary_persisted"),
                    "run_status": response2.json().get("status")}
                if summary2.version != 2 or summary2.source_run_id != run2:
                    raise RuntimeError("SUMMARY_VERSION_NOT_CONTIGUOUS")
                foreign_summary = read_session_summary(db, owner_id=other_id, session_id=session_id)
                result["summary_owner_isolation"] = {
                    "owner_can_read_version": read_session_summary(db, owner_id=owner_id,
                        session_id=session_id) is not None,
                    "other_owner_read_returns_none": foreign_summary is None,
                    "owner_session_version_unique": ("session_id", "version") in unique_columns,
                    "source_run_unique": any("source_run_id" in columns for columns in unique_columns)}
                if not result["summary_owner_isolation"]["owner_can_read_version"] or not result["summary_owner_isolation"]["other_owner_read_returns_none"]:
                    raise RuntimeError("SUMMARY_OWNER_ISOLATION_FAILED")
                pending = PendingAction(action_id=marker + "-action", owner_id=owner_id, session_id=session_id,
                    run_id=run2, trace_id=marker + "-trace", tool_id="create_return_request",
                    target_resource_id="SIMULATED_TEST_DATA", sanitized_arguments_json='{"probe":"redacted"}',
                    arguments_hash="a" * 64, action_version=1, risk_level="WRITE_HIGH_RISK",
                    status="PENDING_CONFIRMATION", idempotency_key=marker + "-action-key",
                    expires_at=utc_now(), audit_reference=marker + "-audit")
                db.add(pending)
                db.commit()
            replay = client.post("/api/chat", headers=headers, json={"session_id": session_id,
                "message": "你好", "client_request_id": marker + "-run-2"})
            with factory() as db:
                summary_count = db.query(SessionMemorySummary).filter_by(session_id=session_id,
                    is_clear_marker=False).count()
                action_after = db.get(PendingAction, marker + "-action")
                result["summary_idempotent_replay"] = {"same_run_id": replay.status_code == 200 and
                    replay.json().get("run_id") == run2, "summary_versions": summary_count,
                    "pending_action_status_unchanged": action_after.status == "PENDING_CONFIRMATION",
                    "pending_action_hash_unchanged": action_after.arguments_hash == "a" * 64}
                if not result["summary_idempotent_replay"]["same_run_id"] or summary_count != 2 or \
                   action_after.status != "PENDING_CONFIRMATION" or action_after.arguments_hash != "a" * 64:
                    raise RuntimeError("IDEMPOTENT_OR_PENDING_ACTION_CHECK_FAILED")

            other = TestClient(app, raise_server_exceptions=False)
            try:
                other_headers = _login(other, other_id, passwords[other_id])
                unauth = TestClient(app, raise_server_exceptions=False)
                try:
                    unauth_result = unauth.delete(f"/api/sessions/{session_id}/memory")
                finally:
                    unauth.close()
                missing_csrf = client.delete(f"/api/sessions/{session_id}/memory", headers={"Origin": str(client.base_url).rstrip("/")})
                foreign = other.delete(f"/api/sessions/{session_id}/memory", headers=other_headers)
                result["clear_api_guards"] = {"unauthenticated": unauth_result.status_code,
                    "missing_csrf": missing_csrf.status_code, "cross_user": foreign.status_code}
                if (unauth_result.status_code, missing_csrf.status_code, foreign.status_code) != (401, 403, 404):
                    raise RuntimeError("CLEAR_API_SECURITY_GUARD_FAILED")
                with factory() as db:
                    owner_summary_after_foreign = read_session_summary(db, owner_id=owner_id,
                        session_id=session_id)
                    foreign_summary_after_foreign = read_session_summary(db, owner_id=other_id,
                        session_id=session_id)
                    result["clear_api_guards"]["owner_summary_preserved"] = owner_summary_after_foreign is not None
                    result["clear_api_guards"]["foreign_summary_unreadable"] = foreign_summary_after_foreign is None
                if not result["clear_api_guards"]["owner_summary_preserved"] or not result["clear_api_guards"]["foreign_summary_unreadable"]:
                    raise RuntimeError("CROSS_USER_CLEAR_CHANGED_MEMORY")
            finally:
                other.close()

            # Inject a PostgreSQL INSERT error while the API has already deleted old summaries.
            phase = "clear_transaction_failure_rollback"
            def fail_summary_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
                if statement.lstrip().lower().startswith("insert into session_memory_summaries"):
                    raise RuntimeError("INJECTED_SUMMARY_INSERT_FAILURE")
            injected_handler = fail_summary_insert
            event.listen(engine, "before_cursor_execute", injected_handler)
            failed_clear = client.delete(f"/api/sessions/{session_id}/memory", headers=headers)
            event.remove(engine, "before_cursor_execute", injected_handler)
            injected_handler = None
            with factory() as db:
                retained = read_session_summary(db, owner_id=owner_id, session_id=session_id)
            result["clear_transaction_rollback"] = {"http_status": failed_clear.status_code,
                "previous_summary_retained": retained is not None and bool(retained.get("source_message_ids")),
                "clear_marker_count": 0}
            if failed_clear.status_code < 500 or retained is None:
                raise RuntimeError("CLEAR_FAILURE_DID_NOT_ROLLBACK")

        # A fresh LangGraph app writes a real PostgreSQL checkpoint while keeping memory runtime-only.
        phase = "langgraph_api_checkpoint_and_replay"
        app.state.engine.dispose(close=True)
        engine.dispose(close=True)
        engine = make_engine(db_url)
        factory = make_session_factory(engine)
        probe_graph_version = "m51q1-" + marker[-6:].lower()
        graph_env = {"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": probe_graph_version,
            "SHOPRELAY_EXECUTION_VERSION": "m5.1-q1", "SHOPRELAY_CONTEXT_WINDOW_TOKENS": "3524"}
        for key, value in graph_env.items():
            os.environ[key] = value
        from app.m1.routing import RouteDecision
        from app.orchestration.adapters import GraphServices
        from app.orchestration.checkpointer import open_checkpointer
        route = RouteDecision(strategy="DIRECT", safety_decision="ALLOW", reason_code="M5_1_PG_PROBE",
            decision_source="RULE").model_dump(mode="json")
        services = GraphServices(router=lambda _query: route,
            direct=lambda _state: {"answer": "合成问候处理完成", "answer_kind": "DIRECT"},
            single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
        graph_app = create_app(database_url=db_url, engine=engine, orchestrator_mode="langgraph",
            chat_provider=object(),  # GraphServices is deterministic; this prevents constructing a real provider.
            checkpointer_factory=open_checkpointer, graph_services=services)
        graph_run_id = None
        with TestClient(graph_app, raise_server_exceptions=False) as graph_client:
            graph_headers = _login(graph_client, owner_id, passwords[owner_id])
            graph_response = graph_client.post("/api/chat", headers=graph_headers, json={"session_id": session_id,
                "message": "你好", "client_request_id": marker + "-graph-run"})
            if graph_response.status_code != 200 or graph_response.json().get("status") != "SUCCEEDED":
                body = graph_response.json() if graph_response.headers.get("content-type", "").startswith("application/json") else {}
                detail = body.get("detail") if isinstance(body, dict) else None
                detail_data = body.get("details") if isinstance(body, dict) else None
                if not isinstance(detail, dict) and isinstance(detail_data, dict):
                    detail = detail_data
                result["langgraph_api_failure"] = {"http_status": graph_response.status_code,
                    "response_top_level_keys": sorted(body.keys()) if isinstance(body, dict) else [],
                    "detail_type": type(body.get("detail")).__name__ if isinstance(body, dict) else None,
                    "detail_keys": sorted(detail.keys()) if isinstance(detail, dict) else [],
                    "code": (body.get("code") if isinstance(body, dict) else None) or
                            (detail.get("code") if isinstance(detail, dict) else None),
                    "run_status": ((body.get("details") or {}).get("status") if isinstance(body, dict)
                                   else None) or ((detail.get("details") or {}).get("status") if isinstance(detail, dict) else None)}
                failed_run_id = ((body.get("details") or {}).get("run_id") if isinstance(body, dict) else None)
                if failed_run_id:
                    run_ids.append(failed_run_id)
                    with factory() as db:
                        failed_run = db.get(Run, failed_run_id)
                        error_detail = json.loads(failed_run.error_detail_json or "{}") if failed_run else {}
                        result["langgraph_api_failure"]["persisted_run_error_code"] = failed_run.error_code if failed_run else None
                        result["langgraph_api_failure"]["exception_type"] = error_detail.get("exception_type")
                        result["langgraph_api_failure"]["missing_module"] = error_detail.get("missing_module")
                        graph_record = db.get(GraphExecutionRecord, failed_run_id)
                        if graph_record is not None and graph_record.checkpoint_id:
                            checkpoint_ids.append(failed_run_id)
                code = result["langgraph_api_failure"]["code"] or "UNCLASSIFIED"
                raise RuntimeError("LANGGRAPH_POSTGRES_CHAT_FAILED_" + str(code)[:60])
            graph_run_id = graph_response.json()["run_id"]
            run_ids.append(graph_run_id)
            checkpoint_ids.append(graph_run_id)
            with factory() as db:
                graph_run = db.get(Run, graph_run_id)
                graph_record = db.execute(text("select status, checkpoint_id from graph_executions where run_id=:id"),
                                          {"id": graph_run_id}).mappings().one()
                summary3 = db.execute(select(SessionMemorySummary).where(
                    SessionMemorySummary.session_id == session_id,
                    SessionMemorySummary.is_clear_marker.is_(False)
                ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one()
                result["langgraph_run"] = {"run_status": graph_run.status,
                    "graph_status": graph_record["status"], "checkpoint_id_present": bool(graph_record["checkpoint_id"]),
                    "summary_version": summary3.version, "summary_source_run_matches": summary3.source_run_id == graph_run_id}
                if graph_record["status"] != "SUCCEEDED" or not graph_record["checkpoint_id"] or \
                   summary3.version != 3 or summary3.source_run_id != graph_run_id:
                    raise RuntimeError("LANGGRAPH_MEMORY_CHECKPOINT_STATE_INVALID")
                action = db.get(PendingAction, marker + "-action")
                if action.status != "PENDING_CONFIRMATION" or action.arguments_hash != "a" * 64:
                    raise RuntimeError("PENDING_ACTION_CHANGED_BY_GRAPH_COMPACTION")
            values = _checkpoint_values(db_url, settings.checkpoint_schema, graph_run_id)
            result["checkpoint_privacy"] = {"summary_sentinel_absent": marker + "-OLD-PRIVATE" not in json.dumps(values, ensure_ascii=False),
                    "contains_no_memory_context_field": not _contains_key(values, "memory_context") and not _contains_key(values, "summary"),
                "checkpoint_keys": sorted(values.keys())}
            if not result["checkpoint_privacy"]["summary_sentinel_absent"] or \
               not result["checkpoint_privacy"]["contains_no_memory_context_field"]:
                raise RuntimeError("MEMORY_LEAKED_TO_GRAPH_CHECKPOINT")
            graph_replay = graph_client.post("/api/chat", headers=graph_headers, json={"session_id": session_id,
                "message": "你好", "client_request_id": marker + "-graph-run"})
            with factory() as db:
                replay_version = db.execute(select(func.max(SessionMemorySummary.version)).where(
                    SessionMemorySummary.session_id == session_id,
                    SessionMemorySummary.is_clear_marker.is_(False))).scalar_one()
            result["langgraph_idempotent_replay"] = {"same_run_id": graph_replay.status_code == 200 and
                graph_replay.json().get("run_id") == graph_run_id, "summary_version_unchanged": replay_version == 3}
            if not result["langgraph_idempotent_replay"]["same_run_id"] or replay_version != 3:
                raise RuntimeError("LANGGRAPH_REPLAY_RECOMPACTED")

        # Recovery after process/app reconstruction; replay the existing graph Run, then use its summary next turn.
        phase = "application_restart_summary_recovery"
        graph_app.state.engine.dispose(close=True)
        graph_app = None
        engine.dispose(close=True)
        engine = make_engine(db_url)
        factory = make_session_factory(engine)
        _add_messages(factory, session_id, 160, 239, marker + "-OLD-PRIVATE")
        recovery_app = create_app(database_url=db_url, engine=engine, orchestrator_mode="langgraph",
            chat_provider=object(), checkpointer_factory=open_checkpointer, graph_services=services)
        with TestClient(recovery_app, raise_server_exceptions=False) as recovery_client:
            recovery_headers = _login(recovery_client, owner_id, passwords[owner_id])
            session_response = recovery_client.get(f"/api/sessions/{session_id}")
            graph_replay_after_restart = recovery_client.post("/api/chat", headers=recovery_headers,
                json={"session_id": session_id, "message": "你好", "client_request_id": marker + "-graph-run"})
            with factory() as db:
                before_next_turn = db.execute(select(SessionMemorySummary).where(
                    SessionMemorySummary.owner_id == owner_id, SessionMemorySummary.session_id == session_id
                ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one()
                restored = read_session_summary(db, owner_id=owner_id, session_id=session_id)
                next_context = prepare_memory_context(db, owner_id=owner_id, session_id=session_id,
                    current_query="restart follow-up", run_id=marker + "-next-turn", budget=_budget())
            result["restart_replay"] = {"session_recovered": session_response.status_code == 200,
                "same_graph_run_id": graph_replay_after_restart.status_code == 200 and
                    graph_replay_after_restart.json().get("run_id") == graph_run_id,
                "summary_version_unchanged": before_next_turn.version == 3}
            if not result["restart_replay"]["session_recovered"] or not result["restart_replay"]["same_graph_run_id"] or before_next_turn.version != 3:
                raise RuntimeError("RESTART_IDEMPOTENT_REPLAY_FAILED")
            recovery_response = recovery_client.post("/api/chat", headers=recovery_headers,
                json={"session_id": session_id, "message": "你好", "client_request_id": marker + "-restart-run"})
            if recovery_response.status_code != 200 or recovery_response.json().get("status") != "SUCCEEDED":
                raise RuntimeError("RESTART_NEXT_TURN_FAILED")
            recovery_run_id = recovery_response.json()["run_id"]
            run_ids.append(recovery_run_id)
            checkpoint_ids.append(recovery_run_id)
        with factory() as db:
            latest_row = db.execute(select(SessionMemorySummary).where(
                SessionMemorySummary.owner_id == owner_id, SessionMemorySummary.session_id == session_id
            ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one()
            recovery_graph = db.get(GraphExecutionRecord, recovery_run_id)
            all_messages_before_clear = db.query(Message).filter_by(session_id=session_id).count()
        result["restart_recovery"] = {"loaded_summary_version_before_next_turn": before_next_turn.version if restored else None,
            "summary_version_after_next_turn": latest_row.version if restored else None,
            "next_turn_contains_compacted_context": bool(restored and "历史压缩摘要" in next_context["memory_context"]),
            "raw_message_count_preserved": all_messages_before_clear >= 156,
            "next_turn_run_id": recovery_run_id,
            "next_turn_summary_persisted_version": latest_row.version,
            "next_turn_graph_checkpoint_restored": recovery_graph is not None and
                recovery_graph.status == "SUCCEEDED" and bool(recovery_graph.checkpoint_id),
            "pending_action_not_executed": True}
        if (restored is None or
                not result["restart_recovery"]["next_turn_contains_compacted_context"] or
                not result["restart_recovery"]["raw_message_count_preserved"] or
                latest_row.version != 4 or latest_row.source_run_id != recovery_run_id or
                not result["restart_recovery"]["next_turn_graph_checkpoint_restored"]):
            raise RuntimeError("RESTART_SUMMARY_RECOVERY_FAILED")

        # Prior valid version survives an injected summary-write failure.
        phase = "summary_write_failure_rollback"
        _add_messages(factory, session_id, 242, 321, marker + "-OLD-PRIVATE")
        failed_run = _create_successful_run(factory, owner_id, session_id, marker + "-write-failure")
        run_ids.append(failed_run.run_id)
        with factory() as db:
            plan = prepare_memory_context(db, owner_id=owner_id, session_id=session_id,
                current_query="synthetic compact request", run_id=failed_run.run_id, budget=_budget())
            result["summary_write_rollback"] = {"planned_level": plan["level"]}
            if plan["level"] != "AUTO_COMPACT":
                raise RuntimeError("SUMMARY_WRITE_FAILURE_PROBE_NOT_AUTO_COMPACT")
            def fail_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
                if statement.lstrip().lower().startswith("insert into session_memory_summaries"):
                    raise RuntimeError("INJECTED_SUMMARY_WRITE_FAILURE")
            event.listen(engine, "before_cursor_execute", fail_insert)
            try:
                persisted, failed_version = persist_auto_summary(db, owner_id=owner_id,
                    session_id=session_id, run_id=failed_run.run_id, plan=plan)
            finally:
                event.remove(engine, "before_cursor_execute", fail_insert)
            previous = read_session_summary(db, owner_id=owner_id, session_id=session_id)
            result["summary_write_rollback"].update({"persisted": persisted, "returned_version": failed_version,
                "previous_version_retained": previous is not None and previous.get("source_message_ids") is not None}
            )
            previous_row = db.execute(select(SessionMemorySummary.version).where(
                SessionMemorySummary.owner_id == owner_id, SessionMemorySummary.session_id == session_id,
                SessionMemorySummary.is_clear_marker.is_(False)
            ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one_or_none()
            result["summary_write_rollback"]["previous_version"] = previous_row
            result["summary_write_rollback"]["previous_version_retained"] = previous_row == 4
            if persisted or previous_row != 4:
                raise RuntimeError("SUMMARY_WRITE_FAILURE_DAMAGED_PREVIOUS")

        # Failed Runs follow the production failure path and never persist a new summary.
        phase = "failed_run_no_summary"
        from app.m1.chat_execution import _finish_memory
        with factory() as db:
            failed, same = create_run(db, owner_id, session_id, marker + "-failed-run", {"message": "failure probe"})
            if failed is None or not same or not claim_run(db, failed, execution_owner_id="m5.1-probe"):
                raise RuntimeError("FAILED_RUN_SETUP_FAILED")
            finish_run(db, failed, status="FAILED", error_code="INJECTED_PROBE_FAILURE", trace_id=str(uuid.uuid4()))
            failed_plan = prepare_memory_context(db, owner_id=owner_id, session_id=session_id,
                current_query="synthetic failed request", run_id=failed.run_id, budget=_budget())
            run_ids.append(failed.run_id)
            _finish_memory(db, run=failed, user_id=owner_id, trace_id=str(uuid.uuid4()), plan=failed_plan, succeeded=False)
            after_failure = read_session_summary(db, owner_id=owner_id, session_id=session_id)
            latest_after_failure = db.execute(select(SessionMemorySummary.version).where(
                SessionMemorySummary.owner_id == owner_id, SessionMemorySummary.session_id == session_id,
                SessionMemorySummary.is_clear_marker.is_(False)
            ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one_or_none()
            pending_after_failure = db.get(PendingAction, marker + "-action")
            result["failed_run"] = {"run_status": db.get(Run, failed.run_id).status,
                "summary_version": latest_after_failure,
                "summary_version_unchanged": after_failure is not None and latest_after_failure == 4,
                "pending_action_unchanged": pending_after_failure.status == "PENDING_CONFIRMATION" and
                    pending_after_failure.arguments_hash == "a" * 64}
            if not result["failed_run"]["summary_version_unchanged"] or not result["failed_run"]["pending_action_unchanged"]:
                raise RuntimeError("FAILED_RUN_WROTE_SUMMARY")

        # The API clear marker must commit, preserve messages, and prevent old message resurrection.
        phase = "clear_memory_api"
        clear_app = create_app(database_url=db_url, engine=engine, orchestrator_mode="langgraph",
            chat_provider=object(), checkpointer_factory=open_checkpointer, graph_services=services)
        with TestClient(clear_app, raise_server_exceptions=False) as client:
            headers = _login(client, owner_id, passwords[owner_id])
            clear = client.delete(f"/api/sessions/{session_id}/memory", headers=headers)
            if clear.status_code != 200:
                raise RuntimeError("OWNER_CLEAR_API_FAILED")
        with factory() as db:
            marker_row = db.execute(select(SessionMemorySummary).where(
                SessionMemorySummary.session_id == session_id).order_by(SessionMemorySummary.version.desc())).scalar_one()
            after_context = prepare_memory_context(db, owner_id=owner_id, session_id=session_id,
                current_query="new question", run_id=marker + "-after-clear", budget=_budget())
            message_count = db.query(Message).filter_by(session_id=session_id).count()
            result["clear_memory"] = {"status": clear.json().get("status"),
                "clear_marker": marker_row.is_clear_marker, "watermark": marker_row.source_through_sequence,
                "message_count_retained": message_count,
                "preclear_marker_absent_from_next_context": marker + "-OLD-PRIVATE" not in after_context["memory_context"],
                "summary_read_after_clear": read_session_summary(db, owner_id=owner_id, session_id=session_id)}
            if not marker_row.is_clear_marker or message_count < 156 or marker + "-OLD-PRIVATE" in after_context["memory_context"]:
                raise RuntimeError("CLEAR_MARKER_OR_MESSAGE_RETENTION_FAILED")

        result["status"] = "PASS"
        result["provider_calls"] = 0
        result["summary_model_calls"] = 0
        result["business_write_operations"] = 0
    except Exception as exc:
        result.update({"status": "FAIL", "failure_phase": phase,
            "error_type": type(exc).__name__, "error_code": str(exc)[:120]})
    finally:
        if injected_handler is not None and engine is not None:
            try:
                event.remove(engine, "before_cursor_execute", injected_handler)
            except Exception:
                pass
        for current_app in (app, graph_app, recovery_app):
            if current_app is not None:
                try:
                    current_app.state.engine.dispose(close=True)
                except Exception:
                    pass
        if engine is not None:
            try:
                from app.orchestration.checkpointer import run_checkpointer_async
                checkpoint_threads_remaining = 0
                for thread_id in checkpoint_ids:
                    _delete_checkpoint(db_url, settings.checkpoint_schema, thread_id)
                    if _checkpoint_values(db_url, settings.checkpoint_schema, thread_id):
                        checkpoint_threads_remaining += 1
                result["cleanup"]["checkpoint_threads_checked"] = len(checkpoint_ids)
                result["cleanup"]["checkpoint_threads_remaining"] = checkpoint_threads_remaining
                with engine.begin() as conn:
                    if session_ids:
                        conn.execute(text("delete from traces where run_id in (select run_id from runs where session_id = any(:ids))"),
                                     {"ids": session_ids})
                        conn.execute(text("delete from observability_records where run_id in (select run_id from runs where session_id = any(:ids))"),
                                     {"ids": session_ids})
                        conn.execute(text("delete from session_memory_summaries where session_id = any(:ids)"),
                                     {"ids": session_ids})
                        conn.execute(text("delete from controlled_pending_actions where session_id = any(:ids)"),
                                     {"ids": session_ids})
                        conn.execute(text("delete from runs where session_id = any(:ids)"), {"ids": session_ids})
                        conn.execute(text("delete from messages where session_id = any(:ids)"), {"ids": session_ids})
                        conn.execute(text("delete from sessions where session_id = any(:ids)"), {"ids": session_ids})
                    conn.execute(text("delete from traces where user_id = any(:ids)"), {"ids": [owner_id, other_id]})
                    conn.execute(text("delete from auth_sessions where user_id = any(:ids)"), {"ids": [owner_id, other_id]})
                    conn.execute(text("delete from demo_credentials where user_id = any(:ids)"), {"ids": [owner_id, other_id]})
                    conn.execute(text("delete from users where user_id = any(:ids)"), {"ids": [owner_id, other_id]})
                result["cleanup"]["completed"] = True
                result["cleanup"]["residual"] = _counts(engine, (owner_id, other_id), tuple(session_ids))
                result["cleanup"]["zero_residual"] = (
                    all(value == 0 for value in result["cleanup"]["residual"].values()) and
                    checkpoint_threads_remaining == 0)
                if preexisting_counts is not None:
                    with engine.connect() as conn:
                        post_counts = {name: conn.execute(text(f"select count(*) from {name}")).scalar_one()
                                       for name in preexisting_counts}
                    result["cleanup"]["preexisting_counts_after"] = post_counts
                    result["cleanup"]["preexisting_data_counts_unchanged"] = preexisting_counts == post_counts
                if not result["cleanup"]["zero_residual"] and result["status"] == "PASS":
                    result["status"] = "FAIL"
                    result["failure_phase"] = "cleanup_verification"
                    result["error_code"] = "PROBE_DATA_REMAINS"
                if result["cleanup"].get("preexisting_data_counts_unchanged") is False and result["status"] == "PASS":
                    result["status"] = "FAIL"
                    result["failure_phase"] = "existing_data_verification"
                    result["error_code"] = "PREEXISTING_DATA_COUNT_CHANGED"
            except Exception as cleanup_exc:
                result["cleanup"].update({"completed": False, "error_type": type(cleanup_exc).__name__})
                if result["status"] == "PASS":
                    result["status"] = "FAIL"
                    result["failure_phase"] = "cleanup"
                    result["error_code"] = "PROBE_CLEANUP_FAILED"
            engine.dispose(close=True)
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        result["finished_at"] = _utc()
        result["container_after_probe"] = "must be restored by caller to originally stopped state"
    return result


if __name__ == "__main__":
    record = probe()
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if record.get("status") == "PASS" else 1)
