"""Bounded policy retrieval and answer execution for online SINGLE/MULTI routes.

The historical Self-RAG implementation remains available for replay and tests, but
online policy answers use one pinned knowledge view and a finite set of requested
policy aspects. No recursive Grade/replan loop is used here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import hashlib
import re
import time
import uuid
from typing import Callable

from app.m1.chat import ChatFailure
from app.m2.retrieval import RetrievalService, RetrievalUnavailable, config_from_environment

MAX_POLICY_ASPECTS = 4
MAX_MERGED_EVIDENCE = 12
ONLINE_RETRIEVAL_VERSION = "m2-r-bounded-policy-v1"
HYBRID_MODE_ALIASES = {"vector", "rrf", "rrf_reranker", "hybrid_rerank"}


def _plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _route_usage(route: dict | None) -> dict | None:
    value = (route or {}).get("usage")
    return value if isinstance(value, dict) else None


def _budget_node(provider, node_name: str):
    settings = getattr(provider, "settings", None)
    if settings is None:
        from app.m0.config import load_settings
        settings = load_settings(require_api_key=False)
    return settings.nodes[node_name]


def _reserve_model(budget, provider, node_name: str, input_text: str) -> float:
    node = _budget_node(provider, node_name)
    allowed, reason, estimate = budget.reserve_model(node, input_text)
    if not allowed:
        raise ChatFailure("MODEL_BUDGET_EXCEEDED", False,
                          "本轮模型调用预算不足，未继续生成政策结论。",
                          {"node": node_name, "budget_reason": reason, "preflight_estimate_usd": estimate,
                           "run_budget": budget.snapshot()})
    return estimate


def _record_model_call(budget, node_name: str, response: dict, fallback_cost: float) -> None:
    budget.record_model(node_name, response, fallback_cost)


def _record_model_failure(budget, node_name: str, fallback_cost: float, exc: ChatFailure) -> None:
    budget.record_failure(node_name, fallback_cost, exc.code, exc.diagnostic)
    exc.diagnostic = {**(exc.diagnostic or {}), "run_budget": budget.snapshot()}


def _sum_usage(calls: list[dict], route_usage: dict | None = None) -> dict:
    prompt = completion = 0
    have_usage = False
    cost = 0.0
    cost_known = True
    entries = []
    if route_usage:
        entries.append({"node": "route", "usage": route_usage})
    entries.extend(calls)
    for entry in entries:
        usage = entry.get("usage") if isinstance(entry, dict) else None
        if not isinstance(usage, dict):
            cost_known = False
            continue
        have_usage = True
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        estimate = usage.get("cost_estimate_usd_peak_rate", usage.get("estimated_cost_usd",
                           entry.get("estimated_cost_usd") if isinstance(entry, dict) else None))
        if estimate is None:
            cost_known = False
        else:
            cost += float(estimate)
    return {
        "model_requests": len(calls) + int(bool(route_usage)),
        "node_model_requests": len(calls),
        "prompt_tokens": prompt if have_usage else None,
        "completion_tokens": completion if have_usage else None,
        "estimated_cost_usd": round(cost, 9) if have_usage and cost_known else None,
        "usage_complete": bool(entries) and have_usage and cost_known,
        "calls": entries,
    }


def _supports_keyword(function, name: str) -> bool:
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def _plan_multi(provider, question: str, context_notes: list[str]) -> tuple[dict, list[dict], list[dict]]:
    plan = getattr(provider, "plan_policy_aspects", None)
    if callable(plan):
        extracted = (plan(question, context_notes=context_notes) if _supports_keyword(plan, "context_notes")
                     else plan(question))
        payload = _plain(extracted.get("plan_result")) if isinstance(extracted, dict) else None
        if not isinstance(payload, dict):
            raise ChatFailure("POLICY_ASPECT_PLAN_INVALID", False,
                              "政策检索目标无效，未生成政策结论。",
                              {"node": "policy_aspect_planning"})
        request_mode = payload.get("request_mode")
        aspects = payload.get("requested_aspects")
        facts_payload = payload.get("user_provided_facts") or []
        if request_mode not in {"POLICY_EXPLANATION", "ELIGIBILITY_DECISION", "ACTION_EXECUTION"}:
            raise ChatFailure("POLICY_ASPECT_PLAN_INVALID", False,
                              "政策检索目标缺少有效请求类型。",
                              {"node": "policy_aspect_planning"})
        if not isinstance(aspects, list) or not aspects or len(aspects) > MAX_POLICY_ASPECTS:
            raise ChatFailure("POLICY_ASPECT_PLAN_INVALID", False,
                              "政策检索方面数量不在安全范围内。",
                              {"node": "policy_aspect_planning", "aspect_count": len(aspects) if isinstance(aspects, list) else None})
        normalized = []
        seen = set()
        for item in aspects:
            row = _plain(item)
            if (not isinstance(row, dict) or not isinstance(row.get("aspect_id"), str) or
                    not row["aspect_id"] or row["aspect_id"] in seen or
                    not isinstance(row.get("query_anchor"), str) or row["query_anchor"] not in question or
                    not isinstance(row.get("text"), str) or not row["text"].strip()):
                raise ChatFailure("POLICY_ASPECT_PLAN_INVALID", False,
                                  "政策检索方面必须有唯一 ID、原问题锚点和描述。",
                                  {"node": "policy_aspect_planning"})
            seen.add(row["aspect_id"])
            normalized.append({"aspect_id": row["aspect_id"], "text": row["text"].strip(),
                               "query_anchor": row["query_anchor"]})
        facts = []
        for item in facts_payload:
            row = _plain(item)
            if (isinstance(row, dict) and isinstance(row.get("source_excerpt"), str) and
                    row["source_excerpt"] in question and isinstance(row.get("text"), str) and row["text"].strip()):
                facts.append({"text": row["text"].strip(), "source_excerpt": row["source_excerpt"],
                              "source_type": "USER_PROVIDED"})
            else:
                raise ChatFailure("POLICY_ASPECT_PLAN_INVALID", False,
                                  "用户背景事实缺少可核验的原问题来源。",
                                  {"node": "policy_aspect_planning"})
        details = {
            "node": "policy_aspect_planning", "prompt_version": extracted.get("prompt_version"),
            "model_id": extracted.get("model_id"), "finish_reason": extracted.get("finish_reason"),
            "raw_content": extracted.get("raw_content"), "usage": extracted.get("usage"),
            "latency_ms": extracted.get("latency_ms"), "estimated_cost_usd": extracted.get("estimated_cost_usd"),
            "max_tokens": extracted.get("max_tokens"), "timeout_seconds": extracted.get("timeout_seconds"),
            "thinking": extracted.get("thinking"), "request_mode": request_mode,
            "requested_aspects": normalized,
        }
        return details, normalized, facts

    # Adapter compatibility for deterministic test providers and historical probes.
    extract = getattr(provider, "extract_conditions", None)
    if not callable(extract):
        raise ChatFailure("POLICY_ASPECT_PLANNING_UNAVAILABLE", False,
                          "无法安全拆分本次复合政策问题，未生成政策结论。")
    extracted = extract(question)
    payload = _plain(extracted.get("conditions_result")) if isinstance(extracted, dict) else None
    if not isinstance(payload, dict):
        raise ChatFailure("CONDITION_SCHEMA_INVALID", False,
                          "政策方面分析结果无效，未生成政策结论。",
                          {"node": "condition_extraction"})
    request_mode = payload.get("request_mode")
    aspects = payload.get("requested_aspects")
    conditions = payload.get("conditions") or []
    if request_mode not in {"POLICY_EXPLANATION", "ELIGIBILITY_DECISION", "ACTION_EXECUTION"}:
        raise ChatFailure("CONDITION_SCHEMA_INVALID", False,
                          "政策方面分析结果缺少有效请求类型。", {"node": "condition_extraction"})
    if not isinstance(aspects, list) or not aspects or len(aspects) > MAX_POLICY_ASPECTS:
        raise ChatFailure("POLICY_ASPECT_LIMIT_EXCEEDED" if isinstance(aspects, list) and len(aspects) > MAX_POLICY_ASPECTS
                          else "CONDITION_SCHEMA_INVALID", False,
                          "政策方面数量不在安全执行范围内，未生成政策结论。",
                          {"node": "condition_extraction", "aspect_count": len(aspects) if isinstance(aspects, list) else None,
                           "limit": MAX_POLICY_ASPECTS})
    seen = set()
    normalized = []
    for item in aspects:
        row = _plain(item)
        if not isinstance(row, dict):
            raise ChatFailure("CONDITION_SCHEMA_INVALID", False, "政策方面结构无效。")
        aspect_id, anchor, text = row.get("aspect_id"), row.get("query_anchor"), row.get("text")
        if (not isinstance(aspect_id, str) or not aspect_id or aspect_id in seen or
                not isinstance(anchor, str) or not anchor or anchor not in question or
                not isinstance(text, str) or not text.strip()):
            raise ChatFailure("CONDITION_SCHEMA_INVALID", False,
                              "政策方面必须有唯一 ID、问题原文锚点和描述。",
                              {"node": "condition_extraction"})
        seen.add(aspect_id)
        normalized.append({"aspect_id": aspect_id, "text": text.strip(), "query_anchor": anchor})
    facts = []
    for condition in conditions:
        row = _plain(condition)
        if (isinstance(row, dict) and row.get("kind") == "USER_PROVIDED_FACT" and
                row.get("source_type") == "USER_PROVIDED" and isinstance(row.get("source_excerpt"), str) and
                row["source_excerpt"] in question):
            facts.append({"text": row["source_excerpt"], "source_type": "USER_PROVIDED"})
    details = {
        "node": "condition_extraction", "prompt_version": extracted.get("prompt_version"),
        "model_id": extracted.get("model_id"), "finish_reason": extracted.get("finish_reason"),
        "raw_content": extracted.get("raw_content"), "usage": extracted.get("usage"),
        "latency_ms": extracted.get("latency_ms"),
        "request_mode": request_mode, "requested_aspects": normalized,
    }
    return details, normalized, facts


def _configured_strategy(retrieval: RetrievalService) -> tuple[str, str]:
    mode = retrieval.config.mode
    if mode == "bm25":
        return "BM25", "default_low_cost_strategy"
    if mode in HYBRID_MODE_ALIASES:
        return "HYBRID_RERANK", f"legacy_mode_alias:{mode}"
    # RetrievalConfig validates the mode. Keep this guard in case a custom service
    # supplies a nonstandard config object.
    return "BM25", "unsupported_mode_safe_default"


def _actual_strategy(result: dict, selected: str) -> tuple[str, bool]:
    if selected != "HYBRID_RERANK":
        return "BM25", bool(result.get("degraded"))
    channels = result.get("channels") or {}
    failures = result.get("failures") or []
    if "vector" not in channels:
        return "BM25", True
    degraded = bool(failures or (result.get("diagnostics") or {}).get("reranker_skipped"))
    # Preserve the selected policy strategy while making a partial execution
    # observable as degraded; a successful vector channel alone does not prove
    # that BM25, fusion, and the Cross-Encoder all ran.
    return ("HYBRID_RERANK_DEGRADED" if degraded else "HYBRID_RERANK"), degraded


def _retrieve_one(retrieval: RetrievalService, query: str, *, view, selected: str) -> tuple[dict, str, bool, str | None]:
    mode = "rrf_reranker" if selected == "HYBRID_RERANK" else "bm25"
    try:
        result = retrieval.retrieve(query, mode=mode, view=view)
        actual, degraded = _actual_strategy(result, selected)
        return result, actual, degraded, None
    except RetrievalUnavailable as exc:
        if selected != "HYBRID_RERANK":
            raise
        # One bounded safe fallback. No alternate semantic pipeline is started.
        try:
            result = retrieval.retrieve(query, mode="bm25", view=view)
            return result, "BM25", True, exc.code
        except RetrievalUnavailable as fallback:
            raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE", fallback.code) from fallback


def _merge_evidence(results: list[tuple[dict, dict]], *, limit: int = MAX_MERGED_EVIDENCE) -> list[dict]:
    by_id: dict[str, dict] = {}
    for aspect, result in results:
        for row in result.get("evidence") or []:
            chunk_id = row.get("chunk_id")
            if not isinstance(chunk_id, str):
                continue
            if chunk_id not in by_id:
                by_id[chunk_id] = {**row, "requested_aspect_ids": []}
            current = by_id[chunk_id]
            if aspect["aspect_id"] not in current["requested_aspect_ids"]:
                current["requested_aspect_ids"].append(aspect["aspect_id"])
            rank = row.get("rank")
            if isinstance(rank, int):
                current["best_aspect_rank"] = min(current.get("best_aspect_rank", rank), rank)
    return sorted(by_id.values(), key=lambda row: (
        -len(row["requested_aspect_ids"]), row.get("best_aspect_rank", 10**9), row["chunk_id"]))[:limit]


def _safe_rejected_answer_text(value: str) -> tuple[str, bool]:
    """Bound and redact the generated user-facing answer; never persist provider raw content."""
    import re
    source = value[:12000]
    source = re.sub(r"(?i)\b(api[_ -]?key|authorization|access[_ -]?token|password|secret)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]", source)
    source = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", source, flags=re.I)
    # ISO policy dates are evidence, not phone numbers. Redact the regions
    # between valid dates rather than introducing reversible text placeholders.
    from datetime import date
    parts = []
    cursor = 0
    for match in re.finditer(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)", source):
        try:
            date.fromisoformat(match.group())
        except ValueError:
            continue
        parts.append(re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[REDACTED_PHONE]", source[cursor:match.start()]))
        parts.append(match.group())
        cursor = match.end()
    parts.append(re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[REDACTED_PHONE]", source[cursor:]))
    source = "".join(parts)
    source = re.sub(r"(?<!\d)\d{9,}(?!\d)", "[REDACTED_IDENTIFIER]", source)
    truncated = len(source) > 3000 or len(value) > 12000
    return source[:3000], truncated


def _safe_short_metadata(value, *, maximum: int = 120) -> str | None:
    """Keep bounded model metadata without permitting arbitrary payload text."""
    if not isinstance(value, str):
        return None
    value = value[:maximum]
    return value if re.fullmatch(r"[A-Za-z0-9_./:-]{1,120}", value) else None


def _answer_rejection_audit(*, answer: dict, evidence: list[dict], issue_codes: list[dict],
        run_id: str | None, trace_id: str | None, retrieval_audit_calls: list[dict] | None,
        failure_classification: str) -> dict:
    """Minimal owner-scoped failure evidence, tied to the current run and retrieval calls."""
    raw_answer = answer.get("answer") if isinstance(answer, dict) else None
    raw_answer = raw_answer if isinstance(raw_answer, str) else ""
    safe_answer, truncated = _safe_rejected_answer_text(raw_answer)
    sections = {}
    for name in ('business_answer','policy_answer'):
        if name in answer:
            text = answer[name]
            if isinstance(text,str):
                safe, cut = _safe_rejected_answer_text(text)
                sections[name] = {'text_redacted':safe,'truncated':cut,'chars':len(text),
                    'sha256':hashlib.sha256(text.encode('utf8')).hexdigest()}
    evidence_ids = [row.get("chunk_id") for row in evidence
        if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)][:32]
    raw_citations = answer.get("citations") if isinstance(answer, dict) else None
    raw_citations = raw_citations if isinstance(raw_citations, list) else []
    allowed = set(evidence_ids)
    valid_citations = [item for item in raw_citations if isinstance(item, str) and item in allowed][:32]
    rejected_citation_hashes = [hashlib.sha256(item.encode("utf-8", errors="replace")).hexdigest()
        for item in raw_citations if isinstance(item, str) and item not in allowed][:16]
    findings = []
    for item in issue_codes[:12]:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code):
            finding = {"code": code}
            window = item.get("window")
            if isinstance(window, str) and re.fullmatch(r"[0-9]{1,5}[A-Za-z\u3400-\u9fff]{1,12}", window):
                finding["window"] = window
            findings.append(finding)
    retrieval_links = []
    for row in (retrieval_audit_calls or [])[:8]:
        if not isinstance(row, dict):
            continue
        retrieval_links.append({key: row.get(key) for key in (
            "call_id", "result_ref", "run_id", "trace_id", "status", "capability", "evidence_ids") if key in row})
    usage = answer.get("usage") if isinstance(answer, dict) else None
    usage_summary = None
    if isinstance(usage, dict):
        def safe_count(key):
            value = usage.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        usage_summary = {"input_units": safe_count("prompt_tokens"),
            "output_units": safe_count("completion_tokens"), "total_units": safe_count("total_tokens")}
    return {"diagnostic_schema_version": "policy-answer-rejection-audit-v1",
        "validator_version": "m2-self-rag-answer-validation-v1",
        "classification": failure_classification, "run_id": run_id, "trace_id": trace_id,
        "candidate_answer_present": bool(raw_answer), "candidate_answer_redacted": safe_answer,
        "candidate_answer_scope": "M3_POLICY_SEGMENT_WITH_STRUCTURED_SECTIONS" if sections else "POLICY_ANSWER",
        "candidate_sections": sections,
        "candidate_persistence_status": "CAPTURED_REDACTED" if raw_answer and safe_answer else
            "OMITTED_EMPTY_OR_UNAVAILABLE",
        "candidate_omission_reason": None if raw_answer and safe_answer else "NO_PARSED_USER_FACING_ANSWER_FIELD",
        "candidate_answer_chars": len(raw_answer), "candidate_answer_truncated": truncated,
        "candidate_answer_sha256": hashlib.sha256(raw_answer.encode("utf-8")).hexdigest() if raw_answer else None,
        "citation_scope": "CURRENT_RUN_RETRIEVED_EVIDENCE_ONLY",
        "candidate_citation_ids": valid_citations,
        "rejected_citation_id_sha256": rejected_citation_hashes,
        "available_evidence_ids": evidence_ids,
        "retrieval_links": retrieval_links, "validation_findings": findings,
        "provider_model_id": _safe_short_metadata(answer.get("model_id")) if isinstance(answer, dict) else None,
        "provider_finish_reason": _safe_short_metadata(answer.get("finish_reason")) if isinstance(answer, dict) else None,
        "usage_summary": usage_summary}


def _citation_rows(answer: dict, evidence: list[dict], aspect_results: list[tuple[dict, dict]], *,
        run_id: str | None = None, trace_id: str | None = None,
        retrieval_audit_calls: list[dict] | None = None) -> list[dict]:
    if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
        diagnostic = _answer_rejection_audit(answer=answer if isinstance(answer, dict) else {},
            evidence=evidence, issue_codes=[], run_id=run_id, trace_id=trace_id,
            retrieval_audit_calls=retrieval_audit_calls, failure_classification="INVALID_OR_EMPTY_CANDIDATE")
        raise ChatFailure("MODEL_INVALID_RESPONSE", False, "模型答复格式无效，未保存为成功答案。", {"answer_rejection_audit": diagnostic})
    ids = answer.get("citations")
    by_id = {row["chunk_id"]: row for row in evidence}
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in by_id for item in ids):
        diagnostic = _answer_rejection_audit(answer=answer, evidence=evidence, issue_codes=[],
            run_id=run_id, trace_id=trace_id, retrieval_audit_calls=retrieval_audit_calls,
            failure_classification="CITATION_OUTSIDE_CURRENT_EVIDENCE")
        raise ChatFailure("MODEL_INVALID_RESPONSE", False,
                          "模型引用不属于本轮有效检索结果，未保存为成功答案。", {"answer_rejection_audit": diagnostic})
    cited = set(ids)
    missing = [aspect["aspect_id"] for aspect, result in aspect_results
               if result.get("evidence") and not (cited & {row["chunk_id"] for row in result["evidence"]})]
    if missing:
        diagnostic = _answer_rejection_audit(answer=answer, evidence=evidence, issue_codes=[],
            run_id=run_id, trace_id=trace_id, retrieval_audit_calls=retrieval_audit_calls,
            failure_classification="REQUIRED_ASPECT_CITATION_MISSING")
        diagnostic["missing_aspect_ids"] = missing[:8]
        raise ChatFailure("ANSWER_ASPECT_CITATION_MISSING", False,
                          "回答没有为每个已检索政策方面提供有效引用。",
                          {"answer_rejection_audit": diagnostic})
    rows = [by_id[item] for item in ids]
    from app.m2.self_rag import _answer_semantic_issues
    issues = _answer_semantic_issues(answer["answer"], rows, answer.get("question", ""))
    if issues:
        diagnostic = _answer_rejection_audit(answer=answer, evidence=evidence, issue_codes=issues,
            run_id=run_id, trace_id=trace_id, retrieval_audit_calls=retrieval_audit_calls,
            failure_classification="POLICY_SEMANTIC_VALIDATION_REJECTED")
        raise ChatFailure("ANSWER_SEMANTIC_VALIDATION_FAILED", False,
                          "模型答复未通过政策阶段或时限核验，未保存为成功答案。",
                          {"semantic_issues": issues, "answer_rejection_audit": diagnostic})
    return rows


def _verify_snapshot_rows(rows: list[dict], view) -> None:
    from app.m2.self_rag import _valid_at
    for row in rows:
        if (not isinstance(row.get("chunk_id"), str) or
                row.get("index_fingerprint") != view.fingerprint or
                row.get("knowledge_version") != view.index.get("knowledge_version") or
                not _valid_at(row, view.evaluation_at)):
            raise ChatFailure("RETRIEVAL_EVIDENCE_VERSION_INVALID", False,
                              "检索证据与本轮知识版本或有效时间不一致，未生成政策结论。",
                              {"chunk_id": row.get("chunk_id")})


def execute_policy_request(question: str, *, route: dict, provider,
                           retrieval: RetrievalService | None = None,
                           index_path=None, evaluation_at: str | None = None,
                           on_retrieval: Callable[[dict], None] | None = None,
                           on_retrieval_audit: Callable[[dict], None] | None = None,
                           run_id: str | None = None, trace_id: str | None = None,
                           memory_context: str | None = None) -> dict:
    """Run one finite SINGLE/MULTI policy request against a single pinned index view."""
    if route.get("strategy") not in {"SINGLE", "MULTI"}:
        raise ValueError("policy executor only accepts SINGLE or MULTI routes")
    retrieval = retrieval or RetrievalService(index_path, config_from_environment())
    configured, configured_reason = _configured_strategy(retrieval)
    multi = route.get("strategy") == "MULTI"
    context_notes = [memory_context] if memory_context else []
    route_usage = _route_usage(route) if route.get("decision_source") == "LLM" else None
    route_count = int(route.get("decision_source") == "LLM" or bool(route.get("fallback_error")))
    from app.m2.policy_budget import PolicyExecutionBudget
    budget = PolicyExecutionBudget(route_count=route_count, route_usage=route_usage,
                                   route_latency_ms=int(route.get("latency_ms") or 0))
    plan_details = None
    aspects: list[dict]
    user_facts: list[dict]
    request_mode = "POLICY_EXPLANATION"
    planning_call = None
    if multi:
        preflight = _reserve_model(budget, provider, "policy_aspect_planning",
                                   json.dumps({"question": question, "context_notes": context_notes}, ensure_ascii=False))
        try:
            plan_details, aspects, user_facts = _plan_multi(provider, question, context_notes)
        except ChatFailure as exc:
            _record_model_failure(budget, "policy_aspect_planning", preflight, exc)
            raise
        _record_model_call(budget, "policy_aspect_planning", plan_details, preflight)
        request_mode = plan_details["request_mode"]
        planning_call = {key: plan_details.get(key) for key in
                         ("node", "prompt_version", "model_id", "finish_reason", "usage", "latency_ms",
                          "estimated_cost_usd", "max_tokens", "timeout_seconds", "thinking")}
        selected = "HYBRID_RERANK" if len(aspects) > 1 else configured
        trigger_reason = "multiple_requested_policy_aspects" if len(aspects) > 1 else configured_reason
        if request_mode == "ACTION_EXECUTION":
            return _clarification(question, aspects, user_facts, retrieval, evaluation_at,
                                  "ACTION_EXECUTION_NOT_SUPPORTED", plan_details, planning_call,
                                  route_usage=route_usage, route_count=route_count, budget=budget)
    else:
        selected, trigger_reason = configured, configured_reason
        aspects = [{"aspect_id": "request", "text": question, "query_anchor": question}]
        user_facts = []

    try:
        view = retrieval.pin_view(evaluation_at)
    except Exception as exc:
        raise ChatFailure("RETRIEVAL_UNAVAILABLE", True,
                          "当前政策索引不可用，未生成政策结论。",
                          {"exception_type": type(exc).__name__}) from exc
    aspect_results = []
    retrieval_calls = []
    retrieval_audit_calls = []
    actual_strategies = []
    degraded = False
    budget_stop_reason = None
    for index, aspect in enumerate(aspects, 1):
        # A classifier may resolve an elliptical follow-up using owner-scoped
        # delivered conversation history. Only retrieval uses this hint; answering
        # and validation retain the actual latest user question and current evidence.
        query = f"{aspect['query_anchor']} {aspect['text']}" if multi else (route.get("retrieval_query") or question)
        aspect_selected = selected
        reserved, budget_reason = budget.reserve_query(float(getattr(retrieval.config, "timeout_seconds", 8.0)))
        if not reserved:
            budget_stop_reason = budget_reason
            for remaining in aspects[index - 1:]:
                aspect_results.append((remaining, {"evidence": [], "knowledge_version": None,
                                                   "index_fingerprint": None}))
            break
        try:
            result, actual, was_degraded, fallback_reason, audit = _retrieve_audited(
                retrieval, query, view=view, selected=aspect_selected,
                evaluation_at=evaluation_at, aspect_id=aspect["aspect_id"], on_audit=on_retrieval_audit,
                run_id=run_id, trace_id=trace_id)
        except RetrievalUnavailable as exc:
            raise ChatFailure("RETRIEVAL_UNAVAILABLE", True,
                "当前政策检索服务不可用，未生成政策结论。",
                {"aspect_id": aspect["aspect_id"], "reason": exc.code}) from exc
        budget.record_retrieval(result.get("latency_ms"))
        _verify_snapshot_rows(result.get("evidence") or [], view)
        _attach_policy_evidence_scope(result, view)
        retrieval_audit_calls.append(audit)
        attempt = {"retrieval_index": index, "aspect_id": aspect["aspect_id"],
                   "retrieval_mode": retrieval.config.mode,
                   "query_anchor": aspect["query_anchor"], "strategy_selected": aspect_selected,
                   "strategy_executed": actual, "fallback_reason": fallback_reason,
                   "call_id": audit["call_id"], "result_ref": audit["result_ref"],
                   "parameter_fingerprint": audit["parameter_fingerprint"],
                   "status": audit["status"], "executed": audit["executed"],
                   "knowledge_version": result.get("knowledge_version"),
                   "index_fingerprint": result.get("index_fingerprint"),
                   "evidence_ids": [row.get("chunk_id") for row in result.get("evidence", [])],
                   "latency_ms": result.get("latency_ms"), "degraded": was_degraded,
                   "failures": result.get("failures") or [], "models": result.get("models"),
                   "diagnostics": result.get("diagnostics") or {}}
        retrieval_calls.append(attempt)
        if on_retrieval:
            on_retrieval({"retrieval_index": index, **attempt,
                          "evidence_count": len(result.get("evidence") or [])})
        if not result.get("evidence") and aspect_selected == "BM25":
            # An empty BM25 result is an explicit evidence gap, not a score threshold.
            # One HYBRID_RERANK attempt is permitted for this aspect.
            selected = "HYBRID_RERANK"
            trigger_reason = "bm25_no_candidates"
            fallback_reserved, fallback_budget_reason = budget.reserve_query(
                float(getattr(retrieval.config, "timeout_seconds", 8.0)))
            try:
                if not fallback_reserved:
                    budget_stop_reason = fallback_budget_reason
                    raise RetrievalUnavailable("RETRIEVAL_BUDGET_EXCEEDED", fallback_budget_reason or "query_limit")
                enhanced, enhanced_actual, enhanced_degraded, enhanced_fallback, fallback_audit = _retrieve_audited(
                    retrieval, query, view=view, selected="HYBRID_RERANK",
                    evaluation_at=evaluation_at, aspect_id=aspect["aspect_id"], on_audit=on_retrieval_audit,
                    run_id=run_id, trace_id=trace_id)
                retrieval_audit_calls.append(fallback_audit)
            except RetrievalUnavailable as exc:
                enhanced = None
                enhanced_actual, enhanced_degraded, enhanced_fallback = "BM25", True, exc.code
            if enhanced is not None:
                budget.record_retrieval(enhanced.get("latency_ms"))
                _verify_snapshot_rows(enhanced.get("evidence") or [], view)
                _attach_policy_evidence_scope(enhanced, view)
                result = enhanced
                actual, was_degraded, fallback_reason = enhanced_actual, enhanced_degraded, enhanced_fallback
                attempt = {"retrieval_index": index, "aspect_id": aspect["aspect_id"],
                           "retrieval_mode": retrieval.config.mode,
                           "query_anchor": aspect["query_anchor"], "strategy_selected": "HYBRID_RERANK",
                           "strategy_executed": actual, "fallback_reason": fallback_reason,
                           "call_id": fallback_audit["call_id"], "result_ref": fallback_audit["result_ref"],
                           "parameter_fingerprint": fallback_audit["parameter_fingerprint"],
                           "status": fallback_audit["status"], "executed": fallback_audit["executed"],
                           "knowledge_version": result.get("knowledge_version"),
                           "index_fingerprint": result.get("index_fingerprint"),
                           "evidence_ids": [row.get("chunk_id") for row in result.get("evidence", [])],
                           "latency_ms": result.get("latency_ms"), "degraded": was_degraded,
                           "failures": result.get("failures") or [], "models": result.get("models"),
                           "diagnostics": result.get("diagnostics") or {}}
                retrieval_calls.append(attempt)
                if on_retrieval:
                    on_retrieval({"retrieval_index": index, **attempt,
                                  "evidence_count": len(result.get("evidence") or [])})
        actual_strategies.append(actual)
        degraded = degraded or was_degraded
        aspect_results.append((aspect, result))

    evidence = _merge_evidence(aspect_results)
    missing_aspects = [aspect["aspect_id"] for aspect, result in aspect_results if not result.get("evidence")]
    if not evidence or missing_aspects:
            return _clarification(question, aspects, user_facts, retrieval, evaluation_at,
                                  "NO_POLICY_EVIDENCE" if not evidence else "POLICY_ASPECT_EVIDENCE_MISSING",
                                  plan_details, planning_call, aspect_results=aspect_results,
                                  retrieval_calls=retrieval_calls, evidence=evidence,
                                  policy_retrieval_audit=retrieval_audit_calls,
                                  missing_aspects=missing_aspects, selected=selected,
                                  trigger_reason=trigger_reason, degraded=degraded,
                                  route_usage=route_usage, route_count=route_count, budget=budget,
                                  budget_stop_reason=budget_stop_reason)

    answer_input = json.dumps({"question": question, "requested_aspects": aspects, "context_notes": context_notes,
                               "evidence": evidence},
                              ensure_ascii=False)
    answer_preflight = _reserve_model(budget, provider, "policy_answer", answer_input)
    answer_fn = getattr(provider, "answer_for_policy_aspects", None) if multi else None
    try:
        if callable(answer_fn):
            kwargs = {"request_mode": request_mode, "requested_aspects": aspects,
                      "user_provided_facts": user_facts}
            if _supports_keyword(answer_fn, "context_notes"):
                kwargs["context_notes"] = context_notes
            completion = answer_fn(question, evidence, **kwargs)
        elif callable(getattr(provider, "answer_with_memory", None)):
            completion = provider.answer_with_memory(question, evidence, context_notes=context_notes)
        else:
            completion = provider.answer(question, evidence)
    except ChatFailure as exc:
        _record_model_failure(budget, "policy_answer", answer_preflight, exc)
        audit = (exc.diagnostic or {}).get("answer_rejection_audit")
        if audit is not None:
            # The adapter owns response parsing; this execution boundary owns
            # current Run/Trace and the completed retrieval audit association.
            audit.update(run_id=run_id, trace_id=trace_id,
                retrieval_links=[{key: row[key] for key in (
                    "call_id", "result_ref", "run_id", "trace_id", "status", "capability", "evidence_ids")
                    if key in row} for row in retrieval_audit_calls[:8]])
        raise
    _record_model_call(budget, "policy_answer", completion, answer_preflight)
    citation_rows = _citation_rows({**completion, "question": question}, evidence, aspect_results,
        run_id=run_id, trace_id=trace_id, retrieval_audit_calls=retrieval_audit_calls)
    refs_by_evidence: dict[str, list[str]] = {}
    calls_by_evidence: dict[str, list[str]] = {}
    for audit in retrieval_audit_calls:
        for evidence_id in audit.get("evidence_ids", []):
            refs_by_evidence.setdefault(evidence_id, []).append(audit["result_ref"])
            calls_by_evidence.setdefault(evidence_id, []).append(audit["call_id"])
    citations = [{key: row.get(key) for key in
                  ("chunk_id", "title", "content", "version", "policy_version", "policy_document_version",
                   "effective_from", "effective_to", "knowledge_version", "index_fingerprint",
                   "evaluation_at", "source_type") if key in row} | {
                      "result_refs": refs_by_evidence.get(row.get("chunk_id"), []),
                      "retrieval_call_ids": calls_by_evidence.get(row.get("chunk_id"), [])}
                 for row in citation_rows]
    calls = []
    if planning_call:
        calls.append({**planning_call, "usage": plan_details.get("usage")})
    calls.append({"node": "policy_answer", "model_id": completion.get("model_id"),
                  "prompt_version": completion.get("prompt_version"),
                  "finish_reason": completion.get("finish_reason"), "usage": completion.get("usage"),
                  "latency_ms": completion.get("latency_ms")})
    usage = _sum_usage(calls, route_usage)
    usage["model_requests"] = len(calls) + route_count
    usage["route_model_requests"] = route_count
    strategy_selected = selected
    strategy_executed = actual_strategies[0] if len(set(actual_strategies)) == 1 else "MIXED"
    total_latency = sum(float(item.get("latency_ms") or 0) for item in retrieval_calls)
    total_latency += sum(float(item.get("latency_ms") or 0) for item in calls)
    total_latency += float(route.get("latency_ms") or 0) if route_count else 0
    retrieval_models = {"embedding": None, "reranker": None}
    for item in retrieval_calls:
        for name in retrieval_models:
            if (item.get("models") or {}).get(name):
                retrieval_models[name] = item["models"][name]
    metadata = {
        "version": ONLINE_RETRIEVAL_VERSION, "strategy_selected": strategy_selected,
        "strategy_executed": strategy_executed, "strategy_trigger_reason": trigger_reason,
        "retrieval_count": len(retrieval_calls), "aspect_ids": [item["aspect_id"] for item in aspects],
        "degraded": degraded, "retrieval_calls": retrieval_calls,
        "requested_aspects": aspects, "request_mode": request_mode,
        "user_provided_facts": user_facts, "plan_raw_output": (plan_details or {}).get("raw_content"),
        "knowledge_version": view.index.get("knowledge_version"),
        "index_fingerprint": view.fingerprint, "evaluation_at": view.evaluation_at,
        "evidence_ids": [row["chunk_id"] for row in evidence],
        "missing_aspect_ids": [], "model_calls": calls,
    }
    return {
        "answer": completion["answer"], "answer_kind": "POLICY_ANSWER", "citations": citations,
        "evidence": evidence, "knowledge_version": view.index.get("knowledge_version"),
        "retrieval": {"mode": retrieval.config.mode, "strategy_selected": strategy_selected, "strategy_executed": strategy_executed,
            "config_version": retrieval.config.version, "config_hash": retrieval.config.config_hash,
            "index_fingerprint": view.fingerprint, "knowledge_version": view.index.get("knowledge_version"),
            "models": retrieval_models, "degraded": degraded,
            "failures": [failure for item in retrieval_calls for failure in item.get("failures", [])]},
        "policy_retrieval": metadata, "policy_retrieval_audit": retrieval_audit_calls, "usage": usage,
        "run_budget": budget.snapshot(),
        "node_usage": _sum_usage(calls), "model_request_count": len(calls),
        "route_model_request_count": route_count,
        "retrieval_hop_count": len(retrieval_calls),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "node_estimated_cost_usd": _sum_usage(calls).get("estimated_cost_usd"),
        "latency_ms": round(total_latency), "model_id": completion.get("model_id"),
        "thinking": completion.get("thinking"), "terminal_status": "SUCCEEDED",
        "stop_reason": "POLICY_EVIDENCE_ANSWERED",
    }


def _policy_parameter_fingerprint(*, query: str, selected: str, mode: str,
                                  evaluation_at: str, aspect_id: str) -> str:
    canonical = json.dumps({"query": query, "strategy_selected": selected,
        "retrieval_mode": mode, "evaluation_at": evaluation_at, "aspect_id": aspect_id},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _attach_policy_evidence_scope(result: dict, view) -> None:
    knowledge_version = result.get("knowledge_version") or view.index.get("knowledge_version")
    index_fingerprint = result.get("index_fingerprint") or view.fingerprint
    for row in result.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        row.setdefault("source_type", "POLICY_DOCUMENT")
        row.setdefault("policy_document_version", row.get("version"))
        row.setdefault("knowledge_version", knowledge_version)
        row.setdefault("index_fingerprint", index_fingerprint)
        row.setdefault("evaluation_at", view.evaluation_at)


def _retrieve_audited(retrieval, query: str, *, view, selected: str,
                      evaluation_at: str | None, aspect_id: str,
                      on_audit: Callable[[dict], None] | None,
                      run_id: str | None, trace_id: str | None):
    """Record application policy retrieval separately from business-tool events."""
    call_id = uuid.uuid4().hex
    result_ref = f"policy-retrieval:{call_id}"
    eval_time = evaluation_at or view.evaluation_at
    common = {"audit_schema_version": "1.0", "source_type": "POLICY_RETRIEVAL",
        "capability": "search_knowledge", "call_id": call_id, "result_ref": result_ref,
        "parameter_fingerprint": _policy_parameter_fingerprint(query=query, selected=selected,
            mode=retrieval.config.mode, evaluation_at=eval_time, aspect_id=aspect_id),
        "run_id": run_id, "trace_id": trace_id,
        "aspect_id": aspect_id, "retrieval_mode": retrieval.config.mode,
        "strategy_selected": selected, "evaluation_at": view.evaluation_at}
    if on_audit:
        on_audit({**common, "status": "STARTED", "executed": True})
    started = time.perf_counter()
    try:
        result, actual, degraded, fallback = _retrieve_one(retrieval, query, view=view, selected=selected)
        _verify_snapshot_rows(result.get("evidence") or [], view)
        _attach_policy_evidence_scope(result, view)
    except Exception as exc:
        failed = {**common, "status": "FAILED", "executed": True,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "error_code": getattr(exc, "code", None) or type(exc).__name__,
            "knowledge_version": view.index.get("knowledge_version"),
            "index_fingerprint": view.fingerprint, "evidence_ids": [], "document_versions": []}
        if on_audit:
            on_audit(failed)
        raise
    evidence_rows = [row for row in result.get("evidence", []) if isinstance(row, dict)]
    completed = {**common, "status": "SUCCEEDED", "executed": True,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "knowledge_version": result.get("knowledge_version") or view.index.get("knowledge_version"),
        "index_fingerprint": result.get("index_fingerprint") or view.fingerprint,
        "evidence_ids": [row.get("chunk_id") for row in evidence_rows if isinstance(row.get("chunk_id"), str)],
        "document_versions": sorted({row.get("policy_document_version") or row.get("version")
            for row in evidence_rows if isinstance(row.get("policy_document_version") or row.get("version"), str)}),
        "strategy_executed": actual, "degraded": bool(degraded)}
    if len(completed["document_versions"]) == 1:
        completed["policy_document_version"] = completed["document_versions"][0]
    if on_audit:
        on_audit(completed)
    return result, actual, degraded, fallback, completed


def _clarification(question, aspects, user_facts, retrieval, evaluation_at, reason,
                   plan_details=None, planning_call=None, *, aspect_results=None,
                   retrieval_calls=None, evidence=None, missing_aspects=None,
                   policy_retrieval_audit=None,
                   selected="BM25", trigger_reason="default_low_cost_strategy", degraded=False,
                   route_usage=None, route_count=0, budget=None, budget_stop_reason=None):
    aspect_results = aspect_results or []
    retrieval_calls = retrieval_calls or []
    evidence = evidence or []
    missing_aspects = missing_aspects or [aspect["aspect_id"] for aspect in aspects]
    calls = [{**planning_call, "usage": plan_details.get("usage")}] if planning_call else []
    usage = _sum_usage(calls, route_usage)
    usage["model_requests"] = len(calls) + route_count
    usage["route_model_requests"] = route_count
    msg = "当前有效政策索引未能为所有必要政策方面找到可核验条款，暂不能给出完整结论。"
    if reason == "ACTION_EXECUTION_NOT_SUPPORTED":
        msg = "本接口仅解释政策，不执行退款、退货或取消操作；未提交任何业务请求。"
    elif budget_stop_reason:
        msg = "本轮检索预算已达到上限，暂不能核实全部必要政策方面；未生成确定性结论。"
        reason = "RETRIEVAL_BUDGET_EXCEEDED"
    metadata = {"version": ONLINE_RETRIEVAL_VERSION, "strategy_selected": selected,
        "strategy_executed": "MIXED" if retrieval_calls and degraded else selected,
        "retrieval_count": len(retrieval_calls), "aspect_ids": [item["aspect_id"] for item in aspects],
        "strategy_trigger_reason": trigger_reason, "degraded": degraded,
        "retrieval_calls": retrieval_calls, "requested_aspects": aspects,
        "request_mode": (plan_details or {}).get("request_mode", "POLICY_EXPLANATION"),
        "user_provided_facts": user_facts, "plan_raw_output": (plan_details or {}).get("raw_content"),
        "knowledge_version": (aspect_results[0][1].get("knowledge_version") if aspect_results else None),
        "index_fingerprint": (aspect_results[0][1].get("index_fingerprint") if aspect_results else None),
        "evidence_ids": [row.get("chunk_id") for row in evidence],
        "missing_aspect_ids": missing_aspects,
        "model_calls": calls, "budget_stop_reason": budget_stop_reason}
    return {"answer": msg, "answer_kind": "CLARIFICATION_REQUIRED", "citations": [],
        "evidence": evidence, "knowledge_version": metadata.get("knowledge_version"),
        "retrieval": {"mode": retrieval.config.mode,
            "strategy_selected": selected, "strategy_executed": metadata["strategy_executed"],
            "config_version": retrieval.config.version, "config_hash": retrieval.config.config_hash,
            "index_fingerprint": metadata.get("index_fingerprint"), "degraded": degraded,
            "failures": [failure for item in retrieval_calls for failure in item.get("failures", [])]},
        "policy_retrieval": metadata, "policy_retrieval_audit": policy_retrieval_audit or [],
        "usage": usage, "node_usage": _sum_usage(calls),
        "run_budget": budget.snapshot() if budget else None,
        "model_request_count": len(calls), "route_model_request_count": route_count,
        "retrieval_hop_count": len(retrieval_calls),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "node_estimated_cost_usd": usage.get("estimated_cost_usd"), "latency_ms": None,
        "model_id": None, "thinking": None, "terminal_status": "SUCCEEDED",
        "stop_reason": reason}
