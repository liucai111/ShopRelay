from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from pydantic import ValidationError

from app.m0.config import ROOT
from app.m0.data import seed
from app.m1.api import CreatePendingActionInput, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import (ConversationSession, Inventory, Order, OrderInventoryReservation, OrderItem, Product,
                           Refund, Run, Shipment, utc_now)
from app.m2.retrieval import RetrievalService, config_from_environment
from app.m4.actions import (ActionError, SERVER_PERMISSION_GRANTS, WRITE_POLICIES,
                            prepare_cancel_action, server_permission_granted)
from app.skills.registry import SKILL_REGISTRY, bind_skill
from app.tools.catalog import TOOL_CATALOG


@pytest.fixture
def cancellation_db(tmp_path):
    path = tmp_path / "m43b3-cancel.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    suffix = uuid.uuid4().hex[:10]
    order_ids = {name: f"M43B3-{name}-{suffix}" for name in
                 ("created", "paid", "shipped", "delivered", "refund", "owner", "missing_stock")}
    with factory() as db:
        product = db.execute(select(Product).order_by(Product.product_id)).scalars().first()
        no_inventory_product = Product(product_id=f"P-M43B3-{suffix}", name="B3 isolated fixture",
            category="ACCESSORY", price_cents=100)
        db.add(no_inventory_product)
        db.flush()
        inventory = db.get(Inventory, product.product_id)
        for name, order_id in order_ids.items():
            status = {"created": "CREATED", "paid": "PAID", "shipped": "SHIPPED",
                      "delivered": "DELIVERED", "refund": "PAID", "owner": "CREATED",
                      "missing_stock": "CREATED"}[name]
            is_paid = status in {"PAID", "SHIPPED", "DELIVERED"}
            db.add(Order(order_id=order_id, user_id="u_other" if name == "owner" else "u_demo",
                status=status, paid_at="2026-09-20T10:00:00Z" if is_paid else None,
                delivered_at="2026-10-01T10:00:00Z" if status == "DELIVERED" else None,
                paid_cents=1200 if is_paid else 0, version=1))
            db.add(OrderItem(order_id=order_id, product_id=no_inventory_product.product_id if name == "missing_stock" else product.product_id,
                quantity=1, paid_cents=1200 if is_paid else 0))
            db.flush()
            if name != "missing_stock":
                inventory.reserved_qty += 1
                db.add(OrderInventoryReservation(reservation_id=f"M43B3-RES-{name}-{suffix}",
                    order_id=order_id, product_id=product.product_id, quantity=1, status="RESERVED",
                    source_type="SIMULATED_TEST_DATA", source_ref=f"M43B3-RES-SRC-{name}-{suffix}",
                    created_at=utc_now()))
        db.flush()
        db.add(Shipment(shipment_id=f"M43B3-SHIP-{suffix}", order_id=order_ids["shipped"],
            status="IN_TRANSIT", observed_at="2026-10-01T12:00:00Z"))
        db.add(Refund(refund_id=f"M43B3-REF-{suffix}", order_id=order_ids["refund"],
            return_id=None, status="SUBMITTED", amount_cents=1200, reason="fixture",
            created_at="2026-10-01T10:00:00Z", submitted_at=utc_now(),
            source_type="SIMULATED_TEST_DATA", source_ref=f"M43B3-REFSRC-{suffix}"))
        session_id = f"M43B3-SESSION-{suffix}"
        run_id = f"M43B3-RUN-{suffix}"
        db.add(ConversationSession(session_id=session_id, user_id="u_demo", created_at=utc_now(), title="isolated cancellation"))
        db.add(Run(run_id=run_id, user_id="u_demo", session_id=session_id,
            client_request_id=f"m43b3-{suffix}", idempotency_scope="m43b3-test",
            request_hash="a" * 64, status="CREATED"))
        db.commit()
    retrieval = RetrievalService(ROOT / "data" / "index.json", config_from_environment())
    yield factory, engine, order_ids, inventory.product_id, session_id, run_id, retrieval
    engine.dispose()


