"""Idempotent PostgreSQL persistence for graph ownership and observability records."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run, utc_now
from .observability import MetricRecord, ObservabilitySink, StructuredLog, TraceSpan

STATE_SCHEMA_VERSION = "g2-state-v1"


class GraphExecutionConflict(RuntimeError):
    pass


def begin_graph_execution(db: Session, run: Run, *, graph_version: str, execution_version: str,
                          state_schema_version: str = STATE_SCHEMA_VERSION) -> GraphExecutionRecord:
    prior = db.get(GraphExecutionRecord, run.run_id)
    if prior is not None:
        raise GraphExecutionConflict("graph execution record already exists; automatic graph replay is forbidden")
    now = utc_now()
    record = GraphExecutionRecord(run_id=run.run_id, user_id=run.user_id, thread_id=run.run_id,
        orchestrator="langgraph", graph_version=graph_version, state_schema_version=state_schema_version,
        execution_version=execution_version, checkpoint_id=None, status="RUNNING", error_code=None,
        started_at=now, updated_at=now)
    db.add(record)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise GraphExecutionConflict("graph execution record already exists; automatic graph replay is forbidden") from exc
    db.refresh(record)
    return record


def update_graph_execution(db: Session, run_id: str, *, status: str, checkpoint_id: str | None = None,
                           error_code: str | None = None) -> None:
    if status not in {"RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"}:
        raise ValueError("invalid graph execution status")
    record = db.get(GraphExecutionRecord, run_id)
    if record is None:
        raise GraphExecutionConflict("graph execution mapping is missing")
    if record.status in {"SUCCEEDED", "FAILED", "UNKNOWN"} and status != record.status:
        raise GraphExecutionConflict("terminal graph execution cannot be changed")
    record.status = status
    record.error_code = error_code
    if checkpoint_id is not None:
        record.checkpoint_id = checkpoint_id
    record.updated_at = utc_now()
    if status != "RUNNING":
        record.finished_at = record.updated_at
    db.commit()


class SQLAlchemyObservabilitySink(ObservabilitySink):
    """Per-run append-only sink. Record keys make retrying persistence idempotent."""
    def __init__(self, factory: sessionmaker, *, user_id: str, run_id: str,
                 trace_id: str, graph_version: str) -> None:
        self.factory = factory
        self.user_id = user_id
        self.run_id = run_id
        self.trace_id = trace_id
        self.graph_version = graph_version

    def _record(self, *, record_id: str, record_type: str, payload: dict,
                span_id: str | None = None, parent_span_id: str | None = None,
                timestamp: datetime | None = None) -> None:
        with self.factory() as db:
            if db.get(ObservabilityRecord, record_id) is not None:
                return
            db.add(ObservabilityRecord(record_id=record_id, run_id=self.run_id, user_id=self.user_id,
                trace_id=self.trace_id, span_id=span_id, parent_span_id=parent_span_id,
                graph_version=self.graph_version, record_type=record_type,
                timestamp=timestamp or utc_now(),
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if db.get(ObservabilityRecord, record_id) is None:
                    raise

    def record_span(self, span: TraceSpan) -> None:
        self._record(record_id=f"span:{span.span_id}", record_type="span",
            payload=span.model_dump(mode="json"), span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            timestamp=datetime.fromisoformat(span.start_time.replace("Z", "+00:00")))

    def record_log(self, item: StructuredLog) -> None:
        if item.run_id != self.run_id or item.trace_id != self.trace_id:
            raise ValueError("observability log correlation does not match the sink scope")
        identity = item.span_id or f"{self.run_id}:{item.node}"
        self._record(record_id=f"log:{identity}:{item.event_type}", record_type="log",
            payload=item.model_dump(mode="json"), span_id=item.span_id,
            timestamp=datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")))
        if item.error_code == "TOOL_DATABASE_UNAVAILABLE":
            self._database_alert(False, datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")))

    def record_metrics(self, item: MetricRecord) -> None:
        payload = item.model_dump(mode="json")
        payload.update({"run_id": self.run_id, "trace_id": self.trace_id})
        identity = item.span_id or item.run_status or "run"
        self._record(record_id=f"metric:{self.run_id}:{identity}", record_type="metric",
            payload=payload, span_id=item.span_id, timestamp=utc_now())
        # A completed execution demonstrates dependency recovery, not that an
        # earlier failed user task has retrospectively succeeded.
        if item.run_status == "SUCCEEDED":
            self._database_alert(True, utc_now())

    def _database_alert(self, healthy: bool, at: datetime) -> None:
        """One local dependency rule; state survives sink/process recreation.

        Uses existing append-only logs (no new table, exporter or message bus).
        Sequential execution is the current runtime contract. Each Run can
        contribute at most one fault and one recovery event.
        """
        rule = "tool-database-unavailable-v1"
        with self.factory() as db:
            previous = db.scalar(select(ObservabilityRecord).where(
                ObservabilityRecord.record_id.like("alert:db:%"),
                ObservabilityRecord.record_type == "log",
            ).order_by(ObservabilityRecord.timestamp.desc(), ObservabilityRecord.record_id.desc()).limit(1))
            prior = json.loads(previous.payload_json) if previous is not None else None
        active = prior is not None and prior["active"]
        if healthy and not active:
            return
        last_trigger = datetime.fromisoformat(prior["last_trigger_at"]) if prior else at
        if healthy:
            event, active = "RECOVERED", False
        elif active and (at - last_trigger).total_seconds() < 60:
            event = "SUPPRESSED"
        else:
            event, active, last_trigger = "TRIGGERED", True, at
        self._record(record_id=f"alert:db:{self.run_id}:{'health' if healthy else 'fault'}", record_type="log",
            timestamp=at, payload={"schema_version": "local-alert-v1", "rule_id": rule,
                "run_id": self.run_id, "trace_id": self.trace_id, "node": "dependency_monitor",
                "event_type": "dependency_alert", "event": event, "active": active,
                "last_trigger_at": last_trigger.isoformat(), "cooldown_seconds": 60,
                "error_code": None if healthy else "TOOL_DATABASE_UNAVAILABLE"})


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def aggregate_metrics(factory: sessionmaker, start: datetime, end: datetime, *, limit: int = 5000) -> dict:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("metrics range must be ordered timezone-aware UTC timestamps")
    with factory() as db:
        rows = db.execute(select(ObservabilityRecord).where(
            ObservabilityRecord.record_type == "metric",
            ObservabilityRecord.timestamp >= start.astimezone(timezone.utc),
            ObservabilityRecord.timestamp < end.astimezone(timezone.utc),
        ).order_by(ObservabilityRecord.timestamp).limit(limit + 1)).scalars().all()
        if len(rows) > limit:
            raise ValueError("metrics range is too large; narrow the requested time window")
        records = [json.loads(row.payload_json) for row in rows]
        executions = db.execute(select(GraphExecutionRecord).where(
            GraphExecutionRecord.finished_at.is_not(None), GraphExecutionRecord.finished_at >= start,
            GraphExecutionRecord.finished_at < end)).scalars().all()
    durations = [float(value) for record in records for value in record.get("node_duration_ms", [])]
    return {
        "from": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "to": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "record_count": len(records),
        "node_execution_count": sum(int(record.get("node_execution_count", 0)) for record in records),
        "node_error_count": sum(int(record.get("node_error_count", 0)) for record in records),
        "node_duration_p50_ms": round(median(durations), 3) if durations else None,
        "node_duration_p95_ms": _percentile(durations, .95),
        "model_request_count": sum(int(record.get("model_request_count", 0)) for record in records),
        "prompt_tokens": sum(int(record.get("prompt_tokens", 0)) for record in records),
        "completion_tokens": sum(int(record.get("completion_tokens", 0)) for record in records),
        "estimated_cost_usd": round(sum(float(record.get("estimated_cost_usd", 0)) for record in records), 9),
        "retrieval_hop_count": sum(int(record.get("retrieval_hop_count", 0)) for record in records),
        "run_terminal_counts": {status: sum(item.status == status for item in executions)
                                for status in ("SUCCEEDED", "FAILED", "UNKNOWN")},
    }
