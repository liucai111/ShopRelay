"""Import a human-reviewed CSV into a new, versioned five-way route dataset.

The importer never approves an AI label by itself. Every accepted decision must
carry the reviewer-entered identity, timestamp, and rationale. Complex route or
evidence corrections are marked NEEDS_CORRECTION and require a new candidate
revision rather than a partial in-place edit.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/dataset/route-m2.2-five-way-candidate-20261021-r1.json"
REVIEW = ROOT / "eval/review/route-m2.2-five-way-candidate-review-r1.csv"
ALLOWED_DECISIONS = {"PENDING_HUMAN_REVIEW", "APPROVE_AS_IS", "APPROVE_WITH_CORRECTION", "NEEDS_CORRECTION"}
STRATEGIES = {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}
SUB_STRATEGIES = {"DIRECT": {""}, "SINGLE": {"", "BM25"}, "MULTI": {"", "MULTI"},
                  "AGENT": {"", "AGENT+SINGLE", "AGENT+MULTI"}, "REFUSE": {""}}
EXPECTED_HEADERS = [
    "case_id", "base_dataset_sha256", "base_case_sha256", "dataset_split", "group_id",
    "user_query", "expected_strategy", "expected_sub_strategy", "expected_intent",
    "expected_reason_code", "expected_safety_decision", "expected_needs_clarification",
    "expected_clarification_question", "policy_aspects", "policy_refs", "business_rule_refs",
    "required_tool_capabilities", "expected_execution", "action_mode", "case_type", "difficulty",
    "risk_category", "candidate_rationale", "risk_notes", "ambiguity_notes", "review_status",
    "reviewer_decision", "corrected_strategy", "corrected_sub_strategy", "reviewer", "reviewed_at",
    "review_rationale",
]
LIST_FIELDS = {"policy_aspects", "policy_refs", "business_rule_refs", "required_tool_capabilities"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return sha256(canonical(value))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside_root(path: Path) -> Path:
    path = path.resolve()
    if ROOT not in path.parents:
        raise ValueError("input/output must stay inside ShopRelay")
    return path


def _time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z") if parsed.utcoffset().total_seconds() == 0 else parsed.isoformat()


def validate_review_row(row: dict[str, str], case: dict[str, Any], dataset_sha: str) -> str:
    if row.get("case_id") != case["case_id"]:
        raise ValueError("case_id mismatch")
    if row.get("base_dataset_sha256") != dataset_sha:
        raise ValueError(f"{case['case_id']}: stale base dataset hash")
    if row.get("base_case_sha256") != case.get("case_sha256"):
        raise ValueError(f"{case['case_id']}: stale base case hash")
    read_only_fields = [
        "dataset_split", "group_id", "user_query", "expected_strategy", "expected_sub_strategy",
        "expected_intent", "expected_reason_code", "expected_safety_decision",
        "expected_needs_clarification", "expected_clarification_question", "expected_execution",
        "action_mode", "case_type", "difficulty", "risk_category", "candidate_rationale",
        "risk_notes", "ambiguity_notes", "review_status",
    ]
    for field in read_only_fields:
        value = case.get(field)
        expected = "" if value is None else ("true" if value is True else "false" if value is False else str(value))
        if row.get(field, "") != expected:
            raise ValueError(f"{case['case_id']}: read-only field changed: {field}")
    for field in LIST_FIELDS:
        try:
            submitted = json.loads(row.get(field, ""))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{case['case_id']}: invalid JSON in read-only field: {field}") from exc
        if canonical(submitted) != canonical(case.get(field, [])):
            raise ValueError(f"{case['case_id']}: read-only field changed: {field}")
    decision = row.get("reviewer_decision", "").strip()
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"{case['case_id']}: invalid reviewer_decision")
    reviewer = row.get("reviewer", "").strip()
    reviewed_at = row.get("reviewed_at", "").strip()
    rationale = row.get("review_rationale", "").strip()
    if decision == "PENDING_HUMAN_REVIEW":
        if reviewer or reviewed_at or rationale:
            raise ValueError(f"{case['case_id']}: pending row cannot claim a completed review")
        return decision
    if not reviewer or not reviewed_at or not rationale:
        raise ValueError(f"{case['case_id']}: completed review requires reviewer, reviewed_at, and rationale")
    _time(reviewed_at)
    corrected_strategy = row.get("corrected_strategy", "").strip()
    corrected_sub = row.get("corrected_sub_strategy", "").strip()
    if decision == "APPROVE_AS_IS" and (corrected_strategy or corrected_sub):
        raise ValueError(f"{case['case_id']}: APPROVE_AS_IS cannot include corrections")
    if decision == "APPROVE_WITH_CORRECTION":
        # Strategy changes also change safety/evidence/tool contracts. Require a
        # new candidate version for those edits; this CSV only supports a narrow
        # sub-strategy correction within the reviewed strategy.
        if corrected_strategy and corrected_strategy != case["expected_strategy"]:
            raise ValueError(f"{case['case_id']}: route changes require NEEDS_CORRECTION and a new dataset version")
        if not corrected_sub:
            raise ValueError(f"{case['case_id']}: correction requires corrected_sub_strategy")
        if corrected_sub not in SUB_STRATEGIES[case["expected_strategy"]]:
            raise ValueError(f"{case['case_id']}: corrected_sub_strategy conflicts with route")
    elif corrected_strategy or corrected_sub:
        raise ValueError(f"{case['case_id']}: corrections are only allowed with APPROVE_WITH_CORRECTION")
    return decision


def import_reviews(source: Path, review_csv: Path, output: Path) -> dict[str, Any]:
    source, review_csv, output = map(_inside_root, (source, review_csv, output))
    if source.name.casefold().find("test_candidate") >= 0 or review_csv.name.casefold().find("test_candidate") >= 0:
        raise ValueError("test-candidate files are outside this importer scope")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite versioned review output: {output.relative_to(ROOT)}")
    dataset = json.loads(source.read_text(encoding="utf-8"))
    if dataset.get("status") != "CANDIDATE_NOT_HUMAN_APPROVED":
        raise ValueError("source must be the versioned unapproved candidate dataset")
    cases = dataset.get("cases", [])
    if canonical_hash(cases) != dataset.get("dataset_sha256_canonical_cases"):
        raise ValueError("candidate dataset canonical SHA-256 mismatch")
    for case in cases:
        core = {key: value for key, value in case.items() if key not in {"case_sha256", "review_history"}}
        if canonical_hash(core) != case.get("case_sha256"):
            raise ValueError(f"{case.get('case_id', '<unknown>')}: frozen candidate case hash mismatch")
    source_map = {case["case_id"]: case for case in cases}
    if len(source_map) != len(cases):
        raise ValueError("source dataset has duplicate case_id values")
    with review_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        rows = list(reader)
    if headers != EXPECTED_HEADERS:
        raise ValueError("review CSV schema mismatch; export the matching versioned template")
    ids = [row.get("case_id", "") for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(source_map):
        raise ValueError("review case_id set must exactly match the candidate dataset")
    reviewed = json.loads(json.dumps(dataset, ensure_ascii=False))
    reviewed_cases = {case["case_id"]: case for case in reviewed["cases"]}
    status_counts = {decision: 0 for decision in ALLOWED_DECISIONS}
    for row in rows:
        base = source_map[row["case_id"]]
        decision = validate_review_row(row, base, dataset["dataset_sha256_canonical_cases"])
        status_counts[decision] += 1
        if decision == "PENDING_HUMAN_REVIEW":
            continue
        target = reviewed_cases[row["case_id"]]
        before = {
            "review_status": target["review_status"],
            "expected_strategy": target["expected_strategy"],
            "expected_sub_strategy": target["expected_sub_strategy"],
        }
        if decision == "APPROVE_WITH_CORRECTION":
            target["expected_sub_strategy"] = row["corrected_sub_strategy"].strip() or None
        target["label_status"] = "HUMAN_REVIEWED"
        target["review_status"] = "APPROVED" if decision in {"APPROVE_AS_IS", "APPROVE_WITH_CORRECTION"} else "NEEDS_CORRECTION"
        target["reviewer_decision"] = decision
        target["reviewer"] = row["reviewer"].strip()
        target["reviewed_at"] = _time(row["reviewed_at"].strip())
        target["review_rationale"] = row["review_rationale"].strip()
        after = {
            "review_status": target["review_status"],
            "expected_strategy": target["expected_strategy"],
            "expected_sub_strategy": target["expected_sub_strategy"],
        }
        target["review_history"] = list(target.get("review_history", [])) + [{
            "reviewer_decision": decision, "reviewer": target["reviewer"],
            "reviewed_at": target["reviewed_at"], "review_rationale": target["review_rationale"],
            "before": before, "after": after,
        }]
    reviewed["status"] = "VERSIONED_HUMAN_REVIEW_IMPORT"
    reviewed["parent_dataset_version"] = dataset.get("dataset_version")
    reviewed["dataset_version"] = output.stem
    reviewed["parent_candidate_dataset_sha256_canonical_cases"] = dataset["dataset_sha256_canonical_cases"]
    reviewed["parent_candidate_dev_sha256_canonical_cases"] = dataset["dev_sha256_canonical_cases"]
    reviewed["parent_candidate_independent_validation_candidate_sha256_canonical_cases"] = dataset[
        "independent_validation_candidate_sha256_canonical_cases"]
    reviewed["dataset_sha256_canonical_cases"] = canonical_hash(reviewed["cases"])
    reviewed["dev_sha256_canonical_cases"] = canonical_hash(
        [case for case in reviewed["cases"] if case["dataset_split"] == "dev"])
    reviewed["independent_validation_candidate_sha256_canonical_cases"] = canonical_hash(
        [case for case in reviewed["cases"] if case["dataset_split"] == "independent_validation_candidate"])
    reviewed["parent_dataset_path"] = source.relative_to(ROOT).as_posix()
    reviewed["parent_dataset_sha256_file"] = sha256(source.read_bytes())
    reviewed["review_import"] = {
        "review_csv_path": review_csv.relative_to(ROOT).as_posix(),
        "review_csv_sha256_file": sha256(review_csv.read_bytes()),
        "imported_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reviewer_entered_timestamps_preserved": True,
        "status_counts": status_counts,
        "approved_count": sum(case["review_status"] == "APPROVED" for case in reviewed["cases"]),
        "test_candidate_rows_included": False,
        "independent_blind_review": False,
        "identity_verification": "NOT_PERFORMED_BY_IMPORTER",
        "unsupported_full_route_or_evidence_corrections": "must be marked NEEDS_CORRECTION and versioned separately",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n"
    with output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(text)
    return reviewed["review_import"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(SOURCE))
    parser.add_argument("--review-csv", default=str(REVIEW))
    parser.add_argument("--output", required=True, help="new versioned output path; existing files are refused")
    args = parser.parse_args()
    try:
        result = import_reviews(Path(args.source), Path(args.review_csv), Path(args.output))
    except (ValueError, FileExistsError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
