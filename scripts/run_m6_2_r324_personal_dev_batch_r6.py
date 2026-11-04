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

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from app.m0.config import load_settings
from eval.m6_1.personal_dev import D1Coordinator, D1PreflightError, create_budget_ledger, implementation_digest, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_batch_m62 import (DATASET_REL, DurableAttemptJournal,
    app_admission, batch_stop_reason_r6, derive_limits, load_exact_profile,
    plan_without_unauthorized_targets, score_archive_r6)
from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger

PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
AUTH_TYPE = "M62_PERSONAL_DEV_R5_UNCONSUMED_22_ONE_TIME"
PRIOR_R5_PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029"
PKG = ROOT / PACKAGE_REL
DATA_SHA = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def validate_r6_offline_application_evidence(root: Path, manifest: dict, current_digest: str) -> dict:
    """Validate the exact 22-case actual-app offline sweep before live execution."""
    path = root / manifest.get("application_admission_path", "")
    expected = (root / PACKAGE_REL / "application-admission-offline-r2.json").resolve(strict=True)
    if path.resolve(strict=True) != expected or digest(path) != manifest.get("application_admission_sha256"):
        raise D1PreflightError("M62_OFFLINE_APPLICATION_ADMISSION_PATH_OR_HASH_INVALID")
    report = json.loads(path.read_text(encoding="utf-8"))
    expected_ids = manifest.get("r5_verified_not_run_case_ids")
    rows = report.get("records")
    if (report.get("schema_version") != "m62-r6-application-admission-r1" or
            report.get("case_count") != len(expected_ids or []) or report.get("external_calls") != 0 or
            report.get("formal_run_created") is not False or
            report.get("implementation_digest") != current_digest or
            report.get("dataset_sha256") != DATA_SHA or
            [row.get("case_id") for row in rows or []] != expected_ids or len(rows or []) != 22):
        raise D1PreflightError("M62_R6_OFFLINE_APPLICATION_ADMISSION_SCOPE_OR_BINDING_INVALID")
    for row in rows:
        archive = (root / row.get("archive_path", "")).resolve(strict=True)
        if (not archive.is_relative_to(root.resolve()) or digest(archive) != row.get("archive_sha256") or
                not isinstance(row.get("cleanup"), dict) or
                row["cleanup"].get("cleanup_status") != "CLEANED_AND_RELEASED" or
                row["cleanup"].get("residual_case_rows") != 0 or
                row.get("application_admission") not in {"APPLICATION_READY", "BLOCKED_ENGINEERING"}):
            raise D1PreflightError("M62_R6_OFFLINE_APPLICATION_ARCHIVE_OR_CLEANUP_INVALID")
    first = next((row for row in rows if row.get("case_id") == manifest["proposed_order"][0]), None)
    if (not first or first.get("route", {}).get("decision_source") != "LLM" or
            int(first.get("intercepted_requests", 0)) < 1 or
            first.get("application_admission") != "APPLICATION_READY"):
        raise D1PreflightError("M62_R6_FIRST_CASE_NOT_VERIFIED_MODEL_REQUIRED_PATH")
    ready = [row["case_id"] for row in rows if row.get("application_admission") == "APPLICATION_READY"]
    if (not isinstance(manifest.get("proposed_order"), list) or
            len(manifest["proposed_order"]) != len(set(manifest["proposed_order"])) or
            set(manifest["proposed_order"]) != set(ready)):
        raise D1PreflightError("M62_R6_CASE_ORDER_NOT_EQUAL_APPLICATION_READY_INTERSECTION")
    return report


