"""Fail-closed adapter contract and projection for ShopRelay's persisted APIs.

This module intentionally has no HTTP client. In this preparation stage it can
run only with an explicitly synthetic mock transport; live DEV/Frozen transport
registration awaits separate authorization and runtime controls.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
import math
import re
from typing import Any, Mapping, Protocol, Sequence

from .contracts import EvalCase, EvalTrial, LifecycleStatus, redact
from .execution_modes import ExecutionMode, ExecutionModeGate, ModeAuthorizationError


class AdapterContractError(ValueError):
    pass


class AdapterUnknownResult(RuntimeError):
    """A submitted request may have executed; the adapter must not resubmit it."""

    def __init__(self, client_request_id: str, *, reason_code: str) -> None:
        self.client_request_id = client_request_id
        self.reason_code = reason_code
        super().__init__(f"execution outcome unknown ({reason_code}); request was not replayed")


class AdapterPending(RuntimeError):
    pass


class UnsupportedCapability(RuntimeError):
    pass


class ShopRelayTransport(Protocol):
    """Transport shape matching the existing owner-authenticated FastAPI API.

    `synthetic_only=True` is required for this preparation build. A future live
    transport must be separately implemented and reviewed; it is not registered.
    """

    synthetic_only: bool

    def create_session(self, *, title: str) -> Mapping[str, Any]: ...
    def post_chat_stream(self, *, session_id: str, message: str,
                         client_request_id: str) -> Mapping[str, Any]: ...
    def get_run(self, *, run_id: str) -> Mapping[str, Any]: ...
    def get_run_events(self, *, run_id: str, after_seq: int) -> Mapping[str, Any]: ...
    def get_run_execution(self, *, run_id: str) -> Mapping[str, Any]: ...


class SyntheticMockShopRelayTransport:
    """In-memory canned API responses; deliberately contains no HTTP implementation."""

    synthetic_only = True

    def __init__(self, *, session_response: Mapping[str, Any], stream_receipt: Mapping[str, Any],
                 run_responses: Mapping[str, Mapping[str, Any]],
                 event_responses: Mapping[str, Sequence[Mapping[str, Any]]],
                 execution_responses: Mapping[str, Mapping[str, Any]],
                 post_exception: type[Exception] | None = None) -> None:
        self._session_response = dict(session_response)
        self._stream_receipt = dict(stream_receipt)
        self._run_responses = {key: dict(value) for key, value in run_responses.items()}
        self._event_responses = {key: tuple(dict(row) for row in value) for key, value in event_responses.items()}
        self._execution_responses = {key: dict(value) for key, value in execution_responses.items()}
        self._post_exception = post_exception
        self.session_create_count = 0
        self.chat_post_count = 0
        self.get_count = 0

    def create_session(self, *, title: str) -> Mapping[str, Any]:
        self.session_create_count += 1
        return dict(self._session_response)

    def post_chat_stream(self, *, session_id: str, message: str,
                         client_request_id: str) -> Mapping[str, Any]:
        self.chat_post_count += 1
        if self._post_exception is not None:
            raise self._post_exception()
        return dict(self._stream_receipt)

    def get_run(self, *, run_id: str) -> Mapping[str, Any]:
        self.get_count += 1
        try:
            return dict(self._run_responses[run_id])
        except KeyError as exc:
            raise AdapterContractError("mock response has no requested Run") from exc

    def get_run_events(self, *, run_id: str, after_seq: int) -> Mapping[str, Any]:
        self.get_count += 1
        try:
            rows = self._event_responses[run_id]
        except KeyError as exc:
            raise AdapterContractError("mock response has no events for requested Run") from exc
        return {"events": [row for row in rows if not isinstance(row, Mapping) or
                            row.get("seq", -1) > after_seq], "has_more": False}

    def get_run_execution(self, *, run_id: str) -> Mapping[str, Any]:
        self.get_count += 1
        try:
            return dict(self._execution_responses[run_id])
        except KeyError as exc:
            raise AdapterContractError("mock response has no execution for requested Run") from exc


@dataclass(frozen=True)
class RunObservation:
    run_id: str
    session_id: str | None
    raw_status: str
    lifecycle_status: str | None
    router_decisions: tuple[Mapping[str, Any], ...]
    retrieval_hops: tuple[Mapping[str, Any], ...]
    evidence_ids: tuple[str, ...]
    handoffs: tuple[Mapping[str, Any], ...]
    tool_calls: tuple[Mapping[str, Any], ...]
    spans: tuple[Mapping[str, Any], ...]
    event_refs: tuple[Mapping[str, Any], ...]
    graph_execution_status: str | None
    graph_execution_status_source: str
    model_id: str | None
    provider_usage_status: str
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost_usd: float | None
    billed_cost_usd: float | None
    provider_request_count: int | None
    business_outcome: str | None
    business_outcome_status: str
    safety_result: str | None
    safety_result_status: str
    error_code: str | None
    latencies_ms: Mapping[str, float | None]
    answer_available: bool


@dataclass(frozen=True)
class TrialObservation:
    session_id: str
    client_request_id: str
    runs: tuple[RunObservation, ...]
    lifecycle_status: str | None
    lifecycle_status_source: str
    adapter_wall_ms: float

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(item.run_id for item in self.runs)

    def to_eval_trial(self, *, trial_id: str, case_id: str,
                      experiment_id: str, trial_index: int) -> EvalTrial:
        if self.lifecycle_status is None:
            raise AdapterPending("Run is not terminal; do not finalize or replay the Trial")
        return EvalTrial(
            trial_id=trial_id, case_id=case_id, experiment_id=experiment_id,
            trial_index=trial_index, run_ids=self.run_ids,
            lifecycle_status=self.lifecycle_status, model_calls=tuple(
                {"run_id": item.run_id, "model_id": item.model_id,
                 "usage_status": item.provider_usage_status,
                 "granularity": "run_aggregate"}
                for item in self.runs if item.model_id or item.provider_usage_status != "UNKNOWN"
            ),
            tool_calls=tuple({"run_id": item.run_id, **dict(call)}
                             for item in self.runs for call in item.tool_calls),
            raw_result_ref=None,
            error={"code": next((item.error_code for item in self.runs if item.error_code), None)}
                  if any(item.error_code for item in self.runs) else None,
            usage={
                "provider_usage_status": _combined_usage_status(self.runs),
                "prompt_tokens": _sum_known(item.prompt_tokens for item in self.runs),
                "completion_tokens": _sum_known(item.completion_tokens for item in self.runs),
                "external_provider_calls": None,
                "usage_granularity": "run_aggregate; API does not expose per-node request counts",
                "estimated_cost_usd": _sum_known(item.estimated_cost_usd for item in self.runs),
                "billed_cost_usd": None,
            },
            latency_ms={
                "adapter_wall": self.adapter_wall_ms,
                "run_executor_total": _sum_known(item.latencies_ms.get("run_executor_total") for item in self.runs),
                "provider": _sum_known(item.latencies_ms.get("provider") for item in self.runs),
                "retriever": _sum_known(item.latencies_ms.get("retriever") for item in self.runs),
                "tools": _sum_known(item.latencies_ms.get("tools") for item in self.runs),
            },
        )


def _sum_known(values) -> float | int | None:
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _combined_usage_status(runs: Sequence[RunObservation]) -> str:
    statuses = {run.provider_usage_status for run in runs}
    if statuses == {"STUB"}:
        return "STUB"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    if statuses == {"NOT_APPLICABLE"}:
        return "NOT_APPLICABLE"
    return "RECORDED" if "RECORDED" in statuses else "UNKNOWN"


def _envelope(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise AdapterContractError(f"response missing {key} object")
    return value


def _bounded_str(value: Any, max_len: int = 160) -> str | None:
    return value[:max_len] if isinstance(value, str) and value else None


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Project only bounded JSON primitives; never retain arbitrary payload trees."""
    if depth > 2:
        return None
    if isinstance(value, str):
        return _bounded_str(redact(value), 160)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return value if math.isfinite(value) and value >= 0 else None
    if isinstance(value, list):
        return [safe for item in value[:50]
                if (safe := _safe_value(item, depth=depth + 1)) is not None]
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                break
            if not isinstance(key, str) or key not in _SAFE_NESTED_KEYS:
                continue
            safe = _safe_value(item, depth=depth + 1)
            if safe is not None:
                result[key] = safe
        return result
    return None


