import json
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.m1.chat import ChatFailure
from app.m2.policy_execution import execute_policy_request
from app.m2.retrieval import RetrievalView


RETURN = {
    "chunk_id": "policy-return", "title": "质量退货",
    "content": "签收后30天内的质量问题可提交整单退货申请，由人工审核。",
    "version": "policy-v1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
    "knowledge_version": "kb-v1", "index_fingerprint": "fp-v1", "rank": 1,
    "score": 1.0, "score_type": "bm25_raw",
}
REFUND = {
    "chunk_id": "policy-refund", "title": "退款前置条件",
    "content": "已签收订单需要已批准的退货申请才能申请退款。申请退款不代表款项已到账。",
    "version": "policy-v1", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
    "knowledge_version": "kb-v1", "index_fingerprint": "fp-v1", "rank": 2,
    "score": 0.9, "score_type": "bm25_raw",
}


class RetrievalStub:
    def __init__(self, *, mode="bm25", bm25_empty=False, bad_snapshot=False, reranker_skipped=False):
        self.config = SimpleNamespace(mode=mode, version="m2-r-test", config_hash="test-hash")
        self.calls = []
        self.bm25_empty = bm25_empty
        self.bad_snapshot = bad_snapshot
        self.reranker_skipped = reranker_skipped

    def pin_view(self, evaluation_at=None):
        return RetrievalView({"knowledge_version": "kb-v1", "chunks": []}, "fp-v1",
                             evaluation_at or "2026-10-20T00:00:00Z")

    def retrieve(self, query, *, mode=None, view=None):
        self.calls.append((query, mode, view.fingerprint))
        if mode == "bm25" and self.bm25_empty:
            rows = []
        elif "退货" in query and "退款" not in query:
            rows = [dict(RETURN)]
        elif "退款" in query and "退货" not in query:
            rows = [dict(REFUND)]
        else:
            rows = [dict(RETURN), dict(REFUND)]
        if self.bad_snapshot and rows:
            rows[0]["index_fingerprint"] = "old-fp"
        channels = {"bm25": [row["chunk_id"] for row in rows]}
        models = {"embedding": None, "reranker": None}
        if mode == "rrf_reranker":
            channels["vector"] = [row["chunk_id"] for row in rows]
            models = {"embedding": "bge-small-test", "reranker": "bge-reranker-test"}
        return {"knowledge_version": "kb-v1", "index_fingerprint": "fp-v1", "config_version": "m2-r-test",
                "config_hash": "test-hash", "mode": mode, "evidence": rows, "channels": channels,
                "models": models, "diagnostics": {"reranker_skipped": self.reranker_skipped and mode == "rrf_reranker"},
                "latency_ms": 2, "degraded": self.reranker_skipped and mode == "rrf_reranker",
                "failures": ([{"channel": "reranker", "error": "RERANKER_INFERENCE_FAILED"}]
                             if self.reranker_skipped and mode == "rrf_reranker" else [])}


class ProviderStub:
    def __init__(self, aspects=None, answer=None):
        self.aspects = aspects or [
            {"aspect_id": "return-window", "text": "质量退货时间窗口", "query_anchor": "退货"},
            {"aspect_id": "refund-prerequisite", "text": "退款申请前置条件", "query_anchor": "退款"},
        ]
        self.answer_text = answer
        self.plan_calls = 0
        self.answer_calls = 0
        self.aspect_answer_calls = 0

    def extract_conditions(self, question):
        self.plan_calls += 1
        return {"conditions_result": {"request_mode": "POLICY_EXPLANATION", "analysis_version": "request_intent_v1",
                "requested_aspects": self.aspects, "conditions": []},
            "prompt_version": "request-intent-v1", "model_id": "stub",
            "raw_content": "{\"stub\":true}", "finish_reason": "stop",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "cost_estimate_usd_peak_rate": 0.00001},
            "latency_ms": 3}

    def answer(self, question, evidence):
        self.answer_calls += 1
        return self._answer(evidence, multi=False)

    def answer_for_policy_aspects(self, question, evidence, **kwargs):
        self.aspect_answer_calls += 1
        return self._answer(evidence, multi=True)

    def _answer(self, evidence, *, multi):
        answer_text = self.answer_text or (
            "质量退货须在签收后30天内提交并经人工审核；已签收订单须有获批退货申请后才能申请退款。"
            if multi else "质量问题可在签收后30天内提交整单退货申请，并由人工审核。")
        return {"answer": answer_text, "citations": [row["chunk_id"] for row in evidence],
            "model_id": "stub", "thinking": "disabled", "prompt_version": "answer-v1",
            "finish_reason": "stop", "usage": {"prompt_tokens": 40, "completion_tokens": 30,
            "cost_estimate_usd_peak_rate": 0.0001}, "latency_ms": 5, "estimated_cost_usd": 0.0001}


