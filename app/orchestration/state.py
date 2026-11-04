"""Small, JSON-safe graph projection; authorization stays outside the graph."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable, TypedDict

from pydantic import BaseModel, ConfigDict, Field


def merge_by_key(key: str):
    def reducer(left: list[dict] | None, right: list[dict] | None) -> list[dict]:
        merged = {item[key]: item for item in (left or [])}
        merged.update({item[key]: item for item in (right or [])})
        return list(merged.values())
    return reducer


class GraphInput(BaseModel):
    """Untrusted execution input. Authenticated owner is deliberately absent."""
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=20_000)
    knowledge_version: str | None = None


@dataclass
class GraphRuntimeContext:
    """Per-invocation trusted context; LangGraph does not checkpoint this object."""
    query: str
    # Owner-filtered, unverified memory prompt projection; runtime-only, never checkpointed.
    memory_context: str = ""
    emit_event: Callable[[str, dict, str], None] | None = None
    route_payload: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    model_request_count: int = 0
    retrieval_hop_count: int = 0
    event_sequence: int = 0
    failure_code: str | None = None
    failure_retryable: bool = False
    failure_diagnostic: dict[str, Any] | None = None
    # Server-built context only. It is deliberately excluded from GraphState/checkpoints.
    trusted_agent_context: Any = None
    # Server-side permission callback for ephemeral, non-executed escalation drafts.
    escalation_permission_checker: Callable[[str, str], bool] | None = None
    m3_supervisor_enabled: bool = False
    active_span_id: str | None = None
    # Runtime-only service injection. It is never added to GraphState/checkpoints.
    observability_sink: Any = None
    # Per-invocation only. Never copied into GraphState/checkpoints/Trace.
    tool_result_store: dict[str, dict[str, Any]] | None = None
    tool_execution_records: list[dict[str, Any]] | None = None
    retrieval_service: Any = None
    retrieval_evaluation_at: str | None = None


class GraphState(TypedDict, total=False):
    """Small checkpoint-safe projection. Messages and answer text live outside it."""
    graph_version: str
    run_id: str
    session_id: str
    trace_id: str
    strategy: str | None
    sub_strategy: str | None
    route: dict[str, Any] | None
    answer_kind: str | None
    citations: list[dict[str, Any]]
    evidence: Annotated[list[dict[str, Any]], merge_by_key("chunk_id")]
    policy_retrieval: dict[str, Any] | None
    policy_retrieval_audit: list[dict[str, Any]]
    knowledge_version: str | None
    grade_result: dict[str, Any] | None
    stop_reason: str | None
    budget: dict[str, Any] | None
    run_budget: dict[str, Any] | None
    retrieval_hop_count: int
    model_request_count: int
    usage: dict[str, Any] | None
    estimated_cost_usd: float
    terminal_status: str
    error_code: str | None
    spans: Annotated[list[dict[str, Any]], merge_by_key("span_id")]
    # M3.1 bounded Supervisor projection. No raw query, identity, credentials,
    # business payloads, or full policy text are checkpointed here.
    business_intent: str | None
    current_task: dict[str, Any] | None
    agent_task_cursor: int
    agent_tasks: Annotated[list[dict[str, Any]], merge_by_key("task_id")]
    intermediate_results: Annotated[list[dict[str, Any]], merge_by_key("result_id")]
    handoffs: Annotated[list[dict[str, Any]], merge_by_key("handoff_id")]
    tool_execution_status: Annotated[list[dict[str, Any]], merge_by_key("execution_id")]
    agent_budget: dict[str, Any] | None
    supervisor_status: str | None


def graph_input_state(value: GraphInput | dict, *, graph_version: str = "g2") -> GraphState:
    parsed = value if isinstance(value, GraphInput) else GraphInput.model_validate(value)
    return {
        "graph_version": graph_version,
        "run_id": parsed.run_id,
        "session_id": parsed.session_id,
        "trace_id": parsed.trace_id,
        "strategy": None,
        "sub_strategy": None,
        "route": None,
        "answer_kind": None,
        "citations": [],
        "evidence": [],
        "policy_retrieval": None,
        "policy_retrieval_audit": [],
        "knowledge_version": parsed.knowledge_version,
        "grade_result": None,
        "stop_reason": None,
        "budget": None,
        "run_budget": None,
        "retrieval_hop_count": 0,
        "model_request_count": 0,
        "usage": None,
        "estimated_cost_usd": 0.0,
        "terminal_status": "RUNNING",
        "error_code": None,
        "spans": [],
        "business_intent": None,
        "current_task": None,
        "agent_task_cursor": 0,
        "agent_tasks": [],
        "intermediate_results": [],
        "handoffs": [],
        "tool_execution_status": [],
        "agent_budget": None,
        "supervisor_status": None,
    }


def project_evidence(rows: list[dict] | None) -> list[dict]:
    """Keep references/version metadata; policy text stays in the service layer."""
    fields = ("chunk_id", "evidence_id", "title", "version", "policy_version",
              "policy_document_version", "effective_from", "effective_to", "knowledge_version",
              "index_fingerprint", "evaluation_at", "source_type", "result_refs",
              "retrieval_call_ids", "rank", "score", "score_type")
    return [{key: row[key] for key in fields if key in row} for row in (rows or [])]


def project_agent_state(agent_state: Any) -> dict[str, Any]:
    """Project existing AgentState fields without transferring trusted identity."""
    data = agent_state.model_dump(mode="json") if hasattr(agent_state, "model_dump") else dict(agent_state)
    return {
        "run_id": data["run_id"], "session_id": data["session_id"], "trace_id": data["trace_id"],
        "strategy": data.get("strategy"), "sub_strategy": data.get("sub_strategy"),
        "route": data.get("route_decision"),
        "answer": data.get("final_answer"), "citations": list(data.get("citations") or []),
        "evidence": project_evidence(data.get("policy_evidence") or []),
        "policy_aspects": list(data.get("policy_aspects") or []),
        "retrieval_strategy": data.get("retrieval_strategy"),
        "knowledge_version": data.get("knowledge_version"),
        "grade_result": data.get("grade_result"), "stop_reason": data.get("stop_reason"),
        "terminal_status": data.get("run_status", "UNKNOWN"),
        "error_code": next((item.get("code") for item in data.get("errors", []) if item.get("code")), None),
        "business_intent": data.get("business_intent"),
        "current_task": data.get("current_task"),
        "agent_task_cursor": int(data.get("step_count", 0) or 0),
        "agent_tasks": list(data.get("task_queue") or []),
        "intermediate_results": list(data.get("agent_results") or data.get("completed_tasks") or []),
        "handoffs": list(data.get("handoffs") or []),
        "tool_execution_status": list(data.get("tool_execution_status") or data.get("tool_results") or []),
        "agent_budget": dict(data.get("agent_budget") or data.get("cost_budget") or {}),
        "supervisor_status": data.get("supervisor_status"),
    }
