"""M4.2 owner-confirmed action framework and isolated test-only executor.

No production business write tool is executable in this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.m1.models import (ActionAudit, AfterSalesEvent, ConversationSession, ObservabilityRecord,
                           Inventory, Order, OrderEvent, OrderInventoryReservation, OrderItem, PendingAction, Refund, ReturnRequest, Run, RunEvent, Shipment, SimulatedTestAction,
                           SupportTicket, SupportTicketEvent, Trace, utc_now)
from app.m1.services import new_id
from app.m1.models import safe_trace_json
from app.m3.policy_tools import evidence_is_valid


class ActionError(Exception):
    def __init__(self, code: str, status_code: int, message: str):
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ToolWritePolicy:
    tool_id: str
    capability: str
    risk_level: str
    required_permissions: tuple[str, ...]
    confirmation_required: bool
    execution_mode: str
    allowed_environment: tuple[str, ...]
    schema_version: str
    audit_required: bool


# Local server policy is authoritative. Remote MCP annotations and model proposals
# are intentionally absent from the authorization decision. This is a policy
# inventory; isolated test, return, refund and order-cancellation executors each
# require their own explicit server switch and the exact test database gate.
WRITE_POLICIES: dict[str, ToolWritePolicy] = {}


def _policy(tool_id: str, capability: str, risk: str, permissions: tuple[str, ...],
            confirm: bool, mode: str, environments: tuple[str, ...], schema: str = "1") -> ToolWritePolicy:
    return ToolWritePolicy(tool_id, capability, risk, permissions, confirm, mode, environments, schema, True)


for _tool_id, _capability in {
    "get_order": "orders.read", "get_order_items": "orders.items.read",
    "track_shipment": "logistics.read", "get_refund_status": "after_sales.refund.read",
    "search_products": "products.search", "get_product_detail": "products.detail.read",
    "recommend_products": "products.recommend", "check_inventory": "inventory.read",
    "search_knowledge": "policy.search",
}.items():
    WRITE_POLICIES[_tool_id] = _policy(_tool_id, _capability, "READ_ONLY", ("business:read",),
        False, "NATIVE_READ", ("shoprelay_m11_test", "production"))

for _tool_id, _capability in {
    "create_refund_request": "after_sales.refund.create",
    "create_return_request": "after_sales.return.create", "cancel_order": "orders.cancel",
}.items():
    WRITE_POLICIES[_tool_id] = _policy(_tool_id, _capability, "WRITE_HIGH_RISK",
        ("actions:confirm", _capability), True, "DISABLED", ())

for _tool_id, _capability in {
    "get_coupon": "coupons.read", "get_warranty": "warranty.read",
}.items():
    # These are reads, not Pending Actions. They have no confirmation or write
    # executor; native/MCP invocation still requires the independent read grant.
    WRITE_POLICIES[_tool_id] = _policy(_tool_id, _capability, "READ_ONLY", ("business:read",),
        False, "NATIVE_READ", ("shoprelay_m11_test", "production"))

WRITE_POLICIES["escalate_to_human"] = _policy("escalate_to_human", "support.ticket.create",
    "WRITE_LOW_RISK", ("actions:confirm", "support.ticket.create"), True,
    "ISOLATED_TICKET_TRANSACTION", ("shoprelay_m11_test",), schema="1")

WRITE_POLICIES["simulated_test_action"] = _policy("simulated_test_action",
    "m4.2.isolated_test_receipt", "WRITE_LOW_RISK", ("actions:confirm",), True,
    "LOCAL_TEST_TRANSACTION", ("shoprelay_m11_test",))
WRITE_POLICIES["create_return_request"] = _policy("create_return_request",
    "after_sales.return.create", "WRITE_HIGH_RISK",
    ("actions:confirm", "after_sales.return.create"), True,
    "ISOLATED_RETURN_TRANSACTION", ("shoprelay_m11_test",), schema="2")
WRITE_POLICIES["create_refund_request"] = _policy("create_refund_request",
    "after_sales.refund.create", "WRITE_HIGH_RISK",
    ("actions:confirm", "after_sales.refund.create"), True,
    "ISOLATED_REFUND_TRANSACTION", ("shoprelay_m11_test",), schema="1")
WRITE_POLICIES["cancel_order"] = _policy("cancel_order", "orders.cancel",
    "WRITE_HIGH_RISK", ("actions:confirm", "orders.cancel"), True,
    "ISOLATED_ORDER_CANCELLATION_TRANSACTION", ("shoprelay_m11_test",), schema="2")

# The only grants are server-owned and limited to the two seeded demo identities.
# They are not read from request data, model output, or MCP annotations.
SERVER_PERMISSION_GRANTS: dict[str, frozenset[str]] = {
    "u_demo": frozenset({"actions:confirm", "after_sales.return.create", "after_sales.refund.create", "orders.cancel", "support.ticket.create", "support.ticket.draft"}),
    "u_other": frozenset({"actions:confirm", "support.ticket.create", "support.ticket.draft"}),
}


def server_draft_permission_granted(owner_id: str, permission: str) -> bool:
    """Check the narrowly scoped server-owned permission for a non-persistent draft.

    This is intentionally separate from ``server_permission_granted``: a draft
    is not the isolated ticket transaction and never authorizes its execution.
    """
    return (isinstance(owner_id, str) and permission == "support.ticket.draft" and
            permission in SERVER_PERMISSION_GRANTS.get(owner_id, frozenset()))

ALLOWED_TRANSITIONS = {
    "CREATED": {"PENDING_CONFIRMATION", "CANCELLED", "EXPIRED"},
    "PENDING_CONFIRMATION": {"CONFIRMED", "REJECTED", "CANCELLED", "EXPIRED"},
    "CONFIRMED": {"EXECUTING", "CANCELLED", "EXPIRED"},
    "EXECUTING": {"SUCCEEDED", "FAILED", "UNKNOWN_RESULT"},
    "SUCCEEDED": set(), "FAILED": set(), "UNKNOWN_RESULT": set(),
    "REJECTED": set(), "CANCELLED": set(), "EXPIRED": set(),
}
_TARGET_RE = re.compile(r"^M42-SIM-[A-Za-z0-9-]{1,96}$")
_SENSITIVE_TEXT_RE = re.compile(r"(?i)(api[_ -]?key|authorization|cookie|password|secret|token|\b\S+@\S+\b|\b\d{10,}\b)")


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def server_permission_granted(owner_id: str, tool_id: str, environment_name: str) -> bool:
    """Check server-owned demo grants and tool/environment policy; never trust caller roles."""
    policy = WRITE_POLICIES.get(tool_id)
    granted = SERVER_PERMISSION_GRANTS.get(owner_id, frozenset())
    return bool(policy and policy.required_permissions and
                set(policy.required_permissions).issubset(granted) and
                policy.execution_mode in {"LOCAL_TEST_TRANSACTION", "ISOLATED_RETURN_TRANSACTION", "ISOLATED_REFUND_TRANSACTION", "ISOLATED_ORDER_CANCELLATION_TRANSACTION", "ISOLATED_TICKET_TRANSACTION"} and
                environment_name in policy.allowed_environment)


def _is_isolated_return_database(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql" and bind.url.database == "shoprelay_m11_test"


def _is_isolated_refund_database(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql" and bind.url.database == "shoprelay_m11_test"


def _is_isolated_cancel_database(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql" and bind.url.database == "shoprelay_m11_test"


def _is_isolated_ticket_database(db: Session) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql" and bind.url.database == "shoprelay_m11_test"


_TICKET_CATEGORIES = {"ACCOUNT_ACCESS", "ORDER_STATUS", "RETURN_REFUND", "PRODUCT_QUALITY",
                      "POLICY_QUESTION", "PRIVACY_SECURITY", "OTHER"}
_TICKET_AGENTS = {"order_logistics", "policy", "after_sales", "product"}
_TICKET_REF_RE = re.compile(r"^(?:evidence|fact|tool|policy):[A-Za-z0-9._:-]{1,128}$")
_TICKET_CLAIM_RE = re.compile(r"^[A-Z0-9_]{1,64}$")


def _ticket_text(value: str, *, limit: int, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ActionError(code, 422, "Ticket text is empty or exceeds its allowed length")
    # Avoid persisting credentials and direct contact identifiers into a local
    # escalation record. Keep the ticket useful while reducing sensitive data.
    value = re.sub(r"(?i)(api[_ -]?key|authorization|cookie|password|secret|token)\s*[:=]\s*\S+", r"\1=[REDACTED]", value)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", value)
    value = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)", "[REDACTED_PHONE]", value)
    if not value.strip():
        raise ActionError(code, 422, "Ticket text is invalid")
    return value.strip()


_RETURN_REASON_CODES = {"QUALITY_ISSUE", "WRONG_ITEM", "DAMAGED_IN_TRANSIT", "OTHER"}
_ACTIVE_RETURN_STATES = {"SUBMITTED", "UNDER_REVIEW", "APPROVED"}
_ACTIVE_REFUND_STATES = {"SUBMITTED", "UNDER_REVIEW", "APPROVED", "PROCESSING"}
_REFUND_REASON_CODES = {"PRE_SHIPMENT", "APPROVED_RETURN", "OTHER"}


def _utc_fact(value: str | None, *, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ActionError(code, 409, "Required order timestamps are not verifiable UTC facts") from exc


def _refund_policy_snapshot(retrieval_service, *, evaluation_at: datetime) -> dict:
    at = evaluation_at.isoformat().replace("+00:00", "Z")
    try:
        view = retrieval_service.pin_view(at)
        result = retrieval_service.retrieve("退款申请 已付款未发货 已批准退货 退款唯一性", mode="bm25", view=view)
    except Exception as exc:
        raise ActionError("REFUND_POLICY_UNAVAILABLE", 503,
                          "A versioned refund policy could not be verified") from exc
    knowledge_version = view.index.get("knowledge_version")
    if result.get("knowledge_version") != knowledge_version or result.get("index_fingerprint") != view.fingerprint:
        raise ActionError("REFUND_POLICY_SNAPSHOT_MISMATCH", 409, "Policy snapshot verification failed")
    required = {"before-shipping", "uniqueness"}
    selected = {}
    for evidence in result.get("evidence", []):
        if not evidence_is_valid(evidence, fingerprint=view.fingerprint,
                knowledge_version=knowledge_version, evaluation_at=view.evaluation_at):
            continue
        if evidence.get("policy_id") == "refund" and evidence.get("clause_id") in required:
            selected[evidence["clause_id"]] = {
                key: evidence.get(key) for key in (
                    "chunk_id", "policy_id", "clause_id", "version", "effective_from", "effective_to")}
    if not required.issubset(selected):
        raise ActionError("REFUND_POLICY_EVIDENCE_NOT_FOUND", 409,
                          "Required effective refund clauses were not retrieved")
    return {"knowledge_version": knowledge_version, "index_fingerprint": view.fingerprint,
        "evaluation_at": view.evaluation_at, "retrieval_mode": "BM25",
        "evidence": [selected[key] for key in sorted(required)]}


def _cancel_policy_snapshot(retrieval_service, *, evaluation_at: datetime, paid: bool) -> dict:
    """Pin cancellation and, when paid, refund rules to the order's paid time."""
    at = evaluation_at.isoformat().replace("+00:00", "Z")
    try:
        view = retrieval_service.pin_view(at)
        result = retrieval_service.retrieve("未发货订单取消 已付款取消 整单退款申请 开放退款", mode="bm25", view=view)
    except Exception as exc:
        raise ActionError("CANCEL_POLICY_UNAVAILABLE", 503,
                          "A versioned cancellation policy could not be verified") from exc
    knowledge_version = view.index.get("knowledge_version")
    if result.get("knowledge_version") != knowledge_version or result.get("index_fingerprint") != view.fingerprint:
        raise ActionError("CANCEL_POLICY_SNAPSHOT_MISMATCH", 409, "Policy snapshot verification failed")
    required = {("cancel", "eligible")}
    if paid:
        required |= {("refund", "before-shipping"), ("refund", "uniqueness")}
    selected = {}
    for evidence in result.get("evidence", []):
        if not evidence_is_valid(evidence, fingerprint=view.fingerprint,
                knowledge_version=knowledge_version, evaluation_at=view.evaluation_at):
            continue
        key = (evidence.get("policy_id"), evidence.get("clause_id"))
        if key in required:
            selected[key] = {name: evidence.get(name) for name in (
                "chunk_id", "policy_id", "clause_id", "version", "effective_from", "effective_to")}
    if not required.issubset(selected):
        raise ActionError("CANCEL_POLICY_EVIDENCE_NOT_FOUND", 409,
                          "Required effective cancellation policy evidence was not retrieved")
    ordered = sorted(required)
    return {"knowledge_version": knowledge_version, "index_fingerprint": view.fingerprint,
        "evaluation_at": view.evaluation_at, "retrieval_mode": "BM25",
        "evidence": [selected[key] for key in ordered]}


