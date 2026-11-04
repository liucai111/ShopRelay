"""Pure payload/schema checks; these tests do not execute a Graph or API endpoint."""
import pytest

from app.m1.api import CreatePendingActionInput
from app.m3.after_sales_tools import build_after_sales_business_result
from app.m3.escalation import EscalationDraft, create_escalation_draft
from app.orchestration.runtime import _agent_business_payload


def _supervisor_result():
    draft = create_escalation_draft(
        permission_checker=lambda owner, permission: owner == "owner-1" and permission == "support.ticket.draft",
        trusted_user_id="owner-1", authenticated=True, owner_verified=True,
        reason_code="HUMAN_REVIEW_REQUESTED", order_ref="ORD-SYN-101",
        summary_code="USER_REQUESTS_HUMAN", user_description="Please contact me about this issue.",
        issue_summary="Human assistance requested.", related_agents=["after_sales"],
        verified_fact_refs=["tool:run-1:order"], unverified_claim_codes=["CLAIM_NOT_VERIFIED"],
        evidence_refs=["evidence:policy-1"],
    )
    serialized = draft.model_dump(mode="json")
    payload = {"source": "shoprelay_escalation_draft", "draft": serialized,
               "pending_action_arguments": draft.pending_action_arguments()}
    reference = "tool_result:run-1:escalation-draft"
    return reference, payload, {
        "supervisor_status": "COMPLETED",
        "handoffs": [{"handoff_id": "handoff-1", "source_agent": "supervisor",
            "target_agent": "after_sales", "status": "COMPLETED",
            "result": {"source_agent": "after_sales", "status": "COMPLETED",
                       "tool_result_refs": [reference]}}],
        "tool_execution_status": [{"capability": "escalate_to_human", "status": "DRAFT_ONLY",
                                    "executed": False}],
    }


def test_escalation_projection_keeps_draft_only_and_serializes_api_contract_without_calling_api():
    ref, payload, result = _supervisor_result()
    business = build_after_sales_business_result(
        result, route={"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]},
        result_store={ref: payload},
    )
    assert business["business_results"]["escalation_proposal"]["status"] == "DRAFT_ONLY"
    assert business["business_results"]["escalation_proposal"]["execution_status"] == "NOT_EXECUTED"
    assert business["business_results"]["escalation_proposal"]["ticket_created"] is False
    assert business["business_results"]["escalation_proposal"]["notification_sent"] is False
    assert business["tool_references"][0]["result_ref"] == ref

    envelope = _agent_business_payload({**result, **business},
        {"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]}, "trace-1")
    assert envelope["trace_id"] == "trace-1"
    assert envelope["handoffs"][0]["target_agent"] == "after_sales"
    assert envelope["business_results"]["escalation_proposal"]["status"] == "DRAFT_ONLY"
    assert envelope["tool_execution_status"][0]["executed"] is False

    api_payload = CreatePendingActionInput.model_validate({
        "tool_id": "escalate_to_human", "session_id": "session-1", "run_id": "run-1",
        "target_resource_id": "ORD-SYN-101",
        "arguments": business["business_results"]["escalation_proposal"]["pending_action_arguments"],
        "idempotency_key": "proposal-1",
    })
    assert api_payload.tool_id == "escalate_to_human"
    assert api_payload.arguments.order_id == "ORD-SYN-101"


def test_escalation_projection_fails_closed_for_missing_or_mis_scoped_draft():
    ref, payload, result = _supervisor_result()
    route = {"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]}
    assert build_after_sales_business_result(result, route=route, result_store={}) is None
    wrong_agent = {**result, "handoffs": [{**result["handoffs"][0], "target_agent": "policy"}]}
    assert build_after_sales_business_result(wrong_agent, route=route, result_store={ref: payload}) is None
    unsafe = {**payload, "draft": {**payload["draft"], "ticket_created": True}}
    assert build_after_sales_business_result(result, route=route, result_store={ref: unsafe}) is None


def test_escalation_draft_requires_server_permission_and_strict_typed_references():
    kwargs = dict(trusted_user_id="owner-1", authenticated=True, owner_verified=True,
        reason_code="HUMAN_REVIEW_REQUESTED", order_ref=None, summary_code="NEED_HUMAN",
        evidence_refs=["evidence:policy-1"])
    with pytest.raises(PermissionError, match="TOOL_NOT_AUTHORIZED"):
        create_escalation_draft(permission_checker=lambda *_: False, **kwargs)
    with pytest.raises(ValueError):
        EscalationDraft(reason_code="HUMAN_REVIEW_REQUESTED", summary_code="NEED_HUMAN",
                        user_description="Need help", issue_summary="Help", evidence_refs=["../secret"])
