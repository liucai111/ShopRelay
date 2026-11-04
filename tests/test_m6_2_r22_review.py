"""R21 real candidate regression and natural-input application integration, offline only."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.m0 import config as config_module
from app.m2.self_rag import _answer_semantic_issues, _policy_window_facts
from eval.m6_1.personal_dev import plan_case
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.personal_dev_runtime import execute_application_case
from test_m6_2_personal_dev_batch_r7 import isolated_context, tmp_path, _TestLedger

ROOT = Path(__file__).resolve().parents[1]
R21 = ROOT / "docs/eval/m6.2-r325-seven-first-runs-20261031"
WAR = {"chunk_id": "general-warranty", "title": "Warranty", "version": "test-v3",
       "content": "Fictional: 18 months from recorded purchase; actual eligibility requires verified purchase/activation facts."}


@pytest.mark.parametrize("answer", [
    "根据本轮证据，虚构保修期为自记录的购买日期起18个月。实际资格需要核验购买和激活事实。",
    "依据上述来源，保修期为从已记录的购买之日起18个月。资格需要核实购买和激活。",
    "According to the retrieved evidence, the fictional warranty lasts 18 months from recorded purchase.",
    "Warranty is 18 months from recorded purchase. Eligibility requires verified purchase/activation facts.",
])
def test_equivalent_warranty_origins(answer):
    assert _answer_semantic_issues(answer, [WAR], "How long is the warranty?") == []


@pytest.mark.parametrize("answer", [
    "保修期为自记录的购买日期起17个月。", "保修期为自记录的购买日期起18天。",
    "保修期为自记录的激活日期起18个月。", "食品保修期为自记录的购买日期起18个月。",
    "保修期为自未核实的购买日期起18个月。", "保修期为18个月。",
    "资格需要核验购买事实。", "资格无需核验购买和激活事实。",
    "Eligibility requires verified activation facts.", "退货期限为自记录的购买日期起18个月。",
])
def test_window_or_eligibility_transfer_is_not_accepted(answer):
    assert _answer_semantic_issues(answer, [WAR], "What is the policy?")


def test_refund_phase_uses_only_this_source_topic_not_other_document():
    source = {"chunk_id": "refund-rule", "title": "refund policy", "content":
        "Application may be reviewed before shipment or after approved return; submission is not approval or payment."}
    assert not _answer_semantic_issues("退款申请可在发货前接受审核，提交不等于获批或退款到账。", [source])
    wrong = {**source, "chunk_id": "return-rule", "title": "return policy"}
    assert _answer_semantic_issues("退款申请可在发货前接受审核。", [wrong])
    assert _answer_semantic_issues("退款已经到账。", [source])
    denied = {"chunk_id":"refund", "title":"refund policy", "content":"Refund applications cannot be submitted before shipment."}
    assert _answer_semantic_issues("退款申请可在发货前提交。", [denied])


def test_saved_complete_candidates_replay_without_changing_historical_delivery():
    replay = json.loads((R21 / "reports/candidate-replay-observations.json").read_text(encoding="utf8"))
    for row in replay["items"]:
        assert not _answer_semantic_issues(row["captured_candidate"], row["source_rows_from_bound_fixture_and_same_run_citation_ids"])
        assert row["candidate_is_not_delivered"]


@pytest.mark.parametrize("case_id,tools", [
    ("D1-AFTERSALES-022", {"get_order", "track_shipment", "search_knowledge"}),
    ("D1-CROSSAGENT-029", {"get_product_detail", "check_inventory", "search_knowledge"}),
    ("D1-AFTERSALES-024", {"get_order", "get_order_items", "get_product_detail", "search_knowledge"}),
    ("D1-POLICY-016", {"search_knowledge"}),
])
def test_actual_natural_input_app_repository_audit_persistence_cleanup(
        isolated_context, monkeypatch, tmp_path, case_id, tools):
    target, coordinator = isolated_context
    data = json.loads((ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.5.json").read_text(encoding="utf8"))
    plan = plan_case(data, case_id)
    seen = []
    class SDK:
        def __init__(self): self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **_kwargs): return self
        def create(self, *, messages, **kwargs):
            # No classifier response/forced route: these natural tasks must be routed by application rules.
            assert "Classify the user's" not in messages[0]["content"]
            payload = json.loads(messages[-1]["content"]);seen.append(payload)
            candidate = next(row["captured_candidate"] for row in json.loads(
                (R21 / "reports/candidate-replay-observations.json").read_text(encoding="utf8"))["items"]
                if row["case_id"] == "D1-POLICY-016")
            output = {"answer": candidate, "citations": [r["chunk_id"] for r in payload["evidence"]]}
            return SimpleNamespace(model="offline-only", choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(output)))], usage=SimpleNamespace(total_tokens=90,
                    model_dump=lambda: {"prompt_tokens":70,"completion_tokens":20,"total_tokens":90}))
    sdk = SDK()
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(config_module.load_settings(require_api_key=False), api_key="offline-only")
    receipt = coordinator.prepare(plan)
    try:
        limits = BudgetLimits(provider_calls=6, tool_calls=8, total_tokens=120000,
            estimated_usd=.18, max_calls_per_trial=14, max_duration_seconds=180)
        result = execute_application_case(coordinator, plan, receipt, _TestLedger(limits), settings)
        assert result["status"] == "SUCCEEDED", (case_id, result["failure_code"])
        events = result["observations"]["run_events"]
        observed = {json.loads(e["payload_json"])["tool"] for e in events if e["event_type"] == "tool_finished"}
        if any(e["event_type"] == "policy_retrieval_finished" for e in events): observed.add("search_knowledge")
        assert observed == tools
        assert result["result"]["answer"]
        if len(tools) > 1:
            nodes = {json.loads(e["payload_json"]).get("agent") for e in events if e["event_type"] == "agent_started"}
            assert "policy" in nodes and len(nodes) >= 2
            assert not seen  # deterministic specialists, not a forced model answer
        path = tmp_path / "archive.json";path.write_text(json.dumps({"case_id":case_id,"result":result},default=str),encoding="utf8")
        assert coordinator.diagnostic_cleanup_safety(receipt, path)["safe_to_cleanup"]
        coordinator.cleanup(receipt)
        assert coordinator.diagnostic_cleanup_evidence(receipt,cleaned=True)["residual_case_rows"] == 0
    finally:
        from sqlalchemy import text
        with coordinator.factory() as db:active=db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id: coordinator.cleanup(receipt)


def test_generic_routes_respect_restricted_capabilities_and_new_natural_wording():
    from app.m1.routing import decide_route
    for query, caps in [
        ("For order ORD-EXAMPLE-77, may I request a refund if it is not shipped?", {"get_order","track_shipment","search_knowledge"}),
        ("For ORD-EXAMPLE-66, what is required before requesting a return for a sealed accessory of the wrong size?", {"get_order","get_order_items","get_product_detail","search_knowledge"}),
        ("Can delivery policy promise tomorrow for the product's unknown stock?", {"get_product_detail","check_inventory","search_knowledge"}),
    ]:
        assert set(decide_route(query, available_capabilities=caps).required_capabilities) == caps
        assert decide_route(query, available_capabilities=[]).strategy == "DIRECT"


def test_legal_shipment_path_requires_owner_and_linked_audit_not_exact_tool_names():
    from eval.m6_1.personal_dev_batch_m62 import review_task_read_coverage
    checks = {k:"PASS" for k in ("run_terminal","run_trace_association","tool_scope","tool_audit_completeness",
        "tool_result_audit_linkage","budget_and_cleanup","owner_scope_evidence")}
    checks["expected_business_tool_execution"]="FAIL"
    grade={"program_checks":checks,"observed_tools":["track_shipment"],"policy_retrieval_call_ids":[]}
    rules={"owner_required":True,"zero_calls_required":False,"version":"review-v1",
        "required_evidence_groups":[{"fact":"stored shipment events","any_of_tool_sets":[["track_shipment"]]}]}
    assert review_task_read_coverage(grade,rules)["status"]=="PASS"
    grade["program_checks"]["owner_scope_evidence"]="UNKNOWN"
    assert review_task_read_coverage(grade,rules)["status"]=="UNKNOWN"


def test_material_generation_uses_raw_bindings_and_recomputes_without_history_writes(tmp_path,monkeypatch):
    import hashlib
    from eval.m6_2 import r21_review_closeout as module
    original=ROOT/module.OUT
    for name in ("ai-proposals.json","retrieval-ai-plan.json"):
        (tmp_path/name).write_bytes((original/name).read_bytes())
    (tmp_path/"source-before").mkdir()
    for source in (original/"source-before").iterdir():
        (tmp_path/"source-before"/source.name).write_bytes(source.read_bytes())
    monkeypatch.setattr(module,"OUT",tmp_path.relative_to(ROOT))
    result=module.build(ROOT)
    files=[tmp_path/name for name in ("review-and-metrics.json","retrieval-review.json","judge-plan.json","exit-matrix.json")]
    first=[hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    module.build(ROOT)
    assert first==[hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
    assert result["run_count"]==39 and result["binding_groups"]==9
    history=json.loads((tmp_path/"history-verification.json").read_text(encoding="utf8"))
    assert history["R21_inventory_total"]==107 and history["unexplained_changes"]==[]
    assert history["R6_original_bytes_restored"] is False
    baseline=json.loads((tmp_path/"rule-route-baseline.json").read_text(encoding="utf8"))
    assert baseline["total"]==52 and baseline["splits"]=={"dev":28,"validation":24}
    assert baseline["not_scored"]==3 and baseline["rule_strategy_accuracy"]["N"]==49
    assert all("latency_ms" not in r["prediction"] for r in baseline["rows"])
    assert all(r["prediction"]["decision_source"]=="FALLBACK" for r in baseline["rows"] if r["status"]=="NOT_SCORED_NEEDS_MODEL")
    review=json.loads(files[0].read_text(encoding="utf8"))
    rows={r["case_id"]:r for r in review["seven"]}
    assert rows["D1-ORDER-008"]["task_evidence_review"]["status"]=="PASS"
    assert rows["D1-ORDER-008"]["original_program_grade"]["task_outcome"]=="FAIL"
    assert rows["D1-POLICY-016"]["answer"] is None
    retrieval=json.loads(files[1].read_text(encoding="utf8"))
    assert len(retrieval["annotations"])==196 and retrieval["reviewed_qrels"]==0
    assert retrieval["human_metrics"]["means"]["P@1"]["value"] is None
    judge=json.loads(files[2].read_text(encoding="utf8"))
    assert len(judge["samples"])==4 and judge["existing_raw_responses_reparsed"]["new_requests"]==0
    for row in judge["samples"]:
        payload=json.loads(row["actual_messages"][1]["content"])
        assert set(payload)=={"question","answer_or_claim","applicable_sources","rubric","scoring_target"}
        assert "dimension_scores" in row["actual_messages"][0]["content"]
    form=json.loads((tmp_path/"relevance-review-form.json").read_text(encoding="utf8"))
    form.update({"reviewer":"OFFLINE_TEST_REVIEWER","reviewed_at_utc":"2026-11-02T00:00:00Z","review_method":"OFFLINE_TEST_NOT_REAL_REVIEW"})
    form["rows"][0]["document_text"]="changed source with same IDs and row count"
    (tmp_path/"relevance-review-completed.json").write_text(json.dumps(form),encoding="utf8")
    with pytest.raises(ValueError,match="QREL_ITEM_SOURCE_DRIFT"): module.score_completed(ROOT)


def test_candidate_reference_metrics_do_not_hide_missing_or_foreign_citations():
    from eval.m6_2.dev_review_materials import archive_projection
    from eval.m6_2.r21_review_closeout import raw_checks
    archive=json.loads((ROOT/"docs/eval/personal-dev-runs/fb95c6f5222b4be3a1d28ebeabdb5d69.json").read_text(encoding="utf8"))
    raw=archive_projection(archive,"48a1660f-74d9-473e-b837-b2fc05a1f34a")
    raw["run_id"]="48a1660f-74d9-473e-b837-b2fc05a1f34a"
    raw["tool_references"]=raw["result_payload"].get("tool_references",[])
    raw["rejected_candidate_audit"]=raw["rejection"]
    data=json.loads((ROOT/"eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.5.json").read_text(encoding="utf8"))
    case=next(r for r in data["cases"] if r["case_id"]=="D1-POLICY-016")
    assert raw_checks(raw,case)["candidate_citation_scope"]=="PASS"
    for ids in ([],["not-this-run"]):
        raw["rejected_candidate_audit"]["candidate_citation_ids"]=ids
        assert raw_checks(raw,case)["candidate_citation_scope"]=="FAIL"
    raw["rejected_candidate_audit"]=None
    assert raw_checks(raw,case)["candidate_citation_scope"]=="NOT_APPLICABLE"


def test_business_warranty_retrieval_requires_matching_call_and_result_reference():
    from eval.m6_2.dev_review_materials import archive_projection
    from eval.m6_2.r21_review_closeout import raw_checks
    archive=json.loads((ROOT/"docs/eval/personal-dev-runs/92bfa9a40b1f4828a0e12cf36e22f1a8.json").read_text(encoding="utf8"))
    run_id=archive["result"]["run_id"];raw=archive_projection(archive,run_id)
    raw.update(run_id=run_id,tool_references=raw["result_payload"]["tool_references"],rejected_candidate_audit=None)
    data=json.loads((ROOT/"eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf8"))
    case=next(r for r in data["cases"] if r["case_id"]=="D1-CROSSAGENT-030")
    assert raw_checks(raw,case)["delivered_citation_scope"]=="PASS"
    for e in raw["events"]:
        if e["event_type"]=="retrieval_hop": e["payload"]["result_ref"]="other-call-result"
    assert raw_checks(raw,case)["delivered_citation_scope"]=="FAIL"
