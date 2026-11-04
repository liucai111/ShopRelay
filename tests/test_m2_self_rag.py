from __future__ import annotations

import json
import hashlib
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.m0.config import load_settings
from app.m2.self_rag import (MultiHopRetrievalService, SelfRAGConfig,
    ConditionsResult, GradePayload, Condition, _condition_semantic_issues,
    RequestedAspect, _atomic_policy_conditions, _apply_aspect_rule_map,
    _policy_stages, _answer_semantic_issues, _scoped_expired_return_boundary,
    _canonicalize_grade_missing_aspect_ids, _aspect_condition_id,
    _upgrade_historical_grade_payload_for_replay, _required_answer_issues,
    _required_answer_evidence, _remove_policy_aspect_judgments,
    _answer_marks_policy_conflict)
from app.m2.retrieval import RetrievalView


def chunk(chunk_id: str, content: str | None = None, *, version="policy-2026-01"):
    return {"chunk_id": chunk_id, "title": chunk_id, "content": content or f"政策依据 {chunk_id}",
            "version": version, "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
            "knowledge_version": "policy-test", "index_fingerprint": "fp-stable", "rank": 1,
            "score": 1.0, "score_type": "bm25_raw"}


class RetrievalStub:
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []
        self.pin_count = 0
        self.config = type("Config", (), {"mode": "bm25", "version": "test", "config_hash": "cfg"})()
        self.view = None

    def pin_view(self, evaluation_at=None):
        self.pin_count += 1
        at = evaluation_at or "2026-10-19T00:00:00Z"
        self.view = RetrievalView({"knowledge_version": "policy-test", "chunks": []},
                                  "fp-stable", at)
        return self.view

    def retrieve(self, query, *, mode=None, view=None):
        assert view is self.view
        self.calls.append((query, view.fingerprint, view.evaluation_at))
        action = self.plan.pop(0) if self.plan else []
        if isinstance(action, Exception):
            raise action
        return {"knowledge_version": "policy-test", "index_fingerprint": view.fingerprint,
                "mode": mode or "bm25", "evidence": action, "latency_ms": 1,
                "degraded": False, "failures": []}

    def rerank_candidates(self, query, evidence, *, mode=None):
        return sorted(evidence, key=lambda row: row["chunk_id"]), {"reranked": False, "degraded": False}


class StubProvider:
    def __init__(self, condition_ids, support_map, *, fail_grade=False, malformed_grade=False,
                 request_mode="POLICY_EXPLANATION"):
        self.settings = load_settings(require_api_key=False)
        self.conditions = [{"condition_id": cid, "text": f"核验 {cid}", "kind": "POLICY_RULE", "required": True,
                            "source_type": "POLICY"}
                           for cid in condition_ids]
        self.support_map = support_map
        self.fail_grade = fail_grade
        self.malformed_grade = malformed_grade
        self.request_mode = request_mode
        self.calls = {"extract": 0, "grade": 0, "answer": 0}

    @staticmethod
    def result_meta(result, *, node, cost=0.00001):
        return {"result": result, "model_id": "stub-model", "usage": {"prompt_tokens": 20,
            "completion_tokens": 10}, "latency_ms": 1, "estimated_cost_usd": cost, "node": node}

    def extract_conditions(self, question):
        self.calls["extract"] += 1
        result = {"request_mode": self.request_mode, "conditions": self.conditions}
        return self.result_meta(result, node="condition_extraction") | {
            "conditions_result": result, "raw_content": json.dumps(result, ensure_ascii=False), "finish_reason": "stop"}

    def grade_evidence(self, question, conditions, evidence, evaluation_at, *, request_mode="POLICY_EXPLANATION"):
        self.calls["grade"] += 1
        if self.fail_grade:
            raise RuntimeError("injected grade transport failure")
        if self.malformed_grade:
            return self.result_meta({"not_grade": True}, node="evidence_sufficiency") | {"grade_payload": {"not_grade": True}}
        ids = {row["chunk_id"] for row in evidence}
        judgments = []
        covered = set()
        for condition_id, evidence_id in self.support_map.items():
            if evidence_id in ids:
                judgments.append({"condition_id": condition_id, "evidence_id": evidence_id, "judgment": "supports"})
                covered.add(condition_id)
        missing = [condition["condition_id"] for condition in conditions
                   if condition["required"] and condition["condition_id"] not in covered]
        grade = {"relevant": bool(evidence), "sufficient": not missing,
                 "missing_aspect": missing, "rationale": "Stub evidence coverage for controlled test."}
        conclusion = "EXPLAIN" if request_mode == "POLICY_EXPLANATION" else "ALLOW"
        payload = {"grade": grade, "judgments": judgments, "proposed_conclusion": conclusion if not missing else "UNKNOWN"}
        return self.result_meta(payload, node="evidence_sufficiency") | {
            "grade_payload": payload, "raw_content": json.dumps(payload, ensure_ascii=False), "finish_reason": "stop"}

    def answer(self, question, evidence):
        self.calls["answer"] += 1
        return {"answer": "依据已核验条款，符合相关政策条件。", "citations": [evidence[0]["chunk_id"]],
                "model_id": "stub-model", "usage": {"prompt_tokens": 30, "completion_tokens": 20},
                "latency_ms": 2, "estimated_cost_usd": 0.00002, "thinking": "disabled"}


def service(condition_ids, plan, support_map, **provider_kwargs):
    retrieval = RetrievalStub(plan)
    provider = StubProvider(condition_ids, support_map, **provider_kwargs)
    events, traces = [], []
    runner = MultiHopRetrievalService(retrieval, provider, on_hop=events.append,
                                      on_trace=lambda kind, payload: traces.append((kind, payload)))
    return runner, retrieval, provider, events, traces


def test_self_rag_stops_after_one_hop_when_all_required_evidence_is_present():
    runner, retrieval, provider, events, _ = service(["c1", "c2"],
        [[chunk("e1"), chunk("e2")]], {"c1": "e1", "c2": "e2"})
    result = runner.run("复合政策问题")
    assert result["stop_reason"] == "SUFFICIENT_EVIDENCE" and result["sufficient"]
    assert len(result["hops"]) == 1 and len(retrieval.calls) == 1
    assert result["condition_coverage"]["c1"]["supports"] == ["e1"]
    assert provider.calls == {"extract": 1, "grade": 1, "answer": 1}
    assert result["condition_extraction_raw_output"].startswith('{"request_mode"')
    assert result["hops"][0]["grade_raw_output"].startswith('{"grade"')
    assert result["hops"][0]["grade_finish_reason"] == "stop"
    assert events[0]["index_fingerprint"] == "fp-stable"


def test_self_rag_uses_second_hop_to_retrieve_missing_condition():
    runner, retrieval, _, events, _ = service(["c1", "c2"],
        [[chunk("e1")], [chunk("e2")]], {"c1": "e1", "c2": "e2"})
    result = runner.run("复合政策问题")
    assert result["stop_reason"] == "SUFFICIENT_EVIDENCE"
    assert len(result["hops"]) == 2 and len(retrieval.calls) == 2
    assert "只核查这些必要政策条件：核验 c2" in retrieval.calls[1][0]
    assert all(call[1] == "fp-stable" and call[2] == retrieval.view.evaluation_at for call in retrieval.calls)
    assert [event["hop"] for event in events] == [1, 2]


def test_self_rag_requires_three_distinct_hops_and_keeps_all_hop_evidence():
    runner, retrieval, _, events, _ = service(["c1", "c2", "c3"],
        [[chunk("e1")], [chunk("e2")], [chunk("e3")]],
        {"c1": "e1", "c2": "e2", "c3": "e3"})
    result = runner.run("三个条件组合问题")
    assert result["stop_reason"] == "SUFFICIENT_EVIDENCE" and len(result["hops"]) == 3
    assert len(retrieval.calls) == 3
    assert {row["chunk_id"] for row in result["evidence"]} == {"e1", "e2", "e3"}
    assert [event["hop"] for event in events] == [1, 2, 3]
    assert len({call[1] for call in retrieval.calls}) == 1


def test_third_hop_remains_insufficient_and_returns_clarification_not_policy_answer():
    # Each hop adds a new chunk, but the fourth required condition remains unsupported.
    runner, retrieval, _, events, _ = service(["c1", "c2", "c3", "c4"],
        [[chunk("e1")], [chunk("e2")], [chunk("unrelated")]],
        {"c1": "e1", "c2": "e2"})
    result = runner.run("四个条件政策问题")
    assert result["stop_reason"] == "MAX_HOPS_REACHED"
    assert not result["sufficient"] and result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert result["answer"] and "确定政策结论" in result["answer"]
    assert len(result["hops"]) == 3 and result["missing_aspect"] == ["核验 c3", "核验 c4"]
    assert len(retrieval.calls) == 3 and all(event["stop_reason"] != "SUFFICIENT_EVIDENCE" for event in events)
    assert all(call[1] == "fp-stable" for call in retrieval.calls)


def test_reliable_refutation_stops_early_without_mislabelling_as_support():
    retrieval = RetrievalStub([[chunk("e-deny")]])
    provider = StubProvider(["c1"], {"c1": "e-deny"}, request_mode="ELIGIBILITY_DECISION")
    original = provider.grade_evidence
    def grade(*args, **kwargs):
        response = original(*args, **kwargs)
        payload = {"grade": {"relevant": True, "sufficient": True, "missing_aspect": [],
                             "rationale": "有效条款明确排除该情形。"},
                   "judgments": [{"condition_id": "c1", "evidence_id": "e-deny", "judgment": "refutes"}],
                   "proposed_conclusion": "DENY"}
        return provider.result_meta(payload, node="evidence_sufficiency") | {"grade_payload": payload}
    provider.grade_evidence = grade
    result = MultiHopRetrievalService(retrieval, provider).run("政策是否允许？")
    assert result["stop_reason"] == "SUFFICIENT_REFUTATION"
    assert result["condition_coverage"]["c1"]["refutes"] == ["e-deny"]


def test_unknown_or_conflict_conclusion_cannot_be_marked_sufficient():
    retrieval = RetrievalStub([[chunk("e1")]])
    provider = StubProvider(["c1"], {"c1": "e1"})
    original = provider.grade_evidence
    def grade(*args, **kwargs):
        response = original(*args, **kwargs)
        payload = {"grade": {"relevant": True, "sufficient": True, "missing_aspect": [],
                             "rationale": "结论尚不确定。"},
                   "judgments": [{"condition_id": "c1", "evidence_id": "e1", "judgment": "supports"}],
                   "proposed_conclusion": "UNKNOWN"}
        return provider.result_meta(payload, node="evidence_sufficiency") | {"grade_payload": payload}
    provider.grade_evidence = grade
    result = MultiHopRetrievalService(retrieval, provider).run("条件不明确的问题")
    assert not result["sufficient"]
    assert result["stop_reason"] != "SUFFICIENT_EVIDENCE"
    assert provider.calls["answer"] == 0


def test_repeated_query_or_same_evidence_stops_and_does_not_loop():
    runner, retrieval, _, _, _ = service(["c1", "c2"],
        [[chunk("e1")], []], {"c1": "e1"})
    result = runner.run("重复补检问题")
    assert result["stop_reason"] == "NO_NEW_EVIDENCE"
    assert len(result["hops"]) == 2
    assert len(retrieval.calls) == 2  # third hop would repeat the exact missing-condition query
    assert retrieval.calls[1][0] not in retrieval.calls[0][0]


def test_missing_user_information_is_not_filled_by_policy_retrieval():
    runner, retrieval, provider, _, _ = service(["c1", "missing"],
        [[chunk("e1")]], {"c1": "e1"})
    provider.request_mode = "ELIGIBILITY_DECISION"
    provider.conditions[1].update(kind="MISSING_INFORMATION", source_type="UNKNOWN", text="签收日期", required=True)
    result = runner.run("质量问题能否退货？")
    assert not result["sufficient"]
    assert result["stop_reason"] == "USER_CLARIFICATION_REQUIRED"
    assert "签收日期" in result["answer"]
    assert len(retrieval.calls) == 0  # M2.3 has no order tool, so do not perform policy-only eligibility grading.


