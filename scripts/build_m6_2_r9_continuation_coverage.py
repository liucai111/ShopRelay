"""Build an additive 40-case coverage view from the R8 ledger and R9 reconciliation."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
R8 = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029/reports/40-case-coverage-ledger-r8-r1.json")
REC = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/reconciliation-r9-continuation-r1.json")
OUT = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/40-case-coverage-ledger-r9-continuation-r1.json")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    if (ROOT / OUT).exists():
        raise SystemExit(f"refusing to overwrite {OUT}")
    source, reconciliation = read(ROOT / R8), read(ROOT / REC)
    updates = {r["case_id"]: r for r in reconciliation["cases"]}
    no_run_case = "D1-POLICY-018"
    planned = set(reconciliation["r9_case_ids"]) | {no_run_case}
    r8_remainder = set(reconciliation["r8_remaining_case_ids"])
    if planned != r8_remainder or len(source["rows"]) != 40:
        raise SystemExit("40-case conservation or R8 remainder binding failed")
    output_rows = []
    for original in source["rows"]:
        row = dict(original)
        case_id = row["case_id"]
        if case_id in updates:
            result = updates[case_id]
            row["continuation_update_r9"] = {
                "status": "EXECUTED_ONCE_R9S4",
                "run_id": result["run_id"],
                "run_status": result["run_status"],
                "code_task_outcome": result["program_grade_recomputed"]["task_outcome"],
                "ai_prereview_only": result["ai_assisted_semantic_prereview"]["status"],
                "archive_path": result["archive_path"],
                "archive_sha256": result["archive_sha256"],
                "cleanup_status": result["cleanup"]["cleanup_status"],
            }
        elif case_id == no_run_case:
            row["continuation_update_r9"] = {
                "status": "ATTEMPTED_NO_RUN_CONSUMED",
                "attempt": 1,
                "run_created": False,
                "reason": "Pre-execution NameError in an earlier R9 segment; attempt remains consumed and was not replayed.",
            }
        else:
            row["continuation_update_r9"] = {"status": "UNCHANGED_FROM_R8_LEDGER"}
        output_rows.append(row)
    counts = Counter(row["continuation_update_r9"]["status"] for row in output_rows)
    result = {
        "schema_version": "m6.2-r324-40case-coverage-ledger-r9-continuation-v1",
        "classification": "PERSONAL_DEV_COVERAGE_LEDGER_NOT_FORMAL_EVALUATION",
        "dataset_sha256": source["dataset_sha256"],
        "case_universe_count": 40,
        "preserves_original_r8_rows_and_statuses": True,
        "source_r8_ledger": R8.as_posix(),
        "source_r8_ledger_sha256": sha(ROOT / R8),
        "source_r9_reconciliation": REC.as_posix(),
        "source_r9_reconciliation_sha256": sha(ROOT / REC),
        "rows": output_rows,
        "continuation_update_counts": dict(sorted(counts.items())),
        "r9_run_count": len(updates),
        "r9_attempted_without_run_count": 1,
        "r9_stop_violation": reconciliation["stop_rule_reconciliation"]["violation"],
        "quality_metrics_by_binding_not_merged": True,
        "limitations": [
            "R9s4's SAFETY-038 STOP was not enforced before SAFETY-039 and SAFETY-040; this deviation is preserved.",
            "Code task outcomes, AI-assisted prereview, and human decisions are separate fields.",
            "This additive ledger does not combine historical code/rule bindings into a unified pass rate.",
        ],
    }
    (ROOT / OUT).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": OUT.as_posix(), "rows": len(output_rows), "updates": dict(counts)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
