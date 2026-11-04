from __future__ import annotations

import json
import secrets
from dataclasses import replace
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import select

from app.m0.config import load_settings
from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run, RunEvent, Trace
from app.m1.routing import RouteDecision
from app.m1.services import append_run_event, set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import checkpoint_conninfo, run_checkpointer_async, strict_serializer
from app.orchestration.graph import build_orchestration_graph, invoke_graph
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.persistence import SQLAlchemyObservabilitySink
from app.orchestration.state import GraphInput, graph_input_state


def fixed_route(strategy="DIRECT"):
    return RouteDecision(strategy=strategy,
        sub_strategy="BM25" if strategy == "SINGLE" else "MULTI" if strategy == "MULTI" else None,
        safety_decision="ALLOW", reason_code="G2_STUB", decision_source="RULE").model_dump(mode="json")


def test_orchestrator_config_defaults_and_environment_wins_local():
    default = load_settings(environ={}, local_values={}, require_api_key=False)
    assert default.orchestrator == "legacy"
    local = load_settings(environ={}, local_values={"SHOPRELAY_ORCHESTRATOR": "langgraph"}, require_api_key=False)
    assert local.orchestrator == "langgraph"
    overridden = load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "legacy"},
                               local_values={"SHOPRELAY_ORCHESTRATOR": "langgraph"}, require_api_key=False)
    assert overridden.orchestrator == "legacy"
    with pytest.raises(ValueError):
        load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "request"}, local_values={}, require_api_key=False)
    with pytest.raises(ValueError):
        load_settings(environ={"SHOPRELAY_LANGGRAPH_CHECKPOINT_SCHEMA": "bad-name"}, local_values={}, require_api_key=False)


def test_m3_supervisor_switch_is_opt_in_and_requires_langgraph():
    assert load_settings(environ={}, local_values={}, require_api_key=False).m3_supervisor_enabled is False
    enabled = load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
        "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true"}, local_values={}, require_api_key=False)
    assert enabled.m3_supervisor_enabled is True
    with pytest.raises(ValueError):
        load_settings(environ={"SHOPRELAY_M3_SUPERVISOR_ENABLED": "yes"}, local_values={}, require_api_key=False)
    with pytest.raises(ValueError):
        load_settings(environ={"SHOPRELAY_M3_SUPERVISOR_ENABLED": "true"}, local_values={}, require_api_key=False)


def test_checkpointer_serializer_is_strict_and_connection_search_path_is_scoped():
    serializer = strict_serializer()
    assert serializer.pickle_fallback is False
    assert serializer._allowed_msgpack_modules is None
    codec, body = serializer.dumps_typed({"run_id": "r1", "status": "RUNNING"})
    assert serializer.loads_typed((codec, body)) == {"run_id": "r1", "status": "RUNNING"}
    conninfo = checkpoint_conninfo("postgresql+psycopg://shoprelay:secret@127.0.0.1/db", "shoprelay_langgraph")
    parsed_conninfo = conninfo_to_dict(conninfo)
    assert parsed_conninfo["options"] == "-c search_path=shoprelay_langgraph"
    assert parsed_conninfo["password"] == "secret"
    assert "secret" not in repr({"diagnostic": "checkpoint connection configured"})
    with pytest.raises(ValueError):
        checkpoint_conninfo("postgresql://localhost/db", "public;drop_schema")


def test_checkpointer_runner_uses_psycopg_compatible_loop():
    import asyncio
    import sys

    async def loop_type():
        return type(asyncio.get_running_loop())

    loop_class = run_checkpointer_async(loop_type())
    if sys.platform == "win32":
        assert issubclass(loop_class, asyncio.SelectorEventLoop)
    else:
        assert issubclass(loop_class, asyncio.AbstractEventLoop)


def test_real_postgres_probe_is_locked_to_shoprelay_test_database():
    from scripts.g2_test_database import resolve_test_database_url
    url = resolve_test_database_url()
    assert url.database == "shoprelay_m11_test"
    assert url.host in {"127.0.0.1", "localhost", "::1"}
    assert url.query["connect_timeout"] == "3"
    with pytest.raises(ValueError):
        resolve_test_database_url("postgresql://127.0.0.1/shoprelay")


