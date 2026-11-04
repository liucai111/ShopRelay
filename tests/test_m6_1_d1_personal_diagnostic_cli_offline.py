"""CLI admission and one-shot authorization tests on an isolated synthetic SQLite target."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text

from app.m1.models import Run
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_dev import D1PreflightError
from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, AUTHORIZED_VERSION,
    PROVIDER_RETRY_ROUND_ID, PersonalAuthorizationError, file_sha256,
    load_price_evidence, validate_personal_budget,
    personal_diagnostic_profile, PROVIDER_PROBE_ROUND_ID, ORDER_DIAGNOSTIC_ROUND_ID,
)
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_diagnostic_checks import diagnostic_cli_projection


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / AUTHORIZED_ASSET_PATH
CODE_SHA256 = "a" * 64
REVISION = "SYNTHETIC-OFFLINE-REVISION"


def copy_current_price(source: Path, destination: Path) -> None:
    record = json.loads(source.read_text(encoding="utf-8"))
    record["checked_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    destination.write_text(json.dumps(record), encoding="utf-8")


def copy_r6_predecessor_evidence(root):
    paths = (
        "docs/eval/d1-diagnostic-r6-policy013-20261027/SHA256SUMS-r6.md",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/closeout-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/closeout-r6.md",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/D1-POLICY-013-authorization-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/state/D1-POLICY-013-execution-state-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/reports/D1-POLICY-013-diagnostic-check-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/reports/D1-POLICY-013-independent-check-r6.json",
        "docs/eval/personal-dev-runs/66c785b79d01425e85f4d5eba1dfebe9.json",
    )
    for relative in paths:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    """Build a separate project-root-shaped workspace; never touches personal D1 SQLite."""
    import app.m0.config
    import eval.m6_1.personal_dev as personal_dev
    import scripts.run_m6_1_d1_personal_dev as cli

    root = tmp_path.parent / ("d1cli-" + uuid.uuid4().hex[:10])
    root.mkdir()
    asset_path = root / AUTHORIZED_ASSET_PATH
    asset_path.parent.mkdir(parents=True)
    shutil.copyfile(DATASET, asset_path)
    profile = personal_diagnostic_profile("D1-PRODUCT-001")
    budget_path = root / profile.budget_path
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / profile.budget_path, budget_path)
    pricing_path = root / profile.pricing_path
    pricing_path.parent.mkdir(parents=True, exist_ok=True)
    copy_current_price(ROOT / profile.pricing_path, pricing_path)
    # Copy the new round's policy inputs into this isolated synthetic workspace.
    policy_profile = personal_diagnostic_profile("D1-POLICY-013")
    policy_budget = root / policy_profile.budget_path
    policy_budget.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / policy_profile.budget_path, policy_budget)
    policy_pricing = root / policy_profile.pricing_path
    copy_current_price(ROOT / policy_profile.pricing_path, policy_pricing)
    rules_source = ROOT / policy_profile.check_rules_path
    rules_target = root / policy_profile.check_rules_path
    rules_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rules_source, rules_target)
    retry_profile = personal_diagnostic_profile("D1-POLICY-013",
        diagnostic_round_id=PROVIDER_RETRY_ROUND_ID)
    retry_budget = root / retry_profile.budget_path
    retry_budget.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / retry_profile.budget_path, retry_budget)
    retry_pricing = root / retry_profile.pricing_path
    retry_pricing.parent.mkdir(parents=True, exist_ok=True)
    copy_current_price(ROOT / retry_profile.pricing_path, retry_pricing)
    retry_rules = root / retry_profile.check_rules_path
    retry_rules.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / retry_profile.check_rules_path, retry_rules)
    probe_profile = personal_diagnostic_profile("D1-POLICY-013",
        diagnostic_round_id=PROVIDER_PROBE_ROUND_ID)
    for relative in (probe_profile.budget_path, probe_profile.pricing_path, probe_profile.check_rules_path):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == probe_profile.pricing_path:
            copy_current_price(ROOT / relative, destination)
        else:
            shutil.copyfile(ROOT / relative, destination)

    for case_id in ("D1-ORDER-007", "D1-ORDER-009"):
        order_profile = personal_diagnostic_profile(case_id,
            diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
        for relative in (order_profile.budget_path, order_profile.pricing_path,
                         order_profile.check_rules_path):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == order_profile.pricing_path:
                copy_current_price(ROOT / relative, destination)
            else:
                shutil.copyfile(ROOT / relative, destination)
    target = PersonalDevDatabase(root)
    engine, _factory = target.open(initialize=True)
    database_identity = target.identity
    engine.dispose()

    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "ASSET_PATH", asset_path)
    monkeypatch.setattr(cli, "ASSET_HASH", AUTHORIZED_ASSET_SHA256)
    monkeypatch.setattr(personal_dev, "implementation_digest", lambda _root: CODE_SHA256)
    monkeypatch.setattr(app.m0.config, "load_settings", lambda **_kwargs: SimpleNamespace(
        model="deepseek-flash", base_url="https://api.deepseek.com", api_key="offline-only"))
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: REVISION + "\n")

    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    _price, price_sha = load_price_evidence(pricing_path,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    now = datetime.now(timezone.utc)
    authorization = {
        "schema_version": "1.0",
        "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING",
        "status": "USER_CONFIRMED",
        "authorization_id": "SYNTHETIC-CLI-OFFLINE-" + now.strftime("%H%M%S%f"),
        "purpose": "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.",
        "grading_status": "NOT_GRADED",
        "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "confirmed_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "case_ids": ["D1-PRODUCT-001"],
        "asset_id": "m6.1-d1-synthetic-dev-draft",
        "data_version": AUTHORIZED_VERSION,
        "asset_path": AUTHORIZED_ASSET_PATH,
        "asset_sha256": AUTHORIZED_ASSET_SHA256,
        "code_sha256": CODE_SHA256,
        "code_revision": REVISION,
        "database_identity": database_identity,
        "model": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com",
        "pricing_sha256": price_sha,
        "budget": budget,
        "budget_sha256": sha256_json(budget),
        "budget_file_sha256": file_sha256(budget_path),
        "limits": {"provider_calls": 4, "tool_calls": 4, "total_tokens": 120000,
            "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1,
            "automatic_retries": 0, "embedding_calls": 0, "judge_calls": 0},
        "protected_assets_allowed": False,
        "formal_gates_changed": False,
    }
    authorization_path = root / profile.authorization_path
    authorization_path.parent.mkdir(parents=True, exist_ok=True)
    authorization_path.write_text(json.dumps(authorization, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"root": root, "cli": cli, "profile": profile, "target": target,
        "authorization": authorization, "authorization_path": authorization_path,
        "budget_path": budget_path, "asset_path": asset_path,
        "database_identity": database_identity}


def authorize_scoped_case(cli_project, case_id, *, profile=None):
    profile = profile or personal_diagnostic_profile(case_id)
    case_budget_path = cli_project["root"] / profile.budget_path
    case_budget_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / profile.budget_path, case_budget_path)
    budget = json.loads(case_budget_path.read_text(encoding="utf-8"))
    price, price_sha = load_price_evidence(cli_project["root"] / profile.pricing_path,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    del price
    now = datetime.now(timezone.utc)
    tool_calls = 0 if not profile.allowed_tools else 4
    authorization = {"schema_version": "1.0", "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING",
        "status": "USER_CONFIRMED", "authorization_id": f"SYNTHETIC-{case_id}-{now.strftime('%H%M%S%f')}",
        "diagnostic_round_id": profile.round_id,
        "purpose": "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.",
        "grading_status": "NOT_GRADED", "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "confirmed_at_utc": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "case_ids": [case_id], "allowed_tools": sorted(profile.allowed_tools),
        "check_rules_sha256": file_sha256(cli_project["root"] / profile.check_rules_path),
        "asset_id": "m6.1-d1-synthetic-dev-draft", "data_version": AUTHORIZED_VERSION,
        "asset_path": AUTHORIZED_ASSET_PATH, "asset_sha256": AUTHORIZED_ASSET_SHA256,
        "code_sha256": CODE_SHA256, "code_revision": REVISION,
        "database_identity": cli_project["database_identity"], "model": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com", "pricing_sha256": price_sha,
        "budget": budget, "budget_sha256": sha256_json(budget),
        "budget_file_sha256": file_sha256(case_budget_path),
        "limits": {"provider_calls": budget["provider_calls"], "tool_calls": budget["tool_calls"],
            "total_tokens": budget["total_tokens"], "estimated_usd": budget["estimated_usd"],
            "timeout_seconds": budget["max_duration_seconds"],
            "concurrency": budget["max_concurrent_trials"],
            "automatic_retries": budget["max_retries"],
            "embedding_calls": budget["embedding_calls"], "judge_calls": budget["judge_calls"]},
        "protected_assets_allowed": False, "formal_gates_changed": False}
    predecessor = profile.predecessor_case_id or {
        "D1-ORDER-007": "D1-POLICY-013", "D1-ORDER-009": "D1-ORDER-007"}.get(case_id)
    if predecessor:
        authorization["predecessor_report_path"] = (profile.predecessor_report_path or
            (Path(profile.report_dir) / f"{predecessor}-diagnostic-check-{profile.report_suffix}.json").as_posix())
        authorization["predecessor_report_sha256"] = profile.predecessor_report_sha256 or "c" * 64
        if profile.predecessor_case_id:
            authorization.update({
                "predecessor_case_id": predecessor,
                "predecessor_round_id": profile.predecessor_round_id,
                "predecessor_code_sha256": profile.predecessor_code_sha256 or CODE_SHA256,
                "predecessor_archive_path": profile.predecessor_archive_path or
                    "docs/eval/personal-dev-runs/offline-order-007.json",
                "predecessor_archive_sha256": profile.predecessor_archive_sha256 or "d" * 64,
                "predecessor_authorization_path": profile.predecessor_authorization_path,
                "predecessor_authorization_sha256": profile.predecessor_authorization_sha256 or "e" * 64,
                "predecessor_state_path": profile.predecessor_state_path,
                "predecessor_state_sha256": profile.predecessor_state_sha256 or "f" * 64,
                "predecessor_independent_report_path": profile.predecessor_independent_report_path,
                "predecessor_independent_report_sha256": profile.predecessor_independent_report_sha256 or "1" * 64,
                "predecessor_rules_sha256": profile.predecessor_rules_sha256 or file_sha256(
                    cli_project["root"] / profile.check_rules_path),
            })
    authorization_path = cli_project["root"] / profile.authorization_path
    authorization_path.parent.mkdir(parents=True, exist_ok=True)
    authorization_path.write_text(json.dumps(authorization, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return profile, authorization_path, case_budget_path


def invoke(cli_project, monkeypatch, *, mode="--check-admission", case_id="D1-PRODUCT-001",
           authorization_path=None, budget_path=None):
    cli = cli_project["cli"]
    auth_path = authorization_path or cli_project["authorization_path"]
    selected_budget = budget_path or cli_project["budget_path"]
    monkeypatch.setattr(sys, "argv", ["run_m6_1_d1_personal_dev.py", mode,
        "--case", case_id, "--diagnostic-authorization", str(auth_path), "--budget", str(selected_budget)])
    return cli.main()


def invoke_authorized_case(cli_project, monkeypatch, case_id, authorization_path, budget_path):
    monkeypatch.setattr(sys, "argv", ["run_m6_1_d1_personal_dev.py", "--check-admission",
        "--case", case_id, "--diagnostic-authorization", str(authorization_path), "--budget", str(budget_path)])
    return cli_project["cli"].main()


def test_actual_cli_product_admission_uses_receipt_bound_product_repository_and_does_not_consume_run(cli_project, monkeypatch, capsys):
    from app.m3.policy_tools import PolicyReadService

    def policy_must_not_run(*_args, **_kwargs):
        raise AssertionError("product admission must not call policy retrieval")

    monkeypatch.setattr(PolicyReadService, "search", policy_must_not_run)
    assert invoke(cli_project, monkeypatch) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admission"] == "PERSONAL_DIAGNOSTIC_SCOPE_CHECKED"
    assert result["case_id"] == "D1-PRODUCT-001"
    assert result["agent_started"] is False
    assert result["run_created"] is False
    assert result["provider_requests"] == 0
    assert result["execution_opportunity_consumed"] is False
    preflight = result["preflight_evidence"]
    assert preflight["kind"] == "product"
    assert preflight["search_source"] == "shoprelay_product_catalog"
    assert preflight["candidate_count"] == 2
    assert preflight["read_calls"] == 3
    assert len(preflight["detail_evidence"]) == 2
    assert all(row["fixture_provenance"] and isinstance(row["weight_g"], (int, float))
        for row in preflight["detail_evidence"])
    assert preflight["cleanup_status"] == "RECEIPT_CLEANED_AND_LEASE_RELEASED"
    assert not (cli_project["root"] / cli_project["profile"].execution_state_path).exists()
    engine, factory = cli_project["target"].open()
    try:
        with factory() as db:
            assert db.execute(select(Run)).first() is None
            row = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert row.lease_id is None and row.receipt is None
            assert db.execute(text("SELECT COUNT(*) FROM d1_fixture_facts")).scalar_one() == 0
    finally:
        engine.dispose()


def test_provider_retry_profile_is_exact_and_case_budget_is_only_remaining_envelope(cli_project):
    from eval.m6_1.budget import BudgetLimits

    profile = personal_diagnostic_profile("D1-POLICY-013",
        diagnostic_round_id=PROVIDER_RETRY_ROUND_ID)
    assert profile.authorization_path.endswith("D1-POLICY-013-authorization-r5.json")
    assert profile.execution_state_path.endswith("D1-POLICY-013-execution-state-r5.json")
    assert profile.budget_limits["provider_calls"] == 3
    assert profile.budget_limits["total_tokens"] == 111808
    assert profile.budget_limits["estimated_usd"] == pytest.approx(0.1701696)
    assert 1 + profile.budget_limits["provider_calls"] == 4
    assert 8192 + profile.budget_limits["total_tokens"] == 120000
    assert pytest.approx(0.0098304 + profile.budget_limits["estimated_usd"]) == 0.18
    limits = BudgetLimits(**profile.budget_limits)
    validate_personal_budget(limits, "查询退货政策", budget_config=profile.budget_limits, profile=profile)
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_DEV_BUDGET_LIMITS_INVALID"):
        invalid = dict(profile.budget_limits, provider_calls=4)
        validate_personal_budget(BudgetLimits(**invalid), "查询退货政策",
            budget_config=invalid, profile=profile)
    with pytest.raises(PersonalAuthorizationError, match="ROUND_NOT_ALLOWLISTED"):
        personal_diagnostic_profile("D1-POLICY-013", diagnostic_round_id="arbitrary-round")


def test_r6_profile_is_distinct_allowlisted_and_preserves_exact_remaining_envelope(cli_project):
    from eval.m6_1.budget import BudgetLimits

    profile = personal_diagnostic_profile("D1-POLICY-013", diagnostic_round_id=PROVIDER_PROBE_ROUND_ID)
    assert profile.authorization_path.endswith("D1-POLICY-013-authorization-r6.json")
    assert profile.execution_state_path.endswith("D1-POLICY-013-execution-state-r6.json")
    assert profile.report_suffix == "r6"
    assert profile.allowed_tools == frozenset({"search_knowledge"})
    assert profile.budget_limits["provider_calls"] == 3
    assert profile.budget_limits["total_tokens"] == 111808
    assert profile.budget_limits["estimated_usd"] == pytest.approx(0.1701696)
    assert 1 + profile.budget_limits["provider_calls"] == 4
    assert 8192 + profile.budget_limits["total_tokens"] == 120000
    assert pytest.approx(0.0098304 + profile.budget_limits["estimated_usd"]) == 0.18
    validate_personal_budget(BudgetLimits(**profile.budget_limits), "查询退货政策",
        budget_config=profile.budget_limits, profile=profile)
    with pytest.raises(PersonalAuthorizationError, match="ROUND_NOT_ALLOWLISTED"):
        personal_diagnostic_profile("D1-POLICY-013", diagnostic_round_id="M61D1-POLICY013-PROVIDER-PROBE-R7")


def test_r6_policy_cli_admission_uses_new_paths_without_consuming_run(cli_project, monkeypatch, capsys):
    profile = personal_diagnostic_profile("D1-POLICY-013", diagnostic_round_id=PROVIDER_PROBE_ROUND_ID)
    _profile, authorization_path, budget_path = authorize_scoped_case(
        cli_project, "D1-POLICY-013", profile=profile)
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-POLICY-013",
        authorization_path, budget_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admission"] == "PERSONAL_DIAGNOSTIC_SCOPE_CHECKED"
    assert result["case_id"] == "D1-POLICY-013"
    assert result["provider_requests"] == 0
    assert result["run_created"] is False
    assert result["preflight_evidence"]["kind"] == "policy"
    assert not (cli_project["root"] / profile.execution_state_path).exists()


def test_r7_order007_rejects_r6_database_identity_mismatch_before_fixture_or_run(cli_project,
        monkeypatch, capsys):
    profile = personal_diagnostic_profile("D1-ORDER-007",
        diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
    copy_r6_predecessor_evidence(cli_project["root"])
    _profile, authorization_path, budget_path = authorize_scoped_case(
        cli_project, "D1-ORDER-007", profile=profile)
    monkeypatch.setattr(cli_project["cli"].D1Coordinator, "prepare",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fixture preparation must not run")))
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-ORDER-007",
        authorization_path, budget_path) == 2
    rejection = json.loads(capsys.readouterr().out)
    assert "DIAGNOSTIC_PREDECESSOR_CLOSEOUT_BINDING_MISMATCH" in rejection["reason"]
    assert rejection["run_created"] is False and rejection["execution_opportunity_consumed"] is False
    engine, factory = cli_project["target"].open()
    try:
        with factory() as db:
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
    finally:
        engine.dispose()


def test_r7_order007_rejects_changed_r6_predecessor_before_fixture_or_provider(cli_project,
        monkeypatch, capsys):
    profile = personal_diagnostic_profile("D1-ORDER-007",
        diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
    copy_r6_predecessor_evidence(cli_project["root"])
    _profile, authorization_path, budget_path = authorize_scoped_case(
        cli_project, "D1-ORDER-007", profile=profile)
    report_path = cli_project["root"] / profile.predecessor_report_path
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["checks"]["decision"]["outcome"] = "STOP"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli_project["cli"].D1Coordinator, "prepare",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fixture preparation must not run")))
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-ORDER-007",
        authorization_path, budget_path) == 2
    rejection = json.loads(capsys.readouterr().out)
    assert "DIAGNOSTIC_PREDECESSOR_REPORT_HASH_MISMATCH" in rejection["reason"]
    assert rejection["run_created"] is False
    assert rejection["execution_opportunity_consumed"] is False
    engine, factory = cli_project["target"].open()
    try:
        with factory() as db:
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
            assert db.execute(text("SELECT COUNT(*) FROM d1_fixture_facts")).scalar_one() == 0
    finally:
        engine.dispose()


def test_r5_policy_cli_admission_uses_new_paths_without_consuming_run(cli_project, monkeypatch, capsys):
    profile = personal_diagnostic_profile("D1-POLICY-013",
        diagnostic_round_id=PROVIDER_RETRY_ROUND_ID)
    _profile, authorization_path, budget_path = authorize_scoped_case(
        cli_project, "D1-POLICY-013", profile=profile)
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-POLICY-013",
        authorization_path, budget_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["admission"] == "PERSONAL_DIAGNOSTIC_SCOPE_CHECKED"
    assert result["case_id"] == "D1-POLICY-013"
    assert result["provider_requests"] == 0
    assert result["run_created"] is False
    assert result["preflight_evidence"]["kind"] == "policy"
    assert result["preflight_evidence"]["cleanup_status"] == "RECEIPT_CLEANED_AND_LEASE_RELEASED"
    assert not (cli_project["root"] / profile.execution_state_path).exists()


def test_three_case_profiles_use_exact_preflight_capabilities_and_leave_no_receipt(cli_project, monkeypatch, capsys):
    from eval.m6_1.fixture_read_models import FixtureOrderRepository
    from app.m3.policy_tools import PolicyReadService

    policy, policy_auth, policy_budget = authorize_scoped_case(cli_project, "D1-POLICY-013")
    assert policy.allowed_tools == frozenset({"search_knowledge"})
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-POLICY-013", policy_auth, policy_budget) == 0
    policy_result = json.loads(capsys.readouterr().out)
    assert policy_result["preflight_evidence"]["kind"] == "policy"
    assert policy_result["preflight_evidence"]["read_calls"] == 1
    assert policy_result["run_created"] is False and policy_result["execution_opportunity_consumed"] is False

    order, order_auth, order_budget = authorize_scoped_case(cli_project, "D1-ORDER-007")
    assert order.allowed_tools == frozenset({"get_order"})
    # This test isolates the case-specific preflight contract; predecessor blocking
    # is exercised separately by the r2 checker tests.
    from eval.m6_1 import personal_dev_diagnostic_checks
    monkeypatch.setattr(personal_dev_diagnostic_checks, "require_diagnostic_predecessor",
        lambda *_args, **_kwargs: None)
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-ORDER-007", order_auth, order_budget) == 0
    order_result = json.loads(capsys.readouterr().out)
    order_preflight = order_result["preflight_evidence"]
    assert order_preflight["kind"] == "order"
    assert order_preflight["order_id"] == "ORD-SYN-101"
    assert order_preflight["stored_status"] == "PAID"
    assert order_preflight["read_calls"] == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ORDER-009 preflight must not query policy or order business reads")

    monkeypatch.setattr(PolicyReadService, "search", forbidden)
    monkeypatch.setattr(FixtureOrderRepository, "get_order", forbidden)
    no_tool, no_tool_auth, no_tool_budget = authorize_scoped_case(cli_project, "D1-ORDER-009")
    assert no_tool.allowed_tools == frozenset()
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-ORDER-009", no_tool_auth, no_tool_budget) == 0
    no_tool_result = json.loads(capsys.readouterr().out)
    assert no_tool_result["preflight_evidence"]["kind"] == "none"
    assert no_tool_result["preflight_evidence"]["read_calls"] == 0

    engine, factory = cli_project["target"].open()
    try:
        with factory() as db:
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
            assert db.execute(text("SELECT COUNT(*) FROM d1_fixture_facts")).scalar_one() == 0
            assert db.execute(select(Run)).first() is None
    finally:
        engine.dispose()


def test_order_case_rejects_incomplete_predecessor_before_fixture_or_run_creation(cli_project, monkeypatch, capsys):
    from eval.m6_1.personal_dev_diagnostic_checks import fail_closed_diagnostic_report

    profile, auth_path, budget_path = authorize_scoped_case(cli_project, "D1-ORDER-007")
    root = cli_project["root"]
    predecessor = personal_diagnostic_profile("D1-POLICY-013")
    report_rel = (Path(profile.report_dir) /
        f"D1-POLICY-013-diagnostic-check-{profile.report_suffix}.json").as_posix()
    archive_rel = "docs/eval/personal-dev-runs/offline-stop-predecessor.json"
    archive_path = root / archive_rel
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive = {"case_id": "D1-POLICY-013", "result": {"status": "UNKNOWN", "run_id": None}}
    archive_bytes = json.dumps(archive, ensure_ascii=False).encode("utf-8")
    archive_path.write_bytes(archive_bytes)
    checks = fail_closed_diagnostic_report(case_id="D1-POLICY-013", reason="synthetic-stop")
    report = {"schema_version": "2.0", "phase": "EXECUTION_CLOSEOUT",
        "classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED", "case_id": "D1-POLICY-013",
        "authorization_id": "OFFLINE-POLICY-STOP", "diagnostic_round_id": profile.round_id,
        "code_sha256": CODE_SHA256, "database_identity": cli_project["database_identity"],
        "asset_sha256": AUTHORIZED_ASSET_SHA256,
        "rules_sha256": file_sha256(root / predecessor.check_rules_path),
        "archive_path": archive_rel, "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "checks": checks,
        "cleanup_evidence": {"cleanup_status": "CLEANED_AND_RELEASED", "lease_released": True}}
    report_path = root / report_rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    report_path.write_bytes(report_bytes)
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    independent_rel = (Path(predecessor.report_dir) /
        f"D1-POLICY-013-independent-check-{predecessor.report_suffix}.json").as_posix()
    independent_path = root / independent_rel
    independent_path.parent.mkdir(parents=True, exist_ok=True)
    independent_path.write_text(json.dumps({"schema_version": "1.0", "case_id": "D1-POLICY-013",
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
        "diagnostic_round_id": predecessor.round_id, "status": "RECOMPUTED_CONSISTENT",
        "execution_report_path": report_rel, "execution_report_sha256": report_sha,
        "raw_archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "rules_sha256": report["rules_sha256"], "matches_execution_report": True,
        "recomputed_checks": checks}, ensure_ascii=False), encoding="utf-8")
    outcome = {"diagnostic_decision": "STOP", "diagnostic_check_report": report_rel,
        "diagnostic_check_report_sha256": report_sha, "run_id": None,
        "cleanup_status": "CLEANED_AND_RELEASED"}
    prior_auth = {"authorization_id": report["authorization_id"],
        "diagnostic_round_id": profile.round_id, "status": "CLOSED_STOPPED",
        "code_sha256": CODE_SHA256, "database_identity": cli_project["database_identity"],
        "asset_sha256": AUTHORIZED_ASSET_SHA256, "execution_outcome": outcome}
    prior_auth_path = root / predecessor.authorization_path
    prior_auth_path.parent.mkdir(parents=True, exist_ok=True)
    prior_auth_path.write_text(json.dumps(prior_auth), encoding="utf-8")
    prior_state = {"authorization_id": prior_auth["authorization_id"],
        "diagnostic_round_id": profile.round_id, "status": "CLOSED_STOPPED",
        "execution_outcome": outcome}
    state_path = root / predecessor.execution_state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    auth["predecessor_report_sha256"] = report_sha
    auth_path.write_text(json.dumps(auth), encoding="utf-8")
    monkeypatch.setattr(cli_project["cli"].D1Coordinator, "prepare",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fixture prepare must not run")))
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-ORDER-007", auth_path, budget_path) == 2
    rejection = json.loads(capsys.readouterr().out)
    assert rejection["reason"] == "DIAGNOSTIC_PREDECESSOR_INDEPENDENT_RECOMPUTATION_MISMATCH:D1-POLICY-013"
    assert rejection["run_created"] is False
    assert rejection["lease_active"] is False
    engine, factory = cli_project["target"].open()
    try:
        with factory() as db:
            assert db.execute(select(Run)).first() is None
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
            assert db.execute(text("SELECT COUNT(*) FROM d1_fixture_facts")).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(("field", "value", "message"), [
    ("allowed_tools", ["get_order"], "PERSONAL_AUTHORIZATION_CAPABILITIES_MISMATCH"),
    ("check_rules_sha256", "0" * 64, "PERSONAL_DIAGNOSTIC_CHECK_RULES_MISMATCH"),
])
def test_three_case_cli_rejects_locked_capability_or_checker_mutation(cli_project, monkeypatch, capsys,
        field, value, message):
    _profile, authorization_path, budget_path = authorize_scoped_case(cli_project, "D1-POLICY-013")
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization[field] = value
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
    assert invoke_authorized_case(cli_project, monkeypatch, "D1-POLICY-013", authorization_path, budget_path) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["admission"] == "REJECTED"
    assert message in output["reason"]
    assert not (cli_project["root"] / personal_diagnostic_profile("D1-POLICY-013").execution_state_path).exists()


@pytest.mark.parametrize("change,reason", [
    ("wrong_case", "PERSONAL_DIAGNOSTIC_CASE_NOT_ALLOWLISTED"),
    ("wrong_auth_case", "SINGLE_ALLOWLISTED_CASE_REQUIRED"),
    ("wrong_code", "PERSONAL_AUTHORIZATION_SCOPE_MISMATCH:code_sha256"),
    ("wrong_database", "PERSONAL_AUTHORIZATION_SCOPE_MISMATCH:database_identity"),
    ("expired", "PERSONAL_AUTHORIZATION_EXPIRED_OR_INVALID"),
    ("closed", "FINAL_USER_CONFIRMATION_REQUIRED"),
    ("budget_bytes", "PERSONAL_AUTHORIZATION_BUDGET_FILE_MISMATCH"),
    ("budget_path", "PERSONAL_DIAGNOSTIC_AUTH_OR_BUDGET_PATH_MISMATCH"),
])
def test_cli_rejects_case_binding_authorization_and_budget_mismatches(cli_project, monkeypatch, capsys, change, reason):
    auth_path = cli_project["authorization_path"]
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    budget_path = cli_project["budget_path"]
    selected_case = "D1-PRODUCT-001"
    selected_auth = auth_path
    selected_budget = budget_path
    if change == "wrong_case":
        selected_case = "D1-ORDER-008"
    elif change == "wrong_auth_case":
        auth["case_ids"] = ["D1-POLICY-013"]
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
    elif change == "wrong_code":
        auth["code_sha256"] = "0" * 64
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
    elif change == "wrong_database":
        auth["database_identity"] = "different-synthetic-db"
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
    elif change == "expired":
        auth["confirmed_at_utc"] = "2020-01-01T00:00:00Z"
        auth["expires_at_utc"] = "2020-01-01T01:00:00Z"
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
    elif change == "closed":
        auth["status"] = "CLOSED_STOPPED"
        auth_path.write_text(json.dumps(auth), encoding="utf-8")
    elif change == "budget_bytes":
        budget_path.write_text(budget_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif change == "budget_path":
        selected_budget = cli_project["root"] / personal_diagnostic_profile("D1-POLICY-013").budget_path
    try:
        exit_code = invoke(cli_project, monkeypatch, case_id=selected_case,
            authorization_path=selected_auth, budget_path=selected_budget)
    except (D1PreflightError, PersonalAuthorizationError) as exc:
        assert reason in str(exc)
    else:
        assert exit_code == 2
        # The CLI reports a rejected admission without creating a Run or
        # consuming the one-time authorization.
        assert reason in capsys.readouterr().out
    assert not (cli_project["root"] / cli_project["profile"].execution_state_path).exists()


def test_cli_execution_dispatch_is_one_shot_and_second_use_is_rejected(cli_project, monkeypatch, capsys):
    calls = []

    def offline_execution_dispatch(_self, *, plan, case_id, ledger, settings, **_kwargs):
        calls.append(case_id)
        assert plan["_personal_admission_evidence"]["kind"] == "product"
        assert ledger.limits.provider_calls == 4
        del settings
        # Synthetic dispatcher response: this test verifies CLI authorization
        # wiring only and does not represent a real Run or Agent event.
        return {"status": "UNKNOWN", "run_id": None, "observations": {"run_events": []},
            "read_calls": 0, "result": None}

    monkeypatch.setattr(cli_project["cli"].D1Coordinator, "_execute_validated_plan", offline_execution_dispatch)
    assert invoke(cli_project, monkeypatch, mode="--execute") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "UNKNOWN"
    assert result["authorization_status"] == "CLOSED_STOPPED"
    assert result["agent_started"] is False
    assert result["run_created"] is False
    assert result["provider_requests"] == 0
    assert calls == ["D1-PRODUCT-001"]
    authorization = json.loads(cli_project["authorization_path"].read_text(encoding="utf-8"))
    state = json.loads((cli_project["root"] / cli_project["profile"].execution_state_path).read_text(encoding="utf-8"))
    assert authorization["status"] == "CLOSED_STOPPED"
    assert state["status"] == "CLOSED_STOPPED"
    with pytest.raises(D1PreflightError, match="PERSONAL_AUTHORIZATION_ALREADY_USED_NO_REPLAY"):
        invoke(cli_project, monkeypatch, mode="--execute")
    assert calls == ["D1-PRODUCT-001"]


@pytest.mark.parametrize(("outcome", "items", "expected", "blocking", "review"), [
    ("CONTINUE", [{"id": "evidence", "status": "PASS", "blocking": True,
        "reason": "verified"}], "CONTINUE", 0, 0),
    ("STOP", [{"id": "evidence", "status": "UNKNOWN", "blocking": True,
        "reason": "missing"}], "STOP", 1, 0),
    ("REVIEW_REQUIRED", [{"id": "semantic", "status": "UNKNOWN", "blocking": False,
        "reason": "needs review"}],
     "REVIEW_REQUIRED", 0, 1),
])
def test_cli_diagnostic_projection_verifies_and_counts_decisions(cli_project, outcome, items,
        expected, blocking, review):
    path = cli_project["root"] / "docs/eval/projection/diagnostic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    blocker_ids = [item["id"] for item in items if item["blocking"] and item["status"] in {"FAIL", "UNKNOWN"}]
    review_ids = [item["id"] for item in items if not item["blocking"] and item["status"] == "UNKNOWN"]
    reasons = [item["id"] + ":" + item["status"] for item in items
        if item["blocking"] and item["status"] in {"FAIL", "UNKNOWN"}]
    document = {"classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED", "case_id": "D1-ORDER-009",
        "cleanup_evidence": {"cleanup_status": "CLEANED_AND_RELEASED"},
        "checks": {"checker_version": "m6.1-d1-diagnostic-checker-r4", "case_id": "D1-ORDER-009",
            "task_outcome": "UNKNOWN", "classification": "PROVISIONAL_DEV_CHECKS",
            "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
            "check_items": items, "stop_reasons": reasons,
            "decision": {"outcome": outcome, "decision_policy": "1.0", "reasons": reasons,
                "blocking_items": blocker_ids, "review_items": review_ids}}}
    path.write_text(json.dumps(document), encoding="utf-8")
    relative = path.relative_to(cli_project["root"]).as_posix()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    projection = diagnostic_cli_projection(cli_project["root"], relative, sha)
    assert projection["diagnostic_report_integrity"] == "VERIFIED"
    assert projection["diagnostic_decision"] == expected
    assert projection["blocking_item_count"] == blocking
    assert projection["review_item_count"] == review
    assert projection["grading_status"] == "NOT_GRADED"


def test_cli_diagnostic_projection_fails_closed_on_missing_hash_or_unknown_status(cli_project):
    missing = diagnostic_cli_projection(cli_project["root"], "docs/eval/missing-diagnostic.json", "a" * 64)
    assert missing["diagnostic_decision"] == "STOP"
    assert missing["diagnostic_report_error"] == "DIAGNOSTIC_REPORT_UNAVAILABLE"
    path = cli_project["root"] / "docs/eval/projection-invalid/diagnostic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    invalid = {"classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED", "case_id": "D1-ORDER-009", "cleanup_evidence": {"cleanup_status": "CLEANED_AND_RELEASED"},
        "checks": {"checker_version": "m6.1-d1-diagnostic-checker-r4", "case_id": "D1-ORDER-009",
            "task_outcome": "UNKNOWN", "classification": "PROVISIONAL_DEV_CHECKS",
            "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
            "stop_reasons": ["bad:UNSURE"],
            "check_items": [{"id": "bad", "status": "UNSURE", "blocking": True, "reason": "bad"}],
            "decision": {"outcome": "STOP", "decision_policy": "1.0", "reasons": ["bad:UNSURE"],
                "blocking_items": ["bad"], "review_items": []}}}
    path.write_text(json.dumps(invalid), encoding="utf-8")
    relative = path.relative_to(cli_project["root"]).as_posix()
    wrong_hash = diagnostic_cli_projection(cli_project["root"], relative, "0" * 64)
    assert wrong_hash["diagnostic_report_error"] == "DIAGNOSTIC_REPORT_HASH_MISMATCH"
    wrong_state = diagnostic_cli_projection(cli_project["root"], relative,
        hashlib.sha256(path.read_bytes()).hexdigest())
    assert wrong_state["diagnostic_report_error"] == "DIAGNOSTIC_REPORT_FIELDS_INVALID"
    assert wrong_state["diagnostic_decision"] == "STOP"


def test_cli_diagnostic_projection_rejects_conflicting_top_level_and_nested_metadata(cli_project):
    path = cli_project["root"] / "docs/eval/projection-conflict/diagnostic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED", "case_id": "D1-ORDER-009", "checks": {"classification": "FORMAL_GOLD",
            "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
            "check_items": [], "decision": {"outcome": "CONTINUE", "blocking_items": [], "review_items": []}}}
    path.write_text(json.dumps(document), encoding="utf-8")
    relative = path.relative_to(cli_project["root"]).as_posix()
    projection = diagnostic_cli_projection(cli_project["root"], relative,
        hashlib.sha256(path.read_bytes()).hexdigest())
    assert projection["diagnostic_report_integrity"] == "FAIL"
    assert projection["diagnostic_report_error"] == "DIAGNOSTIC_REPORT_METADATA_CONFLICT"
    assert projection["diagnostic_decision"] == "STOP"
