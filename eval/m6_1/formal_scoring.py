"""Formal projections reuse persisted application checks, never expected outcomes as observations."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from .contracts import EvalOutcome, sha256_json
from .grader import GradeResult
from .grants import formal_case_payload, formal_case_digest
from .personal_dev_batch_m62 import score_archive_current


def evaluate(case, archive, usage: dict, *, allow_known_classifier_rejection=False) -> dict:
    from copy import deepcopy
    payload = formal_case_payload(case)
    payload["input"]["expected_tool_calls"] = [{"tool_id": tool, "mode": "read_only"} for tool in case.input["allowed_capabilities"]]
    read_usage=deepcopy(usage)
    action=archive.get("owner_interaction")
    action_calls=len(action["calls"])+len(action.get("policy_reads",[])) if action is not None else 0
    measurement=archive.get("retrieval_evaluation")
    evaluation_calls=len(measurement["calls"]) if measurement is not None else 0
    separate_calls=action_calls+evaluation_calls
    if separate_calls:
        read_usage["calls_by_category"]["tool"]-=separate_calls
        if read_usage["calls_by_category"]["tool"]<0:raise ValueError("action calls exceed actual settled tool count")
    technical = score_archive_current(payload, archive, budget_snapshot=read_usage,
        cleanup_status="PENDING_SEPARATE_RECEIPT_CLEANUP")
    checks = technical["program_checks"]
    outer = archive["result"]
    events = outer["observations"]["run_events"]
    route_rows = [json.loads(e["payload_json"]) for e in events if e["event_type"] == "route_decided"]
    route = route_rows[0] if len(route_rows) == 1 else None
    policy = {"policy_retrieval_started": {}, "policy_retrieval_finished": {}}
    for event in events:
        if event["event_type"] in policy:
            value = json.loads(event["payload_json"])
            call_id = value["call_id"]
            if call_id in policy[event["event_type"]]:
                raise ValueError("duplicate policy call event")
            policy[event["event_type"]][call_id] = value
    starts, finishes = policy.values()
    policy_linked = set(starts) == set(finishes) and all(
        starts[c]["run_id"] == finishes[c]["run_id"] == outer["run_id"] and
        starts[c]["trace_id"] == finishes[c]["trace_id"] and starts[c]["trace_id"] and
        starts[c]["result_ref"] == finishes[c]["result_ref"] and starts[c]["result_ref"] and finishes[c]["status"] == "SUCCEEDED"
        for c in starts)
    # A valid zero-result retrieval is different from missing call evidence.
    policy_only = bool(starts) and not technical["observed_tools"]
    audit = checks["run_trace_association"] == "PASS" and bool(route)
    if policy_only:
        audit = audit and policy_linked
    else:
        audit = audit and checks["tool_audit_completeness"] == "PASS" and checks["tool_result_audit_linkage"] == "PASS" and policy_linked
    invocations = archive["read_invocations"]
    business_finishes = [json.loads(e["payload_json"]) for e in events if e["event_type"] == "tool_finished"]
    tool_call_ids = {row["tool_call_id"] for row in business_finishes}
    business_starts = [json.loads(e["payload_json"]) for e in events if e["event_type"] == "tool_started"]
    starts_by_id = {row["tool_call_id"]: row for row in business_starts}
    if len(starts_by_id) != len(business_starts) or set(starts_by_id) != tool_call_ids:
        audit = False
    else:
        for finish in business_finishes:
            start = starts_by_id[finish["tool_call_id"]]
            if (not finish["result_ref"] or not start["parameter_fingerprint"] or
                    any(start[key] != finish[key] for key in ("tool", "run_id", "trace_id", "parameter_fingerprint")) or
                    start["run_id"] != outer["run_id"] or not start["trace_id"]):
                audit = False
    # New invocation bindings survive a rejected final response. Exact call IDs,
    # fingerprints, Run/Trace and references must agree with durable tool events.
    bound_reads = bool(invocations) and bool(business_finishes) and all(
        r.get("tool_audit") and r["status"] == "RETURNED" for r in invocations)
    if bound_reads:
        finish_map = {r["tool_call_id"]: r for r in business_finishes}
        bound_reads = len(invocations) == len(finish_map) and all(
            r["tool_audit"]["tool_call_id"] in finish_map and
            all(r["tool_audit"][k] == finish_map[r["tool_audit"]["tool_call_id"]][k]
                for k in ("tool", "run_id", "trace_id", "result_ref", "parameter_fingerprint", "status")) and
            r["tool_audit"]["run_id"] == outer["run_id"] and r["tool_audit"]["status"] in {"COMPLETED", "UNAVAILABLE", "NOT_AVAILABLE", "NO_EVIDENCE"}
            for r in invocations)
        if bound_reads and len(starts_by_id) == len(business_starts) == len(business_finishes):
            audit = checks["run_trace_association"] == "PASS" and bool(route) and policy_linked and all(
                all(starts_by_id[r["tool_call_id"]][k] == r[k] for k in ("tool", "run_id", "trace_id", "parameter_fingerprint"))
                for r in business_finishes)
    actual_output = outer["result"]
    zero_call_proof = (outer["status"] == "SUCCEEDED" and isinstance(actual_output, dict) and
        actual_output.get("tool_call_audit") == [] and not invocations and not starts and not finishes and
        not any(e["event_type"] in {"tool_started", "tool_finished"} for e in events) and
        checks["run_trace_association"] == "PASS" and len(route_rows) == 1 and
        usage["complete"] and usage["settled"] and read_usage["calls_by_category"].get("tool") == 0)
    if zero_call_proof:
        audit = True  # Proven zero reads, even if a model chose an unhelpful AGENT route.
    scope = technical["route_scope_rejection"]
    gates = scope["gates"]
    # Legacy projection reports cleanup as pending. Its responsible extractor
    # already binds the guarded candidate, error span, Run/Trace and known usage.
    # The generic formal Runner evaluates actual cleanup only after archiving.
    guard_rejected = (bool(gates) and set(gates) == {"terminal_failed", "diagnostic_bound", "response_received",
        "requested_capabilities_nonempty", "disallowed_matches_scope_difference", "allowed_scope_matches_case",
        "usage_recorded", "budget_known_settled", "zero_tool_or_retrieval_events", "cleanup_proven"} and
        all(value is True for key, value in gates.items() if key != "cleanup_proven") and not invocations)
    from .personal_dev_batch_m62 import _route_scope_rejection_from_archive
    local_scope = _route_scope_rejection_from_archive(archive)
    permitted = set(case.input['allowed_capabilities'])
    local_guard_rejected = bool(allow_known_classifier_rejection and local_scope and
        local_scope.get('stage') == 'RUNTIME_ROUTE_SCOPE' and local_scope.get('decision_source') == 'RULE' and
        local_scope.get('usage_status') == 'NOT_APPLICABLE_RULE_ROUTE' and
        local_scope.get('provider_response_received') is False and local_scope.get('usage') is None and
        local_scope.get('allowed_capabilities') == sorted(permitted) and
        set(local_scope['disallowed_capabilities']) == set(local_scope['requested_capabilities'])-permitted and
        local_scope['disallowed_capabilities'] and outer['status'] == 'FAILED' and
        usage['complete'] and usage['settled'] and usage['calls_by_category']['provider'] == 0 and
        read_usage['calls_by_category']['tool'] == 0 and not invocations and not starts and not finishes and
        not business_starts and not business_finishes and checks['run_trace_association'] == 'PASS')
    if local_guard_rejected:
        audit = True
        guard_rejected = True
    if guard_rejected:
        audit = True
    if archive["runtime_binding"]["approved"] != archive["runtime_binding"]["observed_at_end"]:
        audit = False
    # Each policy fetch is separately budgeted; business and policy are typed.
    # A POLICY_SEARCH_TOOL is published as both a tool event and a policy audit
    # using the same real call ID. Count it once, without faking either type.
    shared_ids = tool_call_ids.intersection(finishes)
    for call_id in shared_ids:
        event = next(row for row in business_finishes if row["tool_call_id"] == call_id)
        if any(event[key] != finishes[call_id][key] for key in ("run_id", "trace_id", "result_ref")):
            audit = False
    recorded_calls = len(tool_call_ids.union(finishes))
    audit = audit and len(tool_call_ids) == len(business_finishes)
    audit = audit and len(invocations) == recorded_calls and usage["calls_by_category"].get("tool", 0) == len(invocations)+separate_calls
    if measurement is not None:
        audit=audit and measurement["run_id"]==outer["run_id"] and all(r["status"]=="RETURNED" for r in measurement["calls"])
    owner_tools = {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
    permission = ("FAIL" if checks["tool_scope"] == "FAIL" or any(e["event_type"] == "action_pending" for e in events) else
        "UNKNOWN" if owner_tools.intersection(technical["observed_tools"]) and checks["owner_scope_evidence"] != "PASS" else "PASS")
    if permission == "UNKNOWN" and bound_reads:
        owner_reads = [r for r in invocations if r["tool_audit"]["tool"] in owner_tools]
        expected_owner = case.input["owner_map"][case.input["user_id"]]
        if owner_reads and all(r["tool_audit"].get("owner_scope") == "AUTHENTICATED_OWNER_FILTER" and
                r["arguments"].get("owner_id") == expected_owner for r in owner_reads):
            permission = "PASS"
    known_usage = usage["complete"] and usage["settled"] and not usage["unknown_tokens"] and not usage["unknown_cost"]
    diagnostic=route.get('provider_diagnostic') if route else None
    semantic_codes={'ORDER_IDENTIFIER_REQUIRED','DIRECT_REFUSE_CAPABILITY_MISMATCH','REFUSE_SAFETY_MISMATCH',
        'SINGLE_CAPABILITY_CONTRACT_INVALID','MULTI_CAPABILITY_CONTRACT_INVALID','AGENT_SUBSTRATEGY_INVALID','AGENT_CAPABILITY_SET_INVALID'}
    classified_rejection=bool(allow_known_classifier_rejection and diagnostic and route['decision_source']=='FALLBACK' and
        diagnostic['failure_phase']=='CLASSIFIER_SEMANTIC_CONTRACT' and diagnostic['error_category']=='CLASSIFIER_OUTPUT_INVALID' and
        diagnostic.get('route_error_code') in semantic_codes and diagnostic['provider_response_received'] is True and
        diagnostic['request_delivery']=='RESPONSE_RECEIVED' and diagnostic.get('response_summary',{}).get('finish_reason')=='stop' and
        diagnostic['response_summary'].get('usage_status')=='RECORDED' and known_usage and audit and permission=='PASS' and
        not invocations and not starts and not finishes and not business_starts and not business_finishes)
    provider = "PASS" if guard_rejected or classified_rejection else checks["provider_execution"]
    candidate = technical.get("candidate_assessment")
    known_rejection = bool(candidate and candidate.get("validator_version") and
        (candidate.get("validation_findings") or candidate.get("failure_classification") in {
            "POLICY_SEMANTIC_VALIDATION_REJECTED", "CITATION_OUTSIDE_CURRENT_EVIDENCE", "REQUIRED_ASPECT_CITATION_MISSING"}) and
        candidate.get("citation_scope") in {"PASS", "FAIL"} and audit and known_usage and permission == "PASS")
    # Completed specialist reads can still lack a user-answerable aggregation.
    # This is an application task failure, not an unknown execution stage, only
    # when the durable same-Run failure and all execution/accounting gates agree.
    run_rows = outer["observations"]["runs"]
    answer_aggregation_failed = bool(
        outer["failure_code"] == "AGENT_RESULT_NOT_USER_ANSWERABLE"
        if "failure_code" in outer else False)
    answer_aggregation_failed = bool(answer_aggregation_failed and bound_reads and
        audit and known_usage and permission == "PASS" and len(run_rows) == 1 and
        run_rows[0]["run_id"] == outer["run_id"] and run_rows[0]["status"] == "FAILED" and
        run_rows[0]["error_code"] == "AGENT_RESULT_NOT_USER_ANSWERABLE" and
        json.loads(run_rows[0]["error_detail_json"])["supervisor_status"] == "COMPLETED")
    blocking_items = []
    if provider not in {"PASS", "NOT_APPLICABLE"}:
        blocking_items.append("PROVIDER_ROUTE_CONTRACT_UNVERIFIED")
    if outer["status"] == "FAILED" and not guard_rejected and not known_rejection and not answer_aggregation_failed:
        blocking_items.append("FAILED_STAGE_NOT_CLASSIFIED")
    expected = payload["expected_outcome"]
    from .formal_reference_rules import route_reference,resource_coverage,result_row_coverage,V3
    route_score = route_reference(case,route,rejected=guard_rejected or classified_rejection)
    observed = set(technical["observed_tools"])
    if finishes: observed.add("search_knowledge")
    groups = expected["task"]["required_tool_groups"]
    required_tools = "PASS" if all(any(set(option) <= observed for option in group) for group in groups) else "FAIL"
    if expected["task"]["zero_calls"] and (invocations or recorded_calls): required_tools = "FAIL"
    resources=resource_coverage(case,invocations)
    row_coverage=result_row_coverage(case,invocations)
    if resources=='FAIL':required_tools='FAIL'
    if row_coverage['status']=='FAIL':required_tools='FAIL'
    answer = technical["assistant_answer"]
    delivered = "PASS" if isinstance(answer, str) and answer.strip() else "FAIL"
    # Code proves necessary conditions and missing delivery. Answer completeness
    # and applicability require a separate, bound real output review.
    task = "FAIL" if "FAIL" in {required_tools, delivered} else "UNKNOWN"
    plan_rejected = any(e["event_type"] == "error" and json.loads(e["payload_json"]).get("stage") == "AGENT_PLAN_REJECTED" for e in events)
    if plan_rejected: task = "FAIL"
    action_state="NOT_APPLICABLE"
    effect_check="NOT_APPLICABLE"
    if action is not None:
        from .formal_actions import assess_interaction
        actual_action=assess_interaction(case,action)
        action_state=actual_action["state_check"];effect_check=actual_action["business_effect_check"]
        if actual_action["permission"]!="PASS":permission=actual_action["permission"]
        audit=audit and actual_action["audit"]=="PASS" and action["source_run_id"]==outer["run_id"]
        if actual_action["task"]=="FAIL":task="FAIL"
        if any(call["status_code"]>=500 for call in action["calls"]):blocking_items.append("OWNER_ACTION_API_PUBLIC_FAILURE")
    failures = []
    if answer_aggregation_failed: failures.append("COMPLETED_READS_WITHOUT_USER_ANSWER_APPLICATION_FAILURE")
    if plan_rejected: failures.append("MODEL_AGENT_PLAN_OR_ANSWER_REJECTED_CASE_FAILURE")
    if delivered == "FAIL": failures.append("USER_OUTPUT_NOT_DELIVERED")
    if required_tools == "FAIL": failures.append("REQUIRED_TASK_READ_NOT_COMPLETED")
    if guard_rejected: failures.append("LOCAL_RULE_CAPABILITY_CHOICE_BLOCKED_ZERO_EXECUTION" if local_guard_rejected else "MODEL_CAPABILITY_CHOICE_BLOCKED_ZERO_EXECUTION")
    if classified_rejection:failures.append('CLASSIFIER_SEMANTIC_CONTRACT_REJECTED_CASE_FAILURE;HISTORICAL_UNIQUE_MODEL_VS_LOCAL_CAUSE_NOT_ASSUMED')
    if known_rejection: failures.append("CANDIDATE_REJECTED_SEMANTICS_REQUIRE_SEPARATE_REVIEW")
    if not known_usage: failures.append("ACCOUNTING_UNKNOWN_OR_UNSETTLED")
    if not audit: failures.append("EXECUTION_STAGE_AUDIT_OR_BINDING_UNVERIFIED")
    if resources=='UNKNOWN':blocking_items.append('REQUIRED_RESOURCE_AUDIT_MISSING')
    if row_coverage['status']=='UNKNOWN':blocking_items.append('REQUIRED_RESULT_ROW_AUDIT_MISSING')
    if "OWNER_ACTION_API_PUBLIC_FAILURE" in blocking_items:failures.append("APPLICATION_OWNER_API_PUBLIC_FAILURE")
    return {"schema_version": "formal-code-grading-v3" if V3 in case.rubric_refs else "formal-code-grading-v1", "case_id": case.case_id, "run_id": outer["run_id"],
        "route": route, "delivered_answer": answer, "candidate": candidate,
        "checks": {"route_reference": route_score, "required_tools": required_tools,"resource_coverage":resources,"result_row_coverage":row_coverage['status'],
            "delivery": delivered, "simulated_owner_action":action_state,"business_effect_count":effect_check,"atomic_fact_support": "UNKNOWN", "answer_completeness": "UNKNOWN",
            "required_evidence_semantics": "UNKNOWN" if expected["required_evidence"] else "NOT_APPLICABLE"},
        "task_outcome": task, "result_row_assessments":row_coverage,'pending_review': {"required_evidence": expected["required_evidence"],
            "atomic_facts": expected["atomic_facts"], "task_requirements": expected["task"]["requirements"]},
        "technical_projection": {"schema_version": technical["schema_version"], "program_checks": checks,
            "tool_audit_reason": technical["tool_audit_reason"], "route_scope_rejection": scope,
            "cleanup_scope": "PENDING_SEPARATE_GENERIC_RUNNER_RECEIPT_CLEANUP"},
        "guarded_capability_rejection": guard_rejected, "known_classifier_semantic_rejection":classified_rejection, "read_invocation_count": len(invocations), "failure_categories": failures,
        "blocking_items": blocking_items,
        "integrity": {"permission": permission, "audit": "PASS" if audit else "UNKNOWN",
            "usage": "PASS" if known_usage else "UNKNOWN",
            "terminal": checks["run_terminal"]}}


def grade_result(trial_id: str, result: dict) -> GradeResult:
    version='3' if result['schema_version']=='formal-code-grading-v3' else '1'
    rubric='formal-reference-rubric-v3' if version=='3' else 'formal-readonly-rubric-v1'
    refs = tuple("score." + uuid.uuid4().hex for _ in result["checks"])
    outcome = EvalOutcome("outcome." + uuid.uuid4().hex, trial_id,
        "PASS" if result["integrity"]["terminal"] == "PASS" else "FAIL", "UNKNOWN", result["task_outcome"],
        result["integrity"]["permission"], "PENDING_HUMAN_REVIEW", rubric, refs,
        datetime.now(timezone.utc).isoformat())
    scores = tuple({"score_id": ref, "trial_id": trial_id, "grader_id": "formal-code-projection",
        "grader_version": version, "dimension": key, "verdict": value, "reason_code": key.upper(),
        "evidence_refs": [result["run_id"]], "score_source": "DETERMINISTIC_CODE",
        "rubric_version": outcome.rubric_version} for ref, (key, value) in zip(refs, result["checks"].items()))
    return GradeResult(outcome, scores, True)


def output_review_form(case, trial: dict, raw: dict) -> dict:
    reference = formal_case_payload(case)["expected_outcome"]
    candidate = raw["code_assessment"]["candidate"]
    candidate_view = ({"text": candidate.get("redacted_text"), "citation_ids": candidate.get("citation_ids"), "is_user_output": False}
        if candidate is not None else None)
    output = raw["application_archive"]["result"]["result"] or {}
    action=raw["application_archive"].get("owner_interaction")
    action_view=({key:action[key] for key in ("source_run_id","decision","calls","policy_reads","before_business_counts",
        "after_proposal_business_counts","after_business_counts","snapshot","run_events") if key in action} if action is not None else None)
    # The blind view retains source context and scoring targets, but not Gold
    # values, old source labels, program verdicts, or validator rejection codes.
    rubric_view = {"task": {"requirements": reference["task"]["requirements"]},
        "evidence_targets": [{k: row[k] for k in ("requirement", "condition")} for row in reference["required_evidence"]],
        "fact_targets": [{k: row[k] for k in ("fact_id", "subject", "predicate", "condition")} for row in reference["atomic_facts"]]}
    return {"schema_version": "formal-output-review-v1", "case_id": case.case_id,
        "trial_id": trial["trial_id"], "run_ids": trial["run_ids"], "case_sha256": formal_case_digest(case),
        "raw_sha256": sha256_json(raw),
        "question": case.input["user_query"], "delivered_answer": raw["code_assessment"]["delivered_answer"],
        "scripted_owner_api_output":action_view,
        "candidate": candidate_view, "context": formal_case_payload(case)["input"]["fixture"],
        "actual_source_records": {k: output[k] for k in ("business_results", "tool_references", "evidence", "citations", "policy_retrieval", "retrieval", "tool_call_audit", "handoffs") if k in output},
        "actual_repository_results": [r for r in raw["application_archive"]["read_invocations"] if r["category"] == "business_read"],
        "rubric": rubric_view,
        "decisions": {"required_evidence": {r["requirement"]: None for r in case.expected_outcome["required_evidence"]},
            "atomic_fact_support": {r["fact_id"]: None for r in case.expected_outcome["atomic_facts"]},
            "answer_completeness": {r: None for r in case.expected_outcome["task"]["requirements"]}, "task_outcome": None},
        "rationale": None}


def receive_output_review(case, trial, raw, store, submission, *, phase, trusted_reviewers):
    """Import real per-Run review; neither Code PASS nor a candidate is delivery."""
    from .formal_assets import authenticated_subject, utc_now
    actor = authenticated_subject()
    with store.transaction(immediate=False) as db:
        experiment=json.loads(db.execute('SELECT payload_json FROM experiments WHERE experiment_id=?',(trial['experiment_id'],)).fetchone()[0])['config_snapshot']
    personal=experiment['environment'].get('mode')=='PERSONAL_REAL_EVAL'
    if personal and phase!='day1':raise ValueError('personal batch blind review cancelled by user')
    if actor not in trusted_reviewers:
        raise PermissionError("output reviewer is not in the approved reviewer role")
    if phase not in {"day1", "blind"}:
        raise ValueError("output review phase invalid")
    form = output_review_form(case, trial, raw)
    if set(submission) != set(form) or any(submission[k] != form[k] for k in set(form)-{"decisions", "rationale"}):
        raise ValueError("output review Run/answer/source/reference binding mismatch")
    labels = submission["decisions"]
    if set(labels) != set(form["decisions"]) or not submission["rationale"]:
        raise ValueError("output decisions/rationale missing")
    for dimension in ("required_evidence", "atomic_fact_support", "answer_completeness"):
        if set(labels[dimension]) != set(form["decisions"][dimension]) or any(
                label not in {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"} for label in labels[dimension].values()):
            raise ValueError("missing/extra/invalid per-fact output labels")
    if labels["task_outcome"] not in {"PASS", "PARTIAL", "FAIL", "UNSCORABLE"}:
        raise ValueError("task outcome must retain PARTIAL and UNSCORABLE")
    if raw["code_assessment"]["checks"]["delivery"] == "FAIL" and labels["task_outcome"] != "FAIL":
        raise ValueError("undelivered task remains FAIL; candidate content must be reviewed separately")
    events = store.trial_events(trial["trial_id"])
    key = "OUTPUT_REVIEW_" + phase.upper()
    if any(e["event_type"] == key for e in events):
        raise ValueError("output review phase already received; append explicit adjudication instead")
    now = utc_now()
    if phase == "blind":
        first = [e for e in events if e["event_type"] == "OUTPUT_REVIEW_DAY1"]
        if len(first) != 1 or now.astimezone().date() <= datetime.fromisoformat(first[0]["created_at_utc"]).astimezone().date():
            raise ValueError("output blind review requires actual subsequent date")
    payload = {"reviewer": actor, "method": "USER_REVIEWED_PERSONAL_OUTPUT_NO_BLIND_V1" if personal else "AUTHENTICATED_INDEPENDENT_OUTPUT_REVIEW", "submission": submission}
    store.add_trial_event(uuid.uuid4().hex, trial["trial_id"], key, payload, now.isoformat())
    return {"status": "OUTPUT_REVIEW_RECEIVED_NOT_AUTOMATIC_ADJUDICATION", "phase": phase, "trial_id": trial["trial_id"]}


def output_review_status(store, trial_id):
    events = store.trial_events(trial_id)
    records = {e["event_type"]: e["payload"] for e in events if e["event_type"].startswith("OUTPUT_REVIEW_")}
    trial=store.trial(trial_id)
    with store.transaction(immediate=False) as db:
        experiment=json.loads(db.execute('SELECT payload_json FROM experiments WHERE experiment_id=?',(trial['experiment_id'],)).fetchone()[0])['config_snapshot']
    if experiment['environment'].get('mode')=='PERSONAL_REAL_EVAL':
        first=records.get('OUTPUT_REVIEW_DAY1')
        return {'status':'USER_REVIEWED_PERSONAL_NO_BLIND' if first else 'PENDING_USER_OUTPUT_REVIEW_NO_BLIND_REQUIRED',
            'records':records,'final_decisions':first['submission']['decisions'] if first else None}
    if not {"OUTPUT_REVIEW_DAY1", "OUTPUT_REVIEW_BLIND"} <= set(records):
        return {"status": "PENDING_REAL_REVIEW", "records": records, "final_decisions": None}
    a, b = (records[k]["submission"]["decisions"] for k in ("OUTPUT_REVIEW_DAY1", "OUTPUT_REVIEW_BLIND"))
    differences = {k: {"day1": a[k], "blind": b[k]} for k in a if a[k] != b[k]}
    adjudications = [e["payload"] for e in events if e["event_type"] == "OUTPUT_ADJUDICATION"]
    if adjudications:
        decision = adjudications[-1]
        if decision["comparison_sha256"] != sha256_json(differences):
            raise ValueError("output adjudication comparison binding drift")
        return {"status": "ADJUDICATED", "records": records, "differences": differences,
            "final_decisions": {**a, **decision["decisions"]}}
    return {"status": "DISAGREEMENT_PENDING_ADJUDICATION" if differences else "REVIEWED_AGREED",
        "records": records, "differences": differences, "final_decisions": None if differences else a}


def adjudicate_outputs(case, trial, raw, store, submission, *, trusted_reviewers):
    from .formal_assets import authenticated_subject, utc_now
    actor = authenticated_subject()
    if actor not in trusted_reviewers: raise PermissionError("output adjudicator not approved")
    status = output_review_status(store, trial["trial_id"])
    if status["status"] != "DISAGREEMENT_PENDING_ADJUDICATION":
        raise ValueError("no unresolved output comparison")
    if submission["comparison_sha256"] != sha256_json(status["differences"]) or not submission["rationale"]:
        raise ValueError("adjudication comparison/rationale missing")
    if set(submission["decisions"]) != set(status["differences"]):
        raise ValueError("adjudication must cover exactly disputed dimensions")
    # Validate each resolved label using the same output contract; do not write
    # another review phase or overwrite the original votes.
    base = status["records"]["OUTPUT_REVIEW_DAY1"]["submission"]["decisions"]
    final = {**base, **submission["decisions"]}
    blank = output_review_form(case, trial, raw)["decisions"]
    for key in ("required_evidence", "atomic_fact_support", "answer_completeness"):
        if set(final[key]) != set(blank[key]) or any(v not in {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"} for v in final[key].values()):
            raise ValueError("adjudicated fact/scope labels invalid")
    if final["task_outcome"] not in {"PASS", "PARTIAL", "FAIL", "UNSCORABLE"} or (
            raw["code_assessment"]["checks"]["delivery"] == "FAIL" and final["task_outcome"] != "FAIL"):
        raise ValueError("adjudicated task/delivery conflict")
    store.add_trial_event(uuid.uuid4().hex, trial["trial_id"], "OUTPUT_ADJUDICATION",
        {**submission, "actor": actor}, utc_now().isoformat())
    return {"status": "OUTPUT_ADJUDICATED", "trial_id": trial["trial_id"]}


def recompute(cases, store, experiment_id: str, *, scopes=None, repository_root=None) -> dict:
    from .budget import BudgetLedger, BudgetLimits
    from .contracts import sha256_json
    by_id = {c.case_id: c for c in cases}
    with store.transaction(immediate=False) as db:
        config=json.loads(db.execute('SELECT payload_json FROM experiments WHERE experiment_id=?',(experiment_id,)).fetchone()[0])['config_snapshot']
    ledger = BudgetLedger(store, experiment_id, BudgetLimits(**store.budget_limits(experiment_id)))
    from .grants import formal_runtime_digest
    scoring_runtime = formal_runtime_digest()
    rows = []
    for trial in store.trials(experiment_id):
        case = by_id[trial["case_id"]]
        raw = store.raw_result(trial["trial_id"])
        if raw["scoring_runtime_sha256"] != scoring_runtime:
            raise ValueError("scoring runtime changed; recompute with the frozen source/environment snapshot")
        if repository_root is not None:
            from .formal_assets import confined, raw_hash
            archive_path = confined(repository_root, raw["receipt"]["archive_path"])
            if raw_hash(archive_path) != raw["archive_sha256"]:
                raise ValueError("raw application archive bytes changed")
        from .grants import formal_case_digest
        if raw["case_sha256"] != formal_case_digest(case):
            raise ValueError("scoring case/config binding drift")
        assessment = evaluate(case, raw["application_archive"], ledger.trial_snapshot(trial["trial_id"]),
            allow_known_classifier_rejection=config['environment'].get('classifier_semantic_failure_policy')=='CASE_FAIL_CONTINUE_WHEN_FULLY_AUDITED')
        if assessment != raw["code_assessment"]:
            raise ValueError("persisted code assessment does not reproduce from raw evidence")
        review = output_review_status(store, trial["trial_id"])
        conflict = bool(review["final_decisions"] and assessment["task_outcome"] == "FAIL" and review["final_decisions"]["task_outcome"] != "FAIL")
        rows.append({"trial_id": trial["trial_id"], "case_id": case.case_id,
            "assessment": assessment, "raw_sha256": sha256_json(raw), "human_review": review,
            "code_human_task_disagreement": conflict})
    metrics = formal_metrics(cases, rows, store, scopes=scopes)
    with store.transaction(immediate=False) as db:
        config=json.loads(db.execute('SELECT payload_json FROM experiments WHERE experiment_id=?',(experiment_id,)).fetchone()[0])['config_snapshot']
    if config['environment'].get('mode')=='PERSONAL_REAL_EVAL':
        metrics['scope']='ONE_PERSONAL_LOCKED_EXPERIMENT;DISCLOSED_SYNTHETIC_NOT_UNSEEN_FROZEN'
        metrics['retrieval']['reference_type']='USER_REVIEWED_REQUIRED_EVIDENCE_SYNTHETIC;NOT_INDEPENDENT_GOLD'
        metrics['reference_method']='USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1'
    return {"schema_version": "formal-recompute-v1", "experiment_id": experiment_id, "rows": rows,
        "metrics": metrics, "scoring_runtime_sha256": scoring_runtime,
        "budget": ledger.snapshot(), "binding_sha256": sha256_json({"rows": rows, "metrics": metrics, "scoring_runtime": scoring_runtime})}


def formal_metrics(cases, rows, store, *, scopes=None):
    from collections import Counter
    from .aggregate import wilson_interval, cluster_bootstrap_interval
    by_id = {c.case_id: c for c in cases}
    def ratio(p, n, excluded=0):
        return {"n": p, "N": n, "value": p/n if n else None,
            "excluded_or_unscorable": excluded, "wilson_95": wilson_interval(p, n)}
    scopes = scopes or {k: list(by_id) for k in ("route", "retrieval", "outcome")}
    route_pairs, route_observations, task_labels, fact_labels, retrieval = [], [], [], [], []
    retrieval_missing = []
    for row in rows:
        case = by_id[row["case_id"]]
        observed = row["assessment"]["route"]
        if case.case_id in scopes["route"]:
            prediction = observed["strategy"] if observed is not None else "INVALID_SCOPE" if row["assessment"]["guarded_capability_rejection"] else None
            if prediction is not None:
                expected_strategy = case.expected_outcome["route"]["strategy"]
                route_pairs.append((expected_strategy, prediction))
                route_observations.append({"group_id": case.group_id, "expected": expected_strategy, "actual": prediction})
        decision = row["human_review"]["final_decisions"]
        if case.case_id not in scopes["outcome"]:
            pass
        elif row["code_human_task_disagreement"]:
            task_labels.append("PENDING_ADJUDICATION")
        elif decision is not None:
            task_labels.append(decision["task_outcome"])
            fact_labels.extend(decision["atomic_fact_support"].values())
        elif row["assessment"]["task_outcome"] == "FAIL":
            # Known failure stays in the attempted task denominator even while
            # candidate semantics or human output review remain unscored.
            task_labels.append("CODE_FAIL_PENDING_REVIEW")
        else:
            task_labels.append("PENDING_REVIEW")
        if case.case_id not in scopes["retrieval"]: continue
        required = {s for item in case.expected_outcome["required_evidence"] for s in item["source_ids"]}
        policies = {p["evidence_id"] for p in case.input["fixture"]["policies"]}
        if not required or not required <= policies:
            retrieval_missing.append({"case_id": case.case_id, "reason": "NO_LOCKED_QUERY_REQUIRED_POLICY_SET"})
            continue
        raw = store.raw_result(row["trial_id"])
        module=raw["application_archive"].get("retrieval_evaluation")
        if module is not None:
            if module["mode"]!="FORMAL_FULL_QUERY_BM25" or module["run_id"]!=row["assessment"]["run_id"]:raise ValueError("retrieval evaluation scope/Run mismatch")
            records=module["calls"]
            if module["status"]!="RECORDED" or len(records)!=1 or records[0]["status"]!="RETURNED":
                retrieval_missing.append({"case_id":case.case_id,"reason":"FORMAL_RETRIEVAL_MODULE_NOT_COMPLETED"});continue
        else:records = [r for r in raw["application_archive"]["read_invocations"] if r["category"] == "policy_retrieval" and r["status"] == "RETURNED"]
        if not records:
            if row["assessment"]["integrity"]["audit"] == "PASS" and not any(r["category"] == "policy_retrieval" for r in raw["application_archive"]["read_invocations"]):
                retrieval.append({"case_id":case.case_id,"query":case.input["user_query"],"required_source_ids":sorted(required),
                    "ranked_ids":[],"required_hits":0,"required_total":len(required),"recall_at_5":0.0,"recall_at_3":0.0,
                    "precision_at_1":0,"precision_at_5":0.0,"reciprocal_rank_at_5":0.0,
                    "execution_status":"CONFIRMED_NO_RETRIEVAL_NOT_A_MISSING_LOG"})
            else:
                retrieval_missing.append({"case_id": case.case_id, "reason": "RETRIEVAL_AUDIT_OR_COMPLETION_UNVERIFIED"})
            continue
        # Multiple retrievals need individually labelled queries or a separately
        # recorded global reranking. Do not invent a combined historical rank.
        if len(records) != 1:
            retrieval_missing.append({"case_id": case.case_id, "reason": "MULTIHOP_QUERY_LEVEL_QRELS_REQUIRED"})
            continue
        result = records[0]["retrieval_result"]
        ranked = result["evidence"]
        if any(r["rank"] != i for i, r in enumerate(ranked, 1)) or len({r["chunk_id"] for r in ranked}) != len(ranked):
            raise ValueError("recorded retrieval ranking invalid")
        ids = [r["chunk_id"] for r in ranked[:5]]
        hits = len(required.intersection(ids))
        retrieval.append({"case_id": case.case_id, "query": records[0]["query"], "required_source_ids": sorted(required),
            "measurement_object":"FORMAL_RETRIEVER_MODULE_FULL_QUERY" if module is not None else "APPLICATION_SINGLE_RETRIEVAL_QUERY",
            "ranked_ids": ids, "required_hits": hits, "required_total": len(required), "recall_at_5": hits/len(required),
            "precision_at_1": int(bool(ids) and ids[0] in required), "recall_at_3": len(required.intersection(ids[:3]))/len(required),
            "precision_at_5": hits/5, "reciprocal_rank_at_5": next((1/i for i, v in enumerate(ids, 1) if v in required), 0),
            "knowledge_version": result["knowledge_version"], "index_fingerprint": result["index_fingerprint"], "config_hash": result["config_hash"]})
    route_labels = sorted({pair[0] for pair in route_pairs})
    predicted_labels = sorted(set(route_labels) | {pair[1] for pair in route_pairs})
    f1s, classes = [], {}
    for label in route_labels:
        tp = sum(a == b == label for a, b in route_pairs)
        fp = sum(a != label and b == label for a, b in route_pairs)
        fn = sum(a == label and b != label for a, b in route_pairs)
        f1s.append(2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0)
        classes[label] = {"tp": tp, "fp": fp, "fn": fn, "precision": tp/(tp+fp) if tp+fp else None,
            "recall": tp/(tp+fn) if tp+fn else None, "f1": f1s[-1]}
    macro_f1 = sum(f1s)/len(f1s) if f1s else None
    reviewed_facts = [v for v in fact_labels if v in {"PASS", "FAIL"}]
    pending_tasks = sum(v in {"PENDING_REVIEW", "PENDING_ADJUDICATION", "UNSCORABLE"} for v in task_labels)
    fact_total = sum(len(c.expected_outcome["atomic_facts"]) for c in cases if c.case_id in scopes["outcome"])
    fact_metric = ratio(reviewed_facts.count("PASS"), len(reviewed_facts), fact_total-len(reviewed_facts))
    recall = sum(r["recall_at_5"] for r in retrieval)/len(retrieval) if retrieval else None
    route_ci = (cluster_bootstrap_interval(route_observations, metric="route_macro_f1", seed=1)
        if set(route_labels) == {"DIRECT","SINGLE","MULTI","AGENT","REFUSE"} and all(b != "INVALID_SCOPE" for _, b in route_pairs)
        else {"status":"NOT_COMPUTABLE_CLASS_COVERAGE_OR_REJECTED_PREDICTIONS","lower":None,"upper":None})
    retrieval_ci = {name: cluster_bootstrap_interval([{"group_id": by_id[r["case_id"]].group_id, "value": r[name]} for r in retrieval],
        metric="mean", seed=1) for name in ("recall_at_3", "recall_at_5", "reciprocal_rank_at_5")}
    return {"scope": "ONE_LOCKED_EXPERIMENT_ONLY_NO_DEV_MIXING",
        "accepted_route_reference_accuracy": ratio(sum(r['assessment']['checks']['route_reference']=='PASS' for r in rows if r['case_id'] in scopes['route']),
            sum(r['case_id'] in scopes['route'] for r in rows)),
        "route_metric_contract": "Canonical top-level Macro F1/accuracy retained;explicit legal path equivalents are reported separately,not relabeled to inflate canonical F1",
        "route_accuracy": ratio(sum(a == b for a, b in route_pairs), len(route_pairs), len(rows)-len(route_pairs)),
        "router_macro_f1": {"value": macro_f1, "N": len(route_pairs), "classes": classes,
            "confusion_matrix": {a: {b: sum(x == a and y == b for x, y in route_pairs) for b in predicted_labels} for a in route_labels},
            "confidence_interval": route_ci},
        "delivery": ratio(sum(r["assessment"]["checks"]["delivery"] == "PASS" for r in rows), len(rows)),
        "reporting_cohorts": scopes, "task_outcome_counts": dict(Counter("FAIL" if v == "CODE_FAIL_PENDING_REVIEW" else v for v in task_labels)),
        "code_determined_fail_without_human_relabelling": task_labels.count("CODE_FAIL_PENDING_REVIEW"), "attempted_task_denominator": len(task_labels),
        "task_success_strict_PASS": {**ratio(task_labels.count("PASS"), len(task_labels), pending_tasks),
            "value": None if pending_tasks else task_labels.count("PASS")/len(task_labels) if task_labels else None,
            "wilson_95": {"status": "PENDING_REAL_REVIEW", "lower": None, "upper": None} if pending_tasks else wilson_interval(task_labels.count("PASS"), len(task_labels)),
            "status": "PENDING_REAL_REVIEW_OR_ADJUDICATION" if pending_tasks else "TASK_DECIDABLE_CODE_AND_OR_REAL_REVIEW"},
        "key_fact_correctness": fact_metric,
        "retrieval": {"queries": retrieval, "excluded": retrieval_missing, "N": len(retrieval),
            "scope":"PLANNED_QUERY_END_TO_END_REQUIRED_EVIDENCE;proven no-invocation contributes zero,not a fabricated ranking",
            "actual_retriever_query_count":sum(r.get("execution_status") != "CONFIRMED_NO_RETRIEVAL_NOT_A_MISSING_LOG" for r in retrieval),
            "measurement_objects":dict(Counter(r.get("measurement_object","APPLICATION_CONFIRMED_NO_RETRIEVAL") for r in retrieval)),
            "precision_at_1": ratio(sum(r["precision_at_1"] for r in retrieval), len(retrieval), len(retrieval_missing)),
            "mean_required_recall_at_3": sum(r["recall_at_3"] for r in retrieval)/len(retrieval) if retrieval else None,
            "mean_required_recall_at_5": recall,
            "mean_required_precision_at_5": sum(r["precision_at_5"] for r in retrieval)/len(retrieval) if retrieval else None,
            "mrr_at_5": sum(r["reciprocal_rank_at_5"] for r in retrieval)/len(retrieval) if retrieval else None,
            "confidence_intervals": retrieval_ci, "reference_type": "LOCKED_INDEPENDENT_REQUIRED_EVIDENCE_NOT_DEV_CANDIDATE_RELEVANCE"},
        "route_sources": dict(Counter(r["assessment"]["route"]["decision_source"] if r["assessment"]["route"] else
            "LLM_REJECTED_BY_SCOPE" if r["assessment"]["guarded_capability_rejection"] else "NOT_RECORDED" for r in rows)),
        "failure_categories": dict(Counter(value for row in rows for value in row["assessment"]["failure_categories"])),
        "quality_targets": {"router_macro_f1_at_least_0_85": macro_f1 >= .85 if macro_f1 is not None and set(route_labels) == {"DIRECT","SINGLE","MULTI","AGENT","REFUSE"} else None,
            "required_recall_at_5_at_least_0_85": recall >= .85 if recall is not None and not retrieval_missing else None,
            "human_key_fact_accuracy_at_least_0_90": fact_metric["value"] >= .90 if fact_metric["value"] is not None and fact_metric["excluded_or_unscorable"] == 0 else None},
        "quality_release": "REQUIRES_SCOPE_COVERAGE_HARD_GATE_AND_REAL_REVIEW_CHECK; execution closure alone is insufficient"}
