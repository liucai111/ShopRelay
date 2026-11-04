"""Capped M2-R real policy-quality and PostgreSQL integration probe.

This probe reads only the fixed allowlisted approved dev/validation manifest.
It does not migrate/clear the database, use test_candidate, or change the
production orchestrator default. Real model requests require the explicit
--real flag and are capped across timestamped probe records at 24 / USD 1.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor" / "g2"), str(ROOT / ".vendor"), str(ROOT)]

MANIFEST = ROOT / "eval/raw/m2-r-approved-dev-validation-manifest-r1.json"
HISTORICAL_RETRIEVAL = ROOT / "eval/raw/m2.2-retrieval-20261019T140452750330Z-d483d4c7.json"
INDEX_PATH = ROOT / "data/index.json"
EXPECTED_SOURCE_SHA = "0daea3b85052e34217e5249211bbd7cf3c51d3fce871e2f00c0717e3632acf52"
EXPECTED_REVISION = "20261020_05"
MAX_STAGE_REQUESTS = 24
MAX_STAGE_COST_USD = 1.00
FIXED_CASE_IDS = {
    "pr2-refund_open_uniqueness-02", "pr2-refund_partial_unsupported-02",
    "pr2-refund_reason_hard_negative-02", "pr2-multi_clause_return_refund-03",
    "pr2-return_old_version-01", "pr2-warranty_general-01",
    "pr2-warranty_specific_missing-04",
}
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICE_INPUT_USD_PER_M = 0.30
PRICE_CACHED_INPUT_USD_PER_M = 0.006
PRICE_OUTPUT_USD_PER_M = 1.20

DEV_RUNS = (
    ("pr2-refund_open_uniqueness-02", "legacy"),
    ("pr2-refund_partial_unsupported-02", "legacy"),
    ("pr2-refund_reason_hard_negative-02", "legacy"),
    ("pr2-multi_clause_return_refund-03", "legacy"),
)
VALIDATION_RUNS = (
    ("pr2-return_old_version-01", "legacy"),
    ("pr2-warranty_general-01", "legacy"),
)
BEHAVIOR_RUN = ("pr2-warranty_specific_missing-04", "legacy")
ORCHESTRATOR_PARITY_RUNS = (
    ("pr2-refund_open_uniqueness-02", "langgraph"),
    ("pr2-multi_clause_return_refund-03", "langgraph"),
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_text(value: Any, secret: str | None) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {str(k): _safe_text(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_text(v, secret) for v in value]
    return value


def _write_json(path: Path, value: dict, secret: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = _safe_text(value, secret)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_manifest() -> tuple[dict[str, dict], str]:
    raw = MANIFEST.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "shoprelay.m2-r-approved-dev-validation-manifest.v1":
        raise ValueError("M2-R manifest schema mismatch")
    if manifest.get("source_dataset_sha256") != EXPECTED_SOURCE_SHA:
        raise ValueError("approved dataset SHA mismatch")
    if manifest.get("test_candidate_rows_included") is not False:
        raise ValueError("manifest must explicitly exclude test_candidate rows")
    cases = manifest.get("cases")
    allowed = set(manifest.get("case_ids") or [])
    if not isinstance(cases, list) or allowed != FIXED_CASE_IDS or len(cases) != len(allowed):
        raise ValueError("manifest case allowlist is invalid")
    by_id = {row.get("case_id"): row for row in cases if isinstance(row, dict)}
    if set(by_id) != allowed:
        raise ValueError("manifest contains missing or unexpected cases")
    for case_id, case in by_id.items():
        if case.get("dataset_split") not in {"dev", "validation"} or case.get("review_status") != "APPROVED":
            raise ValueError(f"non-approved or non-dev/validation sample in manifest: {case_id}")
        if case.get("frozen") or case.get("test_candidate_only"):
            raise ValueError(f"frozen/test-only sample is not allowed in this M2-R smoke: {case_id}")
    return by_id, _sha(raw)


def _request_cost(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached = min(prompt, int(details.get("cached_tokens") or details.get("cache_hit_tokens") or 0))
    return round(((prompt - cached) * PRICE_INPUT_USD_PER_M
                  + cached * PRICE_CACHED_INPUT_USD_PER_M
                  + completion * PRICE_OUTPUT_USD_PER_M) / 1_000_000, 12)


def _usage_dict(response: Any) -> dict | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return None
    safe = {key: int(usage[key]) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage.get(key), (int, float))}
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        keep = {key: int(details[key]) for key in ("cached_tokens", "reasoning_tokens")
                if isinstance(details.get(key), (int, float))}
        if keep:
            safe["prompt_tokens_details"] = keep
    return safe or None


class RequestBudgetExceeded(RuntimeError):
    pass


class RequestLedger:
    """Caps SDK create calls before send, records raw responses and disables retries."""

    def __init__(self, report: dict, output_path: Path, *, api_key: str, prior_requests: int,
                 prior_cost: float, prior_cost_known: bool):
        self.report = report
        self.output_path = output_path
        self.secret = api_key
        self.prior_requests = prior_requests
        self.prior_cost = prior_cost
        self.prior_cost_known = prior_cost_known
        self.records: list[dict] = []
        self.denied = 0
        self.lock = threading.Lock()
        self.local = threading.local()
        self.resource_type = None
        self.original_create = None
        self.client = None

    def persist(self) -> None:
        self.report["model_requests"] = list(self.records)
        self.report["request_totals"] = self.totals()
        _write_json(self.output_path, self.report, self.secret)

    @staticmethod
    def _estimate(messages: Any, max_tokens: Any) -> float:
        text = json.dumps(messages, ensure_ascii=False, default=str)
        input_tokens = max(1, len(text.encode("utf-8")) // 2)
        output_tokens = max(1, int(max_tokens or 512))
        return ((input_tokens * PRICE_INPUT_USD_PER_M) + (output_tokens * PRICE_OUTPUT_USD_PER_M)) / 1_000_000

    def install(self, provider) -> None:
        self.client = provider.client
        for method_name in ("classify_route", "plan_policy_aspects", "answer", "answer_for_policy_aspects"):
            method = getattr(provider, method_name, None)
            if not callable(method):
                continue
            def wrapped(*args, _method=method, _name=method_name, **kwargs):
                previous = getattr(self.local, "node", None)
                self.local.node = _name
                try:
                    return _method(*args, **kwargs)
                finally:
                    self.local.node = previous
            setattr(provider, method_name, wrapped)
        self.resource_type = type(provider.client.chat.completions)
        self.original_create = self.resource_type.create

        def counted_create(resource, *args, **kwargs):
            with self.lock:
                total_prior_cost = self.prior_cost
                previous_estimated = sum(float(row.get("reserved_cost_usd") or row.get("estimated_cost_usd") or 0)
                                          for row in self.records)
                reserved = self._estimate(kwargs.get("messages"), kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"))
                next_count = self.prior_requests + len(self.records) + 1
                if (next_count > MAX_STAGE_REQUESTS or not self.prior_cost_known or
                        total_prior_cost + previous_estimated + reserved > MAX_STAGE_COST_USD):
                    self.denied += 1
                    self.persist()
                    raise RequestBudgetExceeded("M2-R request/cost cap refused this call before HTTP send")
                row = {"sequence": next_count, "node": getattr(self.local, "node", "unclassified"),
                       "status": "IN_FLIGHT", "model_requested": kwargs.get("model"),
                       "max_tokens": kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
                       "reserved_cost_usd": round(reserved, 12), "sdk_retries": 0}
                self.records.append(row)
                self.persist()
            started = time.perf_counter()
            try:
                response = self.original_create(resource, *args, **kwargs)
            except Exception as exc:
                row.update({"status": "ERROR", "exception_type": type(exc).__name__,
                            "http_status": getattr(exc, "status_code", None),
                            "provider_error_code": getattr(exc, "code", None),
                            "latency_ms": round((time.perf_counter() - started) * 1000),
                            "estimated_cost_usd": round(reserved, 12), "cost_basis": "preflight_reserve_no_usage"})
                self.persist()
                raise
            usage = _usage_dict(response)
            try:
                choice = response.choices[0]
                finish = getattr(choice, "finish_reason", None)
                content = getattr(getattr(choice, "message", None), "content", None)
            except (AttributeError, IndexError, TypeError):
                finish, content = None, None
            actual_cost = _request_cost(usage)
            row.update({"status": "RESPONSE", "request_id": getattr(response, "_request_id", None),
                        "model_returned": getattr(response, "model", None), "finish_reason": finish,
                        "usage": usage, "latency_ms": round((time.perf_counter() - started) * 1000),
                        "estimated_cost_usd": round(actual_cost if actual_cost is not None else reserved, 12),
                        "cost_basis": "provider_usage_peak_rate_estimate" if actual_cost is not None else "preflight_reserve_no_usage",
                        "raw_content": content if isinstance(content, str) else None})
            self.persist()
            return response

        self.resource_type.create = counted_create

    def uninstall(self) -> None:
        if self.resource_type is not None and self.original_create is not None:
            self.resource_type.create = self.original_create

    def totals(self) -> dict:
        usage_rows = [row.get("usage") for row in self.records if isinstance(row.get("usage"), dict)]
        known_costs = [row.get("estimated_cost_usd") for row in self.records
                       if isinstance(row.get("estimated_cost_usd"), (int, float))]
        return {"stage_request_count": len(self.records),
                "prior_m2_r_request_count": self.prior_requests,
                "cumulative_m2_r_request_count": self.prior_requests + len(self.records),
                "requests_with_usage": len(usage_rows),
                "requests_without_usage": len(self.records) - len(usage_rows),
                "prompt_tokens": sum(row.get("prompt_tokens", 0) for row in usage_rows),
                "completion_tokens": sum(row.get("completion_tokens", 0) for row in usage_rows),
                "estimated_cost_usd_peak_rate": round(sum(known_costs), 12) if len(known_costs) == len(self.records) else None,
                "prior_estimated_cost_usd": round(self.prior_cost, 12),
                "cumulative_estimated_cost_usd": (round(self.prior_cost + sum(known_costs), 12)
                    if self.prior_cost_known and len(known_costs) == len(self.records) else None),
                "requests_denied_before_send_by_budget": self.denied,
                "stage_request_cap": MAX_STAGE_REQUESTS, "stage_cost_cap_usd": MAX_STAGE_COST_USD,
                "sdk_max_retries": 0}


def _prior_totals() -> tuple[int, float, bool]:
    total = cost = 0
    known = True
    for path in (ROOT / "docs/probes").glob("m2-r-real-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            known = False
            continue
        if record.get("record_type") != "m2_r_real_quality_probe":
            continue
        totals = record.get("request_totals") or {}
        count = totals.get("stage_request_count")
        amount = totals.get("estimated_cost_usd_peak_rate")
        if not isinstance(count, int) or not isinstance(amount, (int, float)):
            known = False
            continue
        total += count
        cost += float(amount)
    return total, cost, known


def _nearest_percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 3)


def _score_retrieval(rows: list[dict], gold: set[str]) -> dict:
    ids = [row.get("chunk_id") for row in rows]
    ranks = [index for index, value in enumerate(ids, 1) if value in gold]
    return {"p_at_1": int(bool(ids and ids[0] in gold)),
            "recall_at_3": round(len(set(ids[:3]) & gold) / len(gold), 6) if gold else None,
            "recall_at_5": round(len(set(ids[:5]) & gold) / len(gold), 6) if gold else None,
            "mrr": round(1 / min(ranks), 6) if ranks else 0.0,
            "retrieved_ids": ids}


def _required_evidence_ids(case: dict) -> set[str]:
    required_ids: set[str] = set()
    conditions = {row.get("condition_id") for row in case.get("required_conditions", [])
                  if isinstance(row, dict) and row.get("required")}
    for item in case.get("evidence_judgments", []):
        if item.get("condition_id") in conditions:
            required_ids.update(value for value in item.get("evidence_ids", []) if isinstance(value, str))
    return required_ids or set(case.get("acceptable_evidence_ids") or [])


def _read_historical_baseline(source_sha: str) -> dict:
    raw = HISTORICAL_RETRIEVAL.read_bytes()
    baseline = json.loads(raw)
    matches = baseline.get("dataset_sha256") == source_sha
    return {"path": str(HISTORICAL_RETRIEVAL.relative_to(ROOT)).replace("\\", "/"),
            "sha256": _sha(raw), "dataset_sha256_matches": matches,
            "scored_n": baseline.get("scored_n"), "scored_splits": baseline.get("scored_split_counts"),
            "test_candidate_scored": baseline.get("test_candidate_query_or_gold_scored"),
            "test_candidate_used_for_tuning": baseline.get("test_candidate_used_for_tuning"),
            "metrics": baseline.get("metrics") if matches else None,
            "reuse_status": "REUSED_MATCHING_DATA_SHA" if matches else "NOT_REUSABLE"}


def _retrieval_only(cases: list[dict], retrieval) -> dict:
    loads = retrieval._m2r_model_load_ms if hasattr(retrieval, "_m2r_model_load_ms") else {
        "embedding_load_ms": None, "reranker_load_ms": None}
    per_case = []
    for case in cases:
        gold = _required_evidence_ids(case)
        view = retrieval.pin_view(case["evaluation_at"])
        entry = {"case_id": case["case_id"], "dataset_split": case["dataset_split"],
                 "scoreability": case.get("scoreability"), "evaluation_at": view.evaluation_at,
                 "index_fingerprint": view.fingerprint, "knowledge_version": view.index.get("knowledge_version"),
                 "gold_evidence_ids": sorted(gold), "strategies": {}}
        for name, mode in (("BM25", "bm25"), ("HYBRID_RERANK", "rrf_reranker")):
            start = time.perf_counter()
            try:
                found = retrieval.retrieve(case["user_query"], mode=mode, view=view)
                latency = round((time.perf_counter() - start) * 1000, 3)
                row = {**_score_retrieval(found.get("evidence") or [], gold),
                       "latency_ms": latency, "retrieval_latency_ms": found.get("latency_ms"),
                       "degraded": found.get("degraded"), "failures": found.get("failures"),
                       "diagnostics": found.get("diagnostics"), "models": found.get("models"),
                       "knowledge_version": found.get("knowledge_version"),
                       "index_fingerprint": found.get("index_fingerprint"),
                       "config_hash": found.get("config_hash")}
                diagnostics = found.get("diagnostics") or {}
                for key, target in (("vector_model_load_ms", "embedding_load_ms"),
                                    ("reranker_model_load_ms", "reranker_load_ms")):
                    if isinstance(diagnostics.get(key), (int, float)) and loads[target] is None:
                        loads[target] = diagnostics[key]
                entry["strategies"][name] = row
            except Exception as exc:
                entry["strategies"][name] = {"status": "BLOCKED", "exception_type": type(exc).__name__}
        per_case.append(entry)
    summary = {}
    scoreable = [case for case in cases if case.get("scoreability") == "SCORABLE_CANDIDATE"]
    for name in ("BM25", "HYBRID_RERANK"):
        scored_rows = [entry["strategies"][name] for entry in per_case
                       if entry.get("scoreability") == "SCORABLE_CANDIDATE"
                       and isinstance(entry.get("strategies", {}).get(name), dict)
                       and entry["strategies"][name].get("status") != "BLOCKED"]
        if len(scored_rows) != len(scoreable):
            summary[name] = {"status": "BLOCKED", "scored_n": len(scored_rows), "expected_n": len(scoreable)}
            continue
        summary[name] = {"status": "PASS", "scored_n": len(scored_rows),
            "p_at_1": {"numerator": sum(row["p_at_1"] for row in scored_rows), "denominator": len(scored_rows),
                       "value": round(sum(row["p_at_1"] for row in scored_rows) / len(scored_rows), 6)},
            "recall_at_3": round(sum(row["recall_at_3"] for row in scored_rows) / len(scored_rows), 6),
            "recall_at_5": round(sum(row["recall_at_5"] for row in scored_rows) / len(scored_rows), 6),
            "mrr": round(sum(row["mrr"] for row in scored_rows) / len(scored_rows), 6),
            "latency_ms": {"p50_nearest_rank": _nearest_percentile([row["latency_ms"] for row in scored_rows], .50),
                           "p95_nearest_rank": _nearest_percentile([row["latency_ms"] for row in scored_rows], .95)},
            "degraded_n": sum(bool(row["degraded"]) for row in scored_rows)}
    return {"per_case": per_case, "summary": summary, "model_load_ms": loads,
            "scoreable_denominator": len(scoreable),
            "behavior_only_case_ids_excluded": [case["case_id"] for case in cases
                if case.get("scoreability") != "SCORABLE_CANDIDATE"]}


def _parse_sse(body: str) -> list[dict]:
    events = []
    for block in re.split(r"\r?\n\r?\n", body):
        event_type = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    data = None
        if event_type and isinstance(data, dict):
            events.append({"type": event_type, "event_id": data.get("event_id"),
                           "run_id": data.get("run_id"), "session_id": data.get("session_id"),
                           "trace_id": data.get("trace_id"), "seq": data.get("seq"),
                           "payload": data.get("payload") or {}})
    return events


def _sse_order_valid(event_types: list[str]) -> bool:
    required_order = ("started", "route_decided", "agent_started", "retrieval_hop", "answer", "done")
    applicable = [name for name in required_order if name in event_types]
    terminal = "done" if "done" in event_types else "error" if "error" in event_types else None
    return (all(event_types.index(a) < event_types.index(b)
                for a, b in zip(applicable, applicable[1:])) and
            "started" in event_types and terminal is not None and event_types[-1] == terminal and
            ("answer" not in event_types or ("done" in event_types and
             event_types.index("answer") < event_types.index("done"))))


def _verification_passes(verification: dict[str, Any]) -> bool:
    """Treat explicit test-candidate exclusion as a required false-valued invariant."""
    return (verification.get("test_candidate_rows_included") is False and
            all(value is True for key, value in verification.items()
                if key != "test_candidate_rows_included"))


def _load_db_and_models():
    from qdrant_client import QdrantClient
    from sqlalchemy import text
    from scripts.g2_test_database import resolve_test_database_url
    from app.m0.config import load_settings
    from app.m1.db import current_revision, make_engine
    from app.m2.retrieval import (SMALL_DIR, RERANKER_DIR, RetrievalService,
                                  config_from_environment)
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("test database guard rejected non-loopback or non-test database")
    engine = make_engine(url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version()")).scalar_one()
        db_name = connection.execute(text("SELECT current_database()")).scalar_one()
    revision = current_revision(url.render_as_string(hide_password=False))
    if db_name != "shoprelay_m11_test" or revision != EXPECTED_REVISION:
        engine.dispose()
        raise RuntimeError("PostgreSQL test DB name or migration revision did not match the read-only probe contract")
    local_cache = {"bge_small_present": (SMALL_DIR / "modules.json").exists(),
                   "reranker_base_present": (RERANKER_DIR / "model.safetensors").exists(),
                   "weights_download_allowed": False}
    qdrant = QdrantClient(":memory:")
    config = config_from_environment()
    from dataclasses import replace
    config = replace(config, mode="bm25", qdrant_mode="local", qdrant_url=None)
    retrieval = RetrievalService(INDEX_PATH, config, qdrant_client=qdrant,
                                 collection_namespace="m2r_probe")
    settings = load_settings(require_api_key=True)
    return url, engine, qdrant, retrieval, settings, revision, db_name, version.split(",", 1)[0], local_cache


def _db_fingerprint(engine) -> dict:
    from sqlalchemy import text
    with engine.connect() as connection:
        orders = connection.execute(text("SELECT order_id,user_id,status,paid_at,delivered_at,paid_cents,version FROM orders ORDER BY order_id")).all()
    index_bytes = INDEX_PATH.read_bytes()
    encoded = json.dumps({"orders": [list(row) for row in orders], "index_sha256": _sha(index_bytes)},
                         ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {"order_count": len(orders), "policy_index_sha256": _sha(index_bytes), "sha256": _sha(encoded)}


def _create_probe_user(engine) -> tuple[str, str]:
    from app.m1.db import make_session_factory
    from app.m1.models import User
    from app.m1.services import set_demo_password
    user_id = "m2r_" + uuid.uuid4().hex[:24]
    password = secrets.token_urlsafe(28)
    factory = make_session_factory(engine)
    with factory() as db:
        db.add(User(user_id=user_id, display_name="M2-R isolated probe"))
        db.commit()
        set_demo_password(db, user_id, password)
    return user_id, password


def _login(client, user_id: str, password: str) -> dict[str, str]:
    from app.m1.api import CSRF_HEADER, CSRF_COOKIE
    csrf = client.get("/api/demo/csrf")
    if csrf.status_code != 200:
        raise RuntimeError("CSRF bootstrap failed")
    token = csrf.json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: token},
                           json={"username": user_id, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"isolated probe login failed: HTTP {response.status_code}")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get(CSRF_COOKIE, "")}


def _reset_probe_password(engine, user_id: str) -> str:
    """Recover only this probe's generated identity; seed/demo users are immutable here."""
    from app.m1.db import make_session_factory
    from app.m1.models import User
    from app.m1.services import set_demo_password
    if not re.fullmatch(r"m2r_[0-9a-f]{24}", user_id):
        raise ValueError("resume identity is not a generated M2-R probe user")
    password = secrets.token_urlsafe(28)
    with make_session_factory(engine)() as db:
        user = db.get(User, user_id)
        if user is None or user.display_name != "M2-R isolated probe":
            raise ValueError("resume identity does not match the isolated M2-R marker")
        set_demo_password(db, user_id, password)
    return password