def test_single_defaults_to_bm25_and_records_strategy_and_versions():
    retrieval, provider, events = RetrievalStub(), ProviderStub(), []
    result = execute_policy_request("退货政策", route={"strategy": "SINGLE", "decision_source": "RULE"},
        provider=provider, retrieval=retrieval, on_retrieval=events.append)
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert [call[1] for call in retrieval.calls] == ["bm25"]
    assert result["retrieval"]["strategy_selected"] == "BM25"
    assert result["policy_retrieval"]["index_fingerprint"] == "fp-v1"
    assert result["model_request_count"] == 1
    assert len(events) == 1


def test_policy_retrieval_audit_has_safe_call_identity_and_links_citations():
    retrieval, provider, audit_events = RetrievalStub(), ProviderStub(), []
    result = execute_policy_request("return query", route={"strategy": "SINGLE", "decision_source": "RULE"},
        provider=provider, retrieval=retrieval, on_retrieval_audit=audit_events.append,
        run_id="run-policy-audit", trace_id="trace-policy-audit")
    assert [row["status"] for row in audit_events] == ["STARTED", "SUCCEEDED"]
    started, finished = audit_events
    assert finished["source_type"] == "POLICY_RETRIEVAL"
    assert finished["capability"] == "search_knowledge"
    assert finished["run_id"] == "run-policy-audit" and finished["trace_id"] == "trace-policy-audit"
    assert finished["call_id"] == started["call_id"]
    assert finished["result_ref"] == started["result_ref"]
    assert len(finished["parameter_fingerprint"]) == 64
    assert "query" not in finished and "question" not in finished
    assert finished["knowledge_version"] == "kb-v1"
    assert finished["index_fingerprint"] == "fp-v1"
    assert result["policy_retrieval_audit"] == [finished]
    assert all(finished["result_ref"] in citation["result_refs"] and
        finished["call_id"] in citation["retrieval_call_ids"] for citation in result["citations"])


def test_multi_uses_bounded_aspect_hybrid_without_grade_or_self_rag():
    retrieval, provider = RetrievalStub(), ProviderStub()
    result = execute_policy_request("退货与退款政策如何衔接？", route={"strategy": "MULTI", "decision_source": "RULE"},
        provider=provider, retrieval=retrieval)
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert provider.plan_calls == provider.aspect_answer_calls == 1
    assert provider.answer_calls == 0
    assert [call[1] for call in retrieval.calls] == ["rrf_reranker", "rrf_reranker"]
    assert result["policy_retrieval"]["strategy_selected"] == "HYBRID_RERANK"
    assert result["policy_retrieval"]["strategy_trigger_reason"] == "multiple_requested_policy_aspects"
    assert set(result["policy_retrieval"]["evidence_ids"]) == {"policy-return", "policy-refund"}
    assert {row["chunk_id"] for row in result["citations"]} == {"policy-return", "policy-refund"}
    assert result["model_request_count"] == 2
    assert "self_rag" not in result and "grade_result" not in result