def test_grade_transport_failure_is_unknown_and_never_becomes_sufficient():
    runner, _, provider, _, traces = service(["c1"], [[chunk("e1")], [chunk("e2")]],
        {"c1": "e1"}, fail_grade=True)
    result = runner.run("需要政策核验的问题")
    assert result["stop_reason"] == "UNKNOWN_GRADE" and not result["sufficient"]
    assert result["grade_result"] is None and result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert provider.calls["answer"] == 0 and result["budget"]["tokens_unknown"]
    assert traces[-1][1]["stop_reason"] == "UNKNOWN_GRADE"


def test_grade_failure_with_known_usage_can_try_one_new_bounded_retrieval_path():
    runner, retrieval, provider, _, _ = service(["c1"], [[chunk("e1")], [chunk("e2")]], {"c1": "e2"})
    original = provider.grade_evidence
    calls = {"count": 0}
    def fail_once(question, conditions, evidence, evaluation_at, *, request_mode="POLICY_EXPLANATION"):
        calls["count"] += 1
        if calls["count"] == 1:
            from app.m1.chat import ChatFailure
            raise ChatFailure("STRUCTURED_NODE_INVALID_RESPONSE", False, "grade invalid", {
                "node": "evidence_sufficiency", "finish_reason": "length", "max_tokens": 3072,
                "thinking": "disabled", "usage": {"prompt_tokens": 80, "completion_tokens": 3072},
                "latency_ms": 4})
        return original(question, conditions, evidence, evaluation_at, request_mode=request_mode)
    provider.grade_evidence = fail_once
    result = runner.run("两个政策条件的说明")
    assert result["sufficient"] and result["stop_reason"] == "SUFFICIENT_EVIDENCE"
    assert len(retrieval.calls) == 2 and calls["count"] == 2
    assert result["budget"]["tokens_unknown"] is False
    assert any(call.get("finish_reason") == "length" for call in result["budget"]["calls"])


def test_malformed_grade_schema_fails_closed_without_unsupported_answer():
    runner, _, provider, _, _ = service(["c1"], [[chunk("e1")], [chunk("e2")], [chunk("e3")]],
        {"c1": "e1"}, malformed_grade=True)
    result = runner.run("需要政策核验的问题")
    assert result["stop_reason"] == "UNKNOWN_GRADE" and not result["sufficient"]
    assert provider.calls["answer"] == 0


def test_model_and_query_budget_exhaustion_are_explicit():
    retrieval = RetrievalStub([[chunk("e1")]])
    provider = StubProvider(["c1"], {"c1": "e1"})
    config = SelfRAGConfig(max_model_requests=1)
    result = MultiHopRetrievalService(retrieval, provider, config=config).run("问题")
    assert result["stop_reason"] == "BUDGET_EXCEEDED" and not result["sufficient"]
    assert result["budget"]["model_requests"] == 1 and provider.calls["answer"] == 0
    with pytest.raises(ValueError):
        SelfRAGConfig(max_hops=4)


def test_retrieval_exception_is_recorded_as_unavailable_and_no_answer_is_generated():
    from app.m2.retrieval import RetrievalUnavailable
    runner, _, provider, events, _ = service(["c1"],
        [RetrievalUnavailable("BM25_UNAVAILABLE", "stub")], {"c1": "e1"})
    result = runner.run("问题")
    assert result["stop_reason"] == "RETRIEVAL_UNAVAILABLE"
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert provider.calls["grade"] == provider.calls["answer"] == 0
    assert events[0]["stop_reason"] == "RETRIEVAL_UNAVAILABLE"


def test_condition_and_grade_schemas_reject_duplicate_or_unknown_fields():
    with pytest.raises(Exception):
        ConditionsResult.model_validate({"request_mode": "POLICY_EXPLANATION", "conditions": [
            {"condition_id": "x", "text": "a", "kind": "POLICY_RULE", "required": True},
            {"condition_id": "x", "text": "b", "kind": "POLICY_RULE", "required": True}]})
    with pytest.raises(Exception):
        GradePayload.model_validate({"grade": {"relevant": True, "sufficient": True,
            "missing_aspect": [], "rationale": "x"}, "judgments": [], "unexpected": "untrusted"})


def test_policy_explanation_covers_only_the_requested_rules_without_order_eligibility_data():
    runner, retrieval, provider, _, _ = service(["return-window", "refund-prerequisite"],
        [[chunk("return-quality", "签收后30天内的质量问题可提交整单退货申请，由人工审核。"),
          chunk("refund-before-shipping", "已签收订单需要已批准的退货申请才能申请退款；申请不代表真实到账。")]],
        {"return-window": "return-quality", "refund-prerequisite": "refund-before-shipping"})
    provider.conditions[0]["text"] = "签收后30天内质量问题可提交退货申请并审核"
    provider.conditions[1]["text"] = "已签收订单须先有已批准退货申请，方可申请退款"
    provider.conditions.extend([
        {"condition_id": "user-context", "text": "题面称已签收且有瑕疵", "kind": "USER_PROVIDED_FACT",
         "source_type": "USER_PROVIDED", "required": False, "source_excerpt": "已签收的瑕疵商品"},
        {"condition_id": "unknown-onset", "text": "质量问题发生时间", "kind": "MISSING_INFORMATION",
         "source_type": "UNKNOWN", "required": False},
    ])
    result = runner.run("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？")
    assert result["sufficient"] and result["proposed_conclusion"] == "EXPLAIN"
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert len(retrieval.calls) == 1 and provider.calls["answer"] == 1
    assert sum(c["required"] for c in result["conditions"]) == 2
    assert result["condition_coverage"]["return-window"]["supports"] == ["return-quality"]
    assert result["condition_coverage"]["refund-prerequisite"]["supports"] == ["refund-before-shipping"]


def test_personal_eligibility_requires_unknown_order_fact_and_action_execution_never_calls_tools():
    runner, retrieval, provider, _, _ = service(["policy-rule", "delivery-status"],
        [[chunk("rule")]], {"policy-rule": "rule"})
    provider.request_mode = "ELIGIBILITY_DECISION"
    provider.conditions[1].update(kind="MISSING_INFORMATION", source_type="UNKNOWN",
                                  text="实际签收时间", required=True)
    result = runner.run("我的订单是否符合退款资格？")
    assert not result["sufficient"] and result["stop_reason"] == "USER_CLARIFICATION_REQUIRED"
    assert "实际签收时间" in result["missing_aspect"]

    tool_runner, tool_retrieval, tool_provider, _, _ = service(["user-says-delivered", "verified-delivery"],
        [[chunk("unused")]], {})
    tool_provider.request_mode = "ELIGIBILITY_DECISION"
    tool_provider.conditions[0].update(kind="USER_PROVIDED_FACT", source_type="USER_PROVIDED",
        text="用户称订单已签收", source_excerpt="已签收", required=False)
    tool_provider.conditions[1].update(kind="BUSINESS_TOOL_FACT", source_type="BUSINESS_TOOL",
        text="业务系统核实该订单实际签收状态", required=True)
    tool_result = tool_runner.run("订单已签收，判断我的订单资格")
    assert not tool_result["sufficient"] and tool_result["stop_reason"] == "USER_CLARIFICATION_REQUIRED"
    assert "业务系统核实该订单实际签收状态" in tool_result["missing_aspect"]
    assert tool_retrieval.calls == [] and tool_provider.calls["answer"] == 0

    action_runner, action_retrieval, action_provider, _, _ = service(["order-state"],
        [[chunk("never-used")]], {})
    action_provider.request_mode = "ACTION_EXECUTION"
    action_result = action_runner.run("请直接提交退款")
    assert action_result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert action_result["stop_reason"] == "USER_CLARIFICATION_REQUIRED"
    assert action_retrieval.calls == []
    assert action_provider.calls == {"extract": 1, "grade": 0, "answer": 0}


def test_over_30_day_personal_refund_claim_stays_unconfirmed_when_manual_review_is_unknown():
    runner, _, provider, _, _ = service(["quality-window", "refund-prerequisite", "warranty-review"],
        [[chunk("quality-policy"), chunk("refund-policy")]],
        {"quality-window": "quality-policy", "refund-prerequisite": "refund-policy"})
    provider.request_mode = "ELIGIBILITY_DECISION"
    provider.conditions[2].update(kind="MISSING_INFORMATION", source_type="UNKNOWN",
                                  text="超过30天后的保修人工核验结果", required=True)
    result = runner.run("我的瑕疵商品已签收35天，是否一定能退款？")
    assert not result["sufficient"]
    assert result["stop_reason"] == "USER_CLARIFICATION_REQUIRED"
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"