def _graph_snapshot(engine, run_id: str, user_id: str) -> dict:
    from app.m1.models import GraphExecutionRecord, Message, Run, RunEvent, Trace
    from app.m1.db import make_session_factory
    from app.m1.state import restore_agent_state
    factory = make_session_factory(engine)
    with factory() as db:
        run = db.get(Run, run_id)
        if run is None or run.user_id != user_id:
            raise AssertionError("Run missing or owner mismatch")
        events = db.query(RunEvent).filter_by(run_id=run_id).order_by(RunEvent.seq).all()
        traces = db.query(Trace).filter_by(run_id=run_id).all()
        messages = db.query(Message).filter_by(session_id=run.session_id).order_by(Message.sequence).all()
        graph = db.get(GraphExecutionRecord, run_id)
        state = restore_agent_state(db, user_id, run_id)
        return {"run_status": run.status, "error_code": run.error_code,
                "result": json.loads(run.result_json) if run.result_json else None,
                "usage": json.loads(run.usage_json) if run.usage_json else None,
                "events": [{"type": item.event_type, "seq": item.seq, "payload": json.loads(item.payload_json)} for item in events],
                "trace_types": [item.event_type for item in traces],
                "message_roles": [item.role for item in messages],
                "graph": ({"orchestrator": graph.orchestrator, "graph_version": graph.graph_version,
                           "checkpoint_id": graph.checkpoint_id, "status": graph.status,
                           "error_code": graph.error_code} if graph else None),
                "agent_state": state.model_dump(mode="json") if state else None}


