"""Permission-gated escalation draft; this module never creates a ticket."""
from __future__ import annotations

import re
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EscalationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["DRAFT_ONLY"] = "DRAFT_ONLY"
    execution_status: Literal["NOT_EXECUTED"] = "NOT_EXECUTED"
    ticket_created: Literal[False] = False
    notification_sent: Literal[False] = False
    reason_code: str = Field(min_length=1, max_length=64)
    issue_category: Literal["ACCOUNT_ACCESS", "ORDER_STATUS", "RETURN_REFUND", "PRODUCT_QUALITY", "POLICY_QUESTION", "PRIVACY_SECURITY", "OTHER"] = "OTHER"
    order_ref: str | None = Field(default=None, max_length=64)
    summary_code: str = Field(min_length=1, max_length=64)
    user_description: str = Field(min_length=1, max_length=1000)
    issue_summary: str = Field(min_length=1, max_length=600)
    related_agents: list[Literal["order_logistics", "policy", "after_sales", "product"]] = Field(default_factory=list, max_length=4)
    verified_fact_refs: list[str] = Field(default_factory=list, max_length=12)
    unverified_claim_codes: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("reason_code", "summary_code")
    @classmethod
    def stable_codes_only(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", value):
            raise ValueError("escalation drafts accept stable codes only")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def typed_refs_only(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"evidence:[A-Za-z0-9._:-]{1,128}", item) for item in values):
            raise ValueError("escalation evidence must be typed references")
        return list(dict.fromkeys(values))

    def pending_action_arguments(self) -> dict:
        """Return a proposal payload. The separate owner-confirm API is still required."""
        return {"issue_category": self.issue_category, "order_id": self.order_ref,
            "user_description": self.user_description, "issue_summary": self.issue_summary,
            "reason_code": self.reason_code, "related_agents": self.related_agents,
            "verified_fact_refs": self.verified_fact_refs,
            "unverified_claim_codes": self.unverified_claim_codes,
            "evidence_references": self.evidence_refs}


def create_escalation_draft(*, permission_checker: Callable[[str, str], bool], trusted_user_id: str,
                            authenticated: bool,
                            owner_verified: bool, reason_code: str, order_ref: str | None,
                            summary_code: str, evidence_refs: list[str],
                            issue_category: str = "OTHER", user_description: str | None = None,
                            issue_summary: str | None = None, related_agents: list[str] | None = None,
                            verified_fact_refs: list[str] | None = None,
                            unverified_claim_codes: list[str] | None = None) -> EscalationDraft:
    """Build a non-persistent proposal only after an explicit server policy check."""
    if not isinstance(trusted_user_id, str) or not trusted_user_id or not callable(permission_checker) or \
            not authenticated or not owner_verified or \
            not permission_checker(trusted_user_id, "support.ticket.draft"):
        raise PermissionError("TOOL_NOT_AUTHORIZED")
    if order_ref is not None and not re.fullmatch(r"ORD[-_][A-Z0-9_-]{1,48}", order_ref):
        raise ValueError("ORDER_ID_INVALID")
    return EscalationDraft(reason_code=reason_code, order_ref=order_ref,
        summary_code=summary_code, evidence_refs=evidence_refs, issue_category=issue_category,
        user_description=user_description or summary_code, issue_summary=issue_summary or summary_code,
        related_agents=related_agents or [], verified_fact_refs=verified_fact_refs or [],
        unverified_claim_codes=unverified_claim_codes or [])
