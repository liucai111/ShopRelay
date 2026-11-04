"""Offline r3.2.4 product route + real D1 application/fixture integration."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.m0.config import load_settings
from app.m1.models import Run, RunEvent, Trace
from app.m1.routing import RouteCapabilityScopeError, decide_route
from app.m3.agents import ProductAgent
from app.m3.contracts import AgentResult, AgentTask, TrustedAgentContext
from app.m3.order_tools import AgentExecutionContext
from app.m3.product_tools import build_product_business_result
from app.orchestration.observability import InMemoryObservabilitySink
from eval.m6_1.personal_dev import D1Coordinator, FixtureReceipt, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_scoring import diagnose_personal_d1_run
from eval.m6_1.personal_dev_runtime import execute_application_case
from eval.m6_1.budget import BudgetLimits


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"


@pytest.fixture
def coordinator():
    root = ROOT / "data/eval/offline-tests" / ("product-route-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    target = PersonalDevDatabase(root)
    engine, factory = target.open(initialize=True)
    try:
        yield D1Coordinator(target, factory)
    finally:
        engine.dispose()


def _bundle():
    return json.loads(ASSET.read_text(encoding="utf-8"))


def _case_tools(bundle, case_id):
    return [row["tool_id"] for row in next(c for c in bundle["cases"] if c["case_id"] == case_id)
        ["input"].get("expected_tool_calls", []) if row.get("mode") == "read_only"]


def test_r324_product_comparison_routes_to_only_case_allowed_catalog_tools():
    bundle = _bundle()
    case = next(c for c in bundle["cases"] if c["case_id"] == "D1-PRODUCT-001")
    allowed = _case_tools(bundle, case["case_id"])
    route = decide_route(case["input"]["user_query"], available_capabilities=allowed)
    assert route.strategy == "AGENT"
    assert route.reason_code == "PRODUCT_COMPARISON_CATALOG_LOOKUP"
    assert set(route.required_capabilities) == {"search_products", "get_product_detail"}
    assert set(route.required_capabilities) <= set(allowed)


def test_product_routing_never_uses_missing_order_id_as_a_catalog_blocker():
    product = decide_route("Which fictional Luma desk lamp is lighter?",
        available_capabilities=["search_products", "get_product_detail"])
    order = decide_route("查询我的订单目前的物流状态",
        available_capabilities=["track_shipment"])
    assert product.strategy == "AGENT" and "search_products" in product.required_capabilities
    assert order.strategy == "DIRECT" and order.reason_code == "MISSING_ORDER_IDENTIFIER"


def test_capability_outside_case_scope_is_rejected_without_direct_downgrade():
    class OutOfScopeClassifier:
        def classify_route(self, _query, *, available_capabilities=None):
            assert available_capabilities == ["get_order"]
            return {"strategy": "AGENT", "sub_strategy": "AGENT+SINGLE", "intent_labels": ["live_product"],
                "safety_decision": "ALLOW", "reason_code": "MODEL_ROUTE", "decision_source": "LLM",
                "needs_clarification": False,
                "clarification_question": None, "required_capabilities": ["search_products"]}

    with pytest.raises(RouteCapabilityScopeError, match="exceeds current request capability scope"):
        decide_route("Could you find something for my use case?", OutOfScopeClassifier(),
            available_capabilities=["get_order"])


def test_route_provider_prompt_receives_current_allowed_capabilities_without_api_call():
    from app.m1.chat import DeepSeekProvider

    response_payload = {"strategy": "DIRECT", "sub_strategy": None, "intent_labels": ["uncertain"],
        "safety_decision": "ALLOW", "reason_code": "UNCERTAIN", "needs_clarification": True,
        "clarification_question": "Please clarify.", "required_capabilities": []}
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model="offline", usage=None,
                choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                    content=json.dumps(response_payload)))])

    settings = load_settings(environ={}, local_values={}, require_api_key=False)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    provider = DeepSeekProvider(settings=settings, client=fake_client)
    provider.classify_route("Could you help me?", available_capabilities=["search_products"])
    prompt = calls[0]["messages"][0]["content"]
    assert '"search_products"' in prompt
    assert "Missing an order identifier blocks only order-" in prompt
    assert "search the catalog first" in prompt


def _execute_real_app_offline(coordinator, bundle, case_id, monkeypatch):
    plan = plan_case(bundle, case_id)
    receipt = coordinator.prepare(plan)
    class OfflineSDKClient:
        class _Completions:
            @staticmethod
            def create(**_kwargs):
                raise AssertionError("offline route must not call a Provider")
        chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr("app.m0.config.create_deepseek_client",
        lambda _settings, _node: OfflineSDKClient())
    settings = load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
        "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "g2-m3.3",
        "SHOPRELAY_EXECUTION_VERSION": "m6.1-d1-product-route-offline"},
        local_values={}, require_api_key=False)
    settings = replace(settings, orchestrator="langgraph", m3_supervisor_enabled=True,
        api_key="offline-test-key", database_url=f"sqlite:///{coordinator.target.path.as_posix()}")
    limits = BudgetLimits(provider_calls=4, tool_calls=4, total_tokens=120000, estimated_usd=.18,
        max_calls_per_trial=4, max_concurrent_trials=1, max_duration_seconds=180, max_retries=0)
    result = execute_application_case(coordinator, plan, receipt, SimpleNamespace(limits=limits), settings)
    return plan, receipt, result


def test_real_d1_product_route_reads_fixture_weight_and_persists_agent_tools_run_and_trace(coordinator, monkeypatch):
    bundle = _bundle()
    plan, receipt, result = _execute_real_app_offline(coordinator, bundle, "D1-PRODUCT-001", monkeypatch)
    try:
        assert result["status"] == "SUCCEEDED"
        payload = result["result"]
        assert payload["strategy"] == "AGENT"
        comparison = payload["business_results"]["product_comparison"]
        assert comparison["lighter_product"]["name"] == "Luma-A"
        assert comparison["lighter_product"]["weight_g"] == 410
        assert comparison["weight_difference_g"] == 50
        assert "依据商品目录的 get_product_detail 记录" in payload["answer"]
        assert "Luma-A" in payload["answer"] and "410 g" in payload["answer"]
        assert "Luma-B" in payload["answer"] and "460 g" in payload["answer"]
        assert "相差 50 g" in payload["answer"]
        evidence = [row for row in payload["business_evidence"] if row["kind"] == "catalog_product_fact"]
        assert {row["resource"]["name"] for row in evidence} == {"Luma-A", "Luma-B"}
        assert all(row["source"] == "shoprelay_product_catalog" and
            row["tool"] == "get_product_detail" and row["result_ref"] for row in evidence)
        assert all(row["result_ref"] in {ref["result_ref"] for ref in payload["tool_references"]}
            for row in evidence)
        assert not any(row.get("kind") == "knowledge_document" for row in payload["business_evidence"])
        audit = payload["tool_call_audit"]
        assert len(audit) == 3 and len({row["tool_call_id"] for row in audit}) == 3
        assert {row["tool"] for row in audit} == {"search_products", "get_product_detail"}
        assert all(row["run_id"] == result["run_id"] and row["trace_id"] for row in audit)
        audit_text = json.dumps(audit, ensure_ascii=False)
        assert "Which fictional Luma" not in audit_text and "P-SYN-" not in audit_text
        assert all(row["parameter_fingerprint"] and row["status"] == "COMPLETED" and
            row["executed"] is True and row["result_ref"] for row in audit)
        tool_event_payloads = [json.loads(row["payload_json"]) for row in result["observations"]["run_events"]
            if row["event_type"] in {"tool_started", "tool_finished"}]
        assert {row["tool_call_id"] for row in tool_event_payloads} == {row["tool_call_id"] for row in audit}
        assert all(row.get("trace_id") and row.get("run_id") == result["run_id"] for row in tool_event_payloads)
        assert not any("P-SYN-" in json.dumps(row) or "Luma" in json.dumps(row) for row in tool_event_payloads)
        persisted = json.loads(result["observations"]["runs"][-1]["result_json"])
        assert persisted["business_evidence"] == payload["business_evidence"]
        assert persisted["tool_call_audit"] == audit
        assert set(comparison["evidence_ids"]) == {"syn-product-luma-a", "syn-product-luma-b"}
        assert "Luma-A更轻（410 g）" in payload["answer"]
        events = result["observations"]["run_events"]
        assert any(row["event_type"] == "agent_started" and
            json.loads(row["payload_json"]).get("agent") == "product" for row in events)
        tools = [row for row in events if row["event_type"] in {"tool_started", "tool_finished"}]
        assert {json.loads(row["payload_json"]).get("tool") for row in tools} == {
            "search_products", "get_product_detail"}
        assert len(result["observations"]["traces"]) >= 2
        diagnostic = diagnose_personal_d1_run(case_id="D1-PRODUCT-001",
            expected_tool_calls=plan["case"]["input"]["expected_tool_calls"], result=result)
        assert diagnostic["agent_started"] is True and diagnostic["run_status"] == "SUCCEEDED"
        assert diagnostic["task_outcome"] == "UNKNOWN" and diagnostic["semantic_score"] == "UNKNOWN"
        coordinator.cleanup(receipt)
    finally:
        # Cleanup only this exact receipt if the test assertion did not already
        # clean it; never touch unrelated rows or retry execution.
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def _run_product_agent_on_bundle(coordinator, bundle, *, maximum=16, task_capabilities=None):
    case_id = "D1-PRODUCT-001"
    plan = plan_case(bundle, case_id)
    receipt = coordinator.prepare(plan)
    try:
        repository = coordinator.repositories(plan, receipt, maximum=maximum)["product"]
        result_store = {}
        context = AgentExecutionContext(result_store=result_store, sink=InMemoryObservabilitySink(),
            run_id="offline-product-run", trace_id="offline-product-trace", parent_span_id=None,
            user_query=plan["message"], call_records=[])
        route = decide_route(plan["message"], available_capabilities=["search_products", "get_product_detail"])
        capabilities = task_capabilities or route.required_capabilities or ["get_product_detail"]
        task = AgentTask(task_id="product-offline", target_agent="product",
            task="Resolve requested live product capability",
            required_capabilities=capabilities, context_refs=[], evidence_refs=[])
        trusted = TrustedAgentContext(user_id=plan["owner_id"], session_id="offline-session",
            run_id="offline-product-run", trace_id="offline-product-trace", authenticated=True,
            run_owner_verified=True, session_owner_verified=True)
        agent_result = ProductAgent(repository).execute(task, trusted, context)
        statuses = [{"capability": row["capability"], "status": row["status"],
            "executed": row["executed"], "duration_ms": row.get("duration_ms")}
            for row in context.call_records]
        # The supervisor reports an uncalled detail capability when search is empty.
        for capability in capabilities:
            if not any(row["capability"] == capability for row in statuses):
                statuses.append({"capability": capability, "status": "NOT_EXECUTED", "executed": False})
        supervisor = {"supervisor_status": "COMPLETED", "terminal_status": "RUNNING",
            "handoffs": [{"target_agent": "product", "status": "COMPLETED",
                "required_capabilities": task.required_capabilities,"result": agent_result.model_dump(mode="json")}],
            "tool_execution_status": statuses}
        business = build_product_business_result(supervisor, route=route.model_dump(mode="json"), result_store=result_store)
        return plan, receipt, agent_result, result_store, business
    except Exception:
        coordinator.cleanup(receipt)
        raise


def test_empty_product_search_asks_for_more_details_without_fabricating_results(coordinator):
    bundle = _bundle()
    synthetic = deepcopy(bundle)
    case = next(c for c in synthetic["cases"] if c["case_id"] == "D1-PRODUCT-001")
    synthetic["fixtures"]["products"] = [{"id": "syn-product-other", "name": "Water Bottle",
        "category": "drinkware", "weight_g": 999}]
    case["input"]["fixture_refs"] = ["syn-product-other"]
    case["input"]["acceptable_evidence_ids"] = ["syn-product-other"]
    _, receipt, agent_result, _store, business = _run_product_agent_on_bundle(coordinator, synthetic)
    try:
        assert agent_result.missing_information == ["PRODUCT_CANDIDATES_NOT_FOUND"]
        assert business["answer_kind"] == "CLARIFICATION_REQUIRED"
        assert "没有找到匹配的商品记录" in business["answer"]
        assert "Water Bottle" not in business["answer"]
        assert business["business_results"]["search_products"]["count"] == 0
        search_evidence = [row for row in business["business_evidence"]
            if row["kind"] == "catalog_search_result"]
        assert len(search_evidence) == 1
        assert search_evidence[0]["facts"] == [{"field": "matched_count", "value": 0}]
        assert search_evidence[0]["source"] == "shoprelay_product_catalog"
        assert search_evidence[0]["result_ref"] == business["tool_references"][0]["result_ref"]
        assert "citations" not in business
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_missing_weight_after_retrieval_is_ambiguous_and_clarifies(coordinator):
    bundle = deepcopy(_bundle())
    product_b = next(row for row in bundle["fixtures"]["products"] if row["id"] == "syn-product-luma-b")
    product_b.pop("weight_g")
    _, receipt, agent_result, _store, business = _run_product_agent_on_bundle(coordinator, bundle)
    try:
        assert len(agent_result.tool_result_refs) == 3
        assert business["answer_kind"] == "CLARIFICATION_REQUIRED"
        assert business["business_results"]["product_comparison"]["status"] == "MISSING_COMPARISON_ATTRIBUTE"
        assert "无法可靠比较" in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_direct_clarification_run_does_not_emit_agent_started(coordinator, monkeypatch):
    bundle = _bundle()
    case = next(c for c in bundle["cases"] if c["case_id"] == "D1-PRODUCT-001")
    direct_bundle = deepcopy(bundle)
    direct_case = next(c for c in direct_bundle["cases"] if c["case_id"] == "D1-PRODUCT-001")
    direct_case["input"]["user_query"] = "你好"
    direct_case["input"]["expected_tool_calls"] = []
    direct_case["input"]["fixture_refs"] = []
    direct_case["input"]["acceptable_evidence_ids"] = []
    direct_case["expected_outcome"] = {}
    plan, receipt, result = _execute_real_app_offline(coordinator, direct_bundle, "D1-PRODUCT-001", monkeypatch)
    try:
        assert result["status"] == "SUCCEEDED"
        assert not any(row["event_type"] == "agent_started" for row in result["observations"]["run_events"])
        assert result["result"].get("tool_call_audit", []) == []
        diagnostic = diagnose_personal_d1_run(case_id=case["case_id"], expected_tool_calls=[], result=result)
        assert diagnostic["run_status"] == "SUCCEEDED" and diagnostic["agent_started"] is False
        assert diagnostic["task_outcome"] == "UNKNOWN"
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def _comparison_bundle(query, products):
    bundle = deepcopy(_bundle())
    case = next(c for c in bundle["cases"] if c["case_id"] == "D1-PRODUCT-001")
    bundle["fixtures"]["products"] = products
    case["input"]["user_query"] = query
    case["input"]["fixture_refs"] = [row["id"] for row in products]
    case["input"]["acceptable_evidence_ids"] = [row["id"] for row in products]
    case["input"]["expected_tool_calls"] = [
        {"tool_id": "search_products", "mode": "read_only"},
        {"tool_id": "get_product_detail", "mode": "read_only"}]
    return bundle


@pytest.mark.parametrize(("query", "weights", "winner", "direction"), [
    ("Which Northstar mugs are lighter?", [310, 275], "Northstar Mug B", "MIN"),
    ("Which Northstar mugs are heavier?", [310, 275], "Northstar Mug A", "MAX"),
    ("Compare the weights of Northstar mugs.", [310, 310], None, "SIDE_BY_SIDE"),
])
def test_comparison_direction_and_ties_use_requested_weight_semantics(coordinator, query, weights, winner, direction):
    products = [
        {"id": "syn-northstar-a", "name": "Northstar Mug A", "category": "drinkware", "weight_g": weights[0], "price_cents": 1200},
        {"id": "syn-northstar-b", "name": "Northstar Mug B", "category": "drinkware", "weight_g": weights[1], "price_cents": 1400},
    ]
    _, receipt, agent_result, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle(query, products))
    try:
        result = business["business_results"]["product_comparison"]
        assert result["direction"] == direction
        if winner:
            assert result["winner"]["name"] == winner
            assert winner in business["answer"]
        else:
            assert result["tie"] is True
            assert "重量相同" in business["answer"]
        assert len(business["tool_references"]) >= 3
        evidence = [row for row in business["business_evidence"] if row["kind"] == "catalog_product_fact"]
        assert len(evidence) == 2
        assert all(row["source"] == "shoprelay_product_catalog" and row["result_ref"] for row in evidence)
        assert all(any(fact["field"] == "weight_g" for fact in row["facts"]) for row in evidence)
        if winner:
            expected_winner_weight = min(weights) if direction == "MIN" else max(weights)
            other_weight = max(weights) if direction == "MIN" else min(weights)
            assert f"{expected_winner_weight} g" in business["answer"]
            assert f"相差 {abs(weights[0] - weights[1])} g" in business["answer"]
            assert f"{other_weight} g" in business["answer"]
        assert agent_result.status == "COMPLETED"
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_explicit_two_product_ids_read_only_those_products(coordinator):
    from eval.m6_1.fixture_read_models import product_id

    first_alias, second_alias = "syn-northstar-a", "syn-cedar-b"
    first, second = product_id(first_alias), product_id(second_alias)
    products = [
        {"id": first_alias, "name": "Northstar Mug A", "category": "drinkware", "weight_g": 300, "price_cents": 900},
        {"id": second_alias, "name": "Cedar Mug B", "category": "drinkware", "weight_g": 280, "price_cents": 1100},
        {"id": "syn-unrelated", "name": "Northstar Mug Promotion Item", "category": "drinkware", "weight_g": 10, "price_cents": 1},
    ]
    query = f"Compare {first} and {second} by weight; which product is heavier?"
    bundle = _comparison_bundle(query, products)
    route = decide_route(query, available_capabilities=["search_products", "get_product_detail"])
    assert route.reason_code == "PRODUCT_COMPARISON_EXPLICIT_IDS"
    assert route.required_capabilities == ["get_product_detail"]
    assert "product_direction_max" in route.intent_labels
    _, receipt, _agent_result, result_store, business = _run_product_agent_on_bundle(coordinator, bundle)
    try:
        assert [row["tool"] for row in business["tool_references"]] == ["get_product_detail", "get_product_detail"]
        compared_ids = {row["product_id"] for row in business["business_results"]["product_comparison"]["products"]}
        assert compared_ids == {first, second}
        assert "syn-unrelated" not in json.dumps(result_store, ensure_ascii=False)
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_zero_one_missing_and_unsupported_comparison_data_have_precise_results(coordinator):
    one = [{"id": "syn-vela-kettle", "name": "Vela Kettle", "category": "appliance", "weight_g": 800}]
    _, receipt, _agent, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle("Which Vela kettles are lighter?", one))
    try:
        assert business["business_results"]["product_comparison"]["status"] == "INSUFFICIENT_CANDIDATES"
        assert "一个或更少" in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)

    products = [
        {"id": "syn-vela-a", "name": "Vela Kettle A", "category": "appliance", "weight_g": 800, "price_cents": 4800},
        {"id": "syn-vela-b", "name": "Vela Kettle B", "category": "appliance", "weight_g": 700, "price_cents": 5200},
    ]
    _, receipt, _agent, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle("Compare the Vela kettles.", products))
    try:
        assert business["business_results"]["product_comparison"]["status"] == "COMPARISON_ATTRIBUTE_NOT_SPECIFIED"
        assert "更轻" not in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)

    _, receipt, _agent, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle("Compare battery life for the Vela kettles.", products))
    try:
        assert business["business_results"]["product_comparison"]["status"] == "UNSUPPORTED_REQUESTED_ATTRIBUTE"
        assert "不能用重量或价格替代" in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)

    missing_price = deepcopy(products)
    for product in missing_price:
        product.pop("price_cents", None)
    _, receipt, _agent, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle("Which Vela kettles are cheaper?", missing_price))
    try:
        comparison = business["business_results"]["product_comparison"]
        assert comparison["attribute"] == "price_cents"
        assert comparison["status"] == "MISSING_COMPARISON_ATTRIBUTE"
        assert "目录价格字段" in business["answer"]
        assert "更轻" not in business["answer"] and "更便宜" not in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_price_comparison_uses_price_and_budget_truncation_is_not_a_catalog_claim(coordinator):
    from eval.m6_1.fixture_read_models import product_id

    first_alias, second_alias = "syn-cedar-a", "syn-cedar-b"
    first, second = product_id(first_alias), product_id(second_alias)
    products = [
        {"id": first_alias, "name": "Cedar Bottle A", "category": "drinkware", "weight_g": 100, "price_cents": 2400},
        {"id": second_alias, "name": "Cedar Bottle B", "category": "drinkware", "weight_g": 900, "price_cents": 1900},
    ]
    _, receipt, _agent, _store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle(f"Compare prices of {first} and {second}; which is cheaper?", products))
    try:
        comparison = business["business_results"]["product_comparison"]
        assert comparison["attribute"] == "price_cents"
        assert comparison["winner"]["product_id"] == second
        assert "重量" not in business["answer"]
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)

    many = [{"id": f"syn-cedar-{n}", "name": f"Cedar Bottle {n}", "category": "drinkware",
        "weight_g": 200 + n, "price_cents": 1000 + n} for n in range(12)]
    _, receipt, agent_result, result_store, business = _run_product_agent_on_bundle(
        coordinator, _comparison_bundle("Which Cedar bottles are lighter?", many), maximum=4)
    try:
        comparison = business["business_results"]["product_comparison"]
        assert agent_result.missing_information == ["PRODUCT_CANDIDATES_TRUNCATED"]
        assert comparison["status"] == "CANDIDATE_SCOPE_INCOMPLETE"
        assert comparison["checked_candidate_count"] <= 3
        assert "不能据此判断全部匹配商品" in business["answer"]
        assert len(agent_result.tool_result_refs) <= 4
        search = next(value for value in result_store.values() if value.get("source") == "shoprelay_product_catalog"
            and "products" in value)
        assert search["count"] == 12 and search["truncated"] is True
        assert len(search["products"]) == 10
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_ordinary_ambiguous_single_product_query_still_clarifies_without_reads(coordinator):
    products = [
        {"id": "syn-ambiguous-a", "name": "Northstar Mug A", "category": "drinkware", "weight_g": 300},
        {"id": "syn-ambiguous-b", "name": "Northstar Mug B", "category": "drinkware", "weight_g": 320},
    ]
    bundle = _comparison_bundle("Show details for Northstar Mug A and Northstar Mug B.", products)
    _plan, receipt, agent_result, result_store, business = _run_product_agent_on_bundle(
        coordinator, bundle, task_capabilities=["get_product_detail"])
    try:
        assert agent_result.missing_information == ["PRODUCT_ID_AMBIGUOUS"]
        assert result_store == {}
        assert business is None or business["answer_kind"] == "CLARIFICATION_REQUIRED"
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_missing_order_id_clarifies_without_agent_or_tools_in_real_d1_graph(coordinator, monkeypatch):
    synthetic = deepcopy(_bundle())
    case = next(c for c in synthetic["cases"] if c["case_id"] == "D1-PRODUCT-001")
    case["input"]["user_query"] = "查询我订单目前的物流状态"
    case["input"]["expected_tool_calls"] = []
    case["input"]["fixture_refs"] = []
    case["input"]["acceptable_evidence_ids"] = []
    case["expected_outcome"] = {}
    _plan, receipt, result = _execute_real_app_offline(coordinator, synthetic, "D1-PRODUCT-001", monkeypatch)
    try:
        assert result["status"] == "SUCCEEDED"
        route_events = [json.loads(row["payload_json"]) for row in result["observations"]["run_events"]
            if row["event_type"] == "route_decided"]
        assert any(row.get("reason_code") == "MISSING_ORDER_IDENTIFIER" for row in route_events)
        assert not any(row["event_type"] == "agent_started" for row in result["observations"]["run_events"])
        assert not any(row["event_type"] in {"tool_started", "tool_finished"}
            for row in result["observations"]["run_events"])
        diagnostic = diagnose_personal_d1_run(case_id="D1-PRODUCT-001", expected_tool_calls=[], result=result)
        assert diagnostic["task_outcome"] == "UNKNOWN"
        coordinator.cleanup(receipt)
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)