def _checkpoint_id(database_url: str, schema: str, run_id: str) -> dict | None:
    from app.orchestration.checkpointer import open_checkpointer
    import asyncio
    async def read_one():
        async with open_checkpointer(database_url, schema) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}})
    # Psycopg async requires a SelectorEventLoop on Windows; asyncio.run's
    # default ProactorEventLoop raises InterfaceError before checkpoint access.
    saved = asyncio.run(read_one(), loop_factory=asyncio.SelectorEventLoop)
    if saved is None:
        return None
    return {"checkpoint_id": saved.config["configurable"].get("checkpoint_id"),
            "terminal_status": (saved.checkpoint.get("channel_values") or {}).get("terminal_status")}


def _recover_existing_case(case: dict, entry: dict, mode: str, *, database_url, settings,
                           engine, provider, retrieval, ledger, user_id: str,
                           password: str) -> dict:
    """Reopen an earlier run and replay its idempotency key without a new model call."""
    from fastapi.testclient import TestClient
    from app.m1.api import create_app
    from app.m1.db import make_session_factory
    from app.m1.models import Trace
    app = create_app(database_url=database_url, engine=engine, chat_provider=provider,
        index_path=INDEX_PATH, retrieval_evaluation_at=case.get("evaluation_at"),
        orchestrator_mode=mode)
    app.state.retrieval_service = retrieval
    calls_before = len(ledger.records)
    with TestClient(app) as client:
        headers = _login(client, user_id, password)
        run_response = client.get(f"/api/runs/{entry['run_id']}")
        events_response = client.get(f"/api/runs/{entry['run_id']}/events")
        run = (run_response.json().get("run") or {}) if run_response.status_code == 200 else {}
        events = events_response.json().get("events", []) if events_response.status_code == 200 else []
        replay_response = client.post("/api/chat/stream", headers=headers,
            json={"client_request_id": entry["request_id"], "message": case["user_query"]})
        replay_events = _parse_sse(replay_response.text)
    model_delta = len(ledger.records) - calls_before
    with make_session_factory(engine)() as db:
        trace_count = db.query(Trace).filter_by(run_id=entry["run_id"]).count()
    result = {"run_http_status": run_response.status_code,
        "events_http_status": events_response.status_code,
        "run_status": run.get("status"),
        "persisted_event_types": [row.get("event_type") for row in events],
        "replay_http_status": replay_response.status_code,
        "same_run": bool(replay_events and replay_events[0].get("run_id") == entry["run_id"]),
        "model_request_delta": model_delta, "trace_count": trace_count,
        "passed": (run_response.status_code == 200 and events_response.status_code == 200 and
            run.get("status") == entry.get("run_status") and replay_response.status_code == 200 and
            bool(replay_events) and replay_events[0].get("run_id") == entry["run_id"] and model_delta == 0)}
    if mode == "langgraph":
        result["checkpoint"] = _checkpoint_id(database_url, settings.checkpoint_schema, entry["run_id"])
        result["graph"] = _graph_snapshot(engine, entry["run_id"], user_id).get("graph")
        result["passed"] = result["passed"] and bool(result["checkpoint"] and result["graph"] and
            result["graph"].get("status") == entry.get("run_status"))
    return result


