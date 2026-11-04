"""Exact r3.2.5 profile through the real CLI/app, with intercepted SDK transport."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from test_m6_2_personal_dev_batch_r7 import isolated_context, tmp_path, _InterceptedSDK
from app.m0 import config as config_module
from eval.m6_1.personal_dev import implementation_digest, plan_case, create_budget_ledger
from eval.m6_1.personal_dev_batch_m62 import derive_limits, score_archive_current, batch_stop_reason_r9
from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger
from scripts import run_m6_2_r324_personal_dev_batch_r7 as runner
from scripts import prepare_m6_2_r325_seven as prepare

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("query", ["What coupon discount can I use today?", "今天我的优惠券能用吗？"])
def test_missing_coupon_code_is_generic_zero_read_clarification(query):
    from app.m1.routing import decide_route
    route = decide_route(query, available_capabilities=[])
    assert route.strategy == "DIRECT" and route.needs_clarification
    assert route.required_capabilities == []
    assert route.reason_code == "MISSING_COUPON_IDENTIFIER"


def application_evidence(target, coordinator, monkeypatch):
    bundle = prepare.read(ROOT / runner.PROFILE_CONFIGS["r325-seven"]["dataset_rel"])
    sdk = _InterceptedSDK()
    create = sdk.chat.completions.create
    def recorded_usage(**kwargs):
        response = create(**kwargs)
        response.usage.total_tokens = 90
        return response
    sdk.chat.completions.create = recorded_usage
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(config_module.load_settings(require_api_key=False), api_key="offline-interceptor-only")
    limits, _ = derive_limits(7, per_case_overrides=runner.PROFILE_CONFIGS["r325-seven"]["per_case_limits"])
    ledger = create_budget_ledger(target, {"authorization_id": "offline-" + target.identity,
        "asset_sha256": runner.PROFILE_CONFIGS["r325-seven"]["dataset_sha256"], "case_ids": runner.R325_CASES,
        "database_identity": target.identity, "code_sha256": implementation_digest(ROOT), "git_commit": "OFFLINE",
        "data_version": bundle["version"], "authorization_type": "OFFLINE_TEST", "allowed_tool_capabilities": []},
        derive_limits(7, per_case_overrides=limits.to_dict())[1], settings)
    rows = []
    for case_id in runner.R325_CASES:
        plan = plan_case(bundle, case_id)
        before = len(sdk.requests)
        result = coordinator._execute_validated_plan(plan=plan, case_id=case_id,
            ledger=PerCaseBudgetLedger(ledger, case_id, replace(limits, tool_calls=len(plan["case"]["input"]["expected_tool_calls"]))),
            settings=settings, defer_cleanup=True)
        receipt = result.pop("_diagnostic_receipt")
        archive_path = target.root / result["evidence_path"]
        assert coordinator.diagnostic_cleanup_safety(receipt, archive_path)["safe_to_cleanup"]
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        archive = prepare.read(archive_path)
        grade = score_archive_current(plan["case"], archive, budget_snapshot=ledger.trial_snapshot(case_id),
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        if result["status"] != "SUCCEEDED":
            (target.root / "failed-application.json").write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf8")
        assert result["status"] == "SUCCEEDED", (case_id, result["failure_code"])
        (target.root / "last-grade.json").write_text(json.dumps(grade, ensure_ascii=False, indent=2), encoding="utf8")
        assert batch_stop_reason_r9(grade) is None, (case_id, grade)
        if case_id == "D1-POLICY-015":
            assert result["read_calls"] == 0
        rows.append({"case_id": case_id, "status": "APPLICATION_READY", "run_id": result["run_id"],
            "archive_path": archive_path.relative_to(ROOT).as_posix(), "archive_sha256": runner.digest(archive_path),
            "cleanup": cleanup, "intercepted_provider_requests": len(sdk.requests) - before,
            "grade": grade})
    return {"classification": "ISOLATED_ACTUAL_APPLICATION_WITH_INTERCEPTED_PROVIDER_NOT_REAL_TRIALS",
        "case_ids": runner.R325_CASES, "implementation_digest": implementation_digest(ROOT),
        "external_requests": 0, "records": rows}


@pytest.fixture
def frozen_cli(isolated_context, monkeypatch, tmp_path):
    for name in ("_ACTIVE_PROFILE", "PACKAGE_REL", "AUTH_TYPE", "PKG"):
        monkeypatch.setattr(runner, name, getattr(runner, name))
    # Historical official price is a fixture, not a live price refresh. Freeze
    # only this offline test's clock so old test grants remain reproducible.
    import eval.m6_1.personal_dev_authorization as authorization
    from datetime import datetime, timezone
    class OfflineClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
            return value if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(authorization, "datetime", OfflineClock)
    monkeypatch.setattr(prepare, "datetime", OfflineClock)
    monkeypatch.setattr(runner, "_utc_now", lambda: OfflineClock.now(timezone.utc))
    target, coordinator = isolated_context
    evidence = application_evidence(target, coordinator, monkeypatch)
    package = tmp_path / "cli-package"
    package.mkdir()
    profile = {**runner.PROFILE_CONFIGS["r325-seven"], "package": package.relative_to(ROOT).as_posix(),
        "matrix_rel": (package / "admission-matrix.json").relative_to(ROOT).as_posix()}
    monkeypatch.setitem(runner.PROFILE_CONFIGS, "r325-seven", profile)
    evidence_path = tmp_path / "application-evidence.json"
    prepare.write(evidence_path, evidence)
    monkeypatch.setattr(prepare, "load_settings", lambda **_k: replace(config_module.load_settings(require_api_key=False), api_key="offline-only"))
    prepare.freeze(evidence=evidence_path, target=target)
    monkeypatch.setattr(runner, "PersonalDevDatabase", lambda _root: target)
    monkeypatch.setattr(runner, "load_settings", lambda **_k: replace(config_module.load_settings(require_api_key=False), api_key="offline-only"))
    # Archives remain inside the isolated target; expose the repository-relative path to CLI.
    original = coordinator.__class__._execute_validated_plan
    def execute(self, **kwargs):
        result = original(self, **kwargs)
        result["evidence_path"] = (self.target.root / result["evidence_path"]).relative_to(ROOT).as_posix()
        return result
    monkeypatch.setattr(coordinator.__class__, "_execute_validated_plan", execute)
    yield package, target


def cli(monkeypatch, mode):
    monkeypatch.setattr(sys, "argv", ["batch", "--profile", "r325-seven", "--mode", mode])
    return runner.main()


def test_complete_seven_cli_admission_authorization_execute_cleanup_and_replay(frozen_cli, monkeypatch):
    package, target = frozen_cli
    assert cli(monkeypatch, "check-admission") == 0
    prepare.authorize()
    assert cli(monkeypatch, "execute") == 0
    closeout = prepare.read(package / "reports/closeout-r325.json")
    assert closeout["authorization_final_status"] == "CLOSED_COMPLETED"
    assert closeout["attempted_case_ids"] == runner.R325_CASES
    assert closeout["not_run_case_ids"] == []
    ledger = prepare.read(package / "task-cumulative-ledger.json")
    assert ledger["spent"]["application_runs"] == 27
    assert not any(ledger["reserved"].values())
    first = prepare.report()
    second = prepare.report()
    assert first == second and first["real_runs"] == 7
    derived = prepare.read(ROOT / first["path"])
    assert derived["raw_archive_code_grades_recomputed_equal"] == 7
    assert len(derived["coverage_rows"]) == 40
    assert derived["task_pass_fraction"]["N"] == 0  # pending semantics are not implicit failures
    with pytest.raises(Exception, match="ALREADY_STARTED|BINDING|REPLAY"):
        cli(monkeypatch, "execute")


def test_runtime_bytes_changed_before_cli_refused_without_run(frozen_cli, monkeypatch):
    package, target = frozen_cli
    original = runner.digest
    monkeypatch.setattr(runner, "digest", lambda path: "0" * 64 if Path(path) == ROOT / "app/m2/self_rag.py" else original(path))
    with pytest.raises(Exception, match="RUNTIME_DEPENDENCY"):
        cli(monkeypatch, "check-admission")
    assert not list(package.glob("execution-state-*.json"))


@pytest.mark.parametrize("change", ["extra", "reverse", "omit", "repeat"])
def test_exact_scope_refuses_other_case_or_order(frozen_cli, monkeypatch, change):
    package, _ = frozen_cli
    path = package / "manifest.json"
    manifest = prepare.read(path)
    ids = list(manifest["proposed_order"])
    if change == "extra": ids.append("D1-CROSSAGENT-033")
    elif change == "reverse": ids.reverse()
    elif change == "omit": ids.pop()
    else: ids[-1] = ids[0]
    manifest["proposed_order"] = ids
    path.write_text(json.dumps(manifest), encoding="utf8")
    with pytest.raises(Exception, match="CASE|SCOPE"):
        cli(monkeypatch, "check-admission")


def test_critical_unknown_budget_stops_later_cli_cases(frozen_cli, monkeypatch):
    package, _ = frozen_cli
    assert cli(monkeypatch, "check-admission") == 0
    prepare.authorize()
    original = runner.score_archive_current
    def unknown(*args, **kwargs):
        grade = original(*args, **kwargs)
        grade["program_checks"]["budget_and_cleanup"] = "UNKNOWN"
        grade["continue_decision"] = {"decision": "STOP", "blocking_items": ["budget_unknown"]}
        return grade
    monkeypatch.setattr(runner, "score_archive_current", unknown)
    assert cli(monkeypatch, "execute") == 0
    closeout = prepare.read(package / "reports/closeout-r325.json")
    assert len(closeout["attempted_case_ids"]) == 1
    assert len(closeout["not_run_case_ids"]) == 6
    assert closeout["authorization_final_status"] == "CLOSED_STOPPED"


def test_cli_model_capability_error_is_archived_zero_execution_then_continues(frozen_cli, monkeypatch):
    from types import SimpleNamespace
    package, _ = frozen_cli
    assert cli(monkeypatch, "check-admission") == 0
    prepare.authorize()
    sdk = _InterceptedSDK()
    original = sdk.create
    def create(*, messages, **kwargs):
        if "Classify the user's" in messages[0]["content"]:
            route = {"strategy": "AGENT", "sub_strategy": "AGENT+SINGLE", "intent_labels": ["model_error"],
                "safety_decision": "ALLOW", "reason_code": "WRONG_CAPABILITY", "needs_clarification": False,
                "clarification_question": None, "required_capabilities": ["cancel_order"]}
            return SimpleNamespace(model="offline-only", choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(route)))], usage=SimpleNamespace(
                    total_tokens=90, model_dump=lambda: {"prompt_tokens":70,"completion_tokens":20,"total_tokens":90}))
        response = original(messages=messages, **kwargs)
        response.usage.total_tokens = 90
        return response
    sdk.chat.completions.create = create
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    assert cli(monkeypatch, "execute") == 0
    report = prepare.read(package / "reports/case-results-r325.json")
    assert len(report["cases"]) == 7
    assert any(c["failure_classification"] == "KNOWN_SINGLE_MODEL_CAPABILITY_SELECTION_FAILURE" for c in report["cases"])
    assert all(c["cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED" for c in report["cases"])