def test_cancel_proposals_are_server_derived_and_do_not_mutate(cancellation_db):
    factory, _, orders, product_id, _, _, retrieval = cancellation_db
    with factory() as db:
        before = db.get(Inventory, product_id).reserved_qty
        args, proposal = prepare_cancel_action(db, owner_id="u_demo", order_id=orders["created"],
            reason_code="USER_REQUEST", reason_summary="用户申请取消未付款订单", retrieval_service=retrieval)
        db.rollback()
    assert args["business_outcome"] == "CANCELLED_NO_PAYMENT"
    assert proposal["eligibility"] == "POLICY_PRECONDITIONS_SATISFIED_REQUIRES_CONFIRMATION"
    assert {tuple((row["policy_id"], row["clause_id"])) for row in args["policy_snapshot"]["evidence"]} == {("cancel", "eligible")}
    with factory() as db:
        paid, _ = prepare_cancel_action(db, owner_id="u_demo", order_id=orders["paid"],
            reason_code="OTHER", reason_summary="隔离退款协同取消", retrieval_service=retrieval)
        db.rollback()
    assert paid["business_outcome"] == "CANCELLED_WITH_REFUND_APPLICATION"
    assert {row["clause_id"] for row in paid["policy_snapshot"]["evidence"] if row["policy_id"] == "refund"} == {
        "before-shipping", "uniqueness"}
    with factory() as db:
        assert db.get(Order, orders["created"]).status == "CREATED"
        assert db.get(Inventory, product_id).reserved_qty == before
        assert db.scalar(select(func.count()).select_from(Refund).where(Refund.order_id == orders["paid"])) == 0


@pytest.mark.parametrize(("name", "owner", "expected"), [
    ("shipped", "u_demo", "CANCEL_NOT_ALLOWED"),
    ("delivered", "u_demo", "CANCEL_NOT_ALLOWED"),
    ("refund", "u_demo", "STATE_CONFLICT"),
    ("owner", "u_demo", "ORDER_NOT_FOUND"),
    ("missing_stock", "u_demo", "CANCEL_INVENTORY_FACTS_UNKNOWN"),
])
def test_cancel_proposal_fails_closed_on_disallowed_facts(cancellation_db, name, owner, expected):
    factory, _, orders, _, _, _, retrieval = cancellation_db
    with factory() as db, pytest.raises(ActionError) as error:
        prepare_cancel_action(db, owner_id=owner, order_id=orders[name],
            reason_code="USER_REQUEST", reason_summary="检查取消条件", retrieval_service=retrieval)
    assert error.value.code == expected


def test_aggregate_inventory_without_order_scoped_reservation_is_not_released(cancellation_db):
    factory, _, orders, product_id, _, _, retrieval = cancellation_db
    with factory() as db:
        reservation = db.execute(select(OrderInventoryReservation).where(
            OrderInventoryReservation.order_id == orders["created"])).scalar_one()
        db.delete(reservation)
        db.commit()
    with factory() as db, pytest.raises(ActionError) as error:
        prepare_cancel_action(db, owner_id="u_demo", order_id=orders["created"],
            reason_code="USER_REQUEST", reason_summary="检查订单级预留", retrieval_service=retrieval)
    assert error.value.code == "CANCEL_INVENTORY_FACTS_UNKNOWN"
    with factory() as db:
        assert db.get(Inventory, product_id).reserved_qty > 0
        assert db.get(Order, orders["created"]).status == "CREATED"


def test_cancel_strict_contract_and_isolated_default_closed(cancellation_db):
    factory, engine, orders, _, session_id, run_id, _ = cancellation_db
    payload = {"tool_id": "cancel_order", "session_id": session_id, "run_id": run_id,
        "target_resource_id": orders["created"],
        "arguments": {"order_id": orders["created"], "reason_code": "USER_REQUEST",
                      "reason_summary": "隔离测试", "user_id": "u_other"},
        "idempotency_key": "b3-strict"}
    with pytest.raises(ValidationError):
        CreatePendingActionInput.model_validate(payload)
    app = create_app(database_url=str(engine.url), engine=engine, action_writes_enabled=True,
        cancel_actions_enabled=True, orchestrator_mode="legacy")
    assert app.state.cancel_actions_enabled is False
    assert WRITE_POLICIES["cancel_order"].execution_mode == "ISOLATED_ORDER_CANCELLATION_TRANSACTION"
    assert WRITE_POLICIES["cancel_order"].allowed_environment == ("shoprelay_m11_test",)
    assert server_permission_granted("u_demo", "cancel_order", "shoprelay_m11_test")
    assert not server_permission_granted("u_other", "cancel_order", "shoprelay_m11_test")
    assert not server_permission_granted("u_demo", "cancel_order", "production")
    assert TOOL_CATALOG["cancel_order"].confirmation_required
    assert not TOOL_CATALOG["cancel_order"].mcp_allowed
    skill = SKILL_REGISTRY["order_cancellation"]
    assert "cancel_order" in skill.proposal_tools and "cancel_order" not in skill.allowed_tools
    binding = bind_skill("\u53d6\u6d88\u8ba2\u5355", {"strategy": "AGENT",
        "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]})
    assert binding["skill_id"] == "order_cancellation"
    assert binding["skill_proposable_tools"] == ["cancel_order"]
    assert "orders.cancel" in SERVER_PERMISSION_GRANTS["u_demo"]
