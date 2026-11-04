from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import AfterSalesEvent, Base, Order, Refund, ReturnRequest, User
from app.m3.after_sales_tools import (AfterSalesReadRepository, build_after_sales_business_result,
    evaluate_policy_conditions)
from app.m3.agents import AfterSalesAgent
from app.m3.agents import AgentRegistry, OrderLogisticsAgent, PolicyAgent, ProductAgent
from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.order_tools import AgentExecutionContext
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state


def _factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _insert_order(factory, *, user_id="u1", order_id="ORD-1", status="PAID"):
    with factory.begin() as db:
        db.add(User(user_id=user_id, display_name=user_id))
        db.add(Order(order_id=order_id, user_id=user_id, status=status,
            paid_at="2026-10-10T08:00:00Z", delivered_at=None, paid_cents=1000, version=1))


def _insert_return(factory, *, status="APPROVED", return_id="RT-1", source_type="SIMULATED_TEST_DATA"):
    submitted = datetime(2026, 10, 11, tzinfo=timezone.utc)
    review = submitted.replace(hour=1)
    approved = review.replace(hour=2)
    with factory.begin() as db:
        row = ReturnRequest(return_id=return_id, order_id="ORD-1", status=status,
            reason="fixture-only", created_at=submitted.isoformat(), submitted_at=submitted,
            approved_at=approved if status == "APPROVED" else None,
            rejected_at=approved if status == "REJECTED" else None,
            review_started_at=review if status in {"UNDER_REVIEW", "APPROVED", "REJECTED"} else None,
            source_type=source_type, source_ref="SIMULATED_TEST_DATA:M35-UNIT",
            evidence_ref="fixture:approved-return")
        db.add(row)
        db.flush()
        db.add(AfterSalesEvent(event_id=f"{return_id}:submitted", order_id="ORD-1", return_id=return_id,
            refund_id=None, event_type="RETURN_SUBMITTED", occurred_at=submitted, recorded_at=submitted,
            actor_type="SIMULATED_TEST_DATA", source_type=source_type,
            source_ref="SIMULATED_TEST_DATA:M35-UNIT", details_json="{}"))
        if status in {"UNDER_REVIEW", "APPROVED", "REJECTED"}:
            db.add(AfterSalesEvent(event_id=f"{return_id}:{status.lower()}", order_id="ORD-1",
                return_id=return_id, refund_id=None, event_type="RETURN_REVIEW_STARTED", occurred_at=review,
                recorded_at=review, actor_type="SIMULATED_TEST_DATA", source_type=source_type,
                source_ref="SIMULATED_TEST_DATA:M35-UNIT", details_json="{}"))
        if status in {"APPROVED", "REJECTED"}:
            occurred = approved
            db.add(AfterSalesEvent(event_id=f"{return_id}:{status.lower()}:final", order_id="ORD-1",
                return_id=return_id, refund_id=None, event_type=f"RETURN_{status}", occurred_at=occurred,
                recorded_at=occurred, actor_type="SIMULATED_TEST_DATA", source_type=source_type,
                source_ref="SIMULATED_TEST_DATA:M35-UNIT", details_json="{}"))


def test_after_sales_read_is_owner_scoped_and_reports_real_empty_state_without_mutation():
    engine, factory = _factory()
    _insert_order(factory)
    repo = AfterSalesReadRepository(factory)
    before = factory().execute(select(func.count()).select_from(Refund)).scalar_one()
    result = repo.get_refund_status("ORD-1", "u1")
    after = factory().execute(select(func.count()).select_from(Refund)).scalar_one()
    assert result["source"] == "shoprelay_after_sales_database"
    assert result["availability"] == "AVAILABLE"
    assert result["application_record_state"] == "NO_APPLICATION_RECORD"
    assert result["refunds"] == {"active_request": None, "latest_request": None}
    assert result["returns"] == {"active_request": None, "latest_request": None}
    assert before == after == 0
    try:
        repo.get_refund_status("ORD-1", "u2")
    except Exception as exc:
        assert getattr(exc, "code", None) == "ORDER_NOT_FOUND"
    else:
        raise AssertionError("foreign owner order must be indistinguishable from missing")
    engine.dispose()


