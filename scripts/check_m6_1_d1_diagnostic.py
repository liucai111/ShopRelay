"""Recompute one fixed personal D1 provisional check report from its raw archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import plan_case
from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, PROVIDER_RETRY_ROUND_ID,
    personal_diagnostic_profile,
)
from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record, load_locked_rules


def recompute(case_id: str, *, write: bool = False,
              diagnostic_round_id: str | None = None) -> dict:
    profile = personal_diagnostic_profile(case_id, diagnostic_round_id=diagnostic_round_id)
    if case_id not in {"D1-POLICY-013", "D1-ORDER-007", "D1-ORDER-009"} or not profile.check_rules_path:
        raise ValueError("PERSONAL_DIAGNOSTIC_CASE_NOT_ALLOWLISTED")
    report_path = ((ROOT / profile.report_dir / f"{case_id}-diagnostic-check-{profile.report_suffix}.json")
        if diagnostic_round_id is not None else
        ROOT / "docs/eval/m6.1-d1-three-case-diagnostic-r1/reports" / f"{case_id}-diagnostic-check-r1.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            report.get("reference_review_status") != "reference_unreviewed" or
            report.get("grading_status") != "NOT_GRADED" or report.get("case_id") != case_id):
        raise ValueError("DIAGNOSTIC_REPORT_SCOPE_INVALID")
    bundle_path = ROOT / AUTHORIZED_ASSET_PATH
    if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != AUTHORIZED_ASSET_SHA256:
        raise ValueError("EXACT_D1_DEV_ASSET_HASH_REQUIRED")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    plan = plan_case(bundle, case_id)
    rules, rules_sha = load_locked_rules(ROOT / profile.check_rules_path, case_id=case_id)
    if report.get("rules_sha256") != rules_sha:
        raise ValueError("DIAGNOSTIC_RULES_HASH_MISMATCH")
    receipt = report.get("cleanup_evidence") or {}
    lease_id = (receipt.get("lease_id") or "")
    archive_relative = report.get("archive_path")
    expected_archive = f"docs/eval/personal-dev-runs/{lease_id}.json"
    if not lease_id or archive_relative != expected_archive:
        raise ValueError("DIAGNOSTIC_ARCHIVE_PATH_INVALID")
    archive_path = ROOT / expected_archive
    archived_raw = archive_path.read_bytes()
    archive_sha = hashlib.sha256(archived_raw).hexdigest()
    if archive_sha != report.get("archive_sha256"):
        raise ValueError("DIAGNOSTIC_ARCHIVE_HASH_MISMATCH")
    archived = json.loads(archived_raw.decode("utf-8"))
    checks = evaluate_diagnostic_record(case_id=case_id, archived=archived, plan=plan,
        locked_rules=rules, budget_snapshot=report["budget_snapshot"], cleanup_evidence=receipt)
    consistent = checks == report.get("checks")
    output = {"schema_version": "1.0", "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
        "case_id": case_id, "raw_archive_sha256": archive_sha, "rules_sha256": rules_sha,
        "recomputed_checks": checks, "matches_execution_report": consistent}
    if write:
        output_path = ((ROOT / profile.report_dir / f"{case_id}-recomputed-check-{profile.report_suffix}.json")
            if diagnostic_round_id is not None else
            ROOT / "docs/eval/m6.1-d1-three-case-diagnostic-r1/reports" / f"{case_id}-recomputed-check-r1.json")
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        output["output_path"] = output_path.relative_to(ROOT).as_posix()
        output["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("D1-POLICY-013", "D1-ORDER-007", "D1-ORDER-009"))
    parser.add_argument("--provider-retry-round", action="store_true",
        help="Select the allowlisted r5 POLICY-013 profile; no arbitrary paths are accepted.")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    round_id = PROVIDER_RETRY_ROUND_ID if args.provider_retry_round else None
    result = recompute(args.case, write=args.write, diagnostic_round_id=round_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["matches_execution_report"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
