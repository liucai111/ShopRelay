from __future__ import annotations

import pytest

from scripts.build_route_m2_five_way_candidate import (
    STRATEGIES,
    _make_rows,
    _review_rows,
    build,
)
from scripts.evaluate_route_m2_five_way import load_reviewed_dataset as load_for_scoring
from scripts.evaluate_route_m2_five_way import per_case_rows, score_rows
from scripts.import_route_m2_five_way_review import validate_review_row
from scripts.run_route_m2_five_way_predictions import (
    approved_cases_for_split,
    load_reviewed_dataset as load_for_prediction,
    validate_validation_lock,
)


def test_candidate_dataset_has_balanced_isolated_families_and_no_router_run():
    dataset, audit, _ = build()
    assert len(dataset["cases"]) == 100
    assert dataset["counts"]["by_strategy"] == {strategy: 20 for strategy in STRATEGIES}
    for split in ("dev", "independent_validation_candidate"):
        assert dataset["counts"]["by_split"][split] == {strategy: 10 for strategy in STRATEGIES}
    assert audit["status"] == "PASS"
    assert audit["template_family_split_leakage"] == []
    assert audit["high_similarity_cross_split"] == []
    assert audit["exact_duplicate_queries_within_candidate_set"] == {}
    assert audit["exact_duplicate_with_approved_52_row_baseline"] == []
    assert dataset["content_freeze"]["router_predictions_run"] is False
    assert dataset["real_model_calls"] == 0
    assert all(case["label_status"] == "CANDIDATE" for case in dataset["cases"])
    assert all(case["review_status"] == "PENDING_HUMAN_REVIEW" for case in dataset["cases"])
    assert all(case["reviewer"] is None and case["reviewed_at"] is None for case in dataset["cases"])


def _review_row(case, *, decision="APPROVE_AS_IS", reviewer="", reviewed_at="", rationale=""):
    row = _review_rows([case], "base-dataset-sha")[0]
    row.update({
        "reviewer_decision": decision,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "review_rationale": rationale,
    })
    return row


def test_approval_requires_actual_reviewer_time_and_rationale():
    case = _make_rows()[0]
    row = _review_row(case)
    with pytest.raises(ValueError, match="reviewer, reviewed_at, and rationale"):
        validate_review_row(row, case, "base-dataset-sha")

    row.update(reviewer="human", reviewed_at="2026-10-21T14:00:00+08:00",
               review_rationale="已核对语义和边界")
    assert validate_review_row(row, case, "base-dataset-sha") == "APPROVE_AS_IS"


def test_review_rejects_changed_question_or_candidate_label():
    case = _make_rows()[0]
    row = _review_row(case, decision="PENDING_HUMAN_REVIEW")
    row["user_query"] += "改字"
    with pytest.raises(ValueError, match="read-only field changed: user_query"):
        validate_review_row(row, case, "base-dataset-sha")


def test_review_json_list_whitespace_is_accepted_but_content_changes_are_rejected():
    import json

    case = next(row for row in _make_rows() if row["policy_aspects"])
    row = _review_row(case, decision="PENDING_HUMAN_REVIEW")
    row["policy_aspects"] = json.dumps(case["policy_aspects"], ensure_ascii=False)
    assert validate_review_row(row, case, "base-dataset-sha") == "PENDING_HUMAN_REVIEW"

    row["policy_aspects"] = json.dumps(case["policy_aspects"] + ["unapproved-change"], ensure_ascii=False)
    with pytest.raises(ValueError, match="read-only field changed: policy_aspects"):
        validate_review_row(row, case, "base-dataset-sha")

    row["policy_aspects"] = "[not-json]"
    with pytest.raises(ValueError, match="invalid JSON in read-only field: policy_aspects"):
        validate_review_row(row, case, "base-dataset-sha")


def test_review_rejects_route_change_until_new_candidate_revision():
    case = _make_rows()[0]
    row = _review_row(case, decision="APPROVE_WITH_CORRECTION", reviewer="human",
                      reviewed_at="2026-10-21T14:00:00+08:00", rationale="标签需要调整")
    row["corrected_strategy"] = "AGENT"
    row["corrected_sub_strategy"] = "AGENT+SINGLE"
    with pytest.raises(ValueError, match="route changes require NEEDS_CORRECTION"):
        validate_review_row(row, case, "base-dataset-sha")


