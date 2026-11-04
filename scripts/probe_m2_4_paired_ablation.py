"""Bounded M2.4 paired answer-quality ablation using approved dev/validation only.

The default mode is plan-only. --execute performs real calls, with a global
120-request / $5 estimate guard and OpenAI SDK retries disabled. The per-Run
Self-RAG limits are left at their existing defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

import httpx
from qdrant_client import QdrantClient

from app.m0.config import NodeConfig, create_deepseek_client, load_settings
from app.m1.chat import DeepSeekProvider
from app.m2.retrieval import (RERANKER_REV, SMALL_REV, RetrievalConfig,
                              RetrievalService, config_from_environment)
from app.m2.self_rag import MultiHopRetrievalService, SelfRAGConfig

DATASET_PATH = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
DATASET_SHA256 = "0daea3b85052e34217e5249211bbd7cf3c51d3fce871e2f00c0717e3632acf52"
M2_2_RAW = ROOT / "eval/raw/m2.2-retrieval-20261019T140452750330Z-d483d4c7.json"
HISTORICAL_RUN = ROOT / "docs/probes/m2.3-postgres-deepseek_real-20261020T025934Z.json"
SELECTED_CASES = [
    "pr2-refund_shipped_unreceived-01",  # dev: shipped/undelivered exception
    "pr2-refund_delivered_return-03",    # dev: strong refutation of pending != approved
    "pr2-multi_clause_return_refund-03", # dev: previously seen M2.3 regression case
    "pr2-delivery_policy_limits-01",    # validation: policy cannot prove live delivery fact
    "pr2-return_quality_expired-03",     # validation: 30-day boundary and manual review
    "pr2-return_quality_conditions-02", # validation: food/product policy exception
    "pr2-warranty_specific_missing-01",  # validation: behavior-only, not retrieval-scored
]
ARMS = ["single_hop_bm25", "single_hop_rrf_reranker", "self_rag_max_3_hops"]
REQUEST_CAP = 120
COST_CAP_USD = 5.0
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
JUDGE_PROMPT_VERSION = "m2.4-blinded-claim-judge-r1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def cost_from_usage(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached = min(prompt, int(details.get("cached_tokens") or details.get("cache_hit_tokens") or 0))
    return round(((prompt - cached) * .30 + cached * .006 + completion * 1.20) / 1_000_000, 9)


class StageBudgetExceeded(RuntimeError):
    pass


class StageBudget:
    """Counts a request before it can be sent; no SDK retry is enabled."""
    def __init__(self, *, max_new_requests: int = REQUEST_CAP,
                 max_new_cost_usd: float = COST_CAP_USD):
        self.requests = 0
        self.estimated_cost_usd = 0.0
        self.unknown_cost_reserve_usd = 0.0
        self.baseline_requests = 0
        self.baseline_cost_usd = 0.0
        self.max_new_requests = max_new_requests
        self.max_new_cost_usd = max_new_cost_usd
        self.calls: list[dict] = []
        self.on_call = None

    def seed_prior(self, requests: int, estimated_cost_usd: float) -> None:
        if self.calls or self.requests:
            raise RuntimeError("cannot seed a started stage budget")
        if requests < 0 or estimated_cost_usd < 0:
            raise ValueError("prior stage accounting must be non-negative")
        self.requests = self.baseline_requests = requests
        self.estimated_cost_usd = self.baseline_cost_usd = estimated_cost_usd

    def _saved(self):
        if self.on_call:
            self.on_call()

    def _reserve(self, node: NodeConfig, text: str) -> float:
        estimated_input = max(1, len(text.encode("utf-8")) // 2)
        return round((estimated_input * .30 + node.max_tokens * 1.20) / 1_000_000, 9)

    def call(self, node: NodeConfig, label: str, input_text: str, fn):
        reserve = self._reserve(node, input_text)
        if len(self.calls) >= self.max_new_requests:
            raise StageBudgetExceeded("stage_request_cap")
        new_estimate = (self.estimated_cost_usd + self.unknown_cost_reserve_usd + reserve -
                        self.baseline_cost_usd)
        if new_estimate > self.max_new_cost_usd:
            raise StageBudgetExceeded("stage_cost_cap_preflight")
        self.requests += 1
        started = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            usage = diagnostic.get("usage") if isinstance(diagnostic, dict) else None
            measured = cost_from_usage(usage)
            charge = measured if measured is not None else reserve
            if measured is None:
                self.unknown_cost_reserve_usd += charge
            else:
                self.estimated_cost_usd += measured
            self.calls.append({"sequence": self.requests, "node": label, "status": "FAILED",
                "exception_type": type(exc).__name__, "error_code": getattr(exc, "code", None),
                "provider_status": diagnostic.get("provider_status") if isinstance(diagnostic, dict) else None,
                "finish_reason": diagnostic.get("finish_reason") if isinstance(diagnostic, dict) else None,
                "raw_content": diagnostic.get("raw_content") if isinstance(diagnostic, dict) else None,
                "usage": usage, "latency_ms": round((time.perf_counter() - started) * 1000),
                "estimated_cost_usd": charge, "cost_basis": "usage" if measured is not None else "conservative_preflight_reserve"})
            self._saved()
            raise
        usage = result.get("usage") if isinstance(result, dict) else None
        measured = cost_from_usage(usage)
        if measured is None and isinstance(result, dict):
            try:
                measured = float(result.get("estimated_cost_usd"))
            except (TypeError, ValueError):
                measured = None
        charge = reserve if measured is None else measured
        if measured is None:
            self.unknown_cost_reserve_usd += charge
        else:
            self.estimated_cost_usd += measured
        self.calls.append({"sequence": self.requests, "node": label, "status": "COMPLETED" if measured is not None else "USAGE_UNKNOWN",
            "model_id": result.get("model_id"), "finish_reason": result.get("finish_reason"),
            "provider_request_id": result.get("provider_request_id"),
            "usage": usage, "latency_ms": result.get("latency_ms") or round((time.perf_counter() - started) * 1000),
            "estimated_cost_usd": charge, "cost_basis": "provider_usage_peak_rate_estimate" if measured is not None else "conservative_preflight_reserve",
            "raw_content": result.get("raw_content"),
            "prompt_version": result.get("prompt_version"), "thinking": result.get("thinking"),
            "max_tokens": result.get("max_tokens", node.max_tokens), "timeout_seconds": result.get("timeout_seconds", node.timeout_seconds)})
        self._saved()
        return result


class BudgetedProvider:
    def __init__(self, inner: DeepSeekProvider, budget: StageBudget):
        self.inner = inner
        self.settings = inner.settings
        self.budget = budget

    def answer(self, question: str, evidence: list[dict]) -> dict:
        payload = json.dumps({"question": question, "evidence": evidence}, ensure_ascii=False)
        return self.budget.call(self.settings.nodes["policy_answer"], "policy_answer", payload,
                                lambda: self.inner.answer(question, evidence))

    def extract_conditions(self, question: str) -> dict:
        return self.budget.call(self.settings.nodes["condition_extraction"], "condition_extraction", question,
                                lambda: self.inner.extract_conditions(question))

    def grade_evidence(self, question, conditions, evidence, evaluation_at, **kwargs) -> dict:
        payload = json.dumps({"question": question, "conditions": conditions, "evidence": evidence,
                              "evaluation_at": evaluation_at, **kwargs}, ensure_ascii=False)
        return self.budget.call(self.settings.nodes["evidence_sufficiency"], "evidence_sufficiency", payload,
                                lambda: self.inner.grade_evidence(question, conditions, evidence, evaluation_at, **kwargs))


def load_inputs() -> tuple[dict, list[dict], str, str]:
    raw = DATASET_PATH.read_bytes()
    dataset_sha = sha256(raw)
    if dataset_sha != DATASET_SHA256:
        raise RuntimeError(f"reviewed dataset SHA mismatch: {dataset_sha}")
    dataset = json.loads(raw)
    by_id = {case["case_id"]: case for case in dataset["cases"]}
    cases = []
    for case_id in SELECTED_CASES:
        case = by_id.get(case_id)
        if case is None or case.get("dataset_split") not in {"dev", "validation"}:
            raise RuntimeError(f"selected case is missing or outside dev/validation: {case_id}")
        if case.get("review_status") != "APPROVED":
            raise RuntimeError(f"selected case is not APPROVED: {case_id}")
        cases.append(case)
    baseline_bytes = M2_2_RAW.read_bytes()
    baseline = json.loads(baseline_bytes)
    if baseline.get("dataset_sha256") != dataset_sha:
        raise RuntimeError("M2.2 baseline uses a different dataset file SHA")
    return dataset, cases, dataset_sha, sha256(baseline_bytes)


def build_plan(settings=None) -> dict:
    dataset, cases, dataset_sha, baseline_sha = load_inputs()
    from app.m2.retrieval import index_fingerprint
    index = json.loads((ROOT / "data/index.json").read_text(encoding="utf-8"))
    fingerprint = index_fingerprint(index)
    selected = []
    for case in cases:
        selected.append({"case_id": case["case_id"], "dataset_split": case["dataset_split"],
            "group_id": case["group_id"], "scoreability": case.get("scoreability"),
            "evaluation_at": case["evaluation_at"], "knowledge_version": case["knowledge_version"],
            "question_sha256": sha256(case["user_query"].encode("utf-8")),
            "selection_reason": {
                "pr2-refund_shipped_unreceived-01": "dev state exception and reliable refutation",
                "pr2-refund_delivered_return-03": "dev negative prerequisite: pending review is not approval",
                "pr2-multi_clause_return_refund-03": "dev multi-policy regression; previously seen, excluded from independent-generalization claims",
                "pr2-delivery_policy_limits-01": "validation multi-condition boundary between policy evidence and live delivery facts",
                "pr2-return_quality_expired-03": "validation time-window boundary and manual escalation",
                "pr2-return_quality_conditions-02": "validation product-category exception and no guaranteed approval",
                "pr2-warranty_specific_missing-01": "validation behavior-only missing item-specific warranty evidence; excluded from ordinary retrieval metrics",
            }[case["case_id"]]})
    settings = settings or load_settings(require_api_key=False)
    retrieval_config = config_from_environment()
    effective_nodes = {name: {"thinking": node.thinking, "max_tokens": node.max_tokens,
        "timeout_seconds": node.timeout_seconds, "max_cost_usd": node.max_cost_usd, "locked": node.locked}
        for name, node in settings.nodes.items() if name in {"policy_answer", "condition_extraction", "evidence_sufficiency"}}
    return {"record_type": "m2_4_paired_ablation_plan", "plan_version": "r3",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "commit": __import__("subprocess").run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "experiment_script_sha256": sha256(Path(__file__).read_bytes()),
        "source_tree_dirty_at_planning": bool(__import__("subprocess").run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()),
        "selection": selected, "dataset": {"path": str(DATASET_PATH.relative_to(ROOT)),
            "sha256": dataset_sha, "dataset_version": dataset.get("dataset_version"),
            "only_approved_dev_validation": True, "test_candidate_questions_or_gold_read": False},
        "reused_retrieval_baseline": {"path": str(M2_2_RAW.relative_to(ROOT)), "sha256": baseline_sha},
        "knowledge": {"version": index.get("knowledge_version"), "evaluation_time_policy": "per approved case; fixed in dataset",
                      "active_index_fingerprint": fingerprint, "index_source_sha256": index.get("source_sha256")},
        "arms": [
            {"id": "single_hop_bm25", "method": "BM25 single retrieval + same DeepSeek answer provider", "max_generation_requests_per_case": 1},
            {"id": "single_hop_rrf_reranker", "method": "RRF + BGE-reranker-base single retrieval + same DeepSeek answer provider", "max_generation_requests_per_case": 1},
            {"id": "self_rag_max_3_hops", "method": "Self-RAG RRF+Reranker retrieval, at most 3 hops, evidence grade and answer", "max_generation_requests_per_case": 5},
        ],
        "fixed_variables": {"model": settings.model, "model_source": settings.model_source,
            "api_key_configured": bool(settings.api_key), "api_key_source": settings.api_key_source,
            "base_url_host": urlsplit(settings.base_url).hostname, "answer_node": effective_nodes.get("policy_answer"),
            "condition_extraction_node": effective_nodes.get("condition_extraction"),
            "evidence_sufficiency_node": effective_nodes.get("evidence_sufficiency"),
            "answer_prompt_version": "m1-policy-answer-v1", "self_rag_prompt_version": MultiHopRetrievalService.PROMPT_VERSION,
            "retrieval_config": {"version": retrieval_config.version, "sparse_top_k": retrieval_config.sparse_top_k,
                "dense_top_k": retrieval_config.dense_top_k, "output_top_k": retrieval_config.output_top_k,
                "rrf_constant": retrieval_config.rrf_constant,
                "rerank_candidate_limit": retrieval_config.rerank_candidate_limit,
                "small_model_revision": SMALL_REV, "reranker_revision": RERANKER_REV},
            "judge": {"model": settings.model, "thinking": "disabled", "max_tokens": 1600,
                "timeout_seconds": 30, "prompt_version": JUDGE_PROMPT_VERSION}},
        "budget": {"max_new_deepseek_requests": REQUEST_CAP, "max_estimated_cost_usd": COST_CAP_USD,
            "sdk_retries": 0, "expected_requests": 65, "expected_total_tokens": 170000,
            "expected_cost_usd_peak_rate_estimate": 0.30,
            "worst_case_requests_for_this_plan": 83,
            "request_count_includes": ["answer generation", "Self-RAG nodes", "LLM-as-Judge", "failed requests"],
            "failure_retries": "No automatic retry; any explicit rerun consumes this same stage budget"},
        "replication": {"extra_self_rag_generations": 2,
            "case_id": "pr2-multi_clause_return_refund-03",
            "interpretation": "same dev question/config stability check; not independent sample evidence"},
        "scoring": {"scoreable_cases": sum(case.get("scoreability") == "SCORABLE_CANDIDATE" for case in cases),
            "behavior_only_cases": sum(case.get("scoreability") != "SCORABLE_CANDIDATE" for case in cases),
            "human_review_status": "LLM judge is auxiliary; all generated labels remain PENDING_HUMAN_REVIEW",
            "validation_policy": "run once after fixed configuration; no validation tuning"}}


def preflight(settings) -> dict:
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    proxies = {name: "SET" if __import__("os").environ.get(name) else "UNSET" for name in proxy_names}
    started = time.perf_counter()
    try:
        # No authorization header or API key: 401/404 is accepted as HTTPS reachability.
        response = httpx.get(settings.base_url.rstrip("/"), timeout=8.0, follow_redirects=False)
        reachable = response.status_code in {200, 301, 302, 307, 308, 401, 403, 404}
        return {"status": "REACHABLE_UNAUTHENTICATED" if reachable else "UNEXPECTED_HTTP_STATUS",
            "http_status": response.status_code, "host": urlsplit(settings.base_url).hostname,
            "latency_ms": round((time.perf_counter() - started) * 1000), "proxy_env": proxies,
            "api_key_sent": False, "paid_model_request": False}
    except Exception as exc:
        return {"status": "NETWORK_BLOCKED", "exception_type": type(exc).__name__,
            "host": urlsplit(settings.base_url).hostname,
            "latency_ms": round((time.perf_counter() - started) * 1000), "proxy_env": proxies,
            "api_key_sent": False, "paid_model_request": False}


def _evidence_for(retrieval_result: dict) -> list[dict]:
    return [{k: row.get(k) for k in ("chunk_id", "title", "content", "version", "effective_from", "effective_to",
                                    "knowledge_version", "index_fingerprint", "rank", "score", "retrieval_method")}
            for row in retrieval_result.get("evidence", [])]


def run_answer_arms(cases: list[dict], retrieval: RetrievalService, provider: BudgetedProvider,
                    rag_config: SelfRAGConfig, run_record: dict, arms: list[str] | None = None,
                    on_result=None):
    raw_results = []
    for case in cases:
        view = retrieval.pin_view(case["evaluation_at"])
        for arm in (arms or ARMS):
            item = {"case_id": case["case_id"], "dataset_split": case["dataset_split"],
                    "scoreability": case.get("scoreability"), "arm": arm,
                    "question": case["user_query"], "evaluation_at": case["evaluation_at"],
                    "knowledge_version": view.index["knowledge_version"], "index_fingerprint": view.fingerprint,
                    "start_request_sequence": provider.budget.requests + 1, "status": "NOT_RUN"}
            started = time.perf_counter()
            try:
                if arm == "self_rag_max_3_hops":
                    result = MultiHopRetrievalService(retrieval, provider, config=rag_config).run(
                        case["user_query"], mode="rrf_reranker", evaluation_at=case["evaluation_at"])
                    completion = result.get("completion") or {}
                    evidence = result.get("evidence") or []
                    item.update({"status": "COMPLETED", "answer": completion.get("answer") or result.get("answer"),
                        "citations": completion.get("citations", []), "answer_kind": result.get("answer_kind"),
                        "stop_reason": result.get("stop_reason"), "sufficient": result.get("sufficient"),
                        "request_mode": result.get("request_mode"), "conditions": result.get("conditions"),
                        "requested_aspects": result.get("requested_aspects"), "condition_coverage": result.get("condition_coverage"),
                        "condition_normalizations": result.get("condition_normalizations"),
                        "grade_identifier_normalizations": result.get("grade_identifier_normalizations"),
                        "condition_extraction_error": result.get("condition_extraction_error"),
                        "grade_result": result.get("grade_result"), "hops": result.get("hops"),
                        "condition_extraction_raw_output": result.get("condition_extraction_raw_output"),
                        "condition_semantic_audit": result.get("condition_semantic_audit"),
                        "completion": completion, "final_rerank": result.get("final_rerank"),
                        "budget": result.get("budget"), "generation_error": result.get("generation_error")})
                else:
                    mode = "bm25" if arm == "single_hop_bm25" else "rrf_reranker"
                    found = retrieval.retrieve(case["user_query"], view=view, mode=mode)
                    evidence = found.get("evidence", [])
                    item["evidence"] = evidence
                    completion = provider.answer(case["user_query"], found.get("evidence", []))
                    item.update({"status": "COMPLETED", "answer": completion.get("answer"),
                        "citations": completion.get("citations", []), "answer_kind": "POLICY_ANSWER",
                        "retrieval": {"mode": mode, "latency_ms": found.get("latency_ms"),
                            "degraded": found.get("degraded"), "failures": found.get("failures"),
                            "diagnostics": found.get("diagnostics"), "models": found.get("models"),
                            "config_version": found.get("config_version"), "config_hash": found.get("config_hash")},
                        "completion": completion})
                item["evidence"] = evidence
                item["end_request_sequence"] = provider.budget.requests
                item["end_to_end_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
            except StageBudgetExceeded as exc:
                item.update({"status": "BLOCKED_STAGE_BUDGET", "error_code": str(exc),
                             "end_request_sequence": provider.budget.requests})
                raw_results.append(item)
                run_record["results"] = raw_results
                if on_result:
                    on_result(raw_results)
                raise
            except Exception as exc:
                diagnostic = getattr(exc, "diagnostic", None)
                item.update({"status": "FAILED", "error_code": getattr(exc, "code", None) or type(exc).__name__,
                    "exception_type": type(exc).__name__, "diagnostic": diagnostic,
                    "end_request_sequence": provider.budget.requests,
                    "end_to_end_elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
                # Failures are recorded; no retry is attempted.
            raw_results.append(item)
            run_record["results"] = raw_results
            if on_result:
                on_result(raw_results)
    return raw_results


def _judge_schema_prompt() -> str:
    return """你是答案忠实度的辅助评审。你不知道候选答案来自哪种检索方法。按相同规则评估，不替代人工金标。