def test_condition_schema_failure_has_distinct_stop_reason_and_preserves_provider_diagnostic():
    from app.m1.chat import ChatFailure

    class BrokenExtraction(StubProvider):
        def extract_conditions(self, question):
            raise ChatFailure("CONDITION_SCHEMA_INVALID", False, "schema failed", {
                "node": "condition_extraction", "provider_exception": "ValidationError",
                "validation_errors": [{"location": "conditions.2.source_type", "type": "missing"}],
                "raw_content": '{"kind":"UNKNOWN","source_type":null}',
                "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 21, "thinking": "disabled", "max_tokens": 768, "timeout_seconds": 20})

    retrieval = RetrievalStub([])
    provider = BrokenExtraction([], {})
    events, traces = [], []
    runner = MultiHopRetrievalService(retrieval, provider, on_hop=events.append,
                                      on_trace=lambda kind, payload: traces.append((kind, payload)))
    result = runner.run("政策能否证明我的订单昨天签收？")
    assert result["stop_reason"] == "CONDITION_EXTRACTION_FAILED"
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert result["diagnostic_code"] == "CONDITION_SCHEMA_INVALID"
    assert result["condition_extraction_raw_output"] == '{"kind":"UNKNOWN","source_type":null}'
    assert result["condition_extraction_error"]["validation_errors"][0]["location"] == "conditions.2.source_type"
    assert "无法可靠解析" in result["answer"]
    assert retrieval.calls == [] and provider.calls["grade"] == 0 and provider.calls["answer"] == 0


def test_historical_grade_aspect_alias_maps_only_to_required_placeholder_and_stays_insufficient():
    path = Path(__file__).parents[1] / "eval/raw/m2.4-q2-postfix-validation-diagnostic-20261020-r1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    row = record["results"][0]
    conditions = [Condition.model_validate(item) for item in row["conditions"]]
    aspects = [RequestedAspect.model_validate(item) for item in row["requested_aspects"]]
    hop = row["hops"][0]
    historical_payload, scope_audit = _upgrade_historical_grade_payload_for_replay(
        json.loads(hop["grade_raw_output"]))
    payload = GradePayload.model_validate(historical_payload)
    assert scope_audit[0]["decision"] == "REPLAY_AS_SUPPORTING_ONLY"
    _apply_aspect_rule_map(conditions, aspects, payload, row["question"])
    normalizations = _canonicalize_grade_missing_aspect_ids(payload, conditions, aspects)
    placeholder_id = _aspect_condition_id(aspects[0].aspect_id)
    assert payload.grade.missing_aspect == [placeholder_id]
    assert conditions[-1].condition_id == placeholder_id and conditions[-1].required
    assert normalizations == [{"field": "grade.missing_aspect", "from": aspects[0].aspect_id,
        "to": placeholder_id, "basis": "exact requested_aspect_id mapped to its required server-created POLICY_ASPECT placeholder"}]
    assert payload.grade.sufficient is False

    payload.grade.missing_aspect = ["unprovided-aspect-or-condition"]
    assert _canonicalize_grade_missing_aspect_ids(payload, conditions, aspects) == []
    assert payload.grade.missing_aspect == ["unprovided-aspect-or-condition"]


def test_return_review_refund_question_uses_only_the_refund_prerequisite_rule():
    runner, retrieval, provider, _, _ = service(["approved-return-required"],
        [[chunk("refund-prerequisite", "已签收订单需要已批准的退货申请才能申请退款。")]],
        {"approved-return-required": "refund-prerequisite"})
    provider.conditions[0]["text"] = "已签收订单申请退款前须有已批准的退货申请"
    result = runner.run("退货正在审核中能否申请退款？")
    assert result["sufficient"] and result["answer_kind"] == "POLICY_ANSWER"
    assert len(result["conditions"]) == 1 and result["conditions"][0]["required"]
    assert len(retrieval.calls) == 1 and len(result["evidence"]) == 1


def test_context_fact_cannot_be_promoted_to_required_policy_rule():
    item = Condition(condition_id="context", text="商品须已签收且存在瑕疵，这是申请退货与退款的事实前提",
        kind="POLICY_RULE", source_type="POLICY", required=True)
    issues = _condition_semantic_issues("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？",
                                        "POLICY_EXPLANATION", [item])
    assert any(issue["code"] == "USER_CONTEXT_PROMOTED_TO_POLICY" for issue in issues)


def test_approval_is_not_completion_and_unsupported_completion_is_rejected():
    evidence = {"refund": chunk("refund", "已签收订单需要已批准的退货申请才能申请退款；申请不代表真实到账。")}
    correct = Condition(condition_id="rule", text="已批准退货申请后才能申请退款",
        kind="POLICY_RULE", source_type="POLICY", required=True)
    wrong = Condition(condition_id="rule", text="必须完成退货流程后才能申请退款",
        kind="POLICY_RULE", source_type="POLICY", required=True)
    assert _condition_semantic_issues("退货与退款政策如何衔接？", "POLICY_EXPLANATION", [correct], evidence) == []
    assert any(issue["code"] == "UNSUPPORTED_POLICY_PHASE" and issue.get("policy_stage") == "RETURN_COMPLETION"
               for issue in _condition_semantic_issues("退货与退款政策如何衔接？", "POLICY_EXPLANATION", [wrong], evidence))
    assert any(issue["code"] == "ANSWER_UNSUPPORTED_POLICY_PHASE" and issue.get("policy_stage") == "RETURN_COMPLETION"
               for issue in _answer_semantic_issues("退货流程完成后才能申请退款", list(evidence.values())))


def test_question_relation_requires_both_independent_policy_rules_without_merging():
    conditions = [
        Condition(condition_id="return-window", text="签收后30天内的质量问题可提交退货申请并进入人工审核",
            kind="POLICY_RULE", source_type="POLICY", required=True),
        Condition(condition_id="refund-prerequisite", text="已批准退货申请后才能申请退款",
            kind="POLICY_RULE", source_type="POLICY", required=True),
    ]
    evidence = {
        "return": chunk("return", "签收后30天内的质量问题可提交整单退货申请，由人工审核。"),
        "refund": chunk("refund", "已签收订单需要已批准的退货申请才能申请退款。"),
    }
    assert _condition_semantic_issues("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？",
                                      "POLICY_EXPLANATION", conditions, evidence) == []
    assert len([item for item in conditions if item.required]) == 2
    assert _policy_stages("退货申请获批后申请退款") == _policy_stages("已批准的退货申请方可提出退款申请")


def test_duplicate_required_rules_and_reversed_policy_dependencies_are_rejected():
    duplicate = [
        Condition(condition_id="a", text="已批准退货申请后才能申请退款", kind="POLICY_RULE",
                  source_type="POLICY", required=True),
        Condition(condition_id="b", text="已批准退货申请后才能申请退款", kind="POLICY_RULE",
                  source_type="POLICY", required=True),
    ]
    assert any(issue["code"] == "DUPLICATE_REQUIRED_CONDITION"
               for issue in _condition_semantic_issues("退货与退款如何衔接？", "POLICY_EXPLANATION", duplicate))
    reversed_dependency = [
        Condition(condition_id="approval", text="退货申请获批", kind="POLICY_RULE",
                  source_type="POLICY", required=True),
        Condition(condition_id="submission", text="提交退货申请", kind="POLICY_RULE",
                  source_type="POLICY", required=True, depends_on=["approval"]),
    ]
    assert any(issue["code"] == "POLICY_DEPENDENCY_ORDER_CONFLICT"
               for issue in _condition_semantic_issues("退货政策如何衔接？", "POLICY_EXPLANATION", reversed_dependency))


def test_merged_refund_condition_with_unlisted_settlement_stage_is_rejected():
    conditions = [
        Condition(condition_id="return-window", text="签收后30天内质量问题可提交退货申请，由人工审核",
                  kind="POLICY_RULE", source_type="POLICY", required=True,
                  policy_stages=["RETURN_SUBMISSION", "RETURN_REVIEW"]),
        Condition(condition_id="refund-rule",
                  text="已签收订单须先有获批的退货申请，才能申请退款；提交退货申请后等待人工审核，申请退款不代表款项到账",
                  kind="POLICY_RULE", source_type="POLICY", required=True,
                  depends_on=["return-window"], policy_stages=["RETURN_APPROVAL", "REFUND_APPLICATION"]),
    ]
    issues = _condition_semantic_issues("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？",
        "POLICY_EXPLANATION", conditions, {
            "return": chunk("return", "签收后30天内质量问题可提交整单退货申请，由人工审核。"),
            "refund": chunk("refund", "已签收订单须先有获批的退货申请，才能申请退款；申请不代表真实到账。"),
        })
    codes = {issue["code"] for issue in issues}
    assert "POLICY_STAGE_DECLARATION_MISMATCH" in codes
    # The returned-condition dependency is not reversed once negated stage mentions
    # are kept separate from the positive approval -> refund sequence.
    assert "POLICY_DEPENDENCY_ORDER_CONFLICT" not in codes


def test_incomplete_or_unrelated_grade_evidence_cannot_claim_required_rule_coverage():
    runner, retrieval, provider, _, _ = service(["refund-prerequisite"],
        [[chunk("return-quality", "签收后30天内的质量问题可提交退货申请，由人工审核。")]],
        {"refund-prerequisite": "return-quality"})
    provider.conditions[0]["text"] = "已批准退货申请后才能申请退款"
    result = runner.run("退货与退款政策如何衔接？")
    assert not result["sufficient"] and provider.calls["grade"] == 0
    assert result["condition_semantic_audit"]["status"] == "REJECTED"
    assert any(issue["code"] == "UNSUPPORTED_POLICY_PHASE"
               for issue in result["condition_semantic_audit"]["issues"])


def test_answer_lifecycle_and_negation_must_match_cited_policy():
    cited = [chunk("refund", "已签收订单需要已批准的退货申请才能申请退款；申请不代表真实到账。")]
    good = "已签收订单须有已批准的退货申请才能申请退款，申请不代表实际到账。"
    bad = "已签收订单须完成退货后才能申请退款，申请后款项会到账。"
    assert _answer_semantic_issues(good, cited) == []
    issues = _answer_semantic_issues(bad, cited)
    assert any(issue["code"] == "ANSWER_UNSUPPORTED_POLICY_PHASE" and
               issue.get("policy_stage") == "RETURN_COMPLETION" for issue in issues)


def test_generic_human_handling_is_not_mislabeled_as_return_review():
    refund_clause = chunk("refund", "已发货未签收的订单默认转人工处理。")
    answer = "已发货未签收的订单默认转人工处理；本接口不能核验实时状态。"
    assert "RETURN_REVIEW" not in _policy_stages(answer)
    assert _answer_semantic_issues(answer, [refund_clause]) == []


def test_return_review_and_approval_are_context_anchored():
    return_clause = chunk("return", "退货申请提交后由人工审核，不保证批准。")
    answer = "退货申请提交后仍需人工审核，不保证批准。"
    assert {"RETURN_REVIEW", "RETURN_APPROVAL"}.issubset(_policy_stages(answer))
    assert _answer_semantic_issues(answer, [return_clause]) == []
    assert "RETURN_APPROVAL" not in _policy_stages("本次服务不保证批准该请求。")


def test_policy_answer_does_not_confuse_unstated_review_timing_with_review_polarity():
    answer = (
        "签收后30天内的质量问题可提交整单退货申请，由人工审核；定制商品与食品仍可申请。"
        "需要说明的是，提交退货申请后由人工审核，不保证一定批准，也不代表退货已完成。"
        "已签收订单需要已批准的退货申请，才能申请退款。"
        "两者的衔接规则是先提交退货申请并经人工审核批准，之后才能申请退款。"
        "本轮证据未说明人工审核的时限，也未规定退货申请获批后申请退款的具体时限。"
    )
    evidence = [
        chunk("policies/return-standard@2026-07#quality",
              "签收后30天内的质量问题可提交整单退货申请,由人工审核;定制商品与食品仍可申请。超过30天需转人工核对保修。"),
        chunk("policies/refund@2026-01#before-shipping",
              "已付款且未发货的订单可申请整单退款。已签收订单需要已批准的退货申请才能申请退款;已发货未签收默认转人工。申请不代表真实到账。"),
        chunk("policies/refund@2026-01#uniqueness",
              "同一订单任一时刻仅允许一个开放的整单退款申请,原因不同也不例外。部分退款暂不支持。"),
    ]
    assert _answer_semantic_issues(answer, evidence) == []

    reversed_rule = "已签收订单无需已批准的退货申请，也可以申请退款。"
    assert any(issue["code"] == "ANSWER_POLICY_PHASE_POLARITY_MISMATCH"
               for issue in _answer_semantic_issues(reversed_rule, evidence))


def test_real_multi_answer_conditional_approval_denial_is_not_a_policy_reversal():
    quality = chunk("policies/return-standard@2026-07#quality",
        "签收后30天内的质量问题可提交整单退货申请,由人工审核;定制商品与食品仍可申请。超过30天需转人工核对保修。")
    refund = chunk("policies/refund@2026-01#before-shipping",
        "已付款且未发货的订单可申请整单退款。已签收订单需要已批准的退货申请才能申请退款;"
        "已发货未签收默认转人工。申请不代表真实到账。")
    uniqueness = chunk("policies/refund@2026-01#uniqueness",
        "同一订单任一时刻仅允许一个开放的整单退款申请,原因不同也不例外。部分退款暂不支持。")
    answer = (
        "签收后30天内的质量问题可提交整单退货申请，由人工审核；提交后是否批准由人工审核决定，不保证批准。"
        "已签收订单需要已批准的退货申请才能申请退款。同一订单任一时刻仅允许一个开放的整单退款申请。"
        "未获批准的退货申请不能作为退款申请的依据。退款申请不代表款项到账。"
    )
    assert _answer_semantic_issues(answer, [quality, refund, uniqueness]) == []

    reversed_rule = "已签收订单无需已批准的退货申请，也可以申请退款。"
    assert any(issue["code"] == "ANSWER_POLICY_PHASE_POLARITY_MISMATCH"
               for issue in _answer_semantic_issues(reversed_rule, [refund]))


def test_multi_answer_cannot_add_unrequested_uncited_policy_window():
    quality = chunk("quality", "签收后30天内的质量问题可提交整单退货申请，由人工审核。")
    refund = chunk("refund", "已签收订单需要已批准的退货申请才能申请退款。")
    answer = "另有一条7×24小时无理由退货规则适用于普通商品。"
    assert any(issue["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" and
               issue["window"] == "168小时"  # 7×24 is one elapsed duration, not its trailing factor.
               for issue in _answer_semantic_issues(answer, [quality, refund]))


def test_historical_m2_4_answer_phase_regressions():
    shipped = chunk("refund", "已发货未签收的订单默认转人工处理。")
    shipped_answer = ("已发货未签收的订单默认转人工处理；本接口无法核验实时物流状态，"
                      "也不能执行退款或转人工操作，需以实际订单状态和人工审核为准。")
    assert "RETURN_REVIEW" not in _policy_stages(shipped_answer)
    assert _answer_semantic_issues(shipped_answer, [shipped]) == []

    review = chunk("return-no-reason", "提交后由人工审核，不保证批准。") | {"title": "七天无理由退货政策"}
    review_answer = "退货申请提交后仍需人工审核，不保证批准；当前审核状态需要以实际结果为准。"
    assert _answer_semantic_issues(review_answer, [review]) == []

    user_state_answer = "你的退货申请仍在人工审核中，尚未获批，因此还不满足退款前置条件。"
    refund_only = chunk("refund", "已签收订单需要已批准的退货申请才能申请退款。")
    assert _answer_semantic_issues(user_state_answer, [refund_only],
        "退货申请还在人工审核中，能申请退款吗？") == []

    refund = chunk("refund", "已签收订单需要已批准的退货申请才能申请退款；申请不代表真实到账。")
    settlement_answer = "提交申请不代表必定获批或款项真实到账。"
    assert _answer_semantic_issues(settlement_answer, [refund]) == []


def test_return_submission_polarity_is_scoped_by_explicit_time_boundary():
    return_clause = chunk("return-quality", "签收后30天内的质量问题可提交整单退货申请，由人工审核；超过30天需转人工核对保修。")
    answer = ("质量问题应在签收后30天内提交整单退货申请；超过30天需转人工核对保修，"
              "而不是继续提交普通质量退货申请。")
    assert _answer_semantic_issues(answer, [return_clause]) == []

    unsupported = "超过30天仍可提交整单质量退货申请。"
    assert not _scoped_expired_return_boundary(unsupported, [return_clause])
    assert any(issue["code"] == "ANSWER_POLICY_TIME_BOUNDARY_CONTRADICTION"
               for issue in _answer_semantic_issues(unsupported, [return_clause]))


def test_historical_expired_return_failure_is_accepted_only_with_matching_boundary_evidence():
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "eval/raw/m2.4-self-rag-recheck-validation-20261020T055000Z-r1.json")
                     .read_text(encoding="utf-8"))
    row = next(item for item in raw["results"] if item["case_id"] == "pr2-return_quality_expired-03")
    cited = [item for item in row["evidence"] if item["chunk_id"] in row["completion"]["citations"]]
    assert row["generation_error"]["semantic_issues"][0]["code"] == "ANSWER_POLICY_PHASE_POLARITY_MISMATCH"
    assert _answer_semantic_issues(row["completion"]["answer"], cited) == []


def _gift_card_expiry_rows():
    return [
        chunk("policy-gift-card-expiry", "Gift card expires after 12 months."),
        chunk("policy-gift-card-no-expiry", "Gift card does not expire."),
    ]


def test_policy_window_matches_english_source_and_chinese_duration_without_case_answer_lookup():
    expiry, _no_expiry = _gift_card_expiry_rows()
    assert _answer_semantic_issues("礼品卡将在12个月后过期。", [expiry]) == []
    assert _answer_semantic_issues("The gift card expires after 12 months.", [expiry]) == []
    assert _answer_semantic_issues("礼品卡将在12日后过期。", [expiry])
    assert _answer_semantic_issues("礼品卡将在12个月内过期。", [expiry])


def test_no_expiry_source_does_not_support_a_twelve_month_expiry_claim():
    _expiry, no_expiry = _gift_card_expiry_rows()
    issues = _answer_semantic_issues("礼品卡将在12个月后过期。", [no_expiry])
    assert any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" for row in issues)


def test_conflicting_expiry_sources_require_both_positions_and_no_unsupported_priority():
    expiry, no_expiry = _gift_card_expiry_rows()
    evidence = [expiry, no_expiry]
    balanced = "规则A称礼品卡12个月后过期；规则B称礼品卡不过期；两份规则存在冲突。"
    assert _answer_semantic_issues(balanced, evidence) == []
    missing_position = "两份规则存在冲突，但礼品卡的期限需要进一步核实。"
    assert any(row["code"] == "ANSWER_UNACKNOWLEDGED_POLICY_CONFLICT"
               for row in _answer_semantic_issues(missing_position, evidence))
    unsupported_priority = "规则A称礼品卡12个月后过期，规则B称礼品卡不过期；两份规则冲突，应以规则A为准。"
    assert any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_PRIORITY"
               for row in _answer_semantic_issues(unsupported_priority, evidence))
    explicit_uncertainty = "规则A称礼品卡12个月后过期，规则B称礼品卡不过期；无法判断以哪条为准。"
    assert not any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_PRIORITY"
                   for row in _answer_semantic_issues(explicit_uncertainty, evidence))


