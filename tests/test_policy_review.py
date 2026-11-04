import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.policy_review import (
    CSV_FIELDS,
    export_review_csv,
    import_reviews,
)
from scripts.build_policy_retrieval_dataset import assert_outputs_new
from scripts.validate_policy_retrieval_dataset import audit as audit_policy_dataset, POLICY_FILE


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATASET = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
R3_DATASET = ROOT / "eval/dataset/policy-retrieval-v2-candidate-20261019-r3.json"
FINAL_DATASET = ROOT / "eval/dataset/policy-retrieval-v2-final-candidate-20261019-r7.json"


@pytest.fixture
def tmp_path():
    # Use direct files in this existing writable directory; managed Windows denies
    # traversal of newly created temporary directories.
    class TempFiles:
        def __init__(self):
            self.prefix = ".policy-review-test-" + uuid.uuid4().hex
            self.paths = []

        def __truediv__(self, name):
            path = Path(__file__).parent / f"{self.prefix}-{name}"
            self.paths.append(path)
            return path

        def track(self, path):
            self.paths.append(path)

        def cleanup(self):
            for path in set(self.paths):
                if path.is_file():
                    path.unlink()

    files = TempFiles()
    yield files
    files.cleanup()


def load_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_rows(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_csv_export_has_identity_time_and_case_fingerprint(tmp_path):
    output = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, output)
    rows = load_rows(output)
    assert len(rows) == 112
    assert rows[0]["base_dataset_sha256"]
    assert rows[0]["base_case_sha256"]
    assert "reviewer" in rows[0] and "reviewed_at" in rows[0]
    assert rows[0]["review_status"] == "PENDING_REVIEW"
    assert rows[0]["evidence_source_types"] == ""


def test_dataset_validator_accepts_reviewed_status_without_candidate_warning():
    payload = json.loads(FINAL_DATASET.read_text(encoding="utf-8"))
    payload["cases"][0]["review_status"] = "APPROVED"
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    report = audit_policy_dataset(payload, policies)
    assert not [item for item in report["warnings"] if item["type"] == "unexpected_review_status"]


