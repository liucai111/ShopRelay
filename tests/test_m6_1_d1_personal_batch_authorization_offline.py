import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest

from eval.m6_1.budget import BudgetLimits, BudgetStop
from eval.m6_1.contracts import ContractError, EvalExperiment, redact, sha256_json
from eval.m6_1.personal_dev import plan_case
from eval.m6_1.personal_dev_authorization import (
    BATCH_AUTHORIZATION_TYPE, PersonalAuthorizationError,
    validate_personal_batch_authorization,
)
from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger
from eval.m6_1.personal_dev_scoring import diagnose_personal_d1_run, classify_personal_d1_execution
from eval.m6_1.budget import BudgetLedger
from eval.m6_1.store import ResultStore


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"


def test_all_r324_cases_produce_read_only_plans_in_original_order():
    bundle = json.loads(ASSET.read_text(encoding="utf-8"))
    plans = [plan_case(bundle, row["case_id"]) for row in bundle["cases"]]
    assert len(plans) == 40
    assert [row["case_id"] for row in plans] == [row["case_id"] for row in bundle["cases"]]
    assert len({row["case_id"] for row in plans}) == 40
    for plan in plans:
        assert all(call["mode"] == "read_only" for call in plan["case"]["input"].get("expected_tool_calls", []))
    assert "D1-POLICY-015" in [row["case_id"] for row in plans]
    assert "D1-POLICY-016" in [row["case_id"] for row in plans]


def _budget():
    return {
        "case_count": 40,
        "per_case": {"provider_calls": 4, "tool_calls": 4, "total_tokens": 120000,
            "estimated_usd": 0.18, "max_duration_seconds": 180, "max_concurrent_trials": 1,
            "max_retries": 0, "embedding_calls": 0, "judge_calls": 0},
        "batch": {"provider_calls": 160, "tool_calls": 160, "total_tokens": 4800000,
            "estimated_usd": 7.20, "max_duration_seconds": 180, "max_concurrent_trials": 1,
            "max_retries": 0, "embedding_calls": 0, "judge_calls": 0},
        "provider_hard_spend_cap": False,
    }


def _authorization(case_ids, budget, now):
    return {
        "schema_version": "1.0", "authorization_type": BATCH_AUTHORIZATION_TYPE,
        "status": "USER_CONFIRMED", "authorization_id": "batch-test-r1",
        "purpose": "One-time 40-case personal Dev Agent evaluation; not a formal Gold, Frozen Test, or M2 result.",
        "result_scope": "PERSONAL_DEV_ONLY", "grading_status": "PERSONAL_DEV_ONLY",
        "budget_ref": "docs/eval/m6.1-d1-personal-dev-preparation-r2/21_personal-dev-batch-budget-r1.json",
        "authorized_by": "CHAT_USER_SELF_CONFIRMED",
        "actor_type": "CHAT_USER_SELF_CONFIRMED", "confirmed_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(hours=4)).isoformat(), "case_ids": case_ids,
        "asset_id": "m6.1-d1-synthetic-dev-draft", "data_version": "m6.1-d1-dev-draft-r3.2.4",
        "asset_path": "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json",
        "asset_sha256": "a" * 64, "code_sha256": "b" * 64, "code_revision": "c" * 40,
        "database_identity": "d" * 32, "model": "deepseek-flash",
        "provider_endpoint": "https://api.deepseek.com", "pricing_sha256": "e" * 64,
        "budget_file_sha256": "f" * 64, "budget": budget, "budget_sha256": sha256_json(budget),
        "protected_assets_allowed": False, "formal_gates_changed": False,
    }


def test_batch_authorization_exact_scope_and_expiry():
    bundle = json.loads(ASSET.read_text(encoding="utf-8"))
    ids = [row["case_id"] for row in bundle["cases"]]
    budget = _budget()
    now = datetime.now(timezone.utc)
    auth = _authorization(ids, budget, now - timedelta(minutes=1))
    kwargs = dict(case_ids=ids, asset_sha256="a" * 64, code_sha256="b" * 64,
        code_revision="c" * 40, database_identity="d" * 32, model="deepseek-flash",
        endpoint="https://api.deepseek.com", budget=budget, budget_file_sha256_value="f" * 64,
        pricing_sha256_value="e" * 64, now=now)
    validate_personal_batch_authorization(auth, **kwargs)
    auth["case_ids"] = ids[:-1]
    with pytest.raises(PersonalAuthorizationError, match="EXACT_40_CASE_ALLOWLIST_REQUIRED"):
        validate_personal_batch_authorization(auth, **kwargs)


def test_new_batch_authorization_binds_budget_under_its_unique_batch_directory():
    bundle = json.loads(ASSET.read_text(encoding="utf-8"))
    ids = [row["case_id"] for row in bundle["cases"]]
    budget = _budget()
    now = datetime.now(timezone.utc)
    auth = _authorization(ids, budget, now - timedelta(minutes=1))
    ref = "docs/eval/personal-dev-batches/M61D1-BATCH-R324-NEW/ budget.json".replace("/ ", "/")
    auth["budget_ref"] = ref
    kwargs = dict(case_ids=ids, asset_sha256="a" * 64, code_sha256="b" * 64,
        code_revision="c" * 40, database_identity="d" * 32, model="deepseek-flash",
        endpoint="https://api.deepseek.com", budget=budget, budget_file_sha256_value="f" * 64,
        pricing_sha256_value="e" * 64, expected_budget_ref=ref, now=now)
    validate_personal_batch_authorization(auth, **kwargs)