def _verified_cancellation_inventory(db: Session, order_id: str, *, lock: bool) -> tuple[list[dict], list[tuple[OrderItem, Inventory, OrderInventoryReservation]]]:
    item_query = select(OrderItem).where(OrderItem.order_id == order_id)
    if lock:
        item_query = item_query.with_for_update()
    items = db.execute(item_query
        .order_by(OrderItem.product_id)).scalars().all()
    if not items:
        raise ActionError("CANCEL_INVENTORY_FACTS_UNKNOWN", 409,
                          "Order item reservation facts are unavailable")
    rows: list[tuple[OrderItem, Inventory, OrderInventoryReservation]] = []
    snapshot = []
    for item in items:
        query = select(Inventory).where(Inventory.product_id == item.product_id)
        if lock:
            query = query.with_for_update()
        inventory = db.execute(query).scalar_one_or_none()
        reservation_query = select(OrderInventoryReservation).where(
            OrderInventoryReservation.order_id == order_id,
            OrderInventoryReservation.product_id == item.product_id,
            OrderInventoryReservation.status == "RESERVED")
        if lock:
            reservation_query = reservation_query.with_for_update()
        reservation = db.execute(reservation_query).scalar_one_or_none()
        if (inventory is None or reservation is None or not isinstance(item.quantity, int) or
                item.quantity <= 0 or reservation.quantity != item.quantity or
                inventory.reserved_qty < reservation.quantity):
            raise ActionError("CANCEL_INVENTORY_FACTS_UNKNOWN", 409,
                              "An order-scoped inventory reservation could not be verified for cancellation")
        rows.append((item, inventory, reservation))
        snapshot.append({"product_id": item.product_id, "quantity": item.quantity,
            "reservation_id": reservation.reservation_id, "reservation_quantity": reservation.quantity,
            "reservation_status": reservation.status, "reserved_qty": inventory.reserved_qty,
            "inventory_version": inventory.version})
    return snapshot, rows


def prepare_cancel_action(db: Session, *, owner_id: str, order_id: str, reason_code: str,
                          reason_summary: str, retrieval_service) -> tuple[dict, dict]:
    """Build a server-derived cancellation proposal; does not mutate order or stock."""
    if reason_code not in {"USER_REQUEST", "OTHER"}:
        raise ActionError("CANCEL_REASON_INVALID", 422, "Cancellation reason code is not allowed")
    if (not isinstance(reason_summary, str) or not reason_summary.strip() or len(reason_summary) > 300 or
            _SENSITIVE_TEXT_RE.search(reason_summary)):
        raise ActionError("CANCEL_REASON_INVALID", 422, "Cancellation reason summary is invalid")
    order = db.execute(select(Order).where(Order.order_id == order_id,
        Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    if order.status not in {"CREATED", "PAID"}:
        raise ActionError("CANCEL_NOT_ALLOWED", 409,
                          "Only verified unshipped CREATED or PAID orders can be proposed for cancellation")
    shipments = db.execute(select(Shipment.shipment_id).where(Shipment.order_id == order_id)).first()
    if shipments:
        raise ActionError("CANCEL_ORDER_FACTS_CONFLICT", 409,
                          "Shipment records conflict with an unshipped cancellation state")
    paid = order.status == "PAID"
    if paid and (not order.paid_at or not isinstance(order.paid_cents, int) or order.paid_cents <= 0):
        raise ActionError("CANCEL_ORDER_FACTS_UNKNOWN", 409,
                          "Paid cancellation requires verified payment time and amount")
    if not paid and order.paid_at is not None:
        raise ActionError("CANCEL_ORDER_FACTS_CONFLICT", 409,
                          "CREATED order conflicts with a recorded payment")
    evaluation_at = _utc_fact(order.paid_at, code="CANCEL_ORDER_FACTS_UNKNOWN") if paid else utc_now()
    inventory_snapshot, _ = _verified_cancellation_inventory(db, order_id, lock=True)
    open_refund = db.execute(select(Refund.refund_id).where(Refund.order_id == order_id,
        Refund.status.in_(_ACTIVE_REFUND_STATES))).first()
    if open_refund:
        raise ActionError("STATE_CONFLICT", 409, "An open refund application prevents order cancellation")
    open_return = db.execute(select(ReturnRequest.return_id).where(ReturnRequest.order_id == order_id,
        ReturnRequest.status.in_(_ACTIVE_RETURN_STATES))).first()
    if open_return:
        raise ActionError("STATE_CONFLICT", 409, "An open return application prevents order cancellation")
    policy_snapshot = _cancel_policy_snapshot(retrieval_service,
        evaluation_at=evaluation_at, paid=paid)
    order_snapshot = {"order_id": order.order_id, "status": order.status,
        "paid_at": order.paid_at, "paid_cents": order.paid_cents, "delivered_at": order.delivered_at,
        "version": order.version}
    args = {"order_id": order_id, "reason_code": reason_code,
        "reason_summary": reason_summary.strip(), "reason_source": "USER_CLAIM_UNVERIFIED",
        "order_snapshot": order_snapshot, "inventory_snapshot": inventory_snapshot,
        "policy_snapshot": policy_snapshot,
        "business_outcome": "CANCELLED_WITH_REFUND_APPLICATION" if paid else "CANCELLED_NO_PAYMENT"}
    return args, {"eligibility": "POLICY_PRECONDITIONS_SATISFIED_REQUIRES_CONFIRMATION",
        "refund_required": paid, "payment_effect": "NOT_EXECUTED", "order": order_snapshot,
        "inventory_items": len(inventory_snapshot), "policy": policy_snapshot}


def replay_cancel_action_proposal(db: Session, *, owner_id: str, session_id: str, run_id: str,
                                 target_resource_id: str, reason_code: str, reason_summary: str,
                                 idempotency_key: str) -> PendingAction | None:
    prior = db.execute(select(PendingAction).where(PendingAction.owner_id == owner_id,
        PendingAction.idempotency_key == idempotency_key)).scalar_one_or_none()
    if prior is None:
        return None
    stored = json.loads(prior.sanitized_arguments_json)
    if not (prior.tool_id == "cancel_order" and prior.target_resource_id == target_resource_id and
            prior.session_id == session_id and prior.run_id == run_id and
            stored.get("order_id") == target_resource_id and
            stored.get("reason_code") == reason_code and
            stored.get("reason_summary") == reason_summary.strip()):
        raise ActionError("IDEMPOTENCY_CONFLICT", 409,
                          "Action idempotency key was used with different parameters")
    return prior


def _revalidate_cancel_snapshot(db: Session, *, row: PendingAction, arguments: dict, retrieval_service) -> tuple[Order, list[tuple[OrderItem, Inventory, OrderInventoryReservation]]]:
    order = db.execute(select(Order).where(Order.order_id == row.target_resource_id,
        Order.user_id == row.owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    snapshot = arguments.get("order_snapshot") or {}
    if any(snapshot.get(key) != getattr(order, key, None) for key in
           ("order_id", "status", "paid_at", "paid_cents", "delivered_at", "version")):
        raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                          "Order facts changed after cancellation confirmation was requested")
    paid = order.status == "PAID"
    if order.status not in {"CREATED", "PAID"} or (paid and not order.paid_at) or (not paid and order.paid_at is not None):
        raise ActionError("CANCEL_NOT_ALLOWED", 409, "Order is no longer in a cancellable state")
    if db.execute(select(Shipment.shipment_id).where(Shipment.order_id == order.order_id)).first():
        raise ActionError("CANCEL_ORDER_FACTS_CONFLICT", 409,
                          "Shipment record appeared after cancellation confirmation was requested")
    expected_refund = paid
    if arguments.get("business_outcome") != ("CANCELLED_WITH_REFUND_APPLICATION" if expected_refund else "CANCELLED_NO_PAYMENT"):
        raise ActionError("ACTION_ARGUMENTS_CHANGED", 409, "Cancellation business branch changed")
    if db.execute(select(Refund.refund_id).where(Refund.order_id == order.order_id,
            Refund.status.in_(_ACTIVE_REFUND_STATES)).with_for_update()).first():
        raise ActionError("STATE_CONFLICT", 409, "An open refund application prevents order cancellation")
    if db.execute(select(ReturnRequest.return_id).where(ReturnRequest.order_id == order.order_id,
            ReturnRequest.status.in_(_ACTIVE_RETURN_STATES)).with_for_update()).first():
        raise ActionError("STATE_CONFLICT", 409, "An open return application prevents order cancellation")
    inventory_snapshot, rows = _verified_cancellation_inventory(db, order.order_id, lock=True)
    if inventory_snapshot != (arguments.get("inventory_snapshot") or []):
        raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                          "Inventory reservation facts changed after cancellation confirmation was requested")
    policy = arguments.get("policy_snapshot") or {}
    evaluation_at = _utc_fact(order.paid_at, code="CANCEL_ORDER_FACTS_UNKNOWN") if paid else _utc_fact(
        policy.get("evaluation_at"), code="CANCEL_POLICY_SNAPSHOT_CHANGED")
    current = _cancel_policy_snapshot(retrieval_service, evaluation_at=evaluation_at, paid=paid)
    if current != policy:
        raise ActionError("CANCEL_POLICY_SNAPSHOT_CHANGED", 409,
                          "Pinned cancellation policy snapshot is no longer valid")
    return order, rows


def prepare_refund_action(db: Session, *, owner_id: str, order_id: str, reason_code: str,
                          reason_summary: str, retrieval_service) -> tuple[dict, dict]:
    """Build a server-derived refund request proposal; never approves or pays it."""
    if reason_code not in _REFUND_REASON_CODES:
        raise ActionError("REFUND_REASON_INVALID", 422, "Refund reason code is not allowed")
    if (not isinstance(reason_summary, str) or not reason_summary.strip() or len(reason_summary) > 300 or
            _SENSITIVE_TEXT_RE.search(reason_summary)):
        raise ActionError("REFUND_REASON_INVALID", 422, "Refund reason summary is invalid")
    order = db.execute(select(Order).where(Order.order_id == order_id,
        Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    if not isinstance(order.paid_cents, int) or order.paid_cents <= 0 or not order.paid_at:
        raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409, "Verified paid amount and payment time are required")
    paid_at = _utc_fact(order.paid_at, code="REFUND_ORDER_FACTS_UNKNOWN")
    if order.status == "PAID":
        shipments = db.execute(select(Shipment).where(Shipment.order_id == order_id)).scalars().all()
        if shipments:
            raise ActionError("REFUND_ORDER_FACTS_CONFLICT", 409,
                              "Order status conflicts with recorded shipment facts")
        if reason_code not in {"PRE_SHIPMENT", "OTHER"}:
            raise ActionError("REFUND_REASON_INCOMPATIBLE", 422,
                              "Reason does not match a verified pre-shipment order")
        fact_snapshot = {"branch": "PAID_NOT_SHIPPED", "shipment_records": [],
                         "eligibility": "POLICY_PRECONDITIONS_SATISFIED_REVIEW_REQUIRED"}
        return_id = None
    elif order.status == "SHIPPED":
        shipments = db.execute(select(Shipment).where(Shipment.order_id == order_id)
            .order_by(Shipment.observed_at.desc(), Shipment.shipment_id.desc())).scalars().all()
        if not shipments:
            raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409,
                              "Shipment status is not backed by a shipment record")
        # The policy mandates manual handling for shipped but not delivered orders.
        raise ActionError("REFUND_REQUIRES_MANUAL_REVIEW", 409,
                          "Shipped but undelivered refund requests require manual review")
    elif order.status == "DELIVERED":
        if not order.delivered_at:
            raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409, "Verified delivery time is required")
        delivered_at = _utc_fact(order.delivered_at, code="REFUND_ORDER_FACTS_UNKNOWN")
        approved = db.execute(select(ReturnRequest).where(ReturnRequest.order_id == order_id,
            ReturnRequest.status == "APPROVED", ReturnRequest.source_type.in_(
                ("BUSINESS_SYSTEM", "SIMULATED_TEST_DATA")), ReturnRequest.approved_at.is_not(None))
            .order_by(ReturnRequest.approved_at.desc())).scalars().all()
        audited = []
        for item in approved:
            event = db.execute(select(AfterSalesEvent).where(AfterSalesEvent.return_id == item.return_id,
                AfterSalesEvent.order_id == order_id, AfterSalesEvent.event_type == "RETURN_APPROVED",
                AfterSalesEvent.source_type.in_(("BUSINESS_SYSTEM", "SIMULATED_TEST_DATA")),
                AfterSalesEvent.actor_type.in_(("HUMAN_REVIEWER", "SIMULATED_TEST_DATA")))
                .order_by(AfterSalesEvent.recorded_at.desc())).scalar_one_or_none()
            if event is not None and event.occurred_at == item.approved_at:
                audited.append((item, event))
        if not audited:
            pending = db.execute(select(ReturnRequest.return_id).where(ReturnRequest.order_id == order_id,
                ReturnRequest.status.in_(("SUBMITTED", "UNDER_REVIEW")))).first()
            if pending:
                raise ActionError("RETURN_NOT_APPROVED", 409,
                                  "A pending return review is not an approved refund prerequisite")
            raise ActionError("RETURN_APPROVAL_NOT_VERIFIED", 409,
                              "An audited approved return application is required for a delivered order")
        if reason_code not in {"APPROVED_RETURN", "OTHER"}:
            raise ActionError("REFUND_REASON_INCOMPATIBLE", 422,
                              "Reason does not match the verified approved-return prerequisite")
        return_row, approval_event = audited[0]
        fact_snapshot = {"branch": "DELIVERED_WITH_AUDITED_APPROVED_RETURN",
            "delivered_at": delivered_at.isoformat().replace("+00:00", "Z"),
            "return_id": return_row.return_id, "return_status": return_row.status,
            "return_approved_at": return_row.approved_at.isoformat().replace("+00:00", "Z"),
            "approval_event_id": approval_event.event_id, "approval_source_type": approval_event.source_type,
            "eligibility": "POLICY_PRECONDITIONS_SATISFIED_REVIEW_REQUIRED"}
        return_id = return_row.return_id
    else:
        raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409,
                          "Order state does not map to a verified refund application path")
    active = db.execute(select(Refund.refund_id).where(Refund.order_id == order_id,
        Refund.status.in_(_ACTIVE_REFUND_STATES))).first()
    if active:
        raise ActionError("REFUND_ALREADY_OPEN", 409, "An open refund application already exists")
    policy_snapshot = _refund_policy_snapshot(retrieval_service, evaluation_at=paid_at)
    order_snapshot = {"order_id": order.order_id, "status": order.status, "paid_at": order.paid_at,
        "delivered_at": order.delivered_at, "paid_cents": order.paid_cents, "version": order.version}
    arguments = {"order_id": order_id, "reason_code": reason_code,
        "reason_summary": reason_summary.strip(), "reason_source": "USER_CLAIM_UNVERIFIED",
        "amount_cents": order.paid_cents, "return_id": return_id,
        "order_snapshot": order_snapshot, "fact_snapshot": fact_snapshot,
        "policy_snapshot": policy_snapshot,
        "application_outcome": "SUBMITTED_PENDING_HUMAN_REVIEW"}
    return arguments, {"eligibility": fact_snapshot["eligibility"], "review_required": True,
        "amount_cents": order.paid_cents, "policy": policy_snapshot, "facts": fact_snapshot}


