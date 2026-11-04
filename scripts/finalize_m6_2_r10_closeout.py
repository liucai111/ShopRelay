"""Build the additive R18 review, hold-decision, retrieval and exit artifacts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
R10 = Path('docs/eval/m6.2-r324-40case-personal-dev-batch-r10-four-first-runs-20261030')

REPORTS = R10 / "reports"
R9_REPORTS = Path("docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports")
R17_HOLDS = R9_REPORTS / "12-case-offline-remediation-r17.json"
DATA = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")


def write_md(path: Path, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def collect_runs(package: dict[str, Any]) -> list[dict[str, Any]]:
    return (list(package["historical_runs"]) + list(package["r9s4_runs"])
            + [package["policy018_recovery_run"]] + list(package["r10_first_runs"]))


def build_holds() -> dict[str, Any]:
    from scripts.derive_m6_2_r10_score_closeout import read, sha
    source = read(R17_HOLDS)
    selected = {"D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022", "D1-POLICY-015",
                "D1-POLICY-016", "D1-AFTERSALES-024", "D1-CROSSAGENT-029", "D1-CROSSAGENT-033"}
    recommendations = {
        "D1-ORDER-008": ("Shipment steward decision required", "Use the existing steward pathway to resolve which authoritative shipment/disposition source applies; retain empty Shipment as UNKNOWN and do not infer non-shipment or cancellation eligibility."),
        "D1-ORDER-010": ("Shipment steward decision required", "Confirm the authoritative shipment evidence and precedence for the claimed pickup/status conflict; an empty Shipment row is not evidence of no shipment."),
        "D1-AFTERSALES-022": ("Shipment steward decision required", "Provide an authoritative shipment/disposition source and scope; preserve fulfillment and cancellation eligibility as UNKNOWN until then."),
        "D1-POLICY-015": ("Contract/data owner decision", "Either provide an approved coupon_code or catalog association through user input/allowed upstream result, or revise the task contract and allowed capability through review; do not guess a coupon identifier."),
        "D1-POLICY-016": ("Contract/data owner decision", "Provide a unique product_id from the query or a permitted upstream order-item result, or revise the task contract; do not guess or add search capability."),
        "D1-AFTERSALES-024": ("Business policy decision", "Decide whether paid_at is a universal return-eligibility prerequisite, only a refund-calculation prerequisite, or a case-specific condition. Recommended: keep return eligibility separate from missing payment facts and require payment data only for refund calculation unless policy explicitly says otherwise. Business owner must decide."),
        "D1-CROSSAGENT-029": ("Contract/data owner decision", "Supply an explicit product identity in the user request or a legal upstream record within the existing case scope; otherwise revise the reviewed task/ability contract. Do not expand the allowlist or substitute another case."),
        "D1-CROSSAGENT-033": ("Case-specific independent review hold", "Obtain the required independent electrical-safety review and independent business/policy review for this case, record genuine identities and decisions, then request a separate exact execution authorization. Do not generalize this signature requirement to other cases."),
    }
    rows = []
    case_order = ["D1-ORDER-008", "D1-ORDER-010", "D1-AFTERSALES-022", "D1-POLICY-015",
                  "D1-POLICY-016", "D1-CROSSAGENT-029", "D1-AFTERSALES-024", "D1-CROSSAGENT-033"]
    for case in case_order:
        src = next(x for x in source["cases"] if x["case_id"] == case)
        category, recommendation = recommendations[case]
        rows.append({
            "case_id": case,
            "prior_status": src["status"],
            "current_status": src["status"],
            "prior_reason": src["reason"],
            "source": src["source"],
            "source_sha256": sha(Path(src["source"])) if (ROOT / src["source"]).is_file() else None,
            "evidence": src.get("evidence"),
            "decision_needed": category,
            "recommended_disposition": recommendation,
            "current_run_authorization": "NONE",
            "hold_released": False,
        })
    return {"schema_version": "m6.2-r10-remaining-eight-dispositions-r1",
            "classification": "PERSONAL_DEV_DISPOSITION_RECOMMENDATIONS_NOT_AUTHORIZATION",
            "source_path": str(R17_HOLDS).replace("\\", "/"), "source_sha256": sha(R17_HOLDS),
            "case_count": len(rows), "hold_release_count": 0, "cases": rows}


def build_retrieval(package: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in collect_runs(package):
        refs = item.get("same_run_policy_sources") or []
        tool_refs = item.get("actual_tool_evidence") or []
        archived_evidence = item.get("evidence") or []
        policy_events = [x for x in archived_evidence if isinstance(x, dict) and
                         x.get("event_type") in {"policy_retrieval_started", "policy_retrieval_finished"}]
        policy_refs = [x for x in tool_refs if isinstance(x, dict) and
                       (x.get("tool") == "search_knowledge" or x.get("event_type", "").startswith("policy_retrieval"))]
        if not refs and not policy_refs and not policy_events:
            continue
        evidence_ids = []
        for ref in refs:
            if isinstance(ref, dict):
                evidence_ids.append(ref.get("evidence_id") or ref.get("chunk_id") or ref.get("source_id") or ref.get("citation_id"))
        evidence_ids.extend(x.get("result_ref") for x in policy_refs if x.get("result_ref"))
        for event in policy_events:
            evidence_ids.extend(event.get("payload", {}).get("evidence_ids", []))
        evidence_ids = list(dict.fromkeys(x for x in evidence_ids if x))
        rows.append({
            "case_id": item["case_id"], "run_id": item["run_id"],
            "binding": item.get("binding"), "question": item.get("question"),
            "source_archive": item.get("archive_path"), "source_archive_sha256": item.get("archive_sha256"),
            "actual_policy_references_or_retrieval_refs": refs or policy_refs or policy_events,
            "observed_evidence_ids": evidence_ids,
            "ranked_results_status": "PARTIAL_OR_NOT_CAPTURED_AS_COMPLETE_RANKING",
            "relevance_labels": [], "annotator": None, "annotated_at_utc": None,
            "review_status": "PENDING_INDEPENDENT_DEV_ANNOTATION",
            "note": "Observed citations/retrieval refs are not a complete ranked candidate list and do not establish relevance labels."})
    return {"schema_version": "m6.2-dev-retrieval-annotation-set-r1",
            "classification": "DEV_ANNOTATION_WORKSHEET_NOT_QRELS",
            "source_package": "reports/dev-score-review-package-r18.json",
            "source_package_sha256": None, "rows": rows,
            "metric_status": {"recall_at_k": "N/A", "mrr": "N/A",
                "missing": ["complete per-query ranked candidate list", "reviewed relevance judgments/qrels", "defined cutoffs and query denominator"]},
            "minimum_collection_plan": [
                "For each authorized query capture the full ordered retriever candidate list and retriever configuration/version before answer filtering.",
                "Have an eligible reviewer label relevant/non-relevant for all retrieved candidates plus a reviewed miss pool; retain provenance and adjudications.",
                "Freeze k values, query inclusion rules and relevance threshold before computing Recall@k/MRR; do not infer relevance from answer citations."],
            "row_count": len(rows)}


def build_judge_candidates(package: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in collect_runs(package):
        derived = item.get("derived_score_r18") or {}
        label = item.get("task_outcome_recommendation") or derived.get("task_outcome_recommendation")
        candidate = item.get("rejected_candidate_audit")
        answer = item.get("delivered_answer")
        candidate_text = (candidate or {}).get("candidate_answer_redacted") if isinstance(candidate, dict) else None
        if answer:
            answer_object, object_type = answer, "COMPLETE_DELIVERED_ANSWER"
        elif candidate_text:
            answer_object, object_type = candidate_text, "REJECTED_CANDIDATE_NOT_DELIVERED"
        else:
            answer_object, object_type = None, "NO_SCORABLE_ANSWER_OBJECT"
        if not label and answer_object is None:
            continue
        if item["case_id"] == "D1-CROSSAGENT-028" and label == "FAIL":
            dimension = "required_task_completion"
            caveat = "Required get_order was not executed. This task FAIL is not an answer-content falsehood label."
        elif label == "FAIL" and answer_object is None:
            dimension = "delivery_or_task_outcome"
            caveat = "Use only for delivery/task failure; candidate content is unscorable without a candidate."
        elif object_type == "REJECTED_CANDIDATE_NOT_DELIVERED":
            dimension = "candidate_answer_semantics"
            caveat = "Candidate is separable from failed user delivery. Score candidate content only; task delivery remains FAIL."
        else:
            dimension = "visible_answer_task_completeness"
            caveat = "Dev provisional label; not a confirmed gold label and not interchangeable with fact-support labels."
        rows.append({
            "case_id": item["case_id"], "run_id": item["run_id"], "binding": item.get("binding"),
            "object_type": object_type, "question": item.get("question"),
            "answer_or_candidate": answer_object, "dimension_proposal": dimension,
            "suggested_dev_label": label or "INSUFFICIENT_EVIDENCE",
            "source_archive": item.get("archive_path"), "source_archive_sha256": item.get("archive_sha256"),
            "evidence_refs": item.get("actual_tool_evidence") or item.get("same_run_policy_sources") or [],
            "human_reference_status": "PENDING_EXACT_RUN_REVIEW",
            "judge_eligible_now": False,
            "reason_and_scope": caveat})
    return {"schema_version": "m6.2-dev-judge-candidate-register-r1",
            "classification": "AI_ASSISTED_DEV_CANDIDATES_NOT_CONFIRMED_REFERENCES",
            "judge_calls_this_round": 0,
            "candidates": rows,
            "coverage": {"task_completeness": sorted({x["suggested_dev_label"] for x in rows if x["dimension_proposal"] == "visible_answer_task_completeness"}),
                "pass_partial_fail_candidates": sorted({x["suggested_dev_label"] for x in rows}),
                "confirmed_fail_content_reference": "NOT_ESTABLISHED_BY_TASK_FAILS; require an exact answer-content-reviewed negative",
                "insufficient_evidence_candidates": sum(x["suggested_dev_label"] == "INSUFFICIENT_EVIDENCE" for x in rows),
                "limitation": "Execution, delivery, semantic completeness and fact-support labels are distinct; do not mix denominators."}}


def build_exit(metrics: dict[str, Any], holds: dict[str, Any], retrieval: dict[str, Any], judge: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "m6.2-exit-matrix-r18",
        "m6_1_status": "COMPLETE", "m6_2_status": "IN_PROGRESS",
        "classification": "PERSONAL_DEV_NONFORMAL_NOT_GRADED",
        "rows": [
            {"task": "Dev outcome execution and scoring", "status": "PARTIAL",
             "evidence": "28 R17 historical Runs plus four new R10 Runs (32/40 coverage), across eight bindings; four R10 outcomes have AI review suggestions but no user confirmation.",
             "remaining": "Review exact-Run scoring suggestions; eight cases remain held/blocked and do not enter this run set."},
            {"task": "Route/retrieval/evidence metrics", "status": "PARTIAL",
             "evidence": "Per-binding route sources/tool evidence and same-run citations are indexed; R10 route source is RULE for 4/4 and Provider participation is 0/4.",
             "remaining": f"Retrieval ranking metrics are {retrieval['metric_status']['recall_at_k']}; capture complete rankings and independently judged relevance labels. Do not call citations Recall/MRR."},
            {"task": "Judge calibration", "status": "PARTIAL",
             "evidence": "Prior R4 development calibration remains 8 format-valid, 6/8 exact-category agreement; false-accept denominator remains 0 because no confirmed FAIL content reference.",
             "remaining": "Confirm dimension-matched Dev candidates, especially a content-reviewed negative; run only under separate finite Judge authorization. Current turn Judge calls=0."},
            {"task": "Remaining case disposition", "status": "BLOCKED_BY_SPECIFIC_DECISIONS",
             "evidence": "Eight case-specific rows retained from R17 disposition record; no hold was released.",
             "remaining": "Resolve three Shipment steward decisions; provide/approve parameter-source contracts for POLICY-015/016 and CROSSAGENT-029; decide AFTERSALES-024 paid_at rule; obtain CROSSAGENT-033's case-specific independent reviews."},
            {"task": "Formal freeze, Frozen Test and final report", "status": "NOT_STARTED",
             "evidence": "No formal approval/Gate changed. Personal Dev R10 is NOT_GRADED/reference_unreviewed.",
             "remaining": "Prepare §3.4 independent annotation and next-day blind review, complete exact formal freeze review/authorization, run only authorized Frozen Test, then issue final evaluation report."}
        ],
        "run_coverage": {"historical_r4_to_r9_run_count": 28, "new_r10_run_count": 4,
            "unique_run_count": 32, "case_count": 40, "held_or_blocked_count": holds["case_count"],
            "r9_post_stop_deviation_cases": ["D1-SAFETY-039", "D1-SAFETY-040"],
            "deviation_in_compliant_continuation_denominator": False},
        "metrics_by_binding": metrics["groups"],
        "judge_calibration_preparation": {"prior_r4_exact_agreement": "6/8", "current_calls": 0,
            "current_register_candidate_count": len(judge["candidates"])}}


def main() -> None:
    from scripts.derive_m6_2_r10_score_closeout import build_outputs, read, write, sha
    base = build_outputs()
    pkg_path = REPORTS / "dev-score-review-package-r18.json"
    metrics_path = REPORTS / "dev-metrics-r18.json"
    ledger_path = REPORTS / "40-case-coverage-ledger-r18.json"
    closeout_path = REPORTS / "closeout-r18.json"
    pkg = read(pkg_path)
    metrics = read(metrics_path)
    holds = build_holds()
    retrieval = build_retrieval(pkg)
    retrieval["source_package_sha256"] = sha(pkg_path)
    judge = build_judge_candidates(pkg)
    exit_matrix = build_exit(metrics, holds, retrieval, judge)
    write(REPORTS / "remaining-8-case-disposition-r18.json", holds)
    write(REPORTS / "retrieval-annotation-dev-r1.json", retrieval)
    write(REPORTS / "judge-calibration-dev-candidates-r1.json", judge)
    write(REPORTS / "m6.2-exit-matrix-r18.json", exit_matrix)

    md = ["# M6.2 R10 收口与剩余差距 R18", "",
          "性质：`PERSONAL_DEV`、`PROVISIONAL_DEV_CHECKS`、`reference_unreviewed`、`NOT_GRADED`。本轮没有Provider/Judge/Embedding调用。M6.1保持COMPLETE；未改变正式Gate。",
          "", "## R10 四案真实执行", "",
          "R10-R5四案均各建一个Run、终态SUCCEEDED，全部为RULE路由与Agent应用路径；Provider请求0、tokens 0、只读调用11（2/2/3/4），本地估算USD 0，账单不适用。所有Run/Trace/工具结果关联通过，预算结清、receipt精确清理、Pending Action为0。规则路径的运行不表述为LLM自主成功。",
          "", "| Case | Run | 技术 | 交付 | Task Outcome AI暂定 | 核心复核理由 |", "|---|---|---|---|---|---|"]
    for row in pkg["r10_first_runs"]:
        md.append(f"| {row['case_id']} | `{row['run_id']}` | {row['technical_execution']} | {row['answer_delivery_status']} | **{row['task_outcome_recommendation']}** | {row['ai_rationale']} |")
    md.extend(["", "同Run节点/交接证据："])
    for row in pkg["r10_first_runs"]:
        targets = [f"{handoff.get('source_agent')}→{handoff.get('target_agent')}" for handoff in row["actual_handoffs"]]
        md.append(f"- {row['case_id']}：同Run `agent_started`事件{row['actual_agent_started_events']}个（{', '.join(row['actual_agent_names_from_same_run_events'])}）；handoff {len(targets)}个（{'; '.join(targets)}）。")
    md.append("执行摘要存在投影不一致：CLI摘要和同Run原始事件/交接证明Agent节点实际启动，但execution-state批次布尔字段`agent_started`为false。评分依据是逐Run原始`agent_started`事件及Trace/handoff；不改写历史状态文件。批次布尔投影需另行修正，不能单独作为节点证据。")
    md.extend(["", "- PRODUCT-005：目录记录支持材料，政策引用支持18个月及购买/激活核验条件；未断言个人资格。",
        "- AFTERSALES-019：引述21天未拆封配件规则，但没有确认品类/状态，且把未知订单状态打印成`None`，建议PARTIAL。",
        "- AFTERSALES-020：同Run退货事件为APPROVED，引用政策明确申请提交不等于批准/付款；回答显示未知订单状态`None`，且缺少基于未知付款字段的下一步说明，建议PARTIAL。",
        "- CROSSAGENT-032：订单行product_id与下游商品查询一致，政策引用要求人工审核且核实商品状态；回答未明确指出条件未知并请求确认，建议PARTIAL。",
        "", "R10启动记录：R1按旧准入投影拒绝；R2暴露本地`application_admission_path` KeyError，未创建Run/Provider请求；R3/R4绑定检查失败，未创建Run/Provider请求。R4的新准备授权因profile类型不匹配在执行前关闭；R5使用新的正确绑定授权后才执行四案。以上启动迭代均保留，未重放已创建的Run或继承旧关闭授权。",
        "", "## 28案评分预审和指标分母", "",
        "R18从R17绑定原档生成派生评分包。每条均保留原问题/答案、Run、绑定、证据及待审字段。终态运行没有实际交付时，Task Outcome计FAIL；有受控拒绝候选时，候选语义单列；确实无候选时仅候选内容UNSCORABLE。CROSSAGENT-028必需get_order未执行为Task FAIL，即使语义建议PARTIAL。POLICY-014已保存受控候选但未成功交付，交付/Task FAIL与候选语义审查分列。SAFETY-038原档确无候选和拒绝审计，交付FAIL、候选UNSCORABLE。",
        "", "SAFETY-039/040是R9锁定STOP后的偏差观察：保留结果及用量，但不纳入合规续跑分母。R18不改历史STOP。R9s4的14项观察包含偏差后两案；合规序列是12案，Task Outcome为PASS 4、PARTIAL 4、FAIL 4，PASS端点4/12、Wilson 95%约13.8%–60.9%。此前4/11是旧严格端点排除一个复合交付失败的历史口径，不作为当前Task Outcome分母。其余指标按实现/规则/数据绑定分组，不合并统一成绩。",
        "", "| Binding | Runs | Task PASS/PARTIAL/FAIL | PASS n/N | Wilson 95% | Route source | Provider / tokens / local estimate |", "|---|---:|---|---:|---|---|---|"])
    for key, group in metrics["groups"].items():
        task = group["task_outcome_ai_prereview"]
        ci = task["wilson95"]
        ci_s = "N/A" if ci is None else f"{ci['lower']:.3f}–{ci['upper']:.3f}"
        sums = group["known_usage_sums"]
        md.append(f"| `{key}` | {group['runs']} | {task['counts']} | {task['successes']}/{task['denominator']} | {ci_s} | {group['route_decision_source_counts']} | {sums['provider_requests']} / {sums['tokens']} / ${sums['local_estimated_usd']} |")
    md.extend(["", "程序二值指标（只对相应程序字段明确为PASS/FAIL的记录计算；UNKNOWN、NOT_RECORDED、NOT_APPLICABLE分别排除，不补零）：", "",
        "| Binding | 指标 | PASS n/N | Wilson 95% | Unknown/未记录 | 不适用 |", "|---|---|---:|---|---:|---:|"])
    for key, group in metrics["groups"].items():
        for name, value in group["binary_program_indicators"].items():
            ci = value["wilson95"]
            ci_s = "N/A" if ci is None else f"{ci['lower']:.3f}–{ci['upper']:.3f}"
            md.append(f"| `{key}` | {name} | {value['successes']}/{value['denominator']} | {ci_s} | {value['excluded_unknown_or_unrecorded']} | {value['not_applicable']} |")
    md.extend(["", "## 剩余8案处置", "", "| Case | 原状态 | 必需决定/动作 | 推荐及边界 |", "|---|---|---|---|"])
    for row in holds["cases"]:
        md.append(f"| {row['case_id']} | {row['current_status']} | {row['decision_needed']} | {row['recommended_disposition']} |")
    md.extend(["", "## 检索补证与Judge校准准备", "",
        f"检索工作表列出{retrieval['row_count']}条存在政策检索或引用记录的Run；它保留Run绑定与可观察证据ID，但没有把引用当完整排名或相关性标签。Recall@k、MRR均N/A。最小采集方式见`retrieval-annotation-dev-r1.json`。",
        f"Judge候选登记{len(judge['candidates'])}条候选，全部是AI辅助Dev待确认，不是已确认参考；本轮Judge请求0。R4既有小样本仍为8条格式有效、6/8类别一致；无已确认内容FAIL参照，误放行不可估计；误拒0/4只适用于R4的确认PASS对象。任务FAIL不等于事实错误标签。",
        "", "## M6.2退出差距", "",
        "M6.2仍为IN_PROGRESS。已完成R10四案真实执行与技术收口、28历史Run评分投影修正、32/40守恒台账及按绑定Dev指标。剩余8案的业务/数据/专项审核决定未解除；待审Run的评分建议尚未获得本轮用户确认；完整检索排名与相关性标注缺失；Judge负例覆盖不足；正式冻结审查、精确授权Frozen Test及最终报告未执行。详见`m6.2-exit-matrix-r18.json`。",
        "", "## 关键绑定", "",
        f"实现摘要：`{pkg['r10_first_runs'][0]['binding']['implementation_digest']}`；数据集SHA-256：`{pkg['r10_first_runs'][0]['binding']['dataset_sha256']}`；R10规则SHA-256：`{pkg['r10_first_runs'][0]['binding']['rules_sha256']}`；R10批次：`{pkg['r10_first_runs'][0]['binding']['batch_id']}`。",
        f"授权：`{read(R10 / 'authorization-r10r2-5C7A2E94F1038B6D.json')['authorization_id']}`，`CLOSED_COMPLETED`。共享账本累计Run 20、Provider 10、tokens 8,062、本地估算USD 0.0096744、执行67秒、预检124次；reservation为0。历史消耗保留且没有按段清零。",
        "", "原始Run、R1–R9历史包、关闭授权和偏差观察保持不变。详细逐案答案/证据/程序结论/AI建议及人工待审状态见`dev-score-review-package-r18.md`和JSON。"])
    write_md(REPORTS / "closeout-r18.md", "\n".join(md))

    # Report source and aggregate hashes are attached after deterministic output generation.
    closeout = read(closeout_path)
    closeout["score_review_package_r18"] = {"path": str(pkg_path).replace("\\", "/"), "sha256": sha(pkg_path)}
    closeout["metrics_r18"] = {"path": str(metrics_path).replace("\\", "/"), "sha256": sha(metrics_path), "binding_groups": len(metrics["groups"])}
    closeout["coverage_ledger_r18"] = {"path": str(ledger_path).replace("\\", "/"), "sha256": sha(ledger_path), "rows": 40, "run_count": 32}
    closeout["r18_reports"] = {"remaining_dispositions": {"path": str(R10 / "reports/remaining-8-case-disposition-r18.json").replace("\\", "/"), "sha256": sha(REPORTS / "remaining-8-case-disposition-r18.json")},
        "retrieval_annotation": {"path": str(R10 / "reports/retrieval-annotation-dev-r1.json").replace("\\", "/"), "sha256": sha(REPORTS / "retrieval-annotation-dev-r1.json")},
        "judge_candidates": {"path": str(R10 / "reports/judge-calibration-dev-candidates-r1.json").replace("\\", "/"), "sha256": sha(REPORTS / "judge-calibration-dev-candidates-r1.json")},
        "exit_matrix": {"path": str(R10 / "reports/m6.2-exit-matrix-r18.json").replace("\\", "/"), "sha256": sha(REPORTS / "m6.2-exit-matrix-r18.json")}}
    state = read(R10 / "execution-state-r10r2-5C7A2E94F1038B6D.json")
    closeout["agent_execution_evidence_reconciliation"] = {
        "cli_summary_agent_started": True,
        "execution_state_batch_marker": state.get("agent_started"),
        "per_run_raw_event_evidence": [{"case_id": row["case_id"], "run_id": row["run_id"],
            "agent_started_events": row["actual_agent_started_events"],
            "agents": row["actual_agent_names_from_same_run_events"], "handoffs": len(row["actual_handoffs"]),
            "trace_ids": row["same_run_event_trace_ids"]} for row in pkg["r10_first_runs"]],
        "resolution": "Use same-Run persisted run_events and handoffs as execution evidence; retain state-level projection discrepancy without altering source evidence."}
    write(closeout_path, closeout)
    print(json.dumps({"r10_runs": 4, "historical_runs": 28, "coverage": 32,
        "held": holds["case_count"], "retrieval_rows": retrieval["row_count"],
        "judge_candidates": len(judge["candidates"]), "binding_groups": len(metrics["groups"])}, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", choices=("r18", "r19", "r20", "r21", "r22", "r23", "r24", "r25", "r26", "r27", "r28", "r29", "r30", "m63", "m64", "m65"), default="r18")
    parser.add_argument("--score-relevance", action="store_true",
        help="Score the controlled R19 completed relevance form offline")
    parser.add_argument("--prepare-judge",action="store_true",help="Prepare the exact user-authorized R23 four-object grant;no requests")
    parser.add_argument("--finalize",action="store_true",help="Close R23 materials or project the recorded R30 personal acceptance offline")
    parser.add_argument('--revision',choices=('r1','r2'),default='r1',help='Versioned M6.4 offline evidence output; preserves R1')
    args = parser.parse_args()
    if args.prepare_judge and args.version!="r23":parser.error("Judge preparation requires version r23")
    if args.finalize and args.version not in {'r23','r30','m63'}:parser.error("finalize requires version r23, r30 or m63")
    if args.version == 'm65':
        if args.score_relevance or args.prepare_judge or args.finalize or args.revision!='r1':parser.error('m65 only projects existing delivery evidence;no execution')
        from eval.m6_2.delivery_m65 import build
        print(json.dumps(build(ROOT),ensure_ascii=False))
    elif args.version == 'm64':
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error('m64 only recomputes existing engineering evidence')
        from eval.m6_2.robustness_m64 import build
        print(json.dumps(build(ROOT,revision=args.revision),ensure_ascii=False))
    elif args.version == 'm63':
        if args.score_relevance or args.prepare_judge:parser.error('m63 only aggregates archived trials or records the already confirmed acceptance;no calls')
        from eval.m6_2.personal_core_m63 import build,finalize_accepted_scope
        result=finalize_accepted_scope(ROOT) if args.finalize else build(ROOT)
        keys=('stage_status','actual_trials','accepted_task_counts_coverage_only') if args.finalize else ('actual_trials','budget','paired_case_bootstrap')
        print(json.dumps({k:result[k] for k in keys},ensure_ascii=False))
    elif args.version == 'r30':
        if args.score_relevance or args.prepare_judge:parser.error('r30 only projects archived personal outputs and recorded acceptance;no calls')
        from eval.m6_2.personal_closeout_r30 import build,finalize_accepted_scope
        result=finalize_accepted_scope(ROOT) if args.finalize else build(ROOT)
        keys=('stage_status','runtime_count','accepted_task_counts_batch_description','budget') if args.finalize else ('actual_trials','dev','main','budget','scoped_real_execution')
        print(json.dumps({k:result[k] for k in keys},ensure_ascii=False))
    elif args.version == 'r29':
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error('r29 records approved review method and prepares later assessment;no calls')
        from eval.m6_2.candidate_revision_r27 import finalize_r29
        result=finalize_r29(ROOT)
        print(json.dumps(result,ensure_ascii=False))
    elif args.version == "r28":
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error("r28 reference/adapter correction and offline execution preparation only")
        from eval.m6_2.candidate_revision_r27 import finalize_r28
        result=finalize_r28(ROOT)
        print(json.dumps({'version':result['version'],'data':result['data'],'tests':result['tests'],'runtime':result['runtime'],'formal_activation':False},ensure_ascii=False))
    elif args.version == "r27":
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error("r27 revises authorized new candidates and prepares execution;no calls")
        from eval.m6_2.candidate_revision_r27 import finalize
        result=finalize(ROOT)
        print(json.dumps({'version':result['version'],'counts':result['data']['counts'],'tests':result['tests'],'runtime':result['runtime'],'formal_activation':False},ensure_ascii=False))
    elif args.version == "r26":
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error("r26 only recomputes existing construction and container engineering evidence")
        from eval.m6_2.container_closeout_r26 import build
        print(json.dumps(build(ROOT),ensure_ascii=False))
    elif args.version == "r25":
        if args.score_relevance or args.prepare_judge or args.finalize:parser.error("r25 is isolated engineering and handoff only")
        from eval.m6_2.formal_scope_r25 import build
        print(json.dumps(build(ROOT),ensure_ascii=False))
    elif args.version == "r24":
        if args.score_relevance or args.prepare_judge or args.finalize: parser.error("r24 is offline readiness only")
        from eval.m6_2.formal_readiness_r24 import build
        print(json.dumps(build(ROOT), ensure_ascii=False))
    elif args.version == "r23":
        from eval.m6_2.confirmed_review_r23 import build,prepare_judge,finalize
        if args.prepare_judge and args.finalize: parser.error("choose prepare or finalize")
        print(json.dumps(prepare_judge(ROOT) if args.prepare_judge else finalize(ROOT) if args.finalize else build(ROOT),ensure_ascii=False))
    elif args.version == "r22":
        from eval.m6_2.r21_review_closeout import build, score_completed
        print(json.dumps(score_completed(ROOT) if args.score_relevance else build(ROOT), ensure_ascii=False))
    elif args.version == "r21":
        if args.score_relevance:
            parser.error("r21 recomputes the seven first-run archives, not relevance labels")
        from scripts.prepare_m6_2_r325_seven import report
        print(json.dumps(report(), ensure_ascii=False))
    elif args.version == "r20":
        if args.score_relevance:
            parser.error("r20 applies the confirmed A/B decisions, not relevance labels")
        from eval.m6_2.confirmed_dev_preparation import build_confirmed_preparation
        build_confirmed_preparation(ROOT)
    elif args.version == "r19":
        from eval.m6_2.dev_review_materials import build_r19, score_completed_relevance
        if args.score_relevance:
            score_completed_relevance(ROOT, R10)
        else:
            build_r19(ROOT, R10)
    else:
        main()
