"""Atomic SQLite budget reservations and append-only usage reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import uuid
from typing import Any, Mapping

from .contracts import canonical_json
from .store import ResultStore


class BudgetStop(RuntimeError):
    pass


class BudgetLedgerIntegrityError(ValueError):
    pass


BUDGET_SNAPSHOT_SCHEMA_VERSION = "m6.1-budget-snapshot-v2"
BUDGET_CATEGORIES = ("provider", "tool", "embedding", "judge")


@dataclass(frozen=True)
class BudgetLimits:
    provider_calls: int = 0
    embedding_calls: int = 0
    judge_calls: int = 0
    total_tokens: int = 0
    estimated_usd: float = 0.0
    tool_calls: int = 0
    max_calls_per_trial: int = 0
    max_concurrent_trials: int = 1
    max_duration_seconds: int = 300
    max_retries: int = 0

    def __post_init__(self) -> None:
        for name in ("provider_calls", "embedding_calls", "judge_calls", "total_tokens", "tool_calls",
                     "max_calls_per_trial", "max_concurrent_trials", "max_duration_seconds", "max_retries"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_concurrent_trials < 1 or self.max_duration_seconds < 1:
            raise ValueError("concurrency and duration caps must be positive")
        if isinstance(self.estimated_usd, bool) or not isinstance(self.estimated_usd, (int, float)) or not math.isfinite(self.estimated_usd) or self.estimated_usd < 0:
            raise ValueError("estimated_usd must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BudgetLedger:
    def __init__(self, store: ResultStore, experiment_id: str, limits: BudgetLimits) -> None:
        self.store, self.experiment_id, self.limits = store, experiment_id, limits
        with store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO budget_limits VALUES(?,?)", (experiment_id, canonical_json(limits.to_dict())))
            row = db.execute("SELECT payload_json FROM budget_limits WHERE experiment_id=?", (experiment_id,)).fetchone()
            if json.loads(row[0]) != limits.to_dict():
                raise ValueError("budget limits are locked with the experiment")

    def _usage(self, db) -> tuple[dict[str, int], int, float, int, bool, bool]:
        rows = db.execute("SELECT event_type,reservation_id,category,calls,tokens,estimated_usd,billed_usd,payload_json FROM budget_events WHERE experiment_id=? ORDER BY rowid", (self.experiment_id,)).fetchall()
        states = self._derive_states([dict(row) for row in rows])
        calls_by_category: dict[str, int] = {}
        tokens, cost, active = 0, 0.0, 0
        unknown_tokens = unknown_cost = False
        for state in states.values():
            category = state["category"]
            if state["state"] in {"HELD", "COMMIT"}:
                calls = state["reserved_calls"] if state["state"] == "HELD" else state["actual_calls"]
                calls_by_category[category] = calls_by_category.get(category, 0) + calls
                if state["state"] == "HELD":
                    active += 1
                token_value = state["reserved_tokens"] if state["state"] == "HELD" else state["actual_tokens"]
                cost_value = state["reserved_cost"] if state["state"] == "HELD" else state["actual_cost"]
                if token_value is None:
                    unknown_tokens = True
                    tokens += state["reserved_tokens"] or 0
                else:
                    tokens += token_value
                if cost_value is None:
                    unknown_cost = True
                    cost += state["reserved_cost"] or 0.0
                else:
                    cost += cost_value
        active = len({state["trial_id"] for state in states.values() if state["state"] == "HELD"})
        return calls_by_category, tokens, cost, active, unknown_tokens, unknown_cost

    @staticmethod
    def _derive_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for event in events:
            if not isinstance(event, dict):
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_RECORD_INVALID")
            reservation, kind = event.get("reservation_id"), event.get("event_type")
            category = event.get("category")
            calls, tokens, cost = event.get("calls"), event.get("tokens"), event.get("estimated_usd")
            if kind not in {"RESERVE", "COMMIT", "RELEASE", "RECONCILE"}:
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_TYPE_INVALID")
            if category not in BUDGET_CATEGORIES or not isinstance(reservation, str) or not reservation:
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_SCOPE_INVALID")
            if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_CALL_COUNT_INVALID")
            if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_TOKEN_COUNT_INVALID")
            if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or
                                     not math.isfinite(float(cost)) or cost < 0):
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_COST_INVALID")
            if kind == "RESERVE":
                if reservation in states or calls < 1:
                    raise BudgetLedgerIntegrityError("BUDGET_RESERVATION_DUPLICATE_OR_EMPTY")
                try:
                    details = json.loads(event.get("payload_json"))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise BudgetLedgerIntegrityError("BUDGET_EVENT_PAYLOAD_INVALID") from exc
                if not isinstance(details, dict) or not isinstance(details.get("trial_id"), str) or not details["trial_id"]:
                    raise BudgetLedgerIntegrityError("BUDGET_RESERVATION_TRIAL_INVALID")
                states[reservation] = {"state":"HELD", "category":event["category"], "reserved_calls":event["calls"],
                    "reserved_tokens":event["tokens"], "reserved_cost":event["estimated_usd"], "trial_id":details.get("trial_id"),
                    "actual_calls":0, "actual_tokens":None, "actual_cost":None, "billed_usd":None,
                    "call_count_status":"UNKNOWN"}
                continue
            if reservation not in states:
                raise BudgetLedgerIntegrityError("BUDGET_SETTLEMENT_WITHOUT_RESERVATION")
            state = states[reservation]
            if category != state["category"]:
                raise BudgetLedgerIntegrityError("BUDGET_SETTLEMENT_CATEGORY_MISMATCH")
            try:
                payload = json.loads(event.get("payload_json"))
            except (TypeError, json.JSONDecodeError) as exc:
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_PAYLOAD_INVALID") from exc
            if not isinstance(payload, dict):
                raise BudgetLedgerIntegrityError("BUDGET_EVENT_PAYLOAD_INVALID")
            call_status = payload.get("call_count_status", "RECORDED")
            if call_status not in {"RECORDED", "CONSERVATIVE_UPPER_BOUND", "UNKNOWN"}:
                raise BudgetLedgerIntegrityError("BUDGET_CALL_COUNT_STATUS_INVALID")
            if calls > state["reserved_calls"] or (tokens is not None and state["reserved_tokens"] is not None and
                    tokens > state["reserved_tokens"]) or (cost is not None and state["reserved_cost"] is not None and
                    cost > state["reserved_cost"]):
                raise BudgetLedgerIntegrityError("BUDGET_SETTLEMENT_EXCEEDS_RESERVATION")
            if kind in {"COMMIT", "RELEASE"}:
                if state["state"] != "HELD" or (kind == "RELEASE" and calls != 0):
                    raise BudgetLedgerIntegrityError("BUDGET_RESERVATION_SETTLED_MORE_THAN_ONCE")
                state.update({"state":kind, "actual_calls":calls, "actual_tokens":tokens,
                    "actual_cost":cost, "billed_usd":event.get("billed_usd"),
                    "call_count_status":call_status})
            else:
                if state["state"] != "COMMIT" or (state["actual_tokens"] is not None and
                        state["actual_cost"] is not None):
                    raise BudgetLedgerIntegrityError("BUDGET_RECONCILIATION_STATE_INVALID")
                state.update({"actual_calls":calls, "actual_tokens":tokens,
                    "actual_cost":cost, "billed_usd":event.get("billed_usd"),
                    "call_count_status":call_status})
        return states

    def reserve(self, *, trial_id: str, category: str, calls: int = 1, tokens_reserved: int = 0,
                estimated_usd_reserved: float = 0.0, retry_index: int | None = None) -> str:
        caps = {"provider": self.limits.provider_calls, "embedding": self.limits.embedding_calls, "judge": self.limits.judge_calls, "tool": self.limits.tool_calls}
        if category not in caps or calls < 1 or tokens_reserved < 0 or estimated_usd_reserved < 0:
            raise ValueError("invalid budget reservation")
        if retry_index is not None and (retry_index < 1 or retry_index > self.limits.max_retries):
            raise BudgetStop("retry limit reached")
        reservation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.store.transaction() as db:
            calls_by_category, used_tokens, used_cost, active, unknown_tokens, unknown_cost = self._usage(db)
            active_rows = db.execute("SELECT event_type,reservation_id,payload_json FROM budget_events WHERE experiment_id=? ORDER BY rowid",
                                     (self.experiment_id,)).fetchall()
            active_trials = set()
            trial_states: dict[str, tuple[str, str | None]] = {}
            for event in active_rows:
                details = json.loads(event["payload_json"])
                reservation = event["reservation_id"]
                if event["event_type"] == "RESERVE":
                    trial_states[reservation] = ("HELD", details.get("trial_id"))
                elif reservation in trial_states and event["event_type"] in {"COMMIT", "RELEASE", "RECONCILE"}:
                    trial_states[reservation] = (event["event_type"], trial_states[reservation][1])
            active_trials = {trial_id_value for state, trial_id_value in trial_states.values()
                             if state == "HELD" and trial_id_value}
            # Multiple category reservations (provider + embedding) belong to one
            # active Trial and must not consume multiple concurrency slots.
            if trial_id not in active_trials and active >= self.limits.max_concurrent_trials:
                raise BudgetStop("maximum concurrent reservations reached")
            cap = caps[category]
            if cap == 0 or calls_by_category.get(category, 0) + calls > cap:
                raise BudgetStop(f"{category} call budget exhausted")
            if category == "tool":
                rows = db.execute("SELECT event_type,reservation_id,calls,payload_json FROM budget_events WHERE experiment_id=? AND category='tool' ORDER BY rowid", (self.experiment_id,)).fetchall()
                by_reservation: dict[str, dict[str, Any]] = {}
                for event in rows:
                    details = json.loads(event["payload_json"])
                    if event["event_type"] == "RESERVE":
                        by_reservation[event["reservation_id"]] = {"trial_id": details.get("trial_id"), "state": "HELD", "calls": event["calls"]}
                    elif event["reservation_id"] in by_reservation:
                        by_reservation[event["reservation_id"]].update({"state": event["event_type"], "calls": event["calls"]})
                used_for_trial = sum(item["calls"] for item in by_reservation.values() if item["trial_id"] == trial_id and item["state"] in {"HELD", "COMMIT", "RECONCILE"})
                if used_for_trial + calls > self.limits.max_calls_per_trial:
                    raise BudgetStop("per-trial tool call limit exceeded")
            if retry_index is not None:
                retry_rows = db.execute("SELECT payload_json FROM budget_events WHERE experiment_id=? AND event_type='RESERVE'", (self.experiment_id,)).fetchall()
                for event in retry_rows:
                    prior_payload = json.loads(event["payload_json"])
                    if prior_payload.get("trial_id") == trial_id and prior_payload.get("retry_index") == retry_index:
                        raise BudgetStop("retry index already used")
            if used_tokens + tokens_reserved > self.limits.total_tokens:
                raise BudgetStop("token budget exhausted")
            if used_cost + estimated_usd_reserved > self.limits.estimated_usd:
                raise BudgetStop("estimated cost budget exhausted")
            db.execute("INSERT INTO budget_events VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                str(uuid.uuid4()), self.experiment_id, reservation_id, "RESERVE", category,
                calls, tokens_reserved, estimated_usd_reserved, None,
                canonical_json({"trial_id": trial_id, "retry_index": retry_index, "usage_status": "RESERVED"}), now,
            ))
        return reservation_id

    def settle(self, reservation_id: str, *, status: str, actual_calls: int, actual_tokens: int | None,
               estimated_usd: float | None, billed_usd: float | None = None,
               call_count_status: str = "RECORDED") -> None:
        if status not in {"COMMIT", "RELEASE", "RECONCILE"}:
            raise ValueError("invalid settlement status")
        if call_count_status not in {"RECORDED", "CONSERVATIVE_UPPER_BOUND", "UNKNOWN"}:
            raise ValueError("invalid call count status")
        if actual_calls < 0 or (actual_tokens is not None and actual_tokens < 0):
            raise ValueError("usage must be non-negative or unknown")
        if status == "RELEASE" and actual_calls != 0:
            raise ValueError("a reservation with any attempted call must be committed, not released")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.store.transaction() as db:
            row = db.execute("SELECT category,calls,tokens,estimated_usd FROM budget_events WHERE reservation_id=? AND event_type='RESERVE'", (reservation_id,)).fetchone()
            if not row:
                raise ValueError("reservation not found")
            prior_rows = db.execute("SELECT event_type,tokens,estimated_usd FROM budget_events WHERE reservation_id=? AND event_type IN ('COMMIT','RELEASE','RECONCILE') ORDER BY rowid", (reservation_id,)).fetchall()
            if status == "RECONCILE":
                if not prior_rows or prior_rows[-1]["event_type"] not in {"COMMIT", "RECONCILE"}:
                    raise ValueError("only a committed reservation can be reconciled")
                if prior_rows[-1]["tokens"] is not None and prior_rows[-1]["estimated_usd"] is not None:
                    raise ValueError("reservation usage is already fully reconciled")
            elif prior_rows:
                raise ValueError("reservation already settled")
            if actual_calls > row["calls"] or (actual_tokens is not None and row["tokens"] is not None and actual_tokens > row["tokens"]):
                raise BudgetStop("actual usage exceeds reservation; reconcile before recording")
            if estimated_usd is not None and row["estimated_usd"] is not None and estimated_usd > row["estimated_usd"]:
                raise BudgetStop("estimated cost exceeds reservation; reconcile before recording")
            caps = {"provider": self.limits.provider_calls, "embedding": self.limits.embedding_calls,
                    "judge": self.limits.judge_calls, "tool": self.limits.tool_calls}
            calls_by_category, _, _, _, _, _ = self._usage(db)
            if status in {"COMMIT", "RECONCILE"} and calls_by_category.get(row["category"], 0) - row["calls"] + actual_calls > caps[row["category"]]:
                raise BudgetStop("settled calls exceed locked hard cap")
            db.execute("INSERT INTO budget_events VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                str(uuid.uuid4()), self.experiment_id, reservation_id, status, row["category"], actual_calls,
                actual_tokens, estimated_usd, billed_usd,
                canonical_json({"usage_status": "UNKNOWN" if actual_tokens is None else "RECORDED",
                                "call_count_status": call_count_status}), now,
            ))

    def reserve_retry(self, *, trial_id: str, retry_index: int, category: str) -> str:
        if retry_index < 1 or retry_index > self.limits.max_retries:
            raise BudgetStop("retry limit reached")
        return self.reserve(trial_id=trial_id, category=category, retry_index=retry_index)

    def snapshot(self) -> dict[str, Any]:
        events = self.store.budget_snapshot(self.experiment_id)
        return self.snapshot_from_events(events, self.limits, self.store.budget_limits(self.experiment_id))

    @classmethod
    def snapshot_from_events(cls, events: list[dict[str, Any]], limits: BudgetLimits,
                             locked_limits: dict[str, Any] | None) -> dict[str, Any]:
        """Build the same versioned snapshot from a read-only persisted event export."""
        integrity_errors: list[str] = []
        try:
            states = cls._derive_states(events)
        except (BudgetLedgerIntegrityError, KeyError, TypeError, ValueError) as exc:
            states = {}
            integrity_errors.append(str(exc) if isinstance(exc, BudgetLedgerIntegrityError) else "BUDGET_EVENT_RECORD_INVALID")
        if locked_limits != limits.to_dict():
            integrity_errors.append("BUDGET_LIMITS_MISSING_OR_MISMATCHED")
        committed = [state for state in states.values() if state["state"] == "COMMIT"]
        calls_by_category: dict[str, int] = {category: 0 for category in BUDGET_CATEGORIES}
        for event in committed:
            calls_by_category[event["category"]] = calls_by_category.get(event["category"], 0) + event["actual_calls"]
        reserved_calls_by_category: dict[str, int] = {category: 0 for category in BUDGET_CATEGORIES}
        for state in states.values():
            if state["state"] == "HELD":
                reserved_calls_by_category[state["category"]] += state["reserved_calls"]
        conservative_calls_by_category = {category: calls_by_category[category] + reserved_calls_by_category[category]
                                          for category in BUDGET_CATEGORIES}
        active_reservations = sum(state["state"] == "HELD" for state in states.values())
        external = [e for e in committed if e["category"] in {"provider", "embedding", "judge"}]
        token_rows = [e["actual_tokens"] for e in external]
        estimate_rows = [e["actual_cost"] for e in external]
        billed_rows = [e["billed_usd"] for e in external if e["billed_usd"] is not None]
        external_states = [state for state in committed if state["category"] in {"provider", "embedding", "judge"}]
        token_upper_bound = sum((state["actual_tokens"] if state["actual_tokens"] is not None else
            (state["reserved_tokens"] or 0)) for state in external_states)
        held_external = [state for state in states.values() if state["state"] == "HELD" and
                         state["category"] in {"provider", "embedding", "judge"}]
        token_upper_bound += sum(state["reserved_tokens"] or 0 for state in held_external)
        cost_upper_bound = sum((state["actual_cost"] if state["actual_cost"] is not None else
            (state["reserved_cost"] or 0.0)) for state in external_states)
        cost_upper_bound += sum(state["reserved_cost"] or 0.0 for state in held_external)
        external_activity = external_states + held_external
        call_statuses = {state.get("call_count_status", "RECORDED") for state in external_states}
        calls_status = ("CONSERVATIVE_UPPER_BOUND" if "CONSERVATIVE_UPPER_BOUND" in call_statuses else
                        "UNKNOWN" if "UNKNOWN" in call_statuses else
                        "CONSERVATIVE_UPPER_BOUND" if held_external else
                        "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS" if not external_activity else "RECORDED")
        complete = not integrity_errors
        settled = complete and active_reservations == 0
        return {"budget_snapshot_schema_version": BUDGET_SNAPSHOT_SCHEMA_VERSION,
                "snapshot_kind": "FINAL" if settled else "RUNNING_CONSERVATIVE" if complete else "UNKNOWN",
                "complete": complete, "settled": settled, "active_reservations": active_reservations,
                "integrity_errors": integrity_errors,
                "events": len(events), "committed_calls": sum(calls_by_category.values()), "calls_by_category": calls_by_category,
                "reserved_calls_by_category": reserved_calls_by_category,
                "conservative_calls_by_category": conservative_calls_by_category,
                "calls_status": calls_status,
                "tokens": None if held_external else 0 if not external else sum(token_rows) if token_rows and all(v is not None for v in token_rows) else None,
                "tokens_conservative_budget_consumption": token_upper_bound,
                "token_status": "UNKNOWN_USAGE" if any(v is None for v in token_rows) else "RESERVED_CONSERVATIVE" if held_external else "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS" if not external else "RECORDED",
                "estimated_usd": None if held_external else 0.0 if not external else sum(estimate_rows) if estimate_rows and all(v is not None for v in estimate_rows) else None,
                "estimated_usd_conservative_budget_consumption": cost_upper_bound,
                "estimate_status": "UNKNOWN_USAGE" if any(v is None for v in estimate_rows) else "RESERVED_CONSERVATIVE" if held_external else "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS" if not external else "RECORDED_ESTIMATE",
                "billed_usd": sum(billed_rows) if billed_rows else None,
                "billed_status": "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS" if not external else "UNKNOWN_BILLING" if not billed_rows else "RECORDED_PROVIDER_BILLING",
                "provider_requests": calls_by_category["provider"],
                "judge_requests": calls_by_category["judge"],
                "embedding_requests": calls_by_category["embedding"],
                "limitations": ["Call counts may be conservative upper bounds; missing usage remains UNKNOWN; billed_usd is never inferred from estimates"]}

    def trial_snapshot(self, trial_id: str) -> dict[str, Any]:
        """Durable aggregate usage for one trial, including held reservations."""
        events = self.store.budget_snapshot(self.experiment_id)
        integrity_errors: list[str] = []
        try:
            states = self._derive_states(events)
        except (BudgetLedgerIntegrityError, KeyError, TypeError, ValueError) as exc:
            states = {}
            integrity_errors.append(str(exc) if isinstance(exc, BudgetLedgerIntegrityError) else "BUDGET_EVENT_RECORD_INVALID")
        if self.store.budget_limits(self.experiment_id) != self.limits.to_dict():
            integrity_errors.append("BUDGET_LIMITS_MISSING_OR_MISMATCHED")
        calls: dict[str, int] = {category: 0 for category in BUDGET_CATEGORIES}
        reserved: dict[str, int] = {category: 0 for category in BUDGET_CATEGORIES}
        tokens = 0
        estimated_usd = 0.0
        unknown_tokens = unknown_cost = False
        for state in states.values():
            if state.get("trial_id") != trial_id:
                continue
            is_held = state["state"] == "HELD"
            category = state["category"]
            if is_held:
                reserved[category] += state["reserved_calls"]
            elif state["state"] == "COMMIT":
                calls[category] += state["actual_calls"]
            else:
                continue
            token_value = state["reserved_tokens"] if is_held else state["actual_tokens"]
            cost_value = state["reserved_cost"] if is_held else state["actual_cost"]
            if token_value is None:
                unknown_tokens = True
                tokens += state["reserved_tokens"] or 0
            else:
                tokens += token_value
            if cost_value is None:
                unknown_cost = True
                estimated_usd += state["reserved_cost"] or 0.0
            else:
                estimated_usd += cost_value
        conservative = {category: calls[category] + reserved[category] for category in BUDGET_CATEGORIES}
        active = sum(state.get("trial_id") == trial_id and state["state"] == "HELD" for state in states.values())
        complete = not integrity_errors
        settled = complete and active == 0
        return {"budget_snapshot_schema_version": BUDGET_SNAPSHOT_SCHEMA_VERSION,
                "snapshot_kind": "FINAL" if settled else "RUNNING_CONSERVATIVE" if complete else "UNKNOWN",
                "complete": complete, "settled": settled, "active_reservations": active,
                "integrity_errors": integrity_errors, "calls_by_category": calls,
                "reserved_calls_by_category": reserved, "conservative_calls_by_category": conservative,
                "tokens": tokens, "estimated_usd": estimated_usd,
                "unknown_tokens": unknown_tokens, "unknown_cost": unknown_cost}