def test_after_sales_status_returns_real_refund_and_return_without_sensitive_reason():
    engine, factory = _factory()
    _insert_order(factory)
    with factory.begin() as db:
        db.add(Refund(refund_id="RF-1", order_id="ORD-1", status="UNDER_REVIEW",
            amount_cents=1000, reason="private reason", created_at="2026-10-11T00:00:00Z"))
        db.add(ReturnRequest(return_id="RT-1", order_id="ORD-1", status="APPROVED",
            reason="private return reason", created_at="2026-10-11T00:00:00Z"))
    result = AfterSalesReadRepository(factory).get_refund_status("ORD-1", "u1")
    assert result["refunds"]["active_request"] is None
    assert result["refunds"]["latest_request"]["status"] == "UNKNOWN"
    assert result["returns"]["latest_request"]["status"] == "UNKNOWN"
    assert result["application_record_state"] == "UNKNOWN"
    assert "reason" not in result["refunds"]["latest_request"]
    assert "reason" not in result["returns"]["latest_request"]
    engine.dispose()


def test_after_sales_approval_requires_a_valid_audit_history_and_policy_evaluator_uses_it():
    engine, factory = _factory()
    _insert_order(factory, status="DELIVERED")
    _insert_return(factory, status="APPROVED")
    result = AfterSalesReadRepository(factory).get_refund_status("ORD-1", "u1")
    approved = result["returns"]["latest_request"]
    assert approved["status"] == "APPROVED" and approved["verified"] is True
    assert len(approved["event_ids"]) == 3  # submitted -> review started -> approved
    bad = {"availability": "AVAILABLE", "application_record_state": "UNKNOWN",
           "returns": {"latest_request": approved | {"verified": False}}, "refunds": {}}
    policy = {"evidence": [{"chunk_id": "refund:v1:delivered", "content": "已签收订单须有已批准的退货申请才能申请退款。",
        "effective_from": "2026-01-01T00:00:00Z", "effective_to": None}]}
    order = {"order_id": "ORD-1", "status": "DELIVERED", "paid_at": "2026-10-10T00:00:00Z",
             "delivered_at": "2026-10-16T00:00:00Z"}
    evaluation = evaluate_policy_conditions(order, bad, policy, query="已签收订单能否申请退款")
    assert evaluation["conditions"][0]["evaluation_result"] == "UNKNOWN"
    engine.dispose()


def test_verified_refund_is_linked_to_same_order_approved_return_without_claiming_settlement():
    engine, factory = _factory()
    _insert_order(factory, status="DELIVERED")
    _insert_return(factory, status="APPROVED")
    submitted = datetime(2026, 10, 13, tzinfo=timezone.utc)
    with factory.begin() as db:
        db.add(Refund(refund_id="RF-LINKED", order_id="ORD-1", return_id="RT-1",
            status="SUBMITTED", amount_cents=1000, reason="fixture", created_at=submitted.isoformat(),
            submitted_at=submitted, source_type="SIMULATED_TEST_DATA",
            source_ref="SIMULATED_TEST_DATA:M35-UNIT"))
        db.flush()
        db.add(AfterSalesEvent(event_id="RF-LINKED:EV0", order_id="ORD-1",
            return_id=None, refund_id="RF-LINKED", event_type="REFUND_SUBMITTED",
            occurred_at=submitted, recorded_at=submitted, actor_type="SIMULATED_TEST_DATA",
            source_type="SIMULATED_TEST_DATA", source_ref="SIMULATED_TEST_DATA:M35-UNIT",
            details_json="{}"))
    result = AfterSalesReadRepository(factory).get_refund_status("ORD-1", "u1")
    refund = result["refunds"]["latest_request"]
    assert refund["verified"] is True and refund["status"] == "SUBMITTED"
    assert refund["linked_return_status"] == "APPROVED"
    assert refund["linked_return_verified"] is True
    assert refund["payment_settlement_verified"] is False
    engine.dispose()


def test_return_approval_cannot_skip_required_review_stage():
    submitted = datetime(2026, 10, 11, tzinfo=timezone.utc)
    approved = submitted.replace(hour=2)
    row = ReturnRequest(return_id="RT-JUMP", order_id="ORD-1", status="APPROVED",
        reason="fixture", created_at=submitted.isoformat(), submitted_at=submitted,
        approved_at=approved, source_type="SIMULATED_TEST_DATA",
        source_ref="SIMULATED_TEST_DATA:M35-JUMP")
    events = [
        AfterSalesEvent(event_id="EV-SUB", order_id="ORD-1", return_id="RT-JUMP",
            refund_id=None, event_type="RETURN_SUBMITTED", occurred_at=submitted,
            recorded_at=submitted, actor_type="SIMULATED_TEST_DATA",
            source_type="SIMULATED_TEST_DATA", source_ref="SIMULATED_TEST_DATA:M35-JUMP",
            details_json="{}"),
        AfterSalesEvent(event_id="EV-APP", order_id="ORD-1", return_id="RT-JUMP",
            refund_id=None, event_type="RETURN_APPROVED", occurred_at=approved,
            recorded_at=approved, actor_type="HUMAN_REVIEWER",
            source_type="SIMULATED_TEST_DATA", source_ref="SIMULATED_TEST_DATA:M35-JUMP",
            details_json="{}"),
    ]
    verified, state, _ = AfterSalesReadRepository._verified_history("return", row, events)
    assert verified is False and state is None