def test_multi_fails_closed_on_unanchored_or_over_limit_aspects():
    query = "退货与退款如何衔接？"
    retrieval = RetrievalStub()
    with pytest.raises(ChatFailure) as bad_anchor:
        execute_policy_request(query, route={"strategy": "MULTI"}, provider=ProviderStub(aspects=[
            {"aspect_id": "invented", "text": "取消政策", "query_anchor": "取消"}]), retrieval=retrieval)
    assert bad_anchor.value.code == "CONDITION_SCHEMA_INVALID"
    assert not retrieval.calls
    aspects = [{"aspect_id": f"a{i}", "text": "aspect", "query_anchor": "退货"} for i in range(5)]
    with pytest.raises(ChatFailure) as over_limit:
        execute_policy_request(query, route={"strategy": "MULTI"}, provider=ProviderStub(aspects=aspects),
                               retrieval=RetrievalStub())
    assert over_limit.value.code == "POLICY_ASPECT_LIMIT_EXCEEDED"


def test_bm25_empty_result_triggers_one_hybrid_attempt():
    retrieval, provider = RetrievalStub(bm25_empty=True), ProviderStub()
    result = execute_policy_request("退货政策", route={"strategy": "SINGLE"}, provider=provider,
                                    retrieval=retrieval)
    assert [call[1] for call in retrieval.calls] == ["bm25", "rrf_reranker"]
    assert result["policy_retrieval"]["strategy_selected"] == "HYBRID_RERANK"
    assert result["policy_retrieval"]["strategy_trigger_reason"] == "bm25_no_candidates"
    assert result["answer_kind"] == "POLICY_ANSWER"


def test_hybrid_partial_execution_is_reported_as_degraded():
    retrieval, provider = RetrievalStub(mode="rrf_reranker", reranker_skipped=True), ProviderStub()
    result = execute_policy_request("退货政策", route={"strategy": "SINGLE"}, provider=provider,
                                    retrieval=retrieval)
    assert result["policy_retrieval"]["strategy_selected"] == "HYBRID_RERANK"
    assert result["policy_retrieval"]["strategy_executed"] == "HYBRID_RERANK_DEGRADED"
    assert result["policy_retrieval"]["degraded"] is True


def test_missing_policy_aspect_returns_clarification_without_answer_call():
    retrieval, provider = RetrievalStub(), ProviderStub()
    original = retrieval.retrieve
    def one_missing(query, *, mode=None, view=None):
        result = original(query, mode=mode, view=view)
        if "退款" in query:
            result["evidence"] = []
        return result
    retrieval.retrieve = one_missing
    result = execute_policy_request("退货与退款政策如何衔接？", route={"strategy": "MULTI"},
                                    provider=provider, retrieval=retrieval)
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert result["stop_reason"] == "POLICY_ASPECT_EVIDENCE_MISSING"
    assert result["policy_retrieval"]["missing_aspect_ids"] == ["refund-prerequisite"]
    assert provider.aspect_answer_calls == 0


def test_multi_prefers_separate_bounded_aspect_plan_contract():
    class BoundedPlanProvider(ProviderStub):
        def __init__(self):
            super().__init__()
            self.plan_calls = 0

        def plan_policy_aspects(self, question):
            self.plan_calls += 1
            return {"plan_result": {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [
                {"aspect_id": "return", "text": "质量退货窗口", "query_anchor": "退货"},
                {"aspect_id": "refund", "text": "退款申请前置", "query_anchor": "退款"}],
                "user_provided_facts": [{"text": "用户称商品有瑕疵", "source_excerpt": "瑕疵"}]},
                "raw_content": "plan-only", "prompt_version": "m2-r-plan-v1", "usage": {
                    "prompt_tokens": 5, "completion_tokens": 4}, "latency_ms": 1,
                "model_id": "stub", "finish_reason": "stop", "estimated_cost_usd": 0.00001}

        def extract_conditions(self, question):
            raise AssertionError("online path must not call legacy full condition extraction")

    provider = BoundedPlanProvider()
    result = execute_policy_request("瑕疵商品退货与退款如何衔接？",
        route={"strategy": "MULTI", "decision_source": "RULE"}, provider=provider,
        retrieval=RetrievalStub())
    assert provider.plan_calls == 1
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert result["policy_retrieval"]["user_provided_facts"] == [
        {"text": "用户称商品有瑕疵", "source_excerpt": "瑕疵", "source_type": "USER_PROVIDED"}]
    assert result["run_budget"]["model_requests"] == 2
    assert result["run_budget"]["retrieval_queries"] == 2


