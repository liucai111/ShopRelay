"""D1 asset audit and Shipment bridge checks; synthetic in-memory SQLite only.

These tests do not import or start the Runner, adapters, Agent, or application.
Positive Shipment rows belong only to independent repository probes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, Order, Shipment, User
from app.m3.order_tools import SqlAlchemyOrderReadRepository, ToolExecutionError
from eval.m6_1.contracts import EvalCase
from eval.m6_1.shipment_fixture_loader import ShipmentFixtureError
from eval.m6_1.shipment_fixture_reset import cleanup_bundle_shipments, load_bundle_shipments


ROOT = Path(__file__).resolve().parents[1]
DRAFTS = ROOT / "eval/m6_1/drafts"
SHIPMENT_CASES = [
    "D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022",
    "D1-AFTERSALES-023", "D1-AFTERSALES-025", "D1-CROSSAGENT-028",
    "D1-CROSSAGENT-031",
]


def test_r324_preserves_cases_fixtures_and_formal_review_boundaries():
    parent_path = DRAFTS / "m6.1-d1-synthetic-dev-r3.2.3.json"
    assert hashlib.sha256(parent_path.read_bytes()).hexdigest() == (
        "e8809d4008b9446a621b93018b3aecdd68fed7acef947bf7fbb976ecc5d4afac")
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    asset = json.loads((DRAFTS / "m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf-8"))
    assert asset["case_count"] == len(asset["cases"]) == 40
    assert len({c["case_id"] for c in asset["cases"]}) == 40
    assert [(c["case_id"], c["group_id"]) for c in asset["cases"]] == [
        (c["case_id"], c["group_id"]) for c in parent["cases"]]
    fixtures = copy.deepcopy(asset["fixtures"])
    assert fixtures.pop("fixture_id") == "m6.1-d1-synthetic-fixtures-r3.2.4"
    previous_fixtures = copy.deepcopy(parent["fixtures"])
    previous_fixtures.pop("fixture_id")
    assert fixtures == previous_fixtures
    assert fixtures["shipments"] == []
    for current, previous in zip(asset["cases"], parent["cases"]):
        EvalCase.from_dict(current)
        assert current["split"] == "dev"
        assert current["fixture_ref"] == "m6.1-d1-synthetic-fixtures-r3.2.4"
        assert current["data_version"] == asset["version"] == "m6.1-d1-dev-draft-r3.2.4"
        assert current["input"]["review_status"] == "PENDING_HUMAN_REVIEW"
        assert current["input"]["reviewer_id"] == "UNKNOWN"
        assert current["input"]["reviewed_at_utc"] is None
        assert current["expected_outcome"]["review_status"] == "PENDING_HUMAN_REVIEW"
        normalized = copy.deepcopy(current)
        normalized["fixture_ref"] = previous["fixture_ref"]
        normalized["data_version"] = previous["data_version"]
        if current["case_id"] == "D1-AFTERSALES-023":
            # Only clarify the six existing audit descriptions; retain safety rules.
            normalized["input"]["required_conditions"][2] = previous["input"]["required_conditions"][2]
            for key in ("business_outcome", "blocked_action_capability", "critical_facts"):
                normalized["expected_outcome"][key] = previous["expected_outcome"][key]
            normalized["expected_outcome"]["hard_gates"][2] = previous["expected_outcome"]["hard_gates"][2]
            normalized["expected_outcome"]["semantic_quality_rubric"]["must_include"][1] = (
                previous["expected_outcome"]["semantic_quality_rubric"]["must_include"][1])
        assert normalized == previous
    for key in ("review_status", "reviewer_id", "reviewed_at_utc", "review_method",
                "independence_status", "exposure_status", "scoreability", "access_policy"):
        assert asset[key] == parent[key]
    affected = [c["case_id"] for c in asset["cases"] if any(
        call["tool_id"] == "track_shipment" for call in c["input"]["expected_tool_calls"])]
    audit = asset["source_mapping_audit"]
    assert affected == audit["affected_case_ids"] == SHIPMENT_CASES
    assert audit["fixture_rows"] == 0
    assert audit["code_level_loader_mapping_status"] == "ISOLATED_OFFLINE_COMPONENT_VERIFIED"
    assert audit["runner_fixture_reset_integration_status"] == "NOT_VERIFIED"
    assert audit["runner_owner_order_mapping_status"] == "NOT_VERIFIED"
    assert audit["formal_evaluation_authorization"] == audit["dev_smoke_authorization"] == "NOT_AUTHORIZED"
    assert audit["no_record_semantics"] == "UNKNOWN"
    assert audit["limitation"] in asset["limitations"]
    question = asset["business_rule_audit"]
    assert question["universal_return_eligibility_rule_status"] == "BUSINESS_OWNER_CONFIRMATION_PENDING"
    assert question["formal_decision"] is None
    case024 = next(c for c in asset["cases"] if c["case_id"] == "D1-AFTERSALES-024")
    assert "action_proposal" not in case024["expected_outcome"]
    assert case024["expected_outcome"]["blocked_action_capability"]["status"] == "NOT_PROPOSED"
    assert all(call["mode"] == "read_only" for call in case024["input"]["expected_tool_calls"])


@pytest.fixture
def isolated_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__, Order.__table__, Shipment.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.add_all([User(user_id="db-owner-a", display_name="Probe A"),
                        User(user_id="db-owner-b", display_name="Probe B")])
            db.flush()
            db.add_all([Order(order_id=order_id, user_id=owner_id, status="PAID",
                paid_at=None, delivered_at=None, paid_cents=100, version=1)
                for order_id, owner_id in [("PROBE-A", "db-owner-a"),
                    ("PROBE-A-OTHER", "db-owner-a"), ("PROBE-B", "db-owner-b")]])
            db.commit()
            yield db, factory
    finally:
        engine.dispose()


def probe_bundle(*, with_shipment=True):
    return {"fixtures": {
        "users": [{"user_id": "SYN-ALIAS-A"}],
        "orders_and_snapshots": [
            {"id": "probe-order-a", "order_id": "PROBE-A", "owner_id": "SYN-ALIAS-A"},
            {"id": "probe-order-other", "order_id": "PROBE-A-OTHER", "owner_id": "SYN-ALIAS-A"},
        ],
        "shipments": [{"shipment_id": "probe-shipment", "order_id": "PROBE-A",
            "status": "IN_TRANSIT", "observed_at": "2026-10-25T08:00:00+08:00"}] if with_shipment else [],
    }}


def load_probe(db, *, with_shipment=True):
    return load_bundle_shipments(db, probe_bundle(with_shipment=with_shipment),
        owner_id_map={"SYN-ALIAS-A": "db-owner-a"}, probe_id="r324probe")


def assert_unknown(result):
    # Repository API uses UNAVAILABLE/NO_SHIPMENT_RECORD; its meaning is UNKNOWN.
    assert result["availability"] == "UNAVAILABLE"
    assert result["error_code"] == "NO_SHIPMENT_RECORD"
    assert result["records"] == []


def test_empty_bundle_reset_inserts_zero_and_preserves_unknown(isolated_db):
    db, factory = isolated_db
    for _ in range(2):
        receipt = load_probe(db, with_shipment=False)
        assert receipt.shipment_receipts == ()
        assert cleanup_bundle_shipments(db, receipt) == 0
    db.commit()
    assert db.execute(select(Shipment.shipment_id)).scalars().all() == []
    assert_unknown(SqlAlchemyOrderReadRepository(factory).track_shipment("PROBE-A", "db-owner-a"))


def test_bundle_bridge_loads_exact_order_owner_status_and_utc_time(isolated_db):
    db, factory = isolated_db
    receipt = load_probe(db)
    db.commit()
    assert receipt.shipment_receipts[0].owner_id == "db-owner-a"
    assert receipt.shipment_receipts[0].order_ids == ("PROBE-A",)
    assert receipt.shipment_receipts[0].shipment_ids == ("EV-r324probe-probe-shipment",)
    repo = SqlAlchemyOrderReadRepository(factory)
    result = repo.track_shipment("PROBE-A", "db-owner-a")
    assert result["order_id"] == "PROBE-A"
    assert result["availability"] == "AVAILABLE"
    assert result["records"] == [{"status": "IN_TRANSIT", "observed_at": "2026-10-25T00:00:00Z"}]
    with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
        repo.track_shipment("PROBE-A", "db-owner-b")


def test_same_owner_other_order_does_not_inherit_loaded_shipment(isolated_db):
    db, factory = isolated_db
    load_probe(db)
    db.commit()
    result = SqlAlchemyOrderReadRepository(factory).track_shipment("PROBE-A-OTHER", "db-owner-a")
    assert result["order_id"] == "PROBE-A-OTHER"
    assert_unknown(result)


def test_duplicate_reset_rejected_then_exact_cleanup_allows_clean_reload(isolated_db):
    db, factory = isolated_db
    db.add(Shipment(shipment_id="unrelated-probe", order_id="PROBE-B",
        status="IN_TRANSIT", observed_at="2026-10-24T00:00:00Z"))
    db.commit()
    receipt = load_probe(db)
    db.commit()
    with pytest.raises(ShipmentFixtureError, match="SHIPMENT_ID_ALREADY_EXISTS"):
        load_probe(db)
    assert sorted(db.execute(select(Shipment.shipment_id)).scalars()) == [
        "EV-r324probe-probe-shipment", "unrelated-probe"]
    assert cleanup_bundle_shipments(db, receipt) == 1
    db.commit()
    assert cleanup_bundle_shipments(db, receipt) == 0
    db.commit()
    assert_unknown(SqlAlchemyOrderReadRepository(factory).track_shipment("PROBE-A", "db-owner-a"))
    reloaded = load_probe(db)
    db.commit()
    assert reloaded == receipt
    assert cleanup_bundle_shipments(db, reloaded) == 1
    db.commit()
    assert db.execute(select(Shipment.shipment_id)).scalars().all() == ["unrelated-probe"]
