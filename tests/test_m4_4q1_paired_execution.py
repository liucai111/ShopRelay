from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import Run
from app.m1.services import set_demo_password


class _NoRouteClassifier:
    """Makes any unexpected fallback explicit; the four paired inputs route by rules."""


def _make_app(tmp_path: Path, monkeypatch, mode: str):
    import app.m1.api as api_module

    path = tmp_path / f"m44q1-{mode}.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    password = "m44q1-" + secrets.token_urlsafe(16)
    with factory() as db:
        set_demo_password(db, "u_demo", password)

    original_loader = api_module.load_settings

    def fixed_settings(**_kwargs):
        settings = original_loader(environ={"SHOPRELAY_M3_SUPERVISOR_ENABLED": "false"},
                                   local_values={}, require_api_key=False)
        return replace(settings, m3_supervisor_enabled=False)

    monkeypatch.setattr(api_module, "load_settings", fixed_settings)
    saver = MemorySaver()

    @asynccontextmanager
    async def checkpointer_factory(_database_url, _schema):
        yield saver

    app = create_app(database_url=url, engine=engine, orchestrator_mode=mode,
                     checkpointer_factory=checkpointer_factory,
                     checkpointer_readiness=lambda *_: True,
                     index_path=Path("data/index.json"))
    app.state.chat_provider = _NoRouteClassifier()
    return app, factory, engine, password, saver


def _login(client: TestClient, password: str):
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    response = client.post("/api/demo/login", headers={
        "Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: token,
    }, json={"username": "u_demo", "password": password})
    assert response.status_code == 200
    return {"Origin": str(client.base_url).rstrip("/"),
            CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _sse_rows(response):
    assert response.status_code == 200, response.text[:400]
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]