_SAFE_NESTED_KEYS = frozenset({
    "id", "ref", "evidence_id", "chunk_id", "knowledge_version", "source_id",
    "tool_id", "agent_id", "run_id", "session_id", "handoff_id", "span_id",
    "trace_id", "status", "error_code", "capability", "strategy", "aspect_id",
})


def _safe_fields(source: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    result = {}
    for key in keys:
        if key not in source:
            continue
        safe = _safe_value(source[key])
        if safe is not None:
            result[key] = safe
    return result


_EVENT_FIELDS = {
    "route_decided": ("strategy", "sub_strategy", "decision_source", "latency_ms", "safety_decision"),
    "retrieval_hop": ("hop", "retrieval_index", "aspect_id", "strategy_selected", "strategy_executed",
                      "degraded", "degraded_reason", "fallback_reason", "knowledge_version",
                      "evidence_count", "evidence_ids", "latency_ms", "duration_ms"),
    "agent_started": ("agent_id", "agent_name", "status"),
    "agent_finished": ("agent_id", "agent_name", "status", "duration_ms", "error_code"),
    "tool_started": ("tool_id", "capability", "status"),
    "tool_finished": ("tool_id", "capability", "status", "duration_ms", "error_code",
                      "retrieval_mode", "knowledge_version", "evidence_ids", "retrieval_count", "degraded"),
    "error": ("error_code", "stage", "status"),
    "done": ("status",),
}


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _terminal_status(raw_status: str) -> str | None:
    mapping = {
        "SUCCEEDED": LifecycleStatus.SUCCEEDED.value,
        "FAILED": LifecycleStatus.FAILED.value,
        "MODEL_UNAVAILABLE": LifecycleStatus.MODEL_UNAVAILABLE.value,
        "UNKNOWN_RESULT": LifecycleStatus.UNKNOWN_RESULT.value,
    }
    if raw_status in mapping:
        return mapping[raw_status]
    if raw_status in {"CREATED", "RUNNING"}:
        return None
    raise AdapterContractError(f"unsupported Run status: {raw_status[:64]}")


def _dedupe_events(events: Sequence[Any], *, run_id: str) -> tuple[Mapping[str, Any], ...]:
    by_identity: dict[tuple[str, Any], Mapping[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise AdapterContractError("SSE event must be an object")
        event_run_id = event.get("run_id")
        if event_run_id is not None and event_run_id != run_id:
            raise AdapterContractError("SSE event belongs to a different Run")
        seq = event.get("seq")
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
            raise AdapterContractError("SSE sequence must be a non-negative integer")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            if seq is None:
                raise AdapterContractError("SSE event requires event_id or seq")
            identity = ("seq", seq)
        else:
            identity = ("id", event_id)
        raw_payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else \
                      event.get("data") if isinstance(event.get("data"), Mapping) else {}
        event_type = _bounded_str(event.get("type"), 64)
        normalized = {
            "event_id": _bounded_str(event_id, 128), "seq": seq,
            "type": event_type,
            "timestamp": _bounded_str(event.get("timestamp"), 64),
            "payload": _safe_fields(raw_payload, _EVENT_FIELDS.get(event_type or "", ())),
        }
        prior = by_identity.get(identity)
        if prior is not None and prior != normalized:
            raise AdapterContractError("conflicting duplicate SSE event")
        by_identity[identity] = normalized
    return tuple(sorted(by_identity.values(), key=lambda item: (
        item["seq"] is None, item["seq"] if item["seq"] is not None else 0,
        item["event_id"] or "")))


def map_run_observation(*, run_payload: Mapping[str, Any],
                        event_payloads: Sequence[Mapping[str, Any]],
                        execution_payload: Mapping[str, Any]) -> RunObservation:
    """Map allowlisted fields from the existing Run, events and execution APIs."""
    run = _envelope(run_payload, "run")
    execution = _envelope(execution_payload, "execution")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id or execution.get("run_id") != run_id:
        raise AdapterContractError("Run and execution identifiers do not match")
    events = _dedupe_events(event_payloads, run_id=run_id)
    route_decisions = []
    retrieval_hops = []
    for event in events:
        payload = event["payload"]
        if event["type"] == "route_decided":
            route_decisions.append(dict(payload))
        elif event["type"] == "retrieval_hop":
            retrieval_hops.append(dict(payload))

    retrieval_hops = retrieval_hops[:20]
    evidence_ids: list[str] = []
    for hop in retrieval_hops:
        values = hop.get("evidence_ids")
        if isinstance(values, list):
            evidence_ids.extend(value[:160] for value in values[:50] if isinstance(value, str) and value)
    for row in execution.get("citations", []) if isinstance(execution.get("citations"), list) else []:
        if isinstance(row, Mapping) and isinstance(row.get("chunk_id"), str):
            evidence_ids.append(row["chunk_id"][:160])
    evidence_ids = list(dict.fromkeys(evidence_ids))[:500]

    handoffs = execution.get("handoffs") if isinstance(execution.get("handoffs"), list) else []
    safe_handoffs = tuple(_safe_fields(item, (
        "handoff_id", "source_agent", "target_agent", "required_capabilities", "context_refs",
        "evidence_refs", "status", "error_code"))
        for item in handoffs[:100] if isinstance(item, Mapping))
    tools = execution.get("tool_calls") if isinstance(execution.get("tool_calls"), list) else []
    safe_tools = tuple(_safe_fields(item, (
        "tool_id", "capability", "status", "executed", "duration_ms", "error_code",
        "retrieval_mode", "knowledge_version", "evidence_ids", "retrieval_count", "degraded"))
        for item in tools[:200] if isinstance(item, Mapping))
    spans = execution.get("spans") if isinstance(execution.get("spans"), list) else []
    span_keys = ("trace_id", "span_id", "parent_span_id", "node_name", "strategy", "start_time",
                 "end_time", "duration_ms", "status", "error_code", "graph_version")
    safe_spans = tuple(_safe_fields(item, span_keys)
                       for item in spans[:300] if isinstance(item, Mapping))

    raw_status = run.get("status")
    if not isinstance(raw_status, str):
        raise AdapterContractError("Run status is missing")
    status = _terminal_status(raw_status)
    usage = run.get("usage") if isinstance(run.get("usage"), Mapping) else {}
    model_id = _bounded_str(run.get("model_id"), 128)
    is_stub = bool(model_id and any(tag in model_id.casefold() for tag in ("stub", "mock", "test")))
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    valid_usage = all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                      for value in (prompt, completion))
    usage_status = "STUB" if is_stub else "RECORDED" if valid_usage else "UNKNOWN"
    if is_stub or not valid_usage:
        prompt = completion = None
    cost = usage.get("cost_estimate_usd_peak_rate")
    if cost is None and isinstance(run.get("answer"), Mapping):
        cost = run["answer"].get("estimated_cost_usd")
    estimated_cost = _nonnegative_number(cost)
    if is_stub:
        estimated_cost = None

    route_latency = _nonnegative_number(execution.get("route_latency_ms"))
    run_latency = _nonnegative_number(run.get("latency_ms"))
    retrieval_latency_values = []
    for hop in retrieval_hops:
        value = _nonnegative_number(hop.get("latency_ms"))
        if value is None:
            value = _nonnegative_number(hop.get("duration_ms"))
        if value is not None:
            retrieval_latency_values.append(value)
    tool_latency_values = [_nonnegative_number(item.get("duration_ms")) for item in safe_tools]
    retrieval_meta = execution.get("retrieval") if isinstance(execution.get("retrieval"), Mapping) else {}
    citations = execution.get("citations") if isinstance(execution.get("citations"), list) else []
    safety = next((item.get("safety_decision") for item in route_decisions
                   if item.get("safety_decision") in {"ALLOW", "ISOLATE_INJECTION", "REFUSE"}), None)

    return RunObservation(
        run_id=run_id, session_id=_bounded_str(run.get("session_id"), 128), raw_status=raw_status,
        lifecycle_status=status, router_decisions=tuple(route_decisions),
        retrieval_hops=tuple(retrieval_hops), evidence_ids=tuple(evidence_ids), handoffs=safe_handoffs,
        tool_calls=safe_tools, spans=safe_spans, event_refs=events,
        graph_execution_status=None, graph_execution_status_source="NOT_EXPOSED_BY_OWNER_RUN_API",
        model_id=model_id, provider_usage_status=usage_status,
        prompt_tokens=prompt, completion_tokens=completion,
        estimated_cost_usd=estimated_cost, billed_cost_usd=None, provider_request_count=None,
        business_outcome=None, business_outcome_status="NOT_EXPOSED_BY_CURRENT_RUN_API",
        safety_result=safety, safety_result_status="RECORDED" if safety else "NOT_EXPOSED_OR_NOT_RECORDED",
        error_code=_bounded_str(run.get("error", {}).get("code") if isinstance(run.get("error"), Mapping)
                               else execution.get("error_code"), 64),
        latencies_ms={
            "run_executor_total": run_latency,
            "route": route_latency,
            "retriever": sum(retrieval_latency_values) if retrieval_latency_values else None,
            "tools": sum(value for value in tool_latency_values if value is not None)
                     if any(value is not None for value in tool_latency_values) else None,
            "provider": None,
        },
        answer_available=isinstance(run.get("answer"), Mapping),
    )


class SyntheticShopRelayAdapter:
    """Synthetic mock adapter; no real HTTP or live runtime implementation."""

    def __init__(self, transport: ShopRelayTransport, mode_gate: ExecutionModeGate,
                 *, max_event_pages: int = 10) -> None:
        if type(transport) is not SyntheticMockShopRelayTransport:
            raise ModeAuthorizationError("only the built-in in-memory synthetic transport is enabled in preparation mode")
        if not 1 <= max_event_pages <= 20:
            raise ValueError("event page limit must be between 1 and 20")
        self._transport = transport
        self._mode_gate = mode_gate
        self._max_event_pages = max_event_pages

    def execute_case(self, *, mode: ExecutionMode, grant: Any, case: EvalCase,
                     experiment_id: str, client_request_id: str) -> TrialObservation:
        self._mode_gate.authorize(mode=mode, grant=grant, case=case)
        if mode is not ExecutionMode.SYNTHETIC:
            raise ModeAuthorizationError("only SYNTHETIC is executable in this build")
        message = case.input.get("message")
        if not isinstance(message, str) or not message or case.input.get("fixture_only") is not True:
            raise ModeAuthorizationError("mock adapter accepts only explicitly synthetic fixture input")
        if not experiment_id or not client_request_id:
            raise ValueError("experiment and idempotency identifiers are required")
        return self._submit_chat(message=message,case=case,experiment_id=experiment_id,
            client_request_id=client_request_id)

    def _submit_chat(self,*,message: str,case: EvalCase,experiment_id: str,client_request_id: str) -> TrialObservation:
        started = time.perf_counter()
        session_response = self._transport.create_session(title=f"eval:{experiment_id}:{case.case_id}")
        session = _envelope(session_response, "session")
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AdapterContractError("session creation response has no session_id")
        try:
            receipt = self._transport.post_chat_stream(session_id=session_id, message=message,
                                                      client_request_id=client_request_id)
        except (TimeoutError, ConnectionError) as exc:
            # No POST retry here: the service may have accepted the idempotency key.
            raise AdapterUnknownResult(client_request_id, reason_code=type(exc).__name__) from None
        run_id = _response_run_id(receipt)
        if run_id is None:
            raise AdapterUnknownResult(client_request_id, reason_code="RUN_ID_NOT_RETURNED")
        observation = self.collect_run(run_id=run_id)
        return TrialObservation(session_id=session_id, client_request_id=client_request_id,
                                runs=(observation,), lifecycle_status=observation.lifecycle_status,
                                lifecycle_status_source="OWNER_RUN_STATUS",
                                adapter_wall_ms=(time.perf_counter() - started) * 1000)

    def collect_runs(self, *, run_ids: Sequence[str]) -> tuple[RunObservation, ...]:
        if not run_ids or len(run_ids) > 20 or len(set(run_ids)) != len(run_ids):
            raise ValueError("explicit unique Run IDs required (1..20)")
        # Read-only collection supports Trial -> multiple Run/GraphExecution references.
        return tuple(self.collect_run(run_id=run_id) for run_id in run_ids)

    def collect_run(self, *, run_id: str) -> RunObservation:
        run_response = self._transport.get_run(run_id=run_id)
        event_rows: list[Mapping[str, Any]] = []
        after_seq = 0
        for _ in range(self._max_event_pages):
            page = self._transport.get_run_events(run_id=run_id, after_seq=after_seq)
            rows = page.get("events")
            if not isinstance(rows, list):
                raise AdapterContractError("Run events response has no events list")
            if any(not isinstance(item, Mapping) for item in rows):
                raise AdapterContractError("Run events contain a non-object entry")
            event_rows.extend(rows)
            has_more = page.get("has_more") is True
            seqs = [item.get("seq") for item in rows if isinstance(item.get("seq"), int)
                    and not isinstance(item.get("seq"), bool)]
            if has_more and (not seqs or max(seqs) <= after_seq):
                raise AdapterContractError("event pagination did not advance")
            if seqs:
                after_seq = max(seqs)
            if not has_more:
                break
        else:
            raise AdapterContractError("Run event page limit exceeded")
        execution_response = self._transport.get_run_execution(run_id=run_id)
        return map_run_observation(run_payload=run_response, event_payloads=event_rows,
                                   execution_payload=execution_response)

    def cancel(self, *, run_id: str) -> None:
        # Existing API has no cancellation endpoint. Never emulate cancel by replay or mutation.
        raise UnsupportedCapability("ShopRelay Run cancellation endpoint is not available")

    def cleanup_session(self, *, session_id: str, run_ids: Sequence[str]) -> None:
        # The public API has no owner-scoped session deletion endpoint. A future
        # isolated runner needs an exact-ID cleanup capability; no DB delete is done here.
        cleanup = getattr(self._transport, "cleanup_eval_session", None)
        if not callable(cleanup):
            raise UnsupportedCapability("exact-ID evaluation cleanup endpoint is not available")
        cleanup(session_id=session_id, run_ids=tuple(run_ids))


class FormalShopRelayAdapter(SyntheticShopRelayAdapter):
    """Reuse API collection under a separately signed formal execution claim.

    A deployment must inject its reviewed owner-authenticated transport and live
    target identity verifier. No URL/client, key or formal asset is enabled here.
    """
    def __init__(self,transport: ShopRelayTransport,mode_gate: ExecutionModeGate,*,max_event_pages: int=10):
        if mode_gate.formal_authority is None or not 1<=max_event_pages<=20:
            raise ModeAuthorizationError("formal issuer and bounded collection required")
        self._transport,self._mode_gate,self._max_event_pages=transport,mode_gate,max_event_pages

    def execute_case(self,*,mode: ExecutionMode,grant,case: EvalCase,experiment_id: str,client_request_id: str):
        if mode not in {ExecutionMode.DEV_SMOKE,ExecutionMode.FROZEN_EVAL}:
            raise ModeAuthorizationError("formal adapter requires a formal mode")
        self._mode_gate.authorize(mode=mode,grant=grant,case=case,attempt_id=client_request_id)
        # This hook reads the actual configured target/identity before effects;
        # a constructor's declared label alone is not target verification.
        if (self._transport.verify_execution_subject(self._mode_gate.subject) is not True or
                self._transport.verify_execution_target(self._mode_gate.target) is not True):
            raise ModeAuthorizationError("authenticated formal principal/target mismatch")
        message=case.input.get("message")
        if not isinstance(message,str) or not message: raise AdapterContractError("formal case has no message")
        return self._submit_chat(message=message,case=case,experiment_id=experiment_id,client_request_id=client_request_id)


class DevSmokeShopRelayAdapter:
    """Separately authorized Dev HTTP adapter; never accepts Synthetic grants.

    A per-case budget reservation is held before the Chat POST. Because current
    ShopRelay APIs expose aggregate usage but not an exact per-provider-request
    ledger, request reservations are charged at their conservative upper bound.
    """
    def __init__(self, *, transport, grant_authority, subject: str, budget_ledger,
                 max_event_pages: int = 10, provider_calls_per_run: int = 5,
                 embedding_calls_per_run: int = 2, tokens_per_run: int = 15_000,
                 cost_per_run_usd: float = 0.625) -> None:
        from .dev_http_transport import DevHttpTransport
        from .budget import BudgetStop
        if type(transport) is not DevHttpTransport:
            raise ModeAuthorizationError("DEV_SMOKE requires the dedicated Dev HTTP transport")
        if budget_ledger is None or not subject or not 1 <= max_event_pages <= 20:
            raise ModeAuthorizationError("Dev Smoke requires a subject, bounded event pages, and Budget Ledger")
        if not 1 <= provider_calls_per_run <= 5 or not 0 <= embedding_calls_per_run <= 2:
            raise ModeAuthorizationError("per-Run request reservation exceeds the Dev hard envelope")
        limits = budget_ledger.limits
        if (limits.provider_calls > transport.grant.provider_call_cap or
                limits.embedding_calls > transport.grant.embedding_call_cap or
                limits.total_tokens > transport.grant.token_cap or
                limits.estimated_usd > transport.grant.usd_cap or
                limits.max_duration_seconds > transport.grant.max_duration_seconds or
                limits.max_retries != 0 or limits.max_concurrent_trials != 1):
            raise ModeAuthorizationError("Budget Ledger exceeds the signed Dev grant")
        self._transport, self._authority, self._subject = transport, grant_authority, subject
        self._budget, self._pages = budget_ledger, max_event_pages
        self._provider_reserve, self._embedding_reserve = provider_calls_per_run, embedding_calls_per_run
        self._tokens_reserve, self._cost_reserve = tokens_per_run, cost_per_run_usd
        self._duration_cap = limits.max_duration_seconds
        self._started_at: float | None = None
        self._budget_stop_type = BudgetStop

    def execute_case(self, *, mode: ExecutionMode, grant: Any, case: EvalCase,
                     experiment_id: str, client_request_id: str,
                     evaluation_marker: str) -> TrialObservation:
        from .dev_http_transport import DevAuthorizationError, DevSmokeGrant, DevTransportError
        if mode is not ExecutionMode.DEV_SMOKE or type(grant) is not DevSmokeGrant:
            raise ModeAuthorizationError("DEV_SMOKE requires its own signed grant; Synthetic grants are not accepted")
        self._authority.verify(grant, subject=self._subject, base_url=self._transport.base_url)
        if (case.split != "dev" or case.data_version != grant.asset_version or
                case.input.get("fixture_only") is True):
            raise ModeAuthorizationError("case is outside the authorized, non-synthetic Dev asset")
        if not experiment_id or not re.fullmatch(rf"m61c:{re.escape(evaluation_marker)}:[A-Za-z0-9_.-]{{1,64}}", client_request_id):
            raise ModeAuthorizationError("stable marker-bound client_request_id required")
        message = case.input.get("message")
        if not isinstance(message, str) or not message:
            raise AdapterContractError("Dev case has no message")
        started = time.perf_counter()
        if self._started_at is None:
            self._started_at = started
        elif started - self._started_at >= self._duration_cap:
            raise self._budget_stop_type("Dev Smoke experiment duration budget exhausted")
        trial_id = f"{experiment_id}:{case.case_id}"
        provider_reservation = self._budget.reserve(trial_id=trial_id, category="provider",
            calls=self._provider_reserve, tokens_reserved=self._tokens_reserve,
            estimated_usd_reserved=self._cost_reserve)
        embedding_reservation = None
        if self._embedding_reserve:
            try:
                embedding_reservation = self._budget.reserve(trial_id=trial_id,
                    category="embedding", calls=self._embedding_reserve)
            except Exception:
                self._budget.settle(provider_reservation, status="RELEASE", actual_calls=0,
                                    actual_tokens=0, estimated_usd=0.0)
                raise
        attempted = False
        try:
            self._transport.authenticate()
            marker = evaluation_marker
            session_response = self._transport.create_session(title=f"m6.1-eval:{marker}")
            session = _envelope(session_response, "session")
            session_id = session.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise AdapterContractError("session response has no session_id")
            attempted = True  # Once POST is entered, failures may follow a server-side commit.
            try:
                receipt = self._transport.post_chat_stream(session_id=session_id,
                    message=message, client_request_id=client_request_id)
                attempted = receipt.get("recovered") is not True
                run_id = _response_run_id(receipt)
            except (TimeoutError, ConnectionError, DevTransportError) as exc:
                # A 4xx response is a definite rejection. A timeout, network failure,
                # or 5xx may follow a server-side commit, so resolve by read-only lookup.
                if isinstance(exc, DevTransportError) and exc.status is not None and exc.status < 500:
                    attempted = False
                    raise
                found = self._transport.lookup_run(session_id=session_id,
                                                   client_request_id=client_request_id)
                run_id = found.get("run_id") if found else None
                if not run_id:
                    reason = (f"HTTP_{exc.status}_OUTCOME_UNKNOWN" if isinstance(exc, DevTransportError)
                              and exc.status is not None else "POST_OUTCOME_UNKNOWN")
                    raise AdapterUnknownResult(client_request_id, reason_code=reason) from None
            if not isinstance(run_id, str) or not run_id:
                found = self._transport.lookup_run(session_id=session_id,
                                                   client_request_id=client_request_id)
                run_id = found.get("run_id") if found else None
            if not isinstance(run_id, str) or not run_id:
                raise AdapterUnknownResult(client_request_id, reason_code="RUN_ID_NOT_RETURNED")
            observation = self.collect_run(run_id=run_id)
        except Exception:
            # After a Chat request might have reached the server, retain the full
            # conservative reservation. Pre-POST failures release it.
            if not attempted:
                self._budget.settle(provider_reservation, status="RELEASE", actual_calls=0,
                                    actual_tokens=0, estimated_usd=0.0)
                if embedding_reservation:
                    self._budget.settle(embedding_reservation, status="RELEASE", actual_calls=0,
                                        actual_tokens=0, estimated_usd=0.0)
            else:
                self._budget.settle(provider_reservation, status="COMMIT",
                    actual_calls=self._provider_reserve, actual_tokens=None, estimated_usd=None,
                    call_count_status="CONSERVATIVE_UPPER_BOUND")
                if embedding_reservation:
                    self._budget.settle(embedding_reservation, status="COMMIT",
                        actual_calls=self._embedding_reserve, actual_tokens=None, estimated_usd=0.0,
                        call_count_status="CONSERVATIVE_UPPER_BOUND")
            raise
        # Aggregate tokens may be known; per-node provider/embedding calls are
        # not. Charge the reserved call upper bounds, never infer billing.
        prompt = observation.prompt_tokens
        completion = observation.completion_tokens
        known_tokens = prompt + completion if prompt is not None and completion is not None else None
        if attempted:
            self._budget.settle(provider_reservation, status="COMMIT",
                actual_calls=self._provider_reserve, actual_tokens=known_tokens,
                estimated_usd=observation.estimated_cost_usd,
                call_count_status="CONSERVATIVE_UPPER_BOUND")
            if embedding_reservation:
                self._budget.settle(embedding_reservation, status="COMMIT",
                    actual_calls=self._embedding_reserve, actual_tokens=None, estimated_usd=0.0,
                    call_count_status="CONSERVATIVE_UPPER_BOUND")
        else:
            # A recovered Run did not cause another Provider/Embedding request.
            self._budget.settle(provider_reservation, status="RELEASE", actual_calls=0,
                actual_tokens=0, estimated_usd=0.0)
            if embedding_reservation:
                self._budget.settle(embedding_reservation, status="RELEASE", actual_calls=0,
                    actual_tokens=0, estimated_usd=0.0)
        return TrialObservation(session_id=session_id, client_request_id=client_request_id,
            runs=(observation,), lifecycle_status=observation.lifecycle_status,
            lifecycle_status_source="OWNER_SCOPED_DEV_LOOKUP_OR_RUN_STATUS",
            adapter_wall_ms=(time.perf_counter() - started) * 1000)

    def collect_run(self, *, run_id: str) -> RunObservation:
        run_response = self._transport.get_run(run_id=run_id)
        event_rows: list[Mapping[str, Any]] = []
        after_seq = 0
        for _ in range(self._pages):
            page = self._transport.get_run_events(run_id=run_id, after_seq=after_seq)
            rows = page.get("events")
            if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
                raise AdapterContractError("Run events response is invalid")
            event_rows.extend(rows)
            seqs = [item.get("seq") for item in rows if isinstance(item.get("seq"), int)
                    and not isinstance(item.get("seq"), bool)]
            if page.get("has_more") is True and (not seqs or max(seqs) <= after_seq):
                raise AdapterContractError("event pagination did not advance")
            if seqs:
                after_seq = max(seqs)
            if page.get("has_more") is not True:
                break
        else:
            raise AdapterContractError("event page limit exceeded")
        execution = self._transport.get_run_execution(run_id=run_id)
        return map_run_observation(run_payload=run_response, event_payloads=event_rows,
                                   execution_payload=execution)


def _response_run_id(receipt: Mapping[str, Any]) -> str | None:
    headers = receipt.get("headers") if isinstance(receipt.get("headers"), Mapping) else {}
    header_id = headers.get("X-Run-Id") or headers.get("x-run-id")
    if isinstance(header_id, str) and header_id:
        return header_id
    run_id = receipt.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return None
