"""Offline regression coverage for the versioned three-case diagnostic checker."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from eval.m6_1.personal_dev_diagnostic_checks import (
    DiagnosticCheckError,
    evaluate_diagnostic_record,
    fail_closed_diagnostic_report,
    load_locked_rules,
    require_diagnostic_predecessor,
    validate_diagnostic_check_result,
)
from eval.m6_1.personal_dev_authorization import personal_diagnostic_profile


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "docs/eval/m6.1-d1-diagnostic-closure-r2/locked-check-rules-r2.json"


@pytest.fixture
def rules():
    loaded, _sha = load_locked_rules(RULES_PATH, case_id="D1-POLICY-013")
    return loaded


def budget(*, provider=2, tools=0):
    return {"budget_snapshot_schema_version": "m6.1-budget-snapshot-v2",
            "snapshot_kind": "FINAL", "complete": True, "settled": True,
            "active_reservations": 0, "integrity_errors": [],
            "provider_requests": provider, "calls_by_category": {
                "provider": provider, "tool": tools, "embedding": 0, "judge": 0},
            "tokens": 1157, "tokens_conservative_budget_consumption": 1157,
            "estimated_usd": 0.0014, "estimated_usd_conservative_budget_consumption": 0.0014,
            "embedding_requests": 0, "judge_requests": 0}


def cleanup(*, status="CLEANED_AND_RELEASED", released=True, pending=0, residual=0):
    return {"cleanup_status": status, "lease_released": released, "pending_actions": pending,
            "residual_case_rows": residual, "lease_id": "lease-synthetic", "database_identity": "db-synthetic"}


def policy_archive(*, audit=True, kb="SYN-KB-D1-R3.2.1", doc="SYN-POL-RET-01",
                   event_run="run-1", citations=True, events=True, run_status="SUCCEEDED"):
    run_id, trace_id = "run-1", "trace-1"
    call_id, result_ref, fingerprint = "call-policy-1", "policy-retrieval:call-policy-1", "a" * 64
    evidence = {"chunk_id": "syn-policy-return-window", "source_type": "POLICY_DOCUMENT",
        "version": doc, "policy_document_version": doc, "effective_from": "2026-01-01T00:00:00Z",
        "effective_to": "2027-01-01T00:00:00Z", "knowledge_version": kb,
        "index_fingerprint": "fp-r2", "evaluation_at": "2026-10-25T12:00:00Z"}
    citation = {**evidence, "content": "The synthetic fixture provides a 21 day window.",
        "result_refs": [result_ref] if citations and audit else [],
        "retrieval_call_ids": [call_id] if citations and audit else []}
    audit_row = {"audit_schema_version": "1.0", "source_type": "POLICY_RETRIEVAL",
        "capability": "search_knowledge", "call_id": call_id, "run_id": event_run,
        "trace_id": trace_id, "parameter_fingerprint": fingerprint, "result_ref": result_ref,
        "status": "SUCCEEDED", "executed": True, "evidence_ids": ["syn-policy-return-window"],
        "knowledge_version": kb, "policy_document_version": doc, "document_versions": [doc],
        "index_fingerprint": "fp-r2", "evaluation_at": "2026-10-25T12:00:00Z"}
    event_rows = [
        {"event_type": "route_decided", "run_id": run_id, "trace_id": trace_id,
         "payload_json": json.dumps({"strategy": "SINGLE"})},
        {"event_type": "agent_started", "run_id": run_id, "trace_id": trace_id,
         "payload_json": json.dumps({"agent": "policy"})},
    ]
    if events:
        event_rows += [
            {"event_type": "policy_retrieval_started", "run_id": run_id, "trace_id": trace_id,
             "payload_json": json.dumps({**audit_row, "status": "STARTED"})},
            {"event_type": "policy_retrieval_finished", "run_id": run_id, "trace_id": trace_id,
             "payload_json": json.dumps(audit_row)},
        ]
    app_result = {"strategy": "SINGLE", "route": {"strategy": "SINGLE"},
        "trace_id": trace_id, "answer": "This excerpt belongs only to the fictional fixture.",
        "citations": [citation] if citations else [], "evidence": [evidence] if citations else [],
        "knowledge_version": kb, "retrieval": {"knowledge_version": kb, "index_fingerprint": "fp-r2",
        "evaluation_at": "2026-10-25T12:00:00Z"},
        "policy_retrieval": {"knowledge_version": kb, "index_fingerprint": "fp-r2",
        "evaluation_at": "2026-10-25T12:00:00Z"},
        "policy_retrieval_audit": [audit_row] if audit else []}
    return {"case_id": "D1-POLICY-013", "result": {"status": run_status, "run_id": run_id,
        "result": app_result, "observations": {"runs": [{"run_id": run_id, "status": run_status,
        "user_id": "d1-personal-a"}], "run_events": event_rows}}}


def plan(case_id="D1-POLICY-013", owner="d1-personal-a"):
    return {"case_id": case_id, "owner_id": owner}


def evaluate(record, locked, case_id="D1-POLICY-013", *, ledger=None, clean=None):
    return evaluate_diagnostic_record(case_id=case_id, archived=record, plan=plan(case_id),
        locked_rules=locked, budget_snapshot=ledger or budget(), cleanup_evidence=clean or cleanup())


def test_policy_source_versions_and_call_reference_mapping_pass_as_separate_contracts(rules):
    result = evaluate(policy_archive(), rules)
    assert result["factual_checks"] == {
        "fictional_policy_evidence_present": "PASS",
        "knowledge_version_matches_fixture": "PASS",
        "policy_document_version_matches_fixture": "PASS",
        "index_fingerprint_consistent": "PASS",
        "evaluation_time_within_policy_effective_window": "PASS",
    }
    assert result["evidence_checks"]["policy_retrieval_call_result_citation_linkage"] == "PASS"
    assert result["decision"]["outcome"] == "CONTINUE"
    assert result["task_outcome"] == "UNKNOWN"
    assert result["grading_status"] == "NOT_GRADED"


def test_real_policy_013_archive_replay_corrects_fact_misclassification_but_blocks_missing_audit(rules):
    from scripts.replay_m6_1_d1_diagnostic_r2 import recompute

    report = recompute(write=False)
    checks = report["checks"]
    assert report["archive_sha256"] == "22bbb91ff88f15ce7a9342b46f16750addffef608854f7af9263374ab30d1389"
    assert checks["factual_checks"]["fictional_policy_evidence_present"] == "PASS"
    assert checks["factual_checks"]["knowledge_version_matches_fixture"] == "PASS"
    assert checks["factual_checks"]["policy_document_version_matches_fixture"] == "PASS"
    assert checks["evidence_checks"]["policy_retrieval_call_result_citation_linkage"] == "UNKNOWN"
    assert checks["decision"]["outcome"] == "STOP"
    assert checks["task_outcome"] == "UNKNOWN"
    assert report["historical_authorization_status"] == "CLOSED_COMPLETED"


def test_r3_connection_fallback_is_reported_separately_from_model_direct(rules):
    from eval.m6_1.personal_dev_diagnostic_checks import _route_provider_diagnostic

    archive_path = ROOT / "docs/eval/personal-dev-runs/73731cf9746748f8afa81b06950088da.json"
    report_path = ROOT / "docs/eval/d1-diagnostic-r3-20261027/reports/D1-POLICY-013-diagnostic-check-r3.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    prior_report = json.loads(report_path.read_text(encoding="utf-8"))
    closeout = json.loads((ROOT / "docs/eval/d1-diagnostic-r3-20261027/closeout-r3.json").read_text(encoding="utf-8"))
    r3_rules, _sha = load_locked_rules(
        ROOT / "docs/eval/d1-diagnostic-r3-20261027/locked-check-rules-r3.json",
        case_id="D1-POLICY-013")
    checks = evaluate_diagnostic_record(case_id="D1-POLICY-013", archived=archive,
        plan=plan(), locked_rules=r3_rules, budget_snapshot=prior_report["budget_snapshot"],
        cleanup_evidence=prior_report["cleanup_evidence"])

    provider_failure = checks["route_provider_diagnostic"]
    assert closeout["cases"][0]["run_id"] == checks["run_id"]
    assert provider_failure["classification"] == "PROVIDER_CONNECTION_FAILURE_RECORDED"
    assert provider_failure["failure_phase"] == "CONNECTION_NO_RESPONSE"
    assert provider_failure["provider_response_received"] is False
    assert provider_failure["request_delivery"] == "UNKNOWN"
    assert provider_failure["usage_status"] == "UNKNOWN"
    assert checks["decision"]["outcome"] == "STOP"
    assert checks["route_strategy"] == "DIRECT"

    model_direct = _route_provider_diagnostic({"route": {"decision_source": "LLM",
        "strategy": "DIRECT", "usage": None}}, [])
    assert model_direct["classification"] == "MODEL_RESPONSE"
    assert model_direct["model_strategy"] == "DIRECT"


@pytest.mark.parametrize("record,expected", [
    (policy_archive(citations=False), "UNKNOWN"),
    (policy_archive(doc="wrong-document-v9"), "FAIL"),
])
def test_policy_missing_or_conflicting_reference_is_not_pass(rules, record, expected):
    result = evaluate(record, rules)
    key = "fictional_policy_evidence_present" if expected == "UNKNOWN" else "policy_document_version_matches_fixture"
    assert result["factual_checks"][key] == expected
    assert result["decision"]["outcome"] == "STOP"


def test_cross_run_policy_audit_reference_fails_closed(rules):
    record = policy_archive(event_run="other-run")
    result = evaluate(record, rules)
    assert result["evidence_checks"]["policy_retrieval_call_result_citation_linkage"] == "FAIL"
    assert result["decision"]["outcome"] == "STOP"


def test_answer_and_budget_count_without_policy_call_event_is_unknown_and_blocking(rules):
    result = evaluate(policy_archive(audit=False, events=False), rules, ledger=budget(tools=0))
    assert result["factual_checks"]["fictional_policy_evidence_present"] == "PASS"
    assert result["evidence_checks"]["policy_retrieval_audit_present"] == "UNKNOWN"
    assert result["execution_checks"]["budget_limits_respected"] == "PASS"
    assert result["decision"]["outcome"] == "STOP"
    assert any(x.startswith("policy_retrieval_audit_present:UNKNOWN") for x in result["stop_reasons"])


def test_critical_unknown_blocks_but_semantic_unknown_is_a_review_item(rules):
    result = evaluate(policy_archive(), rules)
    # A fully supported technical record can continue with an explicit semantic review queue.
    assert result["decision"]["outcome"] == "CONTINUE"
    assert result["semantic_review_required"][0]["status"] == "UNKNOWN"
    blocked = evaluate(policy_archive(events=False), rules)
    assert blocked["decision"]["outcome"] == "STOP"
    assert blocked["decision"]["blocking_items"]


def test_failed_terminal_run_can_be_cleaned_but_unknown_run_retains_cleanup_gate(rules):
    failed = evaluate(policy_archive(run_status="FAILED", audit=False, events=False), rules)
    assert failed["execution_checks"]["run_terminal"] == "PASS"
    assert failed["evidence_checks"]["policy_retrieval_audit_present"] == "UNKNOWN"
    assert failed["cleanup_checks"]["receipt_exactly_cleaned_and_lease_released"] == "PASS"
    assert failed["decision"]["outcome"] == "STOP"
    unknown = evaluate(policy_archive(run_status="UNKNOWN"), rules,
        clean=cleanup(status="RETAINED_UNSAFE_STATE", released=False))
    assert unknown["cleanup_checks"]["receipt_exactly_cleaned_and_lease_released"] == "FAIL"
    assert unknown["decision"]["outcome"] == "STOP"


def _write_round_predecessor(tmp_path, *, status="CLOSED_COMPLETED", outcome="CONTINUE",
                             cross_round=False):
    prior = "D1-POLICY-013"
    current = "D1-ORDER-007"
    profile = personal_diagnostic_profile(current)
    prior_profile = personal_diagnostic_profile(prior)
    report_dir = tmp_path / profile.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    rules_path = tmp_path / profile.check_rules_path
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RULES_PATH, rules_path)
    locked, rules_sha = load_locked_rules(rules_path, case_id=prior)
    record = policy_archive(events=(outcome != "STOP"))
    archive_rel = "docs/eval/personal-dev-runs/r3-predecessor-archive.json"
    archive_path = tmp_path / archive_rel
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_bytes = json.dumps(record, ensure_ascii=False).encode("utf-8")
    archive_path.write_bytes(archive_bytes)
    checks = evaluate(record, locked)
    if outcome == "REVIEW_REQUIRED":
        locked["decision_policy"]["semantic_unknown_policy"] = "REQUIRE_REVIEW_BEFORE_CONTINUE"
        rules_path.write_text(json.dumps(locked, ensure_ascii=False, indent=2), encoding="utf-8")
        locked, rules_sha = load_locked_rules(rules_path, case_id=prior)
        checks = evaluate(record, locked)
    else:
        checks = evaluate(record, locked)
    report_rel = (Path(profile.report_dir) /
        f"{prior}-diagnostic-check-{profile.report_suffix}.json").as_posix()
    report_path = tmp_path / report_rel
    report = {"schema_version": "2.0", "phase": "EXECUTION_CLOSEOUT",
        "classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
        "grading_status": "NOT_GRADED", "case_id": prior,
        "authorization_id": "auth-policy-r3-test",
        "diagnostic_round_id": "different-round" if cross_round else profile.round_id,
        "code_sha256": "code-test", "database_identity": "db-test", "asset_sha256": "asset-test",
        "rules_sha256": rules_sha, "archive_path": archive_rel,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(), "checks": checks,
        "cleanup_evidence": {**cleanup(), "database_identity": "db-test"}}
    report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    report_path.write_bytes(report_bytes)
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    independent_rel = (Path(prior_profile.report_dir) /
        f"{prior}-independent-check-{prior_profile.report_suffix}.json").as_posix()
    independent_path = tmp_path / independent_rel
    independent_path.parent.mkdir(parents=True, exist_ok=True)
    independent_path.write_text(json.dumps({"schema_version": "1.0", "case_id": prior,
        "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
        "code_sha256": "code-test", "database_identity": "db-test",
        "diagnostic_round_id": prior_profile.round_id, "status": "RECOMPUTED_CONSISTENT",
        "execution_report_path": report_rel, "execution_report_sha256": report_sha,
        "raw_archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "rules_sha256": rules_sha, "matches_execution_report": True,
        "recomputed_checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
    execution_outcome = {"case_id": prior, "run_status": checks.get("run_status"),
        "result_archive": archive_rel,
        "result_archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "diagnostic_decision": checks.get("decision", {}).get("outcome"), "diagnostic_check_report": report_rel,
        "diagnostic_check_report_sha256": report_sha, "run_id": checks.get("run_id"),
        "cleanup_status": "CLEANED_AND_RELEASED"}
    prior_auth = {"authorization_id": report["authorization_id"],
        "diagnostic_round_id": profile.round_id, "status": status,
        "code_sha256": "code-test", "database_identity": "db-test", "asset_sha256": "asset-test",
        "execution_outcome": execution_outcome}
    prior_auth_path = tmp_path / prior_profile.authorization_path
    prior_auth_path.parent.mkdir(parents=True, exist_ok=True)
    prior_auth_path.write_text(json.dumps(prior_auth), encoding="utf-8")
    prior_state = {"authorization_id": prior_auth["authorization_id"],
        "diagnostic_round_id": profile.round_id, "status": status,
        "execution_outcome": execution_outcome}
    state_path = tmp_path / prior_profile.execution_state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(prior_state), encoding="utf-8")
    current_auth = {"predecessor_report_path": report_rel,
        "predecessor_report_sha256": report_sha, "code_sha256": "code-test",
        "database_identity": "db-test", "asset_sha256": "asset-test"}
    return profile, current_auth, report_path


def test_missing_report_or_checker_exception_fails_closed(rules, tmp_path):
    profile, auth, _report = _write_round_predecessor(tmp_path)
    report_path = tmp_path / auth["predecessor_report_path"]
    report_path.unlink()
    with pytest.raises(DiagnosticCheckError, match="DIAGNOSTIC_PREDECESSOR_REPORT_UNAVAILABLE"):
        require_diagnostic_predecessor(tmp_path, case_id="D1-ORDER-007",
            profile=profile, authorization=auth)
    failed = fail_closed_diagnostic_report(case_id="D1-POLICY-013",
        reason="CHECKER_EXCEPTION", error_type="RuntimeError")
    assert failed["decision"]["outcome"] == "STOP"
    assert failed["check_items"][0]["status"] == "UNKNOWN"


def test_first_case_stop_prevents_successor_execution_entry(tmp_path):
    profile, auth, _report = _write_round_predecessor(tmp_path,
        status="CLOSED_STOPPED", outcome="STOP")
    called = []

    def run_order_case():
        called.append("run")

    with pytest.raises(DiagnosticCheckError, match="DID_NOT_CONTINUE"):
        require_diagnostic_predecessor(tmp_path, case_id="D1-ORDER-007",
            profile=profile, authorization=auth)
        run_order_case()
    assert called == []


def test_review_required_predecessor_blocks_successor_until_review_is_resolved(tmp_path):
    profile, auth, _report = _write_round_predecessor(tmp_path,
        status="CLOSED_COMPLETED", outcome="REVIEW_REQUIRED")
    with pytest.raises(DiagnosticCheckError, match="DIAGNOSTIC_PREDECESSOR_DID_NOT_CONTINUE"):
        require_diagnostic_predecessor(tmp_path, case_id="D1-ORDER-007",
            profile=profile, authorization=auth)


def test_predecessor_requires_this_round_report_hash_and_closed_execution(tmp_path):
    profile, auth, report = _write_round_predecessor(tmp_path)
    require_diagnostic_predecessor(tmp_path, case_id="D1-ORDER-007",
        profile=profile, authorization=auth)
    auth["predecessor_report_sha256"] = "0" * 64
    with pytest.raises(DiagnosticCheckError, match="REPORT_HASH_MISMATCH"):
        require_diagnostic_predecessor(tmp_path, case_id="D1-ORDER-007",
            profile=profile, authorization=auth)
    profile, auth, _report = _write_round_predecessor(tmp_path / "cross", cross_round=True)
    with pytest.raises(DiagnosticCheckError, match="DID_NOT_CONTINUE"):
        require_diagnostic_predecessor(tmp_path / "cross", case_id="D1-ORDER-007",
            profile=profile, authorization=auth)


def test_incomplete_or_inconsistent_report_decision_fails_closed(rules):
    result = evaluate(policy_archive(events=False), rules)
    result["decision"]["outcome"] = "CONTINUE"
    with pytest.raises(DiagnosticCheckError, match="DIAGNOSTIC_DECISION_BLOCKER_REASON_MISMATCH"):
        validate_diagnostic_check_result(result, decision_policy=rules["decision_policy"])
    with pytest.raises(DiagnosticCheckError, match="DIAGNOSTIC_CHECK_REPORT_FIELDS_INCOMPLETE"):
        validate_diagnostic_check_result({"decision": {}}, decision_policy=rules["decision_policy"])


def test_order007_uses_agent_get_order_and_owner_scoped_result(rules):
    order_rules = rules["cases"]["D1-ORDER-007"]
    run_id, trace_id, call_id, result_ref = "run-order", "trace-order", "tool-call-1", "order-result-1"
    event_payload = {"tool_call_id": call_id, "tool": "get_order", "capability": "get_order",
        "result_ref": result_ref, "status": "COMPLETED", "executed": True,
        "run_id": run_id, "trace_id": trace_id}
    app = {"strategy": "AGENT", "route": {"strategy": "AGENT"}, "trace_id": trace_id,
        "business_results": {"get_order": {"order_id": "ORD-SYN-101", "status": "PAID",
            "fixture_provenance": "syn-order-101"}},
        "tool_references": [{"tool": "get_order", "result_ref": result_ref}]}
    record = {"result": {"status": "SUCCEEDED", "run_id": run_id, "result": app,
        "observations": {"runs": [{"run_id": run_id, "status": "SUCCEEDED", "user_id": "d1-personal-a"}],
            "run_events": [{"event_type": "route_decided", "run_id": run_id, "trace_id": trace_id,
                "payload_json": json.dumps({"strategy": "AGENT"})},
                {"event_type": "agent_started", "run_id": run_id, "trace_id": trace_id,
                "payload_json": "{}"},
                {"event_type": "tool_started", "run_id": run_id, "trace_id": trace_id,
                "payload_json": json.dumps({**event_payload, "status": "STARTED"})},
                {"event_type": "tool_finished", "run_id": run_id, "trace_id": trace_id,
                "payload_json": json.dumps(event_payload)}]}}}
    result = evaluate_diagnostic_record(case_id="D1-ORDER-007", archived=record,
        plan=plan("D1-ORDER-007"), locked_rules=rules, budget_snapshot=budget(tools=1), cleanup_evidence=cleanup())
    assert result["route_strategy"] == "AGENT"
    assert result["factual_checks"]["run_owner_matches_fixture_owner"] == "PASS"
    assert result["factual_checks"]["stored_order_id_and_status_match_snapshot"] == "PASS"
    assert result["evidence_checks"]["order_result_ref_matches_tool_event"] == "PASS"
    assert result["evidence_checks"]["unknown_amount_not_rendered_as_value"] == "UNKNOWN"
    assert result["execution_checks"]["budget_limits_respected"] == "PASS"


def test_zero_tool_snapshot_is_explicit_and_matches_zero_tool_events(rules):
    result = evaluate(policy_archive(), rules, ledger=budget(provider=0, tools=0))
    assert result["execution_checks"]["budget_limits_respected"] == "PASS"


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("budget_snapshot_schema_version"),
    lambda value: value.update(complete=False),
    lambda value: value.update(settled=False, active_reservations=1),
    lambda value: value["calls_by_category"].pop("tool"),
    lambda value: value["calls_by_category"].update(tool=-1),
    lambda value: value["calls_by_category"].update(tool=True),
])
def test_budget_missing_incomplete_or_illegal_counts_stay_unknown_and_block(rules, mutation):
    snapshot = budget(provider=0, tools=0)
    mutation(snapshot)
    result = evaluate(policy_archive(), rules, ledger=snapshot)
    assert result["execution_checks"]["budget_limits_respected"] == "UNKNOWN"
    assert result["decision"]["outcome"] == "STOP"


def test_ledger_and_tool_event_conflict_fails_budget_check(rules):
    order_rules = rules["cases"]["D1-ORDER-007"]
    result = evaluate_diagnostic_record(case_id="D1-ORDER-007", archived=policy_archive(),
        plan=plan("D1-ORDER-007"), locked_rules={**rules, "cases": {"D1-ORDER-007": order_rules}},
        budget_snapshot=budget(provider=0, tools=1), cleanup_evidence=cleanup())
    assert result["execution_checks"]["budget_limits_respected"] == "FAIL"


def test_order009_refusal_does_not_need_agent_or_order_query(rules):
    run_id, trace_id = "run-refuse", "trace-refuse"
    record = {"result": {"status": "SUCCEEDED", "run_id": run_id,
        "result": {"strategy": "REFUSE", "route": {"strategy": "REFUSE"}, "trace_id": trace_id,
            "tool_references": [], "business_results": {}},
        "observations": {"runs": [{"run_id": run_id, "status": "SUCCEEDED", "user_id": "d1-personal-b"}],
            "run_events": [{"event_type": "route_decided", "run_id": run_id, "trace_id": trace_id,
                "payload_json": json.dumps({"strategy": "REFUSE"})}]}}}
    result = evaluate_diagnostic_record(case_id="D1-ORDER-009", archived=record,
        plan=plan("D1-ORDER-009", "d1-personal-b"), locked_rules=rules,
        budget_snapshot=budget(tools=0), cleanup_evidence=cleanup())
    assert result["execution_checks"]["route_matches_case_rule"] == "PASS"
    assert result["execution_checks"]["agent_started_event_matches_case_rule"] == "PASS"
    assert result["safety_checks"]["no_target_order_lookup_or_result"] == "PASS"
    assert result["semantic_review_required"][0]["status"] == "UNKNOWN"


def test_rules_scope_is_exact(tmp_path):
    raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    raw["cases"]["D1-ORDER-008"] = copy.deepcopy(raw["cases"]["D1-ORDER-007"])
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DiagnosticCheckError, match="LOCKED_CHECK_RULES_SCOPE_INVALID"):
        load_locked_rules(path, case_id="D1-POLICY-013")
