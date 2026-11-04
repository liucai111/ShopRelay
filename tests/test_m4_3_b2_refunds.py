from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.m0.data import ROOT, seed
from app.m1.api import create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import AfterSalesEvent, ConversationSession, Order, Refund, ReturnRequest, Run, Shipment, utc_now
from app.m2.retrieval import RetrievalService, config_from_environment
from app.m4.actions import ActionError, WRITE_POLICIES, prepare_refund_action, server_permission_granted
from app.skills.registry import SKILL_REGISTRY
from app.tools.catalog import TOOL_CATALOG


@pytest.fixture
def refund_db(tmp_path):
    path = tmp_path / "m43b2-refund.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    now = utc_now()
    suffix = uuid.uuid4().hex[:12]
    orders = {name: f"M43B2-{name}-{suffix}" for name in ("paid", "shipped", "delivered", "pending", "owner")}
    with factory() as db:
        for name, order_id in orders.items():
            status = "DELIVERED" if name in {"delivered", "pending"} else "SHIPPED" if name == "shipped" else "PAID"
            db.add(Order(order_id=order_id, user_id="u_demo", status=status,
                paid_at="2026-09-20T10:00:00Z", delivered_at="2026-10-01T10:00:00Z" if status == "DELIVERED" else None,
                paid_cents=12450, version=1))
        db.flush()
        db.add(Shipment(shipment_id="M43B2-SHIP-" + suffix, order_id=orders["shipped"],
            status="IN_TRANSIT", observed_at="2026-10-01T12:00:00Z"))
        return_id = "M43B2-RETURN-" + suffix
        db.add(ReturnRequest(return_id=return_id, order_id=orders["delivered"], status="APPROVED",
            reason="SIMULATED_TEST_DATA", created_at="2026-10-01T10:00:00Z", submitted_at=now,
            approved_at=now, source_type="SIMULATED_TEST_DATA", source_ref="M43B2-APPROVAL-" + suffix))
        db.flush()
        db.add(AfterSalesEvent(event_id="M43B2-EVENT-" + suffix, order_id=orders["delivered"],
            return_id=return_id, refund_id=None, event_type="RETURN_APPROVED", occurred_at=now,
            recorded_at=now, actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
            source_ref="M43B2-APPROVAL-" + suffix, details_json="{}"))
        db.add(ReturnRequest(return_id="M43B2-PENDING-" + suffix, order_id=orders["pending"], status="UNDER_REVIEW",
            reason="SIMULATED_TEST_DATA", created_at="2026-10-01T10:00:00Z", submitted_at=now,
            review_started_at=now, source_type="SIMULATED_TEST_DATA", source_ref="M43B2-PENDING-SOURCE-" + suffix))
        db.flush()
        db.add(ConversationSession(session_id="M43B2-SESSION-" + suffix, user_id="u_demo", created_at=now,
            title="isolated refund test"))
        db.add(Run(run_id="M43B2-RUN-" + suffix, user_id="u_demo", session_id="M43B2-SESSION-" + suffix,
            client_request_id="m43b2-" + suffix, idempotency_scope="m43b2-test", request_hash="a" * 64,
            status="CREATED"))
        db.commit()
    retrieval = RetrievalService(ROOT / "data" / "index.json", config_from_environment())
    yield factory, engine, orders, return_id, retrieval
    engine.dispose()


