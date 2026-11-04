from __future__ import annotations

import sqlite3
import json
import secrets
import uuid
from contextlib import closing
from pathlib import Path
import gc

import pytest
from pydantic import ValidationError
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.m0.data import ROOT, seed
from app.m2.retrieval import RetrievalUnavailable
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, create_app
from app.m1.db import (LATEST_SCHEMA_REVISION, LEGACY_SCHEMA_REVISION, make_engine,
                       make_session_factory, readiness_state, upgrade_database)
from app.m1.models import ConversationSession, Message, Trace
from app.m1.services import set_demo_password, transition_run

PASSWORD_A = "m1-test-" + secrets.token_urlsafe(18)
PASSWORD_B = "m1-test-" + secrets.token_urlsafe(18)


def new_db_file() -> Path:
    return ROOT / ".tmp" / f"m1-test-{uuid.uuid4().hex}.sqlite3"


def remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest.fixture
def prepared_app():
    path = new_db_file()
    db_url = f"sqlite:///{path.as_posix()}"
    seed(path)
    upgrade_database(db_url)
    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    with factory() as db:
        set_demo_password(db, "u_demo", PASSWORD_A)
        set_demo_password(db, "u_other", PASSWORD_B)
    app = create_app(database_url=db_url, engine=engine)
    try:
        yield app, db_url, factory
    finally:
        gc.collect()
        engine.dispose(close=True)
        gc.collect()
        remove_db(path)


def test_opt_in_retrieval_prewarm_runs_during_lifespan(prepared_app, monkeypatch):
    prepared, db_url, _ = prepared_app
    calls = []
    monkeypatch.setenv("SHOPRELAY_RETRIEVAL_PREWARM", "true")
    app = create_app(database_url=db_url, engine=prepared.state.engine)
    monkeypatch.setattr(app.state.retrieval_service, "warmup", lambda: calls.append("warmup") or {
        "status": "ready", "warmup_total_ms": 12.5,
    })
    with TestClient(app):
        assert app.state.retrieval_prewarm == {"status": "ready", "warmup_total_ms": 12.5}
    assert calls == ["warmup"]


def test_failed_retrieval_prewarm_keeps_service_startup_available(prepared_app, monkeypatch):
    prepared, db_url, _ = prepared_app
    monkeypatch.setenv("SHOPRELAY_RETRIEVAL_PREWARM", "1")
    app = create_app(database_url=db_url, engine=prepared.state.engine)
    def fail_warmup():
        raise RetrievalUnavailable("RERANKER_UNAVAILABLE", "ModelLoadError")
    monkeypatch.setattr(app.state.retrieval_service, "warmup", fail_warmup)
    with TestClient(app):
        assert app.state.retrieval_prewarm == {
            "status": "unavailable", "error_code": "RERANKER_UNAVAILABLE", "error_type": "RetrievalUnavailable",
        }


def login(client: TestClient, username: str, password: str):
    csrf = client.get("/api/demo/csrf")
    token = csrf.json()["csrf_token"]
    result = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: token},
                         json={"username": username, "password": password})
    return result


def test_startup_is_read_only_and_live_has_trace_id():
    path = new_db_file()
    db_url = f"sqlite:///{path.as_posix()}"
    app = create_app(database_url=db_url)
    try:
        with TestClient(app) as client:
            response = client.get("/health/live")
            assert response.status_code == 200
            assert response.json()["trace_id"]
            assert response.json()["trace_id"] == response.headers["x-trace-id"]
            schema = client.get("/openapi.json")
            assert schema.status_code == 200
            assert schema.json()["info"]["version"] == "2.3.0"
            assert "/api/demo/login" in schema.json()["paths"]
            assert "/api/orders/{order_id}" in schema.json()["paths"]
            assert "/api/chat/stream" in schema.json()["paths"]
            assert "/api/runs/{run_id}/events" in schema.json()["paths"]
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        app.state.engine.dispose()
        remove_db(path)


def test_login_is_verified_cookie_based_and_user_id_is_not_authority(prepared_app):
    app, _, factory = prepared_app
    with TestClient(app) as client:
        denied = client.get("/api/sessions/not-a-session")
        assert denied.status_code == 401
        bad = client.get("/api/demo/csrf")
        bad_token = bad.json()["csrf_token"]
        denied_login = client.post("/api/demo/login", headers={"Origin": "http://testserver", CSRF_HEADER: bad_token},
                                   json={"username": "u_demo", "password": "wrong", "user_id": "u_other"})
        assert denied_login.status_code == 401
        success = client.post("/api/demo/login", headers={"Origin": "http://testserver", CSRF_HEADER: bad_token},
                              json={"username": "u_demo", "password": PASSWORD_A, "user_id": "u_other"})
        assert success.status_code == 200
        assert success.json()["user"]["user_id"] == "u_demo"
        cookie = next(item for item in success.headers.get_list("set-cookie") if item.startswith(SESSION_COOKIE + "="))
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        with factory() as db:
            from app.m1.models import DemoCredential
            credential = db.get(DemoCredential, "u_demo")
            assert credential.password_hash != PASSWORD_A
            assert credential.password_hash.startswith("scrypt$")
        assert client.cookies.get(SESSION_COOKIE)


def test_session_list_is_authenticated_bounded_and_owner_scoped(prepared_app):
    app, _, _ = prepared_app
    with TestClient(app) as owner:
        assert owner.get("/api/sessions").status_code == 401
        assert login(owner, "u_demo", PASSWORD_A).status_code == 200
        csrf = owner.cookies.get(CSRF_COOKIE)
        headers = {"Origin": str(owner.base_url).rstrip("/"), CSRF_HEADER: csrf}
        created = owner.post("/api/sessions", headers=headers, json={})
        assert created.status_code == 200
        own_id = created.json()["session"]["session_id"]
        posted = owner.post(f"/api/sessions/{own_id}/messages", headers=headers, json={"content": "会话标题来自首条消息"})
        assert posted.status_code == 200
        own_list = owner.get("/api/sessions?limit=999")
        assert own_list.status_code == 422
        own_list = owner.get("/api/sessions")
        assert own_list.status_code == 200
        assert [item["session_id"] for item in own_list.json()["sessions"]] == [own_id]
        assert own_list.json()["sessions"][0]["title"] == "会话标题来自首条消息"
        assert "user_id" not in own_list.json()["sessions"][0]

    with TestClient(app) as other:
        assert login(other, "u_other", PASSWORD_B).status_code == 200
        other_list = other.get("/api/sessions")
        assert other_list.status_code == 200
        assert other_list.json()["sessions"] == []


def test_csrf_and_origin_are_required_on_state_changes(prepared_app):
    app, _, _ = prepared_app
    with TestClient(app) as client:
        csrf = client.get("/api/demo/csrf").json()["csrf_token"]
        no_origin = client.post("/api/demo/login", headers={CSRF_HEADER: csrf},
                                json={"username": "u_demo", "password": PASSWORD_A})
        assert no_origin.status_code == 403
        wrong_origin = client.post("/api/demo/login", headers={"Origin": "https://evil.example", CSRF_HEADER: csrf},
                                   json={"username": "u_demo", "password": PASSWORD_A})
        assert wrong_origin.status_code == 403
        wrong_token = client.post("/api/demo/login", headers={"Origin": "http://testserver", CSRF_HEADER: "wrong"},
                                  json={"username": "u_demo", "password": PASSWORD_A})
        assert wrong_token.status_code == 403


