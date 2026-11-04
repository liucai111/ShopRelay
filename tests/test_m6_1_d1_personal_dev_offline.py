"""Independent synthetic file-SQLite integration; never invokes an Agent/SDK."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.m1.models import User, Order, Shipment, ConversationSession, Run, RunEvent, Trace, utc_now
from app.m3.order_tools import ToolExecutionError
from eval.m6_1.budget import BudgetStop
from eval.m6_1.contracts import Verdict
from eval.m6_1.personal_dev import (D1Coordinator, D1PreflightError, plan_case, OS_ISOLATION_STATUS,
    create_budget_ledger)
from eval.m6_1.personal_dev_database import PersonalDevDatabase, PersonalDatabaseError
from eval.m6_1.personal_dev_runtime import BudgetClient
from eval.m6_1.shipment_fixture_loader import ShipmentFixtureError
from eval.m6_1.shipment_fixture_reset import load_bundle_shipments


@pytest.fixture
def tmp_path():
    # pytest's Windows 0700 basetemp creation is inaccessible on this host.
    # Use a fresh ordinary directory; do not revisit or change ACLs on basetemp.
    root = Path(__file__).resolve().parents[1] / "data/eval/offline-tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    return root


def bundle(shipped=False, second=False):
    orders = [{"id": "probe-a", "order_id": "ORD-PROBE-A", "owner_id": "SYN-USER-A",
        "status": "PAID", "paid_cents": 100}]
    if second:
        orders.append({"id": "probe-b", "order_id": "ORD-PROBE-B", "owner_id": "SYN-USER-A",
            "status": "PAID", "paid_cents": 200})
    return {"split": "dev", "version": "dev-independent-probe", "fixtures": {
        "users": [{"user_id": "SYN-USER-A"}, {"user_id": "SYN-USER-B"}],
        "orders_and_snapshots": orders,
        "shipments": [{"shipment_id": "probe-shipment", "order_id": "ORD-PROBE-A",
            "status": "IN_TRANSIT", "observed_at": "2026-10-25T08:00:00+08:00"}] if shipped else []},
        "cases": [{"schema_version": "1.0", "case_id": "PROBE-CASE", "group_id": "PROBE-GROUP",
            "split": "dev", "input": {"user_query": "Track ORD-PROBE-A", "user_id": "SYN-USER-A",
                "order_snapshot_id": "probe-a", "fixture_refs": [r["id"] for r in orders],
                "expected_tool_calls": [{"tool_id": "get_order", "mode": "read_only"},
                    {"tool_id": "track_shipment", "mode": "read_only"}]},
            "expected_outcome": {}, "rubric_refs": [], "fixture_ref": "probe-fixtures",
            "risk_level": "LOW", "data_version": "dev-independent-probe"}]}


@pytest.fixture
def coordinator(tmp_path):
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    try:
        yield D1Coordinator(target, factory)
    finally:
        engine.dispose()


def assert_empty(coordinator):
    with coordinator.factory() as db:
        assert db.execute(select(User.user_id)).all() == []
        assert db.execute(select(Order.order_id)).all() == []
        assert db.execute(select(Shipment.shipment_id)).all() == []
        assert db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one() is None


def test_explicit_personal_os_policy_and_input_mapping():
    assert OS_ISOLATION_STATUS == Verdict.NOT_APPLICABLE.value
    data = bundle()
    original = deepcopy(data)
    plan = plan_case(data, "PROBE-CASE")
    assert plan["message"] == data["cases"][0]["input"]["user_query"]
    assert plan["owner_id"] == "d1-personal-a"
    assert plan["orders"][0]["order_id"] == "ORD-PROBE-A"
    assert data == original


@pytest.mark.parametrize("value", [True, "100"])
def test_missing_payment_is_not_filled_with_a_sentinel(value):
    data = bundle()
    data["fixtures"]["orders_and_snapshots"][0]["paid_cents"] = value
    with pytest.raises(D1PreflightError, match="ORDER_FACT_TYPE_INVALID"):
        plan_case(data, "PROBE-CASE")


def test_missing_status_is_explicit_unknown_and_write_capability_is_blocked():
    data = bundle()
    del data["fixtures"]["orders_and_snapshots"][0]["status"]
    plan = plan_case(data, "PROBE-CASE")
    assert plan["orders"] == []
    assert json.loads(plan["facts"][0]["payload"]).get("status") is None
    data = bundle()
    data["cases"][0]["input"]["expected_tool_calls"].append({"tool_id": "create_return_request", "mode": "read_only"})
    with pytest.raises(D1PreflightError, match="FIXTURE_CAPABILITY_NOT_IMPLEMENTED"):
        plan_case(data, "PROBE-CASE")


def test_file_database_is_shared_by_reset_and_query_empty_unknown(coordinator):
    for _ in range(2):
        result = coordinator.offline_query(bundle(), "PROBE-CASE")["ORD-PROBE-A"]
        assert result["availability"] == "UNAVAILABLE"
        assert result["error_code"] == "NO_SHIPMENT_RECORD"
        assert result["records"] == []
        assert_empty(coordinator)


def test_positive_shipment_and_same_owner_other_order_are_separate(coordinator):
    results = coordinator.offline_query(bundle(True, True), "PROBE-CASE")
    assert results["ORD-PROBE-A"]["records"] == [{"status": "IN_TRANSIT", "observed_at": "2026-10-25T00:00:00Z"}]
    assert results["ORD-PROBE-B"]["error_code"] == "NO_SHIPMENT_RECORD"
    assert_empty(coordinator)


def test_wrong_requested_owner_cannot_read_another_owners_order(coordinator):
    data = bundle(True)
    data["cases"][0]["input"]["user_id"] = "SYN-USER-B"
    with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
        coordinator.offline_query(data, "PROBE-CASE")
    assert_empty(coordinator)


def test_wrong_loader_owner_rolls_back_users_orders_and_shipments(coordinator):
    plan = plan_case(bundle(True), "PROBE-CASE")
    plan["orders"][0]["user_id"] = "d1-personal-b"
    with pytest.raises(ShipmentFixtureError, match="ORDER_OWNER_OR_ASSOCIATION_MISMATCH"):
        coordinator.prepare(plan)
    assert_empty(coordinator)


def test_exact_order_id_not_remapped_even_with_same_owner(coordinator):
    plan = plan_case(bundle(True), "PROBE-CASE")
    plan["orders"][0]["order_id"] = "ORD-DIFFERENT-PRELOADED"
    with pytest.raises(ShipmentFixtureError, match="ORDER_OWNER_OR_ASSOCIATION_MISMATCH"):
        coordinator.prepare(plan)
    assert_empty(coordinator)


def test_late_shipment_validation_failure_rolls_back_entire_preload(coordinator):
    data = bundle(True)
    data["fixtures"]["shipments"][0]["status"] = "INVALID"
    with pytest.raises(ShipmentFixtureError, match="SHIPMENT_STATUS_UNSUPPORTED"):
        coordinator.prepare(plan_case(data, "PROBE-CASE"))
    assert_empty(coordinator)


def test_duplicate_reset_keeps_receipt_and_clean_reload(coordinator):
    plan = plan_case(bundle(True), "PROBE-CASE")
    receipt = coordinator.prepare(plan)
    with pytest.raises(PersonalDatabaseError, match="LEASE_BUSY"):
        coordinator.prepare(plan)
    with coordinator.factory() as db:
        assert len(db.execute(select(Shipment)).scalars().all()) == 1
    with pytest.raises(D1PreflightError, match="RECEIPT_CONTENT_MISMATCH"):
        coordinator.cleanup(replace(receipt, order_ids=("ORD-UNRELATED",)))
    coordinator.cleanup(receipt)
    with pytest.raises(D1PreflightError, match="LEASE_MISMATCH"):
        coordinator.cleanup(receipt)
    coordinator.cleanup(coordinator.prepare(plan))
    assert_empty(coordinator)


def test_unrelated_users_orders_and_shipments_are_not_deleted(coordinator):
    with coordinator.factory() as db, db.begin():
        db.add(User(user_id="unrelated-owner", display_name="Independent unrelated probe"))
        db.flush()
        db.add(Order(order_id="ORD-UNRELATED", user_id="unrelated-owner", status="PAID", paid_cents=3,
            paid_at=None, delivered_at=None, version=1))
        db.flush()
        db.add(Shipment(shipment_id="unrelated-shipment", order_id="ORD-UNRELATED",
            status="IN_TRANSIT", observed_at="2026-10-24T00:00:00Z"))
    coordinator.offline_query(bundle(True), "PROBE-CASE")
    with coordinator.factory() as db:
        assert db.execute(select(User.user_id)).scalars().all() == ["unrelated-owner"]
        assert db.execute(select(Order.order_id)).scalars().all() == ["ORD-UNRELATED"]
        assert db.execute(select(Shipment.shipment_id)).scalars().all() == ["unrelated-shipment"]


def test_id_collision_rejected_without_overwriting_existing_data(coordinator):
    with coordinator.factory() as db, db.begin():
        db.add(User(user_id="d1-personal-a", display_name="Must remain"))
    with pytest.raises(D1PreflightError, match="COLLISION_NO_OVERWRITE"):
        coordinator.prepare(plan_case(bundle(), "PROBE-CASE"))
    with coordinator.factory() as db:
        assert db.get(User, "d1-personal-a").display_name == "Must remain"
        assert db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one() is None


def test_wrong_database_rejected_even_for_empty_bundle(coordinator):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with Session(engine) as db, pytest.raises(PersonalDatabaseError, match="TARGET_MISMATCH"):
            load_bundle_shipments(db, bundle(), owner_id_map={"SYN-USER-A": "d1-personal-a"},
                probe_id="wrongtarget", personal_database=coordinator.target)
    finally:
        engine.dispose()


def test_existing_unmarked_file_cannot_be_adopted(tmp_path):
    target = PersonalDevDatabase(tmp_path)
    target.path.parent.mkdir(parents=True)
    engine = create_engine(f"sqlite:///{target.path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (value INTEGER)"))
    engine.dispose()
    with pytest.raises(PersonalDatabaseError, match="MARKER_REQUIRED"):
        target.open(initialize=True)


def test_absent_grant_blocks_before_preload_or_real_backend(coordinator):
    with pytest.raises(D1PreflightError, match="REGISTRY_GRANT"):
        coordinator.execute_authorized(bundle=bundle(), case_id="PROBE-CASE",
            asset=SimpleNamespace(split="dev"), grant=None, permit=None, ledger=None, settings=None)
    assert_empty(coordinator)


def test_budget_rejection_happens_before_sdk_request():
    class DeniedLedger:
        def reserve(self, **_kwargs):
            raise BudgetStop("no approved budget")
    class MustNotCall:
        def with_options(self, **_kwargs):
            raise AssertionError("SDK must not be touched")
    import time
    client = BudgetClient(MustNotCall(), DeniedLedger(), "probe", time.monotonic() + 10, .01)
    with pytest.raises(BudgetStop):
        client.create(messages=[{"role": "user", "content": "Synthetic probe"}], max_tokens=10)


def test_personal_budget_config_is_locked_before_ledger_events(coordinator):
    from eval.m6_1.budget import BudgetLimits
    permit = {"authorization_id": "OFFLINE-ONLY-LEDGER-PROBE", "asset_sha256": "1" * 64,
        "case_ids": ["PROBE-CASE"], "database_identity": coordinator.target.identity,
        "code_sha256": "2" * 64, "git_commit": "0123456"}
    limits = BudgetLimits(provider_calls=1, total_tokens=15000, estimated_usd=.05,
        tool_calls=2, max_calls_per_trial=2, max_duration_seconds=120)
    ledger = create_budget_ledger(coordinator.target, permit, limits, SimpleNamespace(model="OFFLINE-NO-MODEL"))
    reservation = ledger.reserve(trial_id="PROBE-CASE", category="tool", calls=2)
    ledger.settle(reservation, status="COMMIT", actual_calls=2, actual_tokens=0, estimated_usd=0.0)
    with pytest.raises(BudgetStop):
        ledger.reserve(trial_id="PROBE-CASE", category="tool", calls=1)


def test_sdk_attempt_has_no_retries_and_unknown_cost_is_preserved():
    import time
    events = []
    class Ledger:
        def reserve(self, **kwargs):
            events.append(("reserve", kwargs))
            return "probe-reservation"
        def settle(self, _held, **kwargs):
            events.append(("settle", kwargs))
    class SDK:
        def __init__(self):
            self.options = []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            self.options.append(kwargs)
            return self
        def create(self, **_kwargs):
            events.append(("sdk", {}))
            return SimpleNamespace(usage=SimpleNamespace(total_tokens=20))
    sdk = SDK()
    client = BudgetClient(sdk, Ledger(), "PROBE", time.monotonic() + 10, 1.20)
    client.with_options(max_retries=4).create(messages=[], max_tokens=10)
    assert [e[0] for e in events] == ["reserve", "sdk", "settle"]
    reserved_tokens = events[0][1]["tokens_reserved"]
    assert events[0][1]["estimated_usd_reserved"] == pytest.approx(reserved_tokens * 1.20 / 1_000_000)
    assert all(o["max_retries"] == 0 for o in sdk.options)
    assert events[-1][1]["estimated_usd"] == pytest.approx(20 * 1.20 / 1_000_000)
    assert events[-1][1]["actual_tokens"] == 20


def test_connection_failure_settles_as_attempt_with_unknown_usage_not_success():
    import time
    events = []

    class Ledger:
        def reserve(self, **kwargs):
            events.append(("reserve", kwargs))
            return "connection-failure-reservation"

        def settle(self, _held, **kwargs):
            events.append(("settle", kwargs))

    class SDK:
        def __init__(self):
            self.options = []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            self.options.append(kwargs)
            return self

        def create(self, **_kwargs):
            events.append(("sdk_attempt", {}))
            raise RuntimeError("sensitive URL and payload must not enter ledger")

    client = BudgetClient(SDK(), Ledger(), "POLICY-013", time.monotonic() + 10, 1.20)
    with pytest.raises(RuntimeError):
        client.create(messages=[{"role": "user", "content": "synthetic bounded prompt"}], max_tokens=32)
    assert [row[0] for row in events] == ["reserve", "sdk_attempt", "settle"]
    settled = events[-1][1]
    assert settled["status"] == "COMMIT"
    assert settled["actual_calls"] == 1
    assert settled["actual_tokens"] is None
    assert settled["estimated_usd"] is None
    assert settled["call_count_status"] == "CONSERVATIVE_UPPER_BOUND"


def test_unreceipted_shipment_blocks_and_rolls_back_cleanup(coordinator):
    receipt = coordinator.prepare(plan_case(bundle(True), "PROBE-CASE"))
    with coordinator.factory() as db, db.begin():
        db.add(Shipment(shipment_id="not-in-receipt", order_id="ORD-PROBE-A",
            status="IN_TRANSIT", observed_at="2026-10-24T00:00:00Z"))
    with pytest.raises(D1PreflightError, match="UNRECEIPTED_SHIPMENT_RETAINED"):
        coordinator.cleanup(receipt)
    with coordinator.factory() as db:
        assert len(db.execute(select(Shipment)).scalars().all()) == 2
        assert db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one() == receipt.lease_id


def test_unknown_run_retains_receipt_and_fixture_data(coordinator):
    from dataclasses import asdict
    receipt = coordinator.prepare(plan_case(bundle(), "PROBE-CASE"))
    receipt.session_id, receipt.run_id = "OFFLINE-SESSION", "OFFLINE-RUN"
    with coordinator.factory() as db, db.begin():
        db.add(ConversationSession(session_id=receipt.session_id, user_id="d1-personal-a", created_at=utc_now()))
        db.flush()
        db.add(Run(run_id=receipt.run_id, session_id=receipt.session_id, user_id="d1-personal-a",
            client_request_id="OFFLINE", idempotency_scope="OFFLINE", request_hash="1" * 64, status="UNKNOWN_RESULT"))
        db.execute(text("UPDATE d1_personal_target SET receipt=:receipt"), {"receipt": json.dumps(asdict(receipt))})
    archive_path = coordinator.target.root / "docs/eval/personal-dev-runs/unknown-run.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps({"result": {"run_id": receipt.run_id,
        "status": "UNKNOWN_RESULT", "observations": {"runs": [{"run_id": receipt.run_id,
        "status": "UNKNOWN_RESULT"}], "run_events": []}}}), encoding="utf-8")
    safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
    assert safety["safe_to_cleanup"] is False
    assert safety["reason"] == "ARCHIVE_RUN_STATE_OR_EVIDENCE_INCOMPLETE"
    with pytest.raises(D1PreflightError, match="NONTERMINAL_OR_UNKNOWN_RUN_RETAINED"):
        coordinator.cleanup(receipt)
    with coordinator.factory() as db:
        assert db.get(Order, "ORD-PROBE-A") is not None
        assert db.get(Run, receipt.run_id).status == "UNKNOWN_RESULT"


def test_terminal_failed_run_with_saved_evidence_can_be_exactly_cleaned(coordinator):
    from dataclasses import asdict
    receipt = coordinator.prepare(plan_case(bundle(), "PROBE-CASE"))
    receipt.session_id = "FAILED-OFFLINE-SESSION"
    receipt.run_id = "FAILED-OFFLINE-RUN"
    owner_id = receipt.user_ids[0]
    with coordinator.factory() as db, db.begin():
        db.add(ConversationSession(session_id=receipt.session_id, user_id=owner_id, created_at=utc_now()))
        db.flush()
        db.add(Run(run_id=receipt.run_id, session_id=receipt.session_id, user_id=owner_id,
            client_request_id="FAILED-OFFLINE", idempotency_scope="FAILED-OFFLINE",
            request_hash="2" * 64, status="FAILED"))
        db.execute(text("UPDATE d1_personal_target SET receipt=:receipt"),
            {"receipt": json.dumps(asdict(receipt))})
    archive_path = coordinator.target.root / "docs/eval/personal-dev-runs/failed-run.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(json.dumps({"result": {"run_id": receipt.run_id,
        "status": "FAILED", "observations": {"runs": [{"run_id": receipt.run_id,
        "status": "FAILED"}], "run_events": []}}}), encoding="utf-8")
    safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
    assert safety["safe_to_cleanup"] is True
    coordinator.cleanup(receipt)
    evidence = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
    assert evidence["cleanup_status"] == "CLEANED_AND_RELEASED"
    assert evidence["pending_actions"] == 0 and evidence["residual_case_rows"] == 0
    assert_empty(coordinator)


def test_exception_recovery_captures_second_run_and_retains_ambiguous_receipt(coordinator):
    """An exception snapshot counts every Run and does not clean an ambiguous session."""
    from dataclasses import asdict
    from pathlib import Path

    receipt = coordinator.prepare(plan_case(bundle(), "PROBE-CASE"))
    receipt.session_id = "RECOVERY-SESSION"
    receipt.run_id = "RECOVERY-RUN-1"
    owner_id = receipt.user_ids[0]
    with coordinator.factory() as db, db.begin():
        session = ConversationSession(session_id=receipt.session_id, user_id=owner_id, created_at=utc_now())
        db.add(session)
        db.flush()
        for run_id, client_id, scope in (
            (receipt.run_id, "RECOVERY-CLIENT-1", "RECOVERY-SCOPE-1"),
            ("RECOVERY-RUN-2", "RECOVERY-CLIENT-2", "RECOVERY-SCOPE-2"),
        ):
            db.add(Run(run_id=run_id, session_id=receipt.session_id, user_id=owner_id,
                client_request_id=client_id, idempotency_scope=scope,
                request_hash=("1" if run_id == receipt.run_id else "2") * 64, status="SUCCEEDED"))
        db.add(RunEvent(event_id="RECOVERY-EVENT-1", run_id=receipt.run_id,
            session_id=receipt.session_id, trace_id="RECOVERY-TRACE-1", seq=1,
            timestamp=utc_now(), event_type="agent_started", payload_json='{"agent":"offline"}'))
        db.add(Trace(trace_id="RECOVERY-TRACE-1", user_id=owner_id, event_json="{}",
            created_at=utc_now(), run_id=receipt.run_id, event_type="agent_started", status="RECORDED"))
        db.execute(text("UPDATE d1_personal_target SET receipt=:receipt"),
            {"receipt": json.dumps(asdict(receipt))})

    captured = coordinator.capture_active_run_after_exception(case_id="PROBE-CASE",
        user_query="offline synthetic query", error_type="RuntimeError")
    assert captured["captured"] is True
    assert captured["run_id"] == receipt.run_id
    assert captured["run_status"] == "SUCCEEDED"
    assert captured["agent_started"] is True
    assert captured["event_count"] == 1
    assert captured["trace_count"] == 1
    assert captured["run_count"] == 2
    archive_path = coordinator.target.root / Path(captured["evidence_path"])
    archived = json.loads(archive_path.read_text(encoding="utf-8"))
    assert archived["recovery"]["classification"] == "EXECUTION_EXCEPTION"
    assert archived["recovery"]["error_type"] == "RuntimeError"
    assert len(archived["result"]["observations"]["runs"]) == 2
    assert coordinator.inspect_active_run_evidence()["run_count"] == 2
    safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
    assert safety == {"safe_to_cleanup": False, "reason": "DATABASE_RUN_NOT_TERMINAL_OR_AMBIGUOUS"}
    with coordinator.factory() as db:
        assert db.get(Run, "RECOVERY-RUN-2") is not None
        marker = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        assert marker == receipt.lease_id


@pytest.mark.parametrize("field", ["model", "provider_base_url"])
def test_runtime_permit_must_bind_model_and_endpoint_before_preload(coordinator, monkeypatch, field):
    from datetime import datetime, timedelta, timezone
    from eval.m6_1.budget import BudgetLimits
    from eval.m6_1.contracts import sha256_json
    # Invalid synthetic permit plus a verifier test double; no real grant is
    # issued, no asset is made ELIGIBLE, and execution must never be reached.
    monkeypatch.setattr("eval.m6_1.personal_dev.implementation_digest", lambda _root: "2" * 64)
    limits = BudgetLimits()
    settings = SimpleNamespace(model="OFFLINE-NO-MODEL", base_url="https://invalid.test")
    permit = {"test_only": True, "actor_type": "CHAT_USER_SELF_CONFIRMED",
        "asset_sha256": "1" * 64, "case_ids": ["PROBE-CASE"],
        "database_identity": coordinator.target.identity, "code_sha256": "2" * 64,
        "budget_sha256": sha256_json(limits.to_dict()), "model": settings.model,
        "provider_base_url": settings.base_url,
        "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    permit[field] = "MISMATCH-NO-AUTHORIZATION"
    with pytest.raises(D1PreflightError, match="RUNTIME_PERMIT_SCOPE_MISMATCH"):
        coordinator.execute_authorized(bundle=bundle(), case_id="PROBE-CASE",
            asset=SimpleNamespace(split="dev", sha256="1" * 64),
            grant=SimpleNamespace(valid_for=lambda _asset: True), permit=permit,
            ledger=SimpleNamespace(limits=limits), settings=settings)
    assert_empty(coordinator)
