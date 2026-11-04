from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eval.m6_1.contracts import ExposureStatus, IndependenceStatus, ReviewStatus, Scoreability, Split
from eval.m6_1.execution_modes import ExecutionMode, ExecutionModeGate, ModeAuthorizationError
from eval.m6_1.grants import SyntheticGrantAuthority
from eval.m6_1.registry import DatasetAsset, DatasetRegistry
from eval.m6_1.shoprelay_adapter import (
    AdapterContractError,
    AdapterPending,
    AdapterUnknownResult,
    SyntheticMockShopRelayTransport,
    SyntheticShopRelayAdapter,
    UnsupportedCapability,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "eval/m6_1/fixtures/m6.1-b-synthetic-cases-r1.json"


def _synthetic_case_and_gate():
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    asset = DatasetAsset(
        asset_id="m61b-synthetic-cases-r1", asset_type="synthetic_fixture", version="synthetic-r1",
        parent_asset_id=None, split=Split.SYNTHETIC.value, case_count=12,
        source_path=FIXTURE.relative_to(ROOT).as_posix(), sha256=digest,
        review_status=ReviewStatus.NOT_REVIEWED.value, reviewer_id=None, reviewed_at_utc=None,
        review_method=None, independence_status=IndependenceStatus.UNKNOWN.value,
        exposure_status=ExposureStatus.UNKNOWN.value, scoreability=Scoreability.NOT_VERIFIED.value,
        locked_at_utc=None, access_policy="SYNTHETIC_ONLY", limitations=(),
    )
    registry = DatasetRegistry([asset])
    authority = SyntheticGrantAuthority(registry, repository_root=ROOT, issuer="synthetic-issuer",
        trusted_issuers={"synthetic-issuer"}, signing_key=b"s" * 32)
    grant = authority.issue(asset.asset_id, subject="c-prep-test")
    cases = authority.read_cases(grant, subject="c-prep-test")
    return cases[1], grant, ExecutionModeGate(authority, subject="c-prep-test")


def mock_shoprelay_api(*, status="SUCCEEDED", model_id="fixture-model", usage=None,
                       run_ids=None, post_exception=None):
    run_ids = list(run_ids or ["mock-run-1"])
    run_responses, events, executions = {}, {}, {}
    for run_id in run_ids:
        run_responses[run_id] = {"trace_id": "t-run", "run": {
            "run_id": run_id, "session_id": "mock-session-1", "status": status,
            "error": None if status == "SUCCEEDED" else {"code": status},
            "answer": {"answer": "synthetic protected answer that must not be projected", "estimated_cost_usd": 0.003},
            "model_id": model_id, "usage": usage, "latency_ms": 145,
        }}
        events[run_id] = [
            {"event_id": f"{run_id}-e1", "run_id": run_id, "seq": 1, "type": "route_decided",
             "payload": {"strategy": "SINGLE", "sub_strategy": None, "decision_source": "FIXTURE",
                         "latency_ms": 2, "safety_decision": "ALLOW",
                         "answer": "must-not-leak", "secret": "must-not-leak"}},
            {"event_id": f"{run_id}-e2", "run_id": run_id, "seq": 2, "type": "retrieval_hop",
             "payload": {"hop": 0, "aspect_id": "syn-policy", "strategy_selected": "BM25",
                         "strategy_executed": "BM25", "degraded": False, "knowledge_version": "synthetic-v1",
                         "evidence_count": 1, "evidence_ids": ["syn-chunk-a"], "latency_ms": 8,
                         "prompt": "must-not-leak", "nested": {"secret": "must-not-leak"}}},
        ]
        executions[run_id] = {"trace_id": "t-execution", "execution": {
            "run_id": run_id, "session_id": "mock-session-1", "status": status,
            "strategy": "SINGLE", "route_latency_ms": 2,
            "citations": [{"chunk_id": "syn-chunk-a", "knowledge_version": "synthetic-v1"}],
            "retrieved_evidence": [{"chunk_id": "syn-chunk-a"}],
            "handoffs": [{"handoff_id": "syn-handoff", "source_agent": "order", "target_agent": "policy",
                          "evidence_refs": ["syn-order-ref"], "status": "SUCCEEDED", "private_prompt": "secret"}],
            "tool_calls": [{"tool_id": "synthetic_read", "status": "SUCCEEDED", "duration_ms": 5,
                            "arguments": {"private": "must-not-project"}}],
            "spans": [{"trace_id": "t", "span_id": "s", "node_name": "policy", "duration_ms": 9,
                       "payload": "must-not-project"}],
        }}
    return SyntheticMockShopRelayTransport(
        session_response={"trace_id": "t-session", "session": {"session_id": "mock-session-1"}},
        stream_receipt={"headers": {"X-Run-Id": run_ids[0]}, "events": []},
        run_responses=run_responses, event_responses=events, execution_responses=executions,
        post_exception=post_exception,
    )


def test_synthetic_adapter_executes_mock_and_maps_same_run_evidence_without_answer_text():
    case, grant, gate = _synthetic_case_and_gate()
    api = mock_shoprelay_api(usage={"prompt_tokens": 31, "completion_tokens": 12,
                                    "cost_estimate_usd_peak_rate": 0.003})
    adapter = SyntheticShopRelayAdapter(api, gate)
    observation = adapter.execute_case(mode=ExecutionMode.SYNTHETIC, grant=grant, case=case,
        experiment_id="exp.synthetic.adapter", client_request_id="request-synthetic-1")
    run = observation.runs[0]
    assert observation.run_ids == ("mock-run-1",)
    assert observation.lifecycle_status == "SUCCEEDED"
    assert run.router_decisions[0]["strategy"] == "SINGLE"
    assert run.retrieval_hops[0]["strategy_executed"] == "BM25"
    assert run.evidence_ids == ("syn-chunk-a",)
    assert run.handoffs[0] == {"handoff_id":"syn-handoff", "source_agent":"order",
                               "target_agent":"policy", "evidence_refs":["syn-order-ref"], "status":"SUCCEEDED"}
    assert run.tool_calls == ({"tool_id":"synthetic_read", "status":"SUCCEEDED", "duration_ms":5},)
    assert run.provider_usage_status == "RECORDED"
    assert (run.prompt_tokens, run.completion_tokens) == (31, 12)
    assert run.estimated_cost_usd == 0.003 and run.billed_cost_usd is None
    assert run.graph_execution_status is None
    assert run.business_outcome_status == "NOT_EXPOSED_BY_CURRENT_RUN_API"
    assert run.safety_result == "ALLOW"
    assert api.session_create_count == api.chat_post_count == 1
    assert "synthetic protected answer" not in repr(observation)
    assert "private_prompt" not in repr(observation)
    assert "must-not-project" not in repr(observation)
    assert "must-not-leak" not in repr(observation)


def test_event_projection_is_allowlisted_and_nested_values_are_bounded():
    from eval.m6_1.shoprelay_adapter import map_run_observation

    huge_ref = "x" * 500
    run = {"run": {"run_id": "r-safe", "status": "SUCCEEDED", "answer": {"answer": "private"}}}
    execution = {"execution": {"run_id": "r-safe", "citations": [],
        "handoffs": [{"handoff_id": "h1", "context_refs": [
            {"id": huge_ref, "private_prompt": "secret"} for _ in range(100)
        ], "private_prompt": "secret"}], "tool_calls": [], "spans": []}}
    events = [{"run_id": "r-safe", "event_id": "e1", "seq": 1, "type": "route_decided",
        "payload": {"strategy": "SINGLE", "answer": "private", "private_prompt": "secret",
                    "decision_source": "token=do-not-retain"}}]
    observed = map_run_observation(run_payload=run, event_payloads=events,
                                   execution_payload=execution)
    assert observed.event_refs[0]["payload"] == {"strategy": "SINGLE", "decision_source": "[REDACTED]"}
    assert len(observed.handoffs[0]["context_refs"]) == 50
    assert len(observed.handoffs[0]["context_refs"][0]["id"]) == 160
    assert "private" not in repr(observed) and "secret" not in repr(observed)


def test_non_object_event_rows_fail_closed():
    case, grant, gate = _synthetic_case_and_gate()
    api = mock_shoprelay_api()
    api._event_responses["mock-run-1"] = ({"event_id": "ok", "seq": 1, "type": "done"}, "bad-row")
    with pytest.raises(AdapterContractError, match="non-object"):
        SyntheticShopRelayAdapter(api, gate).collect_run(run_id="mock-run-1")


@pytest.mark.parametrize("mode", [ExecutionMode.DEV_SMOKE, ExecutionMode.FROZEN_EVAL])
def test_dev_and_frozen_modes_are_denied_before_any_mock_api_call(mode):
    case, grant, gate = _synthetic_case_and_gate()
    api = mock_shoprelay_api()
    adapter = SyntheticShopRelayAdapter(api, gate)
    with pytest.raises(ModeAuthorizationError):
        adapter.execute_case(mode=mode, grant=grant, case=case,
            experiment_id="exp.denied", client_request_id="request-denied")
    assert api.session_create_count == api.chat_post_count == api.get_count == 0


def test_mode_string_and_uncovered_case_are_rejected():
    case, grant, gate = _synthetic_case_and_gate()
    api = mock_shoprelay_api()
    adapter = SyntheticShopRelayAdapter(api, gate)
    with pytest.raises(ModeAuthorizationError, match="typed ExecutionMode"):
        adapter.execute_case(mode="DEV_SMOKE", grant=grant, case=case,
            experiment_id="exp.denied", client_request_id="request-denied")
    with pytest.raises(ModeAuthorizationError, match="not covered"):
        from dataclasses import replace
        adapter.execute_case(mode=ExecutionMode.SYNTHETIC, grant=grant,
            case=replace(case, case_id="ungranted-case"), experiment_id="exp.denied",
            client_request_id="request-denied")
    assert api.session_create_count == api.chat_post_count == 0


def test_unknown_and_running_results_are_not_converted_to_success_or_replayed():
    case, grant, gate = _synthetic_case_and_gate()
    for status, expected in (("UNKNOWN_RESULT", "UNKNOWN_RESULT"), ("MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE")):
        api = mock_shoprelay_api(status=status, usage=None)
        adapter = SyntheticShopRelayAdapter(api, gate)
        observation = adapter.execute_case(mode=ExecutionMode.SYNTHETIC, grant=grant, case=case,
            experiment_id="exp.synthetic.status", client_request_id=f"req-{status}")
        assert observation.lifecycle_status == expected
        assert observation.runs[0].provider_usage_status == "UNKNOWN"
        assert api.chat_post_count == 1
    api = mock_shoprelay_api(status="RUNNING")
    adapter = SyntheticShopRelayAdapter(api, gate)
    observation = adapter.execute_case(mode=ExecutionMode.SYNTHETIC, grant=grant, case=case,
        experiment_id="exp.synthetic.status", client_request_id="req-running")
    assert observation.lifecycle_status is None
    with pytest.raises(AdapterPending):
        observation.to_eval_trial(trial_id="trial.pending", case_id=case.case_id,
            experiment_id="exp.synthetic.status", trial_index=1)
    assert api.chat_post_count == 1


def test_stub_usage_and_estimated_cost_are_not_reported_as_real_usage_or_invoice():
    case, grant, gate = _synthetic_case_and_gate()
    api = mock_shoprelay_api(model_id="deterministic-test-stub", usage={
        "prompt_tokens": 10, "completion_tokens": 20, "cost_estimate_usd_peak_rate": 0.1})
    observation = SyntheticShopRelayAdapter(api, gate).execute_case(mode=ExecutionMode.SYNTHETIC,
        grant=grant, case=case, experiment_id="exp.stub", client_request_id="req-stub")
    run = observation.runs[0]
    assert run.provider_usage_status == "STUB"
    assert run.prompt_tokens is run.completion_tokens is None
    assert run.estimated_cost_usd is None and run.billed_cost_usd is None


def test_multiple_runs_remain_distinct_and_conflicting_event_refs_fail_closed():
    case, grant, gate = _synthetic_case_and_gate()
    run_ids = ["mock-run-1", "mock-run-2"]
    api = mock_shoprelay_api(run_ids=run_ids)
    adapter = SyntheticShopRelayAdapter(api, gate)
    runs = adapter.collect_runs(run_ids=run_ids)
    assert tuple(row.run_id for row in runs) == tuple(run_ids)
    assert all(row.graph_execution_status_source == "NOT_EXPOSED_BY_OWNER_RUN_API" for row in runs)
    from eval.m6_1.shoprelay_adapter import map_run_observation
    event = {"event_id":"same", "run_id":"r1", "seq":1, "type":"route_decided", "payload":{"strategy":"SINGLE"}}
    run_payload = {"run":{"run_id":"r1", "status":"SUCCEEDED"}}
    execution_payload = {"execution":{"run_id":"r1"}}
    with pytest.raises(AdapterContractError, match="conflicting duplicate"):
        map_run_observation(run_payload=run_payload, event_payloads=[event,
            {**event, "payload":{"strategy":"REFUSE"}}], execution_payload=execution_payload)


def test_adapter_refuses_live_transport_and_post_timeout_is_never_retried():
    case, grant, gate = _synthetic_case_and_gate()

    with pytest.raises(ModeAuthorizationError, match="in-memory synthetic transport"):
        SyntheticShopRelayAdapter(object(), gate)

    api = mock_shoprelay_api(post_exception=TimeoutError)
    adapter = SyntheticShopRelayAdapter(api, gate)
    with pytest.raises(AdapterUnknownResult) as captured:
        adapter.execute_case(mode=ExecutionMode.SYNTHETIC, grant=grant, case=case,
            experiment_id="exp.timeout", client_request_id="req-timeout")
    assert captured.value.client_request_id == "req-timeout"
    assert "sensitive transport detail" not in str(captured.value)
    assert api.chat_post_count == 1


def test_cancel_and_cleanup_report_missing_current_api_capabilities():
    case, grant, gate = _synthetic_case_and_gate()
    adapter = SyntheticShopRelayAdapter(mock_shoprelay_api(), gate)
    with pytest.raises(UnsupportedCapability, match="cancellation"):
        adapter.cancel(run_id="mock-run-1")
    with pytest.raises(UnsupportedCapability, match="cleanup"):
        adapter.cleanup_session(session_id="mock-session-1", run_ids=["mock-run-1"])
