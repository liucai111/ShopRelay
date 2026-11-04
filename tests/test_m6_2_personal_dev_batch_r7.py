"""R7 continuation: frozen R6 remainder verification and actual-app admission.

Every SDK object in this file is an in-process interceptor. No socket/provider
request is made by these tests.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from app.m0.config import load_settings
from app.m0 import config as config_module
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.personal_dev import D1Coordinator, plan_case
from eval.m6_1.personal_dev_batch_m62 import (
    DurableAttemptJournal, TaskQuotaLedger, derive_limits, plan_without_unauthorized_targets, score_archive_r7,
    score_archive_r8, batch_stop_reason_r8, batch_stop_reason_r9,
)
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import execute_application_case

ROOT = Path(__file__).resolve().parents[1]
R6 = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
R7 = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r7-20261029"
DATASET_REL = "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
DATASET_SHA = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
MATRIX_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"


@pytest.fixture
def tmp_path():
    path = ROOT / "data/eval/offline-tests" / f"m62-r7-test-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def isolated_context(tmp_path):
    target = PersonalDevDatabase(tmp_path)
    engine, factory = target.open(initialize=True)
    try:
        yield target, D1Coordinator(target, factory)
    finally:
        engine.dispose()


class _Usage:
    def model_dump(self):
        return {"prompt_tokens": 70, "completion_tokens": 20, "total_tokens": 90}


class _InterceptedSDK:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **_kwargs):
        return self

    def create(self, *, messages, **_kwargs):
        system, user = messages[0]["content"], messages[-1]["content"]
        self.requests.append({"system": system, "user": user})
        query = user.casefold()
        if "Classify the user's" in system:
            available = []
            marker = "available for this request are: "
            if marker in system:
                raw = system.split(marker, 1)[1].split(". ", 1)[0]
                try:
                    available = json.loads(raw)
                except json.JSONDecodeError:
                    available = []
            if not available:
                route, sub, caps = "REFUSE", None, []
            elif len(available) > 1:
                route, sub, caps = "AGENT", "AGENT+SINGLE", available
            elif available == ["search_knowledge"]:
                route, sub, caps = "SINGLE", "BM25", available
            else:
                route, sub, caps = "AGENT", "AGENT+SINGLE", available
            output = {"strategy": route, "sub_strategy": sub, "intent_labels": ["offline_admission"],
                "safety_decision": "REFUSE" if route == "REFUSE" else "ALLOW",
                "reason_code": "INTERCEPTED_ADMISSION", "needs_clarification": False,
                "clarification_question": None, "required_capabilities": caps}
        elif "ֻ�滮���߼���Ŀ��" in system:
            payload = json.loads(user)
            query_text = payload["original_query"]
            output = {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [{
                "aspect_id": "offline-aspect", "text": "retrieve only requested policy facts",
                "query_anchor": query_text[:120]}], "user_provided_facts": []}
        else:
            payload = json.loads(user)
            evidence = payload.get("evidence", [])
            if evidence:
                output = {"answer": "The retrieved synthetic evidence is inconclusive where facts are absent.",
                    "citations": [row["chunk_id"] for row in evidence]}
            else:
                output = {"answer": "The authorized records do not establish that fact.", "citations": []}
        response = SimpleNamespace(model="r7-offline-interceptor", choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=json.dumps(output)))], usage=_Usage())
        return response


def _r6_unconsumed():
    manifest = json.loads((R6 / "batch-manifest-r6.json").read_text(encoding="utf-8"))
    state = json.loads((R6 / "execution-state-r6.json").read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in (R6 / "execution-journal-r6.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    closeout = json.loads((R6 / "reports/closeout-r6-r3.json").read_text(encoding="utf-8"))
    consumed = {case_id for case_id, attempts in state["attempt_count"].items() if attempts != 0}
    remaining = [case_id for case_id in manifest["proposed_order"] if case_id not in consumed]
    assert len(remaining) == 17
    assert closeout["authorization_final_status"] == "CLOSED_STOPPED"
    for case_id in remaining:
        assert state["attempt_count"][case_id] == 0
        assert state["case_state"][case_id] == "NOT_RUN"
    assert not [event for event in journal if event.get("case_id") in remaining and event.get("event") in {
        "CASE_ATTEMPT_RESERVED", "CASE_CLOSEOUT", "CASE_EXCEPTION_STOP"}]
    assert not any(row.get("case_id") in remaining for row in closeout.get("case_results", []))
    return remaining


def test_r7_remainder_is_extracted_from_closed_r6_state_not_hand_entered():
    remaining = _r6_unconsumed()
    assert remaining == ["D1-POLICY-017", "D1-POLICY-018", "D1-AFTERSALES-021",
        "D1-AFTERSALES-023", "D1-AFTERSALES-025", "D1-AFTERSALES-026",
        "D1-AFTERSALES-027", "D1-CROSSAGENT-028", "D1-CROSSAGENT-030",
        "D1-CROSSAGENT-031", "D1-SAFETY-034", "D1-SAFETY-035", "D1-SAFETY-036",
        "D1-SAFETY-037", "D1-SAFETY-038", "D1-SAFETY-039", "D1-SAFETY-040"]


def test_r9_review_projection_separates_r8_batch_stop_from_policy_run_lifecycle():
    from scripts.recompute_m6_2_r9_policy018_closeout import (
        R8_LEDGER, R8_REVIEW, archive_projection, read_json, run_record_from_r8_row,
    )
    ledger, review = read_json(R8_LEDGER), read_json(R8_REVIEW)
    row = next(item for item in ledger["rows"] if item["case_id"] == "D1-POLICY-017")
    review_item = next(item for item in review["items"] if item["run_id"] == "b9e4d91b-f378-40c6-bd46-34c4fc98b2cc")
    record = run_record_from_r8_row(row, prior_review=review_item)
    from scripts.recompute_m6_2_r9_policy018_closeout import R8_JOURNAL
    journal = [json.loads(line) for line in (ROOT / R8_JOURNAL).read_text(encoding="utf-8").splitlines() if line.strip()]
    closeout = next(item for item in journal if item.get("event") == "CASE_CLOSEOUT" and
        item.get("case_id") == "D1-POLICY-017")
    record["archive_path"] = closeout["archive_path"]
    projection = archive_projection(record["archive_path"])
    record["run_status"] = projection["run_status"]
    assert projection["run_status"] == "SUCCEEDED"
    assert record["historical_segment"] == "EXECUTED_R8_BATCH_STOP"
    assert record["run_status"] == "SUCCEEDED"
    assert record["corrected_offline_code_checks"] == review_item["code_checks"]
    assert record["existing_ai_preview"]["status"] == "PASS"
    assert record["delivered_answer"] == review_item["delivered_answer"]
    assert record["delivered_answer_source"].startswith("run-bound R8 review transcription")
    assert record["human_confirmation_status"] == "PENDING_EXACT_RUN_REVIEW"


def test_r9_policy018_archive_projection_keeps_actual_run_and_program_grade():
    from scripts.recompute_m6_2_r9_policy018_closeout import (
        P018_ARCHIVE, archive_projection,
    )
    projection = archive_projection(str(ROOT / P018_ARCHIVE))
    assert projection["run_status"] == "SUCCEEDED"
    assert projection["provider_requests"] == 1
    assert projection["tokens"] == 616


def test_r8_run_projection_uses_explicit_settled_usage_without_defaulting_unknown():
    from scripts.recompute_m6_2_r9_policy018_closeout import (
        R8_LEDGER, R8_REVIEW, read_json, run_record_from_r8_row,
    )
    ledger = read_json(R8_LEDGER)
    row = next(item for item in ledger["rows"] if item["case_id"] == "D1-PRODUCT-003")
    record = run_record_from_r8_row(row)
    assert record["provider_requests"] == row["detail"]["actual_model_requests"] == 0
    assert record["tokens"] == row["detail"]["token_usage"] == 0
    assert record["local_estimated_usd"] == row["detail"]["local_estimated_usd"] == 0
    unknown = {**row, "detail": dict(row["detail"])}
    unknown["detail"].pop("actual_model_requests", None)
    unknown["detail"].pop("token_usage", None)
    unknown["detail"].pop("local_estimated_usd", None)
    unknown["detail"].pop("archive_path", None)
    projection = run_record_from_r8_row(unknown)
    assert projection["provider_requests"] is None
    assert projection["tokens"] is None
    assert projection["local_estimated_usd"] is None
    assert projection["local_cost_status"] == "UNKNOWN_NO_RUN_BOUND_SETTLED_COST"


def test_r6_case_result_usage_projection_restores_run_bound_zero_and_cost():
    from scripts.recompute_m6_2_r9_policy018_closeout import (
        R6_CASE_RESULTS, apply_r6_case_result_usage, read_json,
    )
    rows = read_json(R6_CASE_RESULTS)["cases"]
    expected = {
        "D1-ORDER-007": (0, 0, 0.0),
        "D1-ORDER-009": (0, 0, 0.0),
        "D1-ORDER-011": (1, 828, 0.0009935999999999999),
        "D1-ORDER-012": (1, 824, 0.0009888),
        "D1-POLICY-014": (1, 787, 0.0009444),
    }
    for case_id, (providers, tokens, cost) in expected.items():
        source = next(row for row in rows if row["case_id"] == case_id)
        record = {"local_estimated_usd": None}
        apply_r6_case_result_usage(record, source)
        assert record["provider_requests"] == providers
        assert record["tokens"] == tokens
        assert record["local_estimated_usd"] == cost
        assert record["usage_source"] == R6_CASE_RESULTS.as_posix()


def test_r9_offline_remediation_supersedes_only_verified_engineering_reasons():
    from scripts.recompute_m6_2_r9_policy018_closeout import build_offline_remediation_and_exit
    dispositions, _ = build_offline_remediation_and_exit("r12", {
        "tests": 110, "passed": 110, "failures": 0, "errors": 0, "skipped": 0,
    })
    rows = {row["case_id"]: row for row in dispositions["cases"]}
    for case_id in ("D1-PRODUCT-005", "D1-AFTERSALES-019", "D1-AFTERSALES-020", "D1-CROSSAGENT-032"):
        row = rows[case_id]
        assert row["status"] == "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH"
        assert "prior_reason" in row and "BLOCKED" in row["prior_status"]
        assert "actual isolated application" in row["reason"].lower() or "bound r3.2.4 contract" in row["reason"].lower()
    assert rows["D1-ORDER-008"]["status"] == "HELD_REVIEW_OR_AUTHORIZATION"
    assert rows["D1-CROSSAGENT-033"]["status"] == "HELD_REVIEW_OR_AUTHORIZATION"


def test_application_admission_uses_unique_allowed_order_item_product_id(isolated_context):
    from eval.m6_1.personal_dev_batch_m62 import app_admission
    target, coordinator = isolated_context
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    plan = plan_case(bundle, "D1-CROSSAGENT-032")
    evidence = app_admission(coordinator, plan)
    assert evidence["status"] == "APPLICATION_REPOSITORY_PREFLIGHT_PASS"
    reads = evidence["repository_reads"]
    item_read = next(row for row in reads if row["tool"] == "get_order_items")
    product_read = next(row for row in reads if row["tool"] == "get_product_detail")
    assert item_read["count"] == 1
    assert item_read["product_ids"] == [product_read["product_id"]]
    assert reads.index(item_read) < reads.index(product_read)
    assert evidence["receipt_cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED"
    assert evidence["receipt_cleanup"]["residual_case_rows"] == 0


def test_prior_human_review_matches_exact_run_not_case_id_only():
    from scripts.recompute_m6_2_r9_policy018_closeout import apply_exact_run_human_decision
    confirmed = {"old-run": {"case_id": "D1-POLICY-017", "decision": "PASS",
        "confirmation_event_id": "confirmed-event", "confirmation_event_sha256": "a" * 64,
        "reviewed_at_utc": "2026-10-29T00:00:00Z"}}
    new_attempt = {"case_id": "D1-POLICY-017", "run_id": "new-run"}
    apply_exact_run_human_decision(new_attempt, confirmed)
    assert new_attempt["human_confirmation_status"] == "PENDING_EXACT_RUN_REVIEW"
    assert "human_decision" not in new_attempt
    exact = {"case_id": "D1-POLICY-017", "run_id": "old-run"}
    apply_exact_run_human_decision(exact, confirmed)
    assert exact["human_decision"] == "PASS"
    assert exact["human_confirmation_event_id"] == "confirmed-event"
    assert exact["human_confirmation_status"] == "CONFIRMED_EXACT_RUN_SCOPE"


def test_metrics_mark_unrecorded_checks_instead_of_empty_or_zero():
    from scripts.recompute_m6_2_r9_policy018_closeout import build_review_metrics
    binding = {"implementation_digest": "impl", "rules_sha256": "rules", "dataset_sha256": DATASET_SHA}
    row = {"case_id": "D1-PRODUCT-005", "run_id": "run-1", "binding": binding,
        "run_status": "SUCCEEDED", "route": {"decision_source": "RULE", "strategy": "AGENT"},
        "program_checks": None, "program_task_outcome": "UNKNOWN", "ai_recommendation": None}
    metrics = build_review_metrics({"historical_runs": [row], "r9s4_runs": [],
        "policy018_recovery_run": {**row, "run_id": "run-2"}, "judge_calibration_summary": {}})
    group = next(iter(metrics["groups"].values()))
    assert group["program_tool_execution_checks"]["expected_business_tool_execution:NOT_RECORDED"] == 2
    assert group["program_tool_execution_checks"]["tool_result_audit_linkage:NOT_RECORDED"] == 2
    assert group["program_policy_retrieval_checks"]["NOT_RECORDED"] == 2
    assert group["program_user_delivery_checks"]["NOT_RECORDED"] == 2


def test_r7_replay_recovers_same_run_rejection_but_marks_damaged_candidate_unscorable():
    archive_path = ROOT / "docs/eval/personal-dev-runs/1184132fd96849d087ff104ed12f5baf.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    case = next(row for row in bundle["cases"] if row["case_id"] == "D1-POLICY-014")
    grade = score_archive_r7(case, archive, budget_snapshot={"complete": True, "settled": True,
        "active_reservations": 0, "unknown_tokens": False, "unknown_cost": False,
        "provider_requests": 1, "tokens": 787}, cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["rejection_audit_projection"]["status"] == "CAPTURED_REDACTED"
    assert grade["candidate_assessment"]["status"] == "REJECTED_CANDIDATE_NOT_DELIVERED"
    assert grade["candidate_assessment"]["encoding_quality"] == "TEXT_CAPTURED"
    assert grade["candidate_assessment"]["candidate_semantic_score"] == "PENDING_REVIEW"
    assert grade["candidate_assessment"]["is_user_output"] is False
    assert grade["program_checks"]["user_visible_answer_present"] == "FAIL"
    assert grade["task_outcome"] == "FAIL"
    assert grade["continue_decision"]["decision"] in {"CONTINUE", "STOP"}


def test_r7_attempt_journal_survives_restart_and_refuses_case_replay(tmp_path):
    journal = DurableAttemptJournal(tmp_path / "attempts.jsonl", batch_id="r7")
    journal.acquire_batch()
    journal.reserve_attempt("D1-POLICY-017", budget={"provider_calls": 6}, bindings={"manifest": "sha"})
    restarted = DurableAttemptJournal(tmp_path / "attempts.jsonl", batch_id="r7")
    assert restarted.consumed_cases() == {"D1-POLICY-017"}
    with pytest.raises(Exception):
        restarted.reserve_attempt("D1-POLICY-017", budget={"provider_calls": 6}, bindings={})


def test_r7_runtime_paths_are_batch_bound_and_reject_arbitrary_names(tmp_path):
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import runtime_path
    manifest = {"batch_id": "M62-R324-R7-REMAINDER-0123456789abcdef",
        "runtime_paths": {"authorization": "authorization-r7-0123456789abcdef.json"}}
    assert runtime_path(manifest, "authorization", tmp_path) == tmp_path / "authorization-r7-0123456789abcdef.json"
    manifest["runtime_paths"]["authorization"] = "authorization-other.json"
    with pytest.raises(Exception):
        runtime_path(manifest, "authorization", tmp_path)


def test_r8_complete_capability_scope_rejection_is_task_failure_but_batch_continuable():
    case = {"case_id": "D1-AFTERSALES-021", "input": {"expected_tool_calls": [
        {"tool_id": "get_order", "mode": "read_only"},
        {"tool_id": "search_knowledge", "mode": "read_only"}]}}
    run_id, trace_id = "run-synthetic", "trace-synthetic"
    diagnostic = {"schema_version": "route-capability-scope-diagnostic-v1",
        "stage": "CAPABILITY_SCOPE", "error_code": "ROUTE_CAPABILITY_SCOPE_REJECTED",
        "allowed_capabilities": ["get_order", "search_knowledge"],
        "requested_capabilities": ["get_order", "get_order_items", "search_knowledge"],
        "disallowed_capabilities": ["get_order_items"], "candidate_strategy": "AGENT",
        "candidate_sub_strategy": "AGENT+SINGLE", "provider_response_received": True,
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        "usage_status": "RECORDED", "run_id": run_id, "trace_id": trace_id}
    archive = {"result": {"run_id": run_id, "status": "FAILED", "result": None,
        "failure_code": "ROUTE_CAPABILITY_SCOPE_REJECTED", "observations": {
            "runs": [{"run_id": run_id, "status": "FAILED",
                "error_detail_json": json.dumps({"route_capability_scope": diagnostic})}],
            "messages": [], "run_events": [], "observability_records": [{"run_id": run_id,
                "trace_id": trace_id, "record_type": "span",
                "payload_json": json.dumps({"node_name": "router", "status": "ERROR"})}]}}}
    budget = {"snapshot_kind": "FINAL", "integrity_errors": [], "complete": True, "settled": True,
        "active_reservations": 0, "unknown_tokens": False, "unknown_cost": False,
        "calls_by_category": {"provider": 1, "tool": 0, "judge": 0, "embedding": 0},
        "tokens": 40, "estimated_usd": .001}
    grade = score_archive_r8(case, archive, budget_snapshot=budget,
        cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
    assert grade["route_scope_rejection"]["status"] == "COMPLETE_SAFE_MODEL_CAPABILITY_REJECTION"
    assert grade["program_checks"]["route_within_allowlist"] == "FAIL"
    assert grade["program_checks"]["tool_scope"] == "PASS"
    assert grade["task_outcome"] == "FAIL"
    assert grade["continue_decision"] == {"decision": "CONTINUE", "blocking_items": [],
        "recorded_case_failure": "CAPABILITY_SCOPE_REJECTED_WITH_COMPLETE_ZERO_EXECUTION_AUDIT"}
    assert batch_stop_reason_r8(grade) is None


def test_r8_capability_failure_with_missing_usage_or_activity_audit_stops():
    from eval.m6_1.personal_dev_batch_m62 import score_archive_r8
    case = {"case_id": "D1-POLICY-017", "input": {"expected_tool_calls": [
        {"tool_id": "search_knowledge", "mode": "read_only"}]}}
    archive = {"result": {"run_id": "r", "status": "FAILED",
        "failure_code": "ROUTE_CAPABILITY_SCOPE_REJECTED", "observations": {
        "runs": [{"run_id": "r", "status": "FAILED", "error_detail_json": "{}"}],
        "messages": [], "run_events": [], "traces": []}}}
    grade = score_archive_r8(case, archive, budget_snapshot={"complete": False}, cleanup_status="UNKNOWN")
    assert grade["route_scope_rejection"]["status"] == "UNKNOWN_OR_INCOMPLETE"
    assert batch_stop_reason_r8(grade) == "R8_BLOCKING_EVIDENCE_OR_SAFETY_FAILURE"


def test_cli_failure_summary_recovers_persisted_run_after_report_error(tmp_path):
    from scripts import run_m6_2_r324_personal_dev_batch_r7 as cli
    state_path = tmp_path / "state.json"
    journal_path = tmp_path / "journal.jsonl"
    auth_path = tmp_path / "auth.json"
    state_path.write_text(json.dumps({"agent_started": True, "run_created": True,
        "attempt_count": {"D1-POLICY-017": 1}, "cleanup_status": "CLEANED_AND_RELEASED"}), encoding="utf-8")
    journal_path.write_text(json.dumps({"event": "RUN_CREATED", "case_id": "D1-POLICY-017",
        "run_id": "persisted-run"}) + "\n", encoding="utf-8")
    auth_path.write_text(json.dumps({"status": "CLOSED_STOPPED"}), encoding="utf-8")
    old = dict(cli._CLI_CONTEXT)
    try:
        cli._CLI_CONTEXT.update({"state_path": state_path, "journal_path": journal_path,
            "auth_path": auth_path})
        summary = cli._cli_failure_projection(FileNotFoundError(2, "missing", str(tmp_path / "reports" / "x.json")))
    finally:
        cli._CLI_CONTEXT.clear()
        cli._CLI_CONTEXT.update(old)
    assert summary["run_created"] is True
    assert summary["run_ids"] == ["persisted-run"]
    assert summary["agent_started"] is True
    assert summary["authorization_status"] == "CLOSED_STOPPED"
    assert summary["missing_repository_relative_path"].endswith("/reports/x.json")


def test_cli_failure_projection_retains_only_safe_nameerror_identifier(tmp_path):
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import _cli_failure_projection
    safe = _cli_failure_projection(NameError("local setup", name="missing_binding"))
    unsafe = _cli_failure_projection(NameError("local setup", name="/secret/path"))
    assert safe["undefined_name"] == "missing_binding"
    assert unsafe["undefined_name"] is None


def test_policy018_recovery_profile_is_exact_and_historical_failure_is_proven_no_run():
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import PROFILE_CONFIGS, task_quota_ledger_rel, validate_policy018_recovery
    profile = PROFILE_CONFIGS["r9p018"]
    assert profile["count"] == 1
    assert profile["auth_type"] == "M62_PERSONAL_DEV_R9_POLICY018_ONE_TIME_RECOVERY"
    assert task_quota_ledger_rel("r9p018r3", profile["package"]).endswith("task-cumulative-ledger-r2-policy018-recovery.json")
    assert task_quota_ledger_rel("r9s4", profile["package"]).endswith("task-cumulative-ledger-r1.json")
    assert validate_policy018_recovery(ROOT, {"proposed_order": ["D1-POLICY-018"]})[
        "original_run_created"] is False
    with pytest.raises(Exception):
        validate_policy018_recovery(ROOT, {"proposed_order": ["D1-POLICY-018", "D1-SAFETY-039"]})


def test_task_quota_recovery_preserves_original_attempt_and_cannot_replay(tmp_path):
    source = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r1.json"
    value = json.loads(source.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    value["limits"]["application_runs"] = 16
    value["limit_amendments"] = [{"from_application_runs": 15, "to_application_runs": 16,
        "source_sha256": source_sha, "scope": "POLICY-018 one-time first Run only"}]
    ledger_path = tmp_path / "task-ledger-r2.json"
    ledger = TaskQuotaLedger(ledger_path, task_id=value["task_id"], seed_sha256=value["seed_sha256"])
    ledger.write(value)
    before = ledger.read()
    assert before["spent"]["application_runs"] == 15
    limits = {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
        "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
        "max_calls_per_trial": 20, "max_concurrent_trials": 1,
        "max_duration_seconds": 900, "max_retries": 0}
    ledger.reserve_recovery_case("D1-POLICY-018", limits=limits,
        authorization_id="new-policy018-auth", source_record_sha256=hashlib.sha256(b"source").hexdigest())
    reserved = ledger.read()
    assert reserved["attempts"]["D1-POLICY-018"] == 1
    assert reserved["case_records"]["D1-POLICY-018"]["status"] == "CLOSED_NO_RUN_PRE_EXECUTION_ERROR"
    assert reserved["reserved"]["application_runs"] == 1
    ledger.settle_case("D1-POLICY-018", run_created=True, provider_requests=1, tokens=700,
        local_estimated_usd=0.001, elapsed_seconds=8, status="CLOSED",
        recovery_authorization_id="new-policy018-auth")
    settled = ledger.read()
    assert settled["spent"]["application_runs"] == 16
    assert settled["recovery_records"]["D1-POLICY-018"]["status"] == "CLOSED"
    with pytest.raises(Exception):
        ledger.reserve_recovery_case("D1-POLICY-018", limits=limits,
            authorization_id="second-auth", source_record_sha256=hashlib.sha256(b"source").hexdigest())


def test_r10_first_run_profile_is_exactly_the_four_r17_offline_ready_cases():
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import (
        PROFILE_CONFIGS, R10_FIRST_RUN_CASES, validate_r10_first_run_cases)
    profile = PROFILE_CONFIGS["r10-four-first-runs-r2"]
    assert profile["count"] == 4
    assert profile["profile_version"] == "m6.2-r324-four-first-runs-r2"
    assert R10_FIRST_RUN_CASES == ["D1-PRODUCT-005", "D1-AFTERSALES-019",
        "D1-AFTERSALES-020", "D1-CROSSAGENT-032"]
    sources = {}
    for field, rel in {
        "remediation_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json",
        "coverage_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/40-case-coverage-ledger-r9-final-r17.json",
        "task_ledger_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r2-policy018-recovery.json",
    }.items():
        sources[field] = rel
        sources[field.replace("_path", "_sha256")] = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
    evidence = validate_r10_first_run_cases(ROOT, {"proposed_order": R10_FIRST_RUN_CASES,
        "r9_first_run_sources": sources, "readiness_exception": {
            "case_ids": R10_FIRST_RUN_CASES,
            "source_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json",
            "source_sha256": sources["remediation_sha256"],
            "source_status": "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH"}})
    assert evidence["r9_application_runs"] == 16
    assert evidence["case_count"] == 4
    assert evidence["application_ready_count"] == 4
    assert evidence["new_case_attempts_zero"] is True
    with pytest.raises(Exception):
        validate_r10_first_run_cases(ROOT, {"proposed_order": list(reversed(R10_FIRST_RUN_CASES)),
            "r9_first_run_sources": sources, "readiness_exception": {
                "case_ids": R10_FIRST_RUN_CASES,
                "source_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json",
                "source_sha256": sources["remediation_sha256"],
                "source_status": "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH"}})


def test_task_quota_successor_amends_run_cap_without_resetting_history(tmp_path):
    from eval.m6_1.personal_dev_batch_m62 import TaskQuotaLedger
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import R10_FIRST_RUN_CASES
    source = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/task-cumulative-ledger-r2-policy018-recovery.json"
    value = json.loads(source.read_text(encoding="utf-8"))
    old_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert value["spent"]["application_runs"] == 16
    value["limits"]["application_runs"] = 20
    value["limit_amendments"].append({"from_application_runs": 16, "to_application_runs": 20,
        "source_sha256": old_sha, "authorization_basis": "CURRENT_USER_INSTRUCTION_FOUR_FIRST_RUNS"})
    path = tmp_path / "task-ledger-r3.json"
    ledger = TaskQuotaLedger(path, task_id=value["task_id"], seed_sha256=value["seed_sha256"])
    ledger.write(value)
    assert ledger.read()["spent"]["application_runs"] == 16
    limits = {"provider_calls": 40, "embedding_calls": 0, "judge_calls": 0,
        "total_tokens": 600000, "estimated_usd": 2.0, "tool_calls": 20,
        "max_calls_per_trial": 60, "max_concurrent_trials": 1,
        "max_duration_seconds": 900, "max_retries": 0}
    for case_id in ["D1-PRODUCT-005", "D1-AFTERSALES-019", "D1-AFTERSALES-020", "D1-CROSSAGENT-032"]:
        ledger.reserve_case(case_id, limits=limits)
        ledger.settle_case(case_id, run_created=True, provider_requests=0, tokens=0,
            local_estimated_usd=0.0, elapsed_seconds=1, status="CLOSED_OFFLINE_TEST")
    final = ledger.read()
    assert final["spent"]["application_runs"] == 20
    assert final["spent"]["provider_requests"] == value["spent"]["provider_requests"]
    assert all(final["attempts"][case_id] == 1 for case_id in R10_FIRST_RUN_CASES)


def test_case_results_can_be_atomically_written_and_verified_before_closeout(tmp_path):
    from scripts.run_m6_2_r324_personal_dev_batch_r7 import _atomic_create_json, digest
    case_path = tmp_path / "case-results.json"
    closeout_path = tmp_path / "closeout.json"
    case_sha = _atomic_create_json(case_path, {"cases": [{"case_id": "D1-POLICY-017"}]})
    assert case_path.is_file() and digest(case_path) == case_sha
    _atomic_create_json(closeout_path, {"case_results_path": case_path.name,
        "case_results_sha256": case_sha})
    assert json.loads(closeout_path.read_text(encoding="utf-8"))["case_results_sha256"] == digest(case_path)


def test_safe_persistence_keeps_usage_counts_but_redacts_token_secrets():
    from app.m1.models import safe_trace_json
    value = {"usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
        "api_token": "never-log-this"}}
    saved = json.loads(safe_trace_json(value))
    assert saved["usage"] == {"prompt_tokens": 12, "completion_tokens": 3,
        "total_tokens": 15, "api_token": "[REDACTED]"}


def test_actual_application_persists_capability_rejection_then_r8_safely_continues(isolated_context,
        monkeypatch, tmp_path):
    target, coordinator = isolated_context
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    case = next(row for row in bundle["cases"] if row["case_id"] == "D1-AFTERSALES-021")
    plan = plan_case(bundle, case["case_id"])
    sdk = _InterceptedSDK()
    original_create = sdk.create

    def create_with_scope_error(*, messages, **kwargs):
        system = messages[0]["content"]
        if "Classify the user's" in system:
            allowed = []
            marker = "available for this request are: "
            if marker in system:
                allowed = json.loads(system.split(marker, 1)[1].split(". ", 1)[0])
            requested = sorted(set(allowed) | {"cancel_order"})
            response = {"strategy": "AGENT", "sub_strategy": "AGENT+SINGLE",
                "intent_labels": ["business_action"], "safety_decision": "ALLOW",
                "reason_code": "LIVE_BUSINESS_FACT", "needs_clarification": False,
                "clarification_question": None, "required_capabilities": requested}
            sdk.requests.append({"system": system, "user": messages[-1]["content"]})
            return SimpleNamespace(model="offline-scope-interceptor", choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=json.dumps(response)))], usage=_Usage())
        return original_create(messages=messages, **kwargs)

    sdk.chat.completions.create = create_with_scope_error
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(load_settings(require_api_key=False), api_key="offline-only",
        database_url=f"sqlite:///{target.path.as_posix()}", orchestrator="langgraph", m3_supervisor_enabled=True)
    limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
        estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    ledger = _TestLedger(limits)
    receipt = coordinator.prepare(plan)
    archive_path = tmp_path / "scope-rejection-archive.json"
    try:
        result = execute_application_case(coordinator, plan, receipt, ledger, settings)
        archive = {"case_id": case["case_id"], "result": result}
        archive_path.write_text(json.dumps(archive, ensure_ascii=False, default=str), encoding="utf-8")
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        run = result["observations"]["runs"][0]
        detail = json.loads(run["error_detail_json"])
        diagnostic = detail["route_capability_scope"]
        assert result["status"] == "FAILED"
        assert result["failure_code"] == "ROUTE_CAPABILITY_SCOPE_REJECTED"
        assert diagnostic["run_id"] == result["run_id"]
        assert diagnostic["trace_id"]
        assert diagnostic["disallowed_capabilities"] == ["cancel_order"]
        assert diagnostic["usage_status"] == "RECORDED"
        from eval.m6_1.personal_dev_batch_m62 import _route_scope_rejection_from_archive
        diagnostic_archive = json.loads(archive_path.read_text(encoding="utf-8"))
        assert _route_scope_rejection_from_archive(diagnostic_archive), [
            {key: row.get(key) for key in ("run_id", "trace_id", "node_name", "status", "error_code")}
            for row in result["observations"]["traces"]]
        events = result["observations"]["run_events"]
        assert not [row for row in events if row["event_type"] in {"tool_started", "tool_finished"}]
        assert len(sdk.requests) == 1
        cleanup_safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert cleanup_safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        grade = score_archive_r8(case, archive, budget_snapshot={"snapshot_kind": "FINAL", "integrity_errors": [],
            "complete": True, "settled": True, "active_reservations": 0, "unknown_tokens": False,
            "unknown_cost": False, "calls_by_category": {"provider": 1, "tool": 0, "judge": 0, "embedding": 0},
            "tokens": 90, "estimated_usd": .001},
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        assert grade["route_scope_rejection"]["status"] == "COMPLETE_SAFE_MODEL_CAPABILITY_REJECTION", grade["route_scope_rejection"]
        assert grade["failure_classification"] == "KNOWN_SINGLE_MODEL_CAPABILITY_SELECTION_FAILURE"
        assert grade["task_outcome"] == "FAIL"
        assert batch_stop_reason_r8(grade) is None
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_actual_successful_policy_application_remains_continuable_under_r8(isolated_context,
        monkeypatch, tmp_path):
    target, coordinator = isolated_context
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    case = next(row for row in bundle["cases"] if row["case_id"] == "D1-POLICY-017")
    plan = plan_case(bundle, case["case_id"])
    sdk = _InterceptedSDK()
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(load_settings(require_api_key=False), api_key="offline-only",
        database_url=f"sqlite:///{target.path.as_posix()}", orchestrator="langgraph", m3_supervisor_enabled=True)
    limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
        estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    receipt = coordinator.prepare(plan)
    archive_path = tmp_path / "policy-success-r8-archive.json"
    try:
        result = execute_application_case(coordinator, plan, receipt, _TestLedger(limits), settings)
        archive_path.write_text(json.dumps({"case_id": case["case_id"], "result": result},
            ensure_ascii=False, default=str), encoding="utf-8")
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        assert result["status"] == "SUCCEEDED"
        assert len(sdk.requests) == 1
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        budget = {"snapshot_kind": "FINAL", "complete": True, "settled": True,
            "active_reservations": 0, "integrity_errors": [], "unknown_tokens": False, "unknown_cost": False,
            "calls_by_category": {"provider": 1, "tool": 1, "judge": 0, "embedding": 0},
            "tokens": 90, "estimated_usd": .001}
        grade = score_archive_r8(case, archive, budget_snapshot=budget,
            cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED")
        assert grade["route_scope_rejection"]["status"] == "NOT_APPLICABLE"
        assert grade["continue_decision"]["decision"] == "CONTINUE"
        assert batch_stop_reason_r8(grade) is None
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


@pytest.mark.parametrize(("case_id", "expected_tools"), [
    ("D1-PRODUCT-005", {"get_product_detail", "search_knowledge"}),
    ("D1-AFTERSALES-019", {"get_order", "search_knowledge"}),
    ("D1-AFTERSALES-020", {"get_order", "get_refund_status", "search_knowledge"}),
    ("D1-CROSSAGENT-032", {"get_order", "get_order_items", "get_product_detail", "search_knowledge"}),
])
def test_real_application_composite_routes_execute_only_declared_fixture_reads(
        isolated_context, monkeypatch, tmp_path, case_id, expected_tools):
    target, coordinator = isolated_context
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    case = next(row for row in bundle["cases"] if row["case_id"] == case_id)
    plan = plan_case(bundle, case_id)
    sdk = _InterceptedSDK()
    monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, **_k: sdk)
    settings = replace(load_settings(require_api_key=False), api_key="offline-only",
        database_url=f"sqlite:///{target.path.as_posix()}", orchestrator="langgraph", m3_supervisor_enabled=True)
    limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
        estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
        max_duration_seconds=180, max_retries=0)
    receipt = coordinator.prepare(plan)
    archive_path = tmp_path / f"{case_id}-composite-archive.json"
    try:
        result = execute_application_case(coordinator, plan, receipt, _TestLedger(limits), settings)
        archive_path.write_text(json.dumps({"case_id": case_id, "result": result},
            ensure_ascii=False, default=str), encoding="utf-8")
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        assert result["status"] == "SUCCEEDED"
        payload = result["result"]
        assert payload["route"]["strategy"] == "AGENT"
        assert payload["route"]["route_version"] == "m2.1-route-v12"
        assert set(payload["route"]["required_capabilities"]) == expected_tools
        assert payload["answer"].strip()
        events = result["observations"]["run_events"]
        finished = [row for row in events if row["event_type"] == "tool_finished"]
        observed_tools = {json.loads(row["payload_json"]).get("tool") for row in finished}
        assert observed_tools == expected_tools
        policy_events = [row for row in events if row["event_type"] == "policy_retrieval_finished"]
        assert len(policy_events) == 1
        policy_payload = json.loads(policy_events[0]["payload_json"])
        assert policy_payload["run_id"] == result["run_id"]
        assert policy_payload["status"] == "SUCCEEDED"
        assert policy_payload["result_ref"]
        if case_id == "D1-CROSSAGENT-032":
            business = payload["business_results"]
            order_item = business["order_logistics"]["get_order_items"]["items"][0]
            order = business["order_logistics"]["get_order"]
            product = business["product"]["get_product_detail"]["product"]
            assert order_item["product_id"] == product["product_id"]
            assert business["order_logistics"]["get_order_items"]["order_id"] == order["order_id"]
            assert order["order_id"] == "ORD-SYN-116"
            started_agents = {json.loads(row["payload_json"]).get("agent") for row in events
                if row["event_type"] == "agent_started"}
            assert {"order_logistics", "product", "policy"}.issubset(started_agents)
        assert sdk.requests == []  # deterministic route/specialists/retrieval; no Provider call required
        safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
        assert safety["safe_to_cleanup"] is True
        coordinator.cleanup(receipt)
        cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
        assert archive["result"]["observations"]["runs"][0]["status"] == "SUCCEEDED"
    finally:
        with coordinator.factory() as db:
            active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
        if active == receipt.lease_id:
            coordinator.cleanup(receipt)


def test_successor_task_quota_counts_r8_usage_and_does_not_reset_on_new_segment(tmp_path):
    path = tmp_path / "task-ledger.json"
    task_id = "M62-R8-CONTINUATION-TEST"
    seed = "a" * 64
    quotas = TaskQuotaLedger(path, task_id=task_id, seed_sha256=seed)
    state = {"schema_version": "m62-task-quota-v1", "task_id": task_id, "seed_sha256": seed,
        "limits": {"application_runs": 15, "provider_requests": 600, "tokens": 10_000_000,
            "local_estimated_usd": 30.0, "runtime_seconds": 14_400, "repository_preflight_reads": 1000},
        "spent": {"application_runs": 1, "provider_requests": 1, "tokens": 629,
            "local_estimated_usd": .0007548, "runtime_seconds": 7, "repository_preflight_reads": 0},
        "reserved": {"application_runs": 0, "runtime_seconds": 0, "provider_requests": 0,
            "tokens": 0, "local_estimated_usd": 0.0},
        "attempts": {}, "case_records": {}, "segments": [], "unknown_usage": False, "over_limit": False}
    quotas.write(state)
    per_case = {"max_duration_seconds": 900, "provider_calls": 40,
        "total_tokens": 600000, "estimated_usd": 2.0}
    for index in range(14):
        quotas.reserve_case(f"CASE-{index}", limits=per_case)
        quotas.settle_case(f"CASE-{index}", run_created=True, provider_requests=0, tokens=0,
            local_estimated_usd=0.0, elapsed_seconds=100, status="CLOSED")
    with pytest.raises(Exception, match="M62_TASK_APPLICATION_RUN_QUOTA_EXHAUSTED"):
        quotas.reserve_case("CASE-14", limits=per_case)
    with pytest.raises(Exception, match="M62_TASK_CASE_ATTEMPT_ALREADY_CONSUMED"):
        quotas.reserve_case("CASE-0", limits=per_case)
    final = quotas.read()
    assert final["reserved"]["application_runs"] == 0
    assert final["spent"]["application_runs"] == 15
    assert final["spent"]["provider_requests"] == 1
    assert final["spent"]["tokens"] == 629
    assert final["spent"]["local_estimated_usd"] == .0007548
    assert final["attempts"]["CASE-0"] == 1


def test_task_quota_preflight_returns_persisted_integrity_snapshot(tmp_path):
    path = tmp_path / "preflight-ledger.json"
    quota = TaskQuotaLedger(path, task_id="M62-PREFLIGHT-RETURN-TEST", seed_sha256="b" * 64)
    quota.write({"schema_version": "m62-task-quota-v1", "task_id": "M62-PREFLIGHT-RETURN-TEST",
        "seed_sha256": "b" * 64,
        "limits": {"application_runs": 15, "provider_requests": 600, "tokens": 10000000,
            "local_estimated_usd": 30.0, "runtime_seconds": 14400, "repository_preflight_reads": 1000},
        "spent": {"application_runs": 1, "provider_requests": 1, "tokens": 629,
            "local_estimated_usd": .0007548, "runtime_seconds": 8, "repository_preflight_reads": 35},
        "reserved": {"application_runs": 0, "runtime_seconds": 0, "provider_requests": 0,
            "tokens": 0, "local_estimated_usd": 0.0},
        "attempts": {"D1-POLICY-017": 1}, "case_records": {}, "segments": [],
        "unknown_usage": False, "over_limit": False})

    returned = quota.record_preflight_reads(16)
    persisted = quota.read()
    assert returned["spent"]["repository_preflight_reads"] == 51
    assert returned["state_sha256"] == persisted["state_sha256"]
    assert returned["state_sha256"] == TaskQuotaLedger._hash(returned)


def test_r9_continue_policy_allows_complete_task_failure_but_stops_unknown_evidence():
    grade = {"program_checks": {"run_terminal": "PASS", "run_trace_association": "PASS",
        "tool_scope": "PASS", "tool_audit_completeness": "PASS", "tool_result_audit_linkage": "PASS",
        "budget_and_cleanup": "PASS", "route_within_allowlist": "PASS", "provider_execution": "PASS"},
        "task_outcome": "FAIL", "continue_decision": {"decision": "CONTINUE", "blocking_items": []}}
    assert batch_stop_reason_r9(grade) is None
    grade["program_checks"]["tool_audit_completeness"] = "UNKNOWN"
    assert batch_stop_reason_r9(grade) == "R9_CRITICAL_EXECUTION_EVIDENCE_OR_SAFETY_FAILURE"


def test_r9_honors_explicit_checker_stop_when_critical_checks_are_otherwise_passed():
    grade = {"program_checks": {"run_terminal": "PASS", "run_trace_association": "PASS",
        "tool_scope": "PASS", "tool_audit_completeness": "PASS", "tool_result_audit_linkage": "PASS",
        "budget_and_cleanup": "PASS", "route_within_allowlist": "PASS", "provider_execution": "PASS"},
        "task_outcome": "FAIL", "continue_decision": {"decision": "STOP",
            "blocking_items": ["failure_stage_or_usage_unknown"]}}
    assert batch_stop_reason_r9(grade) == "R9_LOCKED_STOP_DECISION_WITH_BLOCKERS"


def test_r9_complete_safe_capability_rejection_is_nonblocking_but_incomplete_is_blocking():
    grade = {"program_checks": {"run_terminal": "PASS", "run_trace_association": "PASS",
        "tool_scope": "PASS", "tool_audit_completeness": "PASS", "tool_result_audit_linkage": "PASS",
        "budget_and_cleanup": "PASS", "route_within_allowlist": "FAIL", "provider_execution": "PASS"},
        "route_scope_rejection": {"status": "COMPLETE_SAFE_MODEL_CAPABILITY_REJECTION"},
        "continue_decision": {"decision": "CONTINUE", "blocking_items": []}}
    assert batch_stop_reason_r9(grade) is None
    grade["route_scope_rejection"]["status"] = "UNKNOWN_OR_INCOMPLETE"
    assert batch_stop_reason_r9(grade) == "R9_ROUTE_SCOPE_EVIDENCE_FAILURE"


def test_r9_budget_limits_use_new_per_case_caps_but_segment_remains_under_cumulative_quota():
    per_case, batch = derive_limits(15,
        per_case_overrides={"provider_calls": 40, "total_tokens": 600000,
            "estimated_usd": 2.0, "tool_calls": 20, "max_calls_per_trial": 20,
            "max_duration_seconds": 900},
        batch_overrides={"provider_calls": 599, "total_tokens": 9_999_371,
            "estimated_usd": 29.9992452, "tool_calls": 300, "max_calls_per_trial": 300,
            "max_duration_seconds": 14_393})
    assert per_case.provider_calls == 40 and per_case.total_tokens == 600000
    assert batch.provider_calls == 599 and batch.total_tokens == 9_999_371
    assert batch.estimated_usd + .0007548 <= 30.0


def test_r7_runs_all_remainder_cases_through_intercepted_actual_application(isolated_context, monkeypatch, tmp_path):
    target, coordinator = isolated_context
    bundle = json.loads((ROOT / DATASET_REL).read_text(encoding="utf-8"))
    matrix = json.loads((ROOT / MATRIX_REL).read_text(encoding="utf-8"))
    source = {row["case_id"]: row for row in matrix["cases"]}
    cases = {row["case_id"]: row for row in bundle["cases"]}
    records = []
    for case_id in _r6_unconsumed():
        plan = plan_case(bundle, case_id)
        if not source[case_id]["allowed_read_only_capabilities"]:
            plan = plan_without_unauthorized_targets(plan)
        sdk = _InterceptedSDK()
        monkeypatch.setattr(config_module, "create_deepseek_client", lambda *_a, _sdk=sdk, **_k: _sdk)
        settings = replace(load_settings(require_api_key=False), api_key="offline-only",
            database_url=f"sqlite:///{target.path.as_posix()}", orchestrator="langgraph", m3_supervisor_enabled=True)
        limits = BudgetLimits(provider_calls=6, embedding_calls=0, judge_calls=0, total_tokens=120000,
            estimated_usd=.18, tool_calls=8, max_calls_per_trial=8, max_concurrent_trials=1,
            max_duration_seconds=180, max_retries=0)
        receipt = coordinator.prepare(plan)
        try:
            result = execute_application_case(coordinator, plan, receipt, _TestLedger(limits), settings)
            archive_path = ROOT / "data/eval/offline-tests" / f"m62-r7-admission-{uuid.uuid4().hex}.json"
            archive_path.write_text(json.dumps({"case_id": case_id, "result": result,
                "model_transport": "IN_PROCESS_INTERCEPTOR_NO_NETWORK"}, ensure_ascii=False, indent=2,
                default=str) + "\n", encoding="utf-8")
            safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
            if safety.get("safe_to_cleanup") is True:
                coordinator.cleanup(receipt)
            cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=bool(safety.get("safe_to_cleanup")))
            observations = result.get("observations") or {}
            events, traces = observations.get("run_events", []), observations.get("traces", [])
            route_events = [e for e in events if e.get("event_type") == "route_decided"]
            route = result.get("result", {}).get("route", {}) if isinstance(result.get("result"), dict) else {}
            allowed = set(source[case_id]["allowed_read_only_capabilities"])
            route_ok = bool(route) and set(route.get("required_capabilities", [])).issubset(allowed)
            clean = cleanup.get("cleanup_status") == "CLEANED_AND_RELEASED" and cleanup.get("residual_case_rows") == 0
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            grade = score_archive_r7(cases[case_id], archive,
                budget_snapshot={"complete": True, "settled": True, "active_reservations": 0,
                    "unknown_tokens": False, "unknown_cost": False,
                    "provider_requests": len(sdk.requests), "tokens": 90 * len(sdk.requests)},
                cleanup_status="RECEIPT_CLEANED_AND_LEASE_RELEASED" if clean else "UNKNOWN")
            observed = set(grade.get("observed_tools", []))
            expected = {r["tool_id"] for r in cases[case_id].get("input", {}).get("expected_tool_calls", [])
                if r.get("mode") == "read_only"}
            expected_business = expected - {"search_knowledge"}
            policy_ok = ("search_knowledge" not in expected or
                         grade["program_checks"].get("policy_retrieval_audit") == "PASS")
            expected_ok = ((expected_business.issubset(observed) and policy_ok) if expected else
                route.get("strategy") in {"DIRECT", "REFUSE"} and not observed and
                not grade.get("policy_retrieval_call_ids"))
            terminal = result.get("status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
            critical = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
                        "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup",
                        "user_visible_answer_present")
            technical = all(grade["program_checks"].get(name) == "PASS" for name in critical)
            status = "APPLICATION_READY" if (terminal and len(route_events) == 1 and bool(traces) and
                route_ok and clean and expected_ok and technical and result.get("status") != "FAILED") else "BLOCKED_ENGINEERING"
            records.append({"case_id": case_id, "application_admission": status,
                "run_status": result.get("status"), "route": route,
                "expected_capabilities": sorted(expected), "observed_tools": sorted(observed),
                "intercepted_provider_requests": len(sdk.requests), "run_id": result.get("run_id"),
                "archive_path": archive_path.relative_to(ROOT).as_posix(),
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "program_score": grade, "cleanup": cleanup})
        finally:
            with coordinator.factory() as db:
                active = db.execute(text("SELECT lease_id FROM d1_personal_target")).scalar_one()
            if active == receipt.lease_id:
                coordinator.cleanup(receipt)
    assert len(records) == 17 and [row["case_id"] for row in records] == _r6_unconsumed()
    output = tmp_path / "application-admission-offline-replay.json"
    output.write_text(json.dumps({"schema_version": "m62-r7-application-admission-offline-r1",
        "case_count": 17, "case_ids": [row["case_id"] for row in records], "records": records,
        "application_ready_count": sum(row["application_admission"] == "APPLICATION_READY" for row in records),
        "ready_count": sum(row["application_admission"] == "APPLICATION_READY" for row in records),
        "blocked_count": sum(row["application_admission"] != "APPLICATION_READY" for row in records),
        "dataset_sha256": hashlib.sha256((ROOT / DATASET_REL).read_bytes()).hexdigest(),
        "model_transport": "IN_PROCESS_INTERCEPTOR_NO_NETWORK", "external_calls": 0,
        "formal_run_created": False,
        "implementation_digest": __import__("eval.m6_1.personal_dev", fromlist=["implementation_digest"]).implementation_digest(ROOT),
        "isolated_database_identity": target.identity}, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    assert all(row["cleanup"]["cleanup_status"] == "CLEANED_AND_RELEASED" for row in records)
    assert all(row["cleanup"]["residual_case_rows"] == 0 for row in records)


class _TestLedger:
    def __init__(self, limits): self.limits, self.requests = limits, []
    def reserve(self, **kwargs): self.requests.append(("reserve", kwargs)); return f"reservation-{len(self.requests)}"
    def settle(self, *args, **kwargs): self.requests.append(("settle", args, kwargs))
