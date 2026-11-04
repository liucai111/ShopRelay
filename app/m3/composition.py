"""Deterministic composition of validated specialist read results.

This adapter does not execute tools or infer facts. Each specialist's existing
renderer validates that its result references point to this Run's real result
store before the outputs are joined.
"""
from __future__ import annotations

from typing import Any
import re

from app.m3.after_sales_tools import build_after_sales_business_result
from app.m3.order_tools import build_order_business_result, is_return_guidance_query
from app.m3.policy_tools import build_policy_business_result
from app.m3.product_tools import build_product_business_result

_AGENT_ORDER = ("order_logistics", "product", "policy", "after_sales")


def combine_specialist_outputs(outputs: list[tuple[str, dict[str, Any]]], *,
                               tool_execution_status: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Join already validated service outputs without upgrading their claims."""
    if len(outputs) < 2 or len({name for name, _ in outputs}) != len(outputs):
        return None
    by_name = dict(outputs)
    after_sales = by_name.get("after_sales")
    # The After-sales renderer already combines verified order, policy and
    # status facts. Avoid duplicating those prose sections; preserve Product as
    # an independent, explicitly sourced section when present.
    if after_sales is not None:
        answer_parts = ([by_name["order_logistics"]["answer"], after_sales["answer"]]
            if "order_logistics" in by_name and "policy" not in by_name else [after_sales["answer"]])
        if "product" in by_name:
            answer_parts.insert(0, by_name["product"]["answer"])
    else:
        answer_parts = [by_name[name]["answer"] for name in _AGENT_ORDER if name in by_name]
    citations: list[dict[str, Any]] = []
    seen_citations: set[str] = set()
    references: list[dict[str, Any]] = []
    seen_references: set[str] = set()
    for name in _AGENT_ORDER:
        value = by_name.get(name)
        if value is None:
            continue
        for citation in value.get("citations", []):
            citation_id = citation.get("chunk_id")
            if isinstance(citation_id, str) and citation_id not in seen_citations:
                seen_citations.add(citation_id)
                citations.append(citation)
        for reference in value.get("tool_references", []):
            reference_id = reference.get("result_ref")
            if isinstance(reference_id, str) and reference_id not in seen_references:
                seen_references.add(reference_id)
                references.append(reference)
    if not answer_parts or not references:
        return None
    has_policy = any(value.get("answer_kind") == "POLICY_ANSWER" for _, value in outputs)
    has_clarification = any(value.get("answer_kind") == "CLARIFICATION_REQUIRED" for _, value in outputs)
    answer_kind = "CLARIFICATION_REQUIRED" if has_clarification else (
        "POLICY_ANSWER" if has_policy else "BUSINESS_FACTS")
    policy_output = by_name.get("policy") or after_sales or {}
    duration = sum(float(row.get("duration_ms") or 0) for row in tool_execution_status
                   if row.get("executed"))
    return {
        "answer": "".join(answer_parts),
        "answer_kind": answer_kind,
        "business_results": {name: value.get("business_results", {}) for name, value in by_name.items()},
        "tool_references": references,
        "tool_execution_status": tool_execution_status,
        "citations": citations,
        "evidence": citations,
        "knowledge_version": policy_output.get("knowledge_version"),
        "retrieval": policy_output.get("retrieval"),
        "policy_retrieval": policy_output.get("policy_retrieval"),
        "latency_ms": round(duration, 3),
    }


def build_multi_agent_business_result(supervisor_result: dict[str, Any], *, route: dict[str, Any],
                                      trusted: Any, result_store: dict[str, dict[str, Any]], query: str = "") -> dict[str, Any] | None:
    """Render a 2–4 specialist run using the existing single-specialist validators."""
    if supervisor_result.get("supervisor_status") != "COMPLETED" or \
            supervisor_result.get("terminal_status") != "RUNNING":
        return None
    raw_handoffs = supervisor_result.get("handoffs") or []
    if len(raw_handoffs) < 2 or len(raw_handoffs) > 4:
        return None
    handoffs = {row.get("target_agent"): row for row in raw_handoffs if isinstance(row, dict)}
    if len(handoffs) != len(raw_handoffs):
        return None
    targets = [name for name in _AGENT_ORDER if name in handoffs]
    if targets != [row.get("target_agent") for row in raw_handoffs]:
        return None
    if any(row.get("status") != "COMPLETED" or
           (row.get("result") or {}).get("source_agent") != row.get("target_agent")
           for row in raw_handoffs):
        return None
    capabilities = route.get("required_capabilities")
    if not isinstance(capabilities, list):
        return None
    status_rows = supervisor_result.get("tool_execution_status") or []
    outputs: list[tuple[str, dict[str, Any]]] = []
    builders = {
        "order_logistics": lambda sub_result, subroute: build_order_business_result(
            sub_result, route=subroute, trusted=trusted, result_store=result_store),
        "product": lambda sub_result, subroute: build_product_business_result(
            sub_result, route=subroute, result_store=result_store),
        "policy": lambda sub_result, subroute: build_policy_business_result(
            sub_result, route=subroute, result_store=result_store),
    }
    for name in targets:
        handoff = handoffs[name]
        owned = [cap for cap in capabilities if cap in handoff.get("required_capabilities", [])]
        if not owned:
            return None
        agent_status = [row for row in status_rows if row.get("capability") in owned]
        sub_result = {**supervisor_result, "handoffs": [handoff],
                      "tool_execution_status": agent_status}
        subroute = {**route, "required_capabilities": owned}
        if name == "after_sales":
            # Reconstruct only the explicit Order → Policy → After-sales
            # dependency chain; Product remains a separate, independently
            # rendered section in four-specialist runs.
            if owned == ["get_refund_status"] and "policy" not in handoffs:
                value = build_after_sales_business_result(sub_result, route=subroute, result_store=result_store)
                if value is None or value["business_results"]["after_sales"]["order_id"] != trusted.requested_order_id:
                    return None
                outputs.append((name, value))
                continue
            required_chain = ["order_logistics", "policy", "after_sales"]
            if not all(item in handoffs for item in required_chain):
                return None
            sub_result["handoffs"] = [handoffs[item] for item in required_chain]
            order_policy_after_caps = [cap for cap in capabilities if cap in {
                *handoffs["order_logistics"].get("required_capabilities", []),
                *handoffs["policy"].get("required_capabilities", []),
                *handoffs["after_sales"].get("required_capabilities", []),
            }]
            sub_result["tool_execution_status"] = [row for row in status_rows
                if row.get("capability") in order_policy_after_caps]
            subroute["required_capabilities"] = order_policy_after_caps
            value = build_after_sales_business_result(sub_result, route=subroute,
                result_store=result_store)
        else:
            value = builders[name](sub_result, subroute)
        if value is None:
            return None
        outputs.append((name, value))
    combined = combine_specialist_outputs(outputs, tool_execution_status=status_rows)
    # Missing delivery time is a missing fact, never proof of non-delivery or
    # eligibility. Reuse refreshed catalog/owner facts; ask one material question.
    if combined is not None and (is_return_guidance_query(query) or re.search(
            r"能退|可以退|能不能退|退货.*(?:条件|资格)|returnable|eligible", query, re.I)):
        by_name = dict(outputs)
        if {"order_logistics", "product", "policy"}.issubset(by_name):
            order = by_name["order_logistics"]["business_results"].get("get_order")
            items = by_name["order_logistics"]["business_results"].get("get_order_items")
            if order is not None and items is not None and items["items"] and not order["delivered_at"]:
                names = "、".join(item["name"] for item in items["items"])
                combined["answer"] = (f"已查到这笔订单的商品：{names}。是否符合退货条件，还需结合适用政策核实；"
                    "目前订单没有记录签收时间，不能据此判断未签收或直接确定退货资格。请问您何时签收的？")
    return combined
