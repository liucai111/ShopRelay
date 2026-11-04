"""Build a fixed M2-R manifest from explicitly approved dev/validation cases only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
OUTPUT = ROOT / "eval/raw/m2-r-approved-dev-validation-manifest-r1.json"
CASE_IDS = (
    "pr2-refund_open_uniqueness-02",
    "pr2-refund_partial_unsupported-02",
    "pr2-refund_reason_hard_negative-02",
    "pr2-multi_clause_return_refund-03",
    "pr2-return_old_version-01",
    "pr2-warranty_general-01",
    "pr2-warranty_specific_missing-04",
)


def build() -> dict:
    source_bytes = SOURCE.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    dataset = json.loads(source_bytes)
    allow = set(CASE_IDS)
    selected = [row for row in dataset.get("cases", []) if row.get("case_id") in allow]
    by_id = {row["case_id"]: row for row in selected}
    if set(by_id) != allow or len(selected) != len(CASE_IDS):
        raise ValueError("the explicit approved-case allowlist does not match the reviewed dataset")
    cases = []
    for case_id in CASE_IDS:
        case = by_id[case_id]
        if case.get("dataset_split") not in {"dev", "validation"}:
            raise ValueError(f"case is outside dev/validation: {case_id}")
        if case.get("review_status") != "APPROVED":
            raise ValueError(f"case is not human-approved: {case_id}")
        if not isinstance(case.get("user_query"), str) or not case["user_query"].strip():
            raise ValueError(f"case has no user query: {case_id}")
        # Keep only this allowlisted case. No test_candidate rows are copied to the manifest.
        cases.append(case)
    return {
        "schema_version": "shoprelay.m2-r-approved-dev-validation-manifest.v1",
        "source_dataset_path": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "source_dataset_sha256": source_sha,
        "dataset_version": dataset.get("dataset_version"),
        "test_candidate_rows_included": False,
        "selection_policy": "fixed allowlist; approved dev/validation only; no test_candidate data copied",
        "case_ids": list(CASE_IDS),
        "cases": cases,
    }


def main() -> None:
    manifest = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    # Write exact UTF-8 bytes so hashes are stable across Windows/Linux newline rules.
    encoded_bytes = encoded.encode("utf-8")
    OUTPUT.write_bytes(encoded_bytes)
    print(json.dumps({"output": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"),
                      "sha256": hashlib.sha256(encoded_bytes).hexdigest(),
                      "case_count": len(manifest["cases"]),
                      "splits": {split: sum(row["dataset_split"] == split for row in manifest["cases"])
                                 for split in ("dev", "validation")},
                      "test_candidate_rows_included": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
