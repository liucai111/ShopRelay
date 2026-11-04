"""Controlled Skill selection and capability scoping over existing Agent tools."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


MAX_AGENT_CALLS = 4
MAX_STEPS = 14
WRITE_CAPABILITIES = frozenset({"create_refund_request", "create_return_request", "cancel_order"})
NON_EXECUTABLE_CAPABILITIES = frozenset({"escalate_to_human"})


@dataclass(frozen=True)
class Skill:
    skill_id: str
    version: str
    description: str
    allowed_agents: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    preconditions: tuple[str, ...]
    risk_level: str
    required_permissions: tuple[str, ...] = ("business:read",)
    max_agent_calls: int = MAX_AGENT_CALLS
    max_steps: int = MAX_STEPS
    success_condition: str = "All requested read results are validated and traceable"
    failure_condition: str = "Any required source, permission, schema, or budget check fails"
    clarification_condition: str = "A required identifier or verified business fact is missing"
    proposal_tools: tuple[str, ...] = ()

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "required": ["query", "required_capabilities"],
            "additionalProperties": False, "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 4000},
                "required_capabilities": {"type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "string", "enum": list(self.allowed_tools)}}}}

    @property
    def output_schema(self) -> dict[str, Any]:
        properties = {"supervisor_status": {"type": "string", "enum": ["COMPLETED", "STUB_ONLY", "BLOCKED", "FAILED"]},
            "agent_budget": {"type": "object", "required": ["agent_calls", "steps"],
                "properties": {"agent_calls": {"type": "integer", "minimum": 0,
                    "maximum": self.max_agent_calls}, "steps": {"type": "integer", "minimum": 0,
                    "maximum": self.max_steps}}},
            "handoffs": {"type": "array"}, "error_code": {"type": "string", "maxLength": 64}}
        return {"type": "object", "properties": properties, "oneOf": [
            {"required": ["supervisor_status", "agent_budget", "handoffs"],
             "properties": {"supervisor_status": {"enum": ["COMPLETED", "STUB_ONLY", "BLOCKED"]}}},
            {"required": ["supervisor_status", "error_code"],
             "properties": {"supervisor_status": {"const": "FAILED"}}}]}


SKILL_REGISTRY: dict[str, Skill] = {
    "return_handling": Skill("return_handling", "1.0.0",
        "Explain return/refund policy using an owned order, current policy evidence and audited after-sales state.",
        ("order_logistics", "policy", "after_sales"),
        ("get_order", "search_knowledge", "get_refund_status"),
        ("authenticated_owner_context", "order_id_required", "policy_snapshot_pinned"), "READ_ONLY",
        proposal_tools=("create_return_request", "create_refund_request")),
    "order_tracking": Skill("order_tracking", "1.0.0",
        "Read an owned order and its recorded shipment history.",
        ("order_logistics",), ("get_order", "track_shipment"),
        ("authenticated_owner_context", "order_id_required"), "READ_ONLY"),
    "order_cancellation": Skill("order_cancellation", "1.0.0",
        "Verify order, fulfillment and cancellation policy; only propose an owner-confirmed isolated cancellation action.",
        ("order_logistics", "policy", "after_sales"),
        ("get_order", "get_refund_status", "search_knowledge"),
        ("authenticated_owner_context", "order_id_required", "unshipped_state_verified",
         "inventory_reservation_verified", "effective_policy_snapshot_pinned"),
        "READ_ONLY_ACTION_PROPOSAL_ONLY", proposal_tools=("cancel_order",)),
    "human_escalation": Skill("human_escalation", "1.0.0",
        "Prepare a bounded human-support escalation proposal from verified Agent results; ticket submission requires a separate owner-confirmed server action.",
        ("order_logistics", "policy", "after_sales", "product"),
        ("get_order", "get_order_items", "track_shipment", "get_refund_status", "search_knowledge",
         "search_products", "get_product_detail", "check_inventory", "get_coupon", "get_warranty"),
        ("authenticated_owner_context_when_order_is_linked", "bounded_redacted_summary",
         "verified_and_unverified_facts_separated"), "READ_ONLY_ACTION_PROPOSAL_ONLY",
        proposal_tools=("escalate_to_human",)),
    "product_recommend": Skill("product_recommend", "1.0.0",
        "Search the public catalog and return deterministic recommendations with data limits.",
        ("product",), ("search_products", "get_product_detail", "recommend_products", "check_inventory"),
        ("bounded_catalog_query",), "READ_ONLY"),
    "generic_read_only": Skill("generic_read_only", "1.0.0",
        "Fallback Skill for explicitly registered read-only capabilities when no specialist Skill matches.",
        ("order_logistics", "policy", "after_sales", "product"),
        ("get_order", "get_order_items", "track_shipment", "get_refund_status", "search_products",
         "get_product_detail", "recommend_products", "check_inventory", "get_coupon", "get_warranty",
         "search_knowledge"), ("authenticated_context_when_private_data_is_requested",), "READ_ONLY"),
}

_PATTERNS = {
    # Prefer narrow/high-specificity intents before overlapping return/refund
    # vocabulary (for example, cancelling an order and asking about refund policy).
    "order_cancellation": re.compile(r"取消订单|订单取消|撤销订单"),
    "order_tracking": re.compile(r"物流|运单|快递|包裹|配送|追踪|跟踪"),
    "human_escalation": re.compile(r"人工客服|联系人工|转人工|人工处理|找客服"),
    "return_handling": re.compile(r"退货|退款|售后|退费"),
    "product_recommend": re.compile(r"推荐|帮我(?:挑|选)|适合.{0,18}(?:预算|需求)|选购"),
}


def bind_skill(query: str, route: dict[str, Any]) -> dict[str, Any] | None:
    """Bind an existing AGENT route to one versioned Skill without widening it."""
    if route.get("strategy") != "AGENT":
        return None
    capabilities = route.get("required_capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        return None
    for skill_id, pattern in _PATTERNS.items():
        if not pattern.search(query):
            continue
        skill = SKILL_REGISTRY[skill_id]
        # A Skill may constrain an existing route, but may not add tools or turn
        # a write proposal into an executable capability.
        if not set(capabilities).issubset(set(skill.allowed_tools) | set(skill.proposal_tools)) or \
                set(capabilities) & WRITE_CAPABILITIES or \
                (set(capabilities) & NON_EXECUTABLE_CAPABILITIES) - set(skill.proposal_tools):
            return None
        return {"skill_id": skill.skill_id, "skill_version": skill.version,
                "skill_hit_reason": f"KEYWORD_FAMILY:{skill_id}",
                "skill_allowed_tools": list(skill.allowed_tools),
                # Proposal metadata is descriptive only. It is intentionally not
                # included in allowed_tools and does not invoke a write executor.
                "skill_proposable_tools": list(skill.proposal_tools),
                "skill_max_agent_calls": skill.max_agent_calls, "skill_max_steps": skill.max_steps}
    fallback = SKILL_REGISTRY["generic_read_only"]
    if set(capabilities).issubset(fallback.allowed_tools) and not set(capabilities) & WRITE_CAPABILITIES:
        return {"skill_id": fallback.skill_id, "skill_version": fallback.version,
                "skill_hit_reason": "FALLBACK:MINIMAL_READ_ONLY_SET",
                "skill_allowed_tools": list(fallback.allowed_tools),
                "skill_max_agent_calls": fallback.max_agent_calls, "skill_max_steps": fallback.max_steps}
    return None


def validate_skill_execution(route: dict[str, Any], *, query: str | None = None,
                             agent_calls: int = 0, steps: int = 0) -> Skill:
    skill_id = route.get("skill_id")
    skill = SKILL_REGISTRY.get(skill_id) if isinstance(skill_id, str) else None
    if skill is None:
        # Skill selection is optional for unmatched/ambiguous requests; no policy is weakened.
        raise ValueError("SKILL_NOT_SELECTED")
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 4000):
        raise ValueError("SKILL_INPUT_SCHEMA_INVALID")
    if route.get("skill_version") != skill.version:
        raise ValueError("SKILL_VERSION_MISMATCH")
    capabilities = route.get("required_capabilities") or []
    if not isinstance(capabilities, list):
        raise ValueError("SKILL_TOOL_SCOPE_VIOLATION")
    executable = set(capabilities) - set(skill.proposal_tools)
    proposals = set(capabilities) & set(skill.proposal_tools)
    if not executable.issubset(skill.allowed_tools) or not proposals.issubset(skill.proposal_tools):
        raise ValueError("SKILL_TOOL_SCOPE_VIOLATION")
    if proposals and not proposals.issubset(set(route.get("skill_proposable_tools") or [])):
        raise ValueError("SKILL_TOOL_SCOPE_VIOLATION")
    if executable & (WRITE_CAPABILITIES | NON_EXECUTABLE_CAPABILITIES):
        raise ValueError("SKILL_WRITE_EXECUTION_FORBIDDEN")
    if agent_calls > skill.max_agent_calls or steps > skill.max_steps:
        raise ValueError("SKILL_BUDGET_EXCEEDED")
    return skill
