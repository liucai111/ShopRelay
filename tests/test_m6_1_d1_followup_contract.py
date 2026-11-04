"""Fail-closed, in-memory regression for proposal-only escalation execution."""

from app.m3.order_tools import AgentExecutionContext, ToolExecutionError


def test_escalation_capability_is_rejected_before_repository_call():
    context = AgentExecutionContext(
        result_store={}, sink=None, run_id="synthetic-run", trace_id="synthetic-trace",
        parent_span_id=None,
    )

    def must_not_run(_order_id, _owner_id):
        raise AssertionError("proposal capability must not reach a business repository")

    try:
        context.execute_read("escalate_to_human", "ORD-SYN-001", "synthetic-user", must_not_run)
    except ToolExecutionError as exc:
        assert exc.code == "TOOL_NOT_ALLOWED"
    else:
        raise AssertionError("expected proposal-only capability to fail closed")