def validate_r5_unconsumed_cases(root: Path, manifest: dict) -> dict:
    """Prove the new case set was NOT_RUN in R5 from its frozen state and journal."""
    source = manifest.get("r5_history", {})
    expected_paths = {
        "coverage_ledger_path": f"{PRIOR_R5_PACKAGE_REL}/reports/40-case-coverage-ledger-r5-r1.json",
        "execution_state_path": f"{PRIOR_R5_PACKAGE_REL}/execution-state-r5.json",
        "execution_journal_path": f"{PRIOR_R5_PACKAGE_REL}/execution-journal-r5.jsonl",
        "closeout_path": f"{PRIOR_R5_PACKAGE_REL}/reports/closeout-r5-final.json",
    }
    content = {}
    for field, exact_path in expected_paths.items():
        if source.get(field) != exact_path:
            raise D1PreflightError("M62_R6_R5_SOURCE_PATH_NOT_FIXED")
        path = (root / exact_path).resolve(strict=True)
        expected_sha = source.get(field.replace("_path", "_sha256"))
        if not isinstance(expected_sha, str) or digest(path) != expected_sha:
            raise D1PreflightError("M62_R6_R5_SOURCE_HASH_MISMATCH")
        content[field] = path
    closeout = json.loads(content["closeout_path"].read_text(encoding="utf-8"))
    if closeout.get("authorization_final_status") != "CLOSED_STOPPED" or \
            closeout.get("batch_id") != manifest.get("r5_history", {}).get("batch_id"):
        raise D1PreflightError("M62_R6_R5_CLOSEOUT_NOT_STOPPED_OR_BATCH_MISMATCH")
    ledger = json.loads(content["coverage_ledger_path"].read_text(encoding="utf-8"))
    not_run = [row.get("case_id") for row in ledger.get("rows", [])
        if row.get("final_status") == "NOT_RUN_BATCH_STOP"]
    exact = manifest.get("r5_verified_not_run_case_ids")
    if not isinstance(exact, list) or len(exact) != 22 or not_run != exact:
        raise D1PreflightError("M62_R6_R5_NOT_RUN_LEDGER_SCOPE_MISMATCH")
    state = json.loads(content["execution_state_path"].read_text(encoding="utf-8"))
    attempt_counts = state.get("attempt_count", {})
    if any(attempt_counts.get(case_id) != 0 for case_id in exact):
        raise D1PreflightError("M62_R6_R5_CASE_ATTEMPT_ALREADY_CONSUMED")
    reservations = set()
    run_ids = set()
    for line in content["execution_journal_path"].read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "CASE_ATTEMPT_RESERVED": reservations.add(row.get("case_id"))
        if row.get("event") == "CASE_CLOSEOUT" and row.get("run_id"): run_ids.add(row.get("run_id"))
    ledger_rows = {row.get("case_id"): row for row in ledger.get("rows", [])}
    if set(exact) & reservations or any(ledger_rows[c].get("detail", {}).get("run_id") for c in exact):
        raise D1PreflightError("M62_R6_R5_CASE_HAS_ATTEMPT_OR_RUN")
    return {"verified_not_run_case_ids": exact, "r5_consumed_case_ids": sorted(reservations),
        "r5_run_ids": sorted(run_ids), "attempt_count_zero": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=PKG / "batch-manifest-r6.json")
    parser.add_argument("--rules", type=Path, default=PKG / "locked-rules-r6.json")
    parser.add_argument("--mode", choices=("check-admission", "execute"), required=True)
    args = parser.parse_args()
    manifest, rules, matrix = load_exact_profile(ROOT, manifest_path=args.manifest, rules_path=args.rules,
        package_rel=PACKAGE_REL)
    prior_r5 = validate_r5_unconsumed_cases(ROOT, manifest)
    manifest_path, rules_path = args.manifest.resolve(strict=True), args.rules.resolve(strict=True)
    auth_path, budget_path = manifest_path.parent / "authorization-r6.json", manifest_path.parent / "budget-r6.json"
    state_path, journal_path = manifest_path.parent / "execution-state-r6.json", manifest_path.parent / "execution-journal-r6.jsonl"
    case_ids = manifest["proposed_order"]
    if manifest.get("profile_version") != "m6.2-r324-exact-remainder-profile-r6" or not 1 <= len(case_ids) <= 22:
        raise D1PreflightError("M62_R6_PROFILE_OR_CASE_COUNT_INVALID")
    per_case_limits, batch_limits = derive_limits(len(case_ids))
    # Hardcoded profile and budget must match the user's authorized ceilings.
    if per_case_limits.to_dict() != rules["per_case_budget_limits"] or batch_limits.to_dict() != rules["batch_budget_limits"]:
        raise D1PreflightError("M62_LOCKED_BUDGET_MISMATCH")
    target = PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    coordinator = D1Coordinator(target, factory)
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    current_digest = implementation_digest(ROOT)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    state = {"mode": args.mode, "case_count": len(case_ids), "case_ids": case_ids,
        "dataset_sha256": digest(ROOT / DATASET_REL), "implementation_digest": current_digest,
        "git_head": revision, "database_identity": target.identity, "agent_started": False,
        "run_created": False, "provider_requests": 0, "read_only_preflight_calls": 0,
        "actual_tool_calls": 0, "cases": []}
    try:
        if args.mode == "check-admission":
            if auth_path.exists() or budget_path.exists() or state_path.exists() or journal_path.exists():
                raise D1PreflightError("M62_PREVIOUS_AUTH_OR_STATE_EXISTS_NO_REPLAY")
            settings = load_settings(require_api_key=True)
            proxy = ensure_proxy_configuration()
            if settings.model != manifest["bindings"]["model"] or settings.base_url != manifest["bindings"]["provider_endpoint"]:
                raise D1PreflightError("M62_MODEL_ENDPOINT_BINDING_MISMATCH")
            if current_digest != manifest["bindings"]["implementation_digest"]:
                raise D1PreflightError("M62_IMPLEMENTATION_BINDING_MISMATCH")
            if target.identity != manifest["bindings"]["database_identity"]:
                raise D1PreflightError("M62_DATABASE_IDENTITY_MISMATCH")
            _price, pricing_sha = _validate_price_evidence(ROOT, manifest,
                model=settings.model, endpoint=settings.base_url)
            offline_application = validate_r6_offline_application_evidence(ROOT, manifest, current_digest)
            repository_binding = manifest.get("repository_preflight_binding")
            if repository_binding:
                repo_path = (ROOT / repository_binding.get("path", "")).resolve(strict=True)
                expected_repo_path = (manifest_path.parent / "application-admission-r5-blocked.json").resolve()
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
            state.update({"admission": "PASS" if not repository_blockers else "BLOCKED",
                "repository_case_results": repository_case_results, "repository_blockers": repository_blockers,
                "read_only_preflight_calls": total_reads,
                "proxy": proxy, "database_idle": database_idle,
                "manifest_sha256": digest(manifest_path), "rules_sha256": digest(rules_path),
                "admission_matrix_sha256": digest(ROOT / manifest["admission_matrix_path"]),
                "pricing_sha256": pricing_sha,
                "offline_application_admission_sha256": digest(manifest_path.parent / "application-admission-offline-r2.json"),
                "offline_application_case_count": offline_application["case_count"],
                "offline_application_ready_count": offline_application["ready_count"],
                "r5_not_run_verification": prior_r5,
                "application_path": "Exact R5-unconsumed cases exercised through execute_application_case/LangGraph with an in-process intercepted model transport, then isolated personal-SQLite Fixture/Repository/owner preflight. No external Provider call or authorized personal Dev Run is made by this command."})
            out = manifest_path.parent / ("application-admission-r6.json" if not repository_blockers
                else "application-admission-r6-blocked.json")
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
        if not auth_path.is_file() or not budget_path.is_file():
            raise D1PreflightError("M62_EXECUTION_AUTH_AND_BUDGET_REQUIRED")
        proxy = ensure_proxy_configuration()
        admission_path = manifest_path.parent / "application-admission-r6.json"
        if not admission_path.is_file() or state_path.exists():
            raise D1PreflightError("M62_ADMISSION_REPORT_MISSING_OR_EXECUTION_ALREADY_STARTED")
        if journal_path.exists() or journal_path.with_suffix(journal_path.suffix + ".lock").exists():
            raise D1PreflightError("M62_EXECUTION_JOURNAL_ALREADY_EXISTS_NO_REPLAY")
        admission = json.loads(admission_path.read_text(encoding="utf-8"))
        auth, budget = json.loads(auth_path.read_text(encoding="utf-8")), json.loads(budget_path.read_text(encoding="utf-8"))
        validate_application_admission_binding(admission, case_ids=case_ids,
            manifest_sha256=digest(manifest_path), rules_sha256=digest(rules_path),
            code_sha256=current_digest, dataset_sha256=DATA_SHA,
            database_identity=target.identity, pricing_sha256=auth.get("pricing_sha256"))
        if manifest.get("status") != "FROZEN" or auth.get("status") != "ACTIVE" or auth.get("authorization_type") != AUTH_TYPE:
            raise D1PreflightError("M62_AUTHORIZATION_CLOSED_OR_INVALID")
        if (auth.get("case_ids") != case_ids or auth.get("manifest_sha256") != digest(manifest_path) or
                auth.get("rules_sha256") != digest(rules_path) or auth.get("code_sha256") != current_digest or
                auth.get("dataset_sha256") != DATA_SHA or auth.get("database_identity") != target.identity or
                auth.get("model") != settings.model or auth.get("provider_endpoint") != settings.base_url or
                auth.get("git_head_revision") != revision):
            raise D1PreflightError("M62_AUTHORIZATION_BINDING_MISMATCH")
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
        permit = {"authorization_id": auth["authorization_id"], "asset_sha256": DATA_SHA,
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
            # Consume the one-shot attempt in both the fsync'd journal and state
            # file before fixture writes, Run creation, or any Provider request.
            state_doc["attempt_count"][case_id] = 1
            state_doc["case_state"][case_id] = "ATTEMPT_RESERVED"
            _atomic_json(state_path, state_doc)
            journal.reserve_attempt(case_id, budget=per_case_limits.to_dict(), bindings={
                "manifest_sha256": digest(manifest_path), "rules_sha256": digest(rules_path),
                "code_sha256": current_digest, "dataset_sha256": DATA_SHA, "database_identity": target.identity})
            plan = plan_case(bundle, case_id)
            if not matrix_cases[case_id]["allowed_read_only_capabilities"]:
                plan = plan_without_unauthorized_targets(plan)
            case_limits = per_case_limits
            if not matrix_cases[case_id]["allowed_read_only_capabilities"]:
                from dataclasses import replace
                case_limits = replace(per_case_limits, tool_calls=0)
            case_ledger = PerCaseBudgetLedger(ledger, case_id, case_limits)
            try:
                result = coordinator._execute_validated_plan(plan=plan, case_id=case_id, ledger=case_ledger,
                    settings=settings, defer_cleanup=True)
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
                grader = score_archive_r6(plan["case"], archive, budget_snapshot=snap,
                    cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
                grade_path = batch_root / "scores" / f"{case_id}-code-grade-r6.json"
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
                _atomic_json(state_path, state_doc)
                stop_reason = batch_stop_reason_r6(grader, matrix_cases[case_id]["allowed_read_only_capabilities"])
                if stop_reason:
                    journal.append("BATCH_STOP", case_id=case_id, reason=stop_reason)
                    break
            except Exception as exc:
                captured = coordinator.capture_active_run_after_exception(case_id=case_id,
                    user_query=plan["message"], error_type=type(exc).__name__)
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
        print(json.dumps({"status":"BLOCKED_OR_STOPPED","reason_code":str(exc),
            "error_type":"D1PreflightError","agent_started":False,"run_created":False},ensure_ascii=False))
        raise SystemExit(2)
    except Exception as exc:
        # Do not print provider exceptions, configuration values, headers or URL credentials.
        print(json.dumps({"status":"BLOCKED_OR_STOPPED","reason_code":"UNEXPECTED_ERROR_REDACTED",
            "error_type":type(exc).__name__,"agent_started":False,"run_created":False},ensure_ascii=False))
        raise SystemExit(2)