def _run_api_case(case: dict, mode: str, *, database_url, settings, engine, provider,
                  retrieval, ledger, user_id: str, password: str, report: dict,
                  output_path: Path) -> dict:
    from fastapi.testclient import TestClient
    from app.m1.api import create_app
    from app.m1.models import Run, Trace
    from app.m1.db import make_session_factory
    retrieval_rows: list[dict] = []
    original_retrieve = retrieval.retrieve
    def capture(*args, **kwargs):
        value = original_retrieve(*args, **kwargs)
        retrieval_rows.append({"mode": value.get("mode"), "evidence_ids": [row.get("chunk_id") for row in value.get("evidence", [])],
                               "strategy_channels": value.get("channels"), "latency_ms": value.get("latency_ms"),
                               "degraded": value.get("degraded"), "failures": value.get("failures"),
                               "knowledge_version": value.get("knowledge_version"),
                               "index_fingerprint": value.get("index_fingerprint"),
                               "diagnostics": value.get("diagnostics"), "models": value.get("models")})
        return value
    retrieval.retrieve = capture
    app_engine = engine
    app = create_app(database_url=database_url, engine=app_engine, chat_provider=provider,
                     index_path=INDEX_PATH, retrieval_evaluation_at=case.get("evaluation_at"),
                     orchestrator_mode=mode)
    app.state.retrieval_service = retrieval
    request_id = "m2r-" + uuid.uuid4().hex
    request = {"client_request_id": request_id, "message": case["user_query"]}
    entry = {"case_id": case["case_id"], "dataset_split": case["dataset_split"],
             "scoreability": case.get("scoreability"), "expected_strategy": case.get("expected_strategy"),
             "orchestrator_mode": mode, "user_query": case["user_query"], "request_id": request_id,
             "request_count_before": ledger.prior_requests + len(ledger.records), "status": "NOT_RUN"}
    try:
        with TestClient(app) as client:
            ready = client.get("/health/ready")
            if ready.status_code != 200 or ready.json().get("status") != "ready":
                entry.update({"status": "BLOCKED", "failure_code": "API_NOT_READY"})
                return entry
            headers = _login(client, user_id, password)
            started = time.perf_counter()
            response = client.post("/api/chat/stream", headers=headers, json=request)
            entry["http_status"] = response.status_code
            entry["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            events = _parse_sse(response.text)
            entry["sse_event_types"] = [event["type"] for event in events]
            entry["sse_events"] = [{key: event.get(key) for key in ("type", "run_id", "trace_id", "seq")} for event in events]
            run_ids = {event.get("run_id") for event in events if event.get("run_id")}
            if response.status_code != 200 or len(run_ids) != 1:
                entry.update({"status": "FAIL", "failure_code": "SSE_OR_RUN_ID_INVALID"})
                return entry
            run_id = next(iter(run_ids))
            entry["run_id"] = run_id
            entry["trace_id"] = next((event.get("trace_id") for event in events if event.get("trace_id")), None)
            event_types = entry["sse_event_types"]
            entry["sse_order_valid"] = _sse_order_valid(event_types)
            detail = client.get(f"/api/runs/{run_id}")
            entry["run_http_status"] = detail.status_code
            run = (detail.json().get("run") or {}) if detail.status_code == 200 else {}
            entry["run_status"] = run.get("status")
            entry["run_error"] = run.get("error")
            answer = run.get("answer") or {}
            route = answer.get("route") or {}
            entry["actual_strategy"] = route.get("strategy", answer.get("strategy"))
            entry["answer_kind"] = answer.get("answer_kind")
            entry["answer"] = answer.get("answer")
            entry["citations"] = answer.get("citations") or []
            entry["evidence"] = answer.get("evidence") or []
            entry["policy_retrieval"] = answer.get("policy_retrieval") or {}
            entry["retrieval"] = answer.get("retrieval") or {}
            entry["usage"] = answer.get("usage") or run.get("usage")
            entry["run_model_id"] = run.get("model_id")
            entry["latency_ms_run"] = run.get("latency_ms")
            entry["retrieval_calls_captured"] = retrieval_rows
            retrieved_ids = {value for row in retrieval_rows for value in row.get("evidence_ids", []) if isinstance(value, str)}
            citation_ids = {item.get("chunk_id") if isinstance(item, dict) else item for item in entry["citations"]}
            entry["citation_ids_subset_of_retrieval"] = bool(citation_ids) and citation_ids <= retrieved_ids
            gold_ids = _required_evidence_ids(case)
            entry["required_evidence_ids"] = sorted(gold_ids)
            entry["required_evidence_coverage"] = round(len(retrieved_ids & gold_ids) / len(gold_ids), 6) if gold_ids else None
            entry["required_evidence_citation_coverage"] = round(len(citation_ids & gold_ids) / len(gold_ids), 6) if gold_ids else None
            entry["policy_answer_valid"] = (entry.get("run_status") == "SUCCEEDED" and
                entry.get("answer_kind") == "POLICY_ANSWER" and bool(entry.get("answer")) and
                entry["citation_ids_subset_of_retrieval"])
            entry["strategy_matches_candidate"] = entry.get("actual_strategy") == case.get("expected_strategy")
            entry["model_request_count"] = ledger.prior_requests + len(ledger.records) - entry["request_count_before"]
            if case.get("scoreability") != "SCORABLE_CANDIDATE":
                # Behavior-only diagnostics are not route-accuracy or retrieval
                # scores. Their actual response remains available for review.
                entry["strategy_matches_candidate"] = None
                entry["status"] = "DIAGNOSTIC_ONLY"
            else:
                entry["status"] = "PASS" if (entry["run_status"] == "SUCCEEDED" and
                    entry.get("policy_answer_valid") and entry["sse_order_valid"] and
                    entry["strategy_matches_candidate"]) else "FAIL"

            # Capture the authoritative DB view before replay. The API answer alone
            # is not sufficient proof of Run/SSE/Trace/AgentState consistency.
            snapshot = _graph_snapshot(app_engine, run_id, user_id)
            entry["persisted"] = snapshot
            entry["run_status_matches_db"] = snapshot["run_status"] == entry["run_status"]
            entry["persisted_event_types"] = [row["type"] for row in snapshot["events"]]
            entry["run_status"] = snapshot["run_status"]
            entry["trace_id"] = entry.get("trace_id") or next((event.get("trace_id") for event in events if event.get("trace_id")), None)
            if mode == "langgraph":
                entry["checkpoint"] = _checkpoint_id(database_url, settings.checkpoint_schema, run_id)
                entry["graph"] = snapshot["graph"]
                if not entry["checkpoint"] or not entry["graph"] or entry["graph"].get("status") != "SUCCEEDED":
                    entry["status"] = "FAIL"
            if not entry["run_status_matches_db"]:
                entry["status"] = "FAIL"

            trace_count_before = len(snapshot["trace_types"])
            calls_before_replay = len(ledger.records)
            replay = client.post("/api/chat/stream", headers=headers, json=request)
            replay_events = _parse_sse(replay.text)
            entry["idempotent_replay"] = {"http_status": replay.status_code,
                "same_run": bool(replay_events and replay_events[0].get("run_id") == run_id),
                "model_request_delta": len(ledger.records) - calls_before_replay,
                "event_types": [event["type"] for event in replay_events]}
            with make_session_factory(app_engine)() as db:
                traces_after = db.query(Trace).filter_by(run_id=run_id).count()
            entry["idempotent_replay"]["trace_count_unchanged"] = traces_after == trace_count_before
            entry["idempotent_replay"]["passed"] = (entry["idempotent_replay"]["same_run"] and
                entry["idempotent_replay"]["model_request_delta"] == 0 and
                entry["idempotent_replay"]["trace_count_unchanged"])
            if not entry["idempotent_replay"]["passed"]:
                entry["status"] = "FAIL"
        # App instance is now shut down; recreate it and read the same Run/events.
        recreated = create_app(database_url=database_url, engine=app_engine, chat_provider=provider,
            index_path=INDEX_PATH, retrieval_evaluation_at=case.get("evaluation_at"), orchestrator_mode=mode)
        recreated.state.retrieval_service = retrieval
        with TestClient(recreated) as client_after_restart:
            headers_after_restart = _login(client_after_restart, user_id, password)
            before_restart_calls = len(ledger.records)
            reread = client_after_restart.get(f"/api/runs/{entry['run_id']}")
            reread_events = client_after_restart.get(f"/api/runs/{entry['run_id']}/events")
            entry["restart_recovery"] = {"run_http_status": reread.status_code,
                "events_http_status": reread_events.status_code,
                "run_status": (reread.json().get("run") or {}).get("status") if reread.status_code == 200 else None,
                "model_request_delta": len(ledger.records) - before_restart_calls,
                "idempotent_replay_http_status": None}
            restart_replay = client_after_restart.post("/api/chat/stream", headers=headers_after_restart, json=request)
            restart_replay_events = _parse_sse(restart_replay.text)
            entry["restart_recovery"]["idempotent_replay_http_status"] = restart_replay.status_code
            entry["restart_recovery"]["same_run"] = bool(restart_replay_events and
                restart_replay_events[0].get("run_id") == entry["run_id"])
            entry["restart_recovery"]["model_request_delta"] = len(ledger.records) - before_restart_calls
            entry["restart_recovery"]["passed"] = (entry["restart_recovery"]["run_http_status"] == 200 and
                entry["restart_recovery"]["events_http_status"] == 200 and
                entry["restart_recovery"]["run_status"] == entry["run_status"] and
                entry["restart_recovery"]["same_run"] and entry["restart_recovery"]["model_request_delta"] == 0)
            if not entry["restart_recovery"]["passed"]:
                entry["status"] = "FAIL"
        if entry.get("run_status") in {"MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}:
            entry["status"] = "BLOCKED"
        return entry
    finally:
        retrieval.retrieve = original_retrieve
        try:
            app.state.run_executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass
        report.setdefault("chat_runs", []).append(entry)
        report["request_totals"] = ledger.totals()
        ledger.persist()


def _judge_answer(provider, ledger: RequestLedger, case: dict, run: dict, report: dict) -> dict:
    answer = run.get("answer") or ""
    citations = run.get("citations") or []
    if not answer or not citations or run.get("answer_kind") != "POLICY_ANSWER":
        return {"case_id": case["case_id"], "status": "NOT_RUN", "reason": "no_scored_policy_answer"}
    evidence_by_id = {row.get("chunk_id"): row for row in run.get("evidence") or [] if isinstance(row, dict)}
    cited = []
    for item in citations:
        evidence_id = item.get("chunk_id") if isinstance(item, dict) else item
        if evidence_id in evidence_by_id:
            row = evidence_by_id[evidence_id]
            cited.append({"chunk_id": evidence_id, "title": row.get("title"), "content": row.get("content"),
                          "version": row.get("version"), "effective_from": row.get("effective_from"),
                          "effective_to": row.get("effective_to")})
    if not cited:
        return {"case_id": case["case_id"], "status": "NOT_RUN", "reason": "no_valid_cited_evidence"}
    schema = {"type": "object", "additionalProperties": False,
        "required": ["verdict", "unsupported_claims", "missed_core_points", "citation_checks", "clarification_quality"],
        "properties": {"verdict": {"type": "string", "enum": ["SUPPORTED", "UNSUPPORTED", "UNCERTAIN"]},
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
            "missed_core_points": {"type": "array", "items": {"type": "string"}},
            "citation_checks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["chunk_id", "status", "reason"], "properties": {
                    "chunk_id": {"type": "string"}, "status": {"type": "string", "enum": ["SUPPORTS", "IRRELEVANT", "CONTRADICTS"]},
                    "reason": {"type": "string"}}}},
            "clarification_quality": {"type": "string", "enum": ["APPROPRIATE", "UNNECESSARY", "NOT_APPLICABLE"]}}}
    prompt = ("Act as a blind, conservative policy-answer evaluator. You are not told which retrieval or orchestration method produced the answer. "
        "Use only the exact user question and cited policy excerpts below. Do not infer unverified order facts. "
        "Mark UNSUPPORTED if the answer adds a guarantee, deadline, prerequisite, or operational claim not established by the evidence; "
        "mark UNCERTAIN if the evidence is incomplete or ambiguous. Identify omitted core points only when directly required by the question. "
        "A clarification is appropriate only when a missing policy or real-world fact prevents a safe answer. "
        "Return one JSON object matching this schema, no extra text: " + json.dumps(schema, ensure_ascii=False))
    payload = {"question": case["user_query"], "answer": answer, "cited_policy_evidence": cited}
    previous = getattr(ledger.local, "node", None)
    ledger.local.node = "blind_quality_judge"
    try:
        response = provider.client.chat.completions.create(model=provider.settings.model,
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            response_format={"type": "json_object"}, extra_body={"thinking": {"type": "disabled"}},
            max_tokens=768, timeout=20)
    except Exception as exc:
        return {"case_id": case["case_id"], "status": "FAIL", "exception_type": type(exc).__name__,
                "http_status": getattr(exc, "status_code", None), "review_status": "PENDING_HUMAN_REVIEW"}
    finally:
        ledger.local.node = previous
    choice = response.choices[0]
    content = choice.message.content or ""
    try:
        judged = json.loads(content)
        if set(judged) != set(schema["required"]):
            raise ValueError("judge schema keys incomplete or extra")
        if (judged.get("verdict") not in {"SUPPORTED", "UNSUPPORTED", "UNCERTAIN"} or
                not isinstance(judged.get("unsupported_claims"), list) or
                not all(isinstance(value, str) for value in judged["unsupported_claims"]) or
                not isinstance(judged.get("missed_core_points"), list) or
                not all(isinstance(value, str) for value in judged["missed_core_points"]) or
                judged.get("clarification_quality") not in {"APPROPRIATE", "UNNECESSARY", "NOT_APPLICABLE"}):
            raise ValueError("judge schema values invalid")
        if (not isinstance(judged.get("citation_checks"), list) or
                len(judged["citation_checks"]) != len(cited) or
                {row.get("chunk_id") for row in judged["citation_checks"]} != {row["chunk_id"] for row in cited} or
                any(row.get("status") not in {"SUPPORTS", "IRRELEVANT", "CONTRADICTS"} or
                    not isinstance(row.get("reason"), str) for row in judged["citation_checks"])):
            raise ValueError("judge did not cover every cited evidence ID")
        status = "PASS"
        diagnostic = None
    except Exception as exc:
        judged = None
        status = "FAIL"
        diagnostic = type(exc).__name__
    usage = _usage_dict(response)
    return {"case_id": case["case_id"], "status": status,
            "judge": judged, "raw_content": content, "finish_reason": getattr(choice, "finish_reason", None),
            "usage": usage, "latency_ms": ledger.records[-1].get("latency_ms") if ledger.records else None,
            "estimated_cost_usd": _request_cost(usage), "validation_error_type": diagnostic,
            "review_status": "PENDING_HUMAN_REVIEW", "not_a_human_approval": True}


