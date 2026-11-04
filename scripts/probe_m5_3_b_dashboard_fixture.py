"""Create/remove isolated synthetic rows for the M5.3-B dashboard browser probe.

This fixture only uses shoprelay_m11_test through the existing safe resolver. It
does not call chat providers or business tools. Supply a one-time password via
M53B_FIXTURE_PASSWORD; the password is never written to evidence or printed.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".vendor"))
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select, text

from app.m1.db import make_engine, make_session_factory
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential,
    GraphExecutionRecord, ObservabilityRecord, PendingAction, Run, RunEvent, Trace, User)
from app.m1.services import password_hash
from app.m1.models import utc_now
from scripts.g2_test_database import resolve_test_database_url


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"setup", "cleanup"}:
        raise SystemExit("usage: python scripts/probe_m5_3_b_dashboard_fixture.py setup|cleanup <probe_marker>")
    mode, marker = sys.argv[1], sys.argv[2]
    if not marker.startswith("m53b-") or len(marker) > 48 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in marker):
        raise SystemExit("invalid probe marker")
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test":
        raise SystemExit("refusing non-test database")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    admin_id, normal_id = f"{marker}-admin", f"{marker}-user"
    session_admin, session_normal = f"{marker}-session-a", f"{marker}-session-u"
    run_ids = [f"{marker}-agent-ok", f"{marker}-single-ok", f"{marker}-unknown", f"{marker}-unavailable"]
    now = utc_now()
    created: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            database = conn.execute(text("select current_database()")).scalar_one()
            version = conn.execute(text("select current_setting('server_version')")).scalar_one()
            revision = conn.execute(text("select version_num from alembic_version")).scalar_one()
        if database != "shoprelay_m11_test":
            raise SystemExit("unexpected database target")
        if mode == "setup":
            secret = os.environ.get("M53B_FIXTURE_PASSWORD", "")
            if len(secret) < 14:
                raise SystemExit("M53B_FIXTURE_PASSWORD must be a one-time synthetic credential")
            with factory.begin() as db:
                existing = db.execute(select(User.user_id).where(User.user_id.in_([admin_id, normal_id]))).all()
                if existing:
                    raise SystemExit("probe identities already exist; refusing to overwrite")
                db.add_all([User(user_id=admin_id, display_name="隔离监控验收管理员"),
                            User(user_id=normal_id, display_name="隔离监控验收普通用户")])
                db.flush()
                db.add_all([DemoCredential(user_id=admin_id, password_hash=password_hash(secret), active=True, updated_at=now),
                            DemoCredential(user_id=normal_id, password_hash=password_hash(secret), active=True, updated_at=now)])
                db.flush()
                db.add_all([ConversationSession(session_id=session_admin, user_id=admin_id, created_at=now,
                                                title="M5.3-B 隔离监控数据", updated_at=now),
                            ConversationSession(session_id=session_normal, user_id=normal_id, created_at=now,
                                                title="M5.3-B 普通用户验收", updated_at=now)])
                statuses = ["SUCCEEDED", "SUCCEEDED", "UNKNOWN_RESULT", "MODEL_UNAVAILABLE"]
                strategies = ["AGENT", "SINGLE", "AGENT", "DIRECT"]
                retrievals = [
                    {"strategy_selected": "BM25", "strategy_executed": "BM25", "degraded": False,
                     "knowledge_version": "synthetic-dashboard-fixture", "evidence_ids": [f"{marker}-chunk-1"]},
                    {"strategy_selected": "HYBRID_RERANK", "strategy_executed": "HYBRID_RERANK", "degraded": False,
                     "knowledge_version": "synthetic-dashboard-fixture", "evidence_ids": [f"{marker}-chunk-2"]},
                    {"strategy_selected": "HYBRID_RERANK", "strategy_executed": "BM25", "degraded": True,
                     "degraded_reason": "EMBEDDING_UNAVAILABLE", "knowledge_version": "synthetic-dashboard-fixture"},
                    {},
                ]
                for index, run_id in enumerate(run_ids):
                    started = now - timedelta(minutes=12 - index * 2)
                    finished = started + timedelta(milliseconds=180 + index * 75)
                    result = {"route": {"strategy": strategies[index]}, "strategy": strategies[index],
                              "retrieval": retrievals[index], "policy_retrieval": retrievals[index],
                              "citations": ([{"chunk_id": retrievals[index]["evidence_ids"][0]}]
                                            if retrievals[index].get("evidence_ids") else []),
                              "handoffs": ([{"handoff_id": f"{marker}-handoff", "target_agent": "policy"}]
                                           if index == 0 else [])}
                    db.add(Run(run_id=run_id, user_id=admin_id, session_id=session_admin,
                        client_request_id=run_id, idempotency_scope=marker, request_hash="c" * 64,
                        status=statuses[index], error_code=("CHECKPOINT_PERSISTENCE_FAILED" if index == 2 else
                            "MODEL_UNAVAILABLE" if index == 3 else None), result_json=json.dumps(result),
                        model_id="test-stub", usage_json=json.dumps({"prompt_tokens": 130 + index,
                            "completion_tokens": 42, "cost_estimate_usd_peak_rate": 0.0004}), latency_ms=180 + index * 75,
                        started_at=started, finished_at=finished, created_at=started, updated_at=finished))
                db.flush()
                db.add(GraphExecutionRecord(run_id=run_ids[0], user_id=admin_id, thread_id=f"{marker}-thread",
                    orchestrator="langgraph", graph_version="m53b-fixture", state_schema_version="fixture-v1",
                    execution_version="fixture-v1", checkpoint_id=None, status="SUCCEEDED", started_at=now,
                    updated_at=now, finished_at=now))
                event_rows = [
                    (run_ids[0], [("started", {}), ("route_decided", {"strategy":"AGENT"}),
                        ("agent_started", {"agent":"policy", "handoff_id":f"{marker}-handoff"}),
                        ("tool_started", {"tool_id":"search_knowledge"}),
                        ("tool_finished", {"tool_id":"search_knowledge", "status":"SUCCEEDED", "duration_ms":21}),
                        ("done", {"status":"SUCCEEDED"})]),
                    (run_ids[1], [("route_decided", {"strategy":"SINGLE"}),
                        ("retrieval_hop", {"strategy_selected":"HYBRID_RERANK", "strategy_executed":"HYBRID_RERANK", "duration_ms":33})]),
                    (run_ids[2], [("route_decided", {"strategy":"AGENT"}),
                        ("error", {"status":"UNKNOWN_RESULT", "error_code":"CHECKPOINT_PERSISTENCE_FAILED"})]),
                    (run_ids[3], [("route_decided", {"strategy":"DIRECT"}),
                        ("error", {"status":"MODEL_UNAVAILABLE", "error_code":"MODEL_UNAVAILABLE"})]),
                ]
                for run_id, events in event_rows:
                    for seq, (event_type, payload) in enumerate(events, 1):
                        db.add(RunEvent(event_id=f"{run_id}-e{seq}", run_id=run_id, session_id=session_admin,
                            trace_id=f"{run_id}-trace", seq=seq, timestamp=now + timedelta(microseconds=seq),
                            event_type=event_type, payload_json=json.dumps(payload), event_key=f"{run_id}:{seq}"))
                for owner_run, node, parent, duration, status in [
                    (run_ids[0], "supervisor", None, 4, "OK"),
                    (run_ids[0], "m3.agent.policy", "supervisor", 21, "OK"),
                    (run_ids[0], "tool.search_knowledge", "m3.agent.policy", 21, "OK"),
                    (run_ids[1], "m2.retrieval.hybrid_rerank", None, 33, "OK"),
                    (run_ids[2], "m2.retrieval.hybrid_rerank", None, 12, "ERROR"),
                ]:
                    span_id = f"{owner_run}-{node.replace('.', '-')}-span"
                    payload = {"trace_id": f"{owner_run}-trace", "span_id": span_id,
                        "parent_span_id": f"{owner_run}-{parent.replace('.', '-')}-span" if parent else None,
                        "run_id": owner_run, "node_name": node, "strategy": None, "start_time": now.isoformat(),
                        "end_time": now.isoformat(), "duration_ms": duration, "status": status,
                        "error_code": "CHECKPOINT_PERSISTENCE_FAILED" if status == "ERROR" else None,
                        "graph_version": "m53b-fixture", "prompt": "REDACTION_SENTINEL"}
                    db.add(ObservabilityRecord(record_id=f"span:{span_id}", run_id=owner_run, user_id=admin_id,
                        trace_id=f"{owner_run}-trace", span_id=span_id, parent_span_id=None, graph_version="m53b-fixture",
                        record_type="span", timestamp=now, payload_json=json.dumps(payload)))
                db.add(ObservabilityRecord(record_id=f"metric:{marker}", run_id=run_ids[0], user_id=admin_id,
                    trace_id=f"{run_ids[0]}-trace", span_id=None, parent_span_id=None, graph_version="m53b-fixture",
                    record_type="metric", timestamp=now, payload_json=json.dumps({"node_name":"test.stub",
                        "model_request_count":1,"prompt_tokens":130,"completion_tokens":42,"estimated_cost_usd":0.0004})))
                db.add_all([
                    Trace(trace_id=f"{marker}-trace-bm25", user_id=admin_id, run_id=run_ids[0],
                        event_type="policy_retrieval_completed", status="COMPLETED", created_at=now,
                        event_json=json.dumps(retrievals[0])),
                    Trace(trace_id=f"{marker}-trace-hybrid", user_id=admin_id, run_id=run_ids[1],
                        event_type="policy_retrieval_completed", status="COMPLETED", created_at=now,
                        event_json=json.dumps(retrievals[1])),
                ])
            created = {"users": 2, "credentials": 2, "sessions": 2, "runs": 4, "graph_executions": 1,
                       "events": sum(len(items) for _, items in event_rows), "spans": 5, "metrics": 1, "traces": 2}
        else:
            with factory.begin() as db:
                # Exact marker-scoped child-first cleanup; no non-probe rows are selected.
                run_rows = db.execute(select(Run.run_id).where(Run.run_id.like(f"{marker}-%"))).scalars().all()
                session_rows = [session_admin, session_normal]
                db.execute(delete(ActionAudit).where(ActionAudit.action_id.like(f"{marker}-%")))
                db.execute(delete(PendingAction).where(PendingAction.action_id.like(f"{marker}-%")))
                db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_rows)))
                db.execute(delete(Trace).where((Trace.run_id.in_(run_rows)) | Trace.trace_id.like(f"{marker}-%")))
                db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_rows)))
                db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_rows)))
                db.execute(delete(Run).where(Run.run_id.in_(run_rows)))
                db.execute(delete(AuthSession).where(AuthSession.user_id.in_([admin_id, normal_id])))
                db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_rows)))
                db.execute(delete(DemoCredential).where(DemoCredential.user_id.in_([admin_id, normal_id])))
                db.execute(delete(User).where(User.user_id.in_([admin_id, normal_id])))
            created = {"cleanup_requested_run_ids": len(run_rows), "session_ids": len(session_rows)}
        with factory() as db:
            run_ids_remaining = db.execute(select(Run.run_id).where(Run.run_id.like(f"{marker}-%"))).scalars().all()
            user_ids = [admin_id, normal_id]
            residual = {
                "users": db.execute(select(User.user_id).where(User.user_id.in_(user_ids))).scalars().all(),
                "credentials": db.execute(select(DemoCredential.user_id).where(DemoCredential.user_id.in_(user_ids))).scalars().all(),
                "sessions": db.execute(select(ConversationSession.session_id).where(ConversationSession.session_id.like(f"{marker}-%"))).scalars().all(),
                "runs": run_ids_remaining,
                "events": db.execute(select(RunEvent.event_id).where(RunEvent.run_id.in_(run_ids_remaining))).scalars().all() if run_ids_remaining else [],
                "graph_executions": db.execute(select(GraphExecutionRecord.run_id).where(GraphExecutionRecord.run_id.in_(run_ids_remaining))).scalars().all() if run_ids_remaining else [],
                "observability": db.execute(select(ObservabilityRecord.record_id).where(ObservabilityRecord.run_id.in_(run_ids_remaining))).scalars().all() if run_ids_remaining else [],
                "traces": db.execute(select(Trace.trace_id).where(Trace.trace_id.like(f"{marker}-%"))).scalars().all(),
            }
        print(json.dumps({"probe_id": marker, "mode": mode, "database": database, "postgres_version": version,
            "alembic_revision": revision, "created_or_selected": created, "residual_counts": {k:len(v) for k,v in residual.items()},
            "deepseek_requests": 0, "business_transactions": 0, "mcp_writes": 0}, ensure_ascii=False))
        if mode == "cleanup" and any(residual.values()):
            raise SystemExit("probe residue remains")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
