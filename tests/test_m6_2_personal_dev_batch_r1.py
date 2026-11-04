"""M6.2 exact-subset admission, durable attempt and real offline app path tests."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text

from app.m0.config import load_settings
from app.m1.models import Run, RunEvent, Trace, User, Order
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.personal_dev import D1Coordinator, D1PreflightError, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_batch_m62 import (DurableAttemptJournal, app_admission,
    batch_stop_reason, derive_limits, load_exact_profile, plan_without_unauthorized_targets, score_archive)
from eval.m6_1.personal_dev_runtime import execute_application_case


@pytest.fixture
def tmp_path():
    path = Path(__file__).resolve().parents[1] / "data/eval/offline-tests" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fixture_context(tmp_path):
    root = Path(__file__).resolve().parents[1]
    bundle = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf-8"))
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    try:
        yield root, bundle, target, D1Coordinator(target, factory)
    finally:
        engine.dispose()


def test_budget_envelope_scales_exactly_with_subset_size():
    per_case, batch = derive_limits(3)
    assert per_case.provider_calls == 6 and per_case.tool_calls == 8
    assert per_case.total_tokens == 120000 and per_case.estimated_usd == .18
    assert batch.provider_calls == 18 and batch.tool_calls == 24
    assert batch.total_tokens == 360000 and batch.estimated_usd == pytest.approx(.54)
    assert batch.max_duration_seconds == 540 and batch.max_retries == 0
    with pytest.raises(D1PreflightError):
        derive_limits(32)


@pytest.mark.parametrize("events, expected", [
    ([], set()),
    ([{"event":"CASE_ATTEMPT_RESERVED","batch_id":"b","case_id":"A"}], {"A"}),
])
def test_attempt_journal_is_durable_and_replay_proof_survives_reopen(tmp_path, events, expected):
    journal = DurableAttemptJournal(tmp_path / "batch.jsonl", batch_id="b")
    if events:
        journal.acquire_batch()
        journal.path.write_text(json.dumps(events[0]) + "\n", encoding="utf-8")
    assert DurableAttemptJournal(journal.path, batch_id="b").consumed_cases() == expected


def test_attempt_journal_consumes_before_trial_and_rejects_replay(tmp_path):
    journal = DurableAttemptJournal(tmp_path / "batch.jsonl", batch_id="batch-1")
    journal.acquire_batch()
    journal.reserve_attempt("D1-POLICY-013", budget={"provider_calls": 6}, bindings={"code": "x"})
    restarted = DurableAttemptJournal(journal.path, batch_id="batch-1")
    assert restarted.consumed_cases() == {"D1-POLICY-013"}
    with pytest.raises(D1PreflightError, match="ALREADY_CONSUMED"):
        restarted.reserve_attempt("D1-POLICY-013", budget={"provider_calls": 6}, bindings={})
    with pytest.raises(D1PreflightError, match="LOCK_PRESENT"):
        restarted.acquire_batch()


def test_zero_tool_refusal_does_not_preload_or_query_target(fixture_context):
    _root, bundle, _target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-SAFETY-037")
    safe = plan_without_unauthorized_targets(plan)
    assert safe["facts"] == [] and safe["orders"] == [] and safe["order_ids"] == []
    result = app_admission(coordinator, safe)
    assert result["status"] == "NO_BUSINESS_READS_REQUIRED"
    assert result["read_count"] == 0 and result["fixture_receipt"] is None
    with coordinator.factory() as db:
        assert db.execute(select(Order.order_id)).all() == []
        assert db.execute(select(User.user_id)).all() == []
        assert db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one() == (None, None)


def test_product_name_resolves_through_real_repository_during_admission(fixture_context):
    _root, bundle, _target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-PRODUCT-001")
    result = app_admission(coordinator, plan)
    assert result["status"] == "APPLICATION_REPOSITORY_PREFLIGHT_PASS"
    assert result["read_count"] <= 8
    detail_ids = {x.get("product_id") for x in result["repository_reads"] if x["tool"] == "get_product_detail"}
    assert len(detail_ids) == 2
    assert result["receipt_cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED"


def test_admission_never_uses_unallowlisted_product_search(fixture_context, monkeypatch):
    _root, bundle, _target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-PRODUCT-006")
    plan["message"] = "Please tell me about an unidentifiable desk lamp"
    # This case permits only get_product_detail. A preflight search would be
    # an unauthorized capability even if it were used only to derive an ID.
    from eval.m6_1.fixture_read_models import FixtureProductRepository
    monkeypatch.setattr(FixtureProductRepository, "search_products", lambda *_a, **_k:
        pytest.fail("search_products is not allowlisted for this case"))
    result = app_admission(coordinator, plan)
    assert result["status"] == "APPLICATION_ADMISSION_BLOCKED_PARAMETER_NOT_DERIVABLE"
    assert result["blocked_reason"] == "PRODUCT_ID_NOT_DERIVABLE_WITHOUT_ALLOWED_SEARCH"
    assert result["receipt_cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED"
    assert result["read_count"] == 0


def test_warranty_admission_reads_order_items_once(fixture_context, monkeypatch):
    _root, bundle, _target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-CROSSAGENT-030")
    from eval.m6_1.personal_dev import D1Coordinator as CoordinatorType
    original = CoordinatorType.repositories
    counts = {"get_order_items": 0}
    def wrapped(self, *args, **kwargs):
        repos = original(self, *args, **kwargs)
        repository = repos["order"]
        method = repository.get_order_items
        def counted(*a, **k):
            counts["get_order_items"] += 1
            return method(*a, **k)
        repository.get_order_items = counted
        return repos
    monkeypatch.setattr(CoordinatorType, "repositories", wrapped)
    result = app_admission(coordinator, plan)
    assert result["status"] == "APPLICATION_REPOSITORY_PREFLIGHT_PASS"
    assert counts["get_order_items"] == 1
    assert sum(row["tool"] == "get_order_items" for row in result["repository_reads"]) == 1


def test_application_admission_reuses_product_search_result(fixture_context, monkeypatch):
    _root, bundle, _target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-PRODUCT-001")
    from eval.m6_1.personal_dev import D1Coordinator as CoordinatorType
    original = CoordinatorType.repositories
    counts = {"search_products": 0}
    def wrapped(self, *args, **kwargs):
        repos = original(self, *args, **kwargs)
        repository = repos["product"]
        method = repository.search_products
        def counted(*a, **k):
            counts["search_products"] += 1
            return method(*a, **k)
        repository.search_products = counted
        return repos
    monkeypatch.setattr(CoordinatorType, "repositories", wrapped)
    result = app_admission(coordinator, plan)
    assert result["status"] == "APPLICATION_REPOSITORY_PREFLIGHT_PASS"
    assert counts["search_products"] == 1
    assert sum(row["tool"] == "search_products" for row in result["repository_reads"]) == 1


def test_code_grader_does_not_equate_direct_agent_flag_or_run_success_with_task_success():
    case = {"case_id": "SYNTHETIC-CASE", "input": {"expected_tool_calls": []}}
    archive = {"result": {"run_id":"r", "status":"SUCCEEDED", "result":{"route":{"strategy":"DIRECT","decision_source":"RULE","required_capabilities":[]},"tool_call_audit":[]},
        "observations":{"runs":[{"run_id":"r","status":"SUCCEEDED","result_json":"{}"}],
            "run_events":[{"run_id":"r","event_type":"route_decided","payload_json":"{}"}],
            "traces":[{"run_id":"r"}],"messages":[{"role":"assistant","content":"Please clarify."}]}}}
    budget = {"complete":True,"settled":True,"active_reservations":0,"unknown_tokens":False,"unknown_cost":False}
    grade = score_archive(case, archive, budget_snapshot=budget, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["program_checks"]["route_within_allowlist"] == "PASS"
    assert grade["agent_started"] is False
    assert grade["task_outcome"] == "UNSCORED_PENDING_AI_REVIEW"
    assert grade["program_checks"]["provider_execution"] == "NOT_APPLICABLE"
    assert batch_stop_reason(grade, []) is None


def test_provider_fallback_is_a_batch_stop_but_safe_task_failure_continues():
    case = {"case_id":"MODEL-ROUTE", "input":{"expected_tool_calls":[]}}
    archive = {"result":{"run_id":"r", "status":"SUCCEEDED", "result":{"route":{"strategy":"DIRECT",
        "decision_source":"FALLBACK", "fallback_error":"ROUTE_CLASSIFICATION_FAILED", "required_capabilities":[]},
        "tool_call_audit":[]}, "observations":{"runs":[{"run_id":"r","status":"SUCCEEDED"}],
        "run_events":[{"run_id":"r","event_type":"route_decided","payload_json":"{}"}],
        "traces":[{"run_id":"r"}],"messages":[{"role":"assistant","content":"I could not classify this request."}]}}}
    budget={"complete":True,"settled":True,"active_reservations":0,"unknown_tokens":False,"unknown_cost":False}
    grade=score_archive(case,archive,budget_snapshot=budget,cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["program_checks"]["provider_execution"] == "FAIL"
    assert batch_stop_reason(grade, []) == "PROVIDER_OR_PUBLIC_CONTRACT_FAILURE"
    grade["program_checks"]["provider_execution"] = "NOT_APPLICABLE"
    grade["task_outcome"] = "FAIL"
    assert batch_stop_reason(grade, []) is None


def test_r6_only_evidenced_semantic_rejection_is_continuable():
    from eval.m6_1.personal_dev_batch_m62 import batch_stop_reason_r6, score_archive_r6
    diag = {"schema_version": "provider-failure-diagnostic-v1", "failure_phase": "CLASSIFIER_SEMANTIC_CONTRACT",
        "error_category": "CLASSIFIER_OUTPUT_INVALID", "completion_call_entered": True,
        "provider_response_received": True, "request_delivery": "RESPONSE_RECEIVED",
        "exception_types": ["RouteClassifierContractError"], "route_stage": "CLASSIFIER_SEMANTIC_CONTRACT",
        "route_error_code": "DIRECT_REFUSE_CAPABILITY_MISMATCH", "field_path": "required_capabilities",
        "usage_status": "UNKNOWN", "billing_status": "UNKNOWN",
        "response_summary": {"schema_version": "route-response-summary-v1", "provider_response_received": True,
            "choice_count": 1, "finish_reason": "stop", "content_present": True, "content_length": 80,
            "reasoning_content_present": False, "reasoning_content_length": 0, "tool_calls_present": False,
            "usage_status": "RECORDED", "usage": {"prompt_tokens": 70, "completion_tokens": 11, "total_tokens": 81}}}
    case = {"case_id": "SYNTHETIC-SEMANTIC-REJECTION", "input": {"expected_tool_calls": [
        {"tool_id": "get_product_detail", "mode": "read_only"}]}}
    archive = {"result": {"run_id": "r", "status": "SUCCEEDED", "result": {"trace_id": "t",
        "route": {"strategy": "DIRECT", "decision_source": "FALLBACK", "fallback_error": "DIRECT_REFUSE_CAPABILITY_MISMATCH",
            "required_capabilities": [], "provider_diagnostic": diag}, "tool_call_audit": []},
        "observations": {"runs": [{"run_id": "r", "status": "SUCCEEDED", "result_json": "{}"}],
            "run_events": [{"run_id": "r", "trace_id": "t", "event_type": "route_decided", "payload_json": "{}"}],
            "traces": [{"run_id": "r", "trace_id": "t"}],
            "messages": [{"role": "assistant", "content": "Please provide a product identifier."}]}}}
    budget = {"complete": True, "settled": True, "active_reservations": 0, "unknown_tokens": False, "unknown_cost": False}
    grade = score_archive_r6(case, archive, budget_snapshot=budget, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["task_outcome"] == "FAIL"
    assert grade["continuable_single_model_failure"] is True
    assert batch_stop_reason_r6(grade, ["get_product_detail"]) is None
    archive["result"]["result"]["route"]["provider_diagnostic"]["response_summary"]["usage_status"] = "UNKNOWN"
    grade = score_archive_r6(case, archive, budget_snapshot=budget, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["continuable_single_model_failure"] is False
    assert batch_stop_reason_r6(grade, ["get_product_detail"]) == "PROVIDER_OR_PUBLIC_CONTRACT_FAILURE"


def test_r7_replays_rejected_policy_answer_from_same_run_failure_projection():
    from eval.m6_1.personal_dev_batch_m62 import score_archive_r7
    root = Path(__file__).resolve().parents[1]
    archive = json.loads((root / "docs/eval/personal-dev-runs/1184132fd96849d087ff104ed12f5baf.json").read_text(encoding="utf-8"))
    case = json.loads((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_text(encoding="utf-8"))
    case = next(row for row in case["cases"] if row["case_id"] == "D1-POLICY-014")
    budget = {"complete": True, "settled": True, "active_reservations": 0,
        "unknown_tokens": False, "unknown_cost": False, "provider_requests": 1, "tokens": 787}
    grade = score_archive_r7(case, archive, budget_snapshot=budget,
        cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["route_projection_source"] == "SAME_RUN_ROUTE_DECIDED_EVENT"
    assert grade["rejection_audit_projection"]["status"] == "CAPTURED_REDACTED"
    assert grade["candidate_assessment"]["status"] == "REJECTED_CANDIDATE_NOT_DELIVERED"
    assert grade["candidate_assessment"]["is_user_output"] is False
    assert grade["candidate_assessment"]["citation_scope"] == "PASS"
    assert grade["program_checks"]["policy_retrieval_audit"] == "PASS"
    assert grade["program_checks"]["user_visible_answer_present"] == "FAIL"
    assert grade["task_outcome"] == "FAIL"
    assert grade["continue_decision"]["decision"] == "CONTINUE"

    # Missing/unknown audit or inconsistent usage must stop, never become zero.
    observations = archive["result"]["observations"]
    run = observations["runs"][0]
    original = run["error_detail_json"]
    payload = json.loads(original)
    payload["answer_rejection_audit"]["usage_summary"]["total_units"] = None
    run["error_detail_json"] = json.dumps(payload)
    stopped = score_archive_r7(case, archive, budget_snapshot=budget,
        cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert stopped["continue_decision"]["decision"] == "STOP"
    assert "failure_stage_or_usage_unknown" in stopped["continue_decision"]["blocking_items"]
    run["error_detail_json"] = "{}"
    missing = score_archive_r7(case, archive, budget_snapshot=budget,
        cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert missing["rejection_audit_projection"]["status"] == "NOT_CAPTURED"
    assert missing["continue_decision"]["decision"] == "STOP"
    run["error_detail_json"] = original


def test_r7_rejected_answer_is_persisted_through_real_offline_app_chain(fixture_context, monkeypatch):
    from app.m0 import config as config_module
    from eval.m6_1.personal_dev_batch_m62 import score_archive_r7
    from eval.m6_1.personal_dev_runtime import execute_application_case

    root, bundle, target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-POLICY-014")
    receipt = coordinator.prepare(plan)

    class RejectingPolicyAnswerSDK:
        def __init__(self):
            self.requests = []
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **_kwargs): return self
        def create(self, *, messages, **_kwargs):
            self.requests.append(messages)
            prompt, payload = messages[0]["content"], json.loads(messages[-1]["content"])
            if "Classify the user's" in prompt:
                output = {"strategy": "SINGLE", "sub_strategy": "BM25", "intent_labels": ["policy"],
                    "safety_decision": "ALLOW", "reason_code": "POLICY_QUESTION",
                    "needs_clarification": False, "clarification_question": None,
                    "required_capabilities": ["search_knowledge"]}
            elif "original_query" in payload:
                output = {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [{"aspect_id": "conflict", "text": "compare the requested policy terms", "query_anchor": payload["original_query"]}], "user_provided_facts": []}
            else:
                evidence = payload["evidence"]
                output = {"answer": "The two policies are the same and gift cards do not expire.",
                    "citations": [row["chunk_id"] for row in evidence]}
            return SimpleNamespace(model="offline-rejection-fixture", choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=json.dumps(output)))], usage=_Usage(90))

    sdk = RejectingPolicyAnswerSDK()
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(load_settings(require_api_key=False), api_key="offline-only",
        database_url=f"sqlite:///{target.path.as_posix()}", orchestrator="langgraph", m3_supervisor_enabled=True)
    limits = BudgetLimits(provider_calls=6, tool_calls=8, total_tokens=120000, estimated_usd=.18,
        max_calls_per_trial=8, max_duration_seconds=180, max_retries=0)
    ledger = _RecordingLedger(limits)
    archive_path = root / "data/eval/offline-tests" / f"m62-r7-rejected-{uuid.uuid4().hex}.json"
    try:
        result = execute_application_case(coordinator, plan, receipt, ledger, settings)
        assert result["status"] == "FAILED"
        assert len(sdk.requests) == 1
        audit_events = [event for event in result["observations"]["run_events"]
            if event["event_type"] == "policy_retrieval_finished"]
        assert len(audit_events) == 1
        archive_path.write_text(json.dumps({"case_id": plan["case"]["case_id"], "result": result},
            ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        grade = score_archive_r7(plan["case"], json.loads(archive_path.read_text(encoding="utf-8")),
            budget_snapshot={"complete": True, "settled": True, "active_reservations": 0,
                "unknown_tokens": False, "unknown_cost": False, "provider_requests": 1, "tokens": 90},
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        (target.root / "rejection-diagnostic.json").write_text(json.dumps({"grade": grade, "result": result}, ensure_ascii=False, default=str), encoding="utf8")
        assert grade["candidate_assessment"]["status"] == "REJECTED_CANDIDATE_NOT_DELIVERED"
        assert grade["candidate_assessment"]["redacted_text"]
        assert grade["program_checks"]["user_visible_answer_present"] == "FAIL"
        assert grade["continue_decision"]["decision"] == "CONTINUE"
        assert not any(row.get("role") == "assistant" for row in result["observations"]["messages"])
    finally:
        with coordinator.factory() as db:
            marker = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if marker == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_code_grader_does_not_treat_missing_zero_call_tool_audit_as_empty_success():
    case = {"case_id":"NO-AUDIT", "input":{"expected_tool_calls":[]}}
    archive = {"result": {"run_id":"r", "status":"SUCCEEDED", "result":{"route":{"strategy":"DIRECT","required_capabilities":[]}},
        "observations":{"runs":[{"run_id":"r","status":"SUCCEEDED","result_json":"{}"}],
            "run_events":[{"run_id":"r","event_type":"route_decided","payload_json":"{}"}],
            "traces":[{"run_id":"r"}],"messages":[{"role":"assistant","content":"Please clarify."}]}}}
    grade = score_archive(case, archive, budget_snapshot={"complete":True,"settled":True,
        "active_reservations":0,"unknown_tokens":False,"unknown_cost":False},
        cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["program_checks"]["tool_result_audit_linkage"] == "UNKNOWN"
    assert grade["task_outcome"] == "UNKNOWN"


def test_policy_expected_case_without_retrieval_is_safe_task_failure_not_audit_success():
    case = {"case_id":"D1-POLICY-013", "input":{"expected_tool_calls":[{"tool_id":"search_knowledge","mode":"read_only"}]}}
    archive = {"result":{"run_id":"r","status":"SUCCEEDED","result":{"route":{"strategy":"DIRECT","decision_source":"RULE","required_capabilities":[]},"tool_call_audit":[]},
        "observations":{"runs":[{"run_id":"r","status":"SUCCEEDED","result_json":"{}"}],
            "run_events":[{"run_id":"r","event_type":"started","payload_json":"{}"},
                          {"run_id":"r","event_type":"route_decided","payload_json":"{}"}],
            "traces":[{"run_id":"r"}],"messages":[{"role":"assistant","content":"Hello."}]}}}
    budget = {"complete":True,"settled":True,"active_reservations":0,"unknown_tokens":False,"unknown_cost":False}
    grade = score_archive(case, archive, budget_snapshot=budget, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["program_checks"]["tool_audit_completeness"] == "PASS"
    assert grade["program_checks"]["expected_business_tool_execution"] == "NOT_APPLICABLE"
    assert grade["program_checks"]["policy_retrieval_audit"] == "FAIL"
    assert grade["task_outcome"] == "FAIL"


def test_missing_run_event_snapshot_is_not_misclassified_as_zero_tool_calls():
    case = {"case_id":"SYNTHETIC-CASE", "input":{"expected_tool_calls":[]}}
    archive = {"result":{"run_id":"r","status":"SUCCEEDED","result":{"route":{"strategy":"DIRECT","required_capabilities":[]}},
        "observations":{"runs":[{"run_id":"r","status":"SUCCEEDED"}],"run_events":[],"traces":[],
            "messages":[{"role":"assistant","content":"Please clarify."}]}}}
    budget = {"complete":True,"settled":True,"active_reservations":0,"unknown_tokens":False,"unknown_cost":False}
    grade = score_archive(case, archive, budget_snapshot=budget, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["program_checks"]["tool_audit_completeness"] == "UNKNOWN"


def test_exact_profile_rejects_case_set_drift_without_rewriting_old_profile(tmp_path, monkeypatch):
    import hashlib
    import shutil
    from eval.m6_1 import personal_dev_batch_m62 as module
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "m62-package"
    package.mkdir()
    matrix_source = root / "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"
    audit_path = "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/repository-admission-r4.json"
    shutil.copyfile(matrix_source, package / "all40-case-admission-matrix-r2.json")
    monkeypatch.setattr(module, "PACKAGE_REL", str(package.relative_to(root)))
    matrix = json.loads((package / "all40-case-admission-matrix-r2.json").read_text(encoding="utf-8"))
    rules = {"case_order":["D1-POLICY-013"],
        "source_matrix_case_ids_sha256":hashlib.sha256("\n".join(c["case_id"] for c in matrix["cases"]).encode()).hexdigest()}
    rules_path = package / "rules.json"
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    manifest = {"dataset":{"path": "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json", "sha256":"8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"},
        "case_universe_count":40,"case_admission_counts":matrix["admission_counts"],
        "admission_matrix_path":"docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json",
        "admission_matrix_sha256":hashlib.sha256((root / "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json").read_bytes()).hexdigest(),
        "authorized_candidate_case_ids":["D1-POLICY-013"],"proposed_order":["D1-POLICY-013"],
        "locked_rules_sha256":hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        "repository_audit":{"path":audit_path,"sha256":hashlib.sha256((root / audit_path).read_bytes()).hexdigest()},
        "bindings":{"implementation_digest":module.implementation_digest(root)}}
    manifest_path = package / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_exact_profile(root, manifest_path=manifest_path, rules_path=rules_path)[0]["authorized_candidate_case_ids"] == ["D1-POLICY-013"]
    manifest["authorized_candidate_case_ids"] = ["D1-POLICY-013", "D1-ORDER-008"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(D1PreflightError, match="CASE_SET_OR_ORDER"):
        load_exact_profile(root, manifest_path=manifest_path, rules_path=rules_path)


def test_proxy_is_uniform_and_httpx_does_not_bypass_provider(monkeypatch):
    from scripts.run_m6_2_r324_personal_dev_batch import ensure_proxy_configuration
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    result = ensure_proxy_configuration()
    assert result["six_variables_match"] and result["provider_bypass_excluded"]
    assert result["httpx_https_proxy"] == "http://127.0.0.1:7892"
    monkeypatch.setenv("no_proxy", "api.deepseek.com")
    with pytest.raises(D1PreflightError, match="NO_PROXY"):
        ensure_proxy_configuration()


def test_application_admission_binding_rejects_wrong_or_missing_auth_bound_values():
    from scripts.run_m6_2_r324_personal_dev_batch import validate_application_admission_binding
    values = {"admission":"PASS","case_ids":["D1-POLICY-013"],
        "dataset_sha256":"d","implementation_digest":"c","database_identity":"db",
        "manifest_sha256":"m","rules_sha256":"r","pricing_sha256":"p",
        "run_created":False,"provider_requests":0}
    validate_application_admission_binding(values, case_ids=["D1-POLICY-013"],
        manifest_sha256="m", rules_sha256="r", code_sha256="c", dataset_sha256="d",
        database_identity="db", pricing_sha256="p")
    for key, bad in (("manifest_sha256","other"),("pricing_sha256",None),
                     ("run_created",True),("provider_requests",1)):
        altered = dict(values); altered[key] = bad
        with pytest.raises(D1PreflightError, match="ADMISSION_BINDING"):
            validate_application_admission_binding(altered, case_ids=["D1-POLICY-013"],
                manifest_sha256="m", rules_sha256="r", code_sha256="c", dataset_sha256="d",
                database_identity="db", pricing_sha256="p")


class _Usage:
    def __init__(self, total):
        self.prompt_tokens, self.completion_tokens, self.total_tokens = total-12, 12, total
    def model_dump(self):
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens}


class _OfflineSDK:
    """Transport interceptor returning test-only structured outputs; never connects."""
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def with_options(self, **_kwargs):
        return self
    def create(self, *, messages, **_kwargs):
        self.requests.append(messages)
        system, user = messages[0]["content"], messages[-1]["content"]
        if "Classify the user's" in system:
            output = {"strategy":"SINGLE","sub_strategy":"BM25","intent_labels":["policy_lookup"],
                "safety_decision":"ALLOW","reason_code":"POLICY_LOOKUP","needs_clarification":False,
                "clarification_question":None,"required_capabilities":["search_knowledge"]}
        elif "只规划政策检索目标" in system:
            payload = json.loads(user)
            question = payload["original_query"]
            anchor = "return window" if "return window" in question else question[:24]
            output = {"request_mode":"POLICY_EXPLANATION","requested_aspects":[
                {"aspect_id":"aspect-1","text":"answer the requested return-window question","query_anchor":anchor}],
                "user_provided_facts":[]}
        else:
            payload = json.loads(user)
            evidence = payload["evidence"]
            output = {"answer":"According to the fictional policy excerpt: " + evidence[0]["content"],
                "citations":[evidence[0]["chunk_id"]]}
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(output)))
        return SimpleNamespace(model="offline-interceptor", choices=[choice], usage=_Usage(80))


class _ApplicationAdmissionSDK:
    """Generic, isolated model transport for actual app-path admission only."""
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def with_options(self, **_kwargs):
        return self
    def create(self, *, messages, **_kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.requests.append({"system": system, "user": user})
        if "Classify the user's" in system:
            import re
            match = re.search(r"available for this request are: (\[[^]]*\])", system)
            allowed = json.loads(match.group(1)) if match else []
            query = user.casefold()
            if len(allowed) > 1 and any(word in query for word in
                    ("order", "ord-", "shipment", "tracking", "pickup", "cancel", "policy", "return", "refund", "warranty", "订单", "物流", "政策", "退货", "退款", "取消")):
                strategy, sub, caps = "AGENT", "AGENT+MULTI" if "search_knowledge" in allowed else "AGENT+SINGLE", allowed
            elif "search_knowledge" in allowed and any(word in query for word in
                    ("policy", "rule", "return", "refund", "warranty", "coupon", "退货", "规则", "政策")):
                strategy, sub, caps = "SINGLE", "BM25", ["search_knowledge"]
            elif "get_order_items" in allowed and any(word in query for word in ("items", "商品明细", "商品清单")):
                strategy, sub, caps = "AGENT", "AGENT+SINGLE", ["get_order_items"]
            elif "get_order" in allowed and any(word in query for word in ("order", "订单", "status")):
                strategy, sub, caps = "AGENT", "AGENT+SINGLE", ["get_order"]
            elif "check_inventory" in allowed and any(word in query for word in ("stock", "库存", "有货")):
                strategy, sub, caps = "AGENT", "AGENT+SINGLE", ["check_inventory"]
            elif "get_product_detail" in allowed and any(word in query for word in ("product", "商品", "w", "match", "mug", "ml", "pebble")):
                strategy, sub, caps = "AGENT", "AGENT+SINGLE", ["get_product_detail"]
            elif "search_products" in allowed and any(word in query for word in ("recommend", "推荐")):
                caps = [x for x in ("search_products", "recommend_products") if x in allowed]
                strategy, sub = "AGENT", "AGENT+SINGLE"
            elif any(word in query for word in ("someone else", "another user", "他人", "别人的", "泄露", "api keys")):
                strategy, sub, caps = "REFUSE", None, []
            else:
                strategy, sub, caps = "DIRECT", None, []
            output = {"strategy": strategy, "sub_strategy": sub, "intent_labels": ["offline_admission"],
                "safety_decision": "REFUSE" if strategy == "REFUSE" else "ALLOW",
                "reason_code": "OFFLINE_ADMISSION_CLASSIFICATION", "needs_clarification": strategy == "DIRECT",
                "clarification_question": "Please clarify the specific information needed." if strategy == "DIRECT" else None,
                "required_capabilities": caps}
        elif "只规划政策检索目标" in system:
            payload = json.loads(user)
            query = payload["original_query"]
            output = {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [{
                "aspect_id": "admission-aspect", "text": "retrieve only the policy needed to answer the user query",
                "query_anchor": query[:min(120, len(query))]}], "user_provided_facts": []}
        else:
            payload = json.loads(user)
            evidence = payload.get("evidence", [])
            if not evidence:
                output = {"answer": "The available records do not establish this policy fact.", "citations": []}
            else:
                row = evidence[0]
                output = {"answer": "The retrieved fictional source states: " + str(row.get("content", "")),
                    "citations": [row["chunk_id"]]}
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(output)))
        return SimpleNamespace(model="offline-admission-interceptor", choices=[choice], usage=_Usage(96))


class _RecordingLedger:
    def __init__(self, limits): self.limits, self.events = limits, []
    def reserve(self, **kwargs): self.events.append(("reserve", kwargs)); return f"r{len(self.events)}"
    def settle(self, reservation_id, **kwargs): self.events.append(("settle", reservation_id, kwargs))


def test_real_langgraph_application_chain_uses_intercepted_model_and_real_policy_repository(fixture_context, monkeypatch):
    from app.m0 import config as config_module
    _root, bundle, target, coordinator = fixture_context
    plan = plan_case(bundle, "D1-POLICY-013")
    receipt = coordinator.prepare(plan)
    sdk = _OfflineSDK()
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_args, **_kwargs: sdk)
    settings = load_settings(require_api_key=False)
    settings = replace(settings, api_key="offline-interceptor", database_url=f"sqlite:///{target.path.as_posix()}",
        orchestrator="langgraph", m3_supervisor_enabled=True)
    limits = BudgetLimits(provider_calls=6, tool_calls=8, total_tokens=120000, estimated_usd=.18,
        max_calls_per_trial=8, max_duration_seconds=180, max_retries=0)
    ledger = _RecordingLedger(limits)
    try:
        result = execute_application_case(coordinator, plan, receipt, ledger, settings)
        assert result["status"] == "SUCCEEDED"
        assert result["run_id"] and len(result["observations"]["traces"]) > 0
        audit_events = [e for e in result["observations"]["run_events"]
            if e["event_type"] in {"policy_retrieval_started", "policy_retrieval_finished"}]
        assert {e["event_type"] for e in audit_events} == {
            "policy_retrieval_started", "policy_retrieval_finished"}
        start = json.loads(next(e["payload_json"] for e in audit_events
            if e["event_type"] == "policy_retrieval_started"))
        finish = json.loads(next(e["payload_json"] for e in audit_events
            if e["event_type"] == "policy_retrieval_finished"))
        assert start["call_id"] == finish["call_id"]
        assert start["result_ref"] == finish["result_ref"]
        assert start["trace_id"] == finish["trace_id"] == result["result"]["trace_id"]
        assert finish["evidence_ids"]
        assert result["read_calls"] == 1
        # This case is deterministically SINGLE-routed; the real app still
        # performs one intercepted answer request after real BM25 retrieval.
        assert len(sdk.requests) == 1
    finally:
        coordinator.cleanup(receipt)
    with coordinator.factory() as db:
        assert db.execute(select(Run.run_id)).all() == []
        assert db.execute(select(Trace.trace_id)).all() == []
        assert db.execute(select(RunEvent.event_id)).all() == []


def test_all_remaining_ready_cases_have_actual_offline_application_admission(fixture_context, monkeypatch):
    """Exercise the actual claimed LangGraph entry for every remaining READY ID.

    Intercepted model calls never leave process; each case gets a fresh fixture
    receipt and is archived before exact cleanup. Repository-only readiness is
    not used as the admission result.
    """
    from app.m0 import config as config_module
    from app.m3 import after_sales_tools
    from eval.m6_1.personal_dev_batch_m62 import SOURCE_MATRIX_REL, plan_without_unauthorized_targets
    from eval.m6_1.personal_dev_runtime import execute_application_case
    from eval.m6_1.personal_dev_batch_m62 import score_archive
    from datetime import datetime, timezone
    import hashlib
    root, bundle, target, coordinator = fixture_context
    builder_capture = {}
    original_after_sales_builder = after_sales_tools.build_after_sales_business_result
    def capture_after_sales_builder(supervisor_result, *, route, result_store):
        rendered = original_after_sales_builder(supervisor_result, route=route, result_store=result_store)
        if set(route.get("required_capabilities") or []) == {"get_refund_status"}:
            handoffs = supervisor_result.get("handoffs") or []
            agent_result = (handoffs[0].get("result") or {}) if len(handoffs) == 1 else {}
            refs = agent_result.get("tool_result_refs") or []
            builder_capture["last"] = {"supervisor_status":supervisor_result.get("supervisor_status"),
                "terminal_status":supervisor_result.get("terminal_status"),"handoff_count":len(handoffs),
                "handoff_status":handoffs[0].get("status") if handoffs else None,
                "source_agent":agent_result.get("source_agent"),"refs":refs,
                "payload_sources":[(result_store.get(ref) or {}).get("source") for ref in refs],
                "rendered":rendered is not None}
        return rendered
    monkeypatch.setattr(after_sales_tools, "build_after_sales_business_result", capture_after_sales_builder)
    matrix = json.loads((root / SOURCE_MATRIX_REL).read_text(encoding="utf-8"))
    cases = {row["case_id"]: row for row in bundle["cases"]}
    ids = [row["case_id"] for row in matrix["cases"]
        if row["admission_status"] == "READY" and row["case_id"] != "D1-POLICY-013"]
    records = []
    for case_id in ids:
        plan = plan_case(bundle, case_id)
        source = matrix["cases"][[row["case_id"] for row in matrix["cases"]].index(case_id)]
        if not source["allowed_read_only_capabilities"]:
            plan = plan_without_unauthorized_targets(plan)
        sdk = _ApplicationAdmissionSDK()
        monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, _sdk=sdk, **_k: _sdk)
        settings = load_settings(require_api_key=False)
        settings = replace(settings, api_key="offline-admission-only", database_url=f"sqlite:///{target.path.as_posix()}",
            orchestrator="langgraph", m3_supervisor_enabled=True)
        limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
            estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
            max_duration_seconds=180, max_retries=0)
        ledger = _RecordingLedger(limits)
        receipt = coordinator.prepare(plan)
        archive_path = root / "data/eval/offline-tests" / f"m62-r5-admission-{case_id.lower()}.json"
        try:
            result = execute_application_case(coordinator, plan, receipt, ledger, settings)
            archive = {"schema_version": "m62-r5-offline-application-admission-r1", "case_id": case_id,
                "result": result, "model_transport": "IN_PROCESS_INTERCEPTOR_NO_NETWORK",
                "provider_attempts": len(sdk.requests), "created_at_utc": datetime.now(timezone.utc).isoformat()}
            archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2,
                default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)) + "\n", encoding="utf-8")
            safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
            if safety.get("safe_to_cleanup"):
                coordinator.cleanup(receipt)
            cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=bool(safety.get("safe_to_cleanup")))
            run_events = result.get("observations", {}).get("run_events", [])
            route_events = [event for event in run_events if event.get("event_type") == "route_decided"]
            route = result.get("result", {}).get("route", {}) if isinstance(result.get("result"), dict) else {}
            observed_caps = route.get("required_capabilities", [])
            allowed_caps = set(source["allowed_read_only_capabilities"])
            scope_ok = set(observed_caps).issubset(allowed_caps)
            traces = result.get("observations", {}).get("traces", [])
            handoffs = [event for event in run_events if event.get("event_type") in {"agent_started", "agent_handoff"}]
            terminal = result.get("status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
            complete_audit = bool(route_events) and bool(traces) and all(
                row.get("run_id") == result.get("run_id") for row in run_events + traces)
            if not terminal or not complete_audit or not scope_ok or cleanup.get("cleanup_status") != "CLEANED_AND_RELEASED":
                status = "BLOCKED_ENGINEERING"
            elif result.get("status") == "FAILED":
                status = "BLOCKED_ENGINEERING"
            elif set(source["allowed_read_only_capabilities"]) and not observed_caps:
                status = "BLOCKED_CONTRACT_OR_DATA"
            else:
                status = "APPLICATION_READY"
            grade = score_archive(cases[case_id], archive, budget_snapshot={"complete":True,"settled":True,
                "active_reservations":0,"unknown_tokens":False,"unknown_cost":False},
                cleanup_status=cleanup.get("cleanup_status", "UNKNOWN"))
            expected_business = allowed_caps - {"search_knowledge"}
            executed_business = set(grade.get("observed_tools", []))
            capability_execution = ("PASS" if expected_business.issubset(executed_business) and
                ("search_knowledge" not in allowed_caps or grade["program_checks"].get("policy_retrieval_audit") == "PASS")
                else "NOT_APPLICABLE" if not allowed_caps and not executed_business and
                    route.get("strategy") in {"DIRECT", "REFUSE"} else "FAIL")
            if not allowed_caps and (executed_business or grade["policy_retrieval_call_ids"]):
                capability_execution = "FAIL"
            if not allowed_caps and route.get("strategy") not in {"DIRECT", "REFUSE"}:
                capability_execution = "FAIL"
            if capability_execution == "FAIL" and status == "APPLICATION_READY":
                status = "BLOCKED_CONTRACT_OR_DATA"
            record = {"case_id":case_id,"application_admission":status,"run_status":result.get("status"),
                "route":route,"scope_check":"PASS" if scope_ok else "FAIL","event_count":len(run_events),
                "trace_count":len(traces),"agent_handoff_count":len(handoffs),"provider_requests_intercepted":len(sdk.requests),
                "expected_capability_execution":capability_execution,"observed_business_tools":sorted(executed_business),
                "after_sales_builder_diagnostic":builder_capture.get("last") if case_id in {"D1-AFTERSALES-026","D1-AFTERSALES-027"} else None,
                "offline_archive_path":archive_path.relative_to(root).as_posix(),
                "offline_archive_sha256":hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "cleanup":cleanup,"program_score":grade}
            records.append(record)
        except Exception as exc:
            with coordinator.factory() as db:
                active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
            if active == receipt.lease_id:
                coordinator.cleanup(receipt)
            records.append({"case_id":case_id,"application_admission":"BLOCKED_ENGINEERING",
                "error_type":type(exc).__name__,"cleanup":"ATTEMPTED_EXACT_RECEIPT"})
    assert len(records) == 30
    assert {row["case_id"] for row in records} == set(ids)
    ready = {row["case_id"] for row in records if row["application_admission"] == "APPLICATION_READY"}
    blocked = {row["case_id"] for row in records if row["application_admission"] != "APPLICATION_READY"}
    # The generic composite route now performs the owner-scoped order reads.
    assert blocked == set()
    assert len(ready) == 30
    for row in records:
        if "search_knowledge" in row.get("route", {}).get("required_capabilities", []):
            assert row["program_score"]["program_checks"]["policy_retrieval_audit"] == "PASS" or \
                row["case_id"] in blocked
        assert row.get("cleanup", {}).get("cleanup_status") == "CLEANED_AND_RELEASED"
        assert row.get("cleanup", {}).get("residual_case_rows") == 0
    output = target.root / "application-admission-offline-r5.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version":"m62-application-admission-r1","source_matrix":SOURCE_MATRIX_REL,
        "case_count":30,"model_calls":"INTERCEPTED_ONLY","provider_requests":sum(
            row.get("provider_requests_intercepted", 0) for row in records),
        "external_calls":0,"application_ready_count":len(ready),"blocked_count":len(blocked),
        "implementation_digest": __import__("eval.m6_1.personal_dev", fromlist=["implementation_digest"]).implementation_digest(root),
        "dataset_sha256": __import__("hashlib").sha256((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_bytes()).hexdigest(),
        "database_identity": target.identity,"records":records}, ensure_ascii=False, indent=2,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value))+"\n", encoding="utf-8")
    assert all(isinstance(row.get("cleanup"), dict) and row["cleanup"].get("cleanup_status") == "CLEANED_AND_RELEASED" for row in records)


def test_r6_exact_22_unconsumed_cases_actual_application_admission(fixture_context, monkeypatch):
    """R6 rechecks each R5-not-run candidate through the current isolated app stack."""
    from app.m0 import config as config_module
    from eval.m6_1.personal_dev_batch_m62 import (SOURCE_MATRIX_REL, plan_without_unauthorized_targets,
        score_archive_r6)
    from eval.m6_1.personal_dev_runtime import execute_application_case
    from datetime import datetime, timezone
    import hashlib
    root, bundle, target, coordinator = fixture_context
    ids = ["D1-ORDER-007", "D1-ORDER-009", "D1-ORDER-011", "D1-ORDER-012",
        "D1-POLICY-014", "D1-POLICY-017", "D1-POLICY-018", "D1-AFTERSALES-021",
        "D1-AFTERSALES-023", "D1-AFTERSALES-025", "D1-AFTERSALES-026", "D1-AFTERSALES-027",
        "D1-CROSSAGENT-028", "D1-CROSSAGENT-030", "D1-CROSSAGENT-031", "D1-SAFETY-034",
        "D1-SAFETY-035", "D1-SAFETY-036", "D1-SAFETY-037", "D1-SAFETY-038",
        "D1-SAFETY-039", "D1-SAFETY-040"]
    matrix = json.loads((root / SOURCE_MATRIX_REL).read_text(encoding="utf-8"))
    source_rows = {row["case_id"]: row for row in matrix["cases"]}
    cases = {row["case_id"]: row for row in bundle["cases"]}
    records = []
    for case_id in ids:
        source = source_rows[case_id]
        case = cases[case_id]
        plan = plan_case(bundle, case_id)
        if not source["allowed_read_only_capabilities"]:
            plan = plan_without_unauthorized_targets(plan)
        sdk = _ApplicationAdmissionSDK()
        monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, _sdk=sdk, **_k: _sdk)
        settings = load_settings(require_api_key=False)
        settings = replace(settings, api_key="offline-admission-only", database_url=f"sqlite:///{target.path.as_posix()}",
            orchestrator="langgraph", m3_supervisor_enabled=True)
        limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
            estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
            max_duration_seconds=180, max_retries=0)
        ledger = _RecordingLedger(limits)
        receipt = coordinator.prepare(plan)
        archive_path = root / "data/eval/offline-tests" / f"m62-r6-admission-{uuid.uuid4().hex}-{case_id.lower()}.json"
        try:
            result = execute_application_case(coordinator, plan, receipt, ledger, settings)
            archive = {"schema_version": "m62-r6-offline-application-admission-r1", "case_id": case_id,
                "result": result, "model_transport": "IN_PROCESS_INTERCEPTOR_NO_NETWORK",
                "intercepted_requests": len(sdk.requests), "created_at_utc": datetime.now(timezone.utc).isoformat()}
            archive_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2,
                default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value)) + "\n", encoding="utf-8")
            safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
            if safety.get("safe_to_cleanup") is True:
                coordinator.cleanup(receipt)
            cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=bool(safety.get("safe_to_cleanup")))
            route_events = [event for event in result.get("observations", {}).get("run_events", [])
                if event.get("event_type") == "route_decided"]
            route = result.get("result", {}).get("route", {}) if isinstance(result.get("result"), dict) else {}
            allow = set(source["allowed_read_only_capabilities"])
            expected = {row["tool_id"] for row in case.get("input", {}).get("expected_tool_calls", [])
                if row.get("mode") == "read_only"}
            observed = set(result.get("result", {}).get("tool_call_audit", []) and
                [row.get("tool") for row in result["result"]["tool_call_audit"] if isinstance(row, dict)])
            events = result.get("observations", {}).get("run_events", [])
            traces = result.get("observations", {}).get("traces", [])
            audit_complete = bool(route_events and traces) and all(row.get("run_id") == result.get("run_id")
                for row in events + traces)
            scope_ok = set(route.get("required_capabilities", [])).issubset(allow)
            grade = score_archive_r6(case, archive, budget_snapshot={"complete": True, "settled": True,
                "active_reservations": 0, "unknown_tokens": False, "unknown_cost": False},
                cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
            expected_business = expected - {"search_knowledge"}
            policy_ok = ("search_knowledge" not in expected or
                grade["program_checks"].get("policy_retrieval_audit") == "PASS")
            task_capability_ok = (expected_business.issubset(observed) and policy_ok if expected else
                route.get("strategy") in {"DIRECT", "REFUSE"} and not observed and
                not grade.get("policy_retrieval_call_ids"))
            terminal = result.get("status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
            technical_names = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
                "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup")
            technical_ready = all(grade["program_checks"].get(name) == "PASS" for name in technical_names)
            status = ("APPLICATION_READY" if terminal and audit_complete and scope_ok and task_capability_ok and
                technical_ready and cleanup.get("cleanup_status") == "CLEANED_AND_RELEASED" and cleanup.get("residual_case_rows") == 0
                else "BLOCKED_ENGINEERING")
            records.append({"case_id": case_id, "application_admission": status, "run_status": result.get("status"),
                "route": route, "allowed_capabilities": sorted(allow), "expected_capabilities": sorted(expected),
                "observed_tools": sorted(observed), "scope_check": "PASS" if scope_ok else "FAIL",
                "event_count": len(events), "trace_count": len(traces),
                "intercepted_requests": len(sdk.requests), "run_id": result.get("run_id"),
                "archive_path": archive_path.relative_to(root).as_posix(),
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "program_score": grade, "cleanup": cleanup})
        except Exception as exc:
            with coordinator.factory() as db:
                active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
            if active == receipt.lease_id:
                coordinator.cleanup(receipt)
            records.append({"case_id": case_id, "application_admission": "BLOCKED_ENGINEERING",
                "error_type": type(exc).__name__, "cleanup": "EXACT_RECEIPT_CLEANUP_ATTEMPTED"})
    assert [row["case_id"] for row in records] == ids
    output = target.root / "application-admission-offline-r6.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": "m62-r6-application-admission-r1", "case_count": len(ids),
        "case_ids": ids, "model_transport": "IN_PROCESS_INTERCEPTOR_NO_NETWORK", "external_calls": 0,
        "isolated_application_runs": len(records), "formal_run_created": False,
        "implementation_digest": __import__("eval.m6_1.personal_dev", fromlist=["implementation_digest"]).implementation_digest(root),
        "dataset_sha256": hashlib.sha256((root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_bytes()).hexdigest(),
        "database_identity": target.identity, "ready_count": sum(r.get("application_admission") == "APPLICATION_READY" for r in records),
        "blocked_count": sum(r.get("application_admission") != "APPLICATION_READY" for r in records), "records": records}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    assert len(records) == 22
    with coordinator.factory() as db:
        assert db.execute(select(Run.run_id)).all() == []
        assert db.execute(select(Trace.trace_id)).all() == []
        assert db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one() == (None, None)


def test_r6_warranty_composite_result_ref_requires_same_run_typed_evidence():
    from eval.m6_1.personal_dev_batch_m62 import _warranty_composite_result_is_linked
    run_id, trace_id = "run-1", "trace-1"
    calls = [
        ("call-order", "get_order", "order-ref", "business_database"),
        ("call-items", "get_order_items", "items-ref", "business_database"),
        ("call-warranty", "get_warranty", "policy-ref", "shoprelay_policy_retrieval"),
    ]
    audits = [{"tool_call_id": call, "tool": tool, "result_ref": ref, "run_id": run_id,
        "trace_id": trace_id, "executed": True, "status": "COMPLETED",
        "knowledge_version": "kb-v1", "index_fingerprint": "idx-v1"}
        for call, tool, ref, _source in calls]
    events = [{"event_type": "tool_finished", "payload_json": json.dumps({
        "tool_call_id": call, "result_ref": ref, "run_id": run_id, "trace_id": trace_id})}
        for call, _tool, ref, _source in calls]
    payload = {
        "trace_id": trace_id, "tool_call_audit": audits,
        "tool_references": [
            {"tool": "get_order", "source": "business_database", "result_ref": "order-ref"},
            {"tool": "get_order_items", "source": "business_database", "result_ref": "items-ref"},
            {"tool": "get_warranty", "source": "shoprelay_warranty_policy", "result_ref": "warranty-ref"},
            {"tool": "search_knowledge", "source": "shoprelay_policy_retrieval", "result_ref": "policy-ref"}],
        "business_results": {"order_logistics": {"get_order_items": {"items": [
            {"product_id": "P-SYN-1"}]}}, "policy": {"product_id": "P-SYN-1",
            "knowledge_version": "kb-v1", "index_fingerprint": "idx-v1", "evidence_ids": ["chunk-1"]}},
        "citations": [{"chunk_id": "chunk-1"}],
        "tool_execution_status": [{"capability": "get_warranty", "executed": True,
            "status": "COMPLETED", "evidence_ids": ["chunk-1"]}],
    }
    archive = {"result": {"run_id": run_id, "result": payload,
        "observations": {"run_events": events}}}
    assert _warranty_composite_result_is_linked(archive) is True

    wrong_run = json.loads(json.dumps(archive))
    wrong_run["result"]["result"]["tool_call_audit"][2]["run_id"] = "other-run"
    assert _warranty_composite_result_is_linked(wrong_run) is False

    missing_search_ref = json.loads(json.dumps(archive))
    missing_search_ref["result"]["result"]["tool_references"] = [r for r in
        missing_search_ref["result"]["result"]["tool_references"] if r["tool"] != "search_knowledge"]
    assert _warranty_composite_result_is_linked(missing_search_ref) is False
