"""Read-only R7 replay under the versioned budget/checker contract; never executes a case."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from eval.m6_1.budget import BudgetLedger, BudgetLimits
from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record, load_locked_rules


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
ROUND = ROOT / "docs/eval/d1-diagnostic-r7-orders-20261027"
DB_PATH = ROOT / "data/eval/d1-personal-budget.sqlite3"
OUTPUT = ROUND / "derived-reassessment-r4"
EXPECTED_DATASET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
CASES = {
    "D1-ORDER-007": {
        "archive": "docs/eval/personal-dev-runs/7f0f52c44c07413d84b12b4b7d0052b2.json",
        "report": "docs/eval/d1-diagnostic-r7-orders-20261027/reports/D1-ORDER-007-diagnostic-check-r7.json",
        "authorization": "docs/eval/d1-diagnostic-r7-orders-20261027/D1-ORDER-007-authorization-r7.json",
        "state": "docs/eval/d1-diagnostic-r7-orders-20261027/state/D1-ORDER-007-execution-state-r7.json",
        "budget": "docs/eval/d1-diagnostic-r7-orders-20261027/D1-ORDER-007-budget-r7.json",
        "experiment_id": "M61D1-ORDER007-R7-c1edca5e79d743b98a4ea15b50334b8d",
    },
    "D1-ORDER-009": {
        "archive": "docs/eval/personal-dev-runs/90a8f85a2efd4ad98f0d52777c357771.json",
        "report": "docs/eval/d1-diagnostic-r7-orders-20261027/reports/D1-ORDER-009-diagnostic-check-r7.json",
        "authorization": "docs/eval/d1-diagnostic-r7-orders-20261027/D1-ORDER-009-authorization-r7.json",
        "state": "docs/eval/d1-diagnostic-r7-orders-20261027/state/D1-ORDER-009-execution-state-r7.json",
        "budget": "docs/eval/d1-diagnostic-r7-orders-20261027/D1-ORDER-009-budget-r7.json",
        "experiment_id": "M61D1-ORDER009-R7-40083873e3da48a29762b6eab28728e2",
    },
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value, sha256_bytes(raw)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)


def budget_data(case_id: str, spec: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    budget_file, budget_sha = read_json(ROOT / spec["budget"])
    database_uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        locked_row = db.execute("SELECT payload_json FROM budget_limits WHERE experiment_id=?",
                                (spec["experiment_id"],)).fetchone()
        if locked_row is None:
            raise ValueError(f"locked budget absent for {case_id}")
        locked = json.loads(locked_row["payload_json"])
        events = [dict(row) for row in db.execute(
            "SELECT event_type,category,calls,tokens,estimated_usd,billed_usd,payload_json,reservation_id "
            "FROM budget_events WHERE experiment_id=? ORDER BY rowid", (spec["experiment_id"],)).fetchall()]
    if locked != budget_file:
        raise ValueError(f"budget file differs from persisted locked scope for {case_id}")
    snapshot = BudgetLedger.snapshot_from_events(events, BudgetLimits(**locked), locked)
    evidence = {"budget_path": spec["budget"], "budget_file_sha256": budget_sha,
        "locked_budget": locked, "event_count": len(events), "events": events,
        "event_payload_sha256": canonical_hash(events), "budget_snapshot_v2": snapshot}
    return snapshot, evidence


def reassess(case_id: str, spec: dict[str, str], rules: dict[str, Any], r7_closeout: dict[str, Any]) -> dict[str, Any]:
    archive, archive_sha = read_json(ROOT / spec["archive"])
    old_report, old_report_sha = read_json(ROOT / spec["report"])
    authorization, authorization_sha = read_json(ROOT / spec["authorization"])
    state, state_sha = read_json(ROOT / spec["state"])
    case_closeout = r7_closeout["cases"][case_id]
    for label, value, expected in (
        ("archive", archive_sha, case_closeout.get("archive_sha256")),
        ("report", old_report_sha, case_closeout.get("diagnostic_report_sha256")),
        ("authorization", authorization_sha, case_closeout.get("authorization_sha256")),
        ("state", state_sha, case_closeout.get("execution_state_sha256")),
    ):
        if expected and value != expected:
            raise ValueError(f"R7 {label} hash mismatch for {case_id}")
    if (authorization.get("status") != case_closeout.get("authorization_status") or
            state.get("status") != case_closeout.get("execution_state_status") or
            authorization.get("status") not in {"CLOSED_COMPLETED", "CLOSED_STOPPED"} or
            state.get("status") != authorization.get("status")):
        raise ValueError(f"R7 historical authorization/state mismatch for {case_id}")
    snapshot, budget_evidence = budget_data(case_id, spec)
    case_rules = rules["cases"][case_id]
    plan = {"case_id": case_id}
    expected = case_rules.get("expected_fixture", {})
    if expected.get("owner_id"):
        plan["owner_id"] = expected["owner_id"]
    cleanup = old_report.get("cleanup_evidence")
    checks_a = evaluate_diagnostic_record(case_id=case_id, archived=archive, plan=plan,
        locked_rules=rules, budget_snapshot=snapshot, cleanup_evidence=cleanup)
    checks_b = evaluate_diagnostic_record(case_id=case_id, archived=archive, plan=plan,
        locked_rules=rules, budget_snapshot=snapshot, cleanup_evidence=cleanup)
    if canonical_hash(checks_a) != canonical_hash(checks_b):
        raise ValueError(f"new checker replay is not deterministic for {case_id}")
    app = archive.get("result", {}).get("result", {}).get("result", {})
    answer = app.get("answer") if isinstance(app, dict) else None
    return {
        "schema_version": "1.0",
        "phase": "DERIVED_REASSESSMENT",
        "replay_only": True,
        "execution_closeout": False,
        "eligible_as_execution_predecessor": False,
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED",
        "case_id": case_id,
        "diagnostic_round_id": r7_closeout["round_id"],
        "historical_run_id": case_closeout.get("run_id"),
        "historical_authorization_status": authorization.get("status"),
        "historical_execution_state_status": state.get("status"),
        "historical_decision_preserved": case_closeout.get("decision"),
        "historical_checker_budget_status": old_report.get("checks", {}).get("execution_checks", {}).get(
            "budget_limits_respected", "UNKNOWN"),
        "historical_answer_audit": {
            "answer_present": isinstance(answer, str),
            "contains_none_or_null_sentinel": isinstance(answer, str) and
                ("None" in answer or "null" in answer.casefold()),
            "contains_unqualified_current_status_phrase": isinstance(answer, str) and "当前状态为" in answer,
            "historical_answer_modified": False,
        },
        "input_hashes": {"raw_archive_sha256": archive_sha,
            "historical_execution_report_sha256": old_report_sha,
            "historical_authorization_sha256": authorization_sha,
            "historical_execution_state_sha256": state_sha,
            "r7_rules_sha256": r7_closeout["scope"]["locked_rules_sha256"],
            "r3_2_4_dataset_sha256": EXPECTED_DATASET_SHA256},
        "budget_evidence": budget_evidence,
        "checks": checks_a,
        "independent_replay": {"recomputed_twice": True,
            "checks_canonical_sha256": canonical_hash(checks_a),
            "same_as_historical_report": False},
        "limitations": [
            "This derived replay does not create a new Run, execution closeout, authorization, or predecessor CONTINUE.",
            "Semantic review remains UNKNOWN and no formal task score or reviewer decision is produced.",
            "ORDER-007 historical answer defects are recorded without changing the raw archive or R7 report.",
            "Provider billing remains UNKNOWN where no supplier invoice evidence was recorded.",
        ],
    }


def main() -> int:
    dataset_sha = sha256_bytes(DATASET.read_bytes())
    if dataset_sha != EXPECTED_DATASET_SHA256:
        raise SystemExit("D1 dataset hash mismatch; no reassessment written")
    closeout, closeout_sha = read_json(ROUND / "closeout-r7.json")
    rules_path = ROOT / closeout["scope"]["locked_rules_path"]
    rules, rules_sha = load_locked_rules(rules_path, case_id="D1-ORDER-007")
    if rules_sha != closeout["scope"]["locked_rules_sha256"]:
        raise SystemExit("R7 rules hash mismatch; no reassessment written")
    records = [reassess(case_id, spec, rules, closeout) for case_id, spec in CASES.items()]
    OUTPUT.mkdir(parents=True, exist_ok=False)
    reports_dir = OUTPUT / "reports"
    reports_dir.mkdir()
    for record in records:
        target = reports_dir / f"{record['case_id']}-derived-reassessment-r4.json"
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    summary = {"schema_version": "1.0", "phase": "DERIVED_REASSESSMENT", "replay_only": True,
        "round_id": closeout["round_id"], "r7_closeout_sha256": closeout_sha,
        "dataset_sha256": dataset_sha, "new_checker_version": records[0]["checks"]["checker_version"],
        "historical_r7_auth_states_preserved": True, "records": [
            {"case_id": row["case_id"], "historical_run_id": row["historical_run_id"],
             "historical_authorization_status": row["historical_authorization_status"],
             "historical_decision": row["historical_decision_preserved"]["outcome"],
             "new_derived_decision": row["checks"]["decision"]["outcome"],
             "new_budget_status": row["checks"]["execution_checks"]["budget_limits_respected"],
             "new_answer_unknown_money_status": row["checks"].get("evidence_checks", {}).get(
                 "unknown_amount_not_rendered_as_value", "NOT_APPLICABLE"),
             "provider_requests": row["budget_evidence"]["budget_snapshot_v2"]["provider_requests"],
             "business_tool_calls": row["budget_evidence"]["budget_snapshot_v2"]["calls_by_category"]["tool"],
             "tokens": row["budget_evidence"]["budget_snapshot_v2"]["tokens"],
             "local_estimated_usd": row["budget_evidence"]["budget_snapshot_v2"]["estimated_usd"],
             "supplier_billing": row["budget_evidence"]["budget_snapshot_v2"]["billed_status"],
             "report_path": f"reports/{row['case_id']}-derived-reassessment-r4.json"}
            for row in records]}
    with (OUTPUT / "summary-r4.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    lines = ["# R7 budget/checker derived reassessment (replay only)", "",
        f"- Source closeout SHA-256: `{closeout_sha}`",
        f"- r3.2.4 dataset SHA-256: `{dataset_sha}`",
        f"- Checker: `{summary['new_checker_version']}`",
        "- Classification: `PROVISIONAL_DEV_CHECKS`; grading: `NOT_GRADED`.",
        "- This package is not an execution closeout and is not eligible as an execution predecessor.", "",
        "| Case | Historical decision/auth | Derived decision | Budget | Provider | Tool | Tokens | Local estimate | Billing |",
        "|---|---|---|---|---:|---:|---:|---:|---|"]
    for row in summary["records"]:
        lines.append(f"| {row['case_id']} | {row['historical_decision']} / {row['historical_authorization_status']} | "
            f"{row['new_derived_decision']} | {row['new_budget_status']} | {row['provider_requests']} | "
            f"{row['business_tool_calls']} | {row['tokens']} | {row['local_estimated_usd']} | {row['supplier_billing']} |")
    lines += ["", "ORDER-007's new derived report makes the historical `None` amount visible as a failed response check; its old report and answer remain untouched.",
        "ORDER-009's explicit zero tool count is accepted only because the scoped locked budget and complete settled ledger both exist and the archived run-event list contains no business tool events.",
        "Any derived `CONTINUE` is diagnostic-only and cannot satisfy run sequencing or authorize another case.", ""]
    (OUTPUT / "summary-r4.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    manifest_paths = sorted(path for path in OUTPUT.rglob("*") if path.is_file())
    manifest_lines = [f"{sha256_bytes(path.read_bytes())}  {path.relative_to(OUTPUT).as_posix()}" for path in manifest_paths]
    (OUTPUT / "SHA256SUMS-r4.md").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "DERIVED_REASSESSMENT_WRITTEN", "output": OUTPUT.relative_to(ROOT).as_posix(),
        "records": summary["records"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
