from __future__ import annotations

import csv
import hashlib
import json

import pytest

import scripts.import_m2_final_answer_review as importer


FIELDS = list(importer.READ_ONLY_FIELDS) + [
    "reviewer_decision", "reviewer", "reviewed_at", "review_rationale", "review_status",
]


def _write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path):
    source = tmp_path / "source.csv"
    review = tmp_path / "confirmed.csv"
    confirmation = tmp_path / "confirmation.json"
    source_rows = []
    review_rows = []
    for index in range(8):
        base = {field: f"base-{index}-{field}" for field in importer.READ_ONLY_FIELDS}
        base.update({"item_id": f"J{index + 1}", "case_id": f"case-{index}",
                     "dataset_split": "dev" if index < 4 else "validation"})
        source_rows.append(base)
        decision = "UNSUPPORTED" if index == 5 else "SUPPORTED"
        reviewed = {**base, "reviewer_decision": decision,
                    "reviewer": "CHAT_USER_SELF_CONFIRMED", "reviewed_at": "2026-10-21T03:36:40Z",
                    "review_rationale": ("确认 (UNSUPPORTED；相关性UNNECESSARY)" if index == 5
                                         else "确认 (SUPPORTED；相关性NECESSARY)"),
                    "review_status": "REVIEWED"}
        review_rows.append(reviewed)
    _write_csv(source, importer.READ_ONLY_FIELDS, source_rows)
    _write_csv(review, FIELDS, review_rows)
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    record = {
        "record_type": "shoprelay_chat_user_batch_review_confirmation",
        "confirmation_actor": "CHAT_USER_SELF_CONFIRMED", "identity_verification": "NOT_PERFORMED",
        "independent_blind_review": False, "test_candidate_edited": False,
        "record_written_at_utc": "2026-10-21T03:36:40Z",
        "review_time_semantics": "batch record generation time; not separate per-row review event timestamps",
        "input_original_sha256": {"m2.4-q2-human-review-r2.csv": sha(source)},
        "output_sha256": {"ShopRelay_答案审核_用户确认版_8条.csv": sha(review)},
        "answers": {"total": 8, "supported": 7, "unsupported": 1,
                    "claim_07": "supported but unnecessary", "claim_08": "supported only for M2 chat executor"},
    }
    confirmation.write_text(json.dumps(record), encoding="utf-8")
    return source, review, confirmation


def test_answer_review_import_preserves_claim_status_and_batch_time_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    source, review, confirmation = _fixture(tmp_path)
    payload = importer.build_import(source, review, confirmation)
    assert payload["status"] == "IMPORTED_ADJUDICATED_DISPUTE_SUBSET"
    assert payload["metrics"]["claim_status_counts"] == {"SUPPORTED": 7, "UNSUPPORTED": 1}
    assert payload["metrics"]["adjudicated_claim_count"] == 8
    assert payload["confirmation"]["per_item_reviewed_at_recorded"] is False
    unsupported = next(row for row in payload["adjudications"] if row["reviewer_decision"] == "UNSUPPORTED")
    assert unsupported["review_status"] == "REVIEWED"
    assert unsupported["reviewed_at"] is None
    assert unsupported["review_recorded_at_utc"] == "2026-10-21T03:36:40Z"
    assert payload["metrics"]["scope_notes"]["claim-07"] == "supported but unnecessary"


def test_answer_review_import_rejects_modified_question_or_csv_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    source, review, confirmation = _fixture(tmp_path)
    with review.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["question"] = "modified"
    _write_csv(review, FIELDS, rows)
    with pytest.raises(ValueError, match="confirmed answer CSV hash mismatch"):
        importer.build_import(source, review, confirmation)


def test_answer_review_import_rejects_unknown_status_and_per_item_time_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    source, review, confirmation = _fixture(tmp_path)
    with confirmation.open(encoding="utf-8") as stream:
        record = json.load(stream)
    record["review_time_semantics"] = "each row has an independently verified review time"
    confirmation.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="batch timestamp semantics"):
        importer.build_import(source, review, confirmation)