def _quality_summary(report: dict) -> dict:
    unique: dict[str, dict] = {}
    for row in report.get("chat_runs", []):
        if row.get("orchestrator_mode") == "legacy":
            # Continuation records retain every historical run; summarize the
            # latest explicit run for each case while keeping prior rows raw.
            unique[row.get("case_id")] = row
    scoreable = [row for row in unique.values() if row.get("scoreability") == "SCORABLE_CANDIDATE"]
    judge_by_case = {row.get("case_id"): row for row in report.get("judgements", [])}
    split_summary = {}
    for split in ("dev", "validation"):
        current = [row for row in scoreable if row.get("dataset_split") == split]
        judged = [judge_by_case[row["case_id"]].get("judge") for row in current
                  if isinstance(judge_by_case.get(row["case_id"]), dict)
                  and isinstance(judge_by_case[row["case_id"]].get("judge"), dict)]
        split_summary[split] = {"scoreable_denominator": len(current),
            "policy_answer_n": sum(bool(row.get("policy_answer_valid")) for row in current),
            "route_correct_n": sum(bool(row.get("strategy_matches_candidate")) for row in current),
            "mean_retrieved_required_evidence_coverage": (round(sum(row["required_evidence_coverage"] or 0
                for row in current) / len(current), 6) if current else None),
            "judge_verdicts": {value: sum(j.get("verdict") == value for j in judged)
                               for value in ("SUPPORTED", "UNSUPPORTED", "UNCERTAIN")},
            "judge_labels_pending_human_review": True}
    latencies = [float(row["latency_ms"]) for row in scoreable if isinstance(row.get("latency_ms"), (int, float))]
    all_judges = [row.get("judge") for row in report.get("judgements", []) if isinstance(row.get("judge"), dict)]
    retrieval = report.get("retrieval_only") or {}
    safe_failures = [{"case_id": row.get("case_id"), "run_status": row.get("run_status"),
                      "error_code": ((row.get("run_error") or {}).get("code")),
                      "actual_strategy": row.get("actual_strategy")}
                     for row in scoreable if row.get("run_status") != "SUCCEEDED"]
    return {"scoreable_denominator": len(scoreable),
        "policy_answer_valid_n": sum(bool(row.get("policy_answer_valid")) for row in scoreable),
        "policy_answer_valid_rate": (round(sum(bool(row.get("policy_answer_valid")) for row in scoreable) / len(scoreable), 6)
                                      if scoreable else None),
        "route_matches_candidate_n": sum(bool(row.get("strategy_matches_candidate")) for row in scoreable),
        "route_accuracy_on_this_small_selected_set": (round(sum(bool(row.get("strategy_matches_candidate")) for row in scoreable) / len(scoreable), 6)
                                                       if scoreable else None),
        "required_evidence_retrieval_coverage_mean": (round(sum(row.get("required_evidence_coverage") or 0 for row in scoreable) / len(scoreable), 6)
                                                       if scoreable else None),
        "required_evidence_citation_coverage_mean": (round(sum(row.get("required_evidence_citation_coverage") or 0 for row in scoreable) / len(scoreable), 6)
                                                      if scoreable else None),
        "end_to_end_latency_ms": {"p50_nearest_rank": _nearest_percentile(latencies, .50),
                                  "p95_nearest_rank": _nearest_percentile(latencies, .95)},
        "judge_verdicts": {value: sum(row.get("verdict") == value for row in all_judges)
                           for value in ("SUPPORTED", "UNSUPPORTED", "UNCERTAIN")},
        "judge_failures_or_not_run": sum(row.get("status") != "PASS" for row in report.get("judgements", [])),
        "safe_failed_or_non_answer_runs": safe_failures,
        "run_status_counts": {status: sum(row.get("run_status") == status for row in scoreable)
                              for status in ("SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT")},
        "behavior_only": [{"case_id": row.get("case_id"), "run_status": row.get("run_status"),
            "answer_kind": row.get("answer_kind"), "excluded_from_retrieval_denominator": True}
            for row in unique.values() if row.get("scoreability") != "SCORABLE_CANDIDATE"],
        "retrieval_only_metrics": retrieval.get("summary"),
        "by_split": split_summary,
        "interpretation": "selected APPROVED dev/validation regression and diagnostic samples only; not a formal M2 quality release or independent generalization estimate"}


