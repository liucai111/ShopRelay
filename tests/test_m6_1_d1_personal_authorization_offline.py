"""Synthetic tests for the separate, non-scoring personal Dev authorization path."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from eval.m6_1.personal_dev_authorization import (
    AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, AUTHORIZED_CASE, AUTHORIZED_VERSION,
    PersonalAuthorizationError, file_sha256, load_price_evidence, personal_diagnostic_profile,
    claim_personal_diagnostic_execution,
    validate_personal_authorization,
)
from eval.m6_1.contracts import sha256_json


ROOT = Path(__file__).resolve().parents[1]
POLICY_PROFILE = personal_diagnostic_profile("D1-POLICY-013")
PRICE_PATH = ROOT / POLICY_PROFILE.pricing_path
BUDGET_PATH = ROOT / POLICY_PROFILE.budget_path
CHECK_RULES_PATH = ROOT / POLICY_PROFILE.check_rules_path


@pytest.fixture(autouse=True)
def current_synthetic_price_snapshot(tmp_path, monkeypatch):
    """Give synthetic authorization tests a fresh local price snapshot."""
    record = json.loads(PRICE_PATH.read_text(encoding="utf-8"))
    record["checked_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    synthetic_path = tmp_path / "synthetic-current-pricing.json"
    synthetic_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(__import__(__name__, fromlist=["PRICE_PATH"]), "PRICE_PATH", synthetic_path)


def valid_auth():
    budget = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    price, price_sha = load_price_evidence(PRICE_PATH,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    del price
    check_rules_sha = file_sha256(CHECK_RULES_PATH)
    return {
        "schema_version": "1.0",
        "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING",
        "status": "USER_CONFIRMED",
        "authorization_id": "SYNTHETIC-OFFLINE-AUTH",
        "diagnostic_round_id": POLICY_PROFILE.round_id,
        "purpose": "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.",
        "grading_status": "NOT_GRADED",
        "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "confirmed_at_utc": "2026-10-26T12:00:00Z",
        "expires_at_utc": "2026-10-26T14:00:00Z",
        "case_ids": [AUTHORIZED_CASE],
        "allowed_tools": ["search_knowledge"],
        "check_rules_sha256": check_rules_sha,
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
        "budget_file_sha256": file_sha256(BUDGET_PATH),
        "limits": {"provider_calls": 4, "tool_calls": 4, "total_tokens": 120000,
            "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1,
            "automatic_retries": 0, "embedding_calls": 0, "judge_calls": 0},
        "protected_assets_allowed": False,
        "formal_gates_changed": False,
    }


def validate(auth, *, now=None):
    return validate_personal_authorization(auth, case_id=AUTHORIZED_CASE,
        asset_id="m6.1-d1-synthetic-dev-draft", data_version=AUTHORIZED_VERSION,
        asset_path=AUTHORIZED_ASSET_PATH, asset_sha256=AUTHORIZED_ASSET_SHA256,
        code_sha256="a" * 64, code_revision="SYNTHETIC-REVISION",
        database_identity="SYNTHETIC-DB-IDENTITY", model="deepseek-flash",
        endpoint="https://api.deepseek.com", budget=auth["budget"],
        budget_file_sha256_value=auth["budget_file_sha256"],
        pricing_sha256_value=auth["pricing_sha256"],
        check_rules_sha256_value=file_sha256(CHECK_RULES_PATH),
        now=now or datetime(2026, 10, 26, 13, tzinfo=timezone.utc))


def test_official_price_evidence_matches_alias_endpoint_and_peak_prices():
    record, digest = load_price_evidence(PRICE_PATH,
        model="deepseek-flash", endpoint="https://api.deepseek.com")
    assert digest == file_sha256(PRICE_PATH)
    assert record["peak"] == {"input_cache_miss": 0.30, "input_cache_hit": 0.006, "output": 1.20}


def test_one_case_authorization_validates_only_exact_non_scoring_scope():
    validate(valid_auth())


@pytest.mark.parametrize(("key", "value", "error"), [
    ("status", "AWAITING_FINAL_USER_CONFIRMATION", "FINAL_USER_CONFIRMATION_REQUIRED"),
    ("case_ids", ["D1-POLICY-014"], "SINGLE_ALLOWLISTED_CASE_REQUIRED"),
    ("asset_sha256", "0" * 64, "EXACT_DEV_ASSET_SCOPE_REQUIRED"),
    ("database_identity", "other-db", "PERSONAL_AUTHORIZATION_SCOPE_MISMATCH:database_identity"),
    ("grading_status", "SCORED", "PERSONAL_DIAGNOSTIC_MUST_REMAIN_NOT_GRADED"),
    ("protected_assets_allowed", True, "PROTECTED_OR_FORMAL_SCOPE_FORBIDDEN"),
    ("allowed_tools", ["get_order"], "PERSONAL_AUTHORIZATION_CAPABILITIES_MISMATCH"),
    ("check_rules_sha256", "0" * 64, "PERSONAL_DIAGNOSTIC_CHECK_RULES_MISMATCH"),
])
def test_authorization_rejects_unconfirmed_or_out_of_scope_mutation(key, value, error):
    auth = valid_auth()
    auth[key] = value
    with pytest.raises(PersonalAuthorizationError, match=error):
        validate(auth)


def test_expired_authorization_rejected():
    auth = valid_auth()
    auth["expires_at_utc"] = "2026-10-26T12:59:59Z"
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_AUTHORIZATION_EXPIRED_OR_INVALID"):
        validate(auth)


def test_limits_are_exact_and_do_not_allow_policy_to_weaken_formal_gate():
    auth = valid_auth()
    auth["limits"]["provider_calls"] = 5
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_AUTHORIZATION_LIMITS_MISMATCH"):
        validate(auth)


def test_order009_authorization_accepts_zero_business_tools_and_never_inherits_four_tool_cap():
    from eval.m6_1.budget import BudgetLimits
    from eval.m6_1.personal_dev_authorization import validate_personal_budget

    profile = personal_diagnostic_profile("D1-ORDER-009")
    budget_path = ROOT / profile.budget_path
    budget = json.loads(budget_path.read_text(encoding="utf-8"))
    limits = BudgetLimits(**budget)
    validate_personal_budget(limits, "synthetic refusal prompt", budget_config=budget,
        profile=profile)
    auth = valid_auth()
    now = datetime.now(timezone.utc)
    auth.update({"authorization_id": "SYNTHETIC-ORDER-009", "diagnostic_round_id": profile.round_id,
        "case_ids": ["D1-ORDER-009"], "allowed_tools": [],
        "predecessor_report_path": (Path(profile.report_dir) /
            "D1-ORDER-007-diagnostic-check-r3.json").as_posix(),
        "predecessor_report_sha256": "c" * 64,
        "confirmed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (now + __import__("datetime").timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "budget": budget, "budget_sha256": sha256_json(budget),
        "budget_file_sha256": file_sha256(budget_path),
        "limits": {"provider_calls": 4, "tool_calls": 0, "total_tokens": 120000,
            "estimated_usd": 0.18, "timeout_seconds": 180, "concurrency": 1,
            "automatic_retries": 0, "embedding_calls": 0, "judge_calls": 0}})
    validate_personal_authorization(auth, case_id="D1-ORDER-009",
        asset_id="m6.1-d1-synthetic-dev-draft", data_version=AUTHORIZED_VERSION,
        asset_path=AUTHORIZED_ASSET_PATH, asset_sha256=AUTHORIZED_ASSET_SHA256,
        code_sha256="a" * 64, code_revision="SYNTHETIC-REVISION",
        database_identity="SYNTHETIC-DB-IDENTITY", model="deepseek-flash",
        endpoint="https://api.deepseek.com", budget=budget,
        budget_file_sha256_value=file_sha256(budget_path),
        pricing_sha256_value=file_sha256(PRICE_PATH),
        check_rules_sha256_value=file_sha256(ROOT / profile.check_rules_path),
        profile=profile, now=now)
    invalid = dict(budget, tool_calls=4, max_calls_per_trial=4)
    with pytest.raises(PersonalAuthorizationError, match="PERSONAL_DEV_BUDGET_LIMITS_INVALID"):
        validate_personal_budget(BudgetLimits(**invalid), "synthetic refusal prompt",
            budget_config=invalid, profile=profile)
    auth = valid_auth()
    auth["formal_gates_changed"] = True
    with pytest.raises(PersonalAuthorizationError, match="PROTECTED_OR_FORMAL_SCOPE_FORBIDDEN"):
        validate(auth)


def test_formal_registry_grant_still_rejects_unreviewed_dev_asset():
    from eval.m6_1.contracts import ExposureStatus, IndependenceStatus, ReviewStatus, Scoreability, Split
    from eval.m6_1.registry import AccessGrant, DatasetAsset
    asset = DatasetAsset(asset_id="synthetic-unreviewed-dev", asset_type="EVAL_DATASET",
        version="r1", parent_asset_id=None, split=Split.DEV.value, case_count=1,
        source_path="eval/dev.json", sha256="b" * 64,
        review_status=ReviewStatus.NOT_REVIEWED.value, reviewer_id="UNKNOWN",
        reviewed_at_utc=None, review_method=None,
        independence_status=IndependenceStatus.NOT_VERIFIED.value,
        exposure_status=ExposureStatus.NOT_VERIFIED.value,
        scoreability=Scoreability.NOT_VERIFIED.value, locked_at_utc=None,
        access_policy="DEV_ONLY", limitations=())
    grant = AccessGrant(authorization_id="FORMAL-GRANT-PROBE", approved_by="test",
        approved_at_utc="2026-10-26T12:00:00Z", asset_ids=(asset.asset_id,),
        allowed_splits=(Split.DEV.value,), purpose="test", expires_at_utc="2026-10-26T14:00:00Z")
    assert grant.valid_for(asset, now=datetime(2026, 10, 26, 13, tzinfo=timezone.utc)) is False


def test_real_route_remains_unforced_but_case_tool_scope_is_enforced():
    from eval.m6_1.personal_dev import D1PreflightError, plan_case
    from eval.m6_1.personal_dev_runtime import validate_case_route_scope
    bundle = json.loads((ROOT / AUTHORIZED_ASSET_PATH).read_text(encoding="utf-8"))
    plan = plan_case(bundle, AUTHORIZED_CASE)
    route = {"strategy": "SINGLE", "required_capabilities": ["search_knowledge"]}
    assert validate_case_route_scope(plan, route) is route
    with pytest.raises(D1PreflightError, match="ACTUAL_ROUTE_OUTSIDE_CASE_READ_ONLY_SCOPE"):
        validate_case_route_scope(plan, {"strategy": "MULTI", "required_capabilities": ["get_coupon"]})


@pytest.mark.parametrize("case_id", ["D1-POLICY-013", "D1-ORDER-007", "D1-ORDER-009"])
def test_each_new_case_profile_has_an_independent_non_replayable_state_file(tmp_path, case_id):
    profile = personal_diagnostic_profile(case_id)
    auth_path = tmp_path / profile.authorization_path
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    authorization = {"status": "USER_CONFIRMED", "authorization_id": f"OFFLINE-{case_id}",
        "case_ids": [case_id], "diagnostic_round_id": profile.round_id}
    auth_path.write_text(json.dumps(authorization), encoding="utf-8")
    state_path = claim_personal_diagnostic_execution(root=tmp_path,
        authorization_path=auth_path, authorization=authorization)
    assert state_path == tmp_path / profile.execution_state_path
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "IN_USE"
    with pytest.raises(PersonalAuthorizationError):
        claim_personal_diagnostic_execution(root=tmp_path,
            authorization_path=auth_path, authorization=authorization)
