"""Bounded, serializable M3.1 handoff contracts.

Only references and status summaries cross graph/checkpoint boundaries. Trusted
identity is passed separately by the server and never serialized into GraphState.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AgentName = Literal["supervisor", "policy", "order_logistics", "after_sales", "product"]
AgentStatus = Literal[
    "PENDING", "RUNNING", "COMPLETED", "STUB_COMPLETED", "NOT_IMPLEMENTED",
    "REQUIRES_CONFIRMATION", "FAILED",
]

KNOWN_CAPABILITIES = frozenset({
    "list_orders",
    "get_order", "get_order_items", "track_shipment", "get_refund_status",
    "create_refund_request", "create_return_request", "cancel_order", "search_products",
    "get_product_detail", "recommend_products", "check_inventory", "get_coupon",
    "get_warranty", "search_knowledge", "escalate_to_human",
})
CAPABILITY_AGENT = {
    "list_orders": "order_logistics",
    "get_order": "order_logistics", "get_order_items": "order_logistics", "track_shipment": "order_logistics",
    "get_refund_status": "after_sales", "create_refund_request": "after_sales",
    "create_return_request": "after_sales", "cancel_order": "after_sales", "escalate_to_human": "after_sales",
    "search_products": "product", "get_product_detail": "product", "recommend_products": "product",
    "check_inventory": "product", "get_coupon": "policy", "get_warranty": "policy",
    "search_knowledge": "policy",
}
WRITE_CAPABILITIES = frozenset({"create_refund_request", "create_return_request", "cancel_order"})
_REF = re.compile(r"^(?:run|session|message|evidence|fact|tool_result):[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_TASK_TITLES = {
    "Resolve requested live order or shipment capability",
    "Resolve requested live product capability",
    "Retrieve and explain requested policy evidence",
    "Resolve requested after-sales capability under confirmation controls",
}


class TrustedAgentContext(BaseModel):
    """Ephemeral server context, built only after API auth/Run ownership checks."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=64)
    authenticated: bool = True
    run_owner_verified: bool = True
    session_owner_verified: bool = True
    # Server-parsed target only. This is not authorization; every repository
    # query must still apply user_id as an owner predicate.
    requested_order_id: str | None = Field(default=None, min_length=5, max_length=64)
    order_id_error: Literal["ORDER_ID_AMBIGUOUS", "INVALID_ORDER_ID"] | None = None
    order_list_before: str | None = Field(default=None, max_length=64)

    @field_validator("requested_order_id")
    @classmethod
    def order_identifier_shape(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"ORD[-_][A-Z0-9_-]{1,48}", value):
            raise ValueError("order target must be a normalized order identifier")
        return value


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1, max_length=128)
    target_agent: Literal["policy", "order_logistics", "after_sales", "product"]
    task: str = Field(min_length=1, max_length=256)
    required_capabilities: list[str] = Field(min_length=1, max_length=8)
    context_refs: list[str] = Field(max_length=8)
    evidence_refs: list[str] = Field(max_length=16)
    status: AgentStatus = "PENDING"

    @field_validator("task")
    @classmethod
    def task_is_minimized(cls, value: str) -> str:
        if value not in _TASK_TITLES:
            raise ValueError("handoff task must use a minimized, non-user-content task label")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def known_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(value not in KNOWN_CAPABILITIES for value in values):
            raise ValueError("capabilities must be known and unique")
        return values

    @field_validator("context_refs", "evidence_refs")
    @classmethod
    def safe_references(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _REF.fullmatch(value) for value in values):
            raise ValueError("handoff references must be unique, typed identifiers")
        if any(len(value) > 160 for value in values):
            raise ValueError("handoff reference is too long")
        return values

    @model_validator(mode="after")
    def capabilities_match_agent(self):
        if any(CAPABILITY_AGENT.get(item) != self.target_agent for item in self.required_capabilities):
            raise ValueError("task capabilities do not belong to target agent")
        return self


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    result_id: str = Field(min_length=1, max_length=128)
    source_agent: Literal["policy", "order_logistics", "after_sales", "product"]
    status: Literal["COMPLETED", "STUB_COMPLETED", "NOT_IMPLEMENTED", "REQUIRES_CONFIRMATION", "FAILED"]
    fact_refs: list[str] = Field(default_factory=list, max_length=16)
    evidence_refs: list[str] = Field(default_factory=list, max_length=16)
    tool_result_refs: list[str] = Field(default_factory=list, max_length=16)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("fact_refs", "evidence_refs", "tool_result_refs")
    @classmethod
    def safe_result_references(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _REF.fullmatch(value) for value in values):
            raise ValueError("agent results may contain references, not arbitrary payloads")
        return values

    @field_validator("missing_information")
    @classmethod
    def safe_missing_codes(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Z0-9_]{1,64}", item) for item in values):
            raise ValueError("missing information must be represented by stable codes")
        return values

    @field_validator("error_code")
    @classmethod
    def safe_error_code(cls, value: str | None) -> str | None:
        if value is not None and not _ERROR_CODE.fullmatch(value):
            raise ValueError("agent error code must be a stable identifier")
        return value


class AgentHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handoff_id: str = Field(min_length=1, max_length=128)
    source_agent: AgentName
    target_agent: Literal["policy", "order_logistics", "after_sales", "product"]
    task: str = Field(min_length=1, max_length=256)
    required_capabilities: list[str] = Field(min_length=1, max_length=8)
    context_refs: list[str] = Field(max_length=8)
    evidence_refs: list[str] = Field(max_length=16)
    result: AgentResult | None = None
    status: AgentStatus
    error_code: str | None = Field(default=None, max_length=64)

    @field_validator("task")
    @classmethod
    def task_is_minimized(cls, value: str) -> str:
        if value not in _TASK_TITLES:
            raise ValueError("handoff task must use a minimized, non-user-content task label")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def known_capabilities(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(value not in KNOWN_CAPABILITIES for value in values):
            raise ValueError("capabilities must be known and unique")
        return values

    @field_validator("context_refs", "evidence_refs")
    @classmethod
    def safe_references(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _REF.fullmatch(value) for value in values):
            raise ValueError("handoff references must be unique, typed identifiers")
        return values

    @model_validator(mode="after")
    def status_matches_result(self):
        terminal_statuses = {"COMPLETED", "STUB_COMPLETED", "NOT_IMPLEMENTED", "REQUIRES_CONFIRMATION", "FAILED"}
        if self.status in terminal_statuses and self.result is None:
            raise ValueError("terminal handoffs require a structured result")
        if self.status in {"PENDING", "RUNNING"} and self.result is not None:
            raise ValueError("non-terminal handoffs cannot contain a result")
        if self.result is not None and self.result.status != self.status:
            raise ValueError("handoff status must match its result status")
        if self.result is not None and self.result.source_agent != self.target_agent:
            raise ValueError("handoff result must be produced by its target agent")
        if self.result is not None and self.error_code != self.result.error_code:
            raise ValueError("handoff error code must match its structured result")
        if any(CAPABILITY_AGENT.get(item) != self.target_agent for item in self.required_capabilities):
            raise ValueError("handoff capabilities do not belong to target agent")
        if self.status == "FAILED" and not self.error_code:
            raise ValueError("failed handoffs require an error code")
        if self.error_code is not None and not _ERROR_CODE.fullmatch(self.error_code):
            raise ValueError("handoff error code must be a stable identifier")
        return self
