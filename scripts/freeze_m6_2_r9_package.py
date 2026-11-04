"""Create the immutable R9 successor-segment inputs from verified R8 evidence."""
from __future__ import annotations
import argparse, hashlib, json, uuid
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import derive_limits
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029"
R8 = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029"
DATA = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
MATRIX = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"
PRICE = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029/official-pricing-evidence-r4.json"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def canon_sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def create(path: Path, value: dict) -> str:
    if path.exists():
        raise SystemExit(f"refusing to overwrite {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
        f.flush()
    return sha(path)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--successor2", action="store_true")
    parser.add_argument("--successor3", action="store_true")
    parser.add_argument("--successor4", action="store_true")
    parsed = parser.parse_args()
    successor2, successor3, successor4 = parsed.successor2, parsed.successor3, parsed.successor4
    if sum((successor2, successor3, successor4)) > 1:
        raise SystemExit("select one successor segment")
    successor = successor2 or successor3 or successor4
    successor_number = 4 if successor4 else 3 if successor3 else 2 if successor2 else None
    prior_manifest_path = R8 / "batch-manifest-r8.json"
    prior_state_path = R8 / "execution-state-r8-029-DE229C353034.json"
    prior_journal_path = R8 / "execution-journal-r8-029-DE229C353034.jsonl"
    prior_closeout_path = R8 / "reports/closeout-r8.json"
    prior_results_path = R8 / "reports/case-results-r8.json"
    prior_auth_path = R8 / "authorization-r8-029-DE229C353034.json"
    prior_files = {"manifest": prior_manifest_path, "state": prior_state_path,
        "journal": prior_journal_path, "closeout": prior_closeout_path,
        "case_results": prior_results_path, "authorization": prior_auth_path}
    hashes = {key: sha(path) for key, path in prior_files.items()}
    prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
    prior_state = json.loads(prior_state_path.read_text(encoding="utf-8"))
    prior_closeout = json.loads(prior_closeout_path.read_text(encoding="utf-8"))
    prior_results = json.loads(prior_results_path.read_text(encoding="utf-8"))
    prior_auth = json.loads(prior_auth_path.read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in prior_journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    r8_ids = prior_manifest["proposed_order"]
    r8_remaining = [case_id for case_id in r8_ids if prior_state["attempt_count"].get(case_id, 0) == 0]
    task_id = "M62-R324-R8-CONTINUATION-20261029"
    task_path = PKG / "task-cumulative-ledger-r1.json"
    raw_task = json.loads(task_path.read_text(encoding="utf-8"))
    from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger
    task_quota = TaskQuotaLedger(task_path, task_id=task_id, seed_sha256=raw_task["seed_sha256"])
    task_state = task_quota.read()
    task_consumed = [case_id for case_id in r8_remaining if task_state.get("attempts", {}).get(case_id, 0) != 0]
    ids = ([case_id for case_id in r8_remaining if task_state.get("attempts", {}).get(case_id, 0) == 0]
        if successor else r8_remaining)
    if (len(r8_remaining) != 15 or (not successor and len(ids) != 15) or
            (successor and (len(ids) != 14 or task_consumed != ["D1-POLICY-018"])) or
            (not successor and (ids != prior_closeout["not_run_case_ids"] or task_consumed)) or
            prior_closeout["attempted_case_ids"] != ["D1-POLICY-017"] or
            prior_auth.get("status") != "CLOSED_STOPPED" or prior_closeout.get("authorization_final_status") != "CLOSED_STOPPED" or
            len(prior_results.get("cases", [])) != 1 or
            any(prior_state["attempt_count"].get(case_id, 0) != 0 for case_id in ids) or
            any(row.get("case_id") in ids and row.get("event") in {"CASE_ATTEMPT_RESERVED", "RUN_CREATED", "CASE_CLOSEOUT", "CASE_EXCEPTION_STOP"} for row in journal)):
        raise SystemExit("R8 remainder/history verification failed")
    price_record = json.loads(PRICE.read_text(encoding="utf-8"))
    r8_run = prior_results["cases"][0]
    r8_budget = prior_closeout["final_budget"]
    task_id = "M62-R324-R8-CONTINUATION-20261029"
    seed_basis = {"task_id": task_id, "source_r8_hashes": hashes,
        "r8_consumed_case_id": "D1-POLICY-017", "r8_run_id": r8_run["run_id"],
        "r8_usage": {"application_runs": 1, "provider_requests": r8_budget["provider_requests"],
            "tokens": r8_budget["tokens"], "local_estimated_usd": r8_budget["estimated_usd"],
            "runtime_seconds": 7, "repository_preflight_reads": 18},
        "authorized_total_limits": {"application_runs": 15, "provider_requests": 600,
            "tokens": 10000000, "local_estimated_usd": 30.0,
            "runtime_seconds": 14400, "repository_preflight_reads": 1000}}
    seed_sha = canon_sha(seed_basis)
    segment_suffix = f"-s{successor_number}-r1" if successor else "-r3"
    rules_path = PKG / f"locked-rules-r9{segment_suffix}.json"
    source_rules = json.loads((R8 / "locked-rules-r8.json").read_text(encoding="utf-8"))
    per_limits, batch_limits = derive_limits(len(ids),
        per_case_overrides={"provider_calls": 40, "total_tokens": 600000, "estimated_usd": 2.0,
            "tool_calls": 20, "max_calls_per_trial": 20, "max_duration_seconds": 900},
        batch_overrides={"provider_calls": 599, "total_tokens": 9999371,
            "estimated_usd": 29.9992452, "tool_calls": 20*len(ids), "max_calls_per_trial": 20*len(ids),
            "max_duration_seconds": 900*len(ids)})
    rules = {"schema_version": f"m6.2-r324-rules-r9{segment_suffix}", "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
        "case_order": ids, "source_matrix_case_ids_sha256": source_rules["source_matrix_case_ids_sha256"],
        "source_rule_basis": {"path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029/locked-rules-r8.json",
            "sha256": sha(R8 / "locked-rules-r8.json"), "same_cases_and_rule_contract": True},
        "continuation_policy": {"version": "m62-r9-evidence-aware-continuation-v1",
            "continue_after": ["quality failure", "incomplete legal refusal", "complete safe model capability rejection", "complete evidenced candidate answer rejection"],
            "stop_on": ["unknown usage", "missing critical audit", "permission violation", "unexpected write", "nonterminal run", "checker failure", "cleanup failure", "database identity mismatch"],
            "task_outcome_is_not_execution_continue_decision": True,
            "route_scope_rejection_must_prove_zero_unauthorized_execution": True},
        "case_rules": {case_id: source_rules["case_rules"][case_id] for case_id in ids},
        "per_case_budget_limits": per_limits.to_dict(), "batch_budget_limits": batch_limits.to_dict(),
        "limits": {"per_case": {"provider_requests": 40, "tokens": 600000, "local_estimated_usd": 2.0,
            "readonly_calls": 20, "runtime_seconds": 900, "automatic_retries": 0,
            "judge": 0, "embedding": 0, "business_writes": 0, "pending_actions": 0},
            "task_total": seed_basis["authorized_total_limits"],
            "remaining_after_prior_segments": {"application_runs": 15-task_state["spent"]["application_runs"],
                "provider_requests": 600-task_state["spent"]["provider_requests"],
                "tokens": 10000000-task_state["spent"]["tokens"],
                "local_estimated_usd": 30.0-task_state["spent"]["local_estimated_usd"],
                "runtime_seconds": 14400-task_state["spent"]["runtime_seconds"],
                "repository_preflight_reads": 1000-task_state["spent"]["repository_preflight_reads"]}},
        "automatic_retries": 0, "judge_calls": 0, "embedding_calls": 0,
        "business_writes": 0, "pending_actions": 0, "protected_assets_allowed": False}
    rules_sha = create(rules_path, rules)
    batch_id = "M62-R324-R9" + (f"-S{successor_number}" if successor else "") + "-CONTINUATION-" + uuid.uuid4().hex[:16].upper()
    _tag = f"r9s{successor_number}" if successor else "r9"
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    bindings = dict(prior_manifest["bindings"])
    bindings.update({"implementation_digest": implementation_digest(ROOT), "git_head_revision": __import__("subprocess").check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "pricing_evidence_sha256": sha(PRICE), "pricing_checked_at_utc": price_record["checked_at_utc"]})
    manifest = {"schema_version": (f"m6.2-r324-personal-dev-exact-subset-manifest-{_tag}" if successor else "m6.2-r324-personal-dev-exact-subset-manifest-r9"),
        "profile_version": (f"m6.2-r324-exact-remainder-profile-r9-successor{successor_number}" if successor else "m6.2-r324-exact-remainder-profile-r9"), "batch_id": batch_id,
        "task_id": task_id, "task_ledger_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r1.json",
        "task_ledger_seed_sha256": seed_sha, "created_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "FROZEN", "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "grading_status": "NOT_GRADED", "review_status": "reference_unreviewed",
        "dataset": prior_manifest["dataset"], "case_universe_count": 40,
        "case_universe_count_for_segment": len(ids), "task_prior_consumed_case_ids": task_consumed,
        "task_ledger_state_sha256_at_freeze": task_state["state_sha256"],
        "case_admission_counts": matrix["admission_counts"], "admission_matrix_path": prior_manifest["admission_matrix_path"],
        "admission_matrix_sha256": sha(MATRIX), "authorized_candidate_case_ids": ids,
        "proposed_order": ids, "r8_unconsumed_case_ids": r8_remaining,
        "locked_rules_path": rules_path.relative_to(ROOT).as_posix(), "locked_rules_sha256": rules_sha,
        "application_admission_filename": (f"application-admission-offline-r{3+successor_number}.json" if successor else "application-admission-offline-r3.json"),
        "application_admission_schema_version": (f"m62-{_tag}-application-admission-offline-r{3+successor_number}" if successor else "m62-r9-application-admission-offline-r3"),
        "application_admission_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/" + (f"application-admission-offline-r{3+successor_number}.json" if successor else "application-admission-offline-r3.json"),
        "application_admission_sha256": sha(PKG / (f"application-admission-offline-r{3+successor_number}.json" if successor else "application-admission-offline-r3.json")),
        "application_admission_coverage": {"candidate_count": len(ids), "application_ready": len(ids), "blocked": 0,
            "all_candidates_processed": True, "external_calls": 0, "intercepted_model_requests_only": True},
        "repository_audit": prior_manifest["repository_audit"], "bindings": bindings,
        "authorized_limits": {"application_runs": 15, "provider_requests": 600, "read_only_calls_per_case": 20,
            "tokens": 10000000, "local_estimated_usd": 30.0, "runtime_seconds": 14400,
            "concurrency": 1, "automatic_retries": 0, "judge": 0, "embedding": 0,
            "business_writes": 0, "pending_actions": 0, "repository_preflight_reads": 1000},
        "budget_contract": {"per_case": per_limits.to_dict(), "batch": batch_limits.to_dict()},
        "runtime_paths": {"authorization": f"authorization-{_tag}-{batch_id[-16:]}.json",
            "budget": f"budget-{_tag}-{batch_id[-16:]}.json", "state": f"execution-state-{_tag}-{batch_id[-16:]}.json",
            "journal": f"execution-journal-{_tag}-{batch_id[-16:]}.jsonl",
            "admission": f"application-admission-{_tag}-{batch_id[-16:]}.json",
            "admission_blocked": f"application-admission-blocked-{_tag}-{batch_id[-16:]}.json"},
        "task_history": {"r8": {f"{key}_path": path.relative_to(ROOT).as_posix() for key, path in prior_files.items()} |
            {f"{key}_sha256": hashes[key] for key in hashes}},
        "r8_history": {f"{key}_path": path.relative_to(ROOT).as_posix() for key, path in prior_files.items()} |
            {f"{key}_sha256": hashes[key] for key in hashes}}
    manifest_path = PKG / (f"batch-manifest-r9-s{successor_number}-r1.json" if successor else "batch-manifest-r9-r3.json")
    manifest_sha = create(manifest_path, manifest)
    remaining_budget = {
        "application_runs": 15-task_state["spent"]["application_runs"],
        "provider_requests": 600-task_state["spent"]["provider_requests"],
        "tokens": 10000000-task_state["spent"]["tokens"],
        "local_estimated_usd": round(30.0-task_state["spent"]["local_estimated_usd"], 9),
        "runtime_seconds": 14400-task_state["spent"]["runtime_seconds"],
        "repository_preflight_reads": 1000-task_state["spent"]["repository_preflight_reads"],
    }
    if min(remaining_budget.values()) < 0:
        raise SystemExit("cumulative task budget already exceeds the authorized ceiling")
    budget = {"schema_version": f"m6.2-r324-{_tag}-budget-r1", "batch_id": batch_id,
        "manifest_sha256": manifest_sha, "rules_sha256": rules_sha,
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1, "max_duration_seconds": 900, "max_retries": 0},
        "batch_limits": manifest["budget_contract"]["batch"],
        "cumulative_remaining_includes_all_prior_segments": remaining_budget,
        "local_estimate_is_not_provider_hard_cap": True}
    create(PKG / f"budget-{_tag}-{batch_id[-16:]}.json", budget)
    ledger = {"schema_version": "m62-task-quota-v1", "task_id": task_id, "seed_sha256": seed_sha,
        "seed_basis": seed_basis, "limits": seed_basis["authorized_total_limits"],
        "spent": seed_basis["r8_usage"],
        "reserved": {"application_runs": 0, "provider_requests": 0, "tokens": 0,
            "local_estimated_usd": 0.0, "runtime_seconds": 0},
        "attempts": {"D1-POLICY-017": 1},
        "case_records": {"D1-POLICY-017": {"attempt": 1, "status": "HISTORICAL_CLOSED_R8",
            "run_id": r8_run["run_id"], "run_created": True, "provider_requests": 1,
            "tokens": r8_budget["tokens"], "local_estimated_usd": r8_budget["estimated_usd"], "elapsed_seconds": 7}},
        "segments": [{"kind": "R8_HISTORICAL_BASELINE", "source_hashes": hashes,
            "case_id": "D1-POLICY-017", "run_id": r8_run["run_id"]}],
        "unknown_usage": False, "over_limit": False}
    # Constructor validates all fields and writes an integrity-protected state.
    from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger
    quota = TaskQuotaLedger(PKG / "task-cumulative-ledger-r1.json", task_id=task_id, seed_sha256=seed_sha)
    if quota.path.exists() and not successor:
        existing = quota.read()
        if (existing.get("seed_basis") != seed_basis or existing.get("spent") != seed_basis["r8_usage"] or
                existing.get("attempts") != {"D1-POLICY-017": 1} or existing.get("segments") != ledger["segments"]):
            raise SystemExit("existing cumulative task ledger has changed; refusing to reset")
    elif not quota.path.exists():
        quota.write(ledger)
    print(json.dumps({"status": "FROZEN_INPUTS_CREATED", "case_count": len(ids),
        "case_ids": ids, "batch_id": batch_id, "implementation_digest": bindings["implementation_digest"],
        "dataset_sha256": sha(DATA), "manifest_sha256": manifest_sha, "rules_sha256": rules_sha,
        "task_id": task_id, "task_ledger_seed_sha256": seed_sha, "r8_consumption_included": seed_basis["r8_usage"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()
