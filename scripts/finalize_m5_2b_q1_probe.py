"""Capture and remove only the isolated M5.2-B-Q1 probe rows."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from app.m0.config import load_settings
from app.m1.db import make_engine, make_session_factory
from app.m1.models import (
    AuthSession,
    ConversationSession,
    DemoCredential,
    GraphExecutionRecord,
    Message,
    ObservabilityRecord,
    Order,
    Run,
    RunEvent,
    SessionMemorySummary,
    Trace,
    User,
)
from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url

RUN_IDS = [
    "ebfc3f6f-77a6-4803-85b0-1064b2896123",
    "8c9827dd-3e90-4ccb-b09a-0e7428c277e5",
    "f2220a92-b13e-4644-a752-6182b74c73a7",
    "3e4ad427-335b-4d1c-8323-f11e29906dec",
    "a8183dcc-49cb-4da0-a3da-05a14f210384",
    "b2e1087d-0ee2-4631-8b36-083b55bb5a5c",
    "21f2b7aa-9344-4385-8e0f-ef62878c7172",
    "aad92787-65c2-4ddf-b9a1-ce862537c2b5",
]
SESSION_IDS = [
    "bddcc565-2dfa-4ca5-a18e-152d634649ee",
    "1eedd814-040b-4cfe-9d75-1f8007b9cf8d",
    "8b6b4567-aa10-4958-becb-536f2b7d9294",
    "5ad00bad-0707-4a5a-8277-b49b2412f454",
]
ORDER_ID = "ORD-Q1-M52BQ1_6AF07589"
OWNER_PATTERN = "m52bq1_%"


def _count(db, model, column, values):
    return db.scalar(select(func.count()).select_from(model).where(column.in_(values)))


def _owner_counts(db):
    return {
        "users": db.scalar(select(func.count()).select_from(User).where(User.user_id.like(OWNER_PATTERN))),
        "auth_sessions": db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id.like(OWNER_PATTERN))),
        "sessions": _count(db, ConversationSession, ConversationSession.session_id, SESSION_IDS),
        "messages": _count(db, Message, Message.session_id, SESSION_IDS),
        "runs": _count(db, Run, Run.run_id, RUN_IDS),
        "run_events": _count(db, RunEvent, RunEvent.run_id, RUN_IDS),
        "traces": _count(db, Trace, Trace.run_id, RUN_IDS),
        "observability_records": _count(db, ObservabilityRecord, ObservabilityRecord.run_id, RUN_IDS),
        "memory_summaries": _count(db, SessionMemorySummary, SessionMemorySummary.session_id, SESSION_IDS),
        "orders": db.scalar(select(func.count()).select_from(Order).where(Order.order_id == ORDER_ID)),
        "graph_executions": _count(db, GraphExecutionRecord, GraphExecutionRecord.run_id, RUN_IDS),
    }


async def _delete_threads(database_url: str, schema: str):
    async with open_checkpointer(database_url, schema) as saver:
        for run_id in RUN_IDS:
            await saver.adelete_thread(run_id)


def main():
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test":
        raise RuntimeError("UNSAFE_DATABASE_TARGET")
    database_url = url.render_as_string(hide_password=False)
    settings = load_settings(require_api_key=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    report = {
        "stage": "M5.2-B-Q1",
        "version": "r1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "database": "shoprelay_m11_test",
            "postgresql": "16.12",
            "alembic_revision": "20261023_16",
            "graph_version": "m5.2-b-q1",
            "execution_version": "m5.2-b-q1-isolated",
            "deepseek_http_requests": 0,
            "business_writes": 0,
            "mcp_writes": 0,
            "test_candidate_accessed": False,
        },
        "scenarios": {},
        "browser": {
            "actual_in_app_browser": True,
            "frontend_origin": "http://127.0.0.1:5173",
            "login_owner_a": "PASS",
            "login_owner_b": "PASS",
            "owner_b_session_list_count": 0,
            "owner_b_direct_navigation_to_owner_a_session": "redirected_to_new_chat_without_owner_a_history",
            "direct_navigation_to_read_projection_apis": "BLOCKED_BY_BROWSER_CLIENT",
            "execution_projection_cross_owner_browser_http_status": "NOT_VERIFIABLE",
            "memory_projection_cross_owner_browser_http_status": "NOT_VERIFIABLE",
            "console_error_and_warning_entries": [],
            "network_panel_export": "NOT_VERIFIABLE",
            "screenshot_file_export": "NOT_VERIFIABLE",
        },
        "security": {
            "existing_owner_scoped_api_regression": "PASS",
            "owner_projection_status": 200,
            "cross_owner_projection_status": 404,
            "summary_content_exposed": False,
            "browser_direct_api_limitation": "Codex browser client blocked /api navigation; no HTTP status was invented.",
        },
        "memory_persistence": {},
        "synthetic_order": {},
        "cleanup": {},
    }
    try:
        with factory() as db:
            for run_id in RUN_IDS:
                run = db.get(Run, run_id)
                if run is None:
                    continue
                result = json.loads(run.result_json) if run.result_json else {}
                events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)).scalars().all()
                spans = db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run_id,
                    ObservabilityRecord.record_type == "span",
                ).order_by(ObservabilityRecord.timestamp)).scalars().all()
                trace_rows = db.execute(select(Trace).where(
                    Trace.run_id == run_id,
                    Trace.event_type.in_(["policy_retrieval_completed", "policy_retrieval_aspect", "session_memory_context"]),
                ).order_by(Trace.created_at)).scalars().all()
                graph = db.get(GraphExecutionRecord, run_id)
                citations = result.get("citations", []) if isinstance(result.get("citations"), list) else []
                retrieval = result.get("retrieval") or result.get("policy_retrieval")
                span_values = []
                for row in spans:
                    data = json.loads(row.payload_json)
                    span_values.append({
                        "node_name": data.get("node_name"),
                        "status": data.get("status"),
                        "error_code": data.get("error_code"),
                        "duration_ms": round(data.get("duration_ms", 0), 3),
                        "trace_id": row.trace_id,
                        "span_id": row.span_id,
                        "parent_span_id": row.parent_span_id,
                    })
                memory_trace = []
                for row in trace_rows:
                    if row.event_type == "session_memory_context":
                        payload = json.loads(row.event_json)
                        memory_trace.append({
                            "status": row.status,
                            "data": {key: payload.get(key) for key in (
                                "level", "estimated_tokens_before", "estimated_tokens_after",
                                "effective_context_tokens", "compression_ratio", "summary_model_calls",
                                "summary_cost_estimate_usd", "summary_latency_ms", "summary_persisted",
                                "summary_version", "source_message_count", "thresholds",
                            )},
                        })
                report["scenarios"][run_id] = {
                    "status": run.status,
                    "error_code": run.error_code,
                    "strategy": result.get("strategy"),
                    "sub_strategy": result.get("sub_strategy"),
                    "answer_kind": result.get("answer_kind"),
                    "citation_ids": [c.get("chunk_id") if isinstance(c, dict) else c for c in citations],
                    "evidence_ids": [item.get("chunk_id") for item in result.get("evidence", []) if isinstance(item, dict)],
                    "retrieval": retrieval,
                    "handoffs": [{key: item.get(key) for key in (
                        "handoff_id", "source_agent", "target_agent", "required_capabilities",
                        "context_refs", "evidence_refs", "status", "error_code",
                    )} for item in result.get("handoffs", []) if isinstance(item, dict)],
                    "events": [{"seq": row.seq, "type": row.event_type} for row in events],
                    "spans": span_values,
                    "memory_trace": memory_trace,
                    "graph": {
                        "status": graph.status,
                        "thread_id": graph.thread_id,
                        "checkpoint_id": graph.checkpoint_id,
                        "graph_version": graph.graph_version,
                    } if graph else None,
                }
            summaries = db.execute(select(SessionMemorySummary).where(
                SessionMemorySummary.session_id == "1eedd814-040b-4cfe-9d75-1f8007b9cf8d"
            ).order_by(SessionMemorySummary.version)).scalars().all()
            report["memory_persistence"] = {
                "summary_versions": [{
                    "version": row.version,
                    "source_run_id": row.source_run_id,
                    "source_through_sequence": row.source_through_sequence,
                    "source_message_ids": json.loads(row.source_message_ids_json),
                    "estimated_tokens_before": row.estimated_tokens_before,
                    "estimated_tokens_after": row.estimated_tokens_after,
                } for row in summaries],
                "raw_message_count_preserved": _count(db, Message, Message.session_id, [SESSION_IDS[1]]),
                "summary_content_exposed_by_api": False,
                "browser_refresh_restored_version": summaries[-1].version if summaries else None,
                "summarizer": "deterministic_extractive_v1",
                "summary_model_calls": 0,
            }
            order = db.get(Order, ORDER_ID)
            report["synthetic_order"] = {
                "status": order.status if order else None,
                "source": "SIMULATED_TEST_DATA",
                "business_writes": 0,
            }
            report["cleanup"]["before_counts"] = _owner_counts(db)

        run_checkpointer_async(_delete_threads(database_url, settings.checkpoint_schema))
        with factory.begin() as db:
            # Delete only rows rooted in the unique Q1 ids; some legacy FKs are
            # restrictive rather than ON DELETE CASCADE, so use explicit order.
            db.execute(delete(Trace).where(Trace.run_id.in_(RUN_IDS)))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(RUN_IDS)))
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(RUN_IDS)))
            db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(RUN_IDS)))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(SESSION_IDS)))
            db.execute(delete(Message).where(Message.session_id.in_(SESSION_IDS)))
            db.execute(delete(Run).where(Run.run_id.in_(RUN_IDS)))
            db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(SESSION_IDS)))
            db.execute(delete(Order).where(Order.order_id == ORDER_ID))
            db.execute(delete(AuthSession).where(AuthSession.user_id.like(OWNER_PATTERN)))
            db.execute(delete(DemoCredential).where(DemoCredential.user_id.like(OWNER_PATTERN)))
            db.execute(delete(User).where(User.user_id.like(OWNER_PATTERN)))
        with factory() as db:
            report["cleanup"]["after_counts"] = _owner_counts(db)
            report["cleanup"]["all_zero"] = all(value == 0 for value in report["cleanup"]["after_counts"].values())
            report["cleanup"]["checkpoint_threads_deleted"] = len(RUN_IDS)
        Path("docs/probes/m5.2-b-q1-workbench-20261023.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
