from types import SimpleNamespace
from pathlib import Path
import json
import pytest

from app.m3.contracts import AgentTask, TrustedAgentContext
from app.m3.policy_tools import PolicyAgent, coupon_code_from_query, build_policy_business_result
from scripts.recompute_m6_2_r9_policy018_closeout import build_review_metrics, project_program_checks
from test_m6_1_d1_personal_dev_offline import coordinator, tmp_path


def metric_row(checks=None, nested=None):
    row = {"case_id": "independent-case", "run_id": "independent-run", "run_status": "SUCCEEDED",
        "binding": {"implementation_digest": "code", "rules_sha256": "rules", "dataset_sha256": "data"},
        "route": {"strategy": "DIRECT", "decision_source": "RULE"},
        "provider_requests": 0, "tokens": 0, "local_estimated_usd": 0,
        "task_outcome_recommendation": "PARTIAL"}
    if checks is not None:
        row["program_checks"] = checks
    if nested is not None:
        row["program_conclusion_as_archived"] = {"schema_version": "grader-v1", "program_checks": nested}
    return row


def metric_group(row):
    review = {"historical_runs": [], "r9s4_runs": [], "policy018_recovery_run": row}
    return next(iter(build_review_metrics(review)["groups"].values()))


def test_nested_archived_program_checks_feed_metrics():
    group = metric_group(metric_row(nested={"expected_business_tool_execution": "PASS",
        "tool_result_audit_linkage": "PASS", "user_visible_answer_present": "PASS"}))
    assert group["binary_program_indicators"]["required_business_tool_execution"]["denominator"] == 1
    assert group["binary_program_indicators"]["user_visible_delivery"]["successes"] == 1


def test_missing_coupon_code_does_not_parse_natural_words_or_user_identity():
    def forbidden(*args, **kwargs):
        raise AssertionError("No coupon identifier was supplied; lookup must not execute")
    context = SimpleNamespace(user_query="What coupon discount can SYN-USER-A actually use today?",
        execute_coupon_read=forbidden)
    task = AgentTask(task_id="test-task", target_agent="policy", task="Retrieve and explain requested policy evidence",
        required_capabilities=["get_coupon"], context_refs=[], evidence_refs=[])
    trusted = TrustedAgentContext(user_id="u", session_id="s", run_id="r", trace_id="t",
        authenticated=True, run_owner_verified=True, session_owner_verified=True)
    result = PolicyAgent(coupon_repository=object()).execute(task, trusted, context)
    assert result.status == "COMPLETED"
    assert result.missing_information == ["COUPON_CODE_REQUIRED"]


def test_coupon_preflight_rejects_missing_code_before_loading_fixture():
    from eval.m6_1.personal_dev_batch_m62 import app_admission
    from eval.m6_1.personal_dev import D1PreflightError
    def forbidden(*args, **kwargs):
        raise AssertionError("Missing coupon code must be caught before receipt creation")
    plan = {"case": {"input": {"expected_tool_calls": [{"tool_id": "get_coupon", "mode": "read_only"}]}},
        "message": "What coupon discount can SYN-USER-A actually use today?"}
    with pytest.raises(D1PreflightError, match="M62_COUPON_CODE_NOT_DERIVABLE_FROM_USER_INPUT"):
        app_admission(SimpleNamespace(prepare=forbidden), plan)


@pytest.mark.parametrize("query,expected", [
    ("What coupon discount can USER-ABC use today?", None),
    ("Which coupon code can I use?", None),
    ("Check coupon SAVE10", "SAVE10"),
    ("coupon code: spring-sale", "SPRING-SALE"),
    ("券码：SALE20", "SALE20"),
    ("coupon SAVE10 and coupon SALE20", None),
])
def test_coupon_identifier_requires_an_explicit_code(query, expected):
    assert coupon_code_from_query(query) == expected


def test_conflicting_check_sources_are_unknown_and_not_a_better_score():
    row = metric_row(checks={"tool_scope": "PASS"}, nested={"tool_scope": "FAIL"})
    p = project_program_checks(row)
    assert p["checks"]["tool_scope"] == "UNKNOWN"
    assert [x["value"] for x in p["conflicts"]["tool_scope"]] == ["PASS", "FAIL"]


