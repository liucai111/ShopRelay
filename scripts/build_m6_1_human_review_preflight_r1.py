from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/eval/m6.1-human-review-preflight-60-r1-20261028"
ANSWER_CSV = ROOT / "eval/fidelity/m2.4-q2-human-review-r2.csv"
ROUTE_CSV = ROOT / "eval/review/route-m2.1-dev-validation-review-r1.csv"


ANSWER_REVIEW: dict[str, dict[str, Any]] = {
    "J1": {
        "claim": "已发货未签收不得按未发货退款；默认转人工。已签收退款需已批准退货申请。退款申请不代表到账；此接口不能核实实际物流。",
        "source_excerpt": "已付款且未发货的订单可申请整单退款。已签收订单需要已批准的退货申请才能申请退款；已发货未签收默认转人工。申请不代表真实到账。",
        "programmatic_review": "该 polarity mismatch 是误报：答案逐项保留了政策阶段和否定方向，未把申请说成批准或到账。",
        "judge_review": "Judge 标 PASS，但仅作辅助。独立核对政策中的三个互斥阶段后，主体、阶段和否定范围一致。",
        "recommendation": "PASS",
        "rationale": "已出运未签收被分流人工；没有把该状态冒充未发货。答案又明确区分已批准退货与退款申请，并否认申请等于到账。政策不验证该用户的实际物流事实，答案也未声称已核验。",
        "missing": "无影响本争议裁决的关键缺证；此结论只针对政策答案，不判断任何真实订单。",
        "confidence": 0.94,
        "sources": ["policies/refund@2026-01#before-shipping", "policies/cancel@2026-01#eligible"],
    },
    "J2": {
        "claim": "政策不能证明订单昨天签收或仍在期限内；说明无理由 7×24 小时、质量问题 30 天条件并要求运单/订单事实核实。",
        "source_excerpt": "物流进度以查询时的运单事件为准。政策文档不能证明任何订单已经发货、签收或何时送达。无理由退货：签收后7×24小时内、普通商品未影响二次销售；质量问题：签收后30天内可提交，由人工审核。",
        "programmatic_review": "RETURN_REVIEW polarity mismatch 与答案全文不符，属于把“不能由政策证明实时履约”误判为否定政策窗口。",
        "judge_review": "Judge 标 PASS；独立核对后，回答核心正确并保留了普通商品二次销售条件。",
        "recommendation": "PASS",
        "rationale": "答案明确把政策期限与某订单的签收事实分开，未用政策虚构实际签收日；还保留无理由商品条件、质量问题窗口及人工审核阶段。",
        "missing": "无关键缺证。它未给出个人订单结论，符合本项问题边界。",
        "confidence": 0.95,
        "sources": ["policies/delivery@2026-01#tracking", "policies/return-standard@2026-07#no-reason", "policies/return-standard@2026-07#quality"],
    },
    "J3": {
        "claim": "政策文本不能证明具体订单签收时间；列出普通商品无理由 7×24 小时和质量问题 30 天，并说明需用运单核实。",
        "source_excerpt": "无理由退货须在签收后7×24小时内，普通商品未影响二次销售（定制商品和食品不适用）；提交后人工审核、不保证批准。质量问题为签收后30天内可提交，人工审核。政策不能证明任何订单何时签收。",
        "programmatic_review": "程序化 polarity mismatch 对核心回答为误报；但答案对7×24小时规则遗漏“未影响二次销售”条件。",
        "judge_review": "Judge 标 PASS，但没有充分处理同一答案中省略的必要商品状态条件，不能直接照搬。",
        "recommendation": "PARTIAL",
        "rationale": "核心证据边界与签收时间结论正确；但答案概括无理由退货时省略未影响二次销售这一适用条件，用户可能将窗口误解为无条件适用。",
        "missing": "不缺政策来源；缺的是回答完整覆盖政策中必要的商品状态条件。",
        "confidence": 0.88,
        "sources": ["policies/delivery@2026-01#tracking", "policies/return-standard@2026-07#no-reason", "policies/return-standard@2026-07#quality"],
    },
    "J4": {
        "claim": "食品发生质量问题且签收不到30天，可提交退货申请；人工审核，不保证通过，且当前接口不能代提交或核验订单。",
        "source_excerpt": "签收后30天内的质量问题可提交整单退货申请，由人工审核；定制商品与食品仍可申请。超过30天需转人工核对保修。",
        "programmatic_review": "ANSWER_UNSUPPORTED_POLICY_PHASE 将申请权限与批准结果混淆；答案没有宣称已批准，且明示不保证。属于误报。",
        "judge_review": "Judge 标 PASS；独立检查确认回答区分了申请、审核和批准。",
        "recommendation": "PASS",
        "rationale": "质量问题条款明确允许食品提交申请并要求人工审核；答案未把申请升级成批准。7×24小时无理由条款对食品的排除不适用于质量问题条款。",
        "missing": "无关键缺证。具体订单是否签收在30天内仍未核验，答案没有作此声称。",
        "confidence": 0.94,
        "sources": ["policies/return-standard@2026-07#quality", "policies/return-standard@2026-07#no-reason"],
    },
    "J5": {
        "claim": "退货仍在人工审核不满足退款申请要求的“已批准退货”前置条件。",
        "source_excerpt": "已签收订单需要已批准的退货申请才能申请退款。退货申请提交后由人工审核，不保证批准。",
        "programmatic_review": "RETURN_APPROVAL/REFUND_APPLICATION unsupported 判断为阶段解析误报：被引用政策直接规定已批准退货才能申请退款，待审不等于批准。",
        "judge_review": "Judge 标 PASS；独立检查确认未把申请、审核中和批准混为一谈。",
        "recommendation": "PASS",
        "rationale": "用户给出的状态是“仍在审核”，与政策要求的“已批准”不同；答案没有虚构审核结果，并要求以实际状态为准。",
        "missing": "无关键政策缺证；个案实际审核状态未查询，答案明确保留未知。",
        "confidence": 0.95,
        "sources": ["policies/refund@2026-01#before-shipping", "policies/return-standard@2026-07#quality", "policies/return-standard@2026-07#no-reason"],
    },
    "claim-04a": {
        "claim": "“申请退货不代表款项已真实到账。”",
        "source_excerpt": "申请不代表真实到账。该句所在来源明确语境为退款申请；未说明退货申请的状态与退款到账之间的关系。",
        "programmatic_review": "UNSUPPORTED 判断指出所引退款条款不能直接支持把主语换成退货申请；此处有实质来源范围问题。",
        "judge_review": "没有逐断言 Judge 决定；不得把候选 AI 建议当裁决。",
        "recommendation": "INSUFFICIENT_EVIDENCE",
        "rationale": "金额到账的否定条件只绑定退款申请，争议断言却改成退货申请。二者在流程上相关，但现有原文不足以证明这是等价改写；不应自动通过或断言必然矛盾。",
        "missing": "需要明确“退货申请/退款申请/实际到账”之间关系的政策或API契约原文。",
        "confidence": 0.91,
        "sources": ["policies/refund@2026-01#before-shipping"],
    },
    "claim-07": {
        "claim": "同一订单任一时刻仅允许一个开放的整单退款申请，部分退款暂不支持。",
        "source_excerpt": "同一订单任一时刻仅允许一个开放的整单退款申请，原因不同也不例外。部分退款暂不支持。",
        "programmatic_review": "SUPPORTED；来源和限定范围吻合。",
        "judge_review": "没有逐断言 Judge 决定；现存意见仅提示核查相关性。",
        "recommendation": "PARTIAL",
        "rationale": "断言被政策直接支持，但这条额外退款唯一性/部分退款限制没有直接回答“退货与退款条件如何衔接”，有离题或挤占核心解释的风险。作为局部断言可接受，完整答案应优先解释退货审批与退款申请顺序。",
        "missing": "没有事实证据缺失；需要人工确认其在完整回答中的相关性与比例是否足够。",
        "confidence": 0.85,
        "sources": ["policies/refund@2026-01#uniqueness"],
    },
    "claim-08": {
        "claim": "当前接口不能核验具体订单状态，也不能执行退款或退货操作。",
        "source_excerpt": "当前聊天执行器不会调用 GET /api/orders/{id}；M2 当前执行边界不执行实时订单工具，对不支持执行安全失败。GET /api/orders/{id} 本身是认证后只读订单接口。",
        "programmatic_review": "UNCERTAIN；政策 chunk 无法证明系统接口能力，需用 API/执行器契约判定。",
        "judge_review": "没有逐断言 Judge 决定；现有 AI 意见要求缩窄到聊天执行器能力。",
        "recommendation": "PARTIAL",
        "rationale": "若“当前接口”明确指当前聊天执行器，当前执行链不调用订单读取工具，且不提供退款/退货写入工具，结论成立；若指整个 API，则 GET 订单接口存在。表述范围不精确，应限定为“本次聊天入口/执行器”。",
        "missing": "需要在原问题语境中确定“接口”的指代；若无法确定，保留范围歧义，不推断通用 API 均不可查询。",
        "confidence": 0.82,
        "sources": ["app/m1/chat_execution.py#execute_claimed_chat", "docs/m2.1-routing.md#当前执行器", "docs/m1.1-api.md#GET-/api/orders/{id}"],
    },
}


