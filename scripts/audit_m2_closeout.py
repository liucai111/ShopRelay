"""Audit the M2-Closeout human-review sources without opening test_candidate."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_SOURCE = ROOT / "eval/review/route-m2.1-dev-validation-review-r1.csv"
ANSWER_SOURCE = ROOT / "eval/fidelity/m2.4-q2-human-review-r2.csv"
STRATEGIES = {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}
SAFETY = {"ALLOW", "ISOLATE_INJECTION", "REFUSE"}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def audit() -> dict:
    route_rows = _rows(ROUTE_SOURCE)
    answer_rows = _rows(ANSWER_SOURCE)
    ids = [row["case_id"] for row in route_rows]
    queries = [re.sub(r"\s+", " ", row["user_query"]).strip().casefold() for row in route_rows]
    groups: dict[str, set[str]] = defaultdict(set)
    group_counts: Counter[str] = Counter()
    for row in route_rows:
        groups[row["group_id"]].add(row["dataset_split"])
        group_counts[row["group_id"]] += 1
    route_status = Counter(row.get("review_status", "") for row in route_rows)
    answer_status = Counter(row.get("review_status", "") for row in answer_rows)
    answer_decisions = Counter(row.get("reviewer_decision", "") or "PENDING_HUMAN_REVIEW"
                               for row in answer_rows)
    return {
        "record_type": "m2_closeout_source_audit",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS_WITH_HUMAN_REVIEW_PENDING",
        "sources": {
            "route_review_csv": ROUTE_SOURCE.relative_to(ROOT).as_posix(),
            "route_review_csv_sha256": _hash(ROUTE_SOURCE),
            "answer_dispute_csv": ANSWER_SOURCE.relative_to(ROOT).as_posix(),
            "answer_dispute_csv_sha256": _hash(ANSWER_SOURCE),
        },
        "route_candidates": {
            "count": len(route_rows),
            "split_counts": dict(Counter(row["dataset_split"] for row in route_rows)),
            "distinct_template_groups": len(groups),
            "group_size_counts": dict(Counter(group_counts.values())),
            "duplicate_case_ids": len(ids) - len(set(ids)),
            "duplicate_normalized_questions": len(queries) - len(set(queries)),
            "invalid_strategy_labels": sorted({row["expected_strategy"] for row in route_rows} - STRATEGIES),
            "invalid_safety_labels": sorted({row["expected_safety_decision"] for row in route_rows} - SAFETY),
            "groups_crossing_splits": sorted(group for group, splits in groups.items() if len(splits) > 1),
            "review_status_counts": dict(route_status),
            "all_labels_human_approved": all(row.get("review_status") == "APPROVED" for row in route_rows),
        },
        "answer_disputes": {
            "count": len(answer_rows), "review_status_counts": dict(answer_status),
            "reviewer_decision_counts": dict(answer_decisions),
        },
        "test_candidate": {
            "rows_read": 0, "scored": False, "used_for_tuning": False,
            "status": "SEALED_UNFROZEN_NOT_READ",
        },
        "interpretation": (
            "CSV structural/source audit only. Route labels remain candidate labels until a real human import; "
            "no route accuracy is claimed. Exact and whitespace-normalized duplicate checks do not establish semantic uniqueness."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="new versioned JSON path inside ShopRelay")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if ROOT not in output.parents:
        raise SystemExit("output must stay inside ShopRelay")
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output.relative_to(ROOT)}")
    result = audit()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(),
                      "route_candidates": result["route_candidates"],
                      "answer_disputes": result["answer_disputes"],
                      "test_candidate_rows_read": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
