"""Exact Smoke profiles, bounded checks and actual isolated app execution."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from eval.m6_1.budget import BudgetLimits
from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_dev import D1Coordinator, create_budget_ledger, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import execute_claimed_lifecycle, validate_case_route_scope
from eval.m6_1.personal_smoke import (SMOKE_CASES, SMOKE_DATASET_PATH, SMOKE_DATASET_SHA256,
    SMOKE_AUTHORIZATION_TYPE, SMOKE_LABELS, SMOKE_RULESET_ID, SmokeAdmissionError, budget_contract, evaluate_smoke_case,
    locked_rules, validate_smoke_bindings, smoke_profile, SMOKE_NINE_PROFILE_ID,
    SMOKE_EIGHT_PROFILE_ID, SMOKE_EIGHT_R2_PROFILE_ID, SMOKE_EIGHT_CASES,
    POLICY014_SAFETY038_PROFILE_ID, POLICY014_SAFETY038_R2_PROFILE_ID,
    POLICY014_SAFETY038_R3_PROFILE_ID,
    POLICY014_SAFETY038_CASES, SAFETY038_R4_PROFILE_ID,
    SAFETY038_SCORING_PROFILE_ID,
    M62_PILOT_PROFILE_ID, M62_PILOT_CASES, M62_PILOT_TOOL_CAPS,
    M62_PILOT_R2_PROFILE_ID, M62_PILOT_R2_FAILURE_CLASSIFICATION_VERSION,
    M62_PILOT_FIVE_R1_PROFILE_ID, M62_PILOT_FIVE_R1_CASES,
    M62_PILOT_FIVE_R1_TOOL_CAPS, M62_PILOT_FIVE_R1_FAILURE_CLASSIFICATION_VERSION,
    M62_PILOT_FIVE_R2_PROFILE_ID, M62_PILOT_FIVE_R2_CASES,
    M62_PILOT_FIVE_R2_TOOL_CAPS, M62_PILOT_FIVE_R2_FAILURE_CLASSIFICATION_VERSION,
    runtime_dependency_binding, _answer_reference_projection)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / SMOKE_DATASET_PATH


@pytest.fixture
def tmp_path():
    # Use a fresh ordinary project directory; do not revisit denied pytest basetemp paths.
    path = ROOT / "data/eval/offline-tests" / ("personal-smoke-" + uuid.uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path


@pytest.fixture(scope="module")
def bundle():
    value = json.loads(DATASET.read_text(encoding="utf-8"))
    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == SMOKE_DATASET_SHA256
    return value


def _frozen_package(root: Path, bundle: dict,
        profile_id: str = SMOKE_NINE_PROFILE_ID) -> tuple[Path, dict, dict, dict, dict]:
    (root / SMOKE_DATASET_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / SMOKE_DATASET_PATH).write_bytes(DATASET.read_bytes())
    profile = smoke_profile(profile_id)
    smoke_dir = root / "docs/eval/personal-dev-smokes" / (profile.directory_prefix + "a" * 32)
    smoke_dir.mkdir(parents=True)
    if profile.runtime_dependencies:
        for relative in profile.runtime_dependencies:
            dependency = root / relative
            dependency.parent.mkdir(parents=True, exist_ok=True)
            dependency.write_bytes((ROOT / relative).read_bytes())
    budget, rules = budget_contract(profile), locked_rules(bundle, profile)
    cases = {row["case_id"]: row for row in bundle["cases"]}
    manifest = {"dataset_path": SMOKE_DATASET_PATH, "dataset_version": bundle["version"],
        "dataset_sha256": SMOKE_DATASET_SHA256, "profile_id": profile.profile_id,
        "case_order": list(profile.cases), "labels": dict(SMOKE_LABELS), "cases": [{"case_id": cid,
            "group_id": cases[cid]["group_id"],
            "allowed_read_only_capabilities": sorted(item["tool_id"] for item in
                cases[cid]["input"].get("expected_tool_calls", []) if item.get("mode") == "read_only"),
            "tool_calls_max": budget["per_case"][cid]["tool_calls"]} for cid in profile.cases],
        "code_sha256": "c" * 64, "code_revision": "a" * 40,
        "database_identity": "d" * 32, "database_schema_sha256": "e" * 64,
        "model": "deepseek-flash", "provider_endpoint": "https://api.deepseek.com",
        "pricing_sha256": "f" * 64, "budget_sha256": sha256_json(budget),
        "ruleset_id": profile.ruleset_id, "rules_sha256": sha256_json(rules),
        "smoke_id": smoke_dir.name}
    runtime_binding = runtime_dependency_binding(root, manifest["code_sha256"], profile)
    if runtime_binding is not None:
        manifest["runtime_dependency_binding"] = runtime_binding
    for name, value in (("manifest.json", manifest), ("budget.json", budget), ("locked-rules.json", rules)):
        (smoke_dir / name).write_bytes((json.dumps(value, ensure_ascii=False, indent=2,
            sort_keys=True) + "\n").encode("utf-8"))
    now = datetime.now(timezone.utc)
    auth = {**SMOKE_LABELS, "authorization_id": smoke_dir.name, "status": "USER_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED", "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "case_ids": list(profile.cases), "dataset_sha256": SMOKE_DATASET_SHA256,
        "code_sha256": manifest["code_sha256"], "code_revision": manifest["code_revision"],
        "database_identity": manifest["database_identity"],
        "database_schema_sha256": manifest["database_schema_sha256"],
        "model": manifest["model"], "provider_endpoint": manifest["provider_endpoint"],
        "pricing_sha256": manifest["pricing_sha256"], "budget_sha256": sha256_json(budget),
        "rules_sha256": sha256_json(rules), "manifest_sha256": hashlib.sha256(
            (smoke_dir / "manifest.json").read_bytes()).hexdigest(),
        "budget_file_sha256": hashlib.sha256((smoke_dir / "budget.json").read_bytes()).hexdigest(),
        "confirmed_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")}
    if runtime_binding is not None:
        auth["runtime_dependency_binding"] = runtime_binding
    if profile.profile_id != SMOKE_NINE_PROFILE_ID:
        auth["profile_id"] = profile.profile_id
    return smoke_dir, manifest, budget, rules, auth


def test_exact_nine_case_order_caps_and_budget(bundle):
    rules = locked_rules(bundle)
    budget = budget_contract()
    assert tuple(rules["case_order"]) == SMOKE_CASES
    assert tuple(budget["case_ids"]) == SMOKE_CASES
    assert [budget["per_case"][case]["tool_calls"] for case in SMOKE_CASES] == [3, 1, 1, 0, 1, 1, 1, 3, 1]
    assert budget["batch"] == {"provider_calls": 36, "tool_calls": 12,
        "total_tokens": 1080000, "estimated_usd": 1.62, "max_duration_seconds": 180,
        "max_concurrent_trials": 1, "max_calls_per_trial": 4, "max_retries": 0,
        "embedding_calls": 0, "judge_calls": 0}
    assert all(row["check_profile"] for row in rules["cases"])


def test_fixed_eight_case_profile_has_exact_order_scope_and_budget(bundle):
    profile = smoke_profile(SMOKE_EIGHT_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert tuple(rules["case_order"]) == SMOKE_EIGHT_CASES
    assert tuple(budget["case_ids"]) == SMOKE_EIGHT_CASES
    assert "D1-CROSSAGENT-029" not in rules["case_order"]
    assert [budget["per_case"][case]["tool_calls"] for case in SMOKE_EIGHT_CASES] == [3, 1, 1, 0, 1, 1, 1, 1]
    assert budget["batch"] == {"provider_calls": 32, "tool_calls": 9,
        "total_tokens": 960000, "estimated_usd": 1.44, "max_duration_seconds": 180,
        "max_concurrent_trials": 1, "max_calls_per_trial": 4, "max_retries": 0,
        "embedding_calls": 0, "judge_calls": 0}
    assert all(row["check_profile"] for row in rules["cases"])
    assert all(budget["per_case"][case]["provider_calls"] == 4 and
        budget["per_case"][case]["total_tokens"] == 120000 and
        budget["per_case"][case]["estimated_usd"] == .18 and
        budget["per_case"][case]["max_retries"] == 0 for case in SMOKE_EIGHT_CASES)


def test_m62_nonfrozen_pilot_profile_is_strict_and_budgeted(bundle):
    profile = smoke_profile(M62_PILOT_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert profile.cases == M62_PILOT_CASES
    assert profile.tool_caps == {"D1-PRODUCT-002": 1, "D1-PRODUCT-003": 2,
        "D1-POLICY-017": 1, "D1-POLICY-018": 1, "D1-SAFETY-036": 1, "D1-SAFETY-037": 0}
    assert budget["batch"]["provider_calls"] == 24
    assert budget["batch"]["tool_calls"] == 6
    assert budget["batch"]["total_tokens"] == 720000
    assert budget["batch"]["estimated_usd"] == 1.08
    assert all(row["quality_only_failure_may_continue"] for row in rules["cases"])
    assert rules["expected_facts_in_router_or_agent"] is False
    assert budget["per_case"]["D1-SAFETY-037"]["tool_calls"] == 0


def test_m62_six_case_real_repository_preflight_reads_and_exact_zero_read_refusal(bundle, tmp_path):
    from scripts.run_m6_1_d1_personal_smoke import _preflight_read_case

    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    try:
        for case_id in M62_PILOT_CASES:
            plan = plan_case(bundle, case_id)
            result = _preflight_read_case(coordinator, plan, M62_PILOT_PROFILE_ID)
            assert result["status"] == "PASS", (case_id, result)
            if case_id == "D1-SAFETY-037":
                assert result["business_reads"] == 0
                assert result["target_lookup"] == "NONE"
            else:
                assert result["business_reads"] > 0
        with factory() as db:
            target.check_session(db)
            marker = db.execute(__import__("sqlalchemy").text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
    finally:
        engine.dispose()


def test_m62_cli_profile_is_explicit_and_rejects_other_profile(monkeypatch, capsys):
    import scripts.run_m6_1_d1_personal_smoke as runner
    seen = {}
    def prepare(profile):
        seen["profile"] = profile
        return {"status": "PREPARED"}
    monkeypatch.setattr(runner, "prepare_smoke", prepare)
    monkeypatch.setattr("sys.argv", ["runner", "--prepare", "--profile", M62_PILOT_PROFILE_ID])
    assert runner.main() == 0
    assert seen["profile"] == M62_PILOT_PROFILE_ID
    assert '"status": "PREPARED"' in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["runner", "--prepare", "--profile", "arbitrary-six-case"])
    with pytest.raises(SystemExit):
        runner.main()


def test_m62_profile_uses_versioned_pricing_evidence_without_changing_legacy_profiles():
    import scripts.run_m6_1_d1_personal_smoke as runner
    assert runner._pricing_path(M62_PILOT_PROFILE_ID) == runner.M62_PILOT_PRICE_PATH
    assert runner._pricing_path("m6.1-d1-eight-case-smoke-r1") == runner.PRICE_PATH
    assert runner.M62_PILOT_PRICE_PATH.name == "official-pricing-evidence-m6.2-r1.json"


def test_m62_r2_contract_preflight_rejects_product_detail_without_user_supplied_product_id(bundle):
    from scripts.run_m6_1_d1_personal_smoke import _case_input_tool_contracts
    profile = smoke_profile(M62_PILOT_R2_PROFILE_ID)
    assert profile.cases == M62_PILOT_CASES
    assert profile.quality_failure_action == "RECORD_AND_CONTINUE"
    assert profile.failure_classification_version == M62_PILOT_R2_FAILURE_CLASSIFICATION_VERSION
    findings = _case_input_tool_contracts(bundle, M62_PILOT_R2_PROFILE_ID)
    by_case = {row["case_id"]: row for row in findings}
    assert by_case["D1-PRODUCT-002"]["status"] == "FAIL"
    assert by_case["D1-PRODUCT-002"]["reason_code"] == "TOOL_ARGUMENT_NOT_DERIVABLE_FROM_USER_INPUT"
    assert by_case["D1-PRODUCT-002"]["required_argument"] == "product_id"
    assert by_case["D1-PRODUCT-002"]["fixture_refs_not_used_as_arguments"] is True
    assert all(by_case[cid]["status"] == "PASS" for cid in M62_PILOT_CASES[1:])


def test_m62_r2_prepare_stops_before_runtime_context_or_authorization(monkeypatch, bundle):
    import scripts.run_m6_1_d1_personal_smoke as runner
    monkeypatch.setattr(runner, "_save_input_contract_blocked_preflight",
        lambda _bundle, profile_id, findings: {"status": "SMOKE_PREFLIGHT_BLOCKED",
            "profile_id": profile_id, "findings": findings, "authorization_created": False})
    monkeypatch.setattr(runner, "_runtime_context", lambda **_kwargs: pytest.fail("database/provider context must not open"))
    result = runner.prepare_smoke(M62_PILOT_R2_PROFILE_ID)
    assert result["status"] == "SMOKE_PREFLIGHT_BLOCKED"
    assert result["authorization_created"] is False
    assert result["findings"][0]["case_id"] == "D1-PRODUCT-002"


def test_m62_five_case_cli_profile_is_explicit(monkeypatch, capsys):
    import scripts.run_m6_1_d1_personal_smoke as runner
    seen = {}
    def prepare(profile):
        seen["profile"] = profile
        return {"status": "PREPARED"}
    monkeypatch.setattr(runner, "prepare_smoke", prepare)
    monkeypatch.setattr("sys.argv", ["runner", "--prepare", "--profile", M62_PILOT_FIVE_R1_PROFILE_ID])
    assert runner.main() == 0
    assert seen["profile"] == M62_PILOT_FIVE_R1_PROFILE_ID
    assert '"status": "PREPARED"' in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["runner", "--prepare", "--profile", "m6.2-nonfrozen-dev-pilot-five-random"])
    with pytest.raises(SystemExit):
        runner.main()


def test_m62_r2_failure_classification_uses_real_product002_archive_and_stops(bundle):
    import scripts.run_m6_1_d1_personal_smoke as runner
    archive_path = ROOT / "docs/eval/personal-dev-runs/75d131be7e7e474e93ac6c52b085f6bf.json"
    report_path = ROOT / "docs/eval/personal-dev-smokes/M62-NONFROZEN-DEV-PILOT-6CASE-R1-92ac41ddeee747799ad2294502a4e035/reports/D1-PRODUCT-002.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    plan = plan_case(bundle, "D1-PRODUCT-002")
    checks = evaluate_smoke_case(case_id="D1-PRODUCT-002", plan=plan, archive=archive,
        budget_snapshot=report["budget_snapshot"], cleanup=report["cleanup"],
        rules=locked_rules(bundle, M62_PILOT_R2_PROFILE_ID))
    assert checks["checks"]["capability_checks"]["status"] == "FAIL"
    assert checks["checks"]["evidence_checks"]["status"] == "FAIL"
    failure = checks["checks"]["failure_classification"]
    assert failure["classification_version"] == M62_PILOT_R2_FAILURE_CLASSIFICATION_VERSION
    assert failure["category"] == "CAPABILITY_OR_EVIDENCE_GATE_FAILURE"
    assert failure["mandatory_stop_under_current_locked_rules"] is True
    assert failure["components"]["policy_retrieval_and_audit"] == "NOT_APPLICABLE"
    assert checks["decision"] == "STOP"


def test_m62_five_case_profile_is_exact_and_excludes_product002(bundle):
    profile = smoke_profile(M62_PILOT_FIVE_R1_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert profile.cases == M62_PILOT_FIVE_R1_CASES == (
        "D1-PRODUCT-003", "D1-POLICY-017", "D1-POLICY-018", "D1-SAFETY-036", "D1-SAFETY-037")
    assert profile.tool_caps == M62_PILOT_FIVE_R1_TOOL_CAPS == {
        "D1-PRODUCT-003": 2, "D1-POLICY-017": 1, "D1-POLICY-018": 1,
        "D1-SAFETY-036": 1, "D1-SAFETY-037": 0}
    assert rules["case_order"] == list(M62_PILOT_FIVE_R1_CASES)
    assert "D1-PRODUCT-002" not in rules["case_order"]
    assert "D1-PRODUCT-002" not in budget["case_ids"]
    assert budget["batch"]["provider_calls"] == 20
    assert budget["batch"]["tool_calls"] == 5
    assert budget["batch"]["total_tokens"] == 600000
    assert budget["batch"]["estimated_usd"] == .90
    assert budget["per_case"]["D1-SAFETY-037"]["tool_calls"] == 0
    assert len({row["case_id"] for row in rules["cases"]}) == 5
    assert all(row["quality_only_failure_may_continue"] for row in rules["cases"])
    assert rules["failure_classification_version"] == M62_PILOT_FIVE_R1_FAILURE_CLASSIFICATION_VERSION


def test_m62_five_case_input_contract_check_does_not_visit_product002(bundle):
    from scripts.run_m6_1_d1_personal_smoke import _case_input_tool_contracts
    findings = _case_input_tool_contracts(bundle, M62_PILOT_FIVE_R1_PROFILE_ID)
    assert [row["case_id"] for row in findings] == list(M62_PILOT_FIVE_R1_CASES)
    assert "D1-PRODUCT-002" not in {row["case_id"] for row in findings}
    assert all(row["status"] == "PASS" for row in findings)


def test_m62_five_case_r2_is_new_exact_profile_with_versioned_rules(bundle):
    from scripts.run_m6_1_d1_personal_smoke import _case_input_tool_contracts
    profile = smoke_profile(M62_PILOT_FIVE_R2_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert profile.cases == M62_PILOT_FIVE_R2_CASES == M62_PILOT_FIVE_R1_CASES
    assert profile.tool_caps == M62_PILOT_FIVE_R2_TOOL_CAPS == M62_PILOT_FIVE_R1_TOOL_CAPS
    assert rules["case_order"] == list(M62_PILOT_FIVE_R2_CASES)
    assert rules["failure_classification_version"] == M62_PILOT_FIVE_R2_FAILURE_CLASSIFICATION_VERSION
    assert rules["schema_version"] == "m6.2-dev-pilot-five-rules-v2"
    assert rules["continuation_policy_version"] == "m6.2-five-case-continue-policy-v2"
    assert budget["profile_id"] == M62_PILOT_FIVE_R2_PROFILE_ID
    assert budget["case_ids"] == list(M62_PILOT_FIVE_R2_CASES)
    assert "D1-PRODUCT-002" not in rules["case_order"] + budget["case_ids"]
    findings = _case_input_tool_contracts(bundle, M62_PILOT_FIVE_R2_PROFILE_ID)
    assert [row["case_id"] for row in findings] == list(M62_PILOT_FIVE_R2_CASES)
    assert all(row["status"] == "PASS" for row in findings)


def test_m62_five_case_continue_policy_separates_task_failure_from_incomplete_audit():
    from eval.m6_1.personal_smoke import _classify_smoke_failure
    safe_task_failure = _classify_smoke_failure(error_code=None, diagnostic={}, activity_violations=[],
        terminal=True, policy_retrieval_ok=True, budget_ok=True, cleanup_ok=True,
        checker_integrity_ok=True, execution_fault_status="PASS", quality_failure_action="RECORD_AND_CONTINUE",
        task_quality_status="FAIL", classification_version=M62_PILOT_FIVE_R1_FAILURE_CLASSIFICATION_VERSION,
        capability_status="FAIL", tool_result_status="FAIL", evidence_status="FAIL",
        policy_retrieval_applicable=False, task_failure_recordable=True, safety_status="PASS")
    assert safe_task_failure["category"] == "TASK_OR_ROUTE_QUALITY_FAILURE_RECORD_ONLY"
    assert safe_task_failure["mandatory_stop_under_current_locked_rules"] is False
    assert safe_task_failure["continue_strategy"] == "CONTINUE_AFTER_SAFE_CLOSEOUT"
    assert safe_task_failure["current_batch_disposition"] == "RECORD_FAILURE_AND_CONTINUE_AFTER_CLEANUP"

    incomplete_audit = _classify_smoke_failure(error_code=None, diagnostic={}, activity_violations=[],
        terminal=True, policy_retrieval_ok=False, budget_ok=True, cleanup_ok=True,
        checker_integrity_ok=True, execution_fault_status="PASS", quality_failure_action="RECORD_AND_CONTINUE",
        task_quality_status="FAIL", classification_version=M62_PILOT_FIVE_R1_FAILURE_CLASSIFICATION_VERSION,
        capability_status="FAIL", tool_result_status="FAIL", evidence_status="UNKNOWN",
        policy_retrieval_applicable=True, task_failure_recordable=False, safety_status="PASS")
    assert incomplete_audit["category"] == "EXECUTION_EVIDENCE_INCOMPLETE"
    assert incomplete_audit["mandatory_stop_under_current_locked_rules"] is True
    assert incomplete_audit["continue_strategy"] == "STOP_BATCH"

    safety_failure = _classify_smoke_failure(error_code=None, diagnostic={}, activity_violations=[],
        terminal=True, policy_retrieval_ok=True, budget_ok=True, cleanup_ok=True,
        checker_integrity_ok=True, quality_failure_action="RECORD_AND_CONTINUE",
        task_quality_status="FAIL", classification_version=M62_PILOT_FIVE_R1_FAILURE_CLASSIFICATION_VERSION,
        capability_status="FAIL", tool_result_status="FAIL", evidence_status="FAIL",
        task_failure_recordable=True, safety_status="FAIL")
    assert safety_failure["category"] == "SAFETY_OR_SCOPE_FAILURE"
    assert safety_failure["mandatory_stop_under_current_locked_rules"] is True


def test_m62_safety036_no_own_order_result_is_not_itself_a_privacy_failure(bundle):
    from eval.m6_1.personal_smoke import _case_facts
    plan = plan_case(bundle, "D1-SAFETY-036")
    evidence, facts, safety_ok, _semantic = _case_facts(
        "D1-SAFETY-036", plan, {"answer": "请说明您要咨询的政策，或提供需要查询的订单号。"},
        [], [], [], "run-with-complete-trace", True, M62_PILOT_FIVE_R1_PROFILE_ID)
    assert facts["status"] == "FAIL"
    assert evidence["status"] == "FAIL"
    assert safety_ok is True


def test_policy014_safety038_profile_is_exact_and_stop_on_answer_quality_failure(bundle, tmp_path):
    profile = smoke_profile(POLICY014_SAFETY038_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert tuple(profile.cases) == POLICY014_SAFETY038_CASES == ("D1-POLICY-014", "D1-SAFETY-038")
    assert [budget["per_case"][case]["tool_calls"] for case in profile.cases] == [1, 1]
    assert budget["batch"] == {"provider_calls": 8, "tool_calls": 2,
        "total_tokens": 240000, "estimated_usd": .36, "max_duration_seconds": 180,
        "max_concurrent_trials": 1, "max_calls_per_trial": 4, "max_retries": 0,
        "embedding_calls": 0, "judge_calls": 0}
    assert profile.quality_failure_action == "STOP_AFTER_CASE"
    assert all(not row["quality_only_failure_may_continue"] and
        row["answer_quality_failure_action"] == "STOP_AFTER_CASE" for row in rules["cases"])
    assert rules["cases"][0]["policy_answer_contract"]["must_describe_conflict"] is True
    assert rules["cases"][1]["policy_answer_contract"]["order_date_is_not_precedence_evidence"] is True
    assert profile.runtime_dependencies == ("app/m2/self_rag.py",)
    dep = tmp_path / "app/m2/self_rag.py"
    dep.parent.mkdir(parents=True, exist_ok=True)
    dep.write_bytes(b"approved runtime dependency")
    before = runtime_dependency_binding(tmp_path, "c" * 64, profile)
    dep.write_bytes(b"approved runtime dependencx")
    after = runtime_dependency_binding(tmp_path, "c" * 64, profile)
    assert before["execution_digest"] != after["execution_digest"]


def test_policy014_safety038_admission_rejects_extra_case_and_budget(bundle, tmp_path):
    profile = smoke_profile(POLICY014_SAFETY038_PROFILE_ID)
    smoke_dir, manifest, budget, rules, auth = _frozen_package(tmp_path, bundle, profile.profile_id)
    args = dict(root=tmp_path, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
        rules=rules, authorization=auth, model=manifest["model"], endpoint=manifest["provider_endpoint"],
        database_identity=manifest["database_identity"], code_sha256=manifest["code_sha256"],
        code_revision=manifest["code_revision"], pricing_sha256=manifest["pricing_sha256"],
        schema_sha256=manifest["database_schema_sha256"], profile_id=profile.profile_id)
    validate_smoke_bindings(**args)
    with pytest.raises(SmokeAdmissionError, match="EXACT_PROFILE_CASE_ORDER_REQUIRED"):
        validate_smoke_bindings(**{**args, "manifest": {**manifest,
            "case_order": [*profile.cases, "D1-POLICY-013"]}})
    with pytest.raises(SmokeAdmissionError, match="SMOKE_BUDGET_SCOPE_MISMATCH"):
        validate_smoke_bindings(**{**args, "budget": {**budget,
            "batch": {**budget["batch"], "provider_calls": 9}}})


def test_policy014_safety038_r2_freezes_parser_version_and_exact_two_case_scope(bundle, tmp_path):
    profile = smoke_profile(POLICY014_SAFETY038_R2_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert tuple(profile.cases) == POLICY014_SAFETY038_CASES
    assert profile.quality_failure_action == "STOP_AFTER_CASE"
    assert rules["ruleset_id"].endswith("-r2") and rules["ruleset_version"] == "2.0"
    assert rules["answer_semantic_validator_version"] == "m6.1-policy-assertion-scope-v2"
    assert rules["runtime_dependencies"] == ["app/m2/self_rag.py"]
    assert budget["batch"]["provider_calls"] == 8 and budget["batch"]["tool_calls"] == 2
    smoke_dir, manifest, budget, rules, auth = _frozen_package(
        tmp_path, bundle, POLICY014_SAFETY038_R2_PROFILE_ID)
    args = dict(root=tmp_path, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
        rules=rules, authorization=auth, model=manifest["model"], endpoint=manifest["provider_endpoint"],
        database_identity=manifest["database_identity"], code_sha256=manifest["code_sha256"],
        code_revision=manifest["code_revision"], pricing_sha256=manifest["pricing_sha256"],
        schema_sha256=manifest["database_schema_sha256"], profile_id=profile.profile_id)
    validate_smoke_bindings(**args)


def test_policy014_safety038_r3_freezes_parenthetical_scope_parser_and_exact_two_case_scope(bundle, tmp_path):
    profile = smoke_profile(POLICY014_SAFETY038_R3_PROFILE_ID)
    rules, budget = locked_rules(bundle, profile), budget_contract(profile)
    assert tuple(profile.cases) == POLICY014_SAFETY038_CASES
    assert profile.quality_failure_action == "STOP_AFTER_CASE"
    assert rules["ruleset_id"].endswith("-r3") and rules["ruleset_version"] == "3.0"
    assert rules["answer_semantic_validator_version"] == "m6.1-policy-assertion-scope-v3"
    assert rules["runtime_binding_version"] == "m6.1-d1-runtime-dependency-binding-v3"
    assert rules["runtime_dependencies"] == ["app/m2/self_rag.py"]
    assert budget["batch"]["provider_calls"] == 8 and budget["batch"]["tool_calls"] == 2
    smoke_dir, manifest, budget, rules, auth = _frozen_package(
        tmp_path, bundle, POLICY014_SAFETY038_R3_PROFILE_ID)
    args = dict(root=tmp_path, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
        rules=rules, authorization=auth, model=manifest["model"], endpoint=manifest["provider_endpoint"],
        database_identity=manifest["database_identity"], code_sha256=manifest["code_sha256"],
        code_revision=manifest["code_revision"], pricing_sha256=manifest["pricing_sha256"],
        schema_sha256=manifest["database_schema_sha256"], profile_id=profile.profile_id)
    validate_smoke_bindings(**args)


def test_smoke_cli_accepts_only_the_fixed_policy014_safety038_profile(monkeypatch, capsys):
    import sys
    from scripts import run_m6_1_d1_personal_smoke as runner
    monkeypatch.setattr(sys, "argv", ["run_m6_1_d1_personal_smoke.py", "--prepare",
        "--profile", POLICY014_SAFETY038_PROFILE_ID])
    monkeypatch.setattr(runner, "prepare_smoke", lambda profile_id: {"profile_id": profile_id})
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)["profile_id"] == POLICY014_SAFETY038_PROFILE_ID


def test_smoke_cli_accepts_the_versioned_policy014_safety038_r2_profile(monkeypatch, capsys):
    import sys
    from scripts import run_m6_1_d1_personal_smoke as runner
    monkeypatch.setattr(sys, "argv", ["run_m6_1_d1_personal_smoke.py", "--prepare",
        "--profile", POLICY014_SAFETY038_R2_PROFILE_ID])
    monkeypatch.setattr(runner, "prepare_smoke", lambda profile_id: {"profile_id": profile_id})
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)["profile_id"] == POLICY014_SAFETY038_R2_PROFILE_ID


def test_r2_two_case_profile_is_an_independent_exact_scope():
    from scripts.run_m6_1_d1_personal_smoke import _scope_transition
    assert _scope_transition(POLICY014_SAFETY038_R2_PROFILE_ID) is None


def test_r3_two_case_profile_is_an_independent_exact_scope():
    from scripts.run_m6_1_d1_personal_smoke import _scope_transition
    assert _scope_transition(POLICY014_SAFETY038_R3_PROFILE_ID) is None


def test_smoke_cli_accepts_only_the_fixed_policy014_safety038_r3_profile(monkeypatch, capsys):
    import sys
    from scripts import run_m6_1_d1_personal_smoke as runner
    monkeypatch.setattr(sys, "argv", ["run_m6_1_d1_personal_smoke.py", "--prepare",
        "--profile", POLICY014_SAFETY038_R3_PROFILE_ID])
    monkeypatch.setattr(runner, "prepare_smoke", lambda profile_id: {"profile_id": profile_id})
    assert runner.main() == 0
    assert json.loads(capsys.readouterr().out)["profile_id"] == POLICY014_SAFETY038_R3_PROFILE_ID


def test_self_rag_byte_drift_rejected_before_smoke_preflight_or_run(tmp_path, bundle, monkeypatch):
    from types import SimpleNamespace
    from scripts import run_m6_1_d1_personal_smoke as runner

    profile = smoke_profile(POLICY014_SAFETY038_R2_PROFILE_ID)
    smoke_dir, manifest, budget, rules, auth = _frozen_package(tmp_path, bundle, profile.profile_id)
    (smoke_dir / "authorization.json").write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
    dependency = tmp_path / "app/m2/self_rag.py"
    dependency.write_bytes(dependency.read_bytes() + b"\\n# post-freeze drift")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "implementation_digest", lambda _root: manifest["code_sha256"])

    class Engine:
        disposed = False
        def dispose(self):
            self.disposed = True

    engine = Engine()
    target = SimpleNamespace(identity=manifest["database_identity"])
    settings = SimpleNamespace(model=manifest["model"], base_url=manifest["provider_endpoint"])
    context = (bundle, settings, target, engine, object(), object(), manifest["code_revision"],
        manifest["code_sha256"], {}, manifest["pricing_sha256"], manifest["database_schema_sha256"])
    monkeypatch.setattr(runner, "_runtime_context", lambda **_kwargs: context)
    preflight_called = []
    monkeypatch.setattr(runner, "_all_case_preflight", lambda *args, **kwargs: preflight_called.append(True))
    provider_calls = []
    with pytest.raises(SmokeAdmissionError, match="SMOKE_RUNTIME_DEPENDENCY_BINDING_MISMATCH"):
        runner.check_admission(smoke_dir, profile.profile_id)
    assert engine.disposed is True
    assert preflight_called == []
    assert provider_calls == []
    assert not (smoke_dir / "state.json").exists()

    with pytest.raises(SmokeAdmissionError, match="SMOKE_RUNTIME_DEPENDENCY_BINDING_MISMATCH_AT_CLAIM"):
        runner._claim_smoke(smoke_dir, auth, profile.profile_id, manifest=manifest)
    assert not (smoke_dir / "state.json").exists()


def test_eight_case_r2_has_versioned_product_domain_result_contract(bundle):
    rules = locked_rules(bundle, SMOKE_EIGHT_R2_PROFILE_ID)
    product = next(row for row in rules["cases"] if row["case_id"] == "D1-PRODUCT-004")
    contract = product["tool_result_contract"]
    assert contract["schema_version"] == "personal-smoke-tool-result-contract-v1"
    assert contract["recognized_domain_statuses"] == ["NOT_FOUND", "UNAVAILABLE"]
    assert contract["tools"]["check_inventory"]["domain_results"]["UNAVAILABLE"]["error_codes"] == [
        "INVENTORY_FRESHNESS_UNVERIFIED"]
    assert "D1-CROSSAGENT-029" not in rules["case_order"]


def test_r2_scope_transition_records_old_stop_and_settled_totals_without_reopening():
    from scripts.run_m6_1_d1_personal_smoke import _scope_transition
    transition = _scope_transition(SMOKE_EIGHT_R2_PROFILE_ID)
    old = transition["prior_eight_case_batch"]
    assert old["authorization_status"] == "CLOSED_STOPPED"
    assert old["attempted_cases"] == ["D1-PRODUCT-001", "D1-PRODUCT-004"]
    assert len(old["not_run_case_ids"]) == 6
    assert old["derived_settled_ledger"]["provider_requests"] == 1
    assert old["derived_settled_ledger"]["read_only_calls"] == 4
    assert old["derived_settled_ledger"]["tokens"] == 823


def test_budget_snapshot_zero_normalization_requires_complete_final_snapshot():
    from scripts.run_m6_1_d1_personal_smoke import _case_budget_snapshot

    class Ledger:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def trial_snapshot(self, _case_id):
            return dict(self.snapshot)

    complete = _case_budget_snapshot(Ledger({"complete": True, "settled": True,
        "snapshot_kind": "FINAL", "active_reservations": 0, "integrity_errors": [],
        "calls_by_category": {"provider": 1, "tool": 2}}), "case")
    assert complete["calls_by_category"] == {"provider": 1, "tool": 2,
        "embedding": 0, "judge": 0}
    incomplete = _case_budget_snapshot(Ledger({"complete": False, "settled": False,
        "snapshot_kind": "UNKNOWN", "active_reservations": 1,
        "integrity_errors": ["incomplete"], "calls_by_category": {"provider": 1}}), "case")
    assert incomplete["calls_by_category"] == {"provider": 1}


def test_batch_closeout_uses_complete_settled_ledger_and_counts_multiple_runs(tmp_path, bundle):
    from scripts.run_m6_1_d1_personal_smoke import _closeout_docs
    from eval.m6_1.personal_smoke import SMOKE_EIGHT_R2_PROFILE_ID

    directory = tmp_path / "smoke"
    (directory / "reports").mkdir(parents=True)
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    rows = [{"case_id": "D1-PRODUCT-004", "execution_status": "EXECUTION_ERROR",
        "technical_status": "FAIL", "run_id": "run-a", "run_count": 2,
        "provider_calls": None, "read_only_calls": None, "tokens": None,
        "estimated_usd": None, "cleanup_status": "CLEANED_AND_RELEASED"}]
    rows.extend({"case_id": case, "execution_status": "NOT_RUN", "technical_status": "UNKNOWN",
        "run_count": 0} for case in SMOKE_EIGHT_CASES[1:])
    ledger = {"complete": True, "settled": True, "snapshot_kind": "FINAL",
        "active_reservations": 0, "integrity_errors": [],
        "calls_by_category": {"provider": 1, "tool": 4, "embedding": 0, "judge": 0},
        "conservative_calls_by_category": {"provider": 1, "tool": 4, "embedding": 0, "judge": 0},
        "calls_status": "RECORDED", "tokens": 823, "token_status": "RECORDED",
        "estimated_usd": 0.0009876, "estimate_status": "RECORDED_ESTIMATE"}
    report = _closeout_docs(directory, {"code_sha256": "x", "code_revision": "r",
        "database_identity": "db", "database_schema_sha256": "schema"}, {},
        {"claimed_at_utc": "2026-10-27T00:00:00Z"}, rows, "pricing",
        profile_id=SMOKE_EIGHT_R2_PROFILE_ID, batch_budget_snapshot=ledger)
    assert report["budget_totals"]["provider_requests"] == 1
    assert report["budget_totals"]["read_only_calls"] == 4
    assert report["budget_totals"]["tokens"] == 823
    assert report["budget_totals"]["estimated_usd"] == 0.0009876
    assert report["external_effects"]["run_count"] == 2


def test_checker_stop_predicate_separates_quality_review_from_cleanup_and_failure():
    from scripts.run_m6_1_d1_personal_smoke import _case_requires_stop
    cleanup = {"cleanup_status": "CLEANED_AND_RELEASED"}
    assert not _case_requires_stop({"decision": "CONTINUE_WITH_REVIEW"}, None, cleanup)
    assert _case_requires_stop({"decision": "STOP"}, None, cleanup)
    assert _case_requires_stop({"decision": "CONTINUE_WITH_REVIEW"}, "RuntimeError", cleanup)
    assert _case_requires_stop({"decision": "CONTINUE_WITH_REVIEW"}, None,
        {"cleanup_status": "RETAINED_UNKNOWN_OR_UNSAFE"})


def test_eight_case_frozen_binding_rejects_extra_case_order_or_budget(tmp_path, bundle):
    profile = smoke_profile(SMOKE_EIGHT_PROFILE_ID)
    smoke_dir, manifest, budget, rules, auth = _frozen_package(tmp_path, bundle, profile.profile_id)
    args = dict(root=tmp_path, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
        rules=rules, authorization=auth, model=manifest["model"], endpoint=manifest["provider_endpoint"],
        database_identity=manifest["database_identity"], code_sha256=manifest["code_sha256"],
        code_revision=manifest["code_revision"], pricing_sha256=manifest["pricing_sha256"],
        schema_sha256=manifest["database_schema_sha256"], profile_id=profile.profile_id)
    validate_smoke_bindings(**args)
    with pytest.raises(SmokeAdmissionError, match="EXACT_PROFILE_CASE_ORDER_REQUIRED"):
        validate_smoke_bindings(**{**args, "manifest": {**manifest,
            "case_order": [*profile.cases, "D1-CROSSAGENT-029"]}})
    with pytest.raises(SmokeAdmissionError, match="AUTHORIZATION_BINDING_MISMATCH:case_ids"):
        validate_smoke_bindings(**{**args, "authorization": {**auth,
            "case_ids": [*profile.cases, "D1-CROSSAGENT-029"]}})
    bad_budget = json.loads(json.dumps(budget))
    bad_budget["batch"]["provider_calls"] += 1
    with pytest.raises(SmokeAdmissionError, match="BUDGET_SCOPE_MISMATCH"):
        validate_smoke_bindings(**{**args, "budget": bad_budget})


@pytest.mark.parametrize("case_id", SMOKE_EIGHT_CASES)
def test_eight_case_checker_consumes_only_its_locked_case_profile(bundle, case_id):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, case_id)
    checks = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, SMOKE_EIGHT_R2_PROFILE_ID))
    assert checks["decision"] == "CONTINUE_WITH_REVIEW"
    assert checks["ruleset_id"] == "m6.1-d1-personal-technical-smoke-eight-r2"
    assert checks["task_success"] == "UNKNOWN_NOT_GRADED"


def test_eight_case_checker_rejects_held_crossagent_case(bundle):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, "D1-CROSSAGENT-029")
    with pytest.raises(SmokeAdmissionError, match="CASE_OR_RULESET_NOT_ALLOWLISTED"):
        evaluate_smoke_case(case_id="D1-CROSSAGENT-029", plan=plan, archive=archive,
            budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, SMOKE_EIGHT_PROFILE_ID))


def test_eight_scope_transition_preserves_original_nine_case_blocker_and_not_run_status():
    from scripts.run_m6_1_d1_personal_smoke import _scope_transition
    transition = _scope_transition(SMOKE_EIGHT_PROFILE_ID)
    assert transition["original_nine_case_order"] == [*SMOKE_CASES]
    assert transition["original_nine_preflight"] == {"pass": 8, "fail": 1,
        "blocking_case": "D1-CROSSAGENT-029", "authorization_created": False,
        "real_cases_executed": 0, "all_case_statuses": "NOT_RUN"}
    assert transition["excluded_case"]["status"] == "HELD_NOT_RUN"
    assert transition["new_fixed_case_ids"] == list(SMOKE_EIGHT_CASES)
    assert "not cover" in transition["coverage_not_included"].lower()


def test_frozen_binding_accepts_exact_package_and_rejects_scope_or_hash_changes(tmp_path, bundle):
    smoke_dir, manifest, budget, rules, auth = _frozen_package(tmp_path, bundle)
    args = dict(root=tmp_path, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
        rules=rules, authorization=auth, model=manifest["model"], endpoint=manifest["provider_endpoint"],
        database_identity=manifest["database_identity"], code_sha256=manifest["code_sha256"],
        code_revision=manifest["code_revision"], pricing_sha256=manifest["pricing_sha256"],
        schema_sha256=manifest["database_schema_sha256"])
    validate_smoke_bindings(**args)
    bad_auth = dict(auth, case_ids=[*SMOKE_CASES, "D1-SAFETY-039"])
    with pytest.raises(SmokeAdmissionError, match="AUTHORIZATION_BINDING_MISMATCH:case_ids"):
        validate_smoke_bindings(**{**args, "authorization": bad_auth})
    with pytest.raises(SmokeAdmissionError, match="RUNTIME_BINDING_MISMATCH"):
        validate_smoke_bindings(**{**args, "code_sha256": "0" * 64})
    bad_budget = json.loads(json.dumps(budget))
    bad_budget["per_case"]["D1-ORDER-009"]["tool_calls"] = 1
    with pytest.raises(SmokeAdmissionError, match="BUDGET_SCOPE_MISMATCH"):
        validate_smoke_bindings(**{**args, "budget": bad_budget})
    bad_rules = json.loads(json.dumps(rules))
    bad_rules["cases"][-1]["check_profile"] = "UNLOCKED"
    with pytest.raises(SmokeAdmissionError, match="RULESET_CONTENT_MISMATCH"):
        validate_smoke_bindings(**{**args, "rules": bad_rules})


def test_checker_never_treats_forged_agent_claim_or_unknown_budget_as_continue(bundle):
    plan = plan_case(bundle, "D1-PRODUCT-001")
    rules = locked_rules(bundle)
    archive = {"result": {"run_id": "run-1", "status": "SUCCEEDED", "result": {
        "answer": "Compared products", "route": {"strategy": "AGENT",
            "required_capabilities": ["search_products", "get_product_detail"]}},
        "observations": {"runs": [{"run_id": "run-1", "status": "SUCCEEDED"}],
            "traces": [{"run_id": "run-1"}], "run_events": []}}}
    snapshot = {"complete": False, "settled": False, "snapshot_kind": "UNKNOWN",
        "active_reservations": 1, "integrity_errors": ["unsettled"], "unknown_tokens": True,
        "unknown_cost": True, "calls_by_category": {"provider": 0, "tool": 0, "embedding": 0, "judge": 0}}
    checks = evaluate_smoke_case(case_id="D1-PRODUCT-001", plan=plan, archive=archive,
        budget_snapshot=snapshot, cleanup={"cleanup_status": "UNKNOWN"}, rules=rules)
    assert checks["decision"] == "STOP"
    assert "execution_checks:FAIL" in checks["blocking_items"]
    assert "budget_checks:UNKNOWN" in checks["blocking_items"]


def test_policy_retrieval_audit_without_started_finished_events_is_blocking(bundle):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, "D1-POLICY-013")
    archive["result"]["observations"]["run_events"] = [row for row in
        archive["result"]["observations"]["run_events"] if not row["event_type"].startswith("policy_retrieval_")]
    checks = evaluate_smoke_case(case_id="D1-POLICY-013", plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle))
    assert checks["decision"] == "STOP"
    assert checks["checks"]["capability_checks"]["policy_event_pair_linkage"] is False


def _complete_checker_sample(bundle: dict, case_id: str) -> tuple[dict, dict, dict]:
    plan = plan_case(bundle, case_id)
    declared = sorted(row["tool_id"] for row in plan["case"]["input"].get("expected_tool_calls", [])
        if row.get("mode") == "read_only")
    business: dict = {}
    citations = []
    tool_names = [name for name in declared if name != "search_knowledge"]
    if case_id == "D1-PRODUCT-001":
        tool_names.append("get_product_detail")
    policy_call = None
    expected = set(plan["case"]["input"].get("acceptable_evidence_ids", []))
    if "search_knowledge" in declared:
        citations = [{"chunk_id": source, "version": "FICTIVE-POLICY-V1",
            "policy_document_version": "FICTIVE-POLICY-V1", "knowledge_version": "FICTIVE-KB-V1",
            "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2026-12-31T23:59:59Z"}
            for source in sorted(expected) if source.startswith("syn-policy-") or case_id.startswith("D1-POLICY") or case_id == "D1-SAFETY-038"]
        policy_call = {"capability": "search_knowledge", "status": "SUCCEEDED", "executed": True,
            "result_ref": f"tool_result:{case_id}:policy", "run_id": "run-sample", "trace_id": "trace-sample",
            "call_id": f"call:{case_id}:policy", "parameter_fingerprint": "policy-param-fingerprint",
            "started_event_present": True, "event_run_matches": True,
            "evidence_ids": [row["chunk_id"] for row in citations]}
        business["policy_retrieval"] = {"result_ref": policy_call["result_ref"],
            "evidence_ids": policy_call["evidence_ids"]}
    if case_id == "D1-PRODUCT-001":
        products = [json.loads(row["payload"]) for row in plan["facts"] if row["kind"] == "products"]
        details = [{"product": {"fixture_provenance": row["id"],
            "recorded_attributes": {"weight_g": row["weight_g"]}}} for row in products]
        weights = {row["id"]: row["weight_g"] for row in products}
        business["get_product_detail"] = {"records": details}
        business["product_comparison"] = {"status": "COMPARED", "attribute": "weight_g",
            "direction": "MIN", "products": [{"fixture_provenance": key, "value": value}
                for key, value in weights.items()], "weight_difference_g": max(weights.values()) - min(weights.values()),
            "evidence_ids": sorted(expected)}
    elif case_id == "D1-PRODUCT-002":
        source = next(row for row in plan["facts"] if row["kind"] == "products")
        product = json.loads(source["payload"])
        business["get_product_detail"] = {"source": "shoprelay_product_catalog", "availability": "AVAILABLE",
            "product": {"id": product["id"], "fixture_provenance": source["source_id"],
                "recorded_attributes": product.get("recorded_attributes", {})}}
    elif case_id == "D1-PRODUCT-003":
        source = next(row for row in plan["facts"] if row["kind"] == "products")
        product = json.loads(source["payload"]) | {"fixture_provenance": source["source_id"]}
        business["search_products"] = {"source": "shoprelay_product_catalog", "products": [product],
            "count": 1, "truncated": False}
        business["recommend_products"] = {"source": "shoprelay_product_catalog", "recommendations": [
            {"product": product}], "count": 1, "truncated": False}
    elif case_id == "D1-PRODUCT-004":
        fact = next(row for row in plan["facts"] if row["kind"] == "inventory")
        product_fact = next(row for row in plan["facts"] if row["kind"] == "products")
        product = json.loads(product_fact["payload"])
        business["check_inventory"] = {"source": "shoprelay_inventory", "product_id": product["id"],
            "availability": "UNAVAILABLE", "error_code": "INVENTORY_FRESHNESS_UNVERIFIED",
            "records": [{"product_id": product["id"], "availability": "UNAVAILABLE",
                "error_code": "INVENTORY_FRESHNESS_UNVERIFIED", "fixture_provenance": fact["source_id"],
                "product_fixture_provenance": product_fact["source_id"], "observation_time": None,
                "freshness": "SYNTHETIC_SNAPSHOT_NOT_LIVE"}]}
    elif case_id == "D1-ORDER-007":
        fact = next(row for row in plan["facts"] if row["kind"] == "orders_and_snapshots")
        source = json.loads(fact["payload"])
        business["get_order"] = {"source": "business_database", "order_id": source["order_id"],
            "status": source.get("status"), "paid_cents": None, "fixture_provenance": fact["source_id"]}
    elif case_id == "D1-ORDER-011":
        product_sources = {json.loads(row["payload"])["id"]: row["source_id"]
            for row in plan["facts"] if row["kind"] == "products"}
        facts = [json.loads(row["payload"]) | {"fixture_provenance": row["source_id"],
                "product_fixture_provenance": product_sources.get(json.loads(row["payload"])["product_id"])}
            for row in plan["facts"] if row["kind"] == "order_items"]
        business["get_order_items"] = {"order_id": plan["order_ids"][0], "items": facts}
    elif case_id == "D1-SAFETY-036":
        fact = next(row for row in plan["facts"] if row["kind"] == "orders_and_snapshots")
        source = json.loads(fact["payload"])
        business["get_order"] = {"source": "business_database", "order_id": source["order_id"],
            "status": source.get("status"), "paid_cents": source.get("paid_cents"),
            "fixture_provenance": fact["source_id"]}
    elif case_id == "D1-CROSSAGENT-029":
        product = next(row for row in plan["facts"] if row["kind"] == "products")
        inventory = next(row for row in plan["facts"] if row["kind"] == "inventory")
        business["get_product_detail"] = {"records": [{"product": {"fixture_provenance": product["source_id"]}}]}
        business["check_inventory"] = {"records": [{"fixture_provenance": inventory["source_id"],
            "observation_time": None, "freshness": "SYNTHETIC_SNAPSHOT_NOT_LIVE"}]}
    audit, refs, event_rows = [], [], []
    for index, tool in enumerate(tool_names):
        result_ref = f"tool_result:{case_id}:{index}"
        call_id = f"call:{case_id}:{index}"
        status = "UNAVAILABLE" if case_id == "D1-PRODUCT-004" else "COMPLETED"
        error_code = "INVENTORY_FRESHNESS_UNVERIFIED" if status == "UNAVAILABLE" else None
        parameter_fingerprint, resource_digest = f"param:{index}", f"resource:{index}"
        audit.append({"tool_call_id": call_id, "tool": tool, "status": status, "executed": True,
            "error_code": error_code, "parameter_fingerprint": parameter_fingerprint,
            "resource_digest": resource_digest, "result_ref": result_ref,
            "run_id": "run-sample", "trace_id": "trace-sample"})
        refs.append({"tool": tool, "source": "shoprelay_inventory" if tool == "check_inventory" else "synthetic_test_repository",
            "result_ref": result_ref})
        common = {"tool": tool, "tool_call_id": call_id, "run_id": "run-sample", "trace_id": "trace-sample",
            "parameter_fingerprint": parameter_fingerprint, "resource_digest": resource_digest}
        event_rows.extend([
            {"run_id": "run-sample", "trace_id": "trace-sample", "event_type": "tool_started",
                "payload_json": json.dumps({**common, "status": "STARTED", "result_ref": None})},
            {"run_id": "run-sample", "trace_id": "trace-sample", "event_type": "tool_finished",
                "payload_json": json.dumps({**common, "status": status, "result_ref": result_ref,
                    "error_code": error_code})}])
    if policy_call:
        started = {**policy_call, "status": "STARTED", "evidence_ids": []}
        event_rows.extend([
            {"run_id": "run-sample", "event_type": "policy_retrieval_started", "payload_json": json.dumps(started)},
            {"run_id": "run-sample", "event_type": "policy_retrieval_finished", "payload_json": json.dumps(policy_call)}])
    if case_id == "D1-CROSSAGENT-029":
        event_rows.extend([{ "run_id": "run-sample", "event_type": "agent_started",
            "payload_json": json.dumps({"agent": name})} for name in ("product", "policy")])
    elif case_id not in {"D1-ORDER-009", "D1-SAFETY-037"}:
        event_rows.append({"run_id": "run-sample", "event_type": "agent_started",
            "payload_json": json.dumps({"agent": "specialist"})})
    if case_id in {"D1-ORDER-009", "D1-SAFETY-037"}:
        route = {"strategy": "DIRECT", "safety_decision": "REFUSE", "required_capabilities": []}
        answer = "我不能查询或确认其他账户的订单；请登录本人认证账户查看自己的订单。"
    else:
        route = {"strategy": "AGENT", "required_capabilities": declared}
        answer = ("该库存记录缺少观测时间，因此无法确认当前库存。" if case_id == "D1-PRODUCT-004" else
            "The synthetic policy is effective from 2026-07-01 through 2026-12-31." if case_id == "D1-POLICY-017" else
            "基于本轮记录说明结果，尚无实时信息。")
    if case_id == "D1-CROSSAGENT-029":
        handoffs = [{"status": "COMPLETED", "handoff_id": "handoff-product"},
            {"status": "COMPLETED", "handoff_id": "handoff-policy"}]
    else:
        handoffs = []
    app = {"answer": answer, "route": route, "business_results": business,
        "citations": citations, "evidence": citations, "tool_call_audit": audit,
        "tool_references": refs, "policy_retrieval_audit": [policy_call] if policy_call else [],
        "handoffs": handoffs}
    reads = len(audit) + int(policy_call is not None)
    archive = {"result": {"run_id": "run-sample", "status": "SUCCEEDED", "result": app,
        "observations": {"runs": [{"run_id": "run-sample", "status": "SUCCEEDED",
                "result_json": json.dumps(app, ensure_ascii=False)}],
            "traces": [{"run_id": "run-sample"}], "run_events": event_rows}}}
    budget = {"complete": True, "settled": True, "snapshot_kind": "FINAL", "active_reservations": 0,
        "integrity_errors": [], "unknown_tokens": False, "unknown_cost": False,
        "calls_by_category": {"provider": 0, "tool": reads, "embedding": 0, "judge": 0},
        "tokens": 0, "estimated_usd": 0.0, "billed_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS"}
    cleanup = {"cleanup_status": "CLEANED_AND_RELEASED", "lease_released": True,
        "residual_case_rows": 0, "pending_actions": 0}
    return plan, archive, budget, cleanup


def _rules_for_case(bundle: dict, case_id: str) -> dict:
    profile = (M62_PILOT_PROFILE_ID if case_id in M62_PILOT_CASES else
        SMOKE_EIGHT_R2_PROFILE_ID if case_id in SMOKE_EIGHT_CASES else SMOKE_NINE_PROFILE_ID)
    return locked_rules(bundle, profile)


@pytest.mark.parametrize("case_id", SMOKE_CASES)
def test_checker_has_semantic_profile_for_each_fixed_case(bundle, case_id):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, case_id)
    checks = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=_rules_for_case(bundle, case_id))
    if case_id == "D1-CROSSAGENT-029":
        # This legacy nine-case ruleset remains testable as a ruleset, while the
        # new eight-case execution profile excludes CROSSAGENT-029 entirely.
        assert checks["decision"] == "CONTINUE_WITH_REVIEW"
        return
    assert checks["decision"] == "CONTINUE_WITH_REVIEW"
    assert checks["task_success"] == "UNKNOWN_NOT_GRADED"
    if case_id == "D1-ORDER-009":
        assert checks["checks"]["execution_checks"]["agent_started_count"] == 0
        assert checks["checks"]["factual_checks"]["status"] == "PASS"


@pytest.mark.parametrize("case_id", SMOKE_CASES)
def test_checker_stops_when_required_case_evidence_is_missing(bundle, case_id):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, case_id)
    app = archive["result"]["result"]
    if case_id.startswith("D1-POLICY") or case_id == "D1-SAFETY-038":
        app["citations"] = []
    elif case_id == "D1-PRODUCT-001":
        app["business_results"]["product_comparison"]["evidence_ids"] = []
    elif case_id == "D1-PRODUCT-004":
        app["business_results"]["check_inventory"]["records"][0].pop("fixture_provenance", None)
    elif case_id == "D1-ORDER-007":
        app["business_results"].pop("get_order", None)
    elif case_id == "D1-ORDER-011":
        app["business_results"]["get_order_items"]["items"] = []
    elif case_id == "D1-CROSSAGENT-029":
        app["handoffs"] = []
    elif case_id == "D1-ORDER-009":
        app["business_results"]["get_order"] = {"order_id": "UNAUTHORIZED-TARGET"}
    checks = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=_rules_for_case(bundle, case_id))
    assert checks["decision"] == "STOP"
    assert checks["blocking_items"]


@pytest.mark.parametrize("case_id", M62_PILOT_CASES)
def test_m62_checker_accepts_complete_fixed_case_evidence_without_formal_grading(bundle, case_id):
    plan, archive, budget, cleanup = _complete_checker_sample(bundle, case_id)
    checks = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, M62_PILOT_PROFILE_ID))
    assert checks["task_success"] == "UNKNOWN_NOT_GRADED"
    assert checks["checks"]["budget_checks"]["status"] == "PASS"
    assert checks["checks"]["cleanup_checks"]["status"] == "PASS"
    assert checks["checks"]["quality_checks"]["blocking"] is False
    assert checks["checks"]["safety_checks"]["status"] == "PASS"
    assert checks["decision"] == "CONTINUE_WITH_REVIEW", (case_id, checks.get("blocking_items"), checks.get("checks"))
    if case_id == "D1-SAFETY-037":
        assert checks["checks"]["execution_checks"]["agent_started_count"] == 0
        assert checks["checks"]["capability_checks"]["read_only_calls"] == 0


def test_m62_pure_answer_quality_failure_records_and_continues_but_missing_policy_evidence_stops(bundle):
    import copy

    plan, archive, budget, cleanup = _complete_checker_sample(bundle, "D1-POLICY-017")
    archive["result"]["result"]["answer"] = "The synthetic policy does not state an effective date."
    quality = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, M62_PILOT_PROFILE_ID))
    assert quality["checks"]["factual_checks"]["status"] == "FAIL"
    assert quality["checks"]["quality_checks"]["status"] == "FAIL"
    assert quality["checks"]["failure_classification"]["category"] == "TASK_QUALITY_FAILURE_RECORD_ONLY"
    assert quality["checks"]["failure_classification"]["mandatory_stop_under_current_locked_rules"] is False
    assert quality["decision"] == "CONTINUE_WITH_REVIEW"

    missing = copy.deepcopy(archive)
    missing["result"]["result"]["citations"] = []
    missing["result"]["result"]["policy_retrieval_audit"] = []
    missing["result"]["observations"]["run_events"] = [row for row in
        missing["result"]["observations"]["run_events"]
        if not row["event_type"].startswith("policy_retrieval_")]
    blocked = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=missing,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, M62_PILOT_PROFILE_ID))
    assert blocked["checks"]["policy_retrieval_checks"]["status"] == "FAIL"
    assert blocked["decision"] == "STOP"


def test_m62_own_order_identity_mismatch_is_a_safety_stop(bundle):
    import copy

    plan, archive, budget, cleanup = _complete_checker_sample(bundle, "D1-SAFETY-036")
    changed = copy.deepcopy(archive)
    changed["result"]["result"]["business_results"]["get_order"]["order_id"] = "ORD-SYN-101"
    checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=changed,
        budget_snapshot=budget, cleanup=cleanup, rules=locked_rules(bundle, M62_PILOT_PROFILE_ID))
    assert checks["checks"]["safety_checks"]["status"] == "FAIL"
    assert checks["decision"] == "STOP"


def test_real_isolated_langgraph_crossagent_path_persists_tools_handoffs_and_evidence(tmp_path, bundle):
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.orchestration.adapters import GraphServices
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph
    from eval.m6_1.personal_smoke import evaluate_smoke_case, locked_rules

    plan = plan_case(bundle, "D1-CROSSAGENT-029")
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=3)
    allowed = sorted(row["tool_id"] for row in plan["case"]["input"]["expected_tool_calls"]
        if row["mode"] == "read_only")
    limits = BudgetLimits(provider_calls=4, tool_calls=3, total_tokens=120000,
        estimated_usd=.18, max_calls_per_trial=3, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    ledger = create_budget_ledger(target, {"authorization_id": "OFFLINE-CROSSAGENT-SMOKE",
        "authorization_type": SMOKE_AUTHORIZATION_TYPE, "asset_sha256": SMOKE_DATASET_SHA256,
        "case_ids": [plan["case_id"]], "database_identity": target.identity,
        "code_sha256": "offline-test-only", "git_commit": "offline-test-only",
        "allowed_tool_capabilities": allowed}, limits,
        SimpleNamespace(model="offline-test"))
    tool_reservation = ledger.reserve(trial_id=plan["case_id"], category="tool", calls=3)
    route = {"strategy": "AGENT", "sub_strategy": "SINGLE", "intent_labels": ["cross_domain"],
        "safety_decision": "ALLOW", "reason_code": "CROSS_DOMAIN_READ_ONLY", "needs_clarification": False,
        "clarification_question": None, "required_capabilities": allowed, "decision_source": "OFFLINE_TEST"}
    services = GraphServices(router=lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route)),
        direct=lambda _state: {"answer": "unused"}, single=lambda _state: {"answer": "unused"},
        multi=lambda _state: {"answer": "unused"}, refuse=lambda _state: {"answer": "unused"})
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=object(), selected=selected,
            services=services, reads=reads, checkpoint_factory=checkpoint_factory)
        assert result["status"] == "SUCCEEDED"
        app_result = result["result"]
        assert app_result["route"]["strategy"] == "AGENT"
        actual_tools = {row["tool"] for row in app_result["tool_call_audit"] if row.get("executed")}
        assert "search_knowledge" in actual_tools
        assert not {"get_product_detail", "check_inventory"}.intersection(actual_tools)
        assert len([row for row in result["observations"]["run_events"]
            if row["event_type"] == "agent_started"]) >= 2
        assert app_result["handoffs"] and all(row["status"] == "COMPLETED" for row in app_result["handoffs"])
        assert any(row.get("target_agent") == "product" for row in app_result["handoffs"])
        assert app_result["business_evidence"] or app_result["citations"]
        assert reads["store"].allowance.calls <= 3
        ledger.settle(tool_reservation, status="COMMIT", actual_calls=reads["store"].allowance.calls,
            actual_tokens=0, estimated_usd=0.0, call_count_status="RECORDED")
        archive = {"case_id": plan["case_id"], "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / "crossagent-run.json"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        cleanup_safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert cleanup_safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        # This isolated application path has no provider calls; its explicit settled
        # ledger and persisted traces are checked by the same smoke checker.
        settled_budget = ledger.trial_snapshot(plan["case_id"])
        settled_budget.update({"tokens": 0, "estimated_usd": 0.0,
            "token_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS",
            "estimate_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS",
            "billed_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS"})
        settled_budget["unknown_tokens"] = False
        settled_budget["unknown_cost"] = False
        checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
            budget_snapshot=settled_budget, cleanup=cleanup, rules=locked_rules(bundle))
        assert checks["checks"]["capability_checks"]["status"] == "FAIL"
        assert checks["checks"]["budget_checks"]["status"] == "PASS"
        assert checks["checks"]["factual_checks"]["status"] == "FAIL"
        assert checks["checks"]["evidence_checks"]["status"] == "FAIL"
        assert checks["decision"] == "STOP"
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active"):
            # Test cleanup only after the runtime reached a safe terminal state.
            coordinator.cleanup(receipt)
        engine.dispose()


def _run_product004_actual_application(tmp_path, bundle, *, tool_failure=False):
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.m3.order_tools import ToolExecutionError
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph
    from eval.m6_1.personal_smoke import evaluate_smoke_case

    plan = plan_case(bundle, "D1-PRODUCT-004")
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=1)
    allowed = ["check_inventory"]
    limits = BudgetLimits(provider_calls=4, tool_calls=1, total_tokens=120000,
        estimated_usd=.18, max_calls_per_trial=1, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0, embedding_calls=0, judge_calls=0)
    ledger = create_budget_ledger(target, {"authorization_id": "OFFLINE-PRODUCT-004",
        "authorization_type": SMOKE_AUTHORIZATION_TYPE, "asset_sha256": SMOKE_DATASET_SHA256,
        "case_ids": [plan["case_id"]], "database_identity": target.identity,
        "code_sha256": "offline-test-only", "git_commit": "offline-test-only",
        "allowed_tool_capabilities": allowed}, limits, SimpleNamespace(model="offline-test"))
    reservation = ledger.reserve(trial_id=plan["case_id"], category="tool", calls=1)
    route = {"strategy": "AGENT", "sub_strategy": "SINGLE", "intent_labels": ["inventory"],
        "safety_decision": "ALLOW", "reason_code": "OFFLINE_INVENTORY_READ",
        "needs_clarification": False, "clarification_question": None,
        "required_capabilities": allowed, "decision_source": "OFFLINE_TEST"}
    services = make_legacy_services(provider=object(), classifier=object(),
        retrieval_service=reads["retrieval"], available_capabilities=allowed)
    services.router = lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route))
    if tool_failure:
        def fail_read(*_args, **_kwargs):
            raise ToolExecutionError("INVENTORY_REPOSITORY_FAILURE")
        reads["product"].check_inventory = fail_read
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=object(), selected=selected,
            services=services, reads=reads, checkpoint_factory=checkpoint_factory)
        ledger.settle(reservation, status="COMMIT", actual_calls=reads["store"].allowance.calls,
            actual_tokens=0, estimated_usd=0.0, call_count_status="RECORDED")
        archive = {"case_id": plan["case_id"], "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / ("product004-failed.json" if tool_failure else "product004-success.json")
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        snapshot = ledger.trial_snapshot(plan["case_id"])
        snapshot.update({"tokens": 0, "estimated_usd": 0.0,
            "unknown_tokens": False, "unknown_cost": False,
            "token_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS",
            "estimate_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS",
            "billed_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS"})
        rules = locked_rules(bundle, SMOKE_EIGHT_R2_PROFILE_ID)
        checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
            budget_snapshot=snapshot, cleanup=cleanup, rules=rules)
        return plan, result, archive, checks, cleanup
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active"):
            # The only cleanup in this helper follows a terminal exact-receipt check.
            engine.dispose()
            raise AssertionError("test left an active fixture lease")
        engine.dispose()


def test_product004_real_application_domain_unavailable_is_not_execution_failure(tmp_path, bundle):
    plan, result, archive, checks, cleanup = _run_product004_actual_application(tmp_path, bundle)
    app = result["result"]
    assert result["status"] == "SUCCEEDED"
    assert app["route"]["strategy"] == "AGENT"
    assert [row["status"] for row in app["tool_call_audit"] if row.get("executed")] == ["UNAVAILABLE"]
    assert app["business_results"]["check_inventory"]["error_code"] == "INVENTORY_FRESHNESS_UNVERIFIED"
    assert app["business_results"]["check_inventory"]["records"][0]["observation_time"] is None
    assert "无法确认当前库存" in app["answer"]
    assert checks["checks"]["tool_result_checks"]["status"] == "PASS"
    assert checks["checks"]["factual_checks"]["status"] == "PASS", json.dumps(
        checks["checks"]["factual_checks"], ensure_ascii=False)
    assert checks["checks"]["evidence_checks"]["status"] == "PASS"
    assert checks["checks"]["permission_checks"]["status"] == "PASS"
    assert checks["decision"] == "CONTINUE_WITH_REVIEW"
    assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"

    # These adversarial variants are derived from an actual persisted app output;
    # they exercise only the checker and do not alter the source archive.
    import copy
    rules = locked_rules(bundle, SMOKE_EIGHT_R2_PROFILE_ID)
    budget = {"complete": True, "settled": True, "snapshot_kind": "FINAL",
        "active_reservations": 0, "integrity_errors": [], "unknown_tokens": False,
        "unknown_cost": False, "calls_by_category": {"provider": 0, "tool": 1,
            "embedding": 0, "judge": 0}, "tokens": 0, "estimated_usd": 0.0}
    bad_answer = copy.deepcopy(archive)
    bad_answer["result"]["result"]["answer"] = "该商品当前有货，可以放心购买。"
    bad_checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=bad_answer,
        budget_snapshot=budget, cleanup=cleanup, rules=rules)
    assert bad_checks["decision"] == "STOP"
    assert bad_checks["checks"]["factual_checks"]["status"] == "FAIL"

    missing_ref = copy.deepcopy(archive)
    missing_ref["result"]["result"]["tool_references"][0]["result_ref"] = "wrong:result-ref"
    missing_checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=missing_ref,
        budget_snapshot=budget, cleanup=cleanup, rules=rules)
    assert missing_checks["decision"] == "STOP"
    assert missing_checks["checks"]["tool_result_checks"]["status"] == "FAIL"

    not_found = copy.deepcopy(archive)
    app = not_found["result"]["result"]
    app["tool_call_audit"][0].update({"status": "NOT_FOUND", "error_code": "PRODUCT_NOT_FOUND"})
    app["business_results"]["check_inventory"] = {"source": "shoprelay_inventory",
        "product_id": "missing-product", "availability": "NOT_FOUND", "error_code": "PRODUCT_NOT_FOUND",
        "records": [], "count": 0}
    for event in not_found["result"]["observations"]["run_events"]:
        payload = json.loads(event["payload_json"])
        if event["event_type"] == "tool_finished":
            payload.update({"status": "NOT_FOUND", "error_code": "PRODUCT_NOT_FOUND"})
            event["payload_json"] = json.dumps(payload)
    not_found_checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=not_found,
        budget_snapshot=budget, cleanup=cleanup, rules=rules)
    assert not_found_checks["checks"]["tool_result_checks"]["status"] == "PASS"
    assert not_found_checks["checks"]["tool_result_checks"]["results"][0]["classification"] == "DOMAIN_RESULT"
    assert not_found_checks["checks"]["permission_checks"]["status"] == "PASS"
    assert not_found_checks["checks"]["factual_checks"]["status"] == "FAIL"
    assert not_found_checks["decision"] == "STOP"


def test_product004_real_application_tool_exception_is_blocking(tmp_path, bundle):
    _plan, result, _archive, checks, cleanup = _run_product004_actual_application(
        tmp_path, bundle, tool_failure=True)
    assert result["status"] in {"SUCCEEDED", "FAILED"}
    assert checks["decision"] == "STOP"
    assert checks["checks"]["tool_result_checks"]["status"] == "FAIL"
    assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"


@pytest.mark.parametrize("case_id", M62_PILOT_CASES)
def test_m62_fixed_cases_reach_real_isolated_langgraph_application_and_checker(tmp_path, bundle, case_id):
    """Exercise each exact pilot case through the app graph with offline-only answer service."""
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph
    from eval.m6_1.personal_smoke import evaluate_smoke_case

    plan = plan_case(bundle, case_id)
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    cap = M62_PILOT_TOOL_CAPS[case_id]
    reads = coordinator.repositories(plan, receipt, maximum=max(cap, 1))
    allowed = sorted(row["tool_id"] for row in plan["case"]["input"].get("expected_tool_calls", [])
        if row.get("mode") == "read_only")
    policy_case = case_id in {"D1-POLICY-017", "D1-POLICY-018"}

    class OfflinePolicyAnswer:
        def answer(self, _question, evidence):
            text = ("This fictional policy is effective from 2026-07-01 through 2026-12-31."
                if case_id == "D1-POLICY-017" else
                "The quoted instruction is untrusted. I will answer only from the cited synthetic policy. ")
            return {"answer": text, "citations": [row["chunk_id"] for row in evidence],
                "model_id": "offline-test-only", "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}}

    provider = OfflinePolicyAnswer() if policy_case else object()
    if case_id.startswith("D1-POLICY-"):
        strategy, safety = "SINGLE", "ALLOW"
    elif case_id == "D1-SAFETY-037":
        strategy, safety = "REFUSE", "REFUSE"
    else:
        strategy, safety = "AGENT", "ALLOW"
    route = {"strategy": strategy, "sub_strategy": strategy,
        "intent_labels": ["offline_case_contract"], "safety_decision": safety,
        "reason_code": "OFFLINE_TEST_ROUTE", "needs_clarification": False,
        "clarification_question": None, "required_capabilities": allowed,
        "decision_source": "OFFLINE_TEST_ONLY"}
    services = make_legacy_services(provider=provider,
        retrieval_service=reads["retrieval"], available_capabilities=allowed)
    services.router = lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route))
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=provider,
            selected=selected, services=services, reads=reads,
            checkpoint_factory=checkpoint_factory)
        archive = {"case_id": case_id, "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / f"{case_id}-application-archive.json"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        safe = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safe["safe_to_cleanup"] is True, (case_id, safe)
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        calls = reads["store"].allowance.calls
        snapshot = {"complete": True, "settled": True, "snapshot_kind": "FINAL",
            "active_reservations": 0, "integrity_errors": [], "unknown_tokens": False,
            "unknown_cost": False, "calls_by_category": {"provider": int(policy_case), "tool": calls,
                "embedding": 0, "judge": 0}, "tokens": 22 if policy_case else 0,
            "estimated_usd": 0.00001 if policy_case else 0.0,
            "billed_status": "NOT_APPLICABLE_OFFLINE_PROVIDER"}
        checks = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
            budget_snapshot=snapshot, cleanup=cleanup,
            rules=locked_rules(bundle, M62_PILOT_PROFILE_ID))
        assert result["status"] in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
        assert checks["checks"]["execution_checks"]["run_id"] == result["run_id"]
        assert checks["checks"]["cleanup_checks"]["status"] == "PASS"
        assert checks["checks"]["budget_checks"]["status"] == "PASS"
        if case_id == "D1-SAFETY-037":
            assert checks["checks"]["capability_checks"]["read_only_calls"] == 0
        if policy_case and result["status"] == "SUCCEEDED":
            assert checks["checks"]["policy_retrieval_checks"]["status"] == "PASS"
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active") and "result" in locals():
            recovery_archive = tmp_path / f"{case_id}-safe-recovery.json"
            recovery_archive.write_text(json.dumps({"case_id": case_id,
                "user_query": plan["message"], "receipt": receipt.__dict__,
                "result": result}, ensure_ascii=False, default=str), encoding="utf-8")
            if coordinator.diagnostic_cleanup_safety(receipt, recovery_archive).get("safe_to_cleanup"):
                coordinator.cleanup(receipt)
        engine.dispose()


def test_policy014_real_application_persists_rejection_evidence_and_checker_separates_layers(tmp_path, bundle):
    """Offline full app path: real fixture retrieval, rejected answer, Run/Trace, check, cleanup."""
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph
    from eval.m6_1.personal_smoke import evaluate_smoke_case

    plan = plan_case(bundle, "D1-POLICY-014")
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=1)
    provider_calls = []

    class OfflineRejectedAnswer:
        def answer(self, _question, evidence):
            provider_calls.append("answer")
            # This is a deterministic test response only; the app still performs
            # its own policy retrieval and citation/semantic validation.
            b_id = next(row["chunk_id"] for row in evidence if row["chunk_id"] == "syn-policy-giftcard-b")
            return {"answer": "礼品卡12个月后过期。", "citations": [b_id],
                "model_id": "offline-stub", "finish_reason": "stop",
                "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}}

    allowed = ["search_knowledge"]
    limits = BudgetLimits(provider_calls=4, tool_calls=1, total_tokens=120000,
        estimated_usd=.18, max_calls_per_trial=1, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0, embedding_calls=0, judge_calls=0)
    ledger = create_budget_ledger(target, {"authorization_id": "OFFLINE-POLICY-014-REJECTION",
        "authorization_type": SMOKE_AUTHORIZATION_TYPE, "asset_sha256": SMOKE_DATASET_SHA256,
        "case_ids": [plan["case_id"]], "database_identity": target.identity,
        "code_sha256": "offline-test-only", "git_commit": "offline-test-only",
        "allowed_tool_capabilities": allowed}, limits, SimpleNamespace(model="offline-test"))
    provider_reservation = ledger.reserve(trial_id=plan["case_id"], category="provider", calls=1,
        tokens_reserved=120000, estimated_usd_reserved=.18)
    retrieval_reservation = ledger.reserve(trial_id=plan["case_id"], category="tool", calls=1)
    answer_provider = OfflineRejectedAnswer()
    route = {"strategy": "SINGLE", "sub_strategy": "SINGLE", "intent_labels": ["policy_lookup"],
        "safety_decision": "ALLOW", "reason_code": "OFFLINE_POLICY_READ",
        "needs_clarification": False, "clarification_question": None,
        "required_capabilities": allowed, "decision_source": "OFFLINE_TEST"}
    services = make_legacy_services(provider=answer_provider,
        retrieval_service=reads["retrieval"], available_capabilities=allowed)
    services.router = lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route))
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=answer_provider, selected=selected,
            services=services, reads=reads, checkpoint_factory=checkpoint_factory)
        assert provider_calls == ["answer"]
        assert result["status"] == "FAILED"
        assert result["result"] is None
        assert len(result["observations"]["runs"]) == 1
        persisted_run = result["observations"]["runs"][0]
        assert persisted_run["status"] == "FAILED"
        error_detail = json.loads(persisted_run["error_detail_json"])
        rejection = error_detail["answer_rejection_audit"]
        assert rejection["run_id"] == result["run_id"]
        assert rejection["candidate_citation_ids"] == ["syn-policy-giftcard-b"]
        assert set(rejection["available_evidence_ids"]) == set(plan["case"]["input"]["acceptable_evidence_ids"])
        events = result["observations"]["run_events"]
        assert [row["event_type"] for row in events if row["event_type"].startswith("policy_retrieval_")] == [
            "policy_retrieval_started", "policy_retrieval_finished"]
        finished = next(json.loads(row["payload_json"]) for row in events
            if row["event_type"] == "policy_retrieval_finished")
        assert finished["status"] == "SUCCEEDED"
        assert finished["result_ref"] and finished["evidence_ids"]
        ledger.settle(provider_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=19, estimated_usd=.0001, call_count_status="RECORDED")
        ledger.settle(retrieval_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=0, estimated_usd=0.0, call_count_status="RECORDED")
        archive = {"case_id": plan["case_id"], "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / "policy014-rejected-application.json"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        budget = ledger.trial_snapshot(plan["case_id"])
        budget.update({"tokens": 19, "estimated_usd": .0001,
            "unknown_tokens": False, "unknown_cost": False,
            "token_status": "RECORDED", "estimate_status": "RECORDED_ESTIMATE",
            "billed_status": "UNKNOWN"})
        checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
            budget_snapshot=budget, cleanup=cleanup,
            rules=locked_rules(bundle, POLICY014_SAFETY038_PROFILE_ID))
        assert checks["checks"]["policy_retrieval_checks"]["status"] == "PASS"
        assert checks["checks"]["tool_result_checks"]["status"] == "PASS"
        assert checks["checks"]["budget_checks"]["status"] == "PASS"
        assert checks["checks"]["cleanup_checks"]["status"] == "PASS"
        assert checks["checks"]["answer_validation_checks"]["status"] == "FAIL"
        assert checks["checks"]["answer_reference_checks"]["status"] == "FAIL"
        assert checks["checks"]["execution_checks"]["status"] == "PASS"
        assert checks["checks"]["failure_classification"]["components"]["policy_retrieval_and_audit"] == "PASS"
        assert checks["checks"]["failure_classification"]["components"]["budget"] == "PASS"
        assert checks["decision"] == "STOP"
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        assert cleanup["residual_case_rows"] == 0
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active"):
            engine.dispose()
            raise AssertionError("test left an active fixture lease")
        engine.dispose()


@pytest.mark.parametrize(("archive_relative", "expected_answer_sha"), [
    ("docs/eval/personal-dev-runs/5f83a5bc040d4326aa06e1127a032bc5.json",
        "bcc7667761b4830e4c7245556a6752434a2b3e4eaa1ae63b6260d6bdeeb72810"),
    ("docs/eval/personal-dev-runs/a701203eccbb46f2b693de8a78aa501b.json",
        "4e2871b55499ebddc629661651aa15af2ad66f16d09833758e8061e03a212ffb"),
])
def test_policy014_real_failed_candidates_pass_r3_offline_app_checker_and_cleanup(
        tmp_path, bundle, archive_relative, expected_answer_sha):
    """Replay the archived full answer through real fixture retrieval and the application chain."""
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph

    plan = plan_case(bundle, "D1-POLICY-014")
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=1)
    archived = json.loads((ROOT / archive_relative).read_text(encoding="utf-8"))
    old_run = archived["result"]["observations"]["runs"][0]
    rejection = json.loads(old_run["error_detail_json"])["answer_rejection_audit"]
    answer_text = rejection["candidate_answer_redacted"]
    answer_units = rejection.get("usage_summary", {}).get("total_units", 388)
    assert hashlib.sha256(answer_text.encode("utf-8")).hexdigest() == expected_answer_sha
    answer_provider_calls = []

    class OfflineSavedCandidate:
        def answer(self, _question, evidence):
            answer_provider_calls.append("answer")
            available = {row["chunk_id"] for row in evidence}
            assert set(plan["case"]["input"]["acceptable_evidence_ids"]) <= available
            return {"answer": answer_text,
                "citations": list(rejection["candidate_citation_ids"]),
                "model_id": "offline-saved-candidate", "finish_reason": "stop",
                "usage": {"prompt_tokens": 323, "completion_tokens": 65, "total_tokens": answer_units}}

    allowed = ["search_knowledge"]
    limits = BudgetLimits(provider_calls=4, tool_calls=1, total_tokens=120000,
        estimated_usd=.18, max_calls_per_trial=1, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0, embedding_calls=0, judge_calls=0)
    ledger = create_budget_ledger(target, {"authorization_id": "OFFLINE-POLICY-014-R2-SAVED-ANSWER",
        "authorization_type": SMOKE_AUTHORIZATION_TYPE, "asset_sha256": SMOKE_DATASET_SHA256,
        "case_ids": [plan["case_id"]], "database_identity": target.identity,
        "code_sha256": "offline-test-only", "git_commit": "offline-test-only",
        "allowed_tool_capabilities": allowed}, limits, SimpleNamespace(model="offline-test"))
    provider_reservation = ledger.reserve(trial_id=plan["case_id"], category="provider", calls=1,
        tokens_reserved=120000, estimated_usd_reserved=.18)
    retrieval_reservation = ledger.reserve(trial_id=plan["case_id"], category="tool", calls=1)
    answer_provider = OfflineSavedCandidate()
    route = {"strategy": "SINGLE", "sub_strategy": "SINGLE", "intent_labels": ["policy_lookup"],
        "safety_decision": "ALLOW", "reason_code": "OFFLINE_POLICY_COMPARE",
        "needs_clarification": False, "clarification_question": None,
        "required_capabilities": allowed, "decision_source": "OFFLINE_TEST"}
    services = make_legacy_services(provider=answer_provider,
        retrieval_service=reads["retrieval"], available_capabilities=allowed)
    services.router = lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route))
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=answer_provider, selected=selected,
            services=services, reads=reads, checkpoint_factory=checkpoint_factory)
        assert answer_provider_calls == ["answer"]
        assert result["status"] == "SUCCEEDED"
        assert result["result"]["answer"] == answer_text
        assert {row["chunk_id"] for row in result["result"]["citations"]} == set(
            plan["case"]["input"]["acceptable_evidence_ids"])
        events = result["observations"]["run_events"]
        finished = [json.loads(row["payload_json"]) for row in events
            if row["event_type"] == "policy_retrieval_finished"]
        assert len(finished) == 1 and finished[0]["status"] == "SUCCEEDED"
        assert set(finished[0]["evidence_ids"]) == set(plan["case"]["input"]["acceptable_evidence_ids"])
        assert finished[0]["result_ref"] and finished[0]["trace_id"]
        assert result["observations"]["runs"][0]["status"] == "SUCCEEDED"
        ledger.settle(provider_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=answer_units, estimated_usd=.0005, call_count_status="RECORDED")
        ledger.settle(retrieval_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=0, estimated_usd=0.0, call_count_status="RECORDED")
        archive = {"case_id": plan["case_id"], "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / f"policy014-saved-answer-application-{expected_answer_sha[:8]}.json"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        budget = ledger.trial_snapshot(plan["case_id"])
        budget.update({"tokens": answer_units, "estimated_usd": .0005, "unknown_tokens": False,
            "unknown_cost": False, "token_status": "RECORDED",
            "estimate_status": "RECORDED_ESTIMATE", "billed_status": "UNKNOWN"})
        checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
            budget_snapshot=budget, cleanup=cleanup,
            rules=locked_rules(bundle, POLICY014_SAFETY038_R3_PROFILE_ID))
        assert checks["checks"]["policy_retrieval_checks"]["status"] == "PASS"
        assert checks["checks"]["answer_validation_checks"]["status"] == "PASS"
        assert checks["checks"]["answer_reference_checks"]["status"] == "PASS"
        assert checks["checks"]["budget_checks"]["status"] == "PASS"
        assert checks["checks"]["cleanup_checks"]["status"] == "PASS"
        assert checks["decision"] == "CONTINUE_WITH_REVIEW"
        assert checks["task_success"] == "UNKNOWN_NOT_GRADED"
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        assert cleanup["residual_case_rows"] == 0
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active"):
            engine.dispose()
            raise AssertionError("test left an active fixture lease")
        engine.dispose()


def test_answer_reference_projection_distinguishes_accepted_rejected_missing_and_invalid():
    retrieved = {"syn-policy-giftcard-a", "syn-policy-giftcard-b"}
    expected = set(retrieved)
    persisted = [{"run_id": "run-1", "result_json": json.dumps({"answer": "accepted",
        "citations": [{"chunk_id": "syn-policy-giftcard-a"},
            {"chunk_id": "syn-policy-giftcard-b"}]})}]
    accepted = _answer_reference_projection({"answer": "accepted", "citations": [
        {"chunk_id": "syn-policy-giftcard-a"}, {"chunk_id": "syn-policy-giftcard-b"}]},
        persisted, {}, retrieved, expected)
    assert accepted["answer_kind"] == "ACCEPTED_ANSWER"
    assert accepted["status"] == "PASS"
    assert accepted["citation_ids"] == sorted(expected)

    rejected = _answer_reference_projection({}, [{"run_id": "run-1", "result_json": None}], {
        "candidate_answer_present": True, "candidate_answer_redacted": "rejected",
        "candidate_citation_ids": ["syn-policy-giftcard-a", "syn-policy-giftcard-b"]}, retrieved, expected)
    assert rejected["answer_kind"] == "REJECTED_CANDIDATE"
    assert rejected["status"] == "PASS"
    assert rejected["citation_source"] == "answer_rejection_audit"

    missing = _answer_reference_projection({}, [{"run_id": "run-1", "result_json": None}], {}, retrieved, expected)
    assert missing["answer_kind"] == "MISSING"
    assert missing["status"] == "UNKNOWN"
    for ids in ([], ["unknown-id"]):
        invalid = _answer_reference_projection({"answer": "accepted", "citations": ids},
            [{"run_id": "run-1", "result_json": json.dumps({"answer": "accepted", "citations": ids})}],
            {}, retrieved, expected)
        assert invalid["status"] in {"UNKNOWN", "FAIL"}
        assert invalid["status"] != "PASS"


def test_r3_policy014_success_archive_projects_saved_answer_and_both_references(bundle):
    archive = json.loads((ROOT / "docs/eval/personal-dev-runs/aad2076931d243a6a3fcab570a939b7e.json")
        .read_text(encoding="utf-8"))
    result = archive["result"]
    app = result["result"]
    events = result["observations"]["run_events"]
    retrieved = set().union(*(set(json.loads(row["payload_json"]).get("evidence_ids", []))
        for row in events if row["event_type"] == "policy_retrieval_finished"))
    run_rows = result["observations"]["runs"]
    plan_case_row = next(row for row in json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
        if row["case_id"] == "D1-POLICY-014")
    projection = _answer_reference_projection(app, run_rows, {}, retrieved,
        set(plan_case_row["input"]["acceptable_evidence_ids"]))
    assert projection["answer_kind"] == "ACCEPTED_ANSWER"
    assert projection["answer_captured"] is True
    assert projection["citation_ids"] == ["syn-policy-giftcard-a", "syn-policy-giftcard-b"]
    assert projection["status"] == "PASS"


def test_safety038_r4_profile_is_exact_single_case_and_has_instruction_scope(bundle):
    profile = smoke_profile(SAFETY038_R4_PROFILE_ID)
    assert profile.cases == ("D1-SAFETY-038",)
    assert profile.tool_caps == {"D1-SAFETY-038": 1}
    rules = locked_rules(bundle, profile)
    assert rules["case_order"] == ["D1-SAFETY-038"]
    assert rules["cases"][0]["policy_answer_contract"]["must_preserve_unresolved_authority"] is True
    assert budget_contract(profile)["batch"] == {
        "provider_calls": 4, "tool_calls": 1, "total_tokens": 120000,
        "estimated_usd": 0.18, "max_duration_seconds": 180,
        "max_concurrent_trials": 1, "max_calls_per_trial": 4, "max_retries": 0,
        "embedding_calls": 0, "judge_calls": 0}


def test_safety038_scoring_closeout_profile_is_a_new_exact_scope_not_a_reset_of_r4(bundle):
    profile = smoke_profile(SAFETY038_SCORING_PROFILE_ID)
    assert profile.cases == ("D1-SAFETY-038",)
    assert profile.tool_caps == {"D1-SAFETY-038": 1}
    assert profile.quality_failure_action == "STOP_AFTER_CASE"
    assert locked_rules(bundle, profile)["answer_semantic_validator_version"] == "m6.1-policy-assertion-scope-v4"
    assert budget_contract(profile)["batch"]["provider_calls"] == 4
    assert budget_contract(profile)["batch"]["tool_calls"] == 1


def test_safety038_instruction_attempt_is_durable_and_non_replayable(tmp_path, monkeypatch):
    import scripts.run_m6_1_d1_personal_smoke as runner

    ledger = tmp_path / "instruction-attempts.jsonl"
    claim = tmp_path / "instruction-attempt.claim"
    smoke_parent = tmp_path / "personal-dev-smokes"
    smoke_id = "M61D1-SMOKE-R324-SAFETY038-R4-" + "a" * 32
    smoke_dir = smoke_parent / smoke_id
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "SAFETY038_ATTEMPT_LEDGER", ledger)
    monkeypatch.setattr(runner, "SAFETY038_ATTEMPT_CLAIM", claim)
    monkeypatch.setattr(runner, "SMOKE_PARENT", smoke_parent)
    runner._attempt_ledger_rows(initialize=True)
    assert runner._check_safety038_attempt_available()["available"] is True
    record = runner._check_safety038_attempt_available(claim=True, smoke_id=smoke_id,
        manifest={"profile_id": SAFETY038_R4_PROFILE_ID,
            "case_order": ["D1-SAFETY-038"], "dataset_sha256": SMOKE_DATASET_SHA256})
    assert record["consumed_before_run_creation"] is True
    assert record["reserved_provider_calls"] == 4
    assert record["reserved_policy_reads"] == 1
    with pytest.raises(SmokeAdmissionError, match="ALREADY_CONSUMED"):
        runner._check_safety038_attempt_available()
    admission_dir = smoke_parent / ("M61D1-SMOKE-R324-SAFETY038-R4-" + "b" * 32)
    admission_dir.mkdir()
    (admission_dir / "manifest.json").write_text(json.dumps({
        "profile_id": SAFETY038_R4_PROFILE_ID, "scope_transition": None}), encoding="utf-8")
    for name in ("budget.json", "locked-rules.json", "authorization.json"):
        (admission_dir / name).write_text("{}", encoding="utf-8")
    class Engine:
        def dispose(self):
            pass
    monkeypatch.setattr(runner, "_runtime_context", lambda **_kwargs:
        (None, None, None, Engine(), None, None, None, None, None, None, None))
    with pytest.raises(SmokeAdmissionError, match="ALREADY_CONSUMED"):
        runner._load_and_validate(admission_dir, require_api_key=False, check_state=True,
            profile_id=SAFETY038_R4_PROFILE_ID)


def test_safety038_scoring_instruction_has_its_own_persistent_single_use_ledger(tmp_path, monkeypatch):
    import scripts.run_m6_1_d1_personal_smoke as runner

    ledger = tmp_path / "scoring-instruction-attempts.jsonl"
    claim = tmp_path / "scoring-instruction-attempt.claim"
    smoke_parent = tmp_path / "personal-dev-smokes"
    smoke_id = "M61D1-SMOKE-R324-SAFETY038-SCORING-R1-" + "c" * 32
    smoke_dir = smoke_parent / smoke_id
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "SAFETY038_SCORING_ATTEMPT_LEDGER", ledger)
    monkeypatch.setattr(runner, "SAFETY038_SCORING_ATTEMPT_CLAIM", claim)
    monkeypatch.setattr(runner, "SMOKE_PARENT", smoke_parent)
    runner._attempt_ledger_rows(initialize=True, profile_id=SAFETY038_SCORING_PROFILE_ID)
    assert runner._check_safety038_attempt_available(profile_id=SAFETY038_SCORING_PROFILE_ID)["available"]
    record = runner._check_safety038_attempt_available(claim=True, smoke_id=smoke_id,
        manifest={"profile_id": SAFETY038_SCORING_PROFILE_ID,
            "case_order": ["D1-SAFETY-038"], "dataset_sha256": SMOKE_DATASET_SHA256},
        profile_id=SAFETY038_SCORING_PROFILE_ID)
    assert record["consumed_before_run_creation"] is True
    with pytest.raises(SmokeAdmissionError, match="ALREADY_CONSUMED"):
        runner._check_safety038_attempt_available(profile_id=SAFETY038_SCORING_PROFILE_ID)


@pytest.mark.parametrize("answer_source", ["fully_explicit", "saved_r3_rejected_candidate",
    "saved_scoring_r1_sealed_scope_candidate", "saved_scoring_r1_wrong_object_food",
    "saved_scoring_r1_wrong_object_opened"])
def test_safety038_real_application_persists_both_versions_and_preserves_unknown_authority(
        tmp_path, bundle, answer_source):
    """Offline app integration: actual policy fixtures, route, citations, checker and cleanup."""
    from langgraph.checkpoint.memory import MemorySaver
    from app.m0.config import load_settings
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer
    from app.orchestration.runtime import execute_claimed_chat_langgraph

    plan = plan_case(bundle, "D1-SAFETY-038")
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=1)
    provider_calls = []
    saved_answer = None
    saved_citations = None
    answer_usage = {"prompt_tokens": 14, "completion_tokens": 40, "total_tokens": 54}
    if answer_source in {"saved_r3_rejected_candidate", "saved_scoring_r1_sealed_scope_candidate",
            "saved_scoring_r1_wrong_object_food", "saved_scoring_r1_wrong_object_opened"}:
        saved_path = ("docs/eval/personal-dev-runs/7e31771a37d94b7fbf2ff8607e87ec69.json"
            if answer_source != "saved_r3_rejected_candidate" else
            "docs/eval/personal-dev-runs/e8b0738429e84f5ba55a02cd029fd586.json")
        saved_archive = json.loads((ROOT / saved_path)
            .read_text(encoding="utf-8"))
        saved_run = saved_archive["result"]["observations"]["runs"][0]
        rejection = json.loads(saved_run["error_detail_json"])["answer_rejection_audit"]
        saved_answer = rejection["candidate_answer_redacted"]
        saved_citations = rejection["candidate_citation_ids"]
        answer_usage = {"prompt_tokens": 337, "completion_tokens": 108, "total_tokens": 445}
        if answer_source == "saved_scoring_r1_wrong_object_food":
            saved_answer = saved_answer.replace("密封商品", "食品")
        elif answer_source == "saved_scoring_r1_wrong_object_opened":
            saved_answer = saved_answer.replace("密封商品", "已拆封商品")

    class OfflineConflictAnswer:
        def answer(self, _question, evidence):
            provider_calls.append("answer")
            by_id = {row["chunk_id"]: row for row in evidence}
            expected = set(plan["case"]["input"]["acceptable_evidence_ids"])
            assert expected.issubset(by_id), sorted(by_id)
            if saved_answer is not None:
                answer = saved_answer
                citations = saved_citations
            else:
                left, right = (by_id[eid] for eid in sorted(expected))
                answer = ("Fictional sources state: " + left["content"] + "; " + right["content"] + ". "
                    + "The excerpts conflict and have the same effective interval. "
                    + "The order date cannot determine which version is authoritative; ask the policy owner.")
                citations = sorted(expected)
            self.answer_text = answer
            return {"answer": answer, "citations": citations,
                "model_id": "offline-stub", "finish_reason": "stop",
                "usage": answer_usage}

    allowed = ["search_knowledge"]
    limits = BudgetLimits(provider_calls=4, tool_calls=1, total_tokens=120000,
        estimated_usd=.18, max_calls_per_trial=1, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0, embedding_calls=0, judge_calls=0)
    ledger = create_budget_ledger(target, {"authorization_id": "OFFLINE-SAFETY-038",
        "authorization_type": SMOKE_AUTHORIZATION_TYPE, "asset_sha256": SMOKE_DATASET_SHA256,
        "case_ids": [plan["case_id"]], "database_identity": target.identity,
        "code_sha256": "offline-test-only", "git_commit": "offline-test-only",
        "allowed_tool_capabilities": allowed}, limits, SimpleNamespace(model="offline-test"))
    provider_reservation = ledger.reserve(trial_id=plan["case_id"], category="provider", calls=1,
        tokens_reserved=120000, estimated_usd_reserved=.18)
    retrieval_reservation = ledger.reserve(trial_id=plan["case_id"], category="tool", calls=1)
    answer_provider = OfflineConflictAnswer()
    route = {"strategy": "SINGLE", "sub_strategy": "SINGLE", "intent_labels": ["policy_lookup"],
        "safety_decision": "ALLOW", "reason_code": "OFFLINE_POLICY_VERSION_CONFLICT",
        "needs_clarification": False, "clarification_question": None,
        "required_capabilities": allowed, "decision_source": "OFFLINE_TEST"}
    services = make_legacy_services(provider=answer_provider,
        retrieval_service=reads["retrieval"], available_capabilities=allowed)
    services.router = lambda _query, **_kwargs: validate_case_route_scope(plan, dict(route))
    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    selected = replace(settings, database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)

    @asynccontextmanager
    async def checkpoint_factory(_url, _schema):
        yield MemorySaver(serde=strict_serializer())

    try:
        result = execute_claimed_lifecycle(coordinator, plan, receipt,
            application_call=execute_claimed_chat_langgraph, provider=answer_provider, selected=selected,
            services=services, reads=reads, checkpoint_factory=checkpoint_factory)
        assert provider_calls == ["answer"]
        invalid_object = answer_source in {"saved_scoring_r1_wrong_object_food", "saved_scoring_r1_wrong_object_opened"}
        if invalid_object:
            assert result["status"] == "FAILED"
            assert result["result"] is None
            failed_run = result["observations"]["runs"][0]
            assert failed_run["status"] == "FAILED"
            failed_detail = json.loads(failed_run["error_detail_json"])
            rejected = failed_detail["answer_rejection_audit"]
            assert rejected["candidate_answer_redacted"] == saved_answer
            assert rejected["candidate_citation_ids"] == plan["case"]["input"]["acceptable_evidence_ids"]
            assert any(issue["code"] in {"ANSWER_UNSUPPORTED_POLICY_WINDOW", "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED"}
                for issue in failed_detail["semantic_issues"])
        else:
            assert result["status"] == "SUCCEEDED"
            app_result = result["result"]
            assert app_result and app_result["answer"]
            if saved_answer is not None:
                assert app_result["answer"] == saved_answer
            assert set(row["chunk_id"] for row in app_result["citations"]) == set(plan["case"]["input"]["acceptable_evidence_ids"])
        events = result["observations"]["run_events"]
        finished = [json.loads(row["payload_json"]) for row in events
            if row["event_type"] == "policy_retrieval_finished"]
        assert len(finished) == 1
        assert set(finished[0]["evidence_ids"]) >= set(plan["case"]["input"]["acceptable_evidence_ids"])
        assert finished[0]["result_ref"] and finished[0]["trace_id"]
        ledger.settle(provider_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=answer_usage["total_tokens"], estimated_usd=.0001, call_count_status="RECORDED")
        ledger.settle(retrieval_reservation, status="COMMIT", actual_calls=1,
            actual_tokens=0, estimated_usd=0.0, call_count_status="RECORDED")
        archive = {"case_id": plan["case_id"], "user_query": plan["message"],
            "receipt": receipt.__dict__, "result": result}
        archive_path = tmp_path / "safety038-policy-conflict-application.json"
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        budget = ledger.trial_snapshot(plan["case_id"])
        budget.update({"tokens": answer_usage["total_tokens"], "estimated_usd": .0001,
            "unknown_tokens": False, "unknown_cost": False,
            "token_status": "RECORDED", "estimate_status": "RECORDED_ESTIMATE",
            "billed_status": "UNKNOWN"})
        checks = evaluate_smoke_case(case_id=plan["case_id"], plan=plan, archive=archive,
            budget_snapshot=budget, cleanup=cleanup,
            rules=locked_rules(bundle, POLICY014_SAFETY038_R3_PROFILE_ID))
        assert checks["checks"]["policy_retrieval_checks"]["status"] == "PASS"
        assert checks["checks"]["answer_reference_checks"]["status"] == "PASS"
        assert checks["checks"]["budget_checks"]["status"] == "PASS"
        assert checks["checks"]["cleanup_checks"]["status"] == "PASS"
        if invalid_object:
            assert checks["checks"]["answer_validation_checks"]["status"] == "FAIL"
            assert checks["decision"] == "STOP", json.dumps(checks, ensure_ascii=False)
        else:
            assert checks["decision"] == "CONTINUE_WITH_REVIEW", json.dumps(checks, ensure_ascii=False)
        assert checks["task_success"] == "UNKNOWN_NOT_GRADED"
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
    finally:
        marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active") and "result" in locals():
            cleanup_archive = tmp_path / "safety038-policy-conflict-test-terminal-recovery.json"
            cleanup_archive.write_text(json.dumps({"case_id": plan["case_id"],
                "user_query": plan["message"], "receipt": receipt.__dict__,
                "result": result}, ensure_ascii=False, default=str), encoding="utf-8")
            safety = coordinator.diagnostic_cleanup_safety(receipt, cleanup_archive)
            if safety.get("safe_to_cleanup"):
                coordinator.cleanup(receipt)
                marker = coordinator.inspect_active_run_evidence()
        if marker.get("lease_active"):
            engine.dispose()
            raise AssertionError("test left an active fixture lease")
        engine.dispose()


def test_crossagent_preflight_rejects_unresolved_product_without_faking_lookup(tmp_path, bundle):
    from scripts.run_m6_1_d1_personal_smoke import _preflight_read_case

    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    plan = plan_case(bundle, "D1-CROSSAGENT-029")
    try:
        with pytest.raises(SmokeAdmissionError,
                match="CROSSAGENT_PRODUCT_IDENTIFIER_UNRESOLVED_BY_ALLOWED_CAPABILITIES"):
            _preflight_read_case(coordinator, plan)
        with coordinator.factory() as db:
            from sqlalchemy import text
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
    finally:
        engine.dispose()
