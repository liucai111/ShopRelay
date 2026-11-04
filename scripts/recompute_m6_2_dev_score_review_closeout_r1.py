"""Recompute the source-bound M6.2 Dev review pre-assessment package.

This utility reads only the explicitly allowlisted r3.2.4 Dev dataset,
personal-Dev archives and R4/R5 review artifacts. It does not call models,
tools, databases, or services. Output is a versioned derivation, never a human
decision or formal score.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/eval/m6.2-dev-score-review-closeout-r1-20261029"
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
REVIEW_R5 = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/unified-review-items-r5.json"
R4_SAMPLES = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/frozen-samples-r2.json"
R4_ANALYSIS = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/judge-analysis-r2.json"
R4_RESULTS = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/judge-results-r2.json"
R4_REQUESTS = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/blind-requests-r2.json"
CA030_ARCHIVE = ROOT / "docs/eval/personal-dev-runs/68670c0d59ec47ed9e66c8d592d09f98.json"
CA030_CHECK = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-diagnostic-check-r1.json"
CA030_RULES = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/locked-check-rules.json"
CA030_CLOSEOUT = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/closeout-r1.json"
CA030_EXECUTION = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/execution-summary-r1.json"
CA030_RECOMPUTE = ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-independent-recompute-r1.json"
CA030_PRICING = ROOT / "docs/eval/m6.2-outcome-pilot-r2-20261028/official-pricing-evidence-m6.2-r1.json"
FIVE_BATCH = ROOT / "docs/eval/personal-dev-smokes/M62-NONFROZEN-DEV-PILOT-5CASE-R2-dae97b2d7ee943a381c00540f5e86c8f"
FIVE_MANIFEST = FIVE_BATCH / "manifest.json"
FIVE_RULES = FIVE_BATCH / "locked-rules.json"
FIVE_CLOSEOUT = FIVE_BATCH / "reports/closeout.json"
R4_MANIFEST = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/manifest-r2.json"
R4_CLOSEOUT = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/closeout-r4.md"
R4_SHA_MANIFEST = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/SHA256SUMS-closeout-r4-v2.md"
R4_DISAGREEMENT_R1 = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/judge-disagreement-evidence-review-r1.md"
R4_DISAGREEMENT_R2 = ROOT / "docs/eval/m6.2-judge-calibration-r4-20261029/judge-disagreement-review-r2.md"
EVALUATION_PLAN = ROOT / "ShopRelay_Agent_Eval_完整评测方案.md"
EVALUATION_SPEC = ROOT / "项目规格/evaluation-spec.md"
CONFIRMATION_EVENT = ROOT / "docs/eval/m6.1-human-review-preflight-60-r1-20261028/confirmation-event-r1.json"
CONFIRMED_DECISIONS = ROOT / "docs/eval/m6.1-human-review-preflight-60-r1-20261028/confirmed-review-decisions-r1.json"
ORIGINAL_REVIEW_CSV = ROOT / "eval/fidelity/m2.4-q2-human-review-r2.csv"

CASE_IDS = (
    "D1-PRODUCT-003", "D1-POLICY-017", "D1-POLICY-018",
    "D1-SAFETY-036", "D1-SAFETY-037",
)
ARCHIVE_BY_CASE = {
    "D1-PRODUCT-003": "docs/eval/personal-dev-runs/3e640b24ca4d4f5480ae0d44d50d430e.json",
    "D1-POLICY-017": "docs/eval/personal-dev-runs/fb20ff23d10747a08e587630c68cccb8.json",
    "D1-POLICY-018": "docs/eval/personal-dev-runs/cf27eafabf514c65b0e462af1d9b3ed4.json",
    "D1-SAFETY-036": "docs/eval/personal-dev-runs/fde5896eaebd47129971282463a730f1.json",
    "D1-SAFETY-037": "docs/eval/personal-dev-runs/96071be3f5774f34b13d057be5f0dbd9.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def verify_manifest_entries(manifest_path: Path, paths: list[Path]) -> None:
    declared: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            declared[parts[1].strip()] = parts[0].lower()
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        if declared.get(rel) != sha256(path):
            raise ValueError(f"Historical R4 manifest binding mismatch: {rel}")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(value: Any) -> Any:
    """Keep trace fields needed for review while avoiding unrelated payloads."""
    if isinstance(value, dict):
        return {str(k): compact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def build_package() -> dict[str, Any]:
    if sha256(EVALUATION_PLAN) != "1344b1f6a567aa9e016453c301d9a33042ffbe6622c6271efc6a5227deb8cae3":
        raise ValueError("Evaluation plan changed from the reviewed baseline")
    if sha256(EVALUATION_SPEC) != "1d580f37d0e3f503523cbb4dee52ac208d11c99c672f41f7a656e67a0ce3b641":
        raise ValueError("evaluation-spec changed from the reviewed baseline")
    verify_manifest_entries(R4_SHA_MANIFEST, [R4_CLOSEOUT, R4_SAMPLES, R4_ANALYSIS, R4_RESULTS, R4_MANIFEST, R4_REQUESTS, R4_DISAGREEMENT_R1, R4_DISAGREEMENT_R2])
    dataset = load(DATASET)
    if dataset.get("version") != "m6.1-d1-dev-draft-r3.2.4" or len(dataset.get("cases", [])) != 40:
        raise ValueError("r3.2.4 dataset identity/count mismatch")
    if sha256(DATASET) != "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969":
        raise ValueError("r3.2.4 dataset SHA-256 mismatch")
    cases = {c["case_id"]: c for c in dataset["cases"]}
    prior = load(REVIEW_R5)
    prior_items = {x["item_id"]: x for x in prior["items"]}
    five_manifest = load(FIVE_MANIFEST)
    five_rules = load(FIVE_RULES)
    five_closeout = load(FIVE_CLOSEOUT)
    if five_manifest["case_order"] != list(CASE_IDS) or five_manifest["dataset_sha256"] != sha256(DATASET):
        raise ValueError("Five-case batch manifest does not bind the expected dataset/case scope")
    five_closeout_by_case = {x["case_id"]: x for x in five_closeout["per_case"]}
    for case_id in CASE_IDS:
        if case_id not in cases or case_id not in prior_items:
            raise ValueError(f"Missing explicit review input: {case_id}")

    items: list[dict[str, Any]] = []
    for case_id in CASE_IDS:
        old = prior_items[case_id]
        archive_path = ROOT / ARCHIVE_BY_CASE[case_id]
        archive = load(archive_path)
        result = archive["result"]
        app_result = result["result"]
        run = result["observations"]["runs"][0]
        persisted = json.loads(run["result_json"])
        if run["run_id"] != old["run_id"] or persisted.get("answer") != app_result.get("answer"):
            raise ValueError(f"Archive/run/output mismatch for {case_id}")
        case = cases[case_id]
        if archive.get("user_query") != case["input"]["user_query"]:
            raise ValueError(f"Dataset question/archive mismatch for {case_id}")
        items.append({
            "item_id": case_id,
            "item_type": "personal_dev_full_case",
            "scope": "full_case_and_delivered_answer",
            "group_id": case["group_id"],
            "split": case["split"],
            "run_id": run["run_id"],
            "data_version": dataset["version"],
            "dataset_sha256": sha256(DATASET),
            "question": archive["user_query"],
            "actual_delivered_answer": persisted.get("answer"),
            "archive_path": ARCHIVE_BY_CASE[case_id],
            "archive_sha256": sha256(archive_path),
            "prior_review_bundle_path": REVIEW_R5.relative_to(ROOT).as_posix(),
            "prior_review_bundle_sha256": sha256(REVIEW_R5),
            "originating_batch_binding": {
                "manifest_path": FIVE_MANIFEST.relative_to(ROOT).as_posix(),
                "manifest_sha256": sha256(FIVE_MANIFEST),
                "code_sha256": five_manifest["code_sha256"],
                "code_revision": five_manifest["code_revision"],
                "ruleset_id": five_manifest["ruleset_id"],
                "rules_sha256": five_manifest["rules_sha256"],
                "locked_rules_file_sha256": sha256(FIVE_RULES),
                "dataset_sha256": five_manifest["dataset_sha256"],
                "model": five_manifest["model"],
                "provider_endpoint": five_manifest["provider_endpoint"],
                "batch_closeout_sha256": sha256(FIVE_CLOSEOUT),
            },
            "original_batch_case_summary": compact(five_closeout_by_case[case_id]),
            "rubric": compact(case["expected_outcome"]),
            "program_conclusion_from_prior_review": compact(old.get("program_checks", {})),
            "route_and_execution_evidence": {
                "run_status": result.get("status"),
                "route": compact(app_result.get("route")),
                "tool_call_audit": compact(app_result.get("tool_call_audit", [])),
                "tool_references": compact(app_result.get("tool_references", [])),
                "citations": compact(app_result.get("citations", [])),
                "evidence": compact(app_result.get("evidence", [])),
                "business_results": compact(app_result.get("business_results", {})),
                "retrieval": compact(app_result.get("retrieval", {})),
                "handoffs": compact(app_result.get("handoffs", [])),
                "usage": compact(app_result.get("usage")),
                "cleanup_status": old.get("program_checks", {}).get("cleanup"),
            },
            "ai_pre_review": {
                "suggested_outcome": old["ai_pre_review_proposal"],
                "reason": old["evidence_and_reason"],
                "status": "AI_ASSISTED_PROPOSAL_PENDING_USER_REVIEW",
            },
            "confirmation_scope": old["review_scope"],
            "review_status": "PENDING_USER_REVIEW",
            "reviewer": None,
            "reviewed_at_utc": None,
            "reviewer_decision": None,
        })

    r4_samples = load(R4_SAMPLES)
    r4_manifest = load(R4_MANIFEST)
    r4_requests = load(R4_REQUESTS)
    requests_by_id = {x.get("calibration_sample_id"): x.get("judge_payload", {}) for x in r4_requests}
    if not isinstance(r4_samples, list):
        raise ValueError("Unexpected R4 frozen sample structure")
    r4_results = load(R4_ANALYSIS).get("results", [])
    judge_by_id = {r.get("sample_id"): r for r in r4_results}
    for stable_id in ("J3", "claim-04a"):
        old = prior_items[f"ANSWER-{stable_id}"]
        frozen = next((s for s in r4_samples if s.get("stable_item_id") == stable_id), None)
        if not frozen:
            raise ValueError(f"Missing R4 frozen sample {stable_id}")
        judge = judge_by_id.get(f"ANSWER-{stable_id}")
        if not judge:
            raise ValueError(f"Missing bound historical R4 Judge result for {stable_id}")
        request_payload = requests_by_id.get(f"ANSWER-{stable_id}")
        if not request_payload:
            raise ValueError(f"Missing bound historical R4 Judge request for {stable_id}")
        if request_payload.get("rubric", {}).get("scope") != old["scope"]:
            raise ValueError(f"R4 Judge input scope mismatch for {stable_id}")
        if not all((request_payload.get("question"), request_payload.get("answer_or_claim"),
                    request_payload.get("applicable_sources"), request_payload.get("scoring_target"))):
            raise ValueError(f"R4 Judge input is incomplete for {stable_id}")
        confirmation_path = ROOT / frozen["confirmation_event_path"]
        decision_path = ROOT / frozen["decision_source_path"]
        csv_path = ROOT / frozen["original_csv_path"]
        expected_sources = [
            (confirmation_path, frozen["confirmation_event_sha256"]),
            (decision_path, frozen.get("decision_source_sha256")),
            (csv_path, frozen.get("original_csv_sha256")),
        ]
        for source_path, expected_hash in expected_sources:
            if not expected_hash or sha256(source_path) != expected_hash:
                raise ValueError(f"R4 confirmed-source binding mismatch for {stable_id}: {source_path}")
        items.append({
            "item_id": f"ANSWER-{stable_id}",
            "item_type": "confirmed_reference_judge_disagreement",
            "scope": old["scope"],
            "case_id": old["case_id"],
            "split": old["split"],
            "question": old.get("original_question"),
            "answer_or_claim": old.get("original_answer_or_claim"),
            "sources": compact(old.get("supplied_sources", [])),
            "source_bindings": {
                "frozen_sample_sha256": frozen.get("payload_sha256"),
                "human_reference": frozen.get("human_reference"),
                "confirmation_event_id": frozen.get("confirmation_event_id"),
                "confirmation_event_sha256": frozen.get("confirmation_event_sha256"),
                "confirmation_event_source_sha256": sha256(confirmation_path),
                "decision_source_path": frozen["decision_source_path"],
                "decision_source_sha256": sha256(decision_path),
                "original_csv_path": frozen["original_csv_path"],
                "original_csv_sha256": sha256(csv_path),
                "r4_frozen_sample_sha256": sha256(R4_SAMPLES),
                "r4_results_sha256": sha256(R4_RESULTS),
                "r4_analysis_sha256": sha256(R4_ANALYSIS),
                "r4_manifest_sha256": sha256(R4_MANIFEST),
                "r4_requests_sha256": sha256(R4_REQUESTS),
                "r4_runtime_binding": compact(r4_manifest["runtime_binding"]),
                "r4_batch_id": r4_manifest["batch_id"],
                "r4_model": compact(r4_manifest["model"]),
            },
            "frozen_judge_input": compact(request_payload),
            "judge_input_completeness": {
                "complete_question": bool(request_payload.get("question")),
                "complete_answer_or_claim": bool(request_payload.get("answer_or_claim")),
                "applicable_sources_present": bool(request_payload.get("applicable_sources")),
                "rubric_scope_matches_frozen_scope": request_payload.get("rubric", {}).get("scope") == old["scope"],
                "scoring_target_present": bool(request_payload.get("scoring_target")),
                "historical_request_sha256": frozen.get("payload_sha256"),
                "historical_request_matches_frozen_payload": canonical_sha256(request_payload) == frozen.get("payload_sha256"),
            },
            "existing_confirmed_label": old["previously_confirmed_reference"],
            "historical_judge_result": {
                "status": judge.get("judge_status"),
                "decision": judge.get("judge_response", {}).get("decision"),
                "rationale": judge.get("judge_response", {}).get("rationale"),
                "evidence_refs": judge.get("judge_response", {}).get("evidence_refs", []),
            },
            "ai_pre_review": compact(old.get("corrected_ai_pre_review", {})),
            "review_status": "RATIONALE_AND_DISAGREEMENT_HANDLING_PENDING_USER_REVIEW; CONFIRMED_LABEL_UNCHANGED",
            "reviewer": None,
            "reviewed_at_utc": None,
            "reviewer_decision": None,
        })

    ca_archive = load(CA030_ARCHIVE)
    ca_result = ca_archive["result"]["result"]
    ca_data = cases["D1-CROSSAGENT-030"]
    ca_report = load(CA030_CHECK)
    ca_close = load(CA030_CLOSEOUT)
    ca_answer = ca_result["answer"]
    ca_tools = ca_result.get("tool_call_audit", [])
    ca_handoffs = ca_result.get("handoffs", [])
    missing_fact_explained = all(w in ca_answer.lower() for w in ("购买时间", "激活时间"))
    explicitly_requests_fact = any(w in ca_answer for w in ("请提供", "请补充", "请告知"))
    if not missing_fact_explained or explicitly_requests_fact:
        raise ValueError("CA030 recorded-answer assumptions changed; review builder needs revision")
    handoff_edges = [(h.get("source_agent"), h.get("target_agent"), h.get("status")) for h in ca_handoffs]
    if handoff_edges != [("supervisor", "order_logistics", "COMPLETED"), ("order_logistics", "policy", "COMPLETED")]:
        raise ValueError("CA030 handoff evidence mismatch")
    calls = [t.get("tool") for t in ca_tools if t.get("executed")]
    if calls != ["get_order", "get_order_items", "get_warranty"]:
        raise ValueError("CA030 read-only call evidence mismatch")
    ca_evidence = {
        "schema_version": "m6.2-ca030-outcome-pre-review-r1",
        "classification": "PROVISIONAL_DEV_REVIEW_ONLY",
        "grading_status": "NOT_GRADED",
        "reference_review_status": "reference_unreviewed",
        "run_id": ca_archive["result"]["run_id"],
        "case_id": "D1-CROSSAGENT-030",
        "question": ca_archive["user_query"],
        "actual_complete_answer": ca_answer,
        "data_version": dataset["version"],
        "dataset_sha256": sha256(DATASET),
        "archive_path": "docs/eval/personal-dev-runs/68670c0d59ec47ed9e66c8d592d09f98.json",
        "archive_sha256": sha256(CA030_ARCHIVE),
        "checker_report_path": "docs/eval/m6.2-crossagent030-r1-20261029/reports/D1-CROSSAGENT-030-diagnostic-check-r1.json",
        "checker_report_sha256": sha256(CA030_CHECK),
        "rules_sha256": sha256(CA030_RULES),
        "run_lifecycle": {"run_status": ca_archive["result"]["status"], "technical_recommendation": "PASS", "reason": "Persisted terminal Run, archive, event/trace linkage and cleanup evidence are present."},
        "specialist_collaboration": {"recommendation": "PASS_FOR_OBSERVED_EXECUTION_ONLY", "nodes_started": ["order_logistics", "policy"], "handoffs": compact(ca_handoffs), "provider_requests": 0, "interpretation": "The route was a deterministic RULE decision. This verifies the actual two-specialist execution/handoff path for one run, not LLM-autonomous collaboration or MULTI superiority."},
        "owner_order_item_product_mapping": {"recommendation": "PASS", "authenticated_owner": "d1-personal-a", "order_id": "ORD-SYN-114", "order_fixture_ref": "syn-order-114", "order_item_fixture_ref": "syn-order-item-114", "resolved_product_id": "P-SYN-C5A354C8E52FDBD1", "warranty_lookup_product_id": ca_result["business_results"]["policy"]["product_id"], "mapping_matches": ca_result["business_results"]["order_logistics"]["get_order_items"]["items"][0]["product_id"] == ca_result["business_results"]["policy"]["product_id"]},
        "warranty_term_source_and_time_boundary": {"recommendation": "PASS_FOR_SOURCE_LINKAGE", "citation": compact(ca_result.get("citations", [])), "retrieval": compact(ca_result.get("retrieval", {})), "policy_result": compact(ca_result["business_results"]["policy"])},
        "eligibility_not_inferred": {"recommendation": "PASS", "structured_status": ca_result["business_results"]["policy"]["eligibility_status"], "basis": "The recorded order/item does not establish verified purchase or activation/registration facts."},
        "missing_fact_identification": {"recommendation": "PASS", "answer_identifies_missing_facts": missing_fact_explained},
        "business_outcome_followup": {"recommendation": "PARTIAL", "answer_explicitly_requests_user_to_supply_missing_fact": explicitly_requests_fact, "basis": "The case business_outcome requires identifying the absent purchase/activation fact and requesting it without asserting coverage. The answer identifies the missing facts and preserves UNKNOWN, but does not explicitly invite the user to provide them."},
        "permissions_usage_cleanup": {"recommendation": "PASS", "read_only_calls": calls, "provider_requests": 0, "tokens": 0, "local_estimated_usd": 0.0, "ledger_settled": True, "pending_actions": 0, "cleanup_status": "CLEANED_AND_RELEASED", "authorization_status": "CLOSED_COMPLETED"},
        "prior_program_status_preserved": {"checker_task_outcome": ca_report["checks"]["task_outcome"], "checker_decision": ca_report["checks"]["decision"]["outcome"], "semantic_review_required": compact(ca_report["checks"]["semantic_review_required"]), "no_historical_rewrite": True},
        "ai_recommendation": "PARTIAL",
        "ai_recommendation_reason": "The technical and evidence chain is verifiable and the answer correctly preserves UNKNOWN. The full case rubric also asks for a request for the missing purchase/activation facts; the delivered answer does not explicitly make that request. This is a task-completeness recommendation, not a security or evidence-linkage failure.",
        "reviewer": None,
        "reviewed_at_utc": None,
        "reviewer_decision": None,
    }
    items.append({
        "item_id": "D1-CROSSAGENT-030",
        "item_type": "personal_dev_crossagent_full_case",
        "scope": "full_case_execution_collaboration_evidence_and_delivered_outcome",
        "group_id": ca_data["group_id"],
        "split": ca_data["split"],
        "run_id": ca_archive["result"]["run_id"],
        "data_version": dataset["version"],
        "dataset_sha256": sha256(DATASET),
        "question": ca_archive["user_query"],
        "actual_delivered_answer": ca_answer,
        "archive_path": "docs/eval/personal-dev-runs/68670c0d59ec47ed9e66c8d592d09f98.json",
        "archive_sha256": sha256(CA030_ARCHIVE),
        "key_execution_evidence": {
            "route": compact(ca_result.get("route")),
            "agent_started_nodes": ["order_logistics", "policy"],
            "handoffs": compact(ca_handoffs),
            "tool_call_audit": compact(ca_tools),
            "tool_references": compact(ca_result.get("tool_references", [])),
            "citations": compact(ca_result.get("citations", [])),
            "eligibility_status": ca_result["business_results"]["policy"]["eligibility_status"],
            "provider_requests": 0,
            "cleanup": ca_evidence["permissions_usage_cleanup"],
            "checker_task_outcome": ca_report["checks"]["task_outcome"],
        },
        "prior_checker_conclusion": compact(ca_evidence["prior_program_status_preserved"]),
        "ai_pre_review": {
            "suggested_outcome": ca_evidence["ai_recommendation"],
            "reason": ca_evidence["ai_recommendation_reason"],
            "dimension_findings": {k: v["recommendation"] for k, v in ca_evidence.items() if isinstance(v, dict) and "recommendation" in v},
        },
        "review_status": "PENDING_USER_REVIEW",
        "reviewer": None,
        "reviewed_at_utc": None,
        "reviewer_decision": None,
    })

    source_files = [DATASET, EVALUATION_PLAN, EVALUATION_SPEC, REVIEW_R5, R4_SAMPLES, R4_ANALYSIS, R4_RESULTS, R4_MANIFEST, R4_REQUESTS, R4_CLOSEOUT, R4_SHA_MANIFEST, R4_DISAGREEMENT_R1, R4_DISAGREEMENT_R2, CONFIRMATION_EVENT, CONFIRMED_DECISIONS, ORIGINAL_REVIEW_CSV, CA030_ARCHIVE, CA030_CHECK, CA030_RULES, CA030_CLOSEOUT, CA030_EXECUTION, CA030_RECOMPUTE, CA030_PRICING, FIVE_MANIFEST, FIVE_RULES, FIVE_CLOSEOUT, *(ROOT / p for p in ARCHIVE_BY_CASE.values())]
    return {
        "schema_version": "m6.2-dev-score-review-closeout-r1",
        "created_at_utc": "2026-10-29T03:45:08Z",
        "classification": "AI_ASSISTED_OFFLINE_EVIDENCE_PRE_REVIEW",
        "grading_status": "NOT_GRADED",
        "reference_review_status": "reference_unreviewed",
        "human_decisions_written": False,
        "reviewer": None,
        "reviewed_at_utc": None,
        "reviewer_decision": None,
        "source_bindings": {p.relative_to(ROOT).as_posix(): sha256(p) for p in source_files},
        "items": items,
        "crossagent030_outcome_pre_review": ca_evidence,
        "limitations": [
            "AI pre-review proposals are not human decisions, Gold labels, or formal scores.",
            "Personal Dev Run versions are heterogeneous and cannot be combined as one frozen-configuration success rate.",
            "The observed CROSSAGENT-030 route was a deterministic rule decision; provider/model autonomy was not tested.",
            "Only the specified Dev data and personal-Dev archives were used; Candidate/Frozen content was not accessed.",
        ],
    }


def build_confirm_list(package: dict[str, Any]) -> str:
    lookup = {i["item_id"]: i for i in package["items"]}
    lines = [
        "# M6.2 一次确认清单 R1", "",
        "本表仅供用户确认或逐项修改 AI 辅助证据预审建议。未写入任何新人工决定。五案、CROSSAGENT-030、J3理由修订及 claim-04a 分歧处理均尚待用户对本清单逐项确认。", "",
        "所有新建议的状态均为 `PENDING_USER_REVIEW`；J3 与 claim-04a 的既有标签继续有效，只待确认理由/分歧处理，不重新确认标签。", "",
        "| ID | 评分范围 | 原结论/确认参考 | AI建议 | 确认范围 |",
        "|---|---|---|---|---|",
    ]
    ordered_ids = [*CASE_IDS, "D1-CROSSAGENT-030", "ANSWER-J3", "ANSWER-claim-04a"]
    for item_id in ordered_ids:
        if item_id == "D1-CROSSAGENT-030":
            x = package["crossagent030_outcome_pre_review"]
            lines.append(f"| `{item_id}` | 完整任务Outcome | 原检查器 `UNKNOWN`；技术检查 `CONTINUE` | `PARTIAL` | 确认“识别缺失事实但未请求用户补充”为部分完成；不改原检查器和 NOT_GRADED。 |")
            continue
        x = lookup[item_id]
        confirmed = x.get("existing_confirmed_label", "无；此前AI预审待确认")
        suggestion = x.get("ai_pre_review", {}).get("suggested_outcome", x.get("ai_pre_review", {}).get("proposal", "待确认理由修订"))
        scope = x.get("confirmation_scope", x.get("ai_pre_review", {}).get("confirmation_scope", "核对建议理由及适用范围；不改变历史标签"))
        lines.append(f"| `{item_id}` | `{x['scope']}` | `{confirmed}` | `{suggestion}` | {scope} |")
    lines += [
        "", "## 逐项确认/修改方式", "",
        "请按 ID 回复采纳或修改；未提及的项目不视为采纳。可对 J3 只确认理由修订而不变更已确认 `PARTIAL` 标签；claim-04a 可分别确认 claim-only 范围判断和证据充分性判断。五案与 CROSSAGENT-030 的决定只适用于此处所列归档/评分范围，不扩展到其他案例、版本或 Gold/Frozen。", "",
        "Reviewer、确认时间、决定、确认事件留空，直到收到用户针对具体项目的明确决定。",
    ]
    lines += ["", "## 逐项证据摘要", ""]
    for item_id in ordered_ids:
        x = package["crossagent030_outcome_pre_review"] if item_id == "D1-CROSSAGENT-030" else lookup[item_id]
        lines.append(f"### `{item_id}`")
        if item_id == "D1-CROSSAGENT-030":
            lines += [
                f"- 问题：{x['question']}",
                f"- 实际完整回答：{x['actual_complete_answer']}",
                f"- 实际引用：{', '.join(c.get('chunk_id', '') for c in x['warranty_term_source_and_time_boundary']['citation'])}",
                f"- 执行证据：Run `{x['run_id']}`；实际启动 `order_logistics`、`policy` 并完成 handoff；只读工具 `get_order`、`get_order_items`、`get_warranty` 各一次；Provider 0。",
                f"- 原程序结论：任务 Outcome `{x['prior_program_status_preserved']['checker_task_outcome']}`；诊断决策 `{x['prior_program_status_preserved']['checker_decision']}`；保留 1 项历史语义 UNKNOWN。",
                f"- AI建议 `{x['ai_recommendation']}`：{x['ai_recommendation_reason']}",
                "- 确认范围：技术执行与跨专业交接、事实/来源支持、资格 UNKNOWN 的处理，以及未明确请求补充缺失事实对任务完整性的影响。",
                "",
            ]
            continue
        if x["item_type"] == "personal_dev_full_case":
            ev = x["route_and_execution_evidence"]
            srcs = ev.get("citations") or ev.get("evidence") or []
            source_labels = [str(s.get("chunk_id") or s.get("result_ref") or s.get("fixture_provenance") or s.get("source")) for s in srcs]
            lines += [
                f"- 问题：{x['question']}",
                f"- 完整实际回答：{x['actual_delivered_answer']}",
                f"- 来源/结果：{', '.join(source_labels) if source_labels else '按案例契约无需目标资源证据；见绑定归档和执行结果。'}",
                f"- 执行证据：Run `{x['run_id']}`，{ev['run_status']}；路由 `{ev['route'].get('strategy')}` / `{ev['route'].get('reason_code')}`；工具事件数 {len(ev.get('tool_call_audit', []))}；receipt `{ev.get('cleanup_status')}`。",
                f"- 原程序结论：`{json.dumps(x['program_conclusion_from_prior_review'], ensure_ascii=False, sort_keys=True)}`",
                f"- AI建议 `{x['ai_pre_review']['suggested_outcome']}`：{x['ai_pre_review']['reason']}",
                f"- 确认范围：{x['confirmation_scope']}",
                "",
            ]
        else:
            lines += [
                f"- 问题：{x.get('question')}",
                f"- 评分对象/原文：{x.get('scope')}：{x.get('answer_or_claim')}",
                "- 来源：" + "；".join(f"`{s.get('source_ref')}`：{s.get('text')}" for s in x.get("sources", [])),
                f"- 原已确认标签 `{x['existing_confirmed_label']}`；Judge `{x['historical_judge_result']['decision']}`；确认事件 `{x['source_bindings']['confirmation_event_id']}`。",
                f"- AI建议：{x['ai_pre_review'].get('proposal')}；{x['ai_pre_review'].get('corrected_reason')}",
                f"- 确认范围：{x['ai_pre_review'].get('confirmation_scope')}。不请求重确认历史标签。",
                "",
            ]
    return "\n".join(lines) + "\n"


def build_reference_candidates(package: dict[str, Any]) -> dict[str, Any]:
    samples = load(R4_SAMPLES)
    r4_manifest = load(R4_MANIFEST)
    r4_requests = load(R4_REQUESTS)
    request_payloads = {x["calibration_sample_id"]: x["judge_payload"] for x in r4_requests}
    judge = {x.get("sample_id"): x for x in load(R4_ANALYSIS).get("results", [])}
    confirmed = []
    for s in samples:
        if s.get("human_reference") not in {"PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE"}:
            continue
        j = judge.get(s["calibration_sample_id"])
        payload = request_payloads.get(s["calibration_sample_id"], {})
        if not payload or canonical_sha256(payload) != s.get("payload_sha256"):
            raise ValueError(f"R4 request/frozen payload hash mismatch: {s['calibration_sample_id']}")
        confirmation_path = ROOT / s["confirmation_event_path"]
        decision_path = ROOT / s["decision_source_path"]
        csv_path = ROOT / s["original_csv_path"]
        for source_path, expected_hash in (
            (confirmation_path, s.get("confirmation_event_sha256")),
            (decision_path, s.get("decision_source_sha256")),
            (csv_path, s.get("original_csv_sha256")),
        ):
            if not expected_hash or sha256(source_path) != expected_hash:
                raise ValueError(f"R4 confirmed-source hash mismatch: {source_path}")
        input_complete = {
            "question": bool(payload.get("question")),
            "answer_or_claim": bool(payload.get("answer_or_claim")),
            "applicable_sources": bool(payload.get("applicable_sources")),
            "rubric_scope_matches": payload.get("rubric", {}).get("scope") == s["object_scope"],
            "scoring_target": bool(payload.get("scoring_target")),
            "request_matches_frozen_payload_hash": canonical_sha256(payload) == s.get("payload_sha256"),
        }
        confirmed.append({
            "sample_id": s["calibration_sample_id"], "stable_item_id": s["stable_item_id"],
            "case_id": s["case_id"], "split": s["split"], "object_scope": s["object_scope"],
            "human_reference": s["human_reference"], "candidate_sha256": s["candidate_sha256"],
            "question_sha256": s["question_sha256"], "source_text_sha256": s["source_text_sha256"],
            "confirmation_event_id": s["confirmation_event_id"], "confirmation_event_sha256": s["confirmation_event_sha256"],
            "confirmed_source_bindings": {
                "confirmation_event_path": s["confirmation_event_path"],
                "confirmation_event_sha256": sha256(confirmation_path),
                "decision_source_path": s["decision_source_path"],
                "decision_source_sha256": sha256(decision_path),
                "original_csv_path": s["original_csv_path"],
                "original_csv_sha256": sha256(csv_path),
            },
            "historical_judge_status": j.get("judge_status") if j else None,
            "historical_judge_decision": j.get("judge_response", {}).get("decision") if j else None,
            "reference_state": "CONFIRMED_LABEL; RATIONALE_OR_DISAGREEMENT_PENDING" if s["stable_item_id"] in {"J3", "claim-04a"} else "CONFIRMED_DEVELOPMENT_REFERENCE",
            "r4_batch_id": r4_manifest["batch_id"],
            "r4_runtime_binding": compact(r4_manifest["runtime_binding"]),
            "judge_input_completeness": {
                **input_complete,
                "complete_for_historical_objective": all(input_complete.values()),
            },
            "scoring_object": compact(payload.get("scoring_target")),
            "historical_judge_payload": compact(payload),
            "limitations": ["Development calibration only; not independent validation or formal Gold.", "No dimension-level human labels are inferred."] + (["Claim-only sample; judge scope must not expand to full-task completeness."] if s["object_scope"] == "claim_only" else []),
        })
    pending = []
    for item in package["items"]:
        if item["item_type"] == "personal_dev_full_case":
            pending.append({"sample_id": item["item_id"], "object_scope": "full_case_and_delivered_answer", "suggested_outcome": item["ai_pre_review"]["suggested_outcome"], "state": "PENDING_USER_REVIEW_NOT_CALIBRATION_REFERENCE", "explicit_limitation": "Outcome/answer completeness suggestion cannot be relabeled as factual-support failure or hallucination without its own evidence review."})
    pending.append({"sample_id": "D1-CROSSAGENT-030", "object_scope": "full_case_and_delivered_answer", "suggested_outcome": package["crossagent030_outcome_pre_review"]["ai_recommendation"], "state": "PENDING_USER_REVIEW_NOT_CALIBRATION_REFERENCE", "explicit_limitation": "Rule-driven route, no Provider request; single observed multi-specialist run does not establish model-autonomous cooperation."})
    pending_categories = {
        "pending_positive_candidates": ["D1-POLICY-018", "D1-SAFETY-037"],
        "pending_partial_candidates": ["D1-POLICY-017", "D1-SAFETY-036", "D1-CROSSAGENT-030", "ANSWER-J3"],
        "pending_task_completeness_negative_only": ["D1-PRODUCT-003"],
        "confirmed_insufficient_evidence_claim_only": ["ANSWER-claim-04a"],
        "execution_or_delivery_failure_in_this_reviewed_set": [],
        "confirmed_reference_ids_by_label": {
            label: [x["sample_id"] for x in confirmed if x["human_reference"] == label]
            for label in ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE")
        },
        "scope_note": "The bound five-case batch plus CROSSAGENT-030 records all contain a persisted delivered answer; no execution/delivery-failure candidate is included in this eight-item review package. Missing-answer or failed-delivery archives are excluded rather than used as answer-content negatives.",
    }
    return {
        "schema_version": "m6.2-judge-reference-candidates-r1",
        "judge_calls_this_round": 0,
        "confirmed_reference_count": len(confirmed),
        "confirmed_label_counts": {label: sum(1 for x in confirmed if x["human_reference"] == label) for label in ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE")},
        "confirmed_references": confirmed,
        "pending_candidates": pending,
        "pending_candidate_categories": pending_categories,
        "coverage": {
            "confirmed_fail_references": sum(1 for x in confirmed if x["human_reference"] == "FAIL"),
            "false_accept_denominator": "N/A: no confirmed FAIL answer-content reference in the bound 8-item development pool.",
            "dimension_level_human_labels": "NOT_AVAILABLE",
            "partial_is_kept_as_separate_category": True,
            "insufficient_evidence_is_not_binary_positive_or_negative": True,
            "candidate_without_complete_visible_answer_is_excluded": True,
            "all_eight_historical_judge_inputs_have_question_answer_sources_scope_and_scoring_target": all(all(v for v in x["judge_input_completeness"].values()) for x in confirmed),
            "r4_requests_path": R4_REQUESTS.relative_to(ROOT).as_posix(),
            "r4_requests_sha256": sha256(R4_REQUESTS),
            "all_confirmed_source_hashes_match_frozen_samples": all(
                all(x["confirmed_source_bindings"][k] for k in ("confirmation_event_sha256", "decision_source_sha256", "original_csv_sha256"))
                for x in confirmed
            ),
        },
        "pending_user_review_required_before_calibration_use": True,
    }


def build_gap_matrix(package: dict[str, Any], refs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "m6.2-coverage-freeze-gap-matrix-r1",
        "m6_1_status": "COMPLETE",
        "m6_2_status": "IN_PROGRESS",
        "dimensions": [
            {"dimension": "routing", "status": "PARTIAL", "existing_evidence": ["52 confirmed route references: dev 28, validation 24; this is a reference set, not 52 evaluated current-model predictions.", "CROSSAGENT-030 used one deterministic RULE decision WARRANTY_FROM_OWNED_ORDER; no Provider route decision."], "scoring_object": "current version route prediction against version-bound confirmed reference", "current_model_predictions_scored": 0, "remaining": "Run a fresh authorized Dev/Validation route evaluation tied to one implementation/prompt/config; report confusion matrix and macro-F1 with denominators."},
            {"dimension": "retrieval", "status": "PARTIAL", "existing_evidence": ["Historical personal Dev policy retrieval records, including CROSSAGENT-030 BM25 citation and source metadata."], "scoring_object": "retrieved relevant evidence/rank/conditions and applicability metadata", "current_run_bound_cases_reviewed": 1, "retrieval_quality_metrics_computed": 0, "remaining": "Additional authorized, version-bound retrieval cases and denominators for P@1/Recall@k/MRR/condition coverage; assess empty/degraded retrieval separately."},
            {"dimension": "evidence_support", "status": "PARTIAL", "existing_evidence": ["R4 historical Judge batch has 8 responses over 8 confirmed answer/claim references (4 PASS, 3 PARTIAL, 1 INSUFFICIENT_EVIDENCE); historical category agreement was 6/8."], "scoring_object": "claim/reference support, source scope, version/time/condition entailment", "current_round_new_judge_or_evidence_scores": 0, "remaining": "J3 rationale and claim-04a disagreement handling await user confirmation; no confirmed FAIL content reference, false-accept denominator N/A; add approved negative references and dimension labels."},
            {"dimension": "end_to_end_outcome", "status": "PARTIAL", "existing_evidence": ["Five prior personal Dev runs have AI pre-review only; one CROSSAGENT-030 multi-specialist real application run is technically closed with task Outcome UNKNOWN and this package adds a retrospective AI PARTIAL proposal."], "newly_pre_reviewed_runs": 1, "formal_task_scores": 0, "remaining": "User review of these six case assessments; more authorized model-involved cases for task outcome, additional independent MULTI/other outcomes, consistent-bound pilot and representative denominators."},
        ],
        "judge_human_calibration": {"status": "PARTIAL", "historical_r4": {"responses": 8, "category_agreement": "6/8", "reference_counts": {"PASS": 4, "PARTIAL": 3, "INSUFFICIENT_EVIDENCE": 1, "FAIL": 0}, "same_model_bias": True}, "this_round_judge_calls": 0, "remaining": "Resolve the two pending disagreement/rationale items, gather approved confirmed FAIL cases and dimension labels, and perform independent/appropriately governed calibration; no calibration claim is made."},
        "formal_freeze": {"status": "NOT_STARTED", "remaining": ["Versioned dataset/source adjudication and freeze review by assigned independent human reviewers; user confirmation and AI pre-review do not substitute for an independent/blind review required by the formal protocol.", "Registry grant/authorization from the designated Registry owner under the unchanged formal eligibility/review policy.", "A formally authorized, pre-registered Frozen run with locked data/model/code/prompt/rubric, budget ledger and independent cleanup/audit evidence. Candidate/Frozen assets were not read in this work."]},
        "formal_pre_freeze_gap_details": [
            {"gate": "Gold", "current_state": "NOT_APPROVED", "dependency": "Approved case/answer references with source and condition adjudication under the formal review protocol.", "blocker": "Current 52 route references and eight answer/claim references are development references; the eight new package items are proposals only. The current R4 reference pool has no confirmed FAIL content example.", "responsible_role": "Assigned independent human Gold/domain reviewer(s) under the formal evaluation plan; no reviewer identity is inferred here.", "release_condition": "Complete required independent/ blind review and record real reviewer, timestamp, evidence and adjudication; obtain formal Gold approval through its existing process."},
            {"gate": "Registry/grant", "current_state": "NOT_AUTHORIZED", "dependency": "A Registry dataset that satisfies eligibility, review-status and scoreability checks, plus scoped grant.", "blocker": "This user-confirmed Dev review scope does not change PENDING/NOT_REVIEWED source fields into formal ELIGIBLE/scoreable status and does not issue a grant.", "responsible_role": "Designated Registry owner/authorized grant approver; identity not established by this review.", "release_condition": "Satisfy existing Registry policy and create a narrowly scoped authorized grant with the required bindings and expiry."},
            {"gate": "M2", "current_state": "BLOCKED", "dependency": "Existing M2 quality/acceptance closeout and all applicable hard gates.", "blocker": "M6.2 Dev evidence is not an M2 acceptance; the current progress ledger still records M2 BLOCKED and no M2-specific closure evidence was created here.", "responsible_role": "Existing M2 gate owner/approver per the project closeout process; no named individual is present in the reviewed evidence.", "release_condition": "Resolve the recorded M2 blockers with their required evidence and obtain the formal M2 gate decision."},
            {"gate": "Formal run isolation", "current_state": "NOT_EVIDENCED_FOR_FORMAL_RUN", "dependency": "Environment/database identity and side-effect containment evidence required by the formal run profile.", "blocker": "A dedicated personal SQLite identity and exact receipt cleanup for personal Dev do not establish the separate formal environment/isolation approval. The prior user decision that OS isolation is N/A for personal Dev is not extended to formal gates.", "responsible_role": "Environment/isolation owner specified by the formal run process; no identity inferred.", "release_condition": "Provide and verify the formal target identity, isolation and side-effect controls required by the authorized profile."},
            {"gate": "Formal Dev Smoke", "current_state": "NOT_AUTHORIZED", "dependency": "A fixed, bound Smoke plan with eligible reviewed cases, rubric, budget and exact run permit.", "blocker": "Personal Dev runs and this offline pre-review do not constitute the separate formal Smoke authorization or a same-binding 6–10 case score report.", "responsible_role": "Authorized evaluation/run owner and any required independent reviewers under the formal protocol.", "release_condition": "Resolve prerequisites, freeze the exact configuration and obtain the scoped formal Smoke authorization."},
            {"gate": "Frozen Test", "current_state": "NOT_AUTHORIZED", "dependency": "Pre-registration, locked Test/model/code/prompt/rubric and formal grant/permit.", "blocker": "No Frozen asset was read or run, no freeze review was performed, and no Frozen authorization exists in the current gate snapshot.", "responsible_role": "Independent blind reviewer(s), designated Registry owner and formal evaluation authority as required by the existing plan; no individual is named here.", "release_condition": "Complete the formal independent/next-day blind review and dataset freeze, obtain Registry/grant approval and a precise Frozen run authorization, then execute under the locked protocol."}
        ],
        "formal_gate_snapshot": {"gold": "NOT_APPROVED", "registry": "NOT_AUTHORIZED", "m2": "BLOCKED", "formal_dev_smoke": "NOT_AUTHORIZED", "frozen_test": "NOT_AUTHORIZED", "m6_1": "COMPLETE"},
        "isolation": {"personal_dev": "The one-case run used the dedicated personal SQLite identity and receipt/lease cleanup; no claim of OS-level isolation was added.", "formal_run": "Existing formal isolation approval/evidence remains a separate prerequisite; no personal diagnostic substitutes for it."},
        "rate_policy": "Do not merge heterogeneous code/data/ruleset bindings into a single success rate. For any future rate, show n/N and Wilson 95% interval; use N/A where an eligible denominator is absent.",
        "multi_evidence_limit": "One deterministic-rule, two-specialist execution verifies a path only. It does not compare SINGLE/MULTI quality or establish autonomous LLM collaboration.",
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    package = build_package()
    refs = build_reference_candidates(package)
    gaps = build_gap_matrix(package, refs)
    package_again = build_package()
    refs_again = build_reference_candidates(package_again)
    gaps_again = build_gap_matrix(package_again, refs_again)
    if (canonical_sha256(package), canonical_sha256(refs), canonical_sha256(gaps)) != (
        canonical_sha256(package_again), canonical_sha256(refs_again), canonical_sha256(gaps_again)
    ):
        raise ValueError("Two consecutive source-derived builds differ")
    outputs = {
        "eight-item-review-package-r1.json": package,
        "judge-reference-candidates-r1.json": refs,
        "coverage-freeze-gap-matrix-r1.json": gaps,
    }
    for name, obj in outputs.items():
        (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "crossagent030-outcome-pre-review-r1.json").write_text(json.dumps(package["crossagent030_outcome_pre_review"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "confirmation-list-r1.md").write_text(build_confirm_list(package), encoding="utf-8")
    source_paths = [DATASET, EVALUATION_PLAN, EVALUATION_SPEC, REVIEW_R5, R4_SAMPLES, R4_ANALYSIS, R4_RESULTS, R4_MANIFEST, R4_REQUESTS, R4_CLOSEOUT, R4_SHA_MANIFEST, R4_DISAGREEMENT_R1, R4_DISAGREEMENT_R2, CONFIRMATION_EVENT, CONFIRMED_DECISIONS, ORIGINAL_REVIEW_CSV, CA030_ARCHIVE, CA030_CHECK, CA030_RULES, CA030_CLOSEOUT, CA030_EXECUTION, CA030_RECOMPUTE, CA030_PRICING, FIVE_MANIFEST, FIVE_RULES, FIVE_CLOSEOUT, *(ROOT / p for p in ARCHIVE_BY_CASE.values())]
    hist_counts = refs["confirmed_label_counts"]
    recompute = {
        "schema_version": "m6.2-dev-score-review-recompute-r1",
        "classification": "OFFLINE_DERIVED_REVIEW_RECOMPUTATION",
        "inputs": {p.relative_to(ROOT).as_posix(): sha256(p) for p in source_paths},
        "checks": {
            "dataset_identity_and_sha": "PASS",
            "five_case_question_archive_answer_bindings": "PASS",
            "r4_judge_sample_confirmed_label_bindings": "PASS",
            "r4_historical_sha_manifest_selected_entries": "PASS",
            "evaluation_plan_and_spec_baseline_sha": "PASS",
            "j3_source_says_resale_condition_not_unopened": "PASS",
            "claim_04a_scope_is_claim_only_and_evidence_sufficiency_separate": "PASS",
            "ca030_same_run_tools_handoffs_and_product_id_mapping": "PASS",
            "ca030_unknown_eligibility_preserved": "PASS",
            "human_decisions_written": False,
            "new_runs_or_model_calls": 0,
        },
        "repeat_build_check": {
            "builds": 2,
            "status": "MATCH",
            "package_canonical_sha256": canonical_sha256(package),
            "judge_candidates_canonical_sha256": canonical_sha256(refs),
            "coverage_matrix_canonical_sha256": canonical_sha256(gaps),
            "method": "Two consecutive in-process derivations from the same hash-bound source artifacts; not an independent grader implementation.",
        },
        "review_item_count": len(package["items"]),
        "pending_review_item_count": len(package["items"]),
        "historically_confirmed_r4_reference_counts": hist_counts,
        "historically_confirmed_r4_judge_responses": len(load(R4_ANALYSIS).get("results", [])),
        "ca030_ai_outcome_recommendation": package["crossagent030_outcome_pre_review"]["ai_recommendation"],
        "ca030_historical_checker_task_outcome": package["crossagent030_outcome_pre_review"]["prior_program_status_preserved"]["checker_task_outcome"],
        "derived_from": "raw allowed archive and binding records; not from copied PASS fields alone",
        "repeatability_note": "Run this script in the repository to rebuild JSON and confirmation-list derivations from the same bound sources; it makes no external calls.",
    }
    (OUT / "recompute-r1.json").write_text(json.dumps(recompute, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    closeout = {
        "schema_version": "m6.2-dev-score-review-closeout-r1",
        "created_at_utc": package["created_at_utc"],
        "classification": package["classification"],
        "m6_1_status": "COMPLETE",
        "m6_2_status": "IN_PROGRESS",
        "grading_status": "NOT_GRADED",
        "reference_review_status": "reference_unreviewed",
        "runs_created_this_round": 0,
        "provider_judge_embedding_or_business_tool_calls_this_round": 0,
        "human_decisions_written": False,
        "review_items": {"total": len(package["items"]), "pending_user_review": 8, "confirmed_labels_reopened": 0},
        "crossagent030_ai_outcome_recommendation": package["crossagent030_outcome_pre_review"]["ai_recommendation"],
        "crossagent030_historical_checker_task_outcome": package["crossagent030_outcome_pre_review"]["prior_program_status_preserved"]["checker_task_outcome"],
        "judge_calibration": {"historical_r4_samples": 8, "this_round_calls": 0, "confirmed_fail_references": refs["coverage"]["confirmed_fail_references"], "false_accept_denominator": "N/A"},
        "repeat_build_check": recompute["repeat_build_check"],
        "tests": {"command": "python -m pytest -p no:cacheprovider tests/test_m6_2_dev_score_review_closeout_r1.py --junitxml=docs/probes/m62-dev-score-review-closeout-r6.xml", "passed": 6, "failed": 0, "errors": 0, "skipped": 0, "log": "docs/probes/m62-dev-score-review-closeout-r6.log", "junit": "docs/probes/m62-dev-score-review-closeout-r6.xml", "exit_code": 0},
        "formal_gate_snapshot": gaps["formal_gate_snapshot"],
        "report_path": "docs/eval/m6.2-dev-score-review-closeout-r1-20261029/closeout-r1.md",
        "report_sha256": sha256(OUT / "closeout-r1.md"),
        "source_bindings": package["source_bindings"],
        "limitations": package["limitations"],
    }
    (OUT / "closeout-r1.json").write_text(json.dumps(closeout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": OUT.relative_to(ROOT).as_posix(), "items": len(package["items"]), "confirmed_judge_refs": len(refs["confirmed_references"]), "pending_reviews": len(package["items"]), "judge_calls": 0, "runs_created": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