def test_refund_policy_paths_are_distinct_and_amount_is_server_derived(refund_db):
    factory, _, orders, return_id, retrieval = refund_db
    with factory() as db:
        args, proposal = prepare_refund_action(db, owner_id="u_demo", order_id=orders["paid"],
            reason_code="PRE_SHIPMENT", reason_summary="订单尚未发货", retrieval_service=retrieval)
    assert args["amount_cents"] == 12450
    assert args["fact_snapshot"]["branch"] == "PAID_NOT_SHIPPED"
    assert args["reason_source"] == "USER_CLAIM_UNVERIFIED"
    assert proposal["review_required"] is True
    assert {row["clause_id"] for row in args["policy_snapshot"]["evidence"]} == {"before-shipping", "uniqueness"}
    assert args["policy_snapshot"]["knowledge_version"] != args["policy_snapshot"]["evidence"][0]["version"]

    with factory() as db:
        delivered, _ = prepare_refund_action(db, owner_id="u_demo", order_id=orders["delivered"],
            reason_code="APPROVED_RETURN", reason_summary="按已批准退货申请退款", retrieval_service=retrieval)
    assert delivered["return_id"] == return_id
    assert delivered["fact_snapshot"]["approval_event_id"].startswith("M43B2-EVENT-")
    assert delivered["application_outcome"] == "SUBMITTED_PENDING_HUMAN_REVIEW"


@pytest.mark.parametrize(("order_name", "expected"), [
    ("shipped", "REFUND_REQUIRES_MANUAL_REVIEW"),
    ("pending", "RETURN_NOT_APPROVED"),
])
def test_refund_prep_does_not_promote_manual_or_pending_cases(refund_db, order_name, expected):
    factory, _, orders, _, retrieval = refund_db
    with factory() as db, pytest.raises(ActionError) as error:
        prepare_refund_action(db, owner_id="u_demo", order_id=orders[order_name],
            reason_code="OTHER", reason_summary="用户要求退款", retrieval_service=retrieval)
    assert error.value.code == expected


def test_refund_prep_is_owner_scoped_and_existing_open_refund_blocks(refund_db):
    factory, _, orders, _, retrieval = refund_db
    with factory() as db, pytest.raises(ActionError) as wrong_owner:
        prepare_refund_action(db, owner_id="u_other", order_id=orders["paid"],
            reason_code="PRE_SHIPMENT", reason_summary="申请退款", retrieval_service=retrieval)
    assert wrong_owner.value.code == "ORDER_NOT_FOUND"
    with factory() as db:
        db.add(Refund(refund_id="M43B2-EXISTING-" + uuid.uuid4().hex[:8], order_id=orders["paid"],
            return_id=None, status="SUBMITTED", amount_cents=12450, reason="existing",
            created_at="2026-10-01T10:00:00Z", submitted_at=utc_now(),
            source_type="SIMULATED_TEST_DATA", source_ref="M43B2-EXISTING-SOURCE-" + uuid.uuid4().hex[:8]))
        db.commit()
    with factory() as db, pytest.raises(ActionError) as active:
        prepare_refund_action(db, owner_id="u_demo", order_id=orders["paid"],
            reason_code="PRE_SHIPMENT", reason_summary="重复申请退款", retrieval_service=retrieval)
    assert active.value.code == "REFUND_ALREADY_OPEN"


def test_refund_write_policy_isolated_and_skill_is_proposal_only(refund_db):
    factory, engine, orders, _, _ = refund_db
    policy = WRITE_POLICIES["create_refund_request"]
    assert policy.execution_mode == "ISOLATED_REFUND_TRANSACTION"
    assert policy.allowed_environment == ("shoprelay_m11_test",)
    assert policy.confirmation_required and policy.audit_required
    assert not TOOL_CATALOG["create_refund_request"].mcp_allowed
    assert "create_refund_request" in SKILL_REGISTRY["return_handling"].proposal_tools
    assert "create_refund_request" not in SKILL_REGISTRY["return_handling"].allowed_tools
    app = create_app(database_url=str(engine.url), engine=engine, action_writes_enabled=True,
        refund_actions_enabled=True, orchestrator_mode="legacy")
    assert app.state.refund_actions_enabled is False
    assert server_permission_granted("u_demo", "create_refund_request", "shoprelay_m11_test")
    assert not server_permission_granted("u_other", "create_refund_request", "shoprelay_m11_test")
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Refund)) == 0
