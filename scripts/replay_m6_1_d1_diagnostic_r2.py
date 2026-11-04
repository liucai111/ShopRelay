"""Recompute a fixed historical personal D1 diagnostic without changing its source records."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.m6_1.personal_dev import plan_case
from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record, load_locked_rules


CASE_ID = "D1-POLICY-013"
ARCHIVE_REL = "docs/eval/personal-dev-runs/90a90045ba384f15a0d211ffa7d9c1ae.json"
OLD_REPORT_REL = "docs/eval/m6.1-d1-three-case-diagnostic-r1/reports/D1-POLICY-013-diagnostic-check-r1.json"
RULES_REL = "docs/eval/m6.1-d1-diagnostic-closure-r2/locked-check-rules-r2.json"
ASSET_REL = "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
OUTPUT_REL = "docs/eval/m6.1-d1-diagnostic-closure-r2/reports/D1-POLICY-013-historical-replay-r2.json"


def recompute(*, root: Path = ROOT, write: bool = True) -> dict:
    def read_json(rel: str) -> tuple[dict, bytes]:
        raw = (root / rel).read_bytes()
        return json.loads(raw.decode("utf-8")), raw

    archive, archive_bytes = read_json(ARCHIVE_REL)
    old_report, _old_report_bytes = read_json(OLD_REPORT_REL)
    asset, asset_bytes = read_json(ASSET_REL)
    rules, rules_sha = load_locked_rules(root / RULES_REL, case_id=CASE_ID)
    expected_sha = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
    if hashlib.sha256(asset_bytes).hexdigest() != expected_sha:
        raise ValueError("FIXED_R324_ASSET_HASH_MISMATCH")
    selected_plan = plan_case(asset, CASE_ID)
    checks = evaluate_diagnostic_record(case_id=CASE_ID, archived=archive, plan=selected_plan,
        locked_rules=rules, budget_snapshot=old_report.get("budget_snapshot"),
        cleanup_evidence=old_report.get("cleanup_evidence"))
    document = {
        "schema_version": "2.0",
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED",
        "case_id": CASE_ID,
        "replay_only": True,
        "historical_authorization_status": "CLOSED_COMPLETED",
        "historical_status_interpretation": "Lifecycle/cleanup closure only; the prior diagnostic checker did not pass.",
        "archive_path": ARCHIVE_REL,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "prior_report_path": OLD_REPORT_REL,
        "prior_report_sha256": hashlib.sha256((root / OLD_REPORT_REL).read_bytes()).hexdigest(),
        "rules_path": RULES_REL,
        "rules_sha256": rules_sha,
        "asset_path": ASSET_REL,
        "asset_sha256": expected_sha,
        "checks": checks,
        "historical_record_limitations": [
            "The archive contains policy citations, evidence, retrieval metadata, and a retrieval_hop, but no policy call id, parameter fingerprint, result reference, or policy retrieval start/finish events.",
            "The replay does not add or imply any audit event that was absent from the original run.",
            "The original archive, r1 rules, r1 report, and authorization remain unchanged.",
        ],
    }
    if write:
        target = root / OUTPUT_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    result = recompute(write=not args.check_only)
    print(json.dumps({"case_id": result["case_id"], "decision": result["checks"]["decision"],
        "output_path": None if args.check_only else OUTPUT_REL,
        "archive_sha256": result["archive_sha256"], "grading_status": "NOT_GRADED"},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
