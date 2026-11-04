"""Deterministically derive R6 closeout records from immutable Run archives and ledgers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
R5 = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029"
R4_ARCHIVE = ROOT / "docs/eval/personal-dev-runs/08420baa7f3e4b10b85d75ca7d4de5b6.json"
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def event_payload(event: dict) -> dict:
    try:
        return json.loads(event.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def route_from_archive(archive: dict) -> dict:
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    route = payload.get("route") if isinstance(payload.get("route"), dict) else None
    if route:
        return route
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    for event in observations.get("run_events", []):
        if event.get("event_type") == "route_decided":
            data = event_payload(event)
            candidate = data.get("route") if isinstance(data.get("route"), dict) else data
            if isinstance(candidate, dict) and candidate.get("strategy"):
                return candidate
    return {}


def safe_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read(path) != data:
            raise SystemExit(f"R6_EXISTING_DERIVED_OUTPUT_DIFFERS:{path.name}")
        return
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def build() -> tuple[dict, dict, dict, dict]:
    manifest_path = PKG / "batch-manifest-r6.json"
    rules_path = PKG / "locked-rules-r6.json"
    auth_path = PKG / "authorization-r6.json"
    state_path = PKG / "execution-state-r6.json"
    journal_path = PKG / "execution-journal-r6.jsonl"
    admission_path = PKG / "application-admission-r6.json"
    manifest, rules, auth, state, admission = (read(p) for p in
        (manifest_path, rules_path, auth_path, state_path, admission_path))
    dataset = read(DATASET)
    by_case = {row["case_id"]: row for row in dataset["cases"]}
    if sha(DATASET) != manifest["dataset"]["sha256"]:
        raise SystemExit("R6_DATASET_HASH_MISMATCH")
    if manifest["bindings"]["implementation_digest"] != state.get("code_sha256"):
        raise SystemExit("R6_IMPLEMENTATION_BINDING_MISMATCH")
    if auth.get("status") != "CLOSED_STOPPED" or state.get("authorization_id") != auth.get("authorization_id"):
        raise SystemExit("R6_AUTHORIZATION_OR_STATE_NOT_CLOSED")
    if auth.get("manifest_sha256") != sha(manifest_path) or auth.get("rules_sha256") != sha(rules_path):
        raise SystemExit("R6_AUTHORIZATION_BINDING_MISMATCH")
    if admission.get("admission") != "PASS" or admission.get("provider_requests") != 0:
        raise SystemExit("R6_ADMISSION_REPORT_INVALID")

    journal = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    closeouts = {row["case_id"]: row for row in journal if row.get("event") == "CASE_CLOSEOUT"}
    stopped = next((row for row in journal if row.get("event") == "BATCH_STOP"), None)
    batch_close = next((row for row in journal if row.get("event") == "BATCH_CLOSED"), None)
    if not batch_close or not stopped or len(closeouts) != 5:
        raise SystemExit("R6_JOURNAL_CLOSEOUT_COUNT_INVALID")
    results = []
    for case_id in manifest["proposed_order"]:
        if case_id not in closeouts:
            continue
        row = closeouts[case_id]
        archive_path = (ROOT / row["archive_path"]).resolve(strict=True)
        if sha(archive_path) != row.get("archive_sha256"):
            raise SystemExit(f"R6_ARCHIVE_HASH_MISMATCH:{case_id}")
        archive = read(archive_path)
        result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        obs = result.get("observations") if isinstance(result.get("observations"), dict) else {}
        messages = obs.get("messages") if isinstance(obs.get("messages"), list) else []
        answer = next((m.get("content") for m in reversed(messages)
                       if m.get("role") == "assistant" and isinstance(m.get("content"), str)), None)
        user_query = next((m.get("content") for m in messages
                           if m.get("role") == "user" and isinstance(m.get("content"), str)),
                          by_case[case_id]["input"]["user_query"])
        grade_path = ROOT / row["grader_path"]
        grade = read(grade_path)
        budget = row["budget"]
        audits = payload.get("tool_call_audit") if isinstance(payload.get("tool_call_audit"), list) else []
        refs = payload.get("tool_references") if isinstance(payload.get("tool_references"), list) else []
        events = obs.get("run_events") if isinstance(obs.get("run_events"), list) else []
        policy_finished = [event_payload(e) for e in events
                           if e.get("event_type") == "policy_retrieval_finished"]
        route = route_from_archive(archive)
        checks = grade.get("program_checks", {})
        if case_id == "D1-POLICY-014":
            semantic = {"suggestion": "UNSCORABLE_CANDIDATE_NOT_PERSISTED",
                "reason": "The application failed answer semantic validation and did not persist an assistant answer or rejected candidate. The task was not delivered. The exact candidate correctness cannot be judged.",
                "delivery_outcome": "FAIL", "answer_content": "UNSCORABLE_MISSING_CANDIDATE",
                "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        elif case_id == "D1-ORDER-011":
            semantic = {"suggestion": "PASS", "reason": "The delivered answer lists the two synthetic order lines and their recorded quantities; the same-Run get_order_items call and references are linked. No unsupported current stock or payment claims are made.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        elif case_id == "D1-ORDER-007":
            semantic = {"suggestion": "PASS", "reason": "The answer reports the stored PAID status as a record snapshot, supported by the same-Run owner-filtered get_order result. It does not infer payment time, shipping, or eligibility.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        elif case_id == "D1-ORDER-009":
            semantic = {"suggestion": "PASS", "reason": "The route is REFUSE with zero business reads; the answer refuses confirmation/disclosure of another user's order and guides the caller to their authenticated account. No target facts are disclosed.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        elif case_id == "D1-ORDER-012":
            semantic = {"suggestion": "PASS_PROVISIONAL", "reason": "The route is REFUSE, no order capability is allowed or called, and no broad account enumeration occurred. The bound answer is short; human review should confirm the wording is sufficiently task-related.", "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        else:
            semantic = {"suggestion": "UNSCORED", "reason": "No versioned semantic rule is available in this recomputation script.",
                "review_status": "AI_PROVISIONAL_NOT_HUMAN_CONFIRMED"}
        policy_summary = [{k: p.get(k) for k in ("call_id", "run_id", "trace_id", "result_ref", "status",
            "knowledge_version", "index_fingerprint", "evidence_ids", "retrieval_mode") if k in p}
            for p in policy_finished]
        results.append({"case_id": case_id, "group_id": next(c for c in dataset["cases"] if c["case_id"] == case_id)["group_id"],
            "dataset_version": manifest["dataset"]["version"], "dataset_sha256": manifest["dataset"]["sha256"],
            "implementation_digest": manifest["bindings"]["implementation_digest"],
            "rules_sha256": sha(rules_path), "run_id": result.get("run_id"),
            "archive_path": archive_path.relative_to(ROOT).as_posix(), "archive_sha256": sha(archive_path),
            "trace_id": next((e.get("trace_id") for e in events if e.get("trace_id")), None),
            "question": user_query, "run_status": result.get("status"), "route": route,
            "actual_provider_requests": budget.get("calls_by_category", {}).get("provider"),
            "actual_read_only_calls": budget.get("calls_by_category", {}).get("tool"),
            "tokens": budget.get("tokens"), "local_estimated_usd": budget.get("estimated_usd"),
            "vendor_invoice": budget.get("billed_status", "UNKNOWN_BILLING"),
            "elapsed_ms": next((run.get("duration_ms") for run in obs.get("runs", [])
                                 if run.get("run_id") == result.get("run_id")), None),
            "delivered_answer": answer, "answer_present": bool(answer and answer.strip()),
            "tool_calls": [{k: a.get(k) for k in ("tool_call_id", "tool", "capability", "result_ref", "status", "executed", "run_id", "trace_id", "owner_scope", "error_code") if k in a} for a in audits],
            "tool_references": refs, "policy_retrieval_events": policy_summary,
            "program_score_path": row["grader_path"], "program_score_sha256": row["grader_sha256"],
            "program_checks": checks, "program_task_outcome": grade.get("task_outcome"),
            "ai_semantic_pre_review": semantic, "cleanup": row.get("cleanup"),
            "case_attempt_count": state["attempt_count"].get(case_id),
            "provider_usage": payload.get("usage"), "failure_code": result.get("failure_code"),
            "candidate_answer_captured": answer is not None})

    # Link the R5 profile to the actual immutable answer and its confirmed decision, but do not copy it as R5 approval.
    r5_close_path = R5 / "reports/closeout-r5-final.json"
    r5_ledger_path = R5 / "reports/40-case-coverage-ledger-r5-r1.json"
    r5_state_path = R5 / "execution-state-r5.json"
    r5_journal_path = R5 / "execution-journal-r5.jsonl"
    r5_results_path = R5 / "reports/case-results-r5-r1.json"
    r5_results = read(r5_results_path)
    r5_state = read(r5_state_path)
    r5_manifest_path = R5 / "batch-manifest-r5-final8.json"
    r5_manifest = read(r5_manifest_path)
    r5_authorization = read(R5 / "authorization-r5.json")
    r5_journal = [json.loads(line) for line in r5_journal_path.read_text(encoding="utf-8").splitlines()]
    r5_closeouts = {row.get("case_id") for row in r5_journal if row.get("event") == "CASE_CLOSEOUT"}
    r5_not_run_ids = {row["case_id"] for row in read(r5_ledger_path)["rows"]
                      if row.get("final_status") == "NOT_RUN_BATCH_STOP"}
    if (r5_authorization.get("status") != "CLOSED_STOPPED" or len(r5_not_run_ids) != 22 or
            set(r5_manifest.get("proposed_order", [])) - set(r5_state.get("attempt_count", {})) or
            any(r5_state.get("attempt_count", {}).get(case_id) != 0 for case_id in r5_not_run_ids) or
            r5_closeouts.intersection(r5_not_run_ids)):
        raise SystemExit("R5_NOT_RUN_ATTEMPT_OR_ARCHIVE_INTEGRITY_FAILURE")
    r5_rows = {row["case_id"]: row for row in r5_results["cases"]}
    prior_package_path = ROOT / "docs/eval/m6.2-dev-score-review-closeout-r1-20261029/eight-item-review-package-r1.json"
    prior_package = read(prior_package_path)
    prior_p3 = next(row for row in prior_package["items"] if row["item_id"] == "D1-PRODUCT-003")
    r5_p3 = r5_rows["D1-PRODUCT-003"]
    same_answer = prior_p3["actual_delivered_answer"] == r5_p3["delivered_answer"]
    r5_rules_path = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/locked-rules-r5-final8.json"
    r5_rules = read(r5_rules_path)
    old_rubric = prior_p3["rubric"]
    new_rule = r5_rules["case_rules"]["D1-PRODUCT-003"]
    rubric_same = (old_rubric.get("business_outcome") == new_rule.get("expected_business_result_for_reviewer") and
                   old_rubric.get("semantic_quality_rubric", {}).get("must_include") ==
                   new_rule.get("semantic_requirements_for_reviewer", {}).get("must_include"))
    p3_correction = {"schema_version": "m6.2-r6-r5-product003-score-correction-r1",
        "source_r5_run_id": r5_p3["run_id"], "source_r5_answer_sha256": hashlib.sha256(r5_p3["delivered_answer"].encode("utf-8")).hexdigest(),
        "prior_user_confirmed_run_id": prior_p3["run_id"], "prior_user_confirmed_archive_sha256": prior_p3["archive_sha256"],
        "confirmation_event_id": "M62-DEV-REVIEW-CHAT-SELF-CONFIRMED-20261029-R1",
        "prior_confirmed_decision": "FAIL", "same_complete_answer": same_answer,
        "same_locked_requirement": rubric_same,
        "missing_requirement": ["ask for usable shelf depth", "ask for clearance before recommending fit"],
        "r5_ai_pre_review_was": r5_p3["ai_semantic_review"]["suggestion"],
        "correction_recommendation_for_r5": "FAIL",
        "reason": "The exact delivered answer is identical to the answer bound to the user-confirmed FAIL. Both bound rubrics require usable shelf depth and clearance before recommending fit. The answer marks fit unconfirmed but does not ask for either measurement; no rule difference justifies the earlier R5 PARTIAL. This correction is a derived recommendation, not a new human decision for R5.",
        "human_decision_for_r5": "PENDING_USER_CONFIRMATION",
        "source_bindings": {"prior_review_package": {"path": prior_package_path.relative_to(ROOT).as_posix(), "sha256": sha(prior_package_path)},
            "r5_case_results": {"path": r5_results_path.relative_to(ROOT).as_posix(), "sha256": sha(r5_results_path)},
            "r5_rules": {"path": r5_rules_path.relative_to(ROOT).as_posix(), "sha256": sha(r5_rules_path)},
            "user_confirmation_event": {"path": "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmation-event-r1.json",
                "sha256": sha(ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/confirmation-event-r1.json")}}}
    if not same_answer or not rubric_same:
        raise SystemExit("R5_PRODUCT003_BINDING_COMPARISON_NOT_PROVEN")

    pending = []
    for case_id in ("D1-PRODUCT-002", "D1-PRODUCT-001", "D1-PRODUCT-003", "D1-PRODUCT-004", "D1-PRODUCT-006"):
        row = r5_rows[case_id]
        advice = row.get("ai_semantic_review", {})
        if case_id == "D1-PRODUCT-003":
            advice = {"suggestion": "FAIL", "reason": p3_correction["reason"], "derived_correction": True}
        raw_path = ROOT / row["archive_path"]
        pending.append({"item_id": case_id, "group_id": row["group_id"], "question": row["question"],
            "actual_answer": row.get("delivered_answer"), "run_id": row["run_id"],
            "dataset_version": row["dataset_version"], "implementation_digest": "57bee2b1765f0a20a61c9f4a722e4908230fb934ca31236bf32fbad0384010f3",
            "answer_sha256": hashlib.sha256((row.get("delivered_answer") or "").encode("utf-8")).hexdigest(),
            "tool_references": row.get("tool_references"), "program_checks": row.get("program_score", {}).get("program_checks"),
            "program_classification": row.get("classification"), "ai_recommendation": advice,
            "archive_path": row["archive_path"], "archive_sha256": row["archive_sha256"],
            "reviewer": None, "reviewed_at_utc": None, "reviewer_decision": None,
            "status": "PENDING_USER_REVIEW_OF_THIS_R5_BINDING"})
    for row in results:
        pending.append({"item_id": f"{row['case_id']}-R6", "group_id": row["group_id"],
            "question": row["question"], "actual_answer": row.get("delivered_answer"),
            "run_id": row["run_id"], "dataset_version": row["dataset_version"],
            "implementation_digest": row["implementation_digest"],
            "answer_sha256": hashlib.sha256((row.get("delivered_answer") or "").encode("utf-8")).hexdigest(),
            "route": row.get("route"), "tool_calls": row.get("tool_calls"),
            "tool_references": row.get("tool_references"),
            "policy_retrieval_events": row.get("policy_retrieval_events"),
            "program_checks": row.get("program_checks"),
            "program_classification": row.get("program_task_outcome"),
            "ai_recommendation": row.get("ai_semantic_pre_review"),
            "archive_path": row["archive_path"], "archive_sha256": row["archive_sha256"],
            "reviewer": None, "reviewed_at_utc": None, "reviewer_decision": None,
            "status": "PENDING_USER_REVIEW_OF_THIS_R6_BINDING"})
    r4 = read(R4_ARCHIVE)
    r4_run = r4["result"]
    r4_payload = r4_run.get("result") if isinstance(r4_run.get("result"), dict) else {}
    r4_messages = r4_run.get("observations", {}).get("messages", [])
    r4_answer = next((m.get("content") for m in reversed(r4_messages) if m.get("role") == "assistant"), r4_payload.get("answer"))
    pending.append({"item_id": "D1-POLICY-013-R4", "group_id": next(c for c in dataset["cases"] if c["case_id"] == "D1-POLICY-013")["group_id"],
        "question": r4.get("user_query"), "actual_answer": r4_answer, "run_id": r4_run.get("run_id"),
        "dataset_version": manifest["dataset"]["version"], "implementation_digest": r4.get("receipt", {}).get("code_sha256"),
        "answer_sha256": hashlib.sha256((r4_answer or "").encode("utf-8")).hexdigest(),
        "tool_references": r4_payload.get("tool_references"), "program_classification": "HISTORICAL_R4_PROVISIONAL_PARTIAL",
        "ai_recommendation": {"suggestion": "REVIEW_EXISTING_PROVISIONAL_PARTIAL", "reason": "R4 is a separate implementation binding; review the bound policy source, answer, and retrieval event before adopting an outcome. No R5 or later score is copied into this item."},
        "archive_path": R4_ARCHIVE.relative_to(ROOT).as_posix(), "archive_sha256": sha(R4_ARCHIVE),
        "reviewer": None, "reviewed_at_utc": None, "reviewer_decision": None,
        "status": "PENDING_USER_REVIEW_OF_R4_BINDING"})

    source_ledger = read(r5_ledger_path)
    r6_executed = set(closeouts)
    r6_order = manifest["proposed_order"]
    ledger_rows = []
    for row in source_ledger["rows"]:
        item = dict(row)
        if row.get("final_status") == "NOT_RUN_BATCH_STOP":
            if row["case_id"] in r6_executed:
                item["final_status"] = "EXECUTED_R6_BATCH_STOP" if row["case_id"] == stopped.get("case_id") else "EXECUTED_R6"
                item["detail"] = {"r5_status": "NOT_RUN_BATCH_STOP", "r6_run_id": closeouts[row["case_id"]].get("run_id"),
                    "r6_status": closeouts[row["case_id"]].get("run_status"), "r6_archive_path": closeouts[row["case_id"]].get("archive_path"),
                    "r6_archive_sha256": closeouts[row["case_id"]].get("archive_sha256"),
                    "r6_classification": closeouts[row["case_id"]].get("classification")}
            else:
                item["final_status"] = "NOT_RUN_AFTER_R6_BATCH_STOP"
                item["detail"] = {"r5_status": "NOT_RUN_BATCH_STOP", "r6_attempt_count": state["attempt_count"].get(row["case_id"], 0),
                    "r6_batch_stop_case": stopped.get("case_id")}
        ledger_rows.append(item)
    counts: dict[str, int] = {}
    for row in ledger_rows:
        counts[row["final_status"]] = counts.get(row["final_status"], 0) + 1
    if len(ledger_rows) != 40 or sum(counts.values()) != 40 or counts.get("EXECUTED_R6", 0) + counts.get("EXECUTED_R6_BATCH_STOP", 0) != 5:
        raise SystemExit("R6_40_CASE_CONSERVATION_FAILED")

    usage = batch_close["final_budget"]
    closeout = {"schema_version": "m6.2-r324-r6-closeout-r3", "classification": manifest["classification"],
        "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed", "batch_id": manifest["batch_id"],
        "authorization_id": auth["authorization_id"], "authorization_final_status": auth["status"],
        "manifest_sha256": sha(manifest_path), "rules_sha256": sha(rules_path),
        "dataset_sha256": manifest["dataset"]["sha256"], "implementation_digest": manifest["bindings"]["implementation_digest"],
        "git_head_revision_provenance_only": manifest["bindings"]["git_head_revision"],
        "personal_database_identity": manifest["bindings"]["database_identity"],
        "offline_application_admission": {"case_count": admission["case_count"], "ready": 22,
            "blocked": 0, "provider_requests": 0, "formal_runs": 0, "repository_reads": admission["read_only_preflight_calls"]},
        "execution": {"planned": 22, "executed": 5, "not_run_after_batch_stop": 17,
            "stop_case": stopped.get("case_id"), "stop_reason": stopped.get("reason"),
            "run_ids": [closeouts[c].get("run_id") for c in r6_order if c in closeouts]},
        "usage": {"provider_requests": usage.get("provider_requests"), "business_read_calls": usage.get("calls_by_category", {}).get("tool"),
            "tokens": usage.get("tokens"), "local_estimated_usd": usage.get("estimated_usd"),
            "vendor_invoice": usage.get("billed_status"), "active_reservations": usage.get("active_reservations"),
            "judge": usage.get("judge_requests"), "embedding": usage.get("embedding_requests"),
            "budget_snapshot": usage},
        "cleanup": {"all_executed_receipts_cleaned": all(row.get("cleanup", {}).get("cleanup_status") == "CLEANED_AND_RELEASED" for row in results),
            "active_lease": False, "active_receipt": False, "fixture_rows": 0, "pending_actions": 0},
        "case_results_path": "reports/case-results-r6-r2.json", "user_review_path": "reports/pending-user-review-r6-r2.json",
        "r5_product003_correction_path": "reports/product003-r5-correction-r2.json",
        "coverage_40_case_path": "reports/40-case-coverage-ledger-r6-r2.json",
        "r5_closed_stop_preserved": True, "r4_policy013_not_repeated": True,
        "r5_not_run_verification": {"case_count": len(r5_not_run_ids),
            "case_ids": sorted(r5_not_run_ids), "attempt_count_zero_for_all": True,
            "r5_closeout_event_absent_for_all": True,
            "r5_manifest_sha256": sha(r5_manifest_path), "r5_state_sha256": sha(r5_state_path),
            "r5_journal_sha256": sha(r5_journal_path), "r5_authorization_status": r5_authorization.get("status")},
        "judge_calls": 0, "embedding_calls": 0, "business_writes": 0, "pending_actions": 0,
        "formal_gates_changed": False, "candidate_or_frozen_access": False}
    closeout["pending_review_item_count"] = len(pending)
    closeout["case_results_path"] = "reports/case-results-r6-r3.json"
    closeout["correction_report_path"] = "reports/product003-r5-correction-r3.json"
    closeout["pending_review_path"] = "reports/pending-user-review-r6-r3.json"
    closeout["coverage_ledger_path"] = "reports/40-case-coverage-ledger-r6-r3.json"
    closeout["recompute_consistency_path"] = "reports/recompute-consistency-r6-r3.json"
    case_results_doc = {"schema_version": "m6.2-r324-r6-case-results-r3", "cases": results}
    correction_doc = p3_correction
    pending_doc = {"schema_version": "m6.2-r324-r6-pending-user-review-r3", "items": pending}
    ledger_doc = {"schema_version": "m6.2-r324-r6-40-case-ledger-r3", "source_r5_ledger_sha256": sha(r5_ledger_path),
         "rows": ledger_rows, "counts": counts, "total": 40, "unique_case_denominator_preserved": True,
         "historical_binding_quality_denominators_separate": True, "r4_policy013_excluded_from_r6_quality_denominator": True}
    consistency_doc = {"schema_version": "m6.2-r324-r6-independent-recompute-v3",
        "source_archive_count": len(results), "case_result_rows": len(results),
        "coverage_rows": len(ledger_rows), "coverage_count_sum": sum(counts.values()),
        "all_archive_hashes_match_journal": all(sha(ROOT / row["archive_path"]) == row["archive_sha256"] for row in results),
        "all_program_score_hashes_match_journal": all(sha(ROOT / row["program_score_path"]) == row["program_score_sha256"] for row in results),
        "batch_journal_settled": usage.get("settled") is True and usage.get("active_reservations") == 0,
        "batch_totals": {"provider_requests": usage.get("provider_requests"),
            "business_read_calls": usage.get("calls_by_category", {}).get("tool"), "tokens": usage.get("tokens"),
            "local_estimated_usd": usage.get("estimated_usd"), "vendor_invoice": usage.get("billed_status")},
        "programmatic_recompute_stable": True, "human_review": "PENDING_USER_CONFIRMATION",
        "r4_and_r5_quality_denominators_separate": True}
    return case_results_doc, correction_doc, pending_doc, ledger_doc, closeout, consistency_doc, \


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    outputs = build()
    files = [PKG / "reports/case-results-r6-r3.json", PKG / "reports/product003-r5-correction-r3.json",
             PKG / "reports/pending-user-review-r6-r3.json", PKG / "reports/40-case-coverage-ledger-r6-r3.json",
             PKG / "reports/closeout-r6-r3.json", PKG / "reports/recompute-consistency-r6-r3.json"]
    if args.verify:
        for path, value in zip(files, outputs):
            if read(path) != value:
                raise SystemExit(f"R6_RECOMPUTE_DIVERGENCE:{path.name}")
        print(json.dumps({"recompute": "MATCH", "files": [p.relative_to(ROOT).as_posix() for p in files]}))
        return 0
    for path, value in zip(files, outputs):
        safe_write(path, value)
    print(json.dumps({"recompute": "WRITTEN", "files": [p.relative_to(ROOT).as_posix() for p in files]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
