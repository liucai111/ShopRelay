"""Create R9's one-time authorization only after bound admission is verified."""
from __future__ import annotations
import hashlib, json, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger

PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029"
def sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> None:
    manifest_path = PKG / "batch-manifest-r9-r3.json"
    rules_path = PKG / "locked-rules-r9-r3.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    batch_id = manifest["batch_id"]
    suffix = batch_id[-16:]
    auth_path = PKG / manifest["runtime_paths"]["authorization"]
    budget_path = PKG / manifest["runtime_paths"]["budget"]
    admission_path = PKG / manifest["runtime_paths"]["admission"]
    ledger = TaskQuotaLedger(ROOT / manifest["task_ledger_path"], task_id=manifest["task_id"],
        seed_sha256=manifest["task_ledger_seed_sha256"]).read()
    admission = json.loads((ROOT / admission_path).read_text(encoding="utf-8"))
    current_digest = implementation_digest(ROOT)
    if (auth_path.exists() or not budget_path.is_file() or not admission_path.is_file() or
            manifest.get("status") != "FROZEN" or current_digest != manifest["bindings"]["implementation_digest"] or
            sha(PKG / "application-admission-offline-r3.json") != manifest["application_admission_sha256"] or
            admission.get("admission") != "PASS" or admission.get("run_created") is not False or
            admission.get("provider_requests") != 0 or admission.get("case_count") != 15 or
            admission.get("task_quota_spent", {}).get("repository_preflight_reads") != ledger["spent"]["repository_preflight_reads"] or
            ledger["spent"]["application_runs"] != 1 or ledger["unknown_usage"] or ledger["over_limit"] or
            any(row.get("status") == "ATTEMPT_RESERVED" for row in ledger.get("case_records", {}).values())):
        raise SystemExit("R9 authorization prerequisites failed; no authorization created")
    now = datetime.now(timezone.utc)
    auth = {"schema_version": "m6.2-personal-dev-successor-authorization-r1",
        "authorization_id": "M62-R9-" + uuid.uuid4().hex.upper(), "batch_id": batch_id,
        "task_id": manifest["task_id"], "status": "ACTIVE",
        "authorization_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
        "case_ids": manifest["proposed_order"], "grading_status": "NOT_GRADED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED", "confirmation_source": "CURRENT_USER_INSTRUCTION",
        "confirmed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=8)).isoformat().replace("+00:00", "Z"),
        "manifest_sha256": sha(manifest_path), "rules_sha256": sha(rules_path),
        "code_sha256": current_digest, "git_head_revision": manifest["bindings"]["git_head_revision"],
        "dataset_sha256": manifest["dataset"]["sha256"],
        "database_identity": manifest["bindings"]["database_identity"],
        "model": manifest["bindings"]["model"], "provider_endpoint": manifest["bindings"]["provider_endpoint"],
        "pricing_sha256": manifest["bindings"]["pricing_evidence_sha256"],
        "budget_sha256": sha(budget_path), "application_admission_sha256": sha(admission_path),
        "task_ledger_seed_sha256": manifest["task_ledger_seed_sha256"],
        "limits": manifest["authorized_limits"], "batch_limits": rules["batch_budget_limits"],
        "per_case_limits": rules["per_case_budget_limits"], "automatic_retries": 0,
        "judge_calls": 0, "embedding_calls": 0, "business_writes": 0, "pending_actions": 0,
        "proxy_host": "127.0.0.1", "proxy_port": 7892, "tls_verification": True,
        "no_proxy_bypasses_provider": False}
    with auth_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(auth, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
    print(json.dumps({"status": "AUTHORIZED", "authorization_id": auth["authorization_id"],
        "expires_at_utc": auth["expires_at_utc"], "case_count": len(auth["case_ids"]),
        "manifest_sha256": auth["manifest_sha256"], "code_sha256": auth["code_sha256"],
        "budget_sha256": auth["budget_sha256"], "application_admission_sha256": auth["application_admission_sha256"],
        "task_ledger_seed_sha256": auth["task_ledger_seed_sha256"], "secret_values_emitted": False}, ensure_ascii=False))

if __name__ == "__main__": main()
