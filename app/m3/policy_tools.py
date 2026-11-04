"""Read-only Policy Agent adapter over the shared versioned M2 retrieval service."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.m2.retrieval import RetrievalService, RetrievalUnavailable
from app.m1.models import Coupon
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from .contracts import AgentResult, AgentTask, TrustedAgentContext
from .order_tools import AgentExecutionContext, ToolExecutionError


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def evidence_is_valid(row: dict[str, Any], *, fingerprint: str, knowledge_version: str,
                      evaluation_at: str) -> bool:
    """Validate snapshot identity and UTC effective interval before any answer is rendered."""
    try:
        at = _parse_time(evaluation_at)
        start = _parse_time(row["effective_from"])
        end = _parse_time(row["effective_to"]) if row.get("effective_to") else None
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        row.get("chunk_id") and row.get("version") and
        row.get("index_fingerprint") == fingerprint and
        row.get("knowledge_version") == knowledge_version and
        row.get("policy_version", row.get("version")) == row.get("version") and
        start <= at and (end is None or at < end)
    )


def _is_warranty_evidence(row: dict[str, Any]) -> bool:
    """Classify a warranty source from either its text or indexed source metadata.

    Some valid policy clauses state only the term and eligibility conditions in
    the body (for example, "18 months from recorded purchase"). Their versioned
    policy/chunk identifier or title carries the policy topic. Requiring the
    topic word to be repeated in the body silently discards those retrieved
    clauses. This remains bounded to text and metadata returned by retrieval.
    """
    body = str(row.get("content") or "").casefold()
    source_metadata = " ".join(str(row.get(key) or "") for key in ("policy_id", "chunk_id", "title")).casefold()
    terms = ("warranty", "保修", "质保")
    return any(term in body for term in terms) or any(term in source_metadata for term in terms)


class PolicyReadService:
    """Small adapter; index pinning and retrieval algorithms remain owned by M2."""
    def __init__(self, retrieval: RetrievalService, *, evaluation_at: str | None = None) -> None:
        self.retrieval = retrieval
        self.evaluation_at = evaluation_at

    def search(self, query: str, *, mode: str = "BM25", result_ref: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ToolExecutionError("POLICY_QUERY_INVALID")
        if mode not in {"BM25", "HYBRID_RERANK"}:
            raise ToolExecutionError("POLICY_RETRIEVAL_MODE_NOT_ALLOWED")
        selected = "bm25" if mode == "BM25" else "rrf_reranker"
        config = self.retrieval.config
        if (config.sparse_top_k > 5 or config.dense_top_k > 5 or config.output_top_k > 5 or
                config.rerank_candidate_limit > 10 or config.timeout_seconds > 8):
            raise ToolExecutionError("RETRIEVAL_CONFIGURATION_UNBOUNDED")
        try:
            view = self.retrieval.pin_view(self.evaluation_at)
            result = self.retrieval.retrieve(query, mode=selected, view=view)
        except RetrievalUnavailable as exc:
            raise ToolExecutionError(exc.code) from exc
        except Exception as exc:
            raise ToolExecutionError("RETRIEVAL_UNAVAILABLE") from exc
        fingerprint = view.fingerprint
        version = view.index.get("knowledge_version")
        rows = list(result.get("evidence") or [])
        if result.get("index_fingerprint") != fingerprint or result.get("knowledge_version") != version:
            raise ToolExecutionError("POLICY_SNAPSHOT_MISMATCH")
        if any(not evidence_is_valid(row, fingerprint=fingerprint,
                                     knowledge_version=version, evaluation_at=view.evaluation_at)
               for row in rows):
            raise ToolExecutionError("POLICY_EVIDENCE_INVALID")
        return {
            "source": "shoprelay_policy_retrieval",
            "result_ref": result_ref,
            "retrieval_mode": "HYBRID_RERANK" if selected == "rrf_reranker" else "BM25",
            "retrieval_count": 1,
            "knowledge_version": version,
            "index_fingerprint": fingerprint,
            "evaluation_at": view.evaluation_at,
            "config_version": result.get("config_version"),
            "config_hash": result.get("config_hash"),
            "degraded": bool(result.get("degraded")),
            "failures": list(result.get("failures") or []),
            "latency_ms": float(result.get("latency_ms") or 0),
            # Preserve bounded, non-text diagnostics so an operator can verify
            # which retrieval channels/models actually ran without exposing policy text in logs.
            "retrieval_channels": {name: [str(chunk_id) for chunk_id in ids[:5]]
                                   for name, ids in (result.get("channels") or {}).items()},
            "models": {name: value for name, value in (result.get("models") or {}).items()},
            "diagnostics": {name: value for name, value in (result.get("diagnostics") or {}).items()
                            if isinstance(value, (int, float, bool))},
            "evidence": rows[:5],
        }


class CouponReadRepository:
    """Reads only the legacy coupon catalog; catalog metadata is not entitlement proof."""
    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    def get_coupon(self, coupon_code: str) -> dict[str, Any]:
        if not isinstance(coupon_code, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,63}", coupon_code):
            raise ToolExecutionError("COUPON_CODE_INVALID")
        try:
            with self.factory() as db:
                if Coupon.__tablename__ not in set(inspect(db.get_bind()).get_table_names()):
                    return {"source": "shoprelay_coupon_catalog", "availability": "NOT_AVAILABLE",
                            "error_code": "COUPON_DATA_SOURCE_UNAVAILABLE", "coupon": None,
                            "eligibility_status": "UNKNOWN"}
                row = db.execute(select(Coupon).where(Coupon.coupon_code == coupon_code)).scalar_one_or_none()
                if row is None:
                    return {"source": "shoprelay_coupon_catalog", "availability": "NOT_FOUND",
                            "coupon_code": coupon_code, "error_code": "COUPON_NOT_FOUND",
                            "coupon": None, "eligibility_status": "UNKNOWN"}
                return {"source": "shoprelay_coupon_catalog", "availability": "AVAILABLE",
                        "coupon_code": row.coupon_code,
                        "coupon": {"policy_id": row.policy_id, "version": row.version,
                                   "min_spend_cents": row.min_spend_cents},
                        "source_type": row.source_type,
                        "eligibility_status": "UNKNOWN",
                        "unknown_reasons": ["CATALOG_HAS_NO_ACTIVE_WINDOW_OR_USER_ENTITLEMENT"]}
        except SQLAlchemyError as exc:
            raise ToolExecutionError("COUPON_DATABASE_UNAVAILABLE") from exc


def coupon_code_from_query(query: str) -> str | None:
    """Read a supplied coupon code, never natural words or an account ID.

    Explicit code markers accept normal code casing. The shorter ``coupon X``
    form accepts an uppercase token; plain prose such as ``coupon discount``
    supplies no identifier. Multiple different supplied codes need clarification.
    """
    code = r"([A-Za-z0-9][A-Za-z0-9_-]{1,63})(?![A-Za-z0-9_-])"
    explicit = re.findall(r"(?i:coupon\s+code|优惠券码|优惠码|券码)\s*"
                          r"(?:(?i:is)|为|是|[:：=])\s*[\"']?" + code, query)
    upper = re.findall(r"(?i:coupon(?:\s+code)?|优惠券码|优惠券|优惠码|券码)\s+[\"']?"
                       r"([A-Z0-9][A-Z0-9_-]{1,63})(?![A-Za-z0-9_-])", query)
    codes = {value.upper() for value in explicit + upper}
    return next(iter(codes)) if len(codes) == 1 else None


class PolicyAgent:
    """Executes registered policy reads; no policy or coupon eligibility is generated."""
    name = "policy"

    def __init__(self, service: PolicyReadService | None = None, coupon_repository=None) -> None:
        self.service = service
        self.coupon_repository = coupon_repository

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        result_id = f"{task.task_id}:result"
        if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                               error_code="AGENT_CONTEXT_INVALID")
        if task.target_agent != self.name or task.required_capabilities not in (
                ["search_knowledge"], ["get_coupon"], ["get_warranty"]):
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                               error_code="TOOL_NOT_ALLOWED")
        if task.required_capabilities == ["get_coupon"]:
            repository = self.coupon_repository or getattr(context, "coupon_repository", None)
            if repository is None or context is None:
                # A capability without an injected trusted repository is not
                # registered for this execution and must fail closed.
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                    error_code="TOOL_NOT_ALLOWED")
            bind_observability = getattr(repository, "set_execution_context", None)
            if callable(bind_observability):
                bind_observability(context)
            code = coupon_code_from_query(context.user_query)
            if code is None:
                return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                    missing_information=["COUPON_CODE_REQUIRED"])
            try:
                ref = context.execute_coupon_read("get_coupon", {"coupon_code": code},
                    lambda: repository.get_coupon(code))
            except ToolExecutionError as exc:
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED", error_code=exc.code)
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                tool_result_refs=[ref])
        service = self.service or (PolicyReadService(context.retrieval_service,
            evaluation_at=context.retrieval_evaluation_at) if context and context.retrieval_service else None)
        if service is None or context is None:
            return AgentResult(result_id=result_id, source_agent=self.name, status="NOT_IMPLEMENTED",
                missing_information=["POLICY_RETRIEVAL_CONTEXT_UNAVAILABLE"], error_code="AGENT_NOT_IMPLEMENTED")
        if task.required_capabilities == ["get_warranty"]:
            product_ids = re.findall(r"(?<![A-Z0-9])P-[A-Z0-9_-]{2,48}(?![A-Z0-9])", context.user_query.upper())
            if not product_ids:
                product_ids = list({item["product_id"] for ref in task.context_refs
                    if isinstance((value := context.result_store.get(ref)), dict)
                    and value.get("source") == "business_database"
                    for item in value.get("items", []) if isinstance(item.get("product_id"), str)})
            if len(set(product_ids)) != 1:
                return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                    missing_information=["PRODUCT_ID_REQUIRED" if not product_ids else "PRODUCT_ID_AMBIGUOUS"])
            product_id = product_ids[0]
            search_ref = "tool_result:" + hashlib.sha256(
                f"{context.run_id}|warranty-policy-search|{product_id}".encode()).hexdigest()[:48]
            try:
                context.execute_policy_read("get_warranty", query=f"商品保修政策 {product_id}", mode="BM25",
                    result_ref=search_ref,
                    resource_ids={"product_id": product_id},
                    operation=lambda: service.search(f"商品保修政策 warranty {product_id}", mode="BM25", result_ref=search_ref))
            except ToolExecutionError as exc:
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED", error_code=exc.code)
            policy_payload = context.result_store.get(search_ref) or {}
            evidence = [row for row in policy_payload.get("evidence", [])
                        if isinstance(row, dict) and _is_warranty_evidence(row)]
            warranty_ref = "tool_result:" + hashlib.sha256(
                f"{context.run_id}|warranty-result|{product_id}".encode()).hexdigest()[:48]
            context.result_store[warranty_ref] = {"source": "shoprelay_warranty_policy",
                "availability": "AVAILABLE" if evidence else "NOT_AVAILABLE", "product_id": product_id,
                "eligibility_status": "UNKNOWN",
                "unknown_reasons": ["PURCHASE_FACT_NOT_VERIFIED", "ACTIVATION_OR_WARRANTY_RECORD_NOT_AVAILABLE"],
                "policy_result_ref": search_ref, "evidence": evidence,
                "knowledge_version": policy_payload.get("knowledge_version"),
                "index_fingerprint": policy_payload.get("index_fingerprint"),
                "evaluation_at": policy_payload.get("evaluation_at")}
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                tool_result_refs=[warranty_ref, search_ref],
                missing_information=[] if evidence else ["WARRANTY_POLICY_EVIDENCE_NOT_FOUND"])
        route = context.route_payload or {}
        multi = route.get("sub_strategy") == "AGENT+MULTI" or \
            route.get("reason_code") == "MULTI_POLICY_CONDITIONS"
        retrieval_config = getattr(context.retrieval_service, "config", None)
        configured_mode = str(getattr(retrieval_config, "mode", "bm25")).casefold()
        # MULTI describes the orchestration/task relationship; it does not
        # authorize a dense or reranker channel. When the bound retrieval
        # service is BM25-only (as in the personal synthetic Dev database),
        # keep policy reads on that configured channel instead of failing or
        # silently invoking an unbudgeted external retrieval path.
        mode = "HYBRID_RERANK" if multi and configured_mode != "bm25" else "BM25"
        query_hash = hashlib.sha256(context.user_query.encode("utf-8")).hexdigest()[:20]
        ref = "tool_result:" + hashlib.sha256(
            f"{context.run_id}|search_knowledge|{query_hash}".encode("utf-8")).hexdigest()[:48]
        try:
            context.execute_policy_read("search_knowledge", query=context.user_query, mode=mode, result_ref=ref,
                operation=lambda: service.search(context.user_query, mode=mode, result_ref=ref))
        except ToolExecutionError as exc:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                               error_code=exc.code)
        payload = context.result_store.get(ref) or {}
        evidence_refs = ["evidence:" + hashlib.sha256(row["chunk_id"].encode("utf-8")).hexdigest()[:32]
                         for row in payload.get("evidence", []) if isinstance(row.get("chunk_id"), str)]
        missing = ["POLICY_EVIDENCE_NOT_FOUND"] if not evidence_refs else []
        return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
            evidence_refs=evidence_refs, tool_result_refs=[ref], missing_information=missing)


def build_policy_business_result(supervisor_result: dict[str, Any], *, route: dict[str, Any],
                                 result_store: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Render only exact excerpts from verified, current evidence returned this run."""
    clarification_messages = {
        "COUPON_CODE_REQUIRED": "请提供需要核实的优惠券码；当前不能确认优惠券有效性、个人领取资格或可用折扣。",
        "PRODUCT_ID_REQUIRED": "请提供需要查询保修的商品编号；核实个人保修资格还需要购买、激活或保修登记事实。",
        "PRODUCT_ID_AMBIGUOUS": "请明确需要查询保修的一个商品编号；当前不能确定所指商品或个人保修资格。",
    }
    if route.get("required_capabilities") in (["get_coupon"], ["get_warranty"]):
        handoffs = supervisor_result.get("handoffs") or []
        if supervisor_result.get("supervisor_status") == "COMPLETED" and len(handoffs) == 1:
            handoff = handoffs[0]
            result = handoff.get("result") or {}
            missing = result.get("missing_information") or []
            statuses = supervisor_result.get("tool_execution_status") or []
            known_not_executed = (len(statuses) == 1 and
                statuses[0].get("capability") == route["required_capabilities"][0] and
                statuses[0].get("status") == "NOT_EXECUTED" and statuses[0].get("executed") is False)
            if (handoff.get("target_agent") == "policy" and result.get("source_agent") == "policy" and
                    result.get("status") == "COMPLETED" and not result.get("tool_result_refs") and
                    len(missing) == 1 and missing[0] in clarification_messages and
                    known_not_executed):
                return {"answer": clarification_messages[missing[0]], "answer_kind": "CLARIFICATION_REQUIRED",
                    "business_results": {}, "tool_references": [], "tool_execution_status": statuses,
                    "citations": [], "evidence": [], "latency_ms": 0}
    if route.get("required_capabilities") == ["get_warranty"]:
        handoffs = supervisor_result.get("handoffs") or []
        result = (handoffs[0].get("result") or {}) if len(handoffs) == 1 else {}
        refs = result.get("tool_result_refs") or []
        warranty = result_store.get(refs[0]) if len(refs) == 2 else None
        policy = result_store.get(refs[1]) if len(refs) == 2 else None
        status = next((row for row in supervisor_result.get("tool_execution_status", [])
                       if row.get("capability") == "get_warranty"), None)
        if supervisor_result.get("supervisor_status") != "COMPLETED" or \
                result.get("source_agent") != "policy" or not isinstance(warranty, dict) or \
                warranty.get("source") != "shoprelay_warranty_policy" or not isinstance(policy, dict) or \
                policy.get("source") != "shoprelay_policy_retrieval" or not status or not status.get("executed"):
            return None
        evidence = warranty.get("evidence") or []
        if evidence and any(not evidence_is_valid(row, fingerprint=policy["index_fingerprint"],
                knowledge_version=policy["knowledge_version"], evaluation_at=policy["evaluation_at"])
                for row in evidence):
            return None
        excerpts = " ".join(f"【{row.get('chunk_id')}】{str(row.get('content', ''))[:500]}" for row in evidence[:2])
        answer = (f"已检索到商品 {warranty['product_id']} 可适用的保修政策证据：{excerpts}。"
                  "但当前数据没有核实购买时间、激活时间或该商品的保修登记，因此不能确认这件商品的实际保修资格。") if evidence else \
                 f"当前政策索引没有找到商品 {warranty['product_id']} 的保修条款，不能确认保修规则或实际资格。"
        retrieval_metadata = {key: policy.get(key) for key in (
            "retrieval_mode", "knowledge_version", "index_fingerprint", "evaluation_at",
            "config_version", "config_hash", "degraded")}
        return {"answer": answer, "answer_kind": "POLICY_ANSWER" if evidence else "CLARIFICATION_REQUIRED",
            "business_results": {"product_id": warranty["product_id"], "eligibility_status": "UNKNOWN",
                "knowledge_version": policy.get("knowledge_version"),
                "index_fingerprint": policy.get("index_fingerprint"),
                "evaluation_at": policy.get("evaluation_at"),
                "evidence_ids": [row.get("chunk_id") for row in evidence]},
            "tool_references": [{"tool": "get_warranty", "source": "shoprelay_warranty_policy", "result_ref": refs[0]},
                                {"tool": "search_knowledge", "source": "shoprelay_policy_retrieval", "result_ref": refs[1]}],
            "citations": evidence, "evidence": evidence, "knowledge_version": policy.get("knowledge_version"),
            "policy_retrieval": retrieval_metadata,
            "retrieval": {"mode": policy.get("retrieval_mode"),
                "knowledge_version": policy.get("knowledge_version"),
                "index_fingerprint": policy.get("index_fingerprint"),
                "evaluation_at": policy.get("evaluation_at"),
                "config_version": policy.get("config_version"), "config_hash": policy.get("config_hash"),
                "degraded": policy.get("degraded")},
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": float(status.get("duration_ms") or 0)}
    if route.get("required_capabilities") == ["get_coupon"]:
        handoffs = supervisor_result.get("handoffs") or []
        result = (handoffs[0].get("result") or {}) if len(handoffs) == 1 else {}
        refs = result.get("tool_result_refs") or []
        payload = result_store.get(refs[0]) if len(refs) == 1 else None
        status_row = next((row for row in supervisor_result.get("tool_execution_status", [])
                           if row.get("capability") == "get_coupon"), None)
        if supervisor_result.get("supervisor_status") != "COMPLETED" or \
                not isinstance(payload, dict) or payload.get("source") != "shoprelay_coupon_catalog" or \
                result.get("source_agent") != "policy" or not status_row or not status_row.get("executed"):
            return None
        if payload.get("availability") == "NOT_FOUND":
            answer = f"没有在优惠券目录中找到券码 {payload.get('coupon_code')}。"
        elif payload.get("availability") == "NOT_AVAILABLE":
            answer = "当前没有可用的优惠券目录数据源，无法核实该券信息。"
        else:
            coupon = payload.get("coupon") or {}
            answer = (f"目录记录显示券码 {payload.get('coupon_code')} 关联政策 {coupon.get('policy_id')} "
                      f"（版本 {coupon.get('version')}），目录最低消费门槛为 {coupon.get('min_spend_cents')} 分。"
                      "数据源没有用户领取状态、有效时间或订单抵扣记录，因此不能确认当前可用或已抵扣。")
        return {"answer": answer, "answer_kind": "BUSINESS_FACTS",
            "business_results": {"coupon": payload, "eligibility_status": payload.get("eligibility_status", "UNKNOWN")},
            "tool_references": [{"tool": "get_coupon", "source": payload["source"], "result_ref": refs[0]}],
            "citations": [], "evidence": [], "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": float(status_row.get("duration_ms") or 0)}
    if route.get("required_capabilities") != ["search_knowledge"]:
        return None
    handoffs = supervisor_result.get("handoffs") or []
    if supervisor_result.get("supervisor_status") != "COMPLETED" or \
       supervisor_result.get("terminal_status") != "RUNNING" or len(handoffs) != 1:
        return None
    handoff = handoffs[0]
    if (handoff.get("target_agent") != "policy" or handoff.get("status") != "COMPLETED" or
            handoff.get("required_capabilities") != ["search_knowledge"]):
        return None
    agent_result = handoff.get("result") or {}
    if agent_result.get("source_agent") != "policy" or agent_result.get("status") != "COMPLETED":
        return None
    refs = agent_result.get("tool_result_refs") or []
    if len(refs) != 1:
        return None
    payload = result_store.get(refs[0])
    if not isinstance(payload, dict) or payload.get("source") != "shoprelay_policy_retrieval":
        return None
    retrieved_rows = payload.get("evidence") or []
    if not retrieved_rows:
        return {"answer": "当前锁定的有效政策索引未找到可核验条款，因此我不能给出确定的政策结论。",
                "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {},
                "tool_references": [{"tool": "search_knowledge", "source": payload["source"],
                                     "result_ref": refs[0]}], "citations": [], "evidence": [],
                "knowledge_version": payload.get("knowledge_version"),
                "policy_retrieval": _safe_policy_payload(payload),
                "tool_execution_status": supervisor_result.get("tool_execution_status", []),
                "latency_ms": payload.get("latency_ms", 0)}
    fingerprint, version, evaluation_at = (payload.get("index_fingerprint"),
        payload.get("knowledge_version"), payload.get("evaluation_at"))
    if not all(isinstance(value, str) and value for value in (fingerprint, version, evaluation_at)):
        return None
    if any(not evidence_is_valid(row, fingerprint=fingerprint,
            knowledge_version=version, evaluation_at=evaluation_at) for row in retrieved_rows):
        return None
    # BM25 is the single-aspect default; do not turn adjacent hits into extra
    # policy assertions. Cross-policy requests use the existing hybrid reranker
    # and may cite a bounded set of independently retrieved clauses.
    answer_rows = retrieved_rows[:3] if payload.get("retrieval_mode") == "HYBRID_RERANK" else retrieved_rows[:1]
    citations = [{key: row.get(key) for key in (
        "chunk_id", "policy_id", "clause_id", "title", "content", "version", "policy_version", "effective_from",
        "effective_to", "knowledge_version", "index_fingerprint", "rank", "score", "score_type")
        if key in row} for row in answer_rows]
    # This is a constrained source excerpt, not an independently generated policy interpretation.
    answer = "检索到以下当前有效政策原文，具体适用仍以条款范围和已核实事实为准：" + " ".join(
        f"【{row['title']} / {row['chunk_id']} / v{row['version']}】{row['content'][:700]}"
        for row in answer_rows)
    statuses = supervisor_result.get("tool_execution_status", [])
    executed = next((item for item in statuses if item.get("capability") == "search_knowledge"), None)
    if not executed or not executed.get("executed") or executed.get("status") not in {"COMPLETED", "DEGRADED"}:
        return None
    return {"answer": answer, "answer_kind": "POLICY_ANSWER", "business_results": {
                "knowledge_version": version, "index_fingerprint": fingerprint,
                "evaluation_at": evaluation_at, "retrieval_mode": payload.get("retrieval_mode"),
                "evidence_ids": [row["chunk_id"] for row in answer_rows]},
            "tool_references": [{"tool": "search_knowledge", "source": payload["source"],
                                 "result_ref": refs[0]}],
            "tool_execution_status": statuses, "citations": citations, "evidence": citations,
            "knowledge_version": version, "retrieval": {
                "mode": payload.get("retrieval_mode"), "config_version": payload.get("config_version"),
                "config_hash": payload.get("config_hash"), "knowledge_version": version,
                "index_fingerprint": fingerprint, "degraded": payload.get("degraded"),
                "failures": payload.get("failures", [])},
            "policy_retrieval": _safe_policy_payload(payload),
            "retrieval_hop_count": payload.get("retrieval_count", 0),
            "latency_ms": round(float(payload.get("latency_ms") or 0), 3)}


def _safe_policy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in (
        "retrieval_mode", "retrieval_count", "knowledge_version", "index_fingerprint",
        "evaluation_at", "config_version", "config_hash", "degraded", "failures", "latency_ms",
        "retrieval_channels", "models", "diagnostics")}
