"""Audit candidate policy-retrieval data without approving any annotation."""
from __future__ import annotations

import difflib
import hashlib
import json
import argparse
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
POLICY_FILE = ROOT / "knowledge/policies.json"


def normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)


def audit(payload: dict, policies: dict) -> dict:
    problems: list[dict] = []
    warnings: list[dict] = []
    evidence: dict[str, dict] = {}
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            evidence[f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"] = doc
    cases = payload.get("cases", [])
    ids = [c.get("case_id") for c in cases]
    duplicate_ids = [key for key, count in Counter(ids).items() if count != 1]
    for cid in duplicate_ids:
        problems.append({"type": "duplicate_case_id", "case_id": cid})
    exact: dict[str, str] = {}
    group_splits: dict[str, set[str]] = defaultdict(set)
    policy_combination_splits: dict[tuple[str, ...], set[str]] = defaultdict(set)
    policy_combination_cases: dict[tuple[str, ...], list[str]] = defaultdict(list)
    normalized_rows = []
    required = ("case_id", "source", "business_scenario", "template_group", "group_id", "dataset_split", "user_query", "evaluation_at", "policy_version", "knowledge_version", "user_id", "session_history", "order_snapshot_id", "expected_intent", "expected_strategy", "required_conditions", "acceptable_evidence_ids", "expected_tool_calls", "expected_business_effect", "critical_facts", "expected_conclusion", "scoreability", "annotator", "review_status")
    per_split = Counter()
    per_category = Counter()
    category_by_split = Counter()
    review_counts = Counter()
    compound = []
    eligible_by_split = Counter()
    for case in cases:
        cid = case.get("case_id", "<missing>")
        missing = [key for key in required if key not in case]
        if missing:
            problems.append({"type": "missing_fields", "case_id": cid, "fields": missing})
            continue
        query = case["user_query"]
        qnorm = normalize_query(query)
        if not qnorm:
            problems.append({"type": "empty_query", "case_id": cid})
        if qnorm in exact:
            problems.append({"type": "duplicate_query", "case_id": cid, "duplicate_of": exact[qnorm]})
        else:
            exact[qnorm] = cid
        split, group = case.get("dataset_split"), case["template_group"]
        group_splits[group].add(split)
        combination = tuple(sorted(case.get("acceptable_evidence_ids") or case.get("possible_evidence_ids", [])))
        if combination:
            policy_combination_splits[combination].add(split)
            policy_combination_cases[combination].append(cid)
            expected_combo_id = "pc-" + hashlib.sha256("|".join(combination).encode("utf-8")).hexdigest()[:12]
            if case.get("policy_combination_group") != expected_combo_id:
                problems.append({"type": "policy_combination_group_id_mismatch", "case_id": cid, "expected": expected_combo_id, "actual": case.get("policy_combination_group")})
        per_split[split] += 1
        per_category[case["business_scenario"]] += 1
        category_by_split[(split, case["business_scenario"])] += 1
        review_counts[case["review_status"]] += 1
        if case["review_status"] not in {"PENDING_REVIEW", "APPROVED", "NEEDS_CORRECTION"}:
            warnings.append({"type": "unexpected_review_status", "case_id": cid, "status": case["review_status"]})
        if case.get("frozen") is not False or case.get("test_candidate_only") != (split == "test_candidate"):
            problems.append({"type": "split_or_freeze_flag_mismatch", "case_id": cid})
        if case.get("scoreability") == "SCORABLE_CANDIDATE" and case.get("scorable") is True and split in ("dev", "validation"):
            eligible_by_split[split] += 1
        if "compound_reasoning" in case.get("tags", []) and case.get("scorable") is True:
            compound.append(cid)
        if not case.get("required_conditions"):
            problems.append({"type": "empty_required_conditions", "case_id": cid})
        condition_ids = [item.get("condition_id") for item in case.get("required_conditions", [])]
        condition_text_values = [item.get("text") for item in case.get("required_conditions", [])]
        if any(not item.get("condition_id") or not item.get("text") or not isinstance(item.get("required"), bool) for item in case.get("required_conditions", [])):
            problems.append({"type": "invalid_required_condition_shape", "case_id": cid})
        if len(condition_ids) != len(set(condition_ids)) or len(condition_text_values) != len(set(condition_text_values)):
            problems.append({"type": "duplicate_condition_id_or_text", "case_id": cid})
        if case.get("expected_strategy") not in ("SINGLE", "MULTI"):
            problems.append({"type": "invalid_expected_strategy", "case_id": cid, "value": case.get("expected_strategy")})
        if case.get("scoreability", "").startswith("UNSCORABLE") and not case.get("unscorable_reason"):
            problems.append({"type": "missing_unscorable_reason", "case_id": cid})
        if case.get("scorable") != (case.get("scoreability") == "SCORABLE_CANDIDATE"):
            problems.append({"type": "scoreability_flag_mismatch", "case_id": cid})
        if case.get("source") != "CODEX_AUTHORED_UNREVIEWED_CANDIDATE":
            warnings.append({"type": "source_requires_human_confirmation", "case_id": cid, "source": case.get("source")})
        evaluation_at = case["evaluation_at"]
        for evidence_id in case.get("acceptable_evidence_ids", []):
            doc = evidence.get(evidence_id)
            if doc is None:
                problems.append({"type": "unknown_evidence_id", "case_id": cid, "evidence_id": evidence_id})
                continue
            if not (doc["effective_from"] <= evaluation_at and (doc["effective_to"] is None or evaluation_at < doc["effective_to"])):
                problems.append({"type": "evidence_inactive_at_evaluation_time", "case_id": cid, "evidence_id": evidence_id, "evaluation_at": evaluation_at})
        for evidence_id in case.get("possible_evidence_ids", []):
            doc = evidence.get(evidence_id)
            if doc is None:
                problems.append({"type": "unknown_candidate_evidence_id", "case_id": cid, "evidence_id": evidence_id})
            elif case.get("scoreability") not in ("UNSCORABLE", "UNSCORABLE_CANDIDATE") and not (doc["effective_from"] <= evaluation_at and (doc["effective_to"] is None or evaluation_at < doc["effective_to"])):
                problems.append({"type": "candidate_evidence_inactive_at_evaluation_time", "case_id": cid, "evidence_id": evidence_id})
        for evidence_id in case.get("hard_negative_evidence_ids", []):
            doc = evidence.get(evidence_id)
            if doc is None:
                problems.append({"type": "unknown_hard_negative_evidence_id", "case_id": cid, "evidence_id": evidence_id})
            elif not (doc["effective_from"] <= evaluation_at and (doc["effective_to"] is None or evaluation_at < doc["effective_to"])):
                problems.append({"type": "hard_negative_inactive_at_evaluation_time", "case_id": cid, "evidence_id": evidence_id})
        if set(case.get("hard_negative_evidence_ids", [])) & set(case.get("acceptable_evidence_ids", [])):
            problems.append({"type": "hard_negative_is_gold", "case_id": cid})
        condition_texts = {item.get("text") for item in case.get("required_conditions", [])}
        judgment_texts = {item.get("condition") for item in case.get("evidence_judgments", [])}
        if condition_texts != judgment_texts:
            problems.append({"type": "condition_judgment_coverage_mismatch", "case_id": cid, "missing_judgments": sorted(str(x) for x in condition_texts - judgment_texts), "unknown_judgments": sorted(str(x) for x in judgment_texts - condition_texts)})
        for judgment in case.get("evidence_judgments", []):
            if judgment.get("relation") not in ("supports", "refutes", "unknown"):
                problems.append({"type": "invalid_evidence_relation", "case_id": cid})
            for eid in judgment.get("evidence_ids", []):
                if eid not in evidence:
                    problems.append({"type": "judgment_unknown_evidence_id", "case_id": cid, "evidence_id": eid})
                elif eid not in set(case.get("acceptable_evidence_ids", [])) | set(case.get("possible_evidence_ids", [])):
                    problems.append({"type": "judgment_evidence_not_listed_as_candidate", "case_id": cid, "evidence_id": eid})
        normalized_rows.append((cid, split, group, qnorm))
    for group, splits in group_splits.items():
        if len(splits) != 1:
            problems.append({"type": "template_group_split_leakage", "template_group": group, "splits": sorted(str(s) for s in splits)})
    for combination, splits in policy_combination_splits.items():
        if len(splits) != 1:
            problems.append({"type": "policy_combination_split_leakage", "policy_combination_group": "|".join(combination), "splits": sorted(str(s) for s in splits), "case_ids": policy_combination_cases[combination]})
    cross_split_near = []
    for i, (left_id, left_split, left_group, left_q) in enumerate(normalized_rows):
        for right_id, right_split, right_group, right_q in normalized_rows[i + 1:]:
            if left_split != right_split and left_group != right_group:
                ratio = difflib.SequenceMatcher(None, left_q, right_q, autojunk=False).ratio()
                if ratio >= 0.86:
                    cross_split_near.append({"left": left_id, "left_split": left_split, "right": right_id, "right_split": right_split, "similarity": round(ratio, 3)})
    for pair in cross_split_near:
        problems.append({"type": "cross_split_near_duplicate_review", **pair})
    if len(cases) < 80 or len(cases) > 120:
        problems.append({"type": "candidate_count_out_of_range", "count": len(cases)})
    if len(compound) < 25:
        problems.append({"type": "too_few_compound_candidates", "count": len(compound), "minimum": 25})
    return {
        "dataset_version": payload.get("dataset_version"), "case_count": len(cases),
        "template_group_count": len(group_splits), "split_counts": dict(per_split), "category_counts": dict(per_category),
        "category_counts_by_split": {f"{split}:{category}": count for (split, category), count in category_by_split.items()},
        "policy_combination_group_count": len(policy_combination_splits),
        "review_status_counts": dict(review_counts), "compound_candidate_count": len(compound), "compound_case_ids": compound,
        "scorable_pending_dev_validation_counts": dict(eligible_by_split), "approved_scoreable_count": sum(c.get("review_status") == "APPROVED" and c.get("scorable") is True for c in cases),
        "cross_split_near_duplicate_pairs": cross_split_near, "problems": problems, "warnings": warnings,
        "status": "PASS_AUTOMATED_STRUCTURAL_CHECKS" if not problems else "FAIL",
        "human_review_required": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    args = parser.parse_args()
    dataset_path = args.dataset.resolve()
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    policies = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    report = audit(payload, policies)
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    report["source_policy_sha256"] = hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()
    report["dataset_sha256"] = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    report["dataset_path"] = str(dataset_path.relative_to(ROOT))
    version = payload.get("dataset_version", "")
    if version.endswith("-r3"):
        report["review_csv"] = "eval/review/policy-retrieval-v2-review-r7.csv"
    elif version.endswith("-r7-humanreview-") or "-r7-humanreview-" in version:
        report["review_csv"] = "eval/review/policy-retrieval-v2-review-r11-approved.csv"
    elif version.endswith("-r7"):
        report["review_csv"] = "eval/review/policy-retrieval-v2-review-r11.csv"
    else:
        report["review_csv"] = "eval/review/policy-retrieval-v2-review.csv"
    if set(report["review_status_counts"]) == {"PENDING_REVIEW"}:
        report["notice"] = "Automated validity checks do not approve annotations; all candidate rows remain PENDING_REVIEW."
    else:
        report["notice"] = "Automated validity checks do not grant approval; review_status counts are reported as recorded in the input dataset."
    report["supersedes"] = [
        {"report": "docs/probes/policy-dataset-quality-20261018T152755Z.json", "reason": "Earlier draft counted all multi-condition schema fields as compound without requiring the explicit compound_reasoning label; candidate counts below use the labelled set only."},
        {"report": "docs/probes/policy-dataset-quality-20261018T153213Z.json", "reason": "Dataset was subsequently expanded with an unscorable policy-version clarification group and explicit hard-negative/condition-evidence mappings; use the latest report with its matching dataset SHA-256."},
        {"report": "docs/probes/policy-dataset-quality-20261018T153314Z.json", "reason": "The validator incorrectly required context-dependent possible evidence to be active at query time even when the sample is explicitly unscorable pending payment-date clarification; the corrected validator checks existence but only enforces active dates for gold and hard-negative evidence."},
        {"report": "docs/probes/policy-dataset-quality-20261018T153333Z.json", "reason": "The latest authoring pass removed the compound_reasoning tag from unscorable specific-warranty clarification cases so the compound count includes only scoreable candidates."},
        {"report": "docs/probes/policy-dataset-quality-20261018T154208Z.json", "reason": "Split assignments were revised so identical acceptable-evidence policy combinations are held within one split; this revision also reports explicit policy-combination leakage checks."},
        {"report": "docs/probes/policy-dataset-quality-20261018T154318Z.json", "reason": "A later authoring revision added condition-level evidence judgment relations and matching review metadata; use the latest report whose dataset hash matches the current candidate file."},
    ]
    out_dir = ROOT / "docs/probes"
    out = out_dir / f"policy-dataset-quality-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with out.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({**report, "report_path": str(out)}, ensure_ascii=False, indent=2))
    print(json.dumps({"status": report["status"], "report_path": str(out), "dataset_sha256": report["dataset_sha256"], "problems": report["problems"], "warnings": len(report["warnings"])}, ensure_ascii=False))
    return 0 if report["status"] == "PASS_AUTOMATED_STRUCTURAL_CHECKS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