def test_real_d1_diagnostic_adapter_uses_persisted_trace_and_never_grades():
    result = {"status": "SUCCEEDED", "observations": {
        "runs": [{"status": "SUCCEEDED"}],
        "messages": [{"role": "assistant", "content": "已查询。"}],
        "run_events": [{"event_type": "agent_started", "payload_json": "{}"},
            {"event_type": "tool_started", "payload_json": "{\"tool_id\":\"get_order\"}"}],
        "traces": [{"event_type": "orchestrator_selected"}],
    }}
    diagnostic = diagnose_personal_d1_run(case_id="D1-CROSSAGENT-033",
        expected_tool_calls=[{"tool_id": "get_order", "mode": "read_only"}], result=result,
        trial_budget={"calls_by_category": {"provider": 1}, "tokens": 6966, "unknown_tokens": True})
    assert diagnostic["agent_started"] is True
    assert diagnostic["observed_tool_ids"] == ["get_order"]
    assert diagnostic["task_outcome"] == diagnostic["semantic_score"] == "UNKNOWN"
    assert diagnostic["safety_assessment"] == "UNKNOWN"
    assert diagnostic["grading_status"] == "NOT_GRADED"


def test_terminal_direct_fallback_is_not_counted_as_agent_execution_or_success():
    result = {"status": "SUCCEEDED", "run_id": "run-1", "result": {"route": {
        "strategy": "DIRECT", "decision_source": "FALLBACK", "fallback_error": "APIConnectionError: proxy refused"}},
        "observations": {"runs": [{"run_id": "run-1", "status": "SUCCEEDED"}],
            "messages": [], "run_events": [], "traces": []}}
    diagnostic = diagnose_personal_d1_run(case_id="D1-PRODUCT-001", expected_tool_calls=[], result=result,
        trial_budget={"calls_by_category": {"provider": 2}, "tokens": 13932,
            "unknown_tokens": True, "estimated_usd": 0.0167184, "unknown_cost": True})
    classification = classify_personal_d1_execution(diagnostic, result)
    assert diagnostic["run_created"] is True
    assert diagnostic["agent_started"] is False
    assert classification["run_status"] == "SUCCEEDED"
    assert classification["execution_status"] == "UNSCORABLE_ENVIRONMENT_ERROR"
    assert classification["task_outcome"] == "UNKNOWN"


@pytest.mark.parametrize("route", ["DIRECT", "REFUSE"])
def test_terminal_non_agent_route_is_separate_from_agent_execution_and_quality(route):
    result = {"status": "SUCCEEDED", "run_id": "run-non-agent", "result": {"route": {
        "strategy": route, "decision_source": "RULE"}},
        "observations": {"runs": [{"run_id": "run-non-agent", "status": "SUCCEEDED"}],
            "messages": [], "run_events": [{"event_type": "route_decided", "payload_json": "{}"}], "traces": []}}
    diagnostic = diagnose_personal_d1_run(case_id="D1-ORDER-009", expected_tool_calls=[], result=result)
    classification = classify_personal_d1_execution(diagnostic, result)
    assert classification["execution_status"] == "NON_AGENT_ROUTE_TERMINAL_UNSCORED"
    assert classification["agent_started"] is False
    assert classification["task_outcome"] == classification["quality_score"] == "UNKNOWN"
    assert classification["grading_status"] == "NOT_GRADED"


def test_persisted_agent_started_event_is_required_for_agent_started_count():
    run = {"status": "SUCCEEDED", "run_id": "run-2"}
    result = {"status": "SUCCEEDED", "run_id": "run-2", "result": {"route": {
        "strategy": "DIRECT", "decision_source": "FALLBACK"}},
        "observations": {"runs": [run], "messages": [], "run_events": [
            {"event_type": "run_claimed", "payload_json": "{}"}], "traces": []}}
    diagnostic = diagnose_personal_d1_run(case_id="D1-PRODUCT-001", expected_tool_calls=[], result=result)
    assert diagnostic["run_created"] is True
    assert diagnostic["agent_started"] is False


class _MemoryLedger:
    def __init__(self):
        self.usage = {}
        self.events = 0

    def trial_snapshot(self, trial_id):
        return self.usage.setdefault(trial_id, {"calls_by_category": {}, "tokens": 0,
            "estimated_usd": 0.0, "unknown_tokens": False, "unknown_cost": False})

    def reserve(self, *, trial_id, category, calls=1, tokens_reserved=0,
                estimated_usd_reserved=0.0, retry_index=None):
        row = self.trial_snapshot(trial_id)
        row["calls_by_category"][category] = row["calls_by_category"].get(category, 0) + calls
        row["tokens"] += tokens_reserved
        row["estimated_usd"] += estimated_usd_reserved
        self.events += 1
        return str(self.events)

    def settle(self, reservation_id, **kwargs):
        return None

    def snapshot(self):
        return {"events": self.events}