def test_paid_unshipped_refund_condition_is_separate_from_delivered_return_prerequisite():
    order = {"order_id": "ORD-1", "status": "PAID", "paid_at": "2026-10-10T08:00:00Z",
             "delivered_at": None}
    after = {"availability": "AVAILABLE", "application_record_state": "NO_APPLICATION_RECORD",
             "refunds": {"latest_request": None}, "returns": {"latest_request": None}}
    policy = {"evidence": [{"chunk_id": "refund:pre-shipment",
        "content": "已付款且未发货的订单可申请整单退款。",
        "effective_from": "2026-01-01T00:00:00Z", "effective_to": None}]}
    result = evaluate_policy_conditions(order, after, policy, query="已付款未发货订单可以申请退款吗")
    condition = result["conditions"][0]
    assert condition["condition_id"] == "refund_request_for_paid_unshipped_order"
    assert condition["evaluation_result"] == "SATISFIED"
    assert condition["required_fact"] == "verified_paid_and_not_shipped_order"


def test_missing_after_sales_tables_return_not_available_without_migration_or_fake_empty_state():
    engine, factory = _factory()
    _insert_order(factory)
    Refund.__table__.drop(engine)
    ReturnRequest.__table__.drop(engine)
    result = AfterSalesReadRepository(factory).get_refund_status("ORD-1", "u1")
    assert result["availability"] == "NOT_AVAILABLE"
    assert set(result["missing_sources"]) == {"refunds", "returns"}
    assert result["refunds"] is None and result["returns"] is None
    engine.dispose()


def test_not_available_is_a_successful_tool_execution_with_explicit_span_status():
    sink = InMemoryObservabilitySink()
    context = AgentExecutionContext(result_store={}, sink=sink, run_id="r1", trace_id="t1",
        parent_span_id=None, graph_version="g2-m3.5")
    ref = context.execute_read("get_refund_status", "ORD-1", "u1",
        lambda order_id, owner_id: {"source": "shoprelay_after_sales_database", "order_id": order_id,
            "availability": "NOT_AVAILABLE", "missing_sources": ["refunds", "returns"],
            "error_code": "AFTER_SALES_SCHEMA_UNAVAILABLE"})
    assert context.result_store[ref]["availability"] == "NOT_AVAILABLE"
    assert context.call_records[0]["status"] == "NOT_AVAILABLE"
    assert sink.spans[0].status == "OK"
    assert sink.spans[0].error_code == "AFTER_SALES_SCHEMA_UNAVAILABLE"
    assert sink.metrics[0].node_error_count == 0


def test_user_claims_do_not_satisfy_quality_facts_and_payment_time_version_is_checked():
    policy = {"evidence": [{"chunk_id": "policies/return@v1#quality", "policy_id": "return",
        "content": "签收后30天内质量问题可提交退货申请，由人工审核。", "effective_from": "2026-07-01T00:00:00Z",
        "effective_to": None, "version": "v1"}]}
    order = {"order_id": "ORD-1", "status": "DELIVERED", "paid_at": "2026-06-30T00:00:00Z",
        "delivered_at": "2026-10-10T00:00:00Z"}
    after = {"availability": "AVAILABLE", "application_record_state": "NO_APPLICATION_RECORD",
        "returns": {"latest_request": None}, "refunds": {"latest_request": None}}
    result = evaluate_policy_conditions(order, after, policy,
        query="用户说商品有质量问题，请确认已签收后30天内可以退款吗")
    quality = next(item for item in result["conditions"] if item["condition_id"] == "quality_return_window")
    approval = next(item for item in result["conditions"] if item["condition_id"] == "return_manual_review")
    refund = next(item for item in result["conditions"] if item["condition_id"] == "refund_requires_approved_return_when_delivered")
    assert quality["evaluation_result"] == "UNKNOWN"
    assert "QUALITY_ISSUE_TIME_NOT_VERIFIED" in quality["missing_information"]
    assert approval["evaluation_result"] == "UNKNOWN"  # policy version did not cover paid_at
    assert refund["evaluation_result"] == "UNKNOWN"  # no policy evidence valid at paid_at
    assert result["user_claims_used_as_verified_facts"] is False


