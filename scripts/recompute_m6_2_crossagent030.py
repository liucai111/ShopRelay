"""Independently recompute the fixed CROSSAGENT-030 provisional checks offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import implementation_digest, plan_case
from eval.m6_1.personal_dev_authorization import personal_diagnostic_profile
from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record, load_locked_rules


CASE = "D1-CROSSAGENT-030"
ROUND = "M62-CA030-MULTI-R1-20261029"
REPORT = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-diagnostic-check-r1.json"
OUTPUT = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-independent-recompute-r1.json"
ASSET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
ASSET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"


def recompute(*, write: bool = False) -> dict:
    profile = personal_diagnostic_profile(CASE, diagnostic_round_id=ROUND)
    raw_report = REPORT.read_bytes()
    report = json.loads(raw_report.decode("utf-8"))
    if (report.get("case_id") != CASE or report.get("diagnostic_round_id") != ROUND or
            report.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            report.get("reference_review_status") != "reference_unreviewed" or
            report.get("grading_status") != "NOT_GRADED"):
        raise ValueError("CROSSAGENT030_REPORT_SCOPE_INVALID")
    actual_code = implementation_digest(ROOT)
    if report.get("code_sha256") != actual_code:
        raise ValueError("CROSSAGENT030_IMPLEMENTATION_BINDING_MISMATCH")
    asset_raw = ASSET.read_bytes()
    asset_sha = hashlib.sha256(asset_raw).hexdigest()
    if asset_sha != ASSET_SHA256 or report.get("asset_sha256") != asset_sha:
        raise ValueError("CROSSAGENT030_DATASET_BINDING_MISMATCH")
    rules, rules_sha = load_locked_rules(ROOT / profile.check_rules_path, case_id=CASE)
    if report.get("rules_sha256") != rules_sha:
        raise ValueError("CROSSAGENT030_RULE_BINDING_MISMATCH")
    archive_path = ROOT / report["archive_path"]
    archive_raw = archive_path.read_bytes()
    archive_sha = hashlib.sha256(archive_raw).hexdigest()
    if archive_sha != report.get("archive_sha256"):
        raise ValueError("CROSSAGENT030_ARCHIVE_HASH_MISMATCH")
    archived = json.loads(archive_raw.decode("utf-8"))
    plan = plan_case(json.loads(asset_raw.decode("utf-8")), CASE)
    checks = evaluate_diagnostic_record(case_id=CASE, archived=archived, plan=plan,
        locked_rules=rules, budget_snapshot=report["budget_snapshot"],
        cleanup_evidence=report["cleanup_evidence"])
    check_container = report.get("checks")
    if not isinstance(check_container, dict) or not isinstance(check_container.get("check_items"), list):
        raise ValueError("CROSSAGENT030_CHECKS_MISSING")
    result = {
        "schema_version": "m6.2-crossagent030-independent-recompute-r1",
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED",
        "case_id": CASE,
        "run_id": archived["result"]["run_id"],
        "archive_sha256": archive_sha,
        "execution_report_sha256": hashlib.sha256(raw_report).hexdigest(),
        "rules_sha256": rules_sha,
        "implementation_digest": actual_code,
        "dataset_sha256": asset_sha,
        "recomputed_checks": checks,
        "matches_execution_report": checks == check_container,
        "diagnostic_decision": check_container.get("decision"),
        "note": "Offline recomputation from the immutable raw archive; not a new execution or formal score.",
    }
    if write:
        with OUTPUT.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        result["output_path"] = OUTPUT.relative_to(ROOT).as_posix()
        result["output_sha256"] = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = recompute(write=args.write)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["matches_execution_report"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
