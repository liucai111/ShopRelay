"""M6.2 exact-manifest personal Dev batch entry; does not alter the legacy D1 CLI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from app.m0.config import load_settings
from eval.m6_1.personal_dev import D1Coordinator, D1PreflightError, create_budget_ledger, implementation_digest, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_batch_m62 import (DATASET_REL, DurableAttemptJournal,
    TERMINAL_RUNS, TaskQuotaLedger, app_admission, batch_stop_reason_r7, batch_stop_reason_r8, batch_stop_reason_r9, derive_limits, load_exact_profile,
    plan_without_unauthorized_targets, score_archive_r7, score_archive_r8, score_archive_current)
from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger

PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r7-20261029"
AUTH_TYPE = "M62_PERSONAL_DEV_R6_UNCONSUMED_17_ONE_TIME"
PRIOR_R6_PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
PKG = ROOT / PACKAGE_REL
DATA_SHA = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
RUNTIME_STEMS = {"authorization": "authorization", "budget": "budget", "state": "execution-state",
    "journal": "execution-journal", "admission": "application-admission",
    "admission_blocked": "application-admission-blocked"}
_CLI_CONTEXT: dict = {}
PROFILE_CONFIGS = {
    "r7": {"package": PACKAGE_REL, "tag": "r7", "auth_type": AUTH_TYPE,
        "profile_version": "m6.2-r324-exact-remainder-profile-r7-r9", "count": 17,
        "manifest": "batch-manifest-r7-r9.json", "rules": "locked-rules-r7-r9.json"},
    "r8": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029",
        "tag": "r8", "auth_type": "M62_PERSONAL_DEV_R7_UNCONSUMED_16_ONE_TIME",
        "profile_version": "m6.2-r324-exact-remainder-profile-r8", "count": 16,
        "manifest": "batch-manifest-r8.json", "rules": "locked-rules-r8.json"},
    "r9": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9", "auth_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "profile_version": "m6.2-r324-exact-remainder-profile-r9", "count": 15,
        "manifest": "batch-manifest-r9.json", "rules": "locked-rules-r9.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9s2": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9s2", "auth_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "profile_version": "m6.2-r324-exact-remainder-profile-r9-successor2", "count": 14,
        "manifest": "batch-manifest-r9-s2-r1.json", "rules": "locked-rules-r9-s2-r1.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9s3": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9s3", "auth_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "profile_version": "m6.2-r324-exact-remainder-profile-r9-successor3", "count": 14,
        "manifest": "batch-manifest-r9-s3-r1.json", "rules": "locked-rules-r9-s3-r1.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9s4": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9s4", "auth_type": "M62_PERSONAL_DEV_R8_REMAINDER_SUCCESSOR_SEGMENT",
        "profile_version": "m6.2-r324-exact-remainder-profile-r9-successor4", "count": 14,
        "manifest": "batch-manifest-r9-s4-r1.json", "rules": "locked-rules-r9-s4-r1.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9p018": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9p018", "auth_type": "M62_PERSONAL_DEV_R9_POLICY018_ONE_TIME_RECOVERY",
        "profile_version": "m6.2-r324-policy018-first-run-recovery-r1", "count": 1,
        "manifest": "batch-manifest-policy018-recovery-r1.json",
        "rules": "locked-rules-policy018-recovery-r1.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9p018r2": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9p018r2", "auth_type": "M62_PERSONAL_DEV_R9_POLICY018_ONE_TIME_RECOVERY",
        "profile_version": "m6.2-r324-policy018-first-run-recovery-r2", "count": 1,
        "manifest": "batch-manifest-policy018-recovery-r2.json",
        "rules": "locked-rules-policy018-recovery-r2.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r9p018r3": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029",
        "tag": "r9p018r3", "auth_type": "M62_PERSONAL_DEV_R9_POLICY018_ONE_TIME_RECOVERY",
        "profile_version": "m6.2-r324-policy018-first-run-recovery-r3", "count": 1,
        "manifest": "batch-manifest-policy018-recovery-r3.json",
        "rules": "locked-rules-policy018-recovery-r3.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 20, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r10-four-first-runs": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030",
        "tag": "r10", "auth_type": "M62_PERSONAL_DEV_R9_FOUR_FIRST_RUNS_ONE_TIME",
        "profile_version": "m6.2-r324-four-first-runs-r1", "count": 4,
        "manifest": "batch-manifest-r10-first-runs-r1.json",
        "rules": "locked-rules-r10-first-runs-r1.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 60, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
    "r10-four-first-runs-r2": {"package": "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030",
        "tag": "r10r2", "auth_type": "M62_PERSONAL_DEV_R9_FOUR_FIRST_RUNS_ONE_TIME",
        "profile_version": "m6.2-r324-four-first-runs-r2", "count": 4,
        "manifest": "batch-manifest-r10-first-runs-r2.json",
        "rules": "locked-rules-r10-first-runs-r2.json",
        "per_case_limits": {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
            "max_calls_per_trial": 60, "max_concurrent_trials": 1,
            "max_duration_seconds": 900, "max_retries": 0}},
}
_ACTIVE_PROFILE = PROFILE_CONFIGS["r7"]

# A separate, exact approved Dev revision. Legacy profiles retain their inputs.
R325_PACKAGE = "docs/eval/m6.2-r325-seven-first-runs-20261031"
R325_CASES = ["D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022", "D1-POLICY-015",
    "D1-POLICY-016", "D1-CROSSAGENT-029", "D1-AFTERSALES-024"]
PROFILE_CONFIGS["r325-seven"] = {
    "package": R325_PACKAGE, "tag": "r325", "auth_type": "M62_PERSONAL_DEV_R325_SEVEN_FIRST_RUNS",
    "profile_version": "m62-r325-seven-first-runs-v1", "count": 7,
    "manifest": "manifest.json", "rules": "locked-rules.json", "cumulative": True,
    "dataset_rel": "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.5.json",
    "dataset_sha256": "c872fd89824a9b66f92e4f3526e885b84421edc7de1337d3952c675edac7e6a9",
    "matrix_rel": R325_PACKAGE + "/admission-matrix.json",
    "per_case_limits": PROFILE_CONFIGS["r10-four-first-runs-r2"]["per_case_limits"],
}


def validate_r325_bindings(root: Path, manifest: dict) -> dict:
    """Recheck approved sources and mutable runtime bytes before admission/claim."""
    from eval.m6_2.confirmed_dev_preparation import OUT, EVENT_ID
    if manifest["proposed_order"] != R325_CASES or manifest["authorized_candidate_case_ids"] != R325_CASES:
        raise D1PreflightError("M62_R325_EXACT_CASE_ORDER_MISMATCH")
    expected_sources = {
        (OUT / "confirmation-event-r1.json").as_posix(): "d8391c6fddef0521967e06089cece0d96e345053f325ecf20fdd9845420a5f93",
        (OUT / "confirmed-dispositions-r1.json").as_posix(): "5c99097097644d75e06898e17c5b0c0cecf0e110006102020d1850df19c12d37",
    }
    if manifest.get("approved_sources") != expected_sources or any(
            digest(root / path) != sha for path, sha in expected_sources.items()):
        raise D1PreflightError("M62_R325_APPROVAL_SOURCE_BINDING_MISMATCH")
    event = json.loads((root / OUT / "confirmation-event-r1.json").read_text(encoding="utf8"))
    if event["event_id"] != EVENT_ID:
        raise D1PreflightError("M62_R325_CONFIRMATION_EVENT_MISMATCH")
    dependencies = manifest["runtime_dependencies"]
    if set(dependencies) != {"app/m2/self_rag.py", "eval/m6_2/confirmed_dev_preparation.py",
                             "scripts/prepare_m6_2_r325_seven.py"} or any(
            digest(root / path) != sha for path, sha in dependencies.items()):
        raise D1PreflightError("M62_R325_RUNTIME_DEPENDENCY_MISMATCH")
    previous = root / "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030/task-cumulative-ledger-r3-four-first-runs.json"
    if digest(previous) != manifest["previous_task_ledger_sha256"]:
        raise D1PreflightError("M62_R325_PREVIOUS_LEDGER_CHANGED")
    prior = json.loads(previous.read_text(encoding="utf8"))
    if prior["spent"]["application_runs"] != 20 or any(prior["reserved"].values()) or any(
            case in prior["attempts"] or case in prior["case_records"] for case in R325_CASES):
        raise D1PreflightError("M62_R325_FIRST_RUN_ELIGIBILITY_INVALID")
    evidence_path = root / manifest["application_admission_path"]
    if evidence_path.parent != root / _ACTIVE_PROFILE["package"] or digest(evidence_path) != manifest["application_admission_sha256"]:
        raise D1PreflightError("M62_R325_OFFLINE_EVIDENCE_BINDING_INVALID")
    evidence = json.loads(evidence_path.read_text(encoding="utf8"))
    if (evidence["case_ids"] != R325_CASES or [r["case_id"] for r in evidence["records"]] != R325_CASES or
            evidence["implementation_digest"] != implementation_digest(root) or
            evidence["external_requests"] != 0 or len(evidence["records"]) != 7):
        raise D1PreflightError("M62_R325_OFFLINE_APPLICATION_SCOPE_INVALID")
    for row in evidence["records"]:
        if (row["status"] != "APPLICATION_READY" or row["cleanup"]["cleanup_status"] != "CLEANED_AND_RELEASED" or
                digest(root / row["archive_path"]) != row["archive_sha256"]):
            raise D1PreflightError("M62_R325_OFFLINE_ARCHIVE_OR_CLEANUP_INVALID")
    return {"case_count": 7, "application_ready_count": 7, "previous_shared_runs": 20,
            "confirmation_event_id": EVENT_ID, "new_case_attempts_zero_in_previous_ledger": True}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_path(manifest: dict, key: str, parent: Path) -> Path:
    declared = manifest.get("runtime_paths")
    batch_id = manifest.get("batch_id")
    if (not isinstance(declared, dict) or key not in RUNTIME_STEMS or
            not isinstance(batch_id, str) or len(batch_id) < 16 or
            not all(ch.isalnum() or ch == "-" for ch in batch_id)):
        raise D1PreflightError("M62_R7_RUNTIME_PATH_BINDING_INVALID")
    suffix = batch_id[-16:]
    extension = ".jsonl" if key == "journal" else ".json"
    expected = f"{RUNTIME_STEMS[key]}-{_ACTIVE_PROFILE['tag']}-{suffix}{extension}"
    if declared.get(key) != expected:
        raise D1PreflightError("M62_R7_RUNTIME_PATH_BINDING_INVALID")
    return parent / expected


def task_quota_ledger_rel(profile: str, package_rel: str) -> str:
    if profile == "r325-seven":
        return f"{package_rel}/task-cumulative-ledger.json"
    if profile in {"r10-four-first-runs", "r10-four-first-runs-r2"}:
        return f"{package_rel}/task-cumulative-ledger-r3-four-first-runs.json"
    if profile in {"r9p018", "r9p018r2", "r9p018r3"}:
        return "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r2-policy018-recovery.json"
    return f"{package_rel}/task-cumulative-ledger-r1.json"


def ensure_proxy_configuration() -> dict:
    expected = "http://127.0.0.1:7892"
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    for name in names:
        os.environ[name] = expected
    no_proxy = ",".join((os.environ.get("NO_PROXY", ""), os.environ.get("no_proxy", "")))
    exclusions = []
    for part in no_proxy.split(","):
        token = part.strip().lower().removeprefix("*." ).rstrip(".")
        host = token.rsplit(":", 1)[0] if token.count(":") == 1 else token
        exclusions.append((token, host))
    if any(token == "*" or host == "api.deepseek.com" or host == "deepseek.com" or
           host.endswith(".deepseek.com") for token, host in exclusions):
        raise D1PreflightError("M62_PROVIDER_HOST_EXCLUDED_BY_NO_PROXY")
    import httpx
    selected = httpx._utils.get_environment_proxies().get("https://")
    if selected != expected:
        raise D1PreflightError("M62_HTTPX_PROXY_SELECTION_MISMATCH")
    return {"proxy_host": "127.0.0.1", "proxy_port": 7892,
        "six_variables_match": all(os.environ.get(name) == expected for name in names),
        "provider_bypass_excluded": True, "httpx_https_proxy": "http://127.0.0.1:7892"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _atomic_create_json(path: Path, value: dict) -> str:
    """Create one immutable JSON artifact atomically and return its verified SHA-256."""
    temp = path.with_name(path.name + ".tmp")
    if path.exists() or temp.exists():
        raise D1PreflightError("M62_REPORT_OUTPUT_ALREADY_EXISTS")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        # The only removable file here is the uniquely named temp created above.
        if temp.is_file():
            temp.unlink()
        raise
    actual = digest(path)
    json.loads(path.read_text(encoding="utf-8"))
    return actual


def _cli_failure_projection(exc: Exception) -> dict:
    """Report persistent execution facts after an error without trusting memory state."""
    state_path = _CLI_CONTEXT.get("state_path")
    journal_path = _CLI_CONTEXT.get("journal_path")
    auth_path = _CLI_CONTEXT.get("auth_path")
    state = {}
    rows = []
    auth = {}
    for path, target in ((state_path, state), (auth_path, auth)):
        try:
            if path and Path(path).is_file():
                target.update(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            target["read_status"] = "UNKNOWN"
    try:
        if journal_path and Path(journal_path).is_file():
            rows = [json.loads(line) for line in Path(journal_path).read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    except (OSError, json.JSONDecodeError):
        rows = []
    run_ids = sorted({row.get("run_id") for row in rows
        if isinstance(row.get("run_id"), str) and row.get("event") in {
            "RUN_CREATED", "CASE_CLOSEOUT", "CASE_EXCEPTION_STOP"}})
    for row in rows:
        captured = row.get("captured")
        if isinstance(captured, dict) and isinstance(captured.get("run_id"), str):
            run_ids.append(captured["run_id"])
    run_ids = sorted(set(run_ids))
    missing = None
    if isinstance(exc, FileNotFoundError):
        filename = getattr(exc, "filename", None)
        try:
            candidate = Path(filename).resolve(strict=False) if filename else None
            if candidate and candidate.is_relative_to(ROOT.resolve()):
                missing = candidate.relative_to(ROOT.resolve()).as_posix()
        except (OSError, ValueError, TypeError):
            pass
    return {"status": "BLOCKED_OR_STOPPED", "reason_code": (
            str(exc)[:100] if isinstance(exc, D1PreflightError) else "UNEXPECTED_ERROR_REDACTED"),
        "error_type": type(exc).__name__, "missing_repository_relative_path": missing,
        "undefined_name": (getattr(exc, "name", None) if isinstance(exc, NameError) and
            isinstance(getattr(exc, "name", None), str) and getattr(exc, "name").isidentifier() else None),
        "agent_started": bool(state.get("agent_started")),
        "run_created": bool(state.get("run_created") or run_ids),
        "run_ids": run_ids, "attempt_count": state.get("attempt_count"),
        "authorization_status": auth.get("status", "UNKNOWN"),
        "cleanup_status": state.get("cleanup_status", "UNKNOWN"),
        "journal_events": [row.get("event") for row in rows]}


def _assert_idle_personal_database(factory, target) -> dict:
    from sqlalchemy import inspect, text
    with factory() as db:
        target.check_session(db)
        checks = {}
        tables = set(inspect(db.get_bind()).get_table_names())
        for table in ("users", "orders", "shipments", "runs", "controlled_pending_actions", "d1_fixture_facts"):
            if table not in tables:
                raise D1PreflightError(f"M62_REQUIRED_TABLE_MISSING:{table}")
            checks[table] = db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        checks["nonterminal_runs"] = db.execute(text(
            "SELECT COUNT(*) FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','MODEL_UNAVAILABLE')"
        )).scalar_one()
        marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        checks["active_lease"] = marker.lease_id is not None
        checks["active_receipt"] = marker.receipt is not None
        if any(isinstance(v, int) and v != 0 for v in checks.values()) or checks["active_lease"] or checks["active_receipt"]:
            raise D1PreflightError("M62_PERSONAL_DATABASE_NOT_IDLE")
        schema = db.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'")).scalar_one_or_none()
        if not schema or "policy_retrieval_started" not in schema or "policy_retrieval_finished" not in schema:
            raise D1PreflightError("M62_POLICY_RETRIEVAL_EVENT_SCHEMA_NOT_READY")
    return {"identity": target.identity, "empty_scoped_tables": checks,
        "policy_event_schema": "PRESENT"}


def _validate_price_evidence(root: Path, manifest: dict, *, model: str, endpoint: str) -> tuple[dict, str]:
    from datetime import timedelta
    from eval.m6_1.personal_dev_authorization import load_price_evidence
    path = root / manifest["bindings"]["pricing_evidence_path"]
    record, sha = load_price_evidence(path, model=model, endpoint=endpoint)
    checked = datetime.fromisoformat(record["checked_at_utc"].replace("Z", "+00:00"))
    age = _utc_now() - checked
    if age < timedelta(0) or age > timedelta(hours=24):
        raise D1PreflightError("M62_PRICING_EVIDENCE_STALE")
    return record, sha


def validate_application_admission_binding(admission: dict, *, case_ids: list[str],
        manifest_sha256: str, rules_sha256: str, code_sha256: str,
        dataset_sha256: str, database_identity: str, pricing_sha256: str) -> None:
    """Fail closed unless this exact no-model preflight is bound to this batch."""
    if (admission.get("admission") != "PASS" or admission.get("case_ids") != case_ids or
            admission.get("dataset_sha256") != dataset_sha256 or
            admission.get("implementation_digest") != code_sha256 or
            admission.get("database_identity") != database_identity or
            admission.get("manifest_sha256") != manifest_sha256 or
            admission.get("rules_sha256") != rules_sha256 or
            admission.get("pricing_sha256") != pricing_sha256 or
            admission.get("run_created") is not False or admission.get("provider_requests") != 0):
        raise D1PreflightError("M62_APPLICATION_ADMISSION_BINDING_MISMATCH")


def validate_r7_offline_application_evidence(root: Path, manifest: dict, current_digest: str) -> dict:
    """Validate every R6-unconsumed case through the isolated actual app chain."""
    path = root / manifest.get("application_admission_path", "")
    expected = (root / PACKAGE_REL / "application-admission-offline-r11.json").resolve(strict=True)
    if path.resolve(strict=True) != expected or digest(path) != manifest.get("application_admission_sha256"):
        raise D1PreflightError("M62_R7_OFFLINE_APPLICATION_ADMISSION_PATH_OR_HASH_INVALID")
    report = json.loads(path.read_text(encoding="utf-8"))
    expected_ids = manifest.get("r6_verified_unconsumed_case_ids")
    rows = report.get("records")
    if (report.get("schema_version") != "m62-r7-application-admission-offline-r1" or
            report.get("case_count") != len(expected_ids or []) or report.get("external_calls") != 0 or
            report.get("formal_run_created") is not False or
            report.get("implementation_digest") != current_digest or
            report.get("dataset_sha256") != DATA_SHA or
            [row.get("case_id") for row in rows or []] != expected_ids or len(rows or []) != 17):
        raise D1PreflightError("M62_R7_OFFLINE_APPLICATION_ADMISSION_SCOPE_OR_BINDING_INVALID")
    for row in rows:
        archive = (root / row.get("archive_path", "")).resolve(strict=True)
        if (not archive.is_relative_to(root.resolve()) or digest(archive) != row.get("archive_sha256") or
                not isinstance(row.get("cleanup"), dict) or
                row["cleanup"].get("cleanup_status") != "CLEANED_AND_RELEASED" or
                row["cleanup"].get("residual_case_rows") != 0 or
                row.get("application_admission") not in {"APPLICATION_READY", "BLOCKED_ENGINEERING"}):
            raise D1PreflightError("M62_R7_OFFLINE_APPLICATION_ARCHIVE_OR_CLEANUP_INVALID")
    by_id = {row["case_id"]: row for row in rows}
    first = by_id.get(manifest["proposed_order"][0])
    if (not first or first.get("route", {}).get("decision_source") != "LLM" or
            int(first.get("intercepted_provider_requests", 0)) < 1 or
            first.get("application_admission") != "APPLICATION_READY"):
        raise D1PreflightError("M62_R7_FIRST_CASE_NOT_VERIFIED_MODEL_REQUIRED_PATH")
    ready = {row["case_id"] for row in rows if row.get("application_admission") == "APPLICATION_READY"}
    if (not isinstance(manifest.get("proposed_order"), list) or
            len(manifest["proposed_order"]) != len(set(manifest["proposed_order"])) or
            set(manifest["proposed_order"]) != ready):
        raise D1PreflightError("M62_R7_CASE_ORDER_NOT_EQUAL_APPLICATION_READY_INTERSECTION")
    return report


def validate_r6_unconsumed_cases(root: Path, manifest: dict) -> dict:
    """Re-derive R6-unconsumed IDs from its frozen state, journal and closeout."""
    source = manifest.get("r6_history", {})
    expected_paths = {
        "manifest_path": f"{PRIOR_R6_PACKAGE_REL}/batch-manifest-r6.json",
        "execution_state_path": f"{PRIOR_R6_PACKAGE_REL}/execution-state-r6.json",
        "execution_journal_path": f"{PRIOR_R6_PACKAGE_REL}/execution-journal-r6.jsonl",
        "closeout_path": f"{PRIOR_R6_PACKAGE_REL}/reports/closeout-r6-r3.json",
        "case_results_path": f"{PRIOR_R6_PACKAGE_REL}/reports/case-results-r6-r3.json",
    }
    content = {}
    for field, exact_path in expected_paths.items():
        if source.get(field) != exact_path:
            raise D1PreflightError("M62_R6_SOURCE_PATH_NOT_FIXED")
        path = (root / exact_path).resolve(strict=True)
        expected_sha = source.get(field.replace("_path", "_sha256"))
        if not isinstance(expected_sha, str) or digest(path) != expected_sha:
            raise D1PreflightError("M62_R6_SOURCE_HASH_MISMATCH")
        content[field] = path
    closeout = json.loads(content["closeout_path"].read_text(encoding="utf-8"))
    r6_manifest = json.loads(content["manifest_path"].read_text(encoding="utf-8"))
    batch_id = source.get("batch_id")
    if (closeout.get("authorization_final_status") != "CLOSED_STOPPED" or
            closeout.get("batch_id") != batch_id or r6_manifest.get("batch_id") != batch_id):
        raise D1PreflightError("M62_R7_R6_SOURCE_NOT_CLOSED_STOPPED_OR_BATCH_MISMATCH")
    state = json.loads(content["execution_state_path"].read_text(encoding="utf-8"))
    journal_rows = []
    for line in content["execution_journal_path"].read_text(encoding="utf-8").splitlines():
        journal_rows.append(json.loads(line))
    consumed = {case_id for case_id, count in state.get("attempt_count", {}).items() if count != 0}
    exact = [case_id for case_id in r6_manifest.get("proposed_order", []) if case_id not in consumed]
    requested = manifest.get("r6_verified_unconsumed_case_ids")
    reserved = {row.get("case_id") for row in journal_rows if row.get("event") == "CASE_ATTEMPT_RESERVED"}
    closed = {row.get("case_id") for row in journal_rows if row.get("event") == "CASE_CLOSEOUT"}
    run_ids = {row.get("run_id") for row in journal_rows if row.get("event") == "CASE_CLOSEOUT" and row.get("run_id")}
    archive_result = json.loads(content["case_results_path"].read_text(encoding="utf-8"))
    archived_cases = {row.get("case_id") for row in archive_result.get("cases", [])}
    if (len(exact) != 17 or exact != requested or
            set(exact) & (reserved | closed) or set(exact) & archived_cases or
            any(state.get("attempt_count", {}).get(case_id) != 0 or
                state.get("case_state", {}).get(case_id) != "NOT_RUN" for case_id in exact)):
        raise D1PreflightError("M62_R7_R6_UNCONSUMED_SCOPE_OR_ATTEMPT_MISMATCH")
    return {"verified_unconsumed_case_ids": exact, "r6_consumed_case_ids": sorted(consumed),
        "r6_run_ids": sorted(run_ids), "attempt_count_zero": True, "r6_authorization_closed": True}


def validate_r7_unconsumed_cases(root: Path, manifest: dict) -> dict:
    """Re-derive R7's exact unconsumed set from its frozen files and closed auth."""
    package = "docs/eval/m6.2-r324-40case-personal-dev-batch-r7-20261029"
    declared = manifest.get("r7_history")
    expected = {
        "manifest_path": f"{package}/batch-manifest-r7-r10.json",
        "execution_state_path": f"{package}/execution-state-r7-3976d51e27094e8c.json",
        "execution_journal_path": f"{package}/execution-journal-r7-3976d51e27094e8c.jsonl",
        "closeout_path": f"{package}/reports/closeout-r7-reconciled-r1.json",
        "case_results_path": f"{package}/reports/case-results-r7-reconciled-r1.json",
        "authorization_path": f"{package}/authorization-r7-3976d51e27094e8c.json",
    }
    content = {}
    for field, path_text in expected.items():
        if not isinstance(declared, dict) or declared.get(field) != path_text:
            raise D1PreflightError("M62_R8_R7_HISTORY_PATH_NOT_FIXED")
        path = (root / path_text).resolve(strict=True)
        if not path.is_relative_to(root.resolve()):
            raise D1PreflightError("M62_R8_R7_HISTORY_PATH_OUTSIDE_PROJECT")
        sha_field = field.replace("_path", "_sha256")
        if digest(path) != declared.get(sha_field):
            raise D1PreflightError("M62_R8_R7_HISTORY_HASH_MISMATCH")
        content[field] = path
    prior_manifest = json.loads(content["manifest_path"].read_text(encoding="utf-8"))
    state = json.loads(content["execution_state_path"].read_text(encoding="utf-8"))
    journal_rows = [json.loads(line) for line in content["execution_journal_path"].read_text(
        encoding="utf-8").splitlines() if line.strip()]
    closeout = json.loads(content["closeout_path"].read_text(encoding="utf-8"))
    results = json.loads(content["case_results_path"].read_text(encoding="utf-8"))
    auth = json.loads(content["authorization_path"].read_text(encoding="utf-8"))
    attempts = state.get("attempt_count", {})
    exact = [case_id for case_id in prior_manifest.get("proposed_order", []) if attempts.get(case_id) == 0]
    reserved = {row.get("case_id") for row in journal_rows if row.get("event") == "CASE_ATTEMPT_RESERVED"}
    closed = {row.get("case_id") for row in journal_rows if row.get("event") in {
        "CASE_CLOSEOUT", "CASE_EXCEPTION_STOP"}}
    result_ids = {row.get("case_id") for row in results.get("cases", []) if isinstance(row, dict)}
    requested = manifest.get("r7_verified_unconsumed_case_ids")
    if (closeout.get("batch_id") != prior_manifest.get("batch_id") or
            closeout.get("authorization_status") != "CLOSED_STOPPED" or
            auth.get("status") != "CLOSED_STOPPED" or auth.get("authorization_id") !=
            prior_manifest.get("one_time_authorization_id") or len(exact) != 16 or exact != requested or
            manifest.get("proposed_order") != exact or set(exact) & (reserved | closed | result_ids) or
            any(attempts.get(case_id) != 0 or state.get("case_state", {}).get(case_id) != "NOT_RUN"
                for case_id in exact)):
        raise D1PreflightError("M62_R8_R7_UNCONSUMED_SCOPE_OR_ATTEMPT_MISMATCH")
    return {"verified_unconsumed_case_ids": exact,
        "r7_consumed_case_ids": [case_id for case_id in prior_manifest.get("proposed_order", [])
                                  if attempts.get(case_id, 0) != 0],
        "r7_run_ids": sorted({row.get("run_id") for row in journal_rows if row.get("run_id")}),
        "attempt_count_zero": True, "r7_authorization_closed": True}


