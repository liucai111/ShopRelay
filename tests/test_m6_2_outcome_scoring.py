from __future__ import annotations

import copy

import pytest

from eval.m6_2 import outcome_scoring
from eval.m6_2.outcome_scoring import ROOT, build_confirmed_references, score_retrospective_smoke
from eval.m6_2.pilot import build_static_pilot_preflight, PILOT_CASES
from eval.m6_1.personal_smoke import M62_PILOT_PROFILE_ID, M62_PILOT_TOOL_CAPS, budget_contract as smoke_budget, locked_rules, smoke_profile


def test_confirmed_reference_bundle_has_effective_user_confirmation_and_scopes():
    bundle = build_confirmed_references(ROOT)
    state = bundle["effective_confirmation_state"]
    assert state["status"] == "USER_CONFIRMED"
    assert state["user_confirmation_required"] is False
    assert state["source_ai_pre_review_is_human_decision"] is False
    assert state["legacy_derived_file_flags_preserved_as_history"] == {
        "not_a_human_decision": True, "user_confirmation_required": True}
    assert bundle["counts"]["routes"] == {
        "total": 52, "dev": 28, "validation": 24, "retain": 49, "modify": 3}
    assert bundle["counts"]["answer_disputes"] == {
        "PASS": 4, "PARTIAL": 3, "INSUFFICIENT_EVIDENCE": 1}
    assert len(bundle["references"]["smoke_outcomes"]) == 8
    insufficient = next(r for r in bundle["references"]["answer_disputes"]
                        if r["decision"] == "INSUFFICIENT_EVIDENCE")
    assert insufficient["insufficient_evidence_is_binary_label"] is False
    assert any(r["claim_only"] for r in bundle["references"]["answer_disputes"])


def test_reference_builder_rejects_confirmation_binding_drift(monkeypatch):
    original_sha = outcome_scoring.sha256

    def mismatched_proposal_sha(path):
        if path.name == "proposed-review-decisions-r1.json":
            return "0" * 64
        return original_sha(path)

    monkeypatch.setattr(outcome_scoring, "sha256", mismatched_proposal_sha)
    with pytest.raises(ValueError, match="PRE_REVIEW_BINDING_MISMATCH"):
        build_confirmed_references(ROOT)


def test_reference_builder_rejects_single_route_label_drift_with_unchanged_counts(monkeypatch):
    original_load_json = outcome_scoring.load_json

    def drift_one_confirmed_row(path):
        value = original_load_json(path)
        if path.name == "confirmed-review-decisions-r1.json":
            value = copy.deepcopy(value)
            row = value["route_pre_reviews"][0]
            row["user_confirmed_label"] = {
                **row["user_confirmed_label"],
                "strategy": "REFUSE",
                "sub_strategy": None,
                "required_capabilities": [],
            }
        return value

    monkeypatch.setattr(outcome_scoring, "load_json", drift_one_confirmed_row)
    with pytest.raises(ValueError, match="ROUTE_CONFIRMED_DECISION_DRIFT"):
        build_confirmed_references(ROOT)


def test_retrospective_code_projection_reads_archives_and_keeps_bindings_separate():
    result = score_retrospective_smoke(ROOT)
    assert result == score_retrospective_smoke(ROOT)
    assert len(result["rows"]) == 8
    assert result["external_judge_called"] is False
    assert result["classification"].startswith("RETROSPECTIVE_PERSONAL_DEV_REASSESSMENT")
    assert len(result["summary_by_binding"]) == 2
    product = next(row for row in result["rows"] if row["case_id"] == "D1-PRODUCT-001")
    assert product["code_grader"]["run_terminal"] == "PASS"
    assert product["code_grader"]["delivered_answer_present"] is True
    refusal = next(row for row in result["rows"] if row["case_id"] == "D1-ORDER-009")
    assert refusal["code_grader"]["read_call_count"] == 0
    assert refusal["confirmed_dev_outcome"] == "FAIL_PARTIAL_CONTRACT"
    # Successful Run lifecycle is not substituted for the reviewed Task Outcome.
    assert refusal["code_grader"]["run_status"] == "SUCCEEDED"
    assert refusal["task_success_status"] == "FAIL"


def test_pilot_static_preflight_is_non_executing_and_fixture_bound():
    result = build_static_pilot_preflight(ROOT)
    assert result["status"] == "STATIC_PREFLIGHT_PASSED"
    assert result["case_count"] == 6
    assert result["provider_called"] is False
    assert result["run_created"] is False
    assert result["authorization_created_or_activated"] is False
    assert result["database_identity_schema_lease_budget"] == "NOT_CHECKED_BY_STATIC_PREFLIGHT"
    assert result["pilot_runner_profile"] == "NOT_YET_CONNECTED_TO_EXISTING_RUNNER; no real-run admission claimed"


def test_m62_fixed_runner_profile_and_budget_are_exact():
    profile = smoke_profile(M62_PILOT_PROFILE_ID)
    budget = smoke_budget(profile)
    dataset = outcome_scoring.load_json(ROOT / outcome_scoring.DATASET)
    rules = locked_rules(dataset, profile)
    assert profile.cases == PILOT_CASES
    assert profile.tool_caps == M62_PILOT_TOOL_CAPS
    assert budget["batch"]["provider_calls"] == 24
    assert budget["batch"]["tool_calls"] == 6
    assert budget["batch"]["total_tokens"] == 720000
    assert budget["batch"]["estimated_usd"] == 1.08
    assert [r["check_profile"] for r in rules["cases"]] == [
        "SYNTHETIC_PRODUCT_CAPACITY_FROM_CATALOG", "PRODUCT_RECOMMENDATION_WITH_FIT_CLARIFICATION",
        "POLICY_EFFECTIVE_INTERVAL_BOUNDED", "UNTRUSTED_INJECTION_WITH_POLICY_RETRIEVAL",
        "AUTHENTICATED_OWN_ORDER_ONLY", "REFUSAL_WITH_ZERO_BUSINESS_READS"]
    assert budget["per_case"]["D1-SAFETY-037"]["tool_calls"] == 0
    assert rules["cases_are_checker_only"] is True
    assert rules["expected_facts_in_router_or_agent"] is False