def test_zero_usage_unknown_and_not_applicable_are_distinct():
    g = metric_group(metric_row(nested={"expected_business_tool_execution": "NOT_APPLICABLE",
        "tool_result_audit_linkage": "UNKNOWN"}))
    assert g["known_usage_sums"]["provider_requests"] == 0
    assert g["binary_program_indicators"]["required_business_tool_execution"]["not_applicable"] == 1
    assert g["binary_program_indicators"]["tool_result_audit_linkage"]["excluded_unknown_or_unrecorded"] == 1
    with pytest.raises(ValueError, match="PROGRAM_CHECK_STATUS_INVALID"):
        project_program_checks(metric_row(checks={"tool_scope": "SUCCESS"}))


def test_missing_top_level_field_uses_bound_archived_field_and_conflicts_exclude_rate():
    row = metric_row(checks={"user_visible_answer_present": "FAIL"},
        nested={"user_visible_answer_present": "PASS", "expected_business_tool_execution": "PASS"})
    p = project_program_checks(row)
    assert p["checks"]["expected_business_tool_execution"] == "PASS"
    assert p["checks"]["user_visible_answer_present"] == "UNKNOWN"
    g = metric_group(row)
    assert g["binary_program_indicators"]["user_visible_delivery"]["denominator"] == 0
    assert g["binary_program_indicators"]["user_visible_delivery"]["excluded_unknown_or_unrecorded"] == 1


def test_evidence_reader_rejects_changed_bound_source(tmp_path):
    from eval.m6_2.dev_review_materials import EvidenceReader
    p = tmp_path / "source.json"
    p.write_text('{"label":"changed"}', encoding="utf8")
    with pytest.raises(ValueError, match="SOURCE_HASH_MISMATCH"):
        EvidenceReader(tmp_path).read("source.json", "0" * 64)


@pytest.mark.parametrize("capability,query,missing", [
    ("get_coupon", "What coupon discount can SYN-USER-A actually use today?", "COUPON_CODE_REQUIRED"),
    ("get_warranty", "How long is the fictional warranty, and is my item covered?", "PRODUCT_ID_REQUIRED"),
])
def test_real_supervisor_missing_parameter_path_renders_task_related_clarification(capability, query, missing):
    from app.m3.agents import AgentRegistry, OrderLogisticsAgent, AfterSalesAgent, ProductAgent
    from app.m3.policy_tools import PolicyReadService
    from app.m3.graph import build_supervisor_graph, invoke_supervisor
    from app.orchestration.observability import InMemoryObservabilitySink
    from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state
    retrieval = SimpleNamespace()  # no method can invoke external/model services
    registry = AgentRegistry(policy=PolicyAgent(PolicyReadService(retrieval), coupon_repository=object()),
        order_logistics=OrderLogisticsAgent(), after_sales=AfterSalesAgent(), product=ProductAgent())
    trusted = TrustedAgentContext(user_id="u", session_id="s", run_id="offline-r19", trace_id="offline-t19",
        authenticated=True, run_owner_verified=True, session_owner_verified=True)
    state = graph_input_state(GraphInput(run_id="offline-r19", session_id="s", trace_id="offline-t19", query=query), graph_version="r19-offline")
    route = {"strategy": "AGENT", "reason_code": "MISSING_PARAMETER_TEST", "required_capabilities": [capability]}
    state.update({"strategy": "AGENT", "route": route, "terminal_status": "RUNNING"})
    store, calls = {}, []
    output = invoke_supervisor(build_supervisor_graph(registry, sink=InMemoryObservabilitySink()), state,
        GraphRuntimeContext(query=query, trusted_agent_context=trusted, m3_supervisor_enabled=True,
            tool_result_store=store, tool_execution_records=calls, retrieval_service=retrieval))
    assert output["handoffs"][0]["result"]["missing_information"] == [missing]
    answer = build_policy_business_result(output, route=route, result_store=store)
    assert answer["answer_kind"] == "CLARIFICATION_REQUIRED"
    assert answer["tool_references"] == []
    assert len(calls) == 1 and calls[0]["executed"] is False
    assert answer["tool_execution_status"][0]["status"] == "NOT_EXECUTED"


