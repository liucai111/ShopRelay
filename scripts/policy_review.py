"""Prepare auditable human-review materials and safely import reviewed CSVs.

This tool never approves data by itself. Approval requires explicit review fields
in the CSV and is recorded in a new, immutable dataset artifact.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
POLICY_FILE = ROOT / "knowledge/policies.json"
ABLATION_FILE = ROOT / "docs/probes/retrieval-ablation-20261018T154644Z.json"
R3_SOURCE_SUGGESTIONS = ROOT / "eval/review/policy-retrieval-v2-source-suggestions-final-r7.csv"
REVIEW_DIR = ROOT / "eval/review"
AUDIT_DIR = ROOT / "docs/probes"
ALLOWED_REVIEW_STATUSES = {"PENDING_REVIEW", "NEEDS_CORRECTION", "APPROVED"}
ALLOWED_SOURCE_TYPES = {"USER_PROVIDED", "POLICY", "BUSINESS_TOOL", "UNKNOWN"}
EDITABLE_JSON_FIELDS = (
    "user_query", "policy_version", "evaluation_at", "required_conditions",
    "acceptable_evidence_ids", "possible_evidence_ids", "hard_negative_evidence_ids",
    "evidence_judgments", "expected_conclusion", "expected_intent", "expected_strategy",
    "expected_tool_calls", "expected_business_effect", "critical_facts", "scoreability",
    "unscorable_reason",
)
JSON_FIELDS = {
    "policy_version", "required_conditions", "acceptable_evidence_ids", "possible_evidence_ids",
    "hard_negative_evidence_ids", "evidence_judgments", "expected_tool_calls",
    "expected_business_effect", "critical_facts", "evidence_source_types",
}
CSV_FIELDS = [
    "case_id", "base_dataset_sha256", "base_case_sha256", "dataset_split", "template_group",
    "business_scenario", "user_query", "policy_version", "evaluation_at", "required_conditions",
    "condition_source_suggestions_READ_ONLY", "evidence_source_types", "acceptable_evidence_ids",
    "possible_evidence_ids", "hard_negative_evidence_ids", "evidence_judgments",
    "expected_conclusion", "expected_intent", "expected_strategy", "expected_tool_calls",
    "expected_business_effect", "critical_facts", "scoreability", "unscorable_reason",
    "review_status", "reviewer", "reviewed_at", "review_notes",
]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def case_hash(case: dict) -> str:
    return sha256_bytes(canonical_bytes(case))


def encode_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def parse_json_cell(raw: str, field: str) -> Any:
    if raw == "" and field in ("expected_business_effect", "policy_version", "unscorable_reason"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field}: invalid JSON: {exc.msg}") from exc


def suggested_source_types(case: dict) -> dict[str, str]:
    """Heuristic suggestions for a reviewer; suggestions are never imported."""
    # Prefer the versioned, case/condition-specific proposal for r3. It remains
    # informational and is copied only into the read-only CSV column.
    if R3_SOURCE_SUGGESTIONS.exists() and str(case.get("case_id", "")).startswith("pr2-"):
        try:
            with R3_SOURCE_SUGGESTIONS.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = csv.DictReader(stream)
                matches = {(row["condition_id"], row["condition_text"]): row["suggested_source_type"] for row in rows if row["case_id"] == case["case_id"]}
            if all((c.get("condition_id"), c.get("text")) in matches for c in case.get("required_conditions", [])):
                return {c["condition_id"]: matches[(c["condition_id"], c["text"])] for c in case.get("required_conditions", [])}
        except (OSError, KeyError, csv.Error):
            pass
    result = {}
    for condition in case.get("required_conditions", []):
        text = condition.get("text", "")
        if any(term in text for term in ("订单实时", "实时订单", "物流事件", "具体包裹", "实际签收时间", "实时状态")):
            value = "BUSINESS_TOOL"
        elif any(term in text for term in ("签收", "付款", "发货", "未发货", "已发货", "已付款", "未付款", "商品为", "产品为", "用户请求", "包裹", "订单状态", "用户表示")):
            value = "USER_PROVIDED"
        elif any(term in text for term in ("政策", "条款", "规则", "有效版本", "时限", "期限", "例外", "申请条件", "应转人工", "转人工")):
            value = "POLICY"
        else:
            value = "UNKNOWN"
        result[condition.get("condition_id", "<missing>")] = value
    return result


def export_review_csv(dataset_path: Path, output: Path) -> None:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    base_hash = sha256_file(dataset_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        for case in dataset["cases"]:
            row = {key: encode_cell(case.get(key)) for key in CSV_FIELDS if key in case}
            row.update({
                "base_dataset_sha256": base_hash,
                "base_case_sha256": case_hash(case),
                "condition_source_suggestions_READ_ONLY": encode_cell(suggested_source_types(case)),
                "evidence_source_types": "",
                "reviewer": case.get("reviewer") or "",
                "reviewed_at": case.get("reviewed_at") or "",
                "review_notes": case.get("review_notes") or "",
            })
            writer.writerow(row)


def read_diagnostic(dataset_path: Path) -> dict[str, dict]:
    if not ABLATION_FILE.exists():
        return {}
    payload = json.loads(ABLATION_FILE.read_text(encoding="utf-8"))
    # A diagnostic ranking is usable only for the exact dataset snapshot it scored.
    expected_hash = sha256_file(dataset_path)
    if payload.get("dataset_sha256") != expected_hash:
        return {}
    result = {}
    for case in payload.get("cases", []):
        # Deliberately exclude test_candidate rows from ranking-based review priority.
        if case.get("dataset_split") in ("dev", "validation"):
            tops = {name: ranked[0] if ranked else None for name, ranked in case.get("rankings", {}).items()}
            result[case["case_id"]] = {"top1_by_method": tops, "different_top1": len(set(tops.values())) > 1}
    return result


def build_audit(dataset: dict, policies: dict, dataset_path: Path) -> dict:
    cases = dataset["cases"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        groups[case["template_group"]].append(case)
    evidence = {}
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            eid = f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"
            evidence[eid] = {"id": eid, "policy_id": doc["policy_id"], "version": doc["version"], "title": doc["title"], "text": clause["text"], "effective_from": doc["effective_from"], "effective_to": doc["effective_to"]}

    findings: list[dict] = []
    def add(case: dict, issue_type: str, severity: str, current: Any, recommendation: str, basis: str) -> None:
        findings.append({"case_id": case["case_id"], "template_group": case["template_group"], "dataset_split": case["dataset_split"], "issue_type": issue_type, "severity": severity, "current_annotation": current, "suggested_correction": recommendation, "basis": basis, "review_required": True, "auto_applied": False})

    # Evidence-source provenance is absent in the candidate schema for every case.
    for case in cases:
        add(case, "evidence_source_type_missing", "MEDIUM", {"required_conditions": case["required_conditions"], "evidence_judgments": case["evidence_judgments"]}, "During human review, annotate each condition_id with USER_PROVIDED, POLICY, BUSINESS_TOOL, or UNKNOWN in the new review CSV. Keep user/order facts separate from policy rules; these suggestions are informational only.", "The candidate schema has evidence_judgments but no source type; a policy clause may support the rule without verifying the user-specific premise.")

    for case in cases:
        group = case["template_group"]
        if group == "multi_clause_return_refund":
            quality_id = "policies/return-standard@2026-07#quality"
            refund_id = "policies/refund@2026-01#before-shipping"
            wrong = [j for j in case["evidence_judgments"] if ("30天" in j.get("condition", "") or "质量问题" in j.get("condition", "")) and refund_id in j.get("evidence_ids", [])]
            if wrong or (case.get("expected_strategy") == "MULTI" and quality_id not in case.get("acceptable_evidence_ids", [])):
                add(case, "condition_evidence_misattribution", "HIGH", {"misattributed_judgments": wrong, "gold": case["acceptable_evidence_ids"]}, "核对质量退货30天规则是否只映射到退货质量条款、退款批准前置条件是否映射到退款条款；按原问题判断是否确需两条证据。", "质量退货窗口和退款前置条件分别由不同政策条款规定。")
        elif group == "delivery_fact_boundary":
            delivery_id = "policies/delivery@2026-01#tracking"
            fact_judgments = [j for j in case["evidence_judgments"] if ("用户" in j.get("condition", "") or "未经业务系统" in j.get("condition", "")) and delivery_id in j.get("evidence_ids", [])]
            if fact_judgments or (case.get("expected_intent") == "order_status" and "track_shipment" not in case.get("expected_tool_calls", [])):
                add(case, "user_fact_attributed_to_policy", "HIGH", {"fact_judgments": fact_judgments, "order_snapshot_id": case.get("order_snapshot_id"), "expected_tool_calls": case.get("expected_tool_calls")}, "分离用户陈述、政策来源规则和需业务工具核实的实时订单事实；只有实际查询意图才路由到 track_shipment。", "政策文本不能证明特定订单的实际物流状态。")
        elif group == "ambiguous_paid_policy_version":
            add(case, "clarification_case_outside_retrieval_score", "MEDIUM", {"scoreability": case["scoreability"], "expected_strategy": case["expected_strategy"], "acceptable_evidence_ids": case["acceptable_evidence_ids"], "possible_evidence_ids": case["possible_evidence_ids"]}, "Keep out of retrieval ranking. Consider a separate clarification/refusal evaluation where the target is asking for paid_at or explicitly declining to choose a version; define that track only after human review.", "Without paid_at the contract says not to guess the paid-time policy version. This is an abstention/clarification behavior, not an ordinary retrieval-recall label.")
        elif group == "warranty_specific_missing":
            add(case, "clarification_case_outside_retrieval_score", "MEDIUM", {"scoreability": case["scoreability"], "acceptable_evidence_ids": case["acceptable_evidence_ids"], "unscorable_reason": case["unscorable_reason"]}, "Keep out of retrieval ranking. Consider separate evidence-insufficiency/clarification scoring; the general warranty clause can support the limitation, but not a model-specific term.", "Business rules say a missing product-specific warranty term returns insufficient evidence; do not derive a concrete product promise from the generic clause.")

    # Report condition/strategy and template dependency for every group, not an iid count.
    # Do not expose model rankings in the blind-review packet. Old runs also
    # cannot be associated with a revised candidate by case_id alone.
    diagnostic = {}
    group_rows = []
    total_pairs = 0
    similarity_values = []
    for group_name, rows in sorted(groups.items()):
        representative = next((c for c in rows if c.get("variant") == 1), rows[0])
        similarity = []
        for idx, left in enumerate(rows):
            for right in rows[idx + 1:]:
                a = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", left["user_query"]).casefold())
                b = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", right["user_query"]).casefold())
                # Small normalized edit similarity used only as a review aid.
                from difflib import SequenceMatcher
                similarity.append(SequenceMatcher(None, a, b, autojunk=False).ratio())
        total_pairs += len(similarity)
        similarity_values.extend(similarity)
        ranks = {c["case_id"]: diagnostic.get(c["case_id"], {}) for c in rows}
        group_rows.append({
            "template_group": group_name, "case_ids": [c["case_id"] for c in rows], "representative_case_id": representative["case_id"],
            "dataset_split": sorted({c["dataset_split"] for c in rows}), "business_scenario": sorted({c["business_scenario"] for c in rows}),
            "policy_combination_groups": sorted({c["policy_combination_group"] for c in rows}),
            "expected_strategies": dict(Counter(c["expected_strategy"] for c in rows)),
            "scoreability": dict(Counter(c["scoreability"] for c in rows)),
            "positive_evidence_count": sum(bool(c["acceptable_evidence_ids"]) for c in rows),
            "hard_negative_count": sum(bool(c["hard_negative_evidence_ids"]) for c in rows),
            "max_pairwise_paraphrase_similarity": round(max(similarity, default=0), 3),
            "diagnostic_top1_variation_dev_validation_only": {cid: item.get("top1_by_method") for cid, item in ranks.items() if item.get("different_top1")},
        })

    split_summary: dict[str, dict] = {}
    for split in ("dev", "validation", "test_candidate"):
        subset = [c for c in cases if c["dataset_split"] == split]
        split_summary[split] = {
            "case_count": len(subset), "template_group_count": len({c["template_group"] for c in subset}),
            "policy_combination_group_count": len({c["policy_combination_group"] for c in subset}),
            "business_scenario_counts": dict(Counter(c["business_scenario"] for c in subset)),
            "expected_strategy_counts": dict(Counter(c["expected_strategy"] for c in subset)),
            "scorable_counts": dict(Counter(c["scoreability"] for c in subset)),
            "compound_tag_count": sum("compound_reasoning" in c.get("tags", []) for c in subset),
            "hard_negative_case_count": sum(bool(c.get("hard_negative_evidence_ids")) for c in subset),
        }
    issues_by_severity = Counter(item["severity"] for item in findings)
    issues_by_type = Counter(item["issue_type"] for item in findings)
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from validate_policy_retrieval_dataset import audit as structural_audit
    structural = structural_audit(dataset, policies)
    return {
        "report_type": "policy_retrieval_gold_quality_audit",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset["dataset_version"], "dataset_path": str(dataset_path.relative_to(ROOT)),
        "dataset_sha256": sha256_file(dataset_path), "source_policy_sha256": sha256_file(POLICY_FILE),
        "cases_audited": len(cases), "template_groups_audited": len(groups),
        "automatic_approval": False, "candidate_json_modified": False,
        "summary": {"finding_count": len(findings), "by_severity": dict(issues_by_severity), "by_type": dict(issues_by_type), "unique_cases_with_findings": len({f["case_id"] for f in findings})},
        "structural_validation": {"status": structural["status"], "problem_count": len(structural["problems"]), "warning_count": len(structural["warnings"]), "problems": structural["problems"], "warnings": structural["warnings"], "split_counts": structural["split_counts"], "template_group_count": structural["template_group_count"], "policy_combination_group_count": structural["policy_combination_group_count"], "cross_split_near_duplicate_pairs": structural["cross_split_near_duplicate_pairs"]},
        "critical_manual_review_findings": [f for f in findings if f["severity"] == "HIGH"],
        "findings": findings,
        "group_audit": group_rows,
        "design_risks": [
            {"risk": "correlated_paraphrases", "severity": "HIGH", "detail": "Rows are 28 template groups x 4 paraphrases; use template_group as the statistical unit for paired bootstrap and confidence intervals."},
            {"risk": "small_validation_group_count", "severity": "HIGH", "detail": f"Validation has {split_summary['validation']['template_group_count']} template groups and only SINGLE strategy candidates; it cannot validate multi-hop/routing behavior robustly."},
            {"risk": "hard_negative_coverage", "severity": "MEDIUM", "detail": "Hard-negative cases are sparse and unevenly split; validation currently has none, so validation cannot quantify hard-negative rejection."},
            {"risk": "unscorable_clarification_track", "severity": "MEDIUM", "detail": "The eight missing-information cases are excluded from retrieval metrics; clarification/refusal behavior needs a separately defined and human-reviewed outcome track."},
            {"risk": "limited_policy_knowledge", "severity": "HIGH", "detail": "The source knowledge base has only 10 versioned policy chunks. 112 question rows do not expand policy-domain coverage."},
            {"risk": "candidate_test_not_frozen", "severity": "HIGH", "detail": "test_candidate remains an unfrozen candidate and must not drive tuning or label changes."},
        ],
        "split_design": {
            "split_summary": split_summary,
            "within_template_pairwise_query_similarity_pairs": total_pairs,
            "within_template_pairwise_similarity_mean": round(sum(similarity_values) / len(similarity_values), 3) if similarity_values else None,
            "within_template_pairwise_similarity_max": round(max(similarity_values), 3) if similarity_values else None,
            "interpretation": "112 rows are 28 scenario/template clusters with four paraphrases each; confidence intervals and future paired bootstrap should resample template_group clusters, not treat paraphrases as independent business scenarios. Only 10 versioned policy chunks exist; row count does not expand knowledge coverage.",
            "test_candidate_blinding": "Model rankings are intentionally omitted from test_candidate review priority and group materials. Do not use test_candidate rankings to modify labels or tune retrieval.",
        },
        "policy_evidence_catalog": evidence,
        "notice": "All items are audit recommendations. No annotation was approved or changed. Human review is required.",
    }


def markdown_cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def generate_group_packet(report: dict, dataset: dict, output: Path) -> None:
    policies = report["policy_evidence_catalog"]
    issues_by_group: dict[str, list[dict]] = defaultdict(list)
    for item in report["findings"]:
        issues_by_group[item["template_group"]].append(item)
    lines = [
        "# ShopRelay 政策检索候选集：逐模板组人工审核材料（未审核、未冻结）", "",
        f"数据集：`{report['dataset_version']}`；SHA-256：`{report['dataset_sha256']}`。本材料包含全部 {report['cases_audited']} 条、{report['template_groups_audited']} 组；每组先列代表题，再列其余变体。自动发现均为待核查建议，不是批准结论。", "",
        "> **盲审提示：** test_candidate 题不显示检索排名。请先审核问题、证据和结论，再考虑模型结果。不得按模型表现修改答案。", "",
    ]
    groups = report["group_audit"]
    high_groups = {item["template_group"] for item in report["critical_manual_review_findings"]}
    unscorable_groups = {item["template_group"] for item in report["findings"] if item["issue_type"] == "clarification_case_outside_retrieval_score"}
    def review_priority(group):
        name = group["template_group"]
        if name in high_groups:
            return (0, name)
        if name in unscorable_groups:
            return (1, name)
        if group["diagnostic_top1_variation_dev_validation_only"]:
            return (2, name)
        if any(token in name for token in ("version", "boundary", "refund", "delivery", "cancel")):
            return (3, name)
        return (4, name)
    groups = sorted(groups, key=review_priority)
    cases_by_group: dict[str, list[dict]] = defaultdict(list)
    for case in dataset["cases"]:
        cases_by_group[case["template_group"]].append(case)
    for group in groups:
        gname = group["template_group"]
        case_map = {c["case_id"]: c for c in cases_by_group[gname]}
        lines.extend([f"## {gname} — split: {', '.join(group['dataset_split'])}; scenario: {', '.join(group['business_scenario'])}", ""])
        group_issues = [item for item in issues_by_group[gname] if item["issue_type"] != "evidence_source_type_missing"]
        if group_issues:
            lines.append("**自动审计提示（待人核实）**")
            for issue in group_issues:
                lines.append(f"- [{issue['severity']}] {issue['issue_type']}: {issue['suggested_correction']}")
            lines.append("")
        lines.append(f"政策证据组合：`{'; '.join(group['policy_combination_groups'])}`。复合标签数：{sum('compound_reasoning' in case_map[cid].get('tags', []) for cid in group['case_ids'])}。组内改写最大文本相似度：{group['max_pairwise_paraphrase_similarity']}（只作重复风险提示）。")
        for idx, cid in enumerate(group["case_ids"]):
            case = case_map[cid]
            lines.extend(["", f"### {'代表样本' if cid == group['representative_case_id'] else '变体'}：`{cid}`", "",
                          f"- 划分 / 审核：`{case['dataset_split']}` / `{case['review_status']}`", f"- 用户问题：{case['user_query']}",
                          f"- 时间：`{case['evaluation_at']}`；政策版本：`{encode_cell(case['policy_version'])}`；知识版本：`{case['knowledge_version']}`",
                          f"- 必要条件：`{json.dumps(case['required_conditions'], ensure_ascii=False)}`",
                          f"- 当前条件证据判断：`{json.dumps(case['evidence_judgments'], ensure_ascii=False)}`",
                          f"- 候选金标：`{json.dumps(case['acceptable_evidence_ids'], ensure_ascii=False)}`；可能证据：`{json.dumps(case['possible_evidence_ids'], ensure_ascii=False)}`；困难负例：`{json.dumps(case['hard_negative_evidence_ids'], ensure_ascii=False)}`",
                          f"- 来源类型待填写：`{json.dumps(suggested_source_types(case), ensure_ascii=False)}`（启发式建议，需审核；请在 CSV `evidence_source_types` 人工填写最终判断）",
                          f"- 预期结论：{case['expected_conclusion']}", f"- 策略 / 可评分：`{case['expected_strategy']}` / `{case['scoreability']}`；原因：{case.get('unscorable_reason') or '—'}",
                          f"- 审核状态 / 审核者 / 时间 / 备注：`{case['review_status']}` / `{case.get('reviewer') or '—'}` / `{case.get('reviewed_at') or '—'}` / {markdown_cell(case.get('review_notes') or '待填写')}"])
            eids = list(dict.fromkeys(case["acceptable_evidence_ids"] + case["possible_evidence_ids"] + case["hard_negative_evidence_ids"]))
            lines.append("- 对应政策原文：")
            if eids:
                for eid in eids:
                    entry = policies.get(eid)
                    if entry:
                        lines.append(f"  - `{eid}`（{entry['title']}；{entry['effective_from']} 至 {entry['effective_to'] or '无截止'}）：{entry['text']}")
                    else:
                        lines.append(f"  - `{eid}`：**政策原文未找到**")
            else:
                lines.append("  - 无候选金标证据（重点判断是否应澄清/拒答或移入独立评测轨道）")
            lines.append("- 人工填写：判定（APPROVED / NEEDS_CORRECTION / PENDING_REVIEW）、reviewer、reviewed_at（含时区）、review_notes；逐项填写 evidence_source_types。")
        lines.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")


def prepare(dataset_path: Path, review_tag: str = "r4") -> tuple[Path, Path, Path]:
    if not re.fullmatch(r"r[0-9]+", review_tag):
        raise ValueError("review tag must look like r4, r5, ...")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    report = build_audit(dataset, policies, dataset_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = AUDIT_DIR / f"policy-gold-audit-{stamp}.json"
    csv_path = REVIEW_DIR / f"policy-retrieval-v2-review-{review_tag}.csv"
    packet_path = REVIEW_DIR / f"policy-retrieval-v2-groups-{review_tag}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    export_review_csv(dataset_path, csv_path)
    generate_group_packet(report, dataset, packet_path)
    return report_path, csv_path, packet_path


def validate_review_timestamp(value: str, case_id: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{case_id}: reviewed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{case_id}: reviewed_at must include an explicit timezone")
    if parsed.astimezone(timezone.utc) > datetime.now(timezone.utc):
        raise ValueError(f"{case_id}: reviewed_at cannot be in the future")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_row(row: dict[str, str], base: dict) -> dict:
    changes = {}
    for key in EDITABLE_JSON_FIELDS:
        if key not in row:
            raise ValueError(f"CSV missing column: {key}")
        current = base.get(key)
        if key == "policy_version" and isinstance(current, str):
            value = row[key]
        elif key in JSON_FIELDS:
            value = parse_json_cell(row[key], key)
        elif key == "policy_version" and isinstance(current, (dict, list)):
            value = parse_json_cell(row[key], key)
        else:
            value = row[key] if row[key] != "" else None
        changes[key] = value
    if "evidence_source_types" not in row:
        raise ValueError("CSV missing column: evidence_source_types")
    sources = parse_json_cell(row["evidence_source_types"], "evidence_source_types") if row["evidence_source_types"] else None
    if sources is not None and not isinstance(sources, dict):
        raise ValueError(f"{base['case_id']}: evidence_source_types must be a JSON object")
    changes["evidence_source_types"] = sources
    for field in ("review_status", "reviewer", "reviewed_at", "review_notes"):
        if field not in row:
            raise ValueError(f"CSV missing column: {field}")
    changes["review_status"] = row["review_status"].strip()
    changes["reviewer"] = row["reviewer"].strip() or None
    changes["reviewed_at"] = row["reviewed_at"].strip() or None
    changes["review_notes"] = row["review_notes"].strip()
    return changes


def validate_reviewed_case(case: dict) -> None:
    cid = case["case_id"]
    status = case.get("review_status")
    if status not in ALLOWED_REVIEW_STATUSES:
        raise ValueError(f"{cid}: unsupported review_status {status!r}")
    sources = case.get("evidence_source_types")
    if sources is not None:
        expected = {c["condition_id"] for c in case.get("required_conditions", [])}
        if set(sources) != expected:
            raise ValueError(f"{cid}: evidence_source_types must cover exactly condition IDs {sorted(expected)}")
        invalid = {key: value for key, value in sources.items() if value not in ALLOWED_SOURCE_TYPES}
        if invalid:
            raise ValueError(f"{cid}: invalid evidence source values: {invalid}")
    if case.get("scoreability") not in ("SCORABLE_CANDIDATE", "UNSCORABLE_CANDIDATE"):
        raise ValueError(f"{cid}: invalid scoreability")
    case["scorable"] = case["scoreability"] == "SCORABLE_CANDIDATE"
    if case["scorable"] and case.get("unscorable_reason"):
        raise ValueError(f"{cid}: scorable case cannot retain unscorable_reason")
    if not case["scorable"] and not case.get("unscorable_reason"):
        raise ValueError(f"{cid}: unscorable case needs unscorable_reason")
    if case.get("expected_strategy") not in ("SINGLE", "MULTI"):
        raise ValueError(f"{cid}: expected_strategy must be SINGLE or MULTI for this policy retrieval dataset")
    if status == "APPROVED":
        if not case.get("reviewer") or not case.get("reviewed_at") or not case.get("review_notes", "").strip():
            raise ValueError(f"{cid}: APPROVED requires reviewer, timezone-aware reviewed_at, and review_notes")
        required_ids = {c["condition_id"] for c in case.get("required_conditions", [])}
        if not isinstance(sources, dict) or set(sources) != required_ids:
            raise ValueError(f"{cid}: APPROVED requires a source decision for every required condition")


def import_reviews(dataset_path: Path, csv_path: Path, output_dir: Path | None = None) -> Path:
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    base_dataset_hash = sha256_bytes(dataset_bytes)
    csv_hash = sha256_file(csv_path)
    import_id = sha256_bytes(f"{base_dataset_hash}:{csv_hash}".encode("ascii"))
    cases_by_id = {case["case_id"]: case for case in dataset["cases"]}
    if len(cases_by_id) != len(dataset["cases"]):
        raise ValueError("source dataset contains duplicate case_id")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or any(field not in reader.fieldnames for field in CSV_FIELDS):
            raise ValueError("review CSV has missing required columns; regenerate from the selected source dataset")
        rows = list(reader)
    ids = [row.get("case_id", "").strip() for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("review CSV contains duplicate case_id")
    if set(ids) != set(cases_by_id):
        raise ValueError(f"CSV/JSON case_id conflict: missing={sorted(set(cases_by_id)-set(ids))[:5]}, extra={sorted(set(ids)-set(cases_by_id))[:5]}")
    updated_by_id = {}
    changed_count = 0
    for row in rows:
        cid = row["case_id"].strip()
        original = cases_by_id[cid]
        if row.get("base_dataset_sha256") != base_dataset_hash:
            raise ValueError(f"{cid}: stale/conflicting CSV base_dataset_sha256")
        if row.get("base_case_sha256") != case_hash(original):
            raise ValueError(f"{cid}: stale/conflicting CSV base_case_sha256; source JSON changed after export")
        if row.get("dataset_split") != original["dataset_split"] or row.get("template_group") != original["template_group"] or row.get("business_scenario") != original["business_scenario"]:
            raise ValueError(f"{cid}: read-only dataset metadata conflicts with source JSON")
        expected_suggestions = encode_cell(suggested_source_types(original))
        if row.get("condition_source_suggestions_READ_ONLY") != expected_suggestions:
            raise ValueError(f"{cid}: read-only source suggestions were altered; regenerate review material instead")
        changes = parse_row(row, original)
        if changes["review_status"] not in ALLOWED_REVIEW_STATUSES:
            raise ValueError(f"{cid}: invalid review_status")
        mutated = dict(original)
        before_after = {}
        for key, value in changes.items():
            previous = original.get(key)
            if previous != value:
                before_after[key] = {"before": previous, "after": value}
                mutated[key] = value
        # No change to source classifications means keep a pre-existing JSON decision.
        if changes["evidence_source_types"] is None and "evidence_source_types" in original:
            mutated["evidence_source_types"] = original["evidence_source_types"]
            before_after.pop("evidence_source_types", None)
        if before_after:
            if not (changes.get("reviewer") and changes.get("reviewed_at") and changes.get("review_notes", "").strip()):
                raise ValueError(f"{cid}: any annotation/review change requires reviewer, timezone-aware reviewed_at, and review_notes")
            mutated["reviewed_at"] = validate_review_timestamp(changes["reviewed_at"], cid)
            validate_reviewed_case(mutated)
            mutated.setdefault("review_history", []).append({
                "event_id": f"{import_id[:12]}:{cid}", "event_type": "HUMAN_REVIEW_IMPORT",
                "review_import_id": import_id, "source_csv_sha256": csv_hash,
                "reviewer": mutated["reviewer"], "reviewed_at": mutated["reviewed_at"],
                "reason": mutated["review_notes"], "changes": before_after,
            })
            changed_count += 1
        else:
            # A repeated import is a no-op, and an unreviewed row does not acquire approval.
            validate_reviewed_case(mutated)
        updated_by_id[cid] = mutated
    if changed_count == 0:
        raise ValueError("review CSV contains no changes; no output dataset created")

    # Re-run evidence IDs, policy version boundaries, split leakage and schema checks.
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from validate_policy_retrieval_dataset import audit as validate_dataset
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    result = dict(dataset)
    # Keep canonical dataset order; matching and edits are keyed only by case_id.
    result["cases"] = [updated_by_id[case["case_id"]] for case in dataset["cases"]]
    result["dataset_version"] = f"{dataset['dataset_version']}-humanreview-{import_id[:12]}"
    result["status"] = "CANDIDATE_REVIEWED_NOT_FROZEN"
    result["review_import"] = {"import_id": import_id, "source_dataset_sha256": base_dataset_hash, "source_csv_sha256": csv_hash, "changed_case_count": changed_count, "imported_at_utc": datetime.now(timezone.utc).isoformat(), "output_is_frozen": False}
    quality = validate_dataset(result, policies)
    if quality["problems"]:
        raise ValueError("post-import dataset validation failed: " + json.dumps(quality["problems"][:8], ensure_ascii=False))
    out_dir = output_dir or (ROOT / "eval/dataset/reviewed")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"policy-retrieval-reviewed-{import_id[:12]}.json"
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        if existing.get("review_import", {}).get("import_id") == import_id:
            return out_path
        raise FileExistsError(f"refusing to overwrite unrelated output: {out_path}")
    with out_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p_prepare.add_argument("--review-tag", default="r4", help="new review-material revision label (e.g. r5); never overwrites an existing revision")
    p_import = sub.add_parser("import-csv")
    p_import.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p_import.add_argument("--csv", type=Path, required=True)
    p_import.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        report, review, packet = prepare(args.dataset, args.review_tag)
        print(json.dumps({"audit_report": str(report), "review_csv": str(review), "group_packet": str(packet)}, ensure_ascii=False, indent=2))
    else:
        output = import_reviews(args.dataset, args.csv, args.output_dir)
        print(json.dumps({"status": "IMPORTED_TO_NEW_VERSIONED_DATASET_NOT_FROZEN", "output_dataset": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileExistsError) as exc:
        print(f"REVIEW_IMPORT_REJECTED: {exc}", file=sys.stderr)
        raise SystemExit(2)
