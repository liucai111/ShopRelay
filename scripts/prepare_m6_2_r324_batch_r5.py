"""Freeze the exact application-ready remainder of the r3.2.4 READY ledger."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import DATASET_REL, DATASET_SHA256, derive_limits

PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029"
MATRIX_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"
OFFLINE_REL = f"{PACKAGE_REL}/application-admission-offline-r1.json"
R4_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main() -> int:
    package = ROOT / PACKAGE_REL
    blocked_preflight_path = package / "application-admission-r5-blocked.json"
    suffix = "final4" if blocked_preflight_path.is_file() else "final3"
    final_names = (f"batch-manifest-r5-{suffix}.json", f"locked-rules-r5-{suffix}.json", f"batch-selection-r5-{suffix}.json")
    if any((package / name).exists() for name in final_names):
        raise SystemExit(f"R5_{suffix.upper()}_PROFILE_FILES_ALREADY_EXIST_NO_OVERWRITE")
    matrix_path, offline_path = ROOT / MATRIX_REL, ROOT / OFFLINE_REL
    matrix, offline = json.loads(matrix_path.read_text(encoding="utf-8")), json.loads(offline_path.read_text(encoding="utf-8"))
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    if sha(ROOT / DATASET_REL) != DATASET_SHA256:
        raise SystemExit("R5_DATASET_HASH_MISMATCH")
    matrix_rows = matrix.get("cases", [])
    expected_30 = [row["case_id"] for row in matrix_rows
        if row.get("admission_status") == "READY" and row.get("case_id") != "D1-POLICY-013"]
    records = offline.get("records", [])
    if ([row.get("case_id") for row in records] != expected_30 or len(records) != 30 or
            offline.get("external_calls") != 0 or offline.get("implementation_digest") != implementation_digest(ROOT) or
            offline.get("dataset_sha256") != DATASET_SHA256):
        raise SystemExit("R5_ALL30_OFFLINE_APPLICATION_BINDING_INVALID")
    for record in records:
        archive = ROOT / record["offline_archive_path"]
        if (not archive.is_file() or sha(archive) != record.get("offline_archive_sha256") or
                record.get("cleanup", {}).get("cleanup_status") != "CLEANED_AND_RELEASED" or
                record.get("cleanup", {}).get("residual_case_rows") != 0):
            raise SystemExit(f"R5_OFFLINE_CASE_ARCHIVE_OR_CLEANUP_INVALID:{record.get('case_id')}")
    ready_ids = [row["case_id"] for row in records if row.get("application_admission") == "APPLICATION_READY"]
    repository_binding = None
    if blocked_preflight_path.is_file():
        preflight = json.loads(blocked_preflight_path.read_text(encoding="utf-8"))
        matrix_ready = [row["case_id"] for row in records if row.get("application_admission") == "APPLICATION_READY"]
        preflight_rows = {row.get("case_id"): row for row in preflight.get("repository_case_results", [])}
        if (preflight.get("case_ids") != matrix_ready or preflight.get("run_created") is not False or
                preflight.get("provider_requests") != 0 or preflight.get("implementation_digest") != implementation_digest(ROOT) or
                preflight.get("dataset_sha256") != DATASET_SHA256 or set(preflight_rows) != set(matrix_ready)):
            raise SystemExit("R5_REPOSITORY_ADMISSION_BINDING_INVALID")
        selected_statuses = {"APPLICATION_REPOSITORY_PREFLIGHT_PASS", "NO_BUSINESS_READS_REQUIRED"}
        ready_ids = [case_id for case_id in ready_ids if preflight_rows[case_id].get("status") in selected_statuses]
        repository_binding = {"path": f"{PACKAGE_REL}/application-admission-r5-blocked.json",
            "sha256": sha(blocked_preflight_path), "case_ids": matrix_ready,
            "passing_case_ids": ready_ids,
            "blocked_case_ids": [case_id for case_id in matrix_ready if case_id not in ready_ids],
            "run_created": False, "provider_requests": 0}
    if not ready_ids or len(ready_ids) > 30:
        raise SystemExit("R5_NO_APPLICATION_READY_SUBSET")
    # The first live trial must follow the ordinary route that needs a model.
    # PRODUCT-002 has an intercepted LLM route in the all-case application sweep.
    first = next((row for row in records if row.get("case_id") == "D1-PRODUCT-002"), None)
    if (first is None or first.get("application_admission") != "APPLICATION_READY" or
            first.get("route", {}).get("decision_source") != "LLM" or
            int(first.get("provider_requests_intercepted", 0)) < 1):
        raise SystemExit("R5_FIRST_MODEL_REQUIRED_CASE_NOT_PROVEN")
    execution_order = ["D1-PRODUCT-002"] + [case_id for case_id in ready_ids if case_id != "D1-PRODUCT-002"]
    ids_in_bundle = {case["case_id"]: case for case in bundle["cases"]}
    matrix_by_id = {row["case_id"]: row for row in matrix_rows}
    rules_cases = {}
    for case_id in ready_ids:
        matrix_case = matrix_by_id[case_id]
        case = ids_in_bundle[case_id]
        declared = [row["tool_id"] for row in case.get("input", {}).get("expected_tool_calls", [])
                    if row.get("mode") == "read_only"]
        if sorted(set(declared)) != sorted(matrix_case.get("allowed_read_only_capabilities", [])):
            raise SystemExit(f"R5_CASE_ALLOWLIST_SOURCE_MISMATCH:{case_id}")
        rules_cases[case_id] = {
            "allowed_capabilities": sorted(declared),
            "expected_application_path_classes": ["DIRECT", "REFUSE", "SINGLE", "AGENT"],
            "maximum_business_read_calls": 8,
            "required_execution_checks": ["same_run_trace_event_association", "route_within_case_allowlist",
                "expected_tool_or_policy_evidence_or_explicit_zero_read_route", "known_settled_budget", "exact_receipt_cleanup"],
            "source_evidence_requirements": ["result_ref_resolves_within_same_run", "owner_checks_for_private_resources",
                "policy_version_and_effective_interval_from_current_retrieval_when_policy_is_used"],
            "expected_business_result_for_reviewer": matrix_case.get("expected_business_result"),
            "hard_gates_for_reviewer": matrix_case.get("hard_gates", []),
            "semantic_requirements_for_reviewer": matrix_case.get("semantic_requirements", {}),
            "task_success_predicate": matrix_case.get("task_success_predicate"),
            "no_expected_result_in_router_or_tool_arguments": True,
        }
    _, batch_limits = derive_limits(len(ready_ids))
    rules = {
        "schema_version": "m6.2-r324-exact-subset-r5",
        "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
        "execution_scope": "exact application-ready case IDs and order from the 30-case offline application sweep; no substitutions",
        "case_order": execution_order,
        "source_matrix_case_ids_sha256": hashlib.sha256("\n".join(row["case_id"] for row in matrix_rows).encode()).hexdigest(),
        "application_admission_sha256": sha(offline_path),
        "case_rules": rules_cases,
        "budget_policy": {"provider_requests_per_case": 6, "business_read_calls_per_case": 8,
            "tokens_per_case": 120000, "local_estimated_usd_per_case": .18,
            "timeout_seconds_per_case": 180, "concurrency": 1, "automatic_retries": 0,
            "judge": 0, "embedding": 0, "business_writes": 0, "pending_actions": 0,
            "preflight_repository_reads_per_case": 8, "preflight_reads_are_trial_calls": False,
            "local_estimate_is_provider_hard_cap": False},
        "per_case_budget_limits": derive_limits(len(ready_ids))[0].to_dict(),
        "batch_budget_limits": batch_limits.to_dict(),
        "outcome_vocabulary": {"PASS": "locked outcome requirements supported by current-run evidence",
            "PARTIAL": "supported but one or more required components are incomplete",
            "FAIL": "reliable evidence contradicts or does not satisfy task requirements",
            "UNKNOWN": "critical execution or fact evidence cannot be established",
            "UNSCORABLE": "the source/rubric cannot support a defensible decision", "NOT_RUN": "no Run was created",
            "EVALUATOR_FAILURE": "checker or evidence projection failed"},
        "dimension_separation": ["route_scope", "application_execution", "business_facts", "source_evidence",
            "user_visible_delivery", "task_outcome", "system_failure", "budget", "cleanup"],
        "run_succeeded_is_not_task_success": True,
        "agent_started_is_not_required_for_valid_direct_or_refusal": True,
        "candidate_or_frozen_access": False, "formal_gates_changed": False,
        "post_result_rule_changes": False,
        "continuation_rule": "Continue after a task FAIL/PARTIAL only when run is terminal, evidence/audit is complete, budget is known and settled, authorization remains valid, and exact cleanup succeeded. Stop on model/contract, permission, ledger, identity, audit, UNKNOWN critical evidence, checker, or cleanup failure.",
    }
    rules_path = package / f"locked-rules-r5-{suffix}.json"
    write_new(rules_path, rules)
    r4_manifest = json.loads((ROOT / R4_REL / "batch-manifest-r4.json").read_text(encoding="utf-8"))
    repository_audit = r4_manifest["repository_audit"]
    repository_audit["sha256"] = sha(ROOT / repository_audit["path"])
    pricing_path = f"{R4_REL}/official-pricing-evidence-r4.json"
    pricing_sha = sha(ROOT / pricing_path)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": "m6.2-r324-personal-dev-exact-subset-manifest-r5",
        "profile_version": "m6.2-r324-exact-subset-r5",
        "batch_id": "M62-R324-REMAINDER-R5-20261029-" + uuid.uuid4().hex[:12],
        "created_at_utc": timestamp, "status": "FROZEN",
        "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "grading_status": "NOT_GRADED", "review_status": "reference_unreviewed",
        "dataset": {"path": DATASET_REL, "version": bundle["version"], "sha256": DATASET_SHA256,
            "case_count": 40, "stable_case_ids_and_group_ids_preserved": True},
        "case_universe_count": 40, "case_admission_counts": matrix["admission_counts"],
        "admission_matrix_path": MATRIX_REL, "admission_matrix_sha256": sha(matrix_path),
        "authorized_candidate_case_ids": execution_order, "proposed_order": execution_order,
        "application_ready_count": len(ready_ids), "application_admission_path": OFFLINE_REL,
        "application_admission_sha256": sha(offline_path),
        "application_admission_coverage": {"candidates": 30, "application_ready": len(ready_ids),
            "blocked": 30-len(ready_ids), "all_candidates_processed": True,
            "external_calls": 0, "intercepted_model_requests_only": True},
        "repository_preflight_binding": repository_binding,
        "repository_audit": repository_audit,
        "locked_rules_path": f"{PACKAGE_REL}/locked-rules-r5-{suffix}.json", "locked_rules_sha256": sha(rules_path),
        "bindings": {"implementation_digest": implementation_digest(ROOT),
            "git_head_revision": __import__("subprocess").check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "git_head_is_not_working_tree_identity": True,
            "route_version": "m2.1-route-v10", "route_prompt_version": "m2.1-deepseek-route-v4",
            "answer_prompt_version": "m1-policy-answer-v1", "model": r4_manifest["bindings"]["model"],
            "provider_endpoint": r4_manifest["bindings"]["provider_endpoint"],
            "database_profile": r4_manifest["bindings"]["database_profile"],
            "database_identity": r4_manifest["bindings"]["database_identity"],
            "pricing_evidence_path": pricing_path, "pricing_evidence_sha256": pricing_sha},
        "authorized_limits": {"application_runs": len(ready_ids), "provider_requests": 6*len(ready_ids),
            "business_read_calls": 8*len(ready_ids), "tokens": 120000*len(ready_ids),
            "local_estimated_usd": round(.18*len(ready_ids), 2),
            "runtime_seconds": min(7200, 180*len(ready_ids)), "concurrency": 1,
            "automatic_retries": 0, "judge": 0, "embedding": 0, "business_writes": 0,
            "pending_actions": 0, "repository_preflight_reads": 8*len(ready_ids)},
        "selection_note": "The original 30 remaining READY IDs were each exercised through the real application execution entry with in-process model interception. Only APPLICATION_READY rows are selected; the remaining rows are preserved as blocked, not dropped from the 40-case ledger.",
        "not_selected_ready_case_ids": [row["case_id"] for row in records if row.get("application_admission") != "APPLICATION_READY"],
        "r4_policy013_run_not_repeated": True, "protected_assets_allowed": False, "formal_gates_changed": False,
    }
    manifest_path = package / f"batch-manifest-r5-{suffix}.json"
    write_new(manifest_path, manifest)
    write_new(package / f"batch-selection-r5-{suffix}.json", {"schema_version": "m6.2-r324-r5-selection-r3",
        "source_matrix_sha256": sha(matrix_path), "offline_admission_sha256": sha(offline_path),
        "selected_case_ids": execution_order, "not_selected_case_ids": manifest["not_selected_ready_case_ids"],
        "selection_is_exact": True, "execution_order_reason": "The first case is the first model-required case proven by intercepted ordinary application routing; remaining APPLICATION_READY cases retain source-matrix relative order."})
    print(json.dumps({"batch_id": manifest["batch_id"], "application_ready_count": len(ready_ids),
        "selected_case_ids": execution_order, "blocked_case_ids": manifest["not_selected_ready_case_ids"],
        "manifest": manifest_path.relative_to(ROOT).as_posix(), "rules": rules_path.relative_to(ROOT).as_posix(),
        "code_sha256": manifest["bindings"]["implementation_digest"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
