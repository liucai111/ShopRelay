"""Exercise the M1.3 event schema and resumable chat flow only in shoprelay_m11_test."""
from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.exc import IntegrityError

from app.m0.config import load_settings
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.chat import ChatFailure
from app.m1.services import set_demo_password

DB_NAME = "shoprelay_m11_test"


class ProbeProvider:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()
        self.hold_question: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def answer(self, question, evidence):
        with self.lock:
            self.calls += 1
        if question == self.hold_question:
            self.entered.set()
            if not self.release.wait(15):
                raise TimeoutError("probe provider release timed out")
        if not evidence:
            raise ChatFailure("NO_EVIDENCE", False, "No evidence")
        return {"answer": "PostgreSQL SSE 探针答复。", "citations": [evidence[0]["chunk_id"]],
                "model_id": "m1.3-postgres-probe", "thinking": "disabled",
                "usage": {"prompt_tokens": 10, "completion_tokens": 6},
                "latency_ms": 4, "estimated_cost_usd": None}


def event_rows(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def main() -> None:
    settings = load_settings(require_api_key=False)
    if not settings.database_url or not settings.database_url.startswith("postgresql"):
        raise SystemExit("BLOCKED: configure the ShopRelay PostgreSQL password in ignored local configuration")
    test_url = make_url(settings.database_url).set(database=DB_NAME).render_as_string(hide_password=False)
    engine = make_engine(test_url)
    report = {"record_type": "m1.3_postgresql_sse_probe", "status": "FAIL", "database": DB_NAME,
              "created_at": datetime.now(timezone.utc).isoformat(), "database_version": None,
              "migration_revision": None, "checks": {}, "secrets_recorded": False}
    try:
        probe_id = secrets.token_hex(4)
        with engine.connect() as connection:
            report["database_version"] = connection.execute(text("SELECT version()")).scalar_one().split(",", 1)[0]
            baseline_before = {
                "users": connection.execute(text("SELECT count(*) FROM users")).scalar_one(),
                "orders": connection.execute(text("SELECT count(*) FROM orders")).scalar_one(),
            }
        upgrade_database(test_url)
        upgrade_database(test_url)
        revision = current_revision(test_url)
        if revision != "20261019_04":
            raise RuntimeError(f"unexpected revision: {revision}")
        report["migration_revision"] = revision
        report["checks"]["migration_repeat_upgrade"] = "PASS"
        factory = make_session_factory(engine)
        pass_a, pass_b = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        with factory() as db:
            set_demo_password(db, "u_demo", pass_a)
            set_demo_password(db, "u_other", pass_b)
        provider = ProbeProvider()
        app = create_app(database_url=test_url, engine=engine, chat_provider=provider)
        with TestClient(app) as client_a:
            csrf = client_a.get("/api/demo/csrf").json()["csrf_token"]
            origin = "http://testserver"
            headers = {"Origin": origin, CSRF_HEADER: csrf}
            login_a = client_a.post("/api/demo/login", headers=headers,
                                    json={"username": "u_demo", "password": pass_a})
            assert login_a.status_code == 200
            question = "定制商品出现质量问题如何申请退货？"
            payload = {"message": question, "client_request_id": f"pg-m13-stream-success-{probe_id}"}
            first = client_a.post("/api/chat/stream", headers=headers, json=payload)
            rows = event_rows(first)
            expected_types = ["started", "route_decided", "agent_started", "retrieval_hop", "answer", "done"]
            observed_types = [row["type"] for row in rows]
            if first.status_code != 200 or observed_types != expected_types:
                raise AssertionError(f"stream status={first.status_code}; event_types={observed_types}")
            run_id, session_id = rows[0]["run_id"], rows[0]["session_id"]
            assert [row["seq"] for row in rows] == list(range(1, 7))
            assert rows[3]["payload"]["evidence_count"] > 0
            assert provider.calls == 1
            replay = client_a.get(f"/api/runs/{run_id}/events")
            assert [row["event_id"] for row in replay.json()["events"]] == [row["event_id"] for row in rows]
            report["checks"]["event_order_replay_and_run_persistence"] = "PASS"

            # Run two simultaneous subscribers against one not-yet-finished provider call.
            provider.hold_question = "定制商品出现质量问题如何申请退货？"
            provider.entered.clear()
            provider.release.clear()
            concurrent_payload = {"session_id": session_id, "message": provider.hold_question,
                                  "client_request_id": f"pg-m13-stream-concurrent-{probe_id}"}
            client_b = TestClient(app)
            client_c = TestClient(app)
            client_b.cookies.update(client_a.cookies)
            client_c.cookies.update(client_a.cookies)
            concurrent_headers = {"Origin": origin, CSRF_HEADER: client_a.cookies.get(CSRF_COOKIE)}
            with ThreadPoolExecutor(max_workers=2) as pool:
                left = pool.submit(client_b.post, "/api/chat/stream", headers=concurrent_headers,
                                   json=concurrent_payload)
                assert provider.entered.wait(10), "PostgreSQL SSE worker did not reach provider"
                right = pool.submit(client_c.post, "/api/chat/stream", headers=concurrent_headers,
                                    json=concurrent_payload)
                time.sleep(0.15)
                assert provider.calls == 2
                provider.release.set()
                left_response, right_response = left.result(timeout=20), right.result(timeout=20)
            left_rows, right_rows = event_rows(left_response), event_rows(right_response)
            assert left_response.status_code == right_response.status_code == 200
            assert left_rows[0]["run_id"] == right_rows[0]["run_id"]
            assert provider.calls == 2
            report["checks"]["concurrent_subscribers_single_model_execution"] = "PASS"
            client_b.close()
            client_c.close()

            client_other = TestClient(app)
            csrf_other = client_other.get("/api/demo/csrf").json()["csrf_token"]
            assert client_other.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf_other},
                json={"username": "u_other", "password": pass_b}).status_code == 200
            assert client_other.get(f"/api/runs/{run_id}/events").status_code == 404
            client_other.close()
            report["checks"]["cross_user_event_access_denied"] = "PASS"

            conflict = client_a.post("/api/chat/stream", headers=headers,
                json={**payload, "message": "另一个问题"})
            assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
            report["checks"]["idempotency_conflict"] = "PASS"
            cookies = dict(client_a.cookies)

        app.state.engine.dispose()
        restarted_provider = ProbeProvider()
        restarted = create_app(database_url=test_url, chat_provider=restarted_provider)
        with TestClient(restarted) as client:
            client.cookies.update(cookies)
            persisted = client.get(f"/api/runs/{run_id}").json()["run"]
            persisted_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
            assert persisted["status"] == "SUCCEEDED" and persisted["answer"]["citations"]
            assert [item["event_id"] for item in persisted_events] == [row["event_id"] for row in rows]
            assert restarted_provider.calls == 0
            report["checks"]["application_restart_and_no_replay"] = "PASS"
        restarted.state.engine.dispose()

        with engine.begin() as connection:
            baseline_after = {
                "users": connection.execute(text("SELECT count(*) FROM users")).scalar_one(),
                "orders": connection.execute(text("SELECT count(*) FROM orders")).scalar_one(),
            }
        assert baseline_before == baseline_after
        report["checks"]["m0_baseline_rows_preserved"] = "PASS"

        with engine.begin() as connection:
            event = connection.execute(text(
                "SELECT run_id,session_id,trace_id,seq FROM run_events WHERE run_id=:run ORDER BY seq LIMIT 1"),
                {"run": run_id}).mappings().one()
            nested = connection.begin_nested()
            try:
                connection.execute(text("INSERT INTO run_events(event_id,run_id,session_id,trace_id,seq,timestamp,event_type,payload_json) "
                    "VALUES (:id,:run,:session,:trace,:seq,:timestamp,'error','{}')"),
                    {"id": secrets.token_hex(16), "run": event["run_id"], "session": event["session_id"],
                     "trace": event["trace_id"], "seq": event["seq"], "timestamp": datetime.now(timezone.utc)})
            except IntegrityError:
                nested.rollback()
            else:
                nested.commit()
                raise AssertionError("duplicate run event sequence was accepted")
            nested = connection.begin_nested()
            try:
                connection.execute(text("INSERT INTO run_events(event_id,run_id,session_id,trace_id,seq,timestamp,event_type,payload_json) "
                    "VALUES (:id,:run,:session,:trace,:seq,:timestamp,'invented','{}')"),
                    {"id": secrets.token_hex(16), "run": event["run_id"], "session": event["session_id"],
                     "trace": event["trace_id"], "seq": event["seq"] + 100, "timestamp": datetime.now(timezone.utc)})
            except IntegrityError:
                nested.rollback()
            else:
                nested.commit()
                raise AssertionError("unsupported run event type was accepted")
        report["checks"]["unique_seq_and_event_type_constraints"] = "PASS"
        report["status"] = "PASS"
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        engine.dispose()
        report_path = ROOT / "docs" / "probes" / f"m1.3-postgres-sse-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
