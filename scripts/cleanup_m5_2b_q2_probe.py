"""Exact cleanup for the two M5.2-B-Q2 isolated identities and their run threads."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select

from app.m1.db import make_engine, make_session_factory
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential,
    GraphExecutionRecord, Message, ObservabilityRecord, PendingAction, Run, RunEvent,
    SessionMemorySummary, Trace, User)
from app.orchestration.checkpointer import checkpoint_conninfo, run_checkpointer_async, strict_serializer
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]
USER_IDS = (
    "m52bq2_20261023a_owner", "m52bq2_20261023a_other",
    "m52bq2_20261023b_owner", "m52bq2_20261023b_other",
)
RUN_IDS = ("835c719a-3204-4555-a5da-11ebbce15e39", "c33aa6ab-af3a-4f57-85da-ba4999fa58b4")
SESSION_IDS = ("1fb68fd8-9fc9-4949-9025-7dcefbbd39bd",)


def counts(db) -> dict:
    predicates = {
        "users": (User, User.user_id.in_(USER_IDS)),
        "demo_credentials": (DemoCredential, DemoCredential.user_id.in_(USER_IDS)),
        "auth_sessions": (AuthSession, AuthSession.user_id.in_(USER_IDS)),
        "sessions": (ConversationSession, ConversationSession.session_id.in_(SESSION_IDS)),
        "messages": (Message, Message.session_id.in_(SESSION_IDS)),
        "runs": (Run, Run.run_id.in_(RUN_IDS)),
        "run_events": (RunEvent, RunEvent.run_id.in_(RUN_IDS)),
        "traces": (Trace, Trace.run_id.in_(RUN_IDS)),
        "graph_executions": (GraphExecutionRecord, GraphExecutionRecord.run_id.in_(RUN_IDS)),
        "observability_records": (ObservabilityRecord, ObservabilityRecord.run_id.in_(RUN_IDS)),
        "memory_summaries": (SessionMemorySummary, SessionMemorySummary.session_id.in_(SESSION_IDS)),
        "pending_actions": (PendingAction, PendingAction.owner_id.in_(USER_IDS)),
    }
    result = {name: int(db.scalar(select(func.count()).select_from(model).where(predicate)) or 0)
              for name, (model, predicate) in predicates.items()}
    action_ids = select(PendingAction.action_id).where(PendingAction.owner_id.in_(USER_IDS))
    result["action_audits"] = int(db.scalar(select(func.count()).select_from(ActionAudit)
        .where(ActionAudit.action_id.in_(action_ids))) or 0)
    return result


def delete_database_rows(factory) -> tuple[dict, dict]:
    with factory.begin() as db:
        before = counts(db)
        action_ids = select(PendingAction.action_id).where(PendingAction.owner_id.in_(USER_IDS))
        db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
        db.execute(delete(PendingAction).where(PendingAction.owner_id.in_(USER_IDS)))
        db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(SESSION_IDS)))
        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(RUN_IDS)))
        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(RUN_IDS)))
        db.execute(delete(Trace).where(Trace.run_id.in_(RUN_IDS)))
        db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(RUN_IDS)))
        db.execute(delete(Run).where(Run.run_id.in_(RUN_IDS)))
        db.execute(delete(Message).where(Message.session_id.in_(SESSION_IDS)))
        db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(SESSION_IDS)))
        db.execute(delete(AuthSession).where(AuthSession.user_id.in_(USER_IDS)))
        db.execute(delete(DemoCredential).where(DemoCredential.user_id.in_(USER_IDS)))
        db.execute(delete(User).where(User.user_id.in_(USER_IDS)))
    with factory() as db:
        after = counts(db)
    return before, after


async def delete_checkpoint_threads(database_url: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    conninfo = checkpoint_conninfo(database_url, "shoprelay_langgraph")
    async with AsyncPostgresSaver.from_conn_string(conninfo, pipeline=False,
            serde=strict_serializer()) as saver:
        for run_id in RUN_IDS:
            await saver.adelete_thread(run_id)


def main() -> None:
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Q2 cleanup is restricted to loopback shoprelay_m11_test")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            pre = counts(db)
        run_checkpointer_async(delete_checkpoint_threads(url.render_as_string(hide_password=False)))
        before, after = delete_database_rows(factory)
        evidence_path = ROOT / "docs" / "probes" / "m5.2-b-q2-final-r1.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["cleanup"] = {
            "status": "PASS" if all(value == 0 for value in after.values()) else "FAIL_RESIDUAL_ROWS",
            "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "scope": {"user_ids": list(USER_IDS), "session_ids": list(SESSION_IDS), "run_ids": list(RUN_IDS),
                      "database": url.database, "checkpointer_schema": "shoprelay_langgraph"},
            "database_counts_before": before,
            "database_counts_after": after,
            "all_database_counts_zero": all(value == 0 for value in after.values()),
            "checkpointer_threads_deleted": list(RUN_IDS),
        }
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence["cleanup"], ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
