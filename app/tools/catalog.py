"""Unified, server-owned contract inventory for the 15 planned domain tools.

This is an inventory and enforcement input, not a claim that every planned tool
has an executor. MCP annotations never alter these local risk decisions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolContract:
    tool_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    agent: str
    risk_level: str
    required_permissions: tuple[str, ...]
    parameter_constraints: tuple[str, ...]
    data_source: str
    update_semantics: str
    error_states: tuple[str, ...]
    confirmation_required: bool
    mcp_allowed: bool
    implementation_status: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["required_permissions"] = list(self.required_permissions)
        row["parameter_constraints"] = list(self.parameter_constraints)
        row["error_states"] = list(self.error_states)
        return row


_OBJ = {"type": "object", "additionalProperties": False}
_ID = {"type": "string", "minLength": 1, "maxLength": 64}
_RESULT = {"type": "object", "required": ["source"], "properties": {
    "source": {"type": "string", "minLength": 1},
    "status": {"type": ["string", "null"]},
    "availability": {"enum": ["AVAILABLE", "NOT_FOUND", "NOT_AVAILABLE", "UNAVAILABLE", "UNKNOWN"]},
    "error_code": {"type": ["string", "null"]}},
    # Tool-specific business payloads remain typed by each executor; the shared
    # envelope deliberately permits those bounded, versioned nested schemas.
    "additionalProperties": True}


def _schema(*, required: tuple[str, ...] = (), **properties: dict[str, Any]) -> dict[str, Any]:
    return {**_OBJ, "required": list(required), "properties": properties}


def _contract(tool_id: str, agent: str, description: str, fields: dict[str, Any], *,
              required: tuple[str, ...] = (), risk: str = "READ_ONLY", status: str,
              source: str, update: str = "Read-only snapshot at request time",
              permissions: tuple[str, ...] = ("business:read",), constraints: tuple[str, ...] = (),
              errors: tuple[str, ...] = ("NOT_FOUND", "UNAVAILABLE", "TOOL_EXECUTION_FAILED"),
              confirmation: bool = False, mcp: bool = True, evidence: str) -> ToolContract:
    write = risk in {"WRITE_LOW_RISK", "WRITE_HIGH_RISK"}
    return ToolContract(tool_id=tool_id, name=tool_id, description=description,
        input_schema=_schema(required=required, **fields), output_schema=_RESULT, agent=agent,
        risk_level=risk, required_permissions=permissions, parameter_constraints=constraints,
        data_source=source, update_semantics=update, error_states=errors,
        confirmation_required=confirmation, mcp_allowed=mcp and not write,
        implementation_status=status, evidence=evidence)


_rows = [
    _contract("get_order", "order_logistics", "Read an owned order's current stored status and timestamps.",
        {"order_id": _ID}, required=("order_id",), status="NATIVE_READ_IMPLEMENTED",
        source="PostgreSQL orders", constraints=("order_id must be normalized", "owner predicate required"),
        evidence="app/m3/order_tools.py; M3.2 PostgreSQL probe"),
    _contract("get_order_items", "order_logistics", "Read items belonging to an owned order.",
        {"order_id": _ID}, required=("order_id",), status="NATIVE_READ_IMPLEMENTED",
        source="PostgreSQL order_items/products", constraints=("owner predicate required",),
        evidence="app/m3/order_tools.py; M3.2 PostgreSQL probe"),
    _contract("track_shipment", "order_logistics", "Read recorded shipment events for an owned order.",
        {"order_id": _ID}, required=("order_id",), status="NATIVE_READ_IMPLEMENTED",
        source="PostgreSQL shipments", update="Stored event history; not a live carrier guarantee",
        constraints=("owner predicate required",), evidence="app/m3/order_tools.py; M3.2 PostgreSQL probe"),
    _contract("get_refund_status", "after_sales", "Read audited return/refund request state for an owned order.",
        {"order_id": _ID}, required=("order_id",), status="NATIVE_READ_IMPLEMENTED",
        source="PostgreSQL refunds/returns/after_sales_events", constraints=("owner predicate required",),
        evidence="app/m3/after_sales_tools.py; M3.5-DATA PostgreSQL probe"),
    _contract("create_refund_request", "after_sales", "Propose a whole-order refund application; isolated execution requires explicit authenticated confirmation and never moves funds.",
        {"order_id": _ID, "reason_code": {"type": "string", "enum": ["PRE_SHIPMENT", "APPROVED_RETURN", "OTHER"]},
         "reason_summary": {"type": "string", "minLength": 1, "maxLength": 300}},
        required=("order_id", "reason_code", "reason_summary"), risk="WRITE_HIGH_RISK", permissions=("actions:confirm", "after_sales.refund.create"),
        status="ISOLATED_POSTGRES_EXECUTOR; DISABLED_OUTSIDE_shoprelay_m11_test",
        source="refunds + after_sales_events; SIMULATED_TEST_DATA only",
        update="Creates SUBMITTED/PENDING_REVIEW only; never approves or initiates payment",
        constraints=("server derives integer amount from order.paid_cents", "verified owner and scenario-specific policy preconditions",
                     "explicit confirm=true and immutable argument hash", "single open refund per order", "MCP remains disabled"),
        confirmation=True, mcp=False,
        errors=("REQUIRES_CONFIRMATION", "REFUND_REQUIRES_MANUAL_REVIEW", "RETURN_NOT_APPROVED",
                "REFUND_ALREADY_OPEN", "REFUND_POLICY_EVIDENCE_NOT_FOUND", "FORBIDDEN"),
        evidence="app/m4/actions.py; M4.3-B2 isolated transaction"),
    _contract("create_return_request", "after_sales", "Create a return application only after an owner-confirmed isolated Pending Action transaction; approval and refund are separate.",
        {"order_id": _ID, "reason_code": {"type": "string", "enum": ["QUALITY_ISSUE", "WRONG_ITEM", "DAMAGED_IN_TRANSIT", "OTHER"]},
         "reason_summary": {"type": "string", "minLength": 1, "maxLength": 300}},
        required=("order_id", "reason_code", "reason_summary"), risk="WRITE_HIGH_RISK", permissions=("actions:confirm", "after_sales.return.create"),
        status="ISOLATED_POSTGRES_EXECUTOR; DISABLED_OUTSIDE_shoprelay_m11_test", source="returns + after_sales_events; isolated SIMULATED_TEST_DATA only",
        update="Creates SUBMITTED/PENDING_HUMAN_REVIEW only; never approves or refunds", constraints=("server-owned order check", "explicit confirm=true", "immutable argument hash", "one active simulated return per order"),
        confirmation=True, mcp=False, errors=("REQUIRES_CONFIRMATION", "RETURN_ORDER_FACTS_INSUFFICIENT", "RETURN_POLICY_EVIDENCE_NOT_FOUND", "RETURN_ALREADY_OPEN", "ACTION_ARGUMENTS_CHANGED", "FORBIDDEN"), evidence="app/m4/actions.py; M4.3-B1 isolated transaction"),
    _contract("cancel_order", "after_sales", "Cancel an owned, verified unshipped order only through an explicit isolated Pending Action confirmation.",
        {"order_id": _ID, "reason_code": {"type": "string", "enum": ["USER_REQUEST", "OTHER"]},
         "reason_summary": {"type": "string", "minLength": 1, "maxLength": 300}},
        required=("order_id", "reason_code", "reason_summary"), risk="WRITE_HIGH_RISK",
        permissions=("actions:confirm", "orders.cancel"), status="ISOLATED_POSTGRES_EXECUTOR; DISABLED_OUTSIDE_shoprelay_m11_test",
        source="PostgreSQL orders/order_items/inventory; cancel policy and refund application snapshot",
        update="Unpaid CREATED: cancel and release verified reservation; paid PAID: atomic cancel plus submitted refund application; no payment execution",
        constraints=("trusted owner", "explicit confirm=true", "immutable argument hash", "order version CAS", "inventory reservation CAS", "MCP writes forbidden"),
        confirmation=True, mcp=False,
        errors=("REQUIRES_CONFIRMATION", "CANCEL_NOT_ALLOWED", "STATE_CONFLICT", "CANCEL_INVENTORY_FACTS_UNKNOWN", "CANCEL_POLICY_EVIDENCE_NOT_FOUND", "ACTION_BUSINESS_FACTS_CHANGED", "FORBIDDEN"), evidence="app/m4/actions.py; M4.3-B3 isolated transaction"),
    _contract("search_products", "product", "Search the bounded public product catalog.",
        {"keyword": {"type": "string", "maxLength": 80}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        status="NATIVE_READ_IMPLEMENTED", source="PostgreSQL products", constraints=("limit <= 20",),
        evidence="app/m3/product_tools.py; M3.3 PostgreSQL probe"),
    _contract("get_product_detail", "product", "Read stored public product fields.",
        {"product_id": _ID}, required=("product_id",), status="NATIVE_AND_MCP_READ_IMPLEMENTED",
        source="PostgreSQL products", constraints=("product identifier format validated",),
        evidence="app/m3/product_tools.py; docs/probes/m4.1-mcp-dual-transport-20261022-r3.json"),
    _contract("recommend_products", "product", "Return deterministic catalog candidates with explicit match reasons.",
        {"keyword": {"type": "string", "maxLength": 80}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
        status="NATIVE_READ_IMPLEMENTED", source="PostgreSQL products/inventory snapshot",
        update="Catalog snapshot; inventory freshness remains unverified", evidence="app/m3/product_tools.py; M3.3 probe"),
    _contract("check_inventory", "product", "Read inventory snapshot without claiming current sellability.",
        {"product_id": _ID}, required=("product_id",), status="NATIVE_READ_IMPLEMENTED",
        source="PostgreSQL inventory snapshot", update="No observation timestamp; current stock is UNKNOWN",
        evidence="app/m3/product_tools.py; M3.3 BLOCKED_DATA_SOURCE record"),
    _contract("get_coupon", "policy", "Read coupon catalog metadata; does not prove entitlement or usability.",
        {"coupon_code": {"type": "string", "minLength": 1, "maxLength": 64}}, required=("coupon_code",),
        status="NATIVE_AND_MCP_READ_IMPLEMENTED_METADATA_ONLY", source="M0 coupons catalog",
        update="Stored policy/version/threshold only; active validity and user entitlement UNKNOWN",
        constraints=("coupon code normalized and bounded",),
        evidence="migrations/0001_m0.sql; app/m3/policy_tools.py"),
    _contract("get_warranty", "policy", "Read current warranty policy evidence; actual product entitlement is not inferred.",
        {"product_id": _ID}, required=("product_id",), status="POLICY_EVIDENCE_ONLY",
        source="Versioned ShopRelay policy index; no verified purchase/activation source",
        update="Policy evidence pinned to current knowledge snapshot", errors=("POLICY_EVIDENCE_NOT_FOUND", "WARRANTY_ELIGIBILITY_UNKNOWN"),
        evidence="knowledge/policies.json; app/m2/retrieval.py"),
    _contract("search_knowledge", "policy", "Search current, version-pinned policy evidence.",
        {"query": {"type": "string", "minLength": 1, "maxLength": 1000}}, required=("query",),
        status="NATIVE_READ_IMPLEMENTED", source="M2 shared versioned policy index",
        update="Snapshot/version/effective interval validated", evidence="app/m3/policy_tools.py; M3.4 probes"),
    _contract("escalate_to_human", "after_sales", "Propose a bounded human-support ticket; this Agent capability is proposal-only and isolated submission requires a separate explicit owner confirmation.",
        {"issue_category": {"type": "string", "enum": ["ACCOUNT_ACCESS", "ORDER_STATUS", "RETURN_REFUND", "PRODUCT_QUALITY", "POLICY_QUESTION", "PRIVACY_SECURITY", "OTHER"]},
          "order_id": {"anyOf": [_ID, {"type": "null"}]},
          "user_description": {"type": "string", "minLength": 1, "maxLength": 1000},
          "issue_summary": {"type": "string", "minLength": 1, "maxLength": 600},
          "reason_code": {"type": "string", "pattern": "^[A-Z0-9_]{1,64}$"},
          "related_agents": {"type": "array", "maxItems": 4,
              "items": {"type": "string", "enum": ["order_logistics", "policy", "after_sales", "product"]}},
          "verified_fact_refs": {"type": "array", "maxItems": 12,
              "items": {"type": "string", "pattern": "^(evidence|fact|tool|policy):[A-Za-z0-9._:-]{1,128}$"}},
          "unverified_claim_codes": {"type": "array", "maxItems": 12,
              "items": {"type": "string", "pattern": "^[A-Z0-9_]{1,64}$"}},
          "evidence_references": {"type": "array", "maxItems": 12,
              "items": {"type": "string", "pattern": "^(evidence|fact|tool|policy):[A-Za-z0-9._:-]{1,128}$"}}},
        required=("issue_category", "user_description", "issue_summary", "reason_code"),
        risk="WRITE_LOW_RISK", permissions=("actions:confirm", "support.ticket.create"),
        status="ISOLATED_CONFIRMATION_TRANSACTION", source="PostgreSQL shoprelay_m11_test; no external ticket system",
        update="Agent Skill may produce only a proposal; POST /api/actions persists a Pending Action and requires owner confirmation before the isolated ticket transaction.",
        constraints=("related_agents and reference arrays are optional and default to empty in the API schema",
            "linked order must belong to the authenticated owner",
            "verified_fact_refs and evidence_references must resolve in the owner's recorded source Run output",
            "ticket submission does not imply external assignment, notification, or handling"),
        confirmation=True, mcp=False, errors=("ACTION_EXECUTION_DISABLED", "INVALID_ACTION_ARGUMENTS", "ORDER_NOT_FOUND",
            "ACTION_CONTEXT_MISMATCH", "TICKET_REFERENCE_UNVERIFIED", "TICKET_ALREADY_OPEN", "TOOL_NOT_AUTHORIZED"),
        evidence="M4.3-B4 support_tickets/support_ticket_events and Pending Action confirmation"),
]

TOOL_CATALOG: dict[str, ToolContract] = {row.tool_id: row for row in _rows}
if len(TOOL_CATALOG) != 15:
    raise RuntimeError("the planned tool catalog must contain exactly 15 contracts")


def get_tool_contract(tool_id: str) -> ToolContract:
    try:
        return TOOL_CATALOG[tool_id]
    except KeyError as exc:
        raise ValueError("unknown ShopRelay tool") from exc