def test_session_memory_clear_is_authenticated_csrf_protected_and_owner_scoped(prepared_app):
    app, _, factory = prepared_app
    with TestClient(app) as client:
        assert login(client, "u_demo", PASSWORD_A).status_code == 200
        csrf = client.get("/api/demo/csrf").json()["csrf_token"]
        created = client.post("/api/sessions", headers={"Origin": "http://testserver", CSRF_HEADER: csrf},
                              json={"title": "memory clear test"})
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        assert client.delete(f"/api/sessions/{session_id}/memory").status_code == 403
        cleared = client.delete(f"/api/sessions/{session_id}/memory",
            headers={"Origin": "http://testserver", CSRF_HEADER: csrf})
        assert cleared.status_code == 200
        assert cleared.json()["status"] == "memory_cleared"
    with TestClient(app) as other:
        assert login(other, "u_other", PASSWORD_B).status_code == 200
        csrf = other.get("/api/demo/csrf").json()["csrf_token"]
        response = other.delete(f"/api/sessions/{session_id}/memory",
            headers={"Origin": "http://testserver", CSRF_HEADER: csrf})
        assert response.status_code == 404
    with factory() as db:
        from app.m1.models import SessionMemorySummary
        marker = db.query(SessionMemorySummary).filter_by(session_id=session_id).one()
        assert marker.is_clear_marker and marker.summary_json is None


def test_memory_status_is_owner_scoped_and_never_returns_summary_text(prepared_app):
    from app.m1.models import SessionMemorySummary, utc_now
    from app.m1.services import create_conversation, create_run

    app, _, factory = prepared_app
    with TestClient(app) as owner:
        assert login(owner, "u_demo", PASSWORD_A).status_code == 200
        with factory() as db:
            session = create_conversation(db, "u_demo", "memory status")
            session_id = session.session_id
            run, _ = create_run(db, "u_demo", session_id, "memory-status-run", {"message": "x"})
            run.status = "SUCCEEDED"
            run.result_json = json.dumps({"context_memory": {"level": "AUTO_COMPACT",
                "estimated_tokens_before": 1000, "estimated_tokens_after": 300,
                "effective_context_tokens": 2000, "compression_ratio": 0.7,
                "thresholds": {"soft": 0.5, "hard": 0.7, "auto_compact": 0.85},
                "summary_persisted": True, "summary_version": 1}})
            db.add(SessionMemorySummary(summary_id="memory-summary-status", owner_id="u_demo",
                session_id=session_id, version=1, source_run_id=run.run_id, source_through_sequence=18,
                source_message_ids_json=json.dumps(["m1", "m2"]),
                summary_json=json.dumps({"private_summary_text": "do-not-expose"}),
                estimated_tokens_before=1000, estimated_tokens_after=300,
                is_clear_marker=False, created_at=utc_now()))
            db.commit()
        csrf = owner.get("/api/demo/csrf").json()["csrf_token"]
        headers = {"Origin": "http://testserver", CSRF_HEADER: csrf}
        response = owner.get(f"/api/sessions/{session_id}/memory")
        assert response.status_code == 200
        memory = response.json()["memory"]
        assert memory["status"] == "PERSISTED"
        assert memory["summary_persisted"] is True
        assert memory["summary_content_exposed"] is False
        assert memory["source_message_count"] == 2
        assert memory["level"] == "AUTO_COMPACT"
        assert memory["load_ratio"] == 0.5
        assert "do-not-expose" not in response.text
        cleared = owner.delete(f"/api/sessions/{session_id}/memory", headers=headers)
        assert cleared.status_code == 200
        after_clear = owner.get(f"/api/sessions/{session_id}/memory").json()["memory"]
        assert after_clear["status"] == "CLEARED"
        assert after_clear["summary_persisted"] is False
        assert after_clear["summary_content_exposed"] is False
        assert after_clear["estimated_tokens_before"] is None
    with TestClient(app) as other:
        assert login(other, "u_other", PASSWORD_B).status_code == 200
        assert other.get(f"/api/sessions/{session_id}/memory").status_code == 404


def test_run_execution_projection_is_owner_scoped_and_allowlisted(prepared_app):
    from app.m1.models import ObservabilityRecord, Trace, utc_now
    from app.m1.services import append_run_event, create_conversation, create_run

    app, _, factory = prepared_app
    with TestClient(app) as owner:
        assert login(owner, "u_demo", PASSWORD_A).status_code == 200
        with factory() as db:
            session = create_conversation(db, "u_demo", "execution projection")
            run, _ = create_run(db, "u_demo", session.session_id, "execution-projection-run", {"message": "x"})
            run.status = "SUCCEEDED"
            run.result_json = json.dumps({"strategy": "SINGLE", "sub_strategy": None,
                "route": {"strategy": "SINGLE", "decision_source": "RULE", "private_prompt": "hidden"},
                "answer": "Answer", "citations": [{"chunk_id": "chunk-1", "content": "Public policy clause",
                    "title": "Returns", "version": "v2", "internal_secret": "hidden"}],
                "evidence": [{"chunk_id": "chunk-1", "title": "Returns", "version": "v2"},
                             {"chunk_id": "chunk-2", "title": "Other", "version": "v1"}],
                "tool_execution_status": [{"capability": "search_knowledge", "status": "SUCCEEDED",
                    "executed": True, "duration_ms": 12.5, "error_code": None, "private_arguments": "hidden",
                    "retrieval_mode": "BM25", "evidence_ids": ["chunk-1"], "raw_query": "hidden"}],
                "tool_references": [{"tool": "search_knowledge", "source": "policy-index",
                    "result_ref": "result-1", "private_payload": "hidden"}],
                "retrieval": {"strategy_executed": "BM25", "retrieval_count": 2, "raw_prompt": "hidden"}})
            db.commit()
            db.add(ObservabilityRecord(record_id="span:exec-test", run_id=run.run_id,
                user_id="u_demo", trace_id="trace-exec", span_id="span-test", parent_span_id=None,
                graph_version="g1", record_type="span", timestamp=utc_now(),
                payload_json=json.dumps({"trace_id": "trace-exec", "span_id": "span-test",
                    "run_id": run.run_id, "node_name": "router", "strategy": "SINGLE",
                    "start_time": "2026-10-23T00:00:00Z", "end_time": "2026-10-23T00:00:00Z",
                    "duration_ms": 1.2, "status": "SUCCEEDED", "authorization": "hidden"})))
            db.add(Trace(trace_id="trace-retrieval", user_id="u_demo", run_id=run.run_id,
                event_type="policy_retrieval_completed", status="COMPLETED", created_at=utc_now(),
                event_json=json.dumps({"strategy_executed": "BM25", "knowledge_version": "k2",
                    "evidence_ids": ["chunk-1"], "full_query": "hidden"})))
            db.commit()
            run_id = run.run_id
            append_run_event(db, run_id, "trace-exec", "route_decided", {"strategy": "SINGLE"})
        response = owner.get(f"/api/runs/{run_id}/execution")
        assert response.status_code == 200
        projection = response.json()["execution"]
        assert projection["strategy"] == "SINGLE"
        assert projection["retrieval"]["strategy_executed"] == "BM25"
        assert projection["citations"][0]["chunk_id"] == "chunk-1"
        assert projection["retrieved_evidence"][1]["citation_status"] == "RETRIEVED_NOT_CITED"
        assert projection["spans"][0]["node_name"] == "router"
        assert projection["trace_events"][0]["data"]["evidence_ids"] == ["chunk-1"]
        assert projection["tool_calls"] == [{"capability": "search_knowledge", "status": "SUCCEEDED",
            "executed": True, "duration_ms": 12.5, "error_code": None,
            "retrieval_mode": "BM25", "evidence_ids": ["chunk-1"]}]
        assert projection["tool_references"] == [{"tool": "search_knowledge", "source": "policy-index",
            "result_ref": "result-1"}]
        assert "hidden" not in response.text
    with TestClient(app) as other:
        assert login(other, "u_other", PASSWORD_B).status_code == 200
        assert other.get(f"/api/runs/{run_id}/execution").status_code == 404


