"""Exact profile and authorization binding tests for CROSSAGENT-030."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH,
    AUTHORIZED_ASSET_SHA256,
    AUTHORIZED_VERSION,
    PersonalAuthorizationError,
    file_sha256,
    load_price_evidence,
    personal_diagnostic_profile,
    validate_personal_authorization,
)
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.personal_dev import validate_execution_budget
from eval.m6_1.personal_dev_authorization import validate_personal_budget


ROOT = Path(__file__).resolve().parents[1]
CASE = "D1-CROSSAGENT-030"
ROUND = "M62-CA030-MULTI-R1-20261029"


def _binding():
    profile = personal_diagnostic_profile(CASE, diagnostic_round_id=ROUND)
    budget = {"provider_calls": 6, "embedding_calls": 0, "judge_calls": 0,
        "total_tokens": 120000, "estimated_usd": 0.18, "tool_calls": 3,
        "max_calls_per_trial": 3, "max_concurrent_trials": 1,
        "max_duration_seconds": 180, "max_retries": 0}
    limits = BudgetLimits(**budget)
    validate_execution_budget(limits, "For ORD-SYN-114, check the warranty rule and tell me which fact is missing.")
    price, price_sha = load_price_evidence(ROOT / profile.pricing_path,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    del price
    now = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
    rules_sha = file_sha256(ROOT / profile.check_rules_path)
    auth = {
        "schema_version": "1.0",
        "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING",
        "status": "USER_CONFIRMED",
        "authorization_id": "SYNTHETIC-CA030-AUTH",
        "diagnostic_round_id": ROUND,
        "purpose": "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.",
        "grading_status": "NOT_GRADED",
        "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "confirmed_at_utc": "2026-10-29T02:00:00Z",
        "expires_at_utc": "2026-10-29T04:00:00Z",
        "case_ids": [CASE],
        "allowed_tools": sorted(profile.allowed_tools),
        "check_rules_sha256": rules_sha,
        "asset_id": "m6.1-d1-synthetic-dev-draft",
        "data_version": AUTHORIZED_VERSION,
        "asset_path": AUTHORIZED_ASSET_PATH,
        "asset_sha256": AUTHORIZED_ASSET_SHA256,
        "code_sha256": "a" * 64,
        "code_revision": "SYNTHETIC-REVISION",
        "database_identity": "SYNTHETIC-DB-IDENTITY",
        "model": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com",
        "pricing_sha256": price_sha,
        "budget": budget,
        "budget_sha256": sha256_json(budget),
        "budget_file_sha256": sha256_json(budget),
        "limits": {"provider_calls": 6, "tool_calls": 3, "total_tokens": 120000,
            "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1,
            "automatic_retries": 0, "embedding_calls": 0, "judge_calls": 0},
        "protected_assets_allowed": False,
        "formal_gates_changed": False,
    }
    args = dict(case_id=CASE, asset_id="m6.1-d1-synthetic-dev-draft",
        data_version=AUTHORIZED_VERSION, asset_path=AUTHORIZED_ASSET_PATH,
        asset_sha256=AUTHORIZED_ASSET_SHA256, code_sha256="a" * 64,
        code_revision="SYNTHETIC-REVISION", database_identity="SYNTHETIC-DB-IDENTITY",
        model="deepseek-flash", endpoint="https://api.deepseek.com", budget=budget,
        budget_file_sha256_value=sha256_json(budget), pricing_sha256_value=price_sha,
        check_rules_sha256_value=rules_sha, profile=profile, now=now)
    return profile, auth, args


def test_crossagent030_profile_is_exact_and_has_user_caps():
    profile = personal_diagnostic_profile(CASE, diagnostic_round_id=ROUND)
    assert profile.case_id == CASE
    assert profile.allowed_tools == frozenset({"get_order", "get_order_items", "get_warranty"})
    assert profile.preflight_kind == "crossagent_warranty"
    assert profile.budget_limits == {"provider_calls": 6, "tool_calls": 3,
        "total_tokens": 120000, "estimated_usd": 0.18, "max_duration_seconds": 180,
        "max_concurrent_trials": 1, "max_retries": 0, "embedding_calls": 0,
        "judge_calls": 0, "max_calls_per_trial": 3}
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_DIAGNOSTIC_CASE_NOT_ALLOWLISTED"):
        personal_diagnostic_profile("D1-CROSSAGENT-029", diagnostic_round_id=ROUND)
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_DIAGNOSTIC_CASE_NOT_ALLOWLISTED"):
        personal_diagnostic_profile(CASE, diagnostic_round_id="another-round")


def test_exact_crossagent030_authorization_binding_passes():
    _profile, auth, args = _binding()
    validate_personal_authorization(auth, **args)


@pytest.mark.parametrize(("key", "mutate"), [
    ("case_ids", lambda auth: auth.update(case_ids=["D1-CROSSAGENT-029"])),
    ("allowed_tools", lambda auth: auth.update(allowed_tools=["get_order", "search_knowledge"])),
    ("code_sha256", lambda auth: auth.update(code_sha256="b" * 64)),
    ("database_identity", lambda auth: auth.update(database_identity="other-db")),
    ("pricing_sha256", lambda auth: auth.update(pricing_sha256="c" * 64)),
    ("check_rules_sha256", lambda auth: auth.update(check_rules_sha256="d" * 64)),
])
def test_crossagent030_authorization_rejects_binding_drift(key, mutate):
    _profile, auth, args = _binding()
    mutate(auth)
    with pytest.raises(PersonalAuthorizationError):
        validate_personal_authorization(auth, **args)


def test_crossagent030_budget_rejects_provider_tool_or_cost_expansion():
    profile, _auth, _args = _binding()
    for key, value in (("provider_calls", 7), ("tool_calls", 4), ("estimated_usd", 0.19)):
        budget = {"provider_calls": 6, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 120000, "estimated_usd": 0.18, "tool_calls": 3,
            "max_calls_per_trial": 3, "max_concurrent_trials": 1,
            "max_duration_seconds": 180, "max_retries": 0}
        budget[key] = value
        with pytest.raises(Exception):
            validate_personal_budget(BudgetLimits(**budget),
                "For ORD-SYN-114, check the warranty rule and tell me which fact is missing.",
                budget_config=budget, profile=profile)