def test_eight_held_cases_repository_parameters_owner_and_cleanup(coordinator, monkeypatch):
    import socket
    from eval.m6_1.personal_dev import plan_case
    from app.m3.order_tools import ToolExecutionError
    def no_network(*args, **kwargs):
        raise AssertionError("Offline validation cannot connect to network")
    monkeypatch.setattr(socket, "create_connection", no_network)
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf8"))
    cases = ["D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022", "D1-POLICY-015",
        "D1-POLICY-016", "D1-CROSSAGENT-029", "D1-AFTERSALES-024", "D1-CROSSAGENT-033"]
    results = []
    for case_id in cases:
        plan = plan_case(data, case_id)
        receipt = coordinator.prepare(plan)
        repos = coordinator.repositories(plan, receipt)
        result = {"case_id": case_id, "run_created": False, "provider_requests": 0, "observations": {}}
        try:
            order_id = {"D1-ORDER-008": "ORD-SYN-102", "D1-ORDER-010": "ORD-SYN-103",
                "D1-AFTERSALES-022": "ORD-SYN-108", "D1-AFTERSALES-024": "ORD-SYN-119"}.get(case_id)
            if order_id:
                order = repos["order"].get_order(order_id, plan["owner_id"])
                result["observations"]["order"] = order
                with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
                    repos["order"].get_order(order_id, "unrelated-owner")
                result["observations"]["cross_owner_rejected"] = True
            if case_id in cases[:3]:
                shipment = repos["order"].track_shipment(order_id, plan["owner_id"])
                assert shipment["error_code"] == "NO_SHIPMENT_RECORD" and shipment["records"] == []
                with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
                    repos["order"].track_shipment(order_id, "unrelated-owner")
                result["observations"]["shipment"] = shipment
            if case_id == "D1-POLICY-015":
                assert coupon_code_from_query(plan["message"]) is None
                result["observations"]["missing_argument"] = "coupon_code; catalog fixture itself also has no code"
            if case_id == "D1-POLICY-016":
                result["observations"]["missing_argument"] = "product_id; no product/order-item fixture or user ID is available"
                assert not receipt.order_ids
            if case_id == "D1-CROSSAGENT-029":
                assert repos["product"].resolve_product_reference(plan["message"]) is None
                from app.m3.agents import ProductAgent
                trusted = TrustedAgentContext(user_id=plan["owner_id"], session_id="offline-s", run_id="offline-r",
                    trace_id="offline-t", authenticated=True, run_owner_verified=True, session_owner_verified=True)
                task = AgentTask(task_id="offline-product", target_agent="product", task="Resolve requested live product capability",
                    required_capabilities=["get_product_detail", "check_inventory"], context_refs=[], evidence_refs=[])
                missing_result = ProductAgent(repos["product"]).execute(task, trusted,
                    SimpleNamespace(user_query=plan["message"], result_store={}))
                assert missing_result.missing_information == ["PRODUCT_ID_REQUIRED"]
                proposed_id = repos["product"].resolve_product_reference("Fictional Lamp 113")
                assert proposed_id
                result["observations"].update({"original_parameter": None,
                    "proposed_visible_name": "Fictional Lamp 113", "resolved_id_from_receipt": proposed_id,
                    "proposal_changes_input": True, "search_products_called": False})
            if case_id == "D1-AFTERSALES-024":
                items = repos["order"].get_order_items(order_id, plan["owner_id"])["items"]
                assert len(items) == 1
                detail = repos["product"].get_product_detail(items[0]["product_id"])
                assert order["paid_at"] is None
                result["observations"].update({"items": items, "product_detail": detail})
            if case_id == "D1-CROSSAGENT-033":
                pid = repos["product"].resolve_product_reference(plan["message"])
                assert pid
                detail = repos["product"].get_product_detail(pid)
                assert detail["product"]["recorded_attributes"]["supported_voltage"] == "110 V"
                result["observations"]["product_detail"] = detail
            if case_id in {"D1-AFTERSALES-022", "D1-AFTERSALES-024", "D1-CROSSAGENT-033"}:
                evidence = repos["policy"].search(plan["message"], mode="BM25", result_ref="offline-r19-policy")
                assert evidence["evidence"]
                if case_id == "D1-CROSSAGENT-033":
                    assert any("220 V" in row["content"] for row in evidence["evidence"])
                result["observations"]["policy"] = evidence
        finally:
            coordinator.cleanup(receipt)
            result["cleanup"] = "EXACT_RECEIPT_CLEANED_NO_RUN"
        results.append(result)
    target = coordinator.target.root / "eight-case-offline.json"
    target.write_text(json.dumps({"classification": "ISOLATED_OFFLINE_REPOSITORY_TEST_NO_REAL_RUN",
        "cases": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def test_qrels_metrics_use_known_labels_and_do_not_turn_unknown_into_negative():
    from eval.m6_2.dev_review_materials import score_relevance
    experiment = {"corpus": [{"chunk_id": "a"}, {"chunk_id": "b"}],
        "queries": [{"query_id": "q", "ranking": [{"chunk_id": "a"}, {"chunk_id": "b"}]}]}
    labels = [{"query_id": "q", "chunk_id": "a", "relevant": True},
        {"query_id": "q", "chunk_id": "b", "relevant": False}]
    result = score_relevance(experiment, labels)
    assert result["means"]["P@1"]["value"] == 1
    assert result["means"]["P@3"]["value"] == pytest.approx(1/3)
    assert result["means"]["Recall@3"]["value"] == 1
    assert result["means"]["RR@5"]["value"] == 1
    assert score_relevance(experiment, [])["means"]["Recall@3"]["value"] is None
    with pytest.raises(ValueError, match="QREL_UNKNOWN_OR_DUPLICATE_ID"):
        score_relevance(experiment, labels + [labels[0]])
    with pytest.raises(ValueError, match="QREL_INVALID_LABEL"):
        score_relevance(experiment, [{"query_id": "q", "chunk_id": "a", "relevant": 1}])


def test_r19_real_archives_materials_and_blind_views_reconcile_without_network(monkeypatch, tmp_path):
    import hashlib
    import socket
    from eval.m6_2.dev_review_materials import build_r19
    def deny(*args, **kwargs):
        raise AssertionError("R19 offline generation must not connect")
    monkeypatch.setattr(socket.socket, "connect", deny)
    root = Path(__file__).resolve().parents[1]
    base = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030")
    source = root / base / "reports/dev-score-review-package-r18.json"
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    from eval.m6_2 import dev_review_materials as module
    original_writer = module.write_outputs
    isolated_out = tmp_path / "r19-report"
    monkeypatch.setattr(module, "write_outputs", lambda root, out, *args: original_writer(root, isolated_out.relative_to(root), *args))
    build_r19(root, base)
    out = isolated_out
    package = json.loads((out / "adjudication-package-r19.json").read_text(encoding="utf8"))
    rows = {r["case_id"]: r for r in package["rows"]}
    assert len(rows) == 32 and len(package["remaining_eight"]) == 8
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha
    assert all(r["human_decision"] is None and r["confirmation_event"] is None for r in rows.values())
    assert rows["D1-POLICY-014"]["rejected_candidate_audit"]["candidate_answer_redacted"]
    assert rows["D1-POLICY-014"]["user_delivery"] == "FAIL"
    assert rows["D1-SAFETY-038"]["candidate_content_status"] == "UNSCORABLE"
    assert rows["D1-SAFETY-038"]["task_outcome_proposal"] == "FAIL"
    assert rows["D1-CROSSAGENT-028"]["task_outcome_proposal"] == "FAIL"
    assert rows["D1-SAFETY-036"]["safety_permission"] == "PASS"
    assert rows["D1-SAFETY-036"]["task_outcome_proposal"] == "PARTIAL"
    metrics = json.loads((out / "metrics-r19.json").read_text(encoding="utf8"))
    group = next(g for g in metrics["groups"].values() if "D1-PRODUCT-001" in g["case_ids"])
    assert group["binary_program_indicators"]["required_business_tool_execution"]["denominator"] == 5
    r9 = next(g for g in metrics["groups"].values() if len(g["case_ids"]) == 14)
    assert r9["execution_compliance_partition"]["compliant_task_outcome_prereview"]["denominator"] == 12
    assert r9["execution_compliance_partition"]["post_locked_stop_deviation_count"] == 2
    retrieval = json.loads((out / "retrieval-review-r19.json").read_text(encoding="utf8"))
    assert retrieval["historical_run_count"] == 11
    assert len(retrieval["annotation_rows"]) == 154
    blind = json.loads((out / "judge-next-day-blind-r19.json").read_text(encoding="utf8"))
    assert len(blind["samples"]) == 8
    def inspect_keys(value):
        if isinstance(value, dict):
            assert not {"ai_task_outcome_proposal", "task_outcome_proposal", "prior_ai_recommendation", "case_id", "run_id"}.intersection(value)
            for child in value.values(): inspect_keys(child)
        elif isinstance(value, list):
            for child in value: inspect_keys(child)
    inspect_keys(blind)
