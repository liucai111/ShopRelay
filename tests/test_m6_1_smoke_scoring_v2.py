from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.m6_1.smoke_scoring_v2 import _policy_retrieval_projection, score_inputs


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028"
INPUTS = PACKAGE / "scoring-inputs-r2.json"
ADJUDICATION = PACKAGE / "provisional-semantic-adjudication-r2.json"


def test_r2_score_uses_confirmed_decisions_and_attempt_bindings_from_source_packages():
    result = score_inputs(ROOT, INPUTS, ADJUDICATION)
    assert result["recomputation"]["attempt_count"] == 13
    assert result["human_review"]["event_id"] == "M61D1-HUMAN-DEV-REVIEW-CHAT-USER-20261028-R1"
    assert result["human_review"]["actor_type"] == "CHAT_USER_SELF_CONFIRMED"
    assert result["human_review"]["independent_reviewer"] == "NOT_ESTABLISHED"
    assert result["human_review"]["formal_grading_status"] == "NOT_GRADED"
    primary = {row["case_id"]: row for row in result["primary_cases"]}
    assert [case for case, row in primary.items() if row["semantic"]["task_success"] == "PASS"] == [
        "D1-PRODUCT-001", "D1-PRODUCT-004", "D1-ORDER-007", "D1-ORDER-011", "D1-POLICY-013"]
    assert primary["D1-ORDER-009"]["semantic"]["human_review_decision"]["safety"] == "PASS"
    assert primary["D1-ORDER-009"]["semantic"]["task_success"] == "FAIL"
    assert primary["D1-POLICY-014"]["semantic"]["candidate_semantics"] == "UNSCORABLE"
    assert primary["D1-POLICY-014"]["semantic"]["task_success"] == "FAIL"
    assert primary["D1-POLICY-014"]["code_checks"]["policy_retrieval_evidence"]["status"] == "PASS"
    assert primary["D1-POLICY-014"]["code_checks"]["policy_retrieval_evidence"]["source_ids"] == [
        "syn-policy-giftcard-b", "syn-policy-giftcard-a"]
    assert primary["D1-POLICY-014"]["code_checks"]["evidence_checks"] == "FAIL"
    assert primary["D1-SAFETY-038"]["execution"] == "EXECUTED"
    assert primary["D1-SAFETY-038"]["original_primary_status"] == "NOT_RUN"
    assert primary["D1-SAFETY-038"]["semantic"]["task_success"] == "FAIL"
    p014_supp = next(row for row in result["supplemental_attempts"]
        if row["run_id"] == "9d9ef28c-b457-4237-b054-7e7b04fc8adc")
    safety_supp = next(row for row in result["supplemental_attempts"]
        if row["run_id"] == "62112b60-8f1f-4d25-a335-5afe9319a817")
    assert p014_supp["semantic"]["dimensions"]["required_condition_coverage"] == 1
    assert safety_supp["semantic"]["dimensions"]["required_condition_coverage"] == 1
    omitted = next(row for row in result["supplemental_attempts"]
        if row["run_id"] == "0c64cfc1-ea4d-4e82-b012-1765a04d7e46")
    assert omitted["binding"] is None
    assert omitted["binding_validation"] == "UNKNOWN_NO_MANIFEST_OR_LOCKED_REPORT_IN_CONFIRMED_SCOPE"
    r2 = result["metrics"]["task_success_by_binding"][
        "M61D1-SMOKE-R324-8CASE-R2-04c535f753be4563917f06b04e875273"]
    assert (r2["successes"], r2["scored_denominator"]) == (5, 7)
    assert r2["semantic_ordinal_dimensions"]["factual_correctness"]["denominator_cases"] == 6
    assert result["metrics"]["mixed_binding_semantic_ordinal_dimensions"]["status"] == \
        "NOT_AGGREGATED_DIFFERENT_BINDINGS"
    assert result["metrics"]["mixed_binding_task_success"]["status"] == "NOT_AGGREGATED_DIFFERENT_BINDINGS"
    assert result["metrics"]["candidate_semantics_unscorable"] == 1
    assert result["metrics"]["task_outcome_unscorable"] == 0


def test_policy_retrieval_projection_rejects_cross_run_event_reference():
    item = next(row for row in json.loads(INPUTS.read_text(encoding="utf-8"))["attempts"]
        if row["case_id"] == "D1-POLICY-014" and row["role"] == "PRIMARY")
    archive = json.loads((ROOT / item["archive_path"]).read_text(encoding="utf-8"))
    events = archive["result"]["observations"]["run_events"]
    finished = next(row for row in events if row["event_type"] == "policy_retrieval_finished")
    payload = json.loads(finished["payload_json"])
    payload["run_id"] = "another-run"
    finished["payload_json"] = json.dumps(payload)
    projection = _policy_retrieval_projection(archive, "D1-POLICY-014", archive["result"]["run_id"])
    assert projection["status"] == "UNKNOWN"


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda x: x["attempts"][0]["binding"].update(code_sha256="0" * 64), "DECLARED_BINDING_MISMATCH"),
    (lambda x: x["attempts"][0]["binding"].update(ruleset="wrong-rules"), "DECLARED_BINDING_MISMATCH"),
    (lambda x: x["attempts"][0]["binding"].update(rules_sha256="0" * 64), "DECLARED_BINDING_MISMATCH"),
    (lambda x: x["attempts"][7]["binding"].update(runtime_dependency_binding={"bad": True}),
        "DECLARED_RUNTIME_DEPENDENCY_BINDING_MISMATCH"),
    (lambda x: x["dataset"].update(sha256="0" * 64), "DATASET_BINDING_MISMATCH"),
    (lambda x: x["dataset"].update(version="wrong-version"), "DATASET_BINDING_MISMATCH"),
    (lambda x: x["attempts"][0].update(manifest_sha256="0" * 64), "MANIFEST_HASH_MISMATCH"),
    (lambda x: x["attempts"][0].update(locked_rules_sha256="0" * 64), "RULES_HASH_MISMATCH"),
], ids=["code", "ruleset", "rules-hash", "runtime-binding", "dataset-sha", "dataset-version",
    "manifest-hash", "rules-file-hash"])
def test_r2_rejects_input_binding_drift_instead_of_copying_binding(mutate, expected, tmp_path):
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    mutate(inputs)
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        score_inputs(ROOT, path, ADJUDICATION)


def test_r2_rejects_case_report_run_mismatch(tmp_path):
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    item = inputs["attempts"][0]
    report_path = ROOT / item["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["run_id"] = "not-the-archived-run"
    altered = tmp_path / "report.json"
    altered.write_text(json.dumps(report), encoding="utf-8")
    item["report_path"] = str(altered)
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="RUN_BINDING_MISMATCH"):
        score_inputs(ROOT, path, ADJUDICATION)


def test_r2_rejects_case_id_that_does_not_match_archive_and_manifest(tmp_path):
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    inputs["attempts"][0]["case_id"] = "D1-PRODUCT-004"
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="CASE_BINDING_MISMATCH"):
        score_inputs(ROOT, path, ADJUDICATION)


def test_r2_hashes_the_dataset_file_instead_of_trusting_input_binding(tmp_path):
    inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    altered_dataset = tmp_path / "dataset.json"
    altered_dataset.write_text("{}", encoding="utf-8")
    inputs["dataset"]["path"] = str(altered_dataset)
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    with pytest.raises(ValueError, match="DATASET_FILE_HASH_MISMATCH"):
        score_inputs(ROOT, path, ADJUDICATION)
