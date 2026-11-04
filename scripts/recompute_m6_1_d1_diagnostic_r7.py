"""Independently recompute a persisted R7 personal diagnostic check report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import plan_case
from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, ORDER_DIAGNOSTIC_ROUND_ID,
    personal_diagnostic_profile,
)
from eval.m6_1.personal_dev_diagnostic_checks import (
    evaluate_diagnostic_record, load_locked_rules, validate_diagnostic_check_result,
)


def recompute(*, case_id: str, root: Path = ROOT, write: bool = True) -> dict:
    if case_id not in {"D1-ORDER-007", "D1-ORDER-009"}:
        raise ValueError("R7_RECOMPUTE_CASE_NOT_ALLOWLISTED")
    profile = personal_diagnostic_profile(case_id, diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
    report_rel = (Path(profile.report_dir) / f"{case_id}-diagnostic-check-r7.json").as_posix()
    report_path = root / report_rel
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    if (report.get("phase") != "EXECUTION_CLOSEOUT" or report.get("case_id") != case_id or
            report.get("diagnostic_round_id") != ORDER_DIAGNOSTIC_ROUND_ID or
            report.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            report.get("reference_review_status") != "reference_unreviewed" or
            report.get("grading_status") != "NOT_GRADED"):
        raise ValueError("R7_EXECUTION_REPORT_SCOPE_INVALID")
    asset_path = root / AUTHORIZED_ASSET_PATH
    if hashlib.sha256(asset_path.read_bytes()).hexdigest() != AUTHORIZED_ASSET_SHA256:
        raise ValueError("R7_FIXED_DATASET_HASH_MISMATCH")
    bundle = json.loads(asset_path.read_text(encoding="utf-8"))
    plan = plan_case(bundle, case_id)
    archive_rel = report.get("archive_path")
    if not isinstance(archive_rel, str) or Path(archive_rel).is_absolute():
        raise ValueError("R7_ARCHIVE_PATH_INVALID")
    archive_path = root / archive_rel
    if archive_path.is_symlink() or archive_path.resolve().parent != (root / "docs/eval/personal-dev-runs").resolve():
        raise ValueError("R7_ARCHIVE_PATH_INVALID")
    archive_bytes = archive_path.read_bytes()
    archive = json.loads(archive_bytes.decode("utf-8"))
    rules_path = root / profile.check_rules_path
    if rules_path.is_symlink():
        raise ValueError("R7_RULES_PATH_INVALID")
    rules, rules_sha = load_locked_rules(rules_path, case_id=case_id)
    if rules_sha != report.get("rules_sha256"):
        raise ValueError("R7_RULES_HASH_MISMATCH")
    recomputed = evaluate_diagnostic_record(case_id=case_id, archived=archive, plan=plan,
        locked_rules=rules, budget_snapshot=report.get("budget_snapshot"),
        cleanup_evidence=report.get("cleanup_evidence"))
    validate_diagnostic_check_result(recomputed, decision_policy=rules["decision_policy"])
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    matches = recomputed == report.get("checks") and archive_sha == report.get("archive_sha256")
    independent = {
        "schema_version": "1.0",
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED",
        "case_id": case_id,
        "diagnostic_round_id": ORDER_DIAGNOSTIC_ROUND_ID,
        "status": "RECOMPUTED_CONSISTENT" if matches else "RECOMPUTATION_MISMATCH",
        "execution_report_path": report_rel,
        "execution_report_sha256": report_sha,
        "raw_archive_sha256": archive_sha,
        "rules_sha256": rules_sha,
        "code_sha256": report.get("code_sha256"),
        "database_identity": report.get("database_identity"),
        "matches_execution_report": matches,
        "recomputed_checks": recomputed,
        "limitations": [
            "This is a non-scoring recomputation over the persisted synthetic Dev archive.",
            "Run lifecycle success does not establish task quality or formal approval.",
        ],
    }
    independent_rel = (Path(profile.report_dir) / f"{case_id}-independent-check-r7.json").as_posix()
    if write:
        target = root / independent_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(independent, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return independent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=("D1-ORDER-007", "D1-ORDER-009"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    result = recompute(case_id=args.case, write=not args.check_only)
    print(json.dumps({key: result[key] for key in (
        "case_id", "status", "execution_report_path", "execution_report_sha256",
        "raw_archive_sha256", "rules_sha256", "matches_execution_report",
    )}, ensure_ascii=False))
    return 0 if result["matches_execution_report"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
