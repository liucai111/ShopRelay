"""Offline checks for the explicitly allowlisted R6-to-R7 order continuation."""
import json
from pathlib import Path
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from eval.m6_1.budget import BudgetLimits
from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, AUTHORIZED_VERSION,
    ORDER_DIAGNOSTIC_ROUND_ID, file_sha256, load_price_evidence,
    personal_diagnostic_profile, validate_personal_authorization, validate_personal_budget,
)
from eval.m6_1.personal_dev_diagnostic_checks import DiagnosticCheckError, require_diagnostic_predecessor


ROOT = Path(__file__).resolve().parents[1]


def _profile():
    return personal_diagnostic_profile("D1-ORDER-007", diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)


def _authorization(profile, *, database_identity="18d7eb0dd49840218ff63601c747c35b"):
    return {
        "predecessor_case_id": profile.predecessor_case_id,
        "predecessor_round_id": profile.predecessor_round_id,
        "predecessor_report_path": profile.predecessor_report_path,
        "predecessor_report_sha256": profile.predecessor_report_sha256,
        "predecessor_archive_path": profile.predecessor_archive_path,
        "predecessor_archive_sha256": profile.predecessor_archive_sha256,
        "predecessor_authorization_path": profile.predecessor_authorization_path,
        "predecessor_authorization_sha256": profile.predecessor_authorization_sha256,
        "predecessor_state_path": profile.predecessor_state_path,
        "predecessor_state_sha256": profile.predecessor_state_sha256,
        "predecessor_independent_report_path": profile.predecessor_independent_report_path,
        "predecessor_independent_report_sha256": profile.predecessor_independent_report_sha256,
        "predecessor_rules_sha256": profile.predecessor_rules_sha256,
        "predecessor_code_sha256": profile.predecessor_code_sha256,
        "code_sha256": "r7-code-digest",
        "database_identity": database_identity,
        "asset_sha256": AUTHORIZED_ASSET_SHA256,
        "model": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com",
        "pricing_sha256": "061d796e52495ed9563ede55b4371aea9b8672bc8841ead93ba3295d494e384d",
    }


def _copy_r6_evidence(root: Path):
    paths = [
        "docs/eval/d1-diagnostic-r6-policy013-20261027/SHA256SUMS-r6.md",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/closeout-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/closeout-r6.md",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/D1-POLICY-013-authorization-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/state/D1-POLICY-013-execution-state-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/reports/D1-POLICY-013-diagnostic-check-r6.json",
        "docs/eval/d1-diagnostic-r6-policy013-20261027/reports/D1-POLICY-013-independent-check-r6.json",
        "docs/eval/personal-dev-runs/66c785b79d01425e85f4d5eba1dfebe9.json",
        "docs/eval/d1-diagnostic-r3-20261027/locked-check-rules-r3.json",
    ]
    for rel in paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)


def test_r7_order007_accepts_exact_real_r6_predecessor():
    profile = _profile()
    assert profile.predecessor_code_sha256 == "336c8b10927352d35d69c940eeb3bc69817bc1f5584678fa66309f19293602da"
    require_diagnostic_predecessor(ROOT, case_id="D1-ORDER-007", profile=profile,
        authorization=_authorization(profile))


@pytest.mark.parametrize("mutation,expected", [
    ("wrong_hash", "PREDECESSOR_HASH_NOT_ALLOWLISTED"),
    ("report_stop", "PREDECESSOR_REPORT_HASH_MISMATCH"),
    ("report_replay", "PREDECESSOR_REPORT_HASH_MISMATCH"),
    ("independent_hash", "PREDECESSOR_INDEPENDENT_CHECK_HASH_MISMATCH"),
    ("missing_state", "PREDECESSOR_TERMINAL_RECORD_UNAVAILABLE"),
    ("database_identity", "PREDECESSOR_CLOSEOUT_BINDING_MISMATCH"),
    ("missing_report", "PREDECESSOR_REPORT_UNAVAILABLE"),
], ids=["hash", "stop", "replay", "independent", "state", "identity", "report"])
def test_bad_r6_predecessor(tmp_path, mutation, expected):
    profile = _profile()
    root = tmp_path / "r"
    root.mkdir()
    _copy_r6_evidence(root)
    auth = _authorization(profile)
    if mutation == "wrong_hash":
        auth["predecessor_report_sha256"] = "0" * 64
    elif mutation == "database_identity":
        auth["database_identity"] = "different-database"
    elif mutation == "missing_state":
        (root / profile.predecessor_state_path).unlink()
    elif mutation == "missing_report":
        (root / profile.predecessor_report_path).unlink()
    elif mutation in {"report_stop", "report_replay"}:
        report_path = root / profile.predecessor_report_path
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if mutation == "report_stop":
            report["checks"]["decision"]["outcome"] = "STOP"
        else:
            report["phase"] = "PRE_CLEANUP_DIAGNOSTIC_ONLY"
            report["replay_only"] = True
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    elif mutation == "independent_hash":
        path = root / profile.predecessor_independent_report_path
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(DiagnosticCheckError, match=expected):
        require_diagnostic_predecessor(root, case_id="D1-ORDER-007", profile=profile,
            authorization=auth)


