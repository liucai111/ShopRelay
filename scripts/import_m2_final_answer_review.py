"""Import the user-confirmed M2 answer dispute CSV without inventing per-item times."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/fidelity/m2.4-q2-human-review-r2.csv"
REVIEW = ROOT / "eval/fidelity/m2.4-q2-human-review-user-confirmed.csv"
CONFIRMATION = ROOT / "docs/review/ShopRelay_批量审核确认记录_20261021.json"
OUTPUT = ROOT / "eval/fidelity/m2.4-q2-human-adjudication-user-confirmed-r2.json"
ALLOWED_DECISIONS = {"SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "UNCERTAIN"}
READ_ONLY_FIELDS = (
    "item_id", "case_id", "dataset_split", "question", "disputed_claim",
    "policy_or_contract_source", "programmatic_conclusion", "judge_conclusion",
    "ai_recommendation",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside_root(path: Path) -> Path:
    resolved = path.resolve()
    if ROOT not in resolved.parents:
        raise ValueError("paths must remain inside ShopRelay")
    return resolved


def _timestamp(record: dict) -> str:
    value = record.get("record_written_at_utc")
    parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("confirmation timestamp must be timezone aware")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("confirmation timestamp must be canonical UTC")
    if "not separate per-row" not in record.get("review_time_semantics", ""):
        raise ValueError("confirmation must disclose batch timestamp semantics")
    return canonical


def build_import(source: Path = SOURCE, review: Path = REVIEW,
                 confirmation_path: Path = CONFIRMATION) -> dict:
    source, review, confirmation_path = map(inside_root, (source, review, confirmation_path))
    manifest = json.loads(confirmation_path.read_text(encoding="utf-8"))
    if manifest.get("record_type") != "shoprelay_chat_user_batch_review_confirmation":
        raise ValueError("unsupported confirmation record type")
    actor = manifest.get("confirmation_actor")
    if actor != "CHAT_USER_SELF_CONFIRMED" or manifest.get("identity_verification") != "NOT_PERFORMED":
        raise ValueError("confirmation actor or identity disclosure mismatch")
    if manifest.get("independent_blind_review") is not False or manifest.get("test_candidate_edited") is not False:
        raise ValueError("confirmation record violates blind-review/test-candidate boundary")
    record_time = _timestamp(manifest)

    source_bytes, review_bytes = source.read_bytes(), review.read_bytes()
    if sha256(source_bytes) != manifest.get("input_original_sha256", {}).get("m2.4-q2-human-review-r2.csv"):
        raise ValueError("answer review source template hash mismatch")
    if sha256(review_bytes) != manifest.get("output_sha256", {}).get("ShopRelay_答案审核_用户确认版_8条.csv"):
        raise ValueError("confirmed answer CSV hash mismatch")
    source_rows = list(csv.DictReader(source.open("r", encoding="utf-8-sig", newline="")))
    reviewed_rows = list(csv.DictReader(review.open("r", encoding="utf-8-sig", newline="")))
    if len(source_rows) != 8 or len(reviewed_rows) != 8:
        raise ValueError("answer dispute review must contain exactly 8 rows")
    source_by_id = {row["item_id"]: row for row in source_rows}
    reviewed_by_id = {row["item_id"]: row for row in reviewed_rows}
    if len(source_by_id) != 8 or set(source_by_id) != set(reviewed_by_id):
        raise ValueError("answer review item IDs do not exactly match source")

    decisions = Counter()
    splits = Counter()
    normalized = []
    for row in reviewed_rows:
        item_id = row["item_id"]
        baseline = source_by_id[item_id]
        if any(row.get(field, "") != baseline.get(field, "") for field in READ_ONLY_FIELDS):
            raise ValueError(f"{item_id}: source/baseline content changed")
        if row.get("reviewer_decision") not in ALLOWED_DECISIONS:
            raise ValueError(f"{item_id}: invalid reviewer decision")
        if row.get("review_status") != "REVIEWED":
            raise ValueError(f"{item_id}: expected adjudicated REVIEWED marker")
        if row.get("reviewer") != actor or row.get("reviewed_at") != record_time:
            raise ValueError(f"{item_id}: reviewer marker or batch timestamp mismatch")
        if not row.get("review_rationale", "").strip():
            raise ValueError(f"{item_id}: missing adjudication rationale")
        relevance_match = re.search(r"相关性(NECESSARY|UNNECESSARY|RELEVANT_CONTEXT)",
                                    row["review_rationale"])
        if relevance_match is None:
            raise ValueError(f"{item_id}: rationale lacks an explicit relevance adjudication")
        if row.get("dataset_split") not in {"dev", "validation"}:
            raise ValueError(f"{item_id}: only dev/validation answer disputes are importable")
        decisions[row["reviewer_decision"]] += 1
        splits[row["dataset_split"]] += 1
        normalized.append({
            **{field: row.get(field, "") for field in READ_ONLY_FIELDS},
            "reviewer_decision": row["reviewer_decision"],
            "review_status": "REVIEWED",
            "reviewer": actor,
            "reviewed_at": None,
            "review_recorded_at_utc": record_time,
            "review_time_semantics": "batch record creation time; no individual per-item review time recorded",
            "reviewed_relevance": relevance_match.group(1),
            "review_rationale": row["review_rationale"],
        })

    expected = manifest.get("answers", {})
    if (expected.get("total") != len(normalized) or
            expected.get("supported") != decisions["SUPPORTED"] or
            expected.get("unsupported") != decisions["UNSUPPORTED"]):
        raise ValueError("answer decision counts do not match batch confirmation record")
    scope_notes = {
        "claim-07": expected.get("claim_07"),
        "claim-08": expected.get("claim_08"),
        "review_status_semantics": "REVIEWED means this item was adjudicated; it does not mean the claim is supported.",
    }
    status_counts = {key: decisions[key] for key in sorted(ALLOWED_DECISIONS) if decisions[key]}
    necessary_supported = sum(row["reviewer_decision"] == "SUPPORTED" and
                              row["reviewed_relevance"] == "NECESSARY" for row in normalized)
    necessary_total = sum(row["reviewed_relevance"] == "NECESSARY" for row in normalized)
    relevance_counts = Counter(row["reviewed_relevance"] for row in normalized)
    payload = {
        "record_type": "m2_answer_dispute_user_confirmation_import",
        "dataset_version": "m2.4-q2-human-adjudication-user-confirmed-r1",
        "status": "IMPORTED_ADJUDICATED_DISPUTE_SUBSET",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "confirmation": {
            "source": "explicit user confirmation in current ChatGPT conversation",
            "actor_marker": actor,
            "identity_verification": "NOT_PERFORMED",
            "independent_blind_review": False,
            "record_written_at_utc": record_time,
            "per_item_reviewed_at_recorded": False,
            "confirmation_record": confirmation_path.relative_to(ROOT).as_posix(),
            "confirmation_record_sha256": sha256(confirmation_path.read_bytes()),
        },
        "inputs": {
            "source_template": source.relative_to(ROOT).as_posix(),
            "source_template_sha256": sha256(source_bytes),
            "confirmed_csv": review.relative_to(ROOT).as_posix(),
            "confirmed_csv_sha256": sha256(review_bytes),
            "row_count": len(normalized),
            "split_counts": dict(splits),
            "test_candidate_read_or_scored": False,
        },
        "adjudications": normalized,
        "metrics": {
            "scope": "8 previously disputed answer claims only; not an answer-level system quality score",
            "claim_status_counts": status_counts,
            "adjudicated_claim_count": len(normalized),
            "supported_fraction_among_disputes": decisions["SUPPORTED"] / len(normalized),
            "unsupported_claim_count": decisions["UNSUPPORTED"],
            "necessary_claims_supported": necessary_supported,
            "necessary_claims_total_as_explicitly_recorded_in_rationale": necessary_total,
            "necessary_claim_coverage_subset": (necessary_supported / necessary_total if necessary_total else None),
            "relevance_counts": dict(relevance_counts),
            "interpretation": "Claim-level dispute adjudication only. It does not score all answer claims, independent validation, or M2 overall quality.",
            "scope_notes": scope_notes,
        },
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE.relative_to(ROOT).as_posix())
    parser.add_argument("--csv", default=REVIEW.relative_to(ROOT).as_posix())
    parser.add_argument("--confirmation-record", default=CONFIRMATION.relative_to(ROOT).as_posix())
    parser.add_argument("--output", default=OUTPUT.relative_to(ROOT).as_posix())
    args = parser.parse_args()
    output = inside_root(ROOT / args.output)
    if output.exists():
        parser.error(f"refusing to overwrite existing version: {output.relative_to(ROOT)}")
    payload = build_import(ROOT / args.source, ROOT / args.csv, ROOT / args.confirmation_record)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "count": len(payload["adjudications"]),
                      "metrics": payload["metrics"], "output": output.relative_to(ROOT).as_posix()},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
