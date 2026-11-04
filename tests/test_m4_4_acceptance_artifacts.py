from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.skills.registry import SKILL_REGISTRY
from app.tools.catalog import TOOL_CATALOG

ROOT = Path(__file__).resolve().parents[1]


def _json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


def test_m4_4_scenario_lock_matches_eight_frozen_engineering_scenarios():
    dataset_path = ROOT / "eval/m4.4/m4.4-scenarios-r1.json"
    lock = _json("eval/m4.4/m4.4-scenarios-r1.lock.json")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8-sig"))
    assert hashlib.sha256(dataset_path.read_bytes()).hexdigest() == lock["sha256"]
    assert lock["locked_before_execution"] is True
    assert dataset["status"] == "ENGINEERING_SCENARIOS_NOT_GOLD"
    assert len(dataset["scenarios"]) == 8
    assert len({row["scenario_id"] for row in dataset["scenarios"]}) == 8


def test_m4_4_tool_matrix_is_exactly_the_server_owned_catalog():
    matrix = _json("docs/m4.4/m4.4-tool-capability-matrix-r1.json")
    rows = {row["tool_id"]: row for row in matrix["tools"]}
    assert set(rows) == set(TOOL_CATALOG)
    for tool_id, contract in TOOL_CATALOG.items():
        row = rows[tool_id]
        assert row["risk_level"] == contract.risk_level
        assert row["input_schema"] == contract.input_schema
        assert row["output_schema"] == contract.output_schema
        assert row["confirmation_required"] == contract.confirmation_required
        assert row["mcp_allowed"] == contract.mcp_allowed


def test_m4_4_skill_matrix_matches_registered_skills_and_budgets():
    matrix = _json("docs/m4.4/m4.4-skill-capability-matrix-r1.json")
    rows = {row["skill_id"]: row for row in matrix["skills"]}
    assert set(rows) == set(SKILL_REGISTRY)
    for skill_id, skill in SKILL_REGISTRY.items():
        row = rows[skill_id]
        assert row["version"] == skill.version
        assert row["allowed_agents"] == list(skill.allowed_agents)
        assert row["allowed_tools"] == list(skill.allowed_tools)
        assert row["proposal_only_tools"] == list(skill.proposal_tools)
        assert row["max_agent_calls"] == 4
        assert row["max_steps"] == 14


def test_m4_4_postgres_composite_probe_records_bounded_real_tool_execution():
    record = _json("docs/probes/m4.4-multi-agent-postgres-r2.json")
    assert record["status"] == "PASS"
    assert record["database"] == "shoprelay_m11_test"
    assert record["alembic_revision"] == "20261023_15"
    assert record["deepseek_requests"] == 0
    assert record["business_write_executions"] == 0
    assert len(record["run_results"]) == 5
    four_agent = record["run_results"]["m36-08-four-agent-composite"]
    assert four_agent["agents"] == ["order_logistics", "product", "policy", "after_sales"]
    assert four_agent["agent_calls"] == 4
    assert four_agent["controlled_steps"] <= 14
    assert four_agent["run_status"] == four_agent["graph_status"] == "SUCCEEDED"
    assert four_agent["sse_event_types"][-2:] == ["answer", "done"]
    assert record["exact_run_and_fixture_cleanup"] is True
    assert record["fixtures_removed"] is True


def test_m4_4_live_model_request_is_marked_blocked_without_sending_request():
    record = _json("docs/probes/m4.4-real-deepseek-review-blocked-r1.json")
    assert record["status"] == "BLOCKED_AUTO_REVIEW"
    assert record["request_attempted"] is False
    assert record["deepseek_http_requests"] == 0
    assert record["sdk_retries"] == 0
