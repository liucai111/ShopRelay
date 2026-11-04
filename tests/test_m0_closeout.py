import csv
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.freeze_policy_retrieval_dataset import freeze


@pytest.fixture
def tmp_path():
    class TempFiles:
        def __init__(self):
            self.prefix = ".m0-closeout-test-" + uuid.uuid4().hex
            self.paths = []
        def __truediv__(self, name):
            path = Path(__file__).parent / f"{self.prefix}-{name}"
            self.paths.append(path)
            return path
        def cleanup(self):
            for path in set(self.paths):
                if path.is_file():
                    path.unlink()
                elif path.is_dir() and path.parent == Path(__file__).parent and path.name.startswith(self.prefix):
                    shutil.rmtree(path)
    files = TempFiles()
    yield files
    files.cleanup()


def _fixture(tmp_path):
    reviewed_at = "2026-10-19T05:40:17Z"
    case = {
        "case_id": "fixture-1", "dataset_split": "test_candidate", "review_status": "APPROVED",
        "reviewed_at": reviewed_at, "required_conditions": [{"condition_id": "c1", "text": "policy condition", "required": True}],
        "acceptable_evidence_ids": ["p1"],
    }
    payload = {"dataset_version": "fixture", "cases": [case]}
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(payload), encoding="utf-8")
    ds_sha = hashlib.sha256(dataset.read_bytes()).hexdigest()
    case_sha = hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps({"dataset_sha256": ds_sha, "blind_id_map": {"blind-x": {"case_id": "fixture-1", "case_sha256": case_sha}}}), encoding="utf-8")
    review = tmp_path / "review.csv"
    fields = ["blind_id", "dataset_sha256", "independent_required_conditions", "independent_evidence_ids", "independent_conclusion", "conclusion_review", "review_decision", "reviewer", "reviewed_at", "review_notes"]
    with review.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerow({"blind_id": "blind-x", "dataset_sha256": ds_sha,
                    "independent_required_conditions": json.dumps(case["required_conditions"]),
                    "independent_evidence_ids": json.dumps(["p1"]), "independent_conclusion": "fixture outcome",
                    "conclusion_review": "MATCH", "review_decision": "MATCH", "reviewer": "fixture-only",
                    "reviewed_at": "2026-10-20T06:00:00Z", "review_notes": "synthetic unit-test fixture"})
    return dataset, review, map_path


def test_freeze_refuses_before_24_hour_gate(tmp_path):
    dataset, review, idmap = _fixture(tmp_path)
    with pytest.raises(ValueError, match="next-day blind-review gate has not elapsed"):
        freeze(dataset, review, idmap, tmp_path / "out", now=datetime(2026, 10, 20, 4, tzinfo=timezone.utc))


def test_freeze_validates_and_writes_new_output_only(tmp_path):
    dataset, review, idmap = _fixture(tmp_path)
    out = freeze(dataset, review, idmap, tmp_path / "out", now=datetime(2026, 10, 20, 7, tzinfo=timezone.utc))
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "FROZEN_TEST_CANDIDATE_AFTER_BLIND_REVIEW"
    assert result["blind_review"]["reviewed_case_count"] == 1
    assert dataset.exists()


def test_freeze_refuses_gold_disagreement(tmp_path):
    dataset, review, idmap = _fixture(tmp_path)
    rows = list(csv.DictReader(review.open(encoding="utf-8")))
    rows[0]["independent_evidence_ids"] = "[]"
    with review.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with pytest.raises(ValueError, match="differ from reviewed gold"):
        freeze(dataset, review, idmap, tmp_path / "out", now=datetime(2026, 10, 20, 7, tzinfo=timezone.utc))


def test_freeze_requires_real_review_metadata(tmp_path):
    dataset, review, idmap = _fixture(tmp_path)
    rows = list(csv.DictReader(review.open(encoding="utf-8")))
    rows[0]["reviewer"] = ""
    with review.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with pytest.raises(ValueError, match="reviewer and review notes are required"):
        freeze(dataset, review, idmap, tmp_path / "out", now=datetime(2026, 10, 20, 7, tzinfo=timezone.utc))
