"""Offline-only M6.1-D2 loader and escalation proposal wiring tests."""
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, Order, Shipment, User
from app.m3.agents import default_agent_registry
from app.m3.after_sales_tools import build_after_sales_business_result
from app.m3.contracts import TrustedAgentContext
from app.m3.escalation import EscalationDraft
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.order_tools import SqlAlchemyOrderReadRepository, AgentExecutionContext, ToolExecutionError
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.runtime import _agent_business_payload
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state
from eval.m6_1.shipment_fixture_loader import (ShipmentFixtureError, cleanup_shipment_fixtures,
    load_shipment_fixtures)


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine, tables=[User.__table__, Order.__table__, Shipment.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([User(user_id="user-a", display_name="A"), User(user_id="user-b", display_name="B")])
        session.flush()
        session.add_all([
            Order(order_id="ORD-SYN-001", user_id="user-a", status="PAID", paid_at=None,
                  delivered_at=None, paid_cents=100, version=1),
            Order(order_id="ORD-SYN-002", user_id="user-b", status="PAID", paid_at=None,
                  delivered_at=None, paid_cents=100, version=1),
        ])
        session.commit()
        yield session, factory
    engine.dispose()


def shipment_fixture(**overrides):
    return {"shipment_id": "syn-shipment-1", "order_id": "ORD-SYN-001",
            "status": "IN_TRANSIT", "observed_at": "2026-10-25T08:00:00+08:00", **overrides}


def test_shipment_fixture_loader_persists_owner_scoped_readable_record_and_exact_cleanup(db):
    session, factory = db
    receipt = load_shipment_fixtures(session, [shipment_fixture()], owner_id="user-a", probe_id="probe1")
    session.commit()
    assert receipt.shipment_ids == ("EV-probe1-syn-shipment-1",)
    result = SqlAlchemyOrderReadRepository(factory).track_shipment("ORD-SYN-001", "user-a")
    assert result["availability"] == "AVAILABLE"
    assert result["records"] == [{"status": "IN_TRANSIT", "observed_at": "2026-10-25T00:00:00Z"}]
    with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
        SqlAlchemyOrderReadRepository(factory).track_shipment("ORD-SYN-001", "user-b")
    assert cleanup_shipment_fixtures(session, receipt) == 1
    session.commit()
    assert session.execute(select(Shipment.shipment_id)).scalars().all() == []
    assert cleanup_shipment_fixtures(session, receipt) == 0


@pytest.mark.parametrize("row,code", [
    (shipment_fixture(status="DELIVERED"), "SHIPMENT_STATUS_UNSUPPORTED"),
    ({"shipment_id": "syn-shipment-1", "order_id": "ORD-SYN-001", "status": "IN_TRANSIT"},
     "FIXTURE_FIELDS_INVALID"),
    (shipment_fixture(observed_at="2026-10-25T08:00:00"), "OBSERVED_AT_REQUIRES_TIMEZONE"),
])
def test_shipment_fixture_loader_rejects_unsupported_or_incomplete_fixture(db, row, code):
    session, _ = db
    with pytest.raises(ShipmentFixtureError, match=code):
        load_shipment_fixtures(session, [row], owner_id="user-a", probe_id="probe2")


def test_shipment_fixture_loader_rejects_wrong_owner_duplicate_and_existing_id(db):
    session, _ = db
    with pytest.raises(ShipmentFixtureError, match="ORDER_OWNER_OR_ASSOCIATION_MISMATCH"):
        load_shipment_fixtures(session, [shipment_fixture(order_id="ORD-SYN-002")],
                               owner_id="user-a", probe_id="probe3")
    with pytest.raises(ShipmentFixtureError, match="ORDER_OWNER_OR_ASSOCIATION_MISMATCH"):
        load_shipment_fixtures(session, [shipment_fixture(order_id="ORD-MISSING-001")],
                               owner_id="user-a", probe_id="probe3b")
    with pytest.raises(ShipmentFixtureError, match="DUPLICATE_FIXTURE_ID"):
        load_shipment_fixtures(session, [shipment_fixture(), shipment_fixture()],
                               owner_id="user-a", probe_id="probe4")
    session.add(Shipment(shipment_id="EV-probe5-syn-shipment-1", order_id="ORD-SYN-001",
                         status="IN_TRANSIT", observed_at="2026-10-25T00:00:00Z"))
    session.flush()
    with pytest.raises(ShipmentFixtureError, match="SHIPMENT_ID_ALREADY_EXISTS"):
        load_shipment_fixtures(session, [shipment_fixture()], owner_id="user-a", probe_id="probe5")


def test_shipment_cleanup_cannot_delete_outside_receipt_scope(db):
    session, _ = db
    from eval.m6_1.shipment_fixture_loader import ShipmentLoadReceipt
    session.add(Shipment(shipment_id="EV-other-syn-shipment", order_id="ORD-SYN-001",
                         status="IN_TRANSIT", observed_at="2026-10-25T00:00:00Z"))
    session.flush()
    receipt = ShipmentLoadReceipt("probe6", "user-a", ("EV-other-syn-shipment",), ("ORD-SYN-001",))
    with pytest.raises(ShipmentFixtureError, match="CLEANUP_ID_OUTSIDE_PROBE_SCOPE"):
        cleanup_shipment_fixtures(session, receipt)
    assert session.get(Shipment, "EV-other-syn-shipment") is not None
    from eval.m6_1.shipment_fixture_loader import ShipmentLoadReceipt
    wrong_owner_receipt = ShipmentLoadReceipt("other", "user-b", ("EV-other-syn-shipment",), ("ORD-SYN-001",))
    with pytest.raises(ShipmentFixtureError, match="CLEANUP_OWNER_MISMATCH"):
        cleanup_shipment_fixtures(session, wrong_owner_receipt)
    assert session.get(Shipment, "EV-other-syn-shipment") is not None


def _escalation_context(*, permission_checker):
    trusted = TrustedAgentContext(user_id="user-a", session_id="session-a", run_id="run-a",
        trace_id="trace-a", authenticated=True, run_owner_verified=True, session_owner_verified=True)
    events = []
    context = GraphRuntimeContext(query="I want a human agent.", trusted_agent_context=trusted,
        m3_supervisor_enabled=True, tool_result_store={}, tool_execution_records=[],
        observability_sink=InMemoryObservabilitySink(), escalation_permission_checker=permission_checker,
        emit_event=lambda kind, payload, key: events.append((kind, payload, key)))
    state = graph_input_state(GraphInput(run_id="run-a", session_id="session-a", trace_id="trace-a",
                                         query="I want a human agent."))
    state["strategy"] = "AGENT"
    state["route"] = {"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]}
    return trusted, context, state, events


def test_after_sales_graph_builds_structured_draft_only_reference_without_side_effects():
    _, context, state, events = _escalation_context(
        permission_checker=lambda owner, permission: owner == "user-a" and permission == "support.ticket.draft")
    context.route_payload = state["route"]
    sink = InMemoryObservabilitySink()
    graph = build_supervisor_graph(default_agent_registry(), sink=sink)
    result = invoke_supervisor(graph, state, context)
    handoff = result["handoffs"][0]
    agent_result = handoff["result"]
    reference = agent_result["tool_result_refs"][0]
    payload = context.tool_result_store[reference]
    assert handoff["target_agent"] == "after_sales" and handoff["status"] == "COMPLETED"
    assert payload["source"] == "shoprelay_escalation_draft"
    assert payload["draft"]["status"] == "DRAFT_ONLY"
    assert payload["draft"]["execution_status"] == "NOT_EXECUTED"
    assert payload["draft"]["ticket_created"] is False and payload["draft"]["notification_sent"] is False
    assert result["tool_execution_status"] == [{
        "execution_id": result["agent_tasks"][0]["task_id"] + ":escalate_to_human",
        "capability": "escalate_to_human", "status": "DRAFT_ONLY", "executed": False,
        "error_code": None, "duration_ms": result["tool_execution_status"][0]["duration_ms"]}]
    business = build_after_sales_business_result(result, route=context.route_payload or state["route"],
                                                result_store=context.tool_result_store)
    assert business["business_results"]["escalation_proposal"]["draft_ref"] == reference
    assert business["business_results"]["escalation_proposal"]["status"] == "DRAFT_ONLY"
    envelope = _agent_business_payload({**result, **business}, state["route"], "trace-a")
    assert envelope["business_results"]["escalation_proposal"]["pending_action_arguments"]["reason_code"] == \
        "HUMAN_REVIEW_REQUESTED"
    assert envelope["tool_references"][0]["result_ref"] == reference
    assert envelope["handoffs"][0]["target_agent"] == "after_sales"
    assert not any("ticket" in str(item).lower() and item[0] == "ticket_created" for item in events)
    assert {item[0] for item in events} >= {"agent_started", "tool_started", "tool_finished"}
    assert context.tool_execution_records[0]["executed"] is False


def test_escalation_graph_denies_missing_or_mismatched_permissions_and_invalid_refs():
    _, context, state, _ = _escalation_context(permission_checker=lambda _owner, _permission: False)
    result = invoke_supervisor(build_supervisor_graph(default_agent_registry()), state, context)
    assert result["handoffs"][0]["status"] == "FAILED"
    assert result["handoffs"][0]["error_code"] == "TOOL_NOT_AUTHORIZED"
    assert context.tool_result_store == {}

    _, context, state, _ = _escalation_context(permission_checker=None)
    result = invoke_supervisor(build_supervisor_graph(default_agent_registry()), state, context)
    assert result["handoffs"][0]["error_code"] == "PROPOSAL_PERMISSION_UNAVAILABLE"

    _, context, _, _ = _escalation_context(permission_checker=lambda _owner, _permission: True)
    trusted = context.trusted_agent_context
    tool_context = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(),
        run_id="run-a", trace_id="trace-a", parent_span_id=None,
        escalation_permission_checker=context.escalation_permission_checker)
    with pytest.raises(ToolExecutionError, match="PROPOSAL_SCHEMA_INVALID"):
        tool_context.create_escalation_proposal(trusted=trusted, reason_code="HUMAN_REVIEW_REQUESTED",
            order_ref=None, evidence_refs=["../private"], related_agents=["after_sales"])
    assert tool_context.result_store == {}

    _, context, _, _ = _escalation_context(permission_checker=lambda *_: (_ for _ in ()).throw(RuntimeError("denied")))
    tool_context = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(),
        run_id="run-a", trace_id="trace-a", parent_span_id=None,
        escalation_permission_checker=context.escalation_permission_checker)
    with pytest.raises(ToolExecutionError, match="PROPOSAL_PERMISSION_CHECK_FAILED"):
        tool_context.create_escalation_proposal(trusted=context.trusted_agent_context,
            reason_code="HUMAN_REVIEW_REQUESTED", order_ref=None, evidence_refs=[],
            related_agents=["after_sales"])
    assert tool_context.result_store == {}

    broken_trusted = context.trusted_agent_context.model_copy(update={"run_owner_verified": False})
    with pytest.raises(ToolExecutionError, match="AGENT_CONTEXT_INVALID"):
        tool_context.create_escalation_proposal(trusted=broken_trusted,
            reason_code="HUMAN_REVIEW_REQUESTED", order_ref=None, evidence_refs=[],
            related_agents=["after_sales"])

    tool_context.escalation_permission_checker = lambda _owner, _permission: True
    with pytest.raises(ToolExecutionError, match="PROPOSAL_SCHEMA_INVALID"):
        tool_context.create_escalation_proposal(trusted=context.trusted_agent_context,
            reason_code="", order_ref=None, evidence_refs=[], related_agents=["after_sales"])

    owned_target_context = context.trusted_agent_context.model_copy(update={"requested_order_id": "ORD-SYN-001"})
    with pytest.raises(ToolExecutionError, match="ORDER_REFERENCE_UNVERIFIED"):
        tool_context.create_escalation_proposal(trusted=owned_target_context,
            reason_code="HUMAN_REVIEW_REQUESTED", order_ref="ORD-SYN-001", evidence_refs=[],
            related_agents=["after_sales"])
    assert tool_context.result_store == {}


def test_escalation_proposal_serialization_failure_does_not_leak_result(monkeypatch):
    _, context, _, _ = _escalation_context(permission_checker=lambda _owner, _permission: True)
    tool_context = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(),
        run_id="run-a", trace_id="trace-a", parent_span_id=None,
        escalation_permission_checker=context.escalation_permission_checker)
    monkeypatch.setattr(EscalationDraft, "pending_action_arguments", lambda _self: {"unsafe": object()})
    with pytest.raises(ToolExecutionError, match="PROPOSAL_SCHEMA_INVALID"):
        tool_context.create_escalation_proposal(trusted=context.trusted_agent_context,
            reason_code="HUMAN_REVIEW_REQUESTED", order_ref=None, evidence_refs=[],
            related_agents=["after_sales"])
    assert tool_context.result_store == {}


def test_server_draft_permission_is_server_owned_and_narrow():
    from app.m4.actions import server_draft_permission_granted
    assert server_draft_permission_granted("u_demo", "support.ticket.draft") is True
    assert server_draft_permission_granted("u_demo", "support.ticket.create") is False
    assert server_draft_permission_granted("unlisted-user", "support.ticket.draft") is False
