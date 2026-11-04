"""Verified budget carry-forward from the latest closed personal D1 batch."""
from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3

from .budget import BudgetLedger


SOURCE_EXPERIMENT_ID = "M61D1-BATCH-R324-R2-05189a8505d640cf8298a60df0eac6c1"
EXPECTED_CALLS = 41
EXPECTED_RESERVED_TOKENS = 285_926
EXPECTED_RESERVED_USD = 0.3431112


class CarryForwardError(ValueError):
    pass


class ReadOnlyBudgetStore:
    def __init__(self, path):
        self.path = path

    def budget_snapshot(self, experiment_id: str) -> list[dict]:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT event_type,reservation_id,category,calls,tokens,estimated_usd,billed_usd,payload_json "
                "FROM budget_events WHERE experiment_id=? ORDER BY rowid", (experiment_id,)).fetchall()
        return [dict(row) for row in rows]


@dataclass(frozen=True)
class CarryForwardCase:
    provider_calls: int
    conservative_tokens: int
    conservative_usd: float


def inspect_closed_batch_carryforward(store, case_ids: list[str]) -> dict:
    """Read and validate the latest closed batch's ledger; never mutates it."""
    events = store.budget_snapshot(SOURCE_EXPERIMENT_ID)
    if not events:
        raise CarryForwardError("CLOSED_SOURCE_BATCH_LEDGER_MISSING")
    states = BudgetLedger._derive_states(events)
    provider = [state for state in states.values() if state["category"] == "provider"]
    if not provider or any(state["state"] != "COMMIT" for state in provider):
        raise CarryForwardError("CLOSED_SOURCE_BATCH_HAS_UNSETTLED_PROVIDER_RESERVATION")
    by_case: dict[str, dict[str, float | int]] = {}
    for state in provider:
        trial_id = state.get("trial_id")
        if trial_id not in case_ids:
            raise CarryForwardError("CLOSED_SOURCE_BATCH_CASE_OUTSIDE_DATASET")
        row = by_case.setdefault(trial_id, {"provider_calls": 0, "tokens": 0, "usd": 0.0})
        row["provider_calls"] += state["actual_calls"]
        token_value = state["actual_tokens"]
        row["tokens"] += token_value if token_value is not None else (state["reserved_tokens"] or 0)
        usd_value = state["actual_cost"]
        row["usd"] += usd_value if usd_value is not None else (state["reserved_cost"] or 0.0)
    if set(by_case) != set(case_ids):
        raise CarryForwardError("CLOSED_SOURCE_BATCH_CASE_COVERAGE_MISMATCH")
    calls = sum(int(row["provider_calls"]) for row in by_case.values())
    tokens = sum(int(row["tokens"]) for row in by_case.values())
    usd = sum(float(row["usd"]) for row in by_case.values())
    if (calls != EXPECTED_CALLS or tokens != EXPECTED_RESERVED_TOKENS or
            not math.isclose(usd, EXPECTED_RESERVED_USD, rel_tol=0, abs_tol=1e-12)):
        raise CarryForwardError("CLOSED_SOURCE_BATCH_TOTALS_CHANGED")
    return {"source_experiment_id": SOURCE_EXPERIMENT_ID, "provider_calls": calls,
        "conservative_tokens": tokens, "conservative_usd": usd,
        "unknown_actual_tokens": any(state["actual_tokens"] is None for state in provider),
        "unknown_actual_cost": any(state["actual_cost"] is None for state in provider),
        "cases": {case_id: CarryForwardCase(int(row["provider_calls"]), int(row["tokens"]), float(row["usd"]))
            for case_id, row in by_case.items()}}


def apply_carryforward(ledger, carryforward: dict) -> None:
    """Append the source reservation upper bounds once into a new batch ledger."""
    for case_id, case in carryforward["cases"].items():
        if case.provider_calls < 1:
            continue
        reservation = ledger.reserve(trial_id=case_id, category="provider", calls=case.provider_calls,
            tokens_reserved=case.conservative_tokens, estimated_usd_reserved=case.conservative_usd)
        ledger.settle(reservation, status="COMMIT", actual_calls=case.provider_calls,
            actual_tokens=None if carryforward["unknown_actual_tokens"] else case.conservative_tokens,
            estimated_usd=None if carryforward["unknown_actual_cost"] else case.conservative_usd,
            call_count_status="CONSERVATIVE_UPPER_BOUND")
