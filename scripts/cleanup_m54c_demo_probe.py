"""Remove only the M5.4-C demo session and Run created for delivery validation."""
from __future__ import annotations

import asyncio
import json
import selectors

from sqlalchemy import delete, func, select

from app.m1.db import make_engine, make_session_factory
from app.m1.models import (
    ActionAudit, ConversationSession, GraphExecutionRecord, Message,
    ObservabilityRecord, PendingAction, Run, RunEvent, SessionMemorySummary, Trace,
)
from app.orchestration.checkpointer import checkpoint_conninfo, strict_serializer
from scripts.g2_test_database import resolve_test_database_url

SESSION_ID = "d86e05a0-b898-486d-981c-834aaa286c20"
RUN_ID = "af93e5c6-3e3a-4e15-8ec3-3ac84b657ed2"
OWNER_ID = "u_demo"


def _count(db, model, predicate):
    return int(db.scalar(select(func.count()).select_from(model).where(predicate)) or 0)


def counts(db):
    return {
        "sessions": _count(db, ConversationSession, ConversationSession.session_id == SESSION_ID),
        "messages": _count(db, Message, Message.session_id == SESSION_ID),
        "runs": _count(db, Run, Run.run_id == RUN_ID),
        "run_events": _count(db, RunEvent, RunEvent.run_id == RUN_ID),
        "traces": _count(db, Trace, Trace.run_id == RUN_ID),
        "graph_executions": _count(db, GraphExecutionRecord, GraphExecutionRecord.run_id == RUN_ID),
        "observability_records": _count(db, ObservabilityRecord, ObservabilityRecord.run_id == RUN_ID),
        "memory_summaries": _count(db, SessionMemorySummary, SessionMemorySummary.session_id == SESSION_ID),
        "pending_actions": _count(db, PendingAction, PendingAction.session_id == SESSION_ID),
    }


async def delete_checkpoint():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    url = resolve_test_database_url()
    conninfo = checkpoint_conninfo(url.render_as_string(hide_password=False), "shoprelay_langgraph")
    async with AsyncPostgresSaver.from_conn_string(
        conninfo, pipeline=False, serde=strict_serializer()
    ) as saver:
        await saver.adelete_thread(RUN_ID)


def main():
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("cleanup is restricted to loopback shoprelay_m11_test")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    try:
        with factory() as db:
            session = db.get(ConversationSession, SESSION_ID)
            run = db.get(Run, RUN_ID)
            if session is not None and session.user_id != OWNER_ID:
                raise RuntimeError("session owner mismatch; refusing cleanup")
            if run is not None and (run.user_id != OWNER_ID or run.session_id != SESSION_ID):
                raise RuntimeError("run owner/session mismatch; refusing cleanup")
            before = counts(db)
        asyncio.run(delete_checkpoint(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
        with factory.begin() as db:
            action_ids = select(PendingAction.action_id).where(PendingAction.session_id == SESSION_ID)
            db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
            db.execute(delete(PendingAction).where(PendingAction.session_id == SESSION_ID))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id == SESSION_ID))
            db.execute(delete(RunEvent).where(RunEvent.run_id == RUN_ID))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id == RUN_ID))
            db.execute(delete(Trace).where(Trace.run_id == RUN_ID))
            db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id == RUN_ID))
            db.execute(delete(Run).where(Run.run_id == RUN_ID, Run.user_id == OWNER_ID,
                                         Run.session_id == SESSION_ID))
            db.execute(delete(Message).where(Message.session_id == SESSION_ID))
            db.execute(delete(ConversationSession).where(ConversationSession.session_id == SESSION_ID,
                                                          ConversationSession.user_id == OWNER_ID))
        with factory() as db:
            after = counts(db)
        print(json.dumps({"database": url.database, "session_id": SESSION_ID, "run_id": RUN_ID,
                          "before": before, "after": after,
                          "all_zero": all(v == 0 for v in after.values())}, indent=2))
        if any(after.values()):
            raise SystemExit(2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
