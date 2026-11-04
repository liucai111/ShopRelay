import json
from pathlib import Path

from scripts.recompute_m6_2_dev_score_review_closeout_r1 import (
    CASE_IDS,
    OUT,
    build_gap_matrix,
    build_package,
    build_reference_candidates,
)


def test_review_package_is_eight_bound_items_with_no_fabricated_review_fields():
    package = build_package()
    ids = [item["item_id"] for item in package["items"]]
    assert ids == [
        *CASE_IDS,
        "ANSWER-J3",
        "ANSWER-claim-04a",
        "D1-CROSSAGENT-030",
    ]
    for item in package["items"]:
        assert item["reviewer"] is None
        assert item["reviewed_at_utc"] is None
        assert item["reviewer_decision"] is None
        if item["item_type"].startswith("personal_dev_"):
            assert item["archive_sha256"]
        else:
            assert item["source_bindings"]["frozen_sample_sha256"]
        assert item["question"]
        assert item.get("actual_delivered_answer") or item.get("answer_or_claim")
    assert package["human_decisions_written"] is False
    for item in package["items"]:
        if item["item_type"] == "personal_dev_full_case":
            assert item["archive_path"] in package["source_bindings"]
        elif item["item_type"] == "confirmed_reference_judge_disagreement":
            assert item["judge_input_completeness"]["historical_request_matches_frozen_payload"] is True
            assert item["source_bindings"]["confirmation_event_source_sha256"] == item["source_bindings"]["confirmation_event_sha256"]
            assert item["source_bindings"]["decision_source_path"] in package["source_bindings"]
            assert item["source_bindings"]["original_csv_path"] in package["source_bindings"]
    assert "docs/eval/m6.2-crossagent030-r1-20261029/reports/execution-summary-r1.json" in package["source_bindings"]
    assert "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-independent-recompute-r1.json" in package["source_bindings"]
    assert "ShopRelay_Agent_Eval_完整评测方案.md" in package["source_bindings"]
    assert "项目规格/evaluation-spec.md" in package["source_bindings"]
    assert "docs/eval/m6.2-judge-calibration-r4-20261029/closeout-r4.md" in package["source_bindings"]
    assert "docs/eval/m6.2-judge-calibration-r4-20261029/SHA256SUMS-closeout-r4-v2.md" in package["source_bindings"]


def test_j3_corrected_reason_and_claim_only_issue_are_separate():
    package = build_package()
    items = {item["item_id"]: item for item in package["items"]}
    j3 = items["ANSWER-J3"]
    j3_source = " ".join(source["text"] for source in j3["sources"])
    assert "未影响二次销售" in j3_source
    assert "未开封" not in j3_source
    assert j3["existing_confirmed_label"] == "PARTIAL"
    assert j3["historical_judge_result"]["decision"] == "PASS"
    claim = items["ANSWER-claim-04a"]
    assert claim["scope"] == "claim_only"
    assert claim["existing_confirmed_label"] == "INSUFFICIENT_EVIDENCE"
    assert claim["historical_judge_result"]["decision"] == "PARTIAL"
    assert "claim-only" in claim["ai_pre_review"]["corrected_reason"]
    assert "exceeded claim-only scope" in claim["ai_pre_review"]["corrected_reason"]


def test_crossagent030_outcome_pre_review_separates_technical_and_task_completeness():
    ca = build_package()["crossagent030_outcome_pre_review"]
    assert ca["run_lifecycle"]["technical_recommendation"] == "PASS"
    assert ca["specialist_collaboration"]["recommendation"] == "PASS_FOR_OBSERVED_EXECUTION_ONLY"
    assert ca["specialist_collaboration"]["provider_requests"] == 0
    assert ca["business_outcome_followup"]["recommendation"] == "PARTIAL"
    assert ca["business_outcome_followup"]["answer_explicitly_requests_user_to_supply_missing_fact"] is False
    assert ca["eligibility_not_inferred"]["structured_status"] == "UNKNOWN"
    assert ca["permissions_usage_cleanup"]["read_only_calls"] == ["get_order", "get_order_items", "get_warranty"]
    assert ca["permissions_usage_cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED"


def test_judge_candidate_denominators_and_freeze_gaps_do_not_overclaim():
    package = build_package()
    references = build_reference_candidates(package)
    assert references["confirmed_reference_count"] == 8
    assert references["confirmed_label_counts"] == {
        "PASS": 4,
        "PARTIAL": 3,
        "FAIL": 0,
        "INSUFFICIENT_EVIDENCE": 1,
    }
    assert references["coverage"]["false_accept_denominator"].startswith("N/A")
    assert references["judge_calls_this_round"] == 0
    assert references["coverage"]["all_eight_historical_judge_inputs_have_question_answer_sources_scope_and_scoring_target"] is True
    assert references["coverage"]["all_confirmed_source_hashes_match_frozen_samples"] is True
    for ref in references["confirmed_references"]:
        assert ref["judge_input_completeness"]["complete_for_historical_objective"] is True
        assert ref["judge_input_completeness"]["request_matches_frozen_payload_hash"] is True
        assert ref["confirmed_source_bindings"]["confirmation_event_sha256"]
        assert ref["confirmed_source_bindings"]["decision_source_sha256"]
        assert ref["confirmed_source_bindings"]["original_csv_sha256"]
    assert references["pending_candidate_categories"]["pending_task_completeness_negative_only"] == ["D1-PRODUCT-003"]
    assert references["pending_candidate_categories"]["execution_or_delivery_failure_in_this_reviewed_set"] == []
    gaps = build_gap_matrix(package, references)
    assert gaps["m6_1_status"] == "COMPLETE"
    assert gaps["m6_2_status"] == "IN_PROGRESS"
    assert gaps["formal_gate_snapshot"]["gold"] == "NOT_APPROVED"
    assert gaps["dimensions"][0]["current_model_predictions_scored"] == 0
    assert gaps["multi_evidence_limit"].startswith("One deterministic-rule")


def test_j3_and_claim_dispute_records_carry_exact_frozen_judge_inputs():
    package = build_package()
    items = {item["item_id"]: item for item in package["items"]}
    for item_id in ("ANSWER-J3", "ANSWER-claim-04a"):
        item = items[item_id]
        assert item["judge_input_completeness"]["historical_request_matches_frozen_payload"] is True
        payload = item["frozen_judge_input"]
        assert payload["question"] and payload["answer_or_claim"] and payload["applicable_sources"]
        assert payload["rubric"]["scope"] == item["scope"]
        assert payload["scoring_target"]


def test_generated_review_files_are_valid_json_and_explicitly_pending():
    review = json.loads((OUT / "eight-item-review-package-r1.json").read_text(encoding="utf-8"))
    confirm = (OUT / "confirmation-list-r1.md").read_text(encoding="utf-8")
    assert len(review["items"]) == 8
    assert confirm.count("### `") == 8
    assert "reviewer_decision" in review["items"][0]
    assert "PENDING_USER_REVIEW" in confirm