def test_graph_checkpoint_contains_no_query_or_answer_text():
    saver = MemorySaver()
    sink = InMemoryObservabilitySink()
    query = "private order number SR-ORD-991 policy question"
    services = GraphServices(router=lambda _query: fixed_route(),
        direct=lambda _state: {"answer": "private answer content", "answer_kind": "DIRECT"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    graph = build_orchestration_graph(services, sink=sink, checkpointer=saver)
    config = {"configurable": {"thread_id": "run-private", "checkpoint_ns": ""}}
    context = __import__("app.orchestration.state", fromlist=["GraphRuntimeContext"]).GraphRuntimeContext(
        query=query, memory_context="UNVERIFIED SUMMARY MUST STAY OUT OF CHECKPOINT")
    graph.invoke(graph_input_state(GraphInput(run_id="run-private", session_id="s1", trace_id="t1", query=query)),
                 context=context, config=config)
    snapshots = list(saver.list(config))
    assert snapshots
    stored = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
    assert "private order number" not in stored
    assert "private answer content" not in stored
    assert "UNVERIFIED SUMMARY MUST STAY OUT OF CHECKPOINT" not in stored
    assert "query" not in stored
    assert context.execution_result["answer"] == "private answer content"


def test_persistent_observability_sink_deduplicates_records(tmp_path):
    from app.m1.models import Base, ConversationSession, User, utc_now
    from app.m1.models import Run as RunModel

    engine = make_engine(f"sqlite:///{(tmp_path / 'obs.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = utc_now()
    with factory() as db:
        db.add(User(user_id="u1", display_name="User"))
        db.flush()
        db.add(ConversationSession(session_id="s1", user_id="u1", created_at=now, title=None))
        db.flush()
        db.add(RunModel(run_id="r1", user_id="u1", session_id="s1", client_request_id="c1",
            idempotency_scope="scope", request_hash="h", status="RUNNING", created_at=now, updated_at=now))
        db.commit()
    from app.orchestration.observability import MetricRecord, StructuredLog, TraceSpan
    sink = SQLAlchemyObservabilitySink(factory, user_id="u1", run_id="r1", trace_id="t1", graph_version="g2")
    span = TraceSpan(trace_id="t1", span_id="sp1", run_id="r1", node_name="router", start_time="2026-10-20T00:00:00Z",
        end_time="2026-10-20T00:00:00Z", duration_ms=1, status="OK", graph_version="g2")
    log = StructuredLog(timestamp="2026-10-20T00:00:00Z", level="INFO", trace_id="t1", run_id="r1",
        node="router", event_type="node_completed", sanitized_message="node completed", span_id="sp1")
    metric = MetricRecord(node_execution_count=1, node_duration_ms=[1], span_id="sp1", node_name="router", graph_version="g2")
    for _ in range(2):
        sink.record_span(span)
        sink.record_log(log)
        sink.record_metrics(metric)
    with factory() as db:
        rows = db.execute(select(ObservabilityRecord)).scalars().all()
    assert len(rows) == 3
    assert {row.record_type for row in rows} == {"span", "metric", "log"}
    engine.dispose()


def test_run_events_support_idempotent_graph_keys(tmp_path):
    from app.m1.models import Base, ConversationSession, User, utc_now

    engine = make_engine(f"sqlite:///{(tmp_path / 'events.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = utc_now()
    with factory() as db:
        db.add(User(user_id="u1", display_name="User"))
        db.flush()
        db.add(ConversationSession(session_id="s1", user_id="u1", created_at=now, title=None))
        db.flush()
        db.add(Run(run_id="r1", user_id="u1", session_id="s1", client_request_id="c1",
            idempotency_scope="scope", request_hash="h", status="RUNNING", created_at=now, updated_at=now))
        db.commit()
        first = append_run_event(db, "r1", "t1", "route_decided", {"strategy": "SINGLE"}, event_key="graph:route:1")
        second = append_run_event(db, "r1", "t1", "route_decided", {"strategy": "SINGLE"}, event_key="graph:route:1")
        assert first.event_id == second.event_id and first.seq == second.seq
        with pytest.raises(ValueError):
            append_run_event(db, "r1", "t1", "route_decided", {"strategy": "MULTI"}, event_key="graph:route:1")
        assert db.execute(select(RunEvent).where(RunEvent.run_id == "r1")).scalars().all().__len__() == 1
    engine.dispose()


def _api_fixture(tmp_path, services, saver, *, mode="langgraph", admins=(), checkpoint_ready=True):
    path = tmp_path / "g2-api.sqlite3"
    database_url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(database_url)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    password = "g2-test-" + secrets.token_urlsafe(18)
    with factory() as db:
        set_demo_password(db, "u_demo", password)

    @asynccontextmanager
    async def checkpointer_factory(_database_url, _schema):
        yield saver

    app = create_app(database_url=database_url, engine=engine, orchestrator_mode=mode,
        checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: checkpoint_ready,
        graph_services=services, observability_admin_user_ids=admins)
    return app, factory, password, engine


def _login(client, password):
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    result = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: token},
        json={"username": "u_demo", "password": password})
    assert result.status_code == 200
    return {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _seed_admin_run(factory, *, result=None, latency_ms=45, orchestrator="legacy", graph_record=False,
                    include_retrieval=False):
    from app.m1.models import utc_now
    from app.m1.services import create_conversation, new_id

    now = datetime(2026, 10, 20, tzinfo=timezone.utc)  # stable fixture inside the explicitly queried October window
    with factory() as db:
        session = create_conversation(db, "u_demo", "admin observability fixture")
        run_id, trace_id = new_id(), new_id()
        run = Run(run_id=run_id, user_id="u_demo", session_id=session.session_id,
            client_request_id=f"admin-{run_id}", idempotency_scope="admin-fixture",
            request_hash="a" * 64, status="SUCCEEDED", created_at=now, updated_at=now,
            started_at=now, finished_at=now, latency_ms=latency_ms, model_id="test-stub",
            usage_json=json.dumps({"prompt_tokens": 23, "completion_tokens": 9,
                                   "cost_estimate_usd_peak_rate": 0.0002}))
        db.add(run)
        db.flush()
        db.add(Trace(trace_id=trace_id, user_id="u_demo", run_id=run_id,
            event_type="orchestrator_selected", status="RECORDED",
            event_json=json.dumps({"orchestrator": orchestrator}), created_at=now))
        for seq, event_type, payload in [
            (1, "started", {"status": "RUNNING"}),
            (2, "route_decided", {"strategy": "SINGLE"}),
            (3, "agent_started", {"agent": "policy"}),
        ]:
            db.add(RunEvent(event_id=new_id(), run_id=run_id, session_id=session.session_id,
                trace_id=trace_id, seq=seq, timestamp=now, event_type=event_type,
                payload_json=json.dumps(payload)))
        for index in range(2):
            span_id = new_id()
            db.add(ObservabilityRecord(record_id=f"admin-fixture:{run_id}:span:{index}", run_id=run_id,
                user_id="u_demo", trace_id=trace_id, span_id=span_id, parent_span_id=None,
                graph_version="test", record_type="span", timestamp=now,
                payload_json=json.dumps({"trace_id": trace_id, "span_id": span_id, "run_id": run_id,
                    "node_name": f"test.node.{index}", "start_time": now.isoformat(),
                    "end_time": now.isoformat(), "duration_ms": 1, "status": "SUCCEEDED"})))
        if include_retrieval:
            db.add(RunEvent(event_id=new_id(), run_id=run_id, session_id=session.session_id,
                trace_id=trace_id, seq=4, timestamp=now, event_type="retrieval_hop",
                payload_json=json.dumps({"retrieval_index": 1, "aspect_id": "return_policy",
                    "strategy_selected": "BM25", "strategy_executed": "BM25", "evidence_count": 1,
                    "evidence_ids": ["p1"], "knowledge_version": "kb-fixture", "latency_ms": 7,
                    "query_anchor": "SENSITIVE_QUERY_MUST_NOT_BE_PROJECTED"})))
        if graph_record:
            db.add(GraphExecutionRecord(run_id=run_id, user_id="u_demo", thread_id=run_id,
                orchestrator="langgraph", graph_version="test", state_schema_version="test",
                execution_version="test", status="SUCCEEDED", started_at=now, updated_at=now,
                finished_at=now))
        if result is not None:
            run.result_json = json.dumps(result)
        db.commit()
        return run_id


def test_fastapi_langgraph_run_sse_replay_observability_and_idempotency(tmp_path):
    calls = []
    evidence = [{"chunk_id": "policy-1", "title": "Policy", "content": "Valid policy evidence", "version": "v1"}]
    def single(state):
        calls.append("single")
        state["_emit_retrieval_hop"]({"hop": 1, "evidence_count": 1, "knowledge_version": "kb-v1"})
        return {"answer": "Evidence-backed response", "answer_kind": "POLICY_ANSWER", "evidence": evidence,
                "citations": evidence, "knowledge_version": "kb-v1", "retrieval": {"mode": "bm25"},
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost_estimate_usd_peak_rate": 0.0001},
                "model_request_count": 1, "estimated_cost_usd": 0.0001, "terminal_status": "SUCCEEDED"}
    services = GraphServices(router=lambda _query: (calls.append("router") or fixed_route("SINGLE")),
        direct=lambda _state: {}, single=single, multi=lambda _state: {}, refuse=lambda _state: {})
    saver = MemorySaver()
    app, factory, password, engine = _api_fixture(tmp_path, services, saver, admins=("u_demo",))
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.post("/api/chat/stream", headers=headers, json={"message": "policy question", "client_request_id": "g2-1"})
            assert response.status_code == 200
            event_types = [line.removeprefix("event: ") for line in response.text.splitlines() if line.startswith("event: ")]
            assert event_types == ["started", "route_decided", "agent_started", "retrieval_hop", "answer", "done"]
            payloads = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
            seqs = [item["seq"] for item in payloads]
            assert seqs == sorted(set(seqs))
            run_id = payloads[0]["run_id"]
            replay = client.get(f"/api/runs/{run_id}/events?after_seq=0")
            assert replay.status_code == 200
            assert [item["type"] for item in replay.json()["events"]] == event_types
            duplicate = client.post("/api/chat/stream", headers=headers,
                json={"message": "policy question", "client_request_id": "g2-1"})
            assert duplicate.status_code == 200
            assert calls == ["router", "single"]
            now = datetime.now(timezone.utc)
            start = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            metrics = client.get(f"/api/admin/observability/metrics?from={start}&to={end}")
            assert metrics.status_code == 200
            assert metrics.json()["metrics"]["node_execution_count"] == 2
            assert metrics.json()["metrics"]["estimated_cost_usd"] == 0.0001
            with factory() as db:
                run = db.get(Run, run_id)
                graph_run = db.get(GraphExecutionRecord, run_id)
                selected = db.execute(select(Trace).where(Trace.run_id == run_id,
                    Trace.event_type == "orchestrator_selected")).scalar_one()
                assert run.status == "SUCCEEDED" and graph_run.status == "SUCCEEDED"
                assert graph_run.thread_id == run_id and graph_run.checkpoint_id
                assert json.loads(selected.event_json) == {
                    "execution_version": "g2-api-v1", "graph_version": "g2", "orchestrator": "langgraph"}
                assert db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id == run_id)).scalars().all()
            saved = list(saver.list({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}))
            checkpoint_state = repr([item.checkpoint.get("channel_values", {}) for item in saved])
            assert "policy question" not in checkpoint_state
            assert "Evidence-backed response" not in checkpoint_state
    finally:
        engine.dispose()


def test_actual_policy_executor_audit_is_persisted_through_langgraph_api(tmp_path):
    from types import SimpleNamespace
    from app.m2.policy_execution import execute_policy_request
    from app.m2.retrieval import RetrievalView

    class OfflineRetrieval:
        config = SimpleNamespace(mode="bm25", version="offline-r2", config_hash="offline-config",
            timeout_seconds=2.0)

        def pin_view(self, evaluation_at=None):
            return RetrievalView({"knowledge_version": "kb-r2", "chunks": []}, "fp-r2",
                evaluation_at or "2026-10-25T12:00:00Z")

        def retrieve(self, query, *, mode=None, view=None):
            row = {"chunk_id": "policy-r2-fixture", "title": "Synthetic policy",
                "content": "Synthetic offline test evidence.", "version": "doc-r2",
                "effective_from": "2026-01-01T00:00:00Z", "effective_to": "2027-01-01T00:00:00Z",
                "knowledge_version": "kb-r2", "index_fingerprint": "fp-r2", "rank": 1}
            return {"knowledge_version": "kb-r2", "index_fingerprint": "fp-r2",
                "config_version": "offline-r2", "config_hash": "offline-config",
                "mode": mode, "evidence": [row], "channels": {"bm25": [row["chunk_id"]]},
                "models": {"embedding": None, "reranker": None}, "diagnostics": {},
                "latency_ms": 1, "degraded": False, "failures": []}

    class OfflineProvider:
        def answer(self, question, evidence):
            return {"answer": "Offline evidence-backed answer.", "citations": [evidence[0]["chunk_id"]],
                "model_id": "offline-test-only", "thinking": "disabled",
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                "latency_ms": 1, "estimated_cost_usd": 0}

    def single(state):
        return execute_policy_request(state["query"], route=state["route"],
            provider=OfflineProvider(), retrieval=OfflineRetrieval(),
            evaluation_at="2026-10-25T12:00:00Z",
            on_retrieval=state.get("_emit_retrieval_hop"),
            on_retrieval_audit=state.get("_emit_policy_retrieval_audit"),
            run_id=state.get("run_id"), trace_id=state.get("trace_id"))

    services = GraphServices(router=lambda _query: fixed_route("SINGLE"),
        direct=lambda _state: {}, single=single, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver())
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.post("/api/chat", headers=headers,
                json={"message": "synthetic policy query", "client_request_id": "policy-audit-r2"})
            failure = None
            if response.status_code != 200:
                detail = response.json().get("details") or {}
                with factory() as db:
                    failed_run = db.get(Run, detail.get("run_id"))
                    failure = {"error_code": failed_run.error_code if failed_run else None,
                        "error_detail": failed_run.error_detail_json if failed_run else None}
            assert response.status_code == 200, {"response": response.text, "persisted_failure": failure}
            payload = response.json()
            assert payload["status"] == "SUCCEEDED"
            assert payload["policy_retrieval_audit"][0]["source_type"] == "POLICY_RETRIEVAL"
            audit = payload["policy_retrieval_audit"][0]
            assert audit["run_id"] == payload["run_id"] and audit["trace_id"]
            citation = payload["citations"][0]
            assert audit["result_ref"] in citation["result_refs"]
            assert audit["call_id"] in citation["retrieval_call_ids"]
            projected = client.get(f"/api/runs/{payload['run_id']}/execution").json()["execution"]
            assert projected["policy_retrieval_audit"][0]["call_id"] == audit["call_id"]
            assert projected["citations"][0]["result_refs"] == [audit["result_ref"]]
            saved_events = client.get(f"/api/runs/{payload['run_id']}/events?after_seq=0").json()["events"]
            by_type = {row["type"]: row for row in saved_events}
            assert "policy_retrieval_started" in by_type and "policy_retrieval_finished" in by_type
            started = by_type["policy_retrieval_started"]["payload"]
            finished = by_type["policy_retrieval_finished"]["payload"]
            assert started["call_id"] == finished["call_id"] == audit["call_id"]
            assert finished["result_ref"] == audit["result_ref"]
            assert "query" not in finished and "question" not in finished
        with factory() as db:
            saved = db.get(Run, payload["run_id"])
            result_json = json.loads(saved.result_json)
            assert result_json["policy_retrieval_audit"][0]["call_id"] == audit["call_id"]
            assert result_json["citations"][0]["result_refs"] == [audit["result_ref"]]
    finally:
        engine.dispose()


def test_m3_supervisor_requires_order_id_and_persists_safe_clarification_idempotently(tmp_path, monkeypatch):
    import app.m1.api as api_module

    loader = api_module.load_settings
    def m3_settings(**_kwargs):
        return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
            "SHOPRELAY_GRAPH_VERSION": "g2-m3.3", "SHOPRELAY_EXECUTION_VERSION": "m3.3-supervisor-v1"},
            local_values={}, require_api_key=False), m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", m3_settings)
    calls = []
    def router(_query):
        calls.append("router")
        return RouteDecision(strategy="AGENT", safety_decision="ALLOW", reason_code="LIVE_BUSINESS_FACT",
            decision_source="RULE", required_capabilities=["get_order"]).model_dump(mode="json")
    services = GraphServices(router=router, direct=lambda _state: {}, single=lambda _state: {},
        multi=lambda _state: {}, refuse=lambda _state: {})
    saver = MemorySaver()
    app, factory, password, engine = _api_fixture(tmp_path, services, saver)
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            payload = {"message": "查询我的订单当前状态", "client_request_id": "m3.1-agent-idempotent"}
            first = client.post("/api/chat", headers=headers, json=payload)
            assert first.status_code == 200
            assert first.json()["answer_kind"] == "CLARIFICATION_REQUIRED"
            second = client.post("/api/chat", headers=headers, json=payload)
            assert second.status_code in {200, 502, 503}
            assert calls == ["router"]
            with factory() as db:
                run = db.execute(select(Run).where(Run.client_request_id == payload["client_request_id"])).scalar_one()
                graph_run = db.get(GraphExecutionRecord, run.run_id)
                events = db.execute(select(RunEvent).where(RunEvent.run_id == run.run_id).order_by(RunEvent.seq)).scalars().all()
                records = db.execute(select(ObservabilityRecord).where(
                    ObservabilityRecord.run_id == run.run_id)).scalars().all()
                assert run.status == "SUCCEEDED" and run.error_code is None
                assert graph_run.status == "SUCCEEDED" and graph_run.graph_version == "g2-m3.3"
                assert graph_run.state_schema_version == "g2-m3.3-state-v1"
                assert [event.event_type for event in events].count("agent_started") == 1
                span_nodes = {json.loads(item.payload_json).get("node_name")
                    for item in records if item.record_type == "span"}
                assert {"m3.supervisor.plan", "m3.agent.order_logistics"}.issubset(span_nodes)
            config = {"configurable": {"thread_id": run.run_id, "checkpoint_ns": ""}}
            snapshots = list(saver.list(config))
            assert snapshots
            checkpoint_state = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
            assert payload["message"] not in checkpoint_state
            assert "user_id" not in checkpoint_state and "handoffs" in checkpoint_state
    finally:
        engine.dispose()