def test_r3_export_uses_versioned_source_suggestions_and_imports_to_new_unfrozen_copy(tmp_path):
    review_csv = tmp_path / "r3-review.csv"
    export_review_csv(R3_DATASET, review_csv)
    rows = load_rows(review_csv)
    delivery = next(row for row in rows if row["case_id"] == "pr2-delivery_fact_boundary-01")
    suggestions = json.loads(delivery["condition_source_suggestions_READ_ONLY"])
    assert set(suggestions.values()) == {"POLICY"}
    row = next(row for row in rows if row["case_id"] == "pr2-multi_clause_return_refund-04")
    row["review_status"] = "NEEDS_CORRECTION"
    row["reviewer"] = "Temporary test reviewer"
    row["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    row["review_notes"] = "Isolated importer test only; not a real approval."
    conditions = json.loads(row["required_conditions"])
    row["evidence_source_types"] = json.dumps({item["condition_id"]: "USER_PROVIDED" if item["condition_id"] in ("c1", "c2") else "POLICY" for item in conditions})
    write_rows(review_csv, rows)
    output = import_reviews(R3_DATASET, review_csv, Path(__file__).parent)
    tmp_path.track(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    changed = next(case for case in payload["cases"] if case["case_id"] == row["case_id"])
    assert changed["review_status"] == "NEEDS_CORRECTION"
    assert len(changed["review_history"]) == 1
    assert payload["review_import"]["output_is_frozen"] is False
    assert all(case["review_status"] == "PENDING_REVIEW" for case in payload["cases"] if case["case_id"] != row["case_id"])


def test_final_r7_review_csv_matches_dataset_hash_and_keeps_ai_audit_unapproved(tmp_path):
    review_csv = tmp_path / "final-r7.csv"
    export_review_csv(FINAL_DATASET, review_csv)
    rows = load_rows(review_csv)
    assert len(rows) == 112
    assert {row["base_dataset_sha256"] for row in rows} == {__import__("hashlib").sha256(FINAL_DATASET.read_bytes()).hexdigest()}
    assert all(row["review_status"] == "PENDING_REVIEW" and not row["reviewer"] and not row["reviewed_at"] for row in rows)
    delivery = next(row for row in rows if row["case_id"] == "pr2-delivery_policy_limits-01")
    assert json.loads(delivery["condition_source_suggestions_READ_ONLY"]) == {"c1": "POLICY", "c2": "POLICY", "c3": "BUSINESS_TOOL"}
    target = next(row for row in rows if row["case_id"] == "pr2-refund_delivered_return-01")
    target["review_status"] = "NEEDS_CORRECTION"
    target["reviewer"] = "Temporary test reviewer"
    target["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    target["review_notes"] = "Isolated final-dataset importer test only."
    write_rows(review_csv, rows)
    output = import_reviews(FINAL_DATASET, review_csv, Path(__file__).parent)
    tmp_path.track(output)
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["status"] == "CANDIDATE_REVIEWED_NOT_FROZEN"
    assert imported["review_import"]["output_is_frozen"] is False
    assert next(c for c in imported["cases"] if c["case_id"] == target["case_id"])["review_status"] == "NEEDS_CORRECTION"
    assert sum(c["review_status"] == "APPROVED" for c in imported["cases"]) == 0


def test_candidate_builder_refuses_to_overwrite_existing_artifacts(tmp_path):
    existing = tmp_path / "candidate.json"
    existing.write_text("historical", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        assert_outputs_new(existing, tmp_path / "review.csv")
    assert existing.read_text(encoding="utf-8") == "historical"


def test_approved_import_requires_review_identity_timestamp_reason_and_source_map(tmp_path):
    original_bytes = SOURCE_DATASET.read_bytes()
    review_csv = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    row = rows[0]
    row["review_status"] = "APPROVED"
    row["reviewer"] = "Human reviewer"
    row["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    row["review_notes"] = "Verified source policy and condition mapping."
    conds = json.loads(row["required_conditions"])
    row["evidence_source_types"] = json.dumps({item["condition_id"]: "USER_PROVIDED" for item in conds})
    target_case_id = row["case_id"]
    rows.reverse()  # Matching must be by case_id, never CSV row position.
    write_rows(review_csv, rows)

    output = import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)
    tmp_path.track(output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    approved = next(case for case in payload["cases"] if case["case_id"] == target_case_id)
    assert approved["review_status"] == "APPROVED"
    assert approved["reviewer"] == "Human reviewer"
    assert approved["evidence_source_types"]
    assert len(approved["review_history"]) == 1
    event = approved["review_history"][0]
    assert event["changes"]["review_status"]["before"] == "PENDING_REVIEW"
    assert event["changes"]["review_status"]["after"] == "APPROVED"
    assert event["reason"] == "Verified source policy and condition mapping."
    assert payload["status"] == "CANDIDATE_REVIEWED_NOT_FROZEN"
    assert [case["case_id"] for case in payload["cases"]] == [case["case_id"] for case in json.loads(original_bytes)["cases"]]
    assert SOURCE_DATASET.read_bytes() == original_bytes


@pytest.mark.parametrize("missing", ["reviewer", "reviewed_at", "review_notes", "evidence_source_types"])
def test_approved_import_rejects_missing_human_fields(tmp_path, missing):
    review_csv = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    row = rows[0]
    row["review_status"] = "APPROVED"
    row["reviewer"] = "Human reviewer"
    row["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    row["review_notes"] = "Verified."
    conds = json.loads(row["required_conditions"])
    row["evidence_source_types"] = json.dumps({item["condition_id"]: "POLICY" for item in conds})
    row[missing] = ""
    write_rows(review_csv, rows)
    with pytest.raises(ValueError):
        import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)


def test_substantive_edit_cannot_be_imported_as_unreviewed(tmp_path):
    review_csv = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    rows[0]["expected_conclusion"] = "Changed without review metadata"
    write_rows(review_csv, rows)
    with pytest.raises(ValueError, match="any annotation/review change requires"):
        import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)


def test_csv_json_conflicts_and_duplicate_case_ids_are_rejected(tmp_path):
    review_csv = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    rows[0]["base_case_sha256"] = "stale"
    write_rows(review_csv, rows)
    with pytest.raises(ValueError, match="base_case_sha256"):
        import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)

    export_review_csv(SOURCE_DATASET, tmp_path / "other.csv")
    duplicate_rows = load_rows(tmp_path / "other.csv")
    duplicate_rows[1]["case_id"] = duplicate_rows[0]["case_id"]
    write_rows(tmp_path / "other.csv", duplicate_rows)
    with pytest.raises(ValueError, match="duplicate case_id"):
        import_reviews(SOURCE_DATASET, tmp_path / "other.csv", Path(__file__).parent)


@pytest.mark.parametrize("change, message", [("extra", "CSV/JSON case_id conflict"), ("missing", "CSV/JSON case_id conflict")])
def test_csv_rejects_unknown_or_missing_case_ids(tmp_path, change, message):
    review_csv = tmp_path / f"case-id-{change}.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    if change == "extra":
        rows[0]["case_id"] = "not-in-source-dataset"
    else:
        rows.pop()
    write_rows(review_csv, rows)
    with pytest.raises(ValueError, match=message):
        import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)


def test_bad_evidence_id_is_revalidated_and_import_is_idempotent(tmp_path):
    review_csv = tmp_path / "review.csv"
    export_review_csv(SOURCE_DATASET, review_csv)
    rows = load_rows(review_csv)
    row = rows[0]
    row["review_status"] = "NEEDS_CORRECTION"
    row["reviewer"] = "Human reviewer"
    row["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    row["review_notes"] = "Checking evidence change."
    row["acceptable_evidence_ids"] = json.dumps(["policies/does-not-exist@x#bad"])
    write_rows(review_csv, rows)
    with pytest.raises(ValueError, match="post-import dataset validation failed"):
        import_reviews(SOURCE_DATASET, review_csv, Path(__file__).parent)

    # Make a valid correction-only review import and repeat the same byte-identical CSV.
    export_review_csv(SOURCE_DATASET, tmp_path / "idempotent.csv")
    rows = load_rows(tmp_path / "idempotent.csv")
    rows[0]["review_status"] = "NEEDS_CORRECTION"
    rows[0]["reviewer"] = "Human reviewer"
    rows[0]["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    rows[0]["review_notes"] = "Please inspect the condition mapping."
    write_rows(tmp_path / "idempotent.csv", rows)
    first = import_reviews(SOURCE_DATASET, tmp_path / "idempotent.csv", Path(__file__).parent)
    tmp_path.track(first)
    second = import_reviews(SOURCE_DATASET, tmp_path / "idempotent.csv", Path(__file__).parent)
    assert first == second
    result = json.loads(first.read_text(encoding="utf-8"))
    assert len(result["cases"][0]["review_history"]) == 1
