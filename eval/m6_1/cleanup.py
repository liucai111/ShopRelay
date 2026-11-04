"""Fail-closed exact-ID cleanup for explicitly marked isolated Dev eval sessions."""
from __future__ import annotations

import re
from sqlalchemy import delete, inspect, select, text

from app.m1.models import (ConversationSession, GraphExecutionRecord, Message,
    ObservabilityRecord, PendingAction, Run, RunEvent, SessionMemorySummary, Trace)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MARKER = re.compile(r"^[a-f0-9]{32}$")
_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class EvalCleanupBlocked(PermissionError):
    pass


def cleanup_exact_eval_session(db, *, owner_id: str, session_id: str,
                               run_ids: tuple[str, ...], marker: str,
                               checkpoint_schema: str,
                               allow_sqlite_test: bool = False) -> dict[str, int]:
    """Delete only one marked owner's terminal session and the exact supplied Runs.

    Caller must independently enforce the default-off feature flag, database target,
    authenticated cleanup-operator allowlist, Origin and CSRF checks.
    """
    if not owner_id or not _ID.fullmatch(session_id) or not _MARKER.fullmatch(marker):
        raise EvalCleanupBlocked("INVALID_CLEANUP_SCOPE")
    if len(run_ids) > 20 or len(set(run_ids)) != len(run_ids) or any(not _ID.fullmatch(x) for x in run_ids):
        raise EvalCleanupBlocked("INVALID_RUN_ID_LIST")
    if not _SCHEMA.fullmatch(checkpoint_schema):
        raise EvalCleanupBlocked("INVALID_CHECKPOINT_SCHEMA")
    dialect = db.get_bind().dialect.name
    if dialect != "postgresql" and not (allow_sqlite_test and dialect == "sqlite"):
        raise EvalCleanupBlocked("POSTGRESQL_REQUIRED")
    if dialect == "postgresql":
        url = db.get_bind().url
        if (url.database != "shoprelay_m11_test" or
                url.host not in {"127.0.0.1", "localhost", "::1"}):
            raise EvalCleanupBlocked("ISOLATED_DEV_DATABASE_REQUIRED")

    session = db.execute(select(ConversationSession).where(
        ConversationSession.session_id == session_id,
        ConversationSession.user_id == owner_id)).scalar_one_or_none()
    if session is None or session.title != f"m6.1-eval:{marker}":
        raise EvalCleanupBlocked("EVAL_SESSION_MARKER_OR_OWNER_MISMATCH")
    rows = db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.run_id)).scalars().all()
    if any(row.user_id != owner_id for row in rows):
        raise EvalCleanupBlocked("FOREIGN_OWNER_RUN_IN_SESSION")
    actual_ids = tuple(sorted(row.run_id for row in rows))
    if actual_ids != tuple(sorted(run_ids)):
        raise EvalCleanupBlocked("RUN_ID_SET_MISMATCH")
    if any(row.client_request_id != f"m61c:{marker}:{row.client_request_id.rsplit(':', 1)[-1]}" for row in rows):
        raise EvalCleanupBlocked("RUN_MARKER_MISMATCH")
    if any(row.status in {"CREATED", "RUNNING"} for row in rows):
        raise EvalCleanupBlocked("NON_TERMINAL_RUN_PRESENT")
    action_count = db.execute(select(PendingAction.action_id).where(
        PendingAction.session_id == session_id).limit(1)).first()
    if action_count:
        raise EvalCleanupBlocked("BUSINESS_ACTION_PRESENT")
    ids = list(actual_ids)

    # If LangGraph was used, all three checkpointer tables must exist before we
    # delete anything. Thread IDs are the exact Run IDs from GraphExecution.
    graph_rows = db.execute(select(GraphExecutionRecord).where(
        GraphExecutionRecord.run_id.in_(ids) if ids else False)).scalars().all()
    if any(row.user_id != owner_id for row in graph_rows):
        raise EvalCleanupBlocked("FOREIGN_OWNER_GRAPH_EXECUTION")
    traces = db.execute(select(Trace).where(Trace.run_id.in_(ids) if ids else False)).scalars().all()
    if any(row.user_id != owner_id for row in traces):
        raise EvalCleanupBlocked("FOREIGN_OWNER_TRACE")
    spans = db.execute(select(ObservabilityRecord).where(
        ObservabilityRecord.run_id.in_(ids) if ids else False)).scalars().all()
    if any(row.user_id != owner_id for row in spans):
        raise EvalCleanupBlocked("FOREIGN_OWNER_SPAN")
    summaries = db.execute(select(SessionMemorySummary).where(
        SessionMemorySummary.session_id == session_id)).scalars().all()
    if any(row.owner_id != owner_id for row in summaries):
        raise EvalCleanupBlocked("FOREIGN_OWNER_MEMORY_SUMMARY")
    inspector = inspect(db.get_bind())
    checkpoint_names = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
    checkpoint_presence = ({name: inspector.has_table(name, schema=checkpoint_schema)
                            for name in checkpoint_names} if dialect == "postgresql" else {})
    checkpoint_tables = dialect == "postgresql" and all(checkpoint_presence.values())
    if graph_rows and not checkpoint_tables:
        raise EvalCleanupBlocked("CHECKPOINT_TABLES_UNAVAILABLE; no data deleted")
    if dialect == "postgresql" and any(checkpoint_presence.values()) and not checkpoint_tables:
        raise EvalCleanupBlocked("CHECKPOINT_SCHEMA_PARTIAL; no data deleted")
    graph_run_ids = {row.run_id for row in graph_rows}
    checkpoint_thread_ids = sorted({row.thread_id for row in graph_rows})
    orphan_run_ids = sorted(set(ids) - graph_run_ids)
    if dialect == "postgresql" and checkpoint_tables and orphan_run_ids:
        orphan_slots = ",".join(":orphan_" + str(i) for i in range(len(orphan_run_ids)))
        orphan_binds = {f"orphan_{i}": value for i, value in enumerate(orphan_run_ids)}
        orphan_checkpoints = sum(db.execute(text(
            f'SELECT count(*) FROM "{checkpoint_schema}"."{table}" WHERE thread_id IN ({orphan_slots})'),
            orphan_binds).scalar_one() for table in checkpoint_names)
        if orphan_checkpoints:
            raise EvalCleanupBlocked("CHECKPOINT_WITHOUT_GRAPH_EXECUTION; no data deleted")

    counts = {
        "messages": db.execute(select(Message.message_id).where(Message.session_id == session_id)).all().__len__(),
        "run_events": db.execute(select(RunEvent.event_id).where(RunEvent.run_id.in_(ids) if ids else False)).all().__len__(),
        "spans": db.execute(select(ObservabilityRecord.record_id).where(
            ObservabilityRecord.user_id == owner_id,
            ObservabilityRecord.run_id.in_(ids) if ids else False)).all().__len__(),
        "traces": db.execute(select(Trace.trace_id).where(Trace.user_id == owner_id,
            Trace.run_id.in_(ids) if ids else False)).all().__len__(),
        "memory_summaries": len(summaries),
        "graph_executions": len(graph_rows),
        "pending_actions": 0,
        "runs": len(rows), "sessions": 1,
    }
    if graph_rows:
        cp_counts = {}
        slots = ",".join(":thread_" + str(i) for i in range(len(checkpoint_thread_ids)))
        binds = {f"thread_{i}": value for i, value in enumerate(checkpoint_thread_ids)}
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cp_counts[table] = db.execute(text(
                f'SELECT count(*) FROM "{checkpoint_schema}"."{table}" WHERE thread_id IN ({slots})'),
                binds).scalar_one() if checkpoint_thread_ids else 0
        counts["checkpoints"] = sum(cp_counts.values())
    try:
        if graph_rows:
            binds = {f"thread_{i}": value for i, value in enumerate(checkpoint_thread_ids)}
            slots = ",".join(f":thread_{i}" for i in range(len(checkpoint_thread_ids)))
            for table in ("checkpoint_writes", "checkpoints", "checkpoint_blobs"):
                db.execute(text(f'DELETE FROM "{checkpoint_schema}"."{table}" WHERE thread_id IN ({slots})'), binds)
        if ids:
            db.execute(delete(Trace).where(Trace.user_id == owner_id, Trace.run_id.in_(ids)))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.user_id == owner_id,
                ObservabilityRecord.run_id.in_(ids)))
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(ids)))
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.owner_id == owner_id,
                SessionMemorySummary.session_id == session_id))
            db.execute(delete(Run).where(Run.user_id == owner_id, Run.session_id == session_id,
                Run.run_id.in_(ids)))
        db.execute(delete(Message).where(Message.session_id == session_id))
        db.execute(delete(ConversationSession).where(ConversationSession.session_id == session_id,
            ConversationSession.user_id == owner_id))
        db.flush()
        remaining = {
            "messages": db.execute(select(Message.message_id).where(Message.session_id == session_id)).all().__len__(),
            "runs": db.execute(select(Run.run_id).where(Run.session_id == session_id,
                Run.user_id == owner_id)).all().__len__(),
            "sessions": db.execute(select(ConversationSession.session_id).where(
                ConversationSession.session_id == session_id, ConversationSession.user_id == owner_id)).all().__len__(),
            "traces": db.execute(select(Trace.trace_id).where(Trace.user_id == owner_id,
                Trace.run_id.in_(ids) if ids else False)).all().__len__(),
            "spans": db.execute(select(ObservabilityRecord.record_id).where(
                ObservabilityRecord.user_id == owner_id,
                ObservabilityRecord.run_id.in_(ids) if ids else False)).all().__len__(),
            "run_events": db.execute(select(RunEvent.event_id).where(
                RunEvent.run_id.in_(ids) if ids else False)).all().__len__(),
            "memory_summaries": db.execute(select(SessionMemorySummary.summary_id).where(
                SessionMemorySummary.owner_id == owner_id,
                SessionMemorySummary.session_id == session_id)).all().__len__(),
            "graph_executions": db.execute(select(GraphExecutionRecord.run_id).where(
                GraphExecutionRecord.user_id == owner_id,
                GraphExecutionRecord.run_id.in_(ids) if ids else False)).all().__len__(),
            "pending_actions": db.execute(select(PendingAction.action_id).where(
                PendingAction.owner_id == owner_id,
                PendingAction.session_id == session_id)).all().__len__(),
        }
        if graph_rows:
            slots = ",".join(f":thread_{i}" for i in range(len(checkpoint_thread_ids)))
            binds = {f"thread_{i}": value for i, value in enumerate(checkpoint_thread_ids)}
            remaining["checkpoints"] = sum(db.execute(text(
                f'SELECT count(*) FROM "{checkpoint_schema}"."{table}" WHERE thread_id IN ({slots})'),
                binds).scalar_one() for table in checkpoint_names) if checkpoint_thread_ids else 0
        if any(remaining.values()):
            raise EvalCleanupBlocked("POST_DELETE_RESIDUALS_PRESENT; transaction rolled back")
        db.commit()
    except Exception:
        db.rollback()
        raise
    counts["remaining_objects"] = 0
    return counts
