"""Approved Dev contract preparation: isolated repositories/graph, no persisted Run."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from test_m6_1_d1_personal_dev_offline import coordinator, tmp_path, assert_empty
from eval.m6_2.confirmed_dev_preparation import DATA, NEW_DATA, OUT, READY_CASES, revised_dev_bundle
from eval.m6_1.personal_dev import plan_case
from eval.m6_1.personal_dev_batch_m62 import app_admission

ROOT = Path(__file__).resolve().parents[1]


def test_approved_revision_preserves_fixtures_and_unmodified_contracts():
    old = json.loads((ROOT / DATA).read_text(encoding="utf8"))
    new = revised_dev_bundle(old)
    assert new == json.loads((ROOT / NEW_DATA).read_text(encoding="utf8"))
    assert new["fixtures"] == old["fixtures"]
    changed = []
    for before, after in zip(old["cases"], new["cases"], strict=True):
        before = {k: v for k, v in before.items() if k != "data_version"}
        after = {k: v for k, v in after.items() if k != "data_version"}
        if before != after:
            changed.append(after["case_id"])
        assert after["input"]["reviewer_id"] == "UNKNOWN"
    assert changed == ["D1-POLICY-015", "D1-POLICY-016", "D1-CROSSAGENT-029"]


def test_confirmed_outcomes_keep_original_bindings_and_no_execution_grant():
    confirmed = json.loads((ROOT / OUT / "confirmed-decisions-r1.json").read_text(encoding="utf8"))
    assert len({r["run_id"] for r in confirmed["items"]}) == 32
    assert confirmed["descriptive_counts_not_success_rate"] == {"PASS": 14, "PARTIAL": 9, "FAIL": 9}
    assert all(r["scope"] == "TASK_OUTCOME_ONLY" and not r["independent_review"] for r in confirmed["items"])
    prep = json.loads((ROOT / OUT / "execution-preparation-r1.json").read_text(encoding="utf8"))
    assert not prep["execution_authorized"] and prep["authorization_token"] is None
    assert prep["deferred_case_ids"] == ["D1-CROSSAGENT-033"]


def test_seven_revised_cases_repository_and_supervisor_without_run(coordinator, tmp_path):
    from app.m3.agents import default_agent_registry
    from app.m3.contracts import TrustedAgentContext
    from app.m3.order_tools import requested_order_id
    from app.m3.graph import build_supervisor_graph, invoke_supervisor
    from app.orchestration.observability import InMemoryObservabilitySink
    from app.orchestration.state import GraphInput, GraphRuntimeContext, graph_input_state
    data = json.loads((ROOT / NEW_DATA).read_text(encoding="utf8"))
    observations = []
    for case_id in READY_CASES:
        plan = plan_case(data, case_id)
        preflight = app_admission(coordinator, plan)
        assert preflight["status"] in {"APPLICATION_REPOSITORY_PREFLIGHT_PASS", "NO_BUSINESS_READS_REQUIRED"}
        # Graph-only integration exercises actual agents and repositories with a declared
        # route; it is not evidence of a model-selected route or executable CLI authorization.
        receipt = coordinator.prepare(plan)
        try:
            repos = coordinator.repositories(plan, receipt)
            registry = default_agent_registry(order_repository=repos["order"],
                product_repository=repos["product"], policy_service=repos["policy"],
                after_sales_repository=repos["after_sales"], coupon_repository=repos["coupon"])
            allowed = plan["case"]["expected_outcome"]["seven_dimension_trace"]["tool_agent_outcome"]["allowed_tool_ids"]
            trusted = TrustedAgentContext(user_id=plan["owner_id"], session_id="offline-preparation",
                run_id="offline-graph-only", trace_id="offline-graph-trace", authenticated=True,
                run_owner_verified=True, session_owner_verified=True,
                requested_order_id=requested_order_id(plan["message"])[0])
            state = graph_input_state(GraphInput(run_id="offline-graph-only", session_id="offline-preparation",
                trace_id="offline-graph-trace", query=plan["message"]), graph_version="offline-contract-preparation")
            state.update({"strategy": "AGENT", "terminal_status": "RUNNING",
                "route": {"strategy": "AGENT", "reason_code": "DECLARED_OFFLINE_ROUTE",
                          "required_capabilities": allowed}})
            store, calls = {}, []
            output = invoke_supervisor(build_supervisor_graph(registry, sink=InMemoryObservabilitySink()), state,
                GraphRuntimeContext(query=plan["message"], trusted_agent_context=trusted,
                    m3_supervisor_enabled=True, tool_result_store=store, tool_execution_records=calls,
                    retrieval_service=SimpleNamespace()))
            executed = [c for c in calls if c["executed"]]
            if case_id == "D1-POLICY-015":
                assert not executed
                assert output["handoffs"][0]["result"]["missing_information"] == ["COUPON_CODE_REQUIRED"]
            else:
                expected = {c["tool_id"] for c in plan["case"]["input"]["expected_tool_calls"]}
                actual = {c["capability"] for c in executed}
                assert actual == expected, json.dumps({"case": case_id, "calls": calls, "handoffs": output.get("handoffs"), "error": output.get("error_code")}, ensure_ascii=False)
            observations.append({"case_id": case_id, "repository_preflight": preflight["status"],
                "repository_read_count": preflight["read_count"], "graph_executed_calls": len(executed),
                "handoff_count": len(output["handoffs"]), "graph_declared_route_only": True,
                "tool_execution_records": calls, "handoffs": output["handoffs"],
                "persisted_run_created": False, "provider_requests": 0})
        finally:
            coordinator.cleanup(receipt)
        assert_empty(coordinator)
        with coordinator.factory() as db:
            assert db.execute(text("SELECT COUNT(*) FROM runs")).scalar_one() == 0
        observations[-1]["receipt_cleanup"] = "EXACT_CLEANUP_ZERO_RUNS"
    (tmp_path / "offline-admission.json").write_text(json.dumps({
        "classification": "ISOLATED_REPOSITORY_AND_DECLARED_ROUTE_GRAPH_NOT_LIVE_AGENT_EVAL",
        "dataset": NEW_DATA.as_posix(), "cases": observations,
        "real_runs": 0, "provider_requests": 0, "model_route_tested": False,
        "cli_authorization_admission_tested": False}, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
