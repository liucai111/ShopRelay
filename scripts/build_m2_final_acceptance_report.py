"""Reconcile only user-confirmed M2 dev/validation review artifacts and allowed probes."""
from __future__ import annotations

import hashlib
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTE_DATASET = ROOT / "eval/dataset/route-m2.1-dev-validation-user-confirmed-r2.json"
ROUTE_RUN = ROOT / "eval/raw/route-m2.1-20261021T041047425141Z-b37dcd99.json"
ANSWER_IMPORT = ROOT / "eval/fidelity/m2.4-q2-human-adjudication-user-confirmed-r2.json"
CONFIRMATION = ROOT / "docs/review/ShopRelay_批量审核确认记录_20261021.json"
REAL_RUN = ROOT / "docs/probes/m2-r-real-20261021T040811083648Z.json"
OUTPUT = ROOT / "docs/probes/m2-final-acceptance-20261021-r1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build() -> dict:
    from scripts.evaluate_route_m2_1 import load_reviewed_dataset

    route_data = load_reviewed_dataset(ROUTE_DATASET)
    route_run = _read(ROUTE_RUN)
    answers = _read(ANSWER_IMPORT)
    confirmation = _read(CONFIRMATION)
    real = _read(REAL_RUN)
    route_meta = route_data["route_review_import"]
    route_formal = route_run["formal_metrics"]
    answer_metrics = answers["metrics"]

    if route_run.get("dataset_sha256_canonical_cases") != route_data.get("dataset_sha256_canonical_cases"):
        raise ValueError("route evaluation is not bound to the imported confirmed dataset")
    if route_run.get("approved_route_label_count") != 52:
        raise ValueError("route evaluation did not score exactly 52 approved rows")
    if route_meta.get("test_candidate_rows_included") is not False:
        raise ValueError("route import includes disallowed test-candidate rows")
    if len(answers.get("adjudications", [])) != 8:
        raise ValueError("answer adjudication import must contain exactly 8 items")

    target_id = "pr2-refund_reason_hard_negative-02"
    target_runs = [row for row in real.get("chat_runs", []) if row.get("case_id") == target_id]
    success = [row for row in target_runs if row.get("run_status") == "SUCCEEDED" and
               row.get("actual_strategy") == "SINGLE" and row.get("answer_kind") == "POLICY_ANSWER"]
    failed = [row for row in target_runs if row.get("run_status") != "SUCCEEDED"]
    target = success[-1] if success else None
    model_requests = [row for row in real.get("model_requests", [])
                      if row.get("sequence", 0) > (real.get("request_totals", {}).get("prior_m2_r_request_count", 0))]
    if target is not None:
        if not target.get("policy_answer_valid") or not target.get("citation_ids_subset_of_retrieval"):
            raise ValueError("successful hard-negative run lacks valid answer/current citations")
        if not target.get("run_status_matches_db") or not target.get("sse_order_valid"):
            raise ValueError("hard-negative Run/SSE state is inconsistent")
        if not (target.get("restart_recovery") or {}).get("passed"):
            raise ValueError("hard-negative restart/idempotency recovery did not pass")

    return {
        "record_type": "m2_final_acceptance_reconciliation",
        "version": "r1",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_confirmation": {
            "path": CONFIRMATION.relative_to(ROOT).as_posix(),
            "sha256": sha256(CONFIRMATION),
            "actor_marker": confirmation.get("confirmation_actor"),
            "identity_verification": confirmation.get("identity_verification"),
            "record_written_at_utc": confirmation.get("record_written_at_utc"),
            "per_item_review_times_recorded": False,
            "independent_blind_review": confirmation.get("independent_blind_review"),
        },
        "route_review": {
            "status": "IMPORTED_USER_CONFIRMED",
            "dataset": ROUTE_DATASET.relative_to(ROOT).as_posix(),
            "dataset_sha256_canonical_cases": route_data.get("dataset_sha256_canonical_cases"),
            "csv_sha256": route_meta.get("csv_sha256"),
            "reviewed_count": route_meta.get("case_count"),
            "split_counts": route_meta.get("split_counts"),
            "decision_counts": route_meta.get("decision_counts"),
            "corrections": {
                "route-reuse-pr2-delivery_policy_limits-01": "SINGLE/BM25/POLICY_QUESTION",
                "route-reuse-pr2-multi_clause_return_refund-01": "MULTI/MULTI/MULTI_POLICY_CONDITIONS",
                "route-reuse-pr2-multi_clause_return_refund-02": "MULTI/MULTI/MULTI_POLICY_CONDITIONS",
            },
            "metrics_record": ROUTE_RUN.relative_to(ROOT).as_posix(),
            "metrics_record_sha256": sha256(ROUTE_RUN),
            "label_scope": route_meta.get("label_scope"),
            "separate_blind_review": False,
            "route_quality_scope_warning": "These labels cover only policy-question SINGLE/MULTI behavior; they do not constitute a five-way DIRECT/SINGLE/MULTI/AGENT/REFUSE gold set.",
            "metrics": route_formal,
            "diagnostic_on_unapproved_labels": route_run.get("unapproved_label_diagnostic_only"),
            "api_calls": route_run.get("model_api_calls"),
        },
        "answer_review": {
            "status": "IMPORTED_ADJUDICATED_DISPUTE_SUBSET",
            "dataset": ANSWER_IMPORT.relative_to(ROOT).as_posix(),
            "dataset_sha256": sha256(ANSWER_IMPORT),
            "reviewed_items": len(answers.get("adjudications", [])),
            "metrics": answer_metrics,
            "all_answer_claims_scored": False,
            "independent_blind_review": False,
        },
        "hard_negative_post_fix_smoke": {
            "case_id": target_id,
            "prior_failure_preserved": bool(failed),
            "prior_failure_run_ids": [row.get("run_id") for row in failed],
            "new_success_count": len(success),
            "status": "PASS_CASE_ONLY" if target else "BLOCKED",
            "run": ({key: target.get(key) for key in (
                "run_id", "trace_id", "orchestrator_mode", "actual_strategy", "run_status",
                "answer_kind", "answer", "citations", "required_evidence_ids",
                "required_evidence_coverage", "required_evidence_citation_coverage",
                "citation_ids_subset_of_retrieval", "policy_answer_valid", "sse_event_types",
                "run_status_matches_db", "idempotent_replay", "restart_recovery", "usage",
            )} if target else None),
            "new_model_request_count": len(model_requests),
            "model_requests": model_requests,
            "stage_request_totals": real.get("request_totals"),
            "whole_probe_status": real.get("status"),
            "whole_probe_note": "The immutable raw probe has top-level PASS and all recorded verification checks are true. This targeted case result is separately reported and does not rewrite the original M2-R selected-set quality denominator.",
        },
        "historical_quality_context": {
            "m2_r_selected_set_original": "5/6 valid policy answers; one hard-negative ROUTE_EXECUTOR_UNAVAILABLE failure; one behavior-only case excluded.",
            "post_fix_interpretation": "The specific dev hard-negative now has a successful legacy SINGLE regression. The original selected-set result is immutable and is not silently rewritten to 6/6.",
            "historical_self_rag": "BLOCKED historical quality result; Self-RAG is not the M2-R online path and its prior failures remain unchanged.",
        },
        "test_candidate_boundary": {
            "scored": False,
            "used_for_tuning": False,
            "modified": False,
            "access_disclosure": "During a source-path inspection, a truncated read of the route candidate JSON exposed its header and first test_candidate row. It was not used, scored, or modified; no further candidate rows were inspected. Do not claim this artifact remained unread; assess whether it must be resealed before any future blind test.",
        },
        "release_decision": {
            "m2_r_engineering": "PASS",
            "m2_r_case_level_hard_negative_regression": "PASS",
            "m2_route_metrics": "MEASURED_ON_USER_CONFIRMED_DEV_VALIDATION_POLICY_SUBSET",
            "m2_answer_dispute_adjudication": "IMPORTED_8_ITEMS_WITH_1_UNSUPPORTED_CLAIM",
            "m2_formal_quality": "BLOCKED",
            "m2_formal_release": "BLOCKED",
            "m3_start": "BLOCKED_NOT_AUTHORIZED",
            "reasons": [
                "The 52 labels cover only policy-question SINGLE/MULTI behavior, not the full five-way router or independent blind evaluation.",
                "The answer import covers eight disputed claims only; one out-of-scope claim is unsupported and this does not score complete answers or independent generalization.",
                "The M2-R quality gate still lacks a separately approved numeric threshold for the revised architecture; the ADR forbids automatic threshold reduction.",
                "The 60-case test candidate remains unscored and unfrozen; the disclosed accidental exposure means its future blind-test status needs a user decision.",
            ],
        },
        "inputs": {
            "route_confirmation_csv": "eval/review/ShopRelay_路由审核_用户确认版_52条.csv",
            "route_confirmation_csv_sha256": route_meta.get("csv_sha256"),
            "answer_confirmation_csv": "eval/fidelity/m2.4-q2-human-review-user-confirmed.csv",
            "answer_confirmation_csv_sha256": answers.get("inputs", {}).get("confirmed_csv_sha256"),
            "route_dataset": ROUTE_DATASET.relative_to(ROOT).as_posix(),
            "answer_import": ANSWER_IMPORT.relative_to(ROOT).as_posix(),
            "real_run_record": REAL_RUN.relative_to(ROOT).as_posix(),
            "real_run_record_sha256": sha256(REAL_RUN),
            "test_candidate_rows_scored": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT.relative_to(ROOT).as_posix(),
                        help="new versioned report path; existing files are never overwritten")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if ROOT not in output.parents:
        raise SystemExit("output must remain inside ShopRelay")
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing record: {output.relative_to(ROOT)}")
    report = build()
    suffix = output.stem.rsplit("-r", 1)[-1]
    if suffix.isdigit():
        report["version"] = f"r{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": output.relative_to(ROOT).as_posix(),
                      "route_rows": report["route_review"]["reviewed_count"],
                      "answer_items": report["answer_review"]["reviewed_items"],
                      "hard_negative_status": report["hard_negative_post_fix_smoke"]["status"],
                      "m2_release": report["release_decision"]["m2_formal_release"],
                      "m3": report["release_decision"]["m3_start"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
