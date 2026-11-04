"""One real DeepSeek chat smoke call against an isolated temporary SQLite database.

This script never prints or records credentials and never retries a provider call.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.m0.config import load_settings
from app.m0.data import ROOT, seed
from app.m1.api import CSRF_COOKIE, CSRF_HEADER, create_app
from app.m1.chat import DeepSeekProvider, PRICE_SOURCE
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.services import set_demo_password


class CountingProvider:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0

    def answer(self, question, evidence):
        self.calls += 1
        return self.provider.answer(question, evidence)


def main() -> None:
    started = datetime.now(timezone.utc)
    report = {"record_type": "m1.2_real_deepseek_chat_smoke", "started_at_utc": started.isoformat(),
              "status": "NOT_RUN", "request_count_max": 1, "provider_retries": 0,
              "price_source": PRICE_SOURCE,
              "price_basis": "conservative official peak rates; estimate from provider usage, not invoice",
              "secrets_recorded": False}
    path = ROOT / ".tmp" / f"m1.2-real-{uuid.uuid4().hex}.sqlite3"
    url = f"sqlite:///{path.as_posix()}"
    engine = None
    app = None
    try:
        settings = load_settings(require_api_key=True)
        report["model_configured"] = settings.model
        seed(path)
        upgrade_database(url)
        engine = make_engine(url)
        factory = make_session_factory(engine)
        # Demo password is generated here and retained only in process memory.
        password = "m1-probe-" + secrets.token_urlsafe(24)
        with factory() as db:
            set_demo_password(db, "u_demo", password)
        provider = CountingProvider(DeepSeekProvider(settings=settings))
        app = create_app(database_url=url, engine=engine, chat_provider=provider)
        client = TestClient(app)
        try:
            csrf = client.get("/api/demo/csrf").json()["csrf_token"]
            login = client.post("/api/demo/login", headers={"Origin": "http://testserver", CSRF_HEADER: csrf},
                                json={"username": "u_demo", "password": password})
            if login.status_code != 200:
                raise RuntimeError("isolated demo login failed")
            request_payload = {"message": "模拟政策规定，商品签收后出现质量问题，应如何申请退货？",
                               "client_request_id": "m1.2-live-single-call"}
            response = client.post("/api/chat", headers={"Origin": "http://testserver",
                CSRF_HEADER: client.cookies.get(CSRF_COOKIE)}, json=request_payload)
            report["api_status_code"] = response.status_code
            report["provider_calls"] = provider.calls
            if response.status_code != 200:
                report["status"] = "FAIL"
                report["failure"] = response.json().get("code", "HTTP_ERROR")
                report["response"] = response.json()
                detail = response.json().get("details") or {}
                if detail.get("run_id"):
                    failed_run = client.get(f"/api/runs/{detail['run_id']}")
                    report["run_status"] = failed_run.json()["run"]["status"]
                    report["persisted_error"] = failed_run.json()["run"].get("error")
                return
            body = response.json()
            report["question"] = request_payload["message"]
            report["answer"] = body["answer"]
            report["citations"] = body["citations"]
            report["strategy"] = body["strategy"]
            report["sub_strategy"] = body["sub_strategy"]
            report["run_id"] = body["run_id"]
            report["session_id"] = body["session_id"]
            if not body["citations"] or provider.calls != 1:
                raise RuntimeError("success lacked real citations or made an unexpected provider call count")
            run = client.get(f"/api/runs/{body['run_id']}").json()["run"]
            report["run_status"] = run["status"]
            report["actual_model_id"] = run["model_id"]
            report["usage"] = run["usage"]
            report["latency_ms"] = run["latency_ms"]
            report["estimated_cost_usd"] = (run["answer"] or {}).get("estimated_cost_usd")
            report["first_call_persisted"] = run["status"] == "SUCCEEDED" and run["answer"]["citations"] == body["citations"]
            app.state.engine.dispose()
            restarted_provider = CountingProvider(DeepSeekProvider(settings=settings))
            restarted_app = create_app(database_url=url, chat_provider=restarted_provider)
            try:
                with TestClient(restarted_app) as restarted:
                    restarted.cookies.update(client.cookies)
                    restored = restarted.get(f"/api/runs/{body['run_id']}")
                    retry = restarted.post("/api/chat", headers={"Origin": "http://testserver",
                        CSRF_HEADER: restarted.cookies.get(CSRF_COOKIE)}, json=request_payload)
                    report["restart_run_status"] = restored.json()["run"]["status"]
                    report["idempotent_retry_status"] = retry.status_code
                    report["provider_calls_on_retry"] = restarted_provider.calls
                    report["restart_answer_matches"] = restored.json()["run"]["answer"]["answer"] == body["answer"]
            finally:
                restarted_app.state.engine.dispose()
            report["status"] = "PASS" if (report["first_call_persisted"] and report["restart_run_status"] == "SUCCEEDED"
                and report["idempotent_retry_status"] == 200 and report["provider_calls_on_retry"] == 0
                and report["restart_answer_matches"]) else "FAIL"
        finally:
            client.close()
    except Exception as exc:
        report["status"] = "FAIL"
        report["failure_type"] = type(exc).__name__
    finally:
        if app is not None:
            try:
                app.state.engine.dispose()
            except Exception:
                pass
        elif engine is not None:
            engine.dispose()
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        output = ROOT / "docs" / "probes" / f"m1.2-deepseek-live-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "record": str(output.relative_to(ROOT)),
                          "model": report.get("actual_model_id"), "provider_calls": report.get("provider_calls"),
                          "run_status": report.get("run_status"), "citation_count": len(report.get("citations", [])),
                          "secrets_recorded": False}, ensure_ascii=False, indent=2))
        if report["status"] != "PASS":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