def test_m3_after_sales_write_route_requires_confirmation_without_execution(tmp_path, monkeypatch):
    import app.m1.api as api_module

    loader = api_module.load_settings
    def m3_settings(**_kwargs):
        return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph"}, local_values={},
            require_api_key=False), m3_supervisor_enabled=True)
    monkeypatch.setattr(api_module, "load_settings", m3_settings)
    def router(_query):
        return RouteDecision(strategy="AGENT", safety_decision="ALLOW", reason_code="BUSINESS_ACTION_REQUEST",
            decision_source="RULE", required_capabilities=["create_refund_request"]).model_dump(mode="json")
    services = GraphServices(router=router, direct=lambda _state: {}, single=lambda _state: {},
        multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver())
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.post("/api/chat", headers=headers, json={
                "message": "Please submit a refund request", "client_request_id": "m3.1-after-sales-confirm"
            })
            assert response.status_code == 502
            assert response.json()["code"] == "ACTION_CONFIRMATION_REQUIRED"
            assert "未执行任何写操作" in response.json()["message"]
            with factory() as db:
                run = db.execute(select(Run).where(Run.client_request_id == "m3.1-after-sales-confirm")).scalar_one()
                assert run.status == "FAILED" and run.error_code == "ACTION_CONFIRMATION_REQUIRED"
                assert run.result_json is None
                graph_run = db.get(GraphExecutionRecord, run.run_id)
                assert graph_run.status == "FAILED"
    finally:
        engine.dispose()