def test_multi_plan_rejects_user_fact_without_exact_source_excerpt():
    class InvalidFactPlan(ProviderStub):
        def plan_policy_aspects(self, question):
            return {"plan_result": {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [
                {"aspect_id": "return", "text": "退货", "query_anchor": "退货"}],
                "user_provided_facts": [{"text": "已签收", "source_excerpt": "用户已签收"}]},
                "raw_content": "invalid", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    with pytest.raises(ChatFailure) as exc:
        execute_policy_request("退货规则是什么？", route={"strategy": "MULTI"},
                               provider=InvalidFactPlan(), retrieval=RetrievalStub())
    assert exc.value.code == "POLICY_ASPECT_PLAN_INVALID"
    assert not RetrievalStub().calls


def test_retrieval_query_budget_stops_before_answer_without_losing_missing_aspect(monkeypatch):
    monkeypatch.setenv("SHOPRELAY_MAX_RETRIEVAL_QUERIES", "1")
    retrieval, provider = RetrievalStub(), ProviderStub()
    result = execute_policy_request("退货与退款政策如何衔接？", route={"strategy": "MULTI"},
                                    provider=provider, retrieval=retrieval)
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert result["stop_reason"] == "RETRIEVAL_BUDGET_EXCEEDED"
    assert result["run_budget"]["retrieval_queries"] == 1
    assert result["policy_retrieval"]["missing_aspect_ids"] == ["refund-prerequisite"]
    assert provider.aspect_answer_calls == 0


def test_out_of_snapshot_policy_evidence_is_rejected():
    with pytest.raises(ChatFailure) as exc:
        execute_policy_request("退货政策", route={"strategy": "SINGLE"}, provider=ProviderStub(),
                               retrieval=RetrievalStub(bad_snapshot=True))
    assert exc.value.code == "RETRIEVAL_EVIDENCE_VERSION_INVALID"


def test_answer_with_unsupported_policy_stage_is_rejected():
    provider = ProviderStub(answer="必须完成退货后才能申请退款。")
    with pytest.raises(ChatFailure) as exc:
        execute_policy_request("退货与退款政策如何衔接？", route={"strategy": "MULTI"}, provider=provider,
                               retrieval=RetrievalStub())
    assert exc.value.code == "ANSWER_SEMANTIC_VALIDATION_FAILED"


@pytest.mark.parametrize("answer", [
    "申请退货不代表款项已真实到账。",
    "退货申请不意味着资金已经入账。",
    "退货获批后就会收到款项。",
])
def test_answer_cannot_transfer_refund_settlement_rule_to_return_stage(answer):
    provider = ProviderStub(answer=answer)
    with pytest.raises(ChatFailure) as exc:
        execute_policy_request("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？",
            route={"strategy": "MULTI", "decision_source": "RULE"}, provider=provider,
            retrieval=RetrievalStub())
    assert exc.value.code == "ANSWER_SEMANTIC_VALIDATION_FAILED"
    assert any(issue["code"] == "ANSWER_UNSUPPORTED_CROSS_STAGE_SCOPE"
               for issue in exc.value.diagnostic["semantic_issues"])


def test_answer_may_scope_settlement_disclaimer_to_refund_application():
    provider = ProviderStub(answer=(
        "已签收订单须有已批准的退货申请才能申请退款；退款申请不代表款项到账。"))
    result = execute_policy_request("退货与退款政策如何衔接？",
        route={"strategy": "MULTI", "decision_source": "RULE"}, provider=provider,
        retrieval=RetrievalStub())
    assert result["answer_kind"] == "POLICY_ANSWER"


def test_rejected_policy_answer_is_persistable_bounded_and_linked_to_current_run():
    rows = [
        {"chunk_id": "syn-policy-giftcard-a", "title": "Fictional Rule A",
         "content": "Fictional rule A: gift card expires after 12 months.", "version": "SYN-GC-A",
         "policy_document_version": "SYN-GC-A", "effective_from": "2026-01-01T00:00:00Z",
         "effective_to": "2026-12-31T23:59:59Z", "knowledge_version": "kb-v1",
         "index_fingerprint": "fp-v1", "rank": 1, "score": 1.0, "score_type": "bm25_raw"},
        {"chunk_id": "syn-policy-giftcard-b", "title": "Fictional Rule B",
         "content": "Fictional rule B: gift card does not expire.", "version": "SYN-GC-B",
         "policy_document_version": "SYN-GC-B", "effective_from": "2026-01-01T00:00:00Z",
         "effective_to": "2026-12-31T23:59:59Z", "knowledge_version": "kb-v1",
         "index_fingerprint": "fp-v1", "rank": 2, "score": .9, "score_type": "bm25_raw"},
    ]

    class GiftCardRetrieval:
        config = SimpleNamespace(mode="bm25", version="test", config_hash="cfg")

        def pin_view(self, evaluation_at=None):
            return RetrievalView({"knowledge_version": "kb-v1", "chunks": []}, "fp-v1",
                evaluation_at or "2026-10-27T00:00:00Z")

        def retrieve(self, _query, *, mode=None, view=None):
            return {"knowledge_version": "kb-v1", "index_fingerprint": "fp-v1",
                "config_version": "test", "config_hash": "cfg", "mode": mode,
                "evidence": [dict(row) for row in rows], "channels": {"bm25": [row["chunk_id"] for row in rows]},
                "models": {"embedding": None, "reranker": None}, "diagnostics": {},
                "latency_ms": 1, "degraded": False, "failures": []}

    class RejectedAnswerProvider:
        def answer(self, _question, evidence):
            return {"answer": "礼品卡将在12个月后过期。联系 test@example.invalid 或 555-123-4567。",
                "citations": [next(row["chunk_id"] for row in evidence if row["chunk_id"].endswith("-b"))],
                "model_id": "offline-stub", "finish_reason": "stop",
                "reasoning_content": "MUST_NOT_BE_PERSISTED",
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}}

    audit_events = []
    with pytest.raises(ChatFailure) as exc:
        execute_policy_request("Compare the two fictional gift-card expiry rules.",
            route={"strategy": "SINGLE", "decision_source": "RULE"},
            provider=RejectedAnswerProvider(), retrieval=GiftCardRetrieval(),
            on_retrieval_audit=audit_events.append, run_id="run-policy-014-test",
            trace_id="trace-policy-014-test")
    assert exc.value.code == "ANSWER_SEMANTIC_VALIDATION_FAILED"
    diagnostic = exc.value.diagnostic["answer_rejection_audit"]
    assert diagnostic["diagnostic_schema_version"] == "policy-answer-rejection-audit-v1"
    assert diagnostic["run_id"] == "run-policy-014-test"
    assert diagnostic["trace_id"] == "trace-policy-014-test"
    assert diagnostic["candidate_answer_present"] is True
    assert diagnostic["candidate_citation_ids"] == ["syn-policy-giftcard-b"]
    assert set(diagnostic["available_evidence_ids"]) == {row["chunk_id"] for row in rows}
    assert diagnostic["retrieval_links"][0]["call_id"] == audit_events[-1]["call_id"]
    assert diagnostic["retrieval_links"][0]["run_id"] == "run-policy-014-test"
    assert any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW"
        for row in diagnostic["validation_findings"])
    assert "[REDACTED_EMAIL]" in diagnostic["candidate_answer_redacted"]
    assert "[REDACTED_PHONE]" in diagnostic["candidate_answer_redacted"]
    assert "test@example.invalid" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "555-123-4567" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "reasoning_content" not in json.dumps(diagnostic, ensure_ascii=False)
    assert "MUST_NOT_BE_PERSISTED" not in json.dumps(diagnostic, ensure_ascii=False)


