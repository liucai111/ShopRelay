"""Add the R10 four-run segment to the existing M6.2 Dev review and metrics.

The R17 package remains the authoritative source for the earlier 28 Runs.
This adapter binds and appends R10 evidence; it never edits the source package,
archives, prior scores, authorizations, or execution rules.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.recompute_m6_2_r9_policy018_closeout import build_review_metrics, project_program_checks

ROOT = Path(__file__).resolve().parents[1]
R9 = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports")
R10 = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030")
DATA = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
DATA_SHA = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
CASES = ["D1-PRODUCT-005", "D1-AFTERSALES-019", "D1-AFTERSALES-020", "D1-CROSSAGENT-032"]
MANIFEST = R10 / "batch-manifest-r10-first-runs-r5.json"
RULES = R10 / "locked-rules-r10-first-runs-r2.json"
AUTH = R10 / "authorization-r10r2-5C7A2E94F1038B6D.json"
STATE = R10 / "execution-state-r10r2-5C7A2E94F1038B6D.json"
JOURNAL = R10 / "execution-journal-r10r2-5C7A2E94F1038B6D.jsonl"
TASK_LEDGER = R10 / "task-cumulative-ledger-r3-four-first-runs.json"
CASE_RESULTS = R10 / "reports/case-results-r10r2.json"
CLOSEOUT = R10 / "reports/closeout-r10r2.json"
R17_PACKAGE = R9 / "dev-score-review-package-r9-final-r17.json"
R17_LEDGER = R9 / "40-case-coverage-ledger-r9-final-r17.json"


def sha(path: Path) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def read(path: Path) -> Any:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _label(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("suggestion") or value.get("status") or value.get("decision")
    if not isinstance(value, str):
        return None
    normalized = value.upper()
    if normalized in {"PASS", "PARTIAL", "FAIL"}:
        return normalized
    if normalized.startswith(("FAIL_DELIVERY", "TASK_FAIL", "FAIL;")):
        return "FAIL"
    if normalized.startswith(("REVIEW_EXISTING_PROVISIONAL_PARTIAL", "PARTIAL;")):
        return "PARTIAL"
    return None


def _program_checks(item: dict) -> dict:
    return project_program_checks(item)["checks"]


def normalize_existing_28(review: dict) -> None:
    items = list(review["historical_runs"]) + list(review["r9s4_runs"]) + [review["policy018_recovery_run"]]
    if len(items) != 28 or len({x.get("run_id") for x in items}) != 28:
        raise ValueError("R17_SOURCE_MUST_CONTAIN_28_UNIQUE_RUNS")
    for item in items:
        answer = item.get("delivered_answer")
        has_delivery = isinstance(answer, str) and bool(answer.strip())
        checks = _program_checks(item)
        expected_tools = checks.get("expected_business_tool_execution")
        existing = item.get("ai_recommendation") or item.get("existing_ai_preview")
        semantic = _label(existing)
        if item.get("case_id") == "D1-CROSSAGENT-028" and expected_tools == "FAIL":
            task = "FAIL"
            task_reason = "Required get_order did not execute; semantic PARTIAL cannot override the required-tool failure."
        elif not has_delivery and item.get("run_status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}:
            task = "FAIL"
            task_reason = "A real terminal evaluation has no delivered user answer. The delivery/task failure remains scoreable even when candidate semantics are unscorable."
        else:
            task = semantic or "UNKNOWN"
            task_reason = ("Mapped from the exact-run bound provisional recommendation; no new human decision is created."
                if semantic else "No compatible exact-run provisional task recommendation is present.")
        if item.get("case_id") == "D1-POLICY-013" and semantic is None:
            task = "PARTIAL"
            task_reason = "Uses only this exact R4 Run's recorded provisional PARTIAL; later bindings are not copied."
        candidate = item.get("rejected_candidate_audit")
        if has_delivery:
            candidate_status = semantic or "UNSCORED_PENDING_AI_REVIEW"
        elif candidate:
            text = candidate.get("candidate_answer_redacted")
            candidate_status = ("UNSCORABLE_REDACTED_OR_INCOMPLETE" if not text or candidate.get("candidate_answer_truncated")
                else "SEPARATE_CANDIDATE_REVIEW_REQUIRED")
        else:
            candidate_status = "UNSCORABLE_NO_SAVED_CANDIDATE"
        delivery = "PASS" if has_delivery else (
            "FAIL" if item.get("run_status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}
            else "UNKNOWN")
        item["derived_score_r18"] = {
            "safety_scope": checks.get("tool_scope", "UNKNOWN"),
            "content_and_evidence_support": candidate_status,
            "required_tool_completion": expected_tools or "UNKNOWN",
            "user_delivery": delivery,
            "task_outcome_recommendation": task,
            "task_outcome_reason": task_reason,
            "candidate_semantics_separate_from_task_outcome": True,
            "human_decision": item.get("reviewer_decision"),
            "human_confirmation_status": item.get("human_confirmation_status", "PENDING_EXACT_RUN_REVIEW"),
        }
        item["task_outcome_recommendation"] = task
        item["task_outcome_rationale_r18"] = task_reason
        item["candidate_content_status_r18"] = candidate_status
        item["user_delivery_status_r18"] = delivery


def build_r10_items(manifest: dict, rules: dict, case_results: dict) -> list[dict]:
    records = []
    case_rules = rules["case_rules"]
    for row in case_results["cases"]:
        case_id = row["case_id"]
        archive_path = Path(row["archive_path"])
        grade_path = Path(row["grader_path"])
        if sha(archive_path) != row["archive_sha256"] or sha(grade_path) != row["grader_sha256"]:
            raise ValueError(f"R10_RUN_BINDING_HASH_MISMATCH:{case_id}")
        archive, grade = read(archive_path), read(grade_path)
        result = archive["result"]
        payload = result.get("result") or {}
        observations = result.get("observations") or {}
        run_events = [event for event in observations.get("run_events", [])
            if event.get("run_id") == row["run_id"]]
        agent_events = [event for event in run_events if event.get("event_type") == "agent_started"]
        started_agents = []
        for event in agent_events:
            try:
                event_payload = json.loads(event.get("payload_json") or "{}")
            except json.JSONDecodeError:
                event_payload = {}
            if event_payload.get("agent"):
                started_agents.append(event_payload["agent"])
        checks = grade.get("program_checks") or {}
        rule = case_rules[case_id]
        answer = payload.get("answer")
        records.append({
            "case_id": case_id,
            "run_id": row["run_id"],
            "run_status": row["run_status"],
            "binding": {"batch_id": manifest["batch_id"],
                "implementation_digest": manifest["bindings"]["implementation_digest"],
                "manifest_sha256": sha(MANIFEST), "rules_sha256": sha(RULES),
                "dataset_sha256": manifest["dataset"]["sha256"],
                "database_identity": manifest["bindings"]["database_identity"]},
            "archive_path": row["archive_path"], "archive_sha256": row["archive_sha256"],
            "grader_path": row["grader_path"], "grader_sha256": row["grader_sha256"],
            "question": archive["user_query"], "delivered_answer": answer,
            "answer_delivery_status": "PASS" if isinstance(answer, str) and answer.strip() else "FAIL",
            "route": payload.get("route"), "route_source": (payload.get("route") or {}).get("decision_source"),
            "model_participation": {"provider_requests": row["provider_requests"], "tokens": row["tokens"],
                "decision_source": (payload.get("route") or {}).get("decision_source")},
            "actual_tool_evidence": payload.get("tool_references", []),
            "tool_execution_status": payload.get("tool_execution_status", []),
            "same_run_policy_sources": payload.get("citations", []),
            "actual_agent_started_events": len(agent_events),
            "actual_agent_names_from_same_run_events": started_agents,
            "actual_handoffs": payload.get("handoffs", []),
            "same_run_event_trace_ids": sorted({event.get("trace_id") for event in run_events if event.get("trace_id")}),
            "trace_id": payload.get("trace_id"),
            "program_checks": checks,
            "provider_requests": row["provider_requests"], "tokens": row["tokens"],
            "local_estimated_usd": 0.0,
            "business_tool_calls": row.get("business_read_calls"),
            "policy_retrieval_calls": sum(1 for item in payload.get("tool_references", [])
                if item.get("tool") == "search_knowledge"),
            "required_conditions": rule["required_conditions"],
            "locked_task_success_predicate": rule["task_success_predicate"],
            "technical_execution": "PASS" if all(checks.get(k) == "PASS" for k in
                ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
                 "tool_audit_completeness", "tool_result_audit_linkage", "expected_business_tool_execution",
                 "policy_retrieval_audit", "budget_and_cleanup")) else "UNKNOWN",
            "safety_scope": checks.get("tool_scope", "UNKNOWN"),
            "content_evidence_support": "AI_PREVIEW_PENDING_USER_REVIEW",
            "task_outcome_recommendation": None,
            "ai_recommendation": None,
            "ai_rationale": None,
            "reviewer_decision": None, "reviewer": None, "reviewed_at_utc": None,
            "human_confirmation_status": "PENDING_EXACT_RUN_REVIEW",
            "grading_status": "NOT_GRADED", "reference_status": "reference_unreviewed",
            "cleanup": row["cleanup"], "budget": {"provider_requests": row["provider_requests"],
                "tokens": row["tokens"], "local_estimated_usd": 0,
                "supplier_invoice": "UNKNOWN_BILLING" if row["provider_requests"] else "NOT_APPLICABLE_NO_PROVIDER_REQUEST"},
        })
    rationale = {
        "D1-PRODUCT-005": ("PASS", "The answer reports the actual catalog material and cites the same-run fictional warranty source with the 18-month term and the verified-purchase/activation condition. It does not claim personal eligibility."),
        "D1-AFTERSALES-019": ("PARTIAL", "The answer cites the 21-day unopened-accessory rule but does not ask the user to confirm item category and condition as required; it also renders the unknown order status as the literal None."),
        "D1-AFTERSALES-020": ("PARTIAL", "The verified return event chain and policy source support the APPROVED snapshot, and the answer avoids a write. It does not explain which application step is requestable given missing paid_at/paid_cents; the unknown order status is displayed as None."),
        "D1-CROSSAGENT-032": ("PARTIAL", "The answer maps the owner order line to the same product ID and cites the manual-review policy, without asserting eligibility. It does not explicitly state that item condition is unknown or ask the user to confirm it."),
    }
    for item in records:
        label, why = rationale[item["case_id"]]
        item["task_outcome_recommendation"] = label
        item["ai_recommendation"] = label
        item["ai_rationale"] = why
        item["content_evidence_support"] = "SUPPORTED_FACTS_WITH_COMPLETENESS_REVIEW" if label != "FAIL" else "FAIL"
        item["failure_classification"] = "ANSWER_COMPLETENESS_OR_PRESENTATION" if label == "PARTIAL" else "NONE"
        item["task_outcome_rationale_r18"] = why
    return records


def build_outputs() -> dict[str, Any]:
    if sha(DATA) != DATA_SHA:
        raise ValueError("R3_2_4_DEV_DATASET_HASH_MISMATCH")
    base = read(R17_PACKAGE)
    ledger = read(R17_LEDGER)
    manifest, rules = read(MANIFEST), read(RULES)
    authorization, state, journal = read(AUTH), read(STATE), [json.loads(x) for x in (ROOT / JOURNAL).read_text(encoding="utf-8").splitlines() if x.strip()]
    case_results, closeout, task_ledger = read(CASE_RESULTS), read(CLOSEOUT), read(TASK_LEDGER)
    admission = read(R10 / "application-admission-r10r2-5C7A2E94F1038B6D.json")
    ids = [x["case_id"] for x in case_results["cases"]]
    if (ids != CASES or manifest["proposed_order"] != CASES or rules["case_order"] != CASES or
        authorization["status"] != "CLOSED_COMPLETED" or closeout["authorization_final_status"] != "CLOSED_COMPLETED" or
        len(state["run_ids"]) != 4 or any(state["attempt_count"].get(cid) != 1 for cid in CASES) or
        any(state["case_state"].get(cid) != "CLOSED" for cid in CASES)):
        raise ValueError("R10_CLOSEOUT_SCOPE_OR_LIFECYCLE_INVALID")
    if (admission.get("admission") != "PASS" or admission.get("run_created") is not False or
        admission.get("provider_requests") != 0 or admission.get("case_ids") != CASES or
        closeout.get("attempted_case_ids") != CASES or closeout.get("not_run_case_ids") != [] or
        closeout.get("final_budget", {}).get("settled") is not True or
        closeout.get("final_budget", {}).get("active_reservations") != 0 or
        closeout.get("final_budget", {}).get("integrity_errors") or
        any(x.get("event") == "CASE_CLOSEOUT" and
            x.get("cleanup", {}).get("cleanup_status") != "CLEANED_AND_RELEASED" for x in journal)):
        raise ValueError("R10_ADMISSION_BUDGET_CLEANUP_INVALID")
    task_state = task_ledger
    if (task_state["spent"]["application_runs"] != 20 or task_state["reserved"]["application_runs"] != 0 or
        task_state.get("unknown_usage") or task_state.get("over_limit") or
        task_state["spent"]["provider_requests"] != 10 or task_state["spent"]["tokens"] != 8062):
        raise ValueError("SHARED_TASK_LEDGER_NOT_SETTLED_AT_20_RUNS")
    items = list(base["historical_runs"]) + list(base["r9s4_runs"]) + [base["policy018_recovery_run"]]
    if len(items) != 28:
        raise ValueError("R17_REVIEW_PACKAGE_RUN_COUNT_CHANGED")
    normalize_existing_28(base)
    new = build_r10_items(manifest, rules, case_results)
    base["r10_first_runs"] = new
    base["classification"] = "PERSONAL_DEV_AI_ASSISTED_SCORE_REVIEW_NOT_GOLD_OR_FORMAL_SMOKE"
    base["grading_status"] = "NOT_GRADED"
    base["reference_status"] = "reference_unreviewed"
    base["review_scope_note_r18"] = "R17's 28 source-bound Runs are preserved and normalized only in this derived report. Four R10 Runs are appended under a separate binding. No new reviewer fields are populated. STOP-after deviation Runs remain separate from compliant continuation statistics."
    base.setdefault("source_bindings", {})
    sources = [R17_PACKAGE, R17_LEDGER, MANIFEST, RULES, AUTH, STATE, JOURNAL, TASK_LEDGER,
        CASE_RESULTS, CLOSEOUT, DATA, Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json")]
    base["source_bindings_r18"] = {str(path).replace("\\", "/"): sha(path) for path in sources}

    new_ledger = copy.deepcopy(ledger)
    by_case = {row["case_id"]: row for row in new_ledger["rows"]}
    for item in new:
        row = by_case[item["case_id"]]
        row["previous_r17_status"] = row["final_status"]
        row["final_status"] = "EXECUTED_R10"
        row["detail"] = {"run_id": item["run_id"], "run_status": item["run_status"],
            "archive_path": item["archive_path"], "archive_sha256": item["archive_sha256"],
            "grader_path": item["grader_path"], "grader_sha256": item["grader_sha256"],
            "binding": item["binding"], "program_checks": item["program_checks"],
            "task_outcome_recommendation": item["task_outcome_recommendation"],
            "provider_requests": item["budget"]["provider_requests"], "tokens": item["budget"]["tokens"],
            "business_read_calls": next(x["business_read_calls"] for x in case_results["cases"] if x["case_id"] == item["case_id"]),
            "cleanup": item["cleanup"]}
    new_ledger["schema_version"] = "m6.2-r324-40case-coverage-ledger-r18"
    new_ledger["actual_run_count_across_r4_to_r10_records"] = 32
    new_ledger["r10_first_run_case_ids"] = CASES
    new_ledger["r10_batch_id"] = manifest["batch_id"]
    new_ledger["held_or_blocked_count"] = sum(row["final_status"].startswith(("BLOCKED", "HELD", "UNSCORABLE")) for row in new_ledger["rows"])
    new_ledger["note"] = "32 cases have actual Runs across distinct historical and R10 bindings; this is coverage, not a unified 40-case same-configuration score."
    if sum("EXECUTED" in row["final_status"] for row in new_ledger["rows"]) != 32 or new_ledger["held_or_blocked_count"] != 8:
        raise ValueError("FORTY_CASE_COVERAGE_CONSERVATION_FAILED")

    metrics = build_review_metrics(base)
    metrics["schema_version"] = "m6.2-r10-dev-metrics-r18"
    metrics["classification"] = "AI_ASSISTED_DEV_PROVISIONAL_BY_BINDING_NOT_FORMAL"
    output_summary = {
        "schema_version": "m6.2-r10-dev-score-closeout-r18",
        "classification": "PERSONAL_DEV_AI_ASSISTED_PROVISIONAL_NOT_GRADED",
        "review_status": "reference_unreviewed",
        "source_run_count": 28,
        "new_r10_run_count": 4,
        "actual_run_coverage": 32,
        "held_or_blocked_count": 8,
        "post_stop_deviation_case_ids": ["D1-SAFETY-039", "D1-SAFETY-040"],
        "r10_provider_requests": closeout["provider_requests"],
        "r10_tool_calls": closeout["final_budget"]["calls_by_category"]["tool"],
        "r10_tokens": closeout["final_budget"]["tokens"],
        "r10_local_estimated_usd": closeout["final_budget"]["estimated_usd"],
        "supplier_invoice_status": "NOT_APPLICABLE_NO_PROVIDER_REQUESTS_IN_R10; HISTORICAL_INVOICES_UNKNOWN",
        "shared_task_ledger_after_r10": task_state["spent"],
        "shared_task_limits": task_state["limits"],
        "shared_reservations": task_state["reserved"],
        "r10_runs": [{k:x.get(k) for k in ("case_id","run_id","run_status","task_outcome_recommendation","answer_delivery_status","technical_execution","budget","cleanup")} for x in new],
        "r9_post_stop_deviation_excluded_from_compliant_sequence_denominator": True,
        "different_bindings_not_combined": True,
        "metrics_path": "docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030/reports/dev-metrics-r18.json",
        "task_outcome_metric_groups": metrics["groups"],
    }
    return {"review": base, "coverage": new_ledger, "metrics": metrics, "closeout": output_summary,
        "r10_items": new}


def render_review_markdown(out: dict) -> str:
    records = list(out["review"]["historical_runs"]) + list(out["review"]["r9s4_runs"]) + [out["review"]["policy018_recovery_run"]] + out["r10_items"]
    lines = ["# M6.2 Dev 评分预审与 R10 收口 R18", "",
        "性质：PERSONAL_DEV / AI-assisted provisional / NOT_GRADED / reference_unreviewed。28个历史Run按原绑定保存；R10四案按新绑定追加。", "",
        "| Case | Run | 绑定段 | Run | 安全/权限 | 证据/工具 | 实际交付 | Task Outcome建议 | 人工状态 |", "|---|---|---|---|---|---|---|---|---|"]
    for item in records:
        derived = item.get("derived_score_r18") or {}
        binding = item.get("binding") or {}
        segment = binding.get("batch_id") or item.get("historical_segment") or "historical-binding"
        checks = _program_checks(item)
        tool = checks.get("expected_business_tool_execution", "UNKNOWN")
        evidence = checks.get("tool_result_audit_linkage", checks.get("policy_retrieval_audit", "UNKNOWN"))
        safety = derived.get("safety_scope", checks.get("tool_scope", "UNKNOWN"))
        delivery = derived.get("user_delivery", item.get("user_delivery_status_r18", item.get("answer_delivery_status", "UNKNOWN")))
        outcome = derived.get("task_outcome_recommendation", item.get("task_outcome_recommendation", "UNKNOWN"))
        human = item.get("human_confirmation_status", "PENDING_EXACT_RUN_REVIEW")
        lines.append(f"| `{item.get('case_id')}` | `{item.get('run_id')}` | `{segment}` | {item.get('run_status','UNKNOWN')} | {safety} | tool={tool}; evidence={evidence} | {delivery} | **{outcome}** | {human} |")
    lines.extend(["", "## 逐案可读审核依据", ""])
    for item in records:
        derived = item.get("derived_score_r18") or {}
        answer = item.get("delivered_answer")
        if not answer:
            cand = item.get("rejected_candidate_audit")
            answer_view = ("未交付用户；另有受控候选，单列候选审阅。" if cand else "没有已保存的用户可见答案；交付FAIL，候选语义不可评分。")
        else:
            answer_view = answer
        evidence = item.get("tool_and_policy_results") or item.get("evidence") or item.get("actual_tool_evidence") or item.get("source") or []
        rec = item.get("ai_rationale") or item.get("task_outcome_rationale_r18") or item.get("existing_ai_preview") or "需基于对应来源审核。"
        lines.extend([f"### {item.get('case_id')} — `{item.get('run_id')}`", "",
            f"- 问题：{item.get('question','UNKNOWN')}", f"- 实际交付：{answer_view}",
            f"- 安全/权限：{derived.get('safety_scope', _program_checks(item).get('tool_scope','UNKNOWN'))}",
            f"- 必需工具：{derived.get('required_tool_completion', _program_checks(item).get('expected_business_tool_execution','UNKNOWN'))}",
            f"- 证据与调用关联：{json.dumps(evidence,ensure_ascii=False)[:2400]}",
            f"- Task Outcome暂定建议：**{derived.get('task_outcome_recommendation',item.get('task_outcome_recommendation','UNKNOWN'))}**；理由：{rec}",
            f"- 内容候选状态：{derived.get('content_and_evidence_support',item.get('candidate_content_status_r18','REVIEW_SOURCE_BOUND_ANSWER'))}",
            f"- 人工决定：{item.get('reviewer_decision')}；reviewer：{item.get('reviewer')}；时间：{item.get('reviewed_at_utc')}。", ""])
    lines.extend(["## 指标口径", "", "指标按实现摘要、规则摘要和数据SHA分组；没有全40案统一绑定成绩。终态运行无用户交付计入Task Outcome FAIL；被拒候选只用于单独内容复核。PASS比例仅在PASS/PARTIAL/FAIL的可评分建议中计算Wilson 95%区间。SAFETY-039/040作为STOP后偏差观察保留，不计入合规续跑分母。检索Recall/MRR因排名完整性和相关性标签不足继续N/A；Judge旧校准仅作开发校准证据。", ""])
    return "\n".join(lines)


def main() -> None:
    out = build_outputs()
    report_dir = R10 / "reports"
    write(report_dir / "dev-score-review-package-r18.json", out["review"])
    write(report_dir / "40-case-coverage-ledger-r18.json", out["coverage"])
    write(report_dir / "dev-metrics-r18.json", out["metrics"])
    write(report_dir / "closeout-r18.json", out["closeout"])
    (ROOT / report_dir / "dev-score-review-package-r18.md").write_text(render_review_markdown(out), encoding="utf-8")
    print(json.dumps({"review_run_count": len(out["review"]["historical_runs"])+len(out["review"]["r9s4_runs"])+1,
        "r10_run_count": len(out["r10_items"]), "coverage_runs": sum("EXECUTED" in x["final_status"] for x in out["coverage"]["rows"]),
        "held_or_blocked": out["coverage"]["held_or_blocked_count"],
        "metrics_binding_groups": out["metrics"]["binding_group_count"]}, indent=2))


if __name__ == "__main__":
    main()
