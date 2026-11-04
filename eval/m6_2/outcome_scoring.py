"""M6.2 offline reference loader and retrospective Outcome projection.

This code never calls a model or Judge. Human-confirmed Dev references remain
separate from formal Gold and from the original pending CSV annotations.
"""
from __future__ import annotations

import hashlib
import json
import csv
import ast
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = Path("docs/eval/m6.1-human-review-preflight-60-r1-20261028")
SMOKE_REVIEW = Path("docs/eval/m6.1-smoke-human-review-user-confirmed-20261028-r1")
SCORING = Path("docs/eval/m6.1-smoke-scoring-closeout-r2-20261028")
DATASET = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON_OBJECT_REQUIRED:{path}")
    return value


def _bound_file(root: Path, relative: str, expected: str) -> dict[str, str]:
    path = root / relative
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"SOURCE_HASH_MISMATCH:{relative}")
    return {"path": relative.replace("\\", "/"), "sha256": actual}


def _validate_confirmed_review_rows(*, proposal: dict[str, Any], decisions: dict[str, Any],
        event: dict[str, Any], answer_source: dict[str, dict[str, str]],
        route_source: dict[str, dict[str, str]]) -> None:
    """Compare every confirmed decision to its pre-review proposal, event and source row.

    File hashes bind the artifacts, but do not establish that a derived row is the
    decision the user confirmed. This semantic check is intentionally per stable ID.
    """
    answer_proposal = {r.get("item_id"): r for r in proposal.get("answer_pre_reviews", [])}
    answer_decisions = {r.get("item_id"): r for r in decisions.get("answer_pre_reviews", [])}
    answer_event = {r.get("item_id"): r for r in event.get("accepted_answer_decisions", [])}
    if (None in answer_proposal or None in answer_decisions or None in answer_event or
            set(answer_proposal) != set(answer_decisions) or set(answer_proposal) != set(answer_event)):
        raise ValueError("ANSWER_REFERENCE_ID_SET_MISMATCH")
    for item_id, proposed in answer_proposal.items():
        confirmed = answer_decisions[item_id]
        accepted = answer_event[item_id]
        source = answer_source.get(item_id)
        if source is None:
            raise ValueError(f"ANSWER_SOURCE_ROW_MISSING:{item_id}")
        for field in ("case_id", "dataset_split", "question", "disputed_claim"):
            if proposed.get(field) != confirmed.get(field) or confirmed.get(field) != source.get(field):
                raise ValueError(f"ANSWER_REFERENCE_SOURCE_DRIFT:{item_id}:{field}")
        if (proposed.get("programmatic_original") != source.get("programmatic_conclusion") or
                proposed.get("judge_original") != source.get("judge_conclusion") or
                proposed.get("ai_recommendation_original") != source.get("ai_recommendation") or
                proposed.get("source_excerpt_in_review_record") not in source.get("policy_or_contract_source", "") or
                any(version not in source.get("policy_or_contract_source", "")
                    for version in proposed.get("source_versions", []))):
            raise ValueError(f"ANSWER_REFERENCE_SOURCE_EVIDENCE_DRIFT:{item_id}")
        for field in ("source_versions", "source_excerpt_in_review_record", "answer_evidence_scope", "pre_review"):
            if proposed.get(field) != confirmed.get(field):
                raise ValueError(f"ANSWER_REFERENCE_PROPOSAL_DRIFT:{item_id}:{field}")
        decision = proposed.get("pre_review", {}).get("recommendation")
        if (confirmed.get("user_confirmed_decision") != decision or
                accepted.get("decision") != decision or
                accepted.get("case_id") != proposed.get("case_id") or
                accepted.get("full_answer_scope") != proposed.get("answer_evidence_scope")):
            raise ValueError(f"ANSWER_CONFIRMED_DECISION_DRIFT:{item_id}")
        hf = confirmed.get("human_fields", {})
        if (confirmed.get("derived_review_status") != "USER_CONFIRMED" or
                hf.get("reviewer_decision") != decision or
                hf.get("reviewer") != "CHAT_USER_SELF_CONFIRMED" or
                hf.get("reviewed_at_utc") != event.get("confirmed_at_utc") or
                hf.get("status") != "USER_CONFIRMED_DERIVED_DECISION"):
            raise ValueError(f"ANSWER_CONFIRMATION_FIELDS_MISMATCH:{item_id}")

    route_proposal = {r.get("case_id"): r for r in proposal.get("route_pre_reviews", [])}
    route_decisions = {r.get("case_id"): r for r in decisions.get("route_pre_reviews", [])}
    route_event = {r.get("case_id"): r for r in event.get("accepted_route_decisions", [])}
    if (None in route_proposal or None in route_decisions or None in route_event or
            set(route_proposal) != set(route_decisions) or set(route_proposal) != set(route_event)):
        raise ValueError("ROUTE_REFERENCE_ID_SET_MISMATCH")
    route_label_fields = ("strategy", "sub_strategy", "intent", "safety_decision",
        "reason_code", "needs_clarification", "required_capabilities")
    for case_id, proposed in route_proposal.items():
        confirmed = route_decisions[case_id]
        accepted = route_event[case_id]
        source = route_source.get(case_id)
        if source is None:
            raise ValueError(f"ROUTE_SOURCE_ROW_MISSING:{case_id}")
        for field in ("group_id", "dataset_split", "source_case_id", "user_query"):
            if proposed.get(field) != confirmed.get(field) or confirmed.get(field) != source.get(field):
                raise ValueError(f"ROUTE_REFERENCE_SOURCE_DRIFT:{case_id}:{field}")
        for field in ("original_label", "pre_review_recommendation", "suggested_label", "changed_fields"):
            if proposed.get(field) != confirmed.get(field):
                raise ValueError(f"ROUTE_REFERENCE_PROPOSAL_DRIFT:{case_id}:{field}")
        original = proposed.get("original_label", {})
        source_original = {
            "strategy": source.get("expected_strategy"),
            "sub_strategy": source.get("expected_sub_strategy"),
            "intent": source.get("expected_intent"),
            "safety_decision": source.get("expected_safety_decision"),
            "reason_code": source.get("expected_reason_code"),
            "needs_clarification": str(source.get("expected_needs_clarification", "")).lower() == "true",
            "required_capabilities": ast.literal_eval(source.get("required_capabilities", "[]")),
        }
        if {k: original.get(k) for k in route_label_fields} != source_original:
            raise ValueError(f"ROUTE_ORIGINAL_LABEL_SOURCE_MISMATCH:{case_id}")
        decision = proposed.get("pre_review_recommendation")
        confirmed_label = proposed.get("suggested_label")
        if (confirmed.get("user_confirmed_decision") != decision or
                confirmed.get("user_confirmed_label") != confirmed_label or
                accepted.get("decision") != decision or
                accepted.get("confirmed_label") != confirmed_label or
                accepted.get("changed_fields") != proposed.get("changed_fields")):
            raise ValueError(f"ROUTE_CONFIRMED_DECISION_DRIFT:{case_id}")
        hf = confirmed.get("human_fields", {})
        if (confirmed.get("derived_review_status") != "USER_CONFIRMED" or
                hf.get("reviewer_decision") != decision or
                hf.get("reviewer") != "CHAT_USER_SELF_CONFIRMED" or
                hf.get("reviewed_at_utc") != event.get("confirmed_at_utc") or
                hf.get("status") != "USER_CONFIRMED_DERIVED_DECISION"):
            raise ValueError(f"ROUTE_CONFIRMATION_FIELDS_MISMATCH:{case_id}")


