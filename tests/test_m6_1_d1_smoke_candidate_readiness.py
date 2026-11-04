"""Isolated fixture/repository checks for the proposed D1 Smoke candidates.

This uses the real D1 plan, personal fixture receipt, and application read
repositories over a fresh file-SQLite database. It never creates a Run or
imports the Provider/Agent execution path.
"""
from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from eval.m6_1.fixture_read_models import product_id
from eval.m6_1.personal_dev import D1Coordinator, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import validate_case_route_scope
from app.m3.product_tools import parse_product_request_many


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
SMOKE_CASES = (
    "D1-PRODUCT-001", "D1-PRODUCT-004", "D1-ORDER-007", "D1-ORDER-009",
    "D1-ORDER-011", "D1-POLICY-013", "D1-POLICY-014", "D1-CROSSAGENT-029",
    "D1-SAFETY-038",
)


@pytest.fixture(scope="module")
def bundle():
    value = json.loads(DATASET.read_text(encoding="utf-8"))
    assert value["version"] == "m6.1-d1-dev-draft-r3.2.4"
    return value


@pytest.mark.parametrize("case_id", SMOKE_CASES)
def test_smoke_candidate_uses_only_declared_real_fixture_repositories(bundle, tmp_path, case_id,
                                                                      record_property):
    plan = plan_case(bundle, case_id)
    declared = {row["tool_id"] for row in plan["case"]["input"].get("expected_tool_calls", [])
        if row.get("mode") == "read_only"}
    route_hint = plan["case"]["input"].get("expected_strategy")
    assert isinstance(route_hint, str)
    validate_case_route_scope(plan, {"strategy": route_hint,
        "required_capabilities": sorted(declared)})
    record_property("candidate_route_hint", route_hint)
    record_property("declared_read_only_capabilities", json.dumps(sorted(declared)))
    tool_cap = 4  # proposed per-case Smoke limit; PRODUCT-001 needs one search plus two details.
    if case_id == "D1-ORDER-009":
        assert route_hint == "REFUSE"
        assert declared == set()
        assert plan["facts"] == [] and plan["order_ids"] == []
        record_property("fixture_business_fact_count", 0)
        record_property("repository_reads", 0)
        record_property("offline_scope_check", "PASS_NO_TARGET_ORDER_QUERY")
        record_property("cleanup_status", "NOT_APPLICABLE_NO_FIXTURE_OR_RUN_CREATED")
        return

    # Keep the SQLite path short on Windows; tmp_path's parent is the fresh,
    # project-local test workspace allocated by the configured pytest plugin.
    db_root = tmp_path.parent / f"s-{uuid.uuid4().hex[:8]}"
    db_root.mkdir(parents=True)
    target = PersonalDevDatabase(db_root)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = None
    try:
        receipt = coordinator.prepare(plan)
        reads = coordinator.repositories(plan, receipt, maximum=tool_cap)
        observed: dict = {}
        if case_id == "D1-PRODUCT-001":
            _, filters, _ = parse_product_request_many(plan["message"])
            filters = reads["product"].resolve_product_filters(plan["message"], filters)
            search = reads["product"].search_products(filters)
            details = [reads["product"].get_product_detail(row["product_id"])
                for row in search["products"]]
            weights = [(item.get("product") or {}).get("recorded_attributes", {}).get("weight_g")
                for item in details]
            assert search["source"] == "shoprelay_product_catalog"
            assert search["truncated"] is False and len(details) == 2
            assert all(item.get("availability") == "AVAILABLE" for item in details)
            assert all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in weights)
            observed = {"search_source": search["source"], "candidate_count": len(details),
                "detail_sources": [item["product"]["fixture_provenance"] for item in details],
                "weights_g": weights}
        elif case_id == "D1-PRODUCT-004":
            product_fact = next(row for row in plan["facts"] if row["kind"] == "products")
            result = reads["product"].check_inventory(product_id(product_fact["source_id"]))
            assert result["source"] == "shoprelay_inventory"
            assert result["records"] and result["records"][0]["observation_time"] is None
            assert result["records"][0]["error_code"] == "INVENTORY_FRESHNESS_UNVERIFIED"
            observed = {"inventory_source": result["source"], "availability": result["availability"],
                "record_error": result["records"][0]["error_code"], "observation_time": None}
        elif case_id == "D1-ORDER-007":
            result = reads["order"].get_order(plan["order_ids"][0], plan["owner_id"])
            assert result["source"] == "business_database"
            assert result["order_id"] == plan["order_ids"][0]
            assert result["fixture_provenance"]
            observed = {"order_id": result["order_id"], "stored_status": result["status"],
                "unknown_fields": result["unknown_fields"], "source": result["source"]}
        elif case_id == "D1-ORDER-011":
            result = reads["order"].get_order_items(plan["order_ids"][0], plan["owner_id"])
            assert result["source"] == "business_database" and result["items"]
            assert all(row["fixture_provenance"] for row in result["items"])
            assert all(row["product_fixture_provenance"] for row in result["items"])
            observed = {"order_id": result["order_id"], "item_count": len(result["items"]),
                "source": result["source"]}
        elif case_id in {"D1-POLICY-013", "D1-POLICY-014", "D1-SAFETY-038"}:
            result = reads["policy"].search(plan["message"], mode="BM25",
                result_ref=f"offline-smoke-readiness:{case_id}")
            expected_refs = set(plan["case"]["input"].get("acceptable_evidence_ids", []))
            actual_refs = {row.get("chunk_id") for row in result["evidence"]}
            assert result["source"] == "shoprelay_policy_retrieval"
            assert result["degraded"] is False and not result["failures"]
            assert expected_refs.issubset(actual_refs)
            observed = {"retrieval_source": result["source"],
                "knowledge_version": result["knowledge_version"],
                "index_fingerprint": result["index_fingerprint"],
                "evidence_ids": sorted(actual_refs)}
        elif case_id == "D1-CROSSAGENT-029":
            product_fact = next(row for row in plan["facts"] if row["kind"] == "products")
            pid = product_id(product_fact["source_id"])
            detail = reads["product"].get_product_detail(pid)
            inventory = reads["product"].check_inventory(pid)
            policy = reads["policy"].search(plan["message"], mode="BM25",
                result_ref="offline-smoke-readiness:D1-CROSSAGENT-029")
            assert detail["availability"] == "AVAILABLE"
            assert inventory["records"] and inventory["records"][0]["observation_time"] is None
            assert policy["source"] == "shoprelay_policy_retrieval" and policy["evidence"]
            observed = {"product_source": detail["source"], "inventory_status": inventory["records"][0]["error_code"],
                "policy_source": policy["source"], "policy_evidence_count": len(policy["evidence"])}
        else:  # pragma: no cover - the fixed case list is an allowlist
            raise AssertionError(f"unimplemented smoke candidate {case_id}")

        actual_reads = reads["store"].allowance.calls
        assert actual_reads <= tool_cap
        assert actual_reads > 0
        assert observed
        record_property("fixture_repository_reads", actual_reads)
        record_property("observed_fixture_evidence", json.dumps(observed, ensure_ascii=False, sort_keys=True))
    finally:
        if receipt is not None:
            coordinator.cleanup(receipt)
            cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
            assert cleanup["cleanup_status"] == "CLEANED_AND_RELEASED"
            assert cleanup["residual_case_rows"] == 0 and cleanup["lease_released"] is True
            record_property("cleanup_status", cleanup["cleanup_status"])
            record_property("residual_case_rows", cleanup["residual_case_rows"])
        engine.dispose()
