from __future__ import annotations

from scripts.derive_m6_2_r10_score_closeout import build_outputs
from scripts.finalize_m6_2_r10_closeout import build_holds, build_judge_candidates, build_retrieval


def test_r10_closeout_reconciles_four_runs_and_keeps_bindings_separate() -> None:
    result = build_outputs()
    review = result["review"]
    records = (review["historical_runs"] + review["r9s4_runs"] +
               [review["policy018_recovery_run"]] + review["r10_first_runs"])
    assert len(records) == 32
    assert len({row["run_id"] for row in records}) == 32
    assert result["coverage"]["held_or_blocked_count"] == 8
    assert len(result["coverage"]["rows"]) == 40
    assert result["closeout"]["r10_provider_requests"] == 0
    assert result["closeout"]["r10_tool_calls"] == 11
    assert result["closeout"]["r10_tokens"] == 0
    assert result["closeout"]["shared_task_ledger_after_r10"]["application_runs"] == 20
    assert all(row["actual_agent_started_events"] > 0 for row in result["r10_items"])
    assert all(row["actual_handoffs"] for row in result["r10_items"])
    assert all(row["same_run_event_trace_ids"] for row in result["r10_items"])


def test_task_delivery_failure_is_separate_from_candidate_scoring() -> None:
    result = build_outputs()
    items = result["review"]["historical_runs"] + result["review"]["r9s4_runs"]
    by_case = {row["case_id"]: row for row in items}
    policy014 = by_case["D1-POLICY-014"]["derived_score_r18"]
    safety038 = by_case["D1-SAFETY-038"]["derived_score_r18"]
    cross028 = by_case["D1-CROSSAGENT-028"]["derived_score_r18"]
    assert policy014["user_delivery"] == "FAIL"
    assert policy014["content_and_evidence_support"] == "SEPARATE_CANDIDATE_REVIEW_REQUIRED"
    assert safety038["user_delivery"] == "FAIL"
    assert safety038["content_and_evidence_support"] == "UNSCORABLE_NO_SAVED_CANDIDATE"
    assert cross028["required_tool_completion"] == "FAIL"
    assert cross028["task_outcome_recommendation"] == "FAIL"


def test_r10_metrics_include_exact_usage_and_provisional_outcome_denominator() -> None:
    result = build_outputs()
    groups = result["metrics"]["groups"]
    group = next(value for value in groups.values()
                 if value["case_ids"] == ["D1-PRODUCT-005", "D1-AFTERSALES-019",
                                          "D1-AFTERSALES-020", "D1-CROSSAGENT-032"])
    assert group["task_outcome_ai_prereview"]["counts"] == {"PASS": 1, "PARTIAL": 3}
    assert group["task_outcome_ai_prereview"]["denominator"] == 4
    assert group["known_usage_sums"] == {
        "provider_requests": 0, "tokens": 0, "local_estimated_usd": 0.0}
    assert group["route_decision_source_counts"] == {"RULE": 4}
    indicators = group["binary_program_indicators"]
    assert indicators["required_business_tool_execution"]["successes"] == 4
    assert indicators["required_business_tool_execution"]["denominator"] == 4
    assert indicators["user_visible_delivery"]["successes"] == 4


def test_remaining_hold_and_judge_register_do_not_promote_ai_suggestions() -> None:
    result = build_outputs()
    holds = build_holds()
    review = result["review"]
    judge = build_judge_candidates(review)
    assert holds["case_count"] == 8
    assert holds["hold_release_count"] == 0
    assert len(judge["candidates"]) > 0
    assert all(row["judge_eligible_now"] is False for row in judge["candidates"])
    assert all(row["human_reference_status"] == "PENDING_EXACT_RUN_REVIEW"
               for row in judge["candidates"])


def test_retrieval_worksheet_uses_observed_sources_not_citation_counts_as_ranking() -> None:
    review = build_outputs()["review"]
    worksheet = build_retrieval(review)
    assert worksheet["row_count"] >= 4
    assert worksheet["metric_status"]["recall_at_k"] == "N/A"
    assert worksheet["metric_status"]["mrr"] == "N/A"
    assert all(row["review_status"] == "PENDING_INDEPENDENT_DEV_ANNOTATION"
               for row in worksheet["rows"])