def test_rejection_audit_hashes_citations_outside_current_evidence_without_persisting_them():
    from app.m2.policy_execution import _answer_rejection_audit

    audit = _answer_rejection_audit(answer={"answer": "unsupported", "citations": ["foreign-source-id"]},
        evidence=[{"chunk_id": "current-policy-source"}], issue_codes=[], run_id="run-a",
        trace_id="trace-a", retrieval_audit_calls=[], failure_classification="CITATION_OUTSIDE_CURRENT_EVIDENCE")
    assert audit["candidate_citation_ids"] == []
    assert audit["available_evidence_ids"] == ["current-policy-source"]
    assert len(audit["rejected_citation_id_sha256"]) == 1
    assert "foreign-source-id" not in json.dumps(audit)


@pytest.mark.parametrize("case_id,archive_path", [
    ("D1-POLICY-014", "docs/eval/personal-dev-runs/aad2076931d243a6a3fcab570a939b7e.json"),
    ("D1-SAFETY-038", "docs/eval/personal-dev-runs/e7d2b4a2916f4197912590c1eb363c9e.json"),
])
def test_archived_full_policy_answer_runs_through_application_retrieval_and_validation(case_id, archive_path):
    """Replay full real answers through policy retrieval, citation filtering and semantic validation."""
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    archive = json.loads((root / archive_path).read_text(encoding="utf-8"))
    run = archive["result"]["observations"]["runs"][0]
    if run.get("result_json"):
        recorded = json.loads(run["result_json"])
        answer = recorded["answer"]
        citations = recorded.get("citations", [])
        citation_ids = {item if isinstance(item, str) else item.get("chunk_id") for item in citations}
    else:
        rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
        answer = rejection["candidate_answer_redacted"]
        citation_ids = set(rejection["candidate_citation_ids"])
    case = next(row for row in dataset["cases"] if row["case_id"] == case_id)
    rows = []
    for fixture in dataset["fixtures"]["policies"]:
        if fixture["evidence_id"] not in citation_ids:
            continue
        effective_from, effective_to = fixture["effective"].split("/", 1)
        rows.append({"chunk_id": fixture["evidence_id"], "title": fixture["evidence_id"],
            "content": fixture["text"], "version": fixture["version"],
            "effective_from": effective_from, "effective_to": effective_to,
            "knowledge_version": case["input"]["knowledge_version"],
            "index_fingerprint": "offline-r4-fixture-index", "rank": len(rows) + 1,
            "score": 1.0 - len(rows) * 0.1, "score_type": "bm25_raw"})
    assert citation_ids == {"syn-policy-giftcard-a", "syn-policy-giftcard-b"} if case_id == "D1-POLICY-014" else citation_ids == {"syn-policy-return-v1", "syn-policy-return-v2"}

    class ArchivedPolicyRetrieval:
        config = SimpleNamespace(mode="bm25", version="offline-r4", config_hash="offline-r4")

        def pin_view(self, evaluation_at=None):
            return RetrievalView({"knowledge_version": case["input"]["knowledge_version"], "chunks": []},
                "offline-r4-fixture-index", evaluation_at or "2026-10-25T12:00:00Z")

        def retrieve(self, _query, *, mode=None, view=None):
            return {"knowledge_version": case["input"]["knowledge_version"],
                "index_fingerprint": "offline-r4-fixture-index", "config_version": "offline-r4",
                "config_hash": "offline-r4", "mode": mode, "evidence": [dict(row) for row in rows],
                "channels": {"bm25": [row["chunk_id"] for row in rows]},
                "models": {"embedding": None, "reranker": None}, "diagnostics": {},
                "latency_ms": 1, "degraded": False, "failures": []}

    result = execute_policy_request(case["input"]["user_query"],
        route={"strategy": "SINGLE", "decision_source": "OFFLINE_REPLAY"},
        provider=ProviderStub(answer=answer), retrieval=ArchivedPolicyRetrieval(),
        run_id=f"offline-{case_id}", trace_id=f"offline-trace-{case_id}")
    assert result["answer"] == answer
    assert {row["chunk_id"] for row in result["citations"]} == citation_ids
    assert result["policy_retrieval"]["evidence_ids"] == sorted(citation_ids)
    assert result["policy_retrieval_audit"]
