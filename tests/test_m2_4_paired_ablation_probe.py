from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from app.m0.config import NodeConfig
from scripts.probe_m2_4_paired_ablation import (REQUEST_CAP, StageBudget, StageBudgetExceeded,
    _paired_bootstrap, _wilson, aggregate, build_plan, judge_input, judge_one, validate_judge,
    SYSTEM_CONTRACT_SOURCES, JudgeOutputValidationError)


def test_m2_4_plan_is_pinned_to_approved_dev_validation_only():
    plan = build_plan()
    assert len(plan["selection"]) == 7
    assert {row["dataset_split"] for row in plan["selection"]} == {"dev", "validation"}
    assert all(row["scoreability"] == "SCORABLE_CANDIDATE" for row in plan["selection"][:6])
    assert plan["selection"][-1]["scoreability"] == "UNSCORABLE_CANDIDATE"
    assert plan["dataset"]["sha256"] == "0daea3b85052e34217e5249211bbd7cf3c51d3fce871e2f00c0717e3632acf52"
    assert plan["budget"]["sdk_retries"] == 0
    assert plan["budget"]["worst_case_requests_for_this_plan"] == 83
    assert plan["replication"]["extra_self_rag_generations"] == 2


def test_stage_budget_refuses_before_request_cap_without_invoking_call():
    budget = StageBudget()
    budget.requests = REQUEST_CAP
    budget.calls = [{}] * REQUEST_CAP
    invoked = False
    def call():
        nonlocal invoked
        invoked = True
    with pytest.raises(StageBudgetExceeded, match="stage_request_cap"):
        budget.call(NodeConfig("x", "test", "disabled", 32, 2, .01), "x", "small", call)
    assert invoked is False


def test_stage_budget_refuses_before_estimated_cost_cap():
    budget = StageBudget()
    budget.estimated_cost_usd = 5.0
    invoked = False
    with pytest.raises(StageBudgetExceeded, match="stage_cost_cap_preflight"):
        budget.call(NodeConfig("x", "test", "disabled", 32, 2, .01), "x", "small", lambda: invoked)
    assert invoked is False and budget.requests == 0


def test_judge_coverage_requires_exact_approved_condition_ids_and_evidence_ids():
    case = {"scoreability": "SCORABLE_CANDIDATE", "required_conditions": [
        {"condition_id": "c1", "required": True}, {"condition_id": "c2", "required": False}]}
    payload = {"claims": [{"claim_id": "c1", "text": "claim", "status": "SUPPORTED", "scope": "REQUESTED", "relevance": "NECESSARY",
                           "evidence_ids": ["p/one"], "contract_source_ids": [], "rationale": "supported"}],
        "required_condition_coverage": [{"condition_id": "c1", "covered": True, "supported": True, "rationale": "checked"}],
        "overall": "PASS"}
    validate_judge(payload, case, {"p/one"})
    with pytest.raises(ValueError, match="outside candidate pool"):
        validate_judge({**payload, "claims": [{**payload["claims"][0], "evidence_ids": ["p/other"]}]}, case, {"p/one"})
    with pytest.raises(ValueError, match="condition IDs"):
        validate_judge({**payload, "required_condition_coverage": []}, case, {"p/one"})


def _judge_payload(condition_ids, *, contract_source_id=None):
    return {"claims": [{"claim_id": "c1", "text": "answer claim", "status": "SUPPORTED",
        "scope": "REQUESTED", "relevance": "NECESSARY", "evidence_ids": ["p/one"],
        "contract_source_ids": [contract_source_id] if contract_source_id else [], "rationale": "reason"}],
        "required_condition_coverage": [{"condition_id": cid, "covered": True, "supported": True,
            "rationale": "checked"} for cid in condition_ids], "overall": "PASS"}


def test_judge_coverage_is_exact_for_variable_required_condition_counts():
    for ids in [["c1"], ["c1", "c2", "c3"]]:
        case = {"scoreability": "SCORABLE_CANDIDATE", "required_conditions": [
            {"condition_id": cid, "required": True} for cid in ids]}
        validate_judge(_judge_payload(ids), case, {"p/one"})
        with pytest.raises(JudgeOutputValidationError, match="condition IDs"):
            validate_judge(_judge_payload(ids[:-1]), case, {"p/one"})