def replay_refund_action_proposal(db: Session, *, owner_id: str, session_id: str, run_id: str,
                                  target_resource_id: str, reason_code: str, reason_summary: str,
                                  idempotency_key: str) -> PendingAction | None:
    prior = db.execute(select(PendingAction).where(PendingAction.owner_id == owner_id,
        PendingAction.idempotency_key == idempotency_key)).scalar_one_or_none()
    if prior is None:
        return None
    stored = json.loads(prior.sanitized_arguments_json)
    matches = (prior.tool_id == "create_refund_request" and prior.target_resource_id == target_resource_id and
        prior.session_id == session_id and prior.run_id == run_id and stored.get("order_id") == target_resource_id and
        stored.get("reason_code") == reason_code and stored.get("reason_summary") == reason_summary.strip())
    if not matches:
        raise ActionError("IDEMPOTENCY_CONFLICT", 409,
                          "Action idempotency key was used with different parameters")
    return prior


def prepare_return_action(db: Session, *, owner_id: str, order_id: str, reason_code: str,
                          reason_summary: str, retrieval_service) -> tuple[dict, dict]:
    """Recheck owner-scoped order facts and build a server-derived pinned policy basis.

    User-entered reason text is retained as a claim, never promoted to a verified fact.
    The resulting request is always an application for human review, never an approval.
    """
    if reason_code not in _RETURN_REASON_CODES:
        raise ActionError("RETURN_REASON_INVALID", 422, "Return reason code is not allowed")
    if (not isinstance(reason_summary, str) or not reason_summary.strip() or len(reason_summary) > 300 or
            _SENSITIVE_TEXT_RE.search(reason_summary)):
        raise ActionError("RETURN_REASON_INVALID", 422, "Return reason summary is invalid")
    order = db.execute(select(Order).where(Order.order_id == order_id,
        Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    if order.status != "DELIVERED" or not order.delivered_at or not order.paid_at:
        raise ActionError("RETURN_ORDER_FACTS_INSUFFICIENT", 409,
                          "Verified paid and delivered order facts are required")
    try:
        paid_at = datetime.fromisoformat(order.paid_at.replace("Z", "+00:00"))
        delivered_at = datetime.fromisoformat(order.delivered_at.replace("Z", "+00:00"))
        if paid_at.tzinfo is None or delivered_at.tzinfo is None:
            raise ValueError("timezone required")
        paid_at = paid_at.astimezone(timezone.utc)
        delivered_at = delivered_at.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ActionError("RETURN_ORDER_FACTS_INSUFFICIENT", 409,
                          "Order timestamps are not verifiable UTC facts") from exc
    existing = db.execute(select(ReturnRequest.return_id).where(
        ReturnRequest.order_id == order_id, ReturnRequest.status.in_(_ACTIVE_RETURN_STATES))).first()
    if existing:
        raise ActionError("RETURN_ALREADY_OPEN", 409, "An active return application already exists")
    query = "退货申请 退货政策 质量问题 人工审核" if reason_code == "QUALITY_ISSUE" else "退货申请 退货政策 人工审核"
    try:
        view = retrieval_service.pin_view(paid_at.isoformat().replace("+00:00", "Z"))
        result = retrieval_service.retrieve(query, mode="bm25", view=view)
    except Exception as exc:
        raise ActionError("RETURN_POLICY_UNAVAILABLE", 503,
                          "A versioned return policy could not be verified") from exc
    if (result.get("knowledge_version") != view.index.get("knowledge_version") or
            result.get("index_fingerprint") != view.fingerprint):
        raise ActionError("RETURN_POLICY_SNAPSHOT_MISMATCH", 409, "Policy snapshot verification failed")
    selected = []
    for evidence in result.get("evidence", []):
        if not evidence_is_valid(evidence, fingerprint=view.fingerprint,
                knowledge_version=view.index.get("knowledge_version"), evaluation_at=view.evaluation_at):
            continue
        if evidence.get("policy_id") != "return-standard" or "退货" not in str(evidence.get("content", "")):
            continue
        selected.append({key: evidence.get(key) for key in
            ("chunk_id", "policy_id", "clause_id", "version")})
    if not selected:
        raise ActionError("RETURN_POLICY_EVIDENCE_NOT_FOUND", 409,
                          "No effective return policy evidence was found for the order's paid time")
    policy_snapshot = {"knowledge_version": view.index.get("knowledge_version"),
        "index_fingerprint": view.fingerprint, "evaluation_at": view.evaluation_at,
        "retrieval_mode": "BM25", "evidence": selected[:3]}
    order_snapshot = {"order_id": order.order_id, "status": order.status,
        "paid_at": order.paid_at, "delivered_at": order.delivered_at, "version": order.version}
    arguments = {"order_id": order_id, "reason_code": reason_code,
        "reason_summary": reason_summary.strip(), "reason_source": "USER_CLAIM_UNVERIFIED",
        "order_snapshot": order_snapshot, "policy_snapshot": policy_snapshot,
        "application_outcome": "PENDING_HUMAN_REVIEW"}
    return arguments, {"order": order_snapshot, "policy": policy_snapshot,
        "eligibility": "NOT_DETERMINED", "review_required": True}


def _revalidate_return_snapshot(db: Session, *, row: PendingAction, arguments: dict,
                                retrieval_service) -> Order:
    order_id = arguments.get("order_id")
    order = db.execute(select(Order).where(Order.order_id == order_id,
        Order.user_id == row.owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    snapshot = arguments.get("order_snapshot") or {}
    if any(snapshot.get(key) != getattr(order, key, None)
           for key in ("order_id", "status", "paid_at", "delivered_at", "version")):
        raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                          "Verified order facts changed after confirmation was requested")
    if order.status != "DELIVERED" or not order.paid_at or not order.delivered_at:
        raise ActionError("RETURN_ORDER_FACTS_INSUFFICIENT", 409,
                          "Verified paid and delivered order facts are required")
    existing = db.execute(select(ReturnRequest.return_id).where(
        ReturnRequest.order_id == order_id, ReturnRequest.status.in_(_ACTIVE_RETURN_STATES))).first()
    if existing:
        raise ActionError("RETURN_ALREADY_OPEN", 409, "An active return application already exists")
    policy = arguments.get("policy_snapshot") or {}
    try:
        view = retrieval_service.pin_view(policy.get("evaluation_at"))
    except Exception as exc:
        raise ActionError("RETURN_POLICY_UNAVAILABLE", 503, "Pinned return policy is unavailable") from exc
    if view.fingerprint != policy.get("index_fingerprint") or view.index.get("knowledge_version") != policy.get("knowledge_version"):
        raise ActionError("RETURN_POLICY_SNAPSHOT_CHANGED", 409, "Pinned return policy snapshot changed")
    by_id = {item.get("chunk_id"): item for item in view.index.get("chunks", [])}
    if not policy.get("evidence") or any(item.get("chunk_id") not in by_id for item in policy["evidence"]):
        raise ActionError("RETURN_POLICY_EVIDENCE_INVALID", 409, "Pinned return policy evidence is missing")
    for item in policy["evidence"]:
        evidence = by_id[item["chunk_id"]]
        evidence = {**evidence, "index_fingerprint": view.fingerprint,
            "knowledge_version": view.index.get("knowledge_version"), "policy_version": evidence.get("version")}
        if (evidence.get("policy_id") != "return-standard" or "退货" not in str(evidence.get("content", "")) or
                evidence.get("version") != item.get("version") or
                not evidence_is_valid(evidence, fingerprint=view.fingerprint,
                    knowledge_version=view.index.get("knowledge_version"), evaluation_at=view.evaluation_at)):
            raise ActionError("RETURN_POLICY_EVIDENCE_INVALID", 409, "Pinned return policy evidence is invalid")
    return order


def _revalidate_refund_snapshot(db: Session, *, row: PendingAction, arguments: dict,
                                retrieval_service) -> tuple[Order, ReturnRequest | None]:
    order_id = arguments.get("order_id")
    order = db.execute(select(Order).where(Order.order_id == order_id,
        Order.user_id == row.owner_id).with_for_update()).scalar_one_or_none()
    if order is None:
        raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
    snapshot = arguments.get("order_snapshot") or {}
    if any(snapshot.get(key) != getattr(order, key, None)
           for key in ("order_id", "status", "paid_at", "delivered_at", "paid_cents", "version")):
        raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                          "Verified order facts changed after confirmation was requested")
    if not order.paid_at or not isinstance(order.paid_cents, int) or order.paid_cents <= 0:
        raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409, "Verified payment facts are unavailable")
    paid_at = _utc_fact(order.paid_at, code="REFUND_ORDER_FACTS_UNKNOWN")
    branch = (arguments.get("fact_snapshot") or {}).get("branch")
    linked_return = None
    if branch == "PAID_NOT_SHIPPED":
        shipments = db.execute(select(Shipment.shipment_id).where(Shipment.order_id == order_id)).all()
        if order.status != "PAID" or shipments:
            raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                              "Order is no longer verified as paid and not shipped")
        if arguments.get("return_id") is not None:
            raise ActionError("ACTION_ARGUMENTS_CHANGED", 409, "Pre-shipment refund cannot link a return")
    elif branch == "DELIVERED_WITH_AUDITED_APPROVED_RETURN":
        if order.status != "DELIVERED" or not order.delivered_at:
            raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409, "Delivery facts changed")
        _utc_fact(order.delivered_at, code="REFUND_ORDER_FACTS_UNKNOWN")
        return_id = arguments.get("return_id")
        linked_return = db.execute(select(ReturnRequest).where(ReturnRequest.return_id == return_id,
            ReturnRequest.order_id == order_id, ReturnRequest.status == "APPROVED",
            ReturnRequest.source_type.in_(("BUSINESS_SYSTEM", "SIMULATED_TEST_DATA")),
            ReturnRequest.approved_at.is_not(None)).with_for_update()).scalar_one_or_none()
        if linked_return is None or linked_return.approved_at.isoformat().replace("+00:00", "Z") != \
                (arguments.get("fact_snapshot") or {}).get("return_approved_at"):
            raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                              "Approved return prerequisite changed")
        approval_event = db.execute(select(AfterSalesEvent).where(
            AfterSalesEvent.event_id == (arguments.get("fact_snapshot") or {}).get("approval_event_id"),
            AfterSalesEvent.return_id == return_id, AfterSalesEvent.order_id == order_id,
            AfterSalesEvent.event_type == "RETURN_APPROVED",
            AfterSalesEvent.source_type.in_(("BUSINESS_SYSTEM", "SIMULATED_TEST_DATA")),
            AfterSalesEvent.actor_type.in_(("HUMAN_REVIEWER", "SIMULATED_TEST_DATA")))).scalar_one_or_none()
        if approval_event is None or approval_event.occurred_at != linked_return.approved_at:
            raise ActionError("RETURN_APPROVAL_NOT_VERIFIED", 409,
                              "Return approval audit evidence is unavailable")
    else:
        raise ActionError("REFUND_ORDER_FACTS_UNKNOWN", 409, "Refund business branch is invalid")
    active = db.execute(select(Refund.refund_id).where(Refund.order_id == order_id,
        Refund.status.in_(_ACTIVE_REFUND_STATES)).with_for_update()).first()
    if active:
        raise ActionError("REFUND_ALREADY_OPEN", 409, "An open refund application already exists")

    policy = arguments.get("policy_snapshot") or {}
    try:
        view = retrieval_service.pin_view(policy.get("evaluation_at"))
    except Exception as exc:
        raise ActionError("REFUND_POLICY_UNAVAILABLE", 503, "Pinned refund policy is unavailable") from exc
    if (view.fingerprint != policy.get("index_fingerprint") or
            view.index.get("knowledge_version") != policy.get("knowledge_version")):
        raise ActionError("REFUND_POLICY_SNAPSHOT_CHANGED", 409, "Pinned refund policy snapshot changed")
    if policy.get("evaluation_at") != paid_at.isoformat().replace("+00:00", "Z"):
        raise ActionError("REFUND_POLICY_SNAPSHOT_CHANGED", 409,
                          "Refund policy is not pinned to the verified payment time")
    by_id = {item.get("chunk_id"): item for item in view.index.get("chunks", [])}
    evidence_rows = policy.get("evidence") or []
    required_clauses = {"before-shipping", "uniqueness"}
    if not evidence_rows or {item.get("clause_id") for item in evidence_rows} != required_clauses:
        raise ActionError("REFUND_POLICY_EVIDENCE_INVALID", 409,
                          "Pinned refund policy clauses are incomplete or duplicated")
    for item in evidence_rows:
        raw = by_id.get(item.get("chunk_id"))
        if raw is None:
            raise ActionError("REFUND_POLICY_EVIDENCE_INVALID", 409, "Pinned refund policy chunk is missing")
        evidence = {**raw, "index_fingerprint": view.fingerprint,
            "knowledge_version": view.index.get("knowledge_version"), "policy_version": raw.get("version")}
        if (raw.get("policy_id") != "refund" or raw.get("version") != item.get("version") or
                raw.get("clause_id") != item.get("clause_id") or
                not evidence_is_valid(evidence, fingerprint=view.fingerprint,
                    knowledge_version=view.index.get("knowledge_version"), evaluation_at=view.evaluation_at)):
            raise ActionError("REFUND_POLICY_EVIDENCE_INVALID", 409,
                              "Pinned refund policy evidence is not valid at the verified payment time")
    if arguments.get("amount_cents") != order.paid_cents:
        raise ActionError("ACTION_ARGUMENTS_CHANGED", 409, "Refund amount no longer matches verified paid amount")
    return order, linked_return


