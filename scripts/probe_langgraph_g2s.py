"""Bounded real DeepSeek SINGLE/MULTI smoke through the LangGraph API.

Only uses two fixed APPROVED dev cases, the existing loopback test database,
and the already provisioned Checkpointer schema. No migrations or setup run here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, CSRF_COOKIE, create_app
from app.m1.chat import DeepSeekProvider, PRICE_SOURCE
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import GraphExecutionRecord, Message, Run, RunEvent, Trace, User
from app.m1.services import set_demo_password
from app.m1.state import restore_agent_state
from app.orchestration.checkpointer import checkpoint_schema_ready, open_checkpointer, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs" / "probes" / "m2.4-q3-approved-dev-smoke-input-r1.json"
CASE_IDS = ("pr2-refund_pre_ship-01", "pr2-multi_clause_return_refund-03")
MAX_REAL_REQUESTS = 8
MAX_Q3_REAL_REQUESTS = 16
EXPECTED_REVISION = "20261020_05"
EXPECTED_REVIEWED_DATASET_SHA256 = "0daea3b85052e34217e5249211bbd7cf3c51d3fce871e2f00c0717e3632acf52"
PRICE_INPUT_USD_PER_M = 0.30
PRICE_CACHED_INPUT_USD_PER_M = 0.006
PRICE_OUTPUT_USD_PER_M = 1.20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_approved_cases(path: Path = DATASET) -> tuple[list[dict[str, Any]], str, str]:
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "shoprelay.q3-approved-dev-smoke-input.v1":
        raise ValueError("G2-S requires the fixed approved-dev-only Q3 smoke manifest")
    if manifest.get("test_candidate_rows_included") is not False:
        raise ValueError("Q3 smoke manifest must explicitly exclude test_candidate rows")
    if manifest.get("source_dataset_sha256") != EXPECTED_REVIEWED_DATASET_SHA256:
        raise ValueError("Q3 smoke manifest does not match the approved reviewed-dataset SHA")
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("approved-dev-only manifest has no cases array")
    by_id = {case.get("case_id"): case for case in cases if isinstance(case, dict)}
    selected: list[dict[str, Any]] = []
    for case_id, strategy in zip(CASE_IDS, ("SINGLE", "MULTI"), strict=True):
        case = by_id.get(case_id)
        if (case is None or case.get("dataset_split") != "dev" or case.get("test_candidate_only")
                or case.get("frozen") or case.get("review_status") != "APPROVED"
                or case.get("scorable") is not True or case.get("expected_strategy") != strategy
                or not isinstance(case.get("user_query"), str) or not case["user_query"].strip()
                or not case.get("acceptable_evidence_ids")):
            raise ValueError(f"fixed G2-S case failed approved dev validation: {case_id}")
        selected.append(case)
    return selected, manifest["source_dataset_sha256"], hashlib.sha256(raw).hexdigest()


def prior_q3_real_request_count(probe_dir: Path = ROOT / "docs" / "probes") -> int:
    """Count only immutable Q3 runs tied to this approved-dev-only manifest."""
    total = 0
    for path in probe_dir.glob("langgraph-g2s-real-*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        dataset = record.get("dataset") or {}
        if dataset.get("probe_manifest") != str(DATASET.relative_to(ROOT)):
            continue
        count = (record.get("request_totals") or {}).get("request_count")
        if not isinstance(count, int) or count < 0:
            raise ValueError("a prior Q3 smoke record has no valid request count; budget cannot be proven")
        total += count
    return total


class RequestLedger:
    """Thread-safe HTTP request cap and privacy-minimized provider ledger."""

    def __init__(self, maximum: int = MAX_REAL_REQUESTS):
        self.maximum = maximum
        self.records: list[dict[str, Any]] = []
        self.denied_attempts = 0
        self.lock = threading.Lock()
        self.node = threading.local()
        self.resource_class = None
        self.original_create = None

    def reserve_for_test(self, node: str = "test") -> int:
        """Reserve a request slot; the ninth reservation fails before a provider send."""
        with self.lock:
            if len(self.records) >= self.maximum:
                self.denied_attempts += 1
                raise RuntimeError("G2-S total real request budget exhausted before HTTP send")
            sequence = len(self.records) + 1
            self.records.append({"sequence": sequence, "node": node, "status": "IN_FLIGHT"})
            return sequence

    @staticmethod
    def _usage(response: Any) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return None
        result = {key: int(usage[key]) for key in
                  ("prompt_tokens", "completion_tokens", "total_tokens")
                  if isinstance(usage.get(key), (int, float))}
        for source_name in ("prompt_tokens_details", "completion_tokens_details"):
            details = usage.get(source_name)
            if isinstance(details, dict):
                safe = {key: int(details[key]) for key in
                        ("cached_tokens", "reasoning_tokens", "audio_tokens")
                        if isinstance(details.get(key), (int, float))}
                if safe:
                    result[source_name] = safe
        return result or None

    @staticmethod
    def _cost(usage: dict[str, Any] | None) -> float | None:
        if not usage or not isinstance(usage.get("prompt_tokens"), int) or not isinstance(usage.get("completion_tokens"), int):
            return None
        prompt = usage["prompt_tokens"]
        output = usage["completion_tokens"]
        cached = min(prompt, int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)))
        return round(((prompt - cached) * PRICE_INPUT_USD_PER_M
                      + cached * PRICE_CACHED_INPUT_USD_PER_M
                      + output * PRICE_OUTPUT_USD_PER_M) / 1_000_000, 12)

    def install(self, provider: DeepSeekProvider) -> None:
        for method_name in ("answer", "answer_with_requirements", "classify_route", "extract_conditions", "grade_evidence"):
            method = getattr(provider, method_name, None)
            if not callable(method):
                continue

            def wrapped(*args, _method=method, _name=method_name, **kwargs):
                prior = getattr(self.node, "name", None)
                self.node.name = _name
                try:
                    return _method(*args, **kwargs)
                finally:
                    self.node.name = prior

            setattr(provider, method_name, wrapped)

        self.resource_class = type(provider.client.chat.completions)
        self.original_create = self.resource_class.create

        def counted_create(resource, *args, **kwargs):
            sequence = self.reserve_for_test(getattr(self.node, "name", "unknown"))
            record = self.records[sequence - 1]
            record["model_requested"] = kwargs.get("model")
            started = time.perf_counter()
            try:
                response = self.original_create(resource, *args, **kwargs)
            except Exception as exc:
                record.update({"status": "ERROR", "exception_type": type(exc).__name__,
                               "http_status": getattr(exc, "status_code", None),
                               "latency_ms": round((time.perf_counter() - started) * 1000)})
                raise
            usage = self._usage(response)
            try:
                finish_reason = response.choices[0].finish_reason
            except (AttributeError, IndexError, TypeError):
                finish_reason = None
            record.update({"status": "RESPONSE", "model_returned": getattr(response, "model", None),
                           "request_id": getattr(response, "_request_id", None),
                           "finish_reason": finish_reason, "usage": usage,
                           "latency_ms": round((time.perf_counter() - started) * 1000),
                           "estimated_cost_usd_peak_rate": self._cost(usage)})
            return response

        setattr(self.resource_class, "create", counted_create)

    def uninstall(self) -> None:
        if self.resource_class is not None and self.original_create is not None:
            setattr(self.resource_class, "create", self.original_create)

    def totals(self) -> dict[str, Any]:
        usages = [row["usage"] for row in self.records if isinstance(row.get("usage"), dict)]
        costs = [row["estimated_cost_usd_peak_rate"] for row in self.records
                 if isinstance(row.get("estimated_cost_usd_peak_rate"), (int, float))]
        return {"request_count": len(self.records),
                "requests_with_usage": len(usages),
                "requests_without_usage": len(self.records) - len(usages),
                "prompt_tokens": sum(item.get("prompt_tokens", 0) for item in usages),
                "completion_tokens": sum(item.get("completion_tokens", 0) for item in usages),
                "estimated_cost_usd_peak_rate": round(sum(costs), 12) if len(costs) == len(self.records) else None,
                "requests_denied_before_send_by_budget": self.denied_attempts,
                "sdk_max_retries": 0}


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in re.split(r"\r?\n\r?\n", body):
        event_type = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if event_type and data:
            payload = json.loads(data)
            events.append({"type": event_type, "event_id": payload.get("event_id"),
                           "run_id": payload.get("run_id"), "session_id": payload.get("session_id"),
                           "trace_id": payload.get("trace_id"), "seq": payload.get("seq"),
                           "payload": payload.get("payload") or {}})
    return events


def _collect_ids(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            normalized = child_key.lower()
            if normalized in {"chunk_id", "evidence_id"} and isinstance(child, str):
                found.add(child)
            elif normalized in {"chunk_ids", "evidence_ids", "retrieved_evidence_ids"} and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            found.update(_collect_ids(child, normalized))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_ids(child, key))
    return found


def _retrieved_ids(retrieval_calls: list[dict[str, Any]], events: list[dict[str, Any]]) -> set[str]:
    """Combine IDs directly observed from the service and persisted hop events."""
    found = {item for call in retrieval_calls for item in (call.get("evidence_ids") or [])
             if isinstance(item, str)}
    found.update(_collect_ids([event.get("payload") for event in events if event.get("type") == "retrieval_hop"]))
    return found


def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
    csrf_response = client.get("/api/demo/csrf")
    csrf = csrf_response.json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf},
                           json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError("G2-S synthetic demo login failed")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get(CSRF_COOKIE, "")}


async def _checkpoint_read(database_url: str, schema: str, run_id: str, query: str,
                           answer: str | None) -> dict[str, Any]:
    async with open_checkpointer(database_url, schema) as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})
    if saved is None:
        raise AssertionError("checkpoint was not readable after application recreation")
    values = saved.checkpoint.get("channel_values", {})
    serialized = json.dumps(values, ensure_ascii=False, default=str)
    return {"read_after_app_recreation": True,
            "checkpoint_id": saved.config["configurable"].get("checkpoint_id"),
            "query_in_checkpoint": query in serialized,
            "full_answer_in_checkpoint": bool(answer and answer in serialized),
            "terminal_status": values.get("terminal_status")}


def _db_snapshot(engine, user_id: str, run_id: str) -> dict[str, Any]:
    factory = make_session_factory(engine)
    with factory() as db:
        run = db.get(Run, run_id)
        graph = db.get(GraphExecutionRecord, run_id)
        if run is None or graph is None or run.user_id != user_id or graph.user_id != user_id:
            raise AssertionError("Run or graph execution is missing or not owner-scoped")
        events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)).scalars().all()
        traces = db.execute(select(Trace).where(Trace.run_id == run_id)).scalars().all()
        messages = db.execute(select(Message).where(Message.session_id == run.session_id).order_by(Message.sequence)).scalars().all()
        state = restore_agent_state(db, user_id, run_id)
        result = json.loads(run.result_json) if run.result_json else None
        return {"run_id": run.run_id, "session_id": run.session_id, "user_id": run.user_id,
                "run_status": run.status, "error_code": run.error_code,
                "error_detail": json.loads(run.error_detail_json) if run.error_detail_json else None,
                "result": result, "model_id": run.model_id,
                "usage": json.loads(run.usage_json) if run.usage_json else None,
                "latency_ms": run.latency_ms,
                "graph": {"orchestrator": graph.orchestrator, "graph_version": graph.graph_version,
                          "state_schema_version": graph.state_schema_version,
                          "execution_version": graph.execution_version, "checkpoint_id": graph.checkpoint_id,
                          "status": graph.status, "error_code": graph.error_code},
                "events": [{"type": row.event_type, "event_id": row.event_id, "seq": row.seq,
                            "trace_id": row.trace_id, "payload": json.loads(row.payload_json)} for row in events],
                "trace_events": [{"event_type": row.event_type, "status": row.status, "trace_id": row.trace_id}
                                 for row in traces],
                "message_roles": [row.role for row in messages],
                "agent_state": state.model_dump(mode="json") if state else None}


def _run_case(case: dict[str, Any], database_url: str, settings, provider: DeepSeekProvider,
              request_ledger: RequestLedger) -> dict[str, Any]:
    user_id = "g2s_" + uuid.uuid4().hex[:20]
    password = "g2s-" + secrets.token_urlsafe(24)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with factory() as db:
        db.add(User(user_id=user_id, display_name="G2-S isolated smoke"))
        db.commit()
        set_demo_password(db, user_id, password)
    request_id = "g2s-" + uuid.uuid4().hex
    payload = {"message": case["user_query"], "client_request_id": request_id}
    result: dict[str, Any] = {"case_id": case["case_id"], "dataset_split": case["dataset_split"],
        "review_status": case["review_status"], "expected_strategy": case["expected_strategy"],
        "user_query": case["user_query"], "expected_conclusion": case.get("expected_conclusion"),
        "policy_version": case.get("policy_version"), "knowledge_version": case.get("knowledge_version"),
        "evaluation_at": case.get("evaluation_at"), "acceptable_evidence_ids": case.get("acceptable_evidence_ids"),
        "user_id": user_id, "client_request_id": request_id, "status": "NOT_RUN"}
    engine.dispose()

    # Each context is a separate FastAPI app/engine lifetime, used as an application restart boundary.
    first_engine = make_engine(database_url)
    first_app = create_app(database_url=database_url, engine=first_engine, chat_provider=provider,
                            orchestrator_mode="langgraph", retrieval_evaluation_at=case.get("evaluation_at"))
    retrieval_records: list[dict[str, Any]] = []
    retrieval_service = first_app.state.retrieval_service
    original_retrieve = retrieval_service.retrieve

    def capture_retrieval(*args, **kwargs):
        found = original_retrieve(*args, **kwargs)
        retrieval_records.append({"evidence_ids": sorted(_collect_ids(found.get("evidence") or [])),
                                 "knowledge_version": found.get("knowledge_version"),
                                 "index_fingerprint": found.get("index_fingerprint"),
                                 "mode": found.get("mode"), "degraded": found.get("degraded", False)})
        return found

    retrieval_service.retrieve = capture_retrieval
    before_calls = len(request_ledger.records)
    try:
        with TestClient(first_app) as client:
            readiness = client.get("/health/ready")
            if readiness.status_code != 200 or readiness.json().get("status") != "ready":
                raise RuntimeError("LangGraph API readiness failed before model request")
            headers = _login(client, user_id, password)
            started_at = time.perf_counter()
            response = client.post("/api/chat/stream", headers=headers, json=payload)
            elapsed = round((time.perf_counter() - started_at) * 1000)
            initial_events = parse_sse(response.text)
            result.update({"http_status": response.status_code, "end_to_end_latency_ms": elapsed,
                           "sse_events": [{key: item.get(key) for key in
                               ("type", "event_id", "run_id", "session_id", "trace_id", "seq")} for item in initial_events],
                           "sse_event_types": [item["type"] for item in initial_events],
                           "request_count_delta": len(request_ledger.records) - before_calls,
                           "retrieval_calls": retrieval_records})
            run_ids = {item.get("run_id") for item in initial_events if item.get("run_id")}
            if response.status_code != 200 or len(run_ids) != 1:
                result["status"] = "FAIL"
                result["failure_code"] = "SSE_REQUEST_OR_RUN_ID_INVALID"
                result["error_response"] = response.text[:2000] if response.status_code != 200 else None
                return result
            run_id = next(iter(run_ids))
            result["run_id"] = run_id
            result["trace_id"] = next((item.get("trace_id") for item in initial_events if item.get("trace_id")), None)
            event_types = [item["type"] for item in initial_events]
            route_event = next((item for item in initial_events if item["type"] == "route_decided"), None)
            route_payload = (route_event or {}).get("payload") or {}
            route_body = route_payload.get("route") if isinstance(route_payload.get("route"), dict) else route_payload
            result["routed_strategy"] = route_body.get("strategy")
            event_order_ok = not ("started" not in event_types or "route_decided" not in event_types
                    or "agent_started" not in event_types or "retrieval_hop" not in event_types
                    or "answer" not in event_types or "done" not in event_types
                    or event_types.index("started") > event_types.index("route_decided")
                    or event_types.index("route_decided") > event_types.index("agent_started")
                    or event_types.index("retrieval_hop") > event_types.index("answer")
                    or event_types.index("answer") > event_types.index("done"))
            result["sse_required_event_order_valid"] = event_order_ok
            snapshot = _db_snapshot(first_engine, user_id, run_id)
            first_engine.dispose()
            result.update({"run_status": snapshot["run_status"], "stored_error_code": snapshot["error_code"],
                           "stored_error_detail": snapshot["error_detail"],
                           "graph": snapshot["graph"], "trace_events": snapshot["trace_events"],
                           "message_roles": snapshot["message_roles"]})
            run_answer = snapshot["result"] or {}
            self_rag = run_answer.get("self_rag") if isinstance(run_answer.get("self_rag"), dict) else {}
            generated_conditions = self_rag.get("conditions") or []
            source_counts: dict[str, int] = {}
            for condition in generated_conditions:
                source = condition.get("source_type", "UNKNOWN") if isinstance(condition, dict) else "UNKNOWN"
                source_counts[source] = source_counts.get(source, 0) + 1
            grade = self_rag.get("grade_result") or {}
            grade_body = grade.get("grade") if isinstance(grade, dict) else {}
            gold_required_count = case.get("approved_gold_required_condition_count")
            if not isinstance(gold_required_count, int) or gold_required_count < 1:
                gold_required_count = sum(bool(item.get("required")) for item in case.get("required_conditions", []))
            result.update({"strategy": run_answer.get("strategy"), "answer_kind": run_answer.get("answer_kind"),
                           "answer": run_answer.get("answer"), "citations": run_answer.get("citations"),
                           "grade_result": run_answer.get("grade_result"), "stop_reason": run_answer.get("stop_reason"),
                           "usage": run_answer.get("usage"), "latency_ms": run_answer.get("latency_ms"),
                           "estimated_cost_usd": run_answer.get("estimated_cost_usd"),
                           "retrieval_hop_count": run_answer.get("retrieval_hop_count"),
                           "self_rag_condition_summary": ({
                               "condition_analysis_version": self_rag.get("condition_analysis_version"),
                               "request_mode": self_rag.get("request_mode"),
                               "condition_count": len(generated_conditions),
                               "required_condition_count": sum(bool(item.get("required")) for item in generated_conditions
                                                                if isinstance(item, dict)),
                               "required_policy_rule_count": sum(bool(item.get("required")) and item.get("kind") == "POLICY_RULE"
                                                                  for item in generated_conditions if isinstance(item, dict)),
                               "source_type_counts": source_counts,
                               "approved_gold_required_condition_count": gold_required_count,
                               "grade_sufficient": grade_body.get("sufficient") if isinstance(grade_body, dict) else None,
                               "grade_missing_aspects": (grade_body.get("missing_aspect") or [])
                                                         if isinstance(grade_body, dict) else [],
                               "stop_reason": self_rag.get("stop_reason"),
                               "hop_count": len(self_rag.get("hops") or [])}
                               if self_rag else None)})
            result["condition_scope_audit"] = self_rag.get("condition_scope_audit") or []
            result["sufficiency_audit"] = self_rag.get("sufficiency_audit") or []
            result["answer_evidence_audit"] = self_rag.get("answer_evidence_audit") or []
            result["answer_context_notes"] = self_rag.get("answer_context_notes") or []
            result["condition_extraction_raw_output"] = self_rag.get("condition_extraction_raw_output")
            result["grade_hops_raw"] = self_rag.get("hops") or []
            result["answer_raw_output"] = (self_rag.get("completion") or {}).get("raw_content")
            result["required_policy_rules"] = [item for item in generated_conditions
                if isinstance(item, dict) and item.get("kind") == "POLICY_RULE" and item.get("required")]
            if case["case_id"] == "pr2-multi_clause_return_refund-03":
                expected_sources = set(case.get("acceptable_evidence_ids") or [])
                actual_sources = {item.get("source_evidence_id") for item in result["required_policy_rules"]}
                answer_text = result.get("answer") or ""
                from app.m2.self_rag import _unrequested_qualifier_codes
                forbidden_adjacent = _unrequested_qualifier_codes(answer_text, case["user_query"])
                approval_uncertain = bool(re.search(
                    r"(?:是否获批|是否批准|审批结果).{0,16}(?:需|待|仍要|尚需|尚待).{0,10}(?:人工)?审核|"
                    r"(?:人工审核|审核).{0,14}(?:结果|是否获批).{0,12}(?:尚未确定|未确定|仍需确认)|"
                    r"不保证.{0,8}(?:获批|批准)", answer_text))
                timing_unconfirmed = bool(re.search(
                    r"(?:没有说明|未说明|没有提供|未提供).{0,20}(?:多久|时长|时间|间隔).{0,12}(?:质量问题|瑕疵|发生)|"
                    r"(?:不能|无法|尚不能|不足以).{0,12}(?:判断|确定|确认).{0,20}(?:30天|期限|时限|窗口|订单|个案)|"
                    r"不能据此(?:判断|确定|确认)", answer_text))
                approval_before_refund = bool(re.search(
                    r"退货申请.{0,12}(?:已批准|获批|批准).{0,18}(?:后|再).{0,12}(?:申请退款|提出退款申请)|"
                    r"(?:申请退款|提出退款申请).{0,14}(?:前|须以).{0,10}退货申请.{0,10}(?:已批准|获批|批准)",
                    answer_text))
                result["q3_scope_acceptance"] = {
                    "status": "PASS" if (
                        actual_sources == expected_sources and len(result["required_policy_rules"]) == gold_required_count
                        and len(result["answer_evidence_audit"]) == gold_required_count
                        and all(item.get("status") == "REQUIRED_EXCERPT_INCLUDED"
                                for item in result["answer_evidence_audit"])
                        and "30天" in answer_text and "人工审核" in answer_text
                        and approval_uncertain
                        and approval_before_refund
                        and timing_unconfirmed
                        and not forbidden_adjacent
                    ) else "FAIL",
                    "approved_gold_required_rule_count": gold_required_count,
                    "required_policy_rule_count": len(result["required_policy_rules"]),
                    "required_source_evidence_ids": sorted(actual_sources),
                    "expected_acceptable_evidence_ids": sorted(expected_sources),
                    "answer_contains_30_day_and_human_review": "30天" in answer_text and "人工审核" in answer_text,
                    "answer_explains_unconfirmed_timing": timing_unconfirmed,
                    "answer_covers_approval_before_refund": approval_before_refund,
                    "unrequested_adjacent_detail_present": [term for term in forbidden_adjacent if term in answer_text],
                    "approval_uncertainty_explicit": approval_uncertain,
                }
            retrieval_ids = _retrieved_ids(retrieval_records, initial_events)
            citation_ids = {item.get("chunk_id") for item in (run_answer.get("citations") or [])
                            if isinstance(item, dict) and item.get("chunk_id")}
            result["retrieval_evidence_ids"] = sorted(retrieval_ids)
            result["citation_evidence_ids"] = sorted(citation_ids)
            result["citations_subset_of_current_retrieval"] = bool(citation_ids) and citation_ids <= retrieval_ids
            if isinstance(result.get("q3_scope_acceptance"), dict):
                expected_sources = set(result["q3_scope_acceptance"].get("expected_acceptable_evidence_ids") or [])
                result["q3_scope_acceptance"]["citation_evidence_ids"] = sorted(citation_ids)
                result["q3_scope_acceptance"]["citations_match_core_evidence"] = citation_ids == expected_sources
                if citation_ids != expected_sources:
                    result["q3_scope_acceptance"]["status"] = "FAIL"
            result["agent_state_summary"] = ({"run_id": snapshot["agent_state"].get("run_id"),
                "session_id": snapshot["agent_state"].get("session_id"),
                "trace_id": snapshot["agent_state"].get("trace_id"),
                "run_status": snapshot["agent_state"].get("run_status"),
                "strategy": snapshot["agent_state"].get("strategy"),
                "evidence_sufficient": snapshot["agent_state"].get("evidence_sufficient"),
                "knowledge_version": snapshot["agent_state"].get("knowledge_version"),
                "retrieval_hop_count": len(snapshot["agent_state"].get("retrieval_hops") or []),
                "message_count": len(snapshot["agent_state"].get("messages") or [])}
                if snapshot["agent_state"] else None)
    finally:
        first_app.state.engine.dispose()

    run_id = result["run_id"]
    result["provider_requests"] = [row for row in request_ledger.records if row["sequence"] > before_calls]
    # Rebuild the app and engine, then verify persisted state and an idempotent stream replay.
    restarted_engine = make_engine(database_url)
    restarted_app = create_app(database_url=database_url, engine=restarted_engine, chat_provider=provider,
                               orchestrator_mode="langgraph", retrieval_evaluation_at=case.get("evaluation_at"))
    try:
        with TestClient(restarted_app) as client:
            _login(client, user_id, password)
            stored_run_response = client.get(f"/api/runs/{run_id}")
            session_id = result.get("graph") and result.get("agent_state_summary", {}).get("session_id")
            if not session_id:
                with make_session_factory(restarted_engine)() as db:
                    session_id = db.get(Run, run_id).session_id
            stored_session_response = client.get(f"/api/sessions/{session_id}")
            stored_events_response = client.get(f"/api/runs/{run_id}/events?after_seq=0")
            before_replay_events = stored_events_response.json().get("events", []) if stored_events_response.status_code == 200 else []
            current_calls = len(request_ledger.records)
            replay_response = client.post("/api/chat/stream", headers={"Origin": str(client.base_url).rstrip("/"),
                CSRF_HEADER: client.cookies.get(CSRF_COOKIE, "")}, json=payload)
            replay_events = parse_sse(replay_response.text)
            after_replay = client.get(f"/api/runs/{run_id}/events?after_seq=0")
            after_replay_events = after_replay.json().get("events", []) if after_replay.status_code == 200 else []
            result.update({"restart_run_status": stored_run_response.json().get("run", {}).get("status")
                               if stored_run_response.status_code == 200 else None,
                           "restart_session_message_count": len(stored_session_response.json().get("session", {}).get("messages", []))
                               if stored_session_response.status_code == 200 else None,
                           "restart_event_count": len(before_replay_events),
                           "idempotent_replay_http_status": replay_response.status_code,
                           "replay_event_types": [item["type"] for item in replay_events],
                           "replay_run_ids": sorted({item.get("run_id") for item in replay_events if item.get("run_id")}),
                           "persisted_event_count_after_replay": len(after_replay_events),
                           "replay_did_not_increase_model_calls": len(request_ledger.records) == current_calls,
                           "replay_did_not_increase_event_count": len(before_replay_events) == len(after_replay_events),
                           "request_count_delta_total": len(request_ledger.records) - before_calls})
    finally:
        restarted_app.state.engine.dispose()

    try:
        checkpoint = run_checkpointer_async(_checkpoint_read(database_url, settings.checkpoint_schema, run_id,
                                                            case["user_query"], result.get("answer")))
    except Exception as exc:
        checkpoint = {"read_after_app_recreation": False, "error_type": type(exc).__name__}
    result["checkpoint"] = checkpoint
    result["persisted_state_consistent"] = bool(
        result.get("run_status") == "SUCCEEDED"
        and result.get("restart_run_status") == "SUCCEEDED"
        and result.get("graph", {}).get("status") == "SUCCEEDED"
        and result.get("agent_state_summary", {}).get("run_status") == "SUCCEEDED"
        and result.get("graph", {}).get("checkpoint_id") == checkpoint.get("checkpoint_id")
        and checkpoint.get("read_after_app_recreation")
        and not checkpoint.get("query_in_checkpoint")
        and not checkpoint.get("full_answer_in_checkpoint"))
    result["authentication_and_replay_consistent"] = bool(
        result.get("restart_session_message_count", 0) >= 2
        and result.get("idempotent_replay_http_status") == 200
        and result.get("replay_run_ids") == [run_id]
        and result.get("replay_did_not_increase_model_calls")
        and result.get("replay_did_not_increase_event_count"))
    result["citation_and_route_contract"] = bool(
        result.get("strategy") == case["expected_strategy"]
        and result.get("routed_strategy") == case["expected_strategy"]
        and result.get("answer_kind") == "POLICY_ANSWER"
        and result.get("sse_required_event_order_valid")
        and result.get("citations_subset_of_current_retrieval")
        and result.get("q3_scope_acceptance", {}).get("status", "PASS") == "PASS")
    result["status"] = "PASS" if (result["persisted_state_consistent"]
        and result["authentication_and_replay_consistent"]
        and result["citation_and_route_contract"]) else "FAIL"
    return result


def run(only_case_id: str | None = None) -> dict[str, Any]:
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {"schema_version": "shoprelay.langgraph-g2s-real-smoke.v1",
        "started_at": started.isoformat().replace("+00:00", "Z"), "status": "BLOCKED",
        "orchestrator_mode": "langgraph", "max_real_deepseek_requests": 0,
        "q3_cumulative_request_limit": MAX_Q3_REAL_REQUESTS,
        "requested_case_id": only_case_id,
        "sdk_automatic_retries": 0, "provider_retries": 0,
        "price_source": PRICE_SOURCE,
        "price_basis": "ShopRelay peak-rate estimate from usage (not an account invoice)",
        "secrets_recorded": False}
    ledger = RequestLedger(maximum=0)
    try:
        cases, dataset_sha, manifest_sha = load_approved_cases()
        if only_case_id is not None:
            cases = [case for case in cases if case["case_id"] == only_case_id]
            if len(cases) != 1:
                raise ValueError("requested case is not in the fixed approved-dev-only manifest")
        prior_requests = prior_q3_real_request_count()
        remaining_requests = MAX_Q3_REAL_REQUESTS - prior_requests
        if remaining_requests <= 0:
            raise RuntimeError("Q3 cumulative DeepSeek request budget is exhausted before HTTP send")
        ledger = RequestLedger(maximum=min(MAX_REAL_REQUESTS, remaining_requests))
        report["q3_prior_real_request_count"] = prior_requests
        report["max_real_deepseek_requests"] = ledger.maximum
        report["dataset"] = {"path": "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json",
                             "sha256": dataset_sha, "probe_manifest": str(DATASET.relative_to(ROOT)),
                             "probe_manifest_sha256": manifest_sha, "used_splits": ["dev"],
                             "case_ids": [case["case_id"] for case in cases],
                             "test_candidate_read_or_used": False}
        url = resolve_test_database_url()
        settings = load_settings(require_api_key=True)
        report["configuration"] = {"api_key_configured": bool(settings.api_key),
            "api_key_source": settings.api_key_source, "base_url": settings.base_url,
            "model": settings.model, "base_url_source": settings.base_url_source,
            "model_source": settings.model_source, "graph_version": settings.graph_version,
            "execution_version": settings.execution_version, "checkpointer_schema": settings.checkpoint_schema,
            "policy_answer_node": {"thinking": settings.nodes["policy_answer"].thinking,
                "max_tokens": settings.nodes["policy_answer"].max_tokens,
                "timeout_seconds": settings.nodes["policy_answer"].timeout_seconds},
            "route_node": {"thinking": settings.nodes["route"].thinking,
                "max_tokens": settings.nodes["route"].max_tokens,
                "timeout_seconds": settings.nodes["route"].timeout_seconds},
            "condition_node": {"thinking": settings.nodes["condition_extraction"].thinking,
                "max_tokens": settings.nodes["condition_extraction"].max_tokens,
                "timeout_seconds": settings.nodes["condition_extraction"].timeout_seconds},
            "grade_node": {"thinking": settings.nodes["evidence_sufficiency"].thinking,
                "max_tokens": settings.nodes["evidence_sufficiency"].max_tokens,
                "timeout_seconds": settings.nodes["evidence_sufficiency"].timeout_seconds}}
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        with engine.connect() as conn:
            report["database"] = {"database": conn.execute(text("SELECT current_database()")).scalar_one(),
                "postgres_version": conn.execute(text("SELECT current_setting('server_version')")).scalar_one(),
                "application_revision": current_revision(database_url)}
        if report["database"]["database"] != "shoprelay_m11_test":
            raise RuntimeError("G2-S target database guard rejected a non-test database")
        if report["database"]["application_revision"] != EXPECTED_REVISION:
            raise RuntimeError("G2-S test database is not at the already accepted schema revision")
        report["database"]["checkpointer_schema"] = settings.checkpoint_schema
        report["database"]["checkpointer_ready"] = run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema))
        if report["database"]["checkpointer_ready"] is not True:
            raise RuntimeError("G2-S requires the already initialized Checkpointer schema")
        report["versions"] = {name: importlib.metadata.version(name) for name in
            ("langgraph", "langgraph-checkpoint-postgres", "psycopg-pool")}
        engine.dispose()
        provider = DeepSeekProvider(settings=settings)
        if getattr(provider.client, "max_retries", None) != 0:
            raise RuntimeError("G2-S requires OpenAI SDK automatic retries to be disabled")
        ledger.install(provider)
        report["cases"] = []
        for case in cases:
            case_result = _run_case(case, database_url, settings, provider, ledger)
            report["cases"].append(case_result)
            if any(call.get("status") == "ERROR" for call in ledger.records):
                report["stop_reason"] = "real provider request failed; no retry or further case execution"
                break
        report["model_requests"] = ledger.records
        report["request_totals"] = ledger.totals()
        report["real_deepseek_requests"] = ledger.totals()["request_count"]
        report["status"] = "PASS" if len(report["cases"]) == len(cases) and all(
            case["status"] == "PASS" for case in report["cases"]) else "FAIL"
        if any(call.get("status") == "ERROR" and call.get("exception_type") in {
                "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadTimeout",
                "AuthenticationError", "RateLimitError"} for call in ledger.records):
            report["status"] = "BLOCKED"
            report["failure_class"] = "PROVIDER_NETWORK_AUTH_OR_RATE_LIMIT"
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure_code"] = str(exc) if str(exc) in {
            "G2-S target database guard rejected a non-test database",
            "G2-S test database is not at the already accepted schema revision",
            "G2-S requires the already initialized Checkpointer schema",
            "G2-S requires OpenAI SDK automatic retries to be disabled"} else None
        report["model_requests"] = ledger.records
        report["request_totals"] = ledger.totals()
        report["real_deepseek_requests"] = ledger.totals()["request_count"]
    finally:
        ledger.uninstall()
        report.setdefault("model_requests", ledger.records)
        report.setdefault("request_totals", ledger.totals())
        report.setdefault("real_deepseek_requests", ledger.totals()["request_count"])
        report["finished_at"] = utc_now()
        out = ROOT / "docs" / "probes" / (f"langgraph-g2s-real-{started.strftime('%Y%m%dT%H%M%S%fZ')}"
                                              f"-{uuid.uuid4().hex[:8]}.json")
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "record": str(out.relative_to(ROOT)),
            "case_count": len(report.get("cases", [])), "real_deepseek_requests": report["request_totals"]["request_count"],
            "model": (report.get("configuration") or {}).get("model"), "secrets_recorded": False}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true",
                        help="explicitly authorize this run to send up to eight real DeepSeek requests")
    parser.add_argument("--case-id", choices=CASE_IDS,
                        help="optionally limit the real smoke to one approved dev case")
    args = parser.parse_args()
    if not args.real:
        parser.error("real API calls are disabled unless --real is supplied")
    result = run(only_case_id=args.case_id)
    raise SystemExit(0 if result.get("status") == "PASS" else 1)
