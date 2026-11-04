"""Create and precisely remove isolated M5.4-B1 admin-browser fixtures.

The script never prints or persists the temporary password. Set
M54B1_PROBE_PASSWORD for --setup. It only targets resolve_test_database_url().
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select, text

from app.m1.db import make_engine, make_session_factory
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential, GraphExecutionRecord,
                           Message, ObservabilityRecord, PendingAction, Run, RunEvent, SessionMemorySummary,
                           Trace, User, utc_now)
from app.orchestration.admin_observability import aggregate_admin_overview
from app.m1.services import create_conversation, new_id, password_hash
from scripts.g2_test_database import resolve_test_database_url

PROBE_ID = "m54b1_browser_20261024_r1"
USER_ID = "m54b1_browser_admin"
OUT = Path("docs/probes/m5.4-b1-postgres-setup-r1.json")
SNAPSHOT_OUT = Path("docs/probes/m5.4-b1-historical-before-r1.json")


def _ids(factory) -> list[str]:
    with factory() as db:
        return list(db.execute(select(Run.run_id).where(Run.client_request_id.like(f"{PROBE_ID}:%"))).scalars())


def setup(factory, engine) -> dict:
    password = os.environ.get("M54B1_PROBE_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("M54B1_PROBE_PASSWORD must contain at least 12 characters")
    with engine.connect() as connection:
        database = connection.execute(text("select current_database()")).scalar_one()
        if database != "shoprelay_m11_test":
            raise SystemExit("refusing to write outside shoprelay_m11_test")
        pg_version = connection.execute(text("select version()")).scalar_one()
        revision = connection.execute(text("select version_num from alembic_version")).scalar_one()
    with factory() as db:
        if db.get(User, USER_ID) is not None or _ids(factory):
            raise SystemExit("probe fixture already exists; inspect and clean it explicitly before setup")
        db.add(User(user_id=USER_ID, display_name="M5.4-B1 isolated admin probe"))
        db.flush()
        db.add(DemoCredential(user_id=USER_ID, password_hash=password_hash(password), active=True, updated_at=utc_now()))
        db.commit()
        session = create_conversation(db, USER_ID, "M5.4-B1 isolated observability probe")
        now = utc_now()
        cases = [
            # route, explicit trace mode, graph mode, requested, executed, hops, reported latency, wall ms
            ("SINGLE", "legacy", None, "BM25", "BM25", True, None, None),
            ("MULTI", "legacy", None, "RRF", "rrf_reranker", True, None, None),
            ("DIRECT", "legacy", None, None, None, False, None, None),
            ("REFUSE", "legacy", None, None, None, False, None, None),
            ("DIRECT", None, None, None, None, False, None, None),
            ("DIRECT", None, "langgraph", None, None, False, 33, 665),
            ("DIRECT", "legacy", "langgraph", None, None, False, None, None),
            ("SINGLE", "legacy", None, "bm25", "bm25", True, None, None),
        ]
        created = []
        for index, (strategy, trace_executor, graph_executor, requested, executed, include_hops,
                    reported_latency_ms, wall_ms) in enumerate(cases, 1):
            run_id, trace_id = new_id(), new_id()
            effective_wall_ms = wall_ms if wall_ms is not None else 33 * index
            effective_latency_ms = reported_latency_ms if reported_latency_ms is not None else 33 * index
            retrieval = None if requested is None else {
                "strategy_selected": requested, "strategy_executed": executed, "degraded": False,
                "retrieval_count": 2 if strategy == "MULTI" else 1,
            }
            result = {"strategy": strategy, "route": {"strategy": strategy}, "retrieval": retrieval,
                      "citations": [], "source": "SIMULATED_TEST_DATA", "probe_id": PROBE_ID}
            db.add(Run(run_id=run_id, user_id=USER_ID, session_id=session.session_id,
                client_request_id=f"{PROBE_ID}:{index}", idempotency_scope=PROBE_ID,
                request_hash=(str(index) * 64), status="SUCCEEDED", created_at=now - timedelta(seconds=index),
                updated_at=now, started_at=now - timedelta(milliseconds=effective_wall_ms), finished_at=now,
                latency_ms=effective_latency_ms, result_json=json.dumps(result, ensure_ascii=False),
                model_id="m54b1-deterministic-stub", usage_json="{}"))
            db.flush()
            if trace_executor:
                db.add(Trace(trace_id=new_id(), user_id=USER_ID, run_id=run_id,
                    event_type="orchestrator_selected", status="RECORDED",
                    event_json=json.dumps({"orchestrator": trace_executor}), created_at=now))
            if graph_executor:
                db.add(GraphExecutionRecord(run_id=run_id, user_id=USER_ID, thread_id=run_id,
                    orchestrator=graph_executor, graph_version="m54.4-b1-fixture",
                    state_schema_version="m54.4-b1-fixture", execution_version="m54.4-b1-fixture",
                    status="SUCCEEDED", started_at=now - timedelta(milliseconds=effective_wall_ms),
                    updated_at=now, finished_at=now))
            events = [("started", {"status": "RUNNING"}), ("route_decided", {"strategy": strategy})]
            if include_hops:
                if strategy == "SINGLE":
                    hops = [{"retrieval_index": 1, "aspect_id": "single_fixture", "strategy_selected": requested,
                             "strategy_executed": executed, "evidence_count": 2,
                             "evidence_ids": [f"{PROBE_ID}:single:chunk-1", f"{PROBE_ID}:single:chunk-2"],
                             "knowledge_version": "SIMULATED_TEST_KB_V1", "latency_ms": 4,
                             "source": "SIMULATED_TEST_DATA"}]
                else:
                    hops = [
                        {"retrieval_index": 1, "aspect_id": "multi_rrf_fixture", "strategy_selected": "RRF",
                         "strategy_executed": "RRF", "evidence_count": 1,
                         "evidence_ids": [f"{PROBE_ID}:multi:rrf"], "knowledge_version": "SIMULATED_TEST_KB_V1",
                         "latency_ms": 5, "source": "SIMULATED_TEST_DATA"},
                        {"retrieval_index": 2, "aspect_id": "multi_rerank_fixture", "strategy_selected": "rrf_reranker",
                         "strategy_executed": "rrf_reranker", "evidence_count": 1,
                         "evidence_ids": [f"{PROBE_ID}:multi:rerank"], "knowledge_version": "SIMULATED_TEST_KB_V1",
                         "latency_ms": 8, "source": "SIMULATED_TEST_DATA"},
                    ]
                events.extend(("retrieval_hop", item) for item in hops)
            events.extend([("answer", {}), ("done", {"status": "SUCCEEDED"})])
            for seq, (event_type, payload) in enumerate(events, 1):
                db.add(RunEvent(event_id=new_id(), run_id=run_id, session_id=session.session_id,
                    trace_id=trace_id, seq=seq, timestamp=now, event_type=event_type,
                    payload_json=json.dumps(payload, ensure_ascii=False)))
            expected_executor = ("unknown" if trace_executor and graph_executor and trace_executor != graph_executor
                                 else graph_executor or trace_executor or "unknown")
            created.append({"run_id": run_id, "strategy": strategy, "executor_evidence": expected_executor,
                            "expected_retrieval_hops": 1 if strategy == "SINGLE" else 2 if strategy == "MULTI" else 0,
                            "session_id": session.session_id, "reported_latency_ms": effective_latency_ms,
                            "wall_duration_ms": effective_wall_ms})
        db.commit()
    output = {"schema_version": "shoprelay.m5.4-b1.pg-setup.v1", "probe_id": PROBE_ID,
        "database": database, "postgres_version": pg_version, "alembic_revision": revision,
        "user_id": USER_ID, "session_id": session.session_id, "runs": created,
        "data_kind": "SIMULATED_TEST_DATA", "deepseek_http_requests": 0,
        "business_writes": 0, "mcp_writes": 0, "created_rows": {"users": 1, "credentials": 1,
        "sessions": 1, "runs": len(created), "run_events": sum(4 + row["expected_retrieval_hops"] for row in created),
        "traces": sum(1 for row in cases if row[1]),
        "graph_executions": sum(1 for row in cases if row[2])}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def cleanup(factory, engine) -> dict:
    with engine.connect() as connection:
        database = connection.execute(text("select current_database()")).scalar_one()
    if database != "shoprelay_m11_test":
        raise SystemExit("refusing cleanup outside shoprelay_m11_test")
    with factory() as db:
        run_ids = _ids(factory)
        session_ids = list(db.execute(select(ConversationSession.session_id).where(
            ConversationSession.user_id == USER_ID,
            ConversationSession.title == "M5.4-B1 isolated observability probe")).scalars())
        action_ids = list(db.execute(select(PendingAction.action_id).where(
            PendingAction.owner_id == USER_ID,
            PendingAction.run_id.in_(run_ids) if run_ids else False)).scalars())
        before = {"users": db.execute(select(func.count()).select_from(User).where(User.user_id == USER_ID)).scalar_one(),
                  "credentials": db.execute(select(func.count()).select_from(DemoCredential).where(DemoCredential.user_id == USER_ID)).scalar_one(),
                  "auth_sessions": db.execute(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == USER_ID)).scalar_one(),
                  "sessions": len(session_ids), "runs": len(run_ids)}
        if action_ids:
            db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
            db.execute(delete(PendingAction).where(PendingAction.action_id.in_(action_ids)))
        if run_ids:
            db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
            db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids)))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.source_run_id.in_(run_ids)))
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
        if session_ids:
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(session_ids)))
            db.execute(delete(Message).where(Message.session_id.in_(session_ids)))
            db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_ids)))
        db.execute(delete(AuthSession).where(AuthSession.user_id == USER_ID))
        db.execute(delete(DemoCredential).where(DemoCredential.user_id == USER_ID))
        user = db.get(User, USER_ID)
        if user is not None:
            db.execute(delete(User).where(User.user_id == USER_ID))
        db.commit()
        after = {"users": db.execute(select(func.count()).select_from(User).where(User.user_id == USER_ID)).scalar_one(),
                 "credentials": db.execute(select(func.count()).select_from(DemoCredential).where(DemoCredential.user_id == USER_ID)).scalar_one(),
                 "auth_sessions": db.execute(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == USER_ID)).scalar_one(),
                 "sessions": db.execute(select(func.count()).select_from(ConversationSession).where(ConversationSession.user_id == USER_ID)).scalar_one(),
                 "messages": db.execute(select(func.count()).select_from(Message).where(Message.session_id.in_(session_ids))).scalar_one() if session_ids else 0,
                 "runs": db.execute(select(func.count()).select_from(Run).where(Run.client_request_id.like(f"{PROBE_ID}:%"))).scalar_one(),
                 "events": db.execute(select(func.count()).select_from(RunEvent).where(RunEvent.run_id.in_(run_ids))).scalar_one() if run_ids else 0,
                 "traces": db.execute(select(func.count()).select_from(Trace).where(Trace.run_id.in_(run_ids))).scalar_one() if run_ids else 0,
                 "graph_executions": db.execute(select(func.count()).select_from(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids))).scalar_one() if run_ids else 0,
                 "observability": db.execute(select(func.count()).select_from(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids))).scalar_one() if run_ids else 0,
                 "summaries": db.execute(select(func.count()).select_from(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(session_ids))).scalar_one() if session_ids else 0,
                 "actions": db.execute(select(func.count()).select_from(PendingAction).where(PendingAction.owner_id == USER_ID)).scalar_one(),
                 "action_audits": db.execute(select(func.count()).select_from(ActionAudit).where(ActionAudit.owner_id == USER_ID)).scalar_one() if hasattr(ActionAudit, "owner_id") else 0,
                 "checkpoints": 0}
    result = {"database": database, "probe_id": PROBE_ID, "deleted_before": before,
              "deleted_run_count": before["runs"], "remaining": after,
              "residual_zero": all(value == 0 for value in after.values())}
    return result


def historical_snapshot(factory, engine) -> dict:
    """Read-only, aggregate-only snapshot of pre-existing rows (no prompts or user fields)."""
    with engine.connect() as connection:
        database = connection.execute(text("select current_database()")).scalar_one()
        if database != "shoprelay_m11_test":
            raise SystemExit("refusing snapshot outside shoprelay_m11_test")
        pg_version = connection.execute(text("select version()" )).scalar_one()
        revision = connection.execute(text("select version_num from alembic_version")).scalar_one()
    now = utc_now()
    start, end = now - timedelta(days=30), now + timedelta(seconds=1)
    from datetime import datetime
    overview = aggregate_admin_overview(factory, start, end)
    result = {"schema_version": "shoprelay.m5.4-b1.historical-snapshot.v1", "database": database,
        "postgres_version": pg_version, "alembic_revision": revision, "captured_at": now.isoformat(),
        "window": {"from": start.isoformat(), "to": end.isoformat(), "duration_days": 30},
        "preexisting_rows_only": True, "run_counts": overview["run_counts"],
        "latency_ms": overview["latency_ms"], "run_wall_duration_ms": overview["run_wall_duration_ms"],
        "requested_strategy_counts_raw": overview["retrieval"]["requested_strategy_counts"],
        "executed_strategy_counts_raw": overview["retrieval"]["executed_strategy_counts"],
        "requested_strategy_counts_canonical": overview["retrieval"]["canonical_requested_strategy_counts"],
        "executed_strategy_counts_canonical": overview["retrieval"]["canonical_executed_strategy_counts"],
        "raw_canonical_conserved": overview["retrieval"]["canonical_counts_conserve_raw_counts"],
        "mutation_count": 0}
    SNAPSHOT_OUT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("setup", "cleanup", "snapshot"))
    args = parser.parse_args()
    engine = create_engine(resolve_test_database_url(), pool_pre_ping=True)
    factory = make_session_factory(engine)
    try:
        result = setup(factory, engine) if args.mode == "setup" else cleanup(factory, engine) if args.mode == "cleanup" else historical_snapshot(factory, engine)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