def test_langgraph_multi_failure_persists_bounded_answer_diagnostic_without_answer(tmp_path):
    raw = '{"answer":"未经校验的候选内容","citations":["p1"]}'
    diagnostic = {"node": "policy_answer", "provider_exception": "ValueError",
        "failure_reason": "final answer failed policy validation", "raw_content": raw,
        "response_chars": len(raw), "finish_reason": "stop",
        "usage_counts": {"prompt_units": 24, "completion_units": 8, "total_units": None}, "latency_ms": 123,
        "validation_errors": [{"code": "ANSWER_UNSUPPORTED_POLICY_PHASE"}]}
    def multi(state):
        state["_emit_retrieval_hop"]({"hop": 1, "evidence_count": 1, "knowledge_version": "kb-v1"})
        return {"terminal_status": "FAILED", "error_code": "ValueError", "answer": None,
            "answer_kind": None, "error_diagnostic": diagnostic, "evidence": [],
            "model_request_count": 3, "retrieval_hop_count": 1}
    services = GraphServices(router=lambda _query: fixed_route("MULTI"), direct=lambda _state: {},
        single=lambda _state: {}, multi=multi, refuse=lambda _state: {})
    saver = MemorySaver()
    app, factory, password, engine = _api_fixture(tmp_path, services, saver)
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.post("/api/chat/stream", headers=headers,
                json={"message": "两条政策如何衔接？", "client_request_id": "g2-failed-answer-diagnostic"})
            payloads = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
                        if line.startswith("data: ")]
            assert response.status_code == 200
            assert payloads[-1]["type"] == "error"
            assert not any(item["type"] in {"answer", "done"} for item in payloads)
            run_id = payloads[0]["run_id"]
            run_response = client.get(f"/api/runs/{run_id}")
            run = run_response.json()["run"]
            assert run["status"] == "FAILED" and run["answer"] is None
            persisted = run["error"]["details"]
            assert persisted["node"] == "policy_answer"
            assert persisted["raw_content"] == raw
            assert persisted["usage_counts"] == {"prompt_units": 24, "completion_units": 8, "total_units": None}
            assert persisted["validation_errors"] == [{"code": "ANSWER_UNSUPPORTED_POLICY_PHASE"}]
            assert run_response.status_code == 200
            with factory() as db:
                from app.m1.models import Message
                assert db.query(Message).filter_by(session_id=run["session_id"], role="assistant").count() == 0
            replay = client.post("/api/chat/stream", headers=headers,
                json={"message": "两条政策如何衔接？", "client_request_id": "g2-failed-answer-diagnostic"})
            assert replay.status_code == 200
            assert "未经校验的候选内容" not in replay.text
    finally:
        engine.dispose()


