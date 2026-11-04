from pathlib import Path

import pytest

from eval.m6_1.smoke_scoring import score_inputs


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r1-20261028"


def test_archived_scoring_recomputes_primary_and_preserves_supplemental_attempts():
    result = score_inputs(ROOT, PACKAGE / "scoring-inputs-r1.json",
        PACKAGE / "provisional-semantic-adjudication-r1.json")
    assert result["recomputation"]["primary_case_count"] == 8
    assert result["recomputation"]["same_binding_aggregate_claimed"] is False
    assert result["metrics"]["mixed_binding_task_success"]["status"] == "NOT_AGGREGATED_DIFFERENT_BINDINGS"
    assert result["metrics"]["primary_execution"]["original_primary_manifest_not_run"] == 1
    assert any(row["case_id"] == "D1-POLICY-014" and row["attempt_id"] == "r3-success-supplement"
        for row in result["supplemental_attempts"])
    assert result["human_review"] == {"status": "PENDING_HUMAN_REVIEW", "reviewer": None,
        "reviewed_at_utc": None, "decision": None}
    p014 = next(row for row in result["primary_cases"] if row["case_id"] == "D1-POLICY-014")
    safety_r3 = next(row for row in result["primary_cases"] if row["case_id"] == "D1-SAFETY-038")
    safety_r1 = next(row for row in result["supplemental_attempts"]
        if row["attempt_id"] == "scoring-r1-real-trial")
    assert p014["failure_class"] == "EVIDENCE_INSUFFICIENT"
    assert p014["code_checks"]["budget_checks"] == "PASS"
    assert p014["code_checks"]["budget_recompute"]["legacy_check_status"] == "UNKNOWN"
    assert safety_r3["failure_class"] == "EVALUATOR_FAILURE"
    assert safety_r1["failure_class"] == "EVALUATOR_FAILURE"


def test_scoring_rejects_archive_hash_mismatch(tmp_path):
    import json

    inputs = json.loads((PACKAGE / "scoring-inputs-r1.json").read_text(encoding="utf-8"))
    # Make an isolated tiny package with the original source archive and report;
    # preserve the fixed rubric path because it is part of the scoring contract.
    source_archive = ROOT / inputs["attempts"][0]["archive_path"]
    source_report = ROOT / inputs["attempts"][0]["report_path"]
    archive = tmp_path / "archive.json"
    archive.write_bytes(source_archive.read_bytes() + b" ")
    report = tmp_path / "report.json"
    report.write_bytes(source_report.read_bytes())
    inputs["attempts"] = [{**inputs["attempts"][0], "archive_path": "archive.json",
        "report_path": "report.json"}]
    inputs["primary_case_order"] = ["D1-PRODUCT-001"]
    input_path = tmp_path / "inputs.json"
    input_path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="ARCHIVE_HASH_MISMATCH"):
        score_inputs(ROOT, input_path, PACKAGE / "provisional-semantic-adjudication-r1.json")