def build_confirmed_references(root: Path = ROOT) -> dict[str, Any]:
    pre = PREFLIGHT.as_posix()
    decisions_rel = f"{pre}/confirmed-review-decisions-r1.json"
    event_rel = f"{pre}/confirmation-event-r1.json"
    proposal_rel = f"{pre}/proposed-review-decisions-r1.json"
    event = load_json(root / event_rel)
    proposal = load_json(root / proposal_rel)
    decisions = load_json(root / decisions_rel)
    if event.get("status") != "USER_CONFIRMATION_RECORDED" or event.get("actor", {}).get("type") != "CHAT_USER_SELF_CONFIRMED":
        raise ValueError("USER_CONFIRMATION_EVENT_INVALID")
    if event.get("event_id") != decisions.get("review_event", {}).get("event_id"):
        raise ValueError("CONFIRMATION_EVENT_ID_MISMATCH")
    if sha256(root / proposal_rel) != event.get("proposal_binding", {}).get("pre_review_sha256"):
        raise ValueError("PRE_REVIEW_BINDING_MISMATCH")
    if decisions.get("review_event", {}).get("event_sha256") != sha256(root / event_rel):
        raise ValueError("CONFIRMATION_EVENT_HASH_MISMATCH")
    if (len(decisions.get("answer_pre_reviews", [])) != 8 or
            len(decisions.get("route_pre_reviews", [])) != 52):
        raise ValueError("CONFIRMED_REFERENCE_COUNTS_MISMATCH")
    answer_counts: dict[str, int] = {}
    for row in decisions["answer_pre_reviews"]:
        decision = row.get("user_confirmed_decision")
        answer_counts[decision] = answer_counts.get(decision, 0) + 1
    route_counts: dict[str, int] = {}
    route_splits: dict[str, int] = {}
    for row in decisions["route_pre_reviews"]:
        decision = row.get("user_confirmed_decision")
        route_counts[decision] = route_counts.get(decision, 0) + 1
        split = row.get("dataset_split")
        route_splits[split] = route_splits.get(split, 0) + 1
    if answer_counts != {"PASS": 4, "PARTIAL": 3, "INSUFFICIENT_EVIDENCE": 1}:
        raise ValueError("ANSWER_DECISION_COUNTS_MISMATCH")
    if route_counts != {"RETAIN": 49, "MODIFY": 3}:
        raise ValueError("ROUTE_DECISION_COUNTS_MISMATCH")
    if route_splits != {"dev": 28, "validation": 24}:
        raise ValueError("ROUTE_SPLIT_COUNTS_MISMATCH")
    # Source CSV hashes are bound by the confirmation event; do not mutate them.
    source_csvs = event.get("proposal_binding", {}).get("source_csv_sha256", {})
    source_files = {
        "answers": "eval/fidelity/m2.4-q2-human-review-r2.csv",
        "routes": "eval/review/route-m2.1-dev-validation-review-r1.csv",
    }
    source_hashes = {}
    for kind, rel in source_files.items():
        expected = source_csvs.get(f"{kind}_sha256")
        source_hashes[kind] = _bound_file(root, rel, expected)
    def read_csv_rows(relative: str) -> list[dict[str, str]]:
        with (root / relative).open("r", encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    answer_source = {row.get("item_id"): row for row in read_csv_rows(source_files["answers"])}
    route_source = {row.get("case_id"): row for row in read_csv_rows(source_files["routes"])}
    if len(answer_source) != 8 or len(route_source) != 52:
        raise ValueError("SOURCE_CSV_ROW_COUNT_MISMATCH")
    _validate_confirmed_review_rows(proposal=proposal, decisions=decisions, event=event,
        answer_source=answer_source, route_source=route_source)

    route_refs = []
    for row in decisions["route_pre_reviews"]:
        source_row = route_source.get(row.get("case_id"))
        if not source_row or source_row.get("group_id") != row.get("group_id") or source_row.get("dataset_split") != row.get("dataset_split"):
            raise ValueError(f"ROUTE_SOURCE_ROW_MISMATCH:{row.get('case_id')}")
        label = row["user_confirmed_label"]
        route_refs.append({
            "case_id": row["case_id"], "source_case_id": row["source_case_id"],
            "group_id": row["group_id"], "split": row["dataset_split"],
            "reference_kind": "USER_CONFIRMED_ROUTE_REFERENCE",
            "decision": row["user_confirmed_decision"], "label": label,
            "scope": "route-label fields only; historical correctness is not retroactively rewritten",
            "source_review_status": row.get("source_review_status"),
            "original_source_row": {key: source_row.get(key) for key in (
                "source_case_id", "user_query", "expected_strategy", "expected_sub_strategy",
                "expected_intent", "expected_safety_decision", "expected_reason_code",
                "expected_needs_clarification", "required_capabilities", "review_status")},
            "route_contract": row.get("label_version_basis"),
            "rationale": row.get("basis"),
            "confirmation_event_id": event["event_id"],
        })
    answer_refs = []
    for row in decisions["answer_pre_reviews"]:
        source_row = answer_source.get(row.get("item_id"))
        if not source_row or source_row.get("case_id") != row.get("case_id") or source_row.get("dataset_split") != row.get("dataset_split"):
            raise ValueError(f"ANSWER_SOURCE_ROW_MISMATCH:{row.get('item_id')}")
        answer_refs.append({
            "item_id": row["item_id"], "case_id": row["case_id"], "split": row["dataset_split"],
            "reference_kind": "USER_CONFIRMED_ANSWER_DISPUTE_REFERENCE",
            "decision": row["user_confirmed_decision"], "claim": row.get("disputed_claim"),
            "score_scope": row.get("answer_evidence_scope"),
            "original_source_row": {key: source_row.get(key) for key in (
                "question", "disputed_claim", "policy_or_contract_source", "review_status")},
            "claim_only": row.get("answer_evidence_scope") != "FULL_ANSWER_AS_RECORDED_IN_REVIEW_ARTIFACT",
            "source_versions": row.get("source_versions", []),
            "source_excerpt": row.get("source_excerpt_in_review_record"),
            "rationale": row.get("pre_review", {}).get("rationale"),
            "insufficient_evidence_is_binary_label": False,
            "confirmation_event_id": event["event_id"],
        })

    smoke_event_rel = f"{SMOKE_REVIEW.as_posix()}/user-human-review-confirmation-r1.json"
    smoke_event = load_json(root / smoke_event_rel)
    if smoke_event.get("status") != "HUMAN_REVIEW_CONFIRMED_WITH_CORRECTIONS":
        raise ValueError("SMOKE_REVIEW_EVENT_INVALID")
    score_path = root / f"{SCORING.as_posix()}/scores-r2.json"
    score = load_json(score_path)
    attempts: dict[str, list[dict[str, Any]]] = {}
    for group in (score.get("primary_cases", []), score.get("supplemental_attempts", [])):
        for attempt in group:
            attempts.setdefault(attempt.get("case_id"), []).append({
                "attempt_id": attempt.get("attempt_id"), "run_id": attempt.get("run_id"),
                "role": attempt.get("role"), "execution": attempt.get("execution"),
                "binding": attempt.get("binding"),
                "archive_paths": [e.get("path") for e in attempt.get("evidence", [])
                                  if isinstance(e, dict) and "/personal-dev-runs/" in e.get("path", "")],
            })
    smoke_refs = []
    for row in smoke_event.get("primary_decisions", []):
        smoke_refs.append({
            "case_id": row["case_id"], "run_id": row["run_id"],
            "human_confirmed_outcome": row["human_review_decision"],
            "reference_kind": "USER_CONFIRMED_RETROSPECTIVE_DEV_OUTCOME",
            "attempts": attempts.get(row["case_id"], []),
            "scope": "personal Dev Smoke review under per-attempt historical bindings; not formal score",
            "review_event_id": smoke_event["event_id"],
        })
    if len(smoke_refs) != 8:
        raise ValueError("SMOKE_OUTCOME_REFERENCE_COUNT_MISMATCH")
    source_bindings = {
        "confirmation_event": _bound_file(root, event_rel, sha256(root / event_rel)),
        "confirmed_decisions": _bound_file(root, decisions_rel, sha256(root / decisions_rel)),
        "proposal": _bound_file(root, proposal_rel, event["proposal_binding"]["pre_review_sha256"]),
        "answer_csv": source_hashes["answers"], "route_csv": source_hashes["routes"],
        "smoke_review_event": _bound_file(root, smoke_event_rel, sha256(root / smoke_event_rel)),
        "smoke_scores_r2": _bound_file(root, f"{SCORING.as_posix()}/scores-r2.json", sha256(score_path)),
        "dataset": _bound_file(root, DATASET.as_posix(), "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"),
    }
    return {
        "schema_version": "m6.2-user-confirmed-dev-reference-bundle-v1",
        "classification": "USER_CONFIRMED_DEV_REFERENCE_NOT_GOLD_NOT_INDEPENDENT_NOT_BLIND",
        "effective_confirmation_state": {
            "status": "USER_CONFIRMED",
            "user_confirmation_required": False,
            "actor_type": event["actor"]["type"],
            "confirmed_at_utc": event["confirmed_at_utc"],
            "event_id": event["event_id"],
            "method": event["method"],
            "source_ai_pre_review_is_human_decision": False,
            "legacy_derived_file_flags_preserved_as_history": {
                "not_a_human_decision": decisions.get("not_a_human_decision"),
                "user_confirmation_required": decisions.get("user_confirmation_required"),
            },
            "original_csv_review_fields": "PENDING_HUMAN_REVIEW; unchanged; not consumed as final reference",
            "independent_review": False, "blind_review": False, "gold_status": "NOT_APPROVED",
        },
        "references": {"routes": route_refs, "answer_disputes": answer_refs, "smoke_outcomes": smoke_refs},
        "counts": {"routes": {"total": len(decisions["route_pre_reviews"]),
                    "dev": route_splits["dev"], "validation": route_splits["validation"],
                    "retain": 49, "modify": 3},
                   "answer_disputes": answer_counts,
                   "smoke_outcomes": 8},
        "scoreability": {
            "routes": "52 user-confirmed route labels; candidate reference only, split preserved",
            "answer_disputes": "claim-only rows assess only stated claim; INSUFFICIENT_EVIDENCE is excluded from binary positive/negative denominators",
            "smoke_outcomes": "8 user-confirmed retrospective Dev dispositions with attempt bindings; do not pool across code/rules bindings",
        },
        "scoring_denominators": {
            "routes": {"reference_rows": 52, "dev": 28, "validation": 24},
            "answer_disputes": {"items": 8, "binary_disposition_eligible": 7,
                "insufficient_evidence_excluded_from_binary_denominator": 1,
                "claim_only_items_assess_only_claim": sum(bool(r.get("answer_evidence_scope") != "FULL_ANSWER_AS_RECORDED_IN_REVIEW_ARTIFACT") for r in decisions["answer_pre_reviews"])},
            "smoke_outcomes": {"reviewed_cases": 8, "pooled_success_rate": None,
                "reason": "different code/rules bindings; report by binding and case"},
        },
        "exclusion_reasons": [
            {"scope": "insufficient-evidence answer claim", "reason": "not a determinate positive or negative reference"},
            {"scope": "claim-only answer records", "reason": "not a full-answer gold/reference"},
            {"scope": "Candidate/Frozen and Gold", "reason": "out of confirmation scope and not accessed"},
            {"scope": "original CSV pending annotations", "reason": "historical source fields remain unchanged; confirmed derived layer is authoritative for this Dev reference loader"},
        ],
        "excluded": ["Candidate", "Frozen", "Gold adjudication", "unlisted CSV rows", "cross-binding pooled success rate"],
        "source_bindings": source_bindings,
    }


def _archive_code_checks(archive: dict[str, Any], run_id: str, report: dict[str, Any]) -> dict[str, Any]:
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    app = result.get("result") if isinstance(result.get("result"), dict) else {}
    run_status = result.get("status")
    run_id_match = result.get("run_id") == run_id
    terminal = run_status in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"}
    read_calls = result.get("read_calls")
    receipt = archive.get("receipt") if isinstance(archive.get("receipt"), dict) else {}
    answer = app.get("answer")
    trace_id = app.get("trace_id")
    route = app.get("route")
    route_recorded = isinstance(route, (dict, str)) and bool(route)
    receipt_run_match = receipt.get("run_id") == run_id
    report_budget = report.get("budget_snapshot") if isinstance(report.get("budget_snapshot"), dict) else None
    budget = "UNKNOWN"
    if report_budget:
        calls = report_budget.get("calls_by_category")
        reserved = report_budget.get("reserved_calls_by_category")
        keys = ("provider", "tool", "embedding", "judge")
        valid = (report_budget.get("complete") is True and report_budget.get("settled") is True and
            report_budget.get("active_reservations") == 0 and report_budget.get("integrity_errors") == [] and
            isinstance(calls, dict) and isinstance(reserved, dict) and
            all(type(calls.get(k)) is int and calls[k] >= 0 and type(reserved.get(k)) is int and reserved[k] == 0 for k in keys))
        if valid and type(read_calls) is int:
            budget = "PASS" if read_calls == calls["tool"] else "FAIL"
    cleanup = report.get("cleanup", {}) if isinstance(report.get("cleanup"), dict) else {}
    cleanup_status = cleanup.get("cleanup_status")
    cleanup_check = "PASS" if cleanup_status == "CLEANED_AND_RELEASED" and cleanup.get("lease_released") is True and cleanup.get("residual_case_rows") == 0 else "UNKNOWN"
    citations = app.get("citations")
    evidence = app.get("evidence")
    receipt_facts = receipt.get("fact_receipts")
    route_strategy = route.get("strategy") if isinstance(route, dict) else None
    route_caps = route.get("required_capabilities") if isinstance(route, dict) else None
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    tool_events = observations.get("tool_events")
    if isinstance(tool_events, list) and isinstance(read_calls, int):
        tool_event_link = "PASS" if len(tool_events) == read_calls else "FAIL"
    else:
        tool_event_link = "UNKNOWN"
    def field_state(value: Any) -> str:
        if value is None:
            return "MISSING"
        if isinstance(value, (list, dict)):
            return "STRUCTURED_PRESENT" if value else "STRUCTURED_EMPTY"
        if isinstance(value, str):
            return "LEGACY_SERIALIZED_NONEMPTY" if value.strip() else "LEGACY_SERIALIZED_EMPTY"
        return "UNSUPPORTED_TYPE"
    return {
        "run_terminal": "PASS" if terminal and run_id_match else "FAIL" if not run_id_match else "UNKNOWN",
        "route_recorded": "PASS" if route_recorded else "UNKNOWN",
        "route_strategy": route_strategy or "UNKNOWN",
        "route_capabilities": route_caps if isinstance(route_caps, list) else "UNKNOWN",
        "tool_event_linkage": tool_event_link,
        "tool_events_state": field_state(tool_events),
        "business_results_state": field_state(app.get("business_results")),
        "source_fact_receipts_state": field_state(receipt_facts),
        "receipt_fact_ids_available": "PASS" if isinstance(receipt_facts, list) else "UNKNOWN",
        "trace_recorded": "PASS" if isinstance(trace_id, str) and trace_id else "UNKNOWN",
        "receipt_run_link": "PASS" if receipt_run_match else "FAIL" if receipt.get("run_id") else "UNKNOWN",
        "read_call_count": read_calls if type(read_calls) is int else None,
        "delivered_answer_present": isinstance(answer, str) and bool(answer.strip()),
        "citation_field_state": field_state(citations),
        "evidence_field_state": field_state(evidence),
        "policy_retrieval_state": field_state(app.get("policy_retrieval")),
        "budget_recomputed_from_settled_report_and_archive": budget,
        "cleanup_from_report_and_receipt": cleanup_check,
        "run_status": run_status,
        "grading_status": archive.get("grading_status", "UNKNOWN"),
    }


def score_retrospective_smoke(root: Path = ROOT) -> dict[str, Any]:
    refs = build_confirmed_references(root)
    score = load_json(root / f"{SCORING.as_posix()}/scores-r2.json")
    source_index = {}
    for group in (score.get("primary_cases", []), score.get("supplemental_attempts", [])):
        for row in group:
            source_index[row.get("run_id")] = row
    rows = []
    for ref in refs["references"]["smoke_outcomes"]:
        record = source_index.get(ref["run_id"])
        if not record:
            raise ValueError(f"CONFIRMED_RUN_NOT_IN_SCORE_INDEX:{ref['run_id']}")
        archive_evidence = next((e for e in record.get("evidence", []) if isinstance(e, dict) and "/personal-dev-runs/" in e.get("path", "")), None)
        report_evidence = next((e for e in record.get("evidence", []) if isinstance(e, dict) and "/reports/" in e.get("path", "")), None)
        if not archive_evidence or not report_evidence:
            raise ValueError(f"ARCHIVE_OR_REPORT_MISSING:{ref['run_id']}")
        archive_path, report_path = root / archive_evidence["path"], root / report_evidence["path"]
        if sha256(archive_path) != archive_evidence["sha256"] or sha256(report_path) != report_evidence["sha256"]:
            raise ValueError(f"RUN_EVIDENCE_HASH_MISMATCH:{ref['run_id']}")
        archive, report = load_json(archive_path), load_json(report_path)
        if archive.get("result", {}).get("run_id") != ref["run_id"] or archive.get("case_id") != ref["case_id"]:
            raise ValueError(f"ARCHIVE_IDENTITY_MISMATCH:{ref['run_id']}")
        binding = record.get("binding", {})
        code = _archive_code_checks(archive, ref["run_id"], report)
        decision = ref["human_confirmed_outcome"]
        # Candidate answer and delivered output are deliberately separate dimensions.
        semantic = record.get("semantic", {})
        rows.append({
            "case_id": ref["case_id"], "run_id": ref["run_id"],
            "attempt_role": record.get("role"), "execution": record.get("execution"),
            "binding": binding, "raw_evidence": {"archive": archive_evidence, "report": report_evidence},
            "code_grader": code,
            "confirmed_dev_outcome": decision,
            "candidate_answer_status": semantic.get("candidate_answer_status", "UNKNOWN"),
            "delivered_output_status": semantic.get("delivered_output_status", "UNKNOWN"),
            "task_success_status": semantic.get("task_success", "UNKNOWN"),
            "failure_class": record.get("failure_class", "UNKNOWN"),
            "semantic_review": "USER_CONFIRMED_RETROSPECTIVE_DEV_DISPOSITION; not independent, not Gold",
        })
    by_binding: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["binding"].get("batch", "UNKNOWN_BINDING")
        tally = by_binding.setdefault(key, {"executed": 0, "task_success": 0,
            "task_failure": 0, "delivered_output_pass": 0, "delivered_output_partial": 0,
            "delivered_output_fail": 0, "candidate_unscorable": 0,
            "evaluator_failure": 0, "semantic_unscorable": 0,
            "case_ids": []})
        tally["executed"] += 1
        tally["case_ids"].append(row["case_id"])
        status = row["task_success_status"]
        if row["failure_class"] == "EVALUATOR_FAILURE":
            tally["evaluator_failure"] += 1
        elif status == "PASS": tally["task_success"] += 1
        elif status == "FAIL": tally["task_failure"] += 1
        else: tally["semantic_unscorable"] += 1
        delivered = row["delivered_output_status"]
        if delivered == "PASS": tally["delivered_output_pass"] += 1
        elif delivered == "PARTIAL": tally["delivered_output_partial"] += 1
        elif delivered == "FAIL": tally["delivered_output_fail"] += 1
        else: tally["semantic_unscorable"] += 1
        if row["candidate_answer_status"] in {"UNSCORABLE_NO_VERIFIABLE_CANDIDATE", "UNKNOWN"}:
            tally["candidate_unscorable"] += 1
    return {
        "schema_version": "m6.2-retrospective-outcome-score-v1",
        "classification": "RETROSPECTIVE_PERSONAL_DEV_REASSESSMENT_NOT_NEW_TRIAL_NOT_FORMAL_SCORE",
        "reference_bundle_schema": refs["schema_version"],
        "grader": "offline_code_projection_from_raw_archives_plus_confirmed_user_dev_review",
        "external_judge_called": False,
        "rows": rows,
        "summary_by_binding": by_binding,
        "dimensions": ["route_and_capabilities", "tool_permission_and_business_facts",
            "run_trace_evidence_linkage", "delivered_output", "task_outcome_and_refusal_clarification",
            "application_infrastructure_evaluator_faults", "usage_budget_terminal_cleanup"],
        "limitations": ["No cross-binding pooled success rate.",
            "Historical raw archives may not contain a structured event field needed by newer graders; such checks remain UNKNOWN.",
            "Semantic dispositions are the user's scoped, retrospective Dev review, not independent/blind or Gold adjudication."]
    }
