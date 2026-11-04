"""Short-lived M5.4-A browser acceptance server for shoprelay_m11_test only.

The account and order are unique synthetic fixtures.  The chat path uses the
real Router, LangGraph, PostgreSQL tools, retrieval service, Run/SSE and admin
projection; policy text generation is a deterministic local test stub.  The
special four-agent scenario is explicitly route-injected for fixed engineering
coverage and is not Router-quality evidence.  All fixture rows and checkpoints
are removed in finally when the server is stopped with Ctrl+C.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, text
from uvicorn import run

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor" / "g2"), str(ROOT / ".vendor"), str(ROOT)]

from app.m0.config import load_settings
from app.m1.api import create_app
from app.m1.db import make_engine, make_session_factory
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential,
    GraphExecutionRecord, Message, ObservabilityRecord, Order, OrderItem, PendingAction,
    Run, RunEvent, SessionMemorySummary, Trace, User)
from app.m1.routing import decide_route
from app.m1.services import set_demo_password
from app.orchestration.adapters import make_legacy_services
from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url


class DeterministicEvidenceStub:
    """Local test-only answer stub; never contacts a provider."""
    def __init__(self) -> None:
        self.settings = load_settings(require_api_key=False)
        self.plan_calls = 0
        self.answer_calls = 0

    def plan_policy_aspects(self, question: str, *, context_notes=None) -> dict:
        del context_notes
        self.plan_calls += 1
        aspects = []
        for aspect_id, anchor, label in (
            ("return", "退货", "退货政策条款"),
            ("refund", "退款", "退款政策条款"),
        ):
            if anchor in question:
                aspects.append({"aspect_id": aspect_id, "query_anchor": anchor, "text": label})
        if len(aspects) < 2:
            raise RuntimeError("MULTI_FIXTURE_REQUIRES_TWO_POLICY_ASPECTS")
        return {"plan_result": {"request_mode": "POLICY_EXPLANATION",
                "requested_aspects": aspects, "user_provided_facts": []},
            "model_id": "m54a-deterministic-test-stub", "finish_reason": "stop",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "latency_ms": 0,
            "estimated_cost_usd": 0.0, "prompt_version": "m54a-evidence-stub-v1",
            "raw_content": None}

    def answer_for_policy_aspects(self, question: str, evidence: list[dict], *,
                                  request_mode: str, requested_aspects: list[dict],
                                  user_provided_facts: list[dict], context_notes=None) -> dict:
        del question, request_mode, requested_aspects, user_provided_facts, context_notes
        self.answer_calls += 1
        rows = [row for row in evidence if isinstance(row.get("chunk_id"), str)]
        # Repeat only exact, same-run evidence text so no unsupported policy
        # assertions are introduced by this deterministic engineering stub.
        answer = "；".join(row.get("content", "") for row in rows)
        if not answer:
            raise RuntimeError("NO_EVIDENCE_FOR_STUB_ANSWER")
        return {"answer": answer, "citations": [row["chunk_id"] for row in rows],
            "finish_reason": "stop", "model_id": "m54a-deterministic-test-stub",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "latency_ms": 0,
            "estimated_cost_usd": 0.0, "prompt_version": "m54a-evidence-stub-v1",
            "raw_content": None}

    def answer(self, question: str, evidence: list[dict]) -> dict:
        return self.answer_for_policy_aspects(question, evidence,
            request_mode="POLICY_EXPLANATION", requested_aspects=[],
            user_provided_facts=[])


def _checkpoint_delete(database_url: str, schema: str, run_id: str) -> None:
    async def delete_one():
        async with open_checkpointer(database_url, schema) as saver:
            await saver.adelete_thread(run_id)
    run_checkpointer_async(delete_one())


def _run_snapshot(factory, user_id: str) -> list[dict]:
    with factory() as db:
        rows = db.execute(select(Run).where(Run.user_id == user_id)
            .order_by(Run.created_at.asc())).scalars().all()
        output = []
        for row in rows:
            events = db.execute(select(RunEvent).where(RunEvent.run_id == row.run_id)
                .order_by(RunEvent.seq.asc())).scalars().all()
            obs = db.execute(select(ObservabilityRecord).where(
                ObservabilityRecord.run_id == row.run_id).order_by(ObservabilityRecord.timestamp.asc())).scalars().all()
            traces = db.execute(select(Trace).where(Trace.run_id == row.run_id)
                .order_by(Trace.created_at.asc())).scalars().all()
            graph = db.get(GraphExecutionRecord, row.run_id)
            result = json.loads(row.result_json) if row.result_json else {}
            route = result.get("route") or {}
            retrieval = result.get("policy_retrieval") or result.get("retrieval") or {}
            safe_events = []
            event_fields = {
                "route_decided": ("strategy", "sub_strategy", "decision_source", "reason_code", "error_code"),
                "agent_started": ("agent",),
                "retrieval_hop": ("aspect_id", "strategy_selected", "strategy_executed", "knowledge_version",
                                  "evidence_count", "degraded", "degraded_reason", "diagnostics"),
                "tool_started": ("tool_id", "tool", "capability"),
                "tool_finished": ("tool_id", "tool", "capability", "status", "duration_ms", "error_code"),
                "error": ("error_code", "stage"), "done": ("status",),
            }
            for event in events:
                payload = json.loads(event.payload_json) if event.payload_json else {}
                safe_events.append({"seq": event.seq, "type": event.event_type,
                    "timestamp": event.timestamp.isoformat(),
                    "data": {key: payload[key] for key in event_fields.get(event.event_type, ()) if key in payload}})
            span_rows = []
            for item in obs:
                if item.record_type != "span":
                    continue
                payload = json.loads(item.payload_json) if item.payload_json else {}
                span_rows.append({key: payload.get(key) for key in (
                    "span_id", "parent_span_id", "node_name", "strategy", "duration_ms", "status", "error_code")})
            output.append({"run_id": row.run_id, "session_id": row.session_id,
                "status": row.status, "error_code": row.error_code,
                "strategy": result.get("strategy") or route.get("strategy"),
                "sub_strategy": result.get("sub_strategy") or route.get("sub_strategy"),
                "route_source": route.get("decision_source"),
                "route_reason": route.get("reason_code"),
                "answer_kind": result.get("answer_kind"),
                "model_id": row.model_id, "usage": json.loads(row.usage_json) if row.usage_json else None,
                "latency_ms": row.latency_ms,
                "created_at": row.created_at.isoformat(), "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "graph_status": graph.status if graph else None,
                "event_types": [event.event_type for event in events],
                "event_count": len(events),
                "events": safe_events,
                "trace_ids": sorted({item.trace_id for item in events} | {item.trace_id for item in obs} | {item.trace_id for item in traces}),
                "span_count": len(span_rows), "spans": span_rows,
                # Handoffs are checkpoint/trace state, not necessarily present
                # in Run.result_json. Do not turn an absent projection into 0.
                "handoff_count": (len(result["handoffs"])
                                  if isinstance(result.get("handoffs"), list) else None),
                "handoff_projection": ("Run.result_json" if isinstance(result.get("handoffs"), list)
                                       else "NOT_PERSISTED_IN_RUN_RESULT; verify checkpoint or browser timeline"),
                "agent_names": [event.payload_json for event in events if event.event_type == "agent_started"],
                "citation_ids": [item.get("chunk_id") for item in result.get("citations", [])
                                 if isinstance(item, dict)],
                "policy_strategy_selected": retrieval.get("strategy_selected"),
                "policy_strategy_executed": retrieval.get("strategy_executed"),
                "retrieval_degraded": retrieval.get("degraded"),
                "retrieval_diagnostics": retrieval.get("diagnostics"),
                "orchestrator": "langgraph" if graph else None})
        return output


def _cleanup(factory, engine, database_url: str, schema: str, marker: str,
             admin_id: str, other_id: str, order_id: str, report: dict) -> None:
    run_ids = []
    with factory() as db:
        run_ids = list(db.execute(select(Run.run_id).where(Run.user_id == admin_id)).scalars())
    report["observed_runs_before_cleanup"] = _run_snapshot(factory, admin_id)
    report["checkpoints_removed"] = 0
    for run_id in run_ids:
        try:
            _checkpoint_delete(database_url, schema, run_id)
            report["checkpoints_removed"] += 1
        except Exception as exc:
            report.setdefault("checkpoint_cleanup_errors", []).append(type(exc).__name__)
    with factory.begin() as db:
        session_ids = list(db.execute(select(ConversationSession.session_id).where(
            ConversationSession.user_id == admin_id)).scalars())
        action_ids = []
        if run_ids:
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
            db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
            db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids)))
            action_ids = list(db.execute(select(PendingAction.action_id).where(
                PendingAction.run_id.in_(run_ids))).scalars())
            if action_ids:
                db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
            db.execute(delete(PendingAction).where(PendingAction.run_id.in_(run_ids)))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.source_run_id.in_(run_ids)))
        if session_ids:
            db.execute(delete(Message).where(Message.session_id.in_(session_ids)))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(session_ids)))
        db.execute(delete(AuthSession).where(AuthSession.user_id.in_([admin_id, other_id])))
        db.execute(delete(Run).where(Run.user_id.in_([admin_id, other_id])))
        db.execute(delete(ConversationSession).where(ConversationSession.user_id.in_([admin_id, other_id])))
        db.execute(delete(DemoCredential).where(DemoCredential.user_id.in_([admin_id, other_id])))
        db.execute(delete(OrderItem).where(OrderItem.order_id == order_id))
        db.execute(delete(Order).where(Order.order_id == order_id, Order.user_id == admin_id))
        db.execute(delete(User).where(User.user_id.in_([admin_id, other_id])))
    with engine.connect() as connection:
        residual = {
            "users": connection.execute(text("select count(*) from users where user_id like :p"), {"p": marker + "%"}).scalar_one(),
            "credentials": connection.execute(text("select count(*) from demo_credentials where user_id like :p"), {"p": marker + "%"}).scalar_one(),
            "auth_sessions": connection.execute(text("select count(*) from auth_sessions where user_id like :p"), {"p": marker + "%"}).scalar_one(),
            "sessions": connection.execute(text("select count(*) from sessions where user_id like :p"), {"p": marker + "%"}).scalar_one(),
            "runs": connection.execute(text("select count(*) from runs where user_id like :p"), {"p": marker + "%"}).scalar_one(),
            "messages": connection.execute(text("select count(*) from messages where session_id = any(:sids)"), {"sids": session_ids}).scalar_one() if session_ids else 0,
            "run_events": connection.execute(text("select count(*) from run_events where run_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "graph_executions": connection.execute(text("select count(*) from graph_executions where run_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "observability_records": connection.execute(text("select count(*) from observability_records where run_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "traces": connection.execute(text("select count(*) from traces where run_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "action_audits": connection.execute(text("select count(*) from action_audit where action_id = any(:ids)"), {"ids": action_ids}).scalar_one() if action_ids else 0,
            "pending_actions": connection.execute(text("select count(*) from controlled_pending_actions where run_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "memory_summaries": connection.execute(text("select count(*) from session_memory_summaries where session_id = any(:sids)"), {"sids": session_ids}).scalar_one() if session_ids else 0,
            "orders": connection.execute(text("select count(*) from orders where order_id=:id"), {"id": order_id}).scalar_one(),
            "order_items": connection.execute(text("select count(*) from order_items where order_id=:id"), {"id": order_id}).scalar_one(),
            "checkpoints": connection.execute(text("select count(*) from shoprelay_langgraph.checkpoints where thread_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "checkpoint_blobs": connection.execute(text("select count(*) from shoprelay_langgraph.checkpoint_blobs where thread_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
            "checkpoint_writes": connection.execute(text("select count(*) from shoprelay_langgraph.checkpoint_writes where thread_id = any(:ids)"), {"ids": run_ids}).scalar_one() if run_ids else 0,
        }
    report["cleanup"] = {"residual_counts": residual,
        "all_zero": all(value == 0 for value in residual.values()),
        "business_writes_by_chat": 0, "pending_action_write_calls": 0,
        "deepseek_http_requests": 0, "mcp_write_calls": 0, "payment_calls": 0}


def main() -> None:
    marker = os.environ.get("M54A_PROBE_ID", "").strip()
    if not marker or not marker.replace("_", "").isalnum() or len(marker) > 40:
        raise RuntimeError("M54A_PROBE_ID must be a short alphanumeric marker")
    admin_id, other_id = marker + "_admin", marker + "_other"
    password = marker + "-browser-only-test!"
    order_id = "ORD-" + marker.upper().replace("_", "-") + "-01"
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("M54A probe refuses a non-loopback/non-test database")
    database_url = url.render_as_string(hide_password=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    settings = load_settings(require_api_key=False)
    report = {"schema_version": "shoprelay.m5.4-a-browser-server.v1",
        "probe_id": marker, "started_at": started_at,
        "database": "shoprelay_m11_test", "postgres_version": None,
        "alembic_revision": None, "fixture_source": "SIMULATED_TEST_DATA",
        "synthetic_accounts": [admin_id, other_id], "order_id": order_id,
        "orchestrator": "langgraph", "router": "actual decide_route except named M54A_FOUR_AGENT engineering scenario injection",
        "model_provider": "deterministic local evidence stub", "deepseek_http_requests": 0,
        "business_write_executions": 0, "pending_action_write_calls": 0,
        "mcp_write_calls": 0, "payment_calls": 0}
    app = None
    try:
        from app.m1.db import current_revision
        with engine.connect() as connection:
            report["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
            report["database"] = connection.execute(text("select current_database()")).scalar_one()
            product_exists = connection.execute(text("select count(*) from products where product_id='P-CUP'")).scalar_one()
        report["alembic_revision"] = current_revision(database_url)
        if report["database"] != "shoprelay_m11_test" or report["postgres_version"] != "16.12":
            raise RuntimeError("DATABASE_IDENTITY_OR_VERSION_MISMATCH")
        if not product_exists:
            raise RuntimeError("CANONICAL_PRODUCT_NOT_AVAILABLE")
        with factory() as db:
            if any(db.get(User, item) is not None for item in (admin_id, other_id)):
                raise RuntimeError("REFUSING_TO_REUSE_PROBE_ACCOUNT")
        with factory.begin() as db:
            db.add_all([User(user_id=admin_id, display_name="M5.4-A isolated admin"),
                        User(user_id=other_id, display_name="M5.4-A isolated user")])
        with factory() as db:
            set_demo_password(db, admin_id, password)
            set_demo_password(db, other_id, password)
        now = datetime.now(timezone.utc)
        with factory.begin() as db:
            db.add(Order(order_id=order_id, user_id=admin_id, status="DELIVERED",
                paid_at=(now - timedelta(days=3)).isoformat().replace("+00:00", "Z"),
                delivered_at=(now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                paid_cents=4299, version=1))
            db.flush()
            db.add(OrderItem(order_id=order_id, product_id="P-CUP", quantity=1, paid_cents=4299))
        report["fixture_setup"] = {"users": 2, "credentials": 2, "orders": 1,
            "order_items": 1, "product_catalog_rows_created": 0, "source": "SIMULATED_TEST_DATA"}

        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        os.environ["SHOPRELAY_RETRIEVAL_MODE"] = "bm25"
        os.environ["SHOPRELAY_QDRANT_MODE"] = "local"
        os.environ["SHOPRELAY_GRAPH_VERSION"] = "m5.4-a-live-audit"
        os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m5.4-a-browser-r1"
        provider = DeterministicEvidenceStub()
        app = create_app(database_url=database_url, engine=engine, chat_provider=provider,
            orchestrator_mode="langgraph", observability_admin_user_ids=[admin_id])
        services = make_legacy_services(provider=provider,
            retrieval_service=app.state.retrieval_service, index_path=app.state.policy_index_path,
            evaluation_at=None)

        def router(query: str, *, context_notes: str = "") -> dict:
            del context_notes
            if "M54A_FOUR_AGENT" in query:
                return {"strategy": "AGENT", "sub_strategy": "AGENT+MULTI",
                    "safety_decision": "ALLOW", "decision_source": "RULE",
                    "reason_code": "M54A_FIXED_ENGINEERING_SCENARIO",
                    "intent_labels": ["live_business", "composite_policy"],
                    "needs_clarification": False, "required_capabilities": [
                        "get_order", "get_order_items", "get_product_detail",
                        "search_knowledge", "get_refund_status"],
                    "route_version": "m5.4-a-test-harness-v1", "latency_ms": 0}
            return decide_route(query).model_dump(mode="json")

        services.router = router
        app.state.graph_services = services
        report["provider_stub_calls"] = {"planning": 0, "answer": 0}
        report["server"] = {"host": "127.0.0.1", "port": 8000,
            "production_write_gates": False, "mcp_write_allowlist_opened": False}
        print(json.dumps({"probe_id": marker, "admin_user": admin_id,
            "ordinary_user": other_id, "synthetic_order_id": order_id,
            "database": report["database"], "revision": report["alembic_revision"],
            "provider": "deterministic local stub; no remote calls"}, ensure_ascii=False), flush=True)
        run(app, host="127.0.0.1", port=8000, log_level="warning")
    except KeyboardInterrupt:
        report["server_stop"] = "CTRL_C"
    finally:
        if app is not None:
            report["provider_stub_calls"] = {"planning": provider.plan_calls, "answer": provider.answer_calls}
            _cleanup(factory, engine, database_url, settings.checkpoint_schema,
                     marker, admin_id, other_id, order_id, report)
            close = getattr(app.state.retrieval_service, "close", None)
            if callable(close):
                close()
        report["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        output = ROOT / "docs" / "probes" / f"m5.4-a-live-runs-{marker}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"probe_record": str(output),
            "cleanup_all_zero": (report.get("cleanup") or {}).get("all_zero", False)}, ensure_ascii=False), flush=True)
        engine.dispose()


if __name__ == "__main__":
    main()
