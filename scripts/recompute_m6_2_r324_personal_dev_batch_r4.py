"""Deterministically recompute the single frozen R4 case from raw evidence.

Offline only: this script never loads model settings, opens the application DB,
or calls any provider. Its result is a same-rule reproducibility check, not an
independent evaluator.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import DATASET_REL, DATASET_SHA256, score_archive

PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def recompute() -> dict:
    manifest_path, rules_path = PKG / "batch-manifest-r4.json", PKG / "locked-rules-r4.json"
    state_path, auth_path = PKG / "execution-state-r2.json", PKG / "authorization-r2.json"
    journal_path = PKG / "execution-journal-r2.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "FROZEN" or auth.get("status") != "CLOSED_COMPLETED":
        raise ValueError("R4_BATCH_OR_AUTHORIZATION_NOT_CLOSED")
    case_ids = manifest.get("proposed_order")
    if case_ids != ["D1-POLICY-013"] or auth.get("case_ids") != case_ids:
        raise ValueError("R4_EXACT_CASE_SCOPE_MISMATCH")
    expected_code = implementation_digest(ROOT)
    if any((state.get("code_sha256") != expected_code,
            auth.get("code_sha256") != expected_code,
            manifest["bindings"].get("implementation_digest") != expected_code,
            state.get("dataset_sha256") != DATASET_SHA256,
            auth.get("dataset_sha256") != DATASET_SHA256,
            sha(ROOT / DATASET_REL) != DATASET_SHA256,
            state.get("manifest_sha256") != sha(manifest_path),
            state.get("rules_sha256") != sha(rules_path),
            auth.get("manifest_sha256") != sha(manifest_path),
            auth.get("rules_sha256") != sha(rules_path))):
        raise ValueError("R4_FROZEN_BINDING_MISMATCH")
    with journal_path.open("r", encoding="utf-8") as stream:
        journal = [json.loads(line) for line in stream if line.strip()]
    reserved = [row for row in journal if row.get("event") == "CASE_ATTEMPT_RESERVED"]
    closed = [row for row in journal if row.get("event") == "CASE_CLOSEOUT"]
    batches = [row for row in journal if row.get("event") == "BATCH_CLOSED"]
    if len(reserved) != 1 or len(closed) != 1 or len(batches) != 1:
        raise ValueError("R4_JOURNAL_NOT_EXACTLY_ONE_CLOSED_TRIAL")
    closeout = closed[0]
    archive_path = ROOT / closeout["archive_path"]
    if sha(archive_path) != closeout["archive_sha256"]:
        raise ValueError("R4_RAW_ARCHIVE_HASH_MISMATCH")
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    case = next(row for row in bundle["cases"] if row.get("case_id") == case_ids[0])
    budget = closeout["budget"]
    cleanup = closeout["cleanup"]
    # The journal's cleanup contract is CLEANED_AND_RELEASED; the grader's
    # historical input enum is RECEIPT_CLEANED_AND_LEASE_RELEASED. Translate
    # only this explicit positive status. Unknown/error states stay unknown.
    cleanup_status = ("RECEIPT_CLEANED_AND_LEASE_RELEASED"
                      if cleanup.get("cleanup_status") == "CLEANED_AND_RELEASED"
                      and cleanup.get("lease_released") is True
                      and cleanup.get("residual_case_rows") == 0
                      and cleanup.get("pending_actions") == 0
                      else cleanup.get("cleanup_status"))
    grade = score_archive(case, archive, budget_snapshot=budget, cleanup_status=cleanup_status)
    stored_path = ROOT / closeout["grader_path"]
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    if sha(stored_path) != closeout["grader_sha256"] or canonical(grade) != canonical(stored):
        raise ValueError("R4_CODE_GRADER_RECOMPUTE_MISMATCH")
    final_budget = batches[0]["final_budget"]
    if (not final_budget.get("complete") or not final_budget.get("settled") or
            final_budget.get("active_reservations") != 0 or final_budget.get("unknown_tokens") or
            final_budget.get("unknown_cost")):
        raise ValueError("R4_BUDGET_FINAL_SNAPSHOT_INCOMPLETE")
    return {
        "schema_version": "m6.2-r324-recompute-r1",
        "classification": "PERSONAL_DEV_PROVISIONAL_NOT_GRADED",
        "recompute_method": "same frozen code grader, replayed from bound raw archive; reproducibility check, not independent evaluator",
        "case_id": case_ids[0], "run_id": closeout["run_id"],
        "raw_archive_path": closeout["archive_path"], "raw_archive_sha256": sha(archive_path),
        "code_grade_path": closeout["grader_path"], "code_grade_sha256": sha(stored_path),
        "recomputed_code_grade": grade,
        "grade_content_identical": True,
        "authorization_status": auth["status"],
        "attempt_count": state["attempt_count"][case_ids[0]],
        "run_status": closeout["run_status"],
        "budget": final_budget,
        "cleanup": cleanup,
        "not_formal_gold_frozen_or_m2": True
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve(strict=False)
    if output.parent != (PKG / "reports").resolve(strict=False):
        raise SystemExit("OUTPUT_PATH_OUTSIDE_FIXED_PACKAGE")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = recompute()
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status":"PASS","report_path":output.relative_to(ROOT).as_posix(),
        "grade_content_identical":report["grade_content_identical"],
        "run_id":report["run_id"],"task_outcome":report["recomputed_code_grade"]["task_outcome"]},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