def test_scoring_reports_five_way_metrics_and_high_risk_errors():
    cases = [
        {"case_id": "d", "expected_strategy": "DIRECT", "risk_category": None, "required_tool_capabilities": []},
        {"case_id": "s", "expected_strategy": "SINGLE", "risk_category": None, "required_tool_capabilities": ["search_knowledge"]},
        {"case_id": "m", "expected_strategy": "MULTI", "risk_category": None, "required_tool_capabilities": ["search_knowledge"]},
        {"case_id": "a", "expected_strategy": "AGENT", "risk_category": None, "required_tool_capabilities": ["get_order"]},
        {"case_id": "r", "expected_strategy": "REFUSE", "risk_category": "UNAUTHORIZED_ACCESS", "required_tool_capabilities": []},
    ]
    predictions = [
        {"case_id": "d", "predicted_strategy": "AGENT", "tool_calls": ["get_order"]},
        {"case_id": "s", "predicted_strategy": "SINGLE", "tool_calls": []},
        {"case_id": "m", "predicted_strategy": "MULTI", "tool_calls": []},
        {"case_id": "a", "predicted_strategy": "SINGLE", "tool_calls": []},
        {"case_id": "r", "predicted_strategy": "SINGLE", "tool_calls": ["get_order"]},
    ]
    result = score_rows(cases, predictions)
    assert result["sample_count"] == 5
    assert result["accuracy"] == pytest.approx(2 / 5)
    assert result["macro_f1_status"] == "ALL_FIVE_CLASSES_PRESENT"
    assert result["multi_recall"] == 1.0
    assert result["agent_false_trigger_count"] == 1
    assert result["refuse_miss_count"] == 1
    assert result["high_risk_authorization_or_secret_violation_count"] == 1
    assert result["unnecessary_tool_call_case_count"] == 2
    assert result["required_agent_tool_missing_case_count"] == 1
    assert result["tool_called_for_refuse_case_count"] == 1


def test_scoring_rejects_prediction_id_mismatch():
    cases = [{"case_id": "d", "expected_strategy": "DIRECT"}]
    with pytest.raises(ValueError, match="prediction case_id set mismatch"):
        score_rows(cases, [{"case_id": "x", "predicted_strategy": "DIRECT"}])


def test_per_case_output_joins_predictions_by_id_not_row_order():
    cases = [
        {"case_id": "a", "expected_strategy": "DIRECT"},
        {"case_id": "b", "expected_strategy": "AGENT"},
    ]
    predictions = [
        {"case_id": "b", "predicted_strategy": "AGENT"},
        {"case_id": "a", "predicted_strategy": "DIRECT"},
    ]
    rows = per_case_rows(cases, predictions)
    assert [(row["case_id"], row["predicted_strategy"]) for row in rows] == [("a", "DIRECT"), ("b", "AGENT")]


def test_import_creates_versioned_output_without_auto_approving():
    import csv
    import json
    import uuid
    from pathlib import Path

    from scripts import import_route_m2_five_way_review as importer

    root = Path(__file__).resolve().parents[1]
    token = uuid.uuid4().hex
    source = root / "eval/dataset" / f"route-import-test-{token}.json"
    review_csv = root / "eval/review" / f"route-import-test-{token}.csv"
    output = root / "eval/dataset/reviewed" / f"route-m2.2-five-way-reviewed-{token}.json"
    try:
        dataset, _, review_rows = build()
        source.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
        rows = [dict(row) for row in review_rows]
        rows[0].update({
            "reviewer_decision": "APPROVE_AS_IS",
            "reviewer": "Reviewer A",
            "reviewed_at": "2026-10-21T14:00:00+08:00",
            "review_rationale": "核对问题语义、工具边界及预期路由。",
        })
        with review_csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        result = importer.import_reviews(source, review_csv, output)
        reviewed = json.loads(output.read_text(encoding="utf-8"))
        assert reviewed["dataset_version"] == output.stem
        assert reviewed["parent_dataset_version"] == dataset["dataset_version"]
        assert load_for_scoring(output)["dataset_sha256_canonical_cases"] == reviewed["dataset_sha256_canonical_cases"]
        assert load_for_prediction(output)["dataset_sha256_canonical_cases"] == reviewed["dataset_sha256_canonical_cases"]
        approved = [row for row in reviewed["cases"] if row["review_status"] == "APPROVED"]
        assert len(approved) == 1
        assert approved[0]["reviewer"] == "Reviewer A"
        assert approved[0]["reviewed_at"] == "2026-10-21T14:00:00+08:00"
        assert approved[0]["review_history"][-1]["before"]["review_status"] == "PENDING_HUMAN_REVIEW"
        assert approved[0]["review_history"][-1]["after"]["review_status"] == "APPROVED"
        assert result["approved_count"] == 1
        assert result["test_candidate_rows_included"] is False
    finally:
        for path in (source, review_csv, output):
            if path.exists():
                path.unlink()


def test_prediction_runner_uses_only_approved_rows_with_real_review_metadata():
    cases = [
        {"case_id": "pending", "dataset_split": "dev", "review_status": "PENDING_HUMAN_REVIEW"},
        {"case_id": "approved", "dataset_split": "dev", "review_status": "APPROVED",
         "reviewer": "Reviewer A", "reviewed_at": "2026-10-21T14:00:00+08:00",
         "review_rationale": "已核对标签"},
    ]
    assert [row["case_id"] for row in approved_cases_for_split({"cases": cases}, "dev")] == ["approved"]
    with pytest.raises(ValueError, match="no APPROVED cases"):
        approved_cases_for_split({"cases": cases[:1]}, "dev")
    cases[1]["reviewer"] = ""
    with pytest.raises(ValueError, match="lack real review metadata"):
        approved_cases_for_split({"cases": cases}, "dev")


def test_prediction_runner_requires_lock_confirmation_for_validation():
    with pytest.raises(ValueError, match="explicit lock confirmation"):
        validate_validation_lock(
            "independent_validation_candidate", locked_commit=None,
            expected_configuration_sha256=None, confirm_locked_router=False,
        )
