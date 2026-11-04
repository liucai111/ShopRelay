"""Versioned zero-vs-unknown accounting contract on isolated synthetic ledgers."""
from datetime import datetime, timezone
import json

from eval.m6_1.budget import BudgetLedger, BudgetLimits, BUDGET_SNAPSHOT_SCHEMA_VERSION
from eval.m6_1.contracts import EvalExperiment, sha256_json
from eval.m6_1.store import ResultStore


def ledger(tmp_path, *, limits=None, experiment_id="BUDGET-CONTRACT-SYNTHETIC"):
    limits = limits or BudgetLimits(provider_calls=2, embedding_calls=1, judge_calls=1,
        total_tokens=10000, estimated_usd=0.2, tool_calls=3, max_calls_per_trial=3)
    store = ResultStore(tmp_path / f"{experiment_id}.sqlite3")
    config = {"kind": "isolated-synthetic-budget-contract"}
    experiment = EvalExperiment(experiment_id=experiment_id, config_snapshot=config,
        config_sha256=sha256_json(config), git_commit="SYNTHETIC-OFFLINE-REVISION",
        knowledge_index_version=None, model_versions={}, prompt_versions={}, strategy="offline",
        tool_permissions=(), budget=limits.to_dict(), data_version="synthetic-v1",
        scoring_version="not-scored")
    store.lock_experiment(experiment, datetime.now(timezone.utc).isoformat())
    return store, BudgetLedger(store, experiment_id, limits), limits


def test_empty_complete_final_ledger_explicitly_records_all_zero_categories(tmp_path):
    limits = BudgetLimits()
    _store, selected, _limits = ledger(tmp_path, limits=limits, experiment_id="BUDGET-ALL-ZERO")
    snapshot = selected.snapshot()
    assert snapshot["budget_snapshot_schema_version"] == BUDGET_SNAPSHOT_SCHEMA_VERSION
    assert snapshot["snapshot_kind"] == "FINAL"
    assert snapshot["complete"] is snapshot["settled"] is True
    assert snapshot["active_reservations"] == 0
    assert snapshot["calls_by_category"] == {"provider": 0, "tool": 0, "embedding": 0, "judge": 0}
    assert snapshot["tokens"] == 0 and snapshot["estimated_usd"] == 0.0
    trial = selected.trial_snapshot("never-started")
    assert trial["budget_snapshot_schema_version"] == BUDGET_SNAPSHOT_SCHEMA_VERSION
    assert trial["snapshot_kind"] == "FINAL" and trial["complete"] is trial["settled"] is True
    assert trial["calls_by_category"] == {"provider": 0, "tool": 0, "embedding": 0, "judge": 0}


def test_zero_provider_with_one_settled_tool_call_is_explicit_and_final(tmp_path):
    _store, selected, _limits = ledger(tmp_path, experiment_id="BUDGET-TOOL-ONLY")
    reservation = selected.reserve(trial_id="synthetic-trial", category="tool", calls=1)
    selected.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=0, estimated_usd=0.0)
    snapshot = selected.snapshot()
    assert snapshot["complete"] is True and snapshot["settled"] is True
    assert snapshot["calls_by_category"] == {"provider": 0, "tool": 1, "embedding": 0, "judge": 0}
    trial = selected.trial_snapshot("synthetic-trial")
    assert trial["calls_by_category"] == snapshot["calls_by_category"]
    assert trial["snapshot_kind"] == "FINAL"


def test_one_settled_provider_with_zero_tool_calls_is_explicit_and_final(tmp_path):
    _store, selected, _limits = ledger(tmp_path, experiment_id="BUDGET-PROVIDER-ONLY")
    reservation = selected.reserve(trial_id="synthetic-trial", category="provider", calls=1,
        tokens_reserved=120, estimated_usd_reserved=0.01)
    selected.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=35,
        estimated_usd=0.000042)
    snapshot = selected.snapshot()
    assert snapshot["calls_by_category"] == {"provider": 1, "tool": 0, "embedding": 0, "judge": 0}
    assert snapshot["provider_requests"] == 1
    assert selected.trial_snapshot("synthetic-trial")["calls_by_category"] == snapshot["calls_by_category"]


def test_running_reservation_is_separate_from_final_committed_usage(tmp_path):
    _store, selected, _limits = ledger(tmp_path, experiment_id="BUDGET-HELD")
    selected.reserve(trial_id="synthetic-trial", category="provider", calls=1,
        tokens_reserved=300, estimated_usd_reserved=0.01)
    snapshot = selected.snapshot()
    assert snapshot["snapshot_kind"] == "RUNNING_CONSERVATIVE"
    assert snapshot["complete"] is True and snapshot["settled"] is False
    assert snapshot["active_reservations"] == 1
    assert snapshot["calls_by_category"]["provider"] == 0
    assert snapshot["reserved_calls_by_category"]["provider"] == 1
    assert snapshot["conservative_calls_by_category"]["provider"] == 1
    assert snapshot["tokens_conservative_budget_consumption"] == 300


def test_missing_limits_incomplete_history_and_negative_count_fail_closed(tmp_path):
    _store, selected, limits = ledger(tmp_path, experiment_id="BUDGET-INTEGRITY")
    reserve = {"event_type": "RESERVE", "reservation_id": "reservation-1", "category": "tool",
        "calls": 1, "tokens": 0, "estimated_usd": 0.0,
        "payload_json": json.dumps({"trial_id": "synthetic-trial"})}
    missing_settlement = BudgetLedger.snapshot_from_events([reserve], limits, limits.to_dict())
    assert missing_settlement["complete"] is True and missing_settlement["settled"] is False
    missing_limits = BudgetLedger.snapshot_from_events([], limits, None)
    assert missing_limits["complete"] is False
    negative = {**reserve, "calls": -1}
    corrupted = BudgetLedger.snapshot_from_events([negative], limits, limits.to_dict())
    assert corrupted["complete"] is False
    assert corrupted["integrity_errors"]
