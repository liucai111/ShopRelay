from __future__ import annotations

import json

import pytest

from app.m3.order_tools import (AgentExecutionContext, ToolExecutionError,
    not_executed_tool_audit)
from app.orchestration.observability import InMemoryObservabilitySink


def _context():
    events = []
    context = AgentExecutionContext(result_store={}, sink=InMemoryObservabilitySink(),
        run_id="audit-run-1", trace_id="audit-trace-1", parent_span_id=None,
        emit_event=lambda event_type, payload, event_key: events.append(
            {"event_type": event_type, "payload": payload, "event_key": event_key}),
        call_records=[])
    return context, events


def test_read_tool_audits_link_calls_without_persisting_raw_parameters():
    context, events = _context()
    context.execute_read("get_order", "ORD-PRIVATE-123", "owner-private-456",
        lambda order_id, owner_id: {"source": "business_database", "order_id": order_id,
            "owner_id": owner_id, "availability": "AVAILABLE"})
    context.execute_product_read("get_product_detail", {
        "product_id": "P-PRIVATE-123", "query": "private product search text"},
        lambda: {"source": "shoprelay_product_catalog", "availability": "AVAILABLE",
            "product": {"product_id": "P-PRIVATE-123"}})
    context.execute_policy_read("search_knowledge", query="private policy query text", mode="BM25",
        operation=lambda: {"source": "shoprelay_policy_retrieval", "result_ref": "tool_result:policy-audit",
            "evidence": [], "retrieval_count": 0})
    context.execute_policy_read("get_warranty", query="Warranty for P-PRIVATE-123", mode="BM25",
        result_ref="tool_result:warranty-audit", resource_ids={"product_id": "P-PRIVATE-123"},
        operation=lambda: {"source": "shoprelay_policy_retrieval", "result_ref": "tool_result:warranty-audit",
            "evidence": [], "retrieval_count": 0})
    context.execute_coupon_read("get_coupon", {"coupon_code": "PRIVATE-COUPON-77"},
        lambda: {"source": "shoprelay_coupon_catalog", "availability": "NOT_FOUND"})

    audits = context.call_records
    assert len(audits) == 5
    assert len({row["tool_call_id"] for row in audits}) == 5
    assert {row["tool"] for row in audits} == {
        "get_order", "get_product_detail", "search_knowledge", "get_warranty", "get_coupon"}
    assert all(row["run_id"] == "audit-run-1" and row["trace_id"] == "audit-trace-1" for row in audits)
    assert all(row["executed"] is True and row["result_ref"] for row in audits)
    assert all(row["parameter_fingerprint"] and len(row["parameter_fingerprint"]) == 64 for row in audits)
    serialized_audit = json.dumps(audits, ensure_ascii=False)
    serialized_events = json.dumps(events, ensure_ascii=False)
    for secret in ("ORD-PRIVATE-123", "owner-private-456", "P-PRIVATE-123",
                   "private product search text", "private policy query text", "PRIVATE-COUPON-77"):
        assert secret not in serialized_audit
        assert secret not in serialized_events

    for row in audits:
        linked = [event for event in events if event["payload"].get("tool_call_id") == row["tool_call_id"]]
        assert [event["event_type"] for event in linked] == ["tool_started", "tool_finished"]
        # The result reference is guaranteed on the completion event. For some
        # adapters it does not exist until execution returns, so the start event
        # may legitimately carry null; policy adapters that preallocate one can
        # include it earlier.
        started, finished = linked
        assert started["payload"].get("result_ref") in (None, row["result_ref"])
        assert finished["payload"].get("result_ref") == row["result_ref"]
        assert all(event["payload"].get("parameter_fingerprint") == row["parameter_fingerprint"]
            for event in linked)
        assert all(event["payload"].get("run_id") == row["run_id"] and
            event["payload"].get("trace_id") == row["trace_id"] for event in linked)


def test_failed_and_not_executed_calls_have_explicit_audit_states():
    context, _events = _context()
    with pytest.raises(ToolExecutionError, match="PRODUCT_LOOKUP_FAILED"):
        context.execute_product_read("get_product_detail", {"product_id": "P-PRIVATE-ERR"},
            lambda: (_ for _ in ()).throw(ToolExecutionError("PRODUCT_LOOKUP_FAILED")))
    failed = context.call_records[-1]
    assert failed["status"] == "FAILED" and failed["executed"] is True
    assert failed["result_ref"] and failed["tool_call_id"]

    pending = not_executed_tool_audit("get_product_detail", run_id="audit-run-1", trace_id="audit-trace-1",
        error_code="NO_CANDIDATES")
    assert pending["status"] == "NOT_EXECUTED" and pending["executed"] is False
    assert pending["result_ref"] is None and pending["parameter_fingerprint"] is None
    assert pending["tool_call_id"] not in {row["tool_call_id"] for row in context.call_records}
