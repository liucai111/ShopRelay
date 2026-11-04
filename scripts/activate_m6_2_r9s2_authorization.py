"""Create the one-time authorization for the verified 14-case R9 successor."""
from __future__ import annotations

import hashlib
import json
import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.m0.config import load_settings
from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger

PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029"
MANIFEST_REL = "batch-manifest-r9-s2-r1.json"
RULES_REL = "locked-rules-r9-s2-r1.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--successor3", action="store_true")
    parser.add_argument("--successor4", action="store_true")
    parsed = parser.parse_args()
    if parsed.successor3 and parsed.successor4:
        raise SystemExit("select one successor version")
    successor3, successor4 = parsed.successor3, parsed.successor4
    successor_number = 4 if successor4 else 3 if successor3 else 2
    manifest_path = PKG / (f"batch-manifest-r9-s{successor_number}-r1.json" if successor_number > 2 else MANIFEST_REL)
    rules_path = PKG / (f"locked-rules-r9-s{successor_number}-r1.json" if successor_number > 2 else RULES_REL)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    batch_id = manifest["batch_id"]
    auth_path = PKG / manifest["runtime_paths"]["authorization"]
    budget_path = PKG / manifest["runtime_paths"]["budget"]
    admission_path = PKG / manifest["runtime_paths"]["admission"]
    state_path = PKG / manifest["runtime_paths"]["state"]
    journal_path = PKG / manifest["runtime_paths"]["journal"]
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    settings = load_settings(require_api_key=True)
    revision = __import__("subprocess").check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    current_digest = implementation_digest(ROOT)
    quota = TaskQuotaLedger(ROOT / manifest["task_ledger_path"], task_id=manifest["task_id"],
        seed_sha256=manifest["task_ledger_seed_sha256"]).read()
    expected_remaining = [case_id for case_id in manifest["r8_unconsumed_case_ids"]
        if quota.get("attempts", {}).get(case_id, 0) == 0]
    prepared_budget = budget.get("cumulative_remaining_includes_all_prior_segments", {})
    current_remaining = {"application_runs": quota["limits"]["application_runs"]-quota["spent"]["application_runs"],
        "provider_requests": quota["limits"]["provider_requests"]-quota["spent"]["provider_requests"],
        "tokens": quota["limits"]["tokens"]-quota["spent"]["tokens"],
        "local_estimated_usd": round(quota["limits"]["local_estimated_usd"]-quota["spent"]["local_estimated_usd"], 9),
        "runtime_seconds": quota["limits"]["runtime_seconds"]-quota["spent"]["runtime_seconds"],
        "repository_preflight_reads": quota["limits"]["repository_preflight_reads"]-quota["spent"]["repository_preflight_reads"]}
    prepared_nonpreflight = {k: v for k, v in prepared_budget.items() if k != "repository_preflight_reads"}
    current_nonpreflight = {k: v for k, v in current_remaining.items() if k != "repository_preflight_reads"}
    checks = {
        "profile": manifest.get("profile_version") == (
            "m6.2-r324-exact-remainder-profile-r9-successor3" if successor3 else
            f"m6.2-r324-exact-remainder-profile-r9-successor{successor_number}"),
        "unique_case_order": len(manifest["proposed_order"]) == 14 and len(set(manifest["proposed_order"])) == 14,
        "remaining_cases_match_ledger": manifest["proposed_order"] == expected_remaining,
        "no_auth_or_execution_state": not any(p.exists() for p in (auth_path, state_path, journal_path)),
        "frozen_bindings": manifest.get("status") == "FROZEN" and
            manifest.get("bindings", {}).get("implementation_digest") == current_digest and
            manifest.get("bindings", {}).get("git_head_revision") == revision,
        "dataset": manifest.get("dataset", {}).get("sha256") ==
            "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969",
        "budget_bindings": budget.get("batch_id") == batch_id and
            budget.get("manifest_sha256") == sha(manifest_path) and
            budget.get("rules_sha256") == sha(rules_path) and
            budget.get("batch_limits") == rules.get("batch_budget_limits") and
            budget.get("per_case_limits") == rules.get("per_case_budget_limits") and
            prepared_nonpreflight == current_nonpreflight and
            prepared_budget.get("repository_preflight_reads", -1) >= current_remaining["repository_preflight_reads"],
        "admission": admission.get("admission") == "PASS" and admission.get("case_count") == 14 and
            admission.get("run_created") is False and admission.get("provider_requests") == 0 and
            admission.get("database_identity") == manifest["bindings"]["database_identity"] and
            admission.get("implementation_digest") == current_digest and
            admission.get("repository_blockers") == [],
        "admission_task_ledger_snapshot": (successor_number < 3 or
            admission.get("task_quota_ledger_state_sha256") == quota["state_sha256"]),
        "task_ledger": quota.get("unknown_usage") is False and quota.get("over_limit") is False and
            not any(r.get("status") == "ATTEMPT_RESERVED" for r in quota.get("case_records", {}).values()) and
            quota["reserved"].get("application_runs", 0) == 0 and
            quota["spent"]["application_runs"] == 1,
        "provider_and_prices": settings.model == manifest["bindings"]["model"] and
            settings.base_url == manifest["bindings"]["provider_endpoint"],
        "proxy_evidence": admission.get("proxy", {}).get("proxy_host") == "127.0.0.1" and
            admission.get("proxy", {}).get("proxy_port") == 7892 and
            admission.get("proxy", {}).get("six_variables_match") is True and
            admission.get("proxy", {}).get("provider_bypass_excluded") is True,
        "remaining_task_ceiling": len(manifest["proposed_order"]) <= current_remaining["application_runs"] and
            sum(current_remaining[k] < 0 for k in current_remaining) == 0,
    }
    if not all(checks.values()):
        raise SystemExit("R9 successor authorization refused: " + ",".join(k for k,v in checks.items() if not v))
    now = datetime.now(timezone.utc)
    auth = {"schema_version": "m6.2-personal-dev-successor-authorization-r2",
        "authorization_id": "M62-R9S2-" + uuid.uuid4().hex.upper(), "batch_id": batch_id,
        "task_id": manifest["task_id"], "status": "ACTIVE",
        "authorization_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "case_ids": manifest["proposed_order"], "grading_status": "NOT_GRADED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED", "confirmation_source": "CURRENT_USER_INSTRUCTION",
        "confirmed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
        "manifest_sha256": sha(manifest_path), "rules_sha256": sha(rules_path),
        "code_sha256": current_digest, "git_head_revision": revision,
        "dataset_sha256": manifest["dataset"]["sha256"],
        "database_identity": manifest["bindings"]["database_identity"],
        "model": settings.model, "provider_endpoint": settings.base_url,
        "pricing_sha256": manifest["bindings"]["pricing_evidence_sha256"],
        "budget_sha256": sha(budget_path), "application_admission_sha256": sha(admission_path),
        "task_ledger_seed_sha256": manifest["task_ledger_seed_sha256"],
        "task_ledger_state_sha256": quota["state_sha256"],
        "limits": manifest["authorized_limits"], "batch_limits": rules["batch_budget_limits"],
        "per_case_limits": rules["per_case_budget_limits"], "automatic_retries": 0,
        "judge_calls": 0, "embedding_calls": 0, "business_writes": 0, "pending_actions": 0,
        "proxy_host": "127.0.0.1", "proxy_port": 7892, "tls_verification": True,
        "no_proxy_bypasses_provider": False, "task_budget_remaining_at_authorization": current_remaining,
        "preauthorization_checks": checks}
    with auth_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(auth, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
    print(json.dumps({"status": "AUTHORIZED", "authorization_id": auth["authorization_id"],
        "case_count": len(auth["case_ids"]), "expires_at_utc": auth["expires_at_utc"],
        "implementation_digest": current_digest, "task_ledger_state_sha256": quota["state_sha256"],
        "secret_values_emitted": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