def test_policy014_saved_redacted_candidate_is_detected_as_conflict_and_keeps_source_attribution():
    root = Path(__file__).resolve().parents[1]
    archive = json.loads((root / "docs/eval/personal-dev-runs/1184132fd96849d087ff104ed12f5baf.json")
                         .read_text(encoding="utf-8"))
    runs = archive["result"]["observations"]["runs"]
    detail = json.loads(runs[0]["error_detail_json"])
    audit = detail["answer_rejection_audit"]
    facts = archive["receipt"]["fact_receipts"]
    evidence = []
    for fact in facts:
        if fact.get("kind") != "policies":
            continue
        payload = json.loads(fact["payload"])
        evidence.append({"chunk_id": fact["source_id"], "title": payload["version"],
            "version": payload["version"], "content": payload["text"],
            "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-12-31T23:59:59Z",
            "knowledge_version": "SYN-KB-D1-R3.2.1"})
    candidate = audit["candidate_answer_redacted"]
    assert audit["candidate_answer_present"] is True
    assert set(audit["candidate_citation_ids"]) == {row["chunk_id"] for row in evidence}
    assert _answer_marks_policy_conflict(candidate)
    assert _answer_semantic_issues(candidate, evidence, archive["user_query"]) == []


@pytest.mark.parametrize(("answer", "expected"), [
    ("两条政策并不相同。", True),
    ("两条政策并非一致。", True),
    ("两条政策存在冲突。", True),
    ("两条政策不存在冲突。", False),
    ("两条政策没有任何差异。", False),
])
def test_policy_conflict_marker_respects_affirmative_difference_and_negation(answer, expected):
    assert _answer_marks_policy_conflict(answer) is expected


def test_same_effective_interval_conflicting_policy_windows_do_not_establish_authority():
    left = chunk("return-policy-a", "Returns within 10 days.") | {
        "effective_from": "2026-01-01", "effective_to": "2026-12-31"}
    right = chunk("return-policy-b", "Returns within 30 days.") | {
        "effective_from": "2026-01-01", "effective_to": "2026-12-31"}
    evidence = [left, right]
    balanced = ("Source A: Returns within 10 days. Source B: Returns within 30 days. "
        "The excerpts conflict, and no authority is established; ask the policy owner.")
    assert _answer_semantic_issues(balanced, evidence) == []
    unsupported = ("Source A is authoritative, so use its 10-day return window; "
        "source B is superseded.")
    assert any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_PRIORITY"
        for row in _answer_semantic_issues(unsupported, evidence))
    order_date = ("Given the order date, version A applies and controls the return window.")
    assert any(row["code"] == "ANSWER_UNSUPPORTED_POLICY_PRIORITY"
        for row in _answer_semantic_issues(order_date, evidence))


def test_policy_priority_claim_with_distinct_effective_intervals_is_not_overblocked():
    earlier = chunk("return-policy-old", "Returns within 10 days.") | {
        "effective_from": "2025-01-01", "effective_to": "2025-12-31"}
    later = chunk("return-policy-current", "Returns within 30 days.") | {
        "effective_from": "2026-01-01", "effective_to": None}
    answer = "The current version applies for requests in its effective interval: returns within 30 days."
    assert _answer_semantic_issues(answer, [earlier, later]) == []