def test_historical_q_judge_has_complete_condition_coverage_but_unknown_contract_source():
    path = Path(__file__).parents[1] / "eval/raw/m2.4-self-rag-recheck-20261020T063210Z-789a7dac.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    call = next(row for row in record["request_accounting"]["calls"] if row["sequence"] == 98)
    payload = json.loads(call["raw_content"])
    case = {"scoreability": "SCORABLE_CANDIDATE", "required_conditions": [
        {"condition_id": "c2", "required": True}]}
    # Coverage was complete; the response failed for a source path not supplied by the evaluator.
    assert [row["condition_id"] for row in payload["required_condition_coverage"]] == ["c2"]
    with pytest.raises(JudgeOutputValidationError, match="claim fields are incomplete"):
        validate_judge(payload, case, {"policies/refund@2026-01#before-shipping",
            "policies/return-standard@2026-07#no-reason"},
            {row["source_id"] for row in SYSTEM_CONTRACT_SOURCES})
    for claim in payload["claims"]:
        claim.setdefault("contract_source_ids", [])
    allowed = {row["source_id"] for row in SYSTEM_CONTRACT_SOURCES}
    with pytest.raises(JudgeOutputValidationError, match="unknown system contract source"):
        validate_judge(payload, case, {"policies/refund@2026-01#before-shipping",
            "policies/return-standard@2026-07#no-reason"}, allowed)


def test_judge_retries_one_structural_failure_and_preserves_both_attempts():
    ids = ["c1", "c2"]
    case = {"case_id": "case", "scoreability": "SCORABLE_CANDIDATE", "user_query": "q",
        "required_conditions": [{"condition_id": cid, "required": True, "text": cid} for cid in ids]}
    result = {"answer_kind": "POLICY_ANSWER", "answer": "answer", "citations": ["p/one"],
        "evidence": [{"chunk_id": "p/one", "content": "evidence"}]}
    bad = _judge_payload(ids, contract_source_id="docs/not-supplied.md#AGENT")
    good = _judge_payload(ids)
    responses = iter([bad, good])
    class Completions:
        calls = []
        def create(self, **kwargs):
            self.calls.append(kwargs)
            content = json.dumps(next(responses))
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=content))], model="judge-model",
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5}))
    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    settings = SimpleNamespace(model="judge-model")
    node = NodeConfig("judge", "test", "disabled", 1000, 10, .05)
    budget = StageBudget(max_new_requests=3, max_new_cost_usd=.30)
    judged = judge_one(settings, budget, node, client, case, result, "blind")
    assert budget.requests == 2 and len(budget.calls) == 2
    assert len(judged["validation_attempts"]) == 2
    assert judged["validation_attempts"][0]["error_code"] == "JUDGE_CONTRACT_SOURCE_UNKNOWN"
    assert judged["validation_attempts"][1]["status"] == "VALIDATED"
    correction = completions.calls[1]["messages"][0]["content"]
    assert '"c1"' in correction and '"c2"' in correction
    assert "docs/m2.1-routing.md#AGENT" in correction


def test_judge_never_returns_or_scores_invalid_output_after_retry_exhaustion():
    ids = ["c1"]
    case = {"scoreability": "SCORABLE_CANDIDATE", "user_query": "q",
        "required_conditions": [{"condition_id": "c1", "required": True}]}
    result = {"answer_kind": "POLICY_ANSWER", "answer": "answer", "evidence": [{"chunk_id": "p/one"}]}
    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(_judge_payload([]))))], model="judge-model",
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5}))
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    budget = StageBudget(max_new_requests=2, max_new_cost_usd=.30)
    with pytest.raises(JudgeOutputValidationError) as exc:
        judge_one(SimpleNamespace(model="judge-model"), budget,
            NodeConfig("judge", "test", "disabled", 1000, 10, .05), client, case, result, "blind")
    assert exc.value.code == "JUDGE_REQUIRED_CONDITION_COVERAGE_MISMATCH"
    assert len(exc.value.attempts) == 2 and budget.requests == 2


