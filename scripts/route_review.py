"""Export and import versioned human review for M2.1 dev/validation route labels."""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/review/route-m2.1-dev-validation-review-r1.csv"
REVIEW = ROOT / "eval/review/route-m2.1-dev-validation-review-r2.csv"
STRATEGIES = {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}
SUB_STRATEGIES = {"DIRECT": {"", "BM25"}, "SINGLE": {"", "BM25"},
                  "MULTI": {"", "MULTI"}, "AGENT": {"", "AGENT+SINGLE", "AGENT+MULTI"},
                  "REFUSE": {""}}
CAPABILITIES = {"search_knowledge", "get_order", "track_shipment", "create_return_request",
                "create_refund_request", "cancel_order"}
DECISIONS = {"PENDING_HUMAN_REVIEW", "APPROVE_AS_IS", "APPROVE_WITH_CORRECTION", "NEEDS_CORRECTION"}
FIELDS = [
    "case_id", "base_dataset_sha256", "base_case_sha256", "group_id", "dataset_split",
    "source_case_id", "user_query", "expected_strategy", "expected_sub_strategy",
    "expected_intent", "expected_safety_decision", "expected_reason_code",
    "expected_needs_clarification", "required_capabilities", "reviewer_decision",
    "corrected_strategy", "corrected_sub_strategy", "corrected_intent",
    "corrected_safety_decision", "corrected_reason_code", "corrected_needs_clarification",
    "corrected_required_capabilities", "reviewer", "reviewed_at", "review_rationale",
]
EDITABLE = {
    "expected_strategy": "corrected_strategy",
    "expected_sub_strategy": "corrected_sub_strategy",
    "expected_intent": "corrected_intent",
    "expected_safety_decision": "corrected_safety_decision",
    "expected_reason_code": "corrected_reason_code",
    "expected_needs_clarification": "corrected_needs_clarification",
    "required_capabilities": "corrected_required_capabilities",
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside_root(path: Path) -> Path:
    resolved = path.resolve()
    if ROOT not in resolved.parents:
        raise ValueError("input/output must stay inside ShopRelay")
    return resolved


def _load_source_rows(path: Path) -> tuple[list[dict], str]:
    data = path.read_bytes()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 52 or any(row.get("dataset_split") not in {"dev", "validation"} for row in rows):
        raise ValueError("route review source must be the 52-row dev/validation-only CSV")
    ids = [row.get("case_id", "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("route review source has missing or duplicate case_id values")
    return rows, sha256(data)


def _normalize_source_row(row: dict[str, str]) -> dict:
    raw_capabilities = row.get("required_capabilities", "[]").strip()
    try:
        capabilities = json.loads(raw_capabilities)
    except json.JSONDecodeError:
        # The legacy review CSV used Python list repr (single quotes). Parse it
        # as data only; never evaluate arbitrary source text.
        capabilities = ast.literal_eval(raw_capabilities)
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ValueError(f"{row.get('case_id', '<unknown>')}: required_capabilities must be a string array")
    clarify = row.get("expected_needs_clarification", "False").strip().casefold()
    if clarify not in {"true", "false"}:
        raise ValueError(f"{row['case_id']}: malformed expected_needs_clarification")
    return {
        "case_id": row["case_id"], "group_id": row["group_id"],
        "dataset_split": row["dataset_split"],
        "source": "HUMAN_REVIEWED_POLICY_CASE_ROUTE_LABEL_PENDING",
        "source_case_id": row["source_case_id"], "user_query": row["user_query"],
        "expected_strategy": row["expected_strategy"],
        "expected_sub_strategy": row.get("expected_sub_strategy") or None,
        "expected_intent": row["expected_intent"],
        "expected_safety_decision": row["expected_safety_decision"],
        "expected_reason_code": row["expected_reason_code"],
        "expected_needs_clarification": clarify == "true",
        "required_capabilities": capabilities,
        "review_status": "PENDING_REVIEW", "reviewer": None,
        "reviewed_at": None, "review_notes": None, "review_history": [],
    }


def export_review(source: Path, output: Path) -> dict:
    source, output = _inside_root(source), _inside_root(output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite review file: {output.relative_to(ROOT)}")
    source_rows, base_hash = _load_source_rows(source)
    rows = [_normalize_source_row(row) for row in source_rows]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "case_id": row["case_id"], "base_dataset_sha256": base_hash,
                "base_case_sha256": sha256(canonical(row)),
                **{key: json.dumps(row.get(key), ensure_ascii=False, separators=(",", ":"))
                   if isinstance(row.get(key), (dict, list)) else str(row.get(key, ""))
                   for key in FIELDS if key in row},
                "reviewer_decision": "PENDING_HUMAN_REVIEW",
                "corrected_strategy": "", "corrected_sub_strategy": "",
                "corrected_intent": "", "corrected_safety_decision": "",
                "corrected_reason_code": "", "corrected_needs_clarification": "",
                "corrected_required_capabilities": "", "reviewer": "",
                "reviewed_at": "", "review_rationale": "",
            })
    return {"path": output.relative_to(ROOT).as_posix(), "count": len(rows),
            "dataset_sha256": base_hash,
            "split_counts": {split: sum(row["dataset_split"] == split for row in rows)
                             for split in ("dev", "validation")}}


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewed_at must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone")
    return parsed


def _validate_decision(row: dict[str, str], base: dict) -> tuple[str, dict]:
    decision = row.get("reviewer_decision", "").strip() or "PENDING_HUMAN_REVIEW"
    if decision not in DECISIONS:
        raise ValueError(f"{base['case_id']}: invalid reviewer_decision {decision}")
    correction = {}
    if decision == "APPROVE_WITH_CORRECTION" or decision == "NEEDS_CORRECTION":
        for field, csv_field in EDITABLE.items():
            raw = row.get(csv_field, "").strip()
            if not raw:
                raise ValueError(f"{base['case_id']}: missing {csv_field} for {decision}")
            if field == "required_capabilities":
                value = json.loads(raw)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ValueError(f"{base['case_id']}: corrected_required_capabilities must be a JSON string array")
            elif field == "expected_needs_clarification":
                if raw.casefold() not in {"true", "false"}:
                    raise ValueError(f"{base['case_id']}: corrected_needs_clarification must be true/false")
                value = raw.casefold() == "true"
            else:
                value = raw
            correction[field] = value
        if correction["expected_strategy"] not in STRATEGIES:
            raise ValueError(f"{base['case_id']}: unsupported corrected strategy")
        if correction["expected_sub_strategy"] not in SUB_STRATEGIES[correction["expected_strategy"]]:
            raise ValueError(f"{base['case_id']}: corrected sub_strategy conflicts with strategy")
        if correction["expected_safety_decision"] not in {"ALLOW", "ISOLATE_INJECTION", "REFUSE"}:
            raise ValueError(f"{base['case_id']}: unsupported corrected safety decision")
        capabilities = correction["required_capabilities"]
        if len(capabilities) != len(set(capabilities)) or not set(capabilities) <= CAPABILITIES:
            raise ValueError(f"{base['case_id']}: unsupported or duplicate corrected capability")
    if decision != "PENDING_HUMAN_REVIEW":
        if not row.get("reviewer", "").strip() or not row.get("review_rationale", "").strip():
            raise ValueError(f"{base['case_id']}: reviewer and review_rationale are required")
        _parse_time(row.get("reviewed_at", "").strip())
    return decision, correction


def _load_batch_confirmation(path: Path, review_csv: Path) -> dict:
    path = _inside_root(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("record_type") != "shoprelay_chat_user_batch_review_confirmation":
        raise ValueError("unsupported confirmation record type")
    if record.get("confirmation_actor") != "CHAT_USER_SELF_CONFIRMED":
        raise ValueError("confirmation actor marker mismatch")
    if record.get("identity_verification") != "NOT_PERFORMED":
        raise ValueError("confirmation identity-verification disclosure mismatch")
    if record.get("independent_blind_review") is not False or record.get("test_candidate_edited") is not False:
        raise ValueError("confirmation record must preserve blind-review and test-candidate boundaries")
    expected = (record.get("output_sha256") or {}).get("ShopRelay_路由审核_用户确认版_52条.csv")
    if expected != sha256(review_csv.read_bytes()):
        raise ValueError("route confirmation CSV hash does not match confirmation record")
    original_name = "route-m2.1-dev-validation-review-r2.csv"
    original_hash = (record.get("input_original_sha256") or {}).get(original_name)
    original_path = ROOT / "eval/review" / original_name
    if not original_hash or not original_path.is_file() or sha256(original_path.read_bytes()) != original_hash:
        raise ValueError("original route review template hash does not match confirmation record")
    timestamp = record.get("record_written_at_utc")
    parsed = _parse_time(timestamp or "")
    if parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != timestamp:
        raise ValueError("record_written_at_utc must be canonical UTC")
    semantics = record.get("review_time_semantics", "")
    if "not separate per-row" not in semantics:
        raise ValueError("confirmation record must state batch-time semantics")
    return record


def import_review(source: Path, review_csv: Path, output: Path,
                  confirmation_record: Path | None = None) -> dict:
    source, review_csv, output = map(_inside_root, (source, review_csv, output))
    if output.exists():
        raise FileExistsError(f"refusing to overwrite reviewed dataset: {output.relative_to(ROOT)}")
    confirmation = (_load_batch_confirmation(confirmation_record, review_csv)
                    if confirmation_record is not None else None)
    source_rows, source_hash = _load_source_rows(source)
    source_cases = {row["case_id"]: _normalize_source_row(row) for row in source_rows}
    with review_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError("review CSV schema mismatch; export a current r2 review template")
        rows = list(reader)
    ids = [row.get("case_id", "") for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(source_cases):
        raise ValueError("review CSV case_id set must exactly match source dev/validation candidates")
    split_counts = {split: 0 for split in ("dev", "validation")}
    outcome_counts = {decision: 0 for decision in DECISIONS}
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for review_row in rows:
        case_id = review_row["case_id"]
        case = source_cases[case_id]
        if review_row.get("base_dataset_sha256") != source_hash:
            raise ValueError(f"{case_id}: stale base dataset hash")
        if review_row.get("base_case_sha256") != sha256(canonical(case)):
            raise ValueError(f"{case_id}: stale base case hash")
        readonly_fields = ("group_id", "dataset_split", "source_case_id", "user_query", "expected_strategy",
                           "expected_sub_strategy", "expected_intent", "expected_safety_decision",
                           "expected_reason_code", "expected_needs_clarification")
        for field in readonly_fields:
            expected = "" if case.get(field) is None else str(case.get(field))
            if review_row.get(field, "") != expected:
                raise ValueError(f"{case_id}: read-only field changed: {field}")
        try:
            parsed_capabilities = json.loads(review_row.get("required_capabilities", ""))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{case_id}: required_capabilities must remain a JSON array") from exc
        if parsed_capabilities != case.get("required_capabilities", []):
            raise ValueError(f"{case_id}: read-only field changed: required_capabilities")
        decision, correction = _validate_decision(review_row, case)
        if confirmation is not None:
            if review_row.get("reviewer", "").strip() != confirmation["confirmation_actor"]:
                raise ValueError(f"{case_id}: reviewer marker differs from batch confirmation")
            if review_row.get("reviewed_at", "").strip() != confirmation["record_written_at_utc"]:
                raise ValueError(f"{case_id}: batch timestamp differs from confirmation record")
        split_counts[case["dataset_split"]] += 1
        outcome_counts[decision] += 1
        if decision == "PENDING_HUMAN_REVIEW":
            continue
        before = {key: case.get(key) for key in [*EDITABLE.keys(), "review_status", "reviewer", "reviewed_at", "review_notes"]}
        case.update(correction)
        case["review_status"] = "APPROVED" if decision in {"APPROVE_AS_IS", "APPROVE_WITH_CORRECTION"} else "NEEDS_CORRECTION"
        case["reviewer"] = review_row["reviewer"].strip()
        if confirmation is None:
            case["reviewed_at"] = _parse_time(review_row["reviewed_at"].strip()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            # The confirmation file explicitly says this timestamp is a batch-record
            # creation time, not an independently recorded per-case review time.
            case["reviewed_at"] = None
            case["review_recorded_at_utc"] = confirmation["record_written_at_utc"]
        case["review_notes"] = review_row["review_rationale"].strip()
        after = {key: case.get(key) for key in [*EDITABLE.keys(), "review_status", "reviewer", "reviewed_at", "review_notes"]}
        if confirmation is not None:
            after["review_recorded_at_utc"] = case["review_recorded_at_utc"]
        case.setdefault("review_history", []).append({
            "event_type": ("CHAT_USER_SELF_CONFIRMED_ROUTE_IMPORT" if confirmation is not None
                           else "ROUTE_HUMAN_REVIEW_IMPORT"), "decision": decision,
            "recorded_at_utc": (confirmation["record_written_at_utc"] if confirmation is not None
                                else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
            "reviewer": case["reviewer"], "reviewed_at": case["reviewed_at"],
            "rationale": case["review_notes"], "base_dataset_sha256": source_hash,
            "before": before, "after": after,
        })
    payload = {
        "dataset_version": (f"{output.stem}" if confirmation is not None
                             else "route-m2.1-dev-validation-reviewed-r2"),
        "status": "VERSIONED_HUMAN_REVIEW_IMPORT",
        "parent_review_csv": source.relative_to(ROOT).as_posix(),
        "parent_dataset_sha256": source_hash,
        "cases": list(source_cases.values()),
    }
    payload["review_imported_at"] = timestamp
    payload["route_review_import"] = {"csv_sha256": sha256(review_csv.read_bytes()),
                                      "case_count": len(rows), "split_counts": split_counts,
                                      "decision_counts": outcome_counts,
                                      "test_candidate_rows_included": False,
                                      "test_candidate_rows_modified": 0}
    if confirmation is not None:
        expected_route = confirmation.get("route", {})
        if (expected_route.get("total") != len(rows) or
                expected_route.get("approved_as_is") != outcome_counts["APPROVE_AS_IS"] or
                expected_route.get("approved_with_correction") != outcome_counts["APPROVE_WITH_CORRECTION"] or
                expected_route.get("split_counts") != split_counts):
            raise ValueError("route decision counts do not match batch confirmation record")
        payload["route_review_import"].update({
            "confirmation_record": confirmation_record.relative_to(ROOT).as_posix(),
            "confirmation_record_sha256": sha256(_inside_root(confirmation_record).read_bytes()),
            "confirmation_actor": confirmation["confirmation_actor"],
            "identity_verification": confirmation["identity_verification"],
            "record_written_at_utc": confirmation["record_written_at_utc"],
            "per_case_reviewed_at_recorded": False,
            "independent_blind_review": False,
            "label_scope": (expected_route.get("note") or
                           "human-confirmed route labels; scope not separately specified"),
        })
    payload["dataset_sha256_canonical_cases"] = sha256(canonical(payload["cases"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    return {"path": output.relative_to(ROOT).as_posix(), **payload["route_review_import"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--source", default=str(SOURCE.relative_to(ROOT)))
    export.add_argument("--output", default=str(REVIEW.relative_to(ROOT)))
    imp = sub.add_parser("import")
    imp.add_argument("--source", default=str(SOURCE.relative_to(ROOT)))
    imp.add_argument("--csv", default=str(REVIEW.relative_to(ROOT)))
    imp.add_argument("--output", required=True)
    imp.add_argument("--confirmation-record", default=None,
                     help="explicit chat confirmation manifest; its batch timestamp is not stored as per-case reviewed_at")
    args = parser.parse_args()
    if args.command == "export":
        result = export_review(ROOT / args.source, ROOT / args.output)
    else:
        result = import_review(ROOT / args.source, ROOT / args.csv, ROOT / args.output,
                               ROOT / args.confirmation_record if args.confirmation_record else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