def test_post_checkpoint_failure_marks_unknown_and_idempotent_retry_never_reinvokes(tmp_path):
    class FailAfterTerminalCheckpoint(MemorySaver):
        def put(self, config, checkpoint, metadata, new_versions):
            values = checkpoint.get("channel_values", {})
            if values.get("terminal_status") == "SUCCEEDED":
                raise RuntimeError("checkpoint write failed")
            return super().put(config, checkpoint, metadata, new_versions)

    calls = []
    evidence = [{"chunk_id": "p1", "content": "policy", "title": "p1"}]
    def single(_state):
        calls.append("paid_or_effectful_executor")
        return {"answer": "result", "answer_kind": "POLICY_ANSWER", "evidence": evidence,
                "citations": evidence, "model_request_count": 1, "terminal_status": "SUCCEEDED"}
    services = GraphServices(router=lambda _query: fixed_route("SINGLE"), direct=lambda _state: {},
        single=single, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, FailAfterTerminalCheckpoint())
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            payload = {"message": "policy question", "client_request_id": "uncertain-1"}
            failed = client.post("/api/chat", headers=headers, json=payload)
            # The first non-retryable execution failure uses the existing 502 API contract.
            assert failed.status_code == 502
            assert failed.json()["details"]["status"] == "UNKNOWN_RESULT"
            run_id = failed.json()["details"]["run_id"]
            duplicate = client.post("/api/chat", headers=headers, json=payload)
            assert duplicate.status_code == 503
            assert calls == ["paid_or_effectful_executor"]
            with factory() as db:
                run = db.get(Run, run_id)
                graph_run = db.get(GraphExecutionRecord, run_id)
                assert run.status == "UNKNOWN_RESULT"
                assert run.result_json is None
                assert graph_run.status == "UNKNOWN"
    finally:
        engine.dispose()