def validate_r8_offline_application_evidence(root: Path, manifest: dict, current_digest: str) -> dict:
    path_text = manifest.get("application_admission_path")
    exact_path = (root / PACKAGE_REL / "application-admission-offline-r1.json").resolve(strict=True)
    path = (root / path_text).resolve(strict=True)
    if path != exact_path or digest(path) != manifest.get("application_admission_sha256"):
        raise D1PreflightError("M62_R8_OFFLINE_APPLICATION_ADMISSION_PATH_OR_HASH_INVALID")
    report = json.loads(path.read_text(encoding="utf-8"))
    case_ids = manifest.get("proposed_order")
    rows = report.get("records")
    if (report.get("schema_version") != "m62-r8-application-admission-offline-r1" or
            report.get("case_ids") != case_ids or report.get("case_count") != 16 or
            report.get("external_calls") != 0 or report.get("run_created") is not False or
            report.get("implementation_digest") != current_digest or report.get("dataset_sha256") != DATA_SHA or
            [row.get("case_id") for row in rows or []] != case_ids or len(rows or []) != 16):
        raise D1PreflightError("M62_R8_OFFLINE_APPLICATION_ADMISSION_SCOPE_OR_BINDING_INVALID")
    for row in rows:
        archive = (root / row.get("archive_path", "")).resolve(strict=True)
        if (not archive.is_relative_to(root.resolve()) or digest(archive) != row.get("archive_sha256") or
                row.get("application_admission") not in {"APPLICATION_READY", "BLOCKED_ENGINEERING"} or
                row.get("cleanup", {}).get("cleanup_status") != "CLEANED_AND_RELEASED" or
                row.get("cleanup", {}).get("residual_case_rows") != 0):
            raise D1PreflightError("M62_R8_OFFLINE_APPLICATION_ARCHIVE_OR_CLEANUP_INVALID")
    first = rows[0]
    if first.get("application_admission") != "APPLICATION_READY" or int(
            first.get("intercepted_provider_requests", 0)) < 1:
        raise D1PreflightError("M62_R8_FIRST_CASE_NOT_MODEL_REQUIRED_PATH")
    ready_ids = [row["case_id"] for row in rows if row.get("application_admission") == "APPLICATION_READY"]
    if ready_ids != case_ids:
        raise D1PreflightError("M62_R8_ALL_CASES_NOT_APPLICATION_READY")
    return report


