"""Run M1.1 Alembic and API checks in the persistent, project-only shoprelay_m11_test database."""
from __future__ import annotations

import json
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".vendor"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import IntegrityError

from app.m0.config import load_settings
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.services import set_demo_password

DB_NAME = "shoprelay_m11_test"


def main() -> None:
    settings = load_settings(require_api_key=False)
    if not settings.database_url or not settings.database_url.startswith("postgresql"):
        raise SystemExit("BLOCKED: configure the project PostgreSQL DATABASE_URL in ignored .env.m0.local")
    source_url = make_url(settings.database_url)
    admin_url = source_url.set(database="postgres")
    admin_engine = make_engine(admin_url.render_as_string(hide_password=False))
    try:
        with admin_engine.connect() as connection:
            exists = connection.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": DB_NAME}).scalar()
        if not exists:
            with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        admin_engine.dispose()

    database_url = source_url.set(database=DB_NAME).render_as_string(hide_password=False)
    engine = make_engine(database_url)
    report = {"status": "FAIL", "database": DB_NAME, "created_at": datetime.now(timezone.utc).isoformat(),
              "database_version": None, "migration_revision": None, "baseline_rows_preserved": False,
              "api": {}, "secrets_recorded": False}
    try:
        with engine.begin() as connection:
            version = connection.execute(text("SELECT version()" )).scalar_one()
            report["database_version"] = version.split(",", 1)[0]
            m0_baseline = [
                "CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id), created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS traces (trace_id TEXT PRIMARY KEY, user_id TEXT, event_json TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id), status TEXT NOT NULL, paid_at TEXT, delivered_at TEXT, paid_cents INTEGER NOT NULL, version INTEGER NOT NULL)",
            ]
            for ddl in m0_baseline:
                connection.exec_driver_sql(ddl)
            connection.execute(text("INSERT INTO users(user_id,display_name) VALUES ('u_demo','演示甲'),('u_other','演示乙') ON CONFLICT (user_id) DO NOTHING"))
            connection.execute(text("INSERT INTO orders(order_id,user_id,status,paid_at,delivered_at,paid_cents,version) VALUES ('ORD-M1-A','u_demo','PAID',NULL,NULL,1000,1),('ORD-M1-B','u_other','PAID',NULL,NULL,1000,1) ON CONFLICT (order_id) DO NOTHING"))
            before = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
            before_orders = connection.execute(text("SELECT count(*) FROM orders")).scalar_one()
        upgrade_database(database_url)
        upgrade_database(database_url)
        revision = current_revision(database_url)
        if revision != "20261019_03":
            raise RuntimeError(f"unexpected Alembic revision: {revision}")
        report["migration_revision"] = revision
        report["migration_repeat_upgrade"] = "PASS"
        factory = make_session_factory(engine)
        pw_a, pw_b = secrets.token_urlsafe(20), secrets.token_urlsafe(20)
        with factory() as db:
            set_demo_password(db, "u_demo", pw_a)
            set_demo_password(db, "u_other", pw_b)
        app = create_app(database_url=database_url, engine=engine)
        class ProbeChatProvider:
            def __init__(self):
                self.calls = 0
                self.lock = threading.Lock()
                self.hold_question = None
                self.entered = threading.Event()
                self.release = threading.Event()

            def answer(self, question, evidence):
                with self.lock:
                    self.calls += 1
                if question == self.hold_question:
                    self.entered.set()
                    if not self.release.wait(timeout=15):
                        raise TimeoutError("probe provider release timed out")
                return {"answer": "测试政策答复", "citations": [evidence[0]["chunk_id"]],
                        "model_id": "probe-provider", "thinking": "disabled", "usage": None,
                        "latency_ms": 1, "estimated_cost_usd": None}
        provider = ProbeChatProvider()
        app.state.chat_provider = provider
        client_a, client_b = TestClient(app), TestClient(app)
        try:
            def login(client, user, password):
                token = client.get("/api/demo/csrf").json()["csrf_token"]
                return client.post("/api/demo/login", headers={"Origin": "http://testserver", CSRF_HEADER: token},
                                   json={"username": user, "password": password})
            assert login(client_a, "u_demo", pw_a).status_code == 200
            assert login(client_b, "u_other", pw_b).status_code == 200
            assert client_a.get("/api/orders/ORD-M1-B").status_code == 404
            assert client_b.get("/api/orders/ORD-M1-B").status_code == 200
            csrf = client_a.cookies.get(CSRF_COOKIE)
            headers = {"Origin": "http://testserver", CSRF_HEADER: csrf}
            session = client_a.post("/api/sessions", headers=headers, json={}).json()["session"]
            client_a.post(f"/api/sessions/{session['session_id']}/messages", headers=headers, json={"content": "postgres persist"})
            run = client_a.post(f"/api/sessions/{session['session_id']}/runs", headers=headers,
                                json={"client_request_id": "pg-probe-1", "request_payload": {"probe": "m1.1"}}).json()["run"]
            assert client_b.get(f"/api/sessions/{session['session_id']}").status_code == 404
            assert client_a.post(f"/api/sessions/{session['session_id']}/runs", headers=headers,
                                 json={"client_request_id": "pg-probe-1", "request_payload": {"probe": "m1.1"}}).json()["run"]["run_id"] == run["run_id"]
            chat_headers = {"Origin": "http://testserver", CSRF_HEADER: client_a.cookies.get(CSRF_COOKIE)}
            chat = client_a.post("/api/chat", headers=chat_headers, json={
                "session_id": session["session_id"], "client_request_id": "pg-probe-chat-1",
                "message": "定制商品出现质量问题如何申请退货？"})
            assert chat.status_code == 200 and chat.json()["citations"]
            chat_run_id = chat.json()["run_id"]
            assert client_b.get(f"/api/runs/{chat_run_id}").status_code == 404
            assert client_a.get(f"/api/runs/{chat_run_id}").json()["run"]["status"] == "SUCCEEDED"
            report["api"]["chat_run_terminal_and_cited"] = "PASS"
            with engine.begin() as connection:
                nested = connection.begin_nested()
                try:
                    connection.execute(text("INSERT INTO runs(run_id,user_id,session_id,client_request_id,idempotency_scope,request_hash,status,created_at,updated_at) VALUES (:run,'u_demo',:session,'pg-probe-chat-1',:scope,:hash,'CREATED',now(),now())"),
                                       {"run": secrets.token_hex(16), "session": session["session_id"],
                                        "scope": f"chat:{session['session_id']}", "hash": "0" * 64})
                    raise AssertionError("duplicate PostgreSQL chat idempotency key unexpectedly inserted")
                except IntegrityError:
                    nested.rollback()
                else:
                    nested.commit()
            report["api"]["chat_database_unique_constraint"] = "PASS"

            # Exercise the actual PostgreSQL CAS claim through two concurrent API requests.
            provider.hold_question = "质量问题如何申请退货？"
            provider.entered.clear()
            provider.release.clear()
            provider_calls_before = provider.calls
            concurrent_payload = {"session_id": session["session_id"],
                                  "client_request_id": "pg-probe-concurrent-chat-1",
                                  "message": provider.hold_question}
            concurrent_headers = {"Origin": "http://testserver", CSRF_HEADER: csrf}
            with TestClient(app) as client_c, TestClient(app) as client_d:
                client_c.cookies.update(client_a.cookies)
                client_d.cookies.update(client_a.cookies)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(client_c.post, "/api/chat", headers=concurrent_headers,
                                        json=concurrent_payload)
                    assert provider.entered.wait(timeout=10), "first PostgreSQL chat never reached provider"
                    second = pool.submit(client_d.post, "/api/chat", headers=concurrent_headers,
                                         json=concurrent_payload)
                    second_response = second.result(timeout=10)
                    provider.release.set()
                    first_response = first.result(timeout=20)
                assert first_response.status_code == 200
                assert second_response.status_code == 202
                assert first_response.json()["run_id"] == second_response.json()["run_id"]
            assert provider.calls - provider_calls_before == 1
            provider.hold_question = None
            report["api"]["postgres_concurrent_idempotency_and_atomic_claim"] = "PASS"

            with engine.begin() as connection:
                nested = connection.begin_nested()
                try:
                    connection.execute(text("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES (:hash,'missing-user',now(),now())"), {"hash": "0" * 64})
                    raise AssertionError("orphan auth session unexpectedly inserted")
                except IntegrityError:
                    nested.rollback()
                else:
                    nested.commit()
            report["api"].update({"user_a_login": "PASS", "user_b_login": "PASS", "cross_user_order": "PASS",
                                  "cross_user_session": "PASS", "run_idempotency": "PASS", "foreign_key_constraint": "PASS",
                                  "run_status": run["status"]})
            restarted_app = create_app(database_url=database_url)
            try:
                with TestClient(restarted_app) as restarted:
                    restarted.cookies.update(client_a.cookies)
                    persisted = restarted.get(f"/api/sessions/{session['session_id']}")
                    persisted_run = restarted.get(f"/api/runs/{run['run_id']}")
                    assert persisted.status_code == persisted_run.status_code == 200
                    assert persisted.json()["session"]["messages"][0]["content"] == "postgres persist"
                    assert persisted.json()["session"]["messages"][0]["status"] == "RECORDED"
                    assert persisted_run.json()["run"]["status"] == "CREATED"
                    persisted_chat_run = restarted.get(f"/api/runs/{chat_run_id}")
                    assert persisted_chat_run.status_code == 200
                    assert persisted_chat_run.json()["run"]["status"] == "SUCCEEDED"
                    assert persisted_chat_run.json()["run"]["answer"]["citations"]
                report["api"]["restart_persistence"] = "PASS"
            finally:
                restarted_app.state.engine.dispose()
            with factory() as db:
                from sqlalchemy import select
                from app.m1.models import Message, Trace
                message = db.execute(select(Message).where(
                    Message.session_id == session["session_id"], Message.role == "user",
                    Message.content == "postgres persist",
                )).scalar_one()
                trace = db.execute(select(Trace).where(Trace.run_id == run["run_id"])).scalar_one()
                assert message.status == "RECORDED" and trace.status == "CREATED"
                report["api"]["message_trace_status"] = "PASS"
            with engine.connect() as connection:
                after = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
                after_orders = connection.execute(text("SELECT count(*) FROM orders")).scalar_one()
            report["baseline_rows_preserved"] = before == after and before_orders == after_orders
            # PostgreSQL DDL is transactional; prove rollback leaves no probe object behind.
            rollback_table = "m1_probe_rollback_" + secrets.token_hex(6)
            with engine.begin() as connection:
                nested = connection.begin_nested()
                connection.exec_driver_sql(f'CREATE TABLE "{rollback_table}" (id integer)')
                nested.rollback()
                assert connection.execute(text("SELECT to_regclass(:name)"),
                                          {"name": f"public.{rollback_table}"}).scalar_one_or_none() is None
            report["postgres_transactional_ddl_rollback"] = "PASS"
            report["status"] = "PASS" if report["baseline_rows_preserved"] else "FAIL"
        finally:
            client_a.close()
            client_b.close()
        report_path = ROOT / "docs" / "probes" / f"m1.2-postgres-migration-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        report["migration_revision"] = revision
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