def test_order009_profile_is_bound_to_r7_order007_and_zero_business_tools():
    profile = personal_diagnostic_profile("D1-ORDER-009", diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
    assert profile.predecessor_case_id == "D1-ORDER-007"
    assert profile.predecessor_round_id == ORDER_DIAGNOSTIC_ROUND_ID
    assert profile.predecessor_report_path.endswith("D1-ORDER-007-diagnostic-check-r7.json")
    assert profile.allowed_tools == frozenset()
    budget = json.loads((ROOT / profile.budget_path).read_text(encoding="utf-8"))
    assert budget["tool_calls"] == 0
    assert budget["provider_calls"] == 4 and budget["total_tokens"] == 120000


@pytest.mark.parametrize("case_id", ["D1-ORDER-007", "D1-ORDER-009"])
def test_r7_authorization_contract_locks_case_budget_and_lineage(case_id):
    profile = personal_diagnostic_profile(case_id, diagnostic_round_id=ORDER_DIAGNOSTIC_ROUND_ID)
    budget_path = ROOT / profile.budget_path
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    limits = BudgetLimits(**budget)
    if case_id == "D1-ORDER-009":
        assert limits.tool_calls == limits.max_calls_per_trial == 0
    validate_personal_budget(limits, "synthetic case input", budget_config=budget, profile=profile)
    price, price_sha = load_price_evidence(ROOT / profile.pricing_path,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    del price
    rules_sha = file_sha256(ROOT / profile.check_rules_path)
    now = datetime.now(timezone.utc)
    code_sha = "9" * 64
    predecessor_code_sha = profile.predecessor_code_sha256 or code_sha
    auth = {
        "schema_version": "1.0", "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING",
        "status": "USER_CONFIRMED", "authorization_id": "OFFLINE-R7-" + case_id,
        "diagnostic_round_id": ORDER_DIAGNOSTIC_ROUND_ID,
        "purpose": "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.",
        "grading_status": "NOT_GRADED", "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "confirmed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "case_ids": [case_id], "allowed_tools": sorted(profile.allowed_tools),
        "check_rules_sha256": rules_sha, "asset_id": "m6.1-d1-synthetic-dev-draft",
        "data_version": AUTHORIZED_VERSION, "asset_path": AUTHORIZED_ASSET_PATH,
        "asset_sha256": AUTHORIZED_ASSET_SHA256, "code_sha256": code_sha,
        "code_revision": "OFFLINE-R7-REVISION", "database_identity": "OFFLINE-R7-DB",
        "model": "deepseek-flash", "provider_endpoint": "https://api.deepseek.com",
        "pricing_sha256": price_sha, "budget": budget, "budget_sha256": sha256_json(budget),
        "budget_file_sha256": file_sha256(budget_path),
        "limits": {"provider_calls": budget["provider_calls"], "tool_calls": budget["tool_calls"],
            "total_tokens": budget["total_tokens"], "estimated_usd": budget["estimated_usd"],
            "timeout_seconds": budget["max_duration_seconds"],
            "concurrency": budget["max_concurrent_trials"],
            "automatic_retries": budget["max_retries"], "embedding_calls": 0, "judge_calls": 0},
        "protected_assets_allowed": False, "formal_gates_changed": False,
        "predecessor_case_id": profile.predecessor_case_id,
        "predecessor_round_id": profile.predecessor_round_id,
        "predecessor_code_sha256": predecessor_code_sha,
        "predecessor_report_path": profile.predecessor_report_path,
        "predecessor_report_sha256": profile.predecessor_report_sha256 or "a" * 64,
        "predecessor_archive_path": profile.predecessor_archive_path or
            "docs/eval/personal-dev-runs/offline-r7-order-007.json",
        "predecessor_archive_sha256": profile.predecessor_archive_sha256 or "b" * 64,
        "predecessor_authorization_path": profile.predecessor_authorization_path,
        "predecessor_authorization_sha256": profile.predecessor_authorization_sha256 or "c" * 64,
        "predecessor_state_path": profile.predecessor_state_path,
        "predecessor_state_sha256": profile.predecessor_state_sha256 or "d" * 64,
        "predecessor_independent_report_path": profile.predecessor_independent_report_path,
        "predecessor_independent_report_sha256": profile.predecessor_independent_report_sha256 or "e" * 64,
        "predecessor_rules_sha256": profile.predecessor_rules_sha256,
    }
    validate_personal_authorization(auth, case_id=case_id,
        asset_id="m6.1-d1-synthetic-dev-draft", data_version=AUTHORIZED_VERSION,
        asset_path=AUTHORIZED_ASSET_PATH, asset_sha256=AUTHORIZED_ASSET_SHA256,
        code_sha256=code_sha, code_revision="OFFLINE-R7-REVISION",
        database_identity="OFFLINE-R7-DB", model="deepseek-flash",
        endpoint="https://api.deepseek.com", budget=budget,
        budget_file_sha256_value=file_sha256(budget_path), pricing_sha256_value=price_sha,
        check_rules_sha256_value=rules_sha, profile=profile, now=now)
