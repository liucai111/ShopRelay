"""Serializable M1 AgentState contract and a read-only persistence projection.

This is a boundary type for later graph work, not a running LangGraph or agent plan.
Only JSON-compatible values belong in the state; clients, sessions, and credentials do not.
"""
from __future__ import annotations

import json
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.m1.models import Run, RunEvent, Trace
from app.m1.services import read_conversation, read_run, read_run_events
from app.m5.memory import read_session_summary


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str | None = None
    role: str
    content: str
    created_at: str | None = None
    sequence: int | None = None
    status: str = "RECORDED"


class AgentFact(BaseModel):
    """A sourced fact; values may be nested JSON but never live service objects."""
    model_config = ConfigDict(extra="forbid")
    source_id: str
    observed_at: str
    version: str | None = None
    value: JsonValue


class AgentState(BaseModel):
    """Versionable state contract from agent-workflow.md, without graph execution."""
    model_config = ConfigDict(extra="forbid")

    state_version: int = 1
    trace_id: str
    session_id: str
    user_id: str
    run_id: str
    turn_id: str
    run_status: str
    original_query: str = ""
    normalized_query: str = ""
    messages: list[AgentMessage] = Field(default_factory=list)
    summary: str | None = None
    safety_decision: str | None = None
    strategy: str | None = None
    sub_strategy: str | None = None
    route_decision: dict[str, JsonValue] | None = None
    intent_labels: list[str] = Field(default_factory=list)
    plan: list[dict[str, JsonValue]] = Field(default_factory=list)
    task_queue: list[dict[str, JsonValue]] = Field(default_factory=list)
    completed_tasks: list[dict[str, JsonValue]] = Field(default_factory=list)
    order_facts: list[AgentFact] = Field(default_factory=list)
    product_facts: list[AgentFact] = Field(default_factory=list)
    policy_evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    policy_aspects: list[dict[str, JsonValue]] = Field(default_factory=list)
    retrieval_strategy: dict[str, JsonValue] | None = None
    policy_conditions: list[dict[str, JsonValue]] = Field(default_factory=list)
    condition_coverage: dict[str, JsonValue] = Field(default_factory=dict)
    retrieval_hops: list[dict[str, JsonValue]] = Field(default_factory=list)
    grade_result: dict[str, JsonValue] | None = None
    evidence_sufficient: bool | None = None
    stop_reason: str | None = None
    tool_results: list[dict[str, JsonValue]] = Field(default_factory=list)
    pending_action: dict[str, JsonValue] | None = None
    final_answer: str | None = None
    citations: list[dict[str, JsonValue]] = Field(default_factory=list)
    errors: list[dict[str, JsonValue]] = Field(default_factory=list)
    cost_budget: dict[str, JsonValue] = Field(default_factory=dict)
    step_count: int = 0
    handoff_count: int = 0
    business_intent: str | None = None
    current_task: dict[str, JsonValue] | None = None
    agent_results: list[dict[str, JsonValue]] = Field(default_factory=list)
    handoffs: list[dict[str, JsonValue]] = Field(default_factory=list)
    tool_execution_status: list[dict[str, JsonValue]] = Field(default_factory=list)
    agent_budget: dict[str, JsonValue] = Field(default_factory=dict)
    supervisor_status: str | None = None
    knowledge_version: str | None = None


def restore_agent_state(db: Session, owner_id: str, run_id: str) -> AgentState | None:
    """Build the M1 state view from owned session/run/message/event rows.

    This is a projection, not a checkpoint: ephemeral planning fields stay at safe defaults.
    """
    owned = read_run(db, owner_id, run_id)
    if owned is None:
        return None
    run, _ = owned
    conversation_data = read_conversation(db, owner_id, run.session_id)
    if conversation_data is None:
        return None
    _, messages, _ = conversation_data
    events = read_run_events(db, owner_id, run_id, limit=100) or []
    result = json.loads(run.result_json) if run.result_json else {}
    route = next((json.loads(event.payload_json) for event in events if event.event_type == "route_decided"), {})
    citations = result.get("citations", [])
    self_rag = result.get("self_rag") or {}
    policy_retrieval = result.get("policy_retrieval") or {}
    last_event: RunEvent | None = events[-1] if events else None
    last_trace = db.execute(select(Trace).where(Trace.run_id == run_id)
                            .order_by(Trace.created_at.desc(), Trace.trace_id.desc())).scalars().first()
    trace_id = last_event.trace_id if last_event else (last_trace.trace_id if last_trace else run.run_id)
    user_messages = [message for message in messages if message.role == "user"]
    current_message = next((message for message in reversed(user_messages)
                            if run.started_at and message.created_at >= run.started_at
                            and (run.finished_at is None or message.created_at <= run.finished_at)), None)
    original_query = current_message.content if current_message else ""
    error = None
    if run.error_detail_json or run.error_code:
        error = {"code": run.error_code, "detail": json.loads(run.error_detail_json) if run.error_detail_json else {}}
    stored_summary = read_session_summary(db, owner_id=owner_id, session_id=run.session_id)
    return AgentState(
        trace_id=trace_id, session_id=run.session_id, user_id=owner_id,
        run_id=run.run_id, turn_id=run.run_id, run_status=run.status,
        original_query=original_query,
        summary=json.dumps(stored_summary, ensure_ascii=False) if stored_summary else None,
        messages=[AgentMessage(message_id=m.message_id, role=m.role, content=m.content,
                               created_at=m.created_at.isoformat(), sequence=m.sequence, status=m.status)
                  for m in messages],
        strategy=result.get("strategy") or route.get("strategy"),
        sub_strategy=result.get("sub_strategy") or route.get("sub_strategy"),
        route_decision=result.get("route") or route,
        safety_decision=(result.get("route") or route).get("safety_decision"),
        intent_labels=(result.get("route") or route).get("intent_labels", []),
        policy_evidence=citations, final_answer=result.get("answer"), citations=citations,
        policy_aspects=policy_retrieval.get("requested_aspects", []),
        retrieval_strategy={key: policy_retrieval.get(key) for key in
            ("version", "strategy_selected", "strategy_executed", "strategy_trigger_reason",
             "degraded", "knowledge_version", "index_fingerprint", "missing_aspect_ids")
            if key in policy_retrieval} or None,
        policy_conditions=self_rag.get("conditions", []),
        condition_coverage=self_rag.get("condition_coverage", {}),
        retrieval_hops=self_rag.get("hops", policy_retrieval.get("retrieval_calls", [])),
        grade_result=self_rag.get("grade_result"),
        evidence_sufficient=self_rag.get("evidence_sufficient", self_rag.get("sufficient")),
        cost_budget=result.get("run_budget") or result.get("usage") or {},
        stop_reason=self_rag.get("stop_reason", result.get("stop_reason")),
        errors=([error] if error else []), knowledge_version=result.get("knowledge_version"),
        business_intent=result.get("business_intent"), current_task=result.get("current_task"),
        agent_results=result.get("agent_results") or result.get("intermediate_results", []),
        handoffs=result.get("handoffs", []), tool_execution_status=result.get("tool_execution_status", []),
        agent_budget=result.get("agent_budget", {}), supervisor_status=result.get("supervisor_status"),
    )
