"""One real DeepSeek SSE smoke test on a disposable local SQLite database."""
from __future__ import annotations

import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from fastapi.testclient import TestClient

from app.m0.config import load_settings
from app.m0.data import seed
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.services import set_demo_password


def parse_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"record_type": "m1.3_deepseek_sse_smoke", "status": "BLOCKED",
              "created_at": datetime.now(timezone.utc).isoformat(), "model": None,
              "api_requests": 0, "secrets_recorded": False}
    settings = load_settings(require_api_key=False)
    report["model"] = settings.model
    if not settings.api_key.strip():
        report["reason"] = "DeepSeek API key is not configured in process environment or ignored local config"
        _write_report(report, stamp)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    db_path = ROOT / ".tmp" / f"m1.3-deepseek-{secrets.token_hex(8)}.sqlite3"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = None
    try:
        seed(db_path)
        upgrade_database(db_url)
        engine = make_engine(db_url)
        password = secrets.token_urlsafe(24)
        with make_session_factory(engine)() as db:
            set_demo_password(db, "u_demo", password)
        app = create_app(database_url=db_url, engine=engine)
        question = "定制商品出现质量问题如何申请退货？"
        request_id = f"m1.3-deepseek-{secrets.token_hex(10)}"
        with TestClient(app) as client:
            csrf = client.get("/api/demo/csrf").json()["csrf_token"]
            origin = "http://testserver"
            headers = {"Origin": origin, CSRF_HEADER: csrf}
            login = client.post("/api/demo/login", headers=headers,
                json={"username": "u_demo", "password": password})
            if login.status_code != 200:
                raise RuntimeError(f"demo login failed: {login.status_code}")
            report["api_requests"] = 1
            response = client.post("/api/chat/stream", headers=headers,
                json={"message": question, "client_request_id": request_id})
            if response.status_code != 200:
                raise RuntimeError(f"SSE request failed: {response.status_code}")
            events = parse_events(response)
            event_types = [item["type"] for item in events]
            report["event_types"] = event_types
            report["sse_events"] = events
            if events:
                run_id = events[0]["run_id"]
                report["run_id"] = run_id
                report["session_id"] = events[0]["session_id"]
                report["trace_id"] = events[0]["trace_id"]
                run = client.get(f"/api/runs/{run_id}").json()["run"]
                report["run_status"] = run["status"]
                if run.get("error"):
                    report["run_error"] = run["error"]
            if events and events[-1]["type"] == "error":
                report["error_event"] = events[-1]["payload"]
                raise RuntimeError("real model returned a persisted error event")
            if event_types != ["started", "route_decided", "agent_started", "retrieval_hop", "answer", "done"]:
                raise RuntimeError(f"unexpected SSE event types: {event_types}")
            assert events[3]["payload"]["evidence_count"] > 0
            answer_payload = events[-2]["payload"]
            assert answer_payload["model_id"] == settings.model and answer_payload["citations"]
            run_id, session_id = events[0]["run_id"], events[0]["session_id"]
            run = client.get(f"/api/runs/{run_id}").json()["run"]
            assert run["status"] == "SUCCEEDED" and run["answer"]["citations"]
            replay = client.get(f"/api/runs/{run_id}/events").json()["events"]
            assert [item["event_id"] for item in replay] == [item["event_id"] for item in events]
            duplicate = client.post("/api/chat/stream", headers=headers,
                json={"message": question, "client_request_id": request_id})
            assert [item["event_id"] for item in parse_events(duplicate)] == [item["event_id"] for item in events]
            cookies = dict(client.cookies)

        app.state.engine.dispose()
        class NoReplayProvider:
            def answer(self, *_args, **_kwargs):
                raise AssertionError("completed request was unexpectedly re-executed after app restart")
        restarted = create_app(database_url=db_url, chat_provider=NoReplayProvider())
        with TestClient(restarted) as client:
            client.cookies.update(cookies)
            restored = client.get(f"/api/runs/{run_id}").json()["run"]
            restored_events = client.get(f"/api/runs/{run_id}/events").json()["events"]
            assert restored["status"] == "SUCCEEDED" and restored["answer"] == run["answer"]
            assert [item["event_id"] for item in restored_events] == [item["event_id"] for item in events]
        restarted.state.engine.dispose()

        report.update({"status": "PASS", "api_requests": 1, "run_id": run_id,
            "session_id": session_id, "event_types": event_types,
            "event_ids": [item["event_id"] for item in events],
            "trace_id": events[0]["trace_id"], "evidence_count": events[3]["payload"]["evidence_count"],
            "answer": answer_payload["answer"], "citations": answer_payload["citations"],
            "usage": run["usage"], "latency_ms": run["latency_ms"],
            "estimated_cost_usd": answer_payload["estimated_cost_usd"],
            "idempotent_replay_extra_requests": 0, "restart_replay_extra_requests": 0,
            "run_status": restored["status"]})
    except Exception as exc:
        report["status"] = "FAIL"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        if engine is not None:
            engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(db_path) + suffix)
            if path.exists():
                path.unlink()
        _write_report(report, stamp)
        print(json.dumps(report, ensure_ascii=False, indent=2))


def _write_report(report: dict, stamp: str) -> None:
    path = ROOT / "docs" / "probes" / f"m1.3-deepseek-sse-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