def test_offline_review_inputs_keep_all_eight_items_pending_and_three_failures_unscored():
    root = Path(__file__).parents[1]
    review = json.loads((root / "eval/fidelity/m2.4-q-human-adjudication-r1.json").read_text(encoding="utf-8"))
    assert len(review["judge_programmatic_conflicts"]) == 5
    assert all(row["review_status"] == "PENDING_HUMAN_REVIEW" for row in review["judge_programmatic_conflicts"])
    old = json.loads((root / "eval/fidelity/m2.3-multi-real-answer-review-candidate-r1.json").read_text(encoding="utf-8"))
    assert all(row["review_status"] == "PENDING_HUMAN_REVIEW" for row in old["claims"])
    report = json.loads((root / "docs/probes/m2.4-quality-report-r5.json").read_text(encoding="utf-8"))
    failures = [row for row in report["primary_results"] if row["arm"] == "self_rag_max_3_hops"
                and row["answer_kind"] != "POLICY_ANSWER"]
    assert len(failures) == 3
    assert {row["answer_kind"] for row in failures} == {"CLARIFICATION_REQUIRED", "GENERATION_UNAVAILABLE"}
    assert all(row["judge_is_scored"] is False for row in failures)


def test_behavior_only_judge_requires_explicit_behavior_findings():
    case = {"scoreability": "UNSCORABLE_CANDIDATE", "required_conditions": []}
    with pytest.raises(ValueError, match="behavior-only"):
        validate_judge({"claims": [], "required_condition_coverage": [], "overall": "PASS"}, case, set())
    validate_judge({"claims": [], "required_condition_coverage": [],
        "behavior_findings": [{"criterion": "clarify", "pass": True}], "overall": "PASS"}, case, set())


def test_small_sample_interval_helpers_are_defined():
    assert _wilson(6, 6)[0] < 1.0 and _wilson(6, 6)[1] == 1.0
    assert _paired_bootstrap([1.0, 0.0, -1.0], seed=7) == _paired_bootstrap([1.0, 0.0, -1.0], seed=7)


def test_aggregate_skips_structurally_rejected_judge_record():
    summary = aggregate([], [{"case_id": "c", "arm": "single_hop_bm25", "status": "FAILED"}], {})
    assert summary["scoreable_metrics_are_judge_auxiliary_not_human_gold"] is True


def test_aggregate_counts_generation_guard_failures_but_never_scores_them_as_answers():
    result = {"case_id": "c", "arm": "single_hop_bm25", "status": "COMPLETED",
        "replicate": None, "scoreability": "SCORABLE_CANDIDATE",
        "answer_kind": "GENERATION_UNAVAILABLE", "end_to_end_elapsed_ms": 100}
    judge = {"case_id": "c", "arm": "single_hop_bm25", "status": "COMPLETED",
        "judgment": {"required_condition_coverage": [{"covered": True, "supported": True}], "claims": []}}
    summary = aggregate([result], [judge], {"c": {"scoreability": "SCORABLE_CANDIDATE"}})
    row = summary["arms"]["single_hop_bm25"]
    assert row["attempted_scoreable_generation_count"] == 1
    assert row["generation_unavailable_count"] == 1
    assert row["scoreable_answer_count"] == 0
    assert row["required_condition_coverage_judge"]["denominator"] == 0


def test_aggregate_never_scores_unknown_grade_clarification_as_answer_quality():
    result = {"case_id": "c", "arm": "self_rag_max_3_hops", "status": "COMPLETED",
        "replicate": None, "scoreability": "SCORABLE_CANDIDATE",
        "answer_kind": "CLARIFICATION_REQUIRED", "stop_reason": "UNKNOWN_GRADE",
        "end_to_end_elapsed_ms": 100}
    judge = {"case_id": "c", "arm": "self_rag_max_3_hops", "status": "COMPLETED",
        "judgment": {"required_condition_coverage": [{"covered": True, "supported": True}], "claims": []}}
    summary = aggregate([result], [judge], {"c": {"scoreability": "SCORABLE_CANDIDATE"}})
    row = summary["arms"]["self_rag_max_3_hops"]
    assert row["answer_outcomes"] == {"CLARIFICATION_REQUIRED": 1}
    assert row["scoreable_answer_count"] == 0
    assert row["required_condition_coverage_judge"]["denominator"] == 0