def _write_human_review(report: dict, output_path: Path) -> str | None:
    rows_by_case = {}
    judge_by_case = {row.get("case_id"): row for row in report.get("judgements", [])}
    for row in report.get("chat_runs", []):
        if row.get("orchestrator_mode") == "legacy":
            rows_by_case[row.get("case_id")] = row
    if not rows_by_case:
        return None
    name = output_path.stem.replace("m2-r-real-", "m2-r-answer-review-") + ".csv"
    path = ROOT / "eval/fidelity" / name
    columns = ["case_id", "dataset_split", "user_query", "run_status", "answer_kind", "answer",
               "cited_policy_evidence_json", "judge_verdict", "judge_findings_json",
               "review_status", "reviewer", "reviewed_at_utc", "review_notes"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for case_id, row in rows_by_case.items():
            citations = row.get("citations") or []
            cited = {item.get("chunk_id") if isinstance(item, dict) else item for item in citations}
            evidence = [item for item in row.get("evidence") or [] if item.get("chunk_id") in cited]
            judge = judge_by_case.get(case_id) or {}
            writer.writerow({"case_id": case_id, "dataset_split": row.get("dataset_split"),
                "user_query": row.get("user_query"), "run_status": row.get("run_status"),
                "answer_kind": row.get("answer_kind"), "answer": row.get("answer"),
                "cited_policy_evidence_json": json.dumps(evidence, ensure_ascii=False),
                "judge_verdict": (judge.get("judge") or {}).get("verdict"),
                "judge_findings_json": json.dumps(judge.get("judge"), ensure_ascii=False) if judge.get("judge") else "",
                "review_status": "PENDING_HUMAN_REVIEW", "reviewer": "", "reviewed_at_utc": "", "review_notes": ""})
    return str(path.relative_to(ROOT)).replace("\\", "/")


def _provider_reachability(settings) -> dict:
    import httpx
    from urllib.parse import urlsplit
    host = urlsplit(settings.base_url).hostname
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    proxy_state = "UNSET" if not any(os.environ.get(name) for name in proxy_names) else "PRESENT"
    try:
        response = httpx.get(settings.base_url.rstrip("/") + "/", timeout=8, trust_env=True)
        reachable = response.status_code in {200, 301, 302, 307, 308, 401, 403, 404}
        return {"status": ("REACHABLE_UNAUTHENTICATED" if response.status_code == 401 else
                            "REACHABLE" if reachable else "UNEXPECTED_HTTP_STATUS"),
                "http_status": response.status_code, "host": host, "api_key_sent": False,
                "proxy_environment": proxy_state,
                "process_scope_only": "inherited invalid proxy variables, if any, must be cleared only for this invocation"}
    except Exception as exc:
        return {"status": "NETWORK_BLOCKED", "host": host, "exception_type": type(exc).__name__,
                "api_key_sent": False, "proxy_environment": proxy_state}


def _db_revision_matches(engine) -> bool:
    from app.m1.db import current_revision
    return current_revision(str(engine.url)) == EXPECTED_REVISION


def _execute_real(report_path: Path, resume_path: Path | None = None,
                  rerun_dev_case: str | None = None) -> dict:
    import httpx
    from sqlalchemy.engine import URL
    from app.m0.config import load_settings
    from app.m1.chat import DeepSeekProvider
    from app.m2.retrieval import RetrievalService, config_from_environment
    from qdrant_client import QdrantClient
    from dataclasses import replace

    cases_by_id, manifest_sha = _load_manifest()
    if rerun_dev_case is not None and rerun_dev_case not in {case_id for case_id, _ in DEV_RUNS}:
        raise ValueError("only a fixed approved dev regression case may be rerun")
    resumed = None
    if resume_path is not None:
        resumed = json.loads(resume_path.read_text(encoding="utf-8"))
        if (resumed.get("record_type") != "m2_r_real_quality_probe" or
                (resumed.get("dataset") or {}).get("manifest_sha256") != manifest_sha or
                (resumed.get("dataset") or {}).get("test_candidate_rows_included") is not False):
            raise ValueError("resume record does not match the fixed approved manifest")
    source_sha = EXPECTED_SOURCE_SHA
    cases = list(cases_by_id.values())
    prior_requests, prior_cost, prior_cost_known = _prior_totals()
    settings = load_settings(require_api_key=True)
    report: dict = {"record_type": "m2_r_real_quality_probe", "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "RUNNING", "scope": "M2-R approved dev/validation real quality diagnostic; not M6/test_candidate",
        "budget": {"max_stage_deepseek_requests": MAX_STAGE_REQUESTS, "max_stage_estimated_cost_usd": MAX_STAGE_COST_USD,
                   "sdk_max_retries": 0, "includes": ["route", "policy aspect planning", "answer generation", "blind Judge", "failed HTTP attempts"]},
        "dataset": {"manifest_path": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"), "manifest_sha256": manifest_sha,
                    "source_dataset_sha256": source_sha, "fixed_case_ids": list(cases_by_id),
                    "case_count": len(cases), "test_candidate_rows_included": False,
                    "test_candidate_used_for_tuning": False,
                    "scoreable_n": sum(case.get("scoreability") == "SCORABLE_CANDIDATE" for case in cases),
                    "behavior_only_n": sum(case.get("scoreability") != "SCORABLE_CANDIDATE" for case in cases)},
        "configuration": {"model_id": settings.model, "model_source": settings.model_source,
            "base_url_host": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(settings.base_url).hostname,
            "api_key_configured": bool(settings.api_key), "api_key_source": settings.api_key_source,
            "retrieval_mode_default": "BM25", "retrieval_config_version": config_from_environment().version,
            "retrieval_config_hash": replace(config_from_environment(), mode="bm25", qdrant_mode="local", qdrant_url=None).config_hash,
            "knowledge_index_path": str(INDEX_PATH.relative_to(ROOT)).replace("\\", "/"),
            "fixed_node_settings": {name: {"thinking": node.thinking, "max_tokens": node.max_tokens,
                "timeout_seconds": node.timeout_seconds, "max_cost_usd": node.max_cost_usd}
                for name, node in settings.nodes.items() if name in {"route", "policy_answer", "policy_aspect_planning"}}},
        "cost_method": {"input_usd_per_million": PRICE_INPUT_USD_PER_M,
            "cached_input_usd_per_million": PRICE_CACHED_INPUT_USD_PER_M,
            "output_usd_per_million": PRICE_OUTPUT_USD_PER_M, "source": PRICE_SOURCE,
            "is_estimate_not_invoice": True},
        "prior_m2_r_ledger": {"request_count": prior_requests, "estimated_cost_usd": prior_cost,
                              "cost_known": prior_cost_known},
        "chat_runs": list((resumed or {}).get("chat_runs") or []),
        "judgements": list((resumed or {}).get("judgements") or []),
        "retrieval_only": (resumed or {}).get("retrieval_only"),
        "continued_from": ({"path": str(resume_path.relative_to(ROOT)).replace("\\", "/"),
                            "sha256": _sha(resume_path.read_bytes()),
                            "request_count": (resumed.get("request_totals") or {}).get("stage_request_count")}
                           if resumed is not None else None),
        "limitations": ["M2.4 known/dev samples are regressions, not independent generalization evidence.",
            "Judge output is auxiliary and remains PENDING_HUMAN_REVIEW.",
            "Validation cases run once after the fixed M2-R implementation; no tuning follows their results."]}
    ledger = RequestLedger(report, report_path, api_key=settings.api_key, prior_requests=prior_requests,
                           prior_cost=prior_cost, prior_cost_known=prior_cost_known)
    provider = DeepSeekProvider(settings=settings)
    ledger.install(provider)
    engine = None
    qdrant = None
    _write_json(report_path, report, settings.api_key)
    report["network_preflight"] = _provider_reachability(settings)
    if report["network_preflight"]["status"] not in {"REACHABLE_UNAUTHENTICATED", "REACHABLE"}:
        report["status"] = "NETWORK_BLOCKED"
        report["stop_reason"] = "unauthenticated HTTPS preflight failed; no model request sent"
        ledger.persist()
        ledger.uninstall()
        return report

    try:
        database_url, engine, qdrant, retrieval, settings, revision, db_name, db_version, cache = _load_db_and_models()
        report["database"] = {"name": db_name, "revision": revision, "version": db_version,
            "host": database_url.host, "port": database_url.port, "write_scope": "dedicated test DB; no migration or clearing"}
        report["model_cache"] = cache
        report["database_fingerprint_before"] = _db_fingerprint(engine)
        view = retrieval.pin_view()
        report["knowledge"] = {"version": view.index.get("knowledge_version"), "index_fingerprint": view.fingerprint,
            "source_index_sha256": view.index.get("source_sha256"), "evaluation_policy": "fixed per approved case UTC time"}
        report["historical_retrieval_baseline"] = _read_historical_baseline(source_sha)
        report["model_cache"]["model_revisions"] = {
            "embedding": "BAAI/bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620",
            "reranker": "BAAI/bge-reranker-base@2cfc18c9415c912f9d8155881c133215df768a70"}
        if cache["bge_small_present"] and cache["reranker_base_present"]:
            model_loads = {"embedding_load_ms": None, "reranker_load_ms": None}
            start = time.perf_counter()
            retrieval._models()
            model_loads["embedding_load_ms"] = round((time.perf_counter() - start) * 1000, 3)
            start = time.perf_counter()
            retrieval._models(need_ranker=True)
            model_loads["reranker_load_ms"] = round((time.perf_counter() - start) * 1000, 3)
            retrieval._m2r_model_load_ms = model_loads
        else:
            retrieval._m2r_model_load_ms = {"embedding_load_ms": None, "reranker_load_ms": None,
                                             "status": "BLOCKED_CACHE_MISSING_NO_DOWNLOAD"}
        if report["retrieval_only"] is None:
            report["retrieval_only"] = _retrieval_only([case for case in cases if case.get("scoreability") == "SCORABLE_CANDIDATE"], retrieval)
        ledger.persist()
        retrieval_summary = report["retrieval_only"]["summary"]
        if (retrieval_summary.get("BM25", {}).get("status") != "PASS" or
                retrieval_summary.get("HYBRID_RERANK", {}).get("status") != "PASS" or
                retrieval_summary.get("HYBRID_RERANK", {}).get("degraded_n") != 0):
            report["status"] = "BLOCKED"
            report["stop_reason"] = "local retrieval-only preflight failed; no paid model calls sent"
            report["quality_status"] = "BLOCKED"
            ledger.persist()
            return report

        if resumed is None:
            user_id, password = _create_probe_user(engine)
            report["probe_identity"] = {"created": True, "user_id": user_id, "password_recorded": False}
        else:
            user_id = (resumed.get("probe_identity") or {}).get("user_id")
            if not isinstance(user_id, str):
                raise ValueError("resume record is missing its isolated probe identity")
            password = _reset_probe_password(engine, user_id)
            report["probe_identity"] = {"reused_generated_identity": True, "user_id": user_id,
                                        "password_recorded": False}
            for row in report["chat_runs"]:
                if (row.get("run_id") and (not row.get("idempotent_replay") or
                                            not row.get("restart_recovery"))):
                    case = cases_by_id.get(row.get("case_id"))
                    if case is None:
                        continue
                    recovery = _recover_existing_case(case, row,
                        row.get("orchestrator_mode", "legacy"),
                        database_url=database_url.render_as_string(hide_password=False),
                        settings=settings, engine=engine, provider=provider, retrieval=retrieval,
                        ledger=ledger, user_id=user_id, password=password)
                    row["restart_recovery"] = recovery
                    row["idempotent_replay"] = {"passed": recovery.get("passed"),
                        "model_request_delta": recovery.get("model_request_delta"),
                        "same_run": recovery.get("same_run")}
                    if recovery.get("checkpoint") is not None:
                        row["checkpoint"] = recovery["checkpoint"]
                    if recovery.get("graph") is not None:
                        row["graph"] = recovery["graph"]
                    if row.get("sse_event_types"):
                        row["sse_order_valid"] = _sse_order_valid(row["sse_event_types"])
                        row["sse_order_reconciled"] = "error is a valid terminal event for a failed Run"
                    ledger.persist()
        planned_runs = [(case_id, "legacy") for case_id, _ in DEV_RUNS]
        planned_runs += [(case_id, "legacy") for case_id, _ in VALIDATION_RUNS]
        planned_runs += [BEHAVIOR_RUN]
        planned_runs += list(ORCHESTRATOR_PARITY_RUNS)
        completed_runs = {(row.get("case_id"), row.get("orchestrator_mode"))
                          for row in report["chat_runs"] if row.get("run_id")}
        forced_rerun = ({(rerun_dev_case, mode) for mode in ("legacy", "langgraph")}
                        if rerun_dev_case else set())
        all_runs = [item for item in planned_runs if item not in completed_runs or item in forced_rerun]
        report["run_plan"] = [{"case_id": case_id, "orchestrator_mode": mode,
                               "split": cases_by_id[case_id]["dataset_split"],
                               "scoreability": cases_by_id[case_id].get("scoreability"),
                               "already_completed": (case_id, mode) in completed_runs,
                               "explicit_dev_regression_rerun": (case_id, mode) in forced_rerun}
                              for case_id, mode in planned_runs]
        report["skipped_previously_completed_runs"] = [
            {"case_id": case_id, "orchestrator_mode": mode} for case_id, mode in planned_runs
            if (case_id, mode) in completed_runs]
        for case_id, mode in all_runs:
            if prior_requests + len(ledger.records) >= MAX_STAGE_REQUESTS:
                report["status"] = "REQUEST_BUDGET_EXHAUSTED"
                report["stop_reason"] = "M2-R stage request budget reached before next case"
                break
            case = cases_by_id[case_id]
            entry = _run_api_case(case, mode, database_url=database_url.render_as_string(hide_password=False),
                settings=settings, engine=engine, provider=provider, retrieval=retrieval, ledger=ledger,
                user_id=user_id, password=password, report=report, output_path=report_path)
            if entry.get("status") == "BLOCKED" and any(row.get("status") == "ERROR" and row.get("exception_type") in
                    {"APIConnectionError", "APITimeoutError", "AuthenticationError", "RateLimitError"}
                    for row in ledger.records[-2:]):
                report["status"] = "BLOCKED"
                report["stop_reason"] = "provider transport/authentication/rate limit failed; no automatic retry"
                break
        # Blindly judge only the first, unique legacy output for each allowlisted case.
        already_judged: set[str] = {row.get("case_id") for row in report.get("judgements", [])
                                    if row.get("status") in {"PASS", "FAIL"}}
        for run in report.get("chat_runs", []):
            case_id = run.get("case_id")
            if case_id in already_judged or run.get("orchestrator_mode") != "legacy":
                continue
            case = cases_by_id.get(case_id)
            if (case is None or case.get("scoreability") != "SCORABLE_CANDIDATE" or
                    run.get("run_status") != "SUCCEEDED" or run.get("answer_kind") != "POLICY_ANSWER"):
                continue
            already_judged.add(case_id)
            if prior_requests + len(ledger.records) >= MAX_STAGE_REQUESTS:
                report["judgements"].append({"case_id": case_id, "status": "NOT_RUN", "reason": "request_budget_guard"})
                continue
            judged = _judge_answer(provider, ledger, case, run, report)
            report["judgements"].append(judged)
            ledger.persist()
            if judged.get("status") == "FAIL" and ledger.records and ledger.records[-1].get("exception_type") in {
                    "APIConnectionError", "APITimeoutError", "AuthenticationError", "RateLimitError"}:
                report["status"] = "BLOCKED"
                report["stop_reason"] = "blind judge transport/authentication/rate limit failed; no retry"
                break
        report["database_fingerprint_after"] = _db_fingerprint(engine)
        report["seed_orders_and_policy_count_unchanged"] = (
            report["database_fingerprint_before"] == report["database_fingerprint_after"])
        # Reconcile event ordering for every row, including failed-run records
        # imported from an older interrupted probe revision.
        for row in report.get("chat_runs", []):
            event_types = row.get("sse_event_types")
            if isinstance(event_types, list):
                row["sse_order_valid"] = _sse_order_valid(event_types)
                if "error" in event_types:
                    row["sse_order_reconciled"] = "error is a valid terminal event for a failed Run"
        report["verification"] = {"db_seed_orders_and_policy_count_unchanged": report["seed_orders_and_policy_count_unchanged"],
            "all_run_statuses_terminal": all(row.get("run_status") not in {"RUNNING", None}
                                              for row in report.get("chat_runs", [])),
            "all_run_statuses_match_database": all(row.get("run_status_matches_db") is True
                                                    for row in report.get("chat_runs", [])),
            "all_sse_orders_valid": all(row.get("sse_order_valid") for row in report.get("chat_runs", [])),
            "all_same_key_replays_no_model_call": all((row.get("idempotent_replay") or {}).get("passed")
                and (row.get("restart_recovery") or {}).get("passed") for row in report.get("chat_runs", [])),
            "all_langgraph_checkpoint_statuses_match_run": all(
                row.get("orchestrator_mode") != "langgraph" or (
                    isinstance(row.get("checkpoint"), dict) and
                    row["checkpoint"].get("checkpoint_id") and
                    row["checkpoint"].get("terminal_status") == row.get("run_status") and
                    isinstance(row.get("graph"), dict) and
                    row["graph"].get("status") == row.get("run_status") and
                    row["graph"].get("checkpoint_id") == row["checkpoint"].get("checkpoint_id"))
                for row in report.get("chat_runs", [])),
            "all_successful_policy_answers_cited_current_retrieval": all(
                row.get("citation_ids_subset_of_retrieval") for row in report.get("chat_runs", [])
                if row.get("answer_kind") == "POLICY_ANSWER"),
            "test_candidate_rows_included": False}
        status_ok = _verification_passes(report["verification"])
        if report.get("status") == "RUNNING":
            report["status"] = "PASS" if status_ok else "FAIL"
        report["engineering_status"] = "PASS" if status_ok else "FAIL"
        report["quality_status"] = "BLOCKED"  # M2 quality/human gates are unchanged by a small smoke.
        report["m2_formal_release"] = "BLOCKED"
        report["m3_authorized"] = False
        report["human_review"] = {"answer_disputes": "PENDING_HUMAN_REVIEW",
            "route_candidates_dev_validation": 52, "route_candidate_status": "PENDING_REVIEW",
            "test_candidate_frozen": False}
        report["quality_summary"] = _quality_summary(report)
        report["human_review_material"] = _write_human_review(report, report_path)
        ledger.persist()
        return report
    except Exception as exc:
        report["status"] = "BLOCKED"
        original = getattr(exc, "orig", None)
        diagnostic = {"exception_type": type(exc).__name__,
                             "stage": "preflight_database_or_execution",
                             "message": "details omitted to avoid exposing local credentials"}
        sqlstate = getattr(original, "sqlstate", None) or getattr(
            getattr(original, "diag", None), "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            diagnostic["sqlstate"] = sqlstate
        if type(exc).__name__ == "OperationalError":
            diagnostic["category"] = ("POSTGRES_CONNECTION_FAILURE" if
                isinstance(sqlstate, str) and sqlstate.startswith("08") else "DATABASE_OPERATIONAL_FAILURE")
        report["failure"] = diagnostic
        report["request_totals"] = ledger.totals()
        ledger.persist()
        return report
    finally:
        ledger.uninstall()
        if qdrant is not None:
            try:
                qdrant.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="run the capped DeepSeek/PostgreSQL quality smoke")
    parser.add_argument("--output", default=None, help="optional output JSON path under docs/probes")
    parser.add_argument("--resume-from", default=None,
                        help="continue an incomplete M2-R record without repeating completed case/mode pairs")
    parser.add_argument("--rerun-dev-case", default=None,
                        help="one fixed dev regression case in both orchestrators; validation cases cannot be rerun")
    args = parser.parse_args()
    if not args.real:
        parser.error("real requests require the explicit --real flag; no request was sent")
    path = Path(args.output) if args.output else ROOT / "docs/probes" / f"m2-r-real-{_utc_stamp()}.json"
    if not path.is_absolute():
        path = ROOT / path
    if path.suffix.lower() != ".json" or ROOT not in path.parents:
        parser.error("output must be a JSON path inside the ShopRelay repository")
    resume_path = Path(args.resume_from) if args.resume_from else None
    if resume_path is not None and not resume_path.is_absolute():
        resume_path = ROOT / resume_path
    if resume_path is not None and (ROOT not in resume_path.parents or not resume_path.is_file()):
        parser.error("resume record must be an existing JSON path inside the ShopRelay repository")
    report = _execute_real(path, resume_path=resume_path, rerun_dev_case=args.rerun_dev_case)
    print(json.dumps({"status": report.get("status"), "output": str(path.relative_to(ROOT)).replace("\\", "/"),
        "request_totals": report.get("request_totals"), "chat_run_count": len(report.get("chat_runs", [])),
        "judge_count": len(report.get("judgements", [])), "stop_reason": report.get("stop_reason")}, ensure_ascii=False))
    return 0 if report.get("status") in {"PASS", "NETWORK_BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