def _transition(row: PendingAction, target: str) -> tuple[str, str]:
    before = row.status
    if target not in ALLOWED_TRANSITIONS.get(before, set()):
        raise ActionError("INVALID_ACTION_TRANSITION", 409, "Action state transition is not allowed")
    row.status = target
    row.action_version += 1
    return before, target


def _audit(db: Session, row: PendingAction, event_type: str, before: str | None,
           after: str, *, transaction_reference: str | None = None,
           error_code: str | None = None, trace_id: str | None = None) -> None:
    db.add(ActionAudit(
        audit_id=(row.audit_reference if event_type == "ACTION_CREATED_PENDING_CONFIRMATION" else new_id()),
        action_id=row.action_id, owner_id=row.owner_id,
        event_type=event_type, from_status=before, to_status=after,
        occurred_at=utc_now(), trace_id=trace_id or row.trace_id,
        transaction_reference=transaction_reference,
        before_summary_json=safe_trace_json({"status": before, "action_version": max(1, row.action_version - 1)}) if before else None,
        after_summary_json=safe_trace_json({"status": after, "action_version": row.action_version,
                                            "arguments_hash": row.arguments_hash}),
        error_code=error_code,
    ))


def _action_graph_version(tool_id: str) -> str:
    return {"cancel_order": "m4.3-b3", "create_refund_request": "m4.3-b2",
            "create_return_request": "m4.3-b1", "escalate_to_human": "m4.3-b4"}.get(tool_id, "m4.2")


def _record_span(db: Session, row: PendingAction, node: str, status: str,
                 error: str | None = None, trace_id: str | None = None,
                 duration_ms: float = 0.0) -> str:
    now = utc_now()
    span_id = uuid.uuid4().hex
    started = now - timedelta(milliseconds=max(0.0, duration_ms))
    active_trace_id = trace_id or row.trace_id
    payload = {"trace_id": active_trace_id, "span_id": span_id, "parent_span_id": None,
               "run_id": row.run_id, "node_name": node, "strategy": row.tool_id,
               "start_time": started.isoformat(), "end_time": now.isoformat(), "duration_ms": round(duration_ms, 3),
               "status": status, "error_code": error,
               "graph_version": _action_graph_version(row.tool_id)}
    db.add(ObservabilityRecord(record_id=f"m42-action-span:{row.action_id}:{node}:{row.action_version}",
        run_id=row.run_id, user_id=row.owner_id, trace_id=active_trace_id, span_id=span_id,
        graph_version=_action_graph_version(row.tool_id), record_type="span", timestamp=now,
        payload_json=safe_trace_json(payload)))
    return span_id


def _record_trace(db: Session, row: PendingAction, trace_id: str, event_type: str, status: str) -> None:
    existing = db.get(Trace, trace_id)
    payload = safe_trace_json({"trace_id": trace_id, "run_id": row.run_id, "action_id": row.action_id,
        "tool_id": row.tool_id, "event_type": event_type, "status": status})
    if existing:
        if existing.user_id != row.owner_id or existing.run_id != row.run_id:
            raise ActionError("ACTION_TRACE_CONFLICT", 409, "Trace identifier belongs to another execution")
        return
    db.add(Trace(trace_id=trace_id, user_id=row.owner_id, event_json=payload,
        created_at=utc_now(), run_id=row.run_id, event_type=event_type, status=status))


def _record_execution_metrics(db: Session, row: PendingAction, span_id: str,
                              duration_ms: float, trace_id: str) -> None:
    now = utc_now()
    run_status = db.execute(select(Run.status).where(Run.run_id == row.run_id,
        Run.user_id == row.owner_id)).scalar_one_or_none()
    db.add(ObservabilityRecord(record_id=f"m42-action-metric:{row.action_id}:{row.action_version}",
        run_id=row.run_id, user_id=row.owner_id, trace_id=trace_id, span_id=span_id,
        graph_version=_action_graph_version(row.tool_id),
        record_type="metric", timestamp=now,
        payload_json=safe_trace_json({"node_execution_count": 1, "node_error_count": 0,
            "node_duration_ms": [round(duration_ms, 3)], "model_request_count": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "estimated_cost_usd": 0.0,
            "retrieval_hop_count": 0, "run_status": run_status,
            "action_status": row.status, "node_name": f"{row.tool_id}_execute",
            "graph_version": _action_graph_version(row.tool_id),
            "span_id": span_id})))


def _record_safe_log(db: Session, row: PendingAction, trace_id: str,
                     span_id: str) -> None:
    now = utc_now()
    db.add(ObservabilityRecord(record_id=f"m42-action-log:{row.action_id}:{row.action_version}",
        run_id=row.run_id, user_id=row.owner_id, trace_id=trace_id, span_id=span_id,
        graph_version=_action_graph_version(row.tool_id),
        record_type="log", timestamp=now,
        payload_json=safe_trace_json({"timestamp": now.isoformat(), "level": "INFO",
            "trace_id": trace_id, "run_id": row.run_id, "node": f"{row.tool_id}_execute",
            "event_type": "action_committed", "error_code": None,
            "sanitized_message": "Isolated confirmed action committed"})))


