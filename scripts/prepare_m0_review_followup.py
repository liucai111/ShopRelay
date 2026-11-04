"""Create non-authoritative M0 review proposals without editing candidate gold.

All generated artifacts are versioned and are for human review only.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
POLICIES = ROOT / "knowledge/policies.json"
OUT_REVIEW = ROOT / "eval/review"
OUT_PROBES = ROOT / "docs/probes"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def evidence_catalog(policies):
    result = {}
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            eid = f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"
            result[eid] = {"text": clause["text"], "title": doc["title"], "effective_from": doc["effective_from"], "effective_to": doc["effective_to"]}
    return result


def source_suggestion(group, text):
    # Intentionally a review aid, never written to candidate JSON.
    if any(token in text for token in ("核对订单使用记录", "订单使用记录", "查询订单记录", "读取订单记录")):
        return "BUSINESS_TOOL", "订单使用/交易记录属于个案动态数据，应由授权业务工具核实；政策仅说明核对规则。"
    if ("质量问题" in text or "质量退货" in text) and any(token in text for token in ("30天", "三十天", "签收后")) and not any(token in text for token in ("规则", "政策")):
        return "UNKNOWN", "该条件可能把用户提供的签收时长/质量事实与政策期限混写；请拆成 USER_PROVIDED 个案事实和 POLICY 时限规则。"
    if group == "delivery_fact_boundary":
        if any(token in text for token in ("用户问", "用户请求", "查询具体")):
            return "UNKNOWN", "这是问题/任务类型描述，不是用户提供的订单状态事实；建议改写为可由政策证据评价的知识边界条件。"
        if any(token in text for token in ("知识库", "政策文档", "实时状态", "物流进度")):
            return "POLICY", "问题询问政策知识边界；具体订单状态仍须业务工具核验。"
        if any(token in text for token in ("用户表示", "用户说", "用户声称", "已签收", "已发货")):
            return "USER_PROVIDED", "若该条件指用户陈述，只代表用户提供的信息，未独立核验。"
    if group == "multi_clause_return_refund":
        if any(token in text for token in ("商品已签收", "已签收", "订单已签收", "签收")):
            return "USER_PROVIDED", "问题中的个案事实；只有用户明确陈述时可标 USER_PROVIDED，不能由政策证明实际状态。"
        if any(token in text for token in ("30天", "质量问题", "退货申请", "退款", "获批", "批准")):
            return "POLICY", "规则条件应由对应政策条款支持；需逐条确认是否被该问题询问。"
    if any(token in text for token in ("商品类型为", "商品为食品", "普通商品范围", "已签收", "已付款", "未发货", "已发货", "退货申请已获批准", "用户已确认", "用户确认后")):
        return "USER_PROVIDED", "此项应仅表示题面给出的个案前提，不能视为系统核验；未在问题中明确陈述时请改为 UNKNOWN 或工具核验。"
    if any(token in text for token in ("未提供", "未知", "缺少", "未说明")):
        return "UNKNOWN", "这是对当前提示上下文缺失信息的描述，不是政策事实或已核实订单事实。"
    if any(token in text for token in ("政策", "条款", "规则", "期限", "时限", "例外", "申请条件", "应转人工", "适用版本")):
        return "POLICY", "规则/适用条件候选，需由有效政策条文支持。"
    if any(token in text for token in ("订单", "商品", "付款", "签收", "发货", "用户", "包裹")):
        return "USER_PROVIDED", "可能是问题中的用户陈述；如需确认实际情况，应改标 BUSINESS_TOOL 或拆分。"
    return "UNKNOWN", "自动规则无法可靠判断；请人工选择或拆分为事实与规则条件。"


def high_risk_proposals(cases, evidence):
    result = []
    for c in cases:
        group = c["template_group"]
        if group not in {"multi_clause_return_refund", "delivery_fact_boundary"}:
            continue
        orig = {"required_conditions": c["required_conditions"], "acceptable_evidence_ids": c["acceptable_evidence_ids"], "evidence_judgments": c["evidence_judgments"], "expected_conclusion": c["expected_conclusion"], "expected_strategy": c["expected_strategy"], "expected_tool_calls": c["expected_tool_calls"]}
        if group == "multi_clause_return_refund":
            q = c["user_query"]
            case_id = c["case_id"]
            if case_id.endswith("-01"):
                user_facts = ["用户陈述商品签收约20天", "用户陈述商品发生故障"]
                quality_clause_needed, refund_clause_needed = True, True
            elif case_id.endswith("-02"):
                user_facts = ["用户陈述货物已送达（题面未明确签收时间）", "用户陈述质量问题在三十天内提出（计时起点未说明）"]
                quality_clause_needed, refund_clause_needed = True, True
            elif case_id.endswith("-03"):
                user_facts = ["用户陈述商品已签收", "用户陈述商品有瑕疵"]
                quality_clause_needed, refund_clause_needed = True, True
            else:
                user_facts = ["用户陈述为已签收订单", "用户陈述退货申请仍在审核、尚未获批"]
                quality_clause_needed, refund_clause_needed = False, True
            new_conditions = []
            for i, fact in enumerate(user_facts, 1):
                new_conditions.append({"condition_id": f"p{i}", "text": fact, "suggested_source_type": "USER_PROVIDED", "evidence_ids": [], "note": "此用户陈述不可由政策证据验证。"})
            if case_id.endswith("-02"):
                new_conditions.append({"condition_id": f"p{len(new_conditions)+1}", "text": "三十天是否从签收时间起算；题面未给签收时间", "suggested_source_type": "UNKNOWN", "evidence_ids": [], "note": "不能把‘提出质量问题在30天内’直接等同于政策规定的‘签收后30天内’。如需判定本案资格，应澄清签收时间。"})
            if quality_clause_needed:
                term = "期限及人工审核条件" if any(t in q for t in ("30天", "三十天", "20天", "二十天")) else "人工审核条件（题面未说明是否在30天内）"
                new_conditions.append({"condition_id": f"p{len(new_conditions)+1}", "text": "质量问题退货申请" + term, "suggested_source_type": "POLICY", "evidence_ids": ["policies/return-standard@2026-07#quality"], "note": "仅作规则依据；不得把题面未提及的30天时限当成已满足的事实。"})
            if refund_clause_needed:
                new_conditions.append({"condition_id": f"p{len(new_conditions)+1}", "text": "已签收订单须先有已批准的退货申请，方可申请退款", "suggested_source_type": "POLICY", "evidence_ids": ["policies/refund@2026-01#before-shipping"], "note": "退款前置条件；不支持30天质量退货时限。"})
            new_evidence = sorted({eid for cond in new_conditions for eid in cond["evidence_ids"]})
            if case_id.endswith("-04"):
                conclusion = "退货仍在审核不等于已经批准，因此尚未满足已签收订单申请退款的前置条件；该结论不判断质量退货是否在30天内。"
            elif case_id.endswith("-02"):
                conclusion = "政策规定质量问题须在签收后30天内方可提交整单退货申请进入人工审核；题面只说问题在30天内提出、未给签收时间，不能确认该时限已满足。无论是否满足质量退货时限，已签收订单退款仍须先有已批准退货，不能绕过批准。"
            elif case_id.endswith("-03"):
                conclusion = "质量问题退货可提交人工审核；题面未给出时长，不能判定30天时限是否满足。已签收订单须先有已批准退货申请，再申请退款。"
            else:
                conclusion = "质量问题在题面所述时点可提交退货申请进入人工审核，不代表自动批准；已签收订单须先有已批准退货申请，再申请退款。"
            diagnosis = "原标注把质量问题30天条件连到退款条款，并把用户陈述的签收事实当作政策支持；四条改写的问题意图/明示事实不同，不能共用无差别条件清单。"
            strategy_effect = "01–03 同时要求质量退货规则与退款前置条件，候选为 MULTI；04 只询问退货未批准是否满足退款前置条件，候选为 SINGLE。仍需人工确认双层路由契约及评分口径。"
            clarification = "不需为解释规则而澄清。若要判断实际订单是否已签收/退货是否已批准，需订单上下文并调用业务工具；缺少订单信息时应先澄清/请求订单标识。"
            policy_basis = {eid: evidence[eid]["text"] for eid in new_evidence}
        else:
            new_conditions = [
                {"condition_id": "p1", "text": "物流进度以查询时运单事件为准", "suggested_source_type": "POLICY", "evidence_ids": ["policies/delivery@2026-01#tracking"], "note": "这是规范性规则。"},
                {"condition_id": "p2", "text": "政策文档不能证明任何特定订单已发货、签收或何时送达", "suggested_source_type": "POLICY", "evidence_ids": ["policies/delivery@2026-01#tracking"], "note": "知识边界规则。"},
            ]
            conclusion = "回答政策知识边界：具体物流进度以查询时运单事件为准，政策文档不能证明特定订单的实时状态。若用户实际要求查某单状态，应有订单上下文并调用 track_shipment；没有上下文时先澄清。"
            diagnosis = "四题询问政策文本能否证明个案物流状态，属于来源边界咨询，不是实际运单状态断言；原‘用户问具体事实’是任务描述，却被政策条款标为支持。"
            strategy_effect = "现有四问均像政策边界咨询，SINGLE 有依据且不应一律改 AGENT。若人工确认某一变体实际要求查询具体订单状态，则该项需改为 AGENT/业务工具路由，并核实工具契约。"
            clarification = "问‘政策能否证明/该用何来源’不需澄清。实际查件但缺订单标识或授权上下文时需澄清；有上下文时调用 track_shipment。"
            new_evidence = ["policies/delivery@2026-01#tracking"]
            policy_basis = {eid: evidence[eid]["text"] for eid in new_evidence}
        result.append({
            "case_id": c["case_id"], "template_group": group, "dataset_split": c["dataset_split"], "user_query": c["user_query"],
            "original": orig, "issue": diagnosis, "proposed": {"required_conditions": new_conditions, "acceptable_evidence_ids": new_evidence, "expected_conclusion": conclusion, "expected_strategy": strategy_effect, "clarification_behavior": clarification},
            "policy_text_basis": policy_basis, "diff_summary": {"conditions": "拆分用户事实与政策规则；删除问题未表达的条件；修正30天规则证据映射。" if group == "multi_clause_return_refund" else "把规范性政策条件与用户陈述分开；不再以政策条款证明个案物流事实。", "evidence": {"remove_or_correct": orig["evidence_judgments"], "proposed_policy_evidence": new_evidence}, "strategy": strategy_effect},
            "human_review_required": True, "approval_status": "PENDING_REVIEW", "auto_applied": False,
        })
    return result


def make_source_suggestions(cases):
    rows = []
    for c in cases:
        for cond in c.get("required_conditions", []):
            src, rationale = source_suggestion(c["template_group"], cond.get("text", ""))
            mixed = any(k in cond.get("text", "") for k in ("且", "并且", "同时", "/", "与")) or "拆成 USER_PROVIDED" in rationale
            rows.append({"case_id": c["case_id"], "template_group": c["template_group"], "dataset_split": c["dataset_split"], "user_query": c["user_query"], "condition_id": cond.get("condition_id"), "condition_text": cond.get("text"), "suggested_source_type": src, "rationale": rationale, "compound_or_mixed_source": "REVIEW_FOR_SPLIT" if mixed else "REVIEW", "review_status": "PENDING_HUMAN_REVIEW", "authoritative": False})
    return rows


def representative_material(cases, policies):
    ev = evidence_catalog(policies)
    groups = defaultdict(list)
    for c in cases:
        groups[c["template_group"]].append(c)
    lines = ["# M0 逐模板组代表样本审核清单（r1；全部待人工审核）", "", "候选数据未被修改。代表题只作为审核入口，不代表同组变体；每一题仍须单独确认。所有来源标签均为建议。先处理高风险、复合条件、历史版本、动态事实和澄清组。", "", "## 审核顺序", "", "1. `multi_clause_return_refund`、`delivery_fact_boundary`（HIGH 修订提案）", "2. `ambiguous_paid_policy_version`、`warranty_specific_missing`（澄清/拒答行为）", "3. 其余历史版本、取消/退款冲突、优惠券与动态事实组", "4. 其他单政策组", ""]
    audit_path = OUT_PROBES / "policy-gold-audit-20261019T025807Z.json"
    findings = read_json(audit_path).get("findings", []) if audit_path.exists() else []
    by_group = defaultdict(list)
    for item in findings:
        by_group[item["template_group"]].append(item)
    def priority(name):
        if name in {"multi_clause_return_refund", "delivery_fact_boundary"}: return (0, name)
        if name in {"ambiguous_paid_policy_version", "warranty_specific_missing"}: return (1, name)
        if any(t in name for t in ("version", "boundary", "refund", "cancel", "delivery")): return (2, name)
        return (3, name)
    for name in sorted(groups, key=priority):
        rows = sorted(groups[name], key=lambda c: c.get("variant", 0))
        rep = next((c for c in rows if c.get("variant") == 1), rows[0])
        lines += [f"## {name}", "", f"- Split：`{rep['dataset_split']}`；代表：`{rep['case_id']}`；同组共 {len(rows)} 条。", f"- 场景：{rep['business_scenario']}；策略候选：{rep['expected_strategy']}；评分：{rep['scoreability']}。", f"- 问题：{rep['user_query']}", f"- 候选结论：{rep['expected_conclusion']}", f"- 候选证据：{', '.join(rep['acceptable_evidence_ids']) or '无'}", "- 政策原文："]
        for eid in rep.get("acceptable_evidence_ids", []) + rep.get("possible_evidence_ids", []):
            if eid in ev: lines.append(f"  - `{eid}`（{ev[eid]['effective_from']} 至 {ev[eid]['effective_to'] or '当前'}）：{ev[eid]['text']}")
        if not rep.get("acceptable_evidence_ids") and not rep.get("possible_evidence_ids"):
            lines.append("  - 当前无可接受证据；请审核是否属于证据不足/澄清行为样本。")
        lines.append("- 必要条件与来源建议：")
        for cond in rep.get("required_conditions", []):
            src, why = source_suggestion(name, cond.get("text", ""))
            lines.append(f"  - `{cond.get('condition_id')}` {cond.get('text')} — 建议 `{src}`（{why}）")
        hints = by_group.get(name, [])
        lines.append("- 自动审计提示：" + ("；".join(f"{x['severity']} {x['issue_type']}: {x['suggested_correction']}" for x in hints) if hints else "无组级 HIGH 提示；仍需核对问题、条件、版本与证据。"))
        lines.append("- 变体语义差异（规则辅助，需人工确认）：")
        for c in rows:
            lines.append(f"  - `{c['case_id']}`（v{c.get('variant')}）：{c['user_query']}；条件数 {len(c.get('required_conditions', []))}；金标证据 {', '.join(c.get('acceptable_evidence_ids', [])) or '无'}；评分 {c['scoreability']}。")
        lines += ["- 审核意见（每个 case 单独填写，不向其他变体继承）：", "  - 代表题状态：`APPROVED / NEEDS_CORRECTION / DEFERRED`：________；审核者：________；UTC 时间：________；依据/备注：________。", "  - 其余变体逐条复核并填写同样字段；不得复制代表题状态。", ""]
    dest = OUT_REVIEW / "policy-retrieval-v2-representative-review-r1.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def split_analysis(cases):
    splits = defaultdict(list)
    for c in cases: splits[c["dataset_split"]].append(c)
    report = {}
    combos = defaultdict(set)
    for c in cases: combos[c["policy_combination_group"]].add(c["dataset_split"])
    for name in ("dev", "validation", "test_candidate"):
        rows = splits[name]
        groups = defaultdict(list)
        for c in rows: groups[c["template_group"]].append(c)
        report[name] = {
            "cases": len(rows), "template_groups": len(groups),
            "business_scenario_counts": dict(Counter(c["business_scenario"] for c in rows)),
            "strategy": dict(Counter(c["expected_strategy"] for c in rows)),
            "scoreability": dict(Counter(c["scoreability"] for c in rows)),
            "scorable_strategy": dict(Counter(c["expected_strategy"] for c in rows if c["scoreability"] == "SCORABLE_CANDIDATE")),
            "compound_tagged_cases": sum("compound_reasoning" in c.get("tags", []) for c in rows),
            "hard_negative_cases": sum(bool(c.get("hard_negative_evidence_ids")) for c in rows),
            "policy_combination_group_counts": dict(Counter(c["policy_combination_group"] for c in rows)),
            "groups": {k: {
                "business_scenario": v[0]["business_scenario"],
                "strategy": dict(Counter(c["expected_strategy"] for c in v)),
                "scoreability": dict(Counter(c["scoreability"] for c in v)),
                "compound_tagged_cases": sum("compound_reasoning" in c.get("tags", []) for c in v),
                "hard_negative_cases": sum(bool(c.get("hard_negative_evidence_ids")) for c in v),
                "policy_combination_group": v[0]["policy_combination_group"],
                "acceptable_evidence_id_sets": sorted({tuple(sorted(c.get("acceptable_evidence_ids", []))) for c in v}),
            } for k, v in groups.items()}
        }
    moves = [
        {"from": "dev", "to": "validation", "policy_combination_group": "pc-4700b22239de", "template_groups": ["refund_delivered_return", "multi_clause_return_refund"], "reason": "提供两个同一证据组合下的 MULTI 模板改写；整体移动以维持 combination 隔离。"},
        {"from": "dev", "to": "test_candidate", "policy_combination_group": "pc-504161c44035", "template_groups": ["cancel_open_refund_conflict", "multi_clause_cancel_conflict"], "reason": "使候选测试包含可评分的取消/退款冲突 MULTI 样本；整体移动以维持 combination 隔离。"},
    ]
    moved = {(g, m["to"]) for m in moves for g in m["template_groups"]}
    simulated = defaultdict(list)
    for c in cases:
        dest = next((to for group, to in moved if group == c["template_group"]), c["dataset_split"])
        simulated[dest].append(c)
    proposed_summary = {}
    for name in ("dev", "validation", "test_candidate"):
        subset = simulated[name]
        proposed_summary[name] = {"cases": len(subset), "template_groups": len({c["template_group"] for c in subset}), "strategy": dict(Counter(c["expected_strategy"] for c in subset)), "scorable_strategy": dict(Counter(c["expected_strategy"] for c in subset if c["scoreability"] == "SCORABLE_CANDIDATE")), "hard_negative_cases": sum(bool(c.get("hard_negative_evidence_ids")) for c in subset)}
    return {"splits": report, "policy_combination_cross_split": {k: sorted(v) for k, v in combos.items() if len(v) > 1}, "policy_chunk_count": sum(len(doc["clauses"]) for doc in read_json(POLICIES)["documents"]), "assessment": ["validation 当前没有任何 MULTI 样本，无法验证复合检索路由或多证据覆盖。", "test_candidate 的 4 条 MULTI 全部是 UNSCORABLE_CANDIDATE；可计分 MULTI 为 0。不可用它们报告检索 Recall/MRR。", "候选集有 compound_reasoning 标签，但标签数量不能代替逐模板组、可评分策略覆盖；SINGLE 也可能跨多条件。", "现有政策知识仅 10 个版本化 clause/chunk，题目增长不扩大政策主题和证据组合覆盖，Recall@5 在小语料上可能容易饱和。", "当前 policy_combination_group 在 split 间隔离；不能随机移动单行或将同组 paraphrase 拆开。"], "proposal_only": {"action": "待用户批准后才可产生新的 split 版本", "candidate_moves": moves, "projected_counts_if_approved": proposed_summary, "remaining_gap": "该草案会让 validation/test_candidate 均出现可评分 MULTI，但 validation 仍无困难负例；若必须维持证据组合隔离，需要额外补充其他组合的困难负例或评估统计能力受限。", "constraints": ["保留 template_group 整组", "同一 policy_combination_group 整体移动且不得跨 split", "测试集只用于最终一次盲测，不用于调参", "先完成标签人工审核再重划和冻结", "保留足够 dev 组用于调试；对样本不足与置信区间如实报告"], "not_applied": True}}


def main():
    dataset, policies = read_json(DATASET), read_json(POLICIES)
    cases = dataset["cases"]
    evidence = evidence_catalog(policies)
    high = high_risk_proposals(cases, evidence)
    created = datetime.now(timezone.utc).isoformat()
    proposal = {"proposal_id": "policy-gold-high-risk-r1", "created_at_utc": created, "dataset_version": dataset["dataset_version"], "cases_reviewed": 8, "template_groups": ["multi_clause_return_refund", "delivery_fact_boundary"], "candidate_modified": False, "approved_gold": False, "proposals": high, "notice": "仅为待人工审核建议；不得直接导入为 APPROVED。"}
    pjson = OUT_PROBES / "policy-gold-high-risk-revision-proposal-r1.json"
    dump_json(pjson, proposal)
    md = ["# 8 条 HIGH 金标修订提案（r1；未应用、未批准）", "", "依据政策原文和逐题问题提出复核建议。候选 JSON 未改动。条件与来源类型是非权威建议，必须由人工逐条确认。", ""]
    for item in high:
        md += [f"## {item['case_id']}", "", f"- Split：`{item['dataset_split']}`；组：`{item['template_group']}`", f"- 原始问题：{item['user_query']}", f"- 原标注：条件 `{json.dumps(item['original']['required_conditions'], ensure_ascii=False)}`；金标 `{', '.join(item['original']['acceptable_evidence_ids']) or '无'}`；结论：{item['original']['expected_conclusion']}", f"- 问题：{item['issue']}", "- 建议条件/来源/证据："]
        for cond in item["proposed"]["required_conditions"]:
            md.append(f"  - `{cond['condition_id']}` {cond['text']} — `{cond['suggested_source_type']}`；证据：{', '.join(cond['evidence_ids']) or '无'}。{cond['note']}")
        md += [f"- 建议金标证据：{', '.join(item['proposed']['acceptable_evidence_ids']) or '无'}", "- 政策原文依据："]
        for eid, text in item["policy_text_basis"].items(): md.append(f"  - `{eid}`：{text}")
        md += [f"- 建议结论：{item['proposed']['expected_conclusion']}", f"- expected_strategy 影响：{item['proposed']['expected_strategy']}", f"- 澄清行为：{item['proposed']['clarification_behavior']}", f"- 修改前后差异：{item['diff_summary']['conditions']}；证据映射 `{json.dumps(item['diff_summary']['evidence'], ensure_ascii=False)}`", "- 人工结论/依据：________；审核者/UTC 时间：________。", ""]
    pmd = OUT_REVIEW / "policy-gold-high-risk-revision-proposal-r1.md"
    pmd.write_text("\n".join(md), encoding="utf-8")

    src_rows = make_source_suggestions(cases)
    source_csv = OUT_REVIEW / "policy-retrieval-v2-source-suggestions-r1.csv"
    with source_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(src_rows[0]), extrasaction="raise")
        writer.writeheader(); writer.writerows(src_rows)

    group_path = representative_material(cases, policies)
    split = split_analysis(cases)
    split_path = OUT_PROBES / "policy-retrieval-v2-split-review-proposal-r1.json"
    dump_json(split_path, {"created_at_utc": created, "candidate_dataset_sha256": __import__("hashlib").sha256(DATASET.read_bytes()).hexdigest(), **split})

    unscorable = []
    for c in cases:
        if c["scoreability"] != "UNSCORABLE_CANDIDATE": continue
        kind = "warranty_specific_missing" if c["template_group"] == "warranty_specific_missing" else "ambiguous_paid_policy_version"
        unscorable.append({"case_id": c["case_id"], "template_group": c["template_group"], "query": c["user_query"], "scoreability": c["scoreability"], "retrieval_evidence_assessment": "可测一般政策/缺证据说明是否检索到；不可将其计入普通正例 Recall/MRR。" if kind == "warranty_specific_missing" else "两版本条款可被检索，但缺少付款时间使适用版本无法确定；不可用普通正例 Recall/MRR。", "business_outcome_assessment": "不得承诺具体型号保修期限；通用条款支持说明资料不足并转人工核对。" if kind == "warranty_specific_missing" else "不得选定版本或给确定权益；应询问付款时间，若授权且可定位订单可用业务工具读取付款事实。", "behavior_track_proposal": {"metrics": ["正确澄清/拒答率", "无依据确定性结论率（越低越好）", "必要信息识别率", "不必要澄清率", "证据/来源边界遵守率"], "human_review_required": True}, "remain_unscorable_for_retrieval": True, "status": "PENDING_HUMAN_REVIEW"})
    unsc_path = OUT_PROBES / "policy-retrieval-v2-unscorable-behavior-proposal-r1.json"
    dump_json(unsc_path, {"created_at_utc": created, "case_count": len(unscorable), "cases": unscorable, "no_reclassification": True})

    print(json.dumps({"high_risk_proposal": str(pmd.relative_to(ROOT)), "high_risk_cases": len(high), "representative_material": str(group_path.relative_to(ROOT)), "template_groups": len({c['template_group'] for c in cases}), "source_suggestion_csv": str(source_csv.relative_to(ROOT)), "condition_suggestions": len(src_rows), "split_analysis": str(split_path.relative_to(ROOT)), "unscorable_proposal": str(unsc_path.relative_to(ROOT)), "unscorable_cases": len(unscorable), "candidate_modified": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