def test_metrics_is_default_deny_for_non_allowlisted_user(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route(), direct=lambda _state: {"answer": "hello", "answer_kind": "DIRECT"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, _, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=())
    try:
        with TestClient(app) as client:
            _login(client, password)
            response = client.get("/api/admin/observability/metrics?from=2026-10-19T00:00:00Z&to=2026-10-21T00:00:00Z")
            assert response.status_code == 403
    finally:
        engine.dispose()


def test_admin_observability_uses_bounded_sanitized_persistent_projections(tmp_path):
    from app.m1.models import utc_now

    sensitive_answer = "SENTINEL_PRIVATE_ANSWER_SHOULD_NOT_APPEAR_IN_ADMIN_TRACE"
    result = {"strategy": "SINGLE", "route": {"strategy": "SINGLE"},
        "retrieval": {"strategy_selected": "BM25", "strategy_executed": "BM25", "degraded": False},
        "citations": [{"chunk_id": "p1", "content": "private evidence body"}], "answer": sensitive_answer}
    services = GraphServices(router=lambda _query: fixed_route("SINGLE"), direct=lambda _state: {}, single=lambda _state: {},
        multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=("u_demo",))
    run_id = _seed_admin_run(factory, result=result, include_retrieval=True)
    try:
        with TestClient(app) as client:
            unauthenticated = client.get("/api/admin/observability/overview?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z")
            assert unauthenticated.status_code == 401
            headers = _login(client, password)
            start, end = "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"
            overview = client.get(f"/api/admin/observability/overview?from={start}&to={end}", headers=headers)
            assert overview.status_code == 200
            metrics = overview.json()["overview"]
            assert metrics["run_counts"]["total"] == 1
            assert metrics["run_counts"]["by_status"] == {"SUCCEEDED": 1}
            assert metrics["run_counts"]["success_rate"] == 1
            assert metrics["latency_ms"]["p50"] == 45
            assert metrics["latency_ms"]["source"].startswith("Run.latency_ms only")
            assert metrics["run_wall_duration_ms"]["sample_count"] == 1
            assert metrics["model"]["stub_run_count"] == 1
            assert metrics["model"]["provider_usage_run_count"] == 0
            assert metrics["model"]["prompt_tokens"] == 0
            assert metrics["retrieval"]["bm25_count"] == 1
            assert metrics["retrieval"]["canonical_requested_strategy_counts"] == {"BM25": 1}
            assert metrics["retrieval"]["canonical_executed_strategy_counts"] == {"BM25": 1}
            assert metrics["retrieval"]["canonical_counts_conserve_raw_counts"] == {"requested": True, "executed": True}
            assert metrics["events"]["persisted_sse_event_count"] >= 3
            listed = client.get(f"/api/admin/observability/runs?from={start}&to={end}&strategy=SINGLE&limit=10", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["runs"]["items"][0]["run_id"] == run_id
            actual_executor = listed.json()["runs"]["items"][0]["orchestrator"]
            assert actual_executor == "legacy"
            assert listed.json()["runs"]["items"][0]["orchestrator_evidence"] == "orchestrator_selected_trace"
            executor_filter = client.get(
                f"/api/admin/observability/runs?from={start}&to={end}&orchestrator={actual_executor}&limit=10", headers=headers)
            assert executor_filter.status_code == 200
            assert executor_filter.json()["runs"]["items"][0]["run_id"] == run_id
            other_executor = "legacy" if actual_executor == "langgraph" else "langgraph"
            excluded = client.get(
                f"/api/admin/observability/runs?from={start}&to={end}&orchestrator={other_executor}&limit=10", headers=headers)
            assert excluded.status_code == 200 and excluded.json()["runs"]["items"] == []
            unknown_filter = client.get(
                f"/api/admin/observability/runs?from={start}&to={end}&orchestrator=unknown", headers=headers
            )
            assert unknown_filter.status_code == 200 and unknown_filter.json()["runs"]["items"] == []
            detail = client.get(f"/api/admin/observability/runs/{run_id}?event_limit=1&span_limit=1", headers=headers)
            assert detail.status_code == 200
            data = detail.json()["detail"]
            assert data["run"]["run_id"] == run_id
            assert data["orchestrator"] == "legacy"
            assert data["retrieval_hops"]["items"][0]["canonical_executed_strategy"] == "BM25"
            assert data["retrieval_hops"]["items"][0]["evidence_ids"] == ["p1"]
            assert data["retrieval_hops"]["items"][0]["run_id"] == run_id
            assert "SENSITIVE_QUERY_MUST_NOT_BE_PROJECTED" not in detail.text
            assert data["events"]["has_more"] is True
            assert data["spans"]["has_more"] is True
            assert sensitive_answer not in detail.text
            assert "private evidence body" not in detail.text
            assert "cookie" not in detail.text.casefold()
            assert client.get(f"/api/admin/observability/runs/{run_id}?event_limit=0").status_code == 422
            filtered = client.get(f"/api/admin/observability/runs?from={start}&to={end}&agent=policy&limit=10", headers=headers)
            assert filtered.status_code == 200
            assert filtered.json()["runs"]["items"][0]["run_id"] == run_id
            empty = client.get("/api/admin/observability/overview?from=2025-10-01T00:00:00Z&to=2025-10-02T00:00:00Z",
                               headers=headers).json()["overview"]
            assert empty["run_counts"]["total"] == 0
            assert empty["run_counts"]["success_rate"] is None
            assert empty["latency_ms"]["p50"] is None
            assert empty["run_wall_duration_ms"]["p50"] is None

        with factory() as db:
            source = db.get(Run, run_id)
            db.add(Run(run_id="admin-unknown-fixture", user_id=source.user_id, session_id=source.session_id,
                client_request_id="unknown-fixture", idempotency_scope="fixture", request_hash="0" * 64,
                status="UNKNOWN_RESULT", created_at=source.created_at, updated_at=source.created_at,
                started_at=source.created_at, finished_at=source.created_at, latency_ms=None,
                error_code="CHECKPOINT_PERSISTENCE_FAILED"))
            db.commit()
        with TestClient(app) as client:
            headers = _login(client, password)
            overview = client.get("/api/admin/observability/overview?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
                                 headers=headers).json()["overview"]
            assert overview["run_counts"]["by_status"]["UNKNOWN_RESULT"] == 1
            assert overview["run_counts"]["success_rate"] == 0.5
            assert overview["run_counts"]["unknown_result_count"] == 1
    finally:
        engine.dispose()


def test_admin_canonical_strategy_aliases_preserve_algorithm_distinctions():
    from app.orchestration.admin_observability import canonical_retrieval_strategy, _resolve_orchestrator

    assert canonical_retrieval_strategy("BM25") == canonical_retrieval_strategy("bm25") == "BM25"
    assert canonical_retrieval_strategy("RRF") == "RRF"
    assert canonical_retrieval_strategy("rrf_reranker") == "HYBRID_RERANK"
    assert canonical_retrieval_strategy("HYBRID_RERANK") == "HYBRID_RERANK"
    assert canonical_retrieval_strategy("RRF") != canonical_retrieval_strategy("rrf_reranker")
    assert canonical_retrieval_strategy("rrf-vNext") == "OTHER"
    assert _resolve_orchestrator("langgraph", ["langgraph"]) == ("langgraph", "graph_execution")
    assert _resolve_orchestrator(None, ["langgraph"]) == ("langgraph", "orchestrator_selected_trace")
    assert _resolve_orchestrator("langgraph", ["legacy"]) == ("unknown", "CONFLICTING_EVIDENCE")
    assert _resolve_orchestrator(None, []) == ("unknown", "NO_RELIABLE_EVIDENCE")


def test_admin_strategy_alias_aggregation_conserves_raw_counts_and_keeps_unknowns(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route("SINGLE"), direct=lambda _state: {},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=("u_demo",))
    labels = [("BM25", "BM25"), ("bm25", "bm25"), ("RRF", "RRF"),
              ("rrf_reranker", "rrf_reranker"), ("custom-vNext", "custom-vNext")]
    for index, (requested, executed) in enumerate(labels):
        _seed_admin_run(factory, result={"strategy": "SINGLE", "retrieval": {
            "strategy_selected": requested, "strategy_executed": executed, "degraded": False}})
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.get(
                "/api/admin/observability/overview?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z",
                headers=headers)
            assert response.status_code == 200
            retrieval = response.json()["overview"]["retrieval"]
            assert retrieval["requested_strategy_counts"] == {
                "BM25": 1, "RRF": 1, "bm25": 1, "custom-vNext": 1, "rrf_reranker": 1}
            assert retrieval["canonical_requested_strategy_counts"] == {
                "BM25": 2, "HYBRID_RERANK": 1, "OTHER": 1, "RRF": 1}
            assert retrieval["canonical_executed_strategy_counts"] == retrieval["canonical_requested_strategy_counts"]
            assert retrieval["canonical_counts_conserve_raw_counts"] == {"requested": True, "executed": True}
    finally:
        engine.dispose()


def test_admin_retrieval_projection_bounds_raw_labels_and_direct_has_no_inferred_hops(tmp_path):
    from app.m1.models import RunEvent, utc_now
    from app.orchestration.admin_observability import _retrieval_hop_projection

    event = RunEvent(event_id="ev-probe", run_id="run-probe", session_id="s1", trace_id="t1", seq=1,
        timestamp=utc_now(), event_type="retrieval_hop",
        payload_json=json.dumps({"strategy_selected": "x" * 200, "strategy_executed": "rrf-vNext",
            "evidence_ids": ["e" * 200]}))
    projection = _retrieval_hop_projection(event)
    assert len(projection["requested_strategy"]) == 120
    assert projection["requested_strategy_truncated"] is True
    assert projection["canonical_requested_strategy"] == "OTHER"
    assert projection["canonical_executed_strategy"] == "OTHER"
    assert len(projection["evidence_ids"][0]) == 160
    assert projection["evidence_ids_truncated"] is False
    assert projection["evidence_id_values_truncated"] is True

    services = GraphServices(router=lambda _query: fixed_route("DIRECT"), direct=lambda _state: {},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=("u_demo",))
    run_id = _seed_admin_run(factory, result={"strategy": "DIRECT"}, include_retrieval=False)
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            response = client.get(f"/api/admin/observability/runs/{run_id}", headers=headers)
            assert response.status_code == 200
            detail = response.json()["detail"]
            assert detail["retrieval_hops"]["items"] == []
            assert detail["retrieval_hops"]["not_inferred_from_aggregates"] is True
    finally:
        engine.dispose()


def test_admin_missing_graph_execution_uses_explicit_trace_or_unknown(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route("DIRECT"),
        direct=lambda _state: {"answer": "hello", "answer_kind": "DIRECT"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=("u_demo",))
    run_id = _seed_admin_run(factory, orchestrator="langgraph", graph_record=True)
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            with factory() as db:
                graph = db.get(GraphExecutionRecord, run_id)
                assert graph is not None and graph.orchestrator == "langgraph"
                db.delete(graph)
                db.commit()
                evidence = db.execute(select(Trace).where(Trace.run_id == run_id,
                    Trace.event_type == "orchestrator_selected")).scalars().all()
                assert len(evidence) == 1
            start, end = "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"
            query = f"from={start}&to={end}"
            all_runs = client.get(f"/api/admin/observability/runs?{query}", headers=headers).json()["runs"]
            assert next(item for item in all_runs["items"] if item["run_id"] == run_id)["orchestrator"] == "langgraph"
            with factory() as db:
                evidence = db.execute(select(Trace).where(Trace.run_id == run_id,
                    Trace.event_type == "orchestrator_selected")).scalar_one()
                db.delete(evidence)
                db.commit()
            all_runs = client.get(f"/api/admin/observability/runs?{query}", headers=headers).json()["runs"]
            assert next(item for item in all_runs["items"] if item["run_id"] == run_id)["orchestrator"] == "unknown"
            unknown = client.get(f"/api/admin/observability/runs?{query}&orchestrator=unknown", headers=headers)
            assert unknown.status_code == 200
            assert any(item["run_id"] == run_id for item in unknown.json()["runs"]["items"])
            detail = client.get(f"/api/admin/observability/runs/{run_id}", headers=headers).json()["detail"]
            assert detail["orchestrator"] == "unknown"
            assert detail["orchestrator_evidence"] == "NO_RELIABLE_EVIDENCE"
    finally:
        engine.dispose()


def test_admin_observability_routes_remain_deny_by_default(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route(), direct=lambda _state: {"answer": "hello"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, _, password, engine = _api_fixture(tmp_path, services, MemorySaver(), admins=())
    try:
        with TestClient(app) as client:
            headers = _login(client, password)
            query = "from=2026-10-01T00:00:00Z&to=2026-10-02T00:00:00Z"
            assert client.get(f"/api/admin/observability/overview?{query}", headers=headers).status_code == 403
            assert client.get(f"/api/admin/observability/runs?{query}", headers=headers).status_code == 403
            assert client.get("/api/admin/observability/runs/not-found", headers=headers).status_code == 403
    finally:
        engine.dispose()


def test_langgraph_startup_fails_closed_when_checkpoint_schema_is_not_ready(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route(), direct=lambda _state: {"answer": "hello"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, _, _, engine = _api_fixture(tmp_path, services, MemorySaver(), checkpoint_ready=False)
    try:
        with pytest.raises(RuntimeError, match="checkpoint schema is not initialized"):
            with TestClient(app):
                pass
        assert app.state.orchestrator_mode == "langgraph"
    finally:
        engine.dispose()


def test_two_users_cannot_read_langgraph_run_or_events(tmp_path):
    services = GraphServices(router=lambda _query: fixed_route("DIRECT"),
        direct=lambda _state: {"answer": "hello", "answer_kind": "DIRECT"},
        single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
    app, factory, password, engine = _api_fixture(tmp_path, services, MemorySaver())
    with factory() as db:
        set_demo_password(db, "u_other", password)
    try:
        with TestClient(app) as owner_client:
            headers = _login(owner_client, password)
            attempted_override = owner_client.post("/api/chat", headers=headers,
                json={"message": "hello", "client_request_id": "cannot-switch-mode", "orchestrator": "legacy"})
            assert attempted_override.status_code == 200
            ignored_run_id = attempted_override.json()["run_id"]
            with factory() as db:
                assert db.get(GraphExecutionRecord, ignored_run_id).orchestrator == "langgraph"
            response = owner_client.post("/api/chat/stream", headers=headers,
                json={"message": "hello", "client_request_id": "owner-only"})
            assert response.status_code == 200
            events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            run_id = events[0]["run_id"]
        with TestClient(app) as other_client:
            token = other_client.get("/api/demo/csrf").json()["csrf_token"]
            login = other_client.post("/api/demo/login",
                headers={"Origin": str(other_client.base_url).rstrip("/"), CSRF_HEADER: token},
                json={"username": "u_other", "password": password})
            assert login.status_code == 200
            assert other_client.get(f"/api/runs/{run_id}").status_code == 404
            assert other_client.get(f"/api/runs/{run_id}/events?after_seq=0").status_code == 404
    finally:
        engine.dispose()