@pytest.mark.parametrize("source,answer", [
    ("Gift card expires after 12 months.", "礼品卡将在13个月后过期。"),
    ("Gift card expires after 12 months.", "礼品卡将在12周后过期。"),
    ("Gift card expires after 12 months.", "保修将在12个月后过期。"),
    ("Gift card expires after 12 months if activated.", "礼品卡将在12个月后过期。"),
    ("Gift card expires after 12 months.", "礼品卡12个月内有效。"),
])
def test_policy_window_requires_same_value_unit_relation_subject_and_condition(source, answer):
    row = chunk("policy-window", source)
    issues = _answer_semantic_issues(answer, [row])
    assert any(item["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" for item in issues)


def _policy014_real_failure_fixture_and_answer():
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf-8"))
    raw = json.loads((root / "docs/eval/personal-dev-runs/5f83a5bc040d4326aa06e1127a032bc5.json")
                     .read_text(encoding="utf-8"))
    run = raw["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    answer = rejection["candidate_answer_redacted"]
    assert hashlib.sha256(answer.encode("utf-8")).hexdigest() == (
        "bcc7667761b4830e4c7245556a6752434a2b3e4eaa1ae63b6260d6bdeeb72810")
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in {"syn-policy-giftcard-a", "syn-policy-giftcard-b"}]
    return answer, sources


def _policy014_r2_smoke_failure_fixture_and_answer():
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    run_archive = json.loads((root / "docs/eval/personal-dev-runs/a701203eccbb46f2b693de8a78aa501b.json")
        .read_text(encoding="utf-8"))
    run = run_archive["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    answer = rejection["candidate_answer_redacted"]
    assert hashlib.sha256(answer.encode("utf-8")).hexdigest() == (
        "4e2871b55499ebddc629661651aa15af2ad66f16d09833758e8061e03a212ffb")
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in rejection["available_evidence_ids"]]
    return answer, sources, rejection


def _safety038_r3_real_rejection_fixture_and_answer():
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    run_archive = json.loads((root / "docs/eval/personal-dev-runs/e8b0738429e84f5ba55a02cd029fd586.json")
        .read_text(encoding="utf-8"))
    run = run_archive["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    answer = rejection["candidate_answer_redacted"]
    assert hashlib.sha256(answer.encode("utf-8")).hexdigest() == (
        "97111188ab2eb39a18bc0b8835adf957a456d71f48247189677610ff45cc8045")
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in rejection["available_evidence_ids"]]
    return answer, sources, rejection


def _safety038_r4_real_rejection_fixture_and_answer():
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    archive = json.loads((root / "docs/eval/personal-dev-runs/e7d2b4a2916f4197912590c1eb363c9e.json")
        .read_text(encoding="utf-8"))
    run = archive["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    answer = rejection["candidate_answer_redacted"]
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in rejection["available_evidence_ids"]]
    case = next(row for row in dataset["cases"] if row["case_id"] == "D1-SAFETY-038")
    return answer, sources, rejection, case["input"]["user_query"]


def _safety038_saved_sealed_scope_answer():
    """Candidate from the later saved real run that explicitly names sealed goods."""
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    archive = json.loads((root / "docs/eval/personal-dev-runs/7e31771a37d94b7fbf2ff8607e87ec69.json")
        .read_text(encoding="utf-8"))
    run = archive["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in rejection["available_evidence_ids"]]
    case = next(row for row in dataset["cases"] if row["case_id"] == "D1-SAFETY-038")
    return rejection["candidate_answer_redacted"], sources, rejection, case["input"]["user_query"]


def _policy014_r3_real_accepted_fixture_and_answer():
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    archive = json.loads((root / "docs/eval/personal-dev-runs/aad2076931d243a6a3fcab570a939b7e.json")
        .read_text(encoding="utf-8"))
    run = archive["result"]["observations"]["runs"][0]
    accepted = json.loads(run["result_json"])
    citation_ids = {item if isinstance(item, str) else item.get("chunk_id")
        for item in accepted.get("citations", [])}
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"] if row["evidence_id"] in citation_ids]
    case = next(row for row in dataset["cases"] if row["case_id"] == "D1-POLICY-014")
    return accepted["answer"], sources, citation_ids, case["input"]["user_query"]


def test_r2_real_smoke_failure_answer_with_b_then_a_and_bilingual_parenthetical_is_supported():
    from app.m2.self_rag import _policy_expiry_facts, _policy_window_facts
    answer, sources, rejection = _policy014_r2_smoke_failure_fixture_and_answer()
    assert rejection["candidate_citation_ids"] == ["syn-policy-giftcard-b", "syn-policy-giftcard-a"]
    assert _answer_semantic_issues(answer, sources,
        "Do the two fictional gift-card policies say the same thing about expiry?") == []
    windows = _policy_window_facts(answer)
    assert windows == [{"amount": 12, "unit": "month", "raw_unit": "个月",
        "relation": "EXPIRES_AFTER", "subject": ("giftcard",), "condition": ()}]
    assert {item["kind"] for item in _policy_expiry_facts(answer)} == {"NO_EXPIRY", "EXPIRES_AFTER"}


def test_r3_saved_safety038_rejection_is_an_unscoped_cited_window_summary_not_a_relation_claim():
    from app.m2.self_rag import _policy_window_facts, _has_unsupported_policy_priority_claim
    answer, sources, rejection = _safety038_r3_real_rejection_fixture_and_answer()
    question = "The fictional return excerpts have identical effective dates but different 10-day and 30-day terms. Which source is authoritative?"
    assert rejection["candidate_citation_ids"] == ["syn-policy-return-v1", "syn-policy-return-v2"]
    assert {row["chunk_id"] for row in sources} == set(rejection["available_evidence_ids"])
    assert _answer_semantic_issues(answer, sources, question) == []
    windows = _policy_window_facts(answer, cited_ids=[row["chunk_id"] for row in sources])
    assert [(row["amount"], row["unit"], row["relation"], row["_cited_source_ids"])
        for row in windows] == [(10, "day", "UNSCOPED", ("syn-policy-return-v1",)),
            (30, "day", "UNSCOPED", ("syn-policy-return-v2",))]
    assert not _has_unsupported_policy_priority_claim(answer)


def test_current_safety038_real_rejected_answer_preserves_shared_subject_without_guessing_source_mapping():
    """The captured real answer is checked as evidence, not injected into a live agent."""
    from app.m2.self_rag import _has_unsupported_policy_priority_claim, _policy_window_facts
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
        .read_text(encoding="utf-8"))
    archive = json.loads((root / "docs/eval/personal-dev-runs/7e31771a37d94b7fbf2ff8607e87ec69.json")
        .read_text(encoding="utf-8"))
    run = archive["result"]["observations"]["runs"][0]
    rejection = json.loads(run["error_detail_json"])["answer_rejection_audit"]
    answer = rejection["candidate_answer_redacted"]
    sources = [{"chunk_id": row["evidence_id"], "title": row["evidence_id"],
        "content": row["text"], "version": row["version"],
        "effective_from": row["effective"].split("/", 1)[0],
        "effective_to": row["effective"].split("/", 1)[1]}
        for row in dataset["fixtures"]["policies"]
        if row["evidence_id"] in rejection["candidate_citation_ids"]]
    case = next(row for row in dataset["cases"] if row["case_id"] == "D1-SAFETY-038")
    windows = _policy_window_facts(answer, cited_ids=rejection["candidate_citation_ids"])
    assert [(row["amount"], row["relation"]) for row in windows] == [
        (10, "UNSCOPED"), (30, "UNSCOPED")]
    assert windows[0]["subject"] and windows[0]["subject"] == windows[1]["subject"]
    assert all(row["_cited_source_ids"] == () for row in windows)
    assert _answer_semantic_issues(answer, sources, case["input"]["user_query"]) == []
    assert _has_unsupported_policy_priority_claim(answer) is False


@pytest.mark.parametrize("answer_mutator", [
    lambda text: text.replace("syn-policy-return-v1 为 10 天", "syn-policy-return-v1 expires after 10 days"),
    lambda text: text.replace("syn-policy-return-v1 为 10 天", "syn-policy-return-v1 为 11 天"),
    lambda text: text + " 政策V1应优先适用。",
    lambda text: text + " Version 1 is authoritative.",
])
def test_r3_saved_safety038_answer_still_rejects_relation_value_and_precedence_changes(answer_mutator):
    answer, sources, _rejection = _safety038_r3_real_rejection_fixture_and_answer()
    issues = _answer_semantic_issues(answer_mutator(answer), sources,
        "The fictional return excerpts have identical effective dates but different 10-day and 30-day terms. Which source is authoritative?")
    assert issues


@pytest.mark.parametrize("answer", [
    "无法判定哪份来源更具权威性；目前未提供版本优先级或冲突解决规则。",
    "No authoritative version is identified; no priority rule is provided.",
])
def test_absent_or_unknown_policy_authority_is_not_an_affirmative_priority_claim(answer):
    from app.m2.self_rag import _has_unsupported_policy_priority_claim
    assert not _has_unsupported_policy_priority_claim(answer)


def test_affirmative_priority_claim_is_still_detected_next_to_an_absence_statement():
    from app.m2.self_rag import _has_unsupported_policy_priority_claim
    answer = "没有提供版本优先级，但 Version 1 is authoritative."
    assert _has_unsupported_policy_priority_claim(answer)


def test_priority_negation_is_local_to_its_assertion_and_does_not_hide_a_contrastive_claim():
    from app.m2.self_rag import _has_unsupported_policy_priority_claim
    assert not _has_unsupported_policy_priority_claim("没有给出优先级规则。")
    # The first assertion is negative; the post-contrast assertion is positive.
    assert _has_unsupported_policy_priority_claim("没有提供版本优先级，但政策V1优先适用。")


def test_safety038_full_real_rejected_candidate_has_unscoped_value_list_without_guessed_source_mapping():
    from app.m2.self_rag import _policy_window_facts
    answer, sources, rejection, question = _safety038_saved_sealed_scope_answer()
    assert rejection["candidate_citation_ids"] == ["syn-policy-return-v1", "syn-policy-return-v2"]
    facts = _policy_window_facts(answer)
    assert [(row["amount"], row["relation"]) for row in facts] == [
        (10, "UNSCOPED"), (30, "UNSCOPED")]
    # The answer's stated object is retained for both coordinated values;
    # source-to-value ownership remains unassigned and is not guessed.
    assert facts[0]["subject"] and facts[0]["subject"] == facts[1]["subject"]
    assert not any("天与" in row["subject"] for row in facts)
    attributed = _policy_window_facts(answer, cited_ids=rejection["candidate_citation_ids"])
    assert all(row["subject"] == facts[0]["subject"] for row in attributed)
    assert all(row["_cited_source_ids"] == () for row in attributed)
    assert _answer_semantic_issues(answer, sources, question) == []


@pytest.mark.parametrize("replacement", ["食品", "已拆封商品"])
def test_coordinated_safety038_windows_reject_explicitly_changed_object(replacement):
    answer, sources, _rejection, question = _safety038_r4_real_rejection_fixture_and_answer()
    answer, sources, _rejection, question = _safety038_saved_sealed_scope_answer()
    changed = answer.replace("密封商品", replacement)
    assert changed != answer
    issues = _answer_semantic_issues(changed, sources, question)
    assert issues
    assert all(item["code"] in {
        "ANSWER_UNSUPPORTED_POLICY_WINDOW", "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED"} for item in issues)


def test_policy014_r3_complete_accepted_answer_and_safety038_r4_candidate_are_reassessed_from_real_archives():
    answer, sources, citation_ids, question = _policy014_r3_real_accepted_fixture_and_answer()
    assert citation_ids == {"syn-policy-giftcard-a", "syn-policy-giftcard-b"}
    assert _answer_semantic_issues(answer, sources, question) == []
    safety_answer, safety_sources, rejection, safety_question = _safety038_r4_real_rejection_fixture_and_answer()
    assert rejection["candidate_answer_present"] is True
    assert rejection["candidate_citation_ids"] == ["syn-policy-return-v1", "syn-policy-return-v2"]
    assert _answer_semantic_issues(safety_answer, safety_sources, safety_question) == []


def test_value_only_duration_list_is_not_supported_by_an_uncited_or_wrong_value_source():
    from app.m2.self_rag import _answer_semantic_issues
    answer, sources, _rejection, question = _safety038_r4_real_rejection_fixture_and_answer()
    only_one = [row for row in sources if row["chunk_id"] == "syn-policy-return-v1"]
    issues = _answer_semantic_issues(answer, only_one, question)
    assert any(row["code"] in {"ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED", "ANSWER_UNSUPPORTED_POLICY_WINDOW"}
        for row in issues)


@pytest.mark.parametrize("answer_mutator", [
    lambda text: text.replace("12 个月后过期", "13 个月后过期", 1),
    lambda text: text.replace("礼品卡在 12 个月后过期", "订单在 12 个月后过期", 1),
    lambda text: text.replace("expires after 12 months", "expires within 12 months", 1),
    lambda text: text.replace("并不一致", "一致", 1).replace("二者内容相互矛盾", "二者内容并不矛盾", 1),
    lambda text: text + " A 规则优先适用。",
])
def test_r2_real_smoke_failure_answer_rejects_altered_value_object_relation_or_priority(answer_mutator):
    answer, sources, _ = _policy014_r2_smoke_failure_fixture_and_answer()
    bad_answer = answer_mutator(answer)
    issues = _answer_semantic_issues(bad_answer, sources,
        "Do the two fictional gift-card policies say the same thing about expiry?")
    assert issues


def test_r2_real_smoke_failure_answer_rejects_duration_when_only_non_expiry_source_is_cited():
    answer, sources, _ = _policy014_r2_smoke_failure_fixture_and_answer()
    only_b = [row for row in sources if row["chunk_id"] == "syn-policy-giftcard-b"]
    issues = _answer_semantic_issues(answer, only_b,
        "Do the two fictional gift-card policies say the same thing about expiry?")
    assert any(item["code"] in {"ANSWER_UNSUPPORTED_POLICY_WINDOW", "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED"}
        for item in issues)


def test_saved_full_policy014_failure_answer_matches_real_fixture_assertions():
    from app.m2.self_rag import (_policy_assertion_clauses, _policy_expiry_facts,
        _policy_window_facts)
    answer, sources = _policy014_real_failure_fixture_and_answer()
    question = "Do the two fictional gift-card policies say the same thing about expiry?"
    assert _answer_semantic_issues(answer, sources, question) == []
    facts = _policy_window_facts(answer)
    assert len(facts) == 1
    assert facts[0]["amount"] == 12 and facts[0]["unit"] == "month"
    assert facts[0]["relation"] == "EXPIRES_AFTER"
    assert facts[0]["subject"] == ("giftcard",) and facts[0]["condition"] == ()
    assert [fact["kind"] for fact in _policy_expiry_facts(answer)] == ["EXPIRES_AFTER", "NO_EXPIRY"]
    clauses = _policy_assertion_clauses(answer)
    assert len(clauses) == 3  # narrative preface and the independent A/B assertions
    assert sources[0]["content"] == "Fictional rule A: gift card expires after 12 months."


@pytest.mark.parametrize("answer", [
    "The two fictional policy statements conflict:\nFictional rule A: Gift card expires after 12 months, whereas Fictional rule B: The gift card does not expire.",
    "\u89c4\u5219B\uff1a\u793c\u54c1\u5361\u4e0d\u4f1a\u8fc7\u671f\uff1b\u89c4\u5219A\uff1a\u793c\u54c1\u5361\u572812\u4e2a\u6708\u540e\u8fc7\u671f\u3002\u4e24\u6761\u89c4\u5b9a\u4e0d\u4e00\u81f4\u3002",
    "\u793c\u54c1\u5361\u572812\u4e2a\u6708\u540e\u8fc7\u671f\uff0c\u800c\u4e0d\u4f1a\u8fc7\u671f\u3002\u4e24\u6761\u89c4\u5219\u51b2\u7a81\u3002",
    "Fictional rule A says the gift card expires after 12 months; Fictional rule B says the gift card does not expire. The statements conflict and neither has established priority.",
])
def test_policy014_complete_natural_restatements_preserve_conflict_without_priority(answer):
    _saved, sources = _policy014_real_failure_fixture_and_answer()
    assert _answer_semantic_issues(answer, sources,
        "Do the two fictional gift-card policies say the same thing about expiry?") == []


@pytest.mark.parametrize("source,answer,expected", [
    ("Fictional rule B: gift card does not expire.",
     "Fictional rule B states that the gift card expires after 12 months.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months.", "Gift card expires after 13 months.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months.", "Gift card expires after 12 weeks.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months.", "The gift card can be returned within 12 months.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months.", "The warranty expires after 12 months.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months only if activated.",
     "Gift card expires after 12 months.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
    ("Gift card expires after 12 months if not activated.",
     "Gift card expires after 12 months if activated.", "ANSWER_UNSUPPORTED_POLICY_WINDOW"),
])
def test_policy_window_rejects_value_relation_subject_condition_and_polarity_transfer(source, answer, expected):
    row = chunk("policy-window-scope", source)
    assert any(item["code"] == expected for item in _answer_semantic_issues(answer, [row]))


def test_multiple_subjects_windows_and_conditions_are_matched_independently():
    sources = [
        chunk("gift-card-rule", "Gift card expires after 12 months only if activated."),
        chunk("warranty-rule", "Warranty expires after 6 months provided that registered."),
    ]
    correct = ("Gift card expires after 12 months only if activated; "
        "the warranty expires after 6 months provided that registered.")
    assert _answer_semantic_issues(correct, sources) == []
    swapped = ("Gift card expires after 6 months only if activated; "
        "the warranty expires after 12 months provided that registered.")
    assert sum(item["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW"
        for item in _answer_semantic_issues(swapped, sources)) == 2


def test_unresolved_pronoun_scope_is_reported_as_unknown_not_asserted_contradiction():
    source = chunk("gift-card-expiry", "Gift card expires after 12 months.")
    issues = _answer_semantic_issues("It expires after 12 months.", [source])
    assert any(item["code"] == "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED" for item in issues)
    assert not any(item["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" for item in issues)


def test_explicitly_uncertain_window_reference_is_not_misread_as_a_policy_assertion():
    source = chunk("return-window", "Returns may be submitted within 30 days after receipt.")
    assert _answer_semantic_issues(
        "Cannot determine whether this order meets the 30-day window.", [source]) == []
    unsupported = _answer_semantic_issues("The return window is 30 days.", [source])
    assert any(item["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" for item in unsupported)


def test_conflict_requires_supported_positions_same_subject_and_no_invented_precedence():
    _saved, sources = _policy014_real_failure_fixture_and_answer()
    evidence = sources
    unsupported_scope = ("Fictional rule A says the gift card expires after 12 months; "
        "Fictional rule B says the gift card does not expire. All products expire after 12 months.")
    issues = _answer_semantic_issues(unsupported_scope, evidence,
        "Do the two fictional gift-card policies say the same thing about expiry?")
    assert any(item["code"] == "ANSWER_UNSUPPORTED_POLICY_WINDOW" for item in issues)
    priority = ("Fictional rule A says the gift card expires after 12 months; "
        "Fictional rule B says it does not expire. Rule A is controlling.")
    assert any(item["code"] == "ANSWER_UNSUPPORTED_POLICY_PRIORITY"
        for item in _answer_semantic_issues(priority, evidence,
            "Do the two fictional gift-card policies say the same thing about expiry?"))


def test_policy_date_mentions_are_not_duration_assertions_but_real_windows_still_are():
    from app.m2.self_rag import _policy_duration_matches, _policy_window_facts
    date_examples = (
        "规则自2026年7月1日起生效。",
        "该日期为 2026-07-01，不能据此确认送达日期适用性。",
        "The policy starts on July 1, 2026.",
        "The effective date is 1 July 2026.",
    )
    for text in date_examples:
        assert _policy_duration_matches(text) == []
        assert _policy_window_facts(text) == []

    durations = (
        ("商品有效期为7个月。", 7, "month"),
        ("此规则只在1天内有效。", 1, "day"),
        ("送达后7天内可以申请。", 7, "day"),
    )
    for text, amount, unit in durations:
        facts = _policy_window_facts(text)
        assert any(row["amount"] == amount and row["unit"] == unit for row in facts)

    archived_candidate = ("证据不足：本轮唯一的证据片段只说明某条虚构规则仅在一个未具体说明的时间区间内适用，"
        "没有给出任何日期、起止时间或与 7 月 1 日之后送达订单相关的规定，因此无法判断该虚构政策是否适用于 "
        "7 月 1 日之后送达的订单。请补充该政策的生效时间、适用范围（包括是否按送达日期计算）等政策依据。"
        "另外，本接口不能核验任何具体订单的实际送达或订单状态。")
    assert not _policy_window_facts(archived_candidate)


def _approved_return_refund_evidence():
    policies = json.loads((Path(__file__).resolve().parents[1] / "knowledge/policies.json").read_text(encoding="utf-8"))
    selected = []
    for document in policies["documents"]:
        if document["policy_id"] == "return-standard" and document["effective_to"] is None:
            clauses = [clause for clause in document["clauses"] if "30天内的质量问题" in clause["text"]]
        elif document["policy_id"] == "refund":
            clauses = [clause for clause in document["clauses"] if "已签收订单需要已批准的退货申请" in clause["text"]]
        else:
            continue
        for clause in clauses:
            evidence_id = f"policies/{document['policy_id']}@{document['version']}#{clause['clause_id']}"
            selected.append(chunk(evidence_id, clause["text"], version=document["version"]) | {
                "title": document["title"], "effective_from": document["effective_from"],
                "effective_to": document["effective_to"], "policy_version": document["version"]})
    assert len(selected) == 2
    return {row["chunk_id"]: row for row in selected}


def test_historical_v5_extractions_are_replaced_by_identical_source_atomic_rules():
    root = Path(__file__).resolve().parents[1]
    first = json.loads((root / "docs/probes/m2.3-condition-semantics-20261020T013824Z.json").read_text(encoding="utf-8"))
    second = json.loads((root / "docs/probes/m2.3-postgres-deepseek_real-20261020T013902Z.json").read_text(encoding="utf-8"))
    old_a = first["response"]["conditions_result"]["conditions"]
    old_b = json.loads(second["self_rag"]["condition_extraction_raw_output"])["conditions"]
    assert old_a != old_b
    assert any("退款到账" in item["text"] for item in old_b)

    evidence = _approved_return_refund_evidence()
    question = first["question"]
    legacy_a = [Condition.model_validate(item) for item in old_a]
    legacy_b = [Condition.model_validate(item) for item in old_b]
    issues_a = _condition_semantic_issues(question, "POLICY_EXPLANATION", legacy_a, evidence)
    issues_b = _condition_semantic_issues(question, "POLICY_EXPLANATION", legacy_b, evidence)
    assert not issues_a
    assert "POLICY_STAGE_DECLARATION_MISMATCH" in {item["code"] for item in issues_b}

    # New policy rule sets come from the applicable evidence, so different legacy
    # wording cannot merge independent rules or promote an explanatory caveat.
    normalized_a = _atomic_policy_conditions(evidence)
    normalized_b = _atomic_policy_conditions(evidence)
    assert [row.model_dump() for row in normalized_a] == [row.model_dump() for row in normalized_b]
    normalized_reworded_question = _atomic_policy_conditions(evidence)
    assert [row.model_dump() for row in normalized_a] == [row.model_dump() for row in normalized_reworded_question]
    excerpts = {row.source_excerpt for row in normalized_a}
    assert any("30天内的质量问题可提交整单退货申请" in item for item in excerpts)
    assert any("已批准的退货申请才能申请退款" in item for item in excerpts)
    settlement = next(row for row in normalized_a if "不代表真实到账" in row.text)
    assert settlement.rule_role == "NEGATIVE_BOUNDARY" and not settlement.required
    assert all(row.text == row.source_excerpt and row.source_evidence_id in evidence for row in normalized_a)
    assert not any("退货全部完成" in row.text or "退货流程完成" in row.text for row in normalized_a)


class RequestIntentStubProvider:
    def __init__(self):
        self.settings = load_settings(require_api_key=False)
        self.calls = {"extract": 0, "grade": 0, "answer": 0}

    def extract_conditions(self, question):
        self.calls["extract"] += 1
        result = {"request_mode": "POLICY_EXPLANATION", "analysis_version": "request_intent_v1",
            "requested_aspects": [
                {"aspect_id": "quality-return", "text": "瑕疵商品退货的期限和审核阶段",
                 "query_anchor": "瑕疵商品申请退货"},
                {"aspect_id": "refund-prerequisite", "text": "已签收订单退货获批与退款申请的衔接",
                 "query_anchor": "申请退货与退款，两个政策条件如何衔接"}],
            "conditions": [{"condition_id": "fact", "text": "商品已签收且存在瑕疵",
                "kind": "USER_PROVIDED_FACT", "required": False,
                "source_excerpt": "已签收的瑕疵商品", "source_type": "USER_PROVIDED"}]}
        return {"conditions_result": result, "result": result, "raw_content": json.dumps(result, ensure_ascii=False),
                "finish_reason": "stop", "model_id": "stub", "usage": {"prompt_tokens": 20, "completion_tokens": 8},
                "estimated_cost_usd": 0.00001, "latency_ms": 1}

    def grade_evidence(self, question, conditions, evidence, evaluation_at, *, request_mode="POLICY_EXPLANATION",
                       requested_aspects=None):
        self.calls["grade"] += 1
        quality = next(row for row in conditions if row["kind"] == "POLICY_RULE" and
                       "30天内的质量问题" in row["text"])
        refund = next(row for row in conditions if row["kind"] == "POLICY_RULE" and
                      {"RETURN_APPROVAL", "REFUND_APPLICATION"}.issubset(set(row["policy_stages"])))
        supporting = [row["condition_id"] for row in conditions if row["kind"] == "POLICY_RULE"
                      and row["condition_id"] not in {quality["condition_id"], refund["condition_id"]}]
        mapping = [{"aspect_id": "quality-return", "required_rules": [{
                        "rule_id": quality["condition_id"], "basis": "DIRECTLY_ASKED",
                        "query_anchor": "瑕疵商品申请退货", "rationale": "直接回答瑕疵退货的时限和审核阶段"}],
                    "supporting_rule_ids": supporting},
                   {"aspect_id": "refund-prerequisite", "required_rules": [{
                        "rule_id": refund["condition_id"], "basis": "DIRECTLY_ASKED",
                        "query_anchor": "申请退货与退款，两个政策条件如何衔接",
                        "rationale": "回答退货与退款两个政策阶段的衔接"}], "supporting_rule_ids": []}]
        judgments = [{"condition_id": row["condition_id"], "evidence_id": row["source_evidence_id"],
                      "judgment": "supports"} for row in conditions if row["kind"] == "POLICY_RULE"]
        payload = {"grade": {"relevant": True, "sufficient": True, "missing_aspect": [],
                              "rationale": "两个用户询问方面均映射至适用政策原文。"},
                   "judgments": judgments, "aspect_rule_map": mapping,
                   "proposed_conclusion": "EXPLAIN"}
        return {"grade_payload": payload, "result": payload, "raw_content": json.dumps(payload, ensure_ascii=False),
                "finish_reason": "stop", "model_id": "stub", "usage": {"prompt_tokens": 40, "completion_tokens": 20},
                "estimated_cost_usd": 0.00003, "latency_ms": 1}

    def answer(self, question, evidence):
        self.calls["answer"] += 1
        return self._answer_result(evidence)

    def answer_with_requirements(self, question, evidence, *, required_conditions, context_notes):
        self.calls["answer"] += 1
        return self._answer_result(evidence)

    @staticmethod
    def _answer_result(evidence):
        ids = [row["chunk_id"] for row in evidence]
        return {"answer": "质量问题在签收后30天内可提交整单退货申请并由人工审核，是否获批仍需审核确认；"
                "题目没有说明质量问题距签收多久，不能据此确定该瑕疵订单符合30天窗口。"
                "已签收订单须在退货申请获批后才能申请退款。",
                "citations": ids, "model_id": "stub", "usage": {"prompt_tokens": 30, "completion_tokens": 25},
                "estimated_cost_usd": 0.00003, "latency_ms": 1}


def test_request_intent_then_source_atomic_grade_answers_without_settlement_or_completion_inflation():
    evidence = _approved_return_refund_evidence()
    optional = [
        chunk("policies/refund@2026-01#uniqueness",
              "同一订单任一时刻仅允许一个开放的整单退款申请；部分退款暂不支持。"),
        chunk("policies/refund@2026-01#shipment-branch",
              "已发货未签收默认转人工。"),
        chunk("policies/return-standard@2026-07#late-warranty",
              "超过30天需转人工核对保修。"),
    ]
    retrieval = RetrievalStub([[*list(evidence.values()), *optional]])
    provider = RequestIntentStubProvider()
    runner = MultiHopRetrievalService(retrieval, provider, config=SelfRAGConfig(max_model_requests=3))
    question = "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？"
    result = runner.run(question)
    assert result["sufficient"] and result["answer_kind"] == "POLICY_ANSWER"
    assert provider.calls == {"extract": 1, "grade": 1, "answer": 1}
    assert result["condition_analysis_version"] == "request_intent_v1"
    assert result["policy_rule_construction"]["all_source_excerpts_verified"]
    required = [row for row in result["conditions"] if row["kind"] == "POLICY_RULE" and row["required"]]
    assert len(required) == 2
    assert all(row["text"] == row["source_excerpt"] and row["source_evidence_id"] for row in required)
    assert not any(row["required"] and row["rule_role"] == "NEGATIVE_BOUNDARY" for row in result["conditions"])
    assert not any("RETURN_COMPLETION" in row["policy_stages"] for row in required)
    assert result["condition_semantic_audit"]["status"] == "PASS"
    assert result["answer_evidence_audit"] and all(
        item["status"] == "REQUIRED_EXCERPT_INCLUDED" for item in result["answer_evidence_audit"])
    assert set(result["completion"]["citations"]) == set(evidence)
    assert len(result["condition_scope_audit"]) >= 2
    assert _answer_semantic_issues(result["completion"]["answer"],
        [row for row in result["evidence"] if row["chunk_id"] in result["completion"]["citations"]]) == []


def test_overpromoted_relevant_rules_are_scoped_to_two_core_rules_across_current_policy_snapshot():
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "knowledge/policies.json").read_text(encoding="utf-8"))
    evidence = {}
    for document in policy["documents"]:
        if document["policy_id"] not in {"return-standard", "refund", "cancel"} or document["effective_to"] is not None:
            continue
        for clause in document["clauses"]:
            evidence_id = f"policies/{document['policy_id']}@{document['version']}#{clause['clause_id']}"
            evidence[evidence_id] = {"chunk_id": evidence_id, "title": document["title"],
                "content": clause["text"], "effective_from": document["effective_from"],
                "effective_to": document["effective_to"], "version": document["version"]}
    rules = _atomic_policy_conditions(evidence)
    question = "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？"
    quality_anchor = "瑕疵商品申请退货"
    refund_anchor = "申请退货与退款，两个政策条件如何衔接"
    aspects = [RequestedAspect(aspect_id="quality", text="瑕疵商品退货期限和审核阶段",
                               query_anchor=quality_anchor),
               RequestedAspect(aspect_id="refund", text="已签收退款的退货批准前置规则",
                               query_anchor=refund_anchor)]
    quality = next(row for row in rules if "30天内的质量问题" in row.text and "人工审核" in row.text)
    refund = next(row for row in rules if "已签收订单需要已批准的退货申请" in row.text)
    # Reproduce the old behavior by declaring every positive candidate required.
    mappings = []
    for aspect, anchor, selected in ((aspects[0], quality_anchor, [row for row in rules
                                    if row.source_evidence_id.startswith("policies/return-standard")]),
                                    (aspects[1], refund_anchor, [row for row in rules
                                    if row.source_evidence_id.startswith(("policies/refund", "policies/cancel"))])):
        required_assignments = [{"rule_id": row.condition_id, "basis": "DIRECTLY_ASKED",
            "query_anchor": anchor, "rationale": "候选策略认为该条款与当前主题相关"}
            for row in selected if row.rule_role != "NEGATIVE_BOUNDARY"]
        supporting = [row.condition_id for row in selected if row.rule_role == "NEGATIVE_BOUNDARY"]
        mappings.append({"aspect_id": aspect.aspect_id, "required_rules": required_assignments,
                         "supporting_rule_ids": supporting})
    grade = GradePayload.model_validate({"grade": {"relevant": True, "sufficient": True,
        "missing_aspect": [], "rationale": "所有候选条款均有证据"}, "judgments": [],
        "aspect_rule_map": mappings, "proposed_conclusion": "EXPLAIN"})
    conditions = rules + [Condition(condition_id=_aspect_condition_id(a.aspect_id), text=a.text,
        kind="POLICY_ASPECT", required=True, source_type="UNKNOWN") for a in aspects]
    audit = _apply_aspect_rule_map(conditions, aspects, grade, question)
    required = [row for row in conditions if row.kind == "POLICY_RULE" and row.required]
    assert {row.condition_id for row in required} == {quality.condition_id, refund.condition_id}
    assert all(item["decision"] == "SUPPORTING" for item in audit
               if item["condition_id"] not in {quality.condition_id, refund.condition_id})
    assert {reason for item in audit for reason in item["reason_codes"]} >= {
        "UNREQUESTED_SHIPMENT_BRANCH", "UNREQUESTED_RETURN_EXCEPTION", "UNREQUESTED_EXPIRED_WARRANTY",
        "UNREQUESTED_REFUND_CONCURRENCY", "UNREQUESTED_PARTIAL_REFUND", "UNREQUESTED_CANCEL_FLOW"}


def test_overlapping_aspects_can_reuse_rules_but_dependency_direction_must_follow_policy_stages():
    evidence = _approved_return_refund_evidence()
    conditions = _atomic_policy_conditions(evidence)
    quality = next(row for row in conditions if row.source_evidence_id.endswith("#quality")
                   and "30天内的质量问题" in row.text)
    refund = next(row for row in conditions if row.source_evidence_id.endswith("#before-shipping")
                  and "已签收订单需要已批准的退货申请" in row.text)
    question = "瑕疵商品退货和退款的具体条件分别是什么，两个政策条件如何衔接？"
    aspects = [RequestedAspect(aspect_id="quality", text="质量退货条件",
                    query_anchor="瑕疵商品退货"),
               RequestedAspect(aspect_id="refund", text="退款条件",
                    query_anchor="退款的具体条件"),
               RequestedAspect(aspect_id="link", text="退货与退款前后关系",
                    query_anchor="两个政策条件如何衔接")]
    grade = GradePayload.model_validate({
        "grade": {"relevant": True, "sufficient": True, "missing_aspect": [], "rationale": "两个规则覆盖所有方面"},
        "judgments": [],
        "aspect_rule_map": [
            {"aspect_id": "quality", "required_rules": [{"rule_id": quality.condition_id,
                "basis": "DIRECTLY_ASKED", "query_anchor": aspects[0].query_anchor,
                "rationale": "回答质量退货条件"}], "supporting_rule_ids": [refund.condition_id]},
            {"aspect_id": "refund", "required_rules": [{"rule_id": refund.condition_id,
                "basis": "DIRECTLY_ASKED", "query_anchor": aspects[1].query_anchor,
                "rationale": "回答已签收订单退款条件"}], "supporting_rule_ids": [quality.condition_id]},
            {"aspect_id": "link", "required_rules": [
                {"rule_id": quality.condition_id, "basis": "DIRECTLY_ASKED",
                 "query_anchor": aspects[2].query_anchor, "rationale": "说明先提交退货申请并审核"},
                {"rule_id": refund.condition_id, "basis": "POLICY_DEPENDENCY",
                 "query_anchor": aspects[2].query_anchor, "rationale": "退款申请以前置的获批退货为条件",
                 "depends_on_rule_id": quality.condition_id}], "supporting_rule_ids": []}],
        "proposed_conclusion": "EXPLAIN"})
    audit = _apply_aspect_rule_map(conditions, aspects, grade, question)
    assert {row.condition_id for row in conditions if row.kind == "POLICY_RULE" and row.required} == {
        quality.condition_id, refund.condition_id}
    assert refund.depends_on == [quality.condition_id]
    assert {item["aspect_id"] for item in audit if item["condition_id"] == quality.condition_id} == {
        "quality", "refund", "link"}

    redundant_label = GradePayload.model_validate(grade.model_dump())
    link = next(item for item in redundant_label.aspect_rule_map if item.aspect_id == "link")
    for assignment in link.required_rules:
        assignment.basis = "DIRECTLY_ASKED"
        assignment.depends_on_rule_id = None
    quality_link = next(item for item in link.required_rules if item.rule_id == quality.condition_id)
    quality_link.basis = "POLICY_DEPENDENCY"
    fresh_conditions = _atomic_policy_conditions(evidence)
    normalized_audit = _apply_aspect_rule_map(fresh_conditions, aspects, redundant_label, question)
    assert {row.condition_id for row in fresh_conditions if row.kind == "POLICY_RULE" and row.required} == {
        quality.condition_id, refund.condition_id}
    assert any("REDUNDANT_DEPENDENCY_CLASSIFICATION_NORMALIZED" in item["reason_codes"]
               for item in normalized_audit)

    inverted = GradePayload.model_validate(grade.model_dump())
    link = next(item for item in inverted.aspect_rule_map if item.aspect_id == "link")
    for assignment in link.required_rules:
        if assignment.rule_id == quality.condition_id:
            assignment.basis = "POLICY_DEPENDENCY"
            assignment.depends_on_rule_id = refund.condition_id
    fresh_conditions = _atomic_policy_conditions(evidence)
    with pytest.raises(ValueError, match="contradicts policy lifecycle order"):
        _apply_aspect_rule_map(fresh_conditions, aspects, inverted, question)


def test_required_answer_coverage_rejects_omitted_window_review_or_approval_and_missing_time_caveat():
    quality = Condition(condition_id="quality", text="签收后30天内的质量问题可提交整单退货申请,由人工审核",
        kind="POLICY_RULE", required=True, source_excerpt="签收后30天内的质量问题可提交整单退货申请,由人工审核",
        source_evidence_id="return", source_type="POLICY", policy_stages=["RETURN_SUBMISSION", "RETURN_REVIEW"])
    refund = Condition(condition_id="refund", text="已签收订单需要已批准的退货申请才能申请退款",
        kind="POLICY_RULE", required=True, source_excerpt="已签收订单需要已批准的退货申请才能申请退款",
        source_evidence_id="refund", source_type="POLICY", policy_stages=["RETURN_APPROVAL", "REFUND_APPLICATION"])
    question = "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？"
    notes = ["政策规定了从具体业务事件起算的时间窗口，但问题未提供事件间隔；只能解释规则，不得判断个案满足该期限。"]
    good = ("质量问题在签收后30天内可提交整单退货申请并由人工审核，是否获批仍需审核确认；"
            "题目没有说明质量问题距签收多久，不能据此确定个案符合期限。已签收订单须在退货申请获批后才能申请退款。")
    assert _required_answer_issues(good, question, [quality, refund], notes) == []
    good_with_explicit_non_guarantee = ("签收后30天内的质量问题可提交整单退货申请，由人工审核，不保证获批；"
            "问题未说明质量问题距签收多久，无法确认个案是否在30天期限内。已签收订单获批退货申请后才可申请退款。")
    assert _required_answer_issues(good_with_explicit_non_guarantee, question, [quality, refund], notes) == []
    incomplete = "瑕疵商品可以退货，退货流程完成后即可申请退款。"
    codes = {item["code"] for item in _required_answer_issues(incomplete, question, [quality, refund], notes)}
    assert {"ANSWER_REQUIRED_POLICY_STAGE_OMITTED", "ANSWER_REQUIRED_POLICY_WINDOW_OMITTED",
            "ANSWER_MISSING_UNVERIFIED_TIME_WINDOW_CAVEAT"} <= codes
    lifecycle = _answer_semantic_issues(incomplete, [
        {"chunk_id": "return", "title": "退货政策", "content": quality.source_excerpt},
        {"chunk_id": "refund", "title": "退款政策", "content": refund.source_excerpt}], question)
    assert any(item.get("policy_stage") == "RETURN_COMPLETION" for item in lifecycle)


def test_grade_cannot_extend_request_intent_sufficiency_gate_with_supporting_clause():
    evidence = _approved_return_refund_evidence()
    extra = chunk("policies/refund@2026-01#uniqueness", "同一订单任一时刻只允许一个开放的整单退款申请。")
    provider = RequestIntentStubProvider()
    original = provider.grade_evidence

    def conservative_grade(question, conditions, rows, evaluation_at, **kwargs):
        result = original(question, conditions, rows, evaluation_at, **kwargs)
        payload = result["grade_payload"]
        optional_rule = next(item for item in conditions if item["kind"] == "POLICY_RULE"
                             and item["source_evidence_id"] == extra["chunk_id"])
        payload["grade"]["sufficient"] = False
        payload["grade"]["missing_aspect"] = [optional_rule["condition_id"]]
        result["raw_content"] = json.dumps(payload, ensure_ascii=False)
        return result | {"grade_payload": payload}

    provider.grade_evidence = conservative_grade
    result = MultiHopRetrievalService(RetrievalStub([[*evidence.values(), extra]]), provider,
        config=SelfRAGConfig(max_model_requests=3)).run(
            "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？")
    assert result["answer_kind"] == "POLICY_ANSWER"
    assert result["sufficiency_audit"][0]["provider_sufficient"] is False
    assert result["sufficiency_audit"][0]["effective_sufficient"] is True


def test_dependency_from_a_scope_demoted_clause_does_not_raise_the_required_gate():
    evidence = _approved_return_refund_evidence()
    conditions = _atomic_policy_conditions(evidence)
    quality = next(row for row in conditions if "30天内的质量问题" in row.text)
    adjacent_review = Condition(condition_id="adjacent-no-reason-review",
        text="提交后由人工审核，不保证批准", kind="POLICY_RULE", required=False,
        source_excerpt="提交后由人工审核，不保证批准",
        source_evidence_id="policies/return-standard@2026-07#no-reason",
        source_type="POLICY", policy_stages=["RETURN_REVIEW"])
    conditions.append(adjacent_review)
    aspect = RequestedAspect(aspect_id="quality", text="瑕疵商品质量退货条件",
                             query_anchor="瑕疵商品退货条件")
    grade = GradePayload.model_validate({"grade": {"relevant": True, "sufficient": True,
        "missing_aspect": [], "rationale": "质量规则及相邻审核条款均被召回"}, "judgments": [],
        "aspect_rule_map": [{"aspect_id": "quality", "required_rules": [
            {"rule_id": quality.condition_id, "basis": "DIRECTLY_ASKED",
             "query_anchor": aspect.query_anchor, "rationale": "回答质量退货期限"},
            {"rule_id": adjacent_review.condition_id, "basis": "POLICY_DEPENDENCY",
             "query_anchor": aspect.query_anchor, "rationale": "相邻审核文字似为依赖",
             "depends_on_rule_id": quality.condition_id}], "supporting_rule_ids": []}],
        "proposed_conclusion": "EXPLAIN"})
    audit = _apply_aspect_rule_map(conditions, [aspect], grade,
                                   "瑕疵商品退货条件是什么？")
    assert quality.required is True
    assert adjacent_review.required is False
    assert not adjacent_review.depends_on
    assert any(item["condition_id"] == adjacent_review.condition_id and
               "UNREQUESTED_NO_REASON_RETURN_RULE" in item["reason_codes"]
               for item in audit)
    assert any(item["condition_id"] == adjacent_review.condition_id and
               "DEPENDENCY_IGNORED_FOR_SUPPORTING_RULE" in item["reason_codes"]
               for item in audit)


def test_grade_policy_aspect_judgment_is_ignored_and_cannot_satisfy_placeholder():
    from types import SimpleNamespace

    aspect = RequestedAspect(aspect_id="delivery", text="政策能否证明订单签收",
                             query_anchor="政策能否证明")
    placeholder = __import__("app.m2.self_rag", fromlist=["_aspect_placeholders"])._aspect_placeholders([aspect])[0]
    grade = GradePayload.model_validate({"grade": {"relevant": True, "sufficient": True,
        "missing_aspect": [], "rationale": "只有占位方面的重复判断"},
        "judgments": [{"condition_id": placeholder.condition_id, "evidence_id": "policy/delivery",
                       "judgment": "supports"}],
        "aspect_rule_map": [{"aspect_id": aspect.aspect_id, "required_rules": [],
                              "supporting_rule_ids": []}],
        "proposed_conclusion": "EXPLAIN"})
    conditions = [placeholder]
    _apply_aspect_rule_map(conditions, [aspect], grade, "政策能否证明我的订单已签收？")
    audit = _remove_policy_aspect_judgments(grade, conditions)
    runner = MultiHopRetrievalService(RetrievalStub([]), RequestIntentStubProvider())
    coverage, missing, complete, _ = runner._coverage(
        conditions, grade, {"policy/delivery": chunk("policy/delivery", "政策不能证明订单签收。")},
        SimpleNamespace(evaluation_at="2026-10-20T00:00:00Z"))
    assert audit[0]["decision"] == "IGNORED_NON_RULE_JUDGMENT_NO_COVERAGE_EFFECT"
    assert grade.judgments == []
    assert placeholder.required is True
    assert complete is False and missing == [aspect.text]
    assert coverage[placeholder.condition_id].unknown == ["MISSING_POLICY_EVIDENCE"]


def test_policy_grade_cannot_use_policy_to_support_a_user_fact_and_retains_raw_response():
    evidence = _approved_return_refund_evidence()
    retrieval = RetrievalStub([list(evidence.values())])
    provider = RequestIntentStubProvider()
    original = provider.grade_evidence

    def invalid_source_grade(question, conditions, rows, evaluation_at, **kwargs):
        result = original(question, conditions, rows, evaluation_at, **kwargs)
        payload = result["grade_payload"]
        fact = next(item for item in conditions if item["kind"] == "USER_PROVIDED_FACT")
        payload["judgments"].append({"condition_id": fact["condition_id"],
            "evidence_id": rows[0]["chunk_id"], "judgment": "supports"})
        result["grade_payload"] = payload
        result["raw_content"] = json.dumps(payload, ensure_ascii=False)
        return result

    provider.grade_evidence = invalid_source_grade
    result = MultiHopRetrievalService(retrieval, provider,
        config=SelfRAGConfig(max_model_requests=3)).run(
            "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？")

    hop = result["hops"][0]
    assert result["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert hop["grade_error"]["code"] == "GRADE_JUDGMENT_SOURCE_MISMATCH"
    assert hop["grade_validation_stage"] == "evidence_coverage"
    assert '"condition_id": "fact"' in hop["grade_raw_output"]
    assert provider.calls["answer"] == 0


def test_rejected_answer_semantics_are_persistable_without_masking_the_reason():
    evidence = _approved_return_refund_evidence()
    provider = RequestIntentStubProvider()

    def unsupported_answer(question, rows):
        ids = [row["chunk_id"] for row in rows if "return-standard" in row["chunk_id"] or "refund@" in row["chunk_id"]]
        return {"answer": "必须完成退货后才能申请退款。", "citations": ids,
                "model_id": "stub", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "estimated_cost_usd": 0.00001, "latency_ms": 1, "finish_reason": "stop",
                "max_tokens": 700, "prompt_version": "test-answer-v1",
                "raw_content": '{"answer":"必须完成退货后才能申请退款。","citations":["policy-a"]}'}

    provider.answer_with_requirements = lambda question, rows, **kwargs: unsupported_answer(question, rows)
    runner = MultiHopRetrievalService(RetrievalStub([list(evidence.values())]), provider,
                                      config=SelfRAGConfig(max_model_requests=3))
    result = runner.run("已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？")

    assert result["answer_kind"] == "GENERATION_UNAVAILABLE"
    assert result["stop_reason"] == "GENERATION_UNAVAILABLE"
    assert result["generation_error"]["code"] == "ValueError"
    assert any(issue["code"] == "ANSWER_UNSUPPORTED_POLICY_PHASE" and
               issue.get("policy_stage") == "RETURN_COMPLETION"
               for issue in result["generation_error"]["semantic_issues"])
    assert result["answer"] == ""
    diagnostic = result["generation_error"]["diagnostic"]
    assert diagnostic["raw_content"].startswith('{"answer":')
    assert diagnostic["usage_counts"] == {"prompt_units": 10, "completion_units": 5, "total_units": None}
    assert diagnostic["finish_reason"] == "stop"
    assert diagnostic["validation_errors"] == result["generation_error"]["semantic_issues"]
    assert diagnostic["prompt_version"] == "test-answer-v1"
    assert len(diagnostic["raw_content"]) <= 6000


def test_grade_cannot_require_unasked_negative_boundary_or_map_unsupported_rule():
    evidence = _approved_return_refund_evidence()
    rules = _atomic_policy_conditions(evidence)
    aspects = [RequestedAspect(aspect_id="refund", text="退款申请前置条件",
                               query_anchor="退货退款条件如何衔接")]
    settlement = next(row for row in rules if row.rule_role == "NEGATIVE_BOUNDARY")
    placeholder = __import__("app.m2.self_rag", fromlist=["_aspect_placeholders"])._aspect_placeholders(aspects)
    conditions = rules + placeholder
    grade = GradePayload.model_validate({"grade": {"relevant": True, "sufficient": True, "missing_aspect": [],
        "rationale": "test"}, "judgments": [], "aspect_rule_map": [
        {"aspect_id": "refund", "required_rules": [{"rule_id": settlement.condition_id,
         "basis": "DIRECTLY_ASKED", "query_anchor": "退货退款条件如何衔接", "rationale": "回答政策条件"}],
         "supporting_rule_ids": []}], "proposed_conclusion": "EXPLAIN"})
    with pytest.raises(ValueError, match="unrequested negative policy boundary"):
        _apply_aspect_rule_map(conditions, aspects, grade, "退货退款条件如何衔接？")
    wrong = settlement.model_copy(update={"text": "退货完成后才能申请退款", "required": True,
        "policy_stages": ["RETURN_COMPLETION", "REFUND_APPLICATION"], "negative_policy_stages": []})
    issues = _condition_semantic_issues("退货退款政策如何衔接？", "POLICY_EXPLANATION", [wrong], evidence)
    assert {row["code"] for row in issues} & {"POLICY_RULE_SOURCE_MISMATCH", "UNSUPPORTED_POLICY_PHASE"}


def test_manual_review_requirement_remains_a_core_policy_rule():
    evidence = {"returns": chunk("returns", "超过30天需转人工核对。")}
    rule = _atomic_policy_conditions(evidence)[0]
    assert rule.text == "超过30天需转人工核对"
    assert rule.rule_role == "CORE_RULE"
