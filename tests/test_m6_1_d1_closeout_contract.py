"""Pure in-memory contract checks for the M6.1-D1 closeout audit.

This module does not import the FastAPI application or open a database/runtime.
"""
from __future__ import annotations

import re

from app.m3.contracts import CAPABILITY_AGENT
from app.m3.escalation import create_escalation_draft
from app.skills.registry import NON_EXECUTABLE_CAPABILITIES, SKILL_REGISTRY
from app.tools.catalog import get_tool_contract


def test_escalation_catalog_matches_proposal_adapter_input_contract():
    contract = get_tool_contract("escalate_to_human")
    schema = contract.input_schema
    props = schema["properties"]
    required = set(schema["required"])

    assert contract.agent == "after_sales"
    assert CAPABILITY_AGENT["escalate_to_human"] == "after_sales"
    assert "escalate_to_human" in NON_EXECUTABLE_CAPABILITIES
    assert "escalate_to_human" in SKILL_REGISTRY["human_escalation"].proposal_tools
    assert "escalate_to_human" not in SKILL_REGISTRY["human_escalation"].allowed_tools

    assert required == {"issue_category", "user_description", "issue_summary", "reason_code"}
    assert set(props) == required | {
        "order_id", "related_agents", "verified_fact_refs",
        "unverified_claim_codes", "evidence_references",
    }
    assert props["order_id"]["anyOf"][1] == {"type": "null"}

    related = props["related_agents"]
    assert related["maxItems"] == 4
    assert set(related["items"]["enum"]) == {"order_logistics", "policy", "after_sales", "product"}
    for field in ("verified_fact_refs", "evidence_references"):
        assert props[field]["maxItems"] == 12
        assert re.fullmatch(props[field]["items"]["pattern"], "fact:run-1")
        assert not re.fullmatch(props[field]["items"]["pattern"], "../private")
    assert props["unverified_claim_codes"]["maxItems"] == 12
    assert re.fullmatch(props["unverified_claim_codes"]["items"]["pattern"], "USER_CLAIM_UNVERIFIED")

    assert contract.confirmation_required is True
    assert contract.mcp_allowed is False
    assert "TICKET_REFERENCE_UNVERIFIED" in contract.error_states
    assert "ACTION_CONTEXT_MISMATCH" in contract.error_states


def test_escalation_adapter_payload_is_only_a_nonpersistent_proposal():
    draft = create_escalation_draft(
        permission_checker=lambda _user, capability: capability == "support.ticket.draft",
        trusted_user_id="synthetic-user",
        authenticated=True,
        owner_verified=True,
        reason_code="STATUS_CONFLICT",
        order_ref="ORD-SYN-001",
        summary_code="NEEDS_REVIEW",
        issue_category="RETURN_REFUND",
        user_description="Synthetic user says the status is unclear.",
        issue_summary="Check the recorded after-sales status.",
        related_agents=["after_sales", "policy"],
        verified_fact_refs=["fact:run-1"],
        unverified_claim_codes=["USER_CLAIM_STATUS"],
        evidence_refs=["evidence:policy-1"],
    )

    payload = draft.pending_action_arguments()
    catalog_fields = set(get_tool_contract("escalate_to_human").input_schema["properties"])
    assert set(payload) <= catalog_fields
    assert payload["order_id"] == "ORD-SYN-001"
    assert payload["related_agents"] == ["after_sales", "policy"]
    assert payload["verified_fact_refs"] == ["fact:run-1"]
    assert payload["unverified_claim_codes"] == ["USER_CLAIM_STATUS"]
    assert payload["evidence_references"] == ["evidence:policy-1"]
    assert draft.status == "DRAFT_ONLY"
    assert draft.execution_status == "NOT_EXECUTED"
    assert draft.ticket_created is False
    assert draft.notification_sent is False
