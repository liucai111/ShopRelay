"""Offline, deterministic scoring projection for archived personal Dev Smoke runs.

This module does not call a model or infer missing evidence. Semantic decisions
are supplied in a versioned offline adjudication file and remain provisional.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


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


def score_inputs(root: Path, inputs_path: Path, adjudication_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    inputs_path = inputs_path.resolve(strict=True)
    adjudication_path = adjudication_path.resolve(strict=True)
    inputs = _load(inputs_path)
    adjudication = _load(adjudication_path)
    if inputs.get("schema_version") != "m6.1-smoke-scoring-inputs-v1":
        raise ValueError("SCORING_INPUT_SCHEMA_UNSUPPORTED")
    if adjudication.get("schema_version") != "m6.1-smoke-provisional-adjudication-v1":
        raise ValueError("ADJUDICATION_SCHEMA_UNSUPPORTED")
    if adjudication.get("rubric_sha256") != _sha(root / "docs/eval/m6.1-smoke-scoring-closeout-r1-20261028/rubric-r1.json"):
        raise ValueError("RUBRIC_BINDING_MISMATCH")
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
                "binding": item.get("binding"),
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
        check_bundle = report.get("checks", {})
        checks = check_bundle.get("checks", {}) if isinstance(check_bundle, dict) else {}
        sem = semantic_by_key.get((case_id, attempt_id))
        if sem is None:
            raise ValueError(f"SEMANTIC_ASSESSMENT_MISSING:{case_id}:{attempt_id}")
        recomputed_budget, budget_basis = _recompute_budget_status(report, archive)
        code = {
            "run_terminal": "PASS" if report.get("run_status") in {"SUCCEEDED", "FAILED", "CANCELLED"} else "UNKNOWN",
            "execution_checks": _status(checks, "execution_checks"),
            "capability_checks": _status(checks, "capability_checks"),
            "permission_safety_checks": _status(checks, "safety_checks"),
            "business_fact_checks": _status(checks, "factual_checks"),
            "evidence_checks": _status(checks, "evidence_checks"),
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
            "binding": item.get("binding"),
            "evidence": [{"path": item["report_path"], "sha256": _sha(report_path)},
                {"path": item["archive_path"], "sha256": actual_archive_sha}]})

    primary_cases = inputs.get("primary_case_order", [])
    primary = [next((row for row in records if row["case_id"] == case and row["role"] == "PRIMARY"), None)
        for case in primary_cases]
    if any(row is None for row in primary):
        raise ValueError("PRIMARY_CASE_SET_INCOMPLETE")
    dimension_names = ("factual_correctness", "required_condition_coverage",
        "evidence_groundedness", "helpfulness_or_appropriate_uncertainty")
    semantic_summary = {}
    for dim in dimension_names:
        scored = [row for row in primary if row["semantic"].get("dimensions", {}).get(dim) in (0, 1, 2)]
        semantic_summary[dim] = {"numerator": sum(row["semantic"]["dimensions"][dim] for row in scored),
            "maximum": len(scored) * 2, "denominator_cases": len(scored),
            "unscorable": len(primary) - len(scored),
            "proportion": (sum(row["semantic"]["dimensions"][dim] for row in scored) / (len(scored)*2)
                if scored else None),
            "wilson_95_interval": None,
            "note": "Ordinal rubric sum shown descriptively; Wilson intervals are not appropriate for this 0-2 ordinal composite."}
    failure_counts: dict[str, int] = {}
    for row in primary:
        failure_counts[row["failure_class"]] = failure_counts.get(row["failure_class"], 0) + 1
    by_binding: dict[str, list[dict[str, Any]]] = {}
    for row in primary:
        binding_key = row.get("binding", {}).get("batch", "UNKNOWN_BINDING")
        by_binding.setdefault(binding_key, []).append(row)
    per_binding = {}
    for binding_key, bound_rows in by_binding.items():
        scored_rows = [row for row in bound_rows if row["semantic"].get("task_success") in ("PASS", "FAIL")]
        per_binding[binding_key] = {"case_ids": [row["case_id"] for row in bound_rows],
            "successes": sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows),
            "scored_denominator": len(scored_rows),
            "unscorable": len(bound_rows) - len(scored_rows),
            "rate": (sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows) / len(scored_rows)
                if scored_rows else None),
            "wilson_95_interval": _wilson(sum(row["semantic"]["task_success"] == "PASS" for row in scored_rows),
                len(scored_rows)),
            "interpretation": "Descriptive per-binding Dev reassessment only; not formal Smoke acceptance."}
    return {
        "schema_version": "m6.1-personal-dev-smoke-recomputed-scores-v1",
        "rubric_id": adjudication.get("rubric_id"),
        "classification": {"type": "PERSONAL_DEV_TECHNICAL_SMOKE", "checks": "PROVISIONAL_DEV_CHECKS",
            "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED"},
        "recomputation": {"source_inputs_sha256": _sha(inputs_path),
            "adjudication_sha256": _sha(adjudication_path), "rubric_sha256": _sha(root / "docs/eval/m6.1-smoke-scoring-closeout-r1-20261028/rubric-r1.json"),
            "attempt_count": len(records), "primary_case_count": len(primary),
            "same_binding_aggregate_claimed": False, "external_judge_calls": 0},
        "primary_case_order": primary_cases, "primary_cases": primary,
        "supplemental_attempts": [row for row in records if row["role"] != "PRIMARY"],
        "metrics": {"task_success_by_binding": per_binding,
            "mixed_binding_task_success": {"numerator": None, "denominator": None, "unscorable": None,
                "rate": None, "status": "NOT_AGGREGATED_DIFFERENT_BINDINGS"},
            "semantic_ordinal_dimensions": semantic_summary,
            "failure_class_counts_primary": failure_counts,
            "primary_execution": {"executed": sum(row["execution"] == "EXECUTED" for row in primary),
                "not_run": sum(row["execution"] == "NOT_RUN" for row in primary),
                "original_primary_manifest_not_run": sum(row.get("original_primary_status") == "NOT_RUN" for row in primary),
                "unscorable": sum(row["semantic"].get("task_success") == "UNKNOWN" for row in primary)}},
        "human_review": {"status": "PENDING_HUMAN_REVIEW", "reviewer": None,
            "reviewed_at_utc": None, "decision": None},
    }
