from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from app.m1.api import CreatePendingActionInput, EscalationActionArguments, create_app
from app.m3.escalation import create_escalation_draft
from app.m4.actions import SERVER_PERMISSION_GRANTS, WRITE_POLICIES, server_permission_granted
from app.skills.registry import bind_skill, validate_skill_execution
from app.tools.catalog import get_tool_contract


def test_ticket_schema_is_strict_and_bound_to_registered_fields():
    args = EscalationActionArguments(issue_category="RETURN_REFUND", order_id="ORD-123",
        user_description="用户称退款申请一直未更新", issue_summary="需要核实退款申请状态",
        reason_code="STATUS_CONFLICT", related_agents=["after_sales", "policy"],
        verified_fact_refs=["fact:refund-status-1"], unverified_claim_codes=["USER_CLAIM_REFUND_PENDING"],
        evidence_references=["policy:refund-v2"])
    assert args.verified_fact_refs == ["fact:refund-status-1"]
    with pytest.raises(ValidationError):
        EscalationActionArguments(issue_category="OTHER", user_description="x", issue_summary="x",
            reason_code="OTHER", owner_id="u_other")
    with pytest.raises(ValidationError):
        EscalationActionArguments(issue_category="OTHER", user_description="x", issue_summary="x",
            reason_code="OTHER", evidence_references=["../private"])
    payload = CreatePendingActionInput(tool_id="escalate_to_human", session_id="s", run_id="r",
        target_resource_id="ORD-123", arguments={"issue_category": "RETURN_REFUND", "order_id": "ORD-123",
            "user_description": "请核实", "issue_summary": "退款状态待核实", "reason_code": "STATUS_CONFLICT"},
        idempotency_key="ticket-key")
    assert payload.arguments.order_id == "ORD-123"
    with pytest.raises(ValidationError):
        CreatePendingActionInput(tool_id="escalate_to_human", session_id="s", run_id="r",
            target_resource_id="ORD-123", arguments={"issue_category": "RETURN_REFUND", "order_id": "ORD-123",
                "user_description": "请核实", "issue_summary": "退款状态待核实", "reason_code": "STATUS_CONFLICT",
                "priority": "HIGH"}, idempotency_key="ticket-key")


def test_human_escalation_skill_only_proposes_and_does_not_execute_ticket_tool():
    route = {"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]}
    bound = bind_skill("请联系人工客服处理", route)
    assert bound["skill_id"] == "human_escalation"
    assert "escalate_to_human" not in bound["skill_allowed_tools"]
    assert bound["skill_proposable_tools"] == ["escalate_to_human"]
    validate_skill_execution({**route, **bound})
    with pytest.raises(ValueError, match="SKILL_TOOL_SCOPE_VIOLATION"):
        validate_skill_execution({**route, "skill_id": "human_escalation", "skill_version": "1.0.0"})
    draft = create_escalation_draft(permission_checker=lambda _u, c: c == "support.ticket.draft",
        trusted_user_id="u_demo", authenticated=True, owner_verified=True,
        reason_code="STATUS_CONFLICT", order_ref="ORD-123", summary_code="NEEDS_REVIEW",
        issue_category="RETURN_REFUND", user_description="用户称已提交申请",
        issue_summary="后台状态需核验", verified_fact_refs=["fact:run-order-1"],
        unverified_claim_codes=["USER_CLAIM_SUBMITTED"], evidence_refs=["evidence:policy-1"])
    action_args = draft.pending_action_arguments()
    assert action_args["verified_fact_refs"] != action_args["unverified_claim_codes"]
    assert draft.ticket_created is False and draft.notification_sent is False


def test_ticket_executor_policy_is_test_only_and_default_switch_is_closed(tmp_path):
    policy = WRITE_POLICIES["escalate_to_human"]
    assert policy.execution_mode == "ISOLATED_TICKET_TRANSACTION"
    assert policy.confirmation_required and policy.audit_required
    assert policy.allowed_environment == ("shoprelay_m11_test",)
    assert policy.risk_level == "WRITE_LOW_RISK"
    assert "support.ticket.create" in SERVER_PERMISSION_GRANTS["u_demo"]
    assert "support.ticket.create" in SERVER_PERMISSION_GRANTS["u_other"]
    assert server_permission_granted("u_demo", "escalate_to_human", "shoprelay_m11_test")
    assert server_permission_granted("u_other", "escalate_to_human", "shoprelay_m11_test")
    assert not server_permission_granted("u_demo", "escalate_to_human", "production")
    contract = get_tool_contract("escalate_to_human")
    assert contract.mcp_allowed is False and contract.confirmation_required
    engine = create_engine(f"sqlite:///{(tmp_path / 'ticket-gate.sqlite').as_posix()}")
    try:
        app = create_app(database_url=f"sqlite:///{(tmp_path / 'ticket-gate.sqlite').as_posix()}",
            engine=engine, action_writes_enabled=False, ticket_actions_enabled=True,
            orchestrator_mode="legacy")
        assert app.state.ticket_actions_enabled is False
    finally:
        engine.dispose()
