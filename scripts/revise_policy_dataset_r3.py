"""Create a versioned, still-unreviewed r3 candidate with the eight policy fixes."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
OLD_SOURCE_SUGGESTIONS = ROOT / "eval/review/policy-retrieval-v2-source-suggestions-r1.csv"
POLICY_FILE = ROOT / "knowledge/policies.json"
OUTPUT = ROOT / "eval/dataset/policy-retrieval-v2-candidate-20261019-r3.json"
SOURCE_OUTPUT = ROOT / "eval/review/policy-retrieval-v2-source-suggestions-r3.csv"
SPLIT_OUTPUT = ROOT / "docs/probes/policy-retrieval-v2-split-review-proposal-r3.json"
DIFF_OUTPUT = ROOT / "eval/review/policy-gold-high-risk-revision-diff-r3.md"
REVISION_ID = "policy-gold-high-risk-r3"

DELIVERY = "policies/delivery@2026-01#tracking"
RETURN_QUALITY = "policies/return-standard@2026-07#quality"
REFUND_RULE = "policies/refund@2026-01#before-shipping"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def combo_id(case: dict) -> str:
    evidence = sorted(case.get("acceptable_evidence_ids") or case.get("possible_evidence_ids", []))
    return "pc-" + sha256("|".join(evidence).encode("utf-8"))[:12]


def cond(cid: str, text: str, required: bool) -> dict:
    return {"condition_id": cid, "text": text, "required": required}


def judgment(text: str, ids: list[str], relation: str) -> dict:
    return {"condition": text, "evidence_ids": ids, "relation": relation}


def policy_versions(evidence_ids: list[str], policies: dict) -> str | list[str]:
    versions = set()
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            eid = f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"
            if eid in evidence_ids:
                versions.add(doc["version"])
    ordered = sorted(versions)
    if not ordered:
        raise ValueError(f"No policy version found for evidence {evidence_ids}")
    return ordered[0] if len(ordered) == 1 else ordered


def proposed_fields(case: dict) -> tuple[dict, dict[str, str], str]:
    cid = case["case_id"]
    if cid.startswith("pr2-delivery_fact_boundary-"):
        c1 = "物流进度以查询时的运单事件为准"
        c2 = "政策文档不能证明具体订单已发货、签收或送达的事实"
        return ({
            "required_conditions": [cond("c1", c1, True), cond("c2", c2, True)],
            "evidence_judgments": [judgment(c1, [DELIVERY], "supports"), judgment(c2, [DELIVERY], "supports")],
            "acceptable_evidence_ids": [DELIVERY],
            "expected_conclusion": "政策只能说明应以查询时的运单事件判断物流进度，不能证明这个包裹今天已送达。若用户实际要求查询该订单状态，应在具备属主授权和订单标识后查询 track_shipment；当前问题只是政策来源咨询，无需查询工具。",
            "expected_strategy": "SINGLE",
            "expected_intent": "policy_question",
            "expected_tool_calls": [],
            "business_scenario": "evidence_boundary",
        }, {"c1": "POLICY", "c2": "POLICY"}, "政策来源边界咨询；没有实际状态断言或订单查询意图，保留 SINGLE。")

    variant = cid.rsplit("-", 1)[-1]
    if variant == "01":
        fact = "题面假设商品已签收约20天且发生质量故障；退货申请获批是条件前提，不代表系统已核实真实订单状态"
        rule = "已签收订单须先有已批准的退货申请，方可申请退款"
        fields = {
            "required_conditions": [cond("c1", fact, False), cond("c3", rule, True)],
            "evidence_judgments": [judgment(fact, [], "unknown"), judgment(rule, [REFUND_RULE], "supports")],
            "acceptable_evidence_ids": [REFUND_RULE],
            "expected_conclusion": "在题面“退货审核通过”的前提下，再提交整单退款申请；退款申请本身不表示实际到账。题面问的是退款顺序，质量退货的30天资格规则不是回答该顺序所必需的检索证据。",
            "expected_strategy": "SINGLE",
            "expected_intent": "policy_question",
            "expected_tool_calls": [],
            "business_scenario": "refund_state",
            "tags": [tag for tag in case.get("tags", []) if tag != "compound_reasoning"],
        }
        sources = {"c1": "USER_PROVIDED", "c3": "POLICY"}
        rationale = "题面把退货获批作为假设；只需退款前置条款即可回答，不为问题没有询问的退货资格增加第二跳。"
    elif variant == "02":
        fact_delivered = "题面称货物已送达；这是用户提供的前提，未由业务系统核实"
        fact_window = "题面称质量问题在三十天内提出，但没有说明计时起点"
        unknown_anchor = "若要判断质量退货的30天资格，是否从签收时间起算及签收时间仍未知；此项不影响能否绕过退款审批"
        rule = "对已签收订单，须先有已批准的退货申请才能申请退款"
        fields = {
            "required_conditions": [
                cond("c1", fact_delivered, False),
                cond("c2", fact_window, False),
                cond("c3", rule, True),
                cond("c4", unknown_anchor, False),
            ],
            "evidence_judgments": [
                judgment(fact_delivered, [], "unknown"),
                judgment(fact_window, [], "unknown"),
                judgment(rule, [REFUND_RULE], "supports"),
                judgment(unknown_anchor, [], "unknown"),
            ],
            "acceptable_evidence_ids": [REFUND_RULE],
            "expected_conclusion": "不能绕过退货批准。若题面“已送达”对应政策中的已签收订单，必须先有已批准的退货申请，再申请退款。所说的“30天内提出”没有明确起算点；若要判断质量退货资格，应先核实签收时间，但这不改变退款审批前置条件。",
            "expected_strategy": "SINGLE",
            "expected_intent": "policy_question",
            "expected_tool_calls": [],
            "business_scenario": "refund_state",
            "tags": [tag for tag in case.get("tags", []) if tag != "compound_reasoning"],
        }
        sources = {"c1": "USER_PROVIDED", "c2": "USER_PROVIDED", "c3": "POLICY", "c4": "UNKNOWN"}
        rationale = "问的是退款能否绕过批准，退款条款单独足以回答；30天起算点只影响退货资格，作为未决背景记录，不人为构造 MULTI。"
    elif variant == "03":
        facts = "题面陈述商品已签收且有质量瑕疵；这是案例前提，不代表业务系统已核实"
        quality_rule = "签收后30天内的质量问题可提交整单退货申请进入人工审核；不保证批准"
        refund_rule = "已签收订单须先有已批准的退货申请，方可申请退款"
        unknown = "题面未说明质量问题发生时长，不能判定这件商品是否满足30天申请窗口"
        fields = {
            "required_conditions": [cond("c1", facts, False), cond("c2", quality_rule, True), cond("c3", refund_rule, True), cond("c4", unknown, False)],
            "evidence_judgments": [judgment(facts, [], "unknown"), judgment(quality_rule, [RETURN_QUALITY], "supports"), judgment(refund_rule, [REFUND_RULE], "supports"), judgment(unknown, [], "unknown")],
            "acceptable_evidence_ids": sorted([RETURN_QUALITY, REFUND_RULE]),
            "expected_conclusion": "两条规则的顺序是：质量问题在签收后30天内可提交整单退货申请并进入人工审核，不保证获批；已签收订单只有在退货申请获批后才能申请退款。题面没有给出质量问题发生时长，不能断定该个案满足30天窗口。",
            "expected_strategy": "MULTI",
            "expected_intent": "policy_question",
            "expected_tool_calls": [],
            "business_scenario": "multi_clause",
        }
        sources = {"c1": "USER_PROVIDED", "c2": "POLICY", "c3": "POLICY", "c4": "UNKNOWN"}
        rationale = "原问题明确询问两个政策条件如何衔接；退货资格/审核规则和退款前置规则各由不同条款支持，保留 MULTI。实际是否满足30天未知。"
    else:  # 04
        fact_signed = "题面陈述这是已签收订单；未经业务系统核验"
        fact_pending = "题面陈述退货申请仍处于审核中、尚未获批"
        rule = "已签收订单须先有已批准的退货申请，方可申请退款"
        fields = {
            "required_conditions": [cond("c1", fact_signed, False), cond("c2", fact_pending, False), cond("c3", rule, True)],
            "evidence_judgments": [judgment(fact_signed, [], "unknown"), judgment(fact_pending, [], "unknown"), judgment(rule, [REFUND_RULE], "supports")],
            "acceptable_evidence_ids": [REFUND_RULE],
            "expected_conclusion": "还没有满足退款申请的前置条件。退货申请仍在审核中不等于已批准；只有退货获批后，才可为已签收订单申请退款。本题不需要质量退货30天条款。",
            "expected_strategy": "SINGLE",
            "expected_intent": "policy_question",
            "expected_tool_calls": [],
            "business_scenario": "refund_state",
            "tags": [tag for tag in case.get("tags", []) if tag != "compound_reasoning"],
        }
        sources = {"c1": "USER_PROVIDED", "c2": "USER_PROVIDED", "c3": "POLICY"}
        rationale = "只问退款前置条件；退货处于审核中不等于批准。仅需退款条款，一条证据、SINGLE。"
    return fields, sources, rationale


def revise_dataset(source: dict, source_hash: str, policies: dict) -> tuple[dict, list[dict], list[dict]]:
    payload = deepcopy(source)
    payload["dataset_version"] = "policy-retrieval-v2-candidate-20261019-r3"
    payload["revision_parent"] = source["dataset_version"]
    payload["annotation_revision_history"] = deepcopy(source.get("annotation_revision_history", []))
    now = datetime.now(timezone.utc).isoformat()
    evidence_id_to_version = {}
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            evidence_id_to_version[f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"] = doc["version"]
    diffs = []
    condition_sources = []
    targets = {c["case_id"] for c in payload["cases"] if c["template_group"] in {"delivery_fact_boundary", "multi_clause_return_refund"}}
    if len(targets) != 8:
        raise ValueError(f"Expected 8 target rows, found {len(targets)}")
    for case in payload["cases"]:
        if case["case_id"] not in targets:
            continue
        before = {key: deepcopy(case.get(key)) for key in ("required_conditions", "evidence_judgments", "acceptable_evidence_ids", "expected_conclusion", "expected_strategy", "expected_intent", "expected_tool_calls", "business_scenario", "tags", "policy_version", "policy_combination_group")}
        fields, sources, rationale = proposed_fields(case)
        for key, value in fields.items():
            case[key] = deepcopy(value)
        case["policy_version"] = policy_versions(case["acceptable_evidence_ids"], policies)
        case["policy_combination_group"] = combo_id(case)
        case["review_status"] = "PENDING_REVIEW"
        case["reviewer"] = None
        case["reviewed_at"] = None
        # Existing human review history is preserved; this is explicitly a proposal history.
        after = {key: deepcopy(case.get(key)) for key in before}
        mapping = {
            "c1": "retained/reinterpreted with source distinction" if case["template_group"] == "delivery_fact_boundary" else "context fact only (not policy evidence)",
            "c2": "retained/reinterpreted as policy limitation" if case["template_group"] == "delivery_fact_boundary" else "split into contextual fact, unknown timing, or removed as unnecessary",
            "c3": "not applicable" if case["template_group"] == "delivery_fact_boundary" else "retained as the refund prerequisite policy condition",
        }
        if case["template_group"] == "multi_clause_return_refund" and any(c["condition_id"] == "c4" for c in case["required_conditions"]):
            mapping["new c4"] = "unknown timing anchor / missing elapsed time; not treated as evidence-backed policy fact"
        event = {"revision_id": REVISION_ID, "event_type": "AUTOMATED_CANDIDATE_ANNOTATION_REVISION", "created_at_utc": now, "actor": "Codex candidate revision proposal", "human_review_required": True, "approval_status": "PENDING_REVIEW", "reason": rationale, "condition_id_mapping": mapping, "before": before, "after": after}
        case.setdefault("annotation_revision_history", []).append(event)
        # Do not add any fake reviewer or human-review event.
        if case.get("review_history") is None:
            case["review_history"] = []
        diffs.append({"case_id": case["case_id"], "template_group": case["template_group"], "split": case["dataset_split"], "old": before, "new": after, "condition_id_mapping": mapping, "reason": rationale, "review_status": case["review_status"], "source_suggestions": sources})
        for condition in case["required_conditions"]:
            condition_sources.append({"case_id": case["case_id"], "template_group": case["template_group"], "condition_id": condition["condition_id"], "condition_text": condition["text"], "suggested_source_type": sources[condition["condition_id"]], "required": condition["required"], "rationale": rationale, "review_status": "PENDING_HUMAN_REVIEW", "authoritative": False})
    payload["annotation_revision_history"].append({"revision_id": REVISION_ID, "event_type": "VERSIONED_CANDIDATE_REVISION", "created_at_utc": now, "source_dataset_version": source["dataset_version"], "source_dataset_sha256": source_hash, "human_approval": False, "frozen": False, "changed_case_ids": [item["case_id"] for item in diffs], "reason": "Correct the eight HIGH condition/evidence-source inconsistencies; retain only evidence needed by each question."})
    return payload, diffs, condition_sources


def load_old_suggestions() -> dict:
    suggestions = {}
    if OLD_SOURCE_SUGGESTIONS.exists():
        with OLD_SOURCE_SUGGESTIONS.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                key = (row["case_id"], row["condition_id"], row["condition_text"])
                suggestions[key] = row
    return suggestions


def make_full_source_csv(cases: list[dict], changed_rows: list[dict]) -> list[dict]:
    changed = {(r["case_id"], r["condition_id"]): r for r in changed_rows}
    old = load_old_suggestions()
    result = []
    for case in cases:
        for condition in case.get("required_conditions", []):
            key = (case["case_id"], condition["condition_id"])
            manual = changed.get(key)
            previous = old.get((case["case_id"], condition["condition_id"], condition["text"]))
            if manual:
                src, why = manual["suggested_source_type"], manual["rationale"]
            elif previous:
                src, why = previous["suggested_source_type"], previous["rationale"]
            else:
                src, why = "UNKNOWN", "该条件是本次修订新引入或变更；需人工确定来源，不沿用旧标签。"
            result.append({"case_id": case["case_id"], "template_group": case["template_group"], "dataset_split": case["dataset_split"], "user_query": case["user_query"], "condition_id": condition["condition_id"], "condition_text": condition["text"], "required": condition["required"], "suggested_source_type": src, "rationale": why, "review_status": "PENDING_HUMAN_REVIEW", "authoritative": False})
    return result


def build_split_proposal(cases: list[dict], dataset_hash: str) -> dict:
    # Recalculate combos from the revised acceptable evidence, never trust inherited ids.
    split_combo_map: dict[tuple[str, ...], set[str]] = defaultdict(set)
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        groups[c["template_group"]].append(c)
        ids = tuple(sorted(c.get("acceptable_evidence_ids") or c.get("possible_evidence_ids", [])))
        if ids:
            split_combo_map[ids].add(c["dataset_split"])
    combos_by_group = {g: {c["policy_combination_group"] for c in rows} for g, rows in groups.items()}
    def group_pair(combo, expected):
        actual = sorted(g for g, ids in combos_by_group.items() if combo in ids)
        if actual != sorted(expected):
            raise ValueError(f"split proposal expected groups {expected} for {combo}; got {actual}")
        return actual
    validation_combo = "pc-dfefdbe1cbc1"
    test_combo = "pc-504161c44035"
    moves = [
        {"from": "dev", "to": "validation", "policy_combination_group": validation_combo, "template_groups": group_pair(validation_combo, ["cancel_paid_refund", "multi_clause_cancel_refund"]), "reason": "提供两个独立可评分 MULTI 模板组；整个同证据组合移动以避免分割模板或证据组合。"},
        {"from": "dev", "to": "test_candidate", "policy_combination_group": test_combo, "template_groups": group_pair(test_combo, ["cancel_open_refund_conflict", "multi_clause_cancel_conflict"]), "reason": "在保留盲测候选的前提下纳入可评分取消/退款冲突 MULTI；整个同证据组合移动。"},
    ]
    moved = {g: m["to"] for m in moves for g in m["template_groups"]}
    simulated = defaultdict(list)
    for c in cases:
        dest = moved.get(c["template_group"], c["dataset_split"])
        simulated[dest].append(c)
    projected = {}
    for split in ("dev", "validation", "test_candidate"):
        subset = simulated[split]
        projected[split] = {"cases": len(subset), "template_groups": len({c["template_group"] for c in subset}), "strategies": dict(Counter(c["expected_strategy"] for c in subset)), "scorable_strategies": dict(Counter(c["expected_strategy"] for c in subset if c["scoreability"] == "SCORABLE_CANDIDATE")), "hard_negative_cases": sum(bool(c.get("hard_negative_evidence_ids")) for c in subset), "policy_combination_groups": len({c["policy_combination_group"] for c in subset})}
    proposal_leaks = defaultdict(set)
    for split, rows in simulated.items():
        for c in rows:
            combo = tuple(sorted(c.get("acceptable_evidence_ids") or c.get("possible_evidence_ids", [])))
            if combo:
                proposal_leaks[combo].add(split)
    leakage = [{"evidence_ids": list(combo), "splits": sorted(splits)} for combo, splits in proposal_leaks.items() if len(splits) > 1]
    return {"proposal_id": "policy-retrieval-v2-split-review-r3", "created_at_utc": datetime.now(timezone.utc).isoformat(), "dataset_sha256": dataset_hash, "current": {"split_counts": {s: sum(c["dataset_split"] == s for c in cases) for s in ("dev", "validation", "test_candidate")}, "policy_combination_cross_split_leakage": [{"evidence_ids": list(k), "splits": sorted(v)} for k, v in split_combo_map.items() if len(v) > 1]}, "candidate_moves": moves, "projected_if_approved": projected, "projected_policy_combination_leakage": leakage, "review_only_not_applied": True, "risks": ["validation 仍没有困难负例；在不扩充政策知识或移动同证据组合 bundle 的约束下，困难负例分布不均。", "同模板组内路线差异经本轮显式保留，统计需按 template_group 聚类；不要把 paraphrase 当独立任务。", "test_candidate 仍须人工审核、隔日盲审和显式冻结后才能作为正式测试集。"]}


def write_source_csv(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def write_diff(path: Path, dataset: dict, diffs: list[dict], dataset_hash: str) -> None:
    lines = ["# r3 高风险修订前后对照（候选修订，未人工审核）", "", f"新候选：`{dataset['dataset_version']}`；源 r2 SHA-256：`{dataset_hash}`。此文档是可审查候选差异，所有行仍为 `PENDING_REVIEW`。", ""]
    for item in diffs:
        lines += [f"## {item['case_id']} — {item['template_group']} / {item['split']}", "", f"- 用户问题：{next(c['user_query'] for c in dataset['cases'] if c['case_id']==item['case_id'])}", f"- 原 expected_strategy → 新：`{item['old']['expected_strategy']}` → `{item['new']['expected_strategy']}`", f"- 原证据 → 新证据：`{json.dumps(item['old']['acceptable_evidence_ids'], ensure_ascii=False)}` → `{json.dumps(item['new']['acceptable_evidence_ids'], ensure_ascii=False)}`", f"- 原条件：`{json.dumps(item['old']['required_conditions'], ensure_ascii=False)}`", f"- 新条件：`{json.dumps(item['new']['required_conditions'], ensure_ascii=False)}`", f"- 新条件证据判断：`{json.dumps(item['new']['evidence_judgments'], ensure_ascii=False)}`", f"- 新结论：{item['new']['expected_conclusion']}", f"- condition_id 映射：`{json.dumps(item['condition_id_mapping'], ensure_ascii=False)}`", f"- 修订理由：{item['reason']}", f"- 来源类型建议：`{json.dumps(item['source_suggestions'], ensure_ascii=False)}`（待人工确认，不写入审核决定）", f"- 状态：`{item['review_status']}`；自动批准：否", ""]
    with path.open("x", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    for path in (OUTPUT, SOURCE_OUTPUT, SPLIT_OUTPUT, DIFF_OUTPUT):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite versioned artifact: {path}")
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    source_hash = sha256(SOURCE.read_bytes())
    payload, diffs, changed_sources = revise_dataset(source, source_hash, policies)
    # Check all case ids, routes, condition IDs, judgment coverage, and recalculated combination IDs before writing.
    if len(payload["cases"]) != len(source["cases"]) or len(diffs) != 8:
        raise ValueError("case count or revision target count changed")
    for case in payload["cases"]:
        ids = [c["condition_id"] for c in case["required_conditions"]]
        texts = [c["text"] for c in case["required_conditions"]]
        if len(ids) != len(set(ids)) or len(texts) != len(set(texts)):
            raise ValueError(f"duplicate condition id/text: {case['case_id']}")
        if case["expected_strategy"] not in {"SINGLE", "MULTI"}:
            raise ValueError(f"invalid expected_strategy: {case['case_id']} {case['expected_strategy']}")
        if {c["text"] for c in case["required_conditions"]} != {j["condition"] for j in case["evidence_judgments"]}:
            raise ValueError(f"condition/evidence judgment mismatch: {case['case_id']}")
        if case["policy_combination_group"] != combo_id(case):
            raise ValueError(f"stale policy_combination_group: {case['case_id']}")
    output_bytes = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    OUTPUT.write_bytes(output_bytes)
    source_rows = make_full_source_csv(payload["cases"], changed_sources)
    write_source_csv(SOURCE_OUTPUT, source_rows)
    split_report = build_split_proposal(payload["cases"], sha256(output_bytes))
    with SPLIT_OUTPUT.open("x", encoding="utf-8") as f:
        json.dump(split_report, f, ensure_ascii=False, indent=2); f.write("\n")
    write_diff(DIFF_OUTPUT, payload, diffs, source_hash)
    print(json.dumps({"dataset": str(OUTPUT.relative_to(ROOT)), "dataset_version": payload["dataset_version"], "source_sha256": source_hash, "new_sha256": sha256(output_bytes), "changed_cases": len(diffs), "strategy_counts_changed_group": dict(Counter(c["expected_strategy"] for c in payload["cases"] if c["template_group"] == "multi_clause_return_refund")), "source_suggestion_rows": len(source_rows), "split_proposal": str(SPLIT_OUTPUT.relative_to(ROOT)), "candidate_statuses": dict(Counter(c["review_status"] for c in payload["cases"])), "human_approved": False, "frozen": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