def validate_r8_remaining_cases(root: Path, manifest: dict) -> dict:
    """Derive R8's unconsumed tail from immutable state/journal and closed authorization."""
    package = "docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029"
    declared = manifest.get("r8_history")
    exact_paths = {
        "manifest": f"{package}/batch-manifest-r8.json",
        "state": f"{package}/execution-state-r8-029-DE229C353034.json",
        "journal": f"{package}/execution-journal-r8-029-DE229C353034.jsonl",
        "closeout": f"{package}/reports/closeout-r8.json",
        "case_results": f"{package}/reports/case-results-r8.json",
        "authorization": f"{package}/authorization-r8-029-DE229C353034.json",
    }
    files = {}
    if not isinstance(declared, dict):
        raise D1PreflightError("M62_R9_R8_HISTORY_BINDING_MISSING")
    for key, rel in exact_paths.items():
        path = (root / rel).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or declared.get(f"{key}_path") != rel:
            raise D1PreflightError("M62_R9_R8_HISTORY_PATH_INVALID")
        if digest(path) != declared.get(f"{key}_sha256"):
            raise D1PreflightError("M62_R9_R8_HISTORY_HASH_MISMATCH")
        files[key] = path
    prior_manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    state = json.loads(files["state"].read_text(encoding="utf-8"))
    closeout = json.loads(files["closeout"].read_text(encoding="utf-8"))
    results = json.loads(files["case_results"].read_text(encoding="utf-8"))
    auth = json.loads(files["authorization"].read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in files["journal"].read_text(encoding="utf-8").splitlines() if line.strip()]
    r8_ids = prior_manifest.get("proposed_order", [])
    attempts = state.get("attempt_count", {})
    consumed = [cid for cid in r8_ids if attempts.get(cid, 0) != 0]
    unconsumed = [cid for cid in r8_ids if attempts.get(cid, 0) == 0]
    reserved = {row.get("case_id") for row in journal if row.get("event") == "CASE_ATTEMPT_RESERVED"}
    closed = {row.get("case_id") for row in journal if row.get("event") in {"CASE_CLOSEOUT", "CASE_EXCEPTION_STOP"}}
    result_ids = [row.get("case_id") for row in results.get("cases", []) if isinstance(row, dict)]
    r8_run_ids = {row.get("run_id") for row in journal if row.get("event") == "RUN_CREATED" and row.get("run_id")}
    if (len(r8_ids) != 16 or consumed != ["D1-POLICY-017"] or len(unconsumed) != 15 or
            closeout.get("not_run_case_ids") != unconsumed or
            manifest.get("r8_unconsumed_case_ids") != unconsumed or
            closeout.get("authorization_final_status") != "CLOSED_STOPPED" or
            auth.get("status") != "CLOSED_STOPPED" or auth.get("authorization_id") != closeout.get("authorization_id") or
            result_ids != consumed or set(unconsumed) & (reserved | closed | set(result_ids)) or
            any(attempts.get(cid) != 0 or state.get("case_state", {}).get(cid) != "NOT_RUN" for cid in unconsumed) or
            len(r8_run_ids) != 1 or r8_run_ids != {"b9e4d91b-f378-40c6-bd46-34c4fc98b2cc"}):
        raise D1PreflightError("M62_R9_R8_REMAINDER_OR_ATTEMPT_BINDING_INVALID")
    return {"source_batch_id": prior_manifest.get("batch_id"), "verified_consumed_case_ids": consumed,
        "verified_unconsumed_case_ids": unconsumed, "r8_run_ids": sorted(r8_run_ids),
        "attempt_count_zero_for_remainder": True, "authorization_closed": True}