def _record_run_event(db: Session, row: PendingAction, event_type: str, payload: dict, key: str,
                      trace_id: str | None = None) -> None:
    existing = db.execute(select(RunEvent).where(RunEvent.run_id == row.run_id,
                                                 RunEvent.event_key == key)).scalar_one_or_none()
    serialized = safe_trace_json(payload)
    if existing:
        if existing.event_type != event_type or existing.payload_json != serialized:
            raise ActionError("ACTION_EVENT_CONFLICT", 409, "Action event key conflicts with prior event")
        return
    run = db.execute(select(Run).where(Run.run_id == row.run_id).with_for_update()).scalar_one_or_none()
    if run is None or run.user_id != row.owner_id or run.session_id != row.session_id:
        raise ActionError("ACTION_CONTEXT_MISMATCH", 409, "Action run context is no longer valid")
    seq = db.execute(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == row.run_id)).scalar_one() + 1
    db.add(RunEvent(event_id=new_id(), run_id=row.run_id, session_id=row.session_id,
                    trace_id=trace_id or row.trace_id, seq=seq, timestamp=utc_now(), event_type=event_type,
                    payload_json=serialized, event_key=key))


def create_pending_action(db: Session, *, owner_id: str, session_id: str, run_id: str,
                          trace_id: str, tool_id: str, target_resource_id: str,
                          arguments: dict, idempotency_key: str, environment_name: str,
                          ttl_seconds: int = 600, return_actions_enabled: bool = False,
                          refund_actions_enabled: bool = False,
                          cancel_actions_enabled: bool = False,
                          ticket_actions_enabled: bool = False) -> tuple[PendingAction, bool]:
    policy = WRITE_POLICIES.get(tool_id)
    allowed_simulated = (tool_id == "simulated_test_action" and policy is not None and
                         policy.risk_level == "WRITE_LOW_RISK" and
                         policy.execution_mode == "LOCAL_TEST_TRANSACTION")
    allowed_return = (tool_id == "create_return_request" and policy is not None and
                      policy.risk_level == "WRITE_HIGH_RISK" and
                      policy.execution_mode == "ISOLATED_RETURN_TRANSACTION" and
                      return_actions_enabled and environment_name == "shoprelay_m11_test" and
                      _is_isolated_return_database(db))
    allowed_refund = (tool_id == "create_refund_request" and policy is not None and
                      policy.risk_level == "WRITE_HIGH_RISK" and
                      policy.execution_mode == "ISOLATED_REFUND_TRANSACTION" and
                      refund_actions_enabled and environment_name == "shoprelay_m11_test" and
                      _is_isolated_refund_database(db))
    allowed_cancel = (tool_id == "cancel_order" and policy is not None and
                      policy.risk_level == "WRITE_HIGH_RISK" and
                      policy.execution_mode == "ISOLATED_ORDER_CANCELLATION_TRANSACTION" and
                      cancel_actions_enabled and environment_name == "shoprelay_m11_test" and
                      _is_isolated_cancel_database(db))
    allowed_ticket = (tool_id == "escalate_to_human" and policy is not None and
                      policy.risk_level == "WRITE_LOW_RISK" and
                      policy.execution_mode == "ISOLATED_TICKET_TRANSACTION" and
                      policy.confirmation_required and ticket_actions_enabled and
                      environment_name == "shoprelay_m11_test" and
                      _is_isolated_ticket_database(db))
    if policy is None or not (allowed_simulated or allowed_return or allowed_refund or allowed_cancel or allowed_ticket):
        raise ActionError("TOOL_NOT_AUTHORIZED", 403, "This action tool is not authorized")
    if not policy.confirmation_required or not policy.audit_required:
        raise ActionError("ACTION_POLICY_INVALID", 403, "Action policy does not require safeguards")
    if not server_permission_granted(owner_id, tool_id, environment_name):
        raise ActionError("TOOL_NOT_AUTHORIZED", 403, "Action is not granted in this environment")
    if allowed_ticket:
        expected = {"issue_category", "order_id", "user_description", "issue_summary", "reason_code",
                    "related_agents", "verified_fact_refs", "unverified_claim_codes", "evidence_references"}
        if (not isinstance(arguments, dict) or set(arguments) != expected or
            arguments.get("issue_category") not in _TICKET_CATEGORIES or
            not isinstance(arguments.get("reason_code"), str) or
            not _TICKET_CLAIM_RE.fullmatch(arguments.get("reason_code", ""))):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422, "Ticket proposal does not match the registered schema")
        if (arguments.get("order_id") != (None if target_resource_id == "SUPPORT-GENERAL" else target_resource_id) or
            not isinstance(arguments.get("related_agents"), list) or len(arguments["related_agents"]) > 4 or
            any(item not in _TICKET_AGENTS for item in arguments["related_agents"]) or
            not isinstance(arguments.get("verified_fact_refs"), list) or len(arguments["verified_fact_refs"]) > 12 or
            any(not isinstance(item, str) or not _TICKET_REF_RE.fullmatch(item) for item in arguments["verified_fact_refs"]) or
            not isinstance(arguments.get("unverified_claim_codes"), list) or len(arguments["unverified_claim_codes"]) > 12 or
            any(not isinstance(item, str) or not _TICKET_CLAIM_RE.fullmatch(item) for item in arguments["unverified_claim_codes"]) or
            not isinstance(arguments.get("evidence_references"), list) or len(arguments["evidence_references"]) > 12 or
            any(not isinstance(item, str) or not _TICKET_REF_RE.fullmatch(item) for item in arguments["evidence_references"])):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422, "Ticket proposal contains invalid references or fields")
        safe_description = _ticket_text(arguments["user_description"], limit=1000, code="INVALID_ACTION_ARGUMENTS")
        safe_summary = _ticket_text(arguments["issue_summary"], limit=600, code="INVALID_ACTION_ARGUMENTS")
        order_id = arguments["order_id"]
        if order_id is not None:
            order = db.execute(select(Order).where(Order.order_id == order_id,
                Order.user_id == owner_id)).scalar_one_or_none()
            if order is None:
                raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
        if arguments.get("issue_category") == "PRIVACY_SECURITY":
            priority = "HIGH"
        else:
            priority = "NORMAL"
        safe_args = {"issue_category": arguments["issue_category"], "order_id": order_id,
            "user_description": safe_description, "issue_summary": safe_summary,
            "reason_code": arguments["reason_code"],
            "related_agents": list(dict.fromkeys(arguments["related_agents"])),
            "verified_fact_refs": list(dict.fromkeys(arguments["verified_fact_refs"])),
            "unverified_claim_codes": list(dict.fromkeys(arguments["unverified_claim_codes"])),
            "evidence_references": list(dict.fromkeys(arguments["evidence_references"])),
            "priority": priority}
    elif allowed_simulated:
        if not _TARGET_RE.fullmatch(target_resource_id):
            raise ActionError("INVALID_TARGET", 422, "Target must be an isolated M4.2 test resource")
        if (not isinstance(arguments, dict) or set(arguments) != {"label", "value"} or
            not isinstance(arguments.get("label"), str) or not 1 <= len(arguments["label"]) <= 80 or
            not isinstance(arguments.get("value"), str) or not 1 <= len(arguments["value"]) <= 200):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422, "Action arguments do not match the registered schema")
        if any(_SENSITIVE_TEXT_RE.search(value) for value in (arguments["label"], arguments["value"])):
            raise ActionError("SENSITIVE_ACTION_ARGUMENT_REJECTED", 422, "Sensitive values are not accepted by the test action")
        safe_args = {"label": arguments["label"], "value": arguments["value"]}
    elif allowed_return:
        expected = {"order_id", "reason_code", "reason_summary", "reason_source",
                    "order_snapshot", "policy_snapshot", "application_outcome"}
        if (not isinstance(arguments, dict) or set(arguments) != expected or
            arguments.get("order_id") != target_resource_id or
            arguments.get("reason_code") not in _RETURN_REASON_CODES or
            arguments.get("reason_source") != "USER_CLAIM_UNVERIFIED" or
            arguments.get("application_outcome") != "PENDING_HUMAN_REVIEW" or
            not isinstance(arguments.get("reason_summary"), str) or
            not 1 <= len(arguments["reason_summary"]) <= 300 or
            not isinstance(arguments.get("order_snapshot"), dict) or
            not isinstance(arguments.get("policy_snapshot"), dict)):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422, "Return action does not match the server proposal schema")
        order = db.execute(select(Order).where(Order.order_id == target_resource_id,
            Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
        if order is None:
            raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
        if order.status != "DELIVERED" or not order.paid_at or not order.delivered_at:
            raise ActionError("RETURN_ORDER_FACTS_INSUFFICIENT", 409, "Verified paid and delivered order facts are required")
        safe_args = arguments
    elif allowed_refund:
        expected = {"order_id", "reason_code", "reason_summary", "reason_source", "amount_cents",
                    "return_id", "order_snapshot", "fact_snapshot", "policy_snapshot", "application_outcome"}
        if (not isinstance(arguments, dict) or set(arguments) != expected or
            arguments.get("order_id") != target_resource_id or
            arguments.get("reason_code") not in _REFUND_REASON_CODES or
            arguments.get("reason_source") != "USER_CLAIM_UNVERIFIED" or
            arguments.get("application_outcome") != "SUBMITTED_PENDING_HUMAN_REVIEW" or
            not isinstance(arguments.get("amount_cents"), int) or arguments["amount_cents"] <= 0 or
            not isinstance(arguments.get("reason_summary"), str) or
            not 1 <= len(arguments["reason_summary"]) <= 300 or
            not isinstance(arguments.get("order_snapshot"), dict) or
            not isinstance(arguments.get("fact_snapshot"), dict) or
            not isinstance(arguments.get("policy_snapshot"), dict)):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422,
                              "Refund action does not match the server proposal schema")
        order = db.execute(select(Order).where(Order.order_id == target_resource_id,
            Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
        if order is None:
            raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
        if arguments["amount_cents"] != order.paid_cents:
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422,
                              "Refund amount must be derived from the verified order")
        safe_args = arguments
    else:
        expected = {"order_id", "reason_code", "reason_summary", "reason_source", "order_snapshot",
                    "inventory_snapshot", "policy_snapshot", "business_outcome"}
        if (not isinstance(arguments, dict) or set(arguments) != expected or
                arguments.get("order_id") != target_resource_id or
                arguments.get("reason_code") not in {"USER_REQUEST", "OTHER"} or
                arguments.get("reason_source") != "USER_CLAIM_UNVERIFIED" or
                arguments.get("business_outcome") not in {"CANCELLED_NO_PAYMENT", "CANCELLED_WITH_REFUND_APPLICATION"} or
                not isinstance(arguments.get("reason_summary"), str) or
                not 1 <= len(arguments["reason_summary"]) <= 300 or
                not isinstance(arguments.get("order_snapshot"), dict) or
                not isinstance(arguments.get("inventory_snapshot"), list) or not arguments["inventory_snapshot"] or
                not isinstance(arguments.get("policy_snapshot"), dict)):
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422,
                              "Cancellation action does not match the server proposal schema")
        order = db.execute(select(Order).where(Order.order_id == target_resource_id,
            Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
        if order is None:
            raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
        if any(arguments["order_snapshot"].get(key) != getattr(order, key, None) for key in
               ("order_id", "status", "paid_at", "paid_cents", "delivered_at", "version")):
            raise ActionError("ACTION_BUSINESS_FACTS_CHANGED", 409,
                              "Order facts changed while the cancellation proposal was being created")
        safe_args = arguments
    if not 1 <= len(idempotency_key) <= 128 or not 60 <= ttl_seconds <= 1800:
        raise ActionError("INVALID_ACTION_REQUEST", 422, "Idempotency key or expiry is outside policy limits")
    session = db.execute(select(ConversationSession).where(
        ConversationSession.session_id == session_id,
        ConversationSession.user_id == owner_id)).scalar_one_or_none()
    run = db.execute(select(Run).where(Run.run_id == run_id, Run.user_id == owner_id,
                                      Run.session_id == session_id)).scalar_one_or_none()
    if session is None or run is None:
        raise ActionError("NOT_FOUND", 404, "Action context not found")
    if allowed_ticket and (run.session_id != session_id or run.user_id != owner_id):
        raise ActionError("ACTION_CONTEXT_MISMATCH", 409, "Ticket source context is not owned by the authenticated user")
    if allowed_ticket:
        recorded_strings: set[str] = set()
        if run.result_json:
            try:
                def collect(value):
                    if isinstance(value, dict):
                        for child in value.values():
                            collect(child)
                    elif isinstance(value, list):
                        for child in value:
                            collect(child)
                    elif isinstance(value, str):
                        recorded_strings.add(value)
                collect(json.loads(run.result_json))
            except (TypeError, ValueError):
                raise ActionError("ACTION_CONTEXT_INVALID", 409, "Ticket source Run has invalid recorded output")
        for reference in safe_args["verified_fact_refs"] + safe_args["evidence_references"]:
            ref_value = reference.split(":", 1)[1]
            if reference not in recorded_strings and ref_value not in recorded_strings:
                raise ActionError("TICKET_REFERENCE_UNVERIFIED", 409,
                                  "Ticket references must exist in the owner's recorded Run output")
    if run.status not in ({"CREATED", "RUNNING"} if allowed_simulated else
                          {"CREATED", "RUNNING", "SUCCEEDED"}):
        raise ActionError("RUN_NOT_ACTIVE", 409, "Actions can only be proposed by an active Run")
    args_json = _canonical(safe_args)
    args_hash = _hash(_canonical({"tool_id": tool_id, "target_resource_id": target_resource_id,
                                  "schema_version": policy.schema_version, "arguments": safe_args}))
    prior = db.execute(select(PendingAction).where(PendingAction.owner_id == owner_id,
        PendingAction.idempotency_key == idempotency_key)).scalar_one_or_none()
    if prior:
        if (prior.tool_id != tool_id or prior.target_resource_id != target_resource_id or
            prior.arguments_hash != args_hash or prior.session_id != session_id or prior.run_id != run_id):
            raise ActionError("IDEMPOTENCY_CONFLICT", 409, "Action idempotency key was used with different parameters")
        return prior, True
    now = utc_now()
    row = PendingAction(action_id=new_id(), owner_id=owner_id, session_id=session_id, run_id=run_id,
        trace_id=trace_id, tool_id=tool_id, target_resource_id=target_resource_id,
        sanitized_arguments_json=args_json, arguments_hash=args_hash, action_version=1,
        risk_level=policy.risk_level, status="PENDING_CONFIRMATION", idempotency_key=idempotency_key,
        created_at=now, expires_at=now + timedelta(seconds=ttl_seconds), audit_reference=new_id())
    db.add(row)
    db.flush()
    _audit(db, row, "ACTION_CREATED_PENDING_CONFIRMATION", "CREATED", "PENDING_CONFIRMATION")
    _record_trace(db, row, trace_id, "action_pending", "PENDING_CONFIRMATION")
    _record_span(db, row, "pending_action_create", "SUCCEEDED")
    _record_run_event(db, row, "action_pending", {"action_id": row.action_id,
        "tool_id": row.tool_id, "status": row.status, "arguments_hash": row.arguments_hash},
        f"action:{row.action_id}:pending")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.execute(select(PendingAction).where(PendingAction.owner_id == owner_id,
            PendingAction.idempotency_key == idempotency_key)).scalar_one_or_none()
        if (prior and prior.arguments_hash == args_hash and prior.tool_id == tool_id and
            prior.target_resource_id == target_resource_id and prior.session_id == session_id and prior.run_id == run_id):
            return prior, True
        raise ActionError("IDEMPOTENCY_CONFLICT", 409, "Action idempotency key was concurrently reused")
    db.refresh(row)
    return row, False


def replay_return_action_proposal(db: Session, *, owner_id: str, session_id: str, run_id: str,
                                 target_resource_id: str, reason_code: str, reason_summary: str,
                                 idempotency_key: str) -> PendingAction | None:
    """Resolve a retry before fresh eligibility checks, including after commit.

    The client can only match the two original proposal inputs; every other
    persisted field was derived by the server and remains covered by the stored
    canonical argument hash.
    """
    prior = db.execute(select(PendingAction).where(PendingAction.owner_id == owner_id,
        PendingAction.idempotency_key == idempotency_key)).scalar_one_or_none()
    if prior is None:
        return None
    stored = json.loads(prior.sanitized_arguments_json)
    matches = (prior.tool_id == "create_return_request" and
        prior.target_resource_id == target_resource_id and prior.session_id == session_id and
        prior.run_id == run_id and stored.get("order_id") == target_resource_id and
        stored.get("reason_code") == reason_code and stored.get("reason_summary") == reason_summary.strip())
    if not matches:
        raise ActionError("IDEMPOTENCY_CONFLICT", 409,
            "Action idempotency key was used with different parameters")
    return prior


def confirm_pending_action(db: Session, *, owner_id: str, action_id: str,
                          simulated_enabled: bool, environment_name: str,
                          confirmed_arguments_hash: str, request_trace_id: str | None = None,
                          return_actions_enabled: bool = False, refund_actions_enabled: bool = False,
                          cancel_actions_enabled: bool = False,
                          ticket_actions_enabled: bool = False,
                          retrieval_service=None) -> tuple[PendingAction, bool]:
    row = db.execute(select(PendingAction).where(PendingAction.action_id == action_id,
        PendingAction.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ActionError("NOT_FOUND", 404, "Action not found")
    if row.status == "SUCCEEDED":
        return row, True
    active_trace_id = request_trace_id or row.trace_id
    policy = WRITE_POLICIES.get(row.tool_id)
    simulated_mode = (row.tool_id == "simulated_test_action" and simulated_enabled and policy is not None and
        policy.risk_level == "WRITE_LOW_RISK" and policy.execution_mode == "LOCAL_TEST_TRANSACTION")
    return_mode = (row.tool_id == "create_return_request" and return_actions_enabled and policy is not None and
        policy.risk_level == "WRITE_HIGH_RISK" and policy.execution_mode == "ISOLATED_RETURN_TRANSACTION" and
        environment_name == "shoprelay_m11_test" and retrieval_service is not None and
        _is_isolated_return_database(db))
    refund_mode = (row.tool_id == "create_refund_request" and refund_actions_enabled and policy is not None and
        policy.risk_level == "WRITE_HIGH_RISK" and policy.execution_mode == "ISOLATED_REFUND_TRANSACTION" and
        environment_name == "shoprelay_m11_test" and retrieval_service is not None and
        _is_isolated_refund_database(db))
    cancel_mode = (row.tool_id == "cancel_order" and cancel_actions_enabled and policy is not None and
        policy.risk_level == "WRITE_HIGH_RISK" and policy.execution_mode == "ISOLATED_ORDER_CANCELLATION_TRANSACTION" and
        environment_name == "shoprelay_m11_test" and retrieval_service is not None and
        _is_isolated_cancel_database(db))
    ticket_mode = (row.tool_id == "escalate_to_human" and ticket_actions_enabled and policy is not None and
        policy.risk_level == "WRITE_LOW_RISK" and policy.execution_mode == "ISOLATED_TICKET_TRANSACTION" and
        environment_name == "shoprelay_m11_test" and _is_isolated_ticket_database(db))
    if (not (simulated_mode or return_mode or refund_mode or cancel_mode or ticket_mode) or policy is None or
        environment_name not in policy.allowed_environment or
        not server_permission_granted(owner_id, row.tool_id, environment_name)):
        raise ActionError("TOOL_NOT_AUTHORIZED", 403, "Action execution is disabled in this environment")
    canonical = _canonical({"tool_id": row.tool_id, "target_resource_id": row.target_resource_id,
        "schema_version": policy.schema_version,
        "arguments": json.loads(row.sanitized_arguments_json)})
    if (not row.arguments_hash or _hash(canonical) != row.arguments_hash or
        confirmed_arguments_hash != row.arguments_hash):
        raise ActionError("ACTION_ARGUMENTS_CHANGED", 409, "Action parameters failed integrity verification")
    now = utc_now()
    if row.status == "PENDING_CONFIRMATION" and row.expires_at <= now:
        before, after = _transition(row, "EXPIRED")
        row.finished_at = now
        _audit(db, row, "ACTION_EXPIRED", before, after, trace_id=active_trace_id)
        _record_span(db, row, "action_expire", "FAILED", "ACTION_EXPIRED", active_trace_id)
        _record_run_event(db, row, "tool_finished", {"action_id": row.action_id,
            "tool_id": row.tool_id, "status": "EXPIRED", "error_code": "ACTION_EXPIRED"},
            f"action:{row.action_id}:expired", trace_id=active_trace_id)
        db.commit()
        raise ActionError("ACTION_EXPIRED", 410, "Action has expired")
    if row.status != "PENDING_CONFIRMATION":
        raise ActionError("ACTION_NOT_CONFIRMABLE", 409, "Action is not awaiting confirmation")
    # Compare-and-swap is the execution claim. The row lock is effective on PG;
    # this predicate also protects against concurrent clients and SQLite tests.
    claimed = db.execute(update(PendingAction).where(PendingAction.action_id == action_id,
        PendingAction.owner_id == owner_id, PendingAction.status == "PENDING_CONFIRMATION",
        PendingAction.expires_at > now, PendingAction.arguments_hash == _hash(canonical)).values(
            status="CONFIRMED", action_version=PendingAction.action_version + 1,
            confirmed_at=now)).rowcount
    if claimed != 1:
        db.rollback()
        refreshed = db.execute(select(PendingAction).where(PendingAction.action_id == action_id,
            PendingAction.owner_id == owner_id)).scalar_one()
        if refreshed.status == "SUCCEEDED":
            return refreshed, True
        raise ActionError("ACTION_EXECUTION_CLAIMED", 409, "Action is already being handled")
    # Refresh only fields updated by the CAS (the identity map may be stale).
    db.expire(row)
    row = db.execute(select(PendingAction).where(PendingAction.action_id == action_id)).scalar_one()
    _audit(db, row, "USER_CONFIRMED", "PENDING_CONFIRMATION", "CONFIRMED", trace_id=active_trace_id)
    executing = db.execute(update(PendingAction).where(PendingAction.action_id == action_id,
        PendingAction.owner_id == owner_id, PendingAction.status == "CONFIRMED").values(
            status="EXECUTING", action_version=PendingAction.action_version + 1,
            execution_started_at=now)).rowcount
    if executing != 1:
        raise ActionError("ACTION_EXECUTION_CLAIMED", 409, "Action is already being handled")
    db.expire(row)
    row = db.execute(select(PendingAction).where(PendingAction.action_id == action_id)).scalar_one()
    started_event = ("SIMULATED_EXECUTION_STARTED" if simulated_mode or ticket_mode else
                     "RETURN_EXECUTION_STARTED" if return_mode else
                     "REFUND_EXECUTION_STARTED" if refund_mode else "ORDER_CANCEL_EXECUTION_STARTED")
    _audit(db, row, started_event, "CONFIRMED", "EXECUTING", trace_id=active_trace_id)
    _record_run_event(db, row, "tool_started", {"action_id": row.action_id,
        "tool_id": row.tool_id, "status": "EXECUTING"}, f"action:{row.action_id}:started", trace_id=active_trace_id)
    action_started = time.perf_counter()
    args = json.loads(row.sanitized_arguments_json)
    transaction_reference = new_id()
    if simulated_mode:
        receipt_id = new_id()
        db.add(SimulatedTestAction(receipt_id=receipt_id, action_id=row.action_id,
            target_resource_id=row.target_resource_id, label=args["label"], value=args["value"],
            source_type="SIMULATED_TEST_DATA", created_at=now))
        result_payload = {"receipt_id": receipt_id, "source_type": "SIMULATED_TEST_DATA"}
        event_type = "SIMULATED_TRANSACTION_COMMITTED"
        span_node = "simulated_action_execute"
        business_payload = {"action_id": row.action_id, "tool_id": row.tool_id,
                            "status": "SUCCEEDED", "transaction_reference": transaction_reference}
    elif ticket_mode:
        # Recheck every referenced object in the trusted server context at
        # confirmation time. Agent/user payloads never supply owner_id.
        if set(args) != {"issue_category", "order_id", "user_description", "issue_summary", "reason_code",
                        "related_agents", "verified_fact_refs", "unverified_claim_codes", "evidence_references", "priority"}:
            raise ActionError("INVALID_ACTION_ARGUMENTS", 422, "Stored ticket proposal schema is invalid")
        order_id = args["order_id"]
        if row.target_resource_id != (order_id or "SUPPORT-GENERAL"):
            raise ActionError("ACTION_ARGUMENTS_CHANGED", 409, "Ticket target changed")
        if order_id is not None:
            order = db.execute(select(Order).where(Order.order_id == order_id,
                Order.user_id == owner_id).with_for_update()).scalar_one_or_none()
            if order is None:
                raise ActionError("ORDER_NOT_FOUND", 404, "Order not found")
        dedupe_material = "|".join((owner_id, order_id or "", args["issue_category"], args["reason_code"],
            re.sub(r"\s+", " ", args["issue_summary"]).strip().casefold()))
        dedupe_key = _hash(dedupe_material)
        open_ticket = db.execute(select(SupportTicket).where(
            SupportTicket.owner_id == owner_id, SupportTicket.dedupe_key == dedupe_key,
            SupportTicket.status.in_(("SUBMITTED_PENDING_ACCEPTANCE", "ASSIGNED", "IN_PROGRESS")))
            .with_for_update()).scalar_one_or_none()
        if open_ticket is None:
            ticket_id = new_id()
            submitted_at = utc_now()
            db.add(SupportTicket(ticket_id=ticket_id, owner_id=owner_id, order_id=order_id,
                session_id=row.session_id, source_run_id=row.run_id,
                issue_category=args["issue_category"], priority=args["priority"],
                user_description=args["user_description"], issue_summary=args["issue_summary"],
                related_agents_json=safe_trace_json(args["related_agents"]),
                verified_fact_refs_json=safe_trace_json(args["verified_fact_refs"]),
                unverified_claim_codes_json=safe_trace_json(args["unverified_claim_codes"]),
                evidence_references_json=safe_trace_json(args["evidence_references"]),
                reason=args["reason_code"], status="SUBMITTED_PENDING_ACCEPTANCE",
                action_id=row.action_id, audit_reference=row.audit_reference,
                dedupe_key=dedupe_key, source_type="SIMULATED_TEST_DATA",
                source_ref=f"action:{row.action_id}:ticket", created_at=submitted_at, updated_at=submitted_at))
            db.flush()
            db.add(SupportTicketEvent(event_id=new_id(), ticket_id=ticket_id, owner_id=owner_id,
                event_type="TICKET_SUBMITTED", from_status=None, to_status="SUBMITTED_PENDING_ACCEPTANCE",
                actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
                source_ref=f"action:{row.action_id}:ticket-submitted", trace_id=active_trace_id,
                run_id=row.run_id, audit_reference=row.audit_reference, occurred_at=submitted_at,
                details_json=safe_trace_json({"issue_category": args["issue_category"],
                    "order_ref": order_id, "related_agents": args["related_agents"],
                    "verified_fact_refs": args["verified_fact_refs"],
                    "unverified_claim_codes": args["unverified_claim_codes"],
                    "evidence_references": args["evidence_references"],
                    "external_notification": "NOT_SENT", "external_assignment": "NOT_AVAILABLE"})))
            effect = "TICKET_SUBMITTED_PENDING_ACCEPTANCE"
            result_payload = {"ticket_id": ticket_id, "status": "SUBMITTED_PENDING_ACCEPTANCE",
                "source_type": "SIMULATED_TEST_DATA", "external_assignment": "NOT_AVAILABLE",
                "notification_sent": False, "handled": False}
        else:
            ticket_id = open_ticket.ticket_id
            effect = "EXISTING_OPEN_TICKET_REUSED"
            result_payload = {"ticket_id": ticket_id, "status": open_ticket.status,
                "source_type": open_ticket.source_type, "external_assignment": "NOT_AVAILABLE",
                "notification_sent": False, "handled": False, "reused_existing": True}
        event_type = "SIMULATED_TRANSACTION_COMMITTED"
        span_node = "escalate_to_human_execute"
        business_payload = {"action_id": row.action_id, "tool_id": row.tool_id,
            "status": "ACTION_SUCCEEDED", "business_status": result_payload["status"],
            "ticket_id": ticket_id, "business_effect": effect, "transaction_reference": transaction_reference}
    elif return_mode:
        _revalidate_return_snapshot(db, row=row, arguments=args, retrieval_service=retrieval_service)
        # The lock and unique active-order index prevent a second Action ID from
        # producing another live return application for this order.
        submitted_at = utc_now()
        return_id = new_id()
        db.add(ReturnRequest(return_id=return_id, order_id=row.target_resource_id,
            status="SUBMITTED", reason=f'{args["reason_code"]}: {args["reason_summary"]}',
            created_at=submitted_at.isoformat().replace("+00:00", "Z"), submitted_at=submitted_at,
            evidence_ref=",".join(item["chunk_id"] for item in args["policy_snapshot"]["evidence"])[:256],
            source_type="SIMULATED_TEST_DATA", source_ref=row.action_id))
        db.flush()
        db.add(AfterSalesEvent(event_id=new_id(), order_id=row.target_resource_id,
            return_id=return_id, refund_id=None, event_type="RETURN_SUBMITTED", occurred_at=submitted_at,
            recorded_at=submitted_at, actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
            source_ref=row.action_id, details_json=safe_trace_json({"reason_source": "USER_CLAIM_UNVERIFIED",
                "review_required": True,
                "knowledge_version": args["policy_snapshot"]["knowledge_version"],
                "policy_versions": sorted({item["version"] for item in args["policy_snapshot"]["evidence"]})})))
        result_payload = {"return_id": return_id, "status": "SUBMITTED", "source_type": "SIMULATED_TEST_DATA",
            "approval_status": "NOT_APPROVED", "eligibility": "NOT_DETERMINED", "review_required": True,
            "knowledge_version": args["policy_snapshot"]["knowledge_version"],
            "index_fingerprint": args["policy_snapshot"]["index_fingerprint"],
            "policy_evidence": args["policy_snapshot"]["evidence"],
            "policy_evaluation_at": args["policy_snapshot"]["evaluation_at"]}
        event_type = "RETURN_REQUEST_TRANSACTION_COMMITTED"
        span_node = "create_return_request_execute"
        business_payload = {"action_id": row.action_id, "tool_id": row.tool_id,
                            "status": "SUCCEEDED", "business_status": "SUBMITTED",
                            "transaction_reference": transaction_reference}
    elif refund_mode:
        order, linked_return = _revalidate_refund_snapshot(
            db, row=row, arguments=args, retrieval_service=retrieval_service)
        submitted_at = utc_now()
        refund_id = new_id()
        db.add(Refund(refund_id=refund_id, order_id=order.order_id,
            return_id=linked_return.return_id if linked_return else None,
            status="SUBMITTED", amount_cents=order.paid_cents,
            reason=f'{args["reason_code"]}: {args["reason_summary"]}',
            created_at=submitted_at.isoformat().replace("+00:00", "Z"), submitted_at=submitted_at,
            source_type="SIMULATED_TEST_DATA", source_ref=row.action_id))
        db.flush()
        db.add(AfterSalesEvent(event_id=new_id(), order_id=order.order_id,
            return_id=None, refund_id=refund_id, event_type="REFUND_SUBMITTED",
            occurred_at=submitted_at, recorded_at=submitted_at,
            actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
            source_ref=row.action_id, details_json=safe_trace_json({
                "reason_source": "USER_CLAIM_UNVERIFIED", "review_required": True,
                "eligibility": args["fact_snapshot"]["eligibility"],
                "fact_snapshot": args["fact_snapshot"],
                "policy_knowledge_version": args["policy_snapshot"]["knowledge_version"],
                "policy_index_fingerprint": args["policy_snapshot"]["index_fingerprint"],
                "policy_evaluation_at": args["policy_snapshot"]["evaluation_at"],
                "policy_chunk_versions": [{"chunk_id": item["chunk_id"],
                    "policy_id": item["policy_id"], "clause_id": item["clause_id"],
                    "policy_version": item["version"]} for item in args["policy_snapshot"]["evidence"]],
                "amount_cents": order.paid_cents, "payment_status": "NOT_EXECUTED"})))
        result_payload = {"refund_id": refund_id, "status": "SUBMITTED",
            "source_type": "SIMULATED_TEST_DATA", "business_status": "SUBMITTED",
            "approval_status": "PENDING_REVIEW", "payment_status": "NOT_EXECUTED",
            "amount_cents": order.paid_cents, "return_id": linked_return.return_id if linked_return else None,
            "review_required": True, "eligibility": args["fact_snapshot"]["eligibility"],
            "policy_knowledge_version": args["policy_snapshot"]["knowledge_version"],
            "policy_evidence": args["policy_snapshot"]["evidence"],
            "policy_evaluation_at": args["policy_snapshot"]["evaluation_at"]}
        event_type = "REFUND_REQUEST_TRANSACTION_COMMITTED"
        span_node = "create_refund_request_execute"
        business_payload = {"action_id": row.action_id, "tool_id": row.tool_id,
            "status": "ACTION_SUCCEEDED", "business_status": "SUBMITTED_PENDING_REVIEW",
            "payment_status": "NOT_EXECUTED", "transaction_reference": transaction_reference}
    else:
        order, inventory_rows = _revalidate_cancel_snapshot(db, row=row, arguments=args,
                                                              retrieval_service=retrieval_service)
        old_status, old_version = order.status, order.version
        cancelled_at = utc_now()
        changed = db.execute(update(Order).where(Order.order_id == order.order_id,
            Order.user_id == owner_id, Order.status == old_status, Order.version == old_version).values(
                status="CANCELLED", version=Order.version + 1)).rowcount
        if changed != 1:
            raise ActionError("ORDER_STATE_CONFLICT", 409,
                              "Order status changed concurrently; cancellation was not committed")
        released = []
        for item, inventory, reservation in inventory_rows:
            released_at = utc_now()
            reservation_updated = db.execute(update(OrderInventoryReservation).where(
                OrderInventoryReservation.reservation_id == reservation.reservation_id,
                OrderInventoryReservation.status == "RESERVED",
                OrderInventoryReservation.quantity == item.quantity).values(
                    status="RELEASED", released_at=released_at)).rowcount
            if reservation_updated != 1:
                raise ActionError("CANCEL_INVENTORY_CONFLICT", 409,
                                  "Order reservation changed concurrently; cancellation rolled back")
            updated = db.execute(update(Inventory).where(Inventory.product_id == item.product_id,
                Inventory.version == inventory.version, Inventory.reserved_qty >= item.quantity).values(
                    reserved_qty=Inventory.reserved_qty - item.quantity,
                    version=Inventory.version + 1)).rowcount
            if updated != 1:
                raise ActionError("CANCEL_INVENTORY_CONFLICT", 409,
                                  "Reserved inventory changed concurrently; cancellation rolled back")
            released.append({"product_id": item.product_id, "quantity": item.quantity})
        refund_id = None
        if old_status == "PAID":
            refund_id = new_id()
            db.add(Refund(refund_id=refund_id, order_id=order.order_id, return_id=None,
                status="SUBMITTED", amount_cents=order.paid_cents,
                reason=f'{args["reason_code"]}: {args["reason_summary"]}',
                created_at=cancelled_at.isoformat().replace("+00:00", "Z"),
                submitted_at=cancelled_at, source_type="SIMULATED_TEST_DATA", source_ref=row.action_id))
            db.flush()
            db.add(AfterSalesEvent(event_id=new_id(), order_id=order.order_id,
                return_id=None, refund_id=refund_id, event_type="REFUND_SUBMITTED",
                occurred_at=cancelled_at, recorded_at=cancelled_at,
                actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
                source_ref=row.action_id, details_json=safe_trace_json({
                    "created_by": "cancel_order", "review_required": True,
                    "payment_status": "NOT_EXECUTED", "amount_cents": order.paid_cents,
                    "policy_knowledge_version": args["policy_snapshot"]["knowledge_version"],
                    "policy_index_fingerprint": args["policy_snapshot"]["index_fingerprint"],
                    "policy_evaluation_at": args["policy_snapshot"]["evaluation_at"],
                    "policy_evidence": args["policy_snapshot"]["evidence"]})))
        db.add(OrderEvent(event_id=new_id(), order_id=order.order_id, event_type="ORDER_CANCELLED",
            from_status=old_status, to_status="CANCELLED", from_version=old_version,
            to_version=old_version + 1, occurred_at=cancelled_at,
            actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
            source_ref=row.action_id, details_json=safe_trace_json({
                "reason_code": args["reason_code"], "reason_source": args["reason_source"],
                "inventory_released": released, "refund_id": refund_id,
                "refund_status": "SUBMITTED" if refund_id else None,
                "payment_status": "NOT_EXECUTED", "policy_snapshot": args["policy_snapshot"]})))
        result_payload = {"order_id": order.order_id, "order_status": "CANCELLED",
            "order_from_status": old_status, "order_version": old_version + 1,
            "order_event": "ORDER_CANCELLED", "source_type": "SIMULATED_TEST_DATA",
            "refund_id": refund_id, "refund_status": "SUBMITTED" if refund_id else None,
            "refund_approval_status": "PENDING_REVIEW" if refund_id else None,
            "payment_status": "NOT_EXECUTED", "inventory_released": released,
            "policy_knowledge_version": args["policy_snapshot"]["knowledge_version"],
            "policy_evidence": args["policy_snapshot"]["evidence"],
            "policy_evaluation_at": args["policy_snapshot"]["evaluation_at"]}
        event_type = "ORDER_CANCEL_TRANSACTION_COMMITTED"
        span_node = "cancel_order_execute"
        business_payload = {"action_id": row.action_id, "tool_id": row.tool_id,
            "status": "ACTION_SUCCEEDED", "order_status": "CANCELLED",
            "refund_status": "SUBMITTED_PENDING_REVIEW" if refund_id else None,
            "payment_status": "NOT_EXECUTED", "transaction_reference": transaction_reference}
    row.status = "SUCCEEDED"
    row.action_version += 1
    row.finished_at = now
    row.result_json = safe_trace_json(result_payload)
    duration_ms = (time.perf_counter() - action_started) * 1000
    span_id = _record_span(db, row, span_node, "SUCCEEDED",
                           trace_id=active_trace_id, duration_ms=duration_ms)
    _record_execution_metrics(db, row, span_id, duration_ms, active_trace_id)
    _record_safe_log(db, row, active_trace_id, span_id)
    _audit(db, row, event_type, "EXECUTING", "SUCCEEDED",
           transaction_reference=transaction_reference, trace_id=active_trace_id)
    _record_trace(db, row, active_trace_id, event_type, "SUCCEEDED")
    _record_run_event(db, row, "tool_finished", business_payload,
        f"action:{row.action_id}:finished", trace_id=active_trace_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        diagnostic = str(getattr(exc, "orig", exc)).lower()
        if refund_mode and ("uq_refunds_one_active_per_order" in diagnostic or
                            "uq_refunds_simulated_source_ref" in diagnostic):
            raise ActionError("REFUND_ALREADY_OPEN", 409,
                              "An open refund application already exists") from exc
        if cancel_mode and ("uq_refunds_one_active_per_order" in diagnostic or
                            "uq_order_events_one_cancel" in diagnostic or
                            "uq_order_events_source_ref" in diagnostic):
            raise ActionError("STATE_CONFLICT", 409,
                              "Cancellation conflicts with an existing refund or cancellation") from exc
        if ticket_mode and "uq_support_tickets_open_draft" in diagnostic:
            raise ActionError("TICKET_ALREADY_OPEN", 409,
                              "An equivalent support ticket is already open") from exc
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row, False


def close_pending_action(db: Session, *, owner_id: str, action_id: str, target_status: str,
                         request_trace_id: str | None = None) -> PendingAction:
    if target_status not in {"REJECTED", "CANCELLED"}:
        raise ActionError("INVALID_ACTION_TRANSITION", 409, "Unsupported close state")
    row = db.execute(select(PendingAction).where(PendingAction.action_id == action_id,
        PendingAction.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ActionError("NOT_FOUND", 404, "Action not found")
    before, after = _transition(row, target_status)
    row.finished_at = utc_now()
    active_trace_id = request_trace_id or row.trace_id
    _audit(db, row, f"ACTION_{target_status}", before, after, trace_id=active_trace_id)
    _record_span(db, row, f"action_{target_status.lower()}", "SUCCEEDED", trace_id=active_trace_id)
    _record_run_event(db, row, "tool_finished", {"action_id": row.action_id,
        "tool_id": row.tool_id, "status": target_status},
        f"action:{row.action_id}:{target_status.lower()}", trace_id=active_trace_id)
    db.commit()
    db.refresh(row)
    return row


def mark_unknown_result(db: Session, *, owner_id: str, action_id: str, error_code: str) -> PendingAction:
    """Recovery hook for a future external executor; UNKNOWN_RESULT is never replayed."""
    row = db.execute(select(PendingAction).where(PendingAction.action_id == action_id,
        PendingAction.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ActionError("NOT_FOUND", 404, "Action not found")
    before, after = _transition(row, "UNKNOWN_RESULT")
    row.error_code = error_code[:64]
    row.finished_at = utc_now()
    _audit(db, row, "EXECUTION_RESULT_UNKNOWN", before, after, error_code=row.error_code)
    _record_span(db, row, "action_result_unknown", "UNKNOWN", row.error_code)
    _record_run_event(db, row, "tool_finished", {"action_id": row.action_id,
        "tool_id": row.tool_id, "status": "UNKNOWN_RESULT", "error_code": row.error_code},
        f"action:{row.action_id}:unknown")
    db.commit()
    db.refresh(row)
    return row


def action_feedback(row: PendingAction) -> str:
    """Describe only this persisted action result, including separate refund state."""
    if row.status!='SUCCEEDED':
        return f'操作记录状态为 {row.status}；没有据此确认业务已完成或资金到账。'
    result=json.loads(row.result_json) if row.result_json else {}
    if row.tool_id=='cancel_order':
        refund=(f"关联退款申请 {result['refund_id']} 的状态为 {result['refund_status']}，待审核；未执行支付，不代表已批准或到账。"
            if result.get('refund_id') else '本次没有关联退款申请；未执行支付。')
        return f"订单 {result['order_id']} 的取消事务已完成，记录状态为 {result['order_status']}。"+refund
    if row.tool_id=='create_refund_request':
        return f"退款申请 {result['refund_id']} 的记录状态为 {result['status']}，待审核；未执行支付，不代表批准或到账。"
    if row.tool_id=='create_return_request':
        return f"退货申请 {result['return_id']} 的记录状态为 {result['status']}，仍需审核；不代表退货获批或退款到账。"
    if row.tool_id=='escalate_to_human':
        return f"人工工单 {result['ticket_id']} 的记录状态为 {result['status']}；不代表已分配、已受理或已处理，未发送外部通知。"
    return '隔离模拟操作已有事务回执；不是外部真实交易。'


def action_view(row: PendingAction) -> dict:
    return {"action_id": row.action_id, "tool_id": row.tool_id,"answer":action_feedback(row),
            "target_resource_id": row.target_resource_id,
            "sanitized_arguments": json.loads(row.sanitized_arguments_json),
            "arguments_hash": row.arguments_hash, "action_version": row.action_version,
            "risk_level": row.risk_level, "status": row.status,
            "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
            "expires_at": row.expires_at.isoformat().replace("+00:00", "Z"),
            "confirmed_at": row.confirmed_at.isoformat().replace("+00:00", "Z") if row.confirmed_at else None,
            "finished_at": row.finished_at.isoformat().replace("+00:00", "Z") if row.finished_at else None,
            "result": json.loads(row.result_json) if row.result_json else None,
            "error_code": row.error_code, "run_id": row.run_id, "trace_id": row.trace_id,
            "audit_reference": row.audit_reference}


def action_workbench_view(row: PendingAction) -> dict:
    """Narrow, owner-facing projection for the read-only chat workbench.

    Keep internal argument hashes, trace/audit identifiers, raw result payloads,
    and the full verified snapshots out of this contract. Only known business
    action schemas are projected; unknown tools fail closed.
    """
    raw = json.loads(row.sanitized_arguments_json)
    policy = raw.get("policy_snapshot") if isinstance(raw.get("policy_snapshot"), dict) else {}
    order = raw.get("order_snapshot") if isinstance(raw.get("order_snapshot"), dict) else {}
    facts = raw.get("fact_snapshot") if isinstance(raw.get("fact_snapshot"), dict) else {}
    evidence = policy.get("evidence") if isinstance(policy.get("evidence"), list) else []
    policy_refs = [{key: item.get(key) for key in ("chunk_id", "policy_id", "clause_id", "version")
                    if isinstance(item, dict) and item.get(key) is not None}
                   for item in evidence[:12] if isinstance(item, dict)]

    parameters: dict = {}
    verified_snapshot: dict = {}
    user_claims: dict = {}
    capabilities = {
        "create_return_request", "create_refund_request", "cancel_order", "escalate_to_human"
    }
    if row.tool_id not in capabilities:
        raise ActionError("ACTION_NOT_DISPLAYABLE", 404, "Action not found")
    for key in ("order_id", "reason_code", "reason_summary", "amount_cents", "return_id",
                "application_outcome", "business_outcome",
                "issue_category", "issue_summary", "user_description", "related_agents",
                "verified_fact_refs", "unverified_claim_codes", "evidence_references"):
        if key in raw:
            parameters[key] = raw[key]
    if raw.get("reason_source") == "USER_CLAIM_UNVERIFIED":
        user_claims["reason_summary"] = "USER_CLAIM_UNVERIFIED"
    for key in ("status", "paid_at", "delivered_at", "version"):
        if key in order:
            verified_snapshot[key] = order[key]
    for key in ("branch", "return_id", "return_status", "delivered_at", "eligibility"):
        if key in facts:
            verified_snapshot[key] = facts[key]

    result = json.loads(row.result_json) if row.result_json else None
    result_projection = None
    if isinstance(result, dict):
        result_projection = {key: result[key] for key in
            ("ticket_id", "return_id", "refund_id", "order_id", "status", "application_status")
            if key in result and isinstance(result[key], (str, int, bool, type(None)))}
        result_projection['answer']=action_feedback(row)
    now = utc_now()
    expired = row.status in {"CREATED", "PENDING_CONFIRMATION", "CONFIRMED"} and row.expires_at <= now
    return {
        "action_id": row.action_id, "session_id": row.session_id, "run_id": row.run_id,
        "tool_id": row.tool_id, "target_resource_id": row.target_resource_id,
        "status": row.status, "risk_level": row.risk_level, "action_version": row.action_version,
        "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
        "expires_at": row.expires_at.isoformat().replace("+00:00", "Z"),
        "confirmed_at": row.confirmed_at.isoformat().replace("+00:00", "Z") if row.confirmed_at else None,
        "finished_at": row.finished_at.isoformat().replace("+00:00", "Z") if row.finished_at else None,
        "expired": expired,
        "safe_error_code": row.error_code if row.error_code and re.fullmatch(r"[A-Z0-9_]{1,64}", row.error_code) else None,
        "parameters": parameters, "verified_snapshot": verified_snapshot,
        "user_claim_markers": user_claims, "policy_references": policy_refs,
        "policy_version": policy.get("knowledge_version"),
        "policy_evaluation_at": policy.get("evaluation_at"),
        "result_summary": result_projection,
    }
