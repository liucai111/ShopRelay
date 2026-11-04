"""Prepare/authorize the approved exact seven cases using the existing batch CLI.

No application or model execution occurs here. Each artifact is exclusively created.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_m6_2_r324_personal_dev_batch_r7 as runner
from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger, derive_limits, SOURCE_MATRIX_REL
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_2.confirmed_dev_preparation import OUT, EVENT_ID
from app.m0.config import load_settings

PREVIOUS = "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030"


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf8"))


def freeze(*, evidence: Path, target: PersonalDevDatabase | None = None) -> Path:
    profile = runner.PROFILE_CONFIGS["r325-seven"]
    package = ROOT / profile["package"]
    if (package / "manifest.json").exists():
        raise ValueError("FROZEN_MANIFEST_ALREADY_EXISTS")
    target = target or PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    try:
        idle = runner._assert_idle_personal_database(factory, target)
    finally:
        engine.dispose()
    settings = load_settings(require_api_key=False)
    data = read(ROOT / profile["dataset_rel"])
    assert runner.digest(ROOT / profile["dataset_rel"]) == profile["dataset_sha256"]
    cases = {c["case_id"]: c for c in data["cases"]}
    matrix = read(ROOT / SOURCE_MATRIX_REL)
    for row in matrix["cases"]:
        if row["case_id"] in runner.R325_CASES:
            row.update({"admission_status": "READY", "approval_event": EVENT_ID,
                "allowed_read_only_capabilities": sorted(t["tool_id"] for t in cases[row["case_id"]]["input"]["expected_tool_calls"]),
                "repository_read_count_preflight_only": 8})
    matrix["admission_counts"] = dict(Counter(c["admission_status"] for c in matrix["cases"]))
    write(ROOT / profile["matrix_rel"], matrix)
    offline = read(evidence)
    if offline["case_ids"] != runner.R325_CASES or offline["implementation_digest"] != implementation_digest(ROOT):
        raise ValueError("OFFLINE_APPLICATION_EVIDENCE_STALE")
    write(package / "application-evidence.json", offline)
    previous = ROOT / PREVIOUS / "task-cumulative-ledger-r3-four-first-runs.json"
    prior = read(previous)
    TaskQuotaLedger(previous, task_id=prior["task_id"], seed_sha256=prior["seed_sha256"]).read()
    quota_path = package / "task-cumulative-ledger.json"
    prior["limits"]["application_runs"] = 27
    prior["limit_amendments"].append({"from_application_runs": 20, "to_application_runs": 27,
        "source_sha256": runner.digest(previous),
        "authorization_basis": "CURRENT_USER_INSTRUCTION_SEVEN_FIRST_RUNS_R325"})
    quota = TaskQuotaLedger(quota_path, task_id=prior["task_id"], seed_sha256=prior["seed_sha256"])
    if quota_path.exists():
        raise ValueError("TASK_LEDGER_ALREADY_EXISTS")
    quota.write(prior)
    quota.read()
    per_case, batch = derive_limits(7, per_case_overrides=profile["per_case_limits"])
    rules = read(ROOT / PREVIOUS / "locked-rules-r10-first-runs-r2.json")
    rules.update({"schema_version": "m62-r325-seven-locked-rules-v1", "case_order": runner.R325_CASES,
        "per_case_budget_limits": per_case.to_dict(), "batch_budget_limits": batch.to_dict(), "case_rules": {}})
    from eval.m6_1.contracts import sha256_json
    for case_id in runner.R325_CASES:
        case = cases[case_id]
        rules["case_rules"][case_id] = {
            "allowed_capabilities": sorted(t["tool_id"] for t in case["input"]["expected_tool_calls"]),
            "maximum_business_read_calls": len(case["input"]["expected_tool_calls"]),
            "case_input_sha256": sha256_json(case["input"]),
            "business_outcome": case["expected_outcome"]["business_outcome"],
            "semantic_quality_rubric": case["expected_outcome"]["semantic_quality_rubric"],
            "semantic_decision": "AI_ASSISTED_DEV_PREVIEW_PENDING_EXACT_RUN_USER_REVIEW",
            "unknown_business_fact_is_not_missing_execution_evidence": True}
    write(package / "locked-rules.json", rules)
    price = read(ROOT / PREVIOUS / "official-pricing-evidence-r10.json")
    write(package / "pricing-evidence.json", price)
    now = datetime.now(timezone.utc)
    batch_id = "M62-R325-SEVEN-" + uuid.uuid4().hex.upper()
    suffix = batch_id[-16:]
    paths = {key: f"{stem}-{profile['tag']}-{suffix}{'.jsonl' if key == 'journal' else '.json'}"
             for key, stem in runner.RUNTIME_STEMS.items()}
    manifest = read(ROOT / PREVIOUS / "batch-manifest-r10-first-runs-r5.json")
    for key in ("readiness_exception", "r9_first_run_sources", "predecessor_binding", "limits"):
        manifest.pop(key, None)
    manifest.update({"schema_version": "m62-r325-seven-manifest-v1", "profile_version": profile["profile_version"],
        "batch_id": batch_id, "created_at_utc": now.isoformat(), "status": "FROZEN",
        "case_universe_count_for_segment": 7, "case_admission_counts": matrix["admission_counts"],
        "admission_matrix_path": profile["matrix_rel"], "admission_matrix_sha256": runner.digest(ROOT / profile["matrix_rel"]),
        "authorized_candidate_case_ids": runner.R325_CASES, "proposed_order": runner.R325_CASES,
        "dataset": {"path": profile["dataset_rel"], "sha256": profile["dataset_sha256"], "version": data["version"], "case_count": 40},
        "locked_rules_path": (package / "locked-rules.json").relative_to(ROOT).as_posix(),
        "locked_rules_sha256": runner.digest(package / "locked-rules.json"),
        "runtime_paths": paths, "task_ledger_path": quota_path.relative_to(ROOT).as_posix(),
        "previous_task_ledger_sha256": runner.digest(previous),
        "approved_sources": {(OUT / name).as_posix(): runner.digest(ROOT / OUT / name)
            for name in ("confirmation-event-r1.json", "confirmed-dispositions-r1.json")},
        "runtime_dependencies": {path: runner.digest(ROOT / path) for path in (
            "app/m2/self_rag.py", "eval/m6_2/confirmed_dev_preparation.py", "scripts/prepare_m6_2_r325_seven.py")},
        "application_admission_path": (package / "application-evidence.json").relative_to(ROOT).as_posix(),
        "application_admission_sha256": runner.digest(package / "application-evidence.json"),
        "budget_contract": {"per_case": per_case.to_dict(), "batch": batch.to_dict()},
        "task_total_limits": prior["limits"], "database_idle_at_freeze": idle})
    bindings = manifest["bindings"]
    bindings.update({"implementation_digest": implementation_digest(ROOT), "dataset_sha256": profile["dataset_sha256"],
        "database_path": str(target.path), "database_identity": target.identity,
        "model": settings.model, "provider_endpoint": settings.base_url,
        "git_head_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "pricing_evidence_path": (package / "pricing-evidence.json").relative_to(ROOT).as_posix(),
        "pricing_checked_at_utc": price["checked_at_utc"], "pricing_evidence_sha256": runner.digest(package / "pricing-evidence.json"),
        "node_config": {name: vars(config) for name, config in settings.nodes.items()}})
    # Price is reused only inside its existing 24-hour validity; never refresh its timestamp artificially.
    runner._validate_price_evidence(ROOT, manifest, model=settings.model, endpoint=settings.base_url)
    write(package / "manifest.json", manifest)
    write(package / paths["budget"], {"batch_id": batch_id, "manifest_sha256": runner.digest(package / "manifest.json"),
        "rules_sha256": runner.digest(package / "locked-rules.json"), "batch_limits": batch.to_dict(), "per_case_limits": per_case.to_dict()})
    return package / "manifest.json"


def authorize() -> Path:
    profile = runner.PROFILE_CONFIGS["r325-seven"]
    package = ROOT / profile["package"]
    manifest = read(package / "manifest.json")
    admission = package / manifest["runtime_paths"]["admission"]
    checked = read(admission)
    if checked["admission"] != "PASS" or checked["case_ids"] != runner.R325_CASES:
        raise ValueError("COMPLETE_SEVEN_CASE_ADMISSION_REQUIRED")
    now = datetime.now(timezone.utc)
    ledger = TaskQuotaLedger(package / "task-cumulative-ledger.json", task_id=manifest["task_id"], seed_sha256=manifest["task_ledger_seed_sha256"]).read()
    bindings = manifest["bindings"]
    auth = {"schema_version": "m62-r325-seven-authorization-v1", "authorization_id": "M62-R325-AUTH-" + uuid.uuid4().hex,
        "status": "ACTIVE", "authorization_type": profile["auth_type"], "batch_id": manifest["batch_id"],
        "task_id": manifest["task_id"], "case_ids": runner.R325_CASES,
        "authorized_by": "CHAT_USER_SELF_CONFIRMED", "authorization_basis": "Current user instruction: exact seven first real Dev trials, shared cap 27, 2026-10-31; not the prior preparation-only A/B event.",
        "confirmed_at_utc": now.isoformat(), "expires_at_utc": (now + timedelta(hours=8)).isoformat(),
        "grading_status": "NOT_GRADED", "manifest_sha256": runner.digest(package / "manifest.json"),
        "rules_sha256": manifest["locked_rules_sha256"], "code_sha256": bindings["implementation_digest"],
        "dataset_sha256": manifest["dataset"]["sha256"], "database_identity": bindings["database_identity"],
        "model": bindings["model"], "provider_endpoint": bindings["provider_endpoint"],
        "git_head_revision": bindings["git_head_revision"], "pricing_sha256": checked["pricing_sha256"],
        "budget_sha256": runner.digest(package / manifest["runtime_paths"]["budget"]),
        "application_admission_sha256": runner.digest(admission),
        "task_ledger_seed_sha256": manifest["task_ledger_seed_sha256"], "task_ledger_state_sha256": ledger["state_sha256"]}
    path = package / manifest["runtime_paths"]["authorization"]
    write(path, auth)
    return path


def report() -> dict:
    """Recompute from raw archives and settled journal entries, with no client imports/calls."""
    from eval.m6_1.personal_dev_batch_m62 import score_archive_current
    from scripts.recompute_m6_2_r9_policy018_closeout import wilson
    package = ROOT / runner.PROFILE_CONFIGS["r325-seven"]["package"]
    manifest = read(package / "manifest.json")
    profile = runner.PROFILE_CONFIGS["r325-seven"]
    if (manifest["proposed_order"] != runner.R325_CASES or
            manifest["dataset"]["path"] != profile["dataset_rel"] or
            runner.digest(ROOT / profile["dataset_rel"]) != manifest["dataset"]["sha256"] or
            manifest["dataset"]["sha256"] != profile["dataset_sha256"] or
            runner.digest(package / "locked-rules.json") != manifest["locked_rules_sha256"] or
            implementation_digest(ROOT) != manifest["bindings"]["implementation_digest"] or
            any(runner.digest(ROOT / path) != sha for path, sha in manifest["runtime_dependencies"].items())):
        raise ValueError("RECOMPUTE_INPUT_BINDING_MISMATCH")
    bundle = read(ROOT / manifest["dataset"]["path"])
    cases = {c["case_id"]: c for c in bundle["cases"]}
    events = [json.loads(line) for line in (package / manifest["runtime_paths"]["journal"]).read_text(encoding="utf8").splitlines() if line.strip()]
    closed = {row["case_id"]: row for row in events if row["event"] == "CASE_CLOSEOUT"}
    previews = {row["run_id"]: row for row in read(package / "ai-review.json")["items"]} if (package / "ai-review.json").exists() else {}
    rows = []
    for case_id in runner.R325_CASES:
        if case_id not in closed:
            rows.append({"case_id": case_id, "execution": "NOT_RUN_OR_EXCEPTION_SEE_STATE", "task_outcome": "NOT_RUN"})
            continue
        event = closed[case_id]
        archive_path = ROOT / event["archive_path"]
        if runner.digest(archive_path) != event["archive_sha256"]:
            raise ValueError("ARCHIVE_HASH_MISMATCH")
        archive = read(archive_path)
        if archive["case_id"] != case_id or archive["result"]["run_id"] != event["run_id"]:
            raise ValueError("CASE_RUN_BINDING_MISMATCH")
        grade = score_archive_current(cases[case_id], archive, budget_snapshot=event["budget"],
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        original_grade = read(ROOT / event["grader_path"])
        if grade != original_grade:
            raise ValueError("RAW_ARCHIVE_RECOMPUTE_DIFFERS_FROM_EXECUTION_GRADE")
        result = archive["result"]
        inner = result["result"] or {}
        obs = result["observations"]
        route = inner.get("route")
        if route is None:
            route = next((json.loads(e["payload_json"]) for e in obs["run_events"] if e["event_type"] == "route_decided"), None)
        answer = next((m["content"] for m in reversed(obs["messages"]) if m["role"] == "assistant"), None)
        preview = previews.get(event["run_id"])
        if preview and (preview["case_id"] != case_id or preview["archive_sha256"] != event["archive_sha256"]):
            raise ValueError("AI_REVIEW_EXACT_RUN_BINDING_MISMATCH")
        rows.append({"case_id": case_id, "run_id": event["run_id"], "execution": result["status"],
            "query": cases[case_id]["input"]["user_query"], "delivered_answer": answer,
            "route": route, "trace_ids": sorted({t["trace_id"] for t in obs["traces"]}),
            "nodes": [json.loads(e["payload_json"]) for e in obs["run_events"] if e["event_type"] in {"agent_started", "agent_handoff"}],
            "business_results": inner.get("business_results"), "business_evidence": inner.get("business_evidence"),
            "tool_call_audit": inner.get("tool_call_audit"), "tool_references": inner.get("tool_references"),
            "policy_evidence": inner.get("evidence"), "citations": inner.get("citations"),
            "candidate": grade.get("candidate_assessment"), "code_grade": grade,
            "budget": event["budget"], "cleanup": event["cleanup"],
            "archive_path": event["archive_path"], "archive_sha256": event["archive_sha256"],
            "task_outcome": preview["task_outcome"] if preview else "PENDING_AI_PREVIEW",
            "ai_preview": preview, "human_decision": None, "reviewer": None, "reviewed_at": None,
            "binding": {"implementation_digest": manifest["bindings"]["implementation_digest"],
                "dataset_sha256": manifest["dataset"]["sha256"], "rules_sha256": manifest["locked_rules_sha256"]}})
    task_ledger = read(package / "task-cumulative-ledger.json")
    previous = read(ROOT / PREVIOUS / "task-cumulative-ledger-r3-four-first-runs.json")
    counts = Counter(row["task_outcome"] for row in rows)
    n = len(closed)
    task_denominator = sum(counts[key] for key in ("PASS", "PARTIAL", "FAIL"))
    program_metrics = {}
    for name in sorted({key for row in rows if "code_grade" in row for key in row["code_grade"]["program_checks"]}):
        values = Counter(row["code_grade"]["program_checks"][name] for row in rows if "code_grade" in row)
        denom = values["PASS"] + values["FAIL"]
        program_metrics[name] = {"counts": dict(values), "n": values["PASS"], "N": denom,
            "wilson95": wilson(values["PASS"], denom) if denom else None,
            "excluded": {key: value for key, value in values.items() if key not in {"PASS", "FAIL"}}}
    retrieval_rows = []
    for case_id, event in closed.items():
        raw = read(ROOT / event["archive_path"])["result"]
        for item in raw["observations"]["run_events"]:
            if item["event_type"] == "policy_retrieval_finished":
                retrieval_rows.append({"case_id": case_id, "run_id": raw["run_id"],
                    "query": cases[case_id]["input"]["user_query"], "archive_path": event["archive_path"],
                    "archive_sha256": event["archive_sha256"], "actual_finished_event": json.loads(item["payload_json"]),
                    "relevance_labels": None, "recall_mrr": "N/A_NO_HUMAN_QRELS"})
    previous_metrics = read(ROOT / OUT / "confirmed-task-metrics-r1.json")
    prior_decisions = read(ROOT / OUT / "confirmed-decisions-r1.json")["items"]
    coverage_rows = [{"case_id": row["case_id"], "run_id": row["run_id"], "version": "historical_r324",
        "task_outcome": row["decision"], "binding": row["binding"],
        "review": EVENT_ID, "continuation_compliance": row["continuation_compliance"]} for row in prior_decisions]
    coverage_rows.extend({"case_id": row["case_id"], "run_id": row.get("run_id"), "version": "new_r325",
        "task_outcome": row["task_outcome"], "binding": row.get("binding"),
        "review": "PENDING_EXACT_NEW_RUN_USER_REVIEW", "continuation_compliance": "LOCKED_CURRENT_RULES"} for row in rows)
    coverage_rows.append({"case_id": "D1-CROSSAGENT-033", "run_id": None, "task_outcome": "NOT_RUN",
        "review": "DEFERRED_BY_CONFIRMED_DISPOSITION", "reason": "Case-specific independent electrical and business/policy review remains required if included."})
    if len(coverage_rows) != 40 or {row["case_id"] for row in coverage_rows} != set(cases):
        raise ValueError("FORTY_CASE_COVERAGE_NOT_CONSERVED")
    r4 = read(ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030/reports/r19-20261031/metrics-r19.json")["judge_calibration"]
    two_sample = read(ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029/judge-analysis-r1.json")
    summary = {"schema_version": "m62-r325-seven-closeout-derived-v1", "classification": "AI_ASSISTED_DEV_PROVISIONAL_NOT_GRADED",
        "case_count": 7, "real_runs": n, "task_outcomes": dict(counts),
        "task_pass_fraction": {"n": counts["PASS"], "N": task_denominator,
            "wilson95": wilson(counts["PASS"], task_denominator) if task_denominator else None,
            "excluded_pending_or_unscorable": n-task_denominator},
        "cumulative_spent": task_ledger["spent"],
        "segment_spent": {k: task_ledger["spent"][k] - previous["spent"][k] for k in previous["spent"]},
        "remaining": {k: task_ledger["limits"][k] - task_ledger["spent"][k] - task_ledger["reserved"].get(k, 0) for k in task_ledger["limits"]},
        "reserved": task_ledger["reserved"], "supplier_billed_cost": "UNKNOWN_NO_INVOICE",
        "old32_exact_run_user_confirmation": EVENT_ID,
        "coverage": {"universe": 40, "historical_runs_r324": 32, "new_runs_r325": n,
            "total_cases_with_real_run": 32+n, "deferred": ["D1-CROSSAGENT-033"],
            "post_STOP_observations_not_compliant": ["D1-SAFETY-039", "D1-SAFETY-040"]},
        "raw_archive_code_grades_recomputed_equal": n, "rows": rows}
    summary.update({"program_metrics": program_metrics,
        "coverage_rows": coverage_rows,
        "route_sources": dict(Counter(row["route"]["decision_source"] for row in rows if row.get("route"))),
        "historical_confirmed_task_groups": previous_metrics["groups"],
        "historical_program_metrics_source": previous_metrics["program_metrics_source"],
        "unified_39_case_success_rate": "NOT_COMPUTED_DIFFERENT_BINDINGS_AND_POST_STOP_OBSERVATIONS",
        "new_retrieval_evidence": retrieval_rows,
        "retrieval_metric_gap": "Human relevance labels against the complete permitted Dev corpus; event evidence IDs alone are not Recall/MRR or reconstructed historical ranking.",
        "judge_scopes_kept_separate": {"r4_content_and_claims": r4,
            "two_sample_visible_task_completion": {k: two_sample[k] for k in ("scoring_target", "agreement", "false_accept", "false_reject", "confusion_matrix_rows_reference_columns_judge")}},
        "m62_tasks": [
            {"task": 1, "status": "PARTIAL", "evidence": "Historical confirmed32 plus new exact seven raw/code/AI scores. No current unified Frozen score; retrieval qrels and independent labels missing."},
            {"task": 2, "status": "PARTIAL", "evidence": "Existing R4 6/8 and separate visible-task 1/2 agreement. No new Judge calls; dimension labels and independent representative calibration remain missing."},
            {"task": 3, "status": "NOT_EXECUTED", "missing": "Approved exact freeze scope, real independent annotation/next-day blind review, case033 specialist disposition if included, applicable Registry/grant/M2/isolation decisions, explicit Frozen authorization and locked execution inputs."},
            {"task": 4, "status": "DEV_REPORT_AVAILABLE_FORMAL_REPORT_PENDING", "evidence": "Bound numerators/denominators, Wilson intervals, failures, usage, cleanup and recomputation; formal test/report not authorized."}]})
    output = package / "reports/derived-score-and-coverage.json"
    output.parent.mkdir(exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)+"\n"
    if output.exists() and output.read_text(encoding="utf8") != text:
        raise ValueError("DERIVED_REPORT_EXISTS_WITH_DIFFERENT_CONTENT_NEW_VERSION_REQUIRED")
    if not output.exists(): output.write_text(text, encoding="utf8")
    return {"path": output.relative_to(ROOT).as_posix(), "sha256": runner.digest(output), "real_runs": n}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("freeze", "authorize", "report"), required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    print(freeze(evidence=args.evidence) if args.stage == "freeze" else authorize() if args.stage == "authorize" else report())
