"""Offline scope audit of the approved G2-S MULTI run; never calls a model."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.m2.self_rag import _atomic_policy_conditions, _unrequested_qualifier_codes
from app.m1.db import make_engine, make_session_factory
from app.m1.models import Run
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/probes/langgraph-g2s-real-20261020T102805643541Z-a1129929.json"
POLICIES = ROOT / "knowledge/policies.json"
CASE_ID = "pr2-multi_clause_return_refund-03"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> Path:
    source_bytes = SOURCE.read_bytes()
    record = json.loads(source_bytes)
    case = next(item for item in record["cases"] if item.get("case_id") == CASE_ID)
    if case.get("dataset_split") != "dev" or case.get("review_status") != "APPROVED":
        raise ValueError("historical G2-S source is not the expected approved dev run")
    grade = case["grade_result"]
    old_mapped_ids = sorted({rule_id for aspect in grade.get("aspect_rule_map", [])
                             for rule_id in aspect.get("rule_ids", [])})
    try:
        database_url = resolve_test_database_url()
        engine = make_engine(database_url.render_as_string(hide_password=False))
        try:
            with make_session_factory(engine)() as db:
                run_row = db.get(Run, case["run_id"])
                if run_row is None or not run_row.result_json:
                    raise ValueError("persisted source run is unavailable")
                persisted_bytes = run_row.result_json.encode("utf-8")
                persisted = json.loads(run_row.result_json)
        finally:
            engine.dispose()
        self_rag = persisted.get("self_rag") or {}
    except Exception:
        raise RuntimeError("read-only retrieval of the approved G2-S run from shoprelay_m11_test failed") from None
    source_evidence = {row["chunk_id"]: row for row in self_rag.get("evidence", [])
                       if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)}
    rules = _atomic_policy_conditions(source_evidence)
    by_id = {item.condition_id: item for item in rules}
    if set(old_mapped_ids) - set(by_id):
        raise ValueError("historical Grade mapped IDs cannot be resolved to retrieved policy source atoms")
    question = case["user_query"]
    quality = next(item for item in rules if item.source_evidence_id == "policies/return-standard@2026-07#quality"
                   and "30天内的质量问题" in item.text and "人工审核" in item.text)
    refund = next(item for item in rules if item.source_evidence_id == "policies/refund@2026-01#before-shipping"
                  and "已签收订单需要已批准的退货申请" in item.text)
    gold_required_count = 2
    mapping_rows = []
    for rule_id in old_mapped_ids:
        rule = by_id[rule_id]
        reasons = _unrequested_qualifier_codes(rule.text, question,
            source_evidence_id=rule.source_evidence_id)
        mapping_rows.append({"condition_id": rule_id, "source_evidence_id": rule.source_evidence_id,
            "source_excerpt": rule.source_excerpt, "old_grade_classification": "REQUIRED_BY_BROAD_RULE_IDS",
            "q3_scope_classification": "CORE_REQUIRED" if rule_id in {quality.condition_id, refund.condition_id}
                else "SUPPORTING_NOT_REQUIRED",
            "scope_reason_codes": reasons or ([] if rule_id in {quality.condition_id, refund.condition_id}
                                               else ["NOT_NECESSARY_FOR_REQUESTED_ASPECT"])})
    answer = case.get("answer") or ""
    adjacent = {
        "UNREQUESTED_REFUND_CONCURRENCY": bool(re.search(
            r"同一订单.{0,16}(?:一个|一笔).{0,10}(?:开放|未完成).{0,10}退款申请|仅允许一个开放", answer)),
        "UNREQUESTED_PARTIAL_REFUND": bool(re.search(r"部分退款|部分退", answer)),
        "UNREQUESTED_SHIPMENT_BRANCH": bool(re.search(r"已发货.{0,12}(?:尚未|未)签收|已发货未签收", answer)),
        "UNREQUESTED_REFUND_SETTLEMENT": bool(re.search(
            r"申请不代表.{0,20}(?:款项|退款).{0,8}(?:实际|真实)?到账", answer)),
    }
    adjacent_present = [code for code, present in adjacent.items() if present]
    report = {
        "schema_version": "shoprelay.m2.4-q3-historical-scope-audit.v1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS" if len(old_mapped_ids) == 8 and len(mapping_rows) == 8
            and sum(row["q3_scope_classification"] == "CORE_REQUIRED" for row in mapping_rows) == 2 else "FAIL",
        "mode": "OFFLINE_HISTORICAL_RECONCILIATION_NO_MODEL_CALLS",
        "case_id": CASE_ID,
        "dataset_split": case["dataset_split"],
        "dataset_sha256": record["dataset"]["sha256"],
        "source_record": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
        "source_record_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "persisted_run_id": case["run_id"],
        "persisted_run_result_sha256": hashlib.sha256(persisted_bytes).hexdigest(),
        "persisted_source": "read-only Run.result_json from dedicated shoprelay_m11_test",
        "policy_source": str(POLICIES.relative_to(ROOT)).replace("\\", "/"),
        "policy_source_sha256": sha256(POLICIES),
        "question": question,
        "original_grade_required_mapping_count": len(old_mapped_ids),
        "approved_gold_required_condition_count": gold_required_count,
        "core_policy_rules": [quality.model_dump(), refund.model_dump()],
        "mapped_rule_reconciliation": mapping_rows,
        "root_cause": "The legacy aspect_rule_map field rule_ids represented topical relevance, but the executor promoted every mapped CORE_RULE to required=true. Broad aspects therefore made adjacent, supported clauses raise the sufficiency gate.",
        "answer_review": {
            "original_answer": answer,
            "adjacent_supported_but_unrequested_claims": adjacent_present,
            "missing_or_weak_core_items": [
                "The answer did not clearly state that the problem-to-delivery elapsed time was absent, so the specific order cannot be confirmed within the 30-day window.",
                "The human-review outcome was not stated clearly enough to prevent an implication of guaranteed approval."
            ],
            "does_not_edit_gold_or_raw_run": True,
        },
        "test_candidate_read_or_used": False,
        "real_model_requests": 0,
    }
    base = ROOT / "docs/probes/m2.4-q3-scope-audit-20261020.json"
    output = base
    suffix = 1
    while output.exists():
        output = base.with_name(f"{base.stem}-r{suffix}{base.suffix}")
        suffix += 1
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "path": str(output.relative_to(ROOT)),
        "legacy_mapped_rules": len(old_mapped_ids), "core_rules": sum(
            row["q3_scope_classification"] == "CORE_REQUIRED" for row in mapping_rows),
        "unrequested_claims": len(adjacent_present)}, ensure_ascii=False))
    return output


if __name__ == "__main__":
    run()