SOURCE_PATHS = [
    "ShopRelay_Agent_Eval_完整评测方案.md",
    "项目规格/evaluation-spec.md",
    "docs/progress.md",
    "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/exit-matrix-r2.json",
    "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/closeout-r2.md",
    "docs/m2.4-q3-human-review-index-r1.md",
    "docs/m2.4-q2-human-review-r2.md",
    "eval/fidelity/m2.4-q2-human-review-r2.csv",
    "eval/review/route-m2.1-dev-validation-review-r1.csv",
    "eval/dataset/route-annotation-guide.md",
    "docs/m2.1-routing.md",
    "app/m1/routing.py",
    "app/m1/chat_execution.py",
    "docs/m1.1-api.md",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def unique_versions(text: str) -> list[str]:
    return sorted(set(re.findall(r"policies/[\w-]+@\d{4}-\d{2}", text)))


def build() -> None:
    answers = load_csv(ANSWER_CSV)
    routes = load_csv(ROUTE_CSV)
    if len(answers) != 8 or {row["item_id"] for row in answers} != set(ANSWER_REVIEW):
        raise ValueError("ANSWER_DISPUTE_SOURCE_SET_MISMATCH")
    split_counts = {split: sum(row["dataset_split"] == split for row in routes)
        for split in ("dev", "validation")}
    if len(routes) != 52 or split_counts != {"dev": 28, "validation": 24}:
        raise ValueError(f"ROUTE_SOURCE_SET_MISMATCH:{len(routes)}:{split_counts}")
    if any(row.get("review_status") != "PENDING_HUMAN_REVIEW" for row in answers):
        raise ValueError("ANSWER_REVIEW_STATUS_CHANGED")
    if any(row.get("review_status") != "PENDING_HUMAN_REVIEW" for row in routes):
        raise ValueError("ROUTE_REVIEW_STATUS_CHANGED")

    answer_records = []
    for row in answers:
        review = ANSWER_REVIEW[row["item_id"]]
        answer_records.append({
            "item_id": row["item_id"], "case_id": row["case_id"],
            "dataset_split": row["dataset_split"], "question": row["question"],
            "disputed_claim": row["disputed_claim"],
            "source_versions": unique_versions(row["policy_or_contract_source"]),
            "source_excerpt_in_review_record": row["policy_or_contract_source"],
            "programmatic_original": row["programmatic_conclusion"],
            "judge_original": row["judge_conclusion"],
            "ai_recommendation_original": row["ai_recommendation"],
            "answer_evidence_scope": ("FULL_ANSWER_AS_RECORDED_IN_REVIEW_ARTIFACT" if row["item_id"].startswith("J")
                else "ONLY_THE_DISPUTED_CLAIM_IS_RECORDED; FULL_ANSWER_NOT_PRESENT_IN_THE_SPECIFIED_ARTIFACTS"),
            "pre_review": review,
            "human_fields": {"reviewer_decision": None, "reviewer": None,
                "reviewed_at_utc": None, "review_rationale": None,
                "status": "AWAITING_USER_CONFIRMATION"},
        })

    route_records = []
    changed = []
    for row in routes:
        group = row["group_id"]
        multi = group in {"policy:delivery_policy_limits", "policy:multi_clause_return_refund"}
        suggested = {
            "strategy": "MULTI" if multi else "SINGLE",
            "sub_strategy": "MULTI" if multi else "BM25",
            "intent": "policy_question", "safety_decision": "ALLOW",
            "reason_code": "MULTI_POLICY_CONDITIONS" if multi else "POLICY_QUESTION",
            "needs_clarification": False,
            "required_capabilities": ["search_knowledge"],
        }
        original = {
            "strategy": row["expected_strategy"], "sub_strategy": row["expected_sub_strategy"],
            "intent": row["expected_intent"], "safety_decision": row["expected_safety_decision"],
            "reason_code": row["expected_reason_code"],
            "needs_clarification": row["expected_needs_clarification"].strip().lower() == "true",
            "required_capabilities": ast.literal_eval(row["required_capabilities"]),
        }
        field_map = {"strategy": "expected_strategy", "sub_strategy": "expected_sub_strategy",
            "intent": "expected_intent", "safety_decision": "expected_safety_decision",
            "reason_code": "expected_reason_code", "needs_clarification": "expected_needs_clarification",
            "required_capabilities": "required_capabilities"}
        changed_fields = [field_map[k] for k in suggested if suggested[k] != original[k]]
        if changed_fields:
            changed.append(row["case_id"])
        if multi:
            rationale = ("问题需要联合解释多条有效政策：退货时点/证明边界与签收事实来源，或退货申请/人工审核/批准与退款前置条件。它仍是政策咨询，不应转成对具体订单的实时查询。按 route-annotation-guide 的联合条件定义建议 MULTI/MULTI。"
                if group == "policy:delivery_policy_limits" else
                "问题把退货资格或人工审批阶段与退款前置条件明确联系起来，必须联读退货与退款政策；没有请求读取某笔实时订单。按 route-annotation-guide 的多政策联合条件定义建议 MULTI/MULTI。")
            confidence = 0.84 if group == "policy:delivery_policy_limits" else 0.82
            ambiguity = "仍需确认当前路由契约是否将‘一个 BM25 检索可返回多条来源’与‘需联合推理的 MULTI 策略’区分；本建议按联合推理需求而非检索返回条数分类。"
        else:
            rationale = "问题询问一项政策规则、单一资格阶段、指定历史版本或对缺失事实的政策边界；没有要求查询具体订单/实时业务状态。建议 SINGLE/BM25 + search_knowledge，保持安全 ALLOW，不要求澄清。"
            confidence = 0.9
            ambiguity = "若‘我的订单’仅作为政策场景而未请求核实当前订单，仍按政策咨询；如后续改为查询实际订单状态，应另标 AGENT 并核对订单标识和授权。"
        route_records.append({
            "case_id": row["case_id"], "group_id": group, "dataset_split": row["dataset_split"],
            "source_case_id": row["source_case_id"], "user_query": row["user_query"],
            "original_label": original, "source_review_status": row["review_status"],
            "historical_correctness": "NOT_ESTABLISHED_UNREVIEWED_CANDIDATE",
            "pre_review_recommendation": "MODIFY" if changed_fields else "RETAIN",
            "suggested_label": suggested, "changed_fields": changed_fields,
            "basis": rationale, "confidence": confidence, "ambiguity": ambiguity,
            "label_version_basis": {"route_version": "m2.1-route-v7",
                "prompt_version": "m2.1-deepseek-route-v3",
                "annotation_guide": "eval/dataset/route-annotation-guide.md (r1 candidate guide)"},
            "human_fields": {"reviewer_decision": None, "reviewer": None,
                "reviewed_at_utc": None, "review_rationale": None,
                "status": "AWAITING_USER_CONFIRMATION"},
        })

    if len(changed) != 3:
        raise ValueError(f"UNEXPECTED_ROUTE_CHANGE_COUNT:{len(changed)}:{changed}")
    pre_review = {
        "schema_version": "m6.1-evidence-pre-review-60-r1",
        "classification": "AI_ASSISTED_EVIDENCE_PRE_REVIEW_AWAITING_USER_CONFIRMATION",
        "not_a_human_decision": True,
        "scope": {"answer_disputes": 8, "routes": 52, "route_split_counts": split_counts,
            "not_in_scope": ["completed eight-case user-confirmed Dev Smoke review", "all 40/52 additional samples beyond the specified CSV", "test_candidate", "Gold/Frozen adjudication"]},
        "summary": {"answer_disputes": {"PASS": 4, "PARTIAL": 3,
            "FAIL": 0, "INSUFFICIENT_EVIDENCE": 1},
            "routes": {"RETAIN": 49, "MODIFY": 3,
                "original_strategy_counts": {"SINGLE": sum(x["original_label"]["strategy"] == "SINGLE" for x in route_records),
                    "MULTI": sum(x["original_label"]["strategy"] == "MULTI" for x in route_records)},
                "suggested_strategy_counts": {"SINGLE": sum(x["suggested_label"]["strategy"] == "SINGLE" for x in route_records),
                    "MULTI": sum(x["suggested_label"]["strategy"] == "MULTI" for x in route_records)}}},
        "answer_pre_reviews": answer_records, "route_pre_reviews": route_records,
        "user_confirmation_required": True,
        "human_review_fields_written": False,
    }
    (OUT / "proposed-review-decisions-r1.json").write_text(
        json.dumps(pre_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    source_bindings = []
    artifact_versions = {
        "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/exit-matrix-r2.json": "M6.1 exit matrix r2",
        "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/closeout-r2.md": "M6.1 closeout r2",
        "docs/m2.4-q3-human-review-index-r1.md": "M2.4-Q3 review index r1",
        "docs/m2.4-q2-human-review-r2.md": "M2.4-Q2 dispute review r2",
        "eval/fidelity/m2.4-q2-human-review-r2.csv": "M2.4-Q2 dispute source CSV r2",
        "eval/review/route-m2.1-dev-validation-review-r1.csv": "M2.1 route candidate review CSV r1",
        "eval/dataset/route-annotation-guide.md": "M2.1 route annotation guide r1",
        "docs/m2.1-routing.md": "M2.1 execution boundary specification",
    }
    for relative in SOURCE_PATHS:
        path = ROOT / relative
        if not path.is_file():
            raise FileNotFoundError(relative)
        source_bindings.append({"path": relative, "version": artifact_versions.get(relative, "current source snapshot"),
            "sha256": sha(path)})
    source_bindings.append({"path": "AGENTS.md", "status": "NOT_PRESENT_AT_REPOSITORY_ROOT"})
    source_doc = {"schema_version": "m6.1-review-preflight-sources-r1",
        "reviewed_route_contract": {"route_version": "m2.1-route-v7", "prompt_version": "m2.1-deepseek-route-v3",
            "strategy_schema": ["DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"]},
        "policy_source_versions_observed_in_dispute_rows": sorted({version for row in answers
            for version in unique_versions(row["policy_or_contract_source"])}),
        "source_scope": "Only the explicitly named review CSVs, M6.1/M6.2 protocol and route/tool contract files; no Candidate/Frozen content.",
        "sources": source_bindings,
        "policy_evidence_note": "The versioned policy excerpts are transcribed in the specified dispute CSV/Markdown rows; these are the reviewed evidence provided by that audit package. No policy corpus outside those exact artifacts was searched."}
    (OUT / "source-bindings-r1.json").write_text(
        json.dumps(source_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    answer_lines = ["# 8 answer-dispute evidence pre-review", "",
        "AI-assisted pre-review only; every proposal below awaits the user’s one-time confirmation. Original CSV rows remain `PENDING_HUMAN_REVIEW`.", "",
        "| Item / case | Question and disputed claim | Source text/version | Programmatic vs Judge review | Proposed decision and reason | Missing evidence / confidence |",
        "|---|---|---|---|---|---|"]
    for row in answer_records:
        p = row["pre_review"]
        source_versions = ", ".join(row["source_versions"])
        answer_lines.append("| `{}` / `{}` | {}<br>**Claim:** {} | {}<br>`{}` | Program: {}<br>Judge: {} | **{}:** {} | {}<br>Confidence {} |".format(
            row["item_id"], row["case_id"], row["question"].replace("|", "\\|"),
            row["disputed_claim"].replace("|", "\\|"), p["source_excerpt"].replace("|", "\\|"),
            source_versions, p["programmatic_review"].replace("|", "\\|"),
            p["judge_review"].replace("|", "\\|"), p["recommendation"],
            p["rationale"].replace("|", "\\|"), p["missing"].replace("|", "\\|"), p["confidence"]))
    (OUT / "answer-dispute-pre-review-r1.md").write_text("\n".join(answer_lines) + "\n", encoding="utf-8")

    route_lines = ["# 52 dev/validation route-label evidence pre-review", "",
        "Original candidate labels remain unchanged and unapproved. Recommendations are judged from query meaning, the r1 annotation guide, and the versioned M2.1 route contract—not from current program output.", "",
        "| ID / split / group | User query | Original label | Suggested label | Retain/modify | Basis, confidence and ambiguity |",
        "|---|---|---|---|---|---|"]
    for row in route_records:
        old, new = row["original_label"], row["suggested_label"]
        old_text = f"{old['strategy']}/{old['sub_strategy']}; {old['intent']}; {old['safety_decision']}; {old['reason_code']}; clarify={old['needs_clarification']}; caps={old['required_capabilities']}"
        new_text = f"{new['strategy']}/{new['sub_strategy']}; {new['intent']}; {new['safety_decision']}; {new['reason_code']}; clarify={new['needs_clarification']}; caps={new['required_capabilities']}"
        route_lines.append("| `{}` / {} / `{}` | {} | {} | {} | **{}**{} | {} Confidence {}. Ambiguity: {} |".format(
            row["case_id"], row["dataset_split"], row["group_id"], row["user_query"].replace("|", "\\|"),
            old_text.replace("|", "\\|"), new_text.replace("|", "\\|"), row["pre_review_recommendation"],
            ("; fields=" + ", ".join(row["changed_fields"])) if row["changed_fields"] else "",
            row["basis"].replace("|", "\\|"), row["confidence"], row["ambiguity"].replace("|", "\\|")))
    (OUT / "route-pre-review-r1.md").write_text("\n".join(route_lines) + "\n", encoding="utf-8")

    checklist = ["# User confirmation checklist — 60 evidence pre-reviews", "",
        "Please confirm or amend these specific recommendations. This is a proposal; it has not populated the original CSV reviewer fields.", "",
        "## Eight answer disputes", "",
        "| Item / case | Proposed decision | Confirmation point |", "|---|---|---|"]
    for row in answer_records:
        p = row["pre_review"]
        checklist.append(f"| `{row['item_id']}` / `{row['case_id']}` | **{p['recommendation']}** | {p['claim']} Reason: {p['rationale']} Missing: {p['missing']} |")
    checklist += ["", "## Fifty-two route samples", "",
        "All rows retain `policy_question / ALLOW / no clarification / search_knowledge`. The strategy/reason changes are individually shown below; unchanged fields are explicitly included in each suggested tuple.", "",
        "| Case / split | Proposed route tuple (strategy, sub-strategy, intent, safety, reason, clarification, capabilities) | Decision and reason |",
        "|---|---|---|"]
    for row in route_records:
        new = row["suggested_label"]
        tuple_text = f"{new['strategy']}, {new['sub_strategy']}, {new['intent']}, {new['safety_decision']}, {new['reason_code']}, {new['needs_clarification']}, {new['required_capabilities']}"
        checklist.append(f"| `{row['case_id']}` / {row['dataset_split']} | `{tuple_text}` | **{row['pre_review_recommendation']}**. {row['basis']} |")
    checklist += ["", "## Scope", "",
        "The already completed user-confirmed eight-case Smoke review is not repeated or expanded here. Confirming this package would cover only the 8 dispute rows and 52 route rows above. It would not approve Gold, Frozen Test, establish an independent reviewer process, or change any formal Gate.", "",
        "After confirmation, record the exact user-confirmed content, scope, actor type `CHAT_USER_SELF_CONFIRMED`, confirmation-record time and method. Then produce a versioned derived decision record, preserve `INSUFFICIENT_EVIDENCE`/`PARTIAL`/`FAIL` outcomes, and update source CSV review fields only as the confirmed process permits. Recheck the M6.1 exit matrix; if any item remains blocked, state its specific missing evidence. Do not rerun Smoke by default.", ""]
    (OUT / "user-confirmation-checklist-r1.md").write_text("\n".join(checklist), encoding="utf-8")

    print(json.dumps({"status": "BUILT", "answer_items": len(answer_records),
        "answer_counts": pre_review["summary"]["answer_disputes"],
        "route_items": len(route_records), "split_counts": split_counts,
        "route_counts": pre_review["summary"]["routes"],
        "changed_route_case_ids": changed}, ensure_ascii=False))


if __name__ == "__main__":
    build()