def validate_r9_offline_application_evidence(root: Path, manifest: dict, current_digest: str) -> dict:
    path = (root / manifest.get("application_admission_path", "")).resolve(strict=True)
    expected = (root / PACKAGE_REL / manifest.get("application_admission_filename", "")).resolve(strict=True)
    if path != expected or digest(path) != manifest.get("application_admission_sha256"):
        raise D1PreflightError("M62_R9_OFFLINE_APPLICATION_EVIDENCE_HASH_INVALID")
    report = json.loads(path.read_text(encoding="utf-8"))
    ids = manifest.get("proposed_order")
    rows = report.get("records")
    if (report.get("schema_version") != manifest.get("application_admission_schema_version") or
            report.get("case_ids") != ids or report.get("case_count") != len(ids or []) or
            report.get("external_calls") != 0 or report.get("formal_run_created") is not False or
            report.get("implementation_digest") != current_digest or report.get("dataset_sha256") != DATA_SHA or
            [row.get("case_id") for row in rows or []] != ids or len(rows or []) != len(ids or [])):
        raise D1PreflightError("M62_R9_OFFLINE_APPLICATION_SCOPE_OR_BINDING_INVALID")
    for row in rows:
        archive = (root / row.get("archive_path", "")).resolve(strict=True)
        if (not archive.is_relative_to(root.resolve()) or digest(archive) != row.get("archive_sha256") or
                row.get("application_admission") != "APPLICATION_READY" or
                row.get("cleanup", {}).get("cleanup_status") != "CLEANED_AND_RELEASED" or
                row.get("cleanup", {}).get("residual_case_rows") != 0):
            raise D1PreflightError("M62_R9_OFFLINE_APPLICATION_ARCHIVE_OR_CLEANUP_INVALID")
    return report


def validate_policy018_offline_application_evidence(root: Path, manifest: dict,
                                                     current_digest: str) -> dict:
    """Require a current-digest, isolated real-app intercept run for this one case."""
    path = (root / manifest.get("application_admission_path", "")).resolve(strict=True)
    allowed = {(root / PACKAGE_REL / f"application-admission-offline-policy018-recovery-r{i}.json").resolve()
        for i in (2, 3, 4)}
    if path not in allowed or digest(path) != manifest.get("application_admission_sha256"):
        raise D1PreflightError("M62_POLICY018_OFFLINE_APPLICATION_BINDING_INVALID")
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = report.get("records")
    if (report.get("schema_version") != "m62-policy018-recovery-offline-application-v1" or
            report.get("case_ids") != ["D1-POLICY-018"] or report.get("case_count") != 1 or
            report.get("implementation_digest") != current_digest or
            report.get("dataset_sha256") != DATA_SHA or report.get("external_calls") != 0 or
            report.get("formal_run_created") is not False or not isinstance(rows, list) or len(rows) != 1):
        raise D1PreflightError("M62_POLICY018_OFFLINE_APPLICATION_SCOPE_OR_BINDING_INVALID")
    row = rows[0]
    archive = (root / row.get("archive_path", "")).resolve(strict=True)
    if (row.get("case_id") != "D1-POLICY-018" or row.get("application_admission") != "APPLICATION_READY" or
            not archive.is_relative_to((root / "data/eval/offline-tests").resolve()) or
            digest(archive) != row.get("archive_sha256") or row.get("run_status") not in TERMINAL_RUNS or
            row.get("route", {}).get("strategy") != "SINGLE" or
            row.get("route", {}).get("sub_strategy") != "BM25" or
            row.get("route", {}).get("required_capabilities") != ["search_knowledge"] or
            row.get("intercepted_provider_requests", 0) < 1 or
            row.get("program_score", {}).get("program_checks", {}).get("policy_retrieval_audit") != "PASS" or
            not row.get("program_score", {}).get("policy_retrieval_call_ids") or
            row.get("cleanup", {}).get("cleanup_status") != "CLEANED_AND_RELEASED" or
            row.get("cleanup", {}).get("residual_case_rows") != 0):
        raise D1PreflightError("M62_POLICY018_OFFLINE_APPLICATION_EVIDENCE_INCOMPLETE")
    return report


def validate_r9_successor_cases(root: Path, manifest: dict) -> dict:
    history = validate_r8_remaining_cases(root, manifest)
    quota = TaskQuotaLedger(root / manifest["task_ledger_path"], task_id=manifest["task_id"],
        seed_sha256=manifest["task_ledger_seed_sha256"]).read()
    r8_remaining = history["verified_unconsumed_case_ids"]
    consumed = [case_id for case_id in r8_remaining if quota.get("attempts", {}).get(case_id, 0) != 0]
    expected = [case_id for case_id in r8_remaining if quota.get("attempts", {}).get(case_id, 0) == 0]
    if (manifest.get("task_prior_consumed_case_ids") != consumed or manifest.get("proposed_order") != expected or
            manifest.get("r8_unconsumed_case_ids") != r8_remaining or
            manifest.get("case_universe_count_for_segment") != len(expected) or
            any(quota.get("case_records", {}).get(case_id, {}).get("status") == "ATTEMPT_RESERVED"
                for case_id in consumed) or quota.get("unknown_usage") or quota.get("over_limit")):
        raise D1PreflightError("M62_R9_SUCCESSOR_CASE_SCOPE_OR_TASK_LEDGER_MISMATCH")
    return {**history, "task_prior_consumed_case_ids": consumed, "remaining_case_ids": expected,
        "cumulative_spent": quota["spent"], "cumulative_reserved": quota["reserved"]}


