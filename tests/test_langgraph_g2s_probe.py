from __future__ import annotations

import json

import pytest

from scripts.probe_langgraph_g2s import (DATASET, RequestLedger, _retrieved_ids,
    load_approved_cases, parse_sse, prior_q3_real_request_count)


def test_g2s_selects_only_the_two_approved_dev_cases():
    cases, source_dataset_digest, manifest_digest = load_approved_cases()
    assert [item["case_id"] for item in cases] == [
        "pr2-refund_pre_ship-01", "pr2-multi_clause_return_refund-03"]
    assert [item["expected_strategy"] for item in cases] == ["SINGLE", "MULTI"]
    assert all(item["dataset_split"] == "dev" and item["review_status"] == "APPROVED"
               and item["scorable"] is True and not item["test_candidate_only"] for item in cases)
    assert len(source_dataset_digest) == 64
    assert len(manifest_digest) == 64
    assert all(item["approved_gold_required_condition_count"] == len(item["acceptable_evidence_ids"])
               for item in cases)


def test_g2s_manifest_explicitly_rejects_test_candidate_rows(tmp_path):
    manifest = json.loads(DATASET.read_text(encoding="utf-8"))
    manifest["test_candidate_rows_included"] = True
    path = tmp_path / "invalid-smoke-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="exclude test_candidate"):
        load_approved_cases(path)


def test_q3_request_counter_counts_only_runs_using_approved_dev_manifest(tmp_path):
    old = tmp_path / "langgraph-g2s-real-old.json"
    old.write_text(json.dumps({"dataset": {"path": "old-full-dataset.json"},
                               "request_totals": {"request_count": 100}}), encoding="utf-8")
    current = tmp_path / "langgraph-g2s-real-q3.json"
    current.write_text(json.dumps({"dataset": {"probe_manifest": str(DATASET.relative_to(DATASET.parents[2]))},
                                  "request_totals": {"request_count": 3}}), encoding="utf-8")
    assert prior_q3_real_request_count(tmp_path) == 3


def test_request_ledger_enforces_stage_cap_before_ninth_http_request():
    ledger = RequestLedger(maximum=2)
    assert ledger.reserve_for_test() == 1
    assert ledger.reserve_for_test() == 2
    with pytest.raises(RuntimeError, match="budget exhausted before HTTP send"):
        ledger.reserve_for_test()
    assert ledger.totals()["request_count"] == 2
    assert ledger.denied_attempts == 1


def test_sse_parser_keeps_sequence_and_run_correlation():
    body = ("id: evt-1\nevent: started\ndata: {\"event_id\":\"evt-1\",\"run_id\":\"r1\","
            "\"trace_id\":\"t1\",\"seq\":1,\"payload\":{}}\n\n"
            "id: evt-2\nevent: answer\ndata: {\"event_id\":\"evt-2\",\"run_id\":\"r1\","
            "\"trace_id\":\"t1\",\"seq\":2,\"payload\":{}}\n\n")
    events = parse_sse(body)
    assert [event["type"] for event in events] == ["started", "answer"]
    assert [event["seq"] for event in events] == [1, 2]
    assert {event["run_id"] for event in events} == {"r1"}


def test_retrieval_evidence_scope_accepts_service_id_lists_and_persisted_hop_payloads():
    ids = _retrieved_ids(
        [{"evidence_ids": ["policy-a", "policy-b"]}],
        [{"type": "retrieval_hop", "payload": {"evidence_ids": ["policy-b", "policy-c"]}},
         {"type": "answer", "payload": {"chunk_id": "must-not-count"}}])
    assert ids == {"policy-a", "policy-b", "policy-c"}
