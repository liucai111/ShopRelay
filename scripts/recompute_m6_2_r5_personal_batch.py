"""Recompute the closed R5 M6.2 personal batch from immutable source archives."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev_batch_m62 import score_archive

PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029"
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
MATRIX = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"
R4 = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def observability_summary(archive: dict) -> tuple[list[dict], int]:
    observations = archive.get("result", {}).get("observations", {})
    events: list[dict] = []
    handoffs = 0
    for row in observations.get("observability_records", []):
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        event_type = payload.get("event_type")
        node = payload.get("node")
        if isinstance(event_type, str) and (event_type.startswith("node_") or
                event_type.startswith("tool_") or "handoff" in event_type.lower()):
            events.append({"event_type": event_type, "node": node,
                "error_code": payload.get("error_code"), "timestamp": payload.get("timestamp")})
            if "handoff" in event_type.lower():
                handoffs += 1
    return events, handoffs


def elapsed_ms(archive: dict) -> float | None:
    executions = archive.get("result", {}).get("observations", {}).get("graph_executions", [])
    if len(executions) != 1:
        return None
    started, finished = executions[0].get("started_at"), executions[0].get("finished_at")
    if not started or not finished:
        return None
    try:
        return round((datetime.fromisoformat(finished.replace("Z", "+00:00")) -
            datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() * 1000, 3)
    except (TypeError, ValueError):
        return None


def main() -> int:
    bundle, matrix, manifest = read(DATASET), read(MATRIX), read(PKG / "batch-manifest-r5-final8.json")
    rules, auth, state = read(PKG / "locked-rules-r5-final8.json"), read(PKG / "authorization-r5.json"), read(PKG / "execution-state-r5.json")
    admission = read(PKG / "application-admission-r5.json")
    repository_preflight_source = read(ROOT / manifest["repository_preflight_binding"]["path"])
    journal = [json.loads(line) for line in (PKG / "execution-journal-r5.jsonl").read_text(encoding="utf-8").splitlines()]
    cases = {row["case_id"]: row for row in bundle["cases"]}
    matrix_rows = {row["case_id"]: row for row in matrix["cases"]}
    by_case = {row["case_id"]: row for row in admission["repository_case_results"]}
    repository_rows = {row["case_id"]: row for row in repository_preflight_source["repository_case_results"]}
    closes = {row["case_id"]: row for row in journal if row.get("event") == "CASE_CLOSEOUT"}
    offline = read(PKG / "application-admission-offline-r1.json")
    offline_rows = {row["case_id"]: row for row in offline["records"]}
    selected = manifest["proposed_order"]
    if (auth.get("status") != "CLOSED_STOPPED" or
            list(state.get("attempt_count", {})) != selected or len(closes) != 5):
        raise SystemExit("R5_SOURCE_STATE_MISMATCH")

    semantic = {
        "D1-PRODUCT-002": {"suggestion": "PASS", "reason": "The delivered answer says 320 ml and the same-run get_product_detail result supports capacity_ml=320. It does not invent the other catalog fields; those fields are immaterial to the requested comparison.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"},
        "D1-PRODUCT-001": {"suggestion": "PASS", "reason": "The answer identifies Luma-A at 410 g versus Luma-B at 460 g, states the 50 g difference, and cites both same-run detail result_refs. The route is deterministic RULE and made three allowed reads; this is not an LLM-answer trial.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"},
        "D1-PRODUCT-003": {"suggestion": "PARTIAL", "reason": "The catalog supports Cedar Organizer at 18 credits and depth 12 cm, and the answer marks fit as unconfirmed. It does not establish that 12 cm fits the user's unspecified narrow shelf or ask for the shelf measurement; recommendation is therefore only partly grounded.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"},
        "D1-PRODUCT-004": {"suggestion": "PASS", "reason": "The inventory result is UNAVAILABLE with INVENTORY_FRESHNESS_UNVERIFIED and no observation time. The delivered answer distinguishes the stored count of 3 from current availability and does not claim in-stock or out-of-stock now.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"},
        "D1-PRODUCT-006": {"suggestion": "FAIL", "reason": "The user supplied a product attribute and asked whether two 65 W values match. The model route returned FALLBACK with ValueError, no product tool ran, and the response did not compare the values. This is a route/provider-contract failure; it is not evidence that the model made a factual claim.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"},
    }

    case_results = []
    recompute_pairs = []
    for case_id in [case_id for case_id in selected if case_id in closes]:
        close = closes[case_id]
        archive_path = ROOT / close["archive_path"]
        archive = read(archive_path)
        grade_path = ROOT / close["grader_path"]
        stored_grade = read(grade_path)
        first = score_archive(cases[case_id], archive, budget_snapshot=close["budget"],
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        second = score_archive(cases[case_id], archive, budget_snapshot=close["budget"],
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        stable = first == second == stored_grade
        if not stable:
            raise SystemExit(f"R5_SCORE_RECOMPUTE_MISMATCH:{case_id}")
        recompute_pairs.append({"case_id": case_id, "stored_score_sha256": sha(grade_path),
            "recompute_1_equals_stored": first == stored_grade,
            "recompute_2_equals_stored": second == stored_grade, "result": "MATCH"})
        result = archive["result"]
        payload = result.get("result", {})
        observations = result.get("observations", {})
        nodes, handoffs = observability_summary(archive)
        case_results.append({
            "case_id": case_id, "group_id": cases[case_id].get("group_id"),
            "dataset_version": bundle["version"], "case_sha256": hashlib.sha256(
                json.dumps(cases[case_id], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "question": cases[case_id]["input"].get("user_query"), "run_id": result.get("run_id"),
            "trace_id": payload.get("trace_id"), "run_status": result.get("status"),
            "route": payload.get("route"), "actual_model_requests": close["budget"].get("calls_by_category", {}).get("provider"),
            "token_usage": close["budget"].get("tokens"), "local_estimated_usd": close["budget"].get("estimated_usd"),
            "vendor_invoice": "UNKNOWN", "business_read_calls": close["budget"].get("calls_by_category", {}).get("tool"),
            "business_write_calls": 0, "judge_calls": close["budget"].get("calls_by_category", {}).get("judge"),
            "embedding_calls": close["budget"].get("calls_by_category", {}).get("embedding"),
            "elapsed_ms": elapsed_ms(archive), "delivered_answer": payload.get("answer"),
            "tool_references": payload.get("tool_references"), "tool_call_audit": payload.get("tool_call_audit"),
            "citations": payload.get("citations"), "run_event_count": len(observations.get("run_events", [])),
            "node_and_tool_events": nodes, "handoff_event_count": handoffs,
            "program_score": first, "ai_semantic_review": semantic[case_id],
            "archive_path": close["archive_path"], "archive_sha256": close["archive_sha256"],
            "cleanup": close["cleanup"], "grade_path": close["grader_path"],
            "grade_sha256": close["grader_sha256"], "classification": close.get("classification")})

    # Retain R4 as a prior binding, never merge it into the R5 outcome denominator.
    r4_manifest = read(R4 / "batch-manifest-r4.json")
    r4_run = "02650693-694f-4e1d-94ba-eb5463c4de81"
    r4_closeout = read(R4 / "reports" / "closeout-r4.json")
    r4_record = {"case_id": "D1-POLICY-013", "run_id": r4_run,
        "archive_path": "docs/eval/personal-dev-runs/08420baa7f3e4b10b85d75ca7d4de5b6.json",
        "code_sha256": r4_manifest.get("bindings", {}).get("implementation_digest"),
        "batch_id": r4_manifest.get("batch_id"), "prior_binding_only": True,
        "grading_status": "NOT_GRADED", "outcome": "PROVISIONAL_PARTIAL",
        "closeout_sha256": sha(R4 / "reports" / "closeout-r4.json"),
        "classification": "HISTORICAL_R4_NOT_IN_R5_DENOMINATOR"}

    ledger = []
    executed = {row["case_id"] for row in case_results}
    app_blocked = {row["case_id"] for row in offline["records"] if row.get("application_admission") != "APPLICATION_READY"}
    repo_blocked = set(manifest.get("repository_preflight_binding", {}).get("blocked_case_ids", []))
    selected_set = set(selected)
    for case_id in [row["case_id"] for row in matrix["cases"]]:
        original = matrix_rows[case_id]
        if case_id == "D1-POLICY-013":
            status, detail = "HISTORICAL_EXECUTED_R4", r4_record
        elif case_id in executed:
            status, detail = "EXECUTED_R5", next(row for row in case_results if row["case_id"] == case_id)
        elif case_id in app_blocked:
            status, detail = "BLOCKED_APPLICATION_ADMISSION", offline_rows[case_id]
        elif case_id in repo_blocked:
            status, detail = "BLOCKED_REPOSITORY_PREFLIGHT", repository_rows[case_id]
        elif case_id in selected_set:
            status, detail = "NOT_RUN_BATCH_STOP", {"reason": "R5 batch stopped after PRODUCT-006 provider/route contract and critical evidence failure; no attempt reserved for this case."}
        elif original.get("admission_status") != "READY":
            status, detail = original.get("admission_status"), {"source_matrix_reason": original.get("blocking_reason") or original.get("blockers")}
        else:
            raise SystemExit(f"R5_CASE_LEDGER_UNCLASSIFIED:{case_id}")
        ledger.append({"case_id": case_id, "group_id": original.get("group_id"),
            "original_admission_status": original.get("admission_status"), "final_status": status,
            "detail": detail})
    if len(ledger) != 40:
        raise SystemExit("R5_CASE_LEDGER_NOT_CONSERVED")
    counts: dict[str, int] = {}
    for row in ledger:
        counts[row["final_status"]] = counts.get(row["final_status"], 0) + 1
    if counts.get("EXECUTED_R5") != 5 or counts.get("NOT_RUN_BATCH_STOP") != 22 or len(ledger) != 40:
        raise SystemExit("R5_EXPECTED_COVERAGE_COUNTS_MISMATCH")

    final_journal = next(row for row in journal if row.get("event") == "BATCH_CLOSED")
    batch_budget = final_journal["final_budget"]
    if batch_budget.get("calls_by_category", {}).get("provider") != 3 or batch_budget.get("tokens") != 2510:
        raise SystemExit("R5_BATCH_BUDGET_RECOMPUTE_MISMATCH")
    summary = {"schema_version": "m6.2-r324-r5-batch-closeout-r1", "classification": manifest["classification"],
        "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
        "batch_id": manifest["batch_id"], "authorization_id": auth["authorization_id"],
        "authorization_final_status": auth["status"], "implementation_digest": manifest["bindings"]["implementation_digest"],
        "dataset_sha256": manifest["dataset"]["sha256"], "planned_case_count": len(selected),
        "executed_case_count": len(case_results), "not_run_selected_count": len(selected)-len(case_results),
        "original_40_case_conservation": {"total": 40, "counts": counts},
        "r4_policy013_separate_historical_record": r4_record,
        "r5_actuals": {"provider_requests": batch_budget["calls_by_category"]["provider"],
            "business_read_calls": batch_budget["calls_by_category"]["tool"], "tokens": batch_budget["tokens"],
            "local_estimated_usd": batch_budget["estimated_usd"], "vendor_invoice": batch_budget.get("billed_status"),
            "active_reservations": batch_budget.get("active_reservations"), "judge": batch_budget.get("judge_requests"),
            "embedding": batch_budget.get("embedding_requests"), "budget_snapshot": batch_budget},
        "stop": next((row for row in journal if row.get("event") == "BATCH_STOP"), None),
        "case_results_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/case-results-r5-r1.json",
        "recompute_consistency_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/recompute-consistency-r5-r1.json",
        "formal_gates_changed": False, "candidate_or_frozen_access": False}

    reports = PKG / "reports"
    write_new(reports / "case-results-r5-r1.json", {"schema_version": "m6.2-r324-r5-case-results-r1",
        "cases": case_results})
    write_new(reports / "40-case-coverage-ledger-r5-r1.json", {"schema_version": "m6.2-r324-r5-40-case-ledger-r1",
        "source_matrix_sha256": sha(MATRIX), "rows": ledger, "counts": counts,
        "R4_and_R5_quality_denominators_are_separate": True})
    write_new(reports / "recompute-consistency-r5-r1.json", {"schema_version": "m6.2-r324-r5-recompute-r1",
        "recomputes": recompute_pairs, "all_match": all(row["result"] == "MATCH" for row in recompute_pairs),
        "batch_provider_requests": batch_budget["calls_by_category"]["provider"],
        "batch_tools": batch_budget["calls_by_category"]["tool"], "batch_tokens": batch_budget["tokens"],
        "batch_estimated_usd": batch_budget["estimated_usd"], "vendor_invoice": "UNKNOWN"})
    write_new(reports / "batch-closeout-r5-r1.json", summary)
    print(json.dumps({"closeout": "CLOSED_STOPPED", "executed": 5, "not_run": 22,
        "coverage_counts": counts, "recompute_all_match": True,
        "provider_requests": batch_budget["calls_by_category"]["provider"],
        "tools": batch_budget["calls_by_category"]["tool"], "tokens": batch_budget["tokens"],
        "estimated_usd": batch_budget["estimated_usd"], "reports": [
            "reports/case-results-r5-r1.json", "reports/40-case-coverage-ledger-r5-r1.json",
            "reports/recompute-consistency-r5-r1.json", "reports/batch-closeout-r5-r1.json"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