def test_per_case_provider_token_and_cost_caps_are_checked_before_reservation():
    global_ledger = _MemoryLedger()
    limits = BudgetLimits(provider_calls=4, tool_calls=4, total_tokens=120000,
        estimated_usd=0.18, max_calls_per_trial=4, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    per_case = PerCaseBudgetLedger(global_ledger, "D1-POLICY-013", limits)
    for _ in range(4):
        per_case.reserve(trial_id="D1-POLICY-013", category="provider", tokens_reserved=1000,
            estimated_usd_reserved=0.01)
    with pytest.raises(BudgetStop, match="provider request cap"):
        per_case.reserve(trial_id="D1-POLICY-013", category="provider", tokens_reserved=1,
            estimated_usd_reserved=0.001)
    assert global_ledger.events == 4


def test_per_case_unknown_usage_uses_reserved_upper_bound_and_stops_when_next_reservation_exceeds_cap():
    global_ledger = _MemoryLedger()
    limits = BudgetLimits(provider_calls=4, tool_calls=4, total_tokens=120000,
        estimated_usd=0.18, max_calls_per_trial=4, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    per_case = PerCaseBudgetLedger(global_ledger, "D1-POLICY-013", limits)
    usage = global_ledger.trial_snapshot("D1-POLICY-013")
    usage.update({"unknown_tokens": True, "unknown_cost": True, "tokens": 6966,
        "estimated_usd": 0.0083592, "calls_by_category": {"provider": 1}})
    per_case.reserve(trial_id="D1-POLICY-013", category="provider", tokens_reserved=1000,
        estimated_usd_reserved=0.01)
    usage["tokens"] = 119500
    with pytest.raises(BudgetStop, match="token budget exhausted"):
        per_case.reserve(trial_id="D1-POLICY-013", category="provider", tokens_reserved=1000,
            estimated_usd_reserved=0.001)
    assert global_ledger.events == 1


def test_redact_serializes_timezone_aware_datetimes_and_preserves_secret_redaction():
    stamp = datetime.fromisoformat("2026-10-26T15:00:00+08:00")
    value = redact({"nested": [{"created_at": stamp, "api_key": "secret-value"}],
        "text": "Authorization: Bearer abc.def"})
    assert value == {"nested": [{"created_at": "2026-10-26T07:00:00Z", "api_key": "[REDACTED]"}],
        "text": "Authorization: [REDACTED]"}
    with pytest.raises(ContractError, match="naive datetime"):
        redact({"created_at": datetime(2026, 10, 26)})


def test_real_sqlite_ledger_keeps_global_and_per_case_usage_durable():
    global_limits = BudgetLimits(provider_calls=160, tool_calls=160, total_tokens=4800000,
        estimated_usd=7.20, max_calls_per_trial=4, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    ledger_dir = ROOT / ".m6-d1-batch-ledger-offline"
    ledger_dir.mkdir(exist_ok=True)
    store = ResultStore(ledger_dir / f"batch-ledger-{uuid.uuid4().hex}.sqlite3")
    config = {"scope": "synthetic-test"}
    experiment = EvalExperiment(experiment_id="batch.test", config_snapshot=config,
        config_sha256=sha256_json(config), git_commit="a" * 40,
        knowledge_index_version=None, model_versions={"provider": "test"},
        prompt_versions={"application": "test"}, strategy="PERSONAL_DEV_AGENT_EVAL",
        tool_permissions=(), budget=global_limits.to_dict(), data_version="test-r1",
        scoring_version="PERSONAL_DEV_DIAGNOSTIC")
    store.lock_experiment(experiment, "2026-10-26T00:00:00Z")
    ledger = BudgetLedger(store, experiment.experiment_id, global_limits)
    per_case = PerCaseBudgetLedger(ledger, "D1-POLICY-013",
        BudgetLimits(provider_calls=4, tool_calls=4, total_tokens=120000, estimated_usd=.18,
            max_calls_per_trial=4, max_concurrent_trials=1, max_duration_seconds=180, max_retries=0))
    reservation = per_case.reserve(trial_id="D1-POLICY-013", category="provider",
        tokens_reserved=1000, estimated_usd_reserved=.01)
    per_case.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=500,
        estimated_usd=None, call_count_status="RECORDED")
    snapshot = ledger.trial_snapshot("D1-POLICY-013")
    assert snapshot["calls_by_category"]["provider"] == 1
    assert snapshot["tokens"] == 500
    assert snapshot["estimated_usd"] == .01
    # Provider billing is not inferred; the conservative reservation still permits
    # another call within the local USD cap, while missing token usage fails closed.
    reservation2 = per_case.reserve(trial_id="D1-POLICY-013", category="provider",
        tokens_reserved=1000, estimated_usd_reserved=.01)
    per_case.settle(reservation2, status="COMMIT", actual_calls=1, actual_tokens=500,
        estimated_usd=None, call_count_status="RECORDED")
    assert ledger.snapshot()["provider_requests"] == 2
