"""D1 personal Dev entry. Default is read-only preflight; execution is opt-in."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from eval.m6_1.personal_dev import (D1Coordinator, D1PreflightError, plan_case, OS_ISOLATION_STATUS,
    create_budget_ledger, implementation_digest)
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_authorization import (
    PersonalAuthorizationError, file_sha256, personal_diagnostic_profile,
)
from eval.m6_1.personal_dev_diagnostic_checks import diagnostic_cli_projection

ASSET_PATH = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
ASSET_HASH = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize-db", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--execute-batch", action="store_true")
    parser.add_argument("--check-admission", action="store_true")
    parser.add_argument("--check-batch-admission", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--grant", type=Path)
    parser.add_argument("--permit", type=Path)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--diagnostic-authorization", type=Path)
    parser.add_argument("--batch-budget", type=Path)
    args = parser.parse_args()
    if args.diagnostic_authorization is not None and (args.execute_batch or args.check_batch_admission):
        raise D1PreflightError("PERSONAL_DIAGNOSTIC_AND_BATCH_MODES_ARE_MUTUALLY_EXCLUSIVE")
    if hashlib.sha256(ASSET_PATH.read_bytes()).hexdigest() != ASSET_HASH:
        raise D1PreflightError("HISTORICAL_D1_ASSET_CHANGED")
    bundle = json.loads(ASSET_PATH.read_text(encoding="utf-8"))
    target = PersonalDevDatabase(ROOT)
    if sum((bool(args.execute), bool(args.execute_batch), bool(args.check_admission),
            bool(args.check_batch_admission))) > 1:
        raise D1PreflightError("CHOOSE_CHECK_OR_EXECUTE")
    if (not args.execute and not args.execute_batch and not args.check_admission and
            not args.check_batch_admission):
        if args.initialize_db:
            engine, _factory = target.open(initialize=True)
            engine.dispose()
        findings = []
        for case in bundle["cases"]:
            if args.case and args.case != case["case_id"]:
                continue
            try:
                plan_case(bundle, case["case_id"])
                findings.append({"case_id": case["case_id"], "fixture_preflight": "READY_FOR_ADMISSION_CHECK_ONLY"})
            except D1PreflightError as exc:
                findings.append({"case_id": case["case_id"], "fixture_preflight": str(exc)})
        if args.case and not findings:
            raise D1PreflightError("UNKNOWN_CASE")
        print(json.dumps({"mode": "PREFLIGHT_ONLY", "os_isolation": OS_ISOLATION_STATUS,
            "database": str(target.path), "database_initialized": target.path.exists(),
            "registry_grant": "NOT_AUTHORIZED", "runtime_authorization": "NOT_AUTHORIZED",
            "agent_started": False, "findings": findings}, ensure_ascii=False, indent=2))
        return 0
    if args.execute_batch or args.check_batch_admission:
        if (not args.diagnostic_authorization or not args.batch_budget or args.case or args.initialize_db or
                args.registry or args.grant or args.permit or args.budget):
            raise D1PreflightError("PERSONAL_BATCH_REQUIRES_EXACT_AUTH_AND_BUDGET_ONLY")
        batch_root = (ROOT / "docs/eval/personal-dev-batches").resolve()
        auth_path = args.diagnostic_authorization.resolve()
        budget_path = args.batch_budget.resolve()
        if (auth_path.name != "authorization.json" or budget_path.name != "budget.json" or
                auth_path.parent != budget_path.parent or auth_path.parent.parent != batch_root):
            raise D1PreflightError("PERSONAL_BATCH_AUTHORIZATION_PATH_MISMATCH")
        authorization = json.loads(auth_path.read_text(encoding="utf-8"))
        if auth_path.parent.name != authorization.get("authorization_id"):
            raise D1PreflightError("PERSONAL_BATCH_AUTHORIZATION_ID_PATH_MISMATCH")
        budget_config = json.loads(args.batch_budget.read_text(encoding="utf-8"))
        from app.m0.config import load_settings
        import subprocess
        settings = load_settings(require_api_key=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        engine, factory = target.open()
        coordinator = None
        try:
            coordinator = D1Coordinator(target, factory)
            if args.check_batch_admission:
                admission = coordinator.validate_personal_batch_admission(bundle=bundle,
                    authorization=authorization, budget_config=budget_config,
                    budget_file=args.batch_budget, settings=settings, code_revision=revision)
                print(json.dumps({"admission": "PERSONAL_BATCH_SCOPE_CHECKED",
                    "case_count": len(admission["case_ids"]), "database_identity": target.identity,
                    "agent_started": False, "run_created": False, "provider_requests": 0,
                    "grading_status": "PERSONAL_DEV_ONLY"}, ensure_ascii=False))
                return 0
            result = coordinator.execute_personal_batch(bundle=bundle, authorization=authorization,
                budget_config=budget_config, budget_file=args.batch_budget,
                settings=settings, code_revision=revision)
            authorization["status"] = ("CLOSED_COMPLETED" if result["summary"]["batch_status"] == "BATCH_COMPLETED"
                else "CLOSED_STOPPED")
            authorization["closed_at_utc"] = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
            auth_path.write_text(json.dumps(authorization, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": result["summary"]["batch_status"], "batch_dir": result["batch_dir"],
                "case_results": len(result["summary"]["records"]),
                "agent_started": any(row.get("agent_started") is True for row in result["summary"]["records"]),
                "grading_status": "PERSONAL_DEV_ONLY"}, ensure_ascii=False))
        finally:
            engine.dispose()
        return 0
    if args.diagnostic_authorization is not None:
        if (not args.case or not args.budget or args.initialize_db or
                not (args.check_admission or args.execute) or args.batch_budget or
                args.registry or args.grant or args.permit):
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_REQUIRES_EXACT_CASE_AUTH_BUDGET_AND_MODE")
        try:
            auth_path = args.diagnostic_authorization.resolve(strict=True)
            authorization_bytes = auth_path.read_bytes()
            authorization = json.loads(authorization_bytes.decode("utf-8"))
            profile = personal_diagnostic_profile(
                args.case, diagnostic_round_id=authorization.get("diagnostic_round_id"))
            budget_path = args.budget.resolve(strict=True)
            expected_auth_path = (ROOT / profile.authorization_path).resolve(strict=True)
            expected_budget_path = (ROOT / profile.budget_path).resolve(strict=True)
        except OSError as exc:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTH_OR_BUDGET_FILE_MISSING") from exc
        expected_state_path = ROOT / profile.execution_state_path
        if (expected_auth_path != (ROOT / profile.authorization_path).absolute() or
                auth_path != expected_auth_path or budget_path != expected_budget_path):
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTH_OR_BUDGET_PATH_MISMATCH")
        if expected_state_path.exists() or expected_state_path.is_symlink():
            raise D1PreflightError("PERSONAL_AUTHORIZATION_ALREADY_USED_NO_REPLAY")
        budget_bytes = budget_path.read_bytes()
        budget_config = json.loads(budget_bytes.decode("utf-8"))
        from eval.m6_1.budget import BudgetLimits
        from app.m0.config import load_settings
        import subprocess
        limits = BudgetLimits(**budget_config)
        settings = load_settings(require_api_key=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        engine, factory = target.open()
        try:
            coordinator = D1Coordinator(target, factory)
            if args.check_admission:
                plan = coordinator.validate_personal_diagnostic_admission(bundle=bundle,
                    case_id=args.case, authorization=authorization, limits=limits,
                    settings=settings, code_revision=revision, budget_path=budget_path,
                    authorization_path=auth_path)
                binding = plan["_personal_admission_binding"]
                if (file_sha256(auth_path) != binding["authorization_file_sha256"] or
                        file_sha256(budget_path) != binding["budget_file_sha256"] or
                        hashlib.sha256(authorization_bytes).hexdigest() != binding["authorization_file_sha256"] or
                        hashlib.sha256(budget_bytes).hexdigest() != binding["budget_file_sha256"]):
                    raise D1PreflightError("PERSONAL_DIAGNOSTIC_FILE_CHANGED_DURING_PREFLIGHT")
                print(json.dumps({"admission": "PERSONAL_DIAGNOSTIC_SCOPE_CHECKED",
                    "case_id": plan["case_id"], "authorization_id": authorization.get("authorization_id"),
                    "agent_started": False, "run_created": False, "provider_requests": 0,
                    "preflight_evidence": plan.get("_personal_admission_evidence"),
                    "execution_opportunity_consumed": False, "grading_status": "NOT_GRADED",
                    "registry_semantics_changed": False}, ensure_ascii=False))
                return 0
            result = coordinator.execute_personal_diagnostic(bundle=bundle,
                case_id=args.case, authorization=authorization, limits=limits,
                settings=settings, code_revision=revision, budget_path=budget_path,
                authorization_path=auth_path)
            diagnostic_summary = diagnostic_cli_projection(ROOT,
                result.get("diagnostic_check_report"), result.get("diagnostic_check_report_sha256"))
            print(json.dumps({"status": result["status"],
                "authorization_status": result["authorization_status"],
                "authorization_id": authorization.get("authorization_id"),
                "case_id": args.case, "run_id": result.get("run_id"),
                "run_status": result.get("run_status"), "run_created": bool(result.get("run_id")),
                "agent_started": result.get("agent_started"),
                "provider_requests": result.get("provider_requests"),
                "tool_calls": (result.get("budget_ledger") or {}).get("calls_by_category", {}).get("tool", "UNKNOWN"),
                "budget_ledger": result.get("budget_ledger"),
                "preflight_evidence": result.get("preflight_evidence"),
                "cleanup_status": result.get("cleanup_status"),
                "result_archive": result.get("result_archive"),
                "result_archive_sha256": result.get("result_archive_sha256"),
                **diagnostic_summary}, ensure_ascii=False))
        except Exception as exc:
            active = (coordinator.inspect_active_run_evidence() if coordinator is not None else
                {"agent_started": False, "run_created": False, "run_status": "UNKNOWN", "lease_active": "UNKNOWN"})
            print(json.dumps({"admission": "REJECTED" if args.check_admission else "EXECUTION_STOPPED",
                "reason": str(exc), "case_id": args.case,
                "authorization_id": authorization.get("authorization_id"),
                "failure_type": type(exc).__name__, "agent_started": active.get("agent_started", False),
                "run_created": active.get("run_created", False), "run_status": active.get("run_status", "UNKNOWN"),
                "lease_active": active.get("lease_active", "UNKNOWN"),
                "execution_opportunity_consumed": expected_state_path.exists(),
                "diagnostic_decision": "STOP", "blocking_item_count": 1, "review_item_count": 0,
                "diagnostic_check_report": "UNKNOWN", "diagnostic_check_report_sha256": "UNKNOWN",
                "diagnostic_report_integrity": "FAIL", "diagnostic_report_error": type(exc).__name__,
                "cleanup_status": active.get("lease_active") is True and "RETAINED_FOR_INVESTIGATION" or "UNKNOWN",
                "grading_status": "NOT_GRADED"}, ensure_ascii=False))
            return 2
        finally:
            engine.dispose()
        return 0

    if args.initialize_db or not all((args.case, args.registry, args.grant, args.permit, args.budget)):
        raise D1PreflightError("EXECUTE_REQUIRES_CASE_REGISTRY_GRANT_PERMIT_BUDGET; use separate personal authorization path")
    from eval.m6_1.registry import DatasetRegistry, AccessGrant, AccessGuard
    registry = DatasetRegistry.load_metadata(args.registry)
    asset = registry.get(bundle["asset_id"])
    if (asset.split != "dev" or asset.source_path != ASSET_PATH.relative_to(ROOT).as_posix() or
            asset.sha256 != ASSET_HASH or asset.version != bundle["version"]):
        raise D1PreflightError("REGISTRY_ASSET_SCOPE_MISMATCH")
    grant_data = json.loads(args.grant.read_text(encoding="utf-8"))
    for key in ("asset_ids", "allowed_splits"):
        grant_data[key] = tuple(grant_data[key])
    grant = AccessGrant(**grant_data)
    permit = json.loads(args.permit.read_text(encoding="utf-8"))
    if not permit.get("subject"):
        raise D1PreflightError("RUNTIME_SUBJECT_REQUIRED")
    guard = AccessGuard(registry, ROOT, ROOT / "docs/eval/personal-dev-runs/access-audit.jsonl")
    guard.read_cases(asset.asset_id, principal=permit["subject"], grant=grant)
    from eval.m6_1.budget import BudgetLimits
    from app.m0.config import load_settings
    import subprocess
    if (permit.get("git_commit") != subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() or
            permit.get("code_sha256") != implementation_digest(ROOT)):
        raise D1PreflightError("RUNTIME_CODE_REVISION_MISMATCH")
    engine, factory = target.open()
    try:
        settings = load_settings(require_api_key=True)
        ledger = create_budget_ledger(target, permit,
            BudgetLimits(**json.loads(args.budget.read_text(encoding="utf-8"))), settings)
        coordinator = D1Coordinator(target, factory)
        if args.check_admission:
            coordinator.validate_admission(bundle=bundle, case_id=args.case,
                asset=asset, grant=grant, permit=permit, ledger=ledger, settings=settings)
            print(json.dumps({"admission": "AUTHORIZED_SCOPE_CHECKED", "agent_started": False}))
            return 0
        result = coordinator.execute_authorized(bundle=bundle, case_id=args.case,
            asset=asset, grant=grant, permit=permit, ledger=ledger, settings=settings)
        print(json.dumps({"status": result["status"], "grading_status": "NOT_GRADED"}))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (D1PreflightError, PersonalAuthorizationError) as exc:
        print(json.dumps({"admission": "REJECTED", "reason": str(exc),
            "agent_started": False, "run_created": False}, ensure_ascii=False))
        raise SystemExit(2)
