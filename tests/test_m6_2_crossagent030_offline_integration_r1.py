"""CROSSAGENT-030 isolated integration through the real D1 app graph.

This uses the authorized r3.2.4 Dev bundle, a temporary marked SQLite target,
real D1 repositories, real LangGraph orchestration and the real specialist
implementations. Routing uses the actual deterministic policy; Provider access
is a tripwire and cannot inspect expected answers or fixture payloads.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from contextlib import asynccontextmanager
import pytest

from sqlalchemy import select, text

from app.m1.models import Run, RunEvent
from app.orchestration.adapters import GraphServices, default_route
from app.orchestration.runtime import execute_claimed_chat_langgraph
from eval.m6_1.fixture_read_models import product_id
from eval.m6_1.personal_dev import D1Coordinator, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import execute_claimed_lifecycle, validate_case_route_scope
from app.m3.policy_tools import _is_warranty_evidence
from eval.m6_1.personal_dev_diagnostic_checks import evaluate_diagnostic_record, load_locked_rules


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
CASE_ID = "D1-CROSSAGENT-030"
CAPABILITIES = ["get_order", "get_order_items", "get_warranty"]


@pytest.mark.parametrize(("evidence", "expected"), [
    ({"chunk_id": "source-1", "title": "Warranty policy", "content": "18 months from purchase"}, True),
    ({"chunk_id": "syn-policy-warranty", "title": "syn-policy-warranty",
      "content": "18 months from recorded purchase; activation facts required"}, True),
    ({"chunk_id": "generic-policy", "title": "General policy",
      "content": "18 months from recorded purchase"}, False),
])
def test_warranty_classification_requires_body_or_source_metadata(evidence, expected):
    assert _is_warranty_evidence(evidence) is expected


class NoNetworkProvider:
    """Truthful tripwire: this deterministic specialist path must not call it."""
    def __bool__(self):
        return True

    def __getattr__(self, _name):
        raise AssertionError("unexpected model/provider call in offline test")


@asynccontextmanager
async def memory_checkpoint_factory(_url, _schema):
    from langgraph.checkpoint.memory import MemorySaver
    from app.orchestration.checkpointer import strict_serializer
    yield MemorySaver(serde=strict_serializer())


def test_crossagent030_real_app_graph_reads_owned_item_then_warranty():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    plan = plan_case(dataset, CASE_ID)
    db_root = ROOT / "docs/probes" / f"crossagent030-isolated-r1-{uuid.uuid4().hex}"
    db_root.mkdir(parents=True, exist_ok=False)
    target = PersonalDevDatabase(db_root)
    engine, factory = target.open(initialize=True)
    coordinator = D1Coordinator(target, factory)
    receipt = coordinator.prepare(plan)
    reads = coordinator.repositories(plan, receipt, maximum=3)
    def route(query):
        decision = default_route(query, available_capabilities=CAPABILITIES)
        return validate_case_route_scope(plan, decision)

    services = GraphServices(
        router=route,
        direct=lambda _state: {},
        single=lambda _state: {},
        multi=lambda _state: {},
        refuse=lambda _state: {},
    )
    selected = SimpleNamespace(
        database_url=f"sqlite:///{target.path.as_posix()}",
        m3_supervisor_enabled=True,
        checkpoint_schema="d1_offline_test",
        graph_version="crossagent030-offline-r1",
        execution_version="crossagent030-offline-r1",
    )
    try:
        result = execute_claimed_lifecycle(
            coordinator,
            plan,
            receipt,
            application_call=execute_claimed_chat_langgraph,
            provider=NoNetworkProvider(),
            selected=selected,
            services=services,
            reads=reads,
            checkpoint_factory=memory_checkpoint_factory,
        )

        assert result["status"] == "SUCCEEDED"
        assert result["checkpoint_mode"] == "MEMORY_NO_CRASH_RESUME"
        archive = result["result"]
        assert archive["strategy"] == "AGENT"
        assert archive["route"]["route_version"] == "m2.1-route-v9"
        assert archive["route"]["reason_code"] == "WARRANTY_FROM_OWNED_ORDER"
        assert archive["route"]["decision_source"] == "RULE"
        assert archive["answer_kind"] == "POLICY_ANSWER"
        assert archive["usage"] is None
        assert "eligibility_status" in json.dumps(archive["business_results"])
        assert "UNKNOWN" in json.dumps(archive["business_results"])

        expected_pid = product_id("syn-product-warranty-114")
        assert expected_pid in json.dumps(archive["business_results"])
        assert "SYN-WAR-01" in json.dumps(archive)
        assert "syn-policy-warranty" in json.dumps(archive)
        assert "activation" in archive["answer"].lower() or "购买" in archive["answer"]
        assert len(archive["citations"]) == 1
        citation = archive["citations"][0]
        assert citation["chunk_id"] == "syn-policy-warranty"
        assert citation["version"] == "SYN-WAR-01"
        assert citation["effective_from"] == "2026-01-01T00:00:00Z"
        assert citation["effective_to"] == "2027-01-01T00:00:00Z"
        assert citation["knowledge_version"] == plan["knowledge_version"]
        assert citation["index_fingerprint"]
        assert archive["policy_retrieval"]["evaluation_at"] == plan["evaluation_at"]
        assert archive["policy_retrieval"]["index_fingerprint"] == citation["index_fingerprint"]

        events = sorted(result["observations"][RunEvent.__tablename__], key=lambda row: row["seq"])
        event_types = [row["event_type"] for row in events]
        started_agents = [json.loads(row["payload_json"]).get("agent") for row in events
            if row["event_type"] == "agent_started"]
        assert set(started_agents) == {"order_logistics", "policy"}
        assert len(started_agents) == 2
        assert event_types.count("tool_started") == 3
        assert event_types.count("tool_finished") == 3

        payload = archive
        handoffs = payload["handoffs"]
        assert [row["target_agent"] for row in handoffs] == ["order_logistics", "policy"]
        assert [row["source_agent"] for row in handoffs] == ["supervisor", "order_logistics"]
        assert handoffs[0]["status"] == handoffs[1]["status"] == "COMPLETED"
        assert set(row["capability"] for row in payload["tool_execution_status"]) == set(CAPABILITIES)
        assert all(row["executed"] for row in payload["tool_execution_status"])
        audits = payload["tool_call_audit"]
        assert [row["capability"] for row in audits] == ["get_order", "get_order_items", "get_warranty"]
        assert all(row["executed"] is True and row["run_id"] == result["run_id"] for row in audits)
        item_audit = next(row for row in audits if row["capability"] == "get_order_items")
        assert item_audit["result_ref"]
        policy_audit = next(row for row in audits if row["capability"] == "get_warranty")
        assert policy_audit["result_ref"]
        assert reads["store"].allowance.calls == 3

        rules, _rules_sha = load_locked_rules(
            ROOT / "docs/eval/m6.2-crossagent030-r1-20261029/locked-check-rules.json",
            case_id=CASE_ID)
        budget_snapshot = {
            "budget_snapshot_schema_version": "m6.1-budget-snapshot-v2", "complete": True,
            "settled": True, "snapshot_kind": "FINAL", "active_reservations": 0,
            "integrity_errors": [], "calls_by_category": {"provider": 0, "tool": 3, "embedding": 0, "judge": 0},
            "tokens_conservative_budget_consumption": 0,
            "estimated_usd_conservative_budget_consumption": 0.0,
            "provider_requests": 0, "embedding_requests": 0, "judge_requests": 0,
        }
        persisted_events = result["observations"][RunEvent.__tablename__]
        archived = {"result": {"run_id": result["run_id"], "status": result["status"],
            "result": archive, "observations": result["observations"]}}
        check_preview = evaluate_diagnostic_record(case_id=CASE_ID, archived=archived, plan=plan,
            locked_rules=rules, budget_snapshot=budget_snapshot,
            cleanup_evidence={"cleanup_status": "CLEANED_AND_RELEASED", "lease_released": True,
                "pending_actions": 0, "residual_case_rows": 0, "database_identity": target.identity})
        assert check_preview["decision"]["outcome"] == "CONTINUE", json.dumps(check_preview, ensure_ascii=False)
        assert check_preview["factual_checks"]["authenticated_owner_matches_order_fixture"] == "PASS"
        assert check_preview["factual_checks"]["order_item_maps_unique_product_id"] == "PASS"
        assert check_preview["factual_checks"]["warranty_eligibility_remains_unknown_without_purchase_activation"] == "PASS"
        assert check_preview["safety_checks"]["actual_two_specialist_handoff"] == "PASS"
        assert check_preview["evidence_checks"]["warranty_policy_source_and_interval_linked"] == "PASS"
        assert check_preview["semantic_review_required"][0]["status"] == "UNKNOWN"

        with coordinator.factory() as db:
            persisted_run = db.get(Run, result["run_id"])
            stored = json.loads(persisted_run.result_json)
            assert persisted_run.status == "SUCCEEDED"
            assert stored["answer"] == archive["answer"]
            assert db.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        coordinator.cleanup(receipt)
        assert len(persisted_events) > 0
        with coordinator.factory() as db:
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            assert marker.lease_id is None and marker.receipt is None
            assert db.execute(select(Run)).scalars().all() == []
            assert db.execute(text("SELECT count(*) FROM d1_fixture_facts")).scalar_one() == 0
    finally:
        try:
            with coordinator.factory() as db:
                marker = db.execute(text("SELECT lease_id FROM d1_personal_target")).one()
                run = db.get(Run, receipt.run_id) if receipt.run_id else None
                safe = marker.lease_id == receipt.lease_id and (
                    run is None or run.status in {"SUCCEEDED", "FAILED", "CANCELLED", "MODEL_UNAVAILABLE"})
            if safe:
                coordinator.cleanup(receipt)
        finally:
            engine.dispose()