def test_secure_cookie_can_be_forced_by_runtime_configuration(prepared_app, monkeypatch):
    _, db_url, _ = prepared_app
    monkeypatch.setenv("SHOPRELAY_COOKIE_SECURE", "true")
    secure_app = create_app(database_url=db_url)
    try:
        with TestClient(secure_app, base_url="https://testserver") as client:
            result = login(client, "u_demo", PASSWORD_A)
            assert result.status_code == 200
            session_cookie = next(item for item in result.headers.get_list("set-cookie") if item.startswith(SESSION_COOKIE + "="))
            assert "secure" in session_cookie.lower()
    finally:
        secure_app.state.engine.dispose()


def test_invalid_and_oversized_inputs_use_structured_traceable_errors(prepared_app):
    app, _, _ = prepared_app
    with TestClient(app) as client:
        assert login(client, "u_demo", PASSWORD_A).status_code == 200
        headers = {"Origin": "http://testserver", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
        session_id = client.post("/api/sessions", headers=headers, json={}).json()["session"]["session_id"]
        invalid = client.post(f"/api/sessions/{session_id}/messages", headers=headers, json={"content": "x" * 10001})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "INVALID_ARGUMENT"
        assert invalid.json()["trace_id"]
        oversized = client.post(f"/api/sessions/{session_id}/runs", headers=headers,
                                json={"client_request_id": "oversize", "request_payload": {"data": "x" * 65537}})
        assert oversized.status_code == 422
        assert oversized.json()["code"] == "INVALID_ARGUMENT"


def test_two_user_session_message_run_and_order_isolation(prepared_app):
    app, _, _ = prepared_app
    client_a, client_b = TestClient(app), TestClient(app)
    assert login(client_a, "u_demo", PASSWORD_A).status_code == 200
    assert login(client_b, "u_other", PASSWORD_B).status_code == 200
    assert client_a.cookies.get(SESSION_COOKIE) != client_b.cookies.get(SESSION_COOKIE)
    csrf_a = client_a.cookies.get(CSRF_COOKIE)
    headers_a = {"Origin": "http://testserver", CSRF_HEADER: csrf_a}
    session_a = client_a.post("/api/sessions", headers=headers_a, json={"title": "A session", "user_id": "u_other"})
    session_b = client_b.post("/api/sessions", headers={"Origin": "http://testserver", CSRF_HEADER: client_b.cookies.get(CSRF_COOKIE)}, json={})
    assert session_a.status_code == session_b.status_code == 200
    sid_a, sid_b = session_a.json()["session"]["session_id"], session_b.json()["session"]["session_id"]
    for value in ("first", "second"):
        assert client_a.post(f"/api/sessions/{sid_a}/messages", headers=headers_a, json={"content": value, "user_id": "u_other"}).status_code == 200
    run_a = client_a.post(f"/api/sessions/{sid_a}/runs", headers=headers_a,
                          json={"client_request_id": "req-1", "request_payload": {"message": "question"}})
    assert run_a.status_code == 200
    run_id = run_a.json()["run"]["run_id"]
    assert run_a.json()["trace_id"]
    assert run_a.json()["run"]["status"] == "CREATED"
    assert client_a.post(f"/api/sessions/{sid_a}/runs", headers=headers_a,
                         json={"client_request_id": "req-1", "request_payload": {"message": "question"}}).json()["run"]["run_id"] == run_id
    conflict = client_a.post(f"/api/sessions/{sid_a}/runs", headers=headers_a,
                             json={"client_request_id": "req-1", "request_payload": {"message": "different"}})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert client_a.get(f"/api/runs/{run_id}").json()["run"]["events_cursor"] == 0
    assert client_b.get(f"/api/sessions/{sid_a}").status_code == 404
    assert client_a.get(f"/api/sessions/{sid_b}").status_code == 404
    assert client_b.get(f"/api/runs/{run_id}").status_code == 404
    order_a_before = client_a.get("/api/orders/ORD-PAID").json()["order"]
    order_b_before = client_b.get("/api/orders/ORD-OTHER").json()["order"]
    assert order_a_before["order_id"] == "ORD-PAID" and "user_id" not in order_a_before
    assert order_b_before["order_id"] == "ORD-OTHER" and "user_id" not in order_b_before
    assert client_a.get("/api/orders/ORD-OTHER").status_code == 404
    assert client_b.get("/api/orders/ORD-PAID").status_code == 404
    # A caller-supplied identity query parameter is ignored and cannot change the Cookie principal.
    forged = client_a.get("/api/orders/ORD-OTHER?user_id=u_other")
    assert forged.status_code == 404 and "u_other" not in forged.text
    assert TestClient(app).get("/api/orders/ORD-PAID").status_code == 401
    assert client_a.get("/api/orders/ORD-NOT-THERE").status_code == 404
    assert client_a.get("/api/orders/ORD-PAID").json()["order"] == order_a_before
    assert client_b.get("/api/orders/ORD-OTHER").json()["order"] == order_b_before
    content = client_a.get(f"/api/sessions/{sid_a}").json()["session"]["messages"]
    assert [item["content"] for item in content] == ["first", "second"]
    assert all(item["status"] == "RECORDED" for item in content)


def test_persistence_survives_application_restart(prepared_app):
    app, db_url, _ = prepared_app
    first = TestClient(app)
    assert login(first, "u_demo", PASSWORD_A).status_code == 200
    headers = {"Origin": "http://testserver", CSRF_HEADER: first.cookies.get(CSRF_COOKIE)}
    session_response = first.post("/api/sessions", headers=headers, json={})
    session_id = session_response.json()["session"]["session_id"]
    first.post(f"/api/sessions/{session_id}/messages", headers=headers, json={"content": "persist me"})
    run_response = first.post(f"/api/sessions/{session_id}/runs", headers=headers,
                              json={"client_request_id": "restart-1", "request_payload": {"q": 1}})
    run_id = run_response.json()["run"]["run_id"]
    app.state.engine.dispose()
    second_app = create_app(database_url=db_url)
    second = TestClient(second_app)
    second.cookies.update(first.cookies)
    assert second.get(f"/api/sessions/{session_id}").json()["session"]["messages"][0]["content"] == "persist me"
    assert second.get(f"/api/runs/{run_id}").json()["run"]["status"] == "CREATED"
    order_before = first.get("/api/orders/ORD-PAID").json()["order"]
    assert second.get("/api/orders/ORD-PAID").json()["order"] == order_before
    second_app.state.engine.dispose()


def test_agent_state_contract_roundtrips_and_restores_owned_run(prepared_app):
    from app.m1.state import AgentFact, AgentState, restore_agent_state

    app, _, factory = prepared_app

    class FixedProvider:
        def answer(self, question, evidence):
            return {"answer": "退货政策回答。", "citations": [evidence[0]["chunk_id"]],
                    "model_id": "contract-test", "thinking": "disabled",
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    "latency_ms": 1, "estimated_cost_usd": 0.0}

    app.state.chat_provider = FixedProvider()
    with TestClient(app) as client:
        assert login(client, "u_demo", PASSWORD_A).status_code == 200
        headers = {"Origin": "http://testserver", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
        response = client.post("/api/chat/stream", headers=headers,
                               json={"message": "质量问题如何申请退货？", "client_request_id": "agent-state-projection"})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert response.status_code == 200 and events[-1]["type"] == "done"
        run_id = events[0]["run_id"]

    with factory() as db:
        state = restore_agent_state(db, "u_demo", run_id)
        assert state is not None
        assert state.user_id == "u_demo" and state.run_id == run_id and state.turn_id == run_id
        assert state.run_status == "SUCCEEDED" and state.trace_id == events[-1]["trace_id"]
        assert state.original_query == "质量问题如何申请退货？"
        assert state.messages[-1].role == "assistant"
        assert state.final_answer == "退货政策回答。" and state.citations
        assert state.strategy == "SINGLE" and state.sub_strategy == "BM25"
        assert state.route_decision["strategy"] == "SINGLE"
        assert restore_agent_state(db, "u_other", run_id) is None
        encoded = state.model_dump_json()
        restored = AgentState.model_validate_json(encoded)
        assert restored == state
        assert state.safety_decision == "ALLOW" and state.completed_tasks == []
        assert not {"api_key", "database_session", "mcp_client"}.intersection(AgentState.model_fields)
        with pytest.raises(ValidationError):
            AgentState.model_validate({**state.model_dump(), "api_key": "must-not-enter-state"})

    fact = AgentFact(source_id="order:ORD-PAID", observed_at="2026-10-19T00:00:00Z",
                     version="1", value={"status": "PAID"})
    roundtrip = AgentState(trace_id="t", session_id="s", user_id="u", run_id="r", turn_id="r",
                           run_status="SUCCEEDED",
                           order_facts=[fact])
    assert AgentState.model_validate_json(roundtrip.model_dump_json()) == roundtrip


def test_migration_is_repeatable_preserves_m0_data_and_rolls_back_failures(monkeypatch):
    path = new_db_file()
    url = f"sqlite:///{path.as_posix()}"
    try:
        seed(path)
        with closing(sqlite3.connect(path)) as conn:
            users_before = conn.execute("SELECT count(*) FROM users").fetchone()[0]
            orders_before = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
            conn.execute("INSERT INTO sessions(session_id,user_id,created_at) VALUES ('legacy-session','u_demo','2026-10-18T00:00:00Z')")
            conn.execute("INSERT INTO messages(message_id,session_id,role,content,created_at) VALUES ('legacy-message','legacy-session','user','existing row','2026-10-18T00:00:01Z')")
            conn.execute("INSERT INTO traces(trace_id,user_id,event_json,created_at) VALUES ('legacy-trace','u_demo','{}','2026-10-18T00:00:02Z')")
            conn.commit()
        upgrade_database(url)
        upgrade_database(url)
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == users_before
            assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == orders_before
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == LATEST_SCHEMA_REVISION
            assert conn.execute("SELECT content FROM messages WHERE message_id='legacy-message'").fetchone()[0] == "existing row"
            assert conn.execute("SELECT event_json FROM traces WHERE trace_id='legacy-trace'").fetchone()[0] == "{}"
            assert {"auth_sessions", "demo_credentials", "runs"}.issubset(
                {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        legacy_engine = make_engine(url)
        try:
            with make_session_factory(legacy_engine)() as db:
                assert db.get(ConversationSession, "legacy-session").created_at.isoformat() == "2026-10-18T00:00:00+00:00"
                assert db.get(Message, "legacy-message").content == "existing row"
                assert db.get(Message, "legacy-message").status == "RECORDED"
                assert db.get(Trace, "legacy-trace").event_json == "{}"
                assert db.get(Trace, "legacy-trace").status == "RECORDED"
        finally:
            legacy_engine.dispose()
        empty = path.with_name(path.stem + "-empty.sqlite3")
        empty_url = f"sqlite:///{empty.as_posix()}"
        with pytest.raises(RuntimeError, match="M0 schema"):
            upgrade_database(empty_url)
        with closing(sqlite3.connect(empty)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not names.intersection({"auth_sessions", "demo_credentials", "runs"})
        remove_db(empty)

        broken = path.with_name(path.stem + "-broken.sqlite3")
        broken_url = f"sqlite:///{broken.as_posix()}"
        seed(broken)
        original_create_table = Operations.create_table
        def fail_before_auth_table(operations, table_name, *args, **kwargs):
            if table_name == "auth_sessions":
                raise RuntimeError("injected migration failure")
            return original_create_table(operations, table_name, *args, **kwargs)
        monkeypatch.setattr(Operations, "create_table", fail_before_auth_table)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            upgrade_database(broken_url)
        monkeypatch.setattr(Operations, "create_table", original_create_table)
        with closing(sqlite3.connect(broken)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not names.intersection({"demo_credentials", "auth_sessions", "runs"})
    finally:
        remove_db(path)


def test_migration_03_preserves_legacy_cross_session_request_ids():
    import hashlib
    import json
    from sqlalchemy import create_engine
    from alembic import command
    from app.m1.db import alembic_config
    path = new_db_file()
    url = f"sqlite:///{path.as_posix()}"
    try:
        seed(path)
        command.upgrade(alembic_config(url), "20261019_02")
        legacy_payload = {"legacy": "a"}
        legacy_hash = hashlib.sha256(json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO sessions(session_id,user_id,created_at) VALUES ('legacy-a','u_demo','2026-10-19T00:00:00Z'),('legacy-b','u_demo','2026-10-19T00:00:01Z')")
                connection.exec_driver_sql("INSERT INTO runs(run_id,user_id,session_id,client_request_id,request_hash,status,created_at,updated_at) VALUES ('legacy-run-a','u_demo','legacy-a','reused-key',:hash,'CREATED','2026-10-19T00:00:00Z','2026-10-19T00:00:00Z'),('legacy-run-b','u_demo','legacy-b','reused-key','b','CREATED','2026-10-19T00:00:01Z','2026-10-19T00:00:01Z')",
                                            {"hash": legacy_hash})
        finally:
            engine.dispose()
        upgrade_database(url)
        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute("SELECT run_id,idempotency_scope FROM runs WHERE client_request_id='reused-key' ORDER BY run_id").fetchall()
            assert len(rows) == 2 and rows[0][1].startswith("legacy:") and rows[1][1].startswith("legacy:")
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == LATEST_SCHEMA_REVISION
        from app.m1.db import make_engine, make_session_factory
        from app.m1.services import create_run
        migrated_engine = make_engine(url)
        try:
            with make_session_factory(migrated_engine)() as db:
                legacy_run, same = create_run(db, "u_demo", "legacy-a", "reused-key", legacy_payload)
                assert same and legacy_run.run_id == "legacy-run-a"
        finally:
            migrated_engine.dispose()
    finally:
        remove_db(path)


def test_legacy_readiness_does_not_require_m32_read_tables(tmp_path):
    path = tmp_path / "m32-readiness.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    try:
        assert readiness_state(engine) == ("ready", LATEST_SCHEMA_REVISION)
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE alembic_version SET version_num=?", (LEGACY_SCHEMA_REVISION,))
        assert readiness_state(engine) == ("ready", LEGACY_SCHEMA_REVISION)
        assert readiness_state(engine, required_revision=LATEST_SCHEMA_REVISION) == (
            "degraded", LEGACY_SCHEMA_REVISION)
    finally:
        engine.dispose()


def test_run_status_machine_and_model_unavailable_are_explicit(prepared_app):
    _, _, factory = prepared_app
    with factory() as db:
        from app.m1.services import create_conversation, create_run
        conversation = create_conversation(db, "u_demo", None)
        run, same_payload = create_run(db, "u_demo", conversation.session_id, "state-test", {"test": True})
        assert same_payload
        with pytest.raises(ValueError, match="illegal run transition"):
            transition_run(db, run, "SUCCEEDED")
        transition_run(db, run, "RUNNING")
        transition_run(db, run, "MODEL_UNAVAILABLE", error_code="MODEL_UNAVAILABLE")
        assert run.status == "MODEL_UNAVAILABLE"
        assert run.error_code == "MODEL_UNAVAILABLE"
        assert not hasattr(run, "answer")
        assert {item.status for item in db.query(Trace).filter(Trace.run_id == run.run_id).all()} == {"CREATED", "RUNNING", "MODEL_UNAVAILABLE"}
        with pytest.raises(ValueError, match="illegal run transition"):
            transition_run(db, run, "SUCCEEDED")


def test_sqlite_foreign_keys_are_enabled_for_api_connections(prepared_app):
    _, _, factory = prepared_app
    with factory() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        with pytest.raises(IntegrityError):
            db.execute(text(
                "INSERT INTO demo_credentials(user_id,password_hash,active,updated_at) VALUES ('missing-user','hash',1,'2026-10-19T00:00:00Z')"))
        db.rollback()


def test_health_ready_reports_degraded_and_unavailable_without_secrets(prepared_app, monkeypatch):
    app, _, _ = prepared_app
    with TestClient(app) as client:
        assert client.get("/health/ready").json()["status"] == "ready"
    path = new_db_file()
    no_migration_app = create_app(database_url=f"sqlite:///{path.as_posix()}")
    try:
        with TestClient(no_migration_app) as client:
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["status"] == "degraded"
        monkeypatch.setattr(no_migration_app.state.engine, "connect",
                            lambda: (_ for _ in ()).throw(OperationalError("select 1", {}, Exception("secret DSN"))))
        with TestClient(no_migration_app) as client:
            response = client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()["status"] == "unavailable"
            assert "secret DSN" not in response.text
    finally:
        no_migration_app.state.engine.dispose()
        remove_db(path)


class FakeChatProvider:
    def __init__(self, gate=None, failure=None):
        self.calls = 0
        self.gate = gate
        self.failure = failure

    def answer(self, question, evidence):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=4)
        if self.failure:
            raise self.failure
        assert evidence
        return {"answer": "模拟退货政策规定申请需要审核。", "citations": [evidence[0]["chunk_id"]],
                "model_id": "test-deepseek", "thinking": "disabled", "usage": {"prompt_tokens": 10, "completion_tokens": 8},
                "latency_ms": 3, "estimated_cost_usd": 0.00001}


def chat_request(client, message="质量问题如何申请退货？", request_id="chat-1", session_id=None):
    headers = {"Origin": "http://testserver", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
    return client.post("/api/chat", headers=headers,
                       json={"message": message, "client_request_id": request_id, "session_id": session_id})


def test_chat_persists_real_execution_contract_and_retries_idempotently(prepared_app):
    app, _, _ = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    with TestClient(app) as client:
        assert login(client, "u_demo", PASSWORD_A).status_code == 200
        first = chat_request(client)
        assert first.status_code == 200
        payload = first.json()
        assert payload["run_id"] and payload["session_id"] and payload["trace_id"]
        assert payload["strategy"] == "SINGLE" and payload["sub_strategy"] == "BM25"
        assert payload["route"]["decision_source"] == "RULE"
        assert len(payload["citations"]) == 1 and payload["citations"][0]["chunk_id"].startswith("policies/")
        assert provider.calls == 1
        second = chat_request(client)
        assert second.status_code == 200 and second.json()["run_id"] == payload["run_id"]
        assert provider.calls == 1
        conflict = chat_request(client, "换一个问题", "chat-1")
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        run = client.get(f"/api/runs/{payload['run_id']}").json()["run"]
        assert run["status"] == "SUCCEEDED" and run["answer"]["answer"] == payload["answer"]
        assert run["model_id"] == "test-deepseek" and run["usage"]["prompt_tokens"] == 10
        messages = client.get(f"/api/sessions/{payload['session_id']}").json()["session"]["messages"]
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert messages[1]["content"] == payload["answer"]
        with TestClient(app) as anonymous:
            assert anonymous.post("/api/chat", json={"message": "policy?", "client_request_id": "no-auth"}).status_code == 401


def test_direct_and_unsupported_routes_are_persisted_without_fake_execution(prepared_app):
    app, _, _ = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        direct = chat_request(client, "你好", "route-direct")
        assert direct.status_code == 200 and direct.json()["strategy"] == "DIRECT"
        assert provider.calls == 0 and direct.json()["citations"] == []
        agent = chat_request(client, "请查询订单 SR-ORD-1001 当前物流状态", "route-agent")
        assert agent.status_code == 502 and agent.json()["code"] == "ROUTE_EXECUTOR_UNAVAILABLE"
        run_id = agent.json()["details"]["run_id"]
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        assert run["status"] == "FAILED" and run["answer"] is None
        rows = client.get(f"/api/runs/{run_id}/events").json()["events"]
        route = next(row["payload"] for row in rows if row["type"] == "route_decided")
        assert route["strategy"] == "AGENT"
        assert not {"answer", "done"}.intersection(row["type"] for row in rows)
        assert provider.calls == 0


def test_provider_connection_fallback_diagnostic_is_persisted_without_claiming_model_direct(prepared_app):
    app, _, _ = prepared_app
    from app.m1.chat import ChatFailure

    safe_diagnostic = {"schema_version": "provider-failure-diagnostic-v1",
        "failure_phase": "CONNECTION_NO_RESPONSE", "error_category": "CONNECT_FAILURE",
        "completion_call_entered": True, "provider_response_received": False,
        "request_delivery": "OUTCOME_UNKNOWN", "exception_types": ["APIConnectionError", "ConnectError"],
        "errno": 10061, "timeout_stage": None, "http_status": None, "provider_error_code": None,
        "usage_status": "UNKNOWN", "billing_status": "UNKNOWN"}

    class RouteFailingProvider(FakeChatProvider):
        def classify_route(self, _question, **_kwargs):
            raise ChatFailure("ROUTE_CLASSIFICATION_FAILED", True,
                "路由模型暂不可用，已使用安全规则兜底。", {
                    "provider_exception": "APIConnectionError", "provider_diagnostic": safe_diagnostic})

    provider = RouteFailingProvider()
    app.state.chat_provider = provider
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        response = chat_request(client, "我有一个不确定的业务问题", "route-provider-fallback")
        assert response.status_code == 200
        payload = response.json()
        assert payload["strategy"] == "DIRECT"
        assert payload["route"]["decision_source"] == "FALLBACK"
        assert payload["route"]["reason_code"] == "LLM_CLASSIFICATION_FALLBACK"
        assert payload["route"]["provider_diagnostic"]["failure_phase"] == "CONNECTION_NO_RESPONSE"
        assert payload["route"]["provider_diagnostic"]["provider_response_received"] is False
        assert provider.calls == 0
        rows = client.get(f"/api/runs/{payload['run_id']}/events").json()["events"]
        route = next(row["payload"] for row in rows if row["type"] == "route_decided")
        assert route["strategy"] == "DIRECT"
        assert route["decision_source"] == "FALLBACK"
        assert route["provider_diagnostic"] == {key: value for key, value in safe_diagnostic.items()
            if value is not None}
        run = client.get(f"/api/runs/{payload['run_id']}").json()["run"]
        assert run["status"] == "SUCCEEDED"
        assert run["answer"]["answer_kind"] == "DIRECT"
        assert "score" not in run["answer"]


def test_chat_model_unavailable_is_persisted_without_assistant_answer(prepared_app):
    app, _, _ = prepared_app
    from app.m1.chat import ChatFailure
    app.state.chat_provider = FakeChatProvider(failure=ChatFailure("MODEL_UNAVAILABLE", True, "Unavailable"))
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        response = chat_request(client, request_id="chat-failure")
        assert response.status_code == 503 and response.json()["code"] == "MODEL_UNAVAILABLE"
        run_id = response.json()["details"]["run_id"]
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        assert run["status"] == "MODEL_UNAVAILABLE" and run["error"]["code"] == "MODEL_UNAVAILABLE"
        messages = client.get(f"/api/sessions/{run['session_id']}").json()["session"]["messages"]
        assert [row["role"] for row in messages] == ["user"]
        retry = chat_request(client, request_id="chat-failure")
        assert retry.status_code == 503 and app.state.chat_provider.calls == 1


def test_chat_parallel_same_key_claims_one_provider_call(prepared_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    app, _, _ = prepared_app
    gate = Event()
    provider = FakeChatProvider(gate=gate)
    app.state.chat_provider = provider
    client = TestClient(app)
    login(client, "u_demo", PASSWORD_A)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(chat_request, client, "质量问题如何申请退货？", "chat-race", None)
        import time
        deadline = time.time() + 2
        while provider.calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert provider.calls == 1
        second = chat_request(client, "质量问题如何申请退货？", "chat-race", None)
        assert second.status_code == 202 and second.json()["status"] == "RUNNING"
        gate.set()
        assert first.result(timeout=5).status_code == 200
    assert provider.calls == 1


def test_orphaned_running_chat_is_visible_after_app_recreation_and_not_replayed(prepared_app):
    from app.m1.services import claim_run, create_conversation, create_run
    app, _, factory = prepared_app
    with factory() as db:
        session = create_conversation(db, "u_demo", None)
        run, same = create_run(db, "u_demo", session.session_id, "interrupted-chat",
            {"message": "质量问题如何申请退货？", "requested_session_id": None},
            idempotency_scope="chat:auto")
        assert same and claim_run(db, run, execution_owner_id="live-worker")
        run_id = run.run_id
        session_id = session.session_id
    app.state.engine.dispose()
    recovered_provider = FakeChatProvider()
    restarted = create_app(database_url=prepared_app[1], chat_provider=recovered_provider)
    try:
        with TestClient(restarted) as client:
            login(client, "u_demo", PASSWORD_A)
            response = chat_request(client, "质量问题如何申请退货？", "interrupted-chat")
            assert response.status_code == 202 and response.json()["status"] == "RUNNING"
            assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "RUNNING"
            assert client.get(f"/api/sessions/{session_id}").status_code == 200
            assert recovered_provider.calls == 0
    finally:
        restarted.state.engine.dispose()


def test_chat_rejects_unretrieved_citation_as_failure(prepared_app):
    app, _, _ = prepared_app
    class InvalidCitationProvider(FakeChatProvider):
        def answer(self, question, evidence):
            self.calls += 1
            return {"answer": "不能支持的回答", "citations": ["fake/chunk"], "model_id": "test",
                    "thinking": "disabled", "usage": None, "latency_ms": 1, "estimated_cost_usd": None}
    app.state.chat_provider = InvalidCitationProvider()
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        response = chat_request(client, request_id="chat-bad-cite")
        assert response.status_code == 502 and response.json()["code"] == "MODEL_INVALID_RESPONSE"
        run_id = response.json()["details"]["run_id"]
        assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "FAILED"
        session_id = client.get(f"/api/runs/{run_id}").json()["run"]["session_id"]
        messages = client.get(f"/api/sessions/{session_id}").json()["session"]["messages"]
        assert [row["role"] for row in messages] == ["user"]


def test_chat_authentication_and_csrf_are_enforced(prepared_app):
    app, _, _ = prepared_app
    with TestClient(app) as client:
        assert client.post("/api/chat", json={"message": "policy?", "client_request_id": "no-auth"}).status_code == 401
        login(client, "u_demo", PASSWORD_A)
        response = client.post("/api/chat", headers={"Origin": "http://testserver", CSRF_HEADER: "bad"},
                               json={"message": "policy?", "client_request_id": "bad-csrf"})
        assert response.status_code == 403


def test_chat_run_isolated_between_users(prepared_app):
    app, _, _ = prepared_app
    app.state.chat_provider = FakeChatProvider()
    a, b = TestClient(app), TestClient(app)
    assert login(a, "u_demo", PASSWORD_A).status_code == 200
    assert login(b, "u_other", PASSWORD_B).status_code == 200
    session_id = a.post("/api/sessions", headers={"Origin": "http://testserver",
        CSRF_HEADER: a.cookies.get(CSRF_COOKIE)}, json={}).json()["session"]["session_id"]
    response = chat_request(a, request_id="owner-chat", session_id=session_id)
    assert response.status_code == 200
    body = response.json()
    assert b.get(f"/api/sessions/{session_id}").status_code == 404
    assert b.get(f"/api/runs/{body['run_id']}").status_code == 404
    other = chat_request(b, request_id="owner-chat")
    assert other.status_code == 200 and other.json()["run_id"] != body["run_id"]
    assert app.state.chat_provider.calls == 2


def test_chat_with_no_current_policy_evidence_abstains_without_model_call(prepared_app):
    import json
    app, _, _ = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    index_path = ROOT / ".tmp" / f"expired-index-{uuid.uuid4().hex}.json"
    index_path.write_text(json.dumps({"knowledge_version": "expired-test", "chunks": [{
        "chunk_id": "policy/old", "policy_id": "old", "version": "v1", "title": "过期政策",
        "clause_id": "x", "effective_from": "2020-01-01T00:00:00Z", "effective_to": "2021-01-01T00:00:00Z",
        "content": "2020年旧规则"}]}), encoding="utf-8")
    app.state.policy_index_path = index_path
    try:
        with TestClient(app) as client:
            login(client, "u_demo", PASSWORD_A)
            response = chat_request(client, "过期政策怎么办", "chat-expired")
            assert response.status_code == 200
            assert response.json()["citations"] == []
            assert "未能为所有必要政策方面找到可核验条款" in response.json()["answer"]
            assert provider.calls == 0
    finally:
        index_path.unlink(missing_ok=True)


def test_chat_missing_provider_key_persists_unavailable(prepared_app, monkeypatch):
    app, _, _ = prepared_app
    app.state.chat_provider = None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        response = chat_request(client, request_id="chat-no-key")
        assert response.status_code == 503 and response.json()["code"] == "MODEL_UNAVAILABLE"
        run = client.get(f"/api/runs/{response.json()['details']['run_id']}").json()["run"]
        assert run["status"] == "MODEL_UNAVAILABLE"


def test_chat_persistence_failure_never_returns_saved_success(prepared_app, monkeypatch):
    import app.m1.chat_execution as execution_module
    from sqlalchemy.exc import OperationalError
    app, _, _ = prepared_app
    app.state.chat_provider = FakeChatProvider()
    actual_finish = execution_module.finish_run
    injected = {"done": False}
    def fail_first_finish(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            raise OperationalError("commit", {}, RuntimeError("temporary injected failure"))
        return actual_finish(*args, **kwargs)
    monkeypatch.setattr(execution_module, "finish_run", fail_first_finish)
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        response = chat_request(client, request_id="chat-persist-failure")
        assert response.status_code == 503 and response.json()["code"] == "PERSISTENCE_FAILED"
        with app.state.session_factory() as db:
            from app.m1.models import Run
            run = db.query(Run).filter_by(client_request_id="chat-persist-failure").one()
            assert run.status == "FAILED" and run.error_code == "PERSISTENCE_FAILED"
            assert run.result_json is None
            assert db.query(Message).filter_by(session_id=run.session_id, role="assistant").count() == 0


def test_sse_stream_events_are_persisted_ordered_replayable_and_owner_scoped(prepared_app):
    app, _, factory = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    with TestClient(app) as client_a:
        assert login(client_a, "u_demo", PASSWORD_A).status_code == 200
        headers = {"Origin": "http://testserver", CSRF_HEADER: client_a.cookies.get(CSRF_COOKIE)}
        payload = {"message": "质量问题如何申请退货？", "client_request_id": "sse-success-1"}
        response = client_a.post("/api/chat/stream", headers=headers, json=payload)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        types = [row["type"] for row in rows]
        assert types == ["started", "route_decided", "agent_started", "retrieval_hop", "answer", "done"]
        assert rows[3]["payload"]["retrieval_mode"] == "bm25"
        assert rows[3]["payload"]["index_fingerprint"]
        assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
        assert len({row["event_id"] for row in rows}) == len(rows)
        run_id = rows[0]["run_id"]
        assert response.headers["x-run-id"] == run_id
        assert all(row["run_id"] == run_id and row["session_id"] and row["trace_id"] for row in rows)
        assert rows[-2]["payload"]["answer"] and rows[-1]["payload"]["status"] == "SUCCEEDED"
        conversation = client_a.get(f"/api/sessions/{rows[0]['session_id']}").json()["session"]
        assert conversation["runs"][-1]["run_id"] == run_id
        assert conversation["runs"][-1]["answer"]["answer"] == rows[-2]["payload"]["answer"]
        profile = client_a.get("/api/demo/me").json()["user"]
        assert profile["display_name"] and "user_id" not in profile
        assert provider.calls == 1
        replay = client_a.get(f"/api/runs/{run_id}/events")
        assert replay.status_code == 200
        replay_rows = replay.json()["events"]
        assert [row["event_id"] for row in replay_rows] == [row["event_id"] for row in rows]
        resumed = client_a.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": rows[1]["event_id"]})
        assert [row["seq"] for row in resumed.json()["events"]] == [3, 4, 5, 6]
        assert client_a.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "not-an-event"}).status_code == 400
        assert client_a.get(f"/api/runs/{run_id}/events?after_seq=99").status_code == 400
        assert client_a.post("/api/chat/stream", headers=headers, json=payload).status_code == 200
        conflict = client_a.post("/api/chat/stream", headers=headers,
                                 json={**payload, "message": "另一个问题"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        assert provider.calls == 1
        with TestClient(app) as client_b:
            assert login(client_b, "u_other", PASSWORD_B).status_code == 200
            assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404
        from app.m1.models import RunEvent
        with factory() as db:
            stored = db.query(RunEvent).filter_by(run_id=run_id).order_by(RunEvent.seq).all()
            assert [item.seq for item in stored] == list(range(1, len(rows) + 1))
            assert all(item.trace_id == rows[0]["trace_id"] for item in stored)
            assert all(item.timestamp.tzinfo is not None and item.timestamp.utcoffset().total_seconds() == 0 for item in stored)
            assert "api_key" not in " ".join(item.payload_json for item in stored).lower()
            from sqlalchemy import text
            for params in (
                {"event_id": "duplicate-seq", "run_id": run_id, "session_id": rows[0]["session_id"],
                 "trace_id": rows[0]["trace_id"], "seq": 1, "event_type": "error"},
                {"event_id": "invalid-event", "run_id": run_id, "session_id": rows[0]["session_id"],
                 "trace_id": rows[0]["trace_id"], "seq": 99, "event_type": "invented"},
            ):
                with pytest.raises(IntegrityError):
                    db.execute(text("INSERT INTO run_events(event_id,run_id,session_id,trace_id,seq,timestamp,event_type,payload_json) "
                                     "VALUES (:event_id,:run_id,:session_id,:trace_id,:seq,:timestamp,:event_type,'{}')"),
                               {**params, "timestamp": rows[0]["timestamp"]})
                    db.commit()
                db.rollback()


def test_single_chat_can_use_real_rrf_retrieval_service(prepared_app):
    from qdrant_client import QdrantClient
    from app.m2.retrieval import RetrievalConfig, RetrievalService
    app, _, _ = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    app.state.retrieval_service = RetrievalService(config=RetrievalConfig(mode="rrf"),
        qdrant_client=QdrantClient(":memory:"))
    with TestClient(app) as client:
        assert login(client, "u_demo", PASSWORD_A).status_code == 200
        response = chat_request(client, request_id="m2-rrf-chat")
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        persisted = client.get(f"/api/runs/{run_id}").json()["run"]["answer"]
        assert persisted["retrieval"]["mode"] == "rrf"
        events = client.get(f"/api/runs/{run_id}/events").json()["events"]
        retrieval = next(row for row in events if row["type"] == "retrieval_hop")
        assert retrieval["payload"]["retrieval_mode"] == "rrf"
        assert retrieval["payload"]["evidence_count"] > 0
        assert provider.calls == 1


def test_multi_chat_persists_bounded_policy_retrieval_state_replay_and_owner_scope(prepared_app):
    from app.m2.retrieval import RetrievalView

    app, _, factory = prepared_app
    evidence = [
        {"chunk_id": "test-policy-a", "title": "退货条件", "content": "签收后30天内的质量问题可提交整单退货申请，由人工审核。",
         "version": "policy-2026-01", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
         "knowledge_version": "test-knowledge", "index_fingerprint": "test-fingerprint", "rank": 1,
         "score": 1.0, "score_type": "bm25_raw"},
        {"chunk_id": "test-policy-b", "title": "退款条件", "content": "已签收订单需要已批准的退货申请才能申请退款。",
         "version": "policy-2026-01", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
         "knowledge_version": "test-knowledge", "index_fingerprint": "test-fingerprint", "rank": 2,
         "score": 0.9, "score_type": "bm25_raw"},
    ]

    class MultiRetrieval:
        config = type("Config", (), {"mode": "bm25", "version": "test-config", "config_hash": "config-hash"})()
        def pin_view(self, evaluation_at=None):
            return RetrievalView({"knowledge_version": "test-knowledge", "chunks": []},
                                "test-fingerprint", evaluation_at or "2026-10-19T00:00:00Z")
        def retrieve(self, query, *, mode=None, view=None):
            assert view.fingerprint == "test-fingerprint"
            rows = [row for row in evidence if ("退货" in query and row["chunk_id"] == "test-policy-a") or
                    ("退款" in query and row["chunk_id"] == "test-policy-b")]
            for row in rows:
                row["retrieval_method"] = mode
            channels = {"bm25": [row["chunk_id"] for row in rows]}
            if mode == "rrf_reranker":
                channels["vector"] = [row["chunk_id"] for row in rows]
            return {"knowledge_version": "test-knowledge", "index_fingerprint": view.fingerprint,
                    "mode": mode or "bm25", "evidence": rows, "channels": channels,
                    "models": {}, "diagnostics": {}, "latency_ms": 1, "degraded": False, "failures": []}

    class MultiProvider:
        def __init__(self):
            self.calls = {"extract": 0, "answer": 0}
            self.invalid_answer = False
        def extract_conditions(self, question):
            self.calls["extract"] += 1
            return {"conditions_result": {"request_mode": "POLICY_EXPLANATION", "analysis_version": "request_intent_v1",
                "requested_aspects": [
                    {"aspect_id": "return-window", "text": "质量退货时间窗口", "query_anchor": "质量退货"},
                    {"aspect_id": "refund-prerequisite", "text": "退款申请前置条件", "query_anchor": "退款前置条件"}],
                "conditions": []}, "model_id": "stub", "prompt_version": "request-intent-v1",
                "raw_content": "{}", "finish_reason": "stop",
                "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "latency_ms": 1}
        def answer_for_policy_aspects(self, question, rows, **kwargs):
            self.calls["answer"] += 1
            if self.invalid_answer:
                answer = "必须完成退货后才能申请退款。"
            else:
                answer = "质量退货须在签收后30天内提交并经人工审核；已签收订单须有获批退货申请后才能申请退款。"
            return {"answer": answer, "citations": [row["chunk_id"] for row in rows],
                    "model_id": "stub", "thinking": "disabled", "prompt_version": "answer-v1",
                    "finish_reason": "stop", "usage": {"prompt_tokens": 40, "completion_tokens": 25},
                    "latency_ms": 2, "estimated_cost_usd": .00003}

    provider = MultiProvider()
    app.state.chat_provider = provider
    app.state.retrieval_service = MultiRetrieval()
    client_a, client_b = TestClient(app), TestClient(app)
    assert login(client_a, "u_demo", PASSWORD_A).status_code == 200
    assert login(client_b, "u_other", PASSWORD_B).status_code == 200
    headers = {"Origin": "http://testserver", CSRF_HEADER: client_a.cookies.get(CSRF_COOKIE)}
    payload = {"message": "质量退货和退款前置条件如何衔接？", "client_request_id": "policy-multi-api-1"}
    with client_a:
        response = client_a.post("/api/chat/stream", headers=headers, json=payload)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert response.status_code == 200
        types = [event["type"] for event in events]
        assert "retrieval_hop" in types and types.index("retrieval_hop") < types.index("answer") < types.index("done")
        run_id = events[0]["run_id"]
        run = client_a.get(f"/api/runs/{run_id}").json()["run"]
        assert run["status"] == "SUCCEEDED"
        assert run["answer"]["answer_kind"] == "POLICY_ANSWER"
        assert run["answer"]["policy_retrieval"]["strategy_selected"] == "HYBRID_RERANK"
        assert run["answer"]["policy_retrieval"]["strategy_trigger_reason"] == "multiple_requested_policy_aspects"
        assert [item["chunk_id"] for item in run["answer"]["citations"]] == ["test-policy-a", "test-policy-b"]
        replay = client_a.post("/api/chat/stream", headers=headers, json=payload)
        assert replay.status_code == 200
        assert provider.calls == {"extract": 1, "answer": 1}
        provider.invalid_answer = True
        invalid_payload = {"message": payload["message"], "client_request_id": "policy-multi-invalid-answer"}
        invalid_response = client_a.post("/api/chat/stream", headers=headers, json=invalid_payload)
        invalid_events = [json.loads(line[6:]) for line in invalid_response.text.splitlines()
                          if line.startswith("data: ")]
        invalid_types = [event["type"] for event in invalid_events]
        assert invalid_types[-1] == "error"
        assert not {"answer", "done"}.intersection(invalid_types)
        invalid_run_id = invalid_events[0]["run_id"]
        invalid_run = client_a.get(f"/api/runs/{invalid_run_id}").json()["run"]
        assert invalid_run["status"] == "FAILED"
        assert invalid_run["error"]["code"] == "ANSWER_SEMANTIC_VALIDATION_FAILED"
        assert any(issue["code"] == "ANSWER_UNSUPPORTED_POLICY_PHASE"
                   for issue in invalid_run["error"]["details"]["semantic_issues"])
    assert client_b.get(f"/api/runs/{run_id}").status_code == 404
    assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404
    with factory() as db:
        from app.m1.state import restore_agent_state
        from app.m1.models import Trace
        state = restore_agent_state(db, "u_demo", run_id)
        assert state.evidence_sufficient is None and state.stop_reason == "POLICY_EVIDENCE_ANSWERED"
        assert len(state.policy_aspects) == 2 and len(state.retrieval_hops) == 2
        assert state.retrieval_strategy["strategy_selected"] == "HYBRID_RERANK"
        traces = db.query(Trace).filter(Trace.run_id == run_id).all()
        assert any(trace.event_type == "policy_retrieval_aspect" for trace in traces)
        assert any(trace.event_type == "policy_retrieval_completed" for trace in traces)
def test_sse_model_failure_persists_error_without_answer_or_done(prepared_app):
    from app.m1.chat import ChatFailure
    app, _, factory = prepared_app
    provider = FakeChatProvider(failure=ChatFailure("MODEL_UNAVAILABLE", True, "model unavailable"))
    app.state.chat_provider = provider
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        headers = {"Origin": "http://testserver", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
        response = client.post("/api/chat/stream", headers=headers,
                               json={"message": "质量问题如何申请退货？", "client_request_id": "sse-fail-1"})
        rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [row["type"] for row in rows][-1] == "error"
        assert not {"answer", "done"}.intersection(row["type"] for row in rows)
        run_id = rows[0]["run_id"]
        assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "MODEL_UNAVAILABLE"
        with factory() as db:
            assert db.query(Message).filter_by(session_id=rows[0]["session_id"], role="assistant").count() == 0


def test_expired_worker_lease_becomes_unknown_and_is_never_replayed(prepared_app):
    from datetime import timedelta
    from app.m1.models import utc_now
    from app.m1.services import claim_run, create_conversation, create_run
    app, _, factory = prepared_app
    provider = FakeChatProvider()
    app.state.chat_provider = provider
    question = "质量问题如何申请退货？"
    with factory() as db:
        session = create_conversation(db, "u_demo", None)
        run, same = create_run(db, "u_demo", session.session_id, "expired-lease-1",
            {"message": question, "requested_session_id": None}, idempotency_scope="chat:auto")
        assert same and claim_run(db, run, execution_owner_id="crashed-worker")
        run_id = run.run_id
        run.lease_expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    with TestClient(app) as client:
        login(client, "u_demo", PASSWORD_A)
        run_response = client.get(f"/api/runs/{run_id}")
        assert run_response.json()["run"]["status"] == "UNKNOWN_RESULT"
        assert run_response.json()["run"]["error"]["code"] == "UNKNOWN_RESULT"
        assert [item["type"] for item in client.get(f"/api/runs/{run_id}/events").json()["events"]] == ["error"]
        headers = {"Origin": "http://testserver", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
        retry = client.post("/api/chat/stream", headers=headers,
            json={"message": question, "client_request_id": "expired-lease-1"})
        assert retry.status_code == 200
        replay_rows = [json.loads(line[6:]) for line in retry.text.splitlines() if line.startswith("data: ")]
        assert replay_rows[-1]["type"] == "error" and replay_rows[-1]["payload"]["code"] == "UNKNOWN_RESULT"
        assert provider.calls == 0


def test_sse_network_disconnect_does_not_cancel_claimed_run(prepared_app):
    import socket
    import threading
    import time
    import httpx
    import uvicorn

    app, _, _ = prepared_app
    gate = threading.Event()
    provider = FakeChatProvider(gate=gate)
    app.state.chat_provider = provider
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="critical", access_log=False, lifespan="on"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            csrf = client.get("/api/demo/csrf").json()["csrf_token"]
            login_response = client.post("/api/demo/login", headers={"Origin": f"http://127.0.0.1:{port}",
                CSRF_HEADER: csrf}, json={"username": "u_demo", "password": PASSWORD_A})
            assert login_response.status_code == 200
            payload = {"message": "质量问题如何申请退货？", "client_request_id": "disconnect-run-1"}
            headers = {"Origin": f"http://127.0.0.1:{port}", CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}
            run_id = None
            with client.stream("POST", "/api/chat/stream", headers=headers, json=payload) as response:
                assert response.status_code == 200
                event_type = None
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        run_id = data["run_id"]
                        if event_type == "retrieval_hop":
                            call_deadline = time.monotonic() + 2
                            while provider.calls == 0 and time.monotonic() < call_deadline:
                                time.sleep(0.01)
                            break
                assert run_id and provider.calls == 1
            # Closing the HTTP response disconnects its subscriber, not the independent worker.
            assert client.get(f"/api/runs/{run_id}").json()["run"]["status"] == "RUNNING"
            gate.set()
            deadline = time.monotonic() + 5
            final = None
            while time.monotonic() < deadline:
                final = client.get(f"/api/runs/{run_id}").json()["run"]
                if final["status"] != "RUNNING":
                    break
                time.sleep(0.03)
            assert final["status"] == "SUCCEEDED" and final["answer"]["citations"]
            replay = client.get(f"/api/runs/{run_id}/events").json()["events"]
            assert [row["type"] for row in replay][-2:] == ["answer", "done"]
            duplicate = client.post("/api/chat/stream", headers=headers, json=payload)
            assert duplicate.status_code == 200
            assert provider.calls == 1
    finally:
        gate.set()
        server.should_exit = True
        server_thread.join(timeout=10)
        assert not server_thread.is_alive()