def validate_policy018_recovery(root: Path, manifest: dict) -> dict:
    """Accept only the specifically authorized no-Run recovery of R9 POLICY-018."""
    package = root / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029"
    old_state = json.loads((package / "execution-state-r9-0E30E94C060A45C9.json").read_text(encoding="utf-8"))
    old_journal = [json.loads(line) for line in
        (package / "execution-journal-r9-0E30E94C060A45C9.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    old_auth = json.loads((package / "authorization-r9-0E30E94C060A45C9.json").read_text(encoding="utf-8"))
    original = json.loads((package / "reports/pre-run-local-failure-closeout-r1.json").read_text(encoding="utf-8"))
    case_id = "D1-POLICY-018"
    no_run = (old_state.get("case_state", {}).get(case_id) == "CLOSED_NO_RUN_PRE_EXECUTION_ERROR" and
        old_state.get("attempt_count", {}).get(case_id) == 1 and old_state.get("run_created") is False and
        old_state.get("provider_requests") == 0 and original.get("application_run_created") is False and
        original.get("provider_requests") == 0 and original.get("tokens") == 0 and
        old_auth.get("status") == "CLOSED_STOPPED" and original.get("authorization_final_status") == "CLOSED_STOPPED")
    events = [row for row in old_journal if row.get("case_id") == case_id]
    if (manifest.get("proposed_order") != [case_id] or not no_run or
            [row.get("event") for row in events] != ["CASE_ATTEMPT_RESERVED", "CASE_EXCEPTION_STOP"] or
            not events[-1].get("run_created") is False or events[-1].get("provider_requests") != 0 or
            events[-1].get("business_read_calls") != 0):
        raise D1PreflightError("M62_POLICY018_RECOVERY_SOURCE_NOT_ELIGIBLE")
    return {"source_case_id": case_id, "source_authorization_status": old_auth["status"],
        "original_attempt_preserved": True, "original_run_created": False,
        "original_provider_requests": 0, "original_business_reads": 0,
        "source_journal_sha256": digest(package / "execution-journal-r9-0E30E94C060A45C9.jsonl"),
        "source_state_sha256": digest(package / "execution-state-r9-0E30E94C060A45C9.json"),
        "source_failure_closeout_sha256": digest(package / "reports/pre-run-local-failure-closeout-r1.json")}


R10_FIRST_RUN_CASES = ["D1-PRODUCT-005", "D1-AFTERSALES-019", "D1-AFTERSALES-020", "D1-CROSSAGENT-032"]


def validate_r10_first_run_cases(root: Path, manifest: dict) -> dict:
    """Derive the four newly authorized cases from R9's bound hold ledger and task journal."""
    if manifest.get("proposed_order") != R10_FIRST_RUN_CASES:
        raise D1PreflightError("M62_R10_EXACT_CASE_ORDER_MISMATCH")
    remediation_rel = "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json"
    coverage_rel = "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/40-case-coverage-ledger-r9-final-r17.json"
    prior_ledger_rel = "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r2-policy018-recovery.json"
    sources = manifest.get("r9_first_run_sources", {})
    expected_paths = {"remediation_path": remediation_rel, "coverage_path": coverage_rel,
        "task_ledger_path": prior_ledger_rel}
    loaded = {}
    for field, rel in expected_paths.items():
        path = root / rel
        if sources.get(field) != rel or sources.get(field.replace("_path", "_sha256")) != digest(path):
            raise D1PreflightError("M62_R10_PREDECESSOR_BINDING_MISMATCH")
        loaded[field] = json.loads(path.read_text(encoding="utf-8"))
    remediation = loaded["remediation_path"]
    exception = manifest.get("readiness_exception", {})
    if (exception.get("case_ids") != R10_FIRST_RUN_CASES or
            exception.get("source_path") != remediation_rel or
            exception.get("source_sha256") != sources.get("remediation_sha256") or
            exception.get("source_status") != "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH"):
        raise D1PreflightError("M62_R10_READINESS_EXCEPTION_SOURCE_MISMATCH")
    ready = {row.get("case_id") for row in remediation.get("cases", [])
        if row.get("status") == "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH"}
    coverage_rows = {row.get("case_id"): row for row in loaded["coverage_path"].get("rows", [])}
    source_ledger = loaded["task_ledger_path"]
    if (remediation.get("case_count") != 12 or not set(R10_FIRST_RUN_CASES).issubset(ready) or
            any(coverage_rows.get(case_id, {}).get("detail", {}).get("run_id") for case_id in R10_FIRST_RUN_CASES) or
            source_ledger.get("spent", {}).get("application_runs") != 16 or
            source_ledger.get("limits", {}).get("application_runs") != 16 or
            source_ledger.get("unknown_usage") or source_ledger.get("over_limit") or
            source_ledger.get("reserved", {}).get("application_runs") != 0 or
            any(source_ledger.get("attempts", {}).get(case_id, 0) != 0 or
                case_id in source_ledger.get("case_records", {}) for case_id in R10_FIRST_RUN_CASES)):
        raise D1PreflightError("M62_R10_CASE_NOT_ELIGIBLE_AS_FIRST_RUN")
    return {"case_ids": R10_FIRST_RUN_CASES, "case_count": len(R10_FIRST_RUN_CASES),
        "application_ready_count": len(R10_FIRST_RUN_CASES), "r9_application_runs": 16,
        "new_case_attempts_zero": True, "new_case_runs_absent_from_r9_coverage": True,
        "sources": {field: {"path": expected_paths[field], "sha256": sources[field.replace("_path", "_sha256")]}
            for field in expected_paths}}


def main() -> int:
    global _ACTIVE_PROFILE, PACKAGE_REL, AUTH_TYPE, PKG
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=tuple(PROFILE_CONFIGS), default="r7")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--mode", choices=("check-admission", "execute"), required=True)
    args = parser.parse_args()
    _ACTIVE_PROFILE = PROFILE_CONFIGS[args.profile]
    PACKAGE_REL, AUTH_TYPE = _ACTIVE_PROFILE["package"], _ACTIVE_PROFILE["auth_type"]
    PKG = ROOT / PACKAGE_REL
    if not PKG.is_dir():
        raise D1PreflightError("M62_PROFILE_PACKAGE_MISSING")
    manifest_arg = args.manifest or (PKG / _ACTIVE_PROFILE["manifest"])
    rules_arg = args.rules or (PKG / _ACTIVE_PROFILE["rules"])
    if args.profile in {"r8", "r9", "r9s2", "r9s3", "r9s4", "r9p018", "r9p018r2", "r9p018r3", "r10-four-first-runs", "r10-four-first-runs-r2"} and (manifest_arg.resolve(strict=False).parent != PKG.resolve() or
                                  rules_arg.resolve(strict=False).parent != PKG.resolve()):
        raise D1PreflightError("M62_PROFILE_PATH_NOT_FIXED")
    dataset_rel = _ACTIVE_PROFILE.get("dataset_rel", DATASET_REL)
    data_sha = _ACTIVE_PROFILE.get("dataset_sha256", DATA_SHA)
    manifest, rules, matrix = load_exact_profile(ROOT, manifest_path=manifest_arg, rules_path=rules_arg,
        package_rel=PACKAGE_REL, dataset_rel=dataset_rel, dataset_sha256=data_sha,
        matrix_rel=_ACTIVE_PROFILE.get("matrix_rel", __import__("eval.m6_1.personal_dev_batch_m62", fromlist=["SOURCE_MATRIX_REL"]).SOURCE_MATRIX_REL))
    policy018_recovery = args.profile in {"r9p018", "r9p018r2", "r9p018r3"}
    prior_history = (validate_r325_bindings(ROOT, manifest) if args.profile == "r325-seven" else
                     validate_policy018_recovery(ROOT, manifest) if policy018_recovery else
                     validate_r10_first_run_cases(ROOT, manifest) if args.profile in {"r10-four-first-runs", "r10-four-first-runs-r2"} else
                     validate_r6_unconsumed_cases(ROOT, manifest) if args.profile == "r7" else
                     validate_r7_unconsumed_cases(ROOT, manifest) if args.profile == "r8" else
                     validate_r9_successor_cases(ROOT, manifest) if args.profile in {"r9s2", "r9s3", "r9s4"} else
                     validate_r8_remaining_cases(ROOT, manifest))
    manifest_path, rules_path = manifest_arg.resolve(strict=True), rules_arg.resolve(strict=True)
    auth_path = runtime_path(manifest, "authorization", manifest_path.parent)
    budget_path = runtime_path(manifest, "budget", manifest_path.parent)
    state_path = runtime_path(manifest, "state", manifest_path.parent)
    journal_path = runtime_path(manifest, "journal", manifest_path.parent)
    _CLI_CONTEXT.update({"state_path": state_path, "journal_path": journal_path,
                         "auth_path": auth_path})
    case_ids = manifest["proposed_order"]
    if manifest.get("profile_version") != _ACTIVE_PROFILE["profile_version"] or len(case_ids) != _ACTIVE_PROFILE["count"]:
        raise D1PreflightError("M62_PROFILE_OR_CASE_COUNT_INVALID")
    r9_profile = bool(_ACTIVE_PROFILE.get("cumulative")) or args.profile in {"r9", "r9s2", "r9s3", "r9s4", "r9p018", "r9p018r2", "r9p018r3", "r10-four-first-runs", "r10-four-first-runs-r2"}
    overrides = _ACTIVE_PROFILE.get("per_case_limits") if r9_profile else None
    batch_overrides = manifest.get("budget_contract", {}).get("batch") if r9_profile else None
    per_case_limits, batch_limits = derive_limits(len(case_ids), per_case_overrides=overrides,
        batch_overrides=batch_overrides)
    # Hardcoded profile and budget must match the user's authorized ceilings.
    if per_case_limits.to_dict() != rules["per_case_budget_limits"] or batch_limits.to_dict() != rules["batch_budget_limits"]:
        raise D1PreflightError("M62_LOCKED_BUDGET_MISMATCH")
    target = PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    coordinator = D1Coordinator(target, factory)
    bundle = json.loads((ROOT / dataset_rel).read_text(encoding="utf-8"))
    current_digest = implementation_digest(ROOT)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    state = {"mode": args.mode, "case_count": len(case_ids), "case_ids": case_ids,
        "dataset_sha256": digest(ROOT / dataset_rel), "implementation_digest": current_digest,
        "git_head": revision, "database_identity": target.identity, "agent_started": False,
        "run_created": False, "provider_requests": 0, "read_only_preflight_calls": 0,
        "actual_tool_calls": 0, "cases": []}
    try:
        if args.mode == "check-admission":
            admission_path = runtime_path(manifest, "admission", manifest_path.parent)
            if auth_path.exists() or state_path.exists() or journal_path.exists() or admission_path.exists():
                raise D1PreflightError("M62_PREVIOUS_AUTH_OR_STATE_EXISTS_NO_REPLAY")
            if not budget_path.is_file():
                raise D1PreflightError("M62_PREPARED_BUDGET_MISSING")
            prepared_budget = json.loads(budget_path.read_text(encoding="utf-8"))
            if (prepared_budget.get("batch_id") != manifest.get("batch_id") or
                    prepared_budget.get("manifest_sha256") != digest(manifest_path) or
                    prepared_budget.get("rules_sha256") != digest(rules_path) or
                    prepared_budget.get("batch_limits") != batch_limits.to_dict() or
                    prepared_budget.get("per_case_limits") != per_case_limits.to_dict()):
                raise D1PreflightError("M62_PREPARED_BUDGET_BINDING_MISMATCH")
            settings = load_settings(require_api_key=True)
            if args.profile == "r325-seven" and {k: vars(v) for k, v in settings.nodes.items()} != manifest["bindings"]["node_config"]:
                raise D1PreflightError("M62_NODE_CONFIG_BINDING_MISMATCH")
            proxy = ensure_proxy_configuration()
            if settings.model != manifest["bindings"]["model"] or settings.base_url != manifest["bindings"]["provider_endpoint"]:
                raise D1PreflightError("M62_MODEL_ENDPOINT_BINDING_MISMATCH")
            if current_digest != manifest["bindings"]["implementation_digest"]:
                raise D1PreflightError("M62_IMPLEMENTATION_BINDING_MISMATCH")
            task_quota = None
            if r9_profile:
                expected_ledger = task_quota_ledger_rel(args.profile, PACKAGE_REL)
                if manifest.get("task_ledger_path") != expected_ledger:
                    raise D1PreflightError("M62_R9_TASK_LEDGER_PATH_NOT_FIXED")
                task_quota = TaskQuotaLedger(ROOT / manifest["task_ledger_path"],
                    task_id=manifest.get("task_id"), seed_sha256=manifest.get("task_ledger_seed_sha256"))
                task_state = task_quota.read()
                if task_state.get("unknown_usage") or task_state.get("over_limit"):
                    raise D1PreflightError("M62_TASK_CUMULATIVE_LEDGER_NOT_CONTINUABLE")
            if target.identity != manifest["bindings"]["database_identity"]:
                raise D1PreflightError("M62_DATABASE_IDENTITY_MISMATCH")
            _price, pricing_sha = _validate_price_evidence(ROOT, manifest,
                model=settings.model, endpoint=settings.base_url)
            offline_application = (validate_policy018_offline_application_evidence(ROOT, manifest, current_digest)
                if policy018_recovery else validate_r7_offline_application_evidence(ROOT, manifest, current_digest)
                if args.profile == "r7" else validate_r8_offline_application_evidence(ROOT, manifest, current_digest)
                if args.profile == "r8" else prior_history if args.profile in {"r325-seven", "r10-four-first-runs", "r10-four-first-runs-r2"}
                else validate_r9_offline_application_evidence(ROOT, manifest, current_digest))
            repository_binding = manifest.get("repository_preflight_binding")
            if repository_binding:
                repo_path = (ROOT / repository_binding.get("path", "")).resolve(strict=True)
                expected_repo_path = (manifest_path.parent / f"application-admission-{_ACTIVE_PROFILE['tag']}.json").resolve()
                if repo_path != expected_repo_path or digest(repo_path) != repository_binding.get("sha256"):
                    raise D1PreflightError("M62_REPOSITORY_PREFLIGHT_SOURCE_HASH_MISMATCH")
                repository_source = json.loads(repo_path.read_text(encoding="utf-8"))
                passed = {row.get("case_id") for row in repository_source.get("repository_case_results", [])
                    if row.get("status") in {"APPLICATION_REPOSITORY_PREFLIGHT_PASS", "NO_BUSINESS_READS_REQUIRED"}}
                if (repository_source.get("case_ids") != repository_binding.get("case_ids") or
                        not set(case_ids).issubset(passed) or repository_binding.get("passing_case_ids") != case_ids or
                        repository_source.get("run_created") is not False or repository_source.get("provider_requests") != 0):
                    raise D1PreflightError("M62_REPOSITORY_PREFLIGHT_SOURCE_SCOPE_INVALID")
            with factory() as db:
                database_idle = _assert_idle_personal_database(factory, target)
            matrix_cases = {c["case_id"]: c for c in matrix["cases"]}
            total_reads = 0
            repository_case_results = []
            repository_blockers = []
            for case_id in case_ids:
                case = matrix_cases[case_id]
                plan = plan_case(bundle, case_id)
                allowance = int(case.get("repository_read_count_preflight_only", 0))
                if allowance > 8:
                    raise D1PreflightError(f"M62_PREFLIGHT_CASE_CAP_EXCEEDED:{case_id}")
                expected_caps = sorted({x["tool_id"] for x in plan["case"]["input"].get("expected_tool_calls", []) if x.get("mode") == "read_only"})
                if sorted(case["allowed_read_only_capabilities"]) != expected_caps:
                    reason = "M62_CASE_ALLOWLIST_BINDING_MISMATCH"
                    evidence = None
                else:
                    try:
                        if case["allowed_read_only_capabilities"]:
                            evidence = app_admission(coordinator, plan)
                        else:
                            evidence = app_admission(coordinator, plan_without_unauthorized_targets(plan))
                    except Exception as exc:
                        evidence = {"status": "APPLICATION_REPOSITORY_PREFLIGHT_ERROR",
                            "blocked_reason": f"{type(exc).__name__}:{str(exc)[:180]}", "read_count": 0}
                    reason = evidence.get("blocked_reason")
                    if case["allowed_read_only_capabilities"] and evidence.get("status") != "APPLICATION_REPOSITORY_PREFLIGHT_PASS":
                        reason = reason or evidence.get("status")
                    if not case["allowed_read_only_capabilities"] and (
                            evidence.get("status") != "NO_BUSINESS_READS_REQUIRED" or evidence.get("read_count") != 0):
                        reason = reason or "M62_ZERO_READ_CASE_PREFLIGHT_FAILED"
                evidence = evidence or {}
                reads = evidence.get("read_count", 0)
                if isinstance(reads, int) and reads >= 0:
                    total_reads += reads
                # A preflight error may not leave an active lease/receipt. If it
                # does, stop immediately and preserve the failure report only.
                with factory() as db:
                    marker = db.execute(__import__("sqlalchemy").text(
                        "SELECT lease_id,receipt FROM d1_personal_target")).one()
                if marker.lease_id is not None or marker.receipt is not None:
                    raise D1PreflightError(f"M62_PREFLIGHT_LEFT_ACTIVE_RECEIPT:{case_id}")
                case_row = {"case_id": case_id,
                    "status": evidence.get("status", "APPLICATION_REPOSITORY_PREFLIGHT_ERROR"),
                    "preflight_reads": reads,
                    "evidence": [{k:v for k,v in row.items() if k != "fixture_receipt"}
                        for row in evidence.get("repository_reads", [])],
                    "cleanup": evidence.get("receipt_cleanup"),
                    "target_fixture_loaded": bool(case["allowed_read_only_capabilities"]),
                    "blocked_reason": reason}
                repository_case_results.append(case_row)
                if reason:
                    repository_blockers.append({"case_id": case_id, "reason": reason})
            if total_reads > 8*len(case_ids) or total_reads > 320:
                raise D1PreflightError("M62_BATCH_PREFLIGHT_READ_CAP_EXCEEDED")
            task_ledger_state = task_quota.record_preflight_reads(total_reads) if task_quota else None
            state.update({"admission": "PASS" if not repository_blockers else "BLOCKED",
                "repository_case_results": repository_case_results, "repository_blockers": repository_blockers,
                "read_only_preflight_calls": total_reads,
                "proxy": proxy, "database_idle": database_idle,
                "manifest_sha256": digest(manifest_path), "rules_sha256": digest(rules_path),
                "admission_matrix_sha256": digest(ROOT / manifest["admission_matrix_path"]),
                "pricing_sha256": pricing_sha,
                "offline_application_admission_sha256": digest(ROOT / manifest["application_admission_path"]),
                "offline_application_case_count": offline_application["case_count"],
                "offline_application_ready_count": offline_application["application_ready_count"],
                "task_quota_ledger_state_sha256": task_ledger_state.get("state_sha256") if task_ledger_state else None,
                "task_quota_spent": task_ledger_state.get("spent") if task_ledger_state else None,
                "predecessor_unconsumed_verification": prior_history,
                "application_path": "All exact R6-unconsumed cases exercised through execute_application_case/LangGraph with an in-process intercepted model transport, then isolated personal-SQLite Fixture/Repository/owner preflight. No external Provider call or personal Dev trial is made by this command."})
            out = runtime_path(manifest, "admission" if not repository_blockers else "admission_blocked", manifest_path.parent)
            with out.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            print(json.dumps({"admission":state["admission"],"case_count":len(case_ids),"repository_reads":total_reads,
                "agent_started":False,"run_created":False,"provider_requests":0,"application_admission":out.relative_to(ROOT).as_posix()}))
            if repository_blockers:
                print(json.dumps({"admission":"BLOCKED","blocker_count":len(repository_blockers),
                    "blockers":repository_blockers},ensure_ascii=False))
                return 1
            return 0

        settings = load_settings(require_api_key=True)
        if args.profile == "r325-seven" and {k: vars(v) for k, v in settings.nodes.items()} != manifest["bindings"]["node_config"]:
            raise D1PreflightError("M62_NODE_CONFIG_BINDING_MISMATCH")
        if not auth_path.is_file() or not budget_path.is_file():
            raise D1PreflightError("M62_EXECUTION_AUTH_AND_BUDGET_REQUIRED")
        proxy = ensure_proxy_configuration()
        admission_path = runtime_path(manifest, "admission", manifest_path.parent)
        if not admission_path.is_file() or state_path.exists():
            raise D1PreflightError("M62_ADMISSION_REPORT_MISSING_OR_EXECUTION_ALREADY_STARTED")
        if journal_path.exists() or journal_path.with_suffix(journal_path.suffix + ".lock").exists():
            raise D1PreflightError("M62_EXECUTION_JOURNAL_ALREADY_EXISTS_NO_REPLAY")
        admission = json.loads(admission_path.read_text(encoding="utf-8"))
        auth, budget = json.loads(auth_path.read_text(encoding="utf-8")), json.loads(budget_path.read_text(encoding="utf-8"))
        validate_application_admission_binding(admission, case_ids=case_ids,
            manifest_sha256=digest(manifest_path), rules_sha256=digest(rules_path),
            code_sha256=current_digest, dataset_sha256=data_sha,
            database_identity=target.identity, pricing_sha256=auth.get("pricing_sha256"))
        if manifest.get("status") != "FROZEN" or auth.get("status") != "ACTIVE" or auth.get("authorization_type") != AUTH_TYPE:
            raise D1PreflightError("M62_AUTHORIZATION_CLOSED_OR_INVALID")
        if (auth.get("case_ids") != case_ids or auth.get("manifest_sha256") != digest(manifest_path) or
                auth.get("rules_sha256") != digest(rules_path) or auth.get("code_sha256") != current_digest or
                auth.get("dataset_sha256") != data_sha or auth.get("database_identity") != target.identity or
                auth.get("model") != settings.model or auth.get("provider_endpoint") != settings.base_url or
                auth.get("git_head_revision") != revision):
            raise D1PreflightError("M62_AUTHORIZATION_BINDING_MISMATCH")
        task_quota = None
        task_state = None
        if r9_profile:
            expected_ledger = task_quota_ledger_rel(args.profile, PACKAGE_REL)
            if (manifest.get("task_ledger_path") != expected_ledger or
                    auth.get("task_id") != manifest.get("task_id") or
                    auth.get("task_ledger_seed_sha256") != manifest.get("task_ledger_seed_sha256")):
                raise D1PreflightError("M62_TASK_QUOTA_AUTH_BINDING_MISMATCH")
            task_quota = TaskQuotaLedger(ROOT / manifest["task_ledger_path"], task_id=manifest["task_id"],
                seed_sha256=manifest["task_ledger_seed_sha256"])
            task_state = task_quota.read()
            if (task_state.get("unknown_usage") or task_state.get("over_limit") or
                    task_state.get("state_sha256") != auth.get("task_ledger_state_sha256") or
                    task_state["spent"]["provider_requests"] + task_state["reserved"].get("provider_requests", 0) >=
                        task_state["limits"]["provider_requests"] or
                    task_state["spent"]["tokens"] >= task_state["limits"]["tokens"] or
                    task_state["spent"]["local_estimated_usd"] >= task_state["limits"]["local_estimated_usd"]):
                raise D1PreflightError("M62_TASK_CUMULATIVE_BUDGET_UNAVAILABLE")
            if policy018_recovery and "D1-POLICY-018" in task_state.get("recovery_records", {}):
                raise D1PreflightError("M62_POLICY018_RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
        if digest(budget_path) != auth.get("budget_sha256") or budget.get("batch_limits") != batch_limits.to_dict():
            raise D1PreflightError("M62_BUDGET_BINDING_MISMATCH")
        price_record, pricing_sha = _validate_price_evidence(ROOT, manifest, model=settings.model, endpoint=settings.base_url)
        if pricing_sha != auth.get("pricing_sha256"):
            raise D1PreflightError("M62_PRICING_BINDING_MISMATCH")
        if auth.get("application_admission_sha256") != digest(admission_path):
            raise D1PreflightError("M62_APPLICATION_ADMISSION_HASH_MISMATCH")
        if auth.get("authorized_by") != "CHAT_USER_SELF_CONFIRMED" or auth.get("grading_status") != "NOT_GRADED":
            raise D1PreflightError("M62_PERSONAL_AUTHORIZATION_ACTOR_OR_GRADING_INVALID")
        now = _utc_now()
        confirmed = datetime.fromisoformat(auth["confirmed_at_utc"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(auth["expires_at_utc"].replace("Z", "+00:00"))
        if confirmed > now or expires <= now or expires - confirmed > __import__("datetime").timedelta(hours=24):
            raise D1PreflightError("M62_AUTHORIZATION_EXPIRED_OR_INVALID")
        journal = DurableAttemptJournal(journal_path, batch_id=auth["authorization_id"])
        consumed = journal.consumed_cases()
        if consumed:
            raise D1PreflightError("M62_AUTHORIZATION_ALREADY_CONSUMED_NO_REPLAY")
        database_idle = _assert_idle_personal_database(factory, target)
        permit = {"authorization_id": auth["authorization_id"], "asset_sha256": data_sha,
            "case_ids": case_ids, "database_identity": target.identity, "code_sha256": current_digest,
            "git_commit": revision, "data_version": bundle["version"], "authorization_type": AUTH_TYPE,
            "allowed_tool_capabilities": sorted({t for c in matrix["cases"] if c["case_id"] in case_ids for t in c["allowed_read_only_capabilities"]})}
        ledger = create_budget_ledger(target, permit, batch_limits, settings)
        if ledger.snapshot()["events"]:
            raise D1PreflightError("M62_BATCH_BUDGET_LEDGER_NOT_EMPTY")
        if state_path.exists():
            raise D1PreflightError("M62_EXECUTION_STATE_ALREADY_EXISTS_NO_REPLAY")
        journal.acquire_batch()
        state_doc = {"schema_version":"m6.2-r324-execution-state-v2","authorization_id":auth["authorization_id"],
            "manifest_sha256":digest(manifest_path),"rules_sha256":digest(rules_path),"code_sha256":current_digest,
            "dataset_sha256":DATA_SHA,"database_identity":target.identity,"attempt_count":{case_id:0 for case_id in case_ids},
            "case_state":{case_id:"NOT_RUN" for case_id in case_ids}, "proxy": proxy,
            "database_idle_before_run": database_idle, "pricing_sha256": pricing_sha,
            "started_at_utc": _utc_now().isoformat().replace("+00:00", "Z")}
        with state_path.open("x",encoding="utf-8",newline="\n") as stream:
            json.dump(state_doc,stream,ensure_ascii=False,indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        matrix_cases = {c["case_id"]:c for c in matrix["cases"]}
        batch_root = manifest_path.parent
        completed = []
        for index, case_id in enumerate(case_ids):
            if (not journal.lock_path.is_file() or
                    journal.lock_path.read_text(encoding="utf-8").strip() != auth["authorization_id"]):
                raise D1PreflightError("M62_EXECUTION_LOCK_NOT_OWNED")
            if args.profile == "r325-seven":
                validate_r325_bindings(ROOT, manifest)
                if implementation_digest(ROOT) != current_digest:
                    raise D1PreflightError("M62_EXECUTION_CODE_CHANGED")
            case_limits = per_case_limits
            if args.profile == "r325-seven":
                from dataclasses import replace
                case_limits = replace(case_limits, tool_calls=rules["case_rules"][case_id]["maximum_business_read_calls"])
            if task_quota:
                task_state = task_quota.read()
                if (task_state["spent"]["application_runs"] + task_state["reserved"]["application_runs"] >=
                        task_state["limits"]["application_runs"]):
                    journal.append("BATCH_STOP_TASK_APPLICATION_RUN_CAP", case_id=case_id,
                        task_spent=task_state["spent"], task_limits=task_state["limits"])
                    break
                remaining_runtime = (task_state["limits"]["runtime_seconds"] -
                    task_state["spent"]["runtime_seconds"] - task_state["reserved"]["runtime_seconds"])
                if remaining_runtime <= 0:
                    journal.append("BATCH_STOP_TASK_RUNTIME_CAP", case_id=case_id)
                    break
                from dataclasses import replace
                case_limits = replace(per_case_limits,
                    max_duration_seconds=min(per_case_limits.max_duration_seconds, remaining_runtime))
                if policy018_recovery:
                    task_quota.reserve_recovery_case(case_id, limits=case_limits.to_dict(),
                        authorization_id=auth["authorization_id"],
                        source_record_sha256=prior_history["source_journal_sha256"])
                else:
                    task_quota.reserve_case(case_id, limits=case_limits.to_dict())
            # Consume the one-shot attempt in both the fsync'd journal and state
            # file before fixture writes, Run creation, or any Provider request.
            state_doc["attempt_count"][case_id] = 1
            state_doc["case_state"][case_id] = "ATTEMPT_RESERVED"
            _atomic_json(state_path, state_doc)
            journal.reserve_attempt(case_id, budget=per_case_limits.to_dict(), bindings={
                "manifest_sha256": digest(manifest_path), "rules_sha256": digest(rules_path),
                "code_sha256": current_digest, "dataset_sha256": data_sha, "database_identity": target.identity})
            plan = plan_case(bundle, case_id)
            if not matrix_cases[case_id]["allowed_read_only_capabilities"]:
                plan = plan_without_unauthorized_targets(plan)
            if not matrix_cases[case_id]["allowed_read_only_capabilities"]:
                from dataclasses import replace
                case_limits = replace(case_limits, tool_calls=0)
            case_ledger = PerCaseBudgetLedger(ledger, case_id, case_limits)
            case_started = time.monotonic()
            try:
                result = coordinator._execute_validated_plan(plan=plan, case_id=case_id, ledger=case_ledger,
                    settings=settings, defer_cleanup=True)
                state_doc["run_created"] = bool(result.get("run_id"))
                state_doc["agent_started"] = bool(result.get("agent_started"))
                state_doc.setdefault("run_ids", [])
                if result.get("run_id") and result["run_id"] not in state_doc["run_ids"]:
                    state_doc["run_ids"].append(result["run_id"])
                _atomic_json(state_path, state_doc)
                if result.get("run_id"):
                    journal.append("RUN_CREATED", case_id=case_id, run_id=result["run_id"],
                        status=result.get("status"))
                receipt = result.pop("_diagnostic_receipt", None)
                snap = ledger.trial_snapshot(case_id)
                archive_path = ROOT / result["evidence_path"]
                archive = json.loads(archive_path.read_text(encoding="utf-8"))
                cleanup_safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
                if cleanup_safety.get("safe_to_cleanup") is not True:
                    raise D1PreflightError("M62_CLEANUP_SAFETY_CHECK_FAILED")
                coordinator.cleanup(receipt)
                cleanup_evidence = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
                if cleanup_evidence.get("cleanup_status") != "CLEANED_AND_RELEASED" or cleanup_evidence.get("residual_case_rows") != 0:
                    raise D1PreflightError("M62_CLEANUP_VERIFICATION_FAILED")
                grader = (score_archive_current if args.profile == "r325-seven" else
                          score_archive_r7 if args.profile == "r7" else score_archive_r8)(
                    plan["case"], archive, budget_snapshot=snap,
                    cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
                if task_quota:
                    usage_complete = (snap.get("unknown_tokens") is False and snap.get("unknown_cost") is False and
                        not snap.get("integrity_errors") and snap.get("active_reservations") == 0)
                    task_quota.settle_case(case_id, run_created=bool(result.get("run_id")),
                        provider_requests=snap.get("calls_by_category", {}).get("provider") if usage_complete else None,
                        tokens=snap.get("tokens") if usage_complete else None,
                        local_estimated_usd=snap.get("estimated_usd") if usage_complete else None,
                        elapsed_seconds=time.monotonic() - case_started if usage_complete else None,
                        status="CLOSED" if usage_complete else "UNKNOWN_STOP",
                        recovery_authorization_id=auth["authorization_id"] if policy018_recovery else None)
                grade_path = batch_root / "scores" / f"{case_id}-code-grade-{_ACTIVE_PROFILE['tag']}.json"
                grade_path.parent.mkdir(parents=True, exist_ok=True)
                with grade_path.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(grader, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                journal.append("CASE_CLOSEOUT", case_id=case_id, run_id=result.get("run_id"),
                    run_status=result.get("status"), archive_path=result.get("evidence_path"),
                    archive_sha256=result.get("evidence_sha256"), grader_path=grade_path.relative_to(ROOT).as_posix(),
                    grader_sha256=digest(grade_path), budget=snap, cleanup=cleanup_evidence,
                    classification=grader["task_outcome"])
                completed.append({"case_id": case_id, "run_id": result.get("run_id"), "status": result.get("status"), "grader": grader})
                state_doc["case_state"][case_id] = "CLOSED"
                state_doc["cleanup_status"] = cleanup_evidence.get("cleanup_status")
                _atomic_json(state_path, state_doc)
                stop_reason = (batch_stop_reason_r7(grader) if args.profile == "r7" else
                               batch_stop_reason_r9(grader) if r9_profile else
                               batch_stop_reason_r8(grader))
                if stop_reason:
                    journal.append("BATCH_STOP", case_id=case_id, reason=stop_reason)
                    break
            except Exception as exc:
                captured = coordinator.capture_active_run_after_exception(case_id=case_id,
                    user_query=plan["message"], error_type=type(exc).__name__)
                state_doc["run_created"] = bool(state_doc.get("run_created") or captured.get("run_id"))
                state_doc["agent_started"] = bool(state_doc.get("agent_started") or captured.get("agent_started"))
                state_doc["cleanup_status"] = captured.get("cleanup_status", "UNKNOWN")
                _atomic_json(state_path, state_doc)
                if task_quota:
                    try:
                        failed_snapshot = ledger.trial_snapshot(case_id)
                        known = (failed_snapshot.get("unknown_tokens") is False and
                            failed_snapshot.get("unknown_cost") is False and not failed_snapshot.get("integrity_errors") and
                            failed_snapshot.get("active_reservations") == 0)
                        task_quota.settle_case(case_id, run_created=bool(captured.get("run_id")),
                            provider_requests=failed_snapshot.get("calls_by_category", {}).get("provider") if known else None,
                            tokens=failed_snapshot.get("tokens") if known else None,
                            local_estimated_usd=failed_snapshot.get("estimated_usd") if known else None,
                            elapsed_seconds=time.monotonic() - case_started if known else None,
                            status="UNKNOWN_STOP" if not known else "EXECUTION_EXCEPTION",
                            recovery_authorization_id=auth["authorization_id"] if policy018_recovery else None)
                    except Exception:
                        pass
                journal.append("CASE_EXCEPTION_STOP", case_id=case_id, error_type=type(exc).__name__,
                    captured=captured.get("captured"), run_id=captured.get("run_id"), run_status=captured.get("run_status"))
                state_doc["case_state"][case_id] = "UNKNOWN_STOP"
                _atomic_json(state_path, state_doc)
                break
        journal.append("BATCH_CLOSED", closed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            completed_case_ids=[x["case_id"] for x in completed], final_budget=ledger.snapshot())
        auth["status"] = "CLOSED_COMPLETED" if len(completed) == len(case_ids) else "CLOSED_STOPPED"
        auth["closed_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        auth_path.write_text(json.dumps(auth, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        journal_rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        closeout_rows = [row for row in journal_rows if row.get("event") == "CASE_CLOSEOUT"]
        case_results = []
        for row in closeout_rows:
            archive_path = ROOT / row["archive_path"]
            grade_path = ROOT / row["grader_path"]
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            grade = json.loads(grade_path.read_text(encoding="utf-8"))
            result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
            case_results.append({"case_id": row["case_id"], "run_id": row["run_id"],
                "run_status": row.get("run_status"), "task_outcome": grade.get("task_outcome"),
                "failure_classification": grade.get("failure_classification"),
                "continue_decision": grade.get("continue_decision"),
                "archive_path": row["archive_path"], "archive_sha256": row["archive_sha256"],
                "grader_path": row["grader_path"], "grader_sha256": row["grader_sha256"],
                "user_visible_answer_present": grade.get("program_checks", {}).get("user_visible_answer_present"),
                "provider_requests": row.get("budget", {}).get("calls_by_category", {}).get("provider"),
                "tokens": row.get("budget", {}).get("tokens"), "cleanup": row.get("cleanup"),
                "business_read_calls": result.get("read_calls")})
        reports = batch_root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        case_results_path = reports / f"case-results-{_ACTIVE_PROFILE['tag']}.json"
        closeout_path = reports / f"closeout-{_ACTIVE_PROFILE['tag']}.json"
        case_results_value = {"schema_version": f"m6.2-r324-{_ACTIVE_PROFILE['tag']}-case-results-v1",
                "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
                "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
                "batch_id": auth["batch_id"], "manifest_sha256": digest(manifest_path),
                "rules_sha256": digest(rules_path), "dataset_sha256": data_sha,
                "implementation_digest": current_digest, "cases": case_results}
        case_results_sha = _atomic_create_json(case_results_path, case_results_value)
        if digest(case_results_path) != case_results_sha:
            raise D1PreflightError("M62_CASE_RESULTS_HASH_VERIFICATION_FAILED")
        closeout_value = {"schema_version": f"m6.2-r324-{_ACTIVE_PROFILE['tag']}-closeout-v1",
                "classification": "PERSONAL_DEV_AGENT_EVAL_NOT_FORMAL_SMOKE_GOLD_FROZEN_OR_M2",
                "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
                "batch_id": auth["batch_id"], "authorization_id": auth["authorization_id"],
                "authorization_final_status": auth["status"], "manifest_sha256": digest(manifest_path),
                "rules_sha256": digest(rules_path), "dataset_sha256": data_sha,
                "implementation_digest": current_digest, "planned_case_ids": case_ids,
                "attempted_case_ids": [row["case_id"] for row in closeout_rows],
                "not_run_case_ids": [case_id for case_id in case_ids if case_id not in {row["case_id"] for row in closeout_rows}],
                "provider_requests": ledger.snapshot().get("calls_by_category", {}).get("provider", 0),
                "final_budget": ledger.snapshot(), "case_results_path": case_results_path.relative_to(ROOT).as_posix(),
                "case_results_sha256": case_results_sha, "journal_path": journal_path.relative_to(ROOT).as_posix(),
                "journal_sha256": digest(journal_path), "execution_state_path": state_path.relative_to(ROOT).as_posix(),
                "execution_state_sha256": digest(state_path)}
        _atomic_create_json(closeout_path, closeout_value)
        with factory() as db:
            mark = db.execute(__import__("sqlalchemy").text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        if mark.lease_id is None and mark.receipt is None and completed and all(
                x.get("grader",{}).get("program_checks",{}).get("budget_and_cleanup") == "PASS" for x in completed):
            journal.release_lock_after_safe_closeout()
        print(json.dumps({"batch_status":auth["status"],"completed":len(completed),"planned":len(case_ids),
            "agent_started":any(x["grader"].get("agent_started") for x in completed),
            "provider_requests":ledger.snapshot().get("calls_by_category",{}).get("provider",0)},ensure_ascii=False))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except D1PreflightError as exc:
        print(json.dumps(_cli_failure_projection(exc), ensure_ascii=False))
        raise SystemExit(2)
    except Exception as exc:
        # Do not print provider exceptions, configuration values, headers or URL credentials.
        print(json.dumps(_cli_failure_projection(exc), ensure_ascii=False))
        raise SystemExit(2)
