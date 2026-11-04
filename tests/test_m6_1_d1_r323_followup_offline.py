"""Targeted synthetic D1 r3.2.3 contract checks; no app runtime or external services."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.m1.models import Base, Order, Shipment, User
from app.m3.order_tools import SqlAlchemyOrderReadRepository
from app.m4.actions import ActionError, prepare_return_action
from app.tools.catalog import get_tool_contract
from eval.m6_1.contracts import EvalCase
from eval.m6_1.shipment_fixture_reset import load_bundle_shipments


ASSET = Path(__file__).resolve().parents[1] / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.3.json"


def _asset() -> dict:
    return json.loads(ASSET.read_text(encoding="utf-8"))


def test_aftersales_024_reason_boundary_is_documented_without_action_proposal() -> None:
    case = next(row for row in _asset()["cases"] if row["case_id"] == "D1-AFTERSALES-024")
    EvalCase.from_dict(case)
    allowed = get_tool_contract("create_return_request").input_schema["properties"]["reason_code"]["enum"]
    boundary = case["expected_outcome"]["seven_dimension_trace"]["tool_agent_outcome"]["future_contract_boundary"]
    assert "WRONG_SIZE" not in allowed and "OTHER" in allowed
    assert boundary["user_reason"] == "WRONG_SIZE"
    assert boundary["tool_reason_code_if_separately_authorized"] == "OTHER"
    assert boundary["status"] == "DOCUMENTED_ONLY_NOT_PROPOSED"
    assert "USER_CLAIM_UNVERIFIED" in boundary["reason_summary_treatment"]
    assert "action_proposal" not in case["expected_outcome"]
    assert all(call["mode"] == "read_only" for call in case["input"]["expected_tool_calls"])
    assert case["group_id"] == "D1-GRP-LKG-015-R3"
    assert case["input"]["review_status"] == "PENDING_HUMAN_REVIEW"


def test_aftersales_024_missing_paid_at_blocks_current_return_preflight() -> None:
    class NoRetrieval:
        def pin_view(self, _evaluation_at: str) -> None:
            raise AssertionError("missing payment time must fail before retrieval")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(User(user_id="SYN-USER-A", display_name="synthetic"))
            # Schema-only sentinel: D1-AFTERSALES-024 has no verified payment amount.
            db.add(Order(order_id="ORD-SYN-119", user_id="SYN-USER-A", status="DELIVERED",
                paid_at=None, delivered_at="2026-10-22T12:00:00Z", paid_cents=1, version=1))
            db.commit()
            with pytest.raises(ActionError) as invalid_reason:
                prepare_return_action(db, owner_id="SYN-USER-A", order_id="ORD-SYN-119",
                    reason_code="WRONG_SIZE", reason_summary="Wrong size (user claim)",
                    retrieval_service=NoRetrieval())
            assert invalid_reason.value.code == "RETURN_REASON_INVALID"
            with pytest.raises(ActionError) as missing_payment:
                prepare_return_action(db, owner_id="SYN-USER-A", order_id="ORD-SYN-119",
                    reason_code="OTHER", reason_summary="Wrong size (user claim)",
                    retrieval_service=NoRetrieval())
            assert missing_payment.value.code == "RETURN_ORDER_FACTS_INSUFFICIENT"
    finally:
        engine.dispose()


def test_r323_empty_shipment_bundle_returns_unknown_not_unshipped() -> None:
    asset = _asset()
    assert asset["fixtures"]["shipments"] == []
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[User.__table__, Order.__table__, Shipment.__table__])
        factory = sessionmaker(bind=engine)
        with factory() as db:
            db.add(User(user_id="db-user-a", display_name="synthetic"))
            db.add(Order(order_id="ORD-SYN-102", user_id="db-user-a", status="PAID",
                paid_at=None, delivered_at=None, paid_cents=1, version=1))
            db.commit()
            receipt = load_bundle_shipments(db, asset, owner_id_map={"SYN-USER-A": "db-user-a"},
                probe_id="r323empty")
            assert receipt.shipment_receipts == ()
            assert db.execute(select(Shipment.shipment_id)).scalars().all() == []
        result = SqlAlchemyOrderReadRepository(factory).track_shipment("ORD-SYN-102", "db-user-a")
        assert result["availability"] == "UNAVAILABLE"
        assert result["error_code"] == "NO_SHIPMENT_RECORD"
        assert result["records"] == []
        case = next(row for row in asset["cases"] if row["case_id"] == "D1-ORDER-008")
        assert "Do not infer" in case["expected_outcome"]["business_outcome"]
    finally:
        engine.dispose()


def test_shipment_query_never_reuses_another_order_record() -> None:
    # Synthetic repository probe only; this row is not added to r3.2.3.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine, tables=[User.__table__, Order.__table__, Shipment.__table__])
        factory = sessionmaker(bind=engine)
        with factory() as db:
            db.add(User(user_id="db-user-a", display_name="synthetic"))
            db.add_all([
                Order(order_id="PROBE-ORDER-A", user_id="db-user-a", status="PAID",
                    paid_at=None, delivered_at=None, paid_cents=1, version=1),
                Order(order_id="PROBE-ORDER-B", user_id="db-user-a", status="PAID",
                    paid_at=None, delivered_at=None, paid_cents=1, version=1),
            ])
            db.flush()
            db.add(Shipment(shipment_id="PROBE-SHIP-A", order_id="PROBE-ORDER-A",
                status="IN_TRANSIT", observed_at="2026-10-25T00:00:00Z"))
            db.commit()
        repo = SqlAlchemyOrderReadRepository(factory)
        assert repo.track_shipment("PROBE-ORDER-A", "db-user-a")["records"] == [
            {"status": "IN_TRANSIT", "observed_at": "2026-10-25T00:00:00Z"}]
        other = repo.track_shipment("PROBE-ORDER-B", "db-user-a")
        assert other["availability"] == "UNAVAILABLE"
        assert other["error_code"] == "NO_SHIPMENT_RECORD"
        assert other["records"] == []
    finally:
        engine.dispose()