只用输入中的问题、已批准必要条件/行为标准、当前候选证据和答复。逐项列出答复中的事实性原子断言：status 只能 SUPPORTED/UNSUPPORTED/CONTRADICTED/UNCERTAIN；scope 只能 REQUESTED/RELEVANT_CONTEXT/OUT_OF_SCOPE/SYSTEM_CAPABILITY；relevance 只能 NECESSARY/USEFUL/UNNECESSARY。每个有政策支持的断言须给出精确证据 ID；系统能力断言不能由政策证据支持。区分真实引用来源与语义蕴含。错误的肯定业务结论、错误政策前置条件、把澄清题猜成确定承诺是严重问题。对不可评分行为样本，仅评价是否识别缺失/来源类型、是否要求必要澄清、是否拒绝编造；不记入普通检索分数。
required_condition_coverage 必须为输入中每个 required condition_id 各输出一行，完整且仅出现一次，不得遗漏、改写、合并或补造 ID。system_contract_sources 中的 source_id 是唯一允许引用的契约 ID；逐字复制其中的 source_id，不得用记忆中的路径替换。SYSTEM_CAPABILITY 断言必须引用 supplied 的 system_contract source ID，不能用政策 chunk 支持。不要只根据答案类型判优。输出一个 JSON 对象，结构：
{"claims":[{"claim_id":"c1","text":"","status":"SUPPORTED|UNSUPPORTED|CONTRADICTED|UNCERTAIN","scope":"REQUESTED|RELEVANT_CONTEXT|OUT_OF_SCOPE|SYSTEM_CAPABILITY","relevance":"NECESSARY|USEFUL|UNNECESSARY","evidence_ids":[],"contract_source_ids":[],"rationale":""}],"required_condition_coverage":[{"condition_id":"","covered":true,"supported":true,"rationale":""}],"behavior_findings":[{"criterion":"","pass":true,"rationale":""}],"unsupported_definitive":false,"correctly_clarifies_or_abstains":true,"unnecessary_claim_count":0,"overall":"PASS|MIXED|FAIL|UNCERTAIN"}. 只输出 JSON。"""


SYSTEM_CONTRACT_SOURCES = [
    {"source_id": "app/m1/chat_execution.py#AGENT", "type": "system_contract",
     "excerpt": "When the route strategy is AGENT, the M1/M2 chat executor raises ROUTE_EXECUTOR_UNAVAILABLE; it does not query a live order tool or perform a business write."},
    {"source_id": "docs/m2.1-routing.md#AGENT", "type": "system_contract",
     "excerpt": "AGENT routing is classified and persisted, but domain agents/tools are not implemented in M2.1; unsupported execution fails safely."},
    {"source_id": "docs/m1.1-api.md#/api/orders/{id}", "type": "system_contract",
     "excerpt": "GET /api/orders/{id} is a separate authenticated read-only endpoint for the current user's order; it is not invoked by the chat executor."},
]


class JudgeOutputValidationError(ValueError):
    """A Judge response cannot be scored because it violates the supplied contract."""

    def __init__(self, code: str, message: str, attempts: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.attempts = attempts or []


def judge_input(case: dict, result: dict, blind_id: str) -> dict:
    scorable = case.get("scoreability") == "SCORABLE_CANDIDATE"
    return {"blind_candidate_id": blind_id, "evaluation_mode": "answer_fidelity" if scorable else "behavior_only",
        "question": case["user_query"],
        "required_conditions": ([{"condition_id": row.get("condition_id"), "text": row.get("text"), "required": row.get("required")}
            for row in case.get("required_conditions", []) if row.get("required")] if scorable else []),
        "behavior_criteria": ([] if scorable else [row.get("text") for row in case.get("required_conditions", []) if row.get("required")]),
        "expected_conclusion_or_behavior_standard": case.get("expected_conclusion"),
        "system_contract_sources": SYSTEM_CONTRACT_SOURCES,
        "answer_kind": result.get("answer_kind"), "answer": result.get("answer"),
        "citations": result.get("citations", []),
        "evidence": [{"chunk_id": row.get("chunk_id"), "title": row.get("title"), "version": row.get("version"),
                      "effective_from": row.get("effective_from"), "effective_to": row.get("effective_to"),
                      "content": row.get("content")} for row in result.get("evidence", [])]}


def validate_judge(payload: dict, case: dict, evidence_ids: set[str], contract_ids: set[str] | None = None) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        raise JudgeOutputValidationError("JUDGE_CLAIMS_MISSING", "judge result is missing claims array")
    allowed_status = {"SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "UNCERTAIN"}
    allowed_scope = {"REQUESTED", "RELEVANT_CONTEXT", "OUT_OF_SCOPE", "SYSTEM_CAPABILITY"}
    allowed_relevance = {"NECESSARY", "USEFUL", "UNNECESSARY"}
    contract_ids = contract_ids or set()
    claim_ids = set()
    for claim in payload["claims"]:
        if not isinstance(claim, dict) or not {"claim_id", "text", "status", "scope", "relevance",
                "evidence_ids", "contract_source_ids", "rationale"}.issubset(claim):
            raise JudgeOutputValidationError("JUDGE_CLAIM_SCHEMA_INVALID", "judge claim fields are incomplete")
        if not isinstance(claim["claim_id"], str) or not claim["claim_id"] or claim["claim_id"] in claim_ids:
            raise JudgeOutputValidationError("JUDGE_CLAIM_ID_INVALID", "judge claim ids must be unique strings")
        claim_ids.add(claim["claim_id"])
        if claim.get("status") not in allowed_status or claim.get("scope") not in allowed_scope or claim.get("relevance") not in allowed_relevance:
            raise JudgeOutputValidationError("JUDGE_CLAIM_LABEL_INVALID", "judge claim labels invalid")
        if not isinstance(claim.get("evidence_ids"), list) or any(cid not in evidence_ids for cid in claim["evidence_ids"]):
            raise JudgeOutputValidationError("JUDGE_EVIDENCE_ID_UNKNOWN", "judge claim references evidence outside candidate pool")
        claim_contract_ids = claim.get("contract_source_ids", [])
        if not isinstance(claim_contract_ids, list) or any(cid not in contract_ids for cid in claim_contract_ids):
            raise JudgeOutputValidationError("JUDGE_CONTRACT_SOURCE_UNKNOWN", "judge claim references unknown system contract source")
        if claim.get("scope") == "SYSTEM_CAPABILITY" and claim.get("status") == "SUPPORTED" and not claim_contract_ids:
            raise JudgeOutputValidationError("JUDGE_CONTRACT_SOURCE_MISSING", "supported system capability claim lacks a contract source")
    rows = payload.get("required_condition_coverage")
    if not isinstance(rows, list):
        raise JudgeOutputValidationError("JUDGE_COVERAGE_MISSING", "judge coverage is missing")
    if any(not isinstance(row, dict) or not {"condition_id", "covered", "supported", "rationale"}.issubset(row)
           or not isinstance(row.get("covered"), bool) or not isinstance(row.get("supported"), bool)
           for row in rows):
        raise JudgeOutputValidationError("JUDGE_COVERAGE_SCHEMA_INVALID", "judge coverage row is incomplete")
    expected_ids = {row["condition_id"] for row in case.get("required_conditions", []) if row.get("required")}
    actual_ids = {row.get("condition_id") for row in rows}
    if len(actual_ids) != len(rows):
        raise JudgeOutputValidationError("JUDGE_COVERAGE_DUPLICATE_ID", "judge coverage contains duplicate condition IDs")
    if case.get("scoreability") == "SCORABLE_CANDIDATE" and expected_ids != actual_ids:
        raise JudgeOutputValidationError("JUDGE_REQUIRED_CONDITION_COVERAGE_MISMATCH",
            "judge coverage does not match approved required condition IDs")
    if case.get("scoreability") != "SCORABLE_CANDIDATE":
        findings = payload.get("behavior_findings")
        if not isinstance(findings, list) or not findings or any(not isinstance(item.get("pass"), bool) for item in findings):
            raise JudgeOutputValidationError("JUDGE_BEHAVIOR_FINDINGS_INVALID", "behavior-only judge result lacks explicit pass/fail findings")
    if payload.get("overall") not in {"PASS", "MIXED", "FAIL", "UNCERTAIN"}:
        raise JudgeOutputValidationError("JUDGE_OVERALL_INVALID", "judge overall label invalid")


def judge_one(settings, budget: StageBudget, judge_node: NodeConfig, client, case: dict,
              result: dict, blind_id: str) -> dict:
    input_data = judge_input(case, result, blind_id)
    prompt = _judge_schema_prompt()
    serialized = json.dumps(input_data, ensure_ascii=False)
    required_ids = sorted(row["condition_id"] for row in input_data["required_conditions"])
    contract_ids = sorted(row["source_id"] for row in SYSTEM_CONTRACT_SOURCES)
    correction = None
    attempts = []
    for attempt_number in range(1, 3):
        def request():
            started = time.perf_counter()
            system_content = JUDGE_PROMPT_VERSION + "\n" + prompt
            if correction:
                system_content += "\n修正要求：" + correction
            response = client.chat.completions.create(model=settings.model,
                messages=[{"role": "system", "content": system_content},
                          {"role": "user", "content": serialized}],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}}, max_tokens=judge_node.max_tokens,
                timeout=judge_node.timeout_seconds)
            choice = response.choices[0]
            raw = choice.message.content or ""
            usage = response.usage.model_dump() if response.usage else None
            try:
                parsed = json.loads(raw)
                parse_error = None
            except json.JSONDecodeError as exc:
                parsed = None
                parse_error = type(exc).__name__
            return {"result": parsed, "parse_error": parse_error, "raw_content": raw,
                "model_id": getattr(response, "model", None) or settings.model,
                "provider_request_id": getattr(response, "_request_id", None),
                "finish_reason": choice.finish_reason, "usage": usage,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "estimated_cost_usd": cost_from_usage(usage), "prompt_version": JUDGE_PROMPT_VERSION,
                "thinking": "disabled", "max_tokens": judge_node.max_tokens,
                "timeout_seconds": judge_node.timeout_seconds}
        budget_input = serialized + ("\n" + correction if correction else "")
        response = budget.call(judge_node, "llm_as_judge", budget_input, request)
        validation_error = None
        if response.get("finish_reason") != "stop":
            validation_error = JudgeOutputValidationError("JUDGE_RESPONSE_INCOMPLETE", "judge response incomplete")
        elif response.get("parse_error"):
            validation_error = JudgeOutputValidationError("JUDGE_JSON_INVALID", "judge returned invalid JSON")
        else:
            try:
                validate_judge(response["result"], case, {row["chunk_id"] for row in result.get("evidence", [])},
                               set(contract_ids))
            except JudgeOutputValidationError as exc:
                validation_error = exc
        attempts.append({"attempt": attempt_number, "request_sequence": budget.requests,
            "status": "FAILED_VALIDATION" if validation_error else "VALIDATED",
            "error_code": validation_error.code if validation_error else None,
            "raw_content": response.get("raw_content"), "usage": response.get("usage"),
            "provider_request_id": response.get("provider_request_id"),
            "finish_reason": response.get("finish_reason"), "latency_ms": response.get("latency_ms"),
            "estimated_cost_usd": response.get("estimated_cost_usd")})
        if validation_error is None:
            return {**response, "validation_attempts": attempts}
        if attempt_number == 2:
            raise JudgeOutputValidationError(validation_error.code, str(validation_error), attempts)
        correction = (f"上次响应校验失败，错误码 {validation_error.code}。必须完整覆盖以下必需条件 ID（每个恰好一次）："
            f"{json.dumps(required_ids, ensure_ascii=False)}。契约 source_id 仅允许以下精确值："
            f"{json.dumps(contract_ids, ensure_ascii=False)}；不得发明、改写或引用列表外的路径。"
            "请重新判断；不确定时使用 UNCERTAIN，不得补造结论。保持 JSON schema 字段完整。")
    raise JudgeOutputValidationError("JUDGE_RETRY_EXHAUSTED", "judge retry exhausted")


def aggregate(results: list[dict], judgments: list[dict], cases_by_id: dict[str, dict]) -> dict:
    per_arm = {}
    valid_judgments = [row for row in judgments if row.get("status") == "COMPLETED"
                       and isinstance(row.get("judgment"), dict)]
    for arm in ARMS:
        attempted = [row for row in results if row.get("arm") == arm and row.get("replicate") is None
                     and row.get("scoreability") == "SCORABLE_CANDIDATE"]
        selected = [row for row in attempted if row.get("status") == "COMPLETED"
                    and row.get("answer_kind") == "POLICY_ANSWER"]
        judge_by_blind = {(row["case_id"], row["arm"], row.get("replicate")): row["judgment"]
                          for row in valid_judgments}
        required_total = covered = supported = 0
        claim_total = supported_claims = unsupported = contradicted = uncertain = unnecessary = 0
        answer_count = len(selected)
        cases_with_judges = 0
        for item in selected:
            judge = judge_by_blind.get((item["case_id"], arm, None))
            if not judge:
                continue
            cases_with_judges += 1
            for row in judge.get("required_condition_coverage", []):
                required_total += 1
                covered += bool(row.get("covered"))
                supported += bool(row.get("supported"))
            for claim in judge.get("claims", []):
                claim_total += 1
                supported_claims += claim.get("status") == "SUPPORTED"
                unsupported += claim.get("status") == "UNSUPPORTED"
                contradicted += claim.get("status") == "CONTRADICTED"
                uncertain += claim.get("status") == "UNCERTAIN"
                unnecessary += claim.get("relevance") == "UNNECESSARY"
        answer_outcomes = {}
        for row in attempted:
            outcome = row.get("answer_kind") or row.get("status")
            answer_outcomes[outcome] = answer_outcomes.get(outcome, 0) + 1
        unavailable = answer_outcomes.get("GENERATION_UNAVAILABLE", 0)
        per_arm[arm] = {"scoreable_answer_count": answer_count, "attempted_scoreable_generation_count": len(attempted),
            "generation_unavailable_count": unavailable,
            "answer_outcomes": answer_outcomes,
            "generation_success_rate": (answer_count / len(attempted) if attempted else None),
            "judge_completed_count": cases_with_judges,
            "required_condition_coverage_judge": {"covered": covered, "supported": supported, "denominator": required_total,
                "covered_rate": covered / required_total if required_total else None,
                "supported_rate": supported / required_total if required_total else None},
            "claim_status_judge": {"supported": supported_claims, "unsupported": unsupported,
                "contradicted": contradicted, "uncertain": uncertain, "denominator": claim_total,
                "unsupported_rate": unsupported / claim_total if claim_total else None,
                "contradicted_rate": contradicted / claim_total if claim_total else None,
                "unnecessary": unnecessary},
            "elapsed_ms": {"p50": _percentile([row["end_to_end_elapsed_ms"] for row in selected], .5),
                           "p95": _percentile([row["end_to_end_elapsed_ms"] for row in selected], .95)}}
        per_arm[arm]["required_condition_coverage_judge"]["wilson_95"] = _wilson(supported, required_total)
        per_arm[arm]["claim_status_judge"]["supported_wilson_95"] = _wilson(supported_claims, claim_total)
        per_arm[arm]["claim_status_judge"]["unsupported_wilson_95"] = _wilson(unsupported, claim_total)
    paired = {}
    for arm in ARMS[1:]:
        eligible_answer_keys = {(row["case_id"], row["arm"], row.get("replicate"))
            for row in results if row.get("status") == "COMPLETED" and row.get("replicate") is None
            and row.get("scoreability") == "SCORABLE_CANDIDATE" and row.get("answer_kind") == "POLICY_ANSWER"}
        by_arm = {(row["case_id"], row["arm"], row.get("replicate")): row["judgment"]
                  for row in valid_judgments if row.get("replicate") is None
                  and (row["case_id"], row["arm"], row.get("replicate")) in eligible_answer_keys}
        differences = []
        for case_id, case in cases_by_id.items():
            if case.get("scoreability") != "SCORABLE_CANDIDATE":
                continue
            left, right = by_arm.get((case_id, "single_hop_bm25", None)), by_arm.get((case_id, arm, None))
            if left is None or right is None:
                continue
            left_rows = left.get("required_condition_coverage", [])
            right_rows = right.get("required_condition_coverage", [])
            if not left_rows or len(left_rows) != len(right_rows):
                continue
            differences.append((case_id,
                sum(bool(row.get("supported")) for row in right_rows) / len(right_rows) -
                sum(bool(row.get("supported")) for row in left_rows) / len(left_rows)))
        paired[arm] = {"metric": "per_case_judge_supported_required_condition_rate_difference_vs_BM25",
            "n_paired_cases": len(differences), "case_differences": [{"case_id": cid, "difference": round(diff, 6)} for cid, diff in differences],
            "bootstrap_95": _paired_bootstrap([diff for _, diff in differences], seed=24042026)}
    return {"scoreable_metrics_are_judge_auxiliary_not_human_gold": True,
            "ordinary_retrieval_denominator": sum(case.get("scoreability") == "SCORABLE_CANDIDATE" for case in cases_by_id.values()),
            "behavior_only_case_ids": [case_id for case_id, case in cases_by_id.items() if case.get("scoreability") != "SCORABLE_CANDIDATE"],
            "arms": per_arm, "paired_case_cluster_bootstrap_vs_bm25": paired,
            "bootstrap_note": "10,000 paired resamples over selected case clusters; descriptive only because n is small and judge labels are not human gold"}


def _percentile(values: list[float], p: float):
    if not values:
        return None
    values = sorted(float(value) for value in values)
    pos = (len(values) - 1) * p
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (pos - low), 3)


def _wilson(successes: int, n: int, z: float = 1.959963984540054):
    if not n:
        return None
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / den
    return [round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)]


def _paired_bootstrap(values: list[float], *, seed: int, samples: int = 10000):
    if not values:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(len(values))] for _ in values) / len(values))
    means.sort()
    return [round(means[int(.025 * (samples - 1))], 6), round(means[int(.975 * (samples - 1))], 6)]


def default_output() -> Path:
    return ROOT / "eval/raw" / f"m2.4-paired-ablation-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="perform bounded real DeepSeek generation and Judge calls")
    parser.add_argument("--output", type=Path, help="new, non-existing raw record path")
    parser.add_argument("--plan-output", type=Path, help="save a pre-call plan to a new JSON file")
    parser.add_argument("--preflight-only", action="store_true", help="check HTTPS reachability without API key or paid calls")
    args = parser.parse_args()
    settings = load_settings(require_api_key=args.execute)
    plan = build_plan(settings)
    if not args.execute:
        if args.plan_output:
            plan_path = args.plan_output.resolve()
            if plan_path.exists():
                raise SystemExit(f"refusing to overwrite plan: {plan_path}")
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"plan": plan, "network_preflight": preflight(settings) if args.preflight_only else "NOT_RUN",
                          "real_model_requests": 0}, ensure_ascii=False, indent=2))
        return 0
    output = (args.output or default_output()).resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing raw output: {output}")
    net = preflight(settings)
    if net.get("status") != "REACHABLE_UNAUTHENTICATED":
        blocked = {"record_type": "m2_4_paired_ablation_raw", "run_id": str(uuid.uuid4()),
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "git_commit": plan["commit"], "plan": plan, "network_preflight": net,
            "status": "BLOCKED_NETWORK_PREFLIGHT", "real_model_requests": 0,
            "test_candidate_read_for_scoring_or_tuning": False}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(blocked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"status": blocked["status"], "output": str(output), "requests": 0,
                          "preflight": net}, ensure_ascii=False, indent=2))
        return 2
    qdrant = QdrantClient(":memory:")
    base_config = config_from_environment()
    retrieval_config = RetrievalConfig(**{**base_config.__dict__, "mode": "bm25", "qdrant_mode": "local"})
    retrieval = RetrievalService(config=retrieval_config, qdrant_client=qdrant,
        collection_namespace="m24_ablation_" + uuid.uuid4().hex[:8])
    budget = StageBudget()
    raw = {"record_type": "m2_4_paired_ablation_raw", "run_id": str(uuid.uuid4()),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "git_commit": plan["commit"], "plan": plan, "network_preflight": net,
        "status": "RUNNING", "results": [], "judgments": [], "historical_answer_review": None,
        "sdk_retries": 0, "raw_responses_are_verbatim_model_content": True,
        "human_review_status": "PENDING_HUMAN_REVIEW", "test_candidate_read_for_scoring_or_tuning": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Create the append-only record before making any paid request.
    output.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    def checkpoint():
        raw["request_accounting"] = {"requests": budget.requests, "cap": REQUEST_CAP,
            "estimated_cost_usd": round(budget.estimated_cost_usd + budget.unknown_cost_reserve_usd, 9),
            "measured_cost_usd": round(budget.estimated_cost_usd, 9),
            "unknown_cost_reserve_usd": round(budget.unknown_cost_reserve_usd, 9),
            "cost_cap_usd": COST_CAP_USD, "calls": budget.calls,
            "price_source": PRICE_SOURCE, "not_invoice": True}
        temp = output.with_suffix(output.suffix + ".tmp")
        temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        temp.replace(output)

    budget.on_call = checkpoint

    raw["knowledge"] = {"fingerprint": retrieval.pin_view().fingerprint,
                        "knowledge_version": retrieval.pin_view().index["knowledge_version"],
                        "policy_source_sha256": retrieval.pin_view().index.get("source_sha256")}
    raw["runtime_config"] = {"api_key_configured": bool(settings.api_key), "api_key_source": settings.api_key_source,
        "model": settings.model, "base_url_host": urlsplit(settings.base_url).hostname,
        "node_configs": {name: plan["fixed_variables"][name] for name in
                         ("answer_node", "condition_extraction_node", "evidence_sufficiency_node")},
        "self_rag_config": SelfRAGConfig().__dict__, "retrieval_config": retrieval_config.__dict__}
    provider = BudgetedProvider(DeepSeekProvider(settings=settings), budget)
    cases = load_inputs()[1]
    cases_by_id = {case["case_id"]: case for case in cases}
    try:
        results = run_answer_arms(cases, retrieval, provider, SelfRAGConfig(), raw,
                                  on_result=lambda rows: (raw.update({"results": list(rows)}), checkpoint()))
        raw["results"] = results
        # Two extra generations on the previously seen dev MULTI case check
        # Self-RAG output stability; these repeats are not independent samples.
        repeat_case = cases_by_id["pr2-multi_clause_return_refund-03"]
        for repeat_no in (1, 2):
            repeated = run_answer_arms([repeat_case], retrieval, provider, SelfRAGConfig(), {"results": []},
                                       arms=["self_rag_max_3_hops"],
                                       on_result=lambda rows: (raw.setdefault("results", []).extend(rows), checkpoint()))
            for row in repeated:
                row["replicate"] = repeat_no
                row["repeat_reason"] = "same approved dev MULTI case/config; stability check, not independent n"
                checkpoint()
        # Blind candidate aliases are randomized and kept only in the raw audit record.
        blind_pairs = [(result, cases_by_id[result["case_id"]]) for result in raw["results"] if result.get("status") == "COMPLETED"]
        rng = random.Random(24042026)
        rng.shuffle(blind_pairs)
        judge_node = NodeConfig("m2_4_judge", "m2_4_auxiliary_evaluation", "disabled", 1600, 30.0, .05, True)
        judge_client = create_deepseek_client(settings, judge_node)
        judged_by_pair = []
        for i, (result, case) in enumerate(blind_pairs, 1):
            blind_id = "candidate-" + hashlib.sha256(f"{raw['run_id']}:{i}:{rng.random()}".encode()).hexdigest()[:12]
            try:
                response = judge_one(settings, budget, judge_node, judge_client, case, result, blind_id)
                judgement = response["result"]
                record = {"case_id": case["case_id"], "dataset_split": case["dataset_split"],
                    "arm": result["arm"], "blind_candidate_id": blind_id,
                    "replicate": result.get("replicate"),
                    "request_sequence": budget.requests, "status": "COMPLETED", "judgment": judgement,
                    "raw_content": response["raw_content"], "usage": response["usage"],
                    "model_id": response["model_id"], "finish_reason": response["finish_reason"],
                    "latency_ms": response["latency_ms"], "estimated_cost_usd": response["estimated_cost_usd"],
                    "prompt_version": JUDGE_PROMPT_VERSION,
                    "judge_not_told_method": True, "review_status": "PENDING_HUMAN_REVIEW"}
                raw["judgments"].append(record)
                judged_by_pair.append(record)
            except Exception as exc:
                raw["judgments"].append({"case_id": case["case_id"], "dataset_split": case["dataset_split"],
                    "arm": result["arm"], "replicate": result.get("replicate"), "blind_candidate_id": blind_id,
                    "request_sequence": budget.requests, "status": "FAILED",
                    "exception_type": type(exc).__name__, "error_code": getattr(exc, "code", None),
                    "judge_not_told_method": True, "review_status": "PENDING_HUMAN_REVIEW"})
                if isinstance(exc, StageBudgetExceeded):
                    raise
            checkpoint()
        # Add the prior M2.3 answer as one blinded fourth candidate, preserving it verbatim.
        historic = json.loads(HISTORICAL_RUN.read_text(encoding="utf-8"))
        historic_answer = historic.get("answer") or {}
        historic_rag = historic.get("self_rag") or {}
        historical_case = cases_by_id["pr2-multi_clause_return_refund-03"]
        if isinstance(historic_answer, dict):
            historical_text = historic_answer.get("answer")
            historical_kind = historic_answer.get("answer_kind")
            historical_citations = historic_answer.get("citations", [])
        else:
            historical_text = historic_answer
            historical_kind = historic.get("answer_kind")
            historical_citations = historic.get("citations", [])
        historical_item = {"case_id": historical_case["case_id"], "dataset_split": "dev",
            "scoreability": "SCORABLE_CANDIDATE", "answer_kind": historical_kind,
            "answer": historical_text, "citations": historical_citations,
            "evidence": historic_rag.get("evidence", [])}
        historical_blind_id = "candidate-" + hashlib.sha256(f"{raw['run_id']}:historical".encode()).hexdigest()[:12]
        try:
            response = judge_one(settings, budget, judge_node, judge_client, historical_case,
                                 historical_item, historical_blind_id)
            raw["historical_answer_review"] = {"source_run_record": str(HISTORICAL_RUN.relative_to(ROOT)),
                "case_id": historical_case["case_id"], "blind_candidate_id": historical_blind_id,
                "request_sequence": budget.requests, "status": "COMPLETED", "judgment": response["result"], "raw_content": response["raw_content"],
                "usage": response["usage"], "model_id": response["model_id"],
                "finish_reason": response["finish_reason"], "latency_ms": response["latency_ms"],
                "estimated_cost_usd": response["estimated_cost_usd"], "prompt_version": JUDGE_PROMPT_VERSION,
                "judge_not_told_method": True, "review_status": "PENDING_HUMAN_REVIEW"}
        except Exception as exc:
            raw["historical_answer_review"] = {"source_run_record": str(HISTORICAL_RUN.relative_to(ROOT)),
                "case_id": historical_case["case_id"], "status": "FAILED", "exception_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None), "review_status": "PENDING_HUMAN_REVIEW"}
            if isinstance(exc, StageBudgetExceeded):
                raise
        raw["summary"] = aggregate(results, raw["judgments"], cases_by_id)
        raw["replicate_judgments"] = [row for row in raw["judgments"] if row.get("replicate") is not None]
        historic_status = (raw.get("historical_answer_review") or {}).get("status")
        raw["status"] = "COMPLETE_WITH_ERRORS" if (
            any(row.get("status") != "COMPLETED" for row in raw["results"] + raw["judgments"]) or
            historic_status != "COMPLETED") else "COMPLETE"
        raw["experiment_limits"] = {"max_hops": 3, "per_run_self_rag_config_unchanged": True,
            "max_real_requests": REQUEST_CAP, "max_estimated_cost_usd": COST_CAP_USD}
    except Exception as exc:
        raw["status"] = "BLOCKED" if isinstance(exc, StageBudgetExceeded) else "FAILED"
        raw["terminal_error"] = {"exception_type": type(exc).__name__, "error_code": getattr(exc, "code", None),
                                  "safe_message": str(exc)[:300]}
    finally:
        raw["elapsed_wall_seconds"] = round(time.monotonic() - started_global, 3)
        checkpoint()
    print(json.dumps({"status": raw["status"], "output": str(output),
        "requests": budget.requests, "estimated_cost_usd": round(budget.estimated_cost_usd + budget.unknown_cost_reserve_usd, 9),
        "scoreable_cases": 6, "behavior_only_cases": 1,
        "completed_results": sum(row.get("status") == "COMPLETED" for row in raw["results"]),
        "completed_judgments": sum(row.get("status") == "COMPLETED" for row in raw["judgments"]),
        "summary": raw.get("summary")}, ensure_ascii=False, indent=2))
    return 0 if raw["status"] == "COMPLETE" else 1


started_global = time.monotonic()

if __name__ == "__main__":
    raise SystemExit(main())
