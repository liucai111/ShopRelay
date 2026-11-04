"""M1.4 vertical API acceptance against only the dedicated PostgreSQL test DB."""
from __future__ import annotations

import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from fastapi.testclient import TestClient
from sqlalchemy import make_url, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.chat import ChatFailure
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.state import restore_agent_state
from app.m1.services import set_demo_password

DB_NAME = "shoprelay_m11_test"


class ProbeProvider:
    def __init__(self):
        self.calls = 0

    def answer(self, question, evidence):
        self.calls += 1
        if not evidence:
            raise ChatFailure("NO_EVIDENCE", False, "No policy evidence")
        return {"answer": "根据当前政策，可以提交质量退货申请，由人工审核。",
                "citations": [evidence[0]["chunk_id"]], "model_id": "m1.4-integration-stub",
                "thinking": "disabled", "usage": {"prompt_tokens": 8, "completion_tokens": 9},
                "latency_ms": 2, "estimated_cost_usd": None}


class NoReplayProvider:
    def answer(self, *_args, **_kwargs):
        raise AssertionError("persisted run was unexpectedly executed again")


def data_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def do_login(client, username: str, password: str):
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    headers = {"Origin": "http://testserver", CSRF_HEADER: csrf}
    response = client.post("/api/demo/login", headers=headers,
                           json={"username": username, "password": password})
    assert response.status_code == 200, f"demo login failed: {response.status_code}"
    return headers


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"record_type": "m1.4_postgresql_vertical_integration", "status": "FAIL",
              "created_at": datetime.now(timezone.utc).isoformat(), "database": DB_NAME,
              "database_version": None, "alembic_revision": None, "checks": {},
              "real_model_requests": 0, "secrets_recorded": False}
    settings = load_settings(require_api_key=False)
    if not settings.database_url or not settings.database_url.startswith("postgresql"):
        raise SystemExit("BLOCKED: configure PostgreSQL in the Git-ignored local configuration")
    database_url = make_url(settings.database_url).set(database=DB_NAME).render_as_string(hide_password=False)
    if make_url(database_url).database != DB_NAME:
        raise SystemExit("Refusing to run outside shoprelay_m11_test")
    engine = make_engine(database_url)
    restarted_app = None
    try:
        with engine.connect() as connection:
            report["database_version"] = connection.execute(text("SELECT version()" )).scalar_one().split(",", 1)[0]
            before = {
                "users": connection.execute(text("SELECT count(*) FROM users")).scalar_one(),
                "orders": connection.execute(text("SELECT count(*) FROM orders")).scalar_one(),
            }
            owned_orders = dict(connection.execute(text(
                "SELECT user_id, order_id FROM orders WHERE user_id IN ('u_demo','u_other')"
            )).all())
        revision = current_revision(database_url)
        if revision != "20261019_04":
            raise RuntimeError(f"expected current migration 20261019_04, got {revision}")
        if set(owned_orders) != {"u_demo", "u_other"}:
            raise RuntimeError("dedicated PG fixture must already contain one order per demo user")
        report["alembic_revision"] = revision
        report["checks"]["docker_postgresql_revision_and_seed_rows"] = "PASS"

        password_a, password_b = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        factory = make_session_factory(engine)
        with factory() as db:
            set_demo_password(db, "u_demo", password_a)
            set_demo_password(db, "u_other", password_b)
        provider = ProbeProvider()
        app = create_app(database_url=database_url, engine=engine, chat_provider=provider)
        client_a, client_b = TestClient(app), TestClient(app)
        headers_a = do_login(client_a, "u_demo", password_a)
        headers_b = do_login(client_b, "u_other", password_b)
        headers_a = {**headers_a, CSRF_HEADER: client_a.cookies.get("shoprelay_csrf")}
        order_a_id, order_b_id = owned_orders["u_demo"], owned_orders["u_other"]

        order_a_before = client_a.get(f"/api/orders/{order_a_id}").json()["order"]
        order_b_before = client_b.get(f"/api/orders/{order_b_id}").json()["order"]
        assert client_a.get(f"/api/orders/{order_b_id}").status_code == 404
        assert client_b.get(f"/api/orders/{order_a_id}").status_code == 404
        assert client_a.get(f"/api/orders/{order_b_id}?user_id=u_other").status_code == 404
        assert client_a.get("/api/orders/NO-SUCH-ORDER").status_code == 404
        assert client_a.get("/api/orders/" + order_a_id + "?user_id=u_other").json()["order"] == order_a_before
        assert client_b.get(f"/api/orders/{order_b_id}").json()["order"] == order_b_before
        assert "user_id" not in order_a_before and "user_id" not in order_b_before
        report["checks"]["order_owner_reads_and_no_leak_or_mutation"] = "PASS"
        report["checks"]["cross_owner_and_forged_user_id_denied"] = "PASS"
        unauthenticated = TestClient(app)
        try:
            report["checks"]["unauthenticated_order_denied"] = "PASS" if unauthenticated.get(
                f"/api/orders/{order_a_id}").status_code == 401 else "FAIL"
        finally:
            unauthenticated.close()

        # Create session and explicit user message, then run the persisted policy SSE path.
        created = client_a.post("/api/sessions", headers=headers_a, json={"title": "M1.4 integration"})
        assert created.status_code == 200
        session_id = created.json()["session"]["session_id"]
        msg = client_a.post(f"/api/sessions/{session_id}/messages", headers=headers_a,
                            json={"content": "我想了解质量问题退货政策。"})
        assert msg.status_code == 200
        request_id = "m1.4-integration-" + uuid.uuid4().hex
        response = client_a.post("/api/chat/stream", headers=headers_a,
            json={"session_id": session_id, "message": "签收后质量问题如何申请退货？",
                  "client_request_id": request_id, "user_id": "u_other"})
        assert response.status_code == 200
        events = data_events(response)
        assert [item["type"] for item in events] == ["started", "route_decided", "agent_started",
            "retrieval_hop", "answer", "done"]
        run_id = events[0]["run_id"]
        assert events[-1]["payload"]["status"] == "SUCCEEDED"
        assert provider.calls == 1

        order_response_after = client_a.get(f"/api/orders/{order_a_id}").json()["order"]
        assert order_response_after == order_a_before
        report["checks"]["session_message_policy_retrieval_answer_and_sse"] = "PASS"
        report["checks"]["answer_citation_and_terminal_state"] = "PASS"
        assert client_b.get(f"/api/sessions/{session_id}").status_code == 404
        assert client_b.get(f"/api/runs/{run_id}").status_code == 404
        assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404
        report["checks"]["cross_owner_session_run_event_denied"] = "PASS"
        with factory() as db:
            agent_state = restore_agent_state(db, "u_demo", run_id)
            assert agent_state is not None
            assert agent_state.run_status == "SUCCEEDED"
            assert agent_state.original_query == "签收后质量问题如何申请退货？"
            assert agent_state.final_answer == events[-2]["payload"]["answer"]
            assert agent_state.citations
            assert agent_state.model_validate_json(agent_state.model_dump_json()) == agent_state
            assert restore_agent_state(db, "u_other", run_id) is None
        report["checks"]["agent_state_projection_and_json_recovery"] = "PASS"
        conflict = client_a.post("/api/chat/stream", headers=headers_a,
            json={"session_id": session_id, "message": "不同问题", "client_request_id": request_id})
        assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
        replay = client_a.post("/api/chat/stream", headers=headers_a,
            json={"session_id": session_id, "message": "签收后质量问题如何申请退货？",
                  "client_request_id": request_id})
        assert [e["event_id"] for e in data_events(replay)] == [e["event_id"] for e in events]
        assert provider.calls == 1
        report["checks"]["idempotent_replay_and_conflict"] = "PASS"

        schema = client_a.get("/openapi.json").json()
        required_paths = {"/health/live", "/health/ready", "/api/demo/login", "/api/sessions",
                          "/api/sessions/{session_id}", "/api/sessions/{session_id}/messages",
                          "/api/sessions/{session_id}/runs", "/api/runs/{run_id}",
                          "/api/runs/{run_id}/events", "/api/orders/{order_id}",
                          "/api/chat", "/api/chat/stream"}
        assert required_paths.issubset(schema["paths"])
        report["checks"]["openapi_current_core_routes"] = "PASS"
        cookies_a = dict(client_a.cookies)
        cookies_b = dict(client_b.cookies)
        session_response = client_a.get(f"/api/sessions/{session_id}").json()["session"]
        run_response = client_a.get(f"/api/runs/{run_id}").json()["run"]
        retrieval = (run_response.get("answer") or {}).get("retrieval")
        retrieval_event = next(event for event in client_a.get(f"/api/runs/{run_id}/events").json()["events"]
                              if event["type"] == "retrieval_hop")
        expected_retrieval_mode = os.getenv("SHOPRELAY_RETRIEVAL_MODE", "bm25")
        assert retrieval and retrieval["mode"] == expected_retrieval_mode
        assert retrieval_event["payload"]["retrieval_mode"] == expected_retrieval_mode
        assert retrieval_event["payload"]["index_fingerprint"] == retrieval["index_fingerprint"]
        report["retrieval_integration"] = {"mode": retrieval["mode"], "config_version": retrieval["config_version"],
            "index_fingerprint": retrieval["index_fingerprint"], "evidence_count": retrieval_event["payload"]["evidence_count"],
            "degraded": retrieval["degraded"]}
        report["checks"]["configured_retrieval_mode_persisted_in_run_and_sse"] = "PASS"
        event_ids = [e["event_id"] for e in client_a.get(f"/api/runs/{run_id}/events").json()["events"]]
        client_a.close()
        client_b.close()
        app.state.run_executor.shutdown(wait=True, cancel_futures=False)
        app.state.engine.dispose()

        restarted_app = create_app(database_url=database_url, chat_provider=NoReplayProvider())
        with TestClient(restarted_app) as restarted_a:
            restarted_a.cookies.update(cookies_a)
            restored_session = restarted_a.get(f"/api/sessions/{session_id}").json()["session"]
            restored_run = restarted_a.get(f"/api/runs/{run_id}").json()["run"]
            restored_events = restarted_a.get(f"/api/runs/{run_id}/events").json()["events"]
            assert restored_session["messages"] == session_response["messages"]
            assert restored_run == run_response
            assert [e["event_id"] for e in restored_events] == event_ids
            restarted_b = TestClient(restarted_app)
            restarted_b.cookies.update(cookies_b)
            assert restarted_b.get(f"/api/runs/{run_id}").status_code == 404
            restarted_b.close()
            assert restarted_a.get(f"/api/orders/{order_a_id}").json()["order"] == order_a_before
        report["checks"]["restart_restores_messages_run_events_and_orders"] = "PASS"

        with engine.connect() as connection:
            after = {"users": connection.execute(text("SELECT count(*) FROM users")).scalar_one(),
                     "orders": connection.execute(text("SELECT count(*) FROM orders")).scalar_one()}
        assert before == after
        report["m0_user_order_rows_preserved"] = True
        report["checks"]["m0_seed_rows_unchanged"] = "PASS"
        report["provider_calls"] = provider.calls
        report["run_id"] = run_id
        report["session_id"] = session_id
        report["orders_tested"] = 2
        report["status"] = "PASS" if all(value == "PASS" for value in report["checks"].values()) else "FAIL"
    finally:
        if restarted_app is not None:
            restarted_app.state.engine.dispose()
        engine.dispose()
        path = ROOT / "docs" / "probes" / f"m1.4-postgres-integration-{stamp}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
