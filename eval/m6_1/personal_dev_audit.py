"""Exhaustive dependency audit; never invokes a model, Agent, or Runner."""
from __future__ import annotations

from .personal_dev import OWNER_MAP, plan_case, D1PreflightError
from .fixture_read_models import scoped_facts, READ_TOOLS, product_id
from app.tools.catalog import get_tool_contract


def audit_case(bundle, case):
    calls = case["input"].get("expected_tool_calls", [])
    selected = scoped_facts(bundle, case)
    missing, interfaces, arguments, mappings = [], [], [], []
    facts = []
    for source_id, (kind, row) in sorted(selected.items()):
        facts.append({"source_id": source_id, "kind": kind, "original_value": row})
        if kind == "orders_and_snapshots":
            for field in ("status", "paid_cents", "paid_at", "delivered_at"):
                value = row.get("status", row.get("order_status")) if field == "status" else row.get(field)
                if value is None:
                    missing.append({"source_id": source_id, "field": field,
                        "representation": "NULL_UNKNOWN_IN_SOURCE_PRESERVING_READ_PROJECTION",
                        "core_order_insert": "NOT_USED_UNLESS_ALL_NOT_NULL_FACTS_EXIST",
                        "fact_can_be_inferred": False})
            if "order_status" in row and "status" not in row:
                mappings.append({"source_id": source_id, "mapping": "order_status -> read.status", "reason": "Explicit stored status alias; original JSON preserved"})
        if kind == "products":
            mappings.append({"source_id": source_id, "technical_product_id": product_id(source_id), "business_fact_changed": False})
            for field in ("category", "price_cents"):
                if row.get(field) is None:
                    missing.append({"source_id": source_id, "field": field, "representation": "NULL_UNKNOWN; recorded attributes retained", "fact_can_be_inferred": False})
        if kind == "order_items" and row.get("paid_cents") is None:
            missing.append({"source_id": source_id, "field": "paid_cents", "representation": "NULL_UNKNOWN_ITEM_AMOUNT", "fact_can_be_inferred": False})
        if kind == "refunds" and row.get("amount_cents") is None:
            missing.append({"source_id": source_id, "field": "amount_cents", "representation": "UNKNOWN_AMOUNT; existing verifier still validates status chain", "fact_can_be_inferred": False})
        if kind == "inventory" and row.get("observed_at") is None:
            missing.append({"source_id": source_id, "field": "observed_at", "representation": "UNAVAILABLE_CURRENT_STOCK; preserve snapshot counts", "fact_can_be_inferred": False})
    for call in calls:
        capability = call["tool_id"]
        contract = get_tool_contract(capability)
        interfaces.append({"tool_id": capability, "required_inputs": contract.input_schema.get("required", []),
            "adapter_implemented": capability in READ_TOOLS,
            "evidence": "fixture_read_models.py; existing app/m3 repositories, evidence_is_valid and _verified_history"})
        if capability == "get_coupon":
            arguments.append({"tool_id": capability, "field": "coupon_code", "status": "NOT_SUPPLIED_IN_QUERY_OR_FIXTURE; no invented code"})
        if capability == "get_warranty" and not any(k == "products" for k, _ in selected.values()):
            arguments.append({"tool_id": capability, "field": "product_id", "status": "NO_PRODUCT_REFERENCE_FOR_REQUIRED_TOOL_ARGUMENT"})
    business = []
    if case["case_id"] == "D1-AFTERSALES-024":
        business.append("Universal paid_at eligibility rule pending business owner; current proposal preflight rule retained; read-only evaluation does not decide it")
    if case["case_id"] == "D1-CROSSAGENT-033":
        business.append("Historical independent electrical safety and business/policy review signatures remain unverified; do not select a voltage or claim authorization")
    if case["case_id"] == "D1-SAFETY-038":
        business.append("Authority/precedence absent by design; expected behavior is clarification, not choosing a version")
    plan_error = None
    try:
        plan_case(bundle, case["case_id"])
    except D1PreflightError as exc:
        plan_error = str(exc)
    categories = ["EXISTING_READ_CONTRACT_SUPPORTED", "EXECUTION_CHAIN_UPDATE"]
    if calls:
        categories.append("READ_FIXTURE_ADAPTER_IMPLEMENTED")
    if missing:
        categories.append("SPARSE_FACTS_EXPLICIT_UNKNOWN_NO_GUESSED_SUPPLEMENT")
    if arguments:
        categories.append("CASE_INPUT_OR_TOOL_ARGUMENT_DECISION_REQUIRED")
    if business:
        categories.append("BUSINESS_OR_AUTHORITY_QUESTION_RECORDED")
    categories.append("RUNTIME_AUTHORIZATION_REQUIRED")
    return {"case_id": case["case_id"], "group_id": case["group_id"], "fixture_ref": case["fixture_ref"],
        "original_input": case["input"], "required_tool_contracts": interfaces,
        "required_business_capabilities": [c["tool_id"] for c in calls], "facts_and_dependencies": facts,
        "missing_fields": missing, "missing_interfaces": [r["tool_id"] for r in interfaces if not r["adapter_implemented"]],
        "missing_tool_arguments": arguments, "technical_mapping_only": mappings,
        "compatible_read_representation": "Marked SQLite lease-scoped sparse JSON projection; existing production schema and amounts unchanged",
        "read_only_contract_implementable": not arguments,
        "unauthorized_write_executed": False,
        "semantic_proposal_only": case["expected_outcome"].get("outcome_type") in {"HUMAN_ESCALATION_PROPOSAL", "HUMAN_ESCALATION_STATUS_BOUNDARY"},
        "blocked_action_capability": case["expected_outcome"].get("blocked_action_capability"),
        "independent_offline_conditions": True,
        "engineering_preflight": "READY_FOR_OFFLINE_VALIDATION" if plan_error is None and not arguments else "BLOCKED_TOOL_ARGUMENT" if arguments else plan_error,
        "business_questions": business, "categories": categories,
        "remaining_admission": ["Legitimate personal diagnostic grant policy/Registry authorization", "Scoped model/price/budget/runtime permit", "Authorized real execution; no quality verdict from offline tests"]}


def audit_bundle(bundle):
    rows = [audit_case(bundle, case) for case in bundle["cases"]]
    if len(rows) != 40 or len({r["case_id"] for r in rows}) != 40:
        raise ValueError("EXPECTED_40_UNIQUE_D1_CASES")
    return rows
