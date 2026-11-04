"""Offline, deterministic scoring projection for archived personal Dev Smoke runs.

This module does not call a model or infer missing evidence. Semantic decisions
are supplied in a versioned offline adjudication file and remain provisional.
"""
from __future__ import annotations

import hashlib
import json
import math
import copy
from pathlib import Path
from typing import Any

from eval.m6_1.contracts import sha256_json


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON_OBJECT_REQUIRED:{path.as_posix()}")
    return value


def _source_path(root: Path, inputs_path: Path, relative: str) -> Path:
    candidate = root / relative
    if candidate.is_file():
        return candidate
    candidate = inputs_path.parent / relative
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(relative)


def _status(checks: dict[str, Any], name: str) -> str:
    row = checks.get(name)
    return row.get("status", "UNKNOWN") if isinstance(row, dict) else "UNKNOWN"


def _policy_retrieval_projection(archive: dict[str, Any], case_id: str, run_id: str) -> dict[str, Any]:
    """Check persisted retrieval provenance separately from delivered-answer citations."""
    observations = archive.get("result", {}).get("observations", {})
    events = observations.get("run_events", []) if isinstance(observations, dict) else []
    started: dict[str, dict[str, Any]] = {}
    finished: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("event_type") not in {
                "policy_retrieval_started", "policy_retrieval_finished"}:
            continue
        try:
            payload = json.loads(event.get("payload_json", "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if event.get("event_type") == "policy_retrieval_started" and isinstance(call_id, str):
            started[call_id] = {"envelope": event, "payload": payload}
        elif event.get("event_type") == "policy_retrieval_finished":
            finished.append({"envelope": event, "payload": payload})
    policy_facts = [row for row in archive.get("receipt", {}).get("fact_receipts", [])
        if isinstance(row, dict) and row.get("kind") == "policies" and isinstance(row.get("source_id"), str)]
    expected_ids = {row["source_id"] for row in policy_facts}
    valid = []
    for item in finished:
        payload, envelope = item["payload"], item["envelope"]
        call_id = payload.get("call_id")
        start = started.get(call_id)
        if (payload.get("status") == "SUCCEEDED" and payload.get("executed") is True and
                payload.get("source_type") == "POLICY_RETRIEVAL" and payload.get("run_id") == run_id and
                envelope.get("run_id") == run_id and isinstance(envelope.get("trace_id"), str) and
                payload.get("trace_id") == envelope.get("trace_id") and isinstance(call_id, str) and
                payload.get("result_ref") == f"policy-retrieval:{call_id}" and
                isinstance(payload.get("evidence_ids"), list) and
                (not expected_ids or expected_ids.issubset(set(payload["evidence_ids"]))) and
                start is not None and start["payload"].get("run_id") == run_id and
                start["payload"].get("trace_id") == envelope.get("trace_id") and
                start["payload"].get("result_ref") == payload.get("result_ref")):
            valid.append(payload)
    if len(valid) == 1:
        payload = valid[0]
        return {"status": "PASS", "basis": "persisted successful policy retrieval is linked by call_id, Run, Trace and result_ref; source IDs agree with receipt",
            "call_id": payload.get("call_id"), "result_ref": payload.get("result_ref"),
            "source_ids": payload.get("evidence_ids"), "document_versions": payload.get("document_versions"),
            "knowledge_version": payload.get("knowledge_version"),
            "index_fingerprint": payload.get("index_fingerprint"),
            "evaluation_at": payload.get("evaluation_at")}
    if not events and not expected_ids:
        return {"status": "NOT_APPLICABLE", "basis": "archive contains no policy receipt or retrieval event"}
    return {"status": "UNKNOWN", "basis": "policy retrieval event absent, ambiguous or not fully linked to receipt/Run/Trace/result_ref",
        "valid_linked_event_count": len(valid), "expected_receipt_source_ids": sorted(expected_ids)}


def _recompute_budget_status(report: dict[str, Any], archive: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    snapshot = report.get("budget_snapshot")
    if not isinstance(snapshot, dict):
        return "UNKNOWN", {"basis": "final budget snapshot missing"}
    required_categories = ("provider", "tool", "embedding", "judge")
    calls = snapshot.get("calls_by_category")
    reserved = snapshot.get("reserved_calls_by_category")
    counts_valid = (isinstance(calls, dict) and isinstance(reserved, dict) and
        all(type(calls.get(key)) is int and calls[key] >= 0 for key in required_categories) and
        all(type(reserved.get(key)) is int and reserved[key] >= 0 for key in required_categories))
    numeric_valid = (type(snapshot.get("tokens")) is int and snapshot["tokens"] >= 0 and
        isinstance(snapshot.get("estimated_usd"), (int, float)) and snapshot["estimated_usd"] >= 0)
    if (not counts_valid or not numeric_valid or snapshot.get("integrity_errors") != [] or
            snapshot.get("active_reservations") != 0 or snapshot.get("complete") is not True or
            snapshot.get("settled") is not True or snapshot.get("snapshot_kind") != "FINAL" or
            any(reserved.get(key) != 0 for key in required_categories)):
        return "UNKNOWN", {"basis": "snapshot is incomplete, unsettled, reserved, malformed or has integrity errors"}
    raw_result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    read_calls = raw_result.get("read_calls")
    if type(read_calls) is not int or read_calls != calls["tool"]:
        return "FAIL", {"basis": "archive read-call count conflicts with settled ledger",
            "archive_read_calls": read_calls, "ledger_tool_calls": calls["tool"]}
    return "PASS", {"basis": "complete settled FINAL snapshot; all categories explicit nonnegative integers; no reservations; archive read-call count agrees",
        "provider_calls": calls["provider"], "tool_calls": calls["tool"],
        "tokens": snapshot["tokens"], "estimated_usd": snapshot["estimated_usd"],
        "legacy_check_status": _status(report.get("checks", {}).get("checks", {}), "budget_checks")}


def _wilson(successes: int, n: int, z: float = 1.959963984540054) -> list[float] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    radius = z * math.sqrt((p*(1-p) + z*z/(4*n))/n) / denom
    return [round(max(0.0, center-radius), 6), round(min(1.0, center+radius), 6)]


def _validate_manifest_binding(root: Path, inputs: dict[str, Any], item: dict[str, Any],
        report: dict[str, Any], archive: dict[str, Any], case_id: str, attempt_id: str) -> tuple[dict, list]:
    """Resolve historical bindings from the original manifest/rules/authorization."""
    manifest_path = _source_path(root, root / "scoring-inputs-r2.json", item["manifest_path"])
    rules_path = _source_path(root, root / "scoring-inputs-r2.json", item["rules_path"])
    manifest = _load(manifest_path)
    rules = _load(rules_path)
    authorization_path = manifest_path.parent / "authorization.json"
    authorization = _load(authorization_path)
    if _sha(manifest_path) != item.get("manifest_sha256"):
        raise ValueError(f"MANIFEST_HASH_MISMATCH:{case_id}:{attempt_id}")
    if _sha(rules_path) != item.get("locked_rules_sha256"):
        raise ValueError(f"RULES_HASH_MISMATCH:{case_id}:{attempt_id}")
    if manifest.get("schema_version") != "personal-dev-technical-smoke-manifest-v1":
        raise ValueError(f"MANIFEST_SCHEMA_MISMATCH:{case_id}:{attempt_id}")
    if case_id not in manifest.get("case_order", []) or not any(
            isinstance(row, dict) and row.get("case_id") == case_id for row in manifest.get("cases", [])):
        raise ValueError(f"CASE_NOT_IN_BOUND_MANIFEST:{case_id}:{attempt_id}")
    dataset = inputs.get("dataset", {})
    if (manifest.get("dataset_sha256") != dataset.get("sha256") or
            manifest.get("dataset_version") != dataset.get("version")):
        raise ValueError(f"DATASET_BINDING_MISMATCH:{case_id}:{attempt_id}")
    dataset_path = root / dataset.get("path", "")
    if not dataset_path.is_file() or _sha(dataset_path) != dataset.get("sha256"):
        raise ValueError(f"DATASET_FILE_HASH_MISMATCH:{case_id}:{attempt_id}")
    declared = item.get("binding", {})
    if (manifest.get("code_sha256") != declared.get("code_sha256") or
            manifest.get("ruleset_id") != declared.get("ruleset") or
            manifest.get("rules_sha256") != declared.get("rules_sha256")):
        raise ValueError(f"DECLARED_BINDING_MISMATCH:{case_id}:{attempt_id}")
    if sha256_json(rules) != manifest.get("rules_sha256"):
        raise ValueError(f"LOCKED_RULES_CONTENT_MISMATCH:{case_id}:{attempt_id}")
    if rules.get("ruleset_id") != manifest.get("ruleset_id"):
        raise ValueError(f"RULESET_ID_MISMATCH:{case_id}:{attempt_id}")
    if authorization.get("code_sha256") != manifest.get("code_sha256"):
        raise ValueError(f"AUTHORIZATION_CODE_BINDING_MISMATCH:{case_id}:{attempt_id}")
    runtime = manifest.get("runtime_dependency_binding")
    if runtime is not None:
        if declared.get("runtime_dependency_binding") != runtime:
            raise ValueError(f"DECLARED_RUNTIME_DEPENDENCY_BINDING_MISMATCH:{case_id}:{attempt_id}")
        deps = runtime.get("runtime_dependencies")
        if authorization.get("runtime_dependency_binding") != runtime:
            raise ValueError(f"RUNTIME_DEPENDENCY_BINDING_MISMATCH:{case_id}:{attempt_id}")
        if not isinstance(deps, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) != 64
                for k, v in deps.items()):
            raise ValueError(f"RUNTIME_DEPENDENCY_RECORD_INVALID:{case_id}:{attempt_id}")
        if runtime.get("base_implementation_digest") != manifest.get("code_sha256"):
            raise ValueError(f"RUNTIME_BASE_DIGEST_MISMATCH:{case_id}:{attempt_id}")
    elif declared.get("runtime_dependency_binding") is not None:
        raise ValueError(f"RUNTIME_DEPENDENCY_MISSING_FROM_MANIFEST:{case_id}:{attempt_id}")
    if report.get("ruleset_id") != manifest.get("ruleset_id"):
        raise ValueError(f"REPORT_RULESET_BINDING_MISMATCH:{case_id}:{attempt_id}")
    if report.get("run_id") != archive.get("result", {}).get("run_id"):
        raise ValueError(f"RUN_BINDING_MISMATCH:{case_id}:{attempt_id}")
    resolved = {"batch": manifest.get("smoke_id"), "code_sha256": manifest.get("code_sha256"),
        "code_revision": manifest.get("code_revision"), "ruleset": manifest.get("ruleset_id"),
        "rules_sha256": manifest.get("rules_sha256"), "dataset_version": manifest.get("dataset_version"),
        "dataset_sha256": manifest.get("dataset_sha256"), "runtime_dependency_binding": runtime}
    evidence = [{"path": item["manifest_path"], "sha256": _sha(manifest_path)},
        {"path": item["rules_path"], "sha256": _sha(rules_path)},
        {"path": (manifest_path.parent / "authorization.json").relative_to(root).as_posix(),
            "sha256": _sha(authorization_path)}]
    return resolved, evidence


def _apply_user_review(semantic: dict, event: dict, case_id: str, run_id: str) -> dict:
    result = copy.deepcopy(semantic)
    primary = next((row for row in event.get("primary_decisions", [])
        if row.get("case_id") == case_id and row.get("run_id") == run_id), None)
    correction = next((row for row in event.get("supplemental_corrections", [])
        if row.get("case_id") == case_id and row.get("run_id") == run_id), None)
    result["human_review_decision"] = primary or correction or {"status": "NOT_REVIEWED_BY_CONFIRMED_EVENT"}
    if primary and primary.get("human_review_decision") == "PASS":
        result.update(candidate_answer_status="ACCEPTED_USER_OUTPUT", delivered_output_status="PASS",
            task_success="PASS", failure_class="NONE_RECORDED")
    elif primary and case_id == "D1-ORDER-009":
        result.update(delivered_output_status="PARTIAL", task_success="FAIL", failure_class="MODEL_OR_TASK_FAILURE")
    elif primary and case_id == "D1-POLICY-014":
        result.update(candidate_answer_status="UNSCORABLE_NO_VERIFIABLE_CANDIDATE",
            delivered_output_status="FAIL", task_success="FAIL", failure_class="EVIDENCE_INSUFFICIENT")
        result["candidate_semantics"] = primary.get("candidate_semantics", "UNKNOWN")
        result["retrieval_and_two_sources"] = primary.get("retrieval_and_two_sources", "UNKNOWN")
        result["model_error"] = primary.get("model_error", "UNKNOWN")
    elif primary and case_id == "D1-SAFETY-038":
        result.update(delivered_output_status="FAIL", task_success="FAIL", failure_class="EVALUATOR_FAILURE")
    if correction:
        dimensions = result.setdefault("dimensions", {})
        if type(correction.get("required_condition_coverage")) is int:
            dimensions["required_condition_coverage"] = correction["required_condition_coverage"]
        if correction.get("complete_case_contract", "").startswith("PARTIAL"):
            dimensions["required_condition_coverage"] = min(dimensions.get("required_condition_coverage", 2), 1)
        if correction.get("accepted_output") == "ABSENT":
            result.update(candidate_answer_status="REJECTED_CANDIDATE_NOT_DELIVERED",
                delivered_output_status="FAIL", task_success="FAIL")
        if correction.get("task_success") == "FAIL":
            result["task_success"] = "FAIL"
    return result


def score_inputs(root: Path, inputs_path: Path, adjudication_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    inputs_path = inputs_path.resolve(strict=True)
    adjudication_path = adjudication_path.resolve(strict=True)
    inputs = _load(inputs_path)
    adjudication = _load(adjudication_path)
    if inputs.get("schema_version") != "m6.1-smoke-scoring-inputs-v2":
        raise ValueError("SCORING_INPUT_SCHEMA_UNSUPPORTED")
    if adjudication.get("schema_version") != "m6.1-smoke-review-adjudication-source-v2":
        raise ValueError("ADJUDICATION_SCHEMA_UNSUPPORTED")
    if adjudication.get("rubric_sha256") != _sha(root / "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/rubric-r2.json"):
        raise ValueError("RUBRIC_BINDING_MISMATCH")
    event_path = _source_path(root, inputs_path, inputs["human_confirmation_event_path"])
    event = _load(event_path)
    if _sha(event_path) != inputs.get("human_confirmation_event_sha256"):
        raise ValueError("HUMAN_CONFIRMATION_EVENT_HASH_MISMATCH")
    if (event.get("event_id") != "M61D1-HUMAN-DEV-REVIEW-CHAT-USER-20261028-R1" or
            event.get("status") != "HUMAN_REVIEW_CONFIRMED_WITH_CORRECTIONS" or
            event.get("actor", {}).get("type") != "CHAT_USER_SELF_CONFIRMED"):
        raise ValueError("HUMAN_CONFIRMATION_EVENT_NOT_VALID")
    semantic_by_key = {(row["case_id"], row["attempt_id"]): row for row in adjudication.get("assessments", [])}
    if len(semantic_by_key) != len(adjudication.get("assessments", [])):
        raise ValueError("DUPLICATE_SEMANTIC_ASSESSMENT")

    records: list[dict[str, Any]] = []
    for item in inputs.get("attempts", []):
        case_id, attempt_id = item["case_id"], item["attempt_id"]
        if item.get("execution") == "HISTORICAL_ONLY":
            archive_path = _source_path(root, inputs_path, item["archive_path"])
            archive = _load(archive_path)
            if archive.get("case_id") != case_id:
                raise ValueError(f"CASE_BINDING_MISMATCH:{case_id}:{attempt_id}")
            report_path = _source_path(root, inputs_path, item["report_path"]) if item.get("report_path") else None
            records.append({"case_id": case_id, "attempt_id": attempt_id,
                "role": item.get("role"), "execution": "HISTORICAL_ONLY",
                "failure_class": "NOT_REASSESSED_AS_PRIMARY",
                "run_id": archive.get("result", {}).get("run_id"),
                "binding": None,
                "binding_validation": item.get("binding_status", "UNKNOWN_NO_BOUND_MANIFEST"),
                "evidence": [{"path": item["archive_path"], "sha256": _sha(archive_path)}] +
                    ([{"path": item["report_path"], "sha256": _sha(report_path)}] if report_path else [])})
            continue
        report_path = _source_path(root, inputs_path, item["report_path"]) if item.get("report_path") else None
        archive_path = _source_path(root, inputs_path, item["archive_path"]) if item.get("archive_path") else None
        if item.get("execution") == "NOT_RUN":
            if report_path or archive_path:
                raise ValueError(f"NOT_RUN_MUST_NOT_BIND_ARCHIVE:{case_id}:{attempt_id}")
            records.append({"case_id": case_id, "attempt_id": attempt_id,
                "role": item.get("role"), "execution": "NOT_RUN",
                "failure_class": "NOT_RUN", "code_checks": {"overall": "NOT_RUN"},
                "semantic": semantic_by_key.get((case_id, attempt_id)),
                "run_id": None, "binding": item.get("binding"),
                "evidence": item.get("evidence", [])})
            continue
        if report_path is None or archive_path is None:
            raise ValueError(f"EXECUTED_ATTEMPT_REQUIRES_REPORT_AND_ARCHIVE:{case_id}:{attempt_id}")
        report = _load(report_path)
        archive = _load(archive_path)
        actual_archive_sha = _sha(archive_path)
        if report.get("case_id") != case_id or archive.get("case_id") != case_id:
            raise ValueError(f"CASE_BINDING_MISMATCH:{case_id}:{attempt_id}")
        if report.get("archive_sha256") != actual_archive_sha:
            raise ValueError(f"ARCHIVE_HASH_MISMATCH:{case_id}:{attempt_id}")
        run_id = report.get("run_id")
        if archive.get("result", {}).get("run_id") != run_id:
            raise ValueError(f"RUN_BINDING_MISMATCH:{case_id}:{attempt_id}")
        resolved_binding, binding_evidence = _validate_manifest_binding(root, inputs, item,
            report, archive, case_id, attempt_id)
        check_bundle = report.get("checks", {})
        checks = check_bundle.get("checks", {}) if isinstance(check_bundle, dict) else {}
        sem = semantic_by_key.get((case_id, attempt_id))
        if sem is None:
            raise ValueError(f"SEMANTIC_ASSESSMENT_MISSING:{case_id}:{attempt_id}")
        sem = _apply_user_review(sem, event, case_id, run_id)
        recomputed_budget, budget_basis = _recompute_budget_status(report, archive)
        code = {
            "run_terminal": "PASS" if report.get("run_status") in {"SUCCEEDED", "FAILED", "CANCELLED"} else "UNKNOWN",
            "execution_checks": _status(checks, "execution_checks"),
            "capability_checks": _status(checks, "capability_checks"),
            "permission_safety_checks": _status(checks, "safety_checks"),
            "business_fact_checks": _status(checks, "factual_checks"),
            "evidence_checks": _status(checks, "evidence_checks"),
            "policy_retrieval_evidence": _policy_retrieval_projection(archive, case_id, run_id),
            "budget_checks": recomputed_budget,
            "budget_recompute": budget_basis,
            "cleanup_checks": _status(checks, "cleanup_checks"),
            "output_presence_checks": _status(checks, "output_presence_checks"),
            "checker_report": "PASS" if check_bundle else "UNKNOWN",
            "blocking_items": check_bundle.get("blocking_items", []),
            "review_items": check_bundle.get("review_items", []),
        }
        answer_status = sem.get("delivered_output_status", "UNKNOWN")
        blocking = [code[name] for name in ("run_terminal", "execution_checks",
            "capability_checks", "permission_safety_checks", "budget_checks", "cleanup_checks")]
        declared_failure = sem.get("failure_class")
        if code["permission_safety_checks"] == "FAIL":
            failure_class = "APPLICATION_OR_INFRASTRUCTURE_FAILURE"
        elif declared_failure in {"MODEL_OR_TASK_FAILURE", "APPLICATION_OR_INFRASTRUCTURE_FAILURE",
                "EVALUATOR_FAILURE", "EVIDENCE_INSUFFICIENT", "UNSCORABLE", "NOT_RUN", "BUDGET_STOP"}:
            # Failure attribution comes from the evidence-backed adjudication,
            # not from a legacy STOP/FAIL aggregate that may conflate semantic
            # rejection, missing provenance and execution faults.
            failure_class = declared_failure
        elif "FAIL" in blocking:
            failure_class = "APPLICATION_OR_INFRASTRUCTURE_FAILURE"
        elif "UNKNOWN" in blocking or code["checker_report"] != "PASS":
            failure_class = "EVIDENCE_INSUFFICIENT"
        elif answer_status == "FAIL":
            failure_class = sem.get("failure_class", "MODEL_OR_TASK_FAILURE")
        elif code["evidence_checks"] == "FAIL" and sem.get("checker_reliable") is False:
            failure_class = "EVALUATOR_FAILURE"
        else:
            failure_class = sem.get("failure_class", "NONE_RECORDED")
        records.append({"case_id": case_id, "attempt_id": attempt_id,
            "role": item.get("role"), "execution": "EXECUTED",
            "original_primary_status": item.get("original_primary_status"),
            "failure_class": failure_class, "run_id": run_id,
            "run_status": report.get("run_status"),
            "execution_status": report.get("execution_status"),
            "grading_status_source": archive.get("grading_status"),
            "code_checks": code, "semantic": sem,
            "usage": report.get("budget_snapshot"),
            "cleanup": report.get("cleanup"),
            "binding": resolved_binding, "binding_validation": "PASS_FROM_MANIFEST_RULES_AUTHORIZATION",
            "evidence": [{"path": item["report_path"], "sha256": _sha(report_path)},
                {"path": item["archive_path"], "sha256": actual_archive_sha}] + binding_evidence})

    primary_cases = inputs.get("primary_case_order", [])
    primary = [next((row for row in records if row["case_id"] == case and row["role"] == "PRIMARY"), None)
        for case in primary_cases]
    if any(row is None for row in primary):
        raise ValueError("PRIMARY_CASE_SET_INCOMPLETE")
    by_binding: dict[str, list[dict[str, Any]]] = {}
    for row in primary:
        binding_key = row.get("binding", {}).get("batch", "UNKNOWN_BINDING")
        by_binding.setdefault(binding_key, []).append(row)
    per_binding = {}
    for binding_key, bound_rows in by_binding.items():
        dimension_names = ("factual_correctness", "required_condition_coverage",
            "evidence_groundedness", "helpfulness_or_appropriate_uncertainty")
        semantic_summary = {}
        for dim in dimension_names:
            scored = [row for row in bound_rows if row["semantic"].get("dimensions", {}).get(dim) in (0, 1, 2)]
            semantic_summary[dim] = {"numerator": sum(row["semantic"]["dimensions"][dim] for row in scored),
                "maximum": len(scored) * 2, "denominator_cases": len(scored),
                "unscorable": len(bound_rows) - len(scored),
                "proportion": (sum(row["semantic"]["dimensions"][dim] for row in scored) / (len(scored)*2)
                    if scored else None),
                "wilson_95_interval": None,
                "note": "Ordinal rubric sum shown descriptively for this binding only; Wilson intervals are not appropriate for this 0-2 ordinal composite."}
        failure_counts: dict[str, int] = {}
        for row in bound_rows:
            failure_counts[row["failure_class"]] = failure_counts.get(row["failure_class"], 0) + 1
        scored_rows = [row for row in bound_rows if row["semantic"].get("task_success") in ("PASS", "FAIL")]
        per_binding[binding_key] = {"case_ids": [row["case_id"] for row in bound_rows],
            "successes": sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows),
            "scored_denominator": len(scored_rows),
            "unscorable": len(bound_rows) - len(scored_rows),
            "rate": (sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows) / len(scored_rows)
                if scored_rows else None),
            "wilson_95_interval": _wilson(sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows),
                len(scored_rows)),
            "semantic_ordinal_dimensions": semantic_summary,
            "failure_class_counts": failure_counts,
            "interpretation": "Descriptive per-binding Dev reassessment only; not formal Smoke acceptance."}
    return {
        "schema_version": "m6.1-personal-dev-smoke-recomputed-scores-v2",
        "rubric_id": adjudication.get("rubric_id"),
        "classification": {"type": "PERSONAL_DEV_TECHNICAL_SMOKE", "checks": "PROVISIONAL_DEV_CHECKS",
            "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED"},
        "recomputation": {"source_inputs_sha256": _sha(inputs_path),
            "adjudication_sha256": _sha(adjudication_path), "rubric_sha256": _sha(root / "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/rubric-r2.json"),
            "attempt_count": len(records), "primary_case_count": len(primary),
            "same_binding_aggregate_claimed": False, "external_judge_calls": 0,
            "human_confirmation_event_id": event["event_id"], "human_confirmation_event_sha256": _sha(event_path)},
        "primary_case_order": primary_cases, "primary_cases": primary,
        "supplemental_attempts": [row for row in records if row["role"] != "PRIMARY"],
        "metrics": {"task_success_by_binding": per_binding,
            "mixed_binding_task_success": {"numerator": None, "denominator": None, "unscorable": None,
                "rate": None, "status": "NOT_AGGREGATED_DIFFERENT_BINDINGS"},
            "mixed_binding_semantic_ordinal_dimensions": {"status": "NOT_AGGREGATED_DIFFERENT_BINDINGS"},
            "candidate_semantics_unscorable": sum(
                (row.get("semantic") or {}).get("candidate_semantics") == "UNSCORABLE" or
                (row.get("semantic") or {}).get("candidate_answer_status") == "UNSCORABLE_NO_VERIFIABLE_CANDIDATE"
                for row in primary),
            "task_outcome_unscorable": sum((row.get("semantic") or {}).get("task_success") == "UNKNOWN"
                for row in primary),
            "failure_class_counts_by_binding": {key: row["failure_class_counts"] for key, row in per_binding.items()},
        "primary_execution": {"selected_case_record_count": len(primary),
            "selected_case_records_with_execution": sum(row["execution"] == "EXECUTED" for row in primary),
            "selected_case_records_without_execution": sum(row["execution"] == "NOT_RUN" for row in primary),
            "original_primary_manifest_not_run": sum(row.get("original_primary_status") == "NOT_RUN" for row in primary),
            "executed_by_binding": {key: sum(row["execution"] == "EXECUTED" for row in rows)
                for key, rows in by_binding.items()},
            "unscorable": sum(row["semantic"].get("task_success") == "UNKNOWN" for row in primary),
            "note": "Selected scoring records may choose a later attempt for human-reviewed case disposition; actual execution counts must be read by binding. The R2 batch ran seven cases; SAFETY-038 was NOT_RUN in that manifest and the reviewed substitute is R3."}},
        "human_review": {"status": event["status"], "actor_type": event["actor"]["type"],
            "event_id": event["event_id"], "recorded_at_utc": event["recorded_at_utc"],
            "method": event["review_method"], "scope": event["scope"],
            "independent_reviewer": "NOT_ESTABLISHED", "formal_grading_status": "NOT_GRADED"},
    }