def test_legacy_and_langgraph_share_fixed_common_route_contracts(tmp_path, monkeypatch):
    """Paired API runs compare common routes with identical deterministic inputs/stubs."""
    from app.m1.models import RunEvent
    from app.m2 import policy_execution

    policy_calls = []
    evidence = [{
        "chunk_id": "fixture://m44q1/policy-rules-v1#return-refund",
        "title": "M4.4 paired comparison synthetic evidence",
        "content": "Deterministic fixture only; not a ShopRelay policy assertion.",
        "version": "synthetic-fixture-v1",
        "effective_from": "2026-01-01T00:00:00Z",
        "effective_to": None,
    }]

    def fixed_policy_executor(question, *, route, on_retrieval=None, **_kwargs):
        policy_calls.append({"question": question, "strategy": route["strategy"]})
        retrieval_event = {"retrieval_mode": "bm25", "knowledge_version": "synthetic-fixture-v1",
                           "index_fingerprint": "synthetic-fixed-snapshot-r1", "evidence_count": 1,
                           "evidence_ids": [evidence[0]["chunk_id"]]}
        if on_retrieval:
            on_retrieval(retrieval_event)
        return {
            "answer": "\u8be5\u914d\u5bf9\u7528\u4f8b\u53ea\u9a8c\u8bc1\u6267\u884c\u5668\u5951\u7ea6\uff0c\u4e0d\u8868\u8fbe\u771f\u5b9e\u653f\u7b56\u7ed3\u8bba\u3002",
            "answer_kind": "POLICY_ANSWER", "citations": [evidence[0]["chunk_id"]],
            "evidence": evidence, "knowledge_version": "synthetic-fixture-v1",
            "retrieval": {"mode": "bm25", "index_fingerprint": "synthetic-fixed-snapshot-r1"},
            "policy_retrieval": {"version": "m2-r-bounded-policy-v1",
                                  "strategy_selected": "BM25", "strategy_executed": "BM25",
                                  "knowledge_version": "synthetic-fixture-v1",
                                  "index_fingerprint": "synthetic-fixed-snapshot-r1",
                                  "evidence_ids": [evidence[0]["chunk_id"]]},
            "usage": {"model_requests": 0}, "model_request_count": 0,
            "estimated_cost_usd": 0.0, "latency_ms": 0, "terminal_status": "SUCCEEDED",
            "stop_reason": "FIXTURE_COMPLETED",
        }

    monkeypatch.setattr(policy_execution, "execute_policy_request", fixed_policy_executor)
    scenarios = [
        {"id": "DIRECT-GREETING", "query": "\u4f60\u597d", "expected": "DIRECT"},
        {"id": "SINGLE-POLICY", "query": "\u9000\u8d27\u653f\u7b56\u662f\u4ec0\u4e48\uff1f", "expected": "SINGLE"},
        {"id": "MULTI-POLICY", "query": "\u9000\u8d27\u548c\u9000\u6b3e\u653f\u7b56\u5982\u4f55\u8854\u63a5\uff1f", "expected": "MULTI"},
        {"id": "REFUSE-UNAUTHORIZED", "query": "\u67e5\u770b\u5176\u4ed6\u7528\u6237\u7684\u9000\u6b3e\u8bb0\u5f55", "expected": "REFUSE"},
    ]
    outcomes = {"legacy": [], "langgraph": []}
    apps = []
    try:
        for mode in ("legacy", "langgraph"):
            app, factory, engine, password, saver = _make_app(tmp_path, monkeypatch, mode)
            apps.append((app, factory, engine, saver))
            with TestClient(app) as client:
                headers = _login(client, password)
                for case in scenarios:
                    body = {"message": case["query"], "client_request_id": f"m44q1-{case['id']}"}
                    first = client.post("/api/chat/stream", headers=headers, json=body)
                    rows = _sse_rows(first)
                    run_id = rows[0]["run_id"]
                    types = [row["type"] for row in rows]
                    assert rows[-1]["type"] == "done"
                    assert rows[-1]["payload"]["status"] == "SUCCEEDED"
                    route = next(row["payload"] for row in rows if row["type"] == "route_decided")
                    assert route["strategy"] == case["expected"]
                    run_response = client.get(f"/api/runs/{run_id}")
                    assert run_response.status_code == 200
                    run = run_response.json()["run"]
                    assert run["status"] == "SUCCEEDED"
                    answer = run["answer"]
                    if case["expected"] in {"SINGLE", "MULTI"}:
                        assert [(item["chunk_id"] if isinstance(item, dict) else item) for item in answer["citations"]] == [evidence[0]["chunk_id"]]
                        assert answer["knowledge_version"] == "synthetic-fixture-v1"
                    prior_policy_calls = len(policy_calls)
                    replay = _sse_rows(client.post("/api/chat/stream", headers=headers, json=body))
                    assert replay[0]["run_id"] == run_id
                    assert len(policy_calls) == prior_policy_calls
                    with factory() as db:
                        stored_events = db.query(RunEvent).filter_by(run_id=run_id).order_by(RunEvent.seq).all()
                        assert [item.event_type for item in stored_events] == types
                        assert len({item.trace_id for item in stored_events}) == 1
                    graph_record = None
                    if mode == "langgraph":
                        from app.m1.models import GraphExecutionRecord
                        with factory() as db:
                            graph_record = db.query(GraphExecutionRecord).filter_by(run_id=run_id).one()
                        assert graph_record.status == "SUCCEEDED"
                    outcomes[mode].append({
                        "scenario_id": case["id"], "input": case["query"],
                        "strategy": route["strategy"], "route_source": route["decision_source"],
                        "run_status": run["status"], "answer_kind": answer.get("answer_kind"),
                        "answer": answer["answer"],
                        "citation_ids": [(item["chunk_id"] if isinstance(item, dict) else item) for item in answer["citations"]],
                        "knowledge_version": answer.get("knowledge_version"),
                        "sse_types": types, "trace_linked": bool(run_id and rows[0]["trace_id"]),
                        "idempotent_replay_same_run": replay[0]["run_id"] == run_id,
                        "graph_execution_status": graph_record.status if graph_record else None,
                        "model_requests": 0,
                    })
                if mode == "legacy":
                    agent_response = client.post("/api/chat", headers=headers, json={
                        "message": "\u67e5\u8be2\u8ba2\u5355 SYNTH-ORD-AGENT-01 \u5f53\u524d\u72b6\u6001",
                        "client_request_id": "m44q1-AGENT-LEGACY-UNAVAILABLE",
                    })
                    assert agent_response.status_code == 502
                    failure = agent_response.json()["details"]
                    agent_run = client.get(f"/api/runs/{failure['run_id']}").json()["run"]
                    agent_events = client.get(f"/api/runs/{failure['run_id']}/events").json()["events"]
                    agent_route = next(row["payload"] for row in agent_events
                                       if row["type"] == "route_decided")
                    assert agent_route["strategy"] == "AGENT"
                    assert agent_run["status"] == "FAILED"
                    assert agent_run["error"]["code"] == "ROUTE_EXECUTOR_UNAVAILABLE"
                    assert not any(row["type"] in {"answer", "done"} for row in agent_events)
                    outcomes["legacy_agent"] = {
                        "strategy": "AGENT", "run_status": agent_run["status"],
                        "error_code": agent_run["error"]["code"],
                        "answer_or_done_event": False,
                        "langgraph_agent_evidence": "docs/probes/m4.4-multi-agent-postgres-r2.json",
                        "langgraph_capability_difference": "PostgreSQL composite proof covers five Agent scenarios; not a paired-result equality claim",
                    }
        for left, right in zip(outcomes["legacy"], outcomes["langgraph"]):
            for field in ("scenario_id", "strategy", "run_status", "answer_kind", "answer",
                          "citation_ids", "knowledge_version", "sse_types", "model_requests"):
                assert left[field] == right[field], (left["scenario_id"], field)
        assert len(policy_calls) == 4  # SINGLE+MULTI once per mode; replays did not execute again.
        output = {
            "schema_version": "shoprelay.m4.4q1-paired-comparison.v1",
            "record_id": "M44Q1-PAIRED-COMMON-ROUTES-R1",
            "status": "PASS_DETERMINISTIC_STUB_PAIRED_COMMON_CAPABILITIES",
            "modes": ["legacy", "langgraph"],
            "configuration": {"router": "same deterministic decide_route rules", "policy_executor": "same fixed deterministic response/evidence stub",
                              "knowledge_version": "synthetic-fixture-v1", "index_fingerprint": "synthetic-fixed-snapshot-r1",
                              "authenticated_owner": "same seeded demo principal in independent cloned SQLite fixtures",
                              "sdk_retries": 0, "external_model_requests": 0},
            "scenarios": scenarios, "outcomes": outcomes,
            "comparison": {"shared_route_cases": 4, "result_contracts_equal": True,
                           "sse_event_order_equal": True, "idempotent_replay_extra_policy_calls": 0,
                           "legacy_run_statuses": [x["run_status"] for x in outcomes["legacy"]],
                           "langgraph_execution_statuses": [x["graph_execution_status"] for x in outcomes["langgraph"]]},
            "limitations": ["Synthetic evidence is a control fixture, not a real policy citation or quality result.",
                            "This paired run validates shared deterministic execution contracts, not production parity or model quality."],
        }
        target = os.environ.get("SHOPRELAY_M44Q1_PAIR_OUTPUT")
        if target:
            Path(target).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        for app, _factory, engine, _saver in apps:
            engine.dispose()
