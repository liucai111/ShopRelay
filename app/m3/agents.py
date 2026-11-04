"""M3.1 expert Agent interfaces and explicitly non-production implementations."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Protocol

from .contracts import AgentResult, AgentTask, TrustedAgentContext, WRITE_CAPABILITIES
from .order_tools import AgentExecutionContext, OrderReadRepository, ToolExecutionError
from .policy_tools import PolicyAgent, PolicyReadService
from .product_tools import ProductFilters, ProductReadRepository, parse_product_request_many
from .after_sales_tools import evaluate_policy_conditions


class SpecialistAgent(Protocol):
    name: str

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult: ...


class UnimplementedAgent:
    """Fail-closed placeholder. It never claims a business tool ran."""
    name: str

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        del trusted
        del context
        return AgentResult(result_id=f"{task.task_id}:result", source_agent=self.name,
            status="NOT_IMPLEMENTED", missing_information=["SPECIALIST_SERVICE_NOT_IMPLEMENTED"],
            error_code="AGENT_NOT_IMPLEMENTED")


class OrderLogisticsAgent:
    name = "order_logistics"

    def __init__(self, repository: OrderReadRepository | None = None) -> None:
        self.repository = repository

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        result_id = f"{task.task_id}:result"
        if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code="AGENT_CONTEXT_INVALID")
        if self.repository is None or context is None:
            return AgentResult(result_id=result_id, source_agent=self.name, status="NOT_IMPLEMENTED",
                missing_information=["ORDER_TOOL_CONTEXT_UNAVAILABLE"], error_code="AGENT_NOT_IMPLEMENTED")
        if trusted.order_id_error:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code=trusted.order_id_error)
        listing = task.required_capabilities == ["list_orders"]
        if not trusted.requested_order_id and not listing:
            # A clarification is a completed safe outcome; no tool is invoked.
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                missing_information=["ORDER_ID_REQUIRED"])
        references: list[str] = []
        try:
            for capability in task.required_capabilities:
                operation = getattr(self.repository, capability, None)
                if not callable(operation):
                    return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                        error_code="TOOL_NOT_IMPLEMENTED")
                references.append(context.execute_read(capability, trusted.order_list_before or "" if listing else trusted.requested_order_id,
                    trusted.user_id, operation))
        except ToolExecutionError as exc:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code=exc.code)
        return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
            tool_result_refs=references)


class AfterSalesAgent(UnimplementedAgent):
    name = "after_sales"

    def __init__(self, repository=None) -> None:
        self.repository = repository

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        if WRITE_CAPABILITIES.intersection(task.required_capabilities):
            del trusted
            del context
            return AgentResult(result_id=f"{task.task_id}:result", source_agent=self.name,
                status="REQUIRES_CONFIRMATION", missing_information=["CONFIRMATION_FLOW_NOT_IMPLEMENTED"],
                error_code="ACTION_CONFIRMATION_REQUIRED")
        result_id = f"{task.task_id}:result"
        if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code="AGENT_CONTEXT_INVALID")
        if task.required_capabilities == ["escalate_to_human"]:
            if context is None:
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                    error_code="PROPOSAL_CONTEXT_UNAVAILABLE")
            if trusted.order_id_error:
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                    error_code=trusted.order_id_error)
            # An order is linked only when this Run already has an owner-scoped
            # get_order result matching the server-parsed requested identifier.
            order_ref = None
            for ref in task.context_refs:
                payload = context.result_store.get(ref) if ref.startswith("tool_result:") else None
                if (isinstance(payload, dict) and payload.get("source") == "business_database" and
                        payload.get("order_id") == trusted.requested_order_id and
                        trusted.requested_order_id):
                    order_ref = trusted.requested_order_id
                    break
            try:
                proposal_ref = context.create_escalation_proposal(trusted=trusted,
                    reason_code="HUMAN_REVIEW_REQUESTED", order_ref=order_ref,
                    evidence_refs=[ref for ref in task.evidence_refs if ref.startswith("evidence:")],
                    related_agents=["after_sales"])
            except ToolExecutionError as exc:
                return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                    error_code=exc.code)
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                tool_result_refs=[proposal_ref])
        if self.repository is None or context is None:
            return super().execute(task, trusted, context)
        if task.required_capabilities != ["get_refund_status"]:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code="TOOL_NOT_ALLOWED")
        if trusted.order_id_error:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code=trusted.order_id_error)
        if not trusted.requested_order_id:
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                missing_information=["ORDER_ID_REQUIRED"])
        try:
            status_ref = context.execute_read("get_refund_status", trusted.requested_order_id,
                trusted.user_id, self.repository.get_refund_status)
        except ToolExecutionError as exc:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED", error_code=exc.code)
        refs = task.context_refs
        upstream_refs = [ref for ref in refs if ref.startswith("tool_result:")]
        order_ref = next((ref for ref in upstream_refs if (context.result_store.get(ref) or {}).get("source") == "business_database"), None)
        policy_ref = next((ref for ref in upstream_refs if (context.result_store.get(ref) or {}).get("source") == "shoprelay_policy_retrieval"), None)
        order_payload = context.result_store.get(order_ref, {}) if order_ref else {}
        order = order_payload if order_payload.get("order_id") == trusted.requested_order_id else {}
        status_payload = context.result_store.get(status_ref, {})
        policy_payload = context.result_store.get(policy_ref, {}) if policy_ref else {}
        evaluation = evaluate_policy_conditions(order, status_payload, policy_payload, query=context.user_query)
        eval_ref = "tool_result:" + hashlib.sha256(
            f"{context.run_id}|after_sales_evaluation|{task.task_id}".encode()).hexdigest()[:48]
        context.result_store[eval_ref] = evaluation
        evidence_refs = [ref for ref in task.evidence_refs if ref.startswith("evidence:")]
        return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
            fact_refs=[ref for ref in upstream_refs if ref == order_ref],
            evidence_refs=evidence_refs,
            tool_result_refs=[status_ref, eval_ref],
            missing_information=sorted({code for condition in evaluation["conditions"]
                for code in condition["missing_information"]}))


class ProductAgent:
    name = "product"

    def __init__(self, repository: ProductReadRepository | None = None) -> None:
        self.repository = repository

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        result_id = f"{task.task_id}:result"
        if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code="AGENT_CONTEXT_INVALID")
        if self.repository is None or context is None:
            return AgentResult(result_id=result_id, source_agent=self.name, status="NOT_IMPLEMENTED",
                missing_information=["PRODUCT_TOOL_CONTEXT_UNAVAILABLE"], error_code="AGENT_NOT_IMPLEMENTED")
        bind_observability = getattr(self.repository, "set_execution_context", None)
        if callable(bind_observability):
            bind_observability(context)
        allowed = {"search_products", "get_product_detail", "recommend_products", "check_inventory"}
        if not task.required_capabilities or not set(task.required_capabilities).issubset(allowed):
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED",
                error_code="TOOL_NOT_ALLOWED")
        try:
            explicit_ids, filters, wants_stock = parse_product_request_many(context.user_query)
            comparison_request = bool(re.search(
                r"比较|对比|哪.{0,12}(?:更轻|更重|较轻|较重|更贵|更便宜)|谁更?(?:轻|重|贵|便宜)|"
                r"重量|价格.{0,8}(?:比较|对比)|lighter|heavier|lightest|heaviest|cheaper|more expensive|"
                r"costs (?:less|more)|compare|comparison", context.user_query, re.I))
            explicit_comparison_ids = explicit_ids if len(explicit_ids) == 2 and comparison_request else []
            if len(explicit_ids) > 1 and not explicit_comparison_ids:
                return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                    missing_information=["PRODUCT_ID_AMBIGUOUS"])
            product_id = explicit_ids[0] if len(explicit_ids) == 1 else None
            product_ids = list(explicit_comparison_ids)
            resolve_many = getattr(self.repository, "resolve_product_references", None)
            if not explicit_ids and callable(resolve_many):
                product_ids = resolve_many(context.user_query)
                if len(product_ids) > 1 and not comparison_request:
                    return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                        missing_information=["PRODUCT_ID_AMBIGUOUS"])
            normalize_filters = getattr(self.repository, "resolve_product_filters", None)
            if callable(normalize_filters) and not explicit_comparison_ids:
                filters = normalize_filters(context.user_query, filters)
            if product_id is None and not explicit_ids:
                resolver = getattr(self.repository, "resolve_product_reference", None)
                if callable(resolver):
                    product_id = resolver(context.user_query)
            capabilities = set(task.required_capabilities)
            needs_id = "get_product_detail" in capabilities or (
                "check_inventory" in task.required_capabilities and
                not set(task.required_capabilities) & {"search_products", "recommend_products"})
            # In an Order → Product task, an owned order-items result may provide
            # the only safe product identifier. The Agent may consume that typed
            # reference, but never a product ID invented by another Agent's prose.
            if needs_id and product_id is None:
                upstream_ids = {
                    str(item.get("product_id"))
                    for ref in task.context_refs
                    if isinstance((row := context.result_store.get(ref)), dict)
                    and row.get("source") == "business_database"
                    for item in row.get("items", [])
                    if isinstance(item, dict) and isinstance(item.get("product_id"), str)
                }
                if len(upstream_ids) == 1:
                    product_id = next(iter(upstream_ids))
                elif len(upstream_ids) > 1:
                    if comparison_request and capabilities == {"get_product_detail"}:
                        # The request compares all owned order lines. Each ID is
                        # obtained from a typed preceding read, not Agent prose.
                        product_ids = sorted(upstream_ids)
                    else:
                        return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                            missing_information=["PRODUCT_ID_AMBIGUOUS"])
            if needs_id and not product_id and not product_ids and not capabilities.intersection(
                    {"search_products", "recommend_products"}):
                return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                    missing_information=["PRODUCT_ID_REQUIRED"])
            refs_by_capability: dict[str, list[str]] = {capability: [] for capability in capabilities}
            search_result = None
            details_truncated = False
            if "search_products" in capabilities and not explicit_comparison_ids:
                search_ref = context.execute_product_read("search_products",
                    {"keyword": filters.keyword, "category": filters.category,
                     "max_price_cents": filters.max_price_cents, "limit": filters.limit},
                    lambda: self.repository.search_products(filters))
                refs_by_capability["search_products"].append(search_ref)
                search_result = context.result_store[search_ref]
            if "recommend_products" in capabilities:
                recommend_ref = context.execute_product_read("recommend_products",
                    {"keyword": filters.keyword, "category": filters.category,
                     "max_price_cents": filters.max_price_cents,
                     "require_inventory": wants_stock, "limit": filters.limit},
                    lambda: self.repository.recommend_products(filters, require_inventory=wants_stock))
                refs_by_capability["recommend_products"].append(recommend_ref)
            if "get_product_detail" in capabilities:
                if search_result is not None:
                    candidate_ids = [row.get("product_id") for row in search_result.get("products", [])
                        if isinstance(row, dict) and isinstance(row.get("product_id"), str)]
                    candidate_ids = list(dict.fromkeys(candidate_ids))
                    if not candidate_ids:
                        # Empty search is evidence. Do not invent a product ID or
                        # call the detail tool with an absent reference.
                        ordered_refs = [*refs_by_capability.get("get_product_detail", []),
                            *refs_by_capability.get("search_products", [])]
                        return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                            tool_result_refs=ordered_refs, missing_information=["PRODUCT_CANDIDATES_NOT_FOUND"])
                else:
                    candidate_ids = list(dict.fromkeys(product_ids or ([product_id] if product_id else [])))
                allowance = getattr(getattr(self.repository, "store", None), "allowance", None)
                if search_result is not None and allowance is not None and isinstance(
                        getattr(allowance, "maximum", None), int) and isinstance(
                        getattr(allowance, "calls", None), int):
                    remaining = max(0, allowance.maximum - allowance.calls)
                    allowed_ids = candidate_ids[:remaining]
                    details_truncated = len(allowed_ids) < len(candidate_ids)
                    candidate_ids = allowed_ids
                else:
                    details_truncated = False
                for target_id in candidate_ids:
                    detail_ref = context.execute_product_read("get_product_detail", {"product_id": target_id},
                        lambda target_id=target_id: self.repository.get_product_detail(target_id))
                    refs_by_capability["get_product_detail"].append(detail_ref)
            if "check_inventory" in capabilities:
                inventory_ref = context.execute_product_read("check_inventory",
                    {"product_id": product_id, "category": filters.category,
                     "keyword": filters.keyword, "max_price_cents": filters.max_price_cents},
                    lambda: self.repository.check_inventory(product_id, filters))
                refs_by_capability["check_inventory"].append(inventory_ref)
            # The evidence assembler consumes references in capability order.
            # Search runs first to discover candidates, but its reference is
            # placed after detail references for the existing typed contract.
            refs = [ref for capability in sorted(refs_by_capability)
                for ref in refs_by_capability[capability]]
            search_truncated = bool(search_result and search_result.get("truncated"))
            missing_information = (["PRODUCT_CANDIDATES_TRUNCATED"]
                if search_result is not None and (details_truncated or search_truncated) else [])
            return AgentResult(result_id=result_id, source_agent=self.name, status="COMPLETED",
                tool_result_refs=refs, missing_information=missing_information)
        except ToolExecutionError as exc:
            return AgentResult(result_id=result_id, source_agent=self.name, status="FAILED", error_code=exc.code)


@dataclass(frozen=True)
class AgentRegistry:
    policy: SpecialistAgent
    order_logistics: SpecialistAgent
    after_sales: SpecialistAgent
    product: SpecialistAgent

    def as_mapping(self) -> Mapping[str, SpecialistAgent]:
        return {
            "policy": self.policy,
            "order_logistics": self.order_logistics,
            "after_sales": self.after_sales,
            "product": self.product,
        }


def default_agent_registry(*, order_repository: OrderReadRepository | None = None,
                           product_repository: ProductReadRepository | None = None,
                           policy_service: PolicyReadService | None = None,
                           after_sales_repository=None, coupon_repository=None) -> AgentRegistry:
    """Build specialists; only explicitly injected business repositories are live."""
    return AgentRegistry(policy=PolicyAgent(policy_service, coupon_repository=coupon_repository),
        order_logistics=OrderLogisticsAgent(order_repository),
        after_sales=AfterSalesAgent(after_sales_repository), product=ProductAgent(product_repository))


class DeterministicStubAgent:
    """Test-only deterministic response; results are marked STUB_COMPLETED."""
    def __init__(self, name: str) -> None:
        if name not in {"policy", "order_logistics", "after_sales", "product"}:
            raise ValueError("unknown specialist agent")
        self.name = name
        self.calls: list[str] = []

    def execute(self, task: AgentTask, trusted: TrustedAgentContext,
                context: AgentExecutionContext | None = None) -> AgentResult:
        del context
        if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
            return AgentResult(result_id=f"{task.task_id}:result", source_agent=self.name,
                status="FAILED", error_code="AGENT_CONTEXT_INVALID")
        self.calls.append(task.task_id)
        return AgentResult(result_id=f"{task.task_id}:result", source_agent=self.name,
            status="STUB_COMPLETED")


def deterministic_stub_registry() -> AgentRegistry:
    """Build the deterministic stubs used only by M3.1 tests and probes."""
    return AgentRegistry(policy=DeterministicStubAgent("policy"),
        order_logistics=DeterministicStubAgent("order_logistics"),
        after_sales=DeterministicStubAgent("after_sales"),
        product=DeterministicStubAgent("product"))