def test_after_sales_agent_refuses_write_capability_and_requires_trusted_context():
    engine, factory = _factory()
    _insert_order(factory)
    repo = AfterSalesReadRepository(factory)
    agent = AfterSalesAgent(repo)
    task = AgentTask(task_id="task", target_agent="after_sales",
        task="Resolve requested after-sales capability under confirmation controls",
        required_capabilities=["create_refund_request"], context_refs=[], evidence_refs=[])
    trusted = TrustedAgentContext(user_id="u1", session_id="s1", run_id="r1", trace_id="t1")
    result = agent.execute(task, trusted)
    assert result.status == "REQUIRES_CONFIRMATION"
    assert result.error_code == "ACTION_CONFIRMATION_REQUIRED"
    engine.dispose()


def test_supervisor_hands_order_and_policy_refs_to_real_after_sales_agent_in_bounded_order():
    engine, factory = _factory()
    _insert_order(factory)

    class OrderRepo:
        def get_order(self, order_id, owner_id):
            assert owner_id == "u1"
            with factory() as db:
                row = db.get(Order, order_id)
                return {"source": "business_database", "order_id": row.order_id, "status": row.status,
                    "paid_at": row.paid_at, "delivered_at": row.delivered_at,
                    "paid_cents": row.paid_cents, "version": row.version}

        def get_order_items(self, order_id, owner_id):
            return {"source": "business_database", "order_id": order_id, "items": []}

        def track_shipment(self, order_id, owner_id):
            return {"source": "business_database", "order_id": order_id,
                "availability": "UNAVAILABLE", "records": [], "error_code": "NO_SHIPMENT_RECORD"}

    class PolicyService:
        def search(self, query, *, mode, result_ref):
            at = "2026-10-22T00:00:00Z"
            return {"source": "shoprelay_policy_retrieval", "result_ref": result_ref,
                "retrieval_mode": mode, "retrieval_count": 1, "knowledge_version": "k-test",
                "index_fingerprint": "f-test", "evaluation_at": at, "config_version": "test",
                "config_hash": "test", "degraded": False, "failures": [], "latency_ms": 1,
                "evidence": [{"chunk_id": "policies/refund@2026-01#before-shipping", "policy_id": "refund",
                    "clause_id": "before-shipping", "title": "退款政策", "content": "已付款未发货的订单可申请整单退款。",
                    "version": "2026-01", "policy_version": "2026-01",
                    "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
                    "knowledge_version": "k-test", "index_fingerprint": "f-test", "rank": 1, "score": 1.0}]}

    sink = InMemoryObservabilitySink()
    repo = AfterSalesReadRepository(factory)
    registry = AgentRegistry(policy=PolicyAgent(PolicyService()),
        order_logistics=OrderLogisticsAgent(OrderRepo()), after_sales=AfterSalesAgent(repo),
        product=ProductAgent())
    graph = build_supervisor_graph(registry, sink=sink)
    query = "请核实订单 ORD-1 状态和售后记录，并说明退款申请条件。"
    route = {"strategy": "AGENT", "sub_strategy": "AGENT+MULTI", "reason_code": "MULTI_POLICY_CONDITIONS",
        "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]}
    state = graph_input_state(GraphInput(run_id="r1", session_id="s1", trace_id="t1", query=query),
        graph_version="g2-m3.5")
    state["route"] = route
    state["terminal_status"] = "RUNNING"
    trusted = TrustedAgentContext(user_id="u1", session_id="s1", run_id="r1", trace_id="t1",
        requested_order_id="ORD-1")
    context = GraphRuntimeContext(query=query, route_payload=route, trusted_agent_context=trusted,
        m3_supervisor_enabled=True, tool_result_store={}, tool_execution_records=[])
    result = invoke_supervisor(graph, state, context)
    assert result["supervisor_status"] == "COMPLETED"
    assert result["agent_budget"]["agent_calls"] == 3
    assert [item["target_agent"] for item in result["handoffs"]] == [
        "order_logistics", "policy", "after_sales"]
    after_handoff = result["handoffs"][-1]
    assert any(ref.startswith("tool_result:") for ref in after_handoff["context_refs"])
    assert after_handoff["evidence_refs"]
    business = build_after_sales_business_result(result, route=route, result_store=context.tool_result_store)
    assert business is not None, result
    assert business["answer_kind"] == "POLICY_ANSWER"
    assert business["business_results"]["order"]["status"] == "PAID"
    assert any(item["capability"] == "get_refund_status" and item["executed"]
        for item in result["tool_execution_status"])
    assert any(span.node_name == "m3.agent.after_sales" for span in sink.spans)
    engine.dispose()
